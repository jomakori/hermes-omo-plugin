"""Pin the live status struct, and release it only on a clean success.

Everything here is offline: fake adapters/clients stand in for the gateway, so no
token is read and no Discord call is made. The three layers are covered where each
makes its decision — the transport (how the pin reaches the message), the tracker
(when to pin and unpin, exactly once) and the notifier (delivery, pin-cap eviction
and the one retry).
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

from orchestrator.gateway_status import GatewayStatusTransport
from orchestrator.status_notifier import StatusNotifier
from orchestrator.status_tracker import Action, StatusTracker

# ── fakes ─────────────────────────────────────────────────────────────


@dataclasses.dataclass
class SendResult:
    """Mirrors the fields the transport reads off ``gateway...SendResult``."""

    success: bool
    message_id: str | None = None
    error: str | None = None


class FakePinLimit(Exception):
    """Discord's pin cap: code 30001, HTTP 400."""

    def __init__(self, message: str = "Maximum number of pins reached (30001)") -> None:
        super().__init__(message)
        self.code = 30001
        self.status = 400


class FakePermission(Exception):
    """A refusal: 403 / 50013 (needs Manage Messages)."""

    def __init__(self, message: str = "Missing Permissions (50013)") -> None:
        super().__init__(message)
        self.code = 50013
        self.status = 403


class FakePartialMessage:
    def __init__(self, client: FakeDiscordClient, channel_id: int, message_id: int) -> None:
        self._client = client
        self._channel_id = channel_id
        self._message_id = message_id

    async def pin(self) -> None:
        self._client.pin_calls.append((self._channel_id, self._message_id))
        await self._client.pin_at(self._channel_id, self._message_id)

    async def unpin(self) -> None:
        self._client.unpin_calls.append((self._channel_id, self._message_id))
        self._client.unpin_at(self._channel_id, self._message_id)


class FakeChannel:
    def __init__(self, client: FakeDiscordClient, channel_id: int, *, has_partial: bool = True) -> None:
        self._client = client
        self.id = channel_id
        self._has_partial = has_partial

    def get_partial_message(self, message_id: int) -> Any:
        if not self._has_partial:
            return None
        return FakePartialMessage(self._client, self.id, int(message_id))


class FakeDiscordClient:
    """The bit of ``discord.Client`` the pin path touches, plus the pin bookkeeping."""

    def __init__(
        self, *, fetch_only: bool = False, pin_error: Exception | None = None, pin_cap: int | None = None
    ) -> None:
        self.channels: dict[int, FakeChannel] = {}
        self.pins: list[tuple[int, int]] = []
        self.pin_calls: list[tuple[int, int]] = []
        self.unpin_calls: list[tuple[int, int]] = []
        self.fetch_calls: list[int] = []
        self.fetch_only = fetch_only
        self.pin_error = pin_error
        self.pin_cap = pin_cap

    def add_channel(self, channel_id: int, *, has_partial: bool = True) -> FakeChannel:
        channel = FakeChannel(self, int(channel_id), has_partial=has_partial)
        self.channels[int(channel_id)] = channel
        return channel

    def get_channel(self, channel_id: int) -> FakeChannel | None:
        if self.fetch_only:
            return None
        return self.channels.get(int(channel_id))

    async def fetch_channel(self, channel_id: int) -> FakeChannel | None:
        self.fetch_calls.append(int(channel_id))
        return self.channels.get(int(channel_id))

    async def pin_at(self, channel_id: int, message_id: int) -> None:
        if self.pin_error is not None:
            raise self.pin_error
        if self.pin_cap is not None and len(self.pins) >= self.pin_cap:
            raise FakePinLimit()
        if (channel_id, message_id) not in self.pins:
            self.pins.append((channel_id, message_id))

    def unpin_at(self, channel_id: int, message_id: int) -> None:
        try:
            self.pins.remove((channel_id, message_id))
        except ValueError:
            pass


_UNSET = object()


