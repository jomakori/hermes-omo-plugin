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
    extract_claims,
    verification_for,
)
from orchestrator.chains import HOP_REASON_UNVERIFIABLE, ChainResolver  # noqa: E402
from orchestrator.engine import OmoEngine  # noqa: E402
from orchestrator.models import SUCCEEDED  # noqa: E402


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

    verifier = SubprocessVerifier(remote="origin", runner=runner)

    assert verifier(Claim(kind=KIND_BRANCH, value="feat/x")).exists is True
    assert verifier(Claim(kind=KIND_PR, value="o/r#9", repo="o/r")).exists is True
    assert verifier(Claim(kind=KIND_COMMIT, value="abc1234")).exists is True

    assert seen == [
        ["git", "ls-remote", "--heads", "origin", "feat/x"],
        ["gh", "pr", "view", "9", "--repo", "o/r", "--json", "state,url"],
        ["git", "cat-file", "-e", "abc1234"],
    ]


def test_an_empty_ls_remote_is_the_remote_saying_the_branch_is_absent():
    verifier = SubprocessVerifier(runner=lambda command: (0, "\n"))
    verdict = verifier(Claim(kind=KIND_BRANCH, value="feat/ghost"))
    assert verdict.exists is False
    assert "not on origin" in verdict.detail


def test_a_failing_git_is_could_not_check_not_a_missing_branch():
    verifier = SubprocessVerifier(runner=lambda command: (128, "fatal: no remote"))
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

    # The host still saw a clean exit, so the status is not rewritten; what
    # changes is that the result no longer reads as a plain SUCCEEDED.
    assert row["status"] == SUCCEEDED
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
