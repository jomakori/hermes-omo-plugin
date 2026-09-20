import asyncio
import dataclasses

import pytest

from orchestrator.chains import ChainResolver
from orchestrator.engine import OmoEngine
from orchestrator.guards import GuardError


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
    def __init__(self, fail_times=0, error="429 rate limit exceeded", status=True):
        self.fail_times = fail_times
        self.error = error
        self.launches = []
        self.cancelled = []
        self.status = status

    def launch(self, request):
        self.launches.append(request.model)
        if len(self.launches) <= self.fail_times:
            raise RuntimeError(self.error)
        return FakeHandle(request.model)

    def wait(self, handle, *, timeout_seconds=None):
        return None

    def result(self, handle):
        return {"summary": "done", "model": handle.model}

    def status_of(self, handle):
        return self.status

    def cancel(self, handle, *, reason=""):
        self.cancelled.append(handle.model)
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


def make_engine(lifecycle, config=None):
    ctx = FakeCtx(lifecycle, config)
    engine = OmoEngine(ctx, lifecycle=lifecycle, request_factory=request_factory)
    engine.chains = ChainResolver(config or {})
    return ctx, engine


def test_dispatch_sync_succeeds_on_primary():
    lifecycle = FakeLifecycle()
    ctx, engine = make_engine(lifecycle)
    out = engine.dispatch(goal="scan the repo", target="explore")
    assert out["status"] == "succeeded"
    assert lifecycle.launches == ["litellm/deepseek-v4-flash"]
    assert out["model"] == "litellm/deepseek-v4-flash"
    assert out["result"] == {"summary": "done", "model": "litellm/deepseek-v4-flash"}


def test_dispatch_falls_back_on_retryable_error():
    lifecycle = FakeLifecycle(fail_times=1)
    ctx, engine = make_engine(lifecycle)
    out = engine.dispatch(goal="scan the repo", target="explore")
    assert out["status"] == "succeeded"
    assert lifecycle.launches == ["litellm/deepseek-v4-flash", "litellm/minimax-m3"]
    assert out["model"] == "litellm/minimax-m3"


def test_dispatch_exhausts_chain_then_fails():
    lifecycle = FakeLifecycle(fail_times=99)
    ctx, engine = make_engine(lifecycle, {"max_fallback_attempts": 2})
    out = engine.dispatch(goal="scan", target="explore")
    assert out["status"] == "failed"
    assert len(lifecycle.launches) == 1 + 2
    assert "rate limit" in out["error"]


def test_non_retryable_error_does_not_fall_back():
    lifecycle = FakeLifecycle(fail_times=99, error="context_overflow: prompt too long")
    ctx, engine = make_engine(lifecycle)
    out = engine.dispatch(goal="scan", target="explore")
    assert out["status"] == "failed"
    assert len(lifecycle.launches) == 1


def test_chain_override_is_honoured():
    lifecycle = FakeLifecycle()
    _, engine = make_engine(lifecycle, {"chains": {"explore": ["litellm/custom-model"]}})
    out = engine.dispatch(goal="scan", target="explore")
    assert out["model"] == "litellm/custom-model"
    assert lifecycle.launches == ["litellm/custom-model"]


def test_category_dispatch_targets_junior_with_category_chain():
    lifecycle = FakeLifecycle()
    _, engine = make_engine(lifecycle)
    out = engine.dispatch(goal="tiny fix", category="quick")
    assert out["agent"].startswith("sisyphus-junior")
    assert out["model"] == "litellm/deepseek-v4-flash"


def test_guard_rejections_propagate():
    _, engine = make_engine(FakeLifecycle())
    with pytest.raises(GuardError):
        engine.dispatch(goal="x", target="sisyphus-junior")
    with pytest.raises(GuardError):
        engine.dispatch(goal="x", target="explore", category="quick")
    with pytest.raises(GuardError):
        engine.dispatch(goal="x", category="nope")


def test_orchestrator_agents_launch_with_orchestrator_role():
    lifecycle = FakeLifecycle()
    _, engine = make_engine(lifecycle)
    captured = {}

    def capture(**kw):
        captured["role"] = "orchestrator" if kw["spec"].orchestrator else "leaf"
        return request_factory(**kw)

    engine._request_factory = capture
    engine.dispatch(goal="plan it", target="atlas")
    assert captured["role"] == "orchestrator"


def test_status_and_tree_report_workers():
    lifecycle = FakeLifecycle()
    _, engine = make_engine(lifecycle)
    out = engine.dispatch(goal="scan", target="explore")
    run_id = out["run_id"]
    status = engine.status(run_id)
    assert status["run_id"] == run_id
    assert "explore" in status["tree"]
    assert status["workers"][0]["status"] == "SUCCEEDED"
    assert engine.status("omo_missing")["error"]


def test_background_dispatch_then_cancel():
    lifecycle = FakeLifecycle()
    ctx, engine = make_engine(lifecycle)
    out = engine.dispatch(goal="long job", target="hephaestus", background=True)
    assert out["status"] == "running"
    assert len(ctx.spawned) == 1
    cancelled = engine.cancel(out["run_id"])
    assert cancelled["cancelled"] == 1
    asyncio.run(ctx.spawned[0])
    assert lifecycle.launches == []
    assert engine.status(out["run_id"])["workers"][0]["status"] == "CANCELLED"


def test_children_inherit_parent_toolsets():
    lifecycle = FakeLifecycle()
    seen = {}

    def factory(*, goal, context, spec, model, toolsets):
        seen[spec.name] = toolsets
        return request_factory(goal=goal, context=context, spec=spec, model=model, toolsets=toolsets)

    ctx = FakeCtx(lifecycle)
    engine = OmoEngine(ctx, lifecycle=lifecycle, request_factory=factory)
    engine.chains = ChainResolver({})
    engine.dispatch(goal="review", target="oracle")
    engine.dispatch(goal="build", target="hephaestus")
    assert seen["oracle"] is None
    assert seen["hephaestus"] is None


def test_read_only_is_the_guards_job_not_the_launch():
    from orchestrator.guards import READ_ONLY_WORKERS, read_only_pre_tool_call

    READ_ONLY_WORKERS.mark("sa-oracle", "oracle")
    assert read_only_pre_tool_call(tool_name="write_file", session_id="sa-oracle")["action"] == "block"
    assert read_only_pre_tool_call(tool_name="write_file", session_id="sa-builder") is None
    READ_ONLY_WORKERS.clear("sa-oracle")
