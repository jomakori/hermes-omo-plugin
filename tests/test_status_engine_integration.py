"""The dispatched-run path, end to end: engine → notifier → transport → adapter.

Why this file exists
--------------------
``tests/test_status_mock_discord.py`` stands up the real tracker, notifier and
transport over a mock Discord adapter — but it drives them by calling
``notifier.process(event, payload)`` itself. That bypasses the half the live
symptom lives in: ``OmoEngine.set_status_notifier`` and every
``OmoEngine._progress`` emit site. A dispatch that never reaches the notifier
looks identical to a healthy one in that harness.

So this file starts one rung further out: a real :class:`OmoEngine` with a fake
lifecycle, a real :class:`StatusNotifier` running its own worker thread, a real
:class:`GatewayStatusTransport`, and the mock adapter from the existing harness
(imported, not re-written). The only stand-ins are the subagent lifecycle, the
platform boundary, and the session-route bridge (``gateway.session_context`` is
not importable in tests — the route it would return is monkeypatched in, which
is exactly what the live records show: platform/chat/thread all present).

``test_dispatched_run_posts_a_status_message_and_edits_it`` is the one that
would have caught a broken attach/emit path: nothing but a real
``engine.dispatch`` drives it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_engine import FakeLifecycle, request_factory  # noqa: E402
from test_status_mock_discord import GOAL, PARENT_CHAT, THREAD, MockDiscordAdapter, MockRunner  # noqa: E402

from orchestrator import session as session_module  # noqa: E402
from orchestrator.chains import ChainResolver  # noqa: E402
from orchestrator.engine import OmoEngine  # noqa: E402
from orchestrator.gateway_status import GatewayStatusTransport  # noqa: E402
from orchestrator.status_notifier import StatusNotifier  # noqa: E402
from orchestrator.status_tracker import StatusTracker  # noqa: E402

ROUTE = {"platform": "discord", "chat_id": PARENT_CHAT, "thread_id": THREAD}
AGENT = "hephaestus"
AGENT_ROLE = "hephaestus · Deep Agent"


class EngineCtx:
    """The host surface the engine actually reads: config and the event bus."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = config or {}
        self.events: list[tuple[str, Any]] = []

    def get_config(self, key: str, default: Any = None) -> Any:
        return self._config.get(key, default)

    def emit(self, event: str, payload: Any = None) -> None:
        self.events.append((event, payload))


class EngineHarness:
    """A real engine wired to the real notifier stack over the mock adapter."""

    def __init__(self, *, runner: Any = None, adapter: MockDiscordAdapter | None = None) -> None:
        self.adapter = adapter if adapter is not None else MockDiscordAdapter()
        self.runner = runner if runner is not None else MockRunner(self.adapter)
        self.lifecycle = FakeLifecycle()
        self.ctx = EngineCtx()
        self.engine = OmoEngine(self.ctx, lifecycle=self.lifecycle, request_factory=request_factory)
        self.engine.chains = ChainResolver({})
        self.notifier = StatusNotifier(
            self.ctx,
            engine=self.engine,
            tracker=StatusTracker(),
            transport_factory=self._transport,
            tick_seconds=0.05,
        )
        # The attach the existing harness never exercises.
        self.engine.set_status_notifier(self.notifier)
        self.transports: list[GatewayStatusTransport] = []

    def _transport(self, route: dict[str, Any] | None) -> GatewayStatusTransport:
        # The route the notifier resolved off the engine's own run record flows
        # into a REAL transport; only the runner/platform-key/profile seams are
        # injected (gateway.config is not importable in tests).
        transport = GatewayStatusTransport(
            route,
            runner_resolver=lambda: self.runner,
            platform_key=lambda value: value,
            profile_resolver=lambda: "default",
        )
        self.transports.append(transport)
        return transport

    def __enter__(self) -> EngineHarness:
        self.notifier.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.notifier.stop()

    def dispatch(self, *, goal: str = GOAL, agent: str = AGENT) -> str:
        return str(self.engine.dispatch(goal=goal, target=agent)["run_id"])


