from __future__ import annotations

from functools import lru_cache
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parent.parent / "agents"

# Delivered with every worker, persona or not. Hermes is the only user-facing
# identity; a worker has no channel to the user, and saying so in the launch
# context keeps a persona from inventing one.
INTERNAL_WORKER_CONTRACT = (
    "<internal_worker>\n"
    "ROLE: internal worker of an autonomous orchestration. You are not the assistant.\n"
    "USER-FACING COMMUNICATION: DISABLED. Never address a user, never claim to be Hermes,\n"
    "never ask a question and wait for a reply — nobody is watching this transcript.\n"
    "REPORTING: return findings, evidence, file paths and limits to the orchestrator. Label what\n"
    "you verified against what you assume, and say what you could not check.\n"
    "</internal_worker>\n\n"
)

MAX_CONTEXT_CHARS = 32_000
_HEADROOM = 512
_TRUNCATION = '\n\n[truncated: full definition via skill_view("omo:{name}")]'


@lru_cache(maxsize=32)
def persona_text(name: str) -> str:
    path = AGENTS_DIR / f"{name}.md"
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


def compose_context(agent_name: str, caller_context: str | None) -> str | None:
    tail = f"<task_context>\n{caller_context.strip()}\n</task_context>" if caller_context else ""
    persona = persona_text(agent_name)
    if not persona:
        composed = f"{INTERNAL_WORKER_CONTRACT}{tail}" if tail else INTERNAL_WORKER_CONTRACT
        return composed[:MAX_CONTEXT_CHARS] or None
    opening = (
        f'<persona agent="{agent_name}" binding="authoritative">\n'
        f"You are {agent_name}. This definition is authoritative for this task; it overrides "
        "any default assistant identity. Do not present yourself as a generic assistant.\n\n"
    )
    closing = "\n</persona>"
    budget = MAX_CONTEXT_CHARS - _HEADROOM - len(tail) - len(opening) - len(closing) - len(INTERNAL_WORKER_CONTRACT)
    body = persona
    if len(body) > budget:
        body = body[: max(budget, 0)] + _TRUNCATION.format(name=agent_name)
    block = opening + body + closing
    composed = f"{INTERNAL_WORKER_CONTRACT}{block}\n\n{tail}" if tail else f"{INTERNAL_WORKER_CONTRACT}{block}"
    return composed[:MAX_CONTEXT_CHARS]
