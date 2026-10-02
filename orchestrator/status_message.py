"""Pure renderer for the live OMO status message: run state -> exact text.

The Discord message is the engine's only always-on surface, so its shape is a
tested artifact rather than a string built at the call site. This module knows
nothing about Discord, threads or the clock: it takes a plain run-state
structure and returns the string, which is what makes the format unit-testable
with no gateway in the loop.

A run state is a list of blocks, one per agent/worker in the run:

    {
        "agent": "hephaestus",               # the worker owning this block
        "process": "OKT-161 — shell parity", # run/task goal, truncated ~60 chars
        "phases": [
            {"name": "dispatch", "status": "done"},
            {"name": "run", "status": "current", "run_id": "omo_304bf8e5",
             "task_id": "t4", "detail": "wiring the nav badge semantics",
             "phrase": "patching components/shell.rs…"},
            {"name": "review", "status": "reviewing", "reviewer": "momus",
             "run_id": "omo_304bf8e5", "task_id": "t14:review"},
        ],
    }

Blocks are separated by one blank line. Every block carries the three phase
rows in order — dispatch, run, review — and the ``review`` row is *folded into
the producing worker's block*, naming the reviewer and verdict on the row; there
is never a separate review block. The current (🔁) run row carries the run id,
the task id and a short detail, and is followed by the indented rotating line.
"""

from __future__ import annotations

from typing import Any

from orchestrator.status_rotation import ROTATE_INTERVAL_SECONDS

# One emoji per phase status. The set is fixed by the legend; an unknown status
# renders as pending rather than as a blank, so a caller bug cannot silently
# drop a row.
STATUS_EMOJI: dict[str, str] = {
    "pending": "⏳",
    "current": "🔁",
    "running": "🔁",
    "done": "✅",
    "reviewing": "🔍",
    "blocked": "⛔",
    "interrupted": "⚠️",
    "failed": "❌",
    "cancelled": "⏸",
    "skipped": "⏭",
}

# A run row's status set, per the legend.
_RUNNING = ("current", "running")
_RUN_CAUSE = ("blocked", "interrupted", "failed", "cancelled")

HEADER_PREFIX = "🏗️ omo:"
DEFAULT_MAX_CHARS = 2000
DEFAULT_MAX_PROCESS_CHARS = 60
DEFAULT_MAX_DETAIL_CHARS = 48
DEFAULT_MAX_CAUSE_CHARS = 48

__all__ = [
    "DEFAULT_MAX_CAUSE_CHARS",
    "DEFAULT_MAX_CHARS",
    "DEFAULT_MAX_DETAIL_CHARS",
    "DEFAULT_MAX_PROCESS_CHARS",
    "HEADER_PREFIX",
    "PHASES",
    "STATUS_EMOJI",
    "render_block",
    "render_status",
]

# The phase pipeline, in order. Kept here so the renderer and the tracker agree
# on what a block's rows are.
PHASES = ("dispatch", "run", "review")


def _truncate(text: Any, limit: int) -> str:
    """Bound `text` to `limit` characters, naming the cut with an ellipsis."""
    value = str(text or "").strip()
    if limit <= 0 or not value:
        return ""
    if len(value) <= limit:
        return value
    if limit == 1:
        return "…"
    return value[: limit - 1].rstrip() + "…"


def _join(*parts: Any, sep: str = " · ") -> str:
    return sep.join(str(p).strip() for p in parts if str(p or "").strip())


def _run_extra(status: str, phase: dict[str, Any]) -> str:
    """The text after the run emoji: refs+detail while running, else cause/elapsed."""
    if status in _RUNNING:
        refs = _join(phase.get("run_id"), phase.get("task_id"))
        detail = _truncate(phase.get("detail"), DEFAULT_MAX_DETAIL_CHARS)
        if refs and detail:
            return f"{refs} — {detail}"
        return refs or detail
    if status == "done":
        return _truncate(phase.get("duration"), DEFAULT_MAX_CAUSE_CHARS)
    if status in _RUN_CAUSE:
        return _truncate(phase.get("cause") or phase.get("detail"), DEFAULT_MAX_CAUSE_CHARS)
    return ""


