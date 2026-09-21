from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from orchestrator.chains import status_from_message
from orchestrator.guards import GuardError, check_delegation
from roster import AGENTS, CATEGORIES, agent

PENDING = "PENDING"
RUNNING = "RUNNING"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"


@dataclass
class Worker:
    run_id: str
    agent_name: str
    task: str
    chain: tuple[str, ...]
    model: str | None = None
    status: str = PENDING
    handle: Any = None
    result: Any = None
    error: str = ""
    cancel_requested: bool = False
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def as_row(self) -> dict[str, Any]:
        spec = agent(self.agent_name)
        return {
            "agent": spec.display if spec else self.agent_name,
            "status": self.status,
            "task": self.task,
            "model": self.model,
            "run_id": self.run_id,
        }


@dataclass
class Run:
    run_id: str
    goal: str
    workers: list[Worker] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def tree(self) -> str:
        lines = ["Hermes", f"└── omo run {self.run_id}: {self.goal[:60]}"]
        for index, worker in enumerate(self.workers):
            branch = "└──" if index == len(self.workers) - 1 else "├──"
            row = worker.as_row()
            lines.append(f"    {branch} {row['agent']} · {row['status']} · {row['model'] or '-'}")
        return "\n".join(lines)


class OmoEngine:
    def __init__(self, ctx: Any, *, lifecycle: Any = None, request_factory: Any = None) -> None:
        self._ctx = ctx
        self._lifecycle = lifecycle
        self._request_factory = request_factory
        self.chains: Any = None
        self.runs: dict[str, Run] = {}

    def _service(self) -> Any:
        if self._lifecycle is not None:
            return self._lifecycle
        self._lifecycle = self._ctx.subagent_lifecycle
        return self._lifecycle

    def _resolve_target(
        self, *, target: str | None, category: str | None, parent_agent: str | None
    ) -> tuple[str, tuple[str, ...]]:
        resolved = check_delegation(target=target, category=category, parent_agent=parent_agent)
        if category is not None:
            if category not in CATEGORIES:
                raise GuardError(f"Unknown category '{category}'.")
            spec = AGENTS["sisyphus-junior"]
            return spec.name, tuple(self.chains.chain_for(spec.name, CATEGORIES[category]))
        spec = AGENTS[resolved]
        return spec.name, tuple(self.chains.chain_for(spec.name, spec.chain))

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
        # set is not exposed to plugins. Read-only agents are constrained by the
        # pre_tool_call guard instead, which does not need a toolset subset.
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
        if self._request_factory is not None:
            return self._request_factory(goal=goal, context=context, spec=spec, model=model, toolsets=toolsets)
        from agent.subagent_lifecycle import SubagentLaunchRequest  # noqa: PLC0415

        return SubagentLaunchRequest(
            goal=goal,
            context=context,
            role="orchestrator" if spec.orchestrator else "leaf",
            model=self._normalize_model(model),
            allowed_toolsets=toolsets or None,
            metadata={"omo_agent": spec.name, "omo_role": spec.role},
        )

    def _run_sync(self, run: Run, worker: Worker, request: Any) -> dict[str, Any]:
        service = self._service()
        state = self.chains.state_for(worker.agent_name, worker.chain)
        last_error = ""
        while True:
            worker.model = request.model
            worker.status = RUNNING
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
                if self._result_failed(result):
                    worker.status = FAILED
                    worker.error = self._result_error(result)
                else:
                    worker.status = SUCCEEDED
                worker.finished_at = time.time()
                return self._outcome(run, worker)
            except Exception as exc:
                last_error = str(exc)
                if not state.retryable(status=status_from_message(last_error), message=last_error):
                    break
                state.record_failure(request.model or "")
                next_model = state.next_model()
                if next_model is None:
                    break
                request = self._with_model(request, next_model)
        worker.status = FAILED
        worker.error = last_error
        worker.finished_at = time.time()
        return self._outcome(run, worker)

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
            "tree": run.tree(),
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
            return {"run_id": run_id, "tree": run.tree(), "workers": [w.as_row() for w in run.workers]}
        return {"runs": [{"run_id": r.run_id, "tree": r.tree()} for r in self.runs.values()]}

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
        return {"run_id": run_id, "cancelled": cancelled, "tree": run.tree()}

    def shutdown(self) -> None:
        for run in list(self.runs.values()):
            for worker in run.workers:
                if worker.handle is not None and worker.status in (PENDING, RUNNING):
                    try:
                        self._service().cancel(worker.handle, reason="plugin unloaded")
                    except Exception:
                        pass
