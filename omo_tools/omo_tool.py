from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from orchestrator.guards import GuardError

OMO_SCHEMA: dict[str, Any] = {
    "name": "omo",
    "description": (
        "Dispatch engineering work to the native OMO agent fleet. Hermes stays the "
        "sole user-facing agent; workers return structured results. A worker's own "
        "summary is a claim, not evidence: results carry a claim_boundary naming "
        "what the host observed and what the worker merely asserted, so verify "
        "before repeating it. Work with dependencies goes in one call: pass tasks= "
        "to dispatch a declared graph — independent tasks run in parallel, "
        "dependents wait for theirs, and dependents of a failed task come back "
        "BLOCKED rather than being run on a broken input. Every run is attributed to "
        "the session that dispatched it, so status/tree answer for this session's "
        "runs and cancel refuses another session's run unless all_sessions=true."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["dispatch", "status", "tree", "cancel"],
                "description": "Dispatch a task or graph, inspect runs, render a run tree, or cancel a run.",
            },
            "goal": {"type": "string", "description": "The task to accomplish (single-task dispatch)."},
            "tasks": {
                "type": "array",
                "description": (
                    "A declared task graph: [{id, agent|category, prompt, depends_on: [ids]}]. "
                    "Use instead of goal when the work has dependencies."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Task id, referenced by depends_on."},
                        "agent": {"type": "string", "description": "Target agent or role name."},
                        "category": {"type": "string", "description": "Category worker instead of agent."},
                        "prompt": {"type": "string", "description": "The task, with success criteria."},
                        "depends_on": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Ids that must succeed first.",
                        },
                    },
                    "required": ["prompt"],
                },
            },
            "agent": {
                "type": "string",
                "description": (
                    "Target agent (explore, librarian, oracle, hephaestus, prometheus, "
                    "atlas, metis, momus, sisyphus, multimodal-looker, tester, debugger, "
                    "security) or a role name (explorer, researcher, planner, implementer, "
                    "tester, debugger, reviewer, security, documenter, general)."
                ),
            },
            "category": {
                "type": "string",
                "description": (
                    "Category worker (quick, deep, ultrabrain, visual-engineering, writing). Use INSTEAD of agent."
                ),
            },
            "context": {"type": "string", "description": "Project conventions, memory, constraints."},
            "background": {"type": "boolean", "description": "Return immediately; result delivered later."},
            "review": {
                "type": "boolean",
                "description": (
                    "Run a reviewer (momus) over each successful task and re-run the task with the "
                    "reviewer's notes, bounded by max_review_cycles."
                ),
            },
            "max_parallel": {"type": "integer", "description": "Maximum tasks in flight for a graph dispatch."},
            "max_review_cycles": {"type": "integer", "description": "Review/retry bound; 0 disables review."},
            "run_id": {"type": "string", "description": "Run to inspect, render or cancel."},
            "all_sessions": {
                "type": "boolean",
                "description": (
                    "status / tree / cancel only; default false. The runs this session dispatched "
                    "are the only ones those actions see, and cancel refuses a run that belongs to "
                    "another session (the error names the owning session). Set true as the escape "
                    "hatch for cross-session inspection — an operator console or a deliberate "
                    "takeover — to list, render or cancel every session's runs. Records written "
                    "before session attribution (no session id) are hidden by default too, and "
                    "appear only under this flag."
                ),
            },
        },
        "required": ["action"],
    },
}


def render(payload: Any) -> str:
    return json.dumps(payload, indent=2, default=str)


def make_omo_handler(engine: Any) -> Callable[..., Any]:
    async def handler(args: dict[str, Any] | None = None, **_kwargs: Any) -> str:
        params = args or {}
        action = str(params.get("action") or "").strip().lower()
        if action == "dispatch":
            tasks = params.get("tasks")
            if tasks:
                try:
                    return render(
                        engine.dispatch_graph(
                            tasks=tasks,
                            goal=str(params.get("goal") or "").strip(),
                            review=bool(params.get("review", False)),
                            max_parallel=params.get("max_parallel"),
                            max_review_cycles=params.get("max_review_cycles"),
                        )
                    )
                except GuardError as exc:
                    # A rejected graph is a caller error, not a crash: render it so
                    # the model can fix the declaration and retry.
                    return render({"error": str(exc), "rejected": True})
            goal = str(params.get("goal") or "").strip()
            if not goal:
                return render({"error": "goal or tasks is required to dispatch."})
            if (
                params.get("review")
                or params.get("max_review_cycles") is not None
                or params.get("max_parallel") is not None
            ):
                # A single task can be reviewed too. Route it through the graph
                # machinery instead of accepting the controls and ignoring them.
                try:
                    return render(
                        engine.dispatch_graph(
                            tasks=[
                                {
                                    "id": "task-1",
                                    "agent": params.get("agent"),
                                    "category": params.get("category"),
                                    "prompt": goal,
                                }
                            ],
                            goal=goal,
                            review=bool(params.get("review", False)),
                            max_parallel=params.get("max_parallel"),
                            max_review_cycles=params.get("max_review_cycles"),
                        )
                    )
                except GuardError as exc:
                    return render({"error": str(exc), "rejected": True})
            return render(
                engine.dispatch(
                    goal=goal,
                    target=params.get("agent"),
                    category=params.get("category"),
                    context=params.get("context"),
                    background=bool(params.get("background", False)),
                )
            )
        if action == "status":
            return render(engine.status(params.get("run_id"), all_sessions=bool(params.get("all_sessions", False))))
        if action == "tree":
            run_id = str(params.get("run_id") or "").strip()
            if not run_id:
                return render({"error": "run_id is required for tree."})
            payload = engine.status(run_id, all_sessions=bool(params.get("all_sessions", False)))
            if "error" in payload:
                # Includes the ownership refusal: a foreign run is not rendered.
                return render(payload)
            return render(
                {
                    "run_id": run_id,
                    "session_id": payload.get("session_id"),
                    "tree": payload.get("tree"),
                    "workers": payload.get("workers"),
                }
            )
        if action == "cancel":
            run_id = str(params.get("run_id") or "").strip()
            if not run_id:
                return render({"error": "run_id is required to cancel."})
            return render(engine.cancel(run_id, all_sessions=bool(params.get("all_sessions", False))))
        return render({"error": f"unknown action '{action}'"})

    return handler


__all__ = ["OMO_SCHEMA", "make_omo_handler", "render"]
