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
from orchestrator.guards import GuardError, check_delegation  # noqa: E402
from orchestrator.personas import AGENTS_DIR  # noqa: E402
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


def _register_optional(ctx: Any, surface: str, *args: Any, **kwargs: Any) -> bool:
    """Attempt one optional surface, independently of every other.

    A host that does not expose a surface must not cost us the ones it does.
    Required surfaces stay direct calls, so a missing one fails loudly; what is
    avoided is branching the whole path on a host attribute - an attribute that
    exists but does nothing would register nothing at all while the plugin still
    reports as enabled.
    """
    register = getattr(ctx, surface, None)
    if not callable(register):
        return False
    register(*args, **kwargs)
    return True


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
    _register_optional(
        ctx,
        "register_command",
        "omo",
        handler=lambda *_a, **_k: render(engine.status()),
        description="Show OMO runs and workers.",
    )
    for name in AGENTS:
        persona = AGENTS_DIR / f"{name}.md"
        if persona.is_file():
            _register_optional(ctx, "register_skill", name, persona, description=f"Full OMO persona for {name}")
    _register_optional(ctx, "on_unload", engine.shutdown)


__all__ = ["register", "PLUGIN_KEY", "GuardError", "check_delegation"]
