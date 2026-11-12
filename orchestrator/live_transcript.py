"""Append-only JSONL live transcript for one OMO run.

Zero new dependencies: stdlib only. One file per run
(``~/.omo/live/<run_id>.jsonl``, see ``DEFAULT_LIVE_DIR``), written
incrementally as the run progresses, so a caller — or another agent — can
tail it while the run is still live instead of waiting for the final result.

A transcript failure must never break the run it describes: every write is
best-effort and swallows its own errors (see ``LiveTranscript.write``). Every
text field passes through the single redaction choke point ``_redact``
before it is ever written to disk.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_LIVE_DIR = "~/.omo/live"

# A transcript line is a short progress note, not a transcript of the work
# itself: bounded hard at this many characters after redaction.
_MAX_TEXT_CHARS = 500
_VALID_KINDS = ("launch", "status", "tool", "result", "error")

# The one place redaction happens — the only regex in this module that reads
# raw text. Matches `key[:=]value` for api_key/token/secret/password/
# authorization/bearer (case-insensitive), plus the two-word
# `Authorization: Bearer <tok>` shape specifically, so the scheme word reads
# in the log line but the token after it never does.
_REDACT_RE = re.compile(
    r"(?i)\b(api[-_]?key|token|secret|password|authorization|bearer)\b(\s*[:=]\s*)(bearer\s+\S+|\S+)"
)


def _redact(text: str) -> str:
    """Mask credential-shaped values. The only place this module redacts.

    Every ``LiveTranscript.write`` call funnels its text through here via
    ``_clean_text`` — there is no second path that writes a line to disk.
    """
    if not text:
        return text
    return _REDACT_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", text)


def _clean_text(text: Any) -> str:
    """One line, newlines collapsed, redacted, bounded to `_MAX_TEXT_CHARS`."""
    text = "" if text is None else str(text)
    text = " ".join(text.split())
    text = _redact(text)
    return text[:_MAX_TEXT_CHARS]


def transcript_path(run_id: str, dir: str | None = None) -> Path:
    """Where this run's transcript lives, expanded but not created."""
    base = Path(dir or DEFAULT_LIVE_DIR).expanduser()
    return base / f"{run_id}.jsonl"


