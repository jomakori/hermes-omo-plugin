"""Pure renderer for the live OMO status message: run state -> exact text.

The Discord message is the engine's only always-on surface, so its shape is a
tested artifact rather than a string built at the call site. This module knows
nothing about Discord, threads or the clock: it takes a plain run-state
structure and returns the string, which is what makes the format unit-testable
with no gateway in the loop.

A run state states the goal **once**, in the run heading::

    🏗️ omo · omo_304bf8e5 — shell parity across the fleet · 24 workers

and every worker below it carries a *condensed* label instead of a second copy
of the goal::

    {
        "run_id": "omo_304bf8e5",
        "goal": "shell parity across the fleet",
        "review": True,
        "workers": [
            {
                "label": "OKT-161 · shell parity",   # condensed `TICKET · title`, word-cut
                "display": "hephaestus · Deep Agent",  # roster AgentSpec.display
                "process": "OKT-161 — shell parity",   # raw task the label came from
                "title": "Add a provider-unusable hop reason",  # optional, caller-declared
                "review": True,                        # per-run review flag, read off the payload
                "activity": "📖 read_file orchestrator/status_message.py",  # live, optional
                "hop": "claude-sonnet-5 (rate limit)", # chain fallback, optional
                "phases": [
                    {"name": "dispatch", "status": "done"},
                    {"name": "run", "status": "current",
                     "model": "copilot-luna", "elapsed": "3m12s",
                     "phrase": "patching components/shell.rs…"},
                    {"name": "review", "status": "reviewing", "reviewer": "momus",
                     "task_id": "t14:review"},
                ],
            },
        ],
    }

Blocks are separated by one blank line. A block carries the three phase rows in
order — dispatch, run, review — with two deliberate omissions: the dispatch row
is dropped once it is ``done`` (the run row implies it), and the whole review row
is dropped when the run's ``review`` flag is false. The ``review`` row is *folded
into the producing worker's block*; there is never a separate review block. The
current (🔁) run row names the *work* — the caller-declared ``title``, else the
first clause derived from the task text, never the opaque task ref — plus the
serving model and the elapsed time, and is followed by the indented line carrying
the worker's real tool call (or the canned rotating phrase naming the work), with
a chain fallback named on the same line.

Truncation is always on a word boundary and never appends an ellipsis: the only
rendered ellipsis is the deliberate ``…N more`` collapse marker, which never
ends a line by itself.
"""

from __future__ import annotations

import re
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

# The run heading appears once per message; worker lines never repeat the goal.
HEADER_PREFIX = "🏗️ omo"
DEFAULT_MAX_CHARS = 2000
DEFAULT_MAX_GOAL_CHARS = 60
# A worker's condensed label: tighter once the run fans out wide, so a 24-worker
# run still fits Discord's cap with every worker's status rows intact.
DEFAULT_LABEL_CHARS = 44
COMPACT_LABEL_CHARS = 24
FANOUT_COMPACT_THRESHOLD = 8
DEFAULT_MAX_ACTIVITY_CHARS = 48
DEFAULT_MAX_CAUSE_CHARS = 48

# The tool-call line's own glyphs; the tool's emoji is resolved from the host
# registry at runtime (see :func:`tool_emoji`), never from a hardcoded map.
ACTIVITY_GLYPH = "↳"
HOP_GLYPH = "⤵"

# The phase pipeline, in order. Kept here so the renderer and the tracker agree
# on what a block's rows are.
PHASES = ("dispatch", "run", "review")

# A ticket id is the best label a task can offer: stable, short, and what the
# reader already tracks. `OKT-161`, `PROJ-7`, `A1-22`.
_TICKET = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d{1,5}\b")
# A ticket id leading the raw text, with its trailing whitespace.
_LEADING_TICKET = re.compile(r"^([A-Z][A-Z0-9]{1,9}-\d{1,5})\b\s*")
# A `(...)` aside right after the ticket (e.g. `OMR-11 (repo /x): ...`) — only a
# matched pair is stripped, so an unmatched `(` is left exactly where it is.
_PAREN_LEADING = re.compile(r"^\(([^()]*)\)\s*")
_LEADING_SEP = re.compile(r"^[\s:,\-–—]+")
# Otherwise the first clause is the label: cut at an em/en dash, a colon, or a
# sentence end. A bare hyphen is not a separator (`claude-sonnet-5` is a name).
_CLAUSE_SPLIT = re.compile(r"\s*(?:—|–|:)\s*|\s+[.?!]\s+")

