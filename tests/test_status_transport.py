"""The real transport contract, with a real-adapter-shaped stub and no gateway.

``GatewayStatusTransport`` reaches the platform adapter the gateway runs; every
test injects a stub runner so the exact ``send`` / ``edit_message`` calls and the
cross-thread scheduling onto the gateway loop are asserted without a network or a
live gateway.
"""

from __future__ import annotations

import asyncio
import dataclasses
import threading
import time

from orchestrator.gateway_status import GatewayStatusTransport
from orchestrator.status_notifier import StatusNotifier
from orchestrator.status_tracker import StatusTracker


@dataclasses.dataclass
class SendResult:
    """Mirrors ``gateway.platforms.base.SendResult`` (the fields the transport reads)."""

    success: bool
    message_id: str | None = None
    error: str | None = None


class StubAdapter:
    """Same method signatures as the real adapter (see discord adapter.py:2813/2993)."""

    def __init__(self, *, fail_edit: bool = False, gone_edit: bool = False) -> None:
        self.sent: list[dict] = []
        self.edited: list[dict] = []
        self.thread_ids: list[int] = []
        self.fail_edit = fail_edit
        self.gone_edit = gone_edit
        self.is_connected = True

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.thread_ids.append(threading.get_ident())
        self.sent.append({"chat_id": chat_id, "content": content, "reply_to": reply_to, "metadata": metadata})
        return SendResult(success=True, message_id="msg-1")

    async def edit_message(self, chat_id, message_id, content, *, finalize=False, metadata=None):
        self.thread_ids.append(threading.get_ident())
        self.edited.append({"chat_id": chat_id, "message_id": message_id, "content": content, "metadata": metadata})
        if self.gone_edit:
            return SendResult(success=False, error="Unknown Message (10008)")
        if self.fail_edit:
            return SendResult(success=False, error="transient connection reset")
        return SendResult(success=True)


class PlainRunner:
    """Bare runner: only ``adapters`` + the loop, so the fallback lookup is exercised."""

    def __init__(self, adapter=None, *, loop=None, platform: str = "discord") -> None:
        self.adapters = {platform: adapter} if adapter is not None else {}
        self._gateway_loop = loop


