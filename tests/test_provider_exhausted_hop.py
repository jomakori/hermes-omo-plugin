"""A hop that produced no answer must walk the chain on, not close as a success.

The harness can close a turn it could not serve as a *succeeded* child, with the
failure only in the summary. Read as success it ended the walk at the first hop
and the chain stayed inert, so a quota-drained Claude pair never reached the
credit-independent DeepSeek hop behind it.
"""

from __future__ import annotations

import dataclasses
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from orchestrator.chains import (  # noqa: E402
    HOP_REASON_PROVIDER_UNUSABLE,
    classify_failure_reason,
    is_retryable,
    provider_exhausted,
)
from orchestrator.engine import OmoEngine  # noqa: E402

NO_REPLY = (
    "\u26a0\ufe0f No reply: every provider in the fallback chain kept failing "
    "over, so the turn stopped instead of retrying forever. Send `continue` or "
    "switch provider."
)


def test_the_no_reply_turn_is_recognised():
    assert provider_exhausted(NO_REPLY)
    assert not provider_exhausted("Pushed the branch and opened the PR.")
    assert not provider_exhausted("")
    assert not provider_exhausted(None)


def test_no_reply_is_provider_unusable_so_the_stage_budget_is_spared():
    assert classify_failure_reason(message=NO_REPLY) == HOP_REASON_PROVIDER_UNUSABLE


def test_no_reply_is_retryable_so_the_walk_advances():
    assert is_retryable(message=NO_REPLY)


@dataclasses.dataclass
class State:
    name: str = "SUCCEEDED"


@dataclasses.dataclass
class Result:
    summary: str = NO_REPLY
    terminal_state: object | None = dataclasses.field(default_factory=State)
    error_message: str | None = None


def test_a_no_reply_result_reads_as_failed():
    engine = OmoEngine.__new__(OmoEngine)  # the predicate reads only the result

    assert engine._result_failed(Result()) is True
    assert engine._result_failed(Result(summary="did the work")) is False
