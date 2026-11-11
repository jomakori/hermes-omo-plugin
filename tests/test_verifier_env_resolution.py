"""The verifier must resolve its own checkout and credentials, not inherit them.

Issue #54's failure was reported in the host's words -- ``fatal: not a git
repository`` and ``gh auth login`` -- and read as the artifact being absent. The
classification half was fixed separately (#58): a check that could not run is
reported as could-not-check. This file pins the other half: *why* the check
could not run. git was subprocessed in whatever cwd the gateway inherited, and
gh ran without the token the host already exports, so a real claim had no way to
be confirmed even when both pieces were sitting there.

These tests fake the module-level ``_run`` so they assert on cwd and env --
the two things that were wrong -- without touching git, gh or the network.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from orchestrator import artifact_verify  # noqa: E402
from orchestrator.artifact_verify import (  # noqa: E402
    COULD_NOT_CHECK,
    KIND_BRANCH,
    KIND_COMMIT,
    KIND_PR,
    Claim,
    SubprocessVerifier,
)
from orchestrator.models import SUCCEEDED, UNAUTHENTICATED, demote_unauthenticated  # noqa: E402

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

# Verbatim from the two rejected runs (omo_5c9a7d9f, omo_34ef7653).
_NOT_A_REPO = "fatal: not a git repository (or any of the parent directories): .git"
_NO_CREDENTIALS = "gh: To get started with GitHub CLI, please run: gh auth login\nHTTP 401: Bad credentials"

_TOKEN_ENVS = ("GH_TOKEN", "MCP_GITHUB_TOKEN")


def _no_tokens(monkeypatch):
    for name in _TOKEN_ENVS:
        monkeypatch.delenv(name, raising=False)


def _recording_run(monkeypatch, *, code=0, output=""):
    """Replace the real subprocess call, recording what it was handed."""
    calls: list[dict] = []

    def fake_run(command, cwd, timeout_s, env=None):
        calls.append({"command": list(command), "cwd": cwd, "env": env})
        return code, output

    monkeypatch.setattr(artifact_verify, "_run", fake_run)
    return calls


def test_git_runs_in_a_resolved_checkout_not_the_inherited_cwd(monkeypatch):
    """The old code passed cwd=None; git then ran at /opt and answered NON_REPO."""
    _no_tokens(monkeypatch)
    calls = _recording_run(monkeypatch, output="")
    SubprocessVerifier()(Claim(KIND_COMMIT, "022eafa"))

    assert calls, "no subprocess was made"
    cwd = calls[0]["cwd"]
    assert cwd is not None
    assert (pathlib.Path(cwd) / ".git").exists(), cwd


def test_the_resolved_checkout_is_the_plugins_own_tree():
    root = artifact_verify._repo_root()

    assert root is not None
    assert pathlib.Path(root).resolve() == _REPO_ROOT


def test_gh_runs_with_the_token_the_host_already_exports(monkeypatch):
    """gh had no session in the failing runs; the token was there all along."""
    _no_tokens(monkeypatch)
    monkeypatch.setenv("MCP_GITHUB_TOKEN", "host-token")
    calls = _recording_run(monkeypatch, output="OPEN")

    verdict = SubprocessVerifier()(Claim(KIND_PR, "jomakori/openkite#259", repo="jomakori/openkite"))

    assert verdict.exists is True
    assert calls[0]["env"]["GH_TOKEN"] == "host-token"
    assert calls[0]["env"]["MCP_GITHUB_TOKEN"] == "host-token"


def test_an_explicit_token_wins_over_the_environment(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ambient-token")
    calls = _recording_run(monkeypatch, output="OPEN")

    SubprocessVerifier(token="explicit-token")(Claim(KIND_PR, "jomakori/openkite#259", repo="jomakori/openkite"))

    assert calls[0]["env"]["GH_TOKEN"] == "explicit-token"


def test_no_token_invents_no_credentials(monkeypatch):
    """An empty GH_TOKEN would look like a credential gh must use; pass nothing instead."""
    _no_tokens(monkeypatch)
    calls = _recording_run(monkeypatch, code=1, output=_NO_CREDENTIALS)

    verdict = SubprocessVerifier()(Claim(KIND_PR, "jomakori/openkite#259", repo="jomakori/openkite"))

    assert calls[0]["env"] is None
    assert verdict.exists is False
    assert COULD_NOT_CHECK in verdict.detail


def test_a_branch_claim_naming_a_repo_asks_that_repo(monkeypatch):
    """git ls-remote against a local remote would answer for the wrong repository."""
    _no_tokens(monkeypatch)
    calls = _recording_run(monkeypatch, code=1, output="gh: Not Found (HTTP 404)")

    verdict = SubprocessVerifier()(Claim(KIND_BRANCH, "feat/x", repo="jomakori/openkite"))

    assert calls[0]["command"] == ["gh", "api", "repos/jomakori/openkite/branches/feat/x", "--jq", ".name"]
    assert verdict.exists is False
    assert COULD_NOT_CHECK not in verdict.detail, verdict.detail


def test_the_live_failure_no_longer_demotes_a_run(monkeypatch):
    """Non-repo git output plus a credential failure is infrastructure, not a lie."""
    _no_tokens(monkeypatch)

    def runner(command):
        if command[0] == "git":
            return 128, _NOT_A_REPO
        return 1, _NO_CREDENTIALS

    verifier = SubprocessVerifier(cwd="/opt/data", runner=runner)
    verification = artifact_verify.verification_for(
        {
            "summary": "pushed the fix",
            "artifacts": {"repo": "jomakori/hermes-omo-plugin", "commit": "022eafa", "pr": 55},
        },
        verifier,
    )

    assert verification["verified"] is False
    assert COULD_NOT_CHECK in verification["detail"], verification["detail"]
    assert demote_unauthenticated(SUCCEEDED, verification) == SUCCEEDED


def test_a_confirmed_absence_still_demotes(monkeypatch):
    """The gate must not go fail-open: a 404 from the forge is still a lie."""
    monkeypatch.setenv("GH_TOKEN", "host-token")

    def runner(command):
        if command[0] == "git":
            return 128, _NOT_A_REPO
        return 1, "gh: Not Found (HTTP 404)"

    verifier = SubprocessVerifier(runner=runner)
    verification = artifact_verify.verification_for(
        {"summary": "pushed the fix", "artifacts": {"repo": "jomakori/hermes-omo-plugin", "commit": "deadbee"}},
        verifier,
    )

    assert verification["verified"] is False
    assert "not on jomakori/hermes-omo-plugin" in verification["detail"], verification["detail"]
    assert demote_unauthenticated(SUCCEEDED, verification) == UNAUTHENTICATED
