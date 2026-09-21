from __future__ import annotations

from roster import PLAN_FAMILY, agent


class GuardError(RuntimeError):
    pass


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
