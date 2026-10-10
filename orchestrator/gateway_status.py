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

Besides ``send`` / ``edit_message`` / ``delete_message`` it exposes ``pin`` and
``unpin``: the live struct is pinned once it lands, and unpinned only when the
run ends with every worker succeeded. Both reach the platform client the adapter
owns (a public accessor when it has one, else its private ``_client`` — a
coupling that is documented and fails safe to a logged no-op if the host renames
it), resolve the channel exactly as the post did, and take the message through
``channel.get_partial_message(id)``. Discord is the only platform this supports;
a platform without the capability degrades to a no-op.

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

# Discord's pin limit (error code 30001, "Maximum number of pins reached"). It is
# the one pin failure we act on: the caller evicts its own oldest pin and retries.
_PIN_LIMIT_MARKERS = ("30001", "maximum number of pins", "pin limit")
# A refusal (403 / 50013 "Missing Permissions", "Cannot execute action on a
# system message") is permanent: log it loudly and stop, rather than retry.
_PIN_REFUSED_MARKERS = ("50013", "403", "missing permissions", "missing access", "forbidden", "manage messages")

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
        # Set by the last pin: True when Discord refused it because the channel's
        # pin cap is reached (30001), so the caller knows to evict and retry.
        self.last_pin_limit = False

    @property
    def usable(self) -> bool:
        """Deliverable only when we know the chat and can resolve an adapter."""
        return bool(self.chat_id)

    @property
    def _target(self) -> str:
        """The channel that holds the message: the thread when the route has one.

        ``edit_message`` resolves the channel from this argument (discord
        adapter.py:3008), so the edit must name the same channel the post used.
        """
        return self.thread_id or self.chat_id

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
        target = self._target
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
            result = await adapter.edit_message(self._target, message_id, text, metadata=self._metadata())
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

    async def delete(self, run_key: str, message_id: str) -> bool:
        """Delete the previous struct after a move; False when it could not be.

        The moving struct posts the fresh copy first and deletes the previous one
        second, so a failed delete only leaves a stale copy behind — never a lost
        live message. A platform whose adapter has no ``delete_message`` (the base
        class returns False) simply keeps both copies.
        """
        adapter = self._adapter()
        if adapter is None or not message_id:
            return False
        delete_message: Any = getattr(adapter, "delete_message", None)
        if not callable(delete_message):
            return False
        try:
            return bool(await delete_message(self._target, str(message_id)))
        except Exception as exc:
            logger.debug("OMO status delete failed for %s: %s", run_key, exc)
            return False

    async def pin(self, run_key: str, message_id: str) -> bool:
        """Pin the live struct; False when it could not be pinned.

        Follows the same best-effort shape as ``post`` / ``edit`` / ``delete``:
        resolve the adapter, reach the message, ``await message.pin()``. A pin
        refused because the channel's pin cap is full (30001) sets
        :attr:`last_pin_limit` so the caller can evict its own oldest pin and
        retry once; a permission refusal (403 / 50013) is logged as a warning and
        never retried. Any other failure is debug-logged as transient.
        """
        self.last_pin_limit = False
        message = await self._partial_message(message_id)
        if message is None:
            return False
        try:
            await message.pin()
        except Exception as exc:
            self.last_pin_limit = _looks_pin_limit(exc)
            if self.last_pin_limit:
                logger.warning("OMO status pin hit the pin limit for %s: %s", run_key, _error_text(exc))
            elif _looks_pin_refused(exc):
                logger.warning("OMO status pin refused for %s (needs Manage Messages): %s", run_key, _error_text(exc))
            else:
                logger.debug("OMO status pin failed for %s: %s", run_key, exc)
            return False
        return True

    async def unpin(self, run_key: str, message_id: str) -> bool:
        """Unpin the run's struct; False when it could not be unpinned.

        Best-effort and quiet, like ``delete``: a message already unpinned, an
        adapter without the capability, or a transient network error all leave
        the caller's bookkeeping to decide whether to try again.
        """
        message = await self._partial_message(message_id)
        if message is None:
            return False
        try:
            await message.unpin()
        except Exception as exc:
            logger.debug("OMO status unpin failed for %s: %s", run_key, exc)
            return False
        return True

    async def _partial_message(self, message_id: str) -> Any:
        """The partial message object for the channel the post used, or None.

        Resolution mirrors the post exactly: the thread when the route has one,
        else the chat. ``get_channel`` first (cache), ``fetch_channel`` on miss
        (REST) — the same pair the adapter's own ``_resolve_channel`` uses.
        """
        if not message_id:
            return None
        adapter = self._adapter()
        if adapter is None:
            return None
        client = _adapter_client(adapter)
        if client is None:
            return None
        channel = await self._channel(client)
        if channel is None:
            return None
        partial = getattr(channel, "get_partial_message", None)
        if not callable(partial):
            return None
        try:
            return partial(int(message_id))
        except Exception:
            return None

    async def _channel(self, client: Any) -> Any:
        target = self._target
        if not target:
            return None
        try:
            channel_id = int(target)
        except (TypeError, ValueError):
            return None
        get_channel: Any = getattr(client, "get_channel", None)
        channel = None
        if callable(get_channel):
            try:
                channel = get_channel(channel_id)
            except Exception:
                channel = None
        if channel is not None:
            return channel
        fetch_channel: Any = getattr(client, "fetch_channel", None)
        if not callable(fetch_channel):
            return None
        try:
            return await fetch_channel(channel_id)
        except Exception:
            return None


def _adapter_client(adapter: Any) -> Any:
    """The platform client the adapter owns, or None.

    A public accessor is preferred when the host exposes one; otherwise the
    private ``_client`` attribute is used. That private coupling is deliberate
    and documented: the adapter exposes no public client, and a host rename makes
    this fail safe — ``getattr`` returns None and pinning degrades to a logged
    no-op rather than raising.
    """
    for name in ("get_client", "client", "bot"):
        value = getattr(adapter, name, None)
        if callable(value):
            try:
                value = value()
            except Exception:
                value = None
        if value is not None:
            return value
    return getattr(adapter, "_client", None)


def _error_text(exc: Exception) -> str:
    """The code, status and message of a platform exception, for the log line."""
    code = getattr(exc, "code", None)
    status = getattr(exc, "status", None)
    parts = [str(part) for part in (code, status, exc) if part is not None]
    return " ".join(parts)


def _looks_pin_limit(exc: Any) -> bool:
    text = _error_text(exc).lower() if isinstance(exc, Exception) else str(exc).lower()
    return any(marker in text for marker in _PIN_LIMIT_MARKERS)


def _looks_pin_refused(exc: Any) -> bool:
    text = _error_text(exc).lower() if isinstance(exc, Exception) else str(exc).lower()
    return any(marker in text for marker in _PIN_REFUSED_MARKERS)


def _looks_gone(error: Any) -> bool:
    text = str(error or "").lower()
    return any(marker in text for marker in _GONE_MARKERS)
