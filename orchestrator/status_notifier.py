"""The delivery shell around :class:`StatusTracker`.

``StatusTracker`` decides *what* to send; this module does *when* and *how*:

* one daemon worker owns the tracker and every network call, so a run never
  blocks on Discord and two transitions can never race into two posts — the
  tracker is only ever touched from this single thread (or a test);
* ``handle`` merely enqueues the event, so it is safe to call from any worker
  thread the engine happens to be on;
* the same worker wakes on a timer to rotate the in-progress phrase
  (``tick_seconds``), keeping the message visibly alive;
* message ids are written back onto the run record via the engine, so a gateway
  restart adopts the same message and keeps editing it.

Everything the worker does is best-effort. A status message is a courtesy: a
failure to post or edit must never fail the run it describes.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

from orchestrator.status_tracker import Action, StatusTracker

logger = logging.getLogger(__name__)

# How often the worker wakes to rotate the phrase. Kept below the phrase interval
# so a rotation is never late by more than one tick.
DEFAULT_TICK_SECONDS = 1.0

# How long to wait for one delivery to finish on the gateway loop. A status edit
# is not worth stalling the worker on: a loop that never answers is abandoned.
DEFAULT_DELIVERY_TIMEOUT = 10.0


def _default_transport_factory(route: dict[str, Any] | None) -> Any:
    from orchestrator.gateway_status import GatewayStatusTransport

    return GatewayStatusTransport(route)


class StatusNotifier:
    """Fold progress events into one live Discord message per run."""

    def __init__(
        self,
        ctx: Any,
        *,
        engine: Any = None,
        tracker: StatusTracker | None = None,
        transport_factory: Callable[[dict[str, Any] | None], Any] | None = None,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ctx = ctx
        self._engine = engine
        self._tracker = tracker or StatusTracker(clock=clock)
        self._transport_factory = transport_factory or _default_transport_factory
        self._tick_seconds = max(0.05, float(tick_seconds))
        self._queue: queue.Queue[tuple[str, dict[str, Any]] | None] = queue.Queue()
        self._transports: dict[str, Any] = {}
        self._thread: threading.Thread | None = None
        self._stopped = threading.Event()

    # ── ingestion ─────────────────────────────────────────────────────
    def handle(self, event: str, payload: dict[str, Any] | None = None) -> None:
        """Enqueue one progress event; never blocks, never raises, thread-safe."""
        self._queue.put((str(event), dict(payload or {})))

    def process(self, event: str, payload: dict[str, Any] | None = None) -> None:
        """Apply one event and deliver whatever it produced — the worker's unit."""
        try:
            action = self._tracker.apply(str(event), dict(payload or {}))
        except Exception:  # pragma: no cover - a status bug must not break a run
            logger.debug("OMO status tracker failed on %s", event, exc_info=True)
            return
        if action is not None:
            self._deliver(action)

    # ── lifecycle ─────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopped.clear()
        self._thread = threading.Thread(target=self._loop, name="omo-status", daemon=True)
        self._thread.start()

    def adopt(self, run_key: str, message_id: str | None) -> None:
        """Seed a persisted message id so a restart edits instead of re-posting."""
        if not message_id:
            return
        self._tracker.adopt(run_key, message_id)

    def stop(self, timeout: float = 2.0) -> None:
        # The sentinel drains: the loop processes every queued event before the
        # None, so a final edit enqueued at shutdown is delivered rather than
        # dropped. The stop flag is set afterwards so the drain is not raced.
        self._queue.put(None)
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._stopped.set()

    def _loop(self) -> None:
        while not self._stopped.is_set():
            try:
                item = self._queue.get(timeout=self._tick_seconds)
            except queue.Empty:
                self._flush_rotations()
                continue
            if item is None:
                break
            self.process(*item)

    def _flush_rotations(self) -> None:
        try:
            actions = self._tracker.tick()
        except Exception:  # pragma: no cover
            logger.debug("OMO status tick failed", exc_info=True)
            return
        for action in actions:
            self._deliver(action)

    # ── delivery ──────────────────────────────────────────────────────
    def _transport(self, run_key: str) -> Any | None:
        transport = self._transports.get(run_key)
        if transport is not None:
            return transport
        route = self._route(run_key)
        if route is None or not route.get("chat_id"):
            return None
        transport = self._transport_factory(route)
        if transport is None or not getattr(transport, "usable", True):
            return None
        self._transports[run_key] = transport
        return transport

    def _route(self, run_key: str) -> dict[str, Any] | None:
        getter = getattr(self._engine, "run_route", None)
        if not callable(getter):
            return None
        try:
            route = getter(run_key)
        except Exception:
            return None
        return route if isinstance(route, dict) else None

    def deliver(self, action: Action) -> None:
        """Execute one action now — public so tests drive delivery without threads."""
        self._deliver(action)

    def _deliver(self, action: Action) -> None:
        transport = self._transport(action.run_key)
        if transport is None:
            return
        # The adapter's HTTP session is bound to the gateway loop; schedule every
        # delivery onto it (a fresh loop off-thread is exactly what loses it).
        loop = getattr(transport, "loop", None)
        try:
            if action.kind == "post":
                message_id = _await(transport.post(action.run_key, action.text), loop)
                if message_id:
                    self._tracker.note_message_id(action.run_key, str(message_id))
                    self._record(action.run_key, str(message_id))
                return
            if action.kind == "move":
                # The struct follows the conversation: post the fresh copy below,
                # drop the previous one, then keep the new id — in that order, so
                # a failed post leaves the old struct in place rather than losing
                # the only live message.
                message_id = _await(transport.post(action.run_key, action.text), loop)
                if message_id:
                    self._delete(transport, action.run_key, action.message_id, loop)
                    self._tracker.note_message_id(action.run_key, str(message_id))
                    self._record(action.run_key, str(message_id))
                return
            ok = _await(transport.edit(action.run_key, str(action.message_id), action.text), loop)
            if not ok and getattr(transport, "last_gone", False):
                # The message is gone (deleted, or a different process owns it):
                # forget the id so the next transition posts a fresh one.
                self._tracker.note_message_id(action.run_key, None)
                self._record(action.run_key, None)
        except Exception:  # pragma: no cover - best effort by contract
            logger.debug("OMO status delivery failed for %s", action.run_key, exc_info=True)

    def _delete(self, transport: Any, run_key: str, message_id: str | None, loop: Any) -> None:
        """Delete the previous struct after a move; best-effort, never fatal.

        A platform without a deletion API, a message already gone, or a network
        blip all leave the previous copy behind — the new id is tracked either
        way, so exactly the tracked struct stays live.
        """
        if not message_id:
            return
        delete = getattr(transport, "delete", None)
        if not callable(delete):
            return
        try:
            _await(delete(run_key, str(message_id)), loop)
        except Exception:  # pragma: no cover - best effort by contract
            logger.debug("OMO status delete declined for %s (%s)", run_key, message_id, exc_info=True)

    def _record(self, run_key: str, message_id: str | None) -> None:
        setter = getattr(self._engine, "note_status_message", None)
        if not callable(setter):
            return
        try:
            setter(run_key, message_id)
        except Exception:
            logger.debug("OMO status id persist failed for %s", run_key, exc_info=True)


