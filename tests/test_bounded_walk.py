"""Bounds on the chain walk: the premium hop's payload, per-agent attempts, gone clients.

Three economics, one file: the hop onto the last (premium) chain element carries a
bounded handoff rather than the whole launch payload again; a stage that keeps
failing stops instead of re-walking the chain on every re-dispatch; and a request
whose client has already gone is not re-issued.
"""

import dataclasses

from test_engine import make_engine

from orchestrator.chains import client_disconnected


class _Failed:
    name = "FAILED"


class FailedResult:
    def __init__(self, error):
        self.terminal_state = _Failed()
        self.error_message = error
        self.summary = ""


@dataclasses.dataclass
class RecordedHandle:
    model: str


class RecordingLifecycle:
    """Keeps every launch request and fails every result, unless told otherwise."""

    def __init__(self, *, error="429 rate limit exceeded", on_result=None):
        self.error = error
        self.requests = []
        self.cancelled = []
        self._on_result = on_result

    def launch(self, request):
        self.requests.append(request)
        return RecordedHandle(model=request.model)

    def wait(self, handle, *, timeout_seconds=None):
        return None

    def result(self, handle):
        if self._on_result is not None:
            self._on_result(handle)
        return FailedResult(self.error)

    def cancel(self, handle, *, reason=""):
        self.cancelled.append(handle.model)
        return None


def launched(lifecycle):
    return [request.model for request in lifecycle.requests]


def artifacts_of(request):
    """The per-task payload of a launch: its goal plus the framed task context."""
    context = request.context or ""
    _, marker, tail = context.partition("<task_context>")
    body = tail[: -len("\n</task_context>")].strip() if marker and tail.endswith("\n</task_context>") else tail
    return request.goal + body


LONG_GOAL = "restructure the sync path so partial failures surface " * 200
LONG_CONTEXT = "convention: never touch the live plugin. evidence: line 42. " * 900


# ── (a) the premium hop gets a bounded handoff ───────────────────────────────


def test_the_last_hop_receives_a_bounded_handoff():
    lifecycle = RecordingLifecycle()
    _, engine = make_engine(
        lifecycle,
        {"chains": {"explore": ["cheap-model", "premium-model"]}, "escalation_budget_chars": 1000},
    )
    out = engine.dispatch(goal=LONG_GOAL, target="explore", context=LONG_CONTEXT)

    assert launched(lifecycle) == ["cheap-model", "premium-model"]
    primary, premium = lifecycle.requests
    # The cheap hop is untouched: the payload is the task.
    assert primary.goal == LONG_GOAL
    assert LONG_CONTEXT[:40] in primary.context
    # The premium hop gets a brief plus explicit artifacts, inside the budget.
    assert len(artifacts_of(premium)) <= 1000
    assert len(artifacts_of(premium)) < len(artifacts_of(primary))
    assert premium.metadata["omo_handoff"] == "bounded_last_hop"
    assert premium.metadata["omo_handoff_budget_chars"] == 1000
    assert out["status"] == "failed"


def test_a_bounded_handoff_keeps_the_contract_and_the_persona_whole():
    lifecycle = RecordingLifecycle()
    _, engine = make_engine(
        lifecycle,
        {"chains": {"explore": ["cheap-model", "premium-model"]}, "escalation_budget_chars": 800},
    )
    engine.dispatch(goal=LONG_GOAL, target="explore", context=LONG_CONTEXT)

    primary, premium = lifecycle.requests
    head = primary.context.split("<task_context>", 1)[0]
    # The worker contract and persona are constant per agent, not accumulated
    # payload: they travel whole and only the per-task artifacts are clipped.
    assert premium.context.startswith(head)
    assert "<internal_worker>" in premium.context
    assert premium.context.count("<task_context>") == 1
    assert premium.context.endswith("</task_context>")
    # The host refuses a launch whose context exceeds its own cap.
    assert len(premium.context) <= 32_000


def test_a_middle_hop_still_re_issues_the_full_payload():
    lifecycle = RecordingLifecycle()
    _, engine = make_engine(
        lifecycle,
        {"chains": {"explore": ["cheap-model", "middle-model", "premium-model"]}},
    )
    engine.dispatch(goal=LONG_GOAL, target="explore", context=LONG_CONTEXT)

    assert launched(lifecycle) == ["cheap-model", "middle-model", "premium-model"]
    primary, middle, premium = lifecycle.requests
    assert middle.goal == primary.goal
    assert middle.context == primary.context
    assert "omo_handoff" not in middle.metadata
    assert "omo_handoff" in premium.metadata


def test_a_handoff_without_task_context_keeps_the_persona_whole():
    lifecycle = RecordingLifecycle()
    _, engine = make_engine(
        lifecycle,
        {"chains": {"explore": ["cheap-model", "premium-model"]}, "escalation_budget_chars": 500},
    )
    engine.dispatch(goal=LONG_GOAL, target="explore")

    primary, premium = lifecycle.requests
    # No caller context means no payload: only the goal is bounded, and the constant
    # contract/persona is not clipped away with it.
    assert premium.context == primary.context
    assert len(premium.goal) <= 500


def test_the_escalation_budget_is_configurable_and_can_be_disabled():
    lifecycle = RecordingLifecycle()
    _, engine = make_engine(
        lifecycle,
        {"chains": {"explore": ["cheap-model", "premium-model"]}, "escalation_budget_chars": 4000},
    )
    engine.dispatch(goal=LONG_GOAL, target="explore", context=LONG_CONTEXT)
    assert len(artifacts_of(lifecycle.requests[1])) <= 4000

    disabled = RecordingLifecycle()
    _, unbounded = make_engine(
        disabled,
        {"chains": {"explore": ["cheap-model", "premium-model"]}, "escalation_budget_chars": 0},
    )
    unbounded.dispatch(goal=LONG_GOAL, target="explore", context=LONG_CONTEXT)
    # 0 opts out: the premium hop is handed the request as it stands.
    assert disabled.requests[1].goal == LONG_GOAL
    assert disabled.requests[1].context == disabled.requests[0].context
    assert "omo_handoff" not in disabled.requests[1].metadata


