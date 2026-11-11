"""A fallback hop must leave the layer the previous hop was served from.

Two aliases on one key, quota or proxy are one layer: walking from
`claude-sonnet-5` to `claude-haiku-4-5` is not a fallback, it is the same
refusal one hop later. The chain resolver therefore collapses those hops and
says so in the trail, and a hop that loses its layer stays a hop - a drained
Go plan must not delete the direct key from the rest of the walk.

The failures here come from real turn results: the harness closes a turn it
could not serve as *succeeded*, with the outage only in the summary. That is
the shape that ended live runs after one hop, so it is the shape under test.
"""

from __future__ import annotations

import dataclasses
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from test_engine import FakeCtx, FakeHandle, request_factory  # noqa: E402

from orchestrator.chains import (  # noqa: E402
    HOP_REASON_PROVIDER_UNUSABLE,
    HOP_REASON_SAME_LAYER,
    HOP_REASON_SUCCESS,
    LAYER_CLAUDE_PROXY,
    LAYER_DEEPSEEK_DIRECT,
    LAYER_OPENCODE_GO,
    ChainResolver,
    clear_bench,
    layer_of,
)
from orchestrator.engine import OmoEngine  # noqa: E402

# The exact text the harness returned in the live runs, verbatim.
NO_REPLY = (
    "⚠️ No reply: every provider in the fallback chain kept failing over, so the "
    "turn stopped instead of retrying forever. Send `continue` or switch provider."
)


@dataclasses.dataclass
class _TerminalState:
    name: str


@dataclasses.dataclass
class TurnResult:
    """The real result shape: served turns and unserved ones both read SUCCEEDED."""

    summary: str
    terminal_state: _TerminalState = dataclasses.field(default_factory=lambda: _TerminalState("SUCCEEDED"))


class ScriptedLifecycle:
    """A lifecycle that answers per model: `noreply` for an unserved turn."""

    def __init__(self, script: dict[str, str] | None = None):
        self.script = script or {}
        self.launched: list[str] = []

    def launch(self, request):
        self.launched.append(request.model)
        return FakeHandle(request.model)

    def wait(self, handle, *, timeout_seconds=None):
        return None

    def result(self, handle):
        if self.script.get(handle.model) == "noreply":
            return TurnResult(summary=NO_REPLY)
        return TurnResult(summary="did the work")

    def status_of(self, handle):
        # The handle-side status reads succeeded even when the turn was not
        # served - exactly why the walk must read the result, not this.
        return True

    def cancel(self, handle, *, reason=""):
        return None


def make_engine(script: dict[str, str] | None, config: dict | None = None):
    lifecycle = ScriptedLifecycle(script)
    ctx = FakeCtx(lifecycle, config or {})
    engine = OmoEngine(ctx, lifecycle=lifecycle, request_factory=request_factory)
    engine.chains = ChainResolver(config or {})
    return lifecycle, engine


@pytest.fixture(autouse=True)
def _no_bench_from_another_test():
    # Benches are module-level on purpose (they outlive a walk), so they are
    # shared by everything in the process - including later tests.
    clear_bench()
    yield
    clear_bench()


def _reasons(trail):
    return [hop["reason"] for hop in trail]


def _models(trail):
    return [hop["model"] for hop in trail]


# a. the live regression: a provider-exhausted primary must advance, not stop.


def test_a_provider_exhausted_hop_advances_to_the_next_layer():
    chain = ["claude-sonnet-5", "deepseek-v4-flash-go", "deepseek-v4-flash-direct"]
    lifecycle, engine = make_engine({"claude-sonnet-5": "noreply"}, {"chains": {"hephaestus": chain}})

    out = engine.dispatch(goal="build the thing", target="hephaestus")

    # Hop 2 was healthy and is now actually attempted.
    assert lifecycle.launched == ["claude-sonnet-5", "deepseek-v4-flash-go"]
    assert out["status"] == "succeeded"
    # The trail says where the walk went, not just where it ended.
    assert _models(out["hop_history"]) == ["claude-sonnet-5", "deepseek-v4-flash-go"]
    assert _reasons(out["hop_history"]) == [HOP_REASON_PROVIDER_UNUSABLE, HOP_REASON_SUCCESS]
    assert out["model"] == "deepseek-v4-flash-go"
    assert out["result"].summary == "did the work"


