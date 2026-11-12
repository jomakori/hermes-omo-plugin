"""A drained subscription window must be skipped, not hopped into.

The windows differ by provider layer, which is why the bench follows each
layer's own signal: Claude states a reset (a rolling five-hour window, so the
value moves) and Copilot states only that its monthly quota is gone.

Layer, not brand: the DeepSeek Go plan and the direct DeepSeek key serve the
same model through different lanes, so a Go drain must not bench the direct key
- that is how one dead hop used to delete the next hop of the same walk.
"""

from __future__ import annotations

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from orchestrator.chains import (  # noqa: E402
    BOUNDED_BENCH_SECONDS,
    HOP_REASON_PROVIDER_UNUSABLE,
    HOP_REASON_RATE_LIMIT,
    LAYER_CLAUDE_PROXY,
    LAYER_DEEPSEEK_DIRECT,
    LAYER_OPENCODE_GO,
    FallbackState,
    bench_layer,
    layer_benched,
    layer_of,
    reset_at,
    skip_benched,
)

# The exact text the proxy returns when the five-hour window is drained.
CLAUDE_REFUSAL = (
    "litellm.InternalServerError: InternalServerError: OpenAIException - "
    "You've hit your session limit - resets 10:30am (UTC). "
    "Received Model Group=claude-sonnet-5"
)
# The exact body Copilot returns: a monthly quota with no reset stated.
PROVIDER_REFUSAL = (
    "litellm.APIError: APIError: ProviderQuotaException - "
    '{"error":{"message":"You have exceeded your monthly quota","code":"quota_exceeded"}}'
)

CHAIN = ("litellm/claude-sonnet-5", "litellm/claude-haiku-4-5", "litellm/deepseek-v4-flash-direct")


def test_aliases_map_to_the_layer_that_serves_them():
    assert layer_of("litellm/claude-sonnet-5") == LAYER_CLAUDE_PROXY
    assert layer_of("litellm/claude-haiku-4-5") == LAYER_CLAUDE_PROXY
    assert layer_of("litellm/deepseek-v4-flash-go") == LAYER_OPENCODE_GO
    assert layer_of("litellm/mimo-v2.6-flash") == LAYER_OPENCODE_GO
    assert layer_of("litellm/deepseek-v4-flash-direct") == LAYER_DEEPSEEK_DIRECT


def test_the_reset_is_read_from_the_proxys_own_words():
    now = 1_791_961_200.0  # 2026-10-14 07:00:00 UTC
    # 1791973800 is the reset the proxy actually reported in the live probe.
    assert reset_at(CLAUDE_REFUSAL, now=now) == 1_791_973_800.0


def test_a_reading_further_out_than_the_window_is_treated_as_no_timestamp():
    # A five-hour window is never six hours from its reset, so a reading that
    # far ahead is a stale message: fall back to the bounded bench and let the
    # next attempt re-read the live signal instead of trusting a stale one.
    assert reset_at(CLAUDE_REFUSAL, now=1_791_951_600.0) is None


def test_the_machine_readable_reset_is_preferred_when_present():
    assert reset_at("rate_limit_event resetsAt=1791973800", now=1_791_951_600.0) == 1_791_973_800.0


def test_a_refusal_that_states_no_reset_reports_none():
    assert reset_at(PROVIDER_REFUSAL) is None
    assert reset_at("") is None


def test_a_claude_refusal_benches_both_claude_aliases():
    FallbackState(chain=CHAIN).record_failure(
        "litellm/claude-sonnet-5", reason=HOP_REASON_PROVIDER_UNUSABLE, message=CLAUDE_REFUSAL
    )
    assert layer_benched("litellm/claude-sonnet-5")
    # The sibling shares the key and the window: benching one alias must cover
    # the other, or the walk just hands the same refusal to it next.
    assert layer_benched("litellm/claude-haiku-4-5")
    assert not layer_benched("litellm/deepseek-v4-flash-direct")


def test_a_deepseek_go_drain_leaves_the_direct_key_usable():
    # The regression: this refusal used to bench the brand "deepseek", which
    # deleted `deepseek-v4-flash-direct` from every chain built afterwards. The
    # next walk then had no later hop and stopped after one PROVIDER_UNUSABLE.
    FallbackState(chain=CHAIN).record_failure(
        "litellm/deepseek-v4-flash-go", reason=HOP_REASON_PROVIDER_UNUSABLE, message=PROVIDER_REFUSAL
    )
    assert layer_benched("litellm/deepseek-v4-flash-go")
    assert not layer_benched("litellm/deepseek-v4-flash-direct")
    assert skip_benched(("litellm/deepseek-v4-flash-go", "litellm/deepseek-v4-flash-direct")) == (
        "litellm/deepseek-v4-flash-direct",
    )


