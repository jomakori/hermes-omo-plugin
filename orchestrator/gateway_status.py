"""Deliver the run's status message through the gateway's own platform adapter.

The host exposes **no plugin-facing "send" verb**: ``ctx.platform_actions`` (the
capability-gated facade) carries only ``add_reaction`` and ``set_thread_title``,
so a plugin that must post and then edit a message cannot reach a channel through
a sanctioned surface at all. This module therefore reaches the adapter the
gateway already runs for the session's platform — the same object the host's own
notification code posts through — and uses its ``send`` / ``edit_message``
methods (the latter is the PATCH-in-place call).

That direct access is a **documented deviation** from the consent-gated facade
(there is no capability to grant here because there is no verb): it is resolved
through the runner's own profile-aware, fail-closed ``_authorization_adapter``
lookup — the same one ``hermes_cli.platform_actions`` uses — so a plugin scoped to
one profile can never post through another profile's bot.

The adapter's HTTP session (aiohttp / discord.py) is bound to the **gateway's
event loop**. A coroutine that touches it must run on that loop; running it on a
fresh loop from another thread raises and the delivery is silently lost. The
transport therefore exposes :attr:`GatewayStatusTransport.loop` so the notifier
can schedule delivery onto the gateway loop (see ``status_notifier._await``)
rather than call ``asyncio.run`` off-loop.

Everything here is best-effort: a run must never fail because its progress
message could not be delivered. The whole module is import-guarded so the plugin
still loads where the gateway package is absent (CLI, unit tests).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Discord's "Unknown Message" (10008) and the platform-agnostic phrasings a
# deleted message produces. A gone message means "post a fresh one"; any other
# failure is transient and keeps the id.
_GONE_MARKERS = ("10008", "unknown message", "not found", "no longer exists")

__all__ = ["GatewayStatusTransport", "gateway_runner"]


def gateway_runner() -> Any | None:
    """The live gateway runner, or None when no gateway is running in this process."""
    try:
        from gateway import run as gateway_run  # noqa: PLC0415 - optional, gateway-only

        return gateway_run._gateway_runner_ref()
    except Exception:
        return None


def _platform_enum(value: str) -> Any:
    from gateway.config import Platform  # noqa: PLC0415 - optional, gateway-only

    return Platform(value)


def _active_profile() -> str:
    from hermes_cli.profiles import get_active_profile_name  # noqa: PLC0415

    return get_active_profile_name()


class GatewayStatusTransport:
    """One message per run on the session's platform: post once, then edit in place."""

    def __init__(
        self,
        route: dict[str, Any] | None,
        *,
        runner_resolver: Any = None,
        platform_key: Any = None,
        profile_resolver: Any = None,
    ) -> None:
        route = route or {}
        self.platform = str(route.get("platform") or "").strip()
        self.chat_id = str(route.get("chat_id") or "").strip()
        self.thread_id = str(route.get("thread_id") or "").strip()
        # Test seams: production uses the gateway imports; a unit test injects the
        # runner/resolver so the exact send/edit calls can be asserted with no
        # gateway and no network.
        self._runner_resolver = runner_resolver or gateway_runner
        self._platform_key = platform_key or _platform_enum
        self._profile_resolver = profile_resolver or _active_profile
        # Set by the last edit: True when the target message is gone (post anew),
        # False when the failure was transient (keep editing the same id).
        self.last_gone = False

    @property
    def usable(self) -> bool:
        """Deliverable only when we know the chat and can resolve an adapter."""
        return bool(self.chat_id)

    @property
    def loop(self) -> Any | None:
        """The gateway's event loop, or None when no live gateway owns this delivery.

        The adapter's HTTP session is bound here; the notifier schedules delivery
        onto it from its worker thread instead of running a fresh loop.
        """
        runner = self._runner_resolver()
        return getattr(runner, "_gateway_loop", None) if runner is not None else None

    def _adapter(self) -> Any:
        runner = self._runner_resolver()
        if runner is None:
            return None
        try:
            key = self._platform_key(self.platform) if self.platform else None
        except Exception:
            return None
        if key is None:
            return None
        # Profile-aware, fail-closed: the same lookup the host's gated facade uses,
        # so a plugin scoped to one profile never posts through another's bot.
        resolve = getattr(runner, "_authorization_adapter", None)
        if callable(resolve):
            try:
                profile = self._profile_resolver()
            except Exception:
                return None
            adapter = resolve(key, profile)
        else:
            adapters = getattr(runner, "adapters", None)
            if not isinstance(adapters, dict):
                return None
            adapter = adapters.get(key)
        if adapter is None:
            return None
        # Fail closed on a disconnected adapter, exactly as the host's own gated
        # facade does (platform_actions._resolve_adapter): a call that cannot
        # succeed is not worth making.
        try:
            connected = bool(adapter.is_connected)
        except Exception:
            connected = False
        return adapter if connected else None

    def _metadata(self) -> dict[str, Any] | None:
        if self.thread_id:
            return {"thread_id": self.thread_id}
        return None

    async def post(self, run_key: str, text: str) -> str | None:
        """Send a fresh message; return its id, or None when it could not be sent."""
        adapter = self._adapter()
        if adapter is None:
            return None
        target = self.thread_id or self.chat_id
        try:
            result = await adapter.send(target, text, metadata=self._metadata())
        except Exception as exc:
            logger.debug("OMO status post failed for %s: %s", run_key, exc)
            return None
        if getattr(result, "success", False):
            return str(getattr(result, "message_id", "") or "") or None
        logger.debug("OMO status post declined for %s: %s", run_key, getattr(result, "error", ""))
        self.last_gone = _looks_gone(getattr(result, "error", ""))
        return None

    async def edit(self, run_key: str, message_id: str, text: str) -> bool:
        """Edit the existing message in place; False when it must be re-posted."""
        adapter = self._adapter()
        if adapter is None:
            self.last_gone = False
            return False
        try:
            result = await adapter.edit_message(self.chat_id, message_id, text, metadata=self._metadata())
        except Exception as exc:
            logger.debug("OMO status edit failed for %s: %s", run_key, exc)
            self.last_gone = False
            return False
        if getattr(result, "success", False):
            self.last_gone = False
            return True
        error = str(getattr(result, "error", "") or "")
        self.last_gone = _looks_gone(error)
        logger.debug("OMO status edit declined for %s: %s", run_key, error)
        return False


def _looks_gone(error: Any) -> bool:
    text = str(error or "").lower()
    return any(marker in text for marker in _GONE_MARKERS)
