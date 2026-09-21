from __future__ import annotations

import os
import sys
from typing import Any

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from omo_tools.omo_task_tool import OMO_TASK_SCHEMA, make_omo_task_handler  # noqa: E402
from omo_tools.omo_tool import OMO_SCHEMA, make_omo_handler, render  # noqa: E402
from orchestrator.chains import ChainResolver  # noqa: E402
from orchestrator.engine import OmoEngine  # noqa: E402
from orchestrator.guards import (  # noqa: E402
    READ_ONLY_WORKERS,
    GuardError,
    check_delegation,
    read_only_pre_tool_call,
)
from roster import AGENTS  # noqa: E402

PLUGIN_KEY = "omo"


def _settings(ctx: Any) -> dict[str, Any]:
    return {
        "mcp_enabled": ctx.get_config("mcp_enabled", True),
        "max_fallback_attempts": ctx.get_config("max_fallback_attempts", 3),
        "cooldown_seconds": ctx.get_config("cooldown_seconds", 30),
        "restore_primary_after_cooldown": ctx.get_config("restore_primary_after_cooldown", True),
        "runtime_fallback": ctx.get_config("runtime_fallback", None),
        "chains": ctx.get_config("chains", None),
        "categories": ctx.get_config("categories", None),
        "enabled_agents": ctx.get_config("enabled_agents", None),
    }


def register(ctx: Any) -> None:
    engine = OmoEngine(ctx)
    engine.chains = ChainResolver(_settings(ctx))

    ctx.register_tool(
        name="omo",
        toolset="omo",
        schema=OMO_SCHEMA,
        handler=make_omo_handler(engine),
        check_fn=lambda: True,
        requires_env=[],
        is_async=True,
        description="Dispatch engineering work to the native OMO agent fleet.",
        emoji="\U0001f3d7\ufe0f",
    )
    ctx.register_tool(
        name="omo_task",
        toolset="omo",
        schema=OMO_TASK_SCHEMA,
        handler=make_omo_task_handler(engine),
        check_fn=lambda: True,
        requires_env=[],
        is_async=True,
        description="Delegate a subtask to one OMO agent or category worker.",
    )
    ctx.register_hook("pre_tool_call", read_only_pre_tool_call)
    ctx.register_hook("subagent_start", _on_subagent_start)
    ctx.register_hook("subagent_stop", _on_subagent_stop)
    ctx.register_command(
        "omo", handler=lambda *_a, **_k: render(engine.status()), description="Show OMO runs and workers."
    )
    ctx.on_unload(engine.shutdown)


def _on_subagent_start(**kwargs: Any) -> None:
    metadata = kwargs.get("metadata") or {}
    agent_name = str(metadata.get("omo_agent") or "")
    session_id = str(kwargs.get("session_id") or "")
    spec = AGENTS.get(agent_name)
    if spec is not None and spec.read_only and session_id:
        READ_ONLY_WORKERS.mark(session_id, agent_name)


def _on_subagent_stop(**kwargs: Any) -> None:
    session_id = str(kwargs.get("session_id") or "")
    if session_id:
        READ_ONLY_WORKERS.clear(session_id)


__all__ = ["register", "PLUGIN_KEY", "GuardError", "check_delegation"]
