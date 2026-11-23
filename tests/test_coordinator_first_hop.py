"""An unnamed dispatch starts at the coordinator; Jev places the specialist.

A dispatch that names nobody used to land on whatever the static default chain
was, with no coordinator in the loop at all. With ``coordinator_agent`` set, that
first hop is the coordinator's, announced as ``coordinator``; the specialist hop is
Jev's, and a coordinator that gets no confident pick is told to name one instead of
being handed a default lane that would burn a worker for its whole budget.

A dispatch that names an agent or category is still never second-guessed: forcing
a coordinator over an explicit target would make targeted work impossible.
"""

from __future__ import annotations

import dataclasses

import pytest

from orchestrator.chains import ChainResolver
from orchestrator.engine import OmoEngine
from orchestrator.guards import GuardError


class _Ctx:
    """The engine's config lookup and its progress emitter."""

    def __init__(self, config: dict | None = None) -> None:
        self.config = config or {}
        self.events: list[tuple[str, dict]] = []

    def get_config(self, key: str, default: object = None) -> object:
        return self.config.get(key, default)

    def emit(self, name: str, payload: dict) -> None:
        self.events.append((name, payload))


def _engine(**config: object) -> OmoEngine:
    """An engine with just what target resolution touches: config, events, chains."""
    engine = OmoEngine.__new__(OmoEngine)
    engine._ctx = _Ctx(config)
    engine._dispatch_ids = {}
    engine.chains = ChainResolver(config)
    return engine


def _routes(engine: OmoEngine) -> list[dict]:
    return [payload for name, payload in engine._ctx.events if name == "jev_route"]


def test_an_unnamed_dispatch_starts_at_the_coordinator():
    engine = _engine(coordinator_agent="sisyphus")
    name, _chain, source = engine._resolve_dispatch_target(
        goal="build the thing", target=None, category=None, parent_agent=None
    )
    assert (name, source) == ("sisyphus", "coordinator")
    assert _routes(engine) == [
        {"source": "coordinator", "confidence": 0.0, "target": "sisyphus", "status": "configured"}
    ]


def test_the_coordinator_wins_with_routing_off():
    # The coordinator rule runs before the flag: an unnamed dispatch belongs to the
    # coordinator even when `jev_routing_enabled` is false, or the coordinator could
    # be switched off by a setting that was never about it.
    engine = _engine(coordinator_agent="sisyphus", jev_routing_enabled=False)

    def _boom(goal: str) -> tuple[str, float, str]:
        raise AssertionError("the first hop is configured, not routed")

    engine._jev_pick = _boom  # type: ignore[method-assign]
    name, _chain, source = engine._resolve_dispatch_target(
        goal="build the thing", target=None, category=None, parent_agent=None
    )
    assert (name, source) == ("sisyphus", "coordinator")


def test_an_explicit_agent_is_still_never_second_guessed():
    engine = _engine(coordinator_agent="sisyphus")

    def _boom(goal: str) -> tuple[str, float, str]:
        raise AssertionError("neither the coordinator nor Jev may re-route an explicit target")

    engine._jev_pick = _boom  # type: ignore[method-assign]
    name, _chain, source = engine._resolve_dispatch_target(
        goal="critique this plan", target="momus", category=None, parent_agent=None
    )
    assert (name, source) == ("momus", "caller")
    assert _routes(engine) == [{"source": "static", "confidence": 0.0, "target": "momus", "status": "explicit"}]


def test_the_explicit_category_is_never_second_guessed_either():
    engine = _engine(coordinator_agent="sisyphus")
    name, _chain, source = engine._resolve_dispatch_target(
        goal="quick cleanup", target=None, category="quick", parent_agent=None
    )
    assert (name, source) == ("sisyphus-junior", "caller")
    assert _routes(engine) == [
        {"source": "static", "confidence": 0.0, "target": "sisyphus-junior", "status": "explicit"}
    ]


def test_the_coordinators_own_dispatch_is_jevs_pick():
    engine = _engine(coordinator_agent="sisyphus")
    engine._jev_pick = lambda goal, exclude=None: ("debugger", 0.91, "ok")  # type: ignore[method-assign]
    name, _chain, source = engine._resolve_dispatch_target(
        goal="fix the crash", target=None, category=None, parent_agent="sisyphus"
    )
    assert (name, source) == ("debugger", "jev")
    assert _routes(engine) == [{"source": "jev", "confidence": 0.91, "target": "debugger", "status": "ok"}]