__all__ = [
    "ACTIVITY_GLYPH",
    "COMPACT_LABEL_CHARS",
    "DEFAULT_LABEL_CHARS",
    "DEFAULT_MAX_ACTIVITY_CHARS",
    "DEFAULT_MAX_CAUSE_CHARS",
    "DEFAULT_MAX_CHARS",
    "DEFAULT_MAX_GOAL_CHARS",
    "FANOUT_COMPACT_THRESHOLD",
    "HEADER_PREFIX",
    "HOP_GLYPH",
    "PHASES",
    "STATUS_EMOJI",
    "condense_label",
    "label_limit",
    "render_activity",
    "render_block",
    "render_status",
    "tool_emoji",
]


def _cut(text: Any, limit: int) -> str:
    """Bound `text` to `limit` chars on a word boundary, with no ellipsis.

    A rendered line may never *end* in ``…`` (that is reserved for the deliberate
    ``…N more`` collapse marker), so an over-long value is trimmed to its last
    whole word rather than dotted out. A single token longer than the limit is
    sliced — still without an ellipsis.
    """
    value = str(text or "").strip()
    if limit <= 0 or not value:
        return ""
    if len(value) <= limit:
        return value
    head = value[:limit]
    if " " in head:
        candidate = head.rsplit(" ", 1)[0].rstrip()
        if candidate:
            return candidate
    return head.rstrip()


def _first_clause(value: str) -> str:
    head = _CLAUSE_SPLIT.split(value, maxsplit=1)[0].strip()
    return head or value


def _strip_leading_ticket_and_paren(value: str) -> str:
    """Drop a leading ticket-id token and a leading `(...)` aside, if present.

    Only a *matched* leading parenthetical is stripped — an unmatched `(` with
    no closing `)` is left exactly where it is.
    """
    value = _LEADING_TICKET.sub("", value, count=1)
    paren = _PAREN_LEADING.match(value)
    if paren:
        value = value[paren.end() :]
    return _LEADING_SEP.sub("", value)


def _derive_title(value: str) -> str:
    """The work's short title: leading ticket/aside stripped, first clause taken."""
    stripped = _strip_leading_ticket_and_paren(value)
    return _first_clause(stripped) if stripped else ""


def _balance_parens(text: str) -> str:
    """Drop a trailing `(` that a word-boundary cut left without its `)`."""
    if text.count("(") > text.count(")"):
        text = text[: text.rfind("(")].rstrip()
    return text


def _title_only(text: Any, title: Any = None) -> str:
    """Just the work's title, with no ticket prefix — what the run row names.

    The header already states `TICKET · title`; repeating the ticket on the run
    row would waste the row's own budget on something the reader already saw.
    """
    explicit = " ".join(str(title).split()) if title else ""
    if explicit:
        return explicit
    value = " ".join(str(text or "").split())
    if not value:
        return ""
    ticket = _TICKET.search(value)
    if ticket is not None and ticket.start() == 0:
        return _derive_title(value)
    return _first_clause(value)


def label_limit(fanout: int) -> int:
    """The condensed-label budget for a run of `fanout` workers."""
    return COMPACT_LABEL_CHARS if int(fanout or 0) > FANOUT_COMPACT_THRESHOLD else DEFAULT_LABEL_CHARS


def condense_label(text: Any, limit: int = DEFAULT_LABEL_CHARS, title: Any = None) -> str:
    """One short label for a worker: `TICKET · title` when a title is known.

    An explicit `title` (the dispatch task's own, caller-declared) always wins
    and is combined with a detected ticket id as `TICKET · title`. Absent one,
    a task whose text *leads* with a ticket id gets a title derived from what
    follows it (the ticket, and any leading `(...)` aside, stripped — then the
    first clause). A ticket that is not leading, or no ticket at all, falls
    back to the original condensed label (the bare ticket, or the first
    clause) rather than a noisier derived one. Cut on a word boundary — never
    mid-word, never with an ellipsis, never leaving an unmatched paren behind.
    """
    value = " ".join(str(text or "").split())
    ticket = _TICKET.search(value) if value else None
    ticket_text = ticket.group(0) if ticket else ""
    explicit = " ".join(str(title).split()) if title else ""
    if explicit:
        combined = f"{ticket_text} · {explicit}" if ticket_text else explicit
    elif ticket is not None and ticket.start() == 0:
        derived = _derive_title(value)
        combined = f"{ticket_text} · {derived}" if derived else ticket_text
    elif ticket_text:
        combined = ticket_text
    else:
        combined = _first_clause(value) if value else ""
    return _balance_parens(_cut(combined, limit))