class PinAdapter:
    """Adapter-shaped double: connection flag, send/edit/delete, optional client.

    ``client=`` sets the private ``_client`` attribute the transport falls back to;
    ``public_client=`` sets a public ``client`` attribute that must win instead.
    Leaving both unset means "no client reachable at all".
    """

    def __init__(self, client: Any = _UNSET, *, public_client: Any = _UNSET) -> None:
        self.is_connected = True
        self.sent: list[dict] = []
        if client is not _UNSET:
            self._client = client
        if public_client is not _UNSET:
            self.client = public_client

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append({"chat_id": chat_id, "content": content, "metadata": metadata})
        return SendResult(success=True, message_id=f"m{len(self.sent)}")

    async def edit_message(self, chat_id, message_id, content, *, finalize=False, metadata=None):
        return SendResult(success=True)

    async def delete_message(self, chat_id, message_id):
        return True


class PlainRunner:
    def __init__(self, adapter: Any, *, platform: str = "discord") -> None:
        self.adapters = {platform: adapter}


def _transport(adapter: Any, *, thread_id: str = "700") -> GatewayStatusTransport:
    runner = PlainRunner(adapter)
    return GatewayStatusTransport(
        {"platform": "discord", "chat_id": "42", "thread_id": thread_id},
        runner_resolver=lambda: runner,
        platform_key=lambda value: value,  # gateway.config is not importable in tests
        profile_resolver=lambda: "default",
    )


# ── transport: reaching the right message ─────────────────────────────
def test_pin_and_unpin_reach_the_thread_partial_message():
    adapter = PinAdapter(FakeDiscordClient())
    adapter._client.add_channel(700)
    transport = _transport(adapter)

    assert asyncio.run(transport.pin("omo_1", "100")) is True
    assert adapter._client.pins == [(700, 100)]
    assert asyncio.run(transport.unpin("omo_1", "100")) is True
    assert adapter._client.pins == []
    assert adapter._client.pin_calls == [(700, 100)]
    assert adapter._client.unpin_calls == [(700, 100)]


def test_pin_uses_the_chat_when_the_route_has_no_thread():
    adapter = PinAdapter(FakeDiscordClient())
    adapter._client.add_channel(42)
    transport = _transport(adapter, thread_id="")

    assert asyncio.run(transport.pin("omo_1", "100")) is True
    assert adapter._client.pins == [(42, 100)]


def test_pin_prefers_a_public_client_accessor_over_the_private_one():
    public = FakeDiscordClient()
    public.add_channel(700)
    private = FakeDiscordClient()
    private.add_channel(700)
    adapter = PinAdapter(private, public_client=public)

    assert asyncio.run(_transport(adapter).pin("omo_1", "100")) is True
    assert public.pins == [(700, 100)]
    assert private.pins == []


def test_pin_falls_back_to_fetch_channel_when_the_cache_is_cold():
    adapter = PinAdapter(FakeDiscordClient(fetch_only=True))
    adapter._client.add_channel(700)
    transport = _transport(adapter)

    assert asyncio.run(transport.pin("omo_1", "100")) is True
    assert adapter._client.fetch_calls == [700]
    assert adapter._client.pins == [(700, 100)]


def test_pin_without_a_reachable_client_is_a_silent_false():
    transport = _transport(PinAdapter())  # neither _client nor a public one

    assert asyncio.run(transport.pin("omo_1", "100")) is False
    assert transport.last_pin_limit is False


def test_pin_without_the_partial_message_capability_is_a_silent_false():
    adapter = PinAdapter(FakeDiscordClient())
    adapter._client.add_channel(700, has_partial=False)

    assert asyncio.run(_transport(adapter).pin("omo_1", "100")) is False


def test_pin_limit_is_flagged_for_the_caller():
    adapter = PinAdapter(FakeDiscordClient(pin_error=FakePinLimit()))
    adapter._client.add_channel(700)
    transport = _transport(adapter)

    assert asyncio.run(transport.pin("omo_1", "100")) is False
    assert transport.last_pin_limit is True
    assert adapter._client.pins == []