class ProfileRunner(PlainRunner):
    """Runner with the host's profile-aware, fail-closed ``_authorization_adapter``."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.calls: list[tuple] = []

    def _authorization_adapter(self, platform, profile=None):
        self.calls.append((platform, profile))
        return self.adapters.get(platform)


def _transport(adapter, *, loop=None, profile_aware=False, profile="default", thread_id="", runner=None):
    if runner is None:
        cls = ProfileRunner if profile_aware else PlainRunner
        runner = cls(adapter, loop=loop)
    return (
        GatewayStatusTransport(
            {"platform": "discord", "chat_id": "42", "thread_id": thread_id},
            runner_resolver=lambda: runner,
            platform_key=lambda value: value,  # gateway.config is not importable in tests
            profile_resolver=lambda: profile,
        ),
        runner,
    )


class ChunkOnlyEngine:
    """The notifier only needs the route + message-id sink."""

    def __init__(self, route) -> None:
        self._route = route
        self.recorded: list[tuple] = []

    def run_route(self, run_id):
        return self._route

    def note_status_message(self, run_id, message_id):
        self.recorded.append((run_id, message_id))


# ── exact adapter calls ───────────────────────────────────────────────
def test_post_uses_the_adapter_send_signature():
    adapter = StubAdapter()
    transport, _ = _transport(adapter)

    assert asyncio.run(transport.post("omo_1", "hello")) == "msg-1"
    assert adapter.sent == [
        {"chat_id": "42", "content": "hello", "reply_to": None, "metadata": None}
    ]


def test_edit_uses_the_adapter_edit_message_signature():
    adapter = StubAdapter()
    transport, _ = _transport(adapter)

    assert asyncio.run(transport.edit("omo_1", "msg-1", "updated")) is True
    assert adapter.edited == [
        {"chat_id": "42", "message_id": "msg-1", "content": "updated", "metadata": None}
    ]


def test_thread_route_posts_to_the_thread_and_forwards_metadata():
    adapter = StubAdapter()
    transport, _ = _transport(adapter, thread_id="t7")

    asyncio.run(transport.post("omo_1", "x"))
    assert adapter.sent[0]["chat_id"] == "t7"
    assert adapter.sent[0]["metadata"] == {"thread_id": "t7"}


def test_thread_route_edits_the_message_in_the_thread_it_posted_to():
    """The edit must name the channel holding the message, not the parent chat."""
    adapter = StubAdapter()
    transport, _ = _transport(adapter, thread_id="t7")

    asyncio.run(transport.post("omo_1", "x"))
    assert asyncio.run(transport.edit("omo_1", "msg-1", "updated")) is True
    assert adapter.edited[0]["chat_id"] == "t7"


def test_missing_adapter_is_a_silent_no_op():
    transport, _ = _transport(None, runner=PlainRunner(adapter=None))

    assert asyncio.run(transport.post("omo_1", "x")) is None
    assert asyncio.run(transport.edit("omo_1", "m", "x")) is False


def test_disconnected_adapter_fails_closed_without_a_call():
    adapter = StubAdapter()
    adapter.is_connected = False
    transport, _ = _transport(adapter)

    assert asyncio.run(transport.post("omo_1", "x")) is None
    assert asyncio.run(transport.edit("omo_1", "m", "x")) is False
    assert adapter.sent == [] and adapter.edited == []


# ── adapter resolution ────────────────────────────────────────────────
def test_profile_aware_lookup_is_used_when_available():
    adapter = StubAdapter()
    transport, runner = _transport(adapter, profile_aware=True, profile="work")

    asyncio.run(transport.post("omo_1", "x"))
    assert runner.calls == [("discord", "work")]


def test_unresolvable_profile_fails_closed_without_posting():
    adapter = StubAdapter()
    runner = ProfileRunner(adapter)

    def boom():
        raise RuntimeError("profile unresolved")

    transport = GatewayStatusTransport(
        {"platform": "discord", "chat_id": "42"},
        runner_resolver=lambda: runner,
        platform_key=lambda value: value,
        profile_resolver=boom,
    )
    assert asyncio.run(transport.post("omo_1", "x")) is None
    assert adapter.sent == []


# ── gone vs transient ─────────────────────────────────────────────────
def test_gone_edit_marks_last_gone_so_the_next_post_is_fresh():
    adapter = StubAdapter(gone_edit=True)
    transport, _ = _transport(adapter)

    assert asyncio.run(transport.edit("omo_1", "msg-1", "x")) is False
    assert transport.last_gone is True


def test_transient_edit_failure_keeps_the_message_id():
    adapter = StubAdapter(fail_edit=True)
    transport, _ = _transport(adapter)

    assert asyncio.run(transport.edit("omo_1", "msg-1", "x")) is False
    assert transport.last_gone is False


# ── cross-thread delivery: the loop-bound path ────────────────────────
def _spin_loop():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True, name="fake-gateway-loop")
    thread.start()
    for _ in range(500):
        if loop.is_running():
            break
        time.sleep(0.002)
    return loop, thread


def test_delivery_runs_on_the_gateway_loop_not_a_fresh_loop():
    """The adapter session lives on the gateway loop; delivery must run there.

    Under the old ``asyncio.run`` bridge the coroutine executed on the caller's
    thread (or raised off-loop against a real session). The adapter records the
    executing thread id, so this pins the fix: both calls run on the loop thread.
    """
    loop, thread = _spin_loop()
    loop_ident = thread.ident
    caller_ident = threading.get_ident()
    try:
        adapter = StubAdapter()
        transport, _ = _transport(adapter, loop=loop)
        engine = ChunkOnlyEngine({"platform": "discord", "chat_id": "42"})
        notifier = StatusNotifier(
            None,
            engine=engine,
            tracker=StatusTracker(clock=lambda: 1000.0),
            transport_factory=lambda route: transport,
        )

        notifier.process("worker_running", {"run_id": "omo_1", "goal": "g", "agent": "hephaestus"})
        notifier.process("worker_succeeded", {"run_id": "omo_1", "goal": "g", "agent": "hephaestus"})
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(2.0)

    assert caller_ident != loop_ident
    assert len(adapter.sent) == 1
    assert len(adapter.edited) == 1
    assert all(tid == loop_ident for tid in adapter.thread_ids)


def test_delivery_without_a_gateway_loop_still_runs_locally():
    """No gateway (CLI): the coroutine runs here rather than being dropped."""
    adapter = StubAdapter()
    transport, _ = _transport(adapter, loop=None)
    engine = ChunkOnlyEngine({"platform": "discord", "chat_id": "42"})
    notifier = StatusNotifier(
        None,
        engine=engine,
        tracker=StatusTracker(clock=lambda: 1000.0),
        transport_factory=lambda route: transport,
    )

    notifier.process("worker_running", {"run_id": "omo_1", "goal": "g", "agent": "hephaestus"})

    assert len(adapter.sent) == 1
    assert adapter.thread_ids == [threading.get_ident()]
    assert engine.recorded == [("omo_1", "msg-1")]
