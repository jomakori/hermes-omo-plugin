from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Question:
    id: str
    type: str
    instructions: str
    criteria: Any = None


@dataclass(frozen=True)
class Pack:
    name: str
    version: int
    questions: tuple[Question, ...]
    state_keys: tuple[str, ...]
    defaults: dict[str, Any] = field(default_factory=dict)


PACKS: dict[str, Pack] = {
    "route_intent": Pack(
        name="route_intent",
        version=1,
        questions=(
            Question(
                id="tier",
                type="choice",
                instructions="Classify the complexity tier of the user's request.",
                criteria={
                    "trivial": "Single lookup, copy/paste, or one-liner — no reasoning needed.",
                    "quick": "Small bounded task, clear goal, fits in one tool call.",
                    "scoped": "Multi-step but well-defined; scope is clear and contained.",
                    "exploratory": "Goal is clear but approach needs investigation first.",
                    "complex": "Multiple sub-problems, unclear interdependencies, needs planning.",
                    "ambiguous": "Goal itself is underspecified or contradictory.",
                },
            ),
            Question(
                id="domain",
                type="choice",
                instructions="Classify the primary domain of the user's request.",
                criteria={
                    "visual": "UI/UX, SVG, CSS, images, design assets.",
                    "logic": "Code, algorithms, data transformation, debugging.",
                    "writing": "Prose, docs, commit messages, PRs, summaries.",
                    "research": "Information lookup, web search, analysis.",
                    "git": "Git operations, branches, PRs, repos.",
                    "general": "Does not fit another domain cleanly.",
                },
            ),
        ),
        state_keys=("user_message", "last_question", "cwd_basename"),
        defaults={"tier": "scoped", "domain": "general"},
    ),
    "gate_risk": Pack(
        name="gate_risk",
        version=1,
        questions=(
            Question(
                id="irreversible",
                type="noul",
                instructions="Is the proposed action irreversible (cannot be undone without backup/rollback)?",
            ),
            Question(
                id="external_side_effect",
                type="noul",
                instructions="Does the proposed action have external side effects (network calls, emails)?",
            ),
            Question(
                id="destructive",
                type="noul",
                instructions="Is the proposed action destructive (deletes, overwrites, or drops data/files/records)?",
            ),
            Question(
                id="secrets_involved",
                type="noul",
                instructions="Does the proposed action involve secrets, credentials, or sensitive tokens?",
            ),
        ),
        state_keys=("action_text", "dry_run"),
        defaults={
            "irreversible": 0.0,
            "external_side_effect": 0.0,
            "destructive": 0.0,
            "secrets_involved": 0.0,
        },
    ),
    "pick_skill": Pack(
        name="pick_skill",
        version=1,
        questions=(
            Question(
                id="skill",
                type="choice",
                instructions="Select the most appropriate skill for the user's request from the provided shortlist.",
                criteria=None,
            ),
        ),
        state_keys=("user_request", "shortlist"),
        defaults={"skill": None},
    ),
    "pick_agent": Pack(
        name="pick_agent",
        version=1,
        questions=(
            Question(
                id="agent",
                type="choice",
                instructions="Which specialist agent or category should handle this request?",
                criteria=None,
            ),
        ),
        state_keys=("user_message", "last_question", "cwd_basename"),
        defaults={"agent": None},
    ),
}


