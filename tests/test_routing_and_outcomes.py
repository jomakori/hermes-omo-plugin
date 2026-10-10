"""Routing is reachable, its verdicts are announced, and its outcomes are scored.

Jev was never consulted in practice: routing only fills the no-explicit-choice case
and every dispatch named an agent, so the flag was inert. `agent="auto"` asks for a
routed pick on purpose. What the report then needs is not whether Jev agreed with
the caller — that only measures conformity — but what each source actually got
delivered, which is what the outcome records joined on `dispatch_id` provide.
"""

from __future__ import annotations

import pytest

from jev.shadow import ShadowLogger
from orchestrator.chains import ChainResolver
from orchestrator.engine import OmoEngine
from orchestrator.guards import GuardError
from orchestrator.models import CANCELLED, FAILED, SUCCEEDED, Worker


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


def _worker(status: str, run_id: str = "omo_test") -> Worker:
    worker = Worker(run_id=run_id, agent_name="debugger", task="fix it", chain=("m",))
    worker.status = status
    return worker


def test_auto_lets_jev_pick_the_specialist(monkeypatch):
    engine = _engine(jev_routing_enabled=True)
    monkeypatch.setattr(engine, "_jev_pick", lambda goal: ("debugger", 1.0, "ok"))
    name, _chain, source = engine._resolve_dispatch_target(
        goal="fix the loop", target="auto", category=None, parent_agent=None
    )
    assert (name, source) == ("debugger", "jev")


def test_an_explicit_agent_is_never_second_guessed():
    engine = _engine(jev_routing_enabled=True)

    def _boom(goal: str) -> tuple[str, float, str]:
        raise AssertionError("Jev must not be consulted when the caller named an agent")

    engine._jev_pick = _boom  # type: ignore[method-assign]
    name, _chain, source = engine._resolve_dispatch_target(
        goal="anything", target="hephaestus", category=None, parent_agent=None
    )
    assert (name, source) == ("hephaestus", "caller")


def test_an_unconfident_pick_is_announced_and_not_acted_on():
    engine = _engine(jev_routing_enabled=True)
    engine._jev_pick = lambda goal: (None, 0.42, "ok")  # type: ignore[method-assign]
    with pytest.raises(GuardError):
        engine._resolve_dispatch_target(goal="ambiguous ask", target="auto", category=None, parent_agent=None)
    assert any(payload.get("source") == "jev-low-confidence" for _name, payload in engine._ctx.events)


def test_nothing_is_routed_without_auto_or_the_flag():
    engine = _engine()

    def _boom(goal: str) -> tuple[str, float, str]:
        raise AssertionError("Jev must not be consulted without auto or the flag")

    engine._jev_pick = _boom  # type: ignore[method-assign]
    with pytest.raises(GuardError):
        engine._resolve_dispatch_target(goal="x", target=None, category=None, parent_agent=None)
    assert engine._ctx.events == []


def test_an_explicit_agent_still_resolves_with_routing_off():
    engine = _engine(jev_routing_enabled=False)
    name, _chain, source = engine._resolve_dispatch_target(
        goal="x", target="hephaestus", category=None, parent_agent=None
    )
    assert (name, source) == ("hephaestus", "caller")


def test_record_outcome_closes_the_pair(tmp_path):
    path = tmp_path / "shadow.jsonl"
    engine = _engine(jev_shadow_enabled=True, jev_shadow_path=str(path))
    engine._dispatch_ids["omo_test"] = "abc123"
    engine._record_outcome(_worker(SUCCEEDED), delivered=True)
    rows = ShadowLogger(path).read()
    assert [row["_type"] for row in rows] == ["outcome"]
    assert rows[0]["dispatch_id"] == "abc123"
    assert rows[0]["delivered"] is True
    assert rows[0]["status"] == SUCCEEDED


def test_record_outcome_ignores_a_run_it_never_shadowed(tmp_path):
    path = tmp_path / "shadow.jsonl"
    engine = _engine(jev_shadow_enabled=True, jev_shadow_path=str(path))
    engine._record_outcome(_worker(SUCCEEDED, run_id="omo_absent"), delivered=True)
    assert ShadowLogger(path).read() == []


def test_record_outcome_skips_a_verdict_that_is_not_about_the_work(tmp_path):
    path = tmp_path / "shadow.jsonl"
    engine = _engine(jev_shadow_enabled=True, jev_shadow_path=str(path))
    for status in (CANCELLED,):
        engine._dispatch_ids["omo_test"] = "abc123"
        engine._record_outcome(_worker(status), delivered=False)
    assert ShadowLogger(path).read() == []


def test_record_outcome_records_a_failure(tmp_path):
    path = tmp_path / "shadow.jsonl"
    engine = _engine(jev_shadow_enabled=True, jev_shadow_path=str(path))
    engine._dispatch_ids["omo_test"] = "abc123"
    engine._record_outcome(_worker(FAILED), delivered=False)
    assert [row["delivered"] for row in ShadowLogger(path).read()] == [False]
