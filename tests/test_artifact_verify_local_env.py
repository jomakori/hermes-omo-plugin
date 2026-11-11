"""Verifier checks that could not run must not read as checks that failed.

Observed live four times: a worker pushed a real branch and PR, the host's
verifier ran ``git cat-file`` from a cwd that was never a clone, and the
"fatal: not a git repository" output was recorded as the commit being absent.
Two runs were rejected that way while their PRs were in fact green and merged
by hand (hermes-omo-plugin#55, gke_GitOps#496).

These tests pin the distinction with an injected runner, so nothing here
touches git, gh or the network.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from orchestrator.artifact_verify import (  # noqa: E402
    COULD_NOT_CHECK,
    KIND_COMMIT,
    KIND_PR,
    Claim,
    SubprocessVerifier,
)
from orchestrator.models import SUCCEEDED, UNAUTHENTICATED, demote_unauthenticated  # noqa: E402

NON_REPO = "fatal: not a git repository (or any of the parent directories): .git"


def runner_answering(*, git_output: str, gh_output: str, gh_code: int = 0):
    """Answer git cat-file with ``git_output`` and any gh call with ``gh_output``."""
    seen: list[list[str]] = []

    def run(command):
        seen.append(list(command))
        if command[0] == "git":
            return 128, git_output
        return gh_code, gh_output

    run.seen = seen  # type: ignore[attr-defined]
    return run


def test_a_commit_check_that_could_not_run_is_unverifiable_not_missing():
    verifier = SubprocessVerifier(cwd="/opt/data", runner=runner_answering(git_output=NON_REPO, gh_output=""))
    verdict = verifier(Claim(kind=KIND_COMMIT, value="022eafa"))

    assert verdict.exists is False
    assert COULD_NOT_CHECK in verdict.detail, verdict.detail
    assert "not a valid object" not in verdict.detail, verdict.detail


def test_the_same_commit_is_verified_through_the_forge_when_a_repo_is_named():
    runner = runner_answering(git_output=NON_REPO, gh_output="022eafa1c0ffee")
    verdict = SubprocessVerifier(cwd="/opt/data", runner=runner)(
        Claim(KIND_COMMIT, "022eafa", repo="jomakori/openkite")
    )

    assert verdict.exists is True, verdict.detail
    assert ["gh", "api", "repos/jomakori/openkite/commits/022eafa", "--jq", ".sha"] in runner.seen


def test_a_genuinely_absent_commit_still_reads_as_missing():
    verifier = SubprocessVerifier(
        runner=runner_answering(git_output="fatal: Not a valid object name 022eafa", gh_output="")
    )
    verdict = verifier(Claim(KIND_COMMIT, "022eafa", repo="jomakori/openkite"))

    assert verdict.exists is False
    assert COULD_NOT_CHECK not in verdict.detail, verdict.detail


def test_a_forge_that_says_not_found_is_missing_not_unverifiable():
    runner = runner_answering(git_output=NON_REPO, gh_output="gh: Not Found (HTTP 404)", gh_code=1)
    verdict = SubprocessVerifier(runner=runner)(Claim(KIND_COMMIT, "deadbee", repo="jomakori/openkite"))

    assert verdict.exists is False
    assert COULD_NOT_CHECK not in verdict.detail, verdict.detail
    assert "not on jomakori/openkite" in verdict.detail, verdict.detail


def test_a_forge_that_merely_failed_is_unverifiable():
    runner = runner_answering(git_output=NON_REPO, gh_output="error connecting to api.github.com", gh_code=1)
    verdict = SubprocessVerifier(runner=runner)(Claim(KIND_COMMIT, "022eafa", repo="jomakori/openkite"))

    assert COULD_NOT_CHECK in verdict.detail, verdict.detail


def test_a_pr_read_that_merely_failed_is_unverifiable():
    runner = runner_answering(git_output="", gh_output="dial tcp: lookup api.github.com: i/o timeout", gh_code=1)
    verdict = SubprocessVerifier(runner=runner)(Claim(KIND_PR, "jomakori/openkite#259", repo="jomakori/openkite"))

    assert COULD_NOT_CHECK in verdict.detail, verdict.detail


def test_an_unverifiable_commit_never_demotes_a_succeeded_run():
    verifier = SubprocessVerifier(runner=runner_answering(git_output=NON_REPO, gh_output=""))
    verification = {
        "verifiable": True,
        "verified": False,
        "artifacts": [verifier(Claim(KIND_COMMIT, "022eafa")).as_dict()],
    }

    assert demote_unauthenticated(SUCCEEDED, verification) == SUCCEEDED


def test_a_confirmed_absence_still_demotes_a_succeeded_run():
    verifier = SubprocessVerifier(
        runner=runner_answering(git_output="fatal: Not a valid object name 022eafa", gh_output="")
    )
    verification = {
        "verifiable": True,
        "verified": False,
        "artifacts": [verifier(Claim(KIND_COMMIT, "022eafa", repo="jomakori/openkite")).as_dict()],
    }

    assert demote_unauthenticated(SUCCEEDED, verification) == UNAUTHENTICATED
