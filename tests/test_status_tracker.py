"""Throttle, dedupe, folding and rotation rules for the live status message.

No gateway, no network: the tracker is a pure state machine with an injected
clock, so every rule the live publisher depends on is asserted here.
"""

from __future__ import annotations

from orchestrator.status_tracker import (
    DEFAULT_MIN_EDIT_INTERVAL,
    DEFAULT_PHRASE_INTERVAL,
    StatusTracker,
)


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _tracker(clock=None, **kwargs):
    clock = clock or FakeClock()
    return StatusTracker(clock=clock, **kwargs), clock


def _created(**overrides):
    payload = {"run_id": "omo_1", "goal": "add live status", "agent": "hephaestus"}
    payload.update(overrides)
    return payload


def _graph(**overrides):
    payload = {
        "run_id": "omo_g",
        "goal": "ship the feature",
        "review": True,
        "workers": [{"agent": "hephaestus", "task_id": "t1", "task": "write the code"}],
    }
    payload.update(overrides)
    return payload


def _cycling_lines(text):
    return [line for line in text.splitlines() if line.startswith("  ↳ cycling:")]


def test_first_event_posts_and_later_events_edit_the_same_message():
    tracker, clock = _tracker()
    first = tracker.apply("worker_running", _created())
    assert first is not None and first.kind == "post"
    assert first.message_id is None
    tracker.note_message_id("omo_1", "555")

    clock.advance(DEFAULT_MIN_EDIT_INTERVAL + 0.1)
    second = tracker.apply("worker_succeeded", _created())
    assert second is not None and second.kind == "edit"
    assert second.message_id == "555"


def test_identical_render_is_a_no_op():
    tracker, clock = _tracker()
    tracker.apply("worker_running", _created())
    tracker.note_message_id("omo_1", "1")
    clock.advance(10)
    assert tracker.apply("worker_running", _created()) is None


def test_edits_are_throttled_then_flushed_by_tick():
    tracker, clock = _tracker(min_edit_interval=2.0)
    tracker.apply("run_created", _graph())
    tracker.note_message_id("omo_g", "1")

    clock.advance(0.5)
    # A change that is not terminal: it must be throttled, not dropped.
    started = tracker.apply(
        "task_started",
        {"run_id": "omo_g", "agent": "hephaestus", "task_id": "t1", "task": "write the code"},
    )
    assert started is None
    assert tracker.tick() == []  # still inside the interval

    clock.advance(2.0)
    flushed = tracker.tick()
    assert len(flushed) == 1 and flushed[0].kind == "edit"
    assert "- run 🔁" in flushed[0].text


def test_terminal_state_forces_a_final_edit_even_inside_the_throttle():
    tracker, clock = _tracker(min_edit_interval=5.0)
    tracker.apply("worker_running", _created())
    tracker.note_message_id("omo_1", "1")
    # No clock advance at all: terminal must bypass the throttle.
    action = tracker.apply("worker_succeeded", _created())
    assert action is not None and action.kind == "edit"
    assert "- run ✅" in action.text


def test_terminal_run_is_not_rotated_further():
    tracker, clock = _tracker()
    tracker.apply("worker_running", _created())
    tracker.note_message_id("omo_1", "1")
    clock.advance(10)
    tracker.apply("worker_succeeded", _created())
    clock.advance(100)
    assert tracker.tick() == []


def test_phrase_rotates_on_the_rotation_timer():
    tracker, clock = _tracker(min_edit_interval=2.0, phrase_interval=DEFAULT_PHRASE_INTERVAL)
    first = tracker.apply("worker_running", _created())
    tracker.note_message_id("omo_1", "1")

    clock.advance(DEFAULT_PHRASE_INTERVAL + 0.1)
    rotated = tracker.tick()
    assert len(rotated) == 1
    assert rotated[0].text != first.text
    # the rotating line sits under the running row and actually changed
    assert _cycling_lines(rotated[0].text) != _cycling_lines(first.text)
    assert "- run 🔁" in rotated[0].text


def test_missing_message_id_posts_a_fresh_message():
    tracker, clock = _tracker()
    tracker.apply("worker_running", _created())
    tracker.note_message_id("omo_1", "1")
    clock.advance(10)
    # The gateway says the message is gone: the next emit must be a fresh post.
    tracker.note_message_id("omo_1", None)
    action = tracker.apply("worker_succeeded", _created())
    assert action is not None and action.kind == "post"