def test_a_permission_refusal_is_not_the_pin_limit():
    adapter = PinAdapter(FakeDiscordClient(pin_error=FakePermission()))
    adapter._client.add_channel(700)
    transport = _transport(adapter)

    assert asyncio.run(transport.pin("omo_1", "100")) is False
    assert transport.last_pin_limit is False


# ── tracker: when to pin, and the one unpin ───────────────────────────
def _running(run_id: str, task_id: str = "t1", agent: str = "hephaestus") -> dict:
    return {"run_id": run_id, "agent": agent, "task_id": task_id, "goal": "demo"}


def _settled(run_id: str, status: str, task_id: str = "t1", agent: str = "hephaestus") -> dict:
    return {"run_id": run_id, "agent": agent, "task_id": task_id, "status": status}


def _tracker(**kwargs: Any) -> StatusTracker:
    # No throttle in these tests: every transition is asserted, not the cadence.
    return StatusTracker(min_edit_interval=0.0, **kwargs)


def test_the_initial_post_asks_to_be_pinned():
    action = _tracker().apply("worker_running", _running("omo_1"))

    assert action is not None
    assert action.kind == "post"
    assert action.pin is True


def test_a_successful_run_pins_then_unpins_exactly_once():
    tracker = _tracker()
    tracker.apply("worker_running", _running("omo_1"))
    tracker.note_message_id("omo_1", "1000")

    action = tracker.apply("worker_succeeded", _settled("omo_1", "SUCCEEDED"))
    assert action is not None
    assert action.unpin is True
    assert action.message_id == "1000"

    # A duplicated terminal event (and a tick) must not release it a second time.
    assert not _has_unpin(tracker.apply("worker_done", _settled("omo_1", "SUCCEEDED")))
    assert all(not action.unpin for action in tracker.tick())


def test_a_failed_run_is_pinned_and_never_unpinned():
    tracker = _tracker()
    post = tracker.apply("worker_running", _running("omo_1"))
    tracker.note_message_id("omo_1", "1000")

    action = tracker.apply("worker_failed", _settled("omo_1", "FAILED"))
    assert post.pin is True
    assert not _has_unpin(action)


def test_a_blocked_run_stays_pinned():
    tracker = _tracker()
    tracker.apply("worker_running", _running("omo_1"))
    tracker.note_message_id("omo_1", "1000")

    assert not _has_unpin(tracker.apply("worker_blocked", _running("omo_1")))


def test_a_cancelled_run_stays_pinned():
    tracker = _tracker()
    tracker.apply("worker_running", _running("omo_1"))
    tracker.note_message_id("omo_1", "1000")

    assert not _has_unpin(tracker.apply("worker_cancelled", _settled("omo_1", "CANCELLED")))


def test_a_running_run_stays_pinned():
    tracker = _tracker()
    tracker.apply("worker_running", _running("omo_1"))
    tracker.note_message_id("omo_1", "1000")

    assert not _has_unpin(tracker.apply("worker_running", _running("omo_1", "t2")))


def test_a_fanout_unpins_only_after_the_last_worker_succeeds():
    tracker = _tracker()
    tracker.apply("worker_running", _running("omo_1", "t1"))
    tracker.apply("worker_running", _running("omo_1", "t2"))
    tracker.note_message_id("omo_1", "1000")

    first = tracker.apply("worker_succeeded", _settled("omo_1", "SUCCEEDED", "t1"))
    assert not _has_unpin(first)
    second = tracker.apply("worker_succeeded", _settled("omo_1", "SUCCEEDED", "t2"))
    assert _has_unpin(second)


def test_a_fanout_with_one_failure_stays_pinned():
    tracker = _tracker()
    tracker.apply("worker_running", _running("omo_1", "t1"))
    tracker.apply("worker_running", _running("omo_1", "t2"))
    tracker.note_message_id("omo_1", "1000")

    tracker.apply("worker_succeeded", _settled("omo_1", "SUCCEEDED", "t1"))
    assert not _has_unpin(tracker.apply("worker_failed", _settled("omo_2", "FAILED", "t2")))


