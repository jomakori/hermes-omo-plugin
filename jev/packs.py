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


def agent_candidates(enabled: set[str] | None = None) -> dict[str, str]:
    """The ``pick_agent`` option map: agents plus categories, at call time.

    Values are the roster's short display (``name · role``); categories map to
    their own name. ``enabled`` restricts the agent half to that set (an empty or
    None set means the whole roster) — the roster is never baked into the pack, the
    caller supplies this map as dynamic ``criteria`` exactly as ``pick_skill``
    takes a shortlist. Clamped to 255 entries and 255-char strings.
    """
    from roster import AGENTS, CATEGORIES

    candidates: dict[str, str] = {}
    for name, spec in AGENTS.items():
        if enabled is not None and name not in enabled:
            continue
        candidates[str(name)[:255]] = str(spec.display)[:255]
    for name in CATEGORIES:
        candidates.setdefault(str(name)[:255], str(name)[:255])
    return dict(list(candidates.items())[:255])


__all__ = ["Question", "Pack", "PACKS", "agent_candidates"]
