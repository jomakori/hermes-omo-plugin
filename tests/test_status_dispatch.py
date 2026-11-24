"""Behavioural tests for the dispatch-time status message (OMR-23).

The message must exist before the worker has done anything: one post emitted
while the run is being accepted, a coordinator row the heartbeat can rotate in
place, and every later transition carried as an edit of that same message id.
"""

from __future__ import annotations

from typing import Any

from orchestrator import status_rotation as sr
from orchestrator.status_message import ACTIVITY_GLYPH
from orchestrator.status_notifier import StatusNotifier
from orchestrator.status_tracker import StatusTracker

MODEL = "deepseek-v4-flash-go"


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingTransport:
    """A transport that records what it was asked to render, like Discord would."""

    def __init__(self) -> None:
        self.loop = None
        self.last_gone = False
        self.posts: list[tuple[str, str]] = []
        self.edits: list[tuple[str, str]] = []

    def post(self, run_key: str, text: str) -> str:
        self.posts.append((run_key, text))
        return f"msg-{len(self.posts)}"

    def edit(self, run_key: str, message_id: str, text: str) -> bool:
        self.edits.append((message_id, text))
        return True


class StubEngine:
    def __init__(self, route: dict[str, Any] | None) -> None:
        self.route = route
        self.saved: list[tuple[str, str | None]] = []

    def run_route(self, run_key: str) -> dict[str, Any] | None:
        return self.route

    def note_status_message(self, run_key: str, message_id: str | None) -> None:
        self.saved.append((run_key, message_id))


def _harness(resolvable: bool = True):
    clock = FakeClock()
    tracker = StatusTracker(clock=clock)
    transport = RecordingTransport()
    engine = StubEngine({"chat_id": "chan-1"} if resolvable else None)
    notifier = StatusNotifier(
        None,
        engine=engine,
        tracker=tracker,
        transport_factory=lambda route: transport,
    )
    return notifier, tracker, transport, engine, clock


# What the engine folds in when it accepts the run: the coordinator, and the
# workers it is about to start — no worker event has been emitted yet.
DISPATCH = {
    "run_id": "omo_1",
    "goal": "add live status",
    "workers": [
        {
            "agent": "hephaestus",
            "task_id": "",
            "display": "hephaestus · Deep Agent",
            "task": "add live status",
        }
    ],
    "coordinator": {"agent": "sisyphus", "model": MODEL},
}


def _activity_line(text: str) -> str:
    for line in text.splitlines():
        if "cycling:" in line:
            return line
    return ""


def test_dispatch_posts_the_message_before_any_worker_event():
    """Accepting the run posts once, with a coordinator row, event still pending."""
    notifier, _, transport, engine, _ = _harness()
    assert transport.posts == []

    # The only thing fed to the tracker is the dispatch itself.
    notifier.process("run_dispatched", DISPATCH)

    assert len(transport.posts) == 1
    assert transport.edits == []
    run_key, text = transport.posts[0]
    assert run_key == "omo_1"
    # Run id, worker count, coordinator agent + model, and its own row.
    assert "omo_1" in text
    assert "1 worker" in text
    assert f"coordinator · sisyphus · {MODEL}" in text
    assert "hephaestus · Deep Agent" in text
    # Something for the heartbeat to rotate from the very first render.
    assert any(phrase in text for phrase in sr.CYCLING_PHRASES["coordinator"])
    # The id is remembered, which is what makes every later emit an edit.
    assert engine.saved == [("omo_1", "msg-1")]


def test_heartbeat_alone_rotates_the_coordinator_row():
    """Ticks with no worker events still produce edits whose text actually moves."""
    notifier, tracker, transport, _, clock = _harness()
    notifier.process("run_dispatched", DISPATCH)
    assert len(transport.posts) == 1

    for _ in range(4):
        clock.advance(tracker.phrase_interval + 0.01)
        notifier._flush_rotations()

    texts = [text for _, text in transport.edits]
    assert len(texts) >= 2
    # Not a no-op: consecutive renders differ.
    assert all(before != after for before, after in zip(texts, texts[1:], strict=False))
    # And the difference is the rotation, not only the elapsed counter.
    lines = {_activity_line(text) for text in texts if _activity_line(text)}
    assert len(lines) >= 2
    assert any("cycling:" in text for text in texts)


def test_coordinator_phrases_name_polling_and_come_back_distinct():
    """The coordinator phase says it is waiting, and rotating it is not a no-op."""
    phrases = sr.CYCLING_PHRASES["coordinator"]
    assert len(phrases) >= 2
    joined = " ".join(phrases).lower()
    assert any(word in joined for word in ("poll", "wait", "watch"))

    rotated = {sr.cycling_phrase("coordinator", index) for index in range(len(phrases) * 2)}
    assert len(rotated) >= 2

    # Every phrase keeps the rendered activity line inside the 48-char cap.
    for phrase in phrases:
        line = f"  {ACTIVITY_GLYPH} cycling: {phrase} ({int(sr.ROTATE_INTERVAL_SECONDS)}s)"
        assert len(line) <= 48, line


def test_full_run_keeps_one_message_and_settles_by_edit():
    """Dispatch → worker event → settle: one post, one id, edits all the way down."""
    notifier, tracker, transport, engine, clock = _harness()
    step = tracker.min_edit_interval + 0.1

    notifier.process("run_dispatched", DISPATCH)
    clock.advance(step)
    notifier.process("worker_running", {"run_id": "omo_1", "agent": "hephaestus", "task_id": ""})
    clock.advance(step)
    notifier.process("worker_succeeded", {"run_id": "omo_1", "agent": "hephaestus", "task_id": ""})
    clock.advance(step)
    notifier.process("run_finished", {"run_id": "omo_1", "status": "done"})

    assert len(transport.posts) == 1
    assert {message_id for message_id, _ in transport.edits} == {"msg-1"}
    assert len(transport.edits) == 2
    assert {message_id for _, message_id in engine.saved} == {"msg-1"}

    # The live render carried the cycling line; the terminal state rides the last
    # edit of the same message, and the live line is gone from it.
    assert "cycling:" in transport.edits[0][1]
    final = transport.edits[-1][1]
    assert "✅" in final
    assert "cycling:" not in final


def test_hop_trail_lands_on_the_message_posted_at_dispatch():
    """A later worker event with a fallback hop edits the dispatch message, not a new one."""
    notifier, tracker, transport, engine, clock = _harness()
    notifier.process("run_dispatched", DISPATCH)
    clock.advance(tracker.min_edit_interval + 0.1)

    notifier.process(
        "worker_running",
        {
            "run_id": "omo_1",
            "agent": "hephaestus",
            "task_id": "",
            "model": "glm-5",
            "hop": "claude-sonnet-5 (rate limit)",
        },
    )

    assert len(transport.posts) == 1
    assert {message_id for message_id, _ in transport.edits} == {"msg-1"}
    assert "claude-sonnet-5 (rate limit)" in transport.edits[-1][1]


def test_unresolvable_transport_skips_the_post_without_raising():
    """Fail-open: no route means no post, and the run is not disturbed."""
    notifier, tracker, transport, engine, clock = _harness(resolvable=False)

    notifier.process("run_dispatched", DISPATCH)  # must not raise
    assert transport.posts == []
    assert transport.edits == []
    assert engine.saved == []

    clock.advance(tracker.phrase_interval + 0.1)
    assert notifier._flush_rotations() is None
    assert transport.posts == []
    assert transport.edits == []