def test_the_setting_off_never_pins_or_unpins():
    tracker = _tracker(pin_message=False)
    post = tracker.apply("worker_running", _running("omo_1"))
    tracker.note_message_id("omo_1", "1000")
    settled = tracker.apply("worker_succeeded", _settled("omo_1", "SUCCEEDED"))

    assert post.pin is False
    assert not _has_unpin(settled)


def test_a_terminal_success_with_no_render_change_still_unpins():
    # Insurance for the rare case where the final event renders the same text: the
    # release is emitted on its own (kind "unpin") instead of being lost.
    tracker = _ConstantRenderTracker(min_edit_interval=0.0)
    tracker.apply("worker_running", _running("omo_1"))
    tracker.note_message_id("omo_1", "1000")

    action = tracker.apply("worker_succeeded", _settled("omo_1", "SUCCEEDED"))

    assert action is not None
    assert action.kind == "unpin"
    assert action.message_id == "1000"


def test_non_vacuity_inverting_the_unpin_condition_reds_the_failed_run():
    # The failed-run assertion above is only meaningful if the condition it turns
    # on can actually change the outcome. Inverting it must release the pin on the
    # very scenario the real tracker leaves alone — so the negative assertion is
    # not vacuous.
    tracker = _InvertedTracker(min_edit_interval=0.0)
    tracker.apply("worker_running", _running("omo_1"))
    tracker.note_message_id("omo_1", "1000")

    action = tracker.apply("worker_failed", _settled("omo_1", "FAILED"))

    assert _has_unpin(action)


def _has_unpin(action: Action | None) -> bool:
    return action is not None and action.unpin


class _ConstantRenderTracker(StatusTracker):
    """A tracker whose text never changes, so the terminal edit is a no-op."""

    def _render(self, state: Any, now: float) -> str:
        return "unchanged"


class _InvertedTracker(StatusTracker):
    """Flips the success test, exactly the mistake the real tracker must not make."""

    def _all_succeeded(self, state: Any) -> bool:
        return not super()._all_succeeded(state)


# ── notifier: delivery, pin-cap eviction and exactly one retry ────────
class FakeTransport:
    """A transport double: records every delivery; can simulate the pin cap once."""

    def __init__(self, *, pin_limit_once: bool = False) -> None:
        self.posted: list[tuple[str, str]] = []
        self.edited: list[tuple[str, str, str]] = []
        self.deleted: list[str] = []
        self.pins: list[str] = []
        self.unpins: list[str] = []
        self.pin_attempts = 0
        self.pin_limit_once = pin_limit_once
        self.last_gone = False
        self.last_pin_limit = False

    async def post(self, run_key: str, text: str) -> str:
        message_id = f"m{len(self.posted) + 1}"
        self.posted.append((run_key, text))
        return message_id

    async def edit(self, run_key: str, message_id: str, text: str) -> bool:
        self.edited.append((run_key, message_id, text))
        return True

    async def delete(self, run_key: str, message_id: str) -> bool:
        self.deleted.append(message_id)
        return True

    async def pin(self, run_key: str, message_id: str) -> bool:
        self.pin_attempts += 1
        if self.pin_limit_once and self.pin_attempts == 1:
            self.last_pin_limit = True
            return False
        self.last_pin_limit = False
        self.pins.append(message_id)
        return True

    async def unpin(self, run_key: str, message_id: str) -> bool:
        self.unpins.append(message_id)
        return True


class NoPinTransport:
    """A platform without the pin capability: the methods simply do not exist."""

    def __init__(self) -> None:
        self.posted: list[tuple[str, str]] = []
        self.edited: list[tuple[str, str, str]] = []
        self.deleted: list[str] = []
        self.last_gone = False

    async def post(self, run_key: str, text: str) -> str:
        message_id = f"m{len(self.posted) + 1}"
        self.posted.append((run_key, text))
        return message_id

    async def edit(self, run_key: str, message_id: str, text: str) -> bool:
        self.edited.append((run_key, message_id, text))
        return True

    async def delete(self, run_key: str, message_id: str) -> bool:
        self.deleted.append(message_id)
        return True


