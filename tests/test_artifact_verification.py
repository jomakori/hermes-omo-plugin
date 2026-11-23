"""A worker's claimed branch / PR / commit must exist before SUCCEEDED is fact.

Observed four times: a worker returned SUCCEEDED with a summary naming a branch
and a commit that were never pushed (`git cat-file -t b9f38fa` ->
"Not a valid object name"). These tests pin the gate that catches it, with an
injected fake verifier so nothing here touches git, gh or the network.
"""

from __future__ import annotations

import asyncio
import dataclasses
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from orchestrator.artifact_verify import (  # noqa: E402
    COULD_NOT_CHECK,
    KIND_BRANCH,
    KIND_COMMIT,
    KIND_PR,
    Claim,
    SubprocessVerifier,
    Verdict,
    default_verifier,
    extract_claims,
    verification_for,
)
from orchestrator.chains import HOP_REASON_UNVERIFIABLE, ChainResolver  # noqa: E402
from orchestrator.engine import OmoEngine  # noqa: E402
from orchestrator.models import SUCCEEDED, UNAUTHENTICATED, demote_unauthenticated  # noqa: E402


class FakeVerifier:
    """Answers from a set of artifacts that exist; records every call."""

    def __init__(self, existing: set[str] | None = None, *, raises: bool = False) -> None:
        self.existing = existing or set()
        self.raises = raises
        self.calls: list[Claim] = []

    def __call__(self, claim: Claim) -> Verdict:
        self.calls.append(claim)
        if self.raises:
            raise RuntimeError("gh: command not found")
        exists = claim.value in self.existing
        detail = "" if exists else f"claimed {claim.kind} {claim.value} does not exist"
        return Verdict(claimed=claim.value, kind=claim.kind, exists=exists, detail=detail)


# ------------------------------------------------------------------ extraction


def test_a_structured_artifacts_mapping_is_preferred_over_the_prose():
    result = {
        "summary": "pushed fix/other-thing",
        "artifacts": {"repo": "o/r", "branch": "feat/real-branch", "pr": 29},
    }
    assert extract_claims(result) == [
        Claim(kind=KIND_BRANCH, value="feat/real-branch", repo="o/r"),
        Claim(kind=KIND_PR, value="o/r#29", repo="o/r"),
    ]


def test_prose_yields_branches_pr_urls_and_shas():
    summary = (
        "Opened https://github.com/jomakori/hermes-omo-plugin/pull/29 from branch "
        "`fix/summary-claim-qualification` at commit b9f38fa."
    )
    assert extract_claims(summary) == [
        Claim(kind=KIND_PR, value="jomakori/hermes-omo-plugin#29", repo="jomakori/hermes-omo-plugin"),
        Claim(kind=KIND_BRANCH, value="fix/summary-claim-qualification"),
        Claim(kind=KIND_COMMIT, value="b9f38fa"),
    ]


def test_a_pr_url_is_not_re_read_as_a_branch_or_a_sha():
    claims = extract_claims("see https://github.com/o/r/pull/7")
    assert [claim.kind for claim in claims] == [KIND_PR]


def test_prose_with_no_artifact_claims_nothing():
    assert extract_claims("Read-only recon; no branch and no PR were produced.") == []


def test_a_file_path_naming_tests_is_not_claimed_as_a_branch():
    # "tests" is a conventional branch prefix too, but tests/informer.rs is a
    # file a worker touched, not a ref it pushed (observed live: run
    # omo_0986be9f demoted over exactly this).
    assert extract_claims("Created tests/informer.rs") == []


def test_a_file_path_naming_docs_is_not_claimed_as_a_branch():
    assert extract_claims("edited crates/openkite-engine/tests/watch.rs and docs/setup.md") == []


def test_a_real_branch_with_a_conventional_prefix_is_still_claimed():
    assert extract_claims("merged feat/okt211-interest-supervisor") == [
        Claim(kind=KIND_BRANCH, value="feat/okt211-interest-supervisor"),
    ]


