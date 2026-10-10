"""OMO agent roster — who exists and their model chains.

Ported from oh-my-openagent @ e614da3 (5.0.0-beta.79). Personas live as markdown
under ``agents/<name>.md``. Model chains here are defaults; the chart's ``omo:``
values override them at runtime, so ops retune without shipping a new plugin.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class AgentSpec:
    name: str
    role: str
    mode: Literal["primary", "subagent"]
    chain: tuple[str, ...]
    orchestrator: bool = False
    accepts_subagent_type: bool = True

    @property
    def display(self) -> str:
        return f"{self.name} · {self.role}"


L = "litellm/"

AGENTS: dict[str, AgentSpec] = {
    "sisyphus": AgentSpec(
        "sisyphus",
        "Ultraworker",
        "primary",
        (L + "copilot-luna", L + "claude-sonnet-5", L + "copilot-grok", L + "deepseek-v4-flash-direct"),
        orchestrator=True,
    ),
    "hephaestus": AgentSpec(
        "hephaestus",
        "Deep Agent",
        "primary",
        (
            L + "copilot-luna",
            L + "copilot-codex",
            L + "claude-sonnet-5",
            L + "copilot-mai-code",
            L + "deepseek-v4-flash-direct",
        ),
    ),
    "prometheus": AgentSpec(
        "prometheus",
        "Plan Builder",
        "primary",
        (L + "copilot-luna", L + "claude-sonnet-5", L + "copilot-grok", L + "deepseek-v4-flash-direct"),
    ),
    "atlas": AgentSpec(
        "atlas",
        "Plan Executor",
        "primary",
        (L + "copilot-luna", L + "claude-sonnet-5", L + "deepseek-v4-flash-direct"),
        orchestrator=True,
    ),
    "metis": AgentSpec(
        "metis",
        "Plan Consultant",
        "subagent",
        (L + "copilot-luna", L + "claude-sonnet-5", L + "copilot-mai-code", L + "deepseek-v4-flash-direct"),
    ),
    "momus": AgentSpec(
        "momus",
        "Plan Critic",
        "subagent",
        (L + "copilot-luna", L + "claude-sonnet-5", L + "copilot-gemini-3.8-flash", L + "deepseek-v4-flash-direct"),
    ),
    "oracle": AgentSpec(
        "oracle",
        "Architecture / Reasoning",
        "subagent",
        (L + "claude-opus-5", L + "copilot-sonnet-5.5", L + "deepseek-v4-flash-direct"),
    ),
    "librarian": AgentSpec(
        "librarian",
        "Research",
        "subagent",
        (L + "deepseek-v4-flash-direct", L + "copilot-luna", L + "claude-haiku-4-5", L + "deepseek-v4-flash-direct"),
    ),
    "explore": AgentSpec(
        "explore",
        "Repository Exploration",
        "subagent",
        (L + "deepseek-v4-flash-direct", L + "copilot-luna", L + "claude-haiku-4-5", L + "deepseek-v4-flash-direct"),
    ),
    "multimodal-looker": AgentSpec(
        "multimodal-looker",
        "Multimodal Analysis",
        "subagent",
        (L + "claude-sonnet-5", L + "copilot-gemini-3.8-flash", L + "claude-haiku-4-5", L + "deepseek-v4-flash-direct"),
    ),
    "sisyphus-junior": AgentSpec(
        "sisyphus-junior",
        "Specialized Execution Worker",
        "subagent",
        (L + "copilot-luna", L + "claude-haiku-4-5", L + "deepseek-v4-flash-direct"),
        accepts_subagent_type=False,
    ),
    "tester": AgentSpec(
        "tester",
        "Test Author",
        "subagent",
        (L + "copilot-luna", L + "claude-sonnet-5", L + "deepseek-v4-flash-direct"),
    ),
    "debugger": AgentSpec(
        "debugger",
        "Defect Investigator",
        "subagent",
        (L + "copilot-luna", L + "copilot-codex", L + "claude-sonnet-5", L + "copilot-mai-code"),
    ),
    "security": AgentSpec(
        "security",
        "Security Reviewer",
        "subagent",
        (L + "copilot-luna", L + "claude-sonnet-5", L + "copilot-grok"),
    ),
}

# Request-facing role names. The roster keeps OMO's codenames; callers may ask for
# a role instead. Overridable at runtime through the ``roles`` setting.
ROLE_ALIASES: dict[str, str] = {
    "explorer": "explore",
    "researcher": "librarian",
    "planner": "prometheus",
    "implementer": "hephaestus",
    "tester": "tester",
    "debugger": "debugger",
    "reviewer": "momus",
    "security": "security",
    "documenter": "writing",
    # sisyphus-junior refuses to be a direct target, so the general role goes
    # through the category path that reaches it.
    "general": "quick",
}

CATEGORIES: dict[str, tuple[str, ...]] = {
    "deep": (L + "copilot-luna", L + "claude-sonnet-5", L + "copilot-grok", L + "deepseek-v4-flash-direct"),
    "quick": (
        L + "deepseek-v4-flash-direct",
        L + "copilot-luna",
        L + "claude-haiku-4-5",
        L + "deepseek-v4-flash-direct",
    ),
    "ultrabrain": (L + "claude-opus-5", L + "copilot-sonnet-5.5", L + "deepseek-v4-flash-direct"),
    "visual-engineering": (
        L + "claude-sonnet-5",
        L + "copilot-gemini-3.8-flash",
        L + "claude-haiku-4-5",
        L + "deepseek-v4-flash-direct",
    ),
    "writing": (
        L + "copilot-luna",
        L + "claude-sonnet-5",
        L + "copilot-gemini-3.8-flash",
        L + "deepseek-v4-flash-direct",
    ),
}


PLAN_FAMILY: frozenset[str] = frozenset({"plan", "prometheus"})


def agent(name: str) -> AgentSpec | None:
    return AGENTS.get(name)
