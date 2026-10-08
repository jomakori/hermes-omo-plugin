"""Throttle, dedupe, folding and rotation rules for the live status message.

No gateway, no network: the tracker is a pure state machine with an injected
clock, so every rule the live publisher depends on is asserted here.
"""

from __future__ import annotations

from orchestrator.status_tracker import (
    DEFAULT_MIN_EDIT_INTERVAL,
    DEFAULT_MOVE_INTERVAL,
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
    # One run heading, one condensed label per worker — never a second heading.
    assert action.text.count("🏗️ omo") == 1
    assert "write the code · hephaestus · Deep Agent" in action.text
    assert "review it · momus · Plan Critic" in action.text
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
    assert action.text.count("🏗️ omo") == 1
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
    assert action.text.splitlines()[0] == "🏗️ omo · omo_1 — fix the bug"
    assert "fix the bug · hephaestus · Deep Agent" in action.text
    assert "- run 🔁 omo_1" in action.text


def test_adopted_message_id_edits_instead_of_posting():
    # A gateway restart seeds the persisted id before the first event: the
    # message is edited, never re-posted.
    tracker, _ = _tracker()
    tracker.adopt("omo_1", "already-there")
    action = tracker.apply("worker_running", _created())
    assert action.kind == "edit"
    assert action.message_id == "already-there"
    assert "🏗️ omo · omo_1 — add live status" in action.text


# ── T2 review flag: read off the payload, never inferred ──────────────────────


def test_run_created_review_flag_is_read_off_the_payload_not_inferred():
    tracker, _ = _tracker()
    with_review = tracker.apply("run_created", _graph(review=True))
    without = tracker.apply("run_created", {**_graph(), "run_id": "omo_nr", "review": False})
    assert "- review" in with_review.text
    # A run declared without review must not grow a review row at all.
    assert "- review" not in without.text


def test_review_row_still_resolves_the_verdict_when_enabled():
    tracker, clock = _tracker()
    tracker.apply("run_created", _graph(review=True))
    tracker.note_message_id("omo_g", "1")
    clock.advance(10)
    action = tracker.apply(
        "review_verdict",
        {"run_id": "omo_g", "agent": "hephaestus", "task_id": "t1", "verdict": "pass", "reviewer": "momus", "cycle": 2},
    )
    assert "- review ✅ momus · pass (cycle 2)" in action.text


# ── T3 stopped rows name the reason, not the task text ────────────────────────


def test_failed_worker_row_names_the_error_not_the_task():
    tracker, clock = _tracker()
    tracker.apply("worker_running", _created())
    tracker.note_message_id("omo_1", "1")
    clock.advance(10)
    action = tracker.apply("worker_failed", {**_created(), "error": "402 credits exhausted"})
    assert "- run ❌ 402 credits exhausted" in action.text
    assert "add live status · hephaestus" in action.text  # the task text stays on the label


def test_cancelled_worker_row_names_a_cause():
    tracker, clock = _tracker()
    tracker.apply("worker_running", _created())
    tracker.note_message_id("omo_1", "1")
    clock.advance(10)
    action = tracker.apply("worker_cancelled", _created())
    assert "- run ⏸ cancelled" in action.text


def test_task_settled_failed_names_the_error():
    tracker, clock = _tracker()
    tracker.apply("run_created", _graph())
    tracker.note_message_id("omo_g", "1")
    tracker.apply("task_started", {"run_id": "omo_g", "agent": "hephaestus", "task_id": "t1", "task": "write the code"})
    clock.advance(10)
    action = tracker.apply(
        "task_settled",
        {"run_id": "omo_g", "agent": "hephaestus", "task_id": "t1", "status": "failed", "error": "provider 500"},
    )
    assert "- run ❌ provider 500" in action.text


# ── T4 activity line: the worker's real tool call, refreshed on the cadence ────


def test_activity_provider_feeds_the_line_with_the_real_tool_call():
    def provider(session, agent):
        return "📖 read_file a.py"

    tracker, _ = _tracker(activity_provider=provider)
    action = tracker.apply("worker_running", _created(activity_session="s1"))
    assert "  ↳ 📖 read_file a.py" in action.text
    assert "cycling:" not in action.text


def test_activity_line_changes_when_the_tool_changes():
    seen = {"tool": "read_file"}

    def provider(session, agent):
        return {"read_file": "📖 read_file a.py", "patch": "🔧 patch b.py"}[seen["tool"]]

    tracker, clock = _tracker(activity_provider=provider)
    first = tracker.apply("worker_running", _created(activity_session="s1"))
    tracker.note_message_id("omo_1", "1")
    assert "📖 read_file a.py" in first.text
    seen["tool"] = "patch"
    clock.advance(DEFAULT_PHRASE_INTERVAL + 0.1)
    action = tracker.tick()
    assert len(action) == 1 and action[0].kind == "edit"
    assert "🔧 patch b.py" in action[0].text


def test_activity_falls_back_to_the_task_when_unobservable():
    tracker, _ = _tracker(activity_provider=lambda session, agent: None)
    action = tracker.apply("worker_running", _created(activity_session="s1", task="add auth rejection tests"))
    assert "  ↳ cycling: task: add (3s)" in action.text


def test_activity_falls_back_to_generic_phrase_when_task_is_missing():
    tracker, _ = _tracker(activity_provider=lambda session, agent: None)
    action = tracker.apply("worker_running", _created(activity_session="s1", goal="", task=""))
    assert "  ↳ cycling: reading the code… (3s)" in action.text


def test_activity_fallback_is_bounded_and_task_specific():
    tracker, _ = _tracker(activity_provider=lambda session, agent: None)
    action = tracker.apply("worker_running", _created(activity_session="s1", task="implement " + "long " * 100))
    line = next(line for line in action.text.splitlines() if line.startswith("  ↳ cycling:"))
    assert len(line) <= 48
    assert "task: implement" in line


def test_activity_fallback_keeps_a_bounded_unbroken_task_token():
    tracker, _ = _tracker(activity_provider=lambda session, agent: None)
    action = tracker.apply("worker_running", _created(activity_session="s1", task="x" * 100))
    line = next(line for line in action.text.splitlines() if line.startswith("  ↳ cycling:"))
    assert len(line) <= 48
    assert line.startswith("  ↳ cycling: task: " + "x" * 23)


def test_activity_fallback_rotates_even_for_one_word_tasks():
    tracker, clock = _tracker(activity_provider=lambda session, agent: None)
    first = tracker.apply("worker_running", _created(activity_session="s1", task="refactor"))
    assert "cycling: task: refactor" in first.text
    tracker.note_message_id("omo_1", "1")
    clock.advance(DEFAULT_PHRASE_INTERVAL + 0.1)
    rotated = tracker.tick()
    assert len(rotated) == 1
    assert "cycling: working: refactor" in rotated[0].text


# ── T5 model + hop on the live rows ───────────────────────────────────────────


def test_serving_model_rides_the_run_row():
    tracker, _ = _tracker()
    action = tracker.apply("worker_running", {**_created(), "model": "minimax-m3"})
    assert "- run 🔁 omo_1 · minimax-m3" in action.text


def test_fallback_hop_shares_the_activity_line():
    tracker, _ = _tracker()
    action = tracker.apply("worker_running", {**_created(), "hop": "claude-sonnet-5 (rate limit)"})
    assert "- run 🔁 omo_1" in action.text  # the model/hop do not pollute the run row
    assert "⤵ claude-sonnet-5 (rate limit)" in action.text
    assert action.text.splitlines()[-1].count("⤵") == 1


# ── role plumbing ─────────────────────────────────────────────────────────────


def test_agent_role_rides_the_worker_line():
    tracker, _ = _tracker()
    action = tracker.apply("worker_running", {**_created(), "display": "hephaestus · Deep Agent"})
    assert "add live status · hephaestus · Deep Agent" in action.text


# ── T7 the struct moves only as an opt-in: post fresh, drop the previous ───────

# An explicit positive cadence for the move tests. The tracker's default is now
# 0.0 (edit in place), so the move path has to be opted into here rather than
# inherited from DEFAULT_MOVE_INTERVAL.
MOVE_EVERY = 5.0


def _two_worker_graph():
    return {
        "run_id": "omo_g",
        "goal": "ship the feature",
        "review": False,
        "workers": [
            {"agent": "hephaestus", "task_id": "t1", "task": "write the code"},
            {"agent": "explore", "task_id": "t2", "task": "map the repo"},
        ],
    }


def test_default_edits_in_place_and_never_moves_the_struct():
    """The shipped default re-posts nothing: one message, edited in place.

    A changed render well past both the edit throttle and any plausible move
    cadence is still an ``edit`` of the message the run already owns — never a
    ``move`` or a second ``post``.
    """
    tracker, clock = _tracker()
    tracker.apply("worker_running", _created())
    tracker.note_message_id("omo_1", "1")
    clock.advance(DEFAULT_MIN_EDIT_INTERVAL + 3600.0)
    action = tracker.apply("worker_running", {**_created(), "model": "minimax-m3"})
    assert action.kind == "edit"
    assert action.message_id == "1"  # the one live message is kept
    assert DEFAULT_MOVE_INTERVAL == 0.0
    assert tracker.move_interval == 0.0


def test_move_posts_a_fresh_struct_once_the_cadence_elapses():
    tracker, clock = _tracker(move_interval=MOVE_EVERY)
    tracker.apply("run_created", _two_worker_graph())
    tracker.note_message_id("omo_g", "1")
    assert tracker.move_interval == MOVE_EVERY
    clock.advance(MOVE_EVERY + 0.1)
    action = tracker.apply(
        "task_started", {"run_id": "omo_g", "agent": "hephaestus", "task_id": "t1", "task": "write the code"}
    )
    assert action.kind == "move"
    assert action.message_id == "1"  # the previous struct is the one to delete


def test_change_inside_the_move_interval_edits_in_place():
    tracker, clock = _tracker(move_interval=MOVE_EVERY)
    tracker.apply("run_created", _two_worker_graph())
    tracker.note_message_id("omo_g", "1")
    clock.advance(DEFAULT_MIN_EDIT_INTERVAL + 0.1)  # past the edit throttle, before the move cadence
    assert tracker.move_interval > DEFAULT_MIN_EDIT_INTERVAL
    action = tracker.apply(
        "task_started", {"run_id": "omo_g", "agent": "hephaestus", "task_id": "t1", "task": "write the code"}
    )
    assert action.kind == "edit"


def test_terminal_state_stops_moving_and_the_final_struct_stays():
    tracker, clock = _tracker(move_interval=MOVE_EVERY)
    tracker.apply("worker_running", _created())
    tracker.note_message_id("omo_1", "1")
    clock.advance(MOVE_EVERY + 30)
    action = tracker.apply("worker_succeeded", _created())
    assert action.kind == "edit"  # terminal: edit, never move
    clock.advance(10_000)
    assert tracker.tick() == []


def test_a_gone_stored_id_posts_fresh_never_a_dead_edit_loop():
    tracker, clock = _tracker(move_interval=MOVE_EVERY)
    tracker.adopt("omo_1", "stale")
    assert tracker.apply("worker_running", _created()).kind == "edit"
    # The edit found the stored id gone; the notifier clears it.
    tracker.note_message_id("omo_1", None)
    clock.advance(MOVE_EVERY + 1)
    action = tracker.apply("worker_succeeded", _created())
    assert action.kind == "post"