def _review_extra(status: str, phase: dict[str, Any]) -> str:
    """The text after the review emoji: reviewer and outcome, or the live refs."""
    reviewer = str(phase.get("reviewer") or "").strip()
    if status == "reviewing":
        return _join(reviewer, phase.get("run_id"), phase.get("task_id"))
    if status == "done":
        verdict = str(phase.get("verdict") or "").strip()
        named = _join(reviewer, verdict)
        cycle = phase.get("cycle")
        if verdict and cycle:
            return f"{named or verdict} (cycle {int(cycle)})"
        return named or verdict
    if status == "failed":
        return _join(reviewer, phase.get("verdict") or phase.get("cause") or "failed")
    return ""


def _row_extra(name: str, status: str, phase: dict[str, Any]) -> str:
    if name == "run":
        return _run_extra(status, phase)
    if name == "review":
        return _review_extra(status, phase)
    return ""


def _render_row(phase: dict[str, Any]) -> str:
    name = str(phase.get("name") or "?").strip().lower() or "?"
    status = str(phase.get("status") or "pending").strip().lower()
    emoji = STATUS_EMOJI.get(status, STATUS_EMOJI["pending"])
    row = f"- {name} {emoji}"
    extra = _row_extra(name, status, phase)
    if extra:
        row = f"{row} {extra}"
    return row


def _cycling_line(phase: dict[str, Any]) -> str:
    """The indented rotating line, present only under a running run row."""
    name = str(phase.get("name") or "").strip().lower()
    status = str(phase.get("status") or "").strip().lower()
    if name != "run" or status not in _RUNNING:
        return ""
    phrase = str(phase.get("phrase") or "").strip()
    if not phrase:
        return ""
    seconds = int(phase.get("rotate_seconds") or ROTATE_INTERVAL_SECONDS)
    return f"  ↳ cycling: {phrase} ({seconds}s)"


def render_block(block: dict[str, Any], *, row_budget: int | None = None) -> str:
    """Render one worker block. `row_budget` caps visible phase rows (>0 hidden)."""
    agent = str(block.get("agent") or "?").strip() or "?"
    process = _truncate(block.get("process") or block.get("goal"), DEFAULT_MAX_PROCESS_CHARS)
    header = f"{HEADER_PREFIX} {agent}"
    if process:
        header = f"{header}: {process}"

    phases = [p for p in (block.get("phases") or []) if isinstance(p, dict)]
    visible = phases if row_budget is None else phases[: max(0, row_budget)]

    lines = [header]
    for phase in visible:
        lines.append(_render_row(phase))
        cycling = _cycling_line(phase)
        if cycling:
            lines.append(cycling)

    hidden = len(phases) - len(visible)
    if hidden > 0:
        lines.append(f"…{hidden} more")
    return "\n".join(lines)


def render_status(blocks: Any, *, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Render every block, collapsing rows to stay under `max_chars` (Discord)."""
    clean = [b for b in (blocks or []) if isinstance(b, dict)]
    text = "\n\n".join(render_block(b) for b in clean)
    if len(text) <= max_chars:
        return text
    return _collapse(clean, max_chars)


def _collapse(blocks: list[dict[str, Any]], max_chars: int) -> str:
    """Shrink the widest block's visible rows until the message fits.

    Rows are dropped before whole blocks: a truncated phase list still names the
    agent and its outcome, while a dropped block hides a worker entirely. Only
    when even headers do not fit are trailing blocks folded into a count, and a
    final hard truncation guarantees Discord never receives an oversized body.
    """
    count = len(blocks)
    if count == 0:
        return ""

    budgets = [len([p for p in (b.get("phases") or []) if isinstance(p, dict)]) for b in blocks]
    while True:
        text = "\n\n".join(render_block(blocks[i], row_budget=budgets[i]) for i in range(count))
        if len(text) <= max_chars:
            return text
        widest = max(range(count), key=lambda i: budgets[i])
        if budgets[widest] == 0:
            break
        budgets[widest] -= 1

    visible = count
    while visible > 0:
        parts = [render_block(blocks[i], row_budget=0) for i in range(visible)]
        hidden = count - visible
        if hidden:
            parts.append(f"…{hidden} more agents")
        text = "\n\n".join(parts)
        if len(text) <= max_chars:
            return text
        visible -= 1

    # Headers alone still overflow (absurdly long names): hard-truncate rather
    # than hand Discord a message it will reject.
    text = "\n\n".join(f"{HEADER_PREFIX} …" for _ in range(count))
    if len(text) > max_chars:
        return text[: max(1, max_chars - 1)] + "…"
    return text
