"""Read a worker's *live* tool call from its own Hermes profile state DB.

Spec T4 asks for the worker's real tool call on the activity line, not a canned
phrase, and the only place that call exists is the child session's profile store:
``<hermes home>/profiles/<agent>/state.db`` → ``messages`` (``tool_name``,
``tool_calls``, ``timestamp``).

**Worker → child-session mapping (spike result).** The launch handle the host
returns (``agent.subagent_lifecycle.SubagentHandle``) exposes ``subagent_id``,
``parent_session_id``, ``correlation_id``, ``created_at``, ``provider``,
``model``, ``role``, ``depth`` and ``capability`` — but **no child session id**,
and the child's row in ``sessions`` carries the *parent* session id, so neither
the handle nor a join names the child's session unambiguously (many workers share
the parent). The mapping therefore falls back, as the spec allows, to **the
profile's newest session at launch**: the engine captures it once, right after the
launch returns, and pins it on the worker. That is exact when one worker of an agent
runs at a time and approximate when two of the *same agent* launch within the
same instant, which the per-launch pin absorbs for every later tick.

The reader is best-effort and read-only: a missing DB, a locked DB or an unknown
session all return ``None`` so the caller keeps the canned phrase. Nothing here
raises. The emoji is resolved from the host tool registry (``status_message``),
never a hardcoded map.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from orchestrator.status_message import render_activity

__all__ = [
    "make_activity_provider",
    "profiles_root",
    "read_activity",
    "session_at_launch",
]

# The argument keys that name *what* a tool call is aimed at, in the order most
# tools use them. The first present key wins; the value is what the activity line
# shows after the tool name.
_TARGET_KEYS = (
    "path",
    "file_path",
    "command",
    "query",
    "pattern",
    "url",
    "file",
    "directory",
    "target",
    "ref",
    "name",
    "prompt",
    "text",
)


def _hermes_home() -> Path | None:
    """The host's Hermes home, resolved the way the host itself resolves it."""
    try:
        from hermes_constants import get_hermes_home  # noqa: PLC0415 - host-only, optional

        return Path(get_hermes_home())
    except Exception:
        pass
    env = os.environ.get("HERMES_HOME", "").strip()
    if env:
        return Path(env)
    home = os.environ.get("HOME", "").strip()
    return Path(home) / ".hermes" if home else None


def profiles_root(profiles_dir: Any = None) -> Path | None:
    """Where the per-agent profile stores live, or None when it cannot be found."""
    if profiles_dir:
        return Path(str(profiles_dir))
    override = os.environ.get("HERMES_PROFILES_DIR", "").strip()
    if override:
        return Path(override)
    home = _hermes_home()
    return home / "profiles" if home is not None else None


def _db_path(agent: str, profiles_dir: Any = None) -> Path | None:
    name = str(agent or "").strip()
    if not name:
        return None
    root = profiles_root(profiles_dir)
    if root is None:
        return None
    path = root / name / "state.db"
    return path if path.is_file() else None


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.25)
    conn.row_factory = sqlite3.Row
    return conn


def session_at_launch(agent: str, profiles_dir: Any = None) -> str | None:
    """The agent profile's newest session id, or None when it cannot be read.

    The spike (module docstring) proved the launch handle names no child session,
    so this is the mapping: captured once, at launch, and pinned on the worker.
    """
    path = _db_path(agent, profiles_dir)
    if path is None:
        return None
    try:
        with contextlib.closing(_connect(path)) as conn:
            # ``sessions`` keys the session id as its primary column ``id``.
            row = conn.execute(
                "SELECT id FROM sessions WHERE id IS NOT NULL AND id != '' ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            if row and row[0]:
                return str(row[0])
            # A profile with no ``sessions`` row (older store) still has messages.
            row = conn.execute("SELECT session_id FROM messages ORDER BY timestamp DESC LIMIT 1").fetchone()
            return str(row[0]) if row and row[0] else None
    except Exception:
        return None


def read_activity(agent: str, session_id: str, profiles_dir: Any = None) -> tuple[str, str] | None:
    """The session's newest tool call as ``(tool_name, target)``, or None.

    An assistant row carrying ``tool_calls`` is the authoritative "this is what
    the worker is doing"; a bare ``tool_name`` row (the result) is the fallback
    when the assistant row has already been compacted away.
    """
    session = str(session_id or "").strip()
    if not session:
        return None
    path = _db_path(agent, profiles_dir)
    if path is None:
        return None
    try:
        with contextlib.closing(_connect(path)) as conn:
            row = conn.execute(
                "SELECT tool_calls FROM messages "
                "WHERE session_id = ? AND tool_calls IS NOT NULL AND tool_calls != '' "
                "ORDER BY timestamp DESC LIMIT 1",
                (session,),
            ).fetchone()
            if row is not None:
                pair = _from_tool_calls(row[0])
                if pair is not None:
                    return pair
            row = conn.execute(
                "SELECT tool_name FROM messages "
                "WHERE session_id = ? AND tool_name IS NOT NULL AND tool_name != '' "
                "ORDER BY timestamp DESC LIMIT 1",
                (session,),
            ).fetchone()
            if row is not None and row[0]:
                return str(row[0]), ""
    except Exception:
        return None
    return None


def _from_tool_calls(raw: Any) -> tuple[str, str] | None:
    """Parse the newest ``tool_calls`` JSON into ``(tool_name, target)``."""
    try:
        calls = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except (ValueError, TypeError):
        return None
    if not isinstance(calls, list) or not calls:
        return None
    first = calls[-1] if isinstance(calls[-1], dict) else None
    if first is None:
        return None
    function = first.get("function") if isinstance(first, dict) else None
    if isinstance(function, dict):
        name = str(function.get("name") or "").strip()
        args = function.get("arguments")
    else:
        name = str(first.get("name") or "").strip()
        args = first.get("arguments")
    if not name:
        return None
    return name, _target_from_args(args)


def _target_from_args(args: Any) -> str:
    if isinstance(args, (str, bytes)):
        try:
            args = json.loads(args)
        except (ValueError, TypeError):
            return str(args)
    if not isinstance(args, dict):
        return ""
    for key in _TARGET_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
        if isinstance(value, (int, float)):
            return str(value)
    return ""


def make_activity_provider(profiles_dir: Any = None, emoji_resolver: Any = None) -> Any:
    """Build the injected ``provider(session_id, agent) -> str | None``.

    The returned callable formats ``{emoji} {tool} {target}`` (emoji from the host
    registry), bounded to ~48 chars, or None when nothing is observable yet — so
    the tracker falls back to the canned phrase and the line is never blank.
    """

    def provider(session_id: str, agent: str) -> str | None:
        pair = read_activity(agent, session_id, profiles_dir)
        if pair is None:
            return None
        tool, target = pair
        return render_activity(tool, target, emoji_resolver=emoji_resolver) or None

    return provider
