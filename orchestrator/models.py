"""Shared run models for the OMO engine and its task-graph layer.

Split out of ``engine.py`` so the graph scheduler can build runs and workers
without importing the engine that drives them.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from roster import agent

PENDING = "PENDING"
RUNNING = "RUNNING"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"
BLOCKED = "BLOCKED"
RETRYING = "RETRYING"
# A worker whose process is gone: recorded from disk after a restart, never live.
INTERRUPTED = "INTERRUPTED"

# Statuses that only a live process can hold; anything else is a finished record.
UNFINISHED = (PENDING, RUNNING, RETRYING)


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_optional_str(value: Any) -> str | None:
    """A durable string field: absent, blank and null all read as no value."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


# The pieces of a worker's result the read path surfaces, named to match the
# claim_boundary field kinds (see ``orchestrator.boundary``): the worker's own
# summary and structured payload, plus the host-observed terminal state and usage.
_RESULT_FIELDS = (
    "summary",
    "structured_payload",
    "usage_metadata",
    "tool_execution_summary",
    "terminal_state",
    "error_message",
)


# A succeeded worker's summary reads like a report of work the host watched
# happen; it is the worker narrating itself (see ``orchestrator.boundary``:
# `result.summary` is SELF_REPORTED). Terminal success is decided on the host's
# `terminal_state` alone and never cross-checks the text, so the payload says in
# the value itself that nobody verified it.
UNVERIFIED_CLAIM_PREFIX = "[unverified worker claim] "


def result_payload(result: Any, status: str | None = None) -> Any:
    """The readable form of a finished worker's result for a status/tree payload.

    The registry holds the host's result object as-is; a caller polling a
    background run needs its content, not an opaque repr. A dict result passes
    through untouched; a host object is narrowed to the fields a claim_boundary
    can name, so a lost result is never mistaken for a run that produced nothing.

    ``status`` is the worker's own status. On a SUCCEEDED worker the summary is
    prefixed with ``UNVERIFIED_CLAIM_PREFIX``: success says the process exited
    well, not that the narrative is true. A failed or cancelled run already reads
    as unproven, so its summary is left alone.
    """
    if result is None:
        return None
    if isinstance(result, dict):
        return result
    fields: dict[str, Any] = {}
    for name in _RESULT_FIELDS:
        value = getattr(result, name, None)
        if value is None:
            continue
        # An enum (e.g. a terminal state) renders as its name, not ``<Enum.x: 1>``.
        value = getattr(value, "name", value)
        if name == "summary" and status == SUCCEEDED and isinstance(value, str):
            value = f"{UNVERIFIED_CLAIM_PREFIX}{value}"
        fields[name] = value
    return fields or {"value": str(result)}


def unverified_marker(verification: dict[str, Any] | None) -> dict[str, Any]:
    """The explicit marker a result carries when a claimed artifact failed to verify.

    A worker's SUCCEEDED says its process exited cleanly; it says nothing about
    whether the branch or PR its summary names was ever pushed (see
    ``orchestrator.artifact_verify``). When a claim was made and could not be
    confirmed, the result says so in its own fields rather than reading as a
    plain success. A worker that claimed nothing is never marked: there is no
    claim to be wrong about.
    """
    if not verification or not verification.get("verifiable"):
        return {}
    if verification.get("verified"):
        return {}
    return {"verified": False, "verification": dict(verification)}


