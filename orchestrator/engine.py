from __future__ import annotations

import asyncio
import threading
import time
import uuid
from typing import Any

from orchestrator.boundary import claim_boundary
from orchestrator.chains import client_disconnected, status_from_message
from orchestrator.guards import GuardError, check_delegation
from orchestrator.models import (
    CANCELLED,
    FAILED,
    PENDING,
    RUNNING,
    SUCCEEDED,
    Run,
    Worker,
)
from orchestrator.personas import MAX_CONTEXT_CHARS, compose_context
from roster import AGENTS, CATEGORIES, ROLE_ALIASES

# The last element of a chain is the premium, quota-bounded route, so the hop onto
# it hands over a brief plus artifacts instead of re-sending the whole launch
# payload verbatim. The budget is a setting (see README) so it can move without a
# release; `0` restores the old behaviour of hand-off-as-is.
DEFAULT_ESCALATION_BUDGET_CHARS = 6000
_ESCALATION_BRIEF_SHARE = 3
_ESCALATION_MIN_BRIEF_CHARS = 500
_HANDOFF_TRUNCATED = "\n[truncated: last-hop handoff — the full text ran on the earlier chain hops]"

# A stage that keeps failing must stop instead of walking the chain again on every
# re-dispatch: the host's `max_turns` is a global bound on one agent's loop, not a
# bound on how many times this engine will retry the same work.
DEFAULT_MAX_ATTEMPTS_PER_AGENT = 3

_TASK_CONTEXT_OPEN = "<task_context>"
_TASK_CONTEXT_CLOSE = "\n</task_context>"


def _clip(text: str, limit: int) -> str:
    """Bound `text` to `limit` characters, naming the cut when one happens."""
    text = (text or "").strip()
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= len(_HANDOFF_TRUNCATED):
        return text[:limit]
    return text[: limit - len(_HANDOFF_TRUNCATED)] + _HANDOFF_TRUNCATED


