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

import threading
from typing import Any

SESSION_ID_ENV = "HERMES_SESSION_ID"
SESSION_KEY_ENV = "HERMES_SESSION_KEY"
ROUTE_PLATFORM_ENV = "HERMES_SESSION_PLATFORM"
ROUTE_CHAT_ENV = "HERMES_SESSION_CHAT_ID"
ROUTE_THREAD_ENV = "HERMES_SESSION_THREAD_ID"

# The runs this process is executing right now, by run id, holding their session
# live. Deliberately process memory and never a field on the record: a hold exists
# only while an engine is here to release it, so it dies with the process. A stored
# liveness field survives the process and keeps claiming a dead run is alive, which
# is exactly how a record sat at RUNNING for hours holding a dispatch slot (#59).
_HELD_RUNS: dict[str, str] = {}
_HELD_LOCK = threading.Lock()


def hold_run(run_id: str, session_id: str | None) -> None:
    """Mark a run this process is executing, so it and its session count as live."""
    if not run_id:
        return
    with _HELD_LOCK:
        _HELD_RUNS[run_id] = (session_id or "").strip()


def release_run(run_id: str) -> None:
    """Drop a run's hold — idempotent, so every terminal path can call it."""
    with _HELD_LOCK:
        _HELD_RUNS.pop(run_id, None)


def run_is_held(run_id: str) -> bool:
    """Whether this process is executing that run right now."""
    with _HELD_LOCK:
        return run_id in _HELD_RUNS


def session_is_held(session_id: str | None) -> bool:
    """Whether a run this process is still executing belongs to `session_id`."""
    key = (session_id or "").strip()
    if not key:
        return False
    with _HELD_LOCK:
        return key in _HELD_RUNS.values()


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


def current_session_key() -> str | None:
    """The calling session's gateway session_key, or `None` when no bridge can answer.

    Mirrors ``current_session_id``: the same optional bridge, the same fail-open
    behaviour. A run's ``session_key`` is what the host's ``inject_message`` needs
    to deliver a message back into a *gateway* session later — a worker thread has
    no session ContextVars, so this has to be captured at dispatch time, the same
    way ``current_route`` is. A host with no bridge, or one whose import fails,
    just means this run can never be woken, not that the dispatch fails.
    """
    try:
        from gateway.session_context import get_session_env  # noqa: PLC0415

        value = get_session_env(SESSION_KEY_ENV, "") or ""
    except Exception:
        return None
    session_key = str(value).strip()
    return session_key or None


def current_route() -> dict[str, str] | None:
    """Where the calling session's replies land, or `None` when unknown.

    The live status message is delivered back into the conversation that paid for
    the run, which means the run has to remember the platform/chat it came from —
    a worker thread has no session ContextVars. The bridge is the same optional
    one ``current_session_id`` uses, so a host without it just gets no route and
    no status message rather than a failed dispatch.
    """
    try:
        from gateway.session_context import get_session_env  # noqa: PLC0415

        platform = str(get_session_env(ROUTE_PLATFORM_ENV, "") or "").strip()
        chat_id = str(get_session_env(ROUTE_CHAT_ENV, "") or "").strip()
        thread_id = str(get_session_env(ROUTE_THREAD_ENV, "") or "").strip()
    except Exception:
        return None
    if not chat_id:
        return None
    return {"platform": platform, "chat_id": chat_id, "thread_id": thread_id}


def apply_route(run: Any, route: dict[str, str] | None) -> None:
    """Copy a resolved route onto a run record; a missing route stays blank."""
    route = route or {}
    run.platform = str(route.get("platform") or "").strip() or None
    run.chat_id = str(route.get("chat_id") or "").strip() or None
    run.thread_id = str(route.get("thread_id") or "").strip() or None


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


__all__ = [
    "ROUTE_CHAT_ENV",
    "ROUTE_PLATFORM_ENV",
    "ROUTE_THREAD_ENV",
    "SESSION_ID_ENV",
    "SESSION_KEY_ENV",
    "apply_route",
    "belongs_to",
    "current_route",
    "current_session_id",
    "foreign_run",
    "hold_run",
    "release_run",
    "run_is_held",
    "session_is_held",
]