@dataclass
class Worker:
    run_id: str
    agent_name: str
    task: str
    chain: tuple[str, ...]
    # Optional, caller-declared: the work's own short name (e.g. "Add a
    # 'provider unusable' hop reason"). When present it is what the live
    # status message's label and cycling line name instead of a derived
    # fragment of the raw task text.
    title: str = ""
    model: str | None = None
    status: str = PENDING
    handle: Any = None
    result: Any = None
    error: str = ""
    cancel_requested: bool = False
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    # Task-graph fields. A plain dispatch leaves them empty.
    task_id: str = ""
    depends_on: tuple[str, ...] = ()
    # A reconciler (a coordinator that reports on the work) has to run even when
    # one of the tasks it reports on failed: blocking it hides the failure from
    # the only task that was asked to name it.
    runs_on_failure: bool = False
    parent_id: str = ""
    review_cycles: int = 0
    review_verdict: str = ""
    hop_history: list[dict[str, str]] = field(default_factory=list)
    # The child session this worker runs in, resolved at launch (the launch handle
    # names no session, so the profile's newest session at launch is pinned here).
    # The status line reads the worker's live tool call from it.
    activity_session: str | None = None
    # When the child last wrote a message (epoch seconds), as the watchdog last saw
    # it. Zero when unknown: no session, no store, or a host that keeps no transcript.
    last_activity_at: float = 0.0
    # The artifact-verification report for a finished worker (see
    # ``orchestrator.artifact_verify``): whether it claimed a branch/PR/commit at
    # all, and whether each one was actually found. Empty until the worker
    # finishes, and left empty for a worker that claimed nothing.
    verification: dict[str, Any] = field(default_factory=dict)

    def as_row(self) -> dict[str, Any]:
        spec = agent(self.agent_name)
        row: dict[str, Any] = {
            "agent": spec.display if spec else self.agent_name,
            "status": self.status,
            "task": self.task,
            "model": self.model,
            "run_id": self.run_id,
        }
        if self.title:
            row["title"] = self.title
        if self.task_id:
            row["task_id"] = self.task_id
        # The read path surfaces the result a finished worker produced, so a caller
        # polling a background run can read it back. It is deliberately not in
        # `to_dict`: the durable record survives a restart, a result object does not.
        if self.result is not None:
            payload = result_payload(self.result, status=self.status)
            marker = unverified_marker(self.verification)
            if marker and isinstance(payload, dict):
                # A claimed branch/PR nobody could find must not read as a plain
                # success: the marker travels with the result the caller reads.
                payload = {**payload, **marker}
            row["result"] = payload
        if self.depends_on:
            row["depends_on"] = list(self.depends_on)
        if self.parent_id:
            row["parent"] = self.parent_id
        if self.review_cycles:
            row["review_cycles"] = self.review_cycles
        if self.review_verdict:
            row["review_verdict"] = self.review_verdict
        if self.hop_history:
            row["hop_history"] = self.hop_history
        return row

    def to_dict(self) -> dict[str, Any]:
        """The durable fields only: a handle and a result die with the process."""
        return {
            "run_id": self.run_id,
            "agent_name": self.agent_name,
            "task": self.task,
            "chain": list(self.chain),
            "model": self.model,
            "status": self.status,
            "error": self.error,
            "task_id": self.task_id,
            "depends_on": list(self.depends_on),
            "parent_id": self.parent_id,
            "review_cycles": self.review_cycles,
            "review_verdict": self.review_verdict,
            "hop_history": [hop for hop in self.hop_history if isinstance(hop, dict)],
            "activity_session": self.activity_session,
            "last_activity_at": self.last_activity_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Worker | None:
        run_id = str(payload.get("run_id") or "")
        if not run_id:
            return None
        finished = payload.get("finished_at")
        return cls(
            run_id=run_id,
            agent_name=str(payload.get("agent_name") or ""),
            task=str(payload.get("task") or ""),
            chain=tuple(str(model) for model in payload.get("chain") or ()),
            model=payload.get("model") or None,
            status=str(payload.get("status") or PENDING),
            error=str(payload.get("error") or ""),
            task_id=str(payload.get("task_id") or ""),
            depends_on=tuple(str(dep) for dep in payload.get("depends_on") or ()),
            parent_id=str(payload.get("parent_id") or ""),
            review_cycles=as_int(payload.get("review_cycles"), 0),
            review_verdict=str(payload.get("review_verdict") or ""),
            hop_history=[hop for hop in payload.get("hop_history") or () if isinstance(hop, dict)],
            activity_session=as_optional_str(payload.get("activity_session")),
            last_activity_at=as_float(payload.get("last_activity_at")),
            started_at=as_float(payload.get("started_at")),
            finished_at=None if finished is None else as_float(finished),
        )


@dataclass
class Run:
    run_id: str
    goal: str
    workers: list[Worker] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    # Set when the run was adopted from disk with work still unfinished: the
    # record is real, the process behind it is not.
    recovered: bool = False
    # The Hermes session that dispatched this run, so one session cannot read or
    # cancel another's work. `None` on a record written before attribution existed,
    # or by a host with no session bridge — those belong to nobody.
    session_id: str | None = None
    # The gateway session_key the caller's turn was bound to, captured at dispatch
    # time for the same reason as session_id: a worker thread has no ContextVars to
    # resolve it from later. This is what engine._wake_caller passes to
    # ctx.inject_message so a background run can hand its result back into the
    # conversation that dispatched it. `None` when no bridge could answer, or the
    # record predates this field — such a run simply cannot be woken.
    session_key: str | None = None
    # Whether this run was dispatched with background=True. Only a background run
    # is woken on completion: a synchronous dispatch already hands its result back
    # as the tool call's own return value, so waking it too would double-deliver.
    background: bool = False
    # Where the run's live status message lands. Captured at dispatch because a
    # worker thread has no session ContextVars to resolve the route from.
    platform: str | None = None
    chat_id: str | None = None
    thread_id: str | None = None
    # The one message the run edits in place. Persisted so a gateway restart keeps
    # editing the same message instead of posting a second one.
    status_message_id: str | None = None
    # The same message's pin. Persisted so an unpin (or the pin-cap eviction)
    # still knows which pin is ours after a restart, and only touches our own.
    status_pinned_id: str | None = None
    # Whether engine._wake_caller has already fired for this run. Persisted so a
    # restart that re-adopts an already-woken run from disk never wakes it twice.
    woken: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "goal": self.goal,
            "created_at": self.created_at,
            "recovered": self.recovered,
            "session_id": self.session_id,
            "session_key": self.session_key,
            "background": self.background,
            "platform": self.platform,
            "chat_id": self.chat_id,
            "thread_id": self.thread_id,
            "status_message_id": self.status_message_id,
            "status_pinned_id": self.status_pinned_id,
            "woken": self.woken,
            "workers": [worker.to_dict() for worker in self.workers],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Run | None:
        run_id = str(payload.get("run_id") or "")
        if not run_id:
            return None
        workers = [
            worker
            for worker in (Worker.from_dict(e) for e in payload.get("workers") or () if isinstance(e, dict))
            if worker
        ]
        return cls(
            run_id=run_id,
            goal=str(payload.get("goal") or ""),
            workers=workers,
            created_at=as_float(payload.get("created_at")),
            recovered=bool(payload.get("recovered")),
            # Absent on a record written before attribution: reads as unattributed
            # rather than as a read error, and stays hidden from a real session.
            session_id=as_optional_str(payload.get("session_id")),
            session_key=as_optional_str(payload.get("session_key")),
            background=bool(payload.get("background")),
            platform=as_optional_str(payload.get("platform")),
            chat_id=as_optional_str(payload.get("chat_id")),
            thread_id=as_optional_str(payload.get("thread_id")),
            status_message_id=as_optional_str(payload.get("status_message_id")),
            status_pinned_id=as_optional_str(payload.get("status_pinned_id")),
            woken=bool(payload.get("woken")),
        )

    def tree(self) -> str:
        lines = ["Hermes", f"└── omo run {self.run_id}: {self.goal[:60]}"]
        for index, worker in enumerate(self.workers):
            branch = "└──" if index == len(self.workers) - 1 else "├──"
            row = worker.as_row()
            suffix = f" · depends: {','.join(row['depends_on'])}" if row.get("depends_on") else ""
            lines.append(f"    {branch} {row['agent']} · {row['status']} · {row['model'] or '-'}{suffix}")
        return "\n".join(lines)