def _await(value: Any, loop: Any = None) -> Any:
    """Run one delivery coroutine to completion — on the gateway loop when there is one.

    The adapter's HTTP session is bound to the gateway loop, so from the status
    worker thread the coroutine is *scheduled* onto that loop with
    ``run_coroutine_threadsafe`` and its result awaited — the same cross-thread hop
    the host's own dispatch uses (``gateway/run_turn_runner.py:774``). Running it
    on a fresh loop (``asyncio.run``) is what silently loses the delivery: the
    off-loop aiohttp session raises and the notifier swallows it.

    A plain value (a sync fake transport) passes straight through, and with no live
    gateway loop (CLI, unit tests) the coroutine runs locally.
    """
    if not inspect.isawaitable(value):
        return value
    if loop is not None and loop.is_running():
        try:
            future = asyncio.run_coroutine_threadsafe(value, loop)
        except Exception:
            _close(value)
            raise
        return future.result(timeout=DEFAULT_DELIVERY_TIMEOUT)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(value)
    # A running loop in this thread but no gateway loop to hop to: nowhere safe to
    # run it. Close the coroutine rather than leak a never-awaited one.
    _close(value)
    raise RuntimeError("status delivery has no gateway loop to schedule on")


def _close(value: Any) -> None:
    close = getattr(value, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


__all__ = ["DEFAULT_DELIVERY_TIMEOUT", "DEFAULT_TICK_SECONDS", "StatusNotifier"]
