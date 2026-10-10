"""Rotation cadence and cycling phrases for the live status message.

The message must visibly *live* while a worker runs: between real transitions
the ``  ↳ cycling: <phrase> (<n>s)`` line is re-rendered on this cadence, walking
through a small phrase set so the edit is never a no-op. Both the interval and
the phrase set are module-level constants (configuration is not required yet),
and this module does no I/O, so rotation is unit-testable on its own.

The cycling line must never echo the raw ticket/task text verbatim (the header
already carries the ticket), and it must never be cut mid-token: a phrase that
would overflow its budget is trimmed to its last whole word, same discipline as
``status_message._cut``.
"""

from __future__ import annotations

import re

# The cadence the cycling line is re-rendered at, in seconds. It is part of the
# rendered line ("(3s)") and of the tracker's rotation timer.
ROTATE_INTERVAL_SECONDS = 3.0

# Cycling phrases per phase. Small, human, and observed to change so the message
# reads as alive between transitions rather than frozen on the last event.
CYCLING_PHRASES: dict[str, tuple[str, ...]] = {
    "dispatch": (
        "handing off to the worker…",
        "opening the session…",
    ),
    "run": (
        "reading the code…",
        "patching the working tree…",
        "wiring the change…",
        "running the checks…",
    ),
    "review": (
        "reviewing the change…",
        "weighing the risks…",
        "checking the edges…",
    ),
}

# A ticket id leading the raw task text (`OMR-11 (repo /x): do the thing`) is
# already shown in the header; a cycling phrase must name the work, not repeat
# the ticket. Duplicated from status_message's `_TICKET`/`_LEADING_TICKET`
# rather than imported, so this module keeps doing no I/O and no cross-import
# (status_tracker already imports both modules; this one stays a leaf).
_LEADING_TICKET = re.compile(r"^[A-Z][A-Z0-9]{1,9}-\d{1,5}\b\s*")
_CLAUSE_SPLIT = re.compile(r"\s*(?:—|–|:)\s*|\s+[.?!]\s+")
_LEADING_SEP = re.compile(r"^[\s:,\-–—]+")

# How long the work-title fragment folded into a phrase may run before it is
# word-cut; keeps the combined line inside `_PHRASE_LINE_BUDGET`.
_MAX_TITLE_CHARS = 20
# The phrase's own budget once combined with a title. The rendered line is
# "  ↳ cycling: {phrase} (Ns)" — a 13-char prefix and up to a 6-char suffix —
# so a phrase at this budget keeps the whole line within the 48-char activity
# cap asserted across the test suite.
_PHRASE_LINE_BUDGET = 29


def _word_cut(value: str, limit: int) -> str:
    """Bound `value` to `limit` chars on a word boundary, never mid-token."""
    value = value.strip()
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


def derive_title(task: str) -> str:
    """A short fragment of `task` naming the work: leading ticket/paren stripped.

    Drops a leading ticket-id token and a leading ``(...)`` aside (the header's
    label already carries those), then takes the first clause and word-cuts it
    to `_MAX_TITLE_CHARS`. Used both by the cycling phrase and by the renderer's
    label fallback, so the two never disagree about what "the work" is called.
    """
    value = " ".join(str(task or "").split())
    if not value:
        return ""
    value = _LEADING_TICKET.sub("", value, count=1)
    if value.startswith("("):
        close = value.find(")")
        if close != -1:
            value = value[close + 1 :]
    value = _LEADING_SEP.sub("", value)
    if not value:
        return ""
    clause = _CLAUSE_SPLIT.split(value, maxsplit=1)[0].strip() or value
    return _word_cut(clause, _MAX_TITLE_CHARS)


def cycling_phrase(
    phase: str,
    index: int,
    activity: str | None = None,
    task: str | None = None,
    title: str | None = None,
) -> str:
    """Observed activity wins; otherwise a phase-aware phrase naming the work.

    `title` is the caller's already-derived label body (explicit task title, or
    the renderer's condensed label); when absent it is derived here from `task`.
    A phrase never echoes the raw task text (ticket id and all) and is never cut
    mid-word.
    """
    if activity is not None and str(activity).strip():
        return str(activity).strip()
    options = CYCLING_PHRASES.get(phase) or CYCLING_PHRASES["run"]
    body = " ".join(str(title).split()) if title else ""
    if not body and task:
        body = derive_title(task)
    if body:
        body = _word_cut(body, _MAX_TITLE_CHARS)
        options = tuple(_word_cut(f"{phrase.rstrip('…').rstrip()}: {body}", _PHRASE_LINE_BUDGET) for phrase in options)
    if not options:
        return ""
    return options[int(index) % len(options)]


__all__ = ["CYCLING_PHRASES", "ROTATE_INTERVAL_SECONDS", "cycling_phrase", "derive_title"]
