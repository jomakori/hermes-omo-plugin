"""Turn OMO progress events into ONE status message per run.

This is the tested half of the live status feature: it owns the run-state
machine, the in-place-edit throttle, the no-op dedupe and the rotating phrase,
and it touches neither Discord nor the clock — both are injected. The threaded
delivery shell lives in ``status_notifier`` so the rules below can be exercised
with a fake clock and no gateway in the loop.

Rules encoded here:

* one message per ``run_id`` — the first render posts, every later one edits;
* an edit is skipped when the rendered text is unchanged (no-op dedupe) and
  otherwise throttled to at least ``min_edit_interval`` seconds apart;
* a throttled edit is remembered as *pending* and flushed on the next tick, so
  a fast burst of transitions never loses the last state;
* the running phase carries a rotating ``↳ cycling:`` phrase, advanced on the
  rotation timer (``status_rotation``);
* a review is folded into the producing worker's block: the reviewer's own run
  events name the ``review`` row of that block, never a block of their own;
* a terminal state always forces a final edit, ignoring the throttle.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from orchestrator.status_message import DEFAULT_MAX_CHARS, PHASES, render_status
from orchestrator.status_rotation import ROTATE_INTERVAL_SECONDS, cycling_phrase

DEFAULT_MIN_EDIT_INTERVAL = 2.0
# Kept as the phrase-timer default; the interval itself lives with the phrases.
DEFAULT_PHRASE_INTERVAL = ROTATE_INTERVAL_SECONDS

# Run-phase statuses that mean "this worker is finished".
_SETTLED_RUN = ("done", "failed", "cancelled", "blocked", "interrupted")

_SUCCEEDED = {"SUCCEEDED", "SUCCESS", "DONE", "PASS", "PASSED", "OK"}
_CANCELLED = {"CANCELLED", "CANCELED"}

_REVIEW_SUFFIX = ":review"


@dataclass
class Action:
    """A delivery instruction for the notifier: post once, then edit in place."""

    kind: str  # "post" | "edit"
    run_key: str
    text: str
    message_id: str | None = None


@dataclass
class _Phase:
    name: str
    status: str = "pending"
    run_id: str = ""
    task_id: str = ""
    detail: str = ""
    duration: str = ""
    cause: str = ""
    reviewer: str = ""
    verdict: str = ""
    cycle: int = 0


@dataclass
class _Block:
    key: str = ""
    agent: str = "?"
    process: str = ""
    phases: list[_Phase] = field(default_factory=list)
    started_at: float | None = None

    def phase(self, name: str) -> _Phase:
        for phase in self.phases:
            if phase.name == name:
                return phase
        phase = _Phase(name)
        self.phases.append(phase)
        return phase

    def run_status(self) -> str:
        return self.phase("run").status


@dataclass
class _RunState:
    run_key: str
    blocks: list[_Block] = field(default_factory=list)
    process: str = ""
    message_id: str | None = None
    last_text: str = ""
    last_edit_at: float = float("-inf")
    phrase_index: int = 0
    phrase_at: float = float("-inf")
    pending: bool = False
    terminal: bool = False


class StatusTracker:
    """Pure run-state machine: apply an event or tick the clock, get an Action."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        min_edit_interval: float = DEFAULT_MIN_EDIT_INTERVAL,
        phrase_interval: float = DEFAULT_PHRASE_INTERVAL,
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> None:
        self._clock = clock
        self.min_edit_interval = max(0.0, float(min_edit_interval))
        self.phrase_interval = max(0.0, float(phrase_interval))
        self.max_chars = max_chars
        self._runs: dict[str, _RunState] = {}

    # ── public surface ────────────────────────────────────────────────
    def apply(self, event: str, payload: dict[str, Any] | None = None) -> Action | None:
        """Fold one progress event into the run state and return what to deliver."""
        payload = payload or {}
        run_key = str(payload.get("run_id") or "")
        if not run_key:
            return None
        now = self._clock()
        state = self._state_for(run_key)
        # A single-worker dispatch has no `run_created`; take the headline from the
        # first event that carries one so the header is never bare.
        if not state.process and payload.get("goal"):
            state.process = str(payload.get("goal") or "").strip()
        self._reduce(state, str(event), payload, now)
        if self._settled(state):
            state.terminal = True
        return self._maybe_emit(state, now, force=state.terminal)

    def tick(self) -> list[Action]:
        """Advance the rotation clock and flush anything throttled or rotated."""
        now = self._clock()
        actions: list[Action] = []
        for state in self._runs.values():
            if state.terminal:
                continue
            rotated = False
            if self._has_current(state) and (now - state.phrase_at) >= self.phrase_interval:
                state.phrase_at = now
                state.phrase_index += 1
                rotated = True
            if rotated or state.pending:
                action = self._maybe_emit(state, now, force=False)
                if action is not None:
                    actions.append(action)
        return actions

    def note_message_id(self, run_key: str, message_id: str | None) -> None:
        """Record the id a post returned, or clear it when the message is gone.

        Clearing makes the next emit a fresh post: a gateway restart (or a
        deleted message) adopts the stored id, and only a failed edit falls back
        to posting anew.
        """
        state = self._runs.get(run_key)
        if state is None:
            return
        state.message_id = message_id or None

    def adopt(self, run_key: str, message_id: str | None) -> None:
        """Seed a persisted message id so a restart edits instead of re-posting."""
        if not message_id:
            return
        state = self._runs.get(run_key)
        if state is None:
            state = _RunState(run_key=run_key)
            self._runs[run_key] = state
        state.message_id = message_id

    # ── state construction ────────────────────────────────────────────
    def _state_for(self, run_key: str) -> _RunState:
        state = self._runs.get(run_key)
        if state is None:
            state = _RunState(run_key=run_key)
            self._runs[run_key] = state
        return state

    def _new_block(self, agent: Any, task_id: Any, process: str = "") -> _Block:
        name = str(agent or "?").strip() or "?"
        task = str(task_id or "").strip()
        key = task or f"agent:{name}"
        return _Block(key=key, agent=name, process=process, phases=[_Phase(p) for p in PHASES])

    def _block(self, state: _RunState, agent: Any, task_id: Any, process: str = "") -> _Block:
        task = str(task_id or "").strip()
        key = task or f"agent:{str(agent or '?').strip() or '?'}"
        for block in state.blocks:
            if block.key == key:
                if process and not block.process:
                    block.process = process
                return block
        block = self._new_block(agent, task_id, process or state.process)
        state.blocks.append(block)
        return block

    @staticmethod
    def _producer_task(task_id: Any) -> str | None:
        """The producing task a reviewer ref belongs to, or None if not a review."""
        task = str(task_id or "").strip()
        if not task.endswith(_REVIEW_SUFFIX):
            return None
        return task[: -len(_REVIEW_SUFFIX)]

    def _find(self, state: _RunState, task_id: Any) -> _Block | None:
        key = str(task_id or "").strip()
        for block in state.blocks:
            if block.key == key:
                return block
        return None

    def _has_current(self, state: _RunState) -> bool:
        return any(block.run_status() == "current" for block in state.blocks)

    def _settled(self, state: _RunState) -> bool:
        if not state.blocks:
            return False
        return all(block.run_status() in _SETTLED_RUN for block in state.blocks)

    # ── rendering ─────────────────────────────────────────────────────
    def _render(self, state: _RunState) -> str:
        blocks: list[dict[str, Any]] = []
        for index, block in enumerate(state.blocks):
            phases: list[dict[str, Any]] = []
            for phase in block.phases:
                rendered: dict[str, Any] = {"name": phase.name, "status": phase.status}
                if phase.name == "run":
                    rendered["run_id"] = phase.run_id
                    rendered["task_id"] = phase.task_id
                    rendered["detail"] = phase.detail
                    rendered["duration"] = phase.duration
                    rendered["cause"] = phase.cause
                    if phase.status == "current":
                        rendered["phrase"] = cycling_phrase("run", state.phrase_index + index)
                        rendered["rotate_seconds"] = int(self.phrase_interval)
                elif phase.name == "review":
                    rendered["reviewer"] = phase.reviewer
                    rendered["run_id"] = phase.run_id
                    rendered["task_id"] = phase.task_id
                    rendered["verdict"] = phase.verdict
                    rendered["cycle"] = phase.cycle
                    rendered["cause"] = phase.cause
                phases.append(rendered)
            blocks.append({"agent": block.agent, "process": block.process, "phases": phases})
        return render_status(blocks, max_chars=self.max_chars)

    def _maybe_emit(self, state: _RunState, now: float, *, force: bool) -> Action | None:
        text = self._render(state)
        if not state.message_id:
            state.last_text = text
            state.last_edit_at = now
            state.pending = False
            return Action("post", state.run_key, text)
        if text == state.last_text:
            state.pending = False
            return None
        if not force and (now - state.last_edit_at) < self.min_edit_interval:
            state.pending = True
            return None
        state.last_text = text
        state.last_edit_at = now
        state.pending = False
        return Action("edit", state.run_key, text, state.message_id)

    # ── event reduction ───────────────────────────────────────────────
    def _reduce(self, state: _RunState, event: str, payload: dict[str, Any], now: float) -> None:
        handler = _HANDLERS.get(event)
        if handler is not None:
            handler(self, state, payload, now)

    def _start(self, block: _Block, payload: dict[str, Any], now: float) -> None:
        block.phase("dispatch").status = "done"
        run = block.phase("run")
        run.status = "current"
        run.run_id = str(payload.get("run_id") or "")
        run.task_id = str(payload.get("task_id") or payload.get("run_ref") or "")
        run.detail = str(payload.get("task") or payload.get("goal") or block.process or "").strip()
        if block.started_at is None:
            block.started_at = now

    def _finish(self, block: _Block, status: str, payload: dict[str, Any], now: float) -> None:
        block.phase("dispatch").status = "done"
        run = block.phase("run")
        run.status = status
        if status == "done":
            run.duration = _format_duration(block.started_at, now)
        else:
            run.cause = str(payload.get("cause") or payload.get("error") or "").strip()