def _wait(predicate: Any, timeout: float = 5.0) -> bool:
    """Wait for the status worker thread to catch up, or give up."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ── the full dispatched-run path ──────────────────────────────────────


def test_dispatched_run_posts_a_status_message_and_edits_it(monkeypatch):
    monkeypatch.setattr(session_module, "current_route", lambda: dict(ROUTE))
    with EngineHarness() as h:
        run_id = h.dispatch()
        assert _wait(lambda: h.adapter.ops_of("edit")), f"no status edit arrived; ops={h.adapter.ops}"

    posts = h.adapter.ops_of("post")
    assert len(posts) == 1, f"a dispatched run must post exactly one status message; ops={h.adapter.ops}"
    post = posts[0]
    # The post lands in the thread the route names, not the parent chat.
    assert post["channel"] == THREAD
    text = str(post["content"])
    assert text.splitlines()[0] == f"🏗️ omo · {run_id} — {GOAL}"
    assert AGENT_ROLE in text, f"the worker block is missing: {text!r}"

    # The later progress event (the terminal one) edits that same message.
    edits = h.adapter.ops_of("edit")
    assert all(edit["message_id"] == post["message_id"] for edit in edits)
    assert "- run ✅" in h.adapter.text_of(THREAD, str(post["message_id"]))
    # The id was persisted onto the run record, so a restart adopts it.
    assert h.engine.runs[run_id].status_message_id == str(post["message_id"])


def test_a_read_only_call_posts_nothing(monkeypatch):
    monkeypatch.setattr(session_module, "current_route", lambda: dict(ROUTE))
    with EngineHarness() as h:
        assert h.engine.status()["runs"] == []
        time.sleep(0.2)
    assert h.adapter.ops == [], f"a run-free read must not touch the platform; ops={h.adapter.ops}"


def test_a_run_with_no_resolvable_route_silently_no_ops(monkeypatch):
    # No session bridge answer: the record carries no chat, so there is nowhere
    # to post — and the dispatch must still succeed.
    monkeypatch.setattr(session_module, "current_route", lambda: None)
    with EngineHarness() as h:
        result = h.engine.dispatch(goal=GOAL, target=AGENT)
        time.sleep(0.2)
    assert result["status"] == "succeeded"
    assert h.engine.run_route(result["run_id"]) is None
    assert h.adapter.ops == []


def test_a_second_run_gets_its_own_message(monkeypatch):
    monkeypatch.setattr(session_module, "current_route", lambda: dict(ROUTE))
    with EngineHarness() as h:
        first = h.dispatch()
        assert _wait(lambda: h.adapter.ops_of("post")), f"first run never posted; ops={h.adapter.ops}"
        second = h.dispatch(goal="second goal")
        assert _wait(lambda: len(h.adapter.ops_of("post")) == 2), f"second run never posted; ops={h.adapter.ops}"

    posts = h.adapter.ops_of("post")
    assert len({post["message_id"] for post in posts}) == 2
    headings = {str(post["content"]).splitlines()[0] for post in posts}
    assert f"🏗️ omo · {first} — {GOAL}" in headings
    assert f"🏗️ omo · {second} — second goal" in headings


# ── host-side adapter resolution: every way the transport can lose ────


class NoAdapterRunner:
    """A live gateway whose fail-closed lookup refuses the platform."""

    _gateway_loop = None

    def _authorization_adapter(self, platform: Any, profile: Any = None) -> Any:
        return None


class DisconnectedRunner:
    """A live gateway whose adapter is not connected (startup, or a reconnect)."""

    def __init__(self) -> None:
        self.adapter = MockDiscordAdapter(is_connected=False)
        self._gateway_loop = None

    def _authorization_adapter(self, platform: Any, profile: Any = None) -> Any:
        return self.adapter


class NoLoopRunner:
    """A runner that never grew a ``_gateway_loop`` attribute."""

    def __init__(self, adapter: MockDiscordAdapter) -> None:
        self.adapter = adapter

    def _authorization_adapter(self, platform: Any, profile: Any = None) -> Any:
        return self.adapter


def _transport(runner: Any, *, platform_key: Any = None) -> GatewayStatusTransport:
    return GatewayStatusTransport(
        dict(ROUTE),
        runner_resolver=lambda: runner,
        platform_key=platform_key or (lambda value: value),
        profile_resolver=lambda: "default",
    )


def test_no_live_gateway_runner_no_ops_and_warns(caplog):
    transport = _transport(None)
    with caplog.at_level(logging.WARNING, logger="orchestrator.gateway_status"):
        assert asyncio.run(transport.post("omo_1", "text")) is None
        assert asyncio.run(transport.edit("omo_1", "m1", "text")) is False
    assert any("no live gateway runner" in record.getMessage() for record in caplog.records)
    assert "omo_1" in caplog.text and "discord" in caplog.text


def test_refused_adapter_lookup_no_ops_and_warns(caplog):
    transport = _transport(NoAdapterRunner())
    with caplog.at_level(logging.WARNING, logger="orchestrator.gateway_status"):
        assert asyncio.run(transport.post("omo_2", "text")) is None
    assert "no adapter" in caplog.text
    assert "omo_2" in caplog.text


def test_disconnected_adapter_no_ops_and_warns(caplog):
    runner = DisconnectedRunner()
    transport = _transport(runner)
    with caplog.at_level(logging.WARNING, logger="orchestrator.gateway_status"):
        assert asyncio.run(transport.post("omo_3", "text")) is None
    assert runner.adapter.ops == []
    assert "not connected" in caplog.text
    assert "omo_3" in caplog.text


def test_unusable_platform_key_no_ops_and_warns(caplog):
    def boom(_value: Any) -> Any:
        raise ValueError("'discord' is not a valid Platform")

    transport = _transport(NoLoopRunner(MockDiscordAdapter()), platform_key=boom)
    with caplog.at_level(logging.WARNING, logger="orchestrator.gateway_status"):
        assert asyncio.run(transport.post("omo_4", "text")) is None
    assert "platform" in caplog.text
    assert "omo_4" in caplog.text


def test_a_runner_without_a_gateway_loop_still_delivers(caplog):
    adapter = MockDiscordAdapter()
    transport = _transport(NoLoopRunner(adapter))
    assert transport.loop is None
    with caplog.at_level(logging.WARNING, logger="orchestrator.gateway_status"):
        message_id = asyncio.run(transport.post("omo_5", "text"))
    assert message_id and adapter.is_alive(THREAD, message_id)
    assert caplog.text == ""


def test_each_resolution_failure_warns_once_per_transport(caplog):
    transport = _transport(NoAdapterRunner())
    with caplog.at_level(logging.WARNING, logger="orchestrator.gateway_status"):
        for _ in range(5):
            assert asyncio.run(transport.post("omo_6", "text")) is None
    # Loud enough to diagnose an outage, quiet enough not to be the outage.
    assert len([record for record in caplog.records if "no adapter" in record.getMessage()]) == 1
