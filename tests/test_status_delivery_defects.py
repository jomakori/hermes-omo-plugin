"""Regression tests for D1 (first post late/never) and D2 (frozen screen).

Pure state-machine + pure-asyncio tests: no gateway, no network. These fail
against pre-fix code (see FINDINGS.md Q1/Q2/Q3) and pass against the patch in
``status_tracker.py`` / ``status_notifier.py``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

import pytest

from orchestrator import status_notifier as sn
from orchestrator import status_tracker as st
from orchestrator.status_tracker import StatusTracker

# Bound defensively (getattr + fallback) so this module still *imports* against
# pre-fix code and then fails on the behavioural assertions below. A
# collection-time ImportError would only prove these names are new — it would
# never prove the regression itself is caught.
DEFAULT_HEARTBEAT_INTERVAL = getattr(st, "DEFAULT_HEARTBEAT_INTERVAL", 10.0)
DEFAULT_POST_RETRY_INITIAL = getattr(st, "DEFAULT_POST_RETRY_INITIAL", 2.0)


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _tracker(clock=None, **kwargs):
    clock = clock or FakeClock()
    return StatusTracker(clock=clock, **kwargs), clock


def _created(**overrides):
    payload = {"run_id": "omo_1", "goal": "add live status", "agent": "hephaestus"}
    payload.update(overrides)
    return payload


def _graph(**overrides):
    payload = {
        "run_id": "omo_g",
        "goal": "ship the feature",
        "review": False,
        "workers": [{"agent": "hephaestus", "task_id": "t1", "task": "write the code"}],
    }
    payload.update(overrides)
    return payload


# ── D1: first post never delivered (route unresolved / delivery dropped) ──


def test_tick_retries_the_first_post_with_backoff_when_it_never_landed():
    """A post whose delivery never came back (no `note_message_id` call) must
    be retried by `tick()` on a bounded backoff, not left to wait for the next
    worker event that may never arrive (D1 / fix requirement 1)."""
    tracker, clock = _tracker()
    first = tracker.apply("worker_running", _created())
    assert first is not None and first.kind == "post"
    assert first.message_id is None
    # Delivery of `first` is simulated as dropped: note_message_id is never
    # called, so the run still has no message_id.

    # `phrase_at` starts at -inf (pre-existing behaviour, unrelated to this
    # fix) so the very first tick() after a block reaches "current" always
    # fires once regardless of elapsed time; absorb that single fire here so
    # the assertions below test the *backoff*, not that quirk.
    tracker.tick()

    # Immediately after (no time elapsed): tick() must not spam a duplicate
    # retry before the backoff has elapsed.
    assert tracker.tick() == []

    clock.advance(DEFAULT_POST_RETRY_INITIAL + 0.1)
    retried = tracker.tick()
    assert len(retried) == 1
    assert retried[0].kind == "post"
    assert retried[0].message_id is None

    # Still no delivery: next backoff is longer, and still bounded/retried.
    assert tracker.tick() == []
    clock.advance(DEFAULT_POST_RETRY_INITIAL * 2 + 0.1)
    second_retry = tracker.tick()
    assert len(second_retry) == 1
    assert second_retry[0].kind == "post"


def test_first_post_retry_stops_once_delivery_finally_lands():
    """Once a message id lands, tick() must not duplicate the post (no
    duplicate messages requirement)."""
    tracker, clock = _tracker()
    tracker.apply("worker_running", _created())
    clock.advance(DEFAULT_POST_RETRY_INITIAL + 0.1)
    tracker.tick()
    tracker.note_message_id("omo_1", "555")

    clock.advance(DEFAULT_POST_RETRY_INITIAL * 4)
    # No more "post" retries now that message_id is set; any further rotation
    # is an edit, never a second post.
    actions = tracker.tick()
    assert all(action.kind != "post" for action in actions)


# ── D2: posted but frozen (no worker event ever reaches "current") ──


def test_tick_heartbeats_a_run_that_never_reaches_current():
    """A run whose workers never start (D2: `activity_session` stays empty,
    nothing ever reaches "current") must still be re-rendered periodically
    instead of sitting frozen at its initial pending/hourglass snapshot for
    its whole life (fix requirement 2)."""
    tracker, clock = _tracker()
    # run_created only: blocks exist but none has been _start()-ed, so no
    # block ever reaches "current" — exactly the owner's observed shape
    # (started_at == created_at, no progress, dispatch/run both pending).
    first = tracker.apply("run_created", _graph())
    assert first is not None and first.kind == "post"
    tracker.note_message_id("omo_g", "1")

    # Not due yet: no spurious edits before the heartbeat interval elapses.
    clock.advance(DEFAULT_HEARTBEAT_INTERVAL - 0.1)
    assert tracker.tick() == []

    # Heartbeat interval elapsed: the screen must update even with zero
    # worker events, reporting the stall honestly rather than a static
    # hourglass with no information.
    clock.advance(0.2)
    heartbeat = tracker.tick()
    assert len(heartbeat) == 1
    assert heartbeat[0].kind == "edit"
    assert "waiting" in heartbeat[0].text

    # And it keeps heartbeating on every subsequent interval — never freezes
    # again for the run's whole (still-stalled) life.
    clock.advance(DEFAULT_HEARTBEAT_INTERVAL + 0.1)
    second_heartbeat = tracker.tick()
    assert len(second_heartbeat) == 1
    assert second_heartbeat[0].kind == "edit"


def test_a_run_that_does_reach_current_is_unaffected_by_the_heartbeat():
    """Sanity check: once a worker starts normally, rotation (not the
    heartbeat) drives the edits, and the heartbeat does not double-fire."""
    tracker, clock = _tracker()
    tracker.apply("worker_running", _created())
    tracker.note_message_id("omo_1", "1")
    clock.advance(tracker.phrase_interval + 0.1)
    actions = tracker.tick()
    assert len(actions) == 1
    assert actions[0].kind == "edit"


# ── D2/Q3: timed-out deliveries are cancelled, not left running ──


def test_await_cancels_the_coroutine_and_raises_delivery_timeout_on_expiry(monkeypatch):
    """`_await` must cancel the scheduled coroutine on timeout (fix
    requirement 4) so a stale post/edit cannot land minutes later, and must
    raise the dedicated `_DeliveryTimeout` (Q3) rather than swallow it."""
    monkeypatch.setattr(sn, "DEFAULT_DELIVERY_TIMEOUT", 0.05)

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True, name="fake-gateway-loop")
    thread.start()
    for _ in range(500):
        if loop.is_running():
            break
        time.sleep(0.002)

    cancelled = {"yes": False}

    async def never_returns():
        try:
            await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            cancelled["yes"] = True
            raise

    try:
        with pytest.raises(sn._DeliveryTimeout):
            sn._await(never_returns(), loop)
        # Cancellation is scheduled with call_soon_threadsafe; give the loop
        # one tick to actually process it.
        deadline = time.time() + 2.0
        while not cancelled["yes"] and time.time() < deadline:
            time.sleep(0.01)
        assert cancelled["yes"] is True
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(2.0)


# ── Q3/Q4: delivery failures must be loud (WARNING, deduped), never DEBUG ──


class _StubEngine:
    def run_route(self, run_key):
        return None


def test_warn_once_logs_each_run_reason_pair_exactly_once(caplog):
    """The silent fail-open (DEBUG, invisible at WARNING) is why D1/D2 took
    days to find. Every distinct (run_key, reason) must log at WARNING at
    least once, and never spam on every repeat tick."""
    notifier = sn.StatusNotifier(
        None,
        engine=_StubEngine(),
        tracker=StatusTracker(clock=lambda: 1000.0),
        transport_factory=lambda route: None,
    )
    with caplog.at_level(logging.WARNING, logger="orchestrator.status_notifier"):
        notifier._warn_once("omo_1", "timeout", "delivery timed out for %s", "omo_1")
        notifier._warn_once("omo_1", "timeout", "delivery timed out for %s", "omo_1")
        notifier._warn_once("omo_1", "delivery", "delivery failed for %s", "omo_1")

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2  # one per distinct reason, the repeat is deduped