def _format_duration(started_at: float | None, now: float) -> str:
    if not started_at:
        return ""
    seconds = max(0, int(now - started_at))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h{minutes % 60}m"


# ── event handlers (kept as free functions so the mapping is one table) ──
def _h_run_created(tracker: StatusTracker, state: _RunState, payload: dict[str, Any], now: float) -> None:
    state.process = str(payload.get("goal") or payload.get("process") or state.process or "").strip()
    workers = payload.get("workers") or []
    if workers:
        for entry in workers:
            if not isinstance(entry, dict):
                continue
            block = tracker._block(state, entry.get("agent"), entry.get("task_id"))
            # The block's headline is its own task when the run declared one;
            # otherwise the run's goal stands in.
            block.process = str(entry.get("task") or state.process or "").strip()
        return
    agents = payload.get("agents") or ([payload.get("agent")] if payload.get("agent") else [])
    for name in agents:
        tracker._block(state, name, payload.get("task_id"))


def _h_task_started(tracker: StatusTracker, state: _RunState, payload: dict[str, Any], now: float) -> None:
    parent = tracker._producer_task(payload.get("task_id"))
    if parent is not None:
        _reviewer_running(tracker, state, payload, parent)
        return
    tracker._start(tracker._block(state, payload.get("agent"), payload.get("task_id")), payload, now)


