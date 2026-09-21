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
    assert lifecycle.launches == ["deepseek-v4-flash"]
    assert out["model"] == "deepseek-v4-flash"
    assert out["result"] == {"summary": "done", "model": "deepseek-v4-flash"}


def test_dispatch_falls_back_on_retryable_error():
    lifecycle = FakeLifecycle(fail_times=1)
    ctx, engine = make_engine(lifecycle)
    out = engine.dispatch(goal="scan the repo", target="explore")
    assert out["status"] == "succeeded"
    assert lifecycle.launches == ["deepseek-v4-flash", "minimax-m3"]
    assert out["model"] == "minimax-m3"


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
    assert out["model"] == "custom-model"
    assert lifecycle.launches == ["custom-model"]


def test_category_dispatch_targets_junior_with_category_chain():
    lifecycle = FakeLifecycle()
    _, engine = make_engine(lifecycle)
    out = engine.dispatch(goal="tiny fix", category="quick")
    assert out["agent"].startswith("sisyphus-junior")
    assert out["model"] == "deepseek-v4-flash"


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


def test_litellm_prefix_is_stripped_for_hermes():
    lifecycle = FakeLifecycle()
    _, engine = make_engine(lifecycle, {"chains": {"explore": ["litellm/deepseek-v4-flash"]}})
    engine.dispatch(goal="scan", target="explore")
    assert lifecycle.launches == ["deepseek-v4-flash"]


def test_model_aliases_with_a_slash_survive():
    lifecycle = FakeLifecycle()
    _, engine = make_engine(lifecycle, {"chains": {"explore": ["litellm/claude/sonnet-5"]}})
    engine.dispatch(goal="scan", target="explore")
    assert lifecycle.launches == ["claude/sonnet-5"]


class _FailedState:
    name = "FAILED"


class FailingResult:
    terminal_state = _FailedState()
    error_message = "unknown failure with no status or retryable pattern"
    summary = ""


class CreditsResult:
    terminal_state = _FailedState()
    error_message = "HTTP 402: litellm.APIError: Add credits to continue"
    summary = ""


def test_child_terminal_failure_marks_the_run_failed():
    lifecycle = FakeLifecycle()
    lifecycle.result = lambda handle: FailingResult()
    _, engine = make_engine(lifecycle)
    out = engine.dispatch(goal="scan", target="explore")
    assert out["status"] == "failed"
    assert "unknown failure" in out["error"]
    assert len(lifecycle.launches) == 1


def test_child_failure_walks_the_chain_when_retryable():
    lifecycle = FakeLifecycle()
    lifecycle.result = lambda handle: CreditsResult()
    _, engine = make_engine(
        lifecycle,
        {
            "chains": {"explore": ["deepseek-v4-flash", "minimax-m3", "claude-haiku-4-5"]},
            "runtime_fallback": {"retry_on_errors": [402], "max_fallback_attempts": 3},
        },
    )
    out = engine.dispatch(goal="scan", target="explore")
    assert lifecycle.launches == ["deepseek-v4-flash", "minimax-m3", "claude-haiku-4-5"]
    assert out["status"] == "failed"
    assert "Add credits" in out["error"]


def test_child_failure_does_not_walk_when_the_code_is_not_configured():
    lifecycle = FakeLifecycle()
    lifecycle.result = lambda handle: CreditsResult()
    _, engine = make_engine(lifecycle, {"runtime_fallback": {"retry_on_errors": [429]}})
    out = engine.dispatch(goal="scan", target="explore")
    assert len(lifecycle.launches) == 1
    assert out["status"] == "failed"


def test_fallback_can_be_disabled():
    lifecycle = FakeLifecycle(fail_times=99)
    _, engine = make_engine(lifecycle, {"runtime_fallback": {"enabled": False}})
    out = engine.dispatch(goal="scan", target="explore")
    assert out["status"] == "failed"
    assert len(lifecycle.launches) == 1


def test_retry_on_errors_is_configurable():
    lifecycle = FakeLifecycle(fail_times=99, error="HTTP 429 from provider")
    _, engine = make_engine(lifecycle, {"runtime_fallback": {"retry_on_errors": [500]}})
    out = engine.dispatch(goal="scan", target="explore")
    assert out["status"] == "failed"
    assert len(lifecycle.launches) == 1

    lifecycle2 = FakeLifecycle(fail_times=1, error="HTTP 429 from provider")
    _, engine2 = make_engine(lifecycle2, {"runtime_fallback": {"retry_on_errors": [429]}})
    assert engine2.dispatch(goal="scan", target="explore")["status"] == "succeeded"
    assert len(lifecycle2.launches) == 2


def test_nested_runtime_fallback_overrides_flat_keys():
    from orchestrator.chains import ChainResolver

    r = ChainResolver({"max_fallback_attempts": 9, "runtime_fallback": {"max_fallback_attempts": 2}})
    assert r.state_for("explore", ("a", "b", "c")).max_attempts == 2


def test_status_is_extracted_from_the_error_message():
    from orchestrator.chains import status_from_message

    assert status_from_message("HTTP 400: Invalid model name") == 400
    assert status_from_message("litellm.APIError: 503 service down") == 503
    assert status_from_message("connection reset by peer") is None