def test_a_dotted_release_branch_is_still_claimed_not_mistaken_for_a_file():
    # release/v1.2.3's final segment is "v1.2.3": its "extension" is ".3",
    # not a recognized source/doc/config suffix, so it is not a file path and
    # is still claimed as a branch, same as any other conventional-prefix ref.
    assert extract_claims("cut release/v1.2.3") == [
        Claim(kind=KIND_BRANCH, value="release/v1.2.3"),
    ]


# ---------------------------------------------- narrow commit-claim extraction
#
# Regression: a research worker citing an external project's commit, or any
# other hex-looking word, used to "claim" a commit it never made (_SHA ran
# over the whole narrative and took any 7-40 char hex run with a digit).
# SubprocessVerifier._commit() then resolved that token in the WRONG repo and
# demote_unauthenticated flipped a genuinely SUCCEEDED worker to
# UNAUTHENTICATED. A hex token is now a commit claim only when it is a full
# 40-char SHA, repo-qualified, or paired with explicit commit wording in the
# same clause; see module docstring on ``_claims_from_text``.


def test_an_external_projects_commit_citation_is_not_claimed():
    summary = (
        "Investigated how kubernetes/kubernetes handles this upstream; "
        "their fix lands around a1b2c3d9, not something we authored here."
    )
    claims = extract_claims(summary)
    assert [c for c in claims if c.kind == KIND_COMMIT] == []

    verification = verification_for(summary, FakeVerifier())
    assert demote_unauthenticated(SUCCEEDED, verification) == SUCCEEDED


def test_a_bare_hex_token_with_no_commit_wording_is_not_claimed():
    summary = "The build hash came back a1b2c3d, matching the cached artifact."
    claims = extract_claims(summary)
    assert [c for c in claims if c.kind == KIND_COMMIT] == []

    verification = verification_for(summary, FakeVerifier())
    assert demote_unauthenticated(SUCCEEDED, verification) == SUCCEEDED


def test_commit_wording_in_a_different_clause_does_not_reach_a_sha_in_another():
    # "committed" and the hex token are in different clauses (split on the
    # period), so the token must not be read as a commit claim.
    summary = "We committed the plan. See torvalds/linux at a1b2c3d for inspiration."
    assert [c for c in extract_claims(summary) if c.kind == KIND_COMMIT] == []


def test_a_full_forty_char_sha_is_claimed_even_with_no_commit_wording():
    sha = "a" * 40
    assert extract_claims(f"Diffed against {sha} upstream.") == [
        Claim(kind=KIND_COMMIT, value=sha),
    ]


def test_a_repo_qualified_sha_via_at_syntax_is_claimed_with_its_repo():
    assert extract_claims("Based the patch on jomakori/hermes-omo-plugin@b9f38fa.") == [
        Claim(kind=KIND_COMMIT, value="b9f38fa", repo="jomakori/hermes-omo-plugin"),
    ]


def test_a_github_commit_url_is_claimed_with_its_repo():
    url = "https://github.com/jomakori/hermes-omo-plugin/commit/b9f38fa"
    assert extract_claims(f"Pushed it, see {url}") == [
        Claim(kind=KIND_COMMIT, value="b9f38fa", repo="jomakori/hermes-omo-plugin"),
    ]


def test_a_short_sha_with_explicit_commit_wording_in_clause_is_claimed():
    assert extract_claims("Pushed as abc1234 to the remote") == [
        Claim(kind=KIND_COMMIT, value="abc1234"),
    ]
    assert extract_claims("SHA abc1234 is on the remote") == [
        Claim(kind=KIND_COMMIT, value="abc1234"),
    ]
    assert extract_claims("Committed abc1234 locally") == [
        Claim(kind=KIND_COMMIT, value="abc1234"),
    ]


# ----------------------------------------------------------------- the gate


