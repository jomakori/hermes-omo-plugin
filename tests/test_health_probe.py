"""Behavioural tests for the provider health probe.

Every test here drives real code through a fake transport and asserts on the
real `Verdict`/cache it produces — no grepping source for strings.
"""

from __future__ import annotations

import dataclasses

import pytest

from orchestrator.health_probe import (
    REASON_ALIVE,
    REASON_QUOTA,
    REASON_RATE_LIMIT,
    REASON_TRANSPORT,
    HealthProbeCache,
    ProbeResponse,
    Verdict,
    alive_candidates,
    classify,
    probe_once,
)


class ScriptedTransport:
    """Returns the next scripted `ProbeResponse`/exception per call, records calls."""

    def __init__(self, script):
        self._script = list(script)
        self.calls: list[str] = []

    def __call__(self, model: str) -> ProbeResponse:
        self.calls.append(model)
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ── classify() / probe_once(): the three verified signals ──────────────────


def test_a_200_classifies_alive():
    alive, reason = classify(ProbeResponse(status=200, message="ok"))
    assert alive is True
    assert reason == REASON_ALIVE


def test_a_402_quota_exceeded_classifies_dead_with_quota_reason():
    alive, reason = classify(ProbeResponse(status=402, message='{"error":"quota_exceeded"}'))
    assert alive is False
    assert reason == REASON_QUOTA


def test_a_429_classifies_dead_with_rate_limit_reason():
    alive, reason = classify(ProbeResponse(status=429, message='{"error":"five_hour"}'))
    assert alive is False
    assert reason == REASON_RATE_LIMIT


def test_a_402_and_a_429_are_told_apart():
    # The two dead signals must not collapse into one bucket: a drained quota
    # (402) is not the same remediation as a throttle (429).
    quota = classify(ProbeResponse(status=402))
    rate_limit = classify(ProbeResponse(status=429))
    assert quota != rate_limit
    assert quota[1] == REASON_QUOTA
    assert rate_limit[1] == REASON_RATE_LIMIT


def test_an_unrecognized_status_classifies_as_transport():
    alive, reason = classify(ProbeResponse(status=500, message="upstream error"))
    assert alive is False
    assert reason == REASON_TRANSPORT


def test_no_status_at_all_classifies_as_transport():
    alive, reason = classify(ProbeResponse(status=None, message=""))
    assert alive is False
    assert reason == REASON_TRANSPORT


# ── probe_once(): the transport boundary, including raised exceptions ──────


def test_probe_once_reports_alive_from_a_200_response():
    transport = ScriptedTransport([ProbeResponse(status=200, message="pong")])
    verdict = probe_once("copilot-luna", transport)
    assert verdict.alive is True
    assert verdict.reason == REASON_ALIVE
    assert verdict.model == "copilot-luna"
    assert verdict.status == 200


def test_probe_once_reports_dead_quota_from_a_402_response():
    transport = ScriptedTransport([ProbeResponse(status=402, message="quota_exceeded")])
    verdict = probe_once("copilot-luna", transport)
    assert verdict.alive is False
    assert verdict.reason == REASON_QUOTA
    assert verdict.status == 402


def test_probe_once_reports_dead_rate_limit_from_a_429_response():
    transport = ScriptedTransport([ProbeResponse(status=429, message="five_hour")])
    verdict = probe_once("claude-sonnet-5", transport)
    assert verdict.alive is False
    assert verdict.reason == REASON_RATE_LIMIT
    assert verdict.status == 429


def test_a_raised_transport_exception_classifies_as_transport_not_dead_quota():
    # A connection error must not be mistaken for a provider's own quota verdict:
    # nobody was reached, so nothing was learned about billing or rate limits.
    transport = ScriptedTransport([ConnectionError("connection refused")])
    verdict = probe_once("copilot-luna", transport)
    assert verdict.alive is False
    assert verdict.reason == REASON_TRANSPORT
    assert "connection refused" in verdict.message
    assert verdict.status is None


# ── HealthProbeCache: caches the verdict, short TTL, re-probes once stale ──


def test_the_cache_probes_once_then_serves_the_cached_verdict():
    transport = ScriptedTransport([ProbeResponse(status=200)])
    clock = FakeClock()
    cache = HealthProbeCache(transport=transport, ttl_seconds=60.0, _clock=clock)

    first = cache.verdict("copilot-luna")
    clock.advance(5.0)
    second = cache.verdict("copilot-luna")

    assert first.alive is True
    assert second is first  # served from cache, not re-probed
    assert transport.calls == ["copilot-luna"]  # only one real probe happened


def test_the_cache_reprobes_once_the_ttl_expires():
    transport = ScriptedTransport(
        [
            ProbeResponse(status=200),
            ProbeResponse(status=402, message="quota_exceeded"),
        ]
    )
    clock = FakeClock()
    cache = HealthProbeCache(transport=transport, ttl_seconds=10.0, _clock=clock)

    first = cache.verdict("copilot-luna")
    clock.advance(11.0)  # past the TTL
    second = cache.verdict("copilot-luna")

    assert first.reason == REASON_ALIVE
    assert second.reason == REASON_QUOTA
    assert transport.calls == ["copilot-luna", "copilot-luna"]


