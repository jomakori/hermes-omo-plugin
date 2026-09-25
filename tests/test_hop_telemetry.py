"""Per-hop telemetry: why a hop was dropped, and which hop finally served.

A cascade is only legible if the row says which models were tried, why each was
dropped, and which hop answered. Without it the only visible hop is the terminal
one, which is how a dead primary or a drained account stays hidden.
"""

import dataclasses

import pytest
from test_engine import FakeLifecycle, make_engine

from orchestrator.chains import (
    HOP_REASON_BILLING,
    HOP_REASON_RATE_LIMIT,
    HOP_REASON_SEMANTIC,
    HOP_REASON_SUCCESS,
    HOP_REASON_TRANSPORT,
    FallbackState,
    classify_failure_reason,
)

# The two credit bodies this stack actually receives: a LiteLLM model group whose
# upstream balance is gone, and the DeepSeek direct API's own balance error.
CREDIT_ERROR = (
    "Error code: 402 - litellm.APIError: APIError: OpenAIException - Add credits to continue, "
    "or switch to a free model. Received Model Group=deepseek-v4-pro"
)
BALANCE_ERROR = 'DeepseekException - {"error":{"message":"Insufficient Balance (request_id: ff87c034)"}}'
# `retry_on_errors` is the status set that triggers a walk (see README); the
# deployed chains list 402, so a drained hop is a hop-switch rather than a stop.
WALK_CHAIN = {
    "chains": {"explore": ["cheap-model", "premium-model"]},
    "runtime_fallback": {"retry_on_errors": [402, 429, 500]},
}


@dataclasses.dataclass
class FailedState:
    name: str = "FAILED"


@dataclasses.dataclass
class ChildResult:
    """A worker that failed inside itself: the provider error arrives as a result."""

    error_message: str = ""
    summary: str = ""
    terminal_state: FailedState = dataclasses.field(default_factory=FailedState)


class ResultLifecycle:
    """Fails each launch with `errors[i]`, then serves; "" means the hop succeeded."""

    def __init__(self, errors):
        self.errors = list(errors)
        self.launched = []

    def launch(self, request):
        self.launched.append(request.model)
        return request.model

    def wait(self, handle, *, timeout_seconds=None):
        return None

    def result(self, handle):
        index = len(self.launched) - 1
        error = self.errors[index] if 0 <= index < len(self.errors) else ""
        return ChildResult(error_message=error) if error else {"summary": "done"}

    def cancel(self, handle, *, reason=""):
        return None


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"status": 402}, HOP_REASON_BILLING),
        ({"message": CREDIT_ERROR}, HOP_REASON_BILLING),
        ({"message": BALANCE_ERROR}, HOP_REASON_BILLING),
        ({"status": 429, "message": "rate limit exceeded"}, HOP_REASON_RATE_LIMIT),
        ({"status": 503, "message": "service unavailable"}, HOP_REASON_TRANSPORT),
        ({"error_type": "abort"}, HOP_REASON_SEMANTIC),
        ({"error_type": "context_overflow"}, HOP_REASON_SEMANTIC),
        ({"message": "an empty response came back"}, HOP_REASON_SEMANTIC),
    ],
)
def test_the_reason_names_the_class_of_failure(kwargs, expected):
    assert classify_failure_reason(**kwargs) == expected


def test_a_credit_body_is_billing_without_a_usable_status():
    # The status is not always parseable out of a provider body, so the reason
    # must not depend on it.
    assert classify_failure_reason(message=BALANCE_ERROR) == HOP_REASON_BILLING


def test_a_drained_account_switches_to_the_next_hop():
    lifecycle = ResultLifecycle([CREDIT_ERROR, ""])
    _, engine = make_engine(lifecycle, WALK_CHAIN)
    out = engine.dispatch(goal="scan the repo", target="explore")

    assert lifecycle.launched == ["cheap-model", "premium-model"]
    assert out["status"] == "succeeded"
    assert out["model"] == "premium-model"
    assert out["hop_history"] == [
        {"model": "cheap-model", "reason": HOP_REASON_BILLING},
        {"model": "premium-model", "reason": HOP_REASON_SUCCESS},
    ]


def test_every_hop_carries_its_reason_when_the_whole_walk_fails():
    lifecycle = ResultLifecycle([CREDIT_ERROR, "429 rate limit exceeded"])
    _, engine = make_engine(lifecycle, WALK_CHAIN)
    out = engine.dispatch(goal="scan the repo", target="explore")

    assert out["status"] == "failed"
    assert out["hop_history"] == [
        {"model": "cheap-model", "reason": HOP_REASON_BILLING},
        {"model": "premium-model", "reason": HOP_REASON_RATE_LIMIT},
    ]


def test_the_walk_survives_on_the_run_row():
    lifecycle = ResultLifecycle([CREDIT_ERROR, ""])
    _, engine = make_engine(lifecycle, WALK_CHAIN)
    out = engine.dispatch(goal="scan the repo", target="explore")

    row = engine.status(out["run_id"])["workers"][0]
    assert row["hop_history"] == [
        {"model": "cheap-model", "reason": HOP_REASON_BILLING},
        {"model": "premium-model", "reason": HOP_REASON_SUCCESS},
    ]


def test_a_primary_hop_is_recorded_as_the_exit_point():
    _, engine = make_engine(FakeLifecycle())
    out = engine.dispatch(goal="scan the repo", target="explore")

    # No walk happened: the single entry is the exit point of the cascade.
    assert out["status"] == "succeeded"
    assert out["hop_history"] == [{"model": "deepseek-v4-flash", "reason": HOP_REASON_SUCCESS}]


def test_the_walk_records_both_outcomes_in_order():
    state = FallbackState(chain=("cheap-model", "premium-model"))
    state.record_failure("cheap-model", reason=HOP_REASON_BILLING)
    state.record_success("premium-model")

    assert state.hop_history == [
        {"model": "cheap-model", "reason": HOP_REASON_BILLING},
        {"model": "premium-model", "reason": HOP_REASON_SUCCESS},
    ]