def test_a_claimed_branch_that_is_missing_is_unverified():
    verifier = FakeVerifier(existing=set())
    report = verification_for("pushed feat/omr-11-gate", verifier)

    assert report["verifiable"] is True
    assert report["verified"] is False
    assert report["reason"] == HOP_REASON_UNVERIFIABLE
    assert "feat/omr-11-gate" in report["detail"]
    assert report["artifacts"] == [
        {
            "claimed": "feat/omr-11-gate",
            "kind": KIND_BRANCH,
            "exists": False,
            "detail": "claimed branch feat/omr-11-gate does not exist",
        }
    ]


def test_a_claimed_branch_that_is_present_is_verified():
    verifier = FakeVerifier(existing={"feat/omr-11-gate"})
    report = verification_for("pushed feat/omr-11-gate", verifier)

    assert report["verified"] is True
    assert "reason" not in report
    assert report["artifacts"][0]["exists"] is True


def test_a_pr_that_cannot_be_resolved_is_unverified():
    verifier = FakeVerifier(existing={"feat/omr-11-gate"})
    report = verification_for(
        {"artifacts": {"repo": "o/r", "branch": "feat/omr-11-gate", "pr": 4242}},
        verifier,
    )

    assert report["verified"] is False
    assert report["reason"] == HOP_REASON_UNVERIFIABLE
    assert [verdict["exists"] for verdict in report["artifacts"]] == [True, False]


def test_a_worker_that_claims_nothing_is_not_gated():
    verifier = FakeVerifier()
    report = verification_for("Read-only recon; nothing to push.", verifier)

    assert report["verifiable"] is False
    assert "verified" not in report, "a worker with no claim must not be marked unverified"
    assert verifier.calls == [], "extraction is local: no claim must cost no verifier call"


def test_a_verifier_that_raises_fails_open_to_could_not_check():
    verifier = FakeVerifier(raises=True)
    report = verification_for("pushed feat/omr-11-gate", verifier)

    assert report["verified"] is False
    assert COULD_NOT_CHECK in report["detail"]
    assert "gh: command not found" in report["detail"]


def test_each_distinct_claim_costs_exactly_one_call():
    summary = "feat/x then feat/x again, and feat/y; commit abc1234 and abc1234"
    verifier = FakeVerifier(existing={"feat/x", "feat/y", "abc1234"})

    report = verification_for(summary, verifier)

    assert report["verified"] is True
    assert [claim.value for claim in verifier.calls] == ["feat/x", "feat/y", "abc1234"]


# -------------------------------------------------- the real verifier's argv


def test_the_subprocess_verifier_asks_git_and_gh_the_narrow_question():
    seen: list[list[str]] = []

    def runner(command):
        seen.append(list(command))
        if command[1] == "ls-remote":
            return 0, "deadbee\trefs/heads/feat/x\n"
        return 0, '{"state":"OPEN","url":"https://github.com/o/r/pull/9"}'

    verifier = SubprocessVerifier(remote="origin", cwd="/app/repo", runner=runner)

    assert verifier(Claim(kind=KIND_BRANCH, value="feat/x")).exists is True
    assert verifier(Claim(kind=KIND_PR, value="o/r#9", repo="o/r")).exists is True
    assert verifier(Claim(kind=KIND_COMMIT, value="abc1234")).exists is True

    assert seen == [
        ["git", "ls-remote", "--heads", "origin", "feat/x"],
        ["gh", "pr", "view", "9", "--repo", "o/r", "--json", "state,url"],
        ["git", "cat-file", "-e", "abc1234"],
    ]


def test_an_empty_ls_remote_is_the_remote_saying_the_branch_is_absent():
    verifier = SubprocessVerifier(cwd="/app/repo", runner=lambda command: (0, "\n"))
    verdict = verifier(Claim(kind=KIND_BRANCH, value="feat/ghost"))
    assert verdict.exists is False
    assert "not on origin" in verdict.detail


def test_a_failing_git_is_could_not_check_not_a_missing_branch():
    verifier = SubprocessVerifier(cwd="/app/repo", runner=lambda command: (128, "fatal: no remote"))
    verdict = verifier(Claim(kind=KIND_BRANCH, value="feat/x"))
    assert verdict.exists is False
    assert COULD_NOT_CHECK in verdict.detail


