"""Which Hermes session paid for a run.

A run belongs to the session that dispatched it, and the registry is shared by
every session the gateway serves. Without attribution one session can read — and
cancel — work another session is still paying for, so the run carries its
owner's id and every read or cancel is scoped to it.

The id is resolved through the host's own bridge (``gateway.session_context``,
the same accessor Hermes' tools use): the task-local session ContextVar wins,
with ``os.environ`` as the fallback for a subprocess. The bridge is optional —
a host without it, or one whose import fails, must not lose the dispatch — so
every failure resolves to "no session" and nothing raises.
"""

from __future__ import annotations

from typing import Any

SESSION_ID_ENV = "HERMES_SESSION_ID"


def current_session_id() -> str | None:
    """The calling session's id, or `None` when no bridge can answer.

    Never raises: an ImportError, a missing attribute or a broken bridge all mean
    the same thing to the caller — this run is unattributed, keep going.
    """
    try:
        from gateway.session_context import get_session_env  # noqa: PLC0415

        value = get_session_env(SESSION_ID_ENV, "") or ""
    except Exception:
        return None
    session_id = str(value).strip()
    return session_id or None


def belongs_to(run: Any, caller: str | None) -> bool:
    """Whether `run` is the caller's own record.

    Equality, not a permissive fallback: a record whose owner is unknown (no
    session id — written before attribution existed) matches only a caller that
    is itself unknown, so a real session never mistakes it for its own work.
    """
    return getattr(run, "session_id", None) == caller


def foreign_run(run_id: str, owner: str | None, caller: str | None) -> dict[str, Any]:
    """The refusal payload, naming the session that actually owns the run."""
    owner_text = owner or "an unattributed record (no session id)"
    caller_text = caller or "none resolved"
    return {
        "error": (
            f"run {run_id} belongs to session {owner_text}, not the calling session ({caller_text}); "
            "pass all_sessions=true to reach across sessions."
        ),
        "run_id": run_id,
        "owner_session_id": owner or "",
        "session_id": caller or "",
    }


__all__ = ["SESSION_ID_ENV", "belongs_to", "current_session_id", "foreign_run"]