def _h_task_blocked(tracker: StatusTracker, state: _RunState, payload: dict[str, Any], now: float) -> None:
    if tracker._producer_task(payload.get("task_id")) is not None:
        return
    block = tracker._block(state, payload.get("agent"), payload.get("task_id"))
    block.phase("dispatch").status = "done"
    run = block.phase("run")
    run.status = "blocked"
    run.run_id = str(payload.get("run_id") or "")
    run.task_id = str(payload.get("task_id") or "")
    run.cause = str(payload.get("cause") or payload.get("error") or "blocked").strip()


def _h_task_settled(tracker: StatusTracker, state: _RunState, payload: dict[str, Any], now: float) -> None:
    if tracker._producer_task(payload.get("task_id")) is not None:
        return
    tracker._finish(
        tracker._block(state, payload.get("agent"), payload.get("task_id")),
        _status_from(payload.get("status")),
        payload,
        now,
    )


def _h_worker_interrupted(tracker: StatusTracker, state: _RunState, payload: dict[str, Any], now: float) -> None:
    if tracker._producer_task(payload.get("task_id")) is not None:
        return
    block = tracker._block(state, payload.get("agent"), payload.get("task_id"))
    block.phase("dispatch").status = "done"
    run = block.phase("run")
    run.status = "interrupted"
    run.task_id = str(payload.get("task_id") or "")
    run.cause = str(payload.get("cause") or "gateway restart").strip()