def tool_emoji(tool_name: Any, *, default: str = "⚡", resolver: Any = None) -> str:
    """The emoji the host registry holds for `tool_name`, or `default`.

    Resolved at runtime from the host's own tool registry
    (``tools.registry.registry.get_emoji``) so a plugin never keeps a second,
    drifting map of tool emojis. A host without the registry (CLI, unit tests)
    or a registry that raises degrades to `default`.
    """
    name = str(tool_name or "").strip()
    if not name:
        return default
    if resolver is not None:
        try:
            return str(resolver(name, default) or default)
        except Exception:
            return default
    try:
        from tools.registry import registry  # noqa: PLC0415 - host-only, optional

        return str(registry.get_emoji(name, default) or default)
    except Exception:
        return default


def render_activity(
    tool_name: Any,
    target: Any = "",
    *,
    emoji_resolver: Any = None,
    limit: int = DEFAULT_MAX_ACTIVITY_CHARS,
) -> str:
    """`{emoji} {tool} {target}`, bounded to `limit` on a word boundary."""
    name = str(tool_name or "").strip()
    if not name:
        return ""
    emoji = tool_emoji(name, resolver=emoji_resolver)
    return _cut(_join(emoji, name, target, sep=" "), limit)


def _join(*parts: Any, sep: str = " · ") -> str:
    return sep.join(str(p).strip() for p in parts if str(p or "").strip())


def _run_extra(status: str, phase: dict[str, Any], model: str, label: str = "") -> str:
    """The text after the run emoji: title+model+elapsed while running, else cause/elapsed.

    The opaque ``task_id`` never appears here: a reader wants to know *what* is
    running and *how long* it has been running, not an internal reference.
    """
    if status in _RUNNING:
        serving = str(phase.get("model") or model or "").strip()
        head = _join(label, serving)
        elapsed = str(phase.get("elapsed") or "").strip()
        return f"{head} ({elapsed})" if head and elapsed else head
    if status == "done":
        return _cut(phase.get("duration"), DEFAULT_MAX_CAUSE_CHARS)
    if status in _RUN_CAUSE:
        return _cut(phase.get("cause") or phase.get("detail"), DEFAULT_MAX_CAUSE_CHARS)
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


def _row_extra(name: str, status: str, phase: dict[str, Any], model: str, label: str = "") -> str:
    if name == "run":
        return _run_extra(status, phase, model, label)
    if name == "review":
        return _review_extra(status, phase)
    if name == "dispatch" and status in _RUN_CAUSE:
        return _cut(phase.get("cause") or phase.get("detail"), DEFAULT_MAX_CAUSE_CHARS)
    return ""


def _render_row(phase: dict[str, Any], model: str, label: str = "") -> str:
    name = str(phase.get("name") or "?").strip().lower() or "?"
    status = str(phase.get("status") or "pending").strip().lower()
    emoji = STATUS_EMOJI.get(status, STATUS_EMOJI["pending"])
    row = f"- {name} {emoji}"
    extra = _row_extra(name, status, phase, model, label)
    if extra:
        row = f"{row} {extra}"
    return row


def _activity_line(phase: dict[str, Any], block: dict[str, Any]) -> str:
    """The indented line under a running run row: the live call, or the canned phrase.

    The worker's own tool call wins when one is observable; otherwise the canned
    rotating phrase keeps the line alive (it is never blank). A chain fallback is
    named on the same line.
    """
    name = str(phase.get("name") or "").strip().lower()
    status = str(phase.get("status") or "").strip().lower()
    if name != "run" or status not in _RUNNING:
        return ""
    activity = str(block.get("activity") or "").strip()
    if activity:
        line = f"  {ACTIVITY_GLYPH} {activity}"
    else:
        phrase = str(phase.get("phrase") or "").strip()
        if not phrase:
            return ""
        seconds = int(phase.get("rotate_seconds") or ROTATE_INTERVAL_SECONDS)
        line = f"  {ACTIVITY_GLYPH} cycling: {phrase} ({seconds}s)"
    hop = str(block.get("hop") or "").strip()
    if hop:
        line = f"{line} · {HOP_GLYPH} {hop}"
    return line


