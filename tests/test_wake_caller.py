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
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from orchestrator import session
from orchestrator.chains import ChainResolver
from orchestrator.engine import OmoEngine
from orchestrator.models import Run
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


class FakeLifecycle:
    def __init__(self, order=None):
        self.launches = []
        self.order = order if order is not None else []

    def launch(self, request):
        self.launches.append(request.model)
        return FakeHandle(request.model)

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

    def inject_message(self, content, role="user", *, session_key=None):
        self.order.append("wake")
        self.wakes.append((content, session_key))
        if self.raises:
            raise RuntimeError("injection refused by host")
        return self.delivered


class StubGraph:
    """A walk reduced to its two engine-facing calls."""

    def __init__(self, order):
        self.order = order

    def execute(self, run):
        self.order.append("execute")

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
    assert any(event == "omo:graph_done" and payload["run_id"] == run.run_id for event, payload in ctx.events)
    # The dispatch's own spawned walk is covered by the tests above: close it.
    for pending in ctx.spawned:
        pending.close()


def test_the_wake_names_each_worker_and_its_failure(tmp_path, monkeypatch):
    bind_session(monkeypatch)
    path = tmp_path / "runs.json"
    ctx, engine = make_engine(path)
    out = engine.dispatch(goal="scan the repo", target="explore", background=True)
    run = engine.runs[out["run_id"]]
    run.workers[0].error = "429 rate limit exceeded\nstack"

    engine._wake_caller(run)

    content = ctx.wakes[0][0]
    assert out["run_id"] in content
    assert "0/1 worker(s) settled" in content
    assert "[RUNNING] scan the repo" in content
    # A failure is quoted, first line only, so the notice stays readable.
    assert "error: 429 rate limit exceeded" in content
    assert "stack" not in content
    for pending in ctx.spawned:
        pending.close()
