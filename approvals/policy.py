from __future__ import annotations

import enum
from collections.abc import Iterable
from typing import Any

AUTO_ACK = "i-understand-unattended-approval"
DEFAULT_MODE = "forward"
DEFAULT_ALLOW_CHOICES = ("once", "deny")
DEFAULT_MAX_PENDING = 6


class Mode(enum.StrEnum):
    FORWARD = "forward"
    DENY = "deny"
    AUTO = "auto"


def _setting(ctx: Any, dotted: str, flat: str, default: Any) -> Any:
    for key in (dotted, flat):
        try:
            value = ctx.get_config(key, None)
        except Exception:
            value = None
        if value is not None:
            return value
    return default


def resolve_mode(ctx: Any) -> Mode:
    raw = str(_setting(ctx, "approvals.mode", "approval_mode", DEFAULT_MODE) or "").strip().lower()
    if raw == Mode.AUTO.value:
        ack = str(_setting(ctx, "approvals.auto_ack", "approval_auto_ack", "") or "").strip().lower()
        if ack == AUTO_ACK:
            return Mode.AUTO
        return Mode.FORWARD
    if raw == Mode.DENY.value:
        return Mode.DENY
    return Mode.FORWARD


def allow_choices(ctx: Any) -> tuple[str, ...]:
    raw = _setting(ctx, "approvals.allow_choices", "approval_allow_choices", None)
    if not isinstance(raw, (list, tuple)) or not raw:
        return DEFAULT_ALLOW_CHOICES
    cleaned = tuple(
        choice
        for choice in (str(item).strip().lower() for item in raw)
        if choice in ("once", "session", "always", "deny")
    )
    return cleaned or DEFAULT_ALLOW_CHOICES


def max_pending(ctx: Any) -> int:
    raw = _setting(ctx, "approvals.max_pending", "approval_max_pending", DEFAULT_MAX_PENDING)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_PENDING
    return max(1, min(value, 7))


def timeout_margin(ctx: Any) -> float:
    raw = _setting(ctx, "approvals.timeout_margin_seconds", "approval_timeout_margin", 5)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 5.0
    return max(0.0, min(value, 60.0))


def permitted_choices(request: Any, allow: Iterable[str]) -> tuple[str, ...]:
    allowed = tuple(choice for choice in getattr(request, "allowed_choices", ()) or () if choice in set(allow))
    return allowed or ("deny",)