def _brief_allowance(budget: int) -> int:
    """How much of a handoff budget the brief may take; the rest is artifacts."""
    return max(min(_ESCALATION_MIN_BRIEF_CHARS, budget), budget // _ESCALATION_BRIEF_SHARE)


class OmoEngine:
    def __init__(self, ctx: Any, *, lifecycle: Any = None, request_factory: Any = None) -> None:
        self._ctx = ctx
        self._lifecycle = lifecycle
        self._request_factory = request_factory
        self.chains: Any = None
        self.runs: dict[str, Run] = {}
        # Failures per (agent, stage), counted engine-wide so the bound survives the
        # session-scoped FallbackState that every dispatch rebuilds from scratch.
        self._stage_failures: dict[tuple[str, str], int] = {}
        self._stage_lock = threading.Lock()

    def _service(self) -> Any:
        if self._lifecycle is not None:
            return self._lifecycle
        self._lifecycle = self._ctx.subagent_lifecycle
        return self._lifecycle

    def _resolve_target(
        self, *, target: str | None, category: str | None, parent_agent: str | None
    ) -> tuple[str, tuple[str, ...]]:
        target, category = self._apply_role_alias(target, category)
        resolved = check_delegation(target=target, category=category, parent_agent=parent_agent)
        if category is not None:
            if category not in CATEGORIES:
                raise GuardError(f"Unknown category '{category}'.")
            spec = AGENTS["sisyphus-junior"]
            self._check_enabled(spec.name)
            return spec.name, tuple(self.chains.category_chain(category, CATEGORIES[category]))
        spec = AGENTS[resolved]
        self._check_enabled(spec.name)
        return spec.name, tuple(self.chains.chain_for(spec.name, spec.chain))

    def _apply_role_alias(self, target: str | None, category: str | None) -> tuple[str | None, str | None]:
        """Resolve caller-facing role names (implementer, documenter, …) to roster entries.

        The roster is OMO's codenames; callers think in roles. Aliases are
        overridable through the ``roles`` setting, and an alias may point at a
        category, in which case it is routed as one.
        """
        if not target:
            return target, category
        aliases = dict(ROLE_ALIASES)
        configured = self._config("roles", None)
        if isinstance(configured, dict):
            aliases.update({str(k).strip().lower(): str(v).strip() for k, v in configured.items()})
        resolved = aliases.get(target.strip().lower(), target)
        if resolved in CATEGORIES and category is None:
            return None, resolved
        return resolved, category

    def _enabled_agents(self) -> set[str] | None:
        raw = self._config("enabled_agents", None)
        if not raw:
            return None
        if isinstance(raw, str):
            raw = raw.split(",")
        names = {str(name).strip() for name in raw if str(name).strip()}
        return names or None

    def _check_enabled(self, name: str) -> None:
        enabled = self._enabled_agents()
        if enabled is not None and name not in enabled:
            raise GuardError(f"Agent '{name}' is not enabled. enabled_agents = {', '.join(sorted(enabled))}.")

    def dispatch_graph(
        self,
        *,
        tasks: list[dict[str, Any]],
        goal: str = "",
        review: bool = False,
        max_parallel: int | None = None,
        max_review_cycles: int | None = None,
    ) -> dict[str, Any]:
        """Run a declared dependency graph: parallel where independent, ordered where not."""
        from orchestrator.graph import TaskGraph

        return TaskGraph(
            self,
            tasks,
            goal=goal,
            review=review,
            max_parallel=max_parallel,
            max_review_cycles=max_review_cycles,
        ).run()

    def dispatch(
        self,
        *,
        goal: str,
        target: str | None = None,
        category: str | None = None,
        context: str | None = None,
        background: bool = False,
        parent_agent: str | None = None,
    ) -> dict[str, Any]:
        name, chain = self._resolve_target(target=target, category=category, parent_agent=parent_agent)
        chain = tuple(self._normalize_model(model) or model for model in chain)
        run_id = f"omo_{uuid.uuid4().hex[:8]}"
        run = Run(run_id=run_id, goal=goal)
        worker = Worker(run_id=run_id, agent_name=name, task=goal, chain=chain, model=chain[0] if chain else None)
        run.workers.append(worker)
        self.runs[run_id] = run

        spec = AGENTS[name]
        # Children inherit the parent's toolsets: Hermes validates that a launch's
        # allowed_toolsets is a subset of the parent's set and refuses it otherwise
        # ("Requested toolsets would broaden parent permissions"), and the parent's
        # set is not exposed to plugins.
        request = self._request(goal=goal, context=context, spec=spec, model=worker.model, toolsets=None)

        if background:
            worker.status = RUNNING
            self._spawn(self._run_worker(run, worker, request))
            return {
                "run_id": run_id,
                "status": "running",
                "agent": spec.display,
                "model": worker.model,
                "tree": run.tree(),
                "claim_boundary": claim_boundary(),
            }

        return self._run_sync(run, worker, request)

    def _config(self, key: str, default: Any = None) -> Any:
        try:
            return self._ctx.get_config(key, default)
        except Exception:
            return default

    def _request(
        self, *, goal: str, context: str | None, spec: Any, model: str | None, toolsets: tuple[str, ...] | None
    ) -> Any:
        composed = compose_context(spec.name, context)
        if self._request_factory is not None:
            return self._request_factory(goal=goal, context=composed, spec=spec, model=model, toolsets=toolsets)
        from agent.subagent_lifecycle import SubagentLaunchRequest  # noqa: PLC0415

        return SubagentLaunchRequest(
            goal=goal,
            context=composed,
            role="orchestrator" if spec.orchestrator else "leaf",
            model=self._normalize_model(model),
            allowed_toolsets=toolsets or None,
            metadata={"omo_agent": spec.name, "omo_role": spec.role},
        )

    def _run_sync(self, run: Run, worker: Worker, request: Any) -> dict[str, Any]:
        service = self._service()
        state = self.chains.state_for(worker.agent_name, worker.chain)
        last_error = ""
        if self._attempts_exhausted(worker):
            # This stage has already been paid for `max_attempts_per_agent` times and
            # failed every time: another walk would start at the primary and, on a
            # provider-wide failure, end at the premium hop again.
            worker.status = FAILED
            worker.error = self._attempts_exhausted_error(worker)
            worker.finished_at = time.time()
            return self._outcome(run, worker)
        while True:
            worker.model = request.model
            worker.status = RUNNING
            error = ""
            try:
                handle = service.launch(request)
                worker.handle = handle
                if worker.cancel_requested:
                    try:
                        service.cancel(handle, reason="cancelled before start")
                    except Exception:
                        pass
                    worker.status = CANCELLED
                    worker.finished_at = time.time()
                    return self._outcome(run, worker)
                service.wait(handle, timeout_seconds=self._timeout_seconds())
                result = service.result(handle)
                worker.result = result
                if not self._result_failed(result):
                    worker.status = SUCCEEDED
                    worker.finished_at = time.time()
                    return self._outcome(run, worker)
                error = self._result_error(result)
            except Exception as exc:
                error = str(exc)

            # A child that fails inside itself returns a result rather than raising,
            # so a provider error (402 credits, 429, 5xx) must walk the chain here
            # too — otherwise the fallback layer is inert for exactly the failures
            # it exists to handle.
            last_error = error
            if worker.cancel_requested:
                # The caller is gone: the answer has nowhere to land, so every further
                # hop spends tokens nobody can receive.
                break
            if client_disconnected(error):
                break
            if not state.retryable(status=status_from_message(error), message=error):
                break
            state.record_failure(request.model or "")
            next_model = state.next_model()
            if next_model is None:
                break
            request = self._advance_hop(request, state, next_model)

        if worker.cancel_requested or client_disconnected(last_error):
            # Cancelled work is not a stage failure: it says nothing about whether the
            # agent can do the work, so it must not consume the stage's attempt budget.
            worker.status = CANCELLED
        else:
            worker.status = FAILED
            self._record_stage_failure(worker)
        worker.error = last_error
        worker.finished_at = time.time()
        return self._outcome(run, worker)

    def _advance_hop(self, request: Any, state: Any, next_model: str) -> Any:
        """Build the request for the next chain element.

        Every hop but the last re-issues the same payload with the model swapped:
        the payload *is* the task, and a cheaper model deserves the same brief. The
        last element of the chain is the premium, quota-bounded route, so the hop
        onto it carries a bounded handoff instead (see `_handoff`).
        """
        if self._is_last_hop(state):
            return self._handoff(request, next_model)
        return self._with_model(request, next_model)

    @staticmethod
    def _is_last_hop(state: Any) -> bool:
        chain = tuple(getattr(state, "chain", ()) or ())
        return bool(chain) and int(getattr(state, "current_index", 0)) >= len(chain) - 1

    def _escalation_budget(self) -> int:
        try:
            return int(self._config("escalation_budget_chars", DEFAULT_ESCALATION_BUDGET_CHARS))
        except (TypeError, ValueError):
            return DEFAULT_ESCALATION_BUDGET_CHARS

    def _handoff(self, request: Any, next_model: str) -> Any:
        """The bounded payload a premium hop receives: a brief plus explicit artifacts.

        The budget bounds the *task payload* — the goal and the caller's task
        context. The worker contract and persona are constant per agent rather than
        accumulated payload, so they travel whole, still inside the host's own
        32,000-char launch-context cap.
        """
        import dataclasses  # noqa: PLC0415

        budget = self._escalation_budget()
        if budget <= 0:
            # Opt-out: hand the request over as it stands (the pre-bound behaviour).
            return self._with_model(request, next_model)
        brief = _clip(getattr(request, "goal", "") or "", _brief_allowance(budget))
        artifacts_budget = max(0, budget - len(brief))
        context = self._handoff_context(getattr(request, "context", None), artifacts_budget)
        metadata = dict(getattr(request, "metadata", None) or {})
        metadata["omo_handoff"] = "bounded_last_hop"
        metadata["omo_handoff_budget_chars"] = budget
        return dataclasses.replace(
            request,
            goal=brief,
            context=context,
            model=self._normalize_model(next_model),
            metadata=metadata,
        )

    @staticmethod
    def _handoff_context(composed: str | None, artifacts_budget: int) -> str | None:
        """Re-frame the launch context around a bounded copy of the task artifacts.

        `compose_context` frames the caller's task context in a `<task_context>`
        block; splitting there keeps the constant contract/persona head and bounds
        only the per-task payload. A context with no such frame carries no caller
        payload at all, so it is handed over untouched.
        """
        if not composed:
            return None
        head, marker, tail = composed.partition(_TASK_CONTEXT_OPEN)
        if not marker:
            return composed
        artifacts = tail[: -len(_TASK_CONTEXT_CLOSE)] if tail.endswith(_TASK_CONTEXT_CLOSE) else tail
        # The host rejects a launch whose context exceeds its own cap, so the constant
        # head decides how much room the artifacts have left.
        room = MAX_CONTEXT_CHARS - len(head) - len(_TASK_CONTEXT_OPEN) - len(_TASK_CONTEXT_CLOSE)
        bounded = _clip(artifacts, min(artifacts_budget, room))
        if not bounded:
            return head
        return f"{head}{_TASK_CONTEXT_OPEN}\n{bounded}{_TASK_CONTEXT_CLOSE}"

    def _max_attempts_per_agent(self) -> int:
        try:
            return int(self._config("max_attempts_per_agent", DEFAULT_MAX_ATTEMPTS_PER_AGENT))
        except (TypeError, ValueError):
            return DEFAULT_MAX_ATTEMPTS_PER_AGENT

    @staticmethod
    def _stage_key(worker: Worker) -> tuple[str, str]:
        return worker.agent_name, (worker.task or "").strip()

    def _attempts_exhausted(self, worker: Worker) -> bool:
        budget = self._max_attempts_per_agent()
        if budget <= 0:
            return False
        with self._stage_lock:
            return self._stage_failures.get(self._stage_key(worker), 0) >= budget

    def _attempts_exhausted_error(self, worker: Worker) -> str:
        with self._stage_lock:
            failures = self._stage_failures.get(self._stage_key(worker), 0)
        return (
            f"agent '{worker.agent_name}' has already failed this stage {failures} times "
            f"(max_attempts_per_agent={self._max_attempts_per_agent()}); refusing another attempt"
        )

    def _record_stage_failure(self, worker: Worker) -> None:
        key = self._stage_key(worker)
        with self._stage_lock:
            self._stage_failures[key] = self._stage_failures.get(key, 0) + 1

    def _normalize_model(self, model: str | None) -> str | None:
        # OMO/OpenCode chains are written `provider/model`; Hermes wants the bare
        # model and takes the provider from `delegation.provider`. Only the
        # `litellm/` prefix is stripped — aliases like `claude/sonnet-5` are real
        # model names and must survive intact.
        if model and model.startswith("litellm/"):
            return model[len("litellm/") :]
        return model

    def _with_model(self, request: Any, model: str) -> Any:
        import dataclasses  # noqa: PLC0415

        return dataclasses.replace(request, model=self._normalize_model(model))

    def _result_failed(self, result: Any) -> bool:
        # A returned result is not automatically a success: the child's terminal
        # state can be FAILED (e.g. the provider rejected the model) while the
        # handle-side status still reads succeeded. Trust the child's own state.
        state = getattr(result, "terminal_state", None)
        if state is None:
            return False
        return "FAILED" in str(getattr(state, "name", state)).upper()

    def _result_error(self, result: Any) -> str:
        return str(getattr(result, "error_message", "") or getattr(result, "summary", "") or "subagent failed")

    def _timeout_seconds(self) -> float:
        return float(self._config("worker_timeout_seconds", 1800))

    def _outcome(self, run: Run, worker: Worker) -> dict[str, Any]:
        return {
            "run_id": run.run_id,
            "agent": AGENTS[worker.agent_name].display,
            "model": worker.model,
            "status": worker.status.lower(),
            "result": worker.result,
            "error": worker.error,
            # Named separately from a caller-requested cancel: this one says the client
            # that wanted the answer disconnected, so no answer can be delivered.
            "client_gone": client_disconnected(worker.error),
            "tree": run.tree(),
            "claim_boundary": claim_boundary(),
        }

    async def _run_worker(self, run: Run, worker: Worker, request: Any) -> None:
        if worker.cancel_requested:
            worker.status = CANCELLED
            return
        await asyncio.to_thread(self._run_sync, run, worker, request)
        self._ctx.emit(f"{self._key()}:worker_done", {"run_id": run.run_id, **worker.as_row()})

    def _key(self) -> str:
        return "omo"

    def _spawn(self, coro: Any) -> None:
        spawn = getattr(self._ctx, "spawn_task", None)
        if spawn is not None:
            spawn(coro)
        else:
            asyncio.ensure_future(coro)

    def status(self, run_id: str | None = None) -> dict[str, Any]:
        if run_id:
            run = self.runs.get(run_id)
            if run is None:
                return {"error": f"unknown run {run_id}"}
            return {
                "run_id": run_id,
                "tree": run.tree(),
                "workers": [w.as_row() for w in run.workers],
                "claim_boundary": claim_boundary(),
            }
        return {
            "runs": [{"run_id": r.run_id, "tree": r.tree()} for r in self.runs.values()],
            "claim_boundary": claim_boundary(),
        }

    def cancel(self, run_id: str, reason: str = "cancelled by Hermes") -> dict[str, Any]:
        run = self.runs.get(run_id)
        if run is None:
            return {"error": f"unknown run {run_id}"}
        service = self._service()
        cancelled = 0
        for worker in run.workers:
            if worker.status not in (PENDING, RUNNING):
                continue
            worker.cancel_requested = True
            if worker.handle is not None:
                try:
                    service.cancel(worker.handle, reason=reason)
                except Exception:
                    pass
            worker.status = CANCELLED
            cancelled += 1
        return {
            "run_id": run_id,
            "cancelled": cancelled,
            "tree": run.tree(),
            "claim_boundary": claim_boundary(),
        }

    def shutdown(self) -> None:
        for run in list(self.runs.values()):
            for worker in run.workers:
                if worker.handle is not None and worker.status in (PENDING, RUNNING):
                    try:
                        self._service().cancel(worker.handle, reason="plugin unloaded")
                    except Exception:
                        pass