def test_a_pr_claim_with_no_repository_cannot_be_resolved():
    verdict = SubprocessVerifier(runner=lambda command: (0, ""))(Claim(kind=KIND_PR, value="#9"))
    assert verdict.exists is False
    assert "names no repository" in verdict.detail


# ------------------------------------------------------------- engine wiring


@dataclasses.dataclass
class FakeRequest:
    goal: str
    context: str | None
    role: str
    model: str | None
    allowed_toolsets: tuple
    metadata: dict


def request_factory(*, goal, context, spec, model, toolsets):
    return FakeRequest(
        goal=goal,
        context=context,
        role="orchestrator" if spec.orchestrator else "leaf",
        model=model,
        allowed_toolsets=toolsets,
        metadata={"omo_agent": spec.name},
    )


@dataclasses.dataclass
class TerminalState:
    name: str = "SUCCEEDED"


@dataclasses.dataclass
class FakeResult:
    summary: str = "did the work"
    structured_payload: dict | None = None
    terminal_state: object | None = dataclasses.field(default_factory=TerminalState)
    error_message: str | None = None


class FakeHandle:
    def __init__(self, model):
        self.model = model


class FakeLifecycle:
    def __init__(self, result):
        self._result = result
        self.launches: list[str] = []

    def launch(self, request):
        self.launches.append(request.model)
        return FakeHandle(request.model)

    def wait(self, handle, *, timeout_seconds=None):
        return None

    def result(self, handle):
        return self._result

    def cancel(self, handle, *, reason=""):
        return None


class FakeCtx:
    def __init__(self, lifecycle, config=None):
        self.subagent_lifecycle = lifecycle
        self._config = config or {}
        self.spawned = []

    def get_config(self, key, default=None):
        return self._config.get(key, default)

    def emit(self, event, payload=None):
        return None

    def spawn_task(self, coro):
        self.spawned.append(coro)


def make_engine(summary, verifier, config=None):
    lifecycle = FakeLifecycle(FakeResult(summary=summary))
    ctx = FakeCtx(lifecycle, config)
    engine = OmoEngine(ctx, lifecycle=lifecycle, request_factory=request_factory)
    engine.chains = ChainResolver({})
    engine.artifact_verifier = verifier
    return engine


def test_a_succeeded_worker_claiming_a_missing_branch_is_marked_unverified():
    engine = make_engine("Pushed feat/ghost-branch and opened the PR.", FakeVerifier())

    out = engine.dispatch(goal="ship it", target="hephaestus")
    run_id = out["run_id"]
    row = engine.status(run_id)["workers"][0]

    # The host's own check ran and backed none of the claimed artifacts, so the
    # terminal state itself is demoted (see demote_unauthenticated): a plain
    # SUCCEEDED would read as the host having watched this pass, which it did not.
    assert row["status"] == UNAUTHENTICATED
    assert row["result"]["verified"] is False
    assert row["result"]["verification"]["reason"] == HOP_REASON_UNVERIFIABLE
    assert "feat/ghost-branch" in row["result"]["verification"]["detail"]
    assert out["verification"]["verified"] is False


def test_a_succeeded_worker_whose_branch_exists_carries_no_unverified_marker():
    engine = make_engine("Pushed feat/real-branch.", FakeVerifier(existing={"feat/real-branch"}))

    row = engine.status(engine.dispatch(goal="ship it", target="hephaestus")["run_id"])["workers"][0]

    assert row["status"] == SUCCEEDED
    assert "verified" not in row["result"]


def test_a_read_only_worker_is_left_alone():
    verifier = FakeVerifier()
    engine = make_engine("Reviewed the diff; no changes pushed.", verifier)

    row = engine.status(engine.dispatch(goal="review", target="explore")["run_id"])["workers"][0]

    assert row["status"] == SUCCEEDED
    assert "verified" not in row["result"]
    assert verifier.calls == []


