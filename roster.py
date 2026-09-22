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
        (L + "deepseek-v4-flash-direct", L + "claude-sonnet-5", L + "bytedance-glm-5.2"),
        orchestrator=True,
    ),
    "hephaestus": AgentSpec(
        "hephaestus",
        "Deep Agent",
        "primary",
        (L + "minimax-m3", L + "glm-5.3", L + "claude-sonnet-5", L + "bytedance-dola-seed-2.0-code"),
    ),
    "prometheus": AgentSpec(
        "prometheus",
        "Plan Builder",
        "primary",
        (L + "minimax-m3", L + "deepseek-v4-pro", L + "claude-sonnet-5", L + "bytedance-glm-5.2"),
    ),
    "atlas": AgentSpec(
        "atlas",
        "Plan Executor",
        "primary",
        (L + "minimax-m3", L + "deepseek-v4-pro", L + "claude-sonnet-5", L + "bytedance-dola-seed-2.0-pro"),
        orchestrator=True,
    ),
    "metis": AgentSpec(
        "metis",
        "Plan Consultant",
        "subagent",
        (L + "minimax-m3", L + "deepseek-v4-pro", L + "claude-sonnet-5", L + "bytedance-glm-5.2"),
    ),
    "momus": AgentSpec(
        "momus",
        "Plan Critic",
        "subagent",
        (L + "minimax-m3", L + "glm-5.3", L + "claude-sonnet-5", L + "bytedance-dola-seed-2.0-pro"),
    ),
    "oracle": AgentSpec(
        "oracle",
        "Architecture / Reasoning",
        "subagent",
        (L + "claude-opus-5", L + "glm-5.3", L + "bytedance-seed-code"),
    ),
    "librarian": AgentSpec(
        "librarian",
        "Research",
        "subagent",
        (L + "deepseek-v4-flash", L + "minimax-m3", L + "claude-haiku-4-5", L + "bytedance-deepseek-v4-flash"),
    ),
    "explore": AgentSpec(
        "explore",
        "Repository Exploration",
        "subagent",
        (L + "deepseek-v4-flash", L + "minimax-m3", L + "claude-haiku-4-5", L + "bytedance-deepseek-v4-flash"),
    ),
    "multimodal-looker": AgentSpec(
        "multimodal-looker",
        "Multimodal Analysis",
        "subagent",
        (L + "gemini-3.6-flash", L + "qwen3-6-plus", L + "claude-sonnet-5", L + "bytedance-seed-code"),
    ),
    "sisyphus-junior": AgentSpec(
        "sisyphus-junior",
        "Specialized Execution Worker",
        "subagent",
        (L + "deepseek-v4-flash", L + "minimax-m3", L + "claude-haiku-4-5", L + "bytedance-deepseek-v4-flash"),
        accepts_subagent_type=False,
    ),
    "tester": AgentSpec(
        "tester",
        "Test Author",
        "subagent",
        (L + "deepseek-v4-flash", L + "minimax-m3", L + "claude-sonnet-5", L + "bytedance-dola-seed-2.0-code"),
    ),
    "debugger": AgentSpec(
        "debugger",
        "Defect Investigator",
        "subagent",
        (L + "minimax-m3", L + "deepseek-v4-pro", L + "claude-sonnet-5", L + "bytedance-dola-seed-2.0-pro"),
    ),
    "security": AgentSpec(
        "security",
        "Security Reviewer",
        "subagent",
        (L + "deepseek-v4-pro", L + "claude-sonnet-5", L + "glm-5.3", L + "bytedance-dola-seed-2.0-pro"),
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
    "general": "sisyphus-junior",
}

CATEGORIES: dict[str, tuple[str, ...]] = {
    "quick": (L + "deepseek-v4-flash", L + "minimax-m3", L + "claude-haiku-4-5"),
    "deep": (L + "minimax-m3", L + "glm-5.3", L + "claude-sonnet-5", L + "bytedance-dola-seed-2.0-pro"),
    "ultrabrain": (L + "claude-opus-5", L + "glm-5.3", L + "bytedance-seed-code"),
    "visual-engineering": (L + "gemini-3.6-flash", L + "qwen3-6-plus", L + "claude-sonnet-5"),
    "writing": (L + "minimax-m3", L + "claude-sonnet-5"),
}

PLAN_FAMILY: frozenset[str] = frozenset({"plan", "prometheus"})


def agent(name: str) -> AgentSpec | None:
    return AGENTS.get(name)
