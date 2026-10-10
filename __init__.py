from __future__ import annotations

import os
import sys
from typing import Any

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from omo_tools.jev_tool import JEV_SCHEMA, make_jev_handler  # noqa: E402
from omo_tools.omo_task_tool import OMO_TASK_SCHEMA, make_omo_task_handler  # noqa: E402
from omo_tools.omo_tool import OMO_SCHEMA, make_omo_handler, render  # noqa: E402
from orchestrator.chains import ChainResolver  # noqa: E402
from orchestrator.engine import OmoEngine  # noqa: E402
from orchestrator.guards import GuardError, check_delegation  # noqa: E402
from orchestrator.personas import AGENTS_DIR  # noqa: E402
from orchestrator.status_notifier import StatusNotifier  # noqa: E402
from orchestrator.status_tracker import (  # noqa: E402
    DEFAULT_MIN_EDIT_INTERVAL,
    DEFAULT_MOVE_INTERVAL,
    DEFAULT_PHRASE_INTERVAL,
    StatusTracker,
)
from orchestrator.store import RunStore  # noqa: E402
from orchestrator.worker_activity import make_activity_provider  # noqa: E402
from roster import AGENTS  # noqa: E402

PLUGIN_KEY = "omo"


def _settings(ctx: Any) -> dict[str, Any]:
    return {
        "max_fallback_attempts": ctx.get_config("max_fallback_attempts", 3),
        "cooldown_seconds": ctx.get_config("cooldown_seconds", 30),
        "restore_primary_after_cooldown": ctx.get_config("restore_primary_after_cooldown", True),
        "runtime_fallback": ctx.get_config("runtime_fallback", None),
        "chains": ctx.get_config("chains", None),
        "categories": ctx.get_config("categories", None),
        "enabled_agents": ctx.get_config("enabled_agents", None),
    }


def _jev_settings(ctx: Any) -> dict[str, Any]:
    return {
        "enabled": ctx.get_config("jev_enabled", True),
        "base_url": ctx.get_config("jev_base_url", "https://api.typesafe.ai"),
        "model": ctx.get_config("jev_model", "jev-latest"),
        "timeout_s": ctx.get_config("jev_timeout_s", 10),
        "thresholds": ctx.get_config("jev_thresholds", None),
        "api_key_env": ctx.get_config("jev_api_key_env", "TYPESAFE_AI_API_KEY"),
        "routing_enabled": ctx.get_config("jev_routing_enabled", False),
        "routing_threshold": ctx.get_config("jev_routing_threshold", 0.75),
    }


def _jev_report(ctx: Any) -> str:
    """Render the shadow agreement/calibration report from the JSONL sink."""
    try:
        from jev.metrics import format_report
        from jev.shadow import DEFAULT_SHADOW_PATH, ShadowLogger

        path = ctx.get_config("jev_shadow_path", DEFAULT_SHADOW_PATH)
        return format_report(ShadowLogger(path).read())
    except Exception as exc:
        return f"Jev shadow report unavailable: {exc}"


def _store(ctx: Any) -> RunStore:
    return RunStore(
        path=ctx.get_config("state_path", None),
        max_runs=ctx.get_config("max_persisted_runs", None),
    )


def _status_notifier(ctx: Any, engine: OmoEngine) -> StatusNotifier | None:
    """Build the live status notifier, or None when switched off.

    The tracker is pure (see ``orchestrator.status_tracker``), so the throttle and
    dedupe rules are unit-tested without a gateway; only the transport needs one.
    """
    if not _config_flag(ctx, "status_message_enabled", True):
        return None
    tracker = StatusTracker(
        min_edit_interval=_config_number(ctx, "status_edit_interval", DEFAULT_MIN_EDIT_INTERVAL),
        phrase_interval=_config_number(ctx, "status_phrase_interval", DEFAULT_PHRASE_INTERVAL),
        move_interval=_config_number(ctx, "status_move_interval", DEFAULT_MOVE_INTERVAL),
        # Pin the live struct while the run is live, and release it only when the
        # run ends with every worker succeeded. On by default; `status_pin_message`
        # off means neither the pin nor its matching unpin is ever emitted.
        pin_message=_config_flag(ctx, "status_pin_message", True),
        # The activity line reads the worker's own profile DB; injected so the
        # tracker stays pure and unit-testable without a filesystem.
        activity_provider=make_activity_provider(profiles_dir=ctx.get_config("activity_profiles_dir", None)),
    )
    notifier = StatusNotifier(ctx, engine=engine, tracker=tracker)
    # A restart keeps editing the message the run already has, rather than posting
    # a second one: adopt every persisted id before the first event arrives.
    for run_id, run in getattr(engine, "runs", {}).items():
        message_id = getattr(run, "status_message_id", None)
        if message_id:
            notifier.adopt(run_id, message_id)
    return notifier


def _config_flag(ctx: Any, key: str, default: bool) -> bool:
    try:
        return bool(ctx.get_config(key, default))
    except Exception:
        return default


def _config_number(ctx: Any, key: str, default: float) -> float:
    try:
        return float(ctx.get_config(key, default))
    except Exception:
        return default


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
    engine = OmoEngine(ctx, store=_store(ctx))
    engine.chains = ChainResolver(_settings(ctx))

    # The live status message: one Discord message per run, edited in place as the
    # run moves. Built before the tools so the very first transition is captured.
    notifier = _status_notifier(ctx, engine)
    if notifier is not None:
        engine.set_status_notifier(notifier)
        notifier.start()

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
    _js = _jev_settings(ctx)
    ctx.register_tool(
        name="jev_ask",
        toolset="omo",
        schema=JEV_SCHEMA,
        handler=make_jev_handler(
            base_url=_js["base_url"],
            model=_js["model"],
            timeout_s=float(_js["timeout_s"]),
            api_key_env=_js["api_key_env"],
            threshold_overrides=_js["thresholds"],
            enabled=bool(_js["enabled"]),
        ),
        check_fn=lambda: True,
        requires_env=[],
        is_async=True,
        description="Advisory typed-decision classifier (TypeSafe Jev); returns a probability envelope.",
    )
    _register_optional(
        ctx,
        "register_command",
        "omo",
        handler=lambda *_a, **_k: render(engine.status()),
        description="Show OMO runs and workers.",
    )
    _register_optional(
        ctx,
        "register_command",
        "jev_report",
        handler=lambda *_a, **_k: _jev_report(ctx),
        description="Show the Jev shadow agreement and calibration report.",
    )
    for name in AGENTS:
        persona = AGENTS_DIR / f"{name}.md"
        if persona.is_file():
            _register_optional(ctx, "register_skill", name, persona, description=f"Full OMO persona for {name}")
    _register_optional(ctx, "on_unload", _make_teardown(engine, notifier))


def _make_teardown(engine: OmoEngine, notifier: StatusNotifier | None) -> Any:
    """One unload callback: stop the status worker, then settle the runs."""

    def _teardown() -> None:
        if notifier is not None:
            notifier.stop()
        engine.shutdown()

    return _teardown


__all__ = ["register", "PLUGIN_KEY", "GuardError", "check_delegation"]
