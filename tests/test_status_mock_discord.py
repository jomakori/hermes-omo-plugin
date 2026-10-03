"""A fake Discord adapter that drives the REAL tracker, notifier and transport.

Why this file exists
--------------------
``tests/test_status_transport.py`` asserts the *exact* adapter calls with a stub
whose signatures merely match. This harness goes one level deeper: it stands up
a mock Discord adapter that resolves the channel **the way the real adapter does**
and keeps a per-channel message store, then wires it behind the real
:class:`GatewayStatusTransport`, the real :class:`StatusNotifier` and the real
:class:`StatusTracker` with an injected clock. Nothing in the delivery path is a
stand-in — only the platform boundary and the clock are.

The one resolution rule that matters
------------------------------------
On the real discord adapter ``send`` resolves ``metadata['thread_id']`` **first**
(falling back to ``chat_id``), while ``edit_message`` / ``delete_message`` resolve
the **``chat_id`` argument itself** and ignore the metadata. A transport that
posted into a thread but then edited the parent chat would look fine against a
mock that resolved both the same way. So this mock encodes the asymmetry, and a
message edited into a channel that does not hold it fails with Discord's
``Unknown Message (10008)`` — the mock *can* fail, which is what lets the
discriminator test below reproduce the pre-fix bug.

Everything the harness needs lives in this one file; it imports no other test.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.gateway_status import GatewayStatusTransport  # noqa: E402
from orchestrator.status_message import DEFAULT_MAX_CHARS  # noqa: E402
from orchestrator.status_notifier import StatusNotifier  # noqa: E402
from orchestrator.status_tracker import (  # noqa: E402
    DEFAULT_PHRASE_INTERVAL,
    StatusTracker,
)
from orchestrator.worker_activity import make_activity_provider  # noqa: E402
from roster import AGENTS  # noqa: E402

GOAL = "shell parity across the fleet"
PARENT_CHAT = "42"
THREAD = "t7"
# The move path is opt-in now (the tracker default is 0.0), so the moving-struct
# test drives an explicit positive cadence instead of the default.
MOVE_INTERVAL = 5.0
_UNSET = object()


# ── the mock platform boundary ────────────────────────────────────────


@dataclass
class SendResult:
    """Mirrors ``gateway.platforms.base.SendResult`` (the fields the transport reads)."""

    success: bool
    message_id: str | None = None
    error: str | None = None


class MockDiscordAdapter:
    """A channel-aware fake of the gateway's discord adapter.

    ``send`` resolves ``metadata['thread_id']`` first; ``edit_message`` and
    ``delete_message`` resolve the ``chat_id`` argument. A message lives in
    exactly one channel, so an edit that names the wrong one cannot succeed.
    """

    def __init__(self, *, is_connected: bool = True) -> None:
        self.is_connected = is_connected
        self.channels: dict[str, dict[str, str]] = {}
        # Ordered ops: {"op", "channel", "message_id", "content"} — the audit trail.
        self.ops: list[dict[str, Any]] = []
        self._n = 0

    # -- the two resolutions the real adapter makes -------------------
    @staticmethod
    def _send_channel(chat_id: str, metadata: dict[str, Any] | None) -> str:
        thread = str((metadata or {}).get("thread_id") or "").strip()
        return thread or str(chat_id)

    @staticmethod
    def _message_channel(chat_id: str) -> str:
        # edit/delete take the channel from the argument; metadata is ignored.
        return str(chat_id)

    def _store(self, channel: str, text: str) -> str:
        self._n += 1
        message_id = f"m{self._n}"
        self.channels.setdefault(channel, {})[message_id] = text
        return message_id

    # -- adapter surface ---------------------------------------------
    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        channel = self._send_channel(chat_id, metadata)
        message_id = self._store(channel, content)
        self.ops.append({"op": "post", "channel": channel, "message_id": message_id, "content": content})
        return SendResult(success=True, message_id=message_id)

    async def edit_message(self, chat_id, message_id, content, *, finalize=False, metadata=None) -> SendResult:
        channel = self._message_channel(chat_id)
        held = self.channels.get(channel, {})
        if message_id not in held:
            self.ops.append({"op": "edit-gone", "channel": channel, "message_id": message_id, "content": content})
            return SendResult(success=False, error="Unknown Message (10008)")
        held[message_id] = content
        self.ops.append({"op": "edit", "channel": channel, "message_id": message_id, "content": content})
        return SendResult(success=True)

    async def delete_message(self, chat_id, message_id) -> bool:
        channel = self._message_channel(chat_id)
        held = self.channels.get(channel, {})
        if message_id not in held:
            return False
        del held[message_id]
        self.ops.append({"op": "delete", "channel": channel, "message_id": message_id, "content": None})
        return True

    # -- test affordances --------------------------------------------
    def seed(self, channel: str, message_id: str, text: str) -> None:
        """Pre-place a message (a restart adopting an id that already exists)."""
        self.channels.setdefault(channel, {})[message_id] = text

    def hard_delete(self, channel: str, message_id: str) -> None:
        """Simulate a user deleting the message out from under the tracker."""
        self.channels.get(channel, {}).pop(message_id, None)

    def is_alive(self, channel: str, message_id: str) -> bool:
        return message_id in self.channels.get(channel, {})

    def text_of(self, channel: str, message_id: str) -> str:
        return self.channels.get(channel, {}).get(message_id, "")

    def ops_of(self, op: str) -> list[dict[str, Any]]:
        return [entry for entry in self.ops if entry["op"] == op]

    @property
    def latest_text(self) -> str:
        for entry in reversed(self.ops):
            if entry["content"] is not None:
                return str(entry["content"])
        return ""


class MockRunner:
    """The gateway runner the transport resolves its adapter through."""

    def __init__(self, adapter: MockDiscordAdapter, *, loop: Any = None) -> None:
        self.adapters = {"discord": adapter}
        self._gateway_loop = loop

    def _authorization_adapter(self, platform, profile=None):
        return self.adapters.get(platform)


class FakeEngine:
    """Only the two hooks the notifier reads: the route and the id sink."""

    def __init__(self, route: dict[str, Any] | None) -> None:
        self._route = route
        self.recorded: list[tuple[str, str | None]] = []

    def run_route(self, run_id: str) -> dict[str, Any] | None:
        return self._route

    def note_status_message(self, run_id: str, message_id: str | None) -> None:
        self.recorded.append((run_id, message_id))


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Harness:
    """The real tracker + notifier + transport, over the mock adapter."""

    def __init__(
        self,
        *,
        route: Any = _UNSET,
        clock: FakeClock | None = None,
        **tracker_kwargs: Any,
    ) -> None:
        # `route=_UNSET` takes the thread route; `route=None` is the deliberate
        # no-route case (a session with nowhere to post).
        self.clock = clock or FakeClock()
        if route is _UNSET:
            route = {"platform": "discord", "chat_id": PARENT_CHAT, "thread_id": THREAD}
        self.route = route
        self.adapter = MockDiscordAdapter()
        self.runner = MockRunner(self.adapter)
        self.engine = FakeEngine(route)
        self.tracker = StatusTracker(clock=self.clock, **tracker_kwargs)
        self.transport: GatewayStatusTransport = GatewayStatusTransport(
            route,
            runner_resolver=lambda: self.runner,
            platform_key=lambda value: value,  # gateway.config is not importable in tests
            profile_resolver=lambda: "default",
        )
        self.notifier = StatusNotifier(
            None,
            engine=self.engine,
            tracker=self.tracker,
            transport_factory=lambda _route: self.transport,
        )

    def emit(self, event: str, payload: dict[str, Any]) -> str:
        """Apply one progress event through the real notifier; return the live text."""
        self.notifier.process(event, payload)
        return self.adapter.latest_text

    def tick(self) -> str:
        """Run one rotation tick through the real notifier; return the live text."""
        for action in self.tracker.tick():
            self.notifier.deliver(action)
        return self.adapter.latest_text

    def adopt(self, run_key: str, message_id: str) -> None:
        self.notifier.adopt(run_key, message_id)


# ── event payload builders (shaped like graph.py / engine.py) ─────────


def run_created(
    run_id: str, workers: list[dict[str, Any]], *, goal: str = GOAL, review: bool = False
) -> dict[str, Any]:
    return {"run_id": run_id, "goal": goal, "review": review, "workers": workers}


def worker(task_id: str, agent: str, task: str, *, model: str = "minimax-m3", **extra: Any) -> dict[str, Any]:
    spec = AGENTS.get(agent)
    payload = {
        "agent": agent,
        "task_id": task_id,
        "task": task,
        "display": spec.display if spec is not None else agent,
        "model": model,
        "run_ref": task_id,
    }
    payload.update(extra)
    return payload


def harness_payload(
    run_id: str, agent: str, task_id: str, task: str, *, model: str = "minimax-m3", goal: str = GOAL, **extra: Any
):
    base = {"run_id": run_id, "goal": goal}
    base.update(worker(task_id, agent, task, model=model, **extra))
    return base


def _twenty_four_workers() -> list[dict[str, Any]]:
    names = list(AGENTS)
    workers = []
    for i in range(24):
        agent = names[i % len(names)]
        workers.append(worker(f"t{i + 1:02d}", agent, f"worker {i + 1:02d} — {GOAL}"))
    return workers


# ── mock-fidelity tests (the mock is only useful if it can fail) ──────


def test_send_resolves_the_thread_from_metadata_before_the_chat_id():
    adapter = MockDiscordAdapter()
    result = asyncio.run(adapter.send(PARENT_CHAT, "hi", metadata={"thread_id": THREAD}))
    assert result.success
    assert adapter.is_alive(THREAD, result.message_id or "")
    assert not adapter.is_alive(PARENT_CHAT, result.message_id or "")


def test_edit_resolves_the_chat_id_argument_not_the_metadata():
    adapter = MockDiscordAdapter()
    message_id = adapter._store(PARENT_CHAT, "old")
    # A thread in the metadata must NOT redirect the edit: it names the channel.
    wrong = asyncio.run(adapter.edit_message(THREAD, message_id, "new", metadata={"thread_id": THREAD}))
    assert wrong.success is False and "10008" in (wrong.error or "")
    right = asyncio.run(adapter.edit_message(PARENT_CHAT, message_id, "new", metadata={"thread_id": THREAD}))
    assert right.success and adapter.text_of(PARENT_CHAT, message_id) == "new"


# ── coverage: the twelve required scenarios (plus the move) ───────────


def test_graph_fanout_posts_one_heading_with_a_condensed_label_per_worker():
    h = Harness()
    text = h.emit(
        "run_created",
        run_created(
            "omo_g",
            [
                worker("t1", "explore", "OKT-101 — map the repo"),
                worker(
                    "t2",
                    "hephaestus",
                    "OKT-102 — write the fix",
                ),
                worker("t3", "momus", "OKT-103 — review it"),
            ],
        ),
    )
    assert text.count("🏗️ omo") == 1
    assert text.splitlines()[0] == f"🏗️ omo · omo_g — {GOAL} · 3 workers"
    for label, role in (
        ("OKT-101", "explore · Repository Exploration"),
        ("OKT-102", "hephaestus · Deep Agent"),
        ("OKT-103", "momus · Plan Critic"),
    ):
        assert f"{label} · {role}" in text
    # review is off by default: no review row is ever inferred into existence.
    assert "- review" not in text
    assert len(h.adapter.ops_of("post")) == 1
    assert len(h.adapter.ops_of("edit")) == 0


def test_small_run_shape_and_line_count():
    h = Harness()
    running = h.emit(
        "worker_running", harness_payload("omo_1", "hephaestus", "omo_1", "fix the bug", goal="fix the bug")
    )
    assert running.splitlines() == [
        "🏗️ omo · omo_1 — fix the bug",
        "",
        "fix the bug · hephaestus · Deep Agent",
        "- run 🔁 omo_1 · minimax-m3",
        "  ↳ cycling: reading the code… (3s)",
    ]
    assert len(running.splitlines()) == 5

    # The terminal edit drops the activity line and carries the elapsed time.
    terminal = Harness()
    terminal.emit("worker_running", harness_payload("omo_1", "hephaestus", "omo_1", "fix the bug", goal="fix the bug"))
    done = terminal.emit(
        "worker_succeeded", harness_payload("omo_1", "hephaestus", "omo_1", "fix the bug", goal="fix the bug")
    )
    assert done.splitlines() == [
        "🏗️ omo · omo_1 — fix the bug",
        "",
        "fix the bug · hephaestus · Deep Agent",
        "- run ✅ 0s",
    ]
    assert len(done.splitlines()) == 4


def test_24_worker_run_keeps_every_worker_inside_the_discord_cap():
    h = Harness()
    workers = _twenty_four_workers()
    h.emit("run_created", run_created("omo_wide", workers))
    run_id = "omo_wide"
    for entry in workers:
        h.emit("task_started", harness_payload(run_id, entry["agent"], entry["task_id"], entry["task"]))
    final = ""
    for entry in workers:
        final = h.emit(
            "task_settled",
            {**harness_payload(run_id, entry["agent"], entry["task_id"], entry["task"]), "status": "succeeded"},
        )

    shown = sum(1 for entry in workers if f"worker {entry['task_id'][1:]} · " in final)
    assert shown == 24
    assert len(final) <= DEFAULT_MAX_CHARS
    # Every worker's status row survives: the collapse never fires on a plain run.
    assert final.count("- run ✅") == 24
    assert "…" not in final
    assert final.splitlines()[0] == f"🏗️ omo · {run_id} — {GOAL} · 24 workers"


def test_blocked_dependency_names_the_cause():
    h = Harness()
    h.emit(
        "run_created",
        run_created("omo_g", [worker("t1", "explore", "map the repo"), worker("t2", "hephaestus", "write the fix")]),
    )
    h.emit("task_started", harness_payload("omo_g", "explore", "t1", "map the repo"))
    h.emit("task_settled", {**harness_payload("omo_g", "explore", "t1", "map the repo"), "status": "failed"})
    text = h.emit(
        "task_blocked",
        {**harness_payload("omo_g", "hephaestus", "t2", "write the fix"), "cause": "waiting on t1"},
    )
    assert "- run ⛔ waiting on t1" in text


def test_cancelled_worker_row():
    h = Harness()
    h.emit("worker_running", harness_payload("omo_1", "hephaestus", "omo_1", "fix the bug", goal="fix the bug"))
    h.clock.advance(1.0)
    text = h.emit(
        "worker_cancelled", harness_payload("omo_1", "hephaestus", "omo_1", "fix the bug", goal="fix the bug")
    )
    assert "- run ⏸ cancelled" in text


def test_failed_worker_row_names_the_reason_not_the_task():
    h = Harness()
    h.emit("worker_running", harness_payload("omo_1", "hephaestus", "omo_1", "fix the bug", goal="fix the bug"))
    h.clock.advance(1.0)
    text = h.emit(
        "worker_failed",
        {
            **harness_payload("omo_1", "hephaestus", "omo_1", "fix the bug", goal="fix the bug"),
            "error": "402 credits exhausted",
        },
    )
    assert "- run ❌ 402 credits exhausted" in text
    assert "fix the bug · hephaestus · Deep Agent" in text


def test_deleted_message_reposts_a_fresh_struct():
    h = Harness()
    h.emit("worker_running", harness_payload("omo_1", "hephaestus", "omo_1", "fix the bug", goal="fix the bug"))
    message_id = h.adapter.ops_of("post")[0]["message_id"]
    # The user deletes the live message; the next edit hits Discord's 10008.
    h.adapter.hard_delete(THREAD, message_id)
    h.clock.advance(3.0)
    h.emit(
        "worker_running",
        harness_payload("omo_1", "hephaestus", "omo_1", "fix the bug", model="glm-5.3", goal="fix the bug"),
    )

    assert h.adapter.ops_of("edit-gone"), "the gone edit must have been attempted"
    assert ("omo_1", None) in h.engine.recorded  # the id was cleared
    # The next change posts a brand-new message instead of editing a dead id.
    h.clock.advance(3.0)
    h.emit(
        "worker_running",
        harness_payload("omo_1", "hephaestus", "omo_1", "fix the bug", model="claude-sonnet-5", goal="fix the bug"),
    )
    assert len(h.adapter.ops_of("post")) == 2


def test_restart_adopts_the_persisted_message_and_edits_it():
    h = Harness()
    h.adapter.seed(THREAD, "persisted-1", "🏗️ omo · omo_1 — old")
    h.adopt("omo_1", "persisted-1")
    text = h.emit("worker_running", harness_payload("omo_1", "hephaestus", "omo_1", "fix the bug", goal="fix the bug"))

    assert h.adapter.ops_of("post") == []  # never a second message
    edits = h.adapter.ops_of("edit")
    assert edits and edits[0]["message_id"] == "persisted-1"
    assert h.adapter.text_of(THREAD, "persisted-1") == text


def test_no_route_makes_no_transport_call():
    h = Harness(route=None)
    h.emit("worker_running", harness_payload("omo_1", "hephaestus", "omo_1", "fix the bug", goal="fix the bug"))
    assert h.adapter.ops == []


def test_idle_tick_and_duplicate_render_are_no_ops():
    h = Harness()
    first = h.emit("worker_running", harness_payload("omo_1", "hephaestus", "omo_1", "fix the bug", goal="fix the bug"))
    # An identical render is skipped: no edit, no transport call.
    assert (
        h.emit("worker_running", harness_payload("omo_1", "hephaestus", "omo_1", "fix the bug", goal="fix the bug"))
        == first
    )
    assert h.adapter.ops_of("edit") == []
    # One tick past the cadence rotates the line: the message stays alive.
    h.clock.advance(DEFAULT_PHRASE_INTERVAL + 0.1)
    rotated = h.tick()
    assert rotated != first
    assert len(h.adapter.ops_of("edit")) == 1
    # A tick inside the cadence says nothing, however quiet the run is.
    h.clock.advance(0.1)
    assert h.tick() == rotated
    assert len(h.adapter.ops_of("edit")) == 1
    # A terminal run never rotates again, however long the clock runs.
    h.emit("worker_succeeded", harness_payload("omo_1", "hephaestus", "omo_1", "fix the bug", goal="fix the bug"))
    edits_before = len(h.adapter.ops_of("edit"))
    h.clock.advance(10_000)
    assert h.tick() == h.adapter.latest_text
    assert len(h.adapter.ops_of("edit")) == edits_before


def test_review_problems_then_clean():
    h = Harness()
    h.emit("run_created", run_created("omo_g", [worker("t1", "hephaestus", "OKT-161 — write the code")], review=True))
    h.emit("task_started", harness_payload("omo_g", "hephaestus", "t1", "OKT-161 — write the code"))
    h.emit(
        "task_settled",
        {**harness_payload("omo_g", "hephaestus", "t1", "OKT-161 — write the code"), "status": "succeeded"},
    )
    reviewing = h.emit("task_started", harness_payload("omo_g", "momus", "t1:review", "review t1"))
    assert "- review 🔍 momus · omo_g · t1:review" in reviewing

    h.clock.advance(3.0)
    problems = h.emit(
        "review_verdict",
        {
            **harness_payload("omo_g", "hephaestus", "t1", "OKT-161 — write the code"),
            "verdict": "problems",
            "reviewer": "momus",
            "cycle": 1,
        },
    )
    assert "- review ✅ momus · problems (cycle 1)" in problems

    # Cycle 2 comes back clean and overwrites the row.
    h.emit("task_started", harness_payload("omo_g", "hephaestus", "t1", "OKT-161 — write the code"))
    h.clock.advance(3.0)
    clean = h.emit(
        "review_verdict",
        {
            **harness_payload("omo_g", "hephaestus", "t1", "OKT-161 — write the code"),
            "verdict": "pass",
            "reviewer": "momus",
            "cycle": 2,
        },
    )
    assert "- review ✅ momus · pass (cycle 2)" in clean


def test_moving_struct_posts_below_then_deletes_the_previous():
    h = Harness(clock=FakeClock(), min_edit_interval=0.0, move_interval=MOVE_INTERVAL)
    h.emit(
        "worker_running",
        harness_payload("omo_1", "hephaestus", "omo_1", "fix the bug", model="minimax-m3", goal="fix the bug"),
    )
    first_id = h.adapter.ops_of("post")[0]["message_id"]

    h.clock.advance(MOVE_INTERVAL + 0.1)
    moved = h.emit(
        "worker_running",
        harness_payload("omo_1", "hephaestus", "omo_1", "fix the bug", model="glm-5.3", goal="fix the bug"),
    )

    posts = h.adapter.ops_of("post")
    assert len(posts) == 2 and posts[1]["content"] == moved  # the fresh struct
    deleted = h.adapter.ops_of("delete")
    assert deleted and deleted[0]["message_id"] == first_id  # the previous one
    assert not h.adapter.is_alive(THREAD, first_id)
    assert h.adapter.is_alive(THREAD, posts[1]["message_id"])
    # The new id is the one persisted.
    assert h.engine.recorded[-1] == ("omo_1", posts[1]["message_id"])

    # A terminal state edits in place: it never moves the final struct away.
    h.clock.advance(MOVE_INTERVAL + 30)
    h.emit(
        "worker_succeeded",
        harness_payload("omo_1", "hephaestus", "omo_1", "fix the bug", model="glm-5.3", goal="fix the bug"),
    )
    assert len(h.adapter.ops_of("post")) == 2
    assert h.adapter.ops_of("edit"), "the terminal update is an in-place edit"


# ── the discriminator: a mock that cannot fail is worthless ───────────


def test_discriminator_mock_fails_when_the_edit_names_the_wrong_channel():
    """The mock itself must reject a cross-channel edit, or it proves nothing."""
    adapter = MockDiscordAdapter()
    import asyncio

    message_id = adapter._store(THREAD, "live")
    wrong = asyncio.run(adapter.edit_message(PARENT_CHAT, message_id, "x"))
    assert wrong.success is False and "10008" in (wrong.error or "")


def test_discriminator_pre_fix_edit_that_names_the_parent_chat_is_caught():
    """Reproduce the pre-fix bug and show the harness detects it.

    The shipped transport passes ``_target`` (the thread) to ``edit_message``. The
    pre-fix transport passed the parent ``chat_id``. Against the mock's real
    resolution the wrong channel never holds the message, so the edit comes back
    ``Unknown Message (10008)`` and the notifier clears the tracked id — the bug
    surfaces instead of silently editing into the void.
    """

    class PreFixTransport(GatewayStatusTransport):
        @property
        def _target(self) -> str:  # the pre-fix behaviour
            return self.chat_id

    h = Harness(clock=FakeClock(), min_edit_interval=0.0)
    h.transport = PreFixTransport(
        h.route,
        runner_resolver=lambda: h.runner,
        platform_key=lambda value: value,
        profile_resolver=lambda: "default",
    )
    h.notifier = StatusNotifier(None, engine=h.engine, tracker=h.tracker, transport_factory=lambda _route: h.transport)

    # Post goes into the thread (send resolves metadata['thread_id']).
    h.emit("worker_running", harness_payload("omo_1", "hephaestus", "omo_1", "fix the bug", goal="fix the bug"))
    assert h.adapter.ops_of("post")[0]["channel"] == THREAD

    # The pre-fix edit names the parent chat; the mock has nothing there.
    h.clock.advance(3.0)
    h.emit(
        "worker_running",
        harness_payload("omo_1", "hephaestus", "omo_1", "fix the bug", model="glm-5.3", goal="fix the bug"),
    )

    gone = h.adapter.ops_of("edit-gone")
    assert gone and gone[0]["channel"] == PARENT_CHAT
    assert h.engine.recorded[-1] == ("omo_1", None)  # detected: the id was cleared


def test_fixed_transport_edits_the_thread_it_posted_to():
    """The shipped behaviour, for contrast: post and edit land in the same channel."""
    h = Harness(clock=FakeClock(), min_edit_interval=0.0)
    h.emit("worker_running", harness_payload("omo_1", "hephaestus", "omo_1", "fix the bug", goal="fix the bug"))
    h.clock.advance(3.0)
    h.emit(
        "worker_running",
        harness_payload("omo_1", "hephaestus", "omo_1", "fix the bug", model="glm-5.3", goal="fix the bug"),
    )

    assert [op["channel"] for op in h.adapter.ops_of("post")] == [THREAD]
    edits = h.adapter.ops_of("edit")
    assert edits and all(op["channel"] == THREAD for op in edits)
    assert "· glm-5.3" in h.adapter.text_of(THREAD, edits[-1]["message_id"])


# ── the activity line: a real tool call, a real registry emoji ────────

# The host registry's values for the tools a worker actually calls (enumerated
# from ``/opt/hermes/tools/*.py``; ``registry.get_emoji`` cannot be imported in
# this venv, so the reader is exercised with the same values it returns).
REGISTRY_EMOJI = {"read_file": "📖", "write_file": "✍️", "patch": "🔧", "search_files": "🔎", "terminal": "💻"}


def _profile(tmp_path, agent: str, calls: list[dict[str, Any]]) -> Any:
    root = tmp_path / "profiles"
    db = root / agent / "state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sessions (id TEXT, started_at REAL)")
    conn.execute("CREATE TABLE messages (session_id TEXT, tool_name TEXT, tool_calls TEXT, timestamp REAL)")
    conn.execute("INSERT INTO sessions VALUES ('s1', 1.0)")
    conn.execute(
        "INSERT INTO messages VALUES ('s1', NULL, ?, 2.0)",
        (json.dumps(calls),),
    )
    conn.commit()
    conn.close()
    return root


def test_activity_line_carries_the_real_tool_call_and_its_registry_emoji(tmp_path):
    calls = [{"function": {"name": "read_file", "arguments": {"path": "orchestrator/status_message.py"}}}]
    root = _profile(tmp_path, "hephaestus", calls)
    provider = make_activity_provider(
        profiles_dir=root, emoji_resolver=lambda name, default: REGISTRY_EMOJI.get(name, default)
    )
    h = Harness(activity_provider=provider)

    text = h.emit(
        "worker_running",
        harness_payload("omo_1", "hephaestus", "omo_1", "fix the bug", goal="fix the bug", activity_session="s1"),
    )
    assert "  ↳ 📖 read_file orchestrator/status_message.py" in text
    # The canned rotation phrase only stands in when nothing real is readable.
    assert "cycling:" not in text


# ── the numbers the format judgement reports ──────────────────────────


def twenty_four_worker_render() -> dict[str, Any]:
    """Drive the 24-worker case end to end and return the measured shape."""
    h = Harness()
    workers = _twenty_four_workers()
    h.emit("run_created", run_created("omo_304bf8e5", workers))
    for entry in workers:
        h.emit("task_started", harness_payload("omo_304bf8e5", entry["agent"], entry["task_id"], entry["task"]))
    final = ""
    for entry in workers:
        final = h.emit(
            "task_settled",
            {
                **harness_payload("omo_304bf8e5", entry["agent"], entry["task_id"], entry["task"]),
                "status": "succeeded",
            },
        )
    return {
        "chars": len(final),
        "lines": len(final.splitlines()),
        "workers_shown": sum(1 for entry in workers if f"worker {entry['task_id'][1:]} · " in final),
        "run_rows_shown": final.count("- run ✅"),
        "text": final,
    }


if __name__ == "__main__":  # pragma: no cover - a hand-run judgement, not a test
    measured = twenty_four_worker_render()
    print(
        f"24-worker render: {measured['chars']} chars, {measured['lines']} lines, "
        f"{measured['workers_shown']}/24 workers, {measured['run_rows_shown']} run rows"
    )
    print(measured["text"])
