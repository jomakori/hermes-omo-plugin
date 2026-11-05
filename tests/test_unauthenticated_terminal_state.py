"""demote_unauthenticated: a SUCCEEDED claim with zero backing artifacts.

Pure unit tests against orchestrator.models.demote_unauthenticated, isolated
from the engine wiring already exercised in test_artifact_verification.py.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from orchestrator.models import (  # noqa: E402
    FAILED,
    INTERRUPTED,
    SUCCEEDED,
    UNAUTHENTICATED,
    demote_unauthenticated,
)


def test_a_succeeded_worker_verified_true_stays_succeeded():
    verification = {"verifiable": True, "verified": True, "artifacts": [{"exists": True}]}
    assert demote_unauthenticated(SUCCEEDED, verification) == SUCCEEDED


def test_a_succeeded_worker_with_zero_backing_artifacts_is_demoted():
    verification = {
        "verifiable": True,
        "verified": False,
        "artifacts": [{"exists": False, "detail": "claimed branch feat/x does not exist"}],
    }
    assert demote_unauthenticated(SUCCEEDED, verification) == UNAUTHENTICATED


def test_a_succeeded_worker_whose_verification_was_never_attempted_stays_succeeded():
    """Critical false-positive guard: absence of evidence never sought is not evidence."""
    assert demote_unauthenticated(SUCCEEDED, None) == SUCCEEDED
    assert demote_unauthenticated(SUCCEEDED, {}) == SUCCEEDED
    assert demote_unauthenticated(SUCCEEDED, {"verifiable": False, "detail": "nothing to verify"}) == SUCCEEDED


def test_a_failed_worker_is_never_touched():
    verification = {
        "verifiable": True,
        "verified": False,
        "artifacts": [{"exists": False, "detail": "claimed branch feat/x does not exist"}],
    }
    assert demote_unauthenticated(FAILED, verification) == FAILED


def test_an_interrupted_worker_is_never_touched():
    verification = {
        "verifiable": True,
        "verified": False,
        "artifacts": [{"exists": False, "detail": "claimed branch feat/x does not exist"}],
    }
    assert demote_unauthenticated(INTERRUPTED, verification) == INTERRUPTED


def test_a_verifier_crash_never_demotes_a_real_check_came_back_empty():
    """A COULD_NOT_CHECK verdict is non-evidence, not a confirmed absence."""
    verification = {
        "verifiable": True,
        "verified": False,
        "artifacts": [{"exists": False, "detail": "unverified, could not check: gh: command not found"}],
    }
    assert demote_unauthenticated(SUCCEEDED, verification) == SUCCEEDED


def test_a_mix_of_confirmed_and_could_not_check_still_demotes_if_none_exist():
    verification = {
        "verifiable": True,
        "verified": False,
        "artifacts": [
            {"exists": False, "detail": "claimed branch feat/x does not exist"},
            {"exists": False, "detail": "unverified, could not check: timeout"},
        ],
    }
    assert demote_unauthenticated(SUCCEEDED, verification) == UNAUTHENTICATED


def test_any_one_confirmed_existing_artifact_keeps_succeeded():
    verification = {
        "verifiable": True,
        "verified": False,
        "artifacts": [
            {"exists": True},
            {"exists": False, "detail": "claimed pr #4242 does not exist"},
        ],
    }
    assert demote_unauthenticated(SUCCEEDED, verification) == SUCCEEDED