# Per-agent/category capability hints: a short, differentiating one-liner used
# as the default description when a caller passes no per-task `descriptions`
# override to `agent_candidates`.
#
# OMR-12 (orchestrator observation): the roster's bare display string
# ("hephaestus · Deep Agent") carries no task-relevant signal, and feeding
# those generic labels to `pick_agent` collapsed a real routing decision to
# confidence 0.33 — below the 0.5 threshold, so advisory-only. Rebuilding the
# same decision with task-tuned one-line descriptions in the same call came
# back 1.0 confident. These hints are the floor every `pick_agent` call gets
# for free; a caller that knows the actual task should still pass
# `descriptions=` naming what THIS request needs, since nothing beats a hint
# written for the real request.
_CAPABILITY_HINTS: dict[str, str] = {
    "sisyphus": "primary ultraworker; multi-step execution, background subagent fan-out",
    "hephaestus": "deep agent for large or ambiguous code changes across multiple files",
    "prometheus": "builds a structured multi-step plan before any execution starts",
    "atlas": "executes an existing plan: ordered steps, verification gates",
    "metis": "advises on a plan already in progress; does not execute",
    "momus": "critiques a plan or diff; flags gaps, risks, missed cases",
    "oracle": "deep reasoning / architecture judgment on a hard open question",
    "librarian": "external research: web search, docs, information lookup",
    "explore": "repository exploration/search; answers 'where/what is X' in-repo",
    "multimodal-looker": "vision-capable: analyzes images, screenshots, audio",
    "sisyphus-junior": "focused single-task executor; narrow, bounded work only",
    "tester": "writes or extends automated tests for existing code",
    "debugger": "investigates a reported defect to find its root cause",
    "security": "reviews code or config for security issues",
    "deep": "category: routes to the deep-reasoning model chain",
    "quick": "category: routes to the fast/cheap model chain for small tasks",
    "ultrabrain": "category: routes to the top-tier reasoning model chain",
    "visual-engineering": "category: routes to vision-capable models",
    "writing": "category: routes to the prose/docs-tuned model chain",
}


def _default_description(name: str, fallback: str) -> str:
    """``fallback`` (the roster display, or the bare category name) plus this
    module's static capability hint when one exists for ``name`` — strictly
    additive, so any caller relying on the old bare string as a substring
    (e.g. matching on ``name · role``) still finds it.
    """
    hint = _CAPABILITY_HINTS.get(name)
    return f"{fallback} — {hint}" if hint else fallback


def agent_candidates(
    enabled: set[str] | None = None,
    *,
    descriptions: dict[str, str] | None = None,
    unavailable: set[str] | None = None,
) -> dict[str, str]:
    """The ``pick_agent`` option map: agents plus categories, at call time.

    Values differentiate real capability, not just name/role (OMR-12 — see
    ``_CAPABILITY_HINTS`` above for why). Resolution order per candidate:
    ``descriptions[name]`` (the caller's per-task description — always the
    best choice, since it can name exactly what THIS request needs) >
    this module's static ``_CAPABILITY_HINTS`` (a differentiated default,
    far better than bare "Name · Role" but not task-tuned) > the roster's
    bare display string / category name (last resort, only when a name has
    neither).

    ``enabled`` restricts the agent half to that set (an empty or None set
    means the whole roster).

    ``unavailable`` is the merged provider-health probe's dead set
    (``orchestrator.health_probe.alive_candidates``, PR #25) — any name in
    it is dropped here, before Jev ever sees it, so a currently
    quota/rate-limit-dead provider is never placed in Jev's criteria and can
    never be the pick, however well its task-fit would otherwise read.
    ``jev.routing.route_agent`` independently re-applies the same
    ``unavailable`` set as a second gate, so a caller that builds candidates
    here and still passes the raw (unfiltered) ``valid_targets`` elsewhere
    is not silently unprotected.

    The roster is never baked into the pack, the caller supplies this map as
    dynamic ``criteria`` exactly as ``pick_skill`` takes a shortlist. Clamped
    to 255 entries and 255-char strings.
    """
    from roster import AGENTS, CATEGORIES

    dead = {str(name) for name in (unavailable or ())}
    overrides = descriptions or {}
    candidates: dict[str, str] = {}
    for name, spec in AGENTS.items():
        if enabled is not None and name not in enabled:
            continue
        if name in dead:
            continue
        key = str(name)[:255]
        value = overrides.get(name) or _default_description(name, spec.display)
        candidates[key] = str(value)[:255]
    for name in CATEGORIES:
        if name in dead:
            continue
        key = str(name)[:255]
        value = overrides.get(name) or _default_description(name, name)
        candidates.setdefault(key, str(value)[:255])
    return dict(list(candidates.items())[:255])


__all__ = ["Question", "Pack", "PACKS", "agent_candidates"]
