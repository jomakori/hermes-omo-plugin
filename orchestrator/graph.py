"""Declared task graphs: dependencies, parallel scheduling, review cycles.

This layer sits on top of the engine's single-worker path. Hermes still owns the
conversation, the synthesis and the decision to delegate at all — the graph only
decides what may run when, and what happens when a task comes back with problems.
"""

from __future__ import annotations

import contextvars
import dataclasses
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from uuid import uuid4

from orchestrator import session
from orchestrator.boundary import claim_boundary
from orchestrator.guards import GuardError
from orchestrator.models import (
    BLOCKED,
    RETRYING,
    SUCCEEDED,
    Run,
    Worker,
)
from roster import AGENTS

REVIEW_AGENT = "momus"

# The reviewer must answer in a shape the scheduler can read; a verdict buried in
# prose would make the retry decision a guess.
REVIEW_INSTRUCTION = (
    "Review the work above as an internal critic. Reply with JSON only, no prose: "
    '{"verdict": "pass"|"problems", "problems": ["<what is wrong>", ...]}'
)


class TaskGraph:
    """Validate, schedule and collect a declared dependency graph."""

    def __init__(
        self,
        engine: Any,
        tasks: list[dict[str, Any]],
        *,
        goal: str = "",
        review: bool = False,
        max_parallel: int | None = None,
        max_review_cycles: int | None = None,
    ) -> None:
        self.engine = engine
        self.tasks = self._normalize(tasks)
        self.goal = goal or (self.tasks[0]["prompt"][:60] if self.tasks else "task graph")
        self.review = review
        self.max_parallel = max(1, int(max_parallel or engine._config("max_parallel", 4)))
        cycles = engine._config("max_review_cycles", 1) if max_review_cycles is None else max_review_cycles
        self.max_review_cycles = max(0, int(cycles))
        self._validate()

    # ── validation ────────────────────────────────────────────────────
    @staticmethod
    def _normalize(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not isinstance(tasks, list) or not tasks:
            raise GuardError("tasks must be a non-empty list of task objects.")
        normalized = []
        for index, raw in enumerate(tasks):
            if not isinstance(raw, dict):
                raise GuardError(f"task {index} is not an object.")
            prompt = str(raw.get("prompt") or "").strip()
            if not prompt:
                raise GuardError(f"task {raw.get('id') or index} has no prompt.")
            agent_name = raw.get("agent")
            category = raw.get("category")
            if bool(agent_name) == bool(category):
                raise GuardError(f"task {raw.get('id') or index} needs exactly one of agent or category.")
            deps = raw.get("depends_on") or []
            if not isinstance(deps, list):
                raise GuardError(f"task {raw.get('id') or index} has non-list depends_on.")
            normalized.append(
                {
                    "id": str(raw.get("id") or f"task-{index + 1}"),
                    "agent": agent_name,
                    "category": category,
                    "prompt": prompt,
                    "depends_on": [str(d) for d in deps],
                    "parent_id": str(raw.get("parent_id") or ""),
                }
            )
        return normalized

    def _validate(self) -> None:
        ids = [t["id"] for t in self.tasks]
        if len(set(ids)) != len(ids):
            raise GuardError("task ids must be unique.")
        known = set(ids)
        for task in self.tasks:
            for dep in task["depends_on"]:
                if dep not in known:
                    raise GuardError(f"task {task['id']} depends on unknown task {dep}.")
                if dep == task["id"]:
                    raise GuardError(f"task {task['id']} depends on itself.")
        # Kahn: a cycle would otherwise schedule nothing and look like a hang.
        remaining = {t["id"]: set(t["depends_on"]) for t in self.tasks}
        while remaining:
            ready = [tid for tid, deps in remaining.items() if not (deps & set(remaining))]
            if not ready:
                raise GuardError("task graph has a cycle: " + ", ".join(sorted(remaining)))
            for tid in ready:
                remaining.pop(tid)

    # ── scheduling ────────────────────────────────────────────────────
    def run(self) -> dict[str, Any]:
        engine = self.engine
        # The graph is dispatched from the caller's turn, so the session id is
        # resolved here rather than inside a worker thread (ContextVars would not
        # carry it there, and the whole graph belongs to the one session anyway).
        run = Run(run_id=f"omo_{uuid4().hex[:8]}", goal=self.goal, session_id=session.current_session_id())
        workers: dict[str, Worker] = {}
        for task in self.tasks:
            name, chain = engine._resolve_target(target=task["agent"], category=task["category"], parent_agent=None)
            chain = tuple(engine._normalize_model(model) or model for model in chain)
            worker = Worker(
                run_id=run.run_id,
                agent_name=name,
                task=task["prompt"],
                chain=chain,
                model=chain[0] if chain else None,
                task_id=task["id"],
                depends_on=tuple(task["depends_on"]),
                parent_id=task["parent_id"],
            )
            workers[task["id"]] = worker
            run.workers.append(worker)
        engine.runs[run.run_id] = run
        engine._persist()

        pending = set(workers)
        settled: dict[str, bool] = {}
        with ThreadPoolExecutor(max_workers=self.max_parallel) as pool:
            in_flight: dict[Any, str] = {}
            while pending:
                for task_id in [t for t in list(pending) if all(d in settled for d in workers[t].depends_on)]:
                    worker = workers[task_id]
                    pending.discard(task_id)
                    if any(settled[dep] is False for dep in worker.depends_on):
                        worker.status = BLOCKED
                        worker.error = "dependency failed"
                        worker.finished_at = time.time()
                        settled[task_id] = False
                        engine._persist()
                        continue
                    # Threads do not inherit ContextVars, and the host resolves the
                    # active parent session from one (agent/subagent_lifecycle), so a
                    # bare submit has every launch refused with "No active Hermes
                    # parent session is available." Run each task inside a copy of the
                    # caller's context.
                    in_flight[pool.submit(contextvars.copy_context().run, self._run_one, run, worker)] = task_id
                if not in_flight:
                    # Nothing runnable and nothing running: the remainder is blocked
                    # behind a failure (cycles are rejected in _validate).
                    for task_id in pending:
                        worker = workers[task_id]
                        worker.status = BLOCKED
                        worker.error = "dependency unresolved"
                        worker.finished_at = time.time()
                        settled[task_id] = False
                    engine._persist()
                    pending.clear()
                    break
                for future in as_completed(list(in_flight)):
                    task_id = in_flight.pop(future)
                    settled[task_id] = workers[task_id].status == SUCCEEDED
                    break  # re-evaluate readiness after each completion
        engine._persist()
        return self._payload(run)

    def _run_one(self, run: Run, worker: Worker) -> None:
        engine = self.engine
        spec = AGENTS[worker.agent_name]
        request = engine._request(goal=worker.task, context=None, spec=spec, model=worker.model, toolsets=None)
        engine._run_sync(run, worker, request)
        if self.review and self.max_review_cycles and worker.status == SUCCEEDED:
            self._review_loop(run, worker, request)

    # ── review / verification ─────────────────────────────────────────
    def _review_loop(self, run: Run, worker: Worker, request: Any) -> None:
        engine = self.engine
        for cycle in range(1, self.max_review_cycles + 1):
            verdict, problems = self._review_once(run, worker)
            worker.review_verdict = verdict
            if verdict != "problems" or not problems:
                return
            worker.review_cycles = cycle
            worker.status = RETRYING
            revised = dataclasses.replace(request, goal=f"{worker.task}\n\nREVIEW NOTES (cycle {cycle}):\n{problems}")
            engine._run_sync(run, worker, engine._with_model(revised, worker.model or ""))
            if worker.status != SUCCEEDED:
                return

    def _review_once(self, run: Run, worker: Worker) -> tuple[str, str]:
        engine = self.engine
        spec = AGENTS[REVIEW_AGENT]
        chain = tuple(engine._normalize_model(model) or model for model in spec.chain)
        reviewer = Worker(
            run_id=run.run_id,
            agent_name=REVIEW_AGENT,
            task=f"review {worker.task_id}",
            chain=chain,
            model=chain[0] if chain else None,
            task_id=f"{worker.task_id}:review",
            parent_id=worker.task_id,
        )
        run.workers.append(reviewer)
        # The review material travels as context so the run tree keeps the terse
        # label the caller can read.
        material = (
            f"TASK UNDER REVIEW:\n{worker.task}\n\n"
            f"WORKER'S OWN SUMMARY (a claim, not evidence):\n{self._result_text(worker)}\n\n"
            f"{REVIEW_INSTRUCTION}"
        )
        request = engine._request(goal=reviewer.task, context=material, spec=spec, model=reviewer.model, toolsets=None)
        engine._run_sync(run, reviewer, request)
        if reviewer.status != SUCCEEDED:
            return "reviewer_failed", ""
        return self._verdict(reviewer)

    @staticmethod
    def _result_text(worker: Worker) -> str:
        result = worker.result
        for attr in ("summary", "structured_payload"):
            value = getattr(result, attr, None)
            if value:
                return value if isinstance(value, str) else json.dumps(value, default=str)
        return str(result or worker.error or "")

    @classmethod
    def _verdict(cls, reviewer: Worker) -> tuple[str, str]:
        """Name the review outcome: pass, problems, or a verdict nobody could read.

        An unreadable verdict is not a pass by intent — it is recorded as
        `unparsed` so the caller can tell "reviewed and clean" from "review ran and
        said something the scheduler could not use". Both leave the task alone;
        only `problems` re-runs it.
        """

        payload = getattr(reviewer.result, "structured_payload", None)
        if payload is None:
            payload = cls._result_text(reviewer)
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (ValueError, TypeError):
                return "unparsed", ""
        if not isinstance(payload, dict):
            return "unparsed", ""
        if str(payload.get("verdict", "")).lower() != "problems":
            return "pass", ""
        problems = payload.get("problems") or []
        text = problems if isinstance(problems, str) else "\n".join(str(p) for p in problems)
        return ("problems", text) if text else ("pass", "")

    # ── payload ───────────────────────────────────────────────────────
    def _payload(self, run: Run) -> dict[str, Any]:
        # Reviewer rows are reporting, not work: a review that failed to run leaves
        # the task's verdict as it was (see _review_once), so it must not drag the
        # run's overall status either. The row stays in the tree.
        statuses = [w.status for w in run.workers if not w.task_id.endswith(":review")]
        if all(s == SUCCEEDED for s in statuses):
            overall = "succeeded"
        elif any(s == SUCCEEDED for s in statuses):
            overall = "partial"
        else:
            overall = "failed"
        return {
            "run_id": run.run_id,
            "status": overall,
            "tree": run.tree(),
            "workers": [w.as_row() for w in run.workers],
            "results": [
                {
                    "task_id": w.task_id,
                    "status": w.status,
                    "error": w.error,
                    "review_verdict": w.review_verdict,
                    "result": w.result,
                }
                for w in run.workers
                if w.task_id and not w.task_id.endswith(":review")
            ],
            "claim_boundary": claim_boundary(),
        }
