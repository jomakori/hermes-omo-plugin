from __future__ import annotations

from collections.abc import Callable
from typing import Any

OMO_TASK_SCHEMA: dict[str, Any] = {
    "name": "omo_task",
    "description": (
        "Delegate a subtask to one OMO agent (agent=) or spawn a category worker "
        "(category=). Provide EXACTLY ONE of agent or category."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "agent": {
                "type": "string",
                "description": (
                    "Direct target: explore, librarian, oracle, metis, momus, "
                    "multimodal-looker, hephaestus, atlas, sisyphus."
                ),
            },
            "category": {
                "type": "string",
                "description": "Category spawn: quick, deep, ultrabrain, visual-engineering, writing.",
            },
            "prompt": {"type": "string", "description": "The subtask, with context and success criteria."},
            "context": {"type": "string", "description": "Optional additional context."},
            "background": {"type": "boolean", "description": "Run without blocking."},
        },
        "required": ["prompt"],
    },
}


def make_omo_task_handler(engine: Any) -> Callable[..., Any]:
    async def handler(args: dict[str, Any] | None = None, **_kwargs: Any) -> dict[str, Any]:
        params = args or {}
        prompt = str(params.get("prompt") or "").strip()
        if not prompt:
            return {"error": "prompt is required."}
        target = params.get("agent")
        category = params.get("category")
        if bool(target) == bool(category):
            return {"error": "Provide exactly one of agent= or category=."}
        return engine.dispatch(
            goal=prompt,
            target=target,
            category=category,
            context=params.get("context"),
            background=bool(params.get("background", False)),
        )

    return handler


__all__ = ["OMO_TASK_SCHEMA", "make_omo_task_handler"]