def _h_review_verdict(tracker: StatusTracker, state: _RunState, payload: dict[str, Any], now: float) -> None:
    block = tracker._block(state, payload.get("agent"), payload.get("task_id"))
    block.phase("dispatch").status = "done"
    review = block.phase("review")
    review.status = "done"
    review.reviewer = str(payload.get("reviewer") or "momus").strip()
    review.verdict = str(payload.get("verdict") or "").strip()
    review.cycle = int(payload.get("cycle") or 0)


def _h_reviewer_failed(tracker: StatusTracker, state: _RunState, payload: dict[str, Any], now: float) -> None:
    parent = tracker._producer_task(payload.get("task_id"))
    block = tracker._find(state, parent) if parent is not None else None
    if block is None:
        block = tracker._block(state, payload.get("agent"), parent or payload.get("task_id"))
    review = block.phase("review")
    review.status = "failed"
    review.reviewer = str(payload.get("reviewer") or payload.get("agent") or "").strip()
    review.verdict = str(payload.get("verdict") or "reviewer failed").strip()


def _h_run_finished(tracker: StatusTracker, state: _RunState, payload: dict[str, Any], now: float) -> None:
    overall = _status_from(payload.get("status"))
    for block in state.blocks:
        block.phase("dispatch").status = "done"
        run = block.phase("run")
        if run.status in ("pending", "current"):
            run.status = "done" if overall == "done" else overall
    state.terminal = True


def _reviewer_running(tracker: StatusTracker, state: _RunState, payload: dict[str, Any], parent: str) -> None:
    block = tracker._find(state, parent)
    if block is None:
        # No block knows this producer (a review of a worker the message never
        # saw): nothing to fold the row into, so it is dropped rather than shown
        # as a second agent.
        return
    review = block.phase("review")
    review.status = "reviewing"
    review.reviewer = str(payload.get("agent") or "").strip()
    review.run_id = str(payload.get("run_id") or "")
    review.task_id = str(payload.get("task_id") or "")


def _status_from(raw: Any) -> str:
    value = str(raw or "").strip().upper()
    if value in _SUCCEEDED:
        return "done"
    if value in _CANCELLED:
        return "cancelled"
    return "failed"


def _worker_handler(status: str):
    def handler(tracker: StatusTracker, state: _RunState, payload: dict[str, Any], now: float) -> None:
        parent = tracker._producer_task(payload.get("task_id"))
        if parent is not None:
            # A reviewer's run names the producer's review row; its terminal event
            # says nothing new, because the row is settled by `review_verdict` /
            # `reviewer_failed`, not by the review worker finishing.
            if status == "current":
                _reviewer_running(tracker, state, payload, parent)
            return
        block = tracker._block(state, payload.get("agent"), payload.get("task_id"))
        if status == "current":
            tracker._start(block, payload, now)
        else:
            tracker._finish(block, status, payload, now)

    return handler


_HANDLERS: dict[str, Callable[[StatusTracker, _RunState, dict[str, Any], float], None]] = {
    "run_created": _h_run_created,
    "task_started": _h_task_started,
    "task_blocked": _h_task_blocked,
    "task_settled": _h_task_settled,
    "worker_interrupted": _h_worker_interrupted,
    "review_verdict": _h_review_verdict,
    "reviewer_failed": _h_reviewer_failed,
    "run_finished": _h_run_finished,
    "worker_running": _worker_handler("current"),
    "worker_succeeded": _worker_handler("done"),
    "worker_failed": _worker_handler("failed"),
    "worker_cancelled": _worker_handler("cancelled"),
    "worker_blocked": _worker_handler("blocked"),
    "worker_done": _worker_handler("done"),
}


__all__ = [
    "DEFAULT_MIN_EDIT_INTERVAL",
    "DEFAULT_PHRASE_INTERVAL",
    "ROTATE_INTERVAL_SECONDS",
    "Action",
    "StatusTracker",
]