def test_the_coordinators_second_hop_offers_jev_no_coordinator():
    # The loop guard has to hold in the candidate set Jev is scored against, not only
    # after the fact: a coordinator that can be picked is a coordinator that can be
    # asked to delegate to itself.
    import jev.routing as routing

    seen: dict[str, object] = {}

    def _route(*, valid_targets, candidates, **_kwargs):
        seen["valid_targets"] = set(valid_targets)
        seen["candidates"] = set(candidates)
        return "debugger", 0.9, "ok"

    engine = _engine(coordinator_agent="sisyphus")
    engine._health_cache = None  # type: ignore[attr-defined]
    original = routing.route_agent
    routing.route_agent = _route
    try:
        name, _chain, source = engine._resolve_dispatch_target(
            goal="fix the crash", target=None, category=None, parent_agent="sisyphus"
        )
    finally:
        routing.route_agent = original
    assert (name, source) == ("debugger", "jev")
    assert "sisyphus" not in seen["valid_targets"]
    assert "sisyphus" not in seen["candidates"]
    assert "debugger" in seen["candidates"]


def test_a_pick_equal_to_the_coordinator_is_no_pick():
    engine = _engine(coordinator_agent="sisyphus")
    engine._jev_pick = lambda goal, exclude=None: ("sisyphus", 0.99, "ok")  # type: ignore[method-assign]
    with pytest.raises(GuardError):
        engine._resolve_dispatch_target(goal="build it", target=None, category=None, parent_agent="sisyphus")
    assert _routes(engine)[-1]["source"] == "jev-low-confidence"


def test_an_unconfident_second_hop_raises_instead_of_a_default_lane():
    engine = _engine(coordinator_agent="sisyphus")
    engine._jev_pick = lambda goal, exclude=None: (None, 0.42, "ok")  # type: ignore[method-assign]
    with pytest.raises(GuardError, match="No confident specialist"):
        engine._resolve_dispatch_target(goal="ambiguous", target=None, category=None, parent_agent="sisyphus")
    assert _routes(engine) == [{"source": "jev-low-confidence", "confidence": 0.42, "target": "", "status": "ok"}]


def test_no_coordinator_configured_keeps_todays_static_behaviour():
    engine = _engine()

    def _boom(goal: str) -> tuple[str, float, str]:
        raise AssertionError("nothing is routed without a coordinator, auto, or the flag")

    engine._jev_pick = _boom  # type: ignore[method-assign]
    with pytest.raises(GuardError):
        engine._resolve_dispatch_target(goal="x", target=None, category=None, parent_agent=None)
    assert engine._ctx.events == []


def test_a_coordinator_that_cannot_be_resolved_is_reported_not_swallowed():
    # A typo in `coordinator_agent` must not quietly hand the dispatch to the default
    # lane: the operator believes routing is on, so the error has to name the setting.
    engine = _engine(coordinator_agent="not-an-agent")
    with pytest.raises(GuardError, match="coordinator_agent 'not-an-agent'"):
        engine._resolve_dispatch_target(goal="build it", target=None, category=None, parent_agent=None)
    assert _routes(engine) == []


# ── the whole dispatch path ───────────────────────────────────────────────
@dataclasses.dataclass
class _FakeRequest:
    goal: str
    context: str | None
    role: str
    model: str | None
    allowed_toolsets: tuple
    metadata: dict


def _request_factory(*, goal, context, spec, model, toolsets):
    return _FakeRequest(
        goal=goal,
        context=context,
        role="orchestrator" if spec.orchestrator else "leaf",
        model=model,
        allowed_toolsets=toolsets,
        metadata={"omo_agent": spec.name},
    )


class _FakeHandle:
    def __init__(self, model):
        self.model = model


class _FakeLifecycle:
    def __init__(self):
        self.requests = []

    def launch(self, request):
        self.requests.append(request)
        return _FakeHandle(request.model)

    def wait(self, handle, *, timeout_seconds=None):
        return None

    def result(self, handle):
        return {"summary": "done", "model": handle.model}

    def cancel(self, handle, *, reason=""):
        return None


def _live_engine(config: dict) -> tuple[_Ctx, OmoEngine, _FakeLifecycle]:
    """A real engine over the fake lifecycle, so `dispatch` actually runs."""
    lifecycle = _FakeLifecycle()
    ctx = _Ctx(config)
    ctx.subagent_lifecycle = lifecycle
    ctx.spawn_task = lambda coro: None
    engine = OmoEngine(ctx, lifecycle=lifecycle, request_factory=_request_factory)
    engine.chains = ChainResolver(config)
    return ctx, engine, lifecycle


def test_a_full_unnamed_dispatch_launches_the_coordinator():
    _ctx, engine, lifecycle = _live_engine({"coordinator_agent": "sisyphus"})
    out = engine.dispatch(goal="build the thing")
    assert out["agent"] == "sisyphus · Ultraworker"
    assert lifecycle.requests[0].role == "orchestrator"
    assert _routes(engine) == [
        {"source": "coordinator", "confidence": 0.0, "target": "sisyphus", "status": "configured"}
    ]
