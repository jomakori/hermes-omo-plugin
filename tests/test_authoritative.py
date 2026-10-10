from __future__ import annotations

import dataclasses
import unittest.mock as mock

import pytest

from jev.policy import authoritative_target
from jev.routing import route_agent
from orchestrator.chains import ChainResolver
from orchestrator.engine import OmoEngine
from orchestrator.guards import GuardError

_VALID = {"sisyphus", "prometheus", "explore", "quick"}


def test_authoritative_target_rule():
    assert authoritative_target({"value": "prometheus", "confidence": 0.9}, _VALID, 0.75) == "prometheus"
    assert authoritative_target({"value": "prometheus", "confidence": 0.75}, _VALID, 0.75) == "prometheus"
    assert authoritative_target({"value": "prometheus", "confidence": 0.74}, _VALID, 0.75) is None
    assert authoritative_target({"value": "hallucinated", "confidence": 0.99}, _VALID, 0.75) is None
    assert authoritative_target({"value": None, "confidence": 0.99}, _VALID, 0.75) is None
    assert authoritative_target({"value": "prometheus"}, _VALID, 0.75) is None
    assert authoritative_target({"value": "prometheus", "confidence": True}, _VALID, 0.75) is None
    assert authoritative_target(None, _VALID, 0.75) is None


def _route(response=None, raises=False):
    client = mock.MagicMock()
    if raises:
        client.call.side_effect = RuntimeError("boom")
    else:
        client.call.return_value = (response, 5.0)
    return client


def test_route_agent_accepts_confident_valid_pick():
    response = {"model": "jev-latest", "answers": {"agent": {"choice": "prometheus", "confidence": 0.9}}}
    with mock.patch("jev.routing.JevClient", return_value=_route(response)):
        with mock.patch.dict("os.environ", {"TYPESAFE_AI_API_KEY": "k"}):
            target, confidence, status = route_agent(
                state={"user_message": "plan the work"},
                valid_targets=_VALID,
                candidates={name: name for name in _VALID},
                threshold=0.75,
                base_url="https://api.typesafe.ai",
                model="jev-latest",
                timeout_s=5.0,
            )
    assert (target, status) == ("prometheus", "ok")
    assert confidence == pytest.approx(0.9)


def test_route_agent_rejects_low_confidence_and_invalid():
    low = {"answers": {"agent": {"choice": "prometheus", "confidence": 0.4}}}
    invalid = {"answers": {"agent": {"choice": "hallucinated", "confidence": 0.99}}}
    for response in (low, invalid):
        with mock.patch("jev.routing.JevClient", return_value=_route(response)):
            with mock.patch.dict("os.environ", {"TYPESAFE_AI_API_KEY": "k"}):
                target, _confidence, status = route_agent(
                    state={"user_message": "x"},
                    valid_targets=_VALID,
                    candidates={name: name for name in _VALID},
                    threshold=0.75,
                    base_url="https://api.typesafe.ai",
                    model="jev-latest",
                    timeout_s=5.0,
                )
        assert target is None
        assert status == "ok"


def test_route_agent_fail_open_on_error_and_missing_key():
    with mock.patch("jev.routing.JevClient", return_value=_route(raises=True)):
        with mock.patch.dict("os.environ", {"TYPESAFE_AI_API_KEY": "k"}):
            assert route_agent(
                state={},
                valid_targets=_VALID,
                threshold=0.75,
                base_url="https://api.typesafe.ai",
                model="jev-latest",
                timeout_s=5.0,
            ) == (None, 0.0, "error")

    with mock.patch.dict("os.environ", {}, clear=True):
        assert route_agent(
            state={},
            valid_targets=_VALID,
            threshold=0.75,
            base_url="https://api.typesafe.ai",
            model="jev-latest",
            timeout_s=5.0,
        ) == (None, 0.0, "unavailable")


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
        self.launches = []

    def launch(self, request):
        self.launches.append(request.model)
        return _FakeHandle(request.model)

    def wait(self, handle, *, timeout_seconds=None):
        return None

    def result(self, handle):
        return {"summary": "done", "model": handle.model}

    def cancel(self, handle, *, reason=""):
        return None


class _FakeCtx:
    def __init__(self, lifecycle, config):
        self.subagent_lifecycle = lifecycle
        self._config = config
        self.events = []

    def get_config(self, key, default=None):
        return self._config.get(key, default)

    def emit(self, event, payload=None):
        self.events.append((event, payload))

    def spawn_task(self, coro):
        return None


def _make_engine(config):
    lifecycle = _FakeLifecycle()
    ctx = _FakeCtx(lifecycle, config)
    engine = OmoEngine(ctx, lifecycle=lifecycle, request_factory=_request_factory)
    engine.chains = ChainResolver(config)
    return ctx, engine, lifecycle


def _route_events(ctx):
    return [payload for event, payload in ctx.events if event == "jev_route"]


