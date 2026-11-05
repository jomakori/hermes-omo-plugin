"""Wiring: OmoEngine._maybe_dispatch_self_patch, called from _outcome/_restore.

build_self_patch_task (orchestrator.self_patch) is a pure builder with its own
tests (tests/test_self_patch.py). These tests cover only the wiring: does a
finished worker's terminal_state/crash_reason actually trigger a new
background dispatch, through the engine's own `dispatch()` -- and does the
recursion guard/feature flag/non-triggering crash_reason correctly produce
*zero* dispatch calls.
"""

from __future__ import annotations

import dataclasses

from orchestrator.engine import OmoEngine
from orchestrator.models import FAILED, INTERRUPTED, Worker
from orchestrator.self_patch import SELF_PATCH_ORIGIN


class FakeCtx:
    def __init__(self, config=None):
        self._config = config or {}

    def get_config(self, key, default=None):
        return self._config.get(key, default)


def make_engine(config=None):
    engine = OmoEngine(FakeCtx(config))
    return engine


def make_worker(*, status, crash_reason, origin="", run_id="omo_test01"):
    return Worker(
        run_id=run_id,
        agent_name="explore",
        task="do the thing",
        chain=("deepseek-v4-flash-direct",),
        status=status,
        crash_reason=crash_reason,
        origin=origin,
    )


def spy_dispatch(engine):
    """Replace engine.dispatch with a recorder; return the list of call kwargs."""
    calls: list[dict] = []

    def _fake_dispatch(**kwargs):
        calls.append(kwargs)
        return {"run_id": "omo_spawned", "status": "running"}

    engine.dispatch = _fake_dispatch  # type: ignore[method-assign]
    return calls


def test_failed_retained_with_no_origin_triggers_one_tagged_dispatch():
    engine = make_engine()
    calls = spy_dispatch(engine)
    worker = make_worker(
        status=FAILED,
        crash_reason={"status": "RETAINED", "reason": "RuntimeError: boom"},
    )
    engine._maybe_dispatch_self_patch(worker)
    assert len(calls) == 1
    assert calls[0]["origin"] == SELF_PATCH_ORIGIN
    assert calls[0]["background"] is True
    assert "boom" in calls[0]["goal"]


def test_self_patch_origin_worker_never_spawns_a_second_one():
    """Recursion guard: a self-patch run's own failure must not spawn another."""
    engine = make_engine()
    calls = spy_dispatch(engine)
    worker = make_worker(
        status=FAILED,
        crash_reason={"status": "RETAINED", "reason": "RuntimeError: boom again"},
        origin=SELF_PATCH_ORIGIN,
    )
    engine._maybe_dispatch_self_patch(worker)
    assert calls == []


def test_withheld_crash_reason_triggers_nothing():
    engine = make_engine()
    calls = spy_dispatch(engine)
    worker = make_worker(status=FAILED, crash_reason={"status": "WITHHELD"})
    engine._maybe_dispatch_self_patch(worker)
    assert calls == []


def test_feature_flag_disabled_suppresses_an_otherwise_triggering_failure():
    engine = make_engine({"self_patch_enabled": False})
    calls = spy_dispatch(engine)
    worker = make_worker(
        status=FAILED,
        crash_reason={"status": "RETAINED", "reason": "RuntimeError: boom"},
    )
    engine._maybe_dispatch_self_patch(worker)
    assert calls == []


def test_interrupted_with_empty_crash_reason_triggers_one_dispatch():
    engine = make_engine()
    calls = spy_dispatch(engine)
    worker = make_worker(status=INTERRUPTED, crash_reason={"status": "EMPTY"})
    engine._maybe_dispatch_self_patch(worker)
    assert len(calls) == 1
    assert calls[0]["origin"] == SELF_PATCH_ORIGIN


def test_feature_flag_default_is_on():
    """self_patch_enabled follows the jev_enabled pattern: default True (on)."""
    engine = make_engine()  # no config at all
    calls = spy_dispatch(engine)
    worker = make_worker(status=FAILED, crash_reason={"status": "EMPTY"})
    engine._maybe_dispatch_self_patch(worker)
    assert len(calls) == 1


def test_wiring_does_not_mutate_the_finished_worker():
    """Purely additive: the worker's own terminal_state/crash_reason/status are untouched."""
    engine = make_engine()
    spy_dispatch(engine)
    worker = make_worker(
        status=FAILED,
        crash_reason={"status": "RETAINED", "reason": "RuntimeError: boom"},
    )
    before = dataclasses.replace(worker)
    engine._maybe_dispatch_self_patch(worker)
    assert worker.status == before.status
    assert worker.crash_reason == before.crash_reason
    assert worker.origin == before.origin