def test_a_drained_go_lane_does_not_delete_the_direct_key_from_the_walk():
    # The cause of the live single-hop stop: the Go refusal benched the brand
    # "deepseek", so `deepseek-v4-flash-direct` was gone before the walk began.
    chain = ["claude-sonnet-5", "deepseek-v4-flash-go", "deepseek-v4-flash-direct"]
    lifecycle, engine = make_engine(
        {"claude-sonnet-5": "noreply", "deepseek-v4-flash-go": "noreply"},
        {"chains": {"hephaestus": chain}},
    )

    out = engine.dispatch(goal="build the thing", target="hephaestus")

    assert lifecycle.launched == chain
    assert out["status"] == "succeeded"
    assert out["model"] == "deepseek-v4-flash-direct"
    assert _reasons(out["hop_history"]) == [
        HOP_REASON_PROVIDER_UNUSABLE,
        HOP_REASON_PROVIDER_UNUSABLE,
        HOP_REASON_SUCCESS,
    ]


# b. every hop failing is the only way the walk ends FAILED on exhaustion.


def test_every_hop_failing_ends_failed_with_the_same_reason():
    chain = ["claude-sonnet-5", "deepseek-v4-flash-go", "deepseek-v4-flash-direct"]
    lifecycle, engine = make_engine({model: "noreply" for model in chain}, {"chains": {"hephaestus": chain}})

    out = engine.dispatch(goal="build the thing", target="hephaestus")

    # One attempt per hop, no more: the budget bounds retries, not the chain.
    assert lifecycle.launched == chain
    assert out["status"] == "failed"
    # Issue #53: a turn the harness could not serve closes SUCCEEDED carrying the
    # outage in its summary. It must never surface as a success here.
    assert out["status"] != "succeeded"
    assert out["model"] == "deepseek-v4-flash-direct"
    assert _models(out["hop_history"]) == chain
    assert _reasons(out["hop_history"]) == [HOP_REASON_PROVIDER_UNUSABLE] * len(chain)
    assert "No reply" in out["error"]


def test_the_fallback_budget_still_bounds_the_walk():
    chain = ["claude-sonnet-5", "deepseek-v4-flash-go", "deepseek-v4-flash-direct"]
    lifecycle, engine = make_engine(
        {model: "noreply" for model in chain},
        # `max_fallback_attempts` counts the hops a walk may add past the
        # primary, so 1 means "one fallback hop, then stop".
        {"chains": {"hephaestus": chain}, "max_fallback_attempts": 1},
    )

    out = engine.dispatch(goal="build the thing", target="hephaestus")

    # The budget, not the chain, is what ends this walk: the third hop is never
    # launched, and the run still fails on the hop it stopped at.
    assert lifecycle.launched == chain[:2]
    assert out["status"] == "failed"
    assert out["model"] == chain[1]


# c. same-layer hops are collapsed, and the trail explains the short chain.


def test_same_layer_hops_collapse_and_are_named_in_the_trail():
    chain = ["claude-sonnet-5", "claude-haiku-4-5", "deepseek-v4-flash-direct"]
    resolver = ChainResolver({"chains": {"hephaestus": chain}})
    state = resolver.state_for("hephaestus", ())

    assert state.chain == ("claude-sonnet-5", "deepseek-v4-flash-direct")
    assert state.skipped == (("claude-haiku-4-5", "claude-sonnet-5"),)
    # Collapsed, not attempted: the budget only counts hops that were asked.
    assert state.attempt_count == 0

    lifecycle, engine = make_engine({"claude-sonnet-5": "noreply"}, {"chains": {"hephaestus": chain}})
    out = engine.dispatch(goal="build the thing", target="hephaestus")

    assert lifecycle.launched == ["claude-sonnet-5", "deepseek-v4-flash-direct"]
    assert out["status"] == "succeeded"
    # Two hops plus one skip, in the order the chain was written.
    assert _models(out["hop_history"]) == chain
    assert _reasons(out["hop_history"]) == [
        HOP_REASON_PROVIDER_UNUSABLE,
        HOP_REASON_SAME_LAYER,
        HOP_REASON_SUCCESS,
    ]


