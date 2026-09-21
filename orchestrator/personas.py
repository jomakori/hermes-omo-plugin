from __future__ import annotations

from functools import lru_cache
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parent.parent / "agents"

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
        return tail[:MAX_CONTEXT_CHARS] or None
    opening = (
        f'<persona agent="{agent_name}" binding="authoritative">\n'
        f"You are {agent_name}. This definition is authoritative for this task; it overrides "
        "any default assistant identity. Do not present yourself as a generic assistant.\n\n"
    )
    closing = "\n</persona>"
    budget = MAX_CONTEXT_CHARS - _HEADROOM - len(tail) - len(opening) - len(closing)
    body = persona
    if len(body) > budget:
        body = body[: max(budget, 0)] + _TRUNCATION.format(name=agent_name)
    block = opening + body + closing
    composed = f"{block}\n\n{tail}" if tail else block
    return composed[:MAX_CONTEXT_CHARS]