class LiveTranscript:
    """Append-only JSONL writer/reader for one run's live transcript.

    Every method here is best-effort: a transcript is an observability side
    channel, never a dependency of the run it describes. ``write`` is the
    only method on the run's hot path and the only one that must never raise
    (see its own docstring); the others are either read-only or run once at
    construction.
    """

    def __init__(
        self,
        run_id: str,
        path: str | Path | None = None,
        dir: str | None = None,
        goal: str | None = None,
        title: str | None = None,
    ) -> None:
        self.run_id = run_id
        self.path = Path(path).expanduser() if path else transcript_path(run_id, dir)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.debug("live_transcript: could not create %s: %s", self.path.parent, exc)
        # Best-effort retention sweep, once per construction: one directory
        # listing, never recursive, so it costs about as much as the write
        # it sits beside (see `prune`).
        prune(dir=str(self.path.parent))
        # Resume the sequence from an existing file (e.g. a restart that
        # re-attaches to the same run_id/path) instead of restarting at 1 and
        # colliding with lines already on disk. A brand-new file reads back
        # 0 here, so the header below is still this file's first line.
        self._seq = self._last_seq()
        if self._seq == 0:
            launch_text = (title or goal or "run started").strip() or "run started"
            self.write({"agent": "", "task_id": None, "kind": "launch", "text": launch_text})

    def _last_seq(self) -> int:
        """The newest valid `seq` already on disk, or 0 for an empty/missing file."""
        try:
            with open(self.path, "rb") as handle:
                handle.seek(0, 2)
                size = handle.tell()
                if size == 0:
                    return 0
                handle.seek(max(0, size - 4096))
                data = handle.read().decode("utf-8", errors="ignore")
        except OSError:
            return 0
        for raw in reversed(data.splitlines()):
            raw = raw.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except ValueError:
                continue  # a half-written line near EOF; keep looking backward
            try:
                return int(event.get("seq", 0))
            except (TypeError, ValueError, AttributeError):
                continue
        return 0

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def write(self, event: dict[str, Any]) -> None:
        """Append one JSON line, flushed. Never raises.

        A transcript is a side channel: an ``OSError`` (disk full, permission,
        a vanished directory) or a ``ValueError`` (unserializable field) is
        logged at debug and otherwise swallowed, because a write failure here
        must never fail the run it is merely describing.
        """
        try:
            kind = str(event.get("kind") or "status")
            if kind not in _VALID_KINDS:
                kind = "status"
            task_id = event.get("task_id")
            line = {
                "seq": self._next_seq(),
                "ts": time.time(),
                "agent": str(event.get("agent") or ""),
                "task_id": None if task_id is None else str(task_id),
                "kind": kind,
                "text": _clean_text(event.get("text")),
            }
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(line, default=str))
                handle.write("\n")
                handle.flush()
        except (OSError, ValueError) as exc:
            logger.debug("live_transcript: write failed for %s: %s", self.path, exc)

    def read_since(self, since_seq: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        """Events with ``seq > since_seq``, oldest first, capped at ``limit``.

        Tolerant of a truncated/half-written last line — a reader racing the
        writer's last ``flush`` — which is simply skipped rather than raising.
        ``limit=0`` means unbounded (used internally by ``tail_text``).
        """
        events: list[dict[str, Any]] = []
        try:
            with open(self.path, encoding="utf-8") as handle:
                lines = handle.readlines()
        except OSError:
            return events
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except ValueError:
                continue  # truncated/half-written last line
            if not isinstance(event, dict):
                continue
            try:
                seq = int(event.get("seq", 0))
            except (TypeError, ValueError):
                continue
            if seq > since_seq:
                events.append(event)
        events.sort(key=lambda e: e.get("seq", 0))
        return events[:limit] if limit else events

    def tail_text(self, max_lines: int = 40, max_chars: int = 1500) -> str:
        """Newest lines rendered for context injection: ``[agent] kind: text``.

        Bounded on both axes — at most ``max_lines`` lines, and the result
        trimmed to ``max_chars`` (keeping the newest tail of the text when it
        still runs over).
        """
        events = self.read_since(0, limit=0)
        tail = events[-max_lines:] if max_lines else events
        lines = [f"[{e.get('agent') or '?'}] {e.get('kind', 'status')}: {e.get('text', '')}" for e in tail]
        text = "\n".join(lines)
        if max_chars and len(text) > max_chars:
            return text[-max_chars:]
        return text


def for_run(run_id: str, **kw: Any) -> LiveTranscript | None:
    """Build ``run_id``'s transcript, or ``None`` when the live dir cannot be created.

    The single guard every caller needs: a ``None`` here means every later
    touch for this run is a no-op, so a plugin host with a read-only or
    missing home directory degrades to exactly the pre-transcript behaviour.
    """
    try:
        transcript = LiveTranscript(run_id, **kw)
    except Exception as exc:  # noqa: BLE001 - construction must never raise
        logger.debug("live_transcript: for_run(%s) failed: %s", run_id, exc)
        return None
    if not transcript.path.parent.is_dir():
        return None
    return transcript


def prune(dir: str | None = None, max_age_days: int = 7) -> None:
    """Unlink ``*.jsonl`` files older than ``max_age_days``. Best-effort, non-recursive.

    Meant to be called cheaply and often (``LiveTranscript.__init__`` calls it
    once per construction): a single non-recursive directory listing, with
    every per-file and whole-sweep failure swallowed.
    """
    try:
        base = Path(dir or DEFAULT_LIVE_DIR).expanduser()
        if not base.is_dir():
            return
        cutoff = time.time() - max_age_days * 86400
        for entry in base.glob("*.jsonl"):
            try:
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    entry.unlink()
            except OSError:
                continue
    except Exception as exc:  # noqa: BLE001 - retention must never raise
        logger.debug("live_transcript: prune(%s) failed: %s", dir, exc)


__all__ = [
    "DEFAULT_LIVE_DIR",
    "LiveTranscript",
    "for_run",
    "prune",
    "transcript_path",
]
