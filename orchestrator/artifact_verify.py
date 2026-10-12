"""Does a worker's claimed branch, PR or commit actually exist.

A worker's SUCCEEDED says its process exited cleanly. It does not say the
branch, pull request or commit its summary names was ever pushed. Four observed
runs reported SUCCEEDED while naming work that was not there — one claimed the
branch ``fix/summary-claim-qualification`` and commit ``b9f38fa``, and
``git cat-file -t b9f38fa`` answered ``Not a valid object name``.

This module turns such a claim into a checkable question and answers it against
the only things that can settle it: the remote's refs, the GitHub API, the
object database. The checking itself is injected (:class:`ArtifactVerifier`),
so unit tests never touch the network.

Scope, deliberately narrow: the gate *marks* an unverified claim, it never
advances a chain and never re-runs the task. Work that claims a branch may have
half-landed, so re-running it risks a duplicate branch or PR; the decision of
what to do about an unverified claim stays with the reader.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from orchestrator.chains import HOP_REASON_UNVERIFIABLE

KIND_BRANCH = "branch"
KIND_PR = "pr"
KIND_COMMIT = "commit"

# A verifier that raised is not evidence the artifact is missing, only that
# nobody could look. It still fails the gate — an unchecked claim is not a
# verified one — but it says so in these words rather than claiming absence.
COULD_NOT_CHECK = "unverified, could not check"
NO_CLAIM_DETAIL = "no branch, PR or commit was claimed; nothing to verify"

__all__ = [
    "COULD_NOT_CHECK",
    "KIND_BRANCH",
    "KIND_COMMIT",
    "KIND_PR",
    "NO_CLAIM_DETAIL",
    "ArtifactVerifier",
    "Claim",
    "SubprocessVerifier",
    "Verdict",
    "default_verifier",
    "extract_claims",
    "no_claim_report",
    "verification_for",
    "verify_claims",
]


@dataclass(frozen=True)
class Claim:
    """One artifact a worker says it produced.

    ``repo`` is the ``<owner>/<name>`` a PR claim needs to be resolvable at all;
    it is empty when the claim never named one, which is itself a verdict (a
    bare "PR #12" cannot be checked against anything).
    """

    kind: str
    value: str
    repo: str = ""


@dataclass(frozen=True)
class Verdict:
    """Whether one claimed artifact is actually there."""

    claimed: str
    kind: str
    exists: bool
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"claimed": self.claimed, "kind": self.kind, "exists": self.exists, "detail": self.detail}


class ArtifactVerifier(Protocol):
    """The test seam: anything callable as ``verifier(claim) -> Verdict``.

    Production wires :class:`SubprocessVerifier` (git and gh); a test wires a
    fake that answers from a dict, or raises to prove the gate fails open.
    """

    def __call__(self, claim: Claim) -> Verdict: ...


# ---------------------------------------------------------------- extraction

_PR_URL = re.compile(r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)")

# The conventional branch prefixes a worker's narrative actually uses. A token
# without one of these is not treated as a branch: guessing wider would gate on
# any slash-separated word pair in prose.
_BRANCH_PREFIXES = (
    "feat",
    "feature",
    "fix",
    "hotfix",
    "bugfix",
    "chore",
    "docs",
    "refactor",
    "test",
    "tests",
    "perf",
    "ci",
    "build",
    "style",
    "release",
)
_BRANCH = re.compile(r"\b(?:" + "|".join(_BRANCH_PREFIXES) + r")/[A-Za-z0-9._\-/]+")
_SHA = re.compile(r"\b[0-9a-f]{7,40}\b")
_TRAILING = "`.,;:!?)]}>'\"*"

_KIND_BY_KEY = {
    "branch": KIND_BRANCH,
    "branches": KIND_BRANCH,
    "head": KIND_BRANCH,
    "head_branch": KIND_BRANCH,
    "pr": KIND_PR,
    "prs": KIND_PR,
    "pull_request": KIND_PR,
    "pull_requests": KIND_PR,
    "commit": KIND_COMMIT,
    "commits": KIND_COMMIT,
    "sha": KIND_COMMIT,
    "shas": KIND_COMMIT,
}
_REPO_KEYS = ("repo", "repository", "slug")


def _clean(value: Any) -> str:
    return str(value or "").strip().strip(_TRAILING).strip()


def _looks_like_sha(token: str) -> bool:
    """A hex run with at least one digit.

    ``deadbeef`` is valid hex and also an English word; requiring a digit keeps
    prose out of the claim list at the cost of never catching an all-letter
    short SHA, which is the safe direction (a missed claim is not gated, a
    false claim would gate a worker for nothing).
    """
    return any(char.isdigit() for char in token)


def _pr_claim(value: str, repo: str = "") -> Claim | None:
    """A PR claim from a URL or a bare number, carrying whatever repo is known."""
    text = _clean(value)
    if not text:
        return None
    match = _PR_URL.search(text)
    if match:
        owner, name, number = match.groups()
        return Claim(kind=KIND_PR, value=f"{owner}/{name}#{number}", repo=f"{owner}/{name}")
    number = text.lstrip("#").strip()
    if number.isdigit():
        return Claim(kind=KIND_PR, value=f"{repo}#{number}" if repo else f"#{number}", repo=repo)
    return None


def _claims_from_mapping(artifacts: Mapping[str, Any]) -> list[Claim]:
    """Claims from a structured ``artifacts`` mapping — the preferred source.

    A worker that reports its deliverables as data is taken at its word about
    *what* it claims (never about whether the claim is true), so no regex has
    to guess at prose.
    """
    repo = ""
    for key in _REPO_KEYS:
        repo = _clean(artifacts.get(key))
        if repo:
            break
    claims: list[Claim] = []
    for key, raw in artifacts.items():
        kind = _KIND_BY_KEY.get(str(key).strip().lower())
        if not kind:
            continue
        values: Iterable[Any] = raw if isinstance(raw, (list, tuple, set)) else (raw,)
        for value in values:
            if isinstance(value, Mapping):
                # e.g. {"pr": {"number": 29, "repo": "o/r"}}
                nested = _clean(value.get("url") or value.get("number") or value.get("name") or value.get("value"))
                nested_repo = next((_clean(value.get(name)) for name in _REPO_KEYS if value.get(name)), repo)
                value, local_repo = nested, nested_repo
            else:
                local_repo = repo
            text = _clean(value)
            if not text:
                continue
            if kind == KIND_PR:
                claim = _pr_claim(text, local_repo)
                if claim:
                    claims.append(claim)
                continue
            claims.append(Claim(kind=kind, value=text, repo=local_repo))
    return claims


def _claims_from_text(summary: str) -> list[Claim]:
    """Claims read out of a worker's prose, PR URLs first.

    PR URL spans are removed before the branch and SHA passes so a URL's own
    path cannot be re-read as a second, bogus claim.
    """
    text = summary or ""
    claims: list[Claim] = []
    for match in _PR_URL.finditer(text):
        owner, name, number = match.groups()
        claims.append(Claim(kind=KIND_PR, value=f"{owner}/{name}#{number}", repo=f"{owner}/{name}"))
    remainder = _PR_URL.sub(" ", text)
    for match in _BRANCH.finditer(remainder):
        branch = _clean(match.group(0))
        if branch:
            claims.append(Claim(kind=KIND_BRANCH, value=branch))
    remainder = _BRANCH.sub(" ", remainder)
    for token in _SHA.findall(remainder):
        if _looks_like_sha(token):
            claims.append(Claim(kind=KIND_COMMIT, value=token))
    return claims


def _structured_artifacts(result: Any) -> Mapping[str, Any] | None:
    for holder in (result, getattr(result, "structured_payload", None)):
        if isinstance(holder, Mapping):
            for key in ("artifacts", "structured_payload"):
                found = holder.get(key)
                if isinstance(found, Mapping) and key == "artifacts":
                    return found
                if isinstance(found, Mapping):
                    nested = found.get("artifacts")
                    if isinstance(nested, Mapping):
                        return nested
            continue
        found = getattr(holder, "artifacts", None)
        if isinstance(found, Mapping):
            return found
    return None


def _summary_of(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, Mapping):
        return str(result.get("summary") or "")
    return str(getattr(result, "summary", "") or "")


def extract_claims(result: Any) -> list[Claim]:
    """What a worker's result claims it produced, deduped in first-seen order.

    A structured ``artifacts`` mapping wins when it carries anything at all;
    otherwise the summary's prose is read. An empty list means nothing was
    claimed, which must never be gated: a read-only worker produces no branch
    and is not lying about one.
    """
    artifacts = _structured_artifacts(result)
    claims = _claims_from_mapping(artifacts) if artifacts else []
    if not claims:
        claims = _claims_from_text(_summary_of(result))
    seen: set[tuple[str, str, str]] = set()
    unique: list[Claim] = []
    for claim in claims:
        key = (claim.kind, claim.value, claim.repo)
        if key in seen:
            continue
        seen.add(key)
        unique.append(claim)
    return unique


# ------------------------------------------------------------------- the gate


def no_claim_report() -> dict[str, Any]:
    """The verdict for a worker that claimed nothing: not verifiable, not gated."""
    return {"verifiable": False, "detail": NO_CLAIM_DETAIL}


def _verify_one(claim: Claim, verifier: ArtifactVerifier) -> Verdict:
    try:
        verdict = verifier(claim)
    except Exception as exc:
        return Verdict(claimed=claim.value, kind=claim.kind, exists=False, detail=f"{COULD_NOT_CHECK}: {exc}")
    if not isinstance(verdict, Verdict):
        return Verdict(claimed=claim.value, kind=claim.kind, exists=False, detail=COULD_NOT_CHECK)
    return verdict


def verify_claims(claims: Sequence[Claim], verifier: ArtifactVerifier) -> dict[str, Any]:
    """Check every distinct claim exactly once and report the verdicts.

    At most one verifier call per distinct ``(kind, value, repo)``: a summary
    that names its branch five times must not cost five network calls. A
    verifier that raises fails *open* — the run is never failed by this check,
    the claim is simply recorded as unverified.
    """
    if not claims:
        return no_claim_report()
    verdicts = [_verify_one(claim, verifier) for claim in claims]
    failed = [verdict for verdict in verdicts if not verdict.exists]
    report: dict[str, Any] = {
        "verifiable": True,
        "verified": not failed,
        "artifacts": [verdict.as_dict() for verdict in verdicts],
    }
    if failed:
        report["reason"] = HOP_REASON_UNVERIFIABLE
        report["detail"] = "; ".join(
            verdict.detail or f"claimed {verdict.kind} {verdict.claimed} does not exist" for verdict in failed
        )
    return report


def verification_for(result: Any, verifier: ArtifactVerifier) -> dict[str, Any]:
    """The whole gate for one finished worker's result; never raises.

    Extraction itself is local (regex over text the engine already holds), so
    a worker that claims nothing costs no call at all.
    """
    try:
        claims = extract_claims(result)
    except Exception as exc:
        return {
            "verifiable": True,
            "verified": False,
            "reason": HOP_REASON_UNVERIFIABLE,
            "detail": f"{COULD_NOT_CHECK}: {exc}",
        }
    if not claims:
        return no_claim_report()
    return verify_claims(claims, verifier)


# -------------------------------------------------------- the real verifier


def _run(command: Sequence[str], cwd: str | None, timeout_s: float) -> tuple[int, str]:
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        list(command),
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


@dataclass
class SubprocessVerifier:
    """git and gh, asked the narrowest question that settles each claim.

    - branch: ``git ls-remote --heads <remote> <branch>`` — empty output is the
      remote saying the ref is not there, which is the claim being tested.
    - PR: ``gh pr view <n> --repo <o>/<r> --json state,url``.
    - commit: ``git cat-file -e <sha>``.

    ``runner`` is injectable so the command construction can be tested without
    a subprocess; nothing here retries, because a verification is a
    point-in-time read and a retry would only blur a transient failure into a
    confident answer.
    """

    remote: str = "origin"
    cwd: str | None = None
    timeout_s: float = 20.0
    runner: Callable[[Sequence[str]], tuple[int, str]] | None = None

    def _exec(self, command: Sequence[str]) -> tuple[int, str]:
        if self.runner is not None:
            return self.runner(command)
        return _run(command, self.cwd, self.timeout_s)

    def __call__(self, claim: Claim) -> Verdict:
        if claim.kind == KIND_BRANCH:
            return self._branch(claim)
        if claim.kind == KIND_PR:
            return self._pr(claim)
        if claim.kind == KIND_COMMIT:
            return self._commit(claim)
        return Verdict(
            claimed=claim.value,
            kind=claim.kind,
            exists=False,
            detail=f"{COULD_NOT_CHECK}: unknown artifact kind {claim.kind!r}",
        )

    def _branch(self, claim: Claim) -> Verdict:
        code, output = self._exec(["git", "ls-remote", "--heads", self.remote, claim.value])
        if code != 0:
            return Verdict(claim.value, claim.kind, False, f"{COULD_NOT_CHECK}: git ls-remote failed: {output.strip()}")
        exists = bool(output.strip())
        detail = "" if exists else f"branch {claim.value} is not on {self.remote}"
        return Verdict(claim.value, claim.kind, exists, detail)

    def _pr(self, claim: Claim) -> Verdict:
        if not claim.repo:
            return Verdict(claim.value, claim.kind, False, f"PR {claim.value} names no repository; cannot be resolved")
        number = claim.value.rsplit("#", 1)[-1]
        code, output = self._exec(["gh", "pr", "view", number, "--repo", claim.repo, "--json", "state,url"])
        if code != 0:
            return Verdict(claim.value, claim.kind, False, f"PR {claim.value} could not be read: {output.strip()}")
        return Verdict(claim.value, claim.kind, True, "")

    def _commit(self, claim: Claim) -> Verdict:
        code, output = self._exec(["git", "cat-file", "-e", claim.value])
        if code != 0:
            return Verdict(
                claim.value, claim.kind, False, f"commit {claim.value} is not a valid object: {output.strip()}"
            )
        return Verdict(claim.value, claim.kind, True, "")


def default_verifier() -> SubprocessVerifier:
    return SubprocessVerifier()