def test_the_acceptance_case_a_drained_claude_means_the_walk_starts_at_deepseek():
    """No failed hops: the drained layer is simply absent from the chain."""
    FallbackState(chain=CHAIN).record_failure(
        "litellm/claude-sonnet-5", reason=HOP_REASON_PROVIDER_UNUSABLE, message=CLAUDE_REFUSAL
    )
    chain = skip_benched(CHAIN)  # as the next dispatch builds it
    assert chain[0] == "litellm/deepseek-v4-flash-direct"
    assert FallbackState(chain=chain).hop_history == []


def test_the_bench_is_shared_so_the_next_run_does_not_repay_the_hop():
    FallbackState(chain=CHAIN).record_failure(
        "litellm/claude-sonnet-5", reason=HOP_REASON_PROVIDER_UNUSABLE, message=CLAUDE_REFUSAL
    )
    # A separate walk, as a second dispatch builds it, already knows.
    assert FallbackState(chain=skip_benched(CHAIN)).chain[0] == "litellm/deepseek-v4-flash-direct"


def test_the_bench_lifts_itself_at_the_stated_reset():
    now = 1_791_961_200.0
    until = reset_at(CLAUDE_REFUSAL, now=now)
    assert until is not None
    FallbackState(chain=CHAIN).record_failure(
        "litellm/claude-sonnet-5", reason=HOP_REASON_PROVIDER_UNUSABLE, message=CLAUDE_REFUSAL, now=now
    )
    assert layer_benched("litellm/claude-haiku-4-5", now=now + 60)
    # Expiry is the only thing that lifts it - no scheduler, no cron.
    assert not layer_benched("litellm/claude-haiku-4-5", now=until + 1)
    assert skip_benched(CHAIN, now=until + 1) == CHAIN


def test_a_quota_refusal_without_a_reset_benches_a_bounded_window():
    now = 1_791_951_600.0
    FallbackState(chain=CHAIN).record_failure(
        "deepseek-v4-flash-go", reason=HOP_REASON_PROVIDER_UNUSABLE, message=PROVIDER_REFUSAL, now=now
    )
    # Bounded, not written off for the month the message mentions: the layer
    # comes back on its own and the next attempt that reaches it re-probes it.
    assert layer_benched("deepseek-v4-flash-go", now=now + BOUNDED_BENCH_SECONDS - 1)
    assert not layer_benched("deepseek-v4-flash-go", now=now + BOUNDED_BENCH_SECONDS + 1)


def test_a_bounded_bench_never_shortens_a_stated_reset():
    now = 1_791_951_600.0
    bench_layer(LAYER_CLAUDE_PROXY, now + 3.5 * 3600, now=now)
    bench_layer(LAYER_CLAUDE_PROXY, now + BOUNDED_BENCH_SECONDS, now=now)
    assert layer_benched("claude-sonnet-5", now=now + BOUNDED_BENCH_SECONDS + 1)


def test_a_rate_limit_that_names_its_reset_benches_the_layer():
    # A drained window arriving tagged as a rate limit is still a drained
    # window: the reset it names is what makes it one. That reset is a clock
    # time ("resets 10:30am (UTC)"), and reset_at only reads it when it lands
    # within MAX_BENCH_SECONDS, so the clock is frozen inside that window:
    # read off the wall clock this passed only between 04:30 and 10:30 UTC.
    # A bare rate limit is not a drained seat, so there is no bounded bench
    # to fall back on here - the stated reset is the whole assertion.
    now = 1_791_961_200.0  # 2026-10-14 07:00:00 UTC, 3.5h before the reset
    FallbackState(chain=CHAIN).record_failure(
        "claude-haiku-4-5", reason=HOP_REASON_RATE_LIMIT, message=CLAUDE_REFUSAL, now=now
    )
    assert layer_benched("litellm/claude-sonnet-5", now=now)


def test_a_bare_rate_limit_benches_only_the_one_model():
    # A momentary 429 says nothing about the subscription, so it must not take
    # the whole layer out - the sibling stays available to the next walk.
    FallbackState(chain=CHAIN).record_failure(
        "claude-sonnet-5", reason=HOP_REASON_RATE_LIMIT, message="429 rate limit exceeded"
    )
    assert not layer_benched("claude-haiku-4-5")


def test_an_ordinary_failure_still_only_benches_the_one_model():
    FallbackState(chain=CHAIN).record_failure("claude-sonnet-5", reason="transport", message="connection reset")
    # A transport blip says nothing about the seat, so the sibling stays usable.
    assert not layer_benched("claude-haiku-4-5")


def test_when_everything_is_benched_the_chain_is_returned_untouched():
    for layer in (LAYER_CLAUDE_PROXY, LAYER_DEEPSEEK_DIRECT):
        bench_layer(layer, time.time() + 600)
    # No live hop to move to: one refusal says so more cheaply than a hard stop.
    assert skip_benched(CHAIN) == CHAIN
