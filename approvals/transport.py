from __future__ import annotations

import logging
import threading
from typing import Any

from approvals import channel
from approvals.policy import Mode, allow_choices, permitted_choices, resolve_mode, timeout_margin
from approvals.registry import PendingRegistry

logger = logging.getLogger(__name__)


class SessionKeys:
    """`pre_approval_request` hands `present` the gateway session_key it must inject into."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._map: dict[str, str] = {}

    def record(self, request_id: Any, session_key: Any) -> None:
        if not request_id or not session_key:
            return
        with self._lock:
            if len(self._map) > 64:
                self._map.clear()
            self._map[str(request_id)] = str(session_key)

    def take(self, request_id: str) -> str | None:
        with self._lock:
            return self._map.pop(request_id, None)


def on_pre_approval_request(**kwargs: Any) -> None:
    _KEYS.record(kwargs.get("request_id"), kwargs.get("session_key"))


_KEYS = SessionKeys()


def _deny(request: Any, reason: str) -> Any:
    logger.warning("omo approval transport denying (%s)", reason)
    return request.respond("deny")


def make_present_fn(ctx: Any, registry: PendingRegistry) -> Any:
    def present(request: Any) -> Any:
        try:
            mode = resolve_mode(ctx)
            if mode is Mode.DENY:
                return _deny(request, "approvals.mode=deny")
            if mode is Mode.AUTO:
                logger.error(
                    "omo approval transport AUTO-APPROVING a dangerous command (approvals.mode=auto): %s",
                    getattr(request, "description", ""),
                )
                return request.respond("once")

            choices = permitted_choices(request, allow_choices(ctx))
            raw_timeout = float(getattr(request, "timeout_seconds", 0) or 0.0)
            margin = min(timeout_margin(ctx), max(0.0, raw_timeout * 0.25))
            budget = max(0.0, raw_timeout - margin)
            pending = registry.add(
                request,
                choices=choices,
                session_key=_KEYS.take(request.request_id),
                timeout_seconds=budget,
            )
            if pending is None:
                return _deny(request, "approval capacity exhausted")

            ok, reason = channel.deliver(
                ctx,
                content=channel.render(request, short_id=pending.short_id, choices=choices),
                session_key=pending.session_key,
            )
            if not ok:
                registry.resolve(pending.short_id, "deny")
                return _deny(request, reason)

            pending.event.wait(timeout=budget)
            registry.expire()
            choice = pending.decision or "deny"
            if choice not in choices:
                choice = "deny"
            logger.info("omo approval transport resolved %s -> %s", pending.short_id, choice)
            return request.respond(choice)
        except BaseException as exc:
            logger.error("omo approval transport failed; denying: %s", exc, exc_info=True)
            try:
                return request.respond("deny")
            except Exception:
                raise

    return present
