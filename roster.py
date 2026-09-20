"""OMO agent roster — who exists, what they may touch, and their model chains.

Ported from oh-my-openagent @ e614da3 (5.0.0-beta.79). Personas live as markdown
under ``agents/<name>.md``. Model chains here are defaults; the chart's ``omo:``
values override them at runtime, so ops retune without shipping a new plugin.

Two Hermes constraints shape this module (both verified against v2026.9.11):
read-only agents cannot be expressed with toolsets because ``file`` bundles
read_file + write_file + patch and ``SubagentLaunchRequest.blocked_tools`` is
rejected at launch — they are paired with a pre_tool_call guard instead; and MCP
servers are toolsets named ``mcp-<server>``, which is how plugin/github access is
scoped to the orchestrator tier.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

READ_ONLY_TOOLSETS: tuple[str, ...] = ("file", "search", "web", "vision", "session_search")
ENGINEERING_TOOLSETS: tuple[str, ...] = ("file", "search", "web", "terminal", "todo", "skills")
ORCHESTRATOR_TOOLSETS: tuple[str, ...] = ENGINEERING_TOOLSETS + ("delegation", "kanban")

MCP_TOOLSETS: tuple[str, ...] = ("mcp-plane", "mcp-github")


@dataclass(frozen=True)
class AgentSpec:
    name: str
    role: str
    mode: Literal["primary", "subagent"]
    toolsets: tuple[str, ...]
    chain: tuple[str, ...]
    read_only: bool = False
    orchestrator: bool = False
    mcp: tuple[str, ...] = ()
    accepts_subagent_type: bool = True

    @property
    def display(self) -> str:
        return f"{self.name} · {self.role}"

    def effective_toolsets(self, *, mcp_enabled: bool = True) -> tuple[str, ...]:
        merged = list(self.toolsets) + (list(self.mcp) if mcp_enabled else [])
        seen: set[str] = set()
        return tuple(toolset for toolset in merged if not (toolset in seen or seen.add(toolset)))


L = "litellm/"

AGENTS: dict[str, AgentSpec] = {
    "sisyphus": AgentSpec(
        "sisyphus",
        "Ultraworker",
        "primary",
        ORCHESTRATOR_TOOLSETS,
        (L + "deepseek-v4-flash-direct", L + "claude-sonnet-5", L + "bytedance-glm-5.2"),
        orchestrator=True,
        mcp=MCP_TOOLSETS,
    ),
    "hephaestus": AgentSpec(
        "hephaestus",
        "Deep Agent",
        "primary",
        ENGINEERING_TOOLSETS,
        (L + "minimax-m3", L + "glm-5.3", L + "claude-sonnet-5", L + "bytedance-dola-seed-2.0-code"),
        mcp=MCP_TOOLSETS,
    ),
    "prometheus": AgentSpec(
        "prometheus",
        "Plan Builder",
        "primary",
        READ_ONLY_TOOLSETS,
        (L + "minimax-m3", L + "deepseek-v4-pro", L + "claude-sonnet-5", L + "bytedance-glm-5.2"),
        read_only=True,
        mcp=MCP_TOOLSETS,
    ),
    "atlas": AgentSpec(
        "atlas",
        "Plan Executor",
        "primary",
        ORCHESTRATOR_TOOLSETS,
        (L + "minimax-m3", L + "deepseek-v4-pro", L + "claude-sonnet-5", L + "bytedance-dola-seed-2.0-pro"),
        orchestrator=True,
        mcp=MCP_TOOLSETS,
    ),
    "metis": AgentSpec(
        "metis",
        "Plan Consultant",
        "subagent",
        READ_ONLY_TOOLSETS,
        (L + "minimax-m3", L + "deepseek-v4-pro", L + "claude-sonnet-5", L + "bytedance-glm-5.2"),
        read_only=True,
    ),
    "momus": AgentSpec(
        "momus",
        "Plan Critic",
        "subagent",
        READ_ONLY_TOOLSETS,
        (L + "minimax-m3", L + "glm-5.3", L + "claude-sonnet-5", L + "bytedance-dola-seed-2.0-pro"),
        read_only=True,
    ),
    "oracle": AgentSpec(
        "oracle",
        "Architecture / Reasoning",
        "subagent",
        READ_ONLY_TOOLSETS,
        (L + "claude-opus-5", L + "glm-5.3", L + "bytedance-seed-code"),
        read_only=True,
    ),
    "librarian": AgentSpec(
        "librarian",
        "Research",
        "subagent",
        READ_ONLY_TOOLSETS,
        (L + "deepseek-v4-flash", L + "minimax-m3", L + "claude-haiku-4-5", L + "bytedance-deepseek-v4-flash"),
        read_only=True,
    ),
    "explore": AgentSpec(
        "explore",
        "Repository Exploration",
        "subagent",
        READ_ONLY_TOOLSETS,
        (L + "deepseek-v4-flash", L + "minimax-m3", L + "claude-haiku-4-5", L + "bytedance-deepseek-v4-flash"),
        read_only=True,
    ),
    "multimodal-looker": AgentSpec(
        "multimodal-looker",
        "Multimodal Analysis",
        "subagent",
        ("file", "vision"),
        (L + "gemini-3.6-flash", L + "qwen3-6-plus", L + "claude-sonnet-5", L + "bytedance-seed-code"),
        read_only=True,
    ),
    "sisyphus-junior": AgentSpec(
        "sisyphus-junior",
        "Specialized Execution Worker",
        "subagent",
        ENGINEERING_TOOLSETS,
        (L + "deepseek-v4-flash", L + "minimax-m3", L + "claude-haiku-4-5", L + "bytedance-deepseek-v4-flash"),
        accepts_subagent_type=False,
    ),
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


def read_only_tool_names() -> frozenset[str]:
    return frozenset({"write_file", "patch"})
