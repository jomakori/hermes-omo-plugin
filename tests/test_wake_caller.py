"""A background run wakes the conversation that dispatched it.

A background dispatch answers with a run_id and the caller's turn ends there, so
the run's outcome is invisible unless something hands it back. These tests pin
that hand-off: once per run, only for a background dispatch, only when the
caller's session is still reachable — and never at the cost of the run itself.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import pathlib
import sys
import threading
import time
import types

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from orchestrator import session
from orchestrator.chains import ChainResolver
from orchestrator.engine import OmoEngine
from orchestrator.graph import TaskGraph
from orchestrator.models import CANCELLED, FAILED, RUNNING, SUCCEEDED, UNFINISHED, Run
from orchestrator.store import RunStore


# ── the host bridge ──────────────────────────────────────────────────────
def _bind(monkeypatch, session_id, session_key=None):
    """Install a stand-in host bridge, the way the gateway binds a turn."""
    module = types.ModuleType("gateway.session_context")
    values = {session.SESSION_ID_ENV: session_id}
    if session_key is not None:
        values[session.SESSION_KEY_ENV] = session_key
    module.get_session_env = lambda name, default="": values.get(name, default)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gateway", types.ModuleType("gateway"))
    monkeypatch.setitem(sys.modules, "gateway.session_context", module)


def bind_session(monkeypatch, session_id="ses_alpha", session_key="sk_alpha"):
    _bind(monkeypatch, session_id, session_key)


def bind_session_without_a_key(monkeypatch, session_id="ses_alpha"):
    """A host that knows the session but exposes no key to inject back into."""
    _bind(monkeypatch, session_id)


# ── engine fakes ─────────────────────────────────────────────────────────
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
    goal: str = ""


class FakeLifecycle:
    def __init__(self, order=None):
        self.launches = []
        self.order = order if order is not None else []

    def launch(self, request):
        self.launches.append(request.model)
        return FakeHandle(request.model, request.goal)

    def wait(self, handle, *, timeout_seconds=None):
        return None

    def result(self, handle):
        return {"summary": "done", "model": handle.model}

    def status_of(self, handle):
        return True

    def cancel(self, handle, *, reason=""):
        return None


class FakeCtx:
    """A host with the plugin APIs the engine uses — and no injection API."""

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


class WakeCtx(FakeCtx):
    """A host that can inject: records what it was asked to deliver."""

    def __init__(self, lifecycle, *, delivered=True, raises=False, order=None):
        super().__init__(lifecycle)
        self.wakes = []
        self.delivered = delivered
        self.raises = raises
        self.order = order if order is not None else []
        # Set by a test that wants the run's state at the moment of delivery.
        self.watched_run = None
        self.seen_at_wake = []

    def inject_message(self, content, role="user", *, session_key=None):
        self.order.append("wake")
        self.wakes.append((content, session_key))
        if self.watched_run is not None:
            self.seen_at_wake.append([worker.status for worker in self.watched_run.workers])
        if self.raises:
            raise RuntimeError("injection refused by host")
        return self.delivered


class StubGraph:
    """A walk reduced to its two engine-facing calls.

    ``settle=False`` leaves the declared workers untouched, the way a walk that
    blew up before launching them does.
    """

    def __init__(self, order, *, settle=True, raise_after=None):
        self.order = order
        self._settle = settle
        self._raise_after = raise_after

    def execute(self, run):
        self.order.append("execute")
        if self._settle:
            for worker in run.workers:
                worker.status = SUCCEEDED
                worker.finished_at = time.time()
        if self._raise_after is not None:
            raise self._raise_after

    def _payload(self, run):
        return {"ok": True}


def make_engine(path, *, ctx=None, lifecycle=None):
    lifecycle = lifecycle or FakeLifecycle()
    ctx = ctx if ctx is not None else WakeCtx(lifecycle)
    engine = OmoEngine(ctx, lifecycle=lifecycle, request_factory=request_factory, store=RunStore(path=path))
    engine.chains = ChainResolver({})
    return ctx, engine


def stored(path):
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


# ── the wake ─────────────────────────────────────────────────────────────
def test_a_background_dispatch_wakes_its_caller_once(tmp_path, monkeypatch):
    bind_session(monkeypatch)
    path = tmp_path / "runs.json"
    ctx, engine = make_engine(path)

    out = engine.dispatch(goal="scan the repo", target="explore", background=True)
    asyncio.run(ctx.spawned[0])

    assert len(ctx.wakes) == 1
    content, session_key = ctx.wakes[0]
    # The notice lands in the session that dispatched the run, not just any session.
    assert session_key == "sk_alpha"
    assert out["run_id"] in content
    # It says how the run ended and how to read the detail: no polling needed.
    assert "explore" in content
    assert "omo action=status" in content
    assert stored(path)["runs"][0]["woken"] is True


def test_a_foreground_dispatch_never_wakes(tmp_path, monkeypatch):
    bind_session(monkeypatch)
    path = tmp_path / "runs.json"
    ctx, engine = make_engine(path)

    out = engine.dispatch(goal="scan the repo", target="explore")

    # The result came back as the tool call's own return value: waking as well
    # would deliver the same outcome twice.
    assert out["status"] == "succeeded"
    assert ctx.wakes == []


def test_a_caller_with_no_session_key_is_not_woken(tmp_path, monkeypatch):
    bind_session_without_a_key(monkeypatch)
    path = tmp_path / "runs.json"
    ctx, engine = make_engine(path)

    engine.dispatch(goal="scan the repo", target="explore", background=True)
    asyncio.run(ctx.spawned[0])

    assert ctx.wakes == []
    # …and nothing pretends it was delivered.
    assert stored(path)["runs"][0]["woken"] is False


def test_a_host_without_an_injection_api_is_harmless(tmp_path, monkeypatch):
    bind_session(monkeypatch)
    lifecycle = FakeLifecycle()
    path = tmp_path / "runs.json"
    ctx, engine = make_engine(path, ctx=FakeCtx(lifecycle), lifecycle=lifecycle)

    out = engine.dispatch(goal="scan the repo", target="explore", background=True)
    asyncio.run(ctx.spawned[0])

    # An older host simply never wakes anyone; the run itself is unaffected.
    assert engine.status(out["run_id"])["workers"][0]["status"] == "SUCCEEDED"
    assert stored(path)["runs"][0]["woken"] is False


def test_a_refused_injection_is_reported_and_not_retried(tmp_path, monkeypatch):
    bind_session(monkeypatch)
    lifecycle = FakeLifecycle()
    path = tmp_path / "runs.json"
    ctx, engine = make_engine(path, ctx=WakeCtx(lifecycle, delivered=False), lifecycle=lifecycle)

    out = engine.dispatch(goal="scan the repo", target="explore", background=True)
    asyncio.run(ctx.spawned[0])
    assert len(ctx.wakes) == 1
    assert ("omo:wake_failed", {"run_id": out["run_id"]}) in ctx.events

    # A host that said no is not asked again by the next terminal transition.
    engine._wake_caller(engine.runs[out["run_id"]])
    assert len(ctx.wakes) == 1


def test_an_injector_that_raises_does_not_break_the_run(tmp_path, monkeypatch):
    bind_session(monkeypatch)
    lifecycle = FakeLifecycle()
    path = tmp_path / "runs.json"
    ctx, engine = make_engine(path, ctx=WakeCtx(lifecycle, raises=True), lifecycle=lifecycle)

    out = engine.dispatch(goal="scan the repo", target="explore", background=True)
    asyncio.run(ctx.spawned[0])  # must not raise

    assert len(ctx.wakes) == 1
    assert engine.runs[out["run_id"]].woken is True
    engine._wake_caller(engine.runs[out["run_id"]])
    assert len(ctx.wakes) == 1
    assert engine.status(out["run_id"])["workers"][0]["status"] == "SUCCEEDED"


def test_a_woken_run_survives_a_restart_without_waking_twice(tmp_path, monkeypatch):
    bind_session(monkeypatch)
    path = tmp_path / "runs.json"
    ctx, engine = make_engine(path)
    engine.dispatch(goal="scan the repo", target="explore", background=True)
    asyncio.run(ctx.spawned[0])

    # A second engine adopting the same record is what a gateway restart looks like.
    reloaded = Run.from_dict(stored(path)["runs"][0])
    assert reloaded is not None and reloaded.woken is True
    _, restarted = make_engine(path)
    restarted._wake_caller(reloaded)

    assert len(ctx.wakes) == 1


def test_a_background_graph_wakes_once_after_the_walk(tmp_path, monkeypatch):
    bind_session(monkeypatch)
    path = tmp_path / "runs.json"
    order = []
    lifecycle = FakeLifecycle(order=order)
    ctx, engine = make_engine(path, ctx=WakeCtx(lifecycle, order=order), lifecycle=lifecycle)

    out = engine.dispatch_graph(tasks=[{"id": "a", "agent": "explore", "prompt": "recon"}], background=True)
    run = engine.runs[out["run_id"]]
    asyncio.run(engine._run_graph(StubGraph(order), run))

    # The wake follows the walk (never precedes it) and fires exactly once.
    assert order == ["execute", "wake"]
    content, session_key = ctx.wakes[0]
    assert session_key == "sk_alpha"
    assert "1/1 worker(s) settled" in content
    assert any(event == "omo:graph_done" and payload["run_id"] == run.run_id for event, payload in ctx.events)
    # The dispatch's own spawned walk is covered by the tests above: close it.
    for pending in ctx.spawned:
        pending.close()


# ── the gate: a run is not reported while a worker is still live ─────────
def test_a_worker_that_is_still_running_is_not_announced(tmp_path, monkeypatch):
    bind_session(monkeypatch)
    path = tmp_path / "runs.json"
    ctx, engine = make_engine(path)
    out = engine.dispatch(goal="scan the repo", target="explore", background=True)
    run = engine.runs[out["run_id"]]
    assert run.workers[0].status == RUNNING

    # The frame that used to fire at dispatch time: the worker has only just
    # started, so there is nothing to report yet.
    engine._wake_caller(run)
    engine._wake_when_settled(run)

    assert ctx.wakes == []
    # …and the run is not marked delivered, so the settling seam can still speak.
    assert run.woken is False
    assert stored(path)["runs"][0]["woken"] is False
    for pending in ctx.spawned:
        pending.close()


def test_the_notice_never_says_finished_over_a_live_worker(tmp_path, monkeypatch):
    bind_session(monkeypatch)
    path = tmp_path / "runs.json"
    ctx, engine = make_engine(path)
    out = engine.dispatch(goal="scan the repo", target="explore", background=True)
    run = engine.runs[out["run_id"]]
    run.workers[0].error = "429 rate limit exceeded\nstack"

    content = engine._wake_text(run)

    # A worker line saying RUNNING under a "finished" headline is the defect.
    assert "0/1 worker(s) settled" in content
    assert "[RUNNING] scan the repo" in content
    assert "finished" not in content
    assert "still running" in content
    # A failure is quoted, first line only, so the notice stays readable.
    assert "error: 429 rate limit exceeded" in content
    assert "stack" not in content
    for pending in ctx.spawned:
        pending.close()


class BlockingLifecycle(FakeLifecycle):
    """Holds its launch until the test releases it, so the walk outlives its caller."""

    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def launch(self, request):
        self.started.set()
        assert self.release.wait(10), "the test never released the worker"
        return super().launch(request)


def test_a_walk_that_outlives_its_coroutine_still_wakes_when_it_settles(tmp_path, monkeypatch):
    """The observed defect: the notice arrived at dispatch and the run went on for hours.

    Awaiting the worker's thread is cancellable, so the spawned coroutine can go
    away while the worker keeps running. The wake belongs to the walk, not to the
    frame that launched it.
    """
    bind_session(monkeypatch)
    path = tmp_path / "runs.json"
    lifecycle = BlockingLifecycle()
    ctx, engine = make_engine(path, ctx=WakeCtx(lifecycle), lifecycle=lifecycle)

    out = engine.dispatch(goal="scan the repo", target="explore", background=True)
    run = engine.runs[out["run_id"]]

    async def scenario():
        task = asyncio.ensure_future(ctx.spawned[0])
        for _ in range(500):
            if lifecycle.started.is_set():
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Still RUNNING: no notice is due, and none may be spent on it.
        assert run.workers[0].status == RUNNING
        assert ctx.wakes == []
        assert run.woken is False
        lifecycle.release.set()
        for _ in range(500):
            if run.workers[0].status not in UNFINISHED:
                break
            await asyncio.sleep(0.01)

    asyncio.run(scenario())  # joins the walk's thread before returning

    assert run.workers[0].status == SUCCEEDED
    assert len(ctx.wakes) == 1
    assert "1/1 worker(s) settled" in ctx.wakes[0][0]


class FailingLifecycle(FakeLifecycle):
    """Every launch fails at the provider: the run has to be reported, not swallowed."""

    def launch(self, request):
        raise RuntimeError("429 rate limit exceeded")


def test_a_failing_worker_wakes_exactly_once(tmp_path, monkeypatch):
    bind_session(monkeypatch)
    path = tmp_path / "runs.json"
    lifecycle = FailingLifecycle()
    ctx, engine = make_engine(path, ctx=WakeCtx(lifecycle), lifecycle=lifecycle)

    out = engine.dispatch(goal="scan the repo", target="explore", background=True)
    asyncio.run(ctx.spawned[0])
    engine._wake_caller(engine.runs[out["run_id"]])  # a later seam adds nothing
    # A refused run may auto-retry into a second run; this test only owns the first.
    for pending in ctx.spawned[1:]:
        pending.close()

    assert len(ctx.wakes) == 1
    content = ctx.wakes[0][0]
    assert "1/1 worker(s) settled" in content
    assert "[FAILED] scan the repo" in content
    assert "error: 429 rate limit exceeded" in content
    assert engine.runs[out["run_id"]].workers[0].status == FAILED


def test_a_cancelled_run_wakes_its_caller_once(tmp_path, monkeypatch):
    """Cancelling settles the run, so the caller hears it without waiting on a walk."""
    bind_session(monkeypatch)
    path = tmp_path / "runs.json"
    lifecycle = BlockingLifecycle()
    ctx, engine = make_engine(path, ctx=WakeCtx(lifecycle), lifecycle=lifecycle)
    out = engine.dispatch(goal="scan the repo", target="explore", background=True)
    run = engine.runs[out["run_id"]]

    engine.cancel(out["run_id"])
    engine._wake_caller(run)  # a later seam adds nothing

    assert [worker.status for worker in run.workers] == [CANCELLED]
    assert len(ctx.wakes) == 1
    assert "1/1 worker(s) settled" in ctx.wakes[0][0]
    assert "[CANCELLED] scan the repo" in ctx.wakes[0][0]
    lifecycle.release.set()
    for pending in ctx.spawned:
        pending.close()


# ── the last worker to settle is the one that wakes ──────────────────────
def background_graph(engine, tasks):
    """Declare a background graph the way ``dispatch_graph`` does, and hand it back."""
    graph = TaskGraph(engine, tasks, goal="walk")
    run = graph.build()
    run.session_key = session.current_session_key()
    run.background = True
    engine._persist()
    return graph, run


def test_the_wake_fires_once_when_the_last_graph_worker_settles(tmp_path, monkeypatch):
    bind_session(monkeypatch)
    path = tmp_path / "runs.json"
    lifecycle = FakeLifecycle()
    ctx, engine = make_engine(path, ctx=WakeCtx(lifecycle), lifecycle=lifecycle)
    graph, run = background_graph(
        engine,
        [
            {"id": "a", "agent": "explore", "prompt": "recon"},
            {"id": "b", "agent": "oracle", "prompt": "review", "depends_on": ["a"]},
        ],
    )
    ctx.watched_run = run

    asyncio.run(engine._run_graph(graph, run))

    assert [worker.status for worker in run.workers] == [SUCCEEDED, SUCCEEDED]
    assert len(ctx.wakes) == 1
    assert "2/2 worker(s) settled" in ctx.wakes[0][0]
    # Delivered with nothing left running: the notice and the record agree.
    assert ctx.seen_at_wake == [[SUCCEEDED, SUCCEEDED]]


class StagedLifecycle(FakeLifecycle):
    """Settles the second task only after the first worker has settled."""

    def __init__(self):
        super().__init__()
        self.watch = None  # the first worker, set by the test
        self.second_started = threading.Event()

    def launch(self, request):
        if request.goal == "second":
            self.second_started.set()
            deadline = time.time() + 10
            while self.watch is not None and self.watch.status in UNFINISHED and time.time() < deadline:
                time.sleep(0.01)
        return super().launch(request)


def test_two_workers_settling_at_different_times_wake_once(tmp_path, monkeypatch):
    bind_session(monkeypatch)
    path = tmp_path / "runs.json"
    lifecycle = StagedLifecycle()
    ctx, engine = make_engine(path, ctx=WakeCtx(lifecycle), lifecycle=lifecycle)
    graph, run = background_graph(
        engine,
        [
            {"id": "a", "agent": "explore", "prompt": "first"},
            {"id": "b", "agent": "oracle", "prompt": "second"},
        ],
    )
    lifecycle.watch = run.workers[0]
    ctx.watched_run = run

    asyncio.run(engine._run_graph(graph, run))

    first, second = run.workers
    # They settled at different moments: the first was silent, the second woke.
    assert lifecycle.second_started.is_set()
    assert first.finished_at < second.finished_at
    assert len(ctx.wakes) == 1
    assert "2/2 worker(s) settled" in ctx.wakes[0][0]
    assert ctx.seen_at_wake == [[SUCCEEDED, SUCCEEDED]]


def test_a_graph_that_raises_still_wakes_exactly_once(tmp_path, monkeypatch):
    bind_session(monkeypatch)
    path = tmp_path / "runs.json"
    order = []
    lifecycle = FakeLifecycle(order=order)
    ctx, engine = make_engine(path, ctx=WakeCtx(lifecycle, order=order), lifecycle=lifecycle)
    out = engine.dispatch_graph(tasks=[{"id": "a", "agent": "explore", "prompt": "recon"}], background=True)
    run = engine.runs[out["run_id"]]
    boom = RuntimeError("the pool died mid-walk")

    with pytest.raises(RuntimeError, match="the pool died mid-walk"):
        asyncio.run(engine._run_graph(StubGraph(order, settle=False, raise_after=boom), run))

    # The walk never settled its worker: the run is closed out rather than left
    # RUNNING forever, and the caller hears about it once.
    assert run.workers[0].status == FAILED
    assert len(ctx.wakes) == 1
    assert "1/1 worker(s) settled" in ctx.wakes[0][0]
    assert "[FAILED] recon" in ctx.wakes[0][0]
    assert order == ["execute", "wake"]
    for pending in ctx.spawned:
        pending.close()