def test_a_verifier_crash_does_not_fail_the_run():
    engine = make_engine("Pushed feat/ghost-branch.", FakeVerifier(raises=True))

    out = engine.dispatch(goal="ship it", target="hephaestus")
    row = engine.status(out["run_id"])["workers"][0]

    assert row["status"] == SUCCEEDED, "a verifier failure must never fail the run"
    assert row["result"]["verified"] is False
    assert COULD_NOT_CHECK in row["result"]["verification"]["detail"]


def test_the_gate_can_be_switched_off_by_config():
    verifier = FakeVerifier()
    engine = make_engine("Pushed feat/ghost-branch.", verifier, {"verify_claimed_artifacts": False})

    row = engine.status(engine.dispatch(goal="ship it", target="hephaestus")["run_id"])["workers"][0]

    assert "verified" not in row["result"]
    assert verifier.calls == []


def test_the_background_path_verifies_too():
    engine = make_engine("Pushed feat/ghost-branch.", FakeVerifier())
    out = engine.dispatch(goal="ship it", target="hephaestus", background=True)
    asyncio.run(engine._ctx.spawned.pop())

    row = engine.status(out["run_id"])["workers"][0]
    assert row["result"]["verified"] is False


# --------------------------------------- resolving a claim in the right repo
#
# Regression: SubprocessVerifier() was built with no cwd, so _exec() fell back
# to _repo_root(), which climbs from this module's own __file__ and therefore
# always found the plugin's checkout. Repo-less claims were then answered by
# the wrong repository, and "not a valid object" read as evidence of absence --
# which demoted workers whose commit lived in the repo they had just worked in.


def test_a_repo_qualified_commit_is_asked_of_the_forge_never_a_local_checkout():
    seen: list[list[str]] = []

    def runner(command):
        seen.append(list(command))
        return 0, "a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8c9d0\n"

    sha = "a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8c9d0"
    verdict = SubprocessVerifier(runner=runner)(Claim(kind=KIND_COMMIT, value=sha, repo="jomakori/hermes-omo-plugin"))

    assert verdict.exists is True
    assert seen == [["gh", "api", f"repos/jomakori/hermes-omo-plugin/commits/{sha}", "--jq", ".sha"]]


def test_a_repo_less_commit_with_no_workspace_is_could_not_check_not_absent():
    def runner(command):  # pragma: no cover - must never be reached
        raise AssertionError(f"git must not be asked: {command}")

    verdict = SubprocessVerifier(runner=runner)(Claim(kind=KIND_COMMIT, value="abc1234"))

    assert verdict.exists is False
    assert COULD_NOT_CHECK in verdict.detail


def test_a_repo_less_branch_with_no_workspace_is_could_not_check_not_absent():
    def runner(command):  # pragma: no cover - must never be reached
        raise AssertionError(f"git must not be asked: {command}")

    verdict = SubprocessVerifier(runner=runner)(Claim(kind=KIND_BRANCH, value="feat/x"))

    assert verdict.exists is False
    assert COULD_NOT_CHECK in verdict.detail


def test_a_commit_that_cannot_be_located_keeps_a_succeeded_worker_succeeded():
    summary = "Landed the fix in commit a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8c9d0."
    verification = verification_for(
        summary, SubprocessVerifier(runner=lambda command: (1, "fatal: not a git repository"))
    )

    assert demote_unauthenticated(SUCCEEDED, verification) == SUCCEEDED


def test_a_commit_the_named_repo_does_not_have_still_demotes():
    summary = "Landed it in jomakori/hermes-omo-plugin@a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8c9d0."
    verification = verification_for(summary, SubprocessVerifier(runner=lambda command: (1, "gh: Not Found (HTTP 404)")))

    assert demote_unauthenticated(SUCCEEDED, verification) == UNAUTHENTICATED


def test_the_verifier_no_longer_climbs_to_a_repository_root():
    import orchestrator.artifact_verify as module

    assert not hasattr(module, "_repo_root"), "a __file__-derived repo root must not come back"
    assert default_verifier().cwd is None
    assert default_verifier(workspace="/app/repo").cwd == "/app/repo"