# ── (b) attempts are bounded per agent and stage ─────────────────────────────


def test_a_stage_that_keeps_failing_stops_after_the_attempt_budget():
    lifecycle = RecordingLifecycle()
    _, engine = make_engine(
        lifecycle,
        {"max_attempts_per_agent": 2, "chains": {"explore": ["cheap-model", "premium-model"]}},
    )
    outcomes = [engine.dispatch(goal="map the repo", target="explore") for _ in range(3)]

    # Two attempts walked the chain; the third never launched anything.
    assert launched(lifecycle) == ["cheap-model", "premium-model", "cheap-model", "premium-model"]
    assert [out["status"] for out in outcomes] == ["failed", "failed", "failed"]
    assert "max_attempts_per_agent=2" in outcomes[2]["error"]
    assert "refusing another attempt" in outcomes[2]["error"]
    assert outcomes[2]["result"] is None


def test_the_attempt_budget_is_per_stage_and_per_agent():
    lifecycle = RecordingLifecycle()
    _, engine = make_engine(
        lifecycle,
        {
            "max_attempts_per_agent": 1,
            "chains": {"explore": ["cheap-model"], "librarian": ["cheap-model"]},
        },
    )
    assert engine.dispatch(goal="stage one", target="explore")["status"] == "failed"
    # A different stage for the same agent is different work: it still runs.
    assert engine.dispatch(goal="stage two", target="explore")["status"] == "failed"
    # The same stage for a different agent is a different worker: it still runs.
    assert engine.dispatch(goal="stage one", target="librarian")["status"] == "failed"
    # The stage that has already failed once is refused.
    refused = engine.dispatch(goal="stage one", target="explore")
    assert launched(lifecycle) == ["cheap-model", "cheap-model", "cheap-model"]
    assert "max_attempts_per_agent=1" in refused["error"]


def test_disabling_the_attempt_budget_lets_a_stage_fail_forever():
    lifecycle = RecordingLifecycle()
    _, engine = make_engine(
        lifecycle,
        {"max_attempts_per_agent": 0, "chains": {"explore": ["cheap-model"]}},
    )
    for _ in range(5):
        assert engine.dispatch(goal="same stage", target="explore")["status"] == "failed"
    assert launched(lifecycle) == ["cheap-model"] * 5


def test_cancelled_work_does_not_consume_the_attempt_budget():
    lifecycle = RecordingLifecycle(error="HTTP 499: client closed request")
    _, engine = make_engine(
        lifecycle,
        {"max_attempts_per_agent": 2, "chains": {"explore": ["cheap-model", "premium-model"]}},
    )
    for _ in range(3):
        # Nobody can receive this work, so it says nothing about the agent's ability
        # to do it — and must not burn the stage's budget.
        assert engine.dispatch(goal="same stage", target="explore")["status"] == "cancelled"
    assert launched(lifecycle) == ["cheap-model"] * 3


# ── (c) a request whose client is gone is not re-issued ──────────────────────


def test_a_cancelled_run_is_not_walked_onto_the_next_model():
    holder = {}
    lifecycle = RecordingLifecycle(on_result=lambda handle: holder["engine"].cancel(list(holder["engine"].runs)[0]))
    _, engine = make_engine(lifecycle, {"chains": {"explore": ["cheap-model", "premium-model"]}})
    holder["engine"] = engine

    out = engine.dispatch(goal="scan the repo", target="explore")

    assert launched(lifecycle) == ["cheap-model"]
    assert out["status"] == "cancelled"
    assert out["client_gone"] is False


def test_a_client_disconnect_is_never_retried_even_when_499_is_configured():
    lifecycle = RecordingLifecycle(error="HTTP 499: client closed request")
    _, engine = make_engine(
        lifecycle,
        {
            "chains": {"explore": ["cheap-model", "premium-model"]},
            "runtime_fallback": {"retry_on_errors": [499], "max_fallback_attempts": 3},
        },
    )
    out = engine.dispatch(goal="scan the repo", target="explore")

    # The operator listed 499 as retryable; a disconnect is still not retried,
    # because the CLI is gone and its answer can never be delivered.
    assert launched(lifecycle) == ["cheap-model"]
    assert out["status"] == "cancelled"
    assert out["client_gone"] is True
    assert "499" in out["error"]


def test_a_retryable_provider_failure_still_walks_the_chain():
    lifecycle = RecordingLifecycle(error="HTTP 429 rate limit exceeded")
    _, engine = make_engine(lifecycle, {"chains": {"explore": ["cheap-model", "mid-model", "premium-model"]}})
    out = engine.dispatch(goal="scan the repo", target="explore")

    assert launched(lifecycle) == ["cheap-model", "mid-model", "premium-model"]
    assert out["status"] == "failed"
    assert out["client_gone"] is False


def test_client_disconnect_is_recognised_from_the_failure_text():
    assert client_disconnected("HTTP 499: client closed request")
    assert client_disconnected("499 Client Closed Request")
    assert client_disconnected("client disconnected before the answer was written")
    assert client_disconnected("litellm.APIError: client_disconnected")
    assert not client_disconnected("HTTP 429 rate limit exceeded")
    assert not client_disconnected("connection reset by peer")
    assert not client_disconnected("consumed 14999 input tokens")
    assert not client_disconnected("")
    assert not client_disconnected(None)
