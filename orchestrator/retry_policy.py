"""Bounded automatic re-dispatch for a worker's terminal failure, by class.

Design follows the cross-orchestrator survey (Temporal/Prefect/Dagster/Argo/
Restate/Inngest/DBOS/K8s/GitHub Actions — see
``/opt/data/cache/scratch/reports/omo-retry-survey.md``): classify the
terminal outcome into a bounded failure class, look the class up in a small
policy table, and retry (or escalate) through the *existing* dispatch path.
This is deliberately not a workflow engine: no new scheduler, no new state
machine, just a classifier + a table + a counter.

Policy table
------------

| Class               | Action                          | Bound | Notes                                   |
|---------------------|----------------------------------|-------|------------------------------------------|
| TIMEOUT             | narrowed_retry                  | 1     | goal rewritten to a smaller slice        |
| PROVIDER_UNUSABLE   | next_model_retry                | 1     | same goal; chain cooldown already skips  |
|                     |                                  |       | the benched provider (max_fallback_      |
|                     |                                  |       | attempts is untouched)                   |
| STALL               | resume_retry                    | 1     | same resume brief the in-walk watchdog   |
|                     |                                  |       | already uses                             |
| VERIFIER_MISMATCH   | reverify_only                    | 1     | never re-runs the work, only the check   |
| DEPENDENCY_BLOCKED  | requeue_on_parent_success        | 1     | deferred until the parent succeeds       |
| PERMANENT           | escalate                        | 0     | no retry, straight to escalation         |

Every class is bounded to at most one automatic attempt. Liveness is a hard
guard ahead of everything else: a worker whose own record still reads RUNNING
or PENDING is never retried, regardless of what any status notice says.
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from orchestrator.chains import HOP_REASON_PROVIDER_UNUSABLE
from orchestrator.models import BLOCKED, FAILED, PENDING, RUNNING, SUCCEEDED, UNAUTHENTICATED

# The only statuses an evaluate() call actually acts on. CANCELLED and
# INTERRUPTED say something about the caller or the gateway, not about
# whether the work should be retried -- they fall through untouched, exactly
# like _record_outcome's own delivered-only check in engine.py.
_FAILURE_STATUSES = (FAILED, UNAUTHENTICATED, BLOCKED)

# ---------------------------------------------------------------- classes --

TIMEOUT = "TIMEOUT"
PROVIDER_UNUSABLE = "PROVIDER_UNUSABLE"
STALL = "STALL"
VERIFIER_MISMATCH = "VERIFIER_MISMATCH"
DEPENDENCY_BLOCKED = "DEPENDENCY_BLOCKED"
PERMANENT = "PERMANENT"

ALL_CLASSES = (TIMEOUT, PROVIDER_UNUSABLE, STALL, VERIFIER_MISMATCH, DEPENDENCY_BLOCKED, PERMANENT)

# A worker whose own record still says this is live: never a terminal outcome,
# never a retry candidate, whatever a notice elsewhere claims.
_LIVE_STATUSES = (RUNNING, PENDING)

# Matches engine._await_worker's own wording exactly (the hard cap).
_TIMEOUT_PATTERN = re.compile(r"hit the \d+s timeout")
# Matches engine._await_worker's own wording exactly (the idle watchdog).
_STALL_PATTERN = re.compile(r"watchdog stopped it: no activity for")


class LiveWorkerError(RuntimeError):
    """Raised by ``classify_failure`` if handed a worker that is still live.

    Callers must check liveness before classifying at all (see
    ``RetryCoordinator.evaluate``); this is a defensive backstop, not the
    primary guard.
    """


def classify_failure(worker: Any) -> str:
    """The bounded failure class for one terminal worker outcome.

    Order matters: TIMEOUT and STALL are read off the exact wording
    ``engine._await_worker`` raises before anything else, because both land on
    a plain FAILED status with no other distinguishing field. VERIFIER_MISMATCH
    and DEPENDENCY_BLOCKED are read off the worker's own status. Everything
    else falls to PROVIDER_UNUSABLE (via the last hop's recorded reason) or,
    failing that, PERMANENT.
    """
    status = getattr(worker, "status", "")
    if status in _LIVE_STATUSES:
        raise LiveWorkerError(f"worker is still {status}; refusing to classify a live worker")
    if status == SUCCEEDED:
        raise LiveWorkerError("worker succeeded; there is no failure to classify")

    error = str(getattr(worker, "error", "") or "")
    if _TIMEOUT_PATTERN.search(error):
        return TIMEOUT
    if _STALL_PATTERN.search(error):
        return STALL
    if status == UNAUTHENTICATED:
        return VERIFIER_MISMATCH
    if status == BLOCKED:
        return DEPENDENCY_BLOCKED
    hop_history = getattr(worker, "hop_history", None) or []
    if hop_history:
        last_reason = str((hop_history[-1] or {}).get("reason") or "")
        if last_reason == HOP_REASON_PROVIDER_UNUSABLE:
            return PROVIDER_UNUSABLE
    return PERMANENT


# --------------------------------------------------------------- narrowing --

# Resending the same oversized goal is what times out twice (survey finding):
# the retry's prompt must be a smaller, explicitly-framed slice, never the
# original goal verbatim.
NARROW_RETRY_PREFACE = (
    "NARROWED RETRY (previous attempt hit the worker timeout cap): the goal "
    "below was too large for one pass. Scope this attempt to ONLY the first "
    "concrete deliverable implied by the goal below; stop there and report "
    "rather than attempting the rest.\n\n"
)
_NARROW_MAX_CHARS = 1200


def narrow_goal(original_goal: str) -> str:
    """A strictly smaller slice of ``original_goal`` for a post-timeout retry.

    Bounds the retried goal to at most half its original length (and a hard
    cap), then wraps it with an explicit narrowing instruction. For any goal
    of two characters or more the result is provably shorter than the
    original payload, and it is never byte-identical to it even for a goal
    too short to shrink further.
    """
    text = (original_goal or "").strip()
    if not text:
        return NARROW_RETRY_PREFACE.strip()
    bound = min(_NARROW_MAX_CHARS, max(1, len(text) // 2))
    sliced = text[:bound].rstrip() or text[:1]
    return f"{NARROW_RETRY_PREFACE}{sliced}"


# The same resume brief the in-walk watchdog retry already uses
# (``OmoEngine._resume_request``) — kept as a literal copy rather than a cross-
# module import so this module has no dependency on ``engine``.
RESUME_NOTE = (
    "\n\nRESUME: your previous attempt was stopped for no activity. Check your worktree "
    "(git status, git rev-parse HEAD), keep work already done, do not restart. Your final "
    "summary must name the branch, the HEAD SHA and what changed."
)


# ------------------------------------------------------------------ backoff --

DEFAULT_BACKOFF_BASE_SECONDS = 1.0
DEFAULT_BACKOFF_FACTOR = 2.0
# Capped well under the watchdog's own stall window (360s) and nowhere near
# the hard timeout (1800s): backoff between retry attempts must never itself
# look like a stall.
DEFAULT_BACKOFF_CAP_SECONDS = 30.0


def exponential_backoff_with_jitter(
    attempt: int,
    *,
    base: float = DEFAULT_BACKOFF_BASE_SECONDS,
    factor: float = DEFAULT_BACKOFF_FACTOR,
    cap: float = DEFAULT_BACKOFF_CAP_SECONDS,
    rand: Callable[[], float] = random.random,
) -> float:
    """Full-jitter exponential backoff: a random delay in ``[0, ceiling)``.

    ``ceiling`` grows as ``base * factor**attempt``, capped at ``cap`` so no
    single wait can run long enough to look like a watchdog stall. ``rand`` is
    injectable (returns ``[0, 1)``) so a test can make this deterministic
    without ever actually sleeping.
    """
    ceiling = min(cap, base * (factor ** max(0, attempt)))
    return rand() * ceiling


# ------------------------------------------------------------------- policy --


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int
    action: str


POLICY_TABLE: dict[str, RetryPolicy] = {
    TIMEOUT: RetryPolicy(max_retries=1, action="narrowed_retry"),
    PROVIDER_UNUSABLE: RetryPolicy(max_retries=1, action="next_model_retry"),
    STALL: RetryPolicy(max_retries=1, action="resume_retry"),
    VERIFIER_MISMATCH: RetryPolicy(max_retries=1, action="reverify_only"),
    DEPENDENCY_BLOCKED: RetryPolicy(max_retries=1, action="requeue_on_parent_success"),
    PERMANENT: RetryPolicy(max_retries=0, action="escalate"),
}


def _stage_key(worker: Any) -> tuple[str, str]:
    """A stable identity for a worker's logical task, independent of its goal text.

    ``task_id`` (task-graph) wins when present. ``title`` is next: a caller-
    declared stable name that a TIMEOUT retry's narrowed goal does not touch
    (only ``task``/goal is rewritten, never ``title``). The raw task text is
    the last resort for an untitled, non-graph dispatch — exact for every
    class whose retry keeps the same task text, and only dulled for a second
    TIMEOUT on an untitled worker, where the key moves with the narrowed text.
    """
    task_id = str(getattr(worker, "task_id", "") or "").strip()
    title = str(getattr(worker, "title", "") or "").strip()
    task = str(getattr(worker, "task", "") or "").strip()
    return (str(getattr(worker, "agent_name", "")), task_id or title or task)


@dataclass
class RetryCoordinator:
    """Bounded automatic re-dispatch, wired around an existing dispatch call.

    ``dispatch`` is any ``dispatch(goal=..., target=..., background=...,
    title=..., origin=...) -> dict`` callable — production wires
    ``OmoEngine.dispatch``, tests wire a recording stub. ``reverify`` is
    ``(worker) -> dict`` for the VERIFIER_MISMATCH path only; it must never
    re-run the worker's own task.
    """

    dispatch: Callable[..., dict[str, Any]]
    reverify: Callable[[Any], dict[str, Any]] | None = None
    sleep: Callable[[float], None] = time.sleep
    backoff: Callable[[int], float] = exponential_backoff_with_jitter
    policy_table: dict[str, RetryPolicy] = field(default_factory=lambda: dict(POLICY_TABLE))

    _attempts: dict[tuple[str, str], int] = field(default_factory=dict, init=False)
    _escalated: set[tuple[str, str]] = field(default_factory=set, init=False)
    _pending_dependents: dict[str, list[tuple[Any, Any]]] = field(default_factory=dict, init=False)
    escalations: list[dict[str, Any]] = field(default_factory=list, init=False)

    # ---------------------------------------------------------------- entry
    def evaluate(self, run: Any, worker: Any) -> dict[str, Any] | None:
        """Act on one worker's terminal outcome; returns an escalation record or None.

        Liveness guard first, unconditionally: a RUNNING/PENDING worker is
        never retried or escalated, whatever a status notice elsewhere
        claims — this reads the worker's own record, nothing else. A key
        already escalated is inert forever: no re-escalation, no retry, no
        loop, however many more times this is called for it.
        """
        status = getattr(worker, "status", "")
        if status in _LIVE_STATUSES or status == SUCCEEDED:
            return None
        if status not in _FAILURE_STATUSES:
            # CANCELLED / INTERRUPTED: not a stage failure, nothing to retry.
            return None
        key = _stage_key(worker)
        if key in self._escalated:
            return None

        failure_class = classify_failure(worker)
        worker.failure_class = failure_class
        policy = self.policy_table.get(failure_class, self.policy_table[PERMANENT])

        if failure_class == VERIFIER_MISMATCH:
            return self._handle_verifier_mismatch(run, worker, key, policy)
        if failure_class == DEPENDENCY_BLOCKED:
            return self._handle_dependency_blocked(run, worker, key, policy)
        return self._handle_retryable(run, worker, key, failure_class, policy)

    # ------------------------------------------------------------- actions
    def _handle_retryable(
        self, run: Any, worker: Any, key: tuple[str, str], failure_class: str, policy: RetryPolicy
    ) -> dict[str, Any] | None:
        attempts = self._attempts.get(key, 0)
        if attempts >= policy.max_retries:
            return self._escalate(run, worker, failure_class, attempts, missing="retry bound exhausted")

        self.sleep(self.backoff(attempts))
        self._attempts[key] = attempts + 1

        goal = worker.task
        if policy.action == "narrowed_retry":
            goal = narrow_goal(worker.task)
        elif policy.action == "resume_retry":
            goal = f"{worker.task}{RESUME_NOTE}"
        # next_model_retry: same goal — the chain's own cooldown/bench (see
        # orchestrator.chains) already skips the model that just failed.

        self.dispatch(
            goal=goal,
            target=worker.agent_name,
            background=True,
            title=worker.title or None,
            origin="auto_retry",
        )
        return None

    def _handle_verifier_mismatch(
        self, run: Any, worker: Any, key: tuple[str, str], policy: RetryPolicy
    ) -> dict[str, Any] | None:
        """Re-run verification only — never the worker's own task."""
        attempts = self._attempts.get(key, 0)
        if attempts >= policy.max_retries:
            return self._escalate(run, worker, VERIFIER_MISMATCH, attempts, missing="retry bound exhausted")
        self._attempts[key] = attempts + 1
        verification = self.reverify(worker) if self.reverify is not None else {}
        if verification.get("verified"):
            return None
        missing = str(verification.get("detail") or "claimed artifacts could not be read")
        return self._escalate(run, worker, VERIFIER_MISMATCH, attempts + 1, missing=missing)

    def _handle_dependency_blocked(
        self, run: Any, worker: Any, key: tuple[str, str], policy: RetryPolicy
    ) -> dict[str, Any] | None:
        """Defer until the parent succeeds; never retried eagerly."""
        attempts = self._attempts.get(key, 0)
        if attempts >= policy.max_retries:
            return self._escalate(run, worker, DEPENDENCY_BLOCKED, attempts, missing="dependency never succeeded")
        parents: tuple[Any, ...] = tuple(getattr(worker, "depends_on", None) or ())
        if not parents:
            return self._escalate(
                run, worker, DEPENDENCY_BLOCKED, attempts, missing="no parent task recorded to wait on"
            )
        for parent_id in parents:
            self._pending_dependents.setdefault(parent_id, []).append((run, worker))
        return None

    def on_parent_succeeded(self, parent_task_id: str) -> list[tuple[str, str]]:
        """A parent task just succeeded: re-queue everything waiting on it (once).

        Pops the pending list, so a second call for the same parent id finds
        nothing left to requeue — the bound holds even if a caller fires this
        more than once for the same success.
        """
        pending = self._pending_dependents.pop(parent_task_id, [])
        requeued: list[tuple[str, str]] = []
        for _run, worker in pending:
            key = _stage_key(worker)
            attempts = self._attempts.get(key, 0)
            self.sleep(self.backoff(attempts))
            self._attempts[key] = attempts + 1
            self.dispatch(
                goal=worker.task,
                target=worker.agent_name,
                background=True,
                title=worker.title or None,
                origin="auto_retry",
            )
            requeued.append(key)
        return requeued

    # --------------------------------------------------------- escalation
    def _escalate(
        self, run: Any, worker: Any, failure_class: str, attempts: int, *, missing: str
    ) -> dict[str, Any]:
        key = _stage_key(worker)
        record = {
            "run_id": str(getattr(run, "run_id", "") or ""),
            "task_id": str(getattr(worker, "task_id", "") or ""),
            "agent": str(getattr(worker, "agent_name", "") or ""),
            "task": str(getattr(worker, "task", "") or ""),
            "class": failure_class,
            "attempts": attempts,
            "last_error": str(getattr(worker, "error", "") or ""),
            "missing_deliverable": missing,
        }
        self._escalated.add(key)
        self.escalations.append(record)
        # Printed, not just logged: the orchestrator persona reading the tool
        # surface must see this without having to go find a log file.
        print(
            f"[omo-retry] ESCALATION run={record['run_id']} "
            f"task={record['task_id'] or record['task'][:60]} class={failure_class} "
            f"attempts={attempts} missing={missing!r}"
        )
        return record

    def pending_escalations(self, run_id: str | None = None) -> list[dict[str, Any]]:
        """Every escalation this coordinator has ever recorded, newest last.

        Retrievable through the tool surface so the calling orchestrator
        persona can re-dispatch with a plan change instead of a blind retry.
        """
        if run_id is None:
            return list(self.escalations)
        return [record for record in self.escalations if record["run_id"] == run_id]


__all__ = [
    "ALL_CLASSES",
    "DEPENDENCY_BLOCKED",
    "DEFAULT_BACKOFF_BASE_SECONDS",
    "DEFAULT_BACKOFF_CAP_SECONDS",
    "DEFAULT_BACKOFF_FACTOR",
    "NARROW_RETRY_PREFACE",
    "PERMANENT",
    "POLICY_TABLE",
    "PROVIDER_UNUSABLE",
    "RESUME_NOTE",
    "RetryCoordinator",
    "RetryPolicy",
    "STALL",
    "TIMEOUT",
    "VERIFIER_MISMATCH",
    "classify_failure",
    "exponential_backoff_with_jitter",
    "narrow_goal",
]
