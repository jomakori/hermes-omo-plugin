"""Bounded automatic re-dispatch, by failure class (see orchestrator/retry_policy.py).

Every test injects its own `sleep`/`backoff` so nothing here ever really sleeps,
and drives `RetryCoordinator` directly against hand-built `Worker`/`Run` records
rather than the full engine, so each failure class is isolated and deterministic.
"""

from __future__ import annotations

from typing import Any

import pytest

from orchestrator.chains import HOP_REASON_PROVIDER_UNUSABLE
from orchestrator.models import BLOCKED, FAILED, PENDING, RUNNING, SUCCEEDED, UNAUTHENTICATED, Run, Worker
from orchestrator.retry_policy import (
    DEPENDENCY_BLOCKED,
    PERMANENT,
    PROVIDER_UNUSABLE,
    RESUME_NOTE,
    STALL,
    TIMEOUT,
    VERIFIER_MISMATCH,
    LiveWorkerError,
    RetryCoordinator,
    classify_failure,
    narrow_goal,
)


class DispatchRecorder:
    """Stands in for OmoEngine.dispatch: records every call, never launches anything."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"run_id": "auto-retry"}


def make_worker(**overrides: Any) -> Worker:
    base = dict(run_id="r1", agent_name="hephaestus", task="goal text", chain=())
    base.update(overrides)
    return Worker(**base)


def make_run() -> Run:
    return Run(run_id="r1", goal="goal text")


def make_coordinator(**overrides: Any) -> tuple[RetryCoordinator, DispatchRecorder]:
    dispatch = DispatchRecorder()
    kwargs: dict[str, Any] = dict(dispatch=dispatch, sleep=lambda _s: None, backoff=lambda _attempt: 0.0)
    kwargs.update(overrides)
    return RetryCoordinator(**kwargs), dispatch


# ── TIMEOUT: exactly one retry, narrowed goal, never byte-identical ─────────


def test_timeout_retries_once_with_a_narrowed_goal():
    long_goal = "Implement the entire feature end to end. " * 30
    worker = make_worker(task=long_goal, status=FAILED, error="worker hit the 1800s timeout cap")
    run = make_run()
    coordinator, dispatch = make_coordinator()

    first = coordinator.evaluate(run, worker)

    assert first is None
    assert len(dispatch.calls) == 1
    retried_goal = dispatch.calls[0]["goal"]
    assert retried_goal != long_goal
    assert len(retried_goal) < len(long_goal)
    assert worker.failure_class == TIMEOUT


def test_timeout_escalates_after_its_one_retry_is_exhausted():
    worker = make_worker(task="goal", status=FAILED, error="worker hit the 1800s timeout cap")
    run = make_run()
    coordinator, dispatch = make_coordinator()

    coordinator.evaluate(run, worker)  # consumes the one allowed retry
    second = coordinator.evaluate(run, worker)

    assert second is not None
    assert second["class"] == TIMEOUT
    assert second["missing_deliverable"] == "retry bound exhausted"
    assert len(dispatch.calls) == 1  # no second dispatch


def test_narrow_goal_is_never_byte_identical_even_for_a_tiny_goal():
    assert narrow_goal("x") != "x"
    assert narrow_goal("") != ""


# ── PROVIDER_UNUSABLE: one retry, same goal, relies on chain cooldown ──────


def test_provider_unusable_retries_once_on_same_goal():
    worker = make_worker(
        task="goal",
        status=FAILED,
        hop_history=[{"reason": HOP_REASON_PROVIDER_UNUSABLE}],
    )
    run = make_run()
    coordinator, dispatch = make_coordinator()

    result = coordinator.evaluate(run, worker)

    assert result is None
    assert len(dispatch.calls) == 1
    assert dispatch.calls[0]["goal"] == "goal"
    assert worker.failure_class == PROVIDER_UNUSABLE


def test_provider_unusable_escalates_after_its_one_retry():
    worker = make_worker(task="goal", status=FAILED, hop_history=[{"reason": HOP_REASON_PROVIDER_UNUSABLE}])
    run = make_run()
    coordinator, dispatch = make_coordinator()

    coordinator.evaluate(run, worker)
    escalation = coordinator.evaluate(run, worker)

    assert escalation is not None
    assert escalation["class"] == PROVIDER_UNUSABLE
    assert len(dispatch.calls) == 1


# ── STALL: one resume retry, carrying the existing resume brief ───────────


def test_stall_retries_once_with_the_resume_note():
    worker = make_worker(task="goal", status=FAILED, error="watchdog stopped it: no activity for 400s")
    run = make_run()
    coordinator, dispatch = make_coordinator()

    result = coordinator.evaluate(run, worker)

    assert result is None
    assert len(dispatch.calls) == 1
    assert RESUME_NOTE in dispatch.calls[0]["goal"]
    assert worker.failure_class == STALL


def test_stall_escalates_after_its_one_resume_retry():
    worker = make_worker(task="goal", status=FAILED, error="watchdog stopped it: no activity for 400s")
    run = make_run()
    coordinator, dispatch = make_coordinator()

    coordinator.evaluate(run, worker)
    escalation = coordinator.evaluate(run, worker)

    assert escalation is not None
    assert escalation["class"] == STALL
    assert len(dispatch.calls) == 1


# ── VERIFIER_MISMATCH: reverify only, never re-run the task ───────────────


def test_verifier_mismatch_recovers_on_reverify_without_rerunning_the_task():
    worker = make_worker(task="goal", status=UNAUTHENTICATED)
    run = make_run()
    reverify_calls: list[Worker] = []

    def reverify(w: Worker) -> dict[str, Any]:
        reverify_calls.append(w)
        return {"verified": True}

    coordinator, dispatch = make_coordinator(reverify=reverify)

    result = coordinator.evaluate(run, worker)

    assert result is None
    assert len(reverify_calls) == 1
    assert dispatch.calls == []  # the work itself is never re-run
    assert worker.failure_class == VERIFIER_MISMATCH


def test_verifier_mismatch_escalates_with_what_it_could_not_read():
    worker = make_worker(task="goal", status=UNAUTHENTICATED)
    run = make_run()

    def reverify(_w: Worker) -> dict[str, Any]:
        return {"verified": False, "detail": "could not read PR #12"}

    coordinator, dispatch = make_coordinator(reverify=reverify)

    escalation = coordinator.evaluate(run, worker)

    assert escalation is not None
    assert escalation["class"] == VERIFIER_MISMATCH
    assert escalation["missing_deliverable"] == "could not read PR #12"
    assert dispatch.calls == []
    # Second evaluation must not loop: already escalated.
    assert coordinator.evaluate(run, worker) is None
    assert len(coordinator.escalations) == 1


# ── DEPENDENCY_BLOCKED: re-queue only once the parent succeeds ────────────


def test_dependency_blocked_defers_then_requeues_on_parent_success():
    worker = make_worker(task="goal", status=BLOCKED, depends_on=("parent-1",))
    run = make_run()
    coordinator, dispatch = make_coordinator()

    deferred = coordinator.evaluate(run, worker)
    assert deferred is None
    assert dispatch.calls == []  # not re-queued yet - parent has not succeeded

    requeued = coordinator.on_parent_succeeded("parent-1")

    assert len(requeued) == 1
    assert len(dispatch.calls) == 1
    assert dispatch.calls[0]["goal"] == "goal"
    # A second success notice for the same parent re-queues nothing further.
    assert coordinator.on_parent_succeeded("parent-1") == []
    assert len(dispatch.calls) == 1


def test_dependency_blocked_without_a_parent_escalates_immediately():
    worker = make_worker(task="goal", status=BLOCKED, depends_on=())
    run = make_run()
    coordinator, dispatch = make_coordinator()

    escalation = coordinator.evaluate(run, worker)

    assert escalation is not None
    assert escalation["class"] == DEPENDENCY_BLOCKED
    assert escalation["missing_deliverable"] == "no parent task recorded to wait on"
    assert dispatch.calls == []


def test_dependency_blocked_escalates_if_requeued_attempt_fails_again():
    worker = make_worker(task="goal", status=BLOCKED, depends_on=("parent-1",))
    run = make_run()
    coordinator, dispatch = make_coordinator()

    coordinator.evaluate(run, worker)
    coordinator.on_parent_succeeded("parent-1")  # consumes the one allowed requeue

    escalation = coordinator.evaluate(run, worker)  # blocked again after the requeue

    assert escalation is not None
    assert escalation["class"] == DEPENDENCY_BLOCKED
    assert escalation["missing_deliverable"] == "dependency never succeeded"
    assert len(dispatch.calls) == 1  # no further re-queue attempt


# ── PERMANENT: no retry, straight to escalation ────────────────────────────


def test_permanent_escalates_with_no_retry_at_all():
    worker = make_worker(task="goal", status=FAILED, error="TypeError: unexpected keyword argument")
    run = make_run()
    coordinator, dispatch = make_coordinator()

    escalation = coordinator.evaluate(run, worker)

    assert escalation is not None
    assert escalation["class"] == PERMANENT
    assert dispatch.calls == []


# ── liveness guard: RUNNING/PENDING is never retried or classified ─────────


def test_running_worker_is_never_retried_or_dispatched():
    worker = make_worker(task="goal", status=RUNNING)
    run = make_run()
    coordinator, dispatch = make_coordinator()

    assert coordinator.evaluate(run, worker) is None
    assert dispatch.calls == []
    assert worker.failure_class == ""  # never even classified


def test_pending_worker_is_never_retried_or_dispatched():
    worker = make_worker(task="goal", status=PENDING)
    run = make_run()
    coordinator, dispatch = make_coordinator()

    assert coordinator.evaluate(run, worker) is None
    assert dispatch.calls == []


def test_classify_failure_refuses_a_live_or_succeeded_worker():
    running = make_worker(task="goal", status=RUNNING)
    succeeded = make_worker(task="goal", status=SUCCEEDED)
    with pytest.raises(LiveWorkerError):
        classify_failure(running)
    with pytest.raises(LiveWorkerError):
        classify_failure(succeeded)


# ── escalation: emitted exactly once, never loops ──────────────────────────


def test_escalation_is_emitted_once_and_never_loops(capsys):
    worker = make_worker(task="goal", status=FAILED, error="boom, nothing recognizable")
    run = make_run()
    coordinator, dispatch = make_coordinator()

    results = [coordinator.evaluate(run, worker) for _ in range(5)]

    assert results[0] is not None
    assert all(r is None for r in results[1:])
    assert len(coordinator.escalations) == 1
    assert dispatch.calls == []
    printed = capsys.readouterr().out
    assert printed.count("[omo-retry] ESCALATION") == 1


# ── backoff: injected, never a real sleep ──────────────────────────────────


def test_backoff_is_injected_and_never_a_real_sleep():
    sleeps: list[float] = []
    backoffs: list[int] = []

    def fake_backoff(attempt: int) -> float:
        backoffs.append(attempt)
        return 7.5

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    worker = make_worker(task="goal", status=FAILED, error="worker hit the 1800s timeout cap")
    run = make_run()
    coordinator, _dispatch = make_coordinator(sleep=fake_sleep, backoff=fake_backoff)

    coordinator.evaluate(run, worker)

    assert backoffs == [0]
    assert sleeps == [7.5]  # the coordinator's own fake delay, never time.sleep