def test_graph_run_has_one_block_per_worker():
    tracker, _ = _tracker()
    action = tracker.apply(
        "run_created",
        _graph(
            workers=[
                {"agent": "hephaestus", "task_id": "t1", "task": "write the code"},
                {"agent": "momus", "task_id": "t2", "task": "review it"},
            ]
        ),
    )
    assert action.kind == "post"
    assert action.text.count("🏗️ omo:") == 2
    assert "\n\n" in action.text
    assert "- dispatch ⏳" in action.text  # not started yet


def test_reviewer_events_fold_into_the_producer_block():
    tracker, clock = _tracker()
    tracker.apply("run_created", _graph())
    tracker.note_message_id("omo_g", "1")
    clock.advance(10)
    action = tracker.apply(
        "worker_running",
        {"run_id": "omo_g", "agent": "momus", "task_id": "t1:review", "task": "review t1"},
    )
    # no separate momus block: the review row is folded into the producer's block
    assert action.text.count("🏗️ omo:") == 1
    assert "- review 🔍 momus · omo_g · t1:review" in action.text


def test_review_verdict_names_the_reviewer_and_cycle():
    tracker, clock = _tracker()
    tracker.apply("run_created", _graph())
    tracker.note_message_id("omo_g", "1")
    clock.advance(10)
    action = tracker.apply(
        "review_verdict",
        {
            "run_id": "omo_g",
            "agent": "hephaestus",
            "task_id": "t1",
            "verdict": "problems",
            "reviewer": "momus",
            "cycle": 1,
        },
    )
    assert "- review ✅ momus · problems (cycle 1)" in action.text


def test_task_blocked_names_the_dependency():
    tracker, clock = _tracker()
    tracker.apply("run_created", _graph(task_id="t1"))
    tracker.note_message_id("omo_g", "1")
    clock.advance(10)
    action = tracker.apply(
        "task_blocked",
        {"run_id": "omo_g", "agent": "hephaestus", "task_id": "t1", "cause": "waiting on prometheus"},
    )
    assert action is not None and "- run ⛔ waiting on prometheus" in action.text


def test_task_settled_draws_the_run_duration():
    tracker, clock = _tracker()
    tracker.apply("run_created", _graph())
    tracker.note_message_id("omo_g", "1")
    tracker.apply("task_started", {"run_id": "omo_g", "agent": "hephaestus", "task_id": "t1", "task": "write the code"})
    clock.advance(22 * 60)
    action = tracker.apply(
        "task_settled",
        {"run_id": "omo_g", "agent": "hephaestus", "task_id": "t1", "status": "succeeded"},
    )
    assert action is not None and "- run ✅ 22m" in action.text


def test_run_finished_marks_terminal_and_finalises():
    tracker, clock = _tracker(min_edit_interval=99.0)
    tracker.apply("run_created", _graph())
    tracker.note_message_id("omo_g", "9")
    action = tracker.apply("run_finished", {"run_id": "omo_g", "status": "succeeded"})
    assert action is not None and action.kind == "edit"
    assert "- run ✅" in action.text


def test_interrupted_worker_renders_its_cause():
    tracker, clock = _tracker()
    tracker.apply("worker_running", _created())
    tracker.note_message_id("omo_1", "1")
    clock.advance(10)
    action = tracker.apply("worker_interrupted", {**_created(), "cause": "gateway restart"})
    assert action is not None and "- run ⚠️ gateway restart" in action.text


def test_events_for_unknown_run_id_are_ignored():
    tracker, _ = _tracker()
    assert tracker.apply("worker_running", {"goal": "no run id"}) is None


def test_single_dispatch_without_run_created_still_names_the_goal():
    # The engine's single-worker path never emits run_created: the goal has to
    # ride in on the first worker event or the header would be bare.
    tracker, _ = _tracker()
    action = tracker.apply(
        "worker_running",
        {"run_id": "omo_1", "goal": "fix the bug", "agent": "hephaestus", "task": "fix the bug"},
    )
    assert action.kind == "post"
    assert action.text.splitlines()[0] == "🏗️ omo: hephaestus: fix the bug"
    assert "- run 🔁 omo_1" in action.text


def test_adopted_message_id_edits_instead_of_posting():
    # A gateway restart seeds the persisted id before the first event: the
    # message is edited, never re-posted.
    tracker, _ = _tracker()
    tracker.adopt("omo_1", "already-there")
    action = tracker.apply("worker_running", _created())
    assert action.kind == "edit"
    assert action.message_id == "already-there"
    assert "🏗️ omo: hephaestus: add live status" in action.text
