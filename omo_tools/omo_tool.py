from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

OMO_SCHEMA: dict[str, Any] = {
    "name": "omo",
    "description": (
        "Dispatch engineering work to the native OMO agent fleet. Hermes stays the "
        "sole user-facing agent; workers return structured results. A worker's own "
        "summary is a claim, not evidence: results carry a claim_boundary naming "
        "what the host observed and what the worker merely asserted, so verify "
        "before repeating it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["dispatch", "status", "cancel"],
                "description": "dispatch a task, inspect runs, or cancel a run.",
            },
            "goal": {"type": "string", "description": "The task to accomplish."},
            "agent": {
                "type": "string",
                "description": (
                    "Target agent (explore, librarian, oracle, hephaestus, prometheus, "
                    "atlas, metis, momus, sisyphus, multimodal-looker)."
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
            "run_id": {"type": "string", "description": "Run to inspect or cancel."},
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
            goal = str(params.get("goal") or "").strip()
            if not goal:
                return render({"error": "goal is required to dispatch."})
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
            return render(engine.status(params.get("run_id")))
        if action == "cancel":
            run_id = str(params.get("run_id") or "").strip()
            if not run_id:
                return render({"error": "run_id is required to cancel."})
            return render(engine.cancel(run_id))
        return render({"error": f"unknown action '{action}'"})

    return handler


__all__ = ["OMO_SCHEMA", "make_omo_handler", "render"]
