from __future__ import annotations

import threading
from dataclasses import dataclass, field

from roster import PLAN_FAMILY, agent, read_only_tool_names


class GuardError(RuntimeError):
    pass


@dataclass
class ReadOnlyRegistry:
    sessions: dict[str, str] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def mark(self, session_id: str, agent_name: str) -> None:
        if not session_id:
            return
        with self._lock:
            self.sessions[session_id] = agent_name

    def clear(self, session_id: str) -> None:
        with self._lock:
            self.sessions.pop(session_id, None)

    def is_read_only(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self.sessions


READ_ONLY_WORKERS = ReadOnlyRegistry()


def read_only_pre_tool_call(*, tool_name: str = "", session_id: str = "", **_kwargs) -> dict | None:
    # Read-only agents keep the full `file` toolset, so the write tools must be
    # vetoed per session; the session->agent map is filled from subagent_start.
    if tool_name in read_only_tool_names() and READ_ONLY_WORKERS.is_read_only(session_id):
        return {
            "action": "block",
            "message": f"{tool_name} is denied: this agent is read-only (OMO roster restriction).",
        }
    return None


def check_delegation(*, target: str | None, category: str | None, parent_agent: str | None = None) -> str:
    if bool(target) == bool(category):
        raise GuardError("Provide exactly one of agent= or category=.")
    if category is not None:
        return "sisyphus-junior"
    assert target is not None
    spec = agent(target)
    if spec is None:
        raise GuardError(f"Unknown agent '{target}'.")
    if not spec.accepts_subagent_type:
        raise GuardError(f"'{target}' cannot be a direct target — use category= instead.")
    if parent_agent in PLAN_FAMILY and target in PLAN_FAMILY:
        raise GuardError("Plan-family agents cannot delegate to plan-family agents.")
    return target