def test_a_collapsed_hop_never_spends_the_fallback_budget():
    # Three layers live behind four written hops, and the walk needs two
    # fallbacks (Go, then Direct). A budget of two fits only if the collapsed
    # hop never counted against it.
    chain = ["claude-sonnet-5", "claude-haiku-4-5", "deepseek-v4-flash-go", "deepseek-v4-flash-direct"]
    lifecycle, engine = make_engine(
        {"claude-sonnet-5": "noreply", "deepseek-v4-flash-go": "noreply"},
        {"chains": {"hephaestus": chain}, "max_fallback_attempts": 2},
    )

    out = engine.dispatch(goal="build the thing", target="hephaestus")

    assert lifecycle.launched == ["claude-sonnet-5", "deepseek-v4-flash-go", "deepseek-v4-flash-direct"]
    assert out["status"] == "succeeded"
    assert _reasons(out["hop_history"]) == [
        HOP_REASON_PROVIDER_UNUSABLE,
        HOP_REASON_SAME_LAYER,
        HOP_REASON_PROVIDER_UNUSABLE,
        HOP_REASON_SUCCESS,
    ]


def test_the_first_hop_is_always_kept_even_when_the_chain_repeats_its_layer():
    # Collapse must never empty the chain: a single-layer chain is attempted.
    resolver = ChainResolver({"chains": {"hephaestus": ["claude-sonnet-5", "claude-haiku-4-5"]}})
    state = resolver.state_for("hephaestus", ())

    assert state.chain == ("claude-sonnet-5",)
    assert state.skipped == (("claude-haiku-4-5", "claude-sonnet-5"),)


def test_the_configured_order_is_preserved_around_a_collapsed_hop():
    chain = ["deepseek-v4-flash-direct", "claude-sonnet-5", "claude-haiku-4-5", "deepseek-v4-flash-go"]
    resolver = ChainResolver({"chains": {"hephaestus": chain}})
    state = resolver.state_for("hephaestus", ())

    # `deepseek-*` twice is not a collapsed pair: Go and direct are two lanes.
    assert state.chain == ("deepseek-v4-flash-direct", "claude-sonnet-5", "deepseek-v4-flash-go")
    assert state.skipped == (("claude-haiku-4-5", "claude-sonnet-5"),)


# d. the table is closed: an unknown alias is its own layer.


def test_the_layers_are_the_ones_the_stack_runs_on():
    assert layer_of("litellm/claude-sonnet-5") == LAYER_CLAUDE_PROXY
    assert layer_of("litellm/claude-haiku-4-5") == LAYER_CLAUDE_PROXY
    assert layer_of("litellm/deepseek-v4-flash-go") == LAYER_OPENCODE_GO
    assert layer_of("litellm/mimo-v2.6-flash") == LAYER_OPENCODE_GO
    assert layer_of("litellm/deepseek-v4-flash-direct") == LAYER_DEEPSEEK_DIRECT


def test_an_unknown_alias_keeps_its_own_layer():
    assert layer_of("litellm/griffin-xl") == "griffin-xl"
    # Not folded into kilo by a prefix it does not share, and not merged with a
    # neighbour it may not share a quota with.
    assert layer_of("litellm/gpt-oss-local") != layer_of("litellm/griffin-xl")
    resolver = ChainResolver({"chains": {"hephaestus": ["griffin-xl", "deepseek-v4-flash-direct"]}})
    assert resolver.state_for("hephaestus", ()).chain == ("griffin-xl", "deepseek-v4-flash-direct")


def test_two_different_unknown_aliases_are_not_collapsed():
    resolver = ChainResolver({"chains": {"hephaestus": ["ghost-a", "ghost-b"]}})
    state = resolver.state_for("hephaestus", ())

    assert state.chain == ("ghost-a", "ghost-b")
    assert state.skipped == ()


def test_an_unknown_alias_is_attempted_like_any_other_hop():
    chain = ["griffin-xl", "claude-sonnet-5"]
    lifecycle, engine = make_engine({"griffin-xl": "noreply"}, {"chains": {"hephaestus": chain}})

    out = engine.dispatch(goal="build the thing", target="hephaestus")

    assert lifecycle.launched == chain
    assert out["status"] == "succeeded"
    assert _reasons(out["hop_history"]) == [HOP_REASON_PROVIDER_UNUSABLE, HOP_REASON_SUCCESS]