def test_flag_off_target_is_byte_identical():
    ctx, engine, lifecycle = _make_engine({})
    expected = engine._resolve_target(target="explore", category=None, parent_agent=None)
    got = engine._resolve_dispatch_target(goal="scan", target="explore", category=None, parent_agent=None)
    assert got == (*expected, "caller")

    out = engine.dispatch(goal="scan", target="explore")
    assert out["status"] == "succeeded"
    assert out["agent"] == "explore · Repository Exploration"
    assert lifecycle.launches == ["deepseek-v4-flash-direct"]
    assert _route_events(ctx) == []


def test_the_flag_covers_the_unnamed_dispatch():
    # With the flag on, a dispatch that names nobody is Jev's to place.
    ctx, engine, _lifecycle = _make_engine({"jev_routing_enabled": True})
    with mock.patch.object(engine, "_jev_pick", return_value=("explore", 0.9, "ok")) as pick:
        out = engine.dispatch(goal="scan the tree")

    assert out["status"] == "succeeded"
    assert out["agent"] == "explore · Repository Exploration"
    pick.assert_called_once()
    assert _route_events(ctx)[0]["source"] == "jev"


def test_auto_asks_for_a_routed_pick():
    ctx, engine, _lifecycle = _make_engine({})
    with mock.patch.object(engine, "_jev_pick", return_value=("explore", 0.9, "ok")):
        out = engine.dispatch(goal="scan the tree", target="auto")

    assert out["agent"] == "explore · Repository Exploration"
    assert _route_events(ctx)[0] == {"source": "jev", "confidence": 0.9, "target": "explore", "status": "ok"}


def test_auto_without_a_confident_pick_is_refused():
    ctx, engine, _lifecycle = _make_engine({})
    with mock.patch.object(engine, "_jev_pick", return_value=(None, 0.2, "ok")):
        with pytest.raises(GuardError):
            engine.dispatch(goal="something vague", target="auto")

    assert _route_events(ctx)[0]["source"] == "jev-low-confidence"


def test_auto_asks_jev_even_with_the_flag_off():
    ctx, engine, _lifecycle = _make_engine({})
    with mock.patch.object(engine, "_jev_pick", return_value=("explore", 0.9, "ok")) as pick:
        out = engine.dispatch(goal="scan the tree", target="auto")

    pick.assert_called_once()
    assert out["agent"] == "explore · Repository Exploration"
    assert _route_events(ctx)[0]["source"] == "jev"


def test_flag_on_explicit_agent_wins_and_jev_is_not_called():
    ctx, engine, lifecycle = _make_engine({"jev_routing_enabled": True})
    with mock.patch.object(engine, "_jev_pick") as pick:
        out = engine.dispatch(goal="scan", target="explore")

    assert out["status"] == "succeeded"
    assert lifecycle.launches == ["deepseek-v4-flash-direct"]
    pick.assert_not_called()
    assert _route_events(ctx) == [{"source": "static", "confidence": 0.0, "target": "explore", "status": "explicit"}]


def test_flag_on_confident_valid_pick_is_used():
    ctx, engine, _lifecycle = _make_engine({"jev_routing_enabled": True})
    with mock.patch.object(engine, "_jev_pick", return_value=("prometheus", 0.9, "ok")):
        out = engine.dispatch(goal="plan the migration")

    assert out["status"] == "succeeded"
    assert out["agent"] == "prometheus · Plan Builder"
    assert _route_events(ctx)[0]["source"] == "jev"
    assert _route_events(ctx)[0]["target"] == "prometheus"


def test_flag_on_low_confidence_is_announced_and_falls_back_to_static():
    ctx, engine, _lifecycle = _make_engine({"jev_routing_enabled": True})
    with mock.patch.object(engine, "_jev_pick", return_value=(None, 0.4, "ok")):
        with pytest.raises(GuardError):
            engine.dispatch(goal="plan the migration")

    assert _route_events(ctx) == [{"source": "jev-low-confidence", "confidence": 0.4, "target": "", "status": "ok"}]


def test_flag_on_invalid_pick_is_announced_and_falls_back_to_static():
    ctx, engine, _lifecycle = _make_engine({"jev_routing_enabled": True})
    with mock.patch.object(engine, "_jev_pick", return_value=(None, 0.9, "ok")):
        with pytest.raises(GuardError):
            engine.dispatch(goal="do something odd")

    assert _route_events(ctx)[0]["source"] == "jev-low-confidence"


def test_flag_on_jev_error_is_announced_as_unavailable():
    ctx, engine, _lifecycle = _make_engine({"jev_routing_enabled": True})
    with mock.patch.object(engine, "_jev_pick", return_value=(None, 0.0, "error")):
        with pytest.raises(GuardError):
            engine.dispatch(goal="plan the migration")

    assert _route_events(ctx) == [{"source": "jev-unavailable", "confidence": 0.0, "target": "", "status": "error"}]


def test_flag_on_rejects_out_of_vocabulary_pick_from_resolution():
    # A pick that survives the helper but fails delegation/validity must not route.
    ctx, engine, _lifecycle = _make_engine({"jev_routing_enabled": True})
    with mock.patch.object(engine, "_jev_pick", return_value=("nonexistent-agent", 0.99, "ok")):
        with pytest.raises(GuardError):
            engine.dispatch(goal="plan the migration")

    assert _route_events(ctx)[0]["source"] == "jev-low-confidence"
