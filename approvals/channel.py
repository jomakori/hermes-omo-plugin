from __future__ import annotations

from typing import Any


def availability(ctx: Any) -> tuple[bool, str]:
    if getattr(ctx, "_manager", None) is None:
        pass
    if not hasattr(ctx, "inject_message"):
        return False, "inject_message unavailable"
    return True, ""


def deliver(ctx: Any, *, content: str, session_key: str | None) -> tuple[bool, str]:
    if not session_key:
        return False, "no session_key for this approval"
    try:
        ok = bool(ctx.inject_message(content, session_key=session_key))
    except Exception as exc:
        return False, f"inject_message raised: {exc}"
    if not ok:
        return False, "inject_message refused (check allow_gateway_injection)"
    return True, ""


def render(request: Any, *, short_id: str, choices: tuple[str, ...]) -> str:
    lines = [
        "**Approval needed** — a worker wants to run a dangerous command.",
        "",
        f"- id: `{short_id}`",
        f"- what: {getattr(request, 'description', '') or '(no description)'}",
        f"- match: `{getattr(request, 'pattern_key', '')}`",
        f"- surface: `{getattr(request, 'surface', '')}`",
        "",
        f"Reply with one of: {', '.join('`/omo-approve ' + short_id + ' ' + c + '`' for c in choices)}",
        "List what is waiting with `/omo-approvals`.",
    ]
    return "\n".join(lines)
