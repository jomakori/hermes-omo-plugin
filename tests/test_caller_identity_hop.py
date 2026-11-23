"""Who is calling: the identity that makes the coordinator's second hop reachable.

The second hop (``_coordinator_second_hop``) fires when a dispatch's
``parent_agent`` is the configured ``coordinator_agent``. Nothing used to set
that field, so the branch was dead. These tests pin the identity source (the
calling agent's host subagent id, matched back to the OMO worker this process
launched under it) and both outcomes: the coordinator's own dispatch is placed
by Jev, and every other caller still gets the first hop it always got.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import unittest.mock as mock

import pytest

from omo_tools.omo_task_tool import make_omo_task_handler
from omo_tools.omo_tool import make_omo_handler
from orchestrator import host_bridge
from orchestrator.chains import ChainResolver
from orchestrator.engine import OmoEngine
from orchestrator.guards import GuardError


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
    """A launch handle carrying the id the host would stamp on the child."""

    def __init__(self, model, subagent_id):
        self.model = model
        self.subagent_id = subagent_id


class _FakeLifecycle:
    def __init__(self):
        self.launches = []

    def launch(self, request):
        self.launches.append(request.model)
        return _FakeHandle(request.model, f"sa_{len(self.launches)}")

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


def _calling_agent(monkeypatch, agent):
    monkeypatch.setattr(host_bridge, "_resolve_parent_agent", lambda: agent)


def _agent_with_subagent_id(subagent_id):
    return type("_CallingAgent", (), {"_subagent_id": subagent_id})()


def _agent_without_subagent_id():
    return type("_RootAgent", (), {})()


def _launch_coordinator(engine):
    """One dispatch from chat side: the coordinator is the first hop and stays alive."""
    out = engine.dispatch(goal="plan the work")
    assert out["status"] == "succeeded"
    assert out["agent"] == "sisyphus · Ultraworker"
    return out


def test_the_root_agent_has_no_subagent_id(monkeypatch):
    _calling_agent(monkeypatch, None)
    assert host_bridge.calling_subagent_id() == ""

    _calling_agent(monkeypatch, _agent_without_subagent_id())
    assert host_bridge.calling_subagent_id() == ""

    _calling_agent(monkeypatch, _agent_with_subagent_id("sa_7"))
    assert host_bridge.calling_subagent_id() == "sa_7"


def test_a_calling_worker_is_named_from_the_handle_it_was_launched_under(monkeypatch):
    ctx, engine, _lifecycle = _make_engine({"coordinator_agent": "sisyphus"})
    _calling_agent(monkeypatch, None)
    assert engine._calling_agent_name() is None

    _launch_coordinator(engine)
    _calling_agent(monkeypatch, _agent_with_subagent_id("sa_1"))
    assert engine._calling_agent_name() == "sisyphus"

    # An id this process never launched is not a caller, not a guess.
    _calling_agent(monkeypatch, _agent_with_subagent_id("sa_unknown"))
    assert engine._calling_agent_name() is None


def test_the_coordinators_own_dispatch_is_placed_by_jev(monkeypatch):
    ctx, engine, lifecycle = _make_engine({"coordinator_agent": "sisyphus"})
    _calling_agent(monkeypatch, None)
    _launch_coordinator(engine)
    assert _route_events(ctx)[-1]["source"] == "coordinator"

    _calling_agent(monkeypatch, _agent_with_subagent_id("sa_1"))
    with mock.patch.object(engine, "_jev_pick", return_value=("explore", 0.9, "ok")) as pick:
        out = engine.dispatch(goal="scan the tree")

    assert out["status"] == "succeeded"
    assert out["agent"] == "explore · Repository Exploration"
    # Jev was asked with the coordinator excluded: the loop cannot route into itself.
    assert pick.call_args.kwargs["exclude"] == frozenset({"sisyphus"})
    # Only the specialist was launched; the caller did not become a second coordinator.
    assert lifecycle.launches == ["claude-sonnet-5", "deepseek-v4-flash-direct"]
    assert [event["source"] for event in _route_events(ctx)][-1] == "jev"


def test_a_dispatch_from_chat_still_lands_on_the_coordinator(monkeypatch):
    ctx, engine, lifecycle = _make_engine({"coordinator_agent": "sisyphus"})
    _calling_agent(monkeypatch, _agent_without_subagent_id())
    with mock.patch.object(engine, "_jev_pick") as pick:
        out = engine.dispatch(goal="plan the work")

    assert out["agent"] == "sisyphus · Ultraworker"
    pick.assert_not_called()
    assert _route_events(ctx)[-1]["source"] == "coordinator"
    assert lifecycle.launches == ["claude-sonnet-5"]


def test_a_specialist_worker_dispatching_still_lands_on_the_coordinator(monkeypatch):
    ctx, engine, lifecycle = _make_engine({"coordinator_agent": "sisyphus"})
    _calling_agent(monkeypatch, None)
    engine.dispatch(goal="write the parser", target="hephaestus")

    _calling_agent(monkeypatch, _agent_with_subagent_id("sa_1"))
    with mock.patch.object(engine, "_jev_pick") as pick:
        out = engine.dispatch(goal="plan the work")

    assert out["agent"] == "sisyphus · Ultraworker"
    pick.assert_not_called()
    assert _route_events(ctx)[-1]["source"] == "coordinator"
    assert lifecycle.launches == ["claude-sonnet-5", "claude-sonnet-5"]


def test_an_explicit_target_from_the_coordinator_is_never_second_guessed(monkeypatch):
    ctx, engine, lifecycle = _make_engine({"coordinator_agent": "sisyphus"})
    _calling_agent(monkeypatch, None)
    _launch_coordinator(engine)

    _calling_agent(monkeypatch, _agent_with_subagent_id("sa_1"))
    with mock.patch.object(engine, "_jev_pick") as pick:
        out = engine.dispatch(goal="scan the tree", target="explore")

    assert out["agent"] == "explore · Repository Exploration"
    pick.assert_not_called()
    assert _route_events(ctx)[-1]["source"] == "static"
    assert lifecycle.launches == ["claude-sonnet-5", "deepseek-v4-flash-direct"]


def test_an_explicit_parent_agent_outranks_the_detected_caller(monkeypatch):
    ctx, engine, _lifecycle = _make_engine({"coordinator_agent": "sisyphus"})
    _calling_agent(monkeypatch, None)
    _launch_coordinator(engine)

    _calling_agent(monkeypatch, _agent_with_subagent_id("sa_1"))
    out = engine.dispatch(goal="plan the work", parent_agent="deep")

    assert out["agent"] == "sisyphus · Ultraworker"
    assert _route_events(ctx)[-1]["source"] == "coordinator"


def test_an_unconfident_second_hop_refuses_instead_of_delegating_blind(monkeypatch):
    ctx, engine, lifecycle = _make_engine({"coordinator_agent": "sisyphus"})
    _calling_agent(monkeypatch, None)
    _launch_coordinator(engine)
    launches_before = list(lifecycle.launches)

    _calling_agent(monkeypatch, _agent_with_subagent_id("sa_1"))
    # What _jev_pick returns when the pick is under jev_routing_threshold.
    with mock.patch.object(engine, "_jev_pick", return_value=(None, 0.4, "ok")):
        with pytest.raises(GuardError, match="sisyphus"):
            engine.dispatch(goal="scan the tree")

    assert lifecycle.launches == launches_before


# ── the tool boundary: a refusal caused by the caller identity is rendered ──
def test_a_refused_second_hop_reaches_the_model_as_a_rejection(monkeypatch):
    """An unconfident second hop is a caller error the model can act on, so the
    omo tool must hand it back rendered — the same contract as a rejected graph."""
    ctx, engine, lifecycle = _make_engine({"coordinator_agent": "sisyphus"})
    _calling_agent(monkeypatch, None)
    _launch_coordinator(engine)
    launches_before = list(lifecycle.launches)

    _calling_agent(monkeypatch, _agent_with_subagent_id("sa_1"))
    with mock.patch.object(engine, "_jev_pick", return_value=(None, 0.4, "ok")):
        payload = json.loads(asyncio.run(make_omo_handler(engine)({"action": "dispatch", "goal": "scan the tree"})))

    assert payload["rejected"] is True
    assert "sisyphus" in payload["error"]
    assert lifecycle.launches == launches_before


def test_a_live_caller_identity_now_reaches_the_lineage_guard(monkeypatch):
    """omo_task always names its target, so the guard it can newly hit is the
    lineage one: a planning worker may not delegate into the planning family."""
    ctx, engine, lifecycle = _make_engine({})
    _calling_agent(monkeypatch, None)
    engine.dispatch(goal="write the plan", target="prometheus")

    _calling_agent(monkeypatch, _agent_with_subagent_id("sa_1"))
    launches_before = list(lifecycle.launches)
    payload = json.loads(
        asyncio.run(make_omo_task_handler(engine)({"agent": "prometheus", "prompt": "write the plan again"}))
    )

    assert payload["rejected"] is True
    assert lifecycle.launches == launches_before