def test_force_bypasses_a_fresh_cache_entry():
    transport = ScriptedTransport([ProbeResponse(status=200), ProbeResponse(status=429)])
    clock = FakeClock()
    cache = HealthProbeCache(transport=transport, ttl_seconds=60.0, _clock=clock)

    cache.verdict("claude-sonnet-5")
    forced = cache.verdict("claude-sonnet-5", force=True)

    assert forced.reason == REASON_RATE_LIMIT
    assert transport.calls == ["claude-sonnet-5", "claude-sonnet-5"]


def test_cached_verdict_never_probes_and_is_none_before_any_probe():
    transport = ScriptedTransport([ProbeResponse(status=200)])
    clock = FakeClock()
    cache = HealthProbeCache(transport=transport, ttl_seconds=60.0, _clock=clock)

    assert cache.cached_verdict("copilot-luna") is None
    assert transport.calls == []  # the passive read never triggered a probe

    cache.verdict("copilot-luna")
    assert cache.cached_verdict("copilot-luna").alive is True
    assert transport.calls == ["copilot-luna"]  # still just the one active probe


def test_cached_verdict_goes_stale_after_the_ttl_without_reprobing():
    transport = ScriptedTransport([ProbeResponse(status=200)])
    clock = FakeClock()
    cache = HealthProbeCache(transport=transport, ttl_seconds=10.0, _clock=clock)

    cache.verdict("copilot-luna")
    clock.advance(11.0)

    assert cache.cached_verdict("copilot-luna") is None  # stale, not served
    assert transport.calls == ["copilot-luna"]  # the passive read still never probed


def test_invalidate_drops_one_model_or_everything():
    transport = ScriptedTransport([ProbeResponse(status=200), ProbeResponse(status=200), ProbeResponse(status=200)])
    clock = FakeClock()
    cache = HealthProbeCache(transport=transport, ttl_seconds=60.0, _clock=clock)
    cache.verdict("a")
    cache.verdict("b")

    cache.invalidate("a")
    assert cache.cached_verdict("a") is None
    assert cache.cached_verdict("b") is not None

    cache.invalidate()
    assert cache.cached_verdict("b") is None


def test_is_alive_is_a_thin_read_of_verdict_alive():
    transport = ScriptedTransport([ProbeResponse(status=402)])
    cache = HealthProbeCache(transport=transport, ttl_seconds=60.0)
    assert cache.is_alive("copilot-luna") is False


# ── alive_candidates(): what chain construction consults ───────────────────


def test_alive_candidates_drops_a_model_probed_dead():
    transport = ScriptedTransport([ProbeResponse(status=402, message="quota_exceeded")])
    cache = HealthProbeCache(transport=transport, ttl_seconds=60.0)
    cache.verdict("copilot-luna")  # force the dead verdict into the cache

    chain = ("copilot-luna", "claude-sonnet-5", "deepseek-v4-flash-direct")
    assert alive_candidates(chain, cache) == ("claude-sonnet-5", "deepseek-v4-flash-direct")


def test_alive_candidates_keeps_an_unprobed_model():
    # Nothing has probed "claude-sonnet-5" yet: absence of a verdict is not
    # evidence of dead, so it must stay in the candidate chain.
    cache = HealthProbeCache(transport=ScriptedTransport([]), ttl_seconds=60.0)
    chain = ("claude-sonnet-5", "deepseek-v4-flash-direct")
    assert alive_candidates(chain, cache) == chain


def test_alive_candidates_keeps_a_model_probed_alive():
    transport = ScriptedTransport([ProbeResponse(status=200)])
    cache = HealthProbeCache(transport=transport, ttl_seconds=60.0)
    cache.verdict("copilot-luna")

    chain = ("copilot-luna", "claude-sonnet-5")
    assert alive_candidates(chain, cache) == chain


def test_alive_candidates_reinstates_a_model_once_its_dead_verdict_goes_stale():
    transport = ScriptedTransport([ProbeResponse(status=402)])
    clock = FakeClock()
    cache = HealthProbeCache(transport=transport, ttl_seconds=10.0, _clock=clock)
    cache.verdict("copilot-luna")

    chain = ("copilot-luna", "claude-sonnet-5")
    assert alive_candidates(chain, cache) == ("claude-sonnet-5",)

    clock.advance(11.0)  # the dead verdict is now stale, so it reads as unprobed
    assert alive_candidates(chain, cache) == chain


# ── Verdict.is_stale: the TTL boundary itself ───────────────────────────────


def test_is_stale_is_false_just_under_the_ttl_and_true_at_it():
    verdict = Verdict(model="m", alive=True, reason=REASON_ALIVE, checked_at=0.0)
    assert verdict.is_stale(10.0, now=9.999) is False
    assert verdict.is_stale(10.0, now=10.0) is True


def test_verdict_is_frozen():
    verdict = Verdict(model="m", alive=True, reason=REASON_ALIVE)
    with pytest.raises(dataclasses.FrozenInstanceError):
        verdict.alive = False
