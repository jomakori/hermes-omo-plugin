"""The watchdog: a child that stops writing is stopped, not waited out.

The hard ``worker_timeout_seconds`` bound stays, but it used to be the only one, so a
hung child held the run for thirty minutes. These tests pin the shorter bound and, just
as important, pin when it must *not* fire: a watchdog with no liveness signal must not
kill a working child.
"""

import dataclasses
import time

from orchestrator import engine as engine_module
from orchestrator.chains import ChainResolver
from orchestrator.engine import OmoEngine


@dataclasses.dataclass
class FakeRequest:
    goal: str
    context: str | None
    role: str
    model: str | None
    allowed_toolsets: tuple
    metadata: dict


def request_factory(*, goal, context, spec, model, toolsets):
    return FakeRequest(
        goal=goal,
        context=context,
        role="orchestrator" if spec.orchestrator else "leaf",
        model=model,
        allowed_toolsets=toolsets,
        metadata={"omo_agent": spec.name},
    )


@dataclasses.dataclass
class FakeHandle:
    model: str


class SlowLifecycle:
    """A child that runs for ``slices`` watchdog looks before it finishes."""

    def __init__(self, slices=1, summary="done"):
        self.slices = slices
        self.summary = summary
        self.launches = []
        self.waits = 0
        self.cancelled = []
        self.handle = None

    def launch(self, request):
        self.handle = FakeHandle(request.model)
        self.launches.append(request.model)
        return self.handle

    def wait(self, handle, *, timeout_seconds=None):
        # A real slice blocks; without that the loop would spin and no clock would move.
        time.sleep(0.005)
        self.waits += 1
        return None

    def result(self, handle):
        if self.waits < self.slices:
            return {"ready": False}
        return {"ready": True, "summary": self.summary, "model": handle.model}

    def status_of(self, handle):
        return True

    def cancel(self, handle, *, reason=""):
        self.cancelled.append(reason)
        return None


class FakeCtx:
    def __init__(self, lifecycle, config=None):
        self.subagent_lifecycle = lifecycle
        self._config = config or {}
        self.spawned = []
        self.events = []

    def get_config(self, key, default=None):
        return self._config.get(key, default)

    def emit(self, event, payload=None):
        self.events.append((event, payload))

    def spawn_task(self, coro):
        self.spawned.append(coro)


CONFIG = {
    "worker_activity_slice_seconds": 0.01,
    "worker_stall_seconds": 0.05,
    "worker_timeout_seconds": 5,
}


def make_engine(lifecycle, config=None):
    merged = dict(CONFIG)
    merged.update(config or {})
    ctx = FakeCtx(lifecycle, merged)
    engine = OmoEngine(ctx, lifecycle=lifecycle, request_factory=request_factory)
    engine.chains = ChainResolver(merged)
    return ctx, engine


def test_a_child_that_stops_writing_is_stopped_before_the_hard_timeout():
    lifecycle = SlowLifecycle(slices=10_000)
    ctx, engine = make_engine(lifecycle)
    # A pinned session whose newest message is ten minutes old.
    engine._activity_at = lambda worker: time.time() - 600

    started = time.time()
    out = engine.dispatch(goal="scan the repo", target="explore")
    elapsed = time.time() - started

    stall_bound = CONFIG["worker_stall_seconds"]
    hard_timeout = CONFIG["worker_timeout_seconds"]
    assert out["status"] == "failed"
    assert "no activity" in out["error"]
    assert "watchdog" in out["error"]
    assert lifecycle.cancelled and "no activity" in lifecycle.cancelled[0]
    # Judge the stall against the hard timeout it must beat, not an absolute
    # wall-clock figure: a loaded CI runner adds scheduling slack to both.
    assert elapsed < hard_timeout / 2, (
        f"watchdog fired at {elapsed:.3f}s, not meaningfully before the {hard_timeout}s hard timeout"
    )
    assert elapsed >= stall_bound, f"watchdog fired at {elapsed:.3f}s, before the {stall_bound}s stall bound"
    assert lifecycle.waits < 100, "and after a handful of slices, not thousands"
    assert any(name == "worker_failed" for name, _ in ctx.events)


def test_a_child_with_no_readable_signal_is_never_called_stalled():
    lifecycle = SlowLifecycle(slices=4)
    ctx, engine = make_engine(lifecycle)
    engine._activity_at = lambda worker: None

    out = engine.dispatch(goal="scan the repo", target="explore")

    assert out["status"] == "succeeded"
    assert lifecycle.cancelled == []


def test_a_slow_child_that_keeps_writing_is_left_alone_and_heartbeats():
    lifecycle = SlowLifecycle(slices=5)
    ctx, engine = make_engine(lifecycle)
    engine._activity_at = lambda worker: time.time()
    original = engine_module._HEARTBEAT_INTERVAL_SECONDS
    engine_module._HEARTBEAT_INTERVAL_SECONDS = 0.0
    try:
        out = engine.dispatch(goal="scan the repo", target="explore")
    finally:
        engine_module._HEARTBEAT_INTERVAL_SECONDS = original

    assert out["status"] == "succeeded"
    assert lifecycle.cancelled == []
    beats = [payload for name, payload in ctx.events if name == "worker_heartbeat"]
    assert beats, "a running child must be reported as still alive"
    assert "elapsed_seconds" in beats[0] and "idle_seconds" in beats[0]


def test_the_hard_timeout_still_ends_a_child_that_never_finishes():
    lifecycle = SlowLifecycle(slices=10_000)
    ctx, engine = make_engine(lifecycle, {"worker_timeout_seconds": 0.05, "worker_stall_seconds": 0})
    engine._activity_at = lambda worker: None

    out = engine.dispatch(goal="scan the repo", target="explore")

    assert out["status"] == "failed"
    assert "timeout" in out["error"]
    assert lifecycle.cancelled and "timeout" in lifecycle.cancelled[0]


