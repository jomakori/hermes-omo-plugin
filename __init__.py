from __future__ import annotations

import logging
import os
import sys
from typing import Any

logger = logging.getLogger(__name__)

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from approvals.commands import make_omo_approvals, make_omo_approve  # noqa: E402
from approvals.policy import max_pending, resolve_mode  # noqa: E402
from approvals.registry import PendingRegistry  # noqa: E402
from approvals.transport import make_present_fn, on_pre_approval_request  # noqa: E402
from orchestrator.chains import ChainResolver  # noqa: E402
from orchestrator.engine import OmoEngine  # noqa: E402
from orchestrator.guards import (  # noqa: E402
    READ_ONLY_WORKERS,
    GuardError,
    check_delegation,
    read_only_pre_tool_call,
)
from roster import AGENTS  # noqa: E402
from tools.omo_task_tool import OMO_TASK_SCHEMA, make_omo_task_handler  # noqa: E402
from tools.omo_tool import OMO_SCHEMA, make_omo_handler  # noqa: E402

PLUGIN_KEY = "omo"


def _settings(ctx: Any) -> dict[str, Any]:
    return {
        "mcp_enabled": ctx.get_config("mcp_enabled", True),
        "max_fallback_attempts": ctx.get_config("max_fallback_attempts", 3),
        "cooldown_seconds": ctx.get_config("cooldown_seconds", 30),
        "restore_primary_after_cooldown": ctx.get_config("restore_primary_after_cooldown", True),
        "chains": ctx.get_config("chains", None),
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

    registry = PendingRegistry(max_pending(ctx))
    ctx.register_approval_transport(PLUGIN_KEY, make_present_fn(ctx, registry))
    ctx.register_hook("pre_approval_request", on_pre_approval_request)
    ctx.register_command(
        "omo-approve",
        handler=make_omo_approve(registry),
        description="Answer a waiting OMO approval.",
        args_hint="<id> <once|session|always|deny>",
    )
    ctx.register_command(
        "omo-approvals",
        handler=make_omo_approvals(registry),
        description="List OMO approvals waiting for an answer.",
    )

    ctx.register_command("omo", handler=lambda *_a, **_k: engine.status(), description="Show OMO runs and workers.")

    def _shutdown() -> None:
        registry.deny_all("plugin unloaded")
        engine.shutdown()

    ctx.on_unload(_shutdown)
    _log_transport_state(ctx)


def _log_transport_state(ctx: Any) -> None:
    if resolve_mode(ctx).value == "forward":
        logger.info(
            "omo approval transport registered. Forwarding is inert until the host sets "
            "security.approval.transport: omo in config.yaml and "
            "plugins.entries.omo.allow_gateway_injection: true; until then Hermes auto-denies "
            "worker dangerous commands."
        )


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