def _row_visible(name: str, status: str, block: dict[str, Any]) -> bool:
    """Whether a phase row earns its line at all.

    Dispatch is implied by the run row once it is done, and a run with review
    switched off must render no review row at all (the flag is read off the
    payload, never inferred).
    """
    if name == "dispatch" and status == "done":
        return False
    if name == "review" and not bool(block.get("review", True)):
        return False
    return True


def render_block(block: dict[str, Any], *, row_budget: int | None = None) -> str:
    """Render one worker block. `row_budget` caps visible phase rows (>0 hidden)."""
    limit = int(block.get("label_limit") or DEFAULT_LABEL_CHARS)
    label = str(block.get("label") or "").strip()
    if not label:
        label = condense_label(block.get("process") or block.get("goal"), limit, title=block.get("title"))
    display = str(block.get("display") or "").strip() or str(block.get("agent") or "?").strip() or "?"
    model = str(block.get("model") or "").strip()
    header = _join(label, display) or "?"
    # The run row names the work itself, never the header's ticket again: just
    # the title, word-cut to the activity budget so it never blows the line.
    row_label = _cut(
        _title_only(block.get("process") or block.get("goal"), block.get("title")), DEFAULT_MAX_ACTIVITY_CHARS
    )

    phases = [p for p in (block.get("phases") or []) if isinstance(p, dict)]
    visible = phases if row_budget is None else phases[: max(0, row_budget)]

    lines = [header]
    for phase in visible:
        name = str(phase.get("name") or "?").strip().lower() or "?"
        status = str(phase.get("status") or "pending").strip().lower()
        if not _row_visible(name, status, block):
            continue
        lines.append(_render_row(phase, model, row_label))
        activity = _activity_line(phase, block)
        if activity:
            lines.append(activity)

    hidden = len(phases) - len(visible)
    if hidden > 0:
        lines.append(f"…{hidden} more")
    return "\n".join(lines)


def _heading(run: dict[str, Any]) -> str:
    run_id = str(run.get("run_id") or "").strip()
    goal = _cut(run.get("goal"), DEFAULT_MAX_GOAL_CHARS)
    if not run_id and not goal:
        return ""
    workers = [w for w in (run.get("workers") or []) if isinstance(w, dict)]
    head = HEADER_PREFIX
    if run_id:
        head = f"{head} · {run_id}"
    if goal:
        head = f"{head} — {goal}"
    if len(workers) > 1:
        head = f"{head} · {len(workers)} workers"
    return head


def render_status(run: Any, *, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Render the run heading plus every worker block, collapsing to fit Discord."""
    run = run if isinstance(run, dict) else {}
    workers = [b for b in (run.get("workers") or []) if isinstance(b, dict)]
    heading = _heading(run)
    body = "\n\n".join(render_block(b) for b in workers)
    if body:
        text = f"{heading}\n\n{body}" if heading else body
    else:
        text = heading
    if len(text) <= max_chars:
        return text
    return _collapse(run, workers, max_chars)


def _collapse(run: dict[str, Any], blocks: list[dict[str, Any]], max_chars: int) -> str:
    """Shrink visible rows — then whole blocks — until the message fits.

    Rows are dropped before whole blocks: a truncated phase list still names the
    worker and its outcome, while a dropped block hides a worker entirely. Only
    when even headers do not fit are trailing blocks folded into a count. Nothing
    is ever dot-dotted out: the sole ellipsis is the ``…N more`` marker.
    """
    count = len(blocks)
    heading = _heading(run)
    if count == 0:
        return _cut(run.get("goal"), max_chars)
    budgets = [len([p for p in (b.get("phases") or []) if isinstance(p, dict)]) for b in blocks]
    while True:
        parts = [render_block(blocks[i], row_budget=budgets[i]) for i in range(count)]
        text = _compose(heading, parts)
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
        text = _compose(heading, parts)
        if len(text) <= max_chars:
            return text
        visible -= 1

    # Headers alone still overflow (absurdly long labels): trim the heading and
    # return it rather than hand Discord an oversized body.
    return _cut(heading, max_chars)


def _compose(heading: str, parts: list[str]) -> str:
    body = "\n\n".join(parts)
    if body:
        return f"{heading}\n\n{body}" if heading else body
    return heading