def test_no_evidence_at_all_is_not_stalled_at_the_stall_bound_but_hits_hard_timeout():
    """With worker_stall_seconds=360 but zero liveness signal, 360s must not fire
    the stall path — only the (much shorter, here) hard timeout may end the child."""
    lifecycle = SlowLifecycle(slices=10_000)
    ctx, engine = make_engine(
        lifecycle,
        {"worker_timeout_seconds": 0.05, "worker_stall_seconds": 360},
    )
    engine._activity_at = lambda worker: None

    started = time.time()
    out = engine.dispatch(goal="scan the repo", target="explore")
    elapsed = time.time() - started

    assert out["status"] == "failed"
    assert "timeout" in out["error"]
    assert "no activity" not in out["error"], "no evidence means no stall verdict, ever"
    assert lifecycle.cancelled and "timeout" in lifecycle.cancelled[0]
    assert elapsed < 10, "stopped by the hard timeout, not by waiting out 360s"


def test_evidence_that_goes_idle_past_the_stall_bound_is_stopped():
    """Once liveness evidence exists and then stops advancing, the stall fires
    well before the hard timeout."""
    lifecycle = SlowLifecycle(slices=10_000)
    ctx, engine = make_engine(lifecycle, {"worker_stall_seconds": 0.05, "worker_timeout_seconds": 5})
    # A pinned session whose newest message is far in the past: evidence exists,
    # but it is stale, so idle time immediately exceeds the stall bound.
    engine._activity_at = lambda worker: time.time() - 600

    started = time.time()
    out = engine.dispatch(goal="scan the repo", target="explore")
    elapsed = time.time() - started

    assert out["status"] == "failed"
    assert "no activity" in out["error"]
    assert "watchdog" in out["error"]
    assert lifecycle.cancelled and "no activity" in lifecycle.cancelled[0]
    assert elapsed < 5 / 2, "the watchdog stopped it well before the 5s hard timeout"


def test_the_activity_reader_speaks_the_real_store(tmp_path):
    """The watchdog reads the host's own sqlite, not a shape invented in a test."""
    import sqlite3

    from orchestrator import worker_activity

    profile = tmp_path / "hephaestus"
    profile.mkdir()
    db = profile / "state.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE messages (session_id TEXT, timestamp REAL, text TEXT)")
    conn.executemany(
        "INSERT INTO messages VALUES (?, ?, ?)",
        [("sess-a", 1000.0, "hi"), ("sess-a", 1005.5, "still working"), ("sess-b", 2000.0, "other")],
    )
    conn.commit()
    conn.close()

    assert worker_activity.last_message_at("hephaestus", "sess-a", tmp_path) == 1005.5
    assert worker_activity.last_message_at("hephaestus", "sess-missing", tmp_path) is None
    assert worker_activity.last_message_at("nobody", "sess-a", tmp_path) is None


def test_a_stalled_child_is_failed_rather_than_cancelled():
    """The caller did not cancel it: a stall is a failure the graph must see."""
    lifecycle = SlowLifecycle(slices=10_000)
    ctx, engine = make_engine(lifecycle)
    engine._activity_at = lambda worker: time.time() - 600

    out = engine.dispatch(goal="scan the repo", target="explore")

    assert out["status"] == "failed"
    assert out["status"] != "cancelled"


def test_open_tool_call_is_alive_until_its_result_lands(tmp_path):
    """An assistant row with tool_calls and no result yet is in flight, not silent."""
    import json
    import sqlite3

    from orchestrator import worker_activity

    profile = tmp_path / "hephaestus"
    profile.mkdir()
    conn = sqlite3.connect(profile / "state.db")
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, tool_calls TEXT, timestamp REAL)"
    )
    calls = json.dumps([{"function": {"name": "terminal", "arguments": {"command": "gh pr checks"}}}])
    conn.execute(
        "INSERT INTO messages (session_id, role, tool_calls, timestamp) VALUES ('s', 'assistant', ?, 1000.0)",
        (calls,),
    )
    conn.commit()
    assert worker_activity.open_tool_call_at("hephaestus", "s", tmp_path) == 1000.0
    conn.execute("INSERT INTO messages (session_id, role, tool_calls, timestamp) VALUES ('s', 'tool', NULL, 1100.0)")
    conn.commit()
    conn.close()
    assert worker_activity.open_tool_call_at("hephaestus", "s", tmp_path) is None
    assert worker_activity.open_tool_call_at("hephaestus", "missing", tmp_path) is None


def test_a_stalled_child_gets_one_resume_before_the_hop_fails():
    lifecycle = SlowLifecycle(slices=10_000)
    ctx, engine = make_engine(lifecycle, {"worker_stall_seconds": 0.05, "worker_timeout_seconds": 5})
    engine._activity_at = lambda worker: time.time() - 600

    out = engine.dispatch(goal="scan the repo", target="explore")

    assert out["status"] == "failed"
    assert "no activity" in out["error"]
    assert len(lifecycle.launches) == 2, "one resume, then the stall fails the hop"


def test_the_resume_brief_names_branch_sha_and_status():
    lifecycle = SlowLifecycle()
    _, engine = make_engine(lifecycle)
    spec = type("S", (), {"orchestrator": False, "name": "x"})()
    req = request_factory(goal="fix x", context=None, spec=spec, model="m", toolsets=None)
    resumed = engine._resume_request(req)
    assert resumed.goal.startswith("fix x")
    assert "RESUME" in resumed.goal and "git status" in resumed.goal and "HEAD SHA" in resumed.goal