class FakeEngine:
    def __init__(self, routes: dict[str, dict]) -> None:
        self._routes = routes
        self.messages: dict[str, str | None] = {}
        self.pins: dict[str, str | None] = {}

    def run_route(self, run_id: str) -> dict | None:
        return self._routes.get(run_id)

    def note_status_message(self, run_id: str, message_id: str | None) -> None:
        self.messages[run_id] = message_id

    def note_status_pin(self, run_id: str, pinned_id: str | None) -> None:
        self.pins[run_id] = pinned_id

    def clear_status_pin(self, run_id: str, pinned_id: str | None) -> None:
        if self.pins.get(run_id) == pinned_id:
            self.pins[run_id] = None

    def oldest_status_pin(self, exclude_run_id: str | None = None) -> tuple[str, str] | None:
        for run_id, pinned_id in self.pins.items():
            if pinned_id and run_id != exclude_run_id:
                return run_id, pinned_id
        return None


def _notifier(engine: FakeEngine, transports: dict[str, Any]) -> StatusNotifier:
    def factory(route: dict[str, Any] | None) -> Any:
        return transports.get((route or {}).get("chat_id"))

    return StatusNotifier(
        object(),
        engine=engine,
        tracker=StatusTracker(min_edit_interval=0.0),
        transport_factory=factory,
    )


def _discord_route(chat_id: str) -> dict:
    return {"platform": "discord", "chat_id": chat_id, "thread_id": ""}


def test_a_successful_run_pins_the_posted_message_and_unpins_it():
    engine = FakeEngine({"omo_1": _discord_route("42")})
    transport = FakeTransport()
    notifier = _notifier(engine, {"42": transport})

    notifier.process("worker_running", _running("omo_1"))
    assert transport.pins == ["m1"]
    assert engine.pins["omo_1"] == "m1"

    notifier.process("worker_succeeded", _settled("omo_1", "SUCCEEDED"))
    assert transport.unpins == ["m1"]
    assert engine.pins["omo_1"] is None


def test_a_failed_run_pins_but_never_unpins():
    engine = FakeEngine({"omo_1": _discord_route("42")})
    transport = FakeTransport()
    notifier = _notifier(engine, {"42": transport})

    notifier.process("worker_running", _running("omo_1"))
    notifier.process("worker_failed", _settled("omo_1", "FAILED"))

    assert transport.pins == ["m1"]
    assert transport.unpins == []
    assert engine.pins["omo_1"] == "m1"


def test_the_pin_cap_evicts_our_oldest_pin_and_retries_exactly_once():
    engine = FakeEngine({"omo_new": _discord_route("42"), "omo_old": _discord_route("77")})
    engine.pins["omo_old"] = "900"
    new_transport = FakeTransport(pin_limit_once=True)
    old_transport = FakeTransport()
    notifier = _notifier(engine, {"42": new_transport, "77": old_transport})

    notifier.process("worker_running", _running("omo_new"))

    assert new_transport.pin_attempts == 2  # the first refused, one retry only
    assert old_transport.unpins == ["900"]  # our oldest pin, released
    assert engine.pins["omo_old"] is None
    assert engine.pins["omo_new"] == "m1"


def test_a_platform_without_the_pin_capability_is_a_noop():
    engine = FakeEngine({"omo_1": _discord_route("42")})
    transport = NoPinTransport()
    notifier = _notifier(engine, {"42": transport})

    notifier.process("worker_running", _running("omo_1"))  # must not raise
    notifier.process("worker_succeeded", _settled("omo_1", "SUCCEEDED"))

    assert transport.posted
    assert not hasattr(transport, "pin")
    assert engine.pins.get("omo_1") is None
