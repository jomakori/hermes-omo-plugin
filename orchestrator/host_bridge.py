"""Best-effort bridge: make one OMO worker visible in Hermes's live Subagents dock.

Tees a worker's transcript lines into the host's live-transcript format
(``tools.delegation_live_log``) and registers/unregisters a row in the host's
live-subagent registry (``tools.delegate_tool_registry``), so the chat UI's
Subagents panel can list and tail a real OMO worker instead of only the
plugin's own ``~/.omo/live/<run_id>.jsonl`` feed. Every host import and call
here is guarded: a host missing an expected name (older Hermes, a test host,
a sandboxed import) degrades to "no row, no host transcript" and never
raises into the worker's own walk. Redaction is NOT duplicated here — the
host writer already redacts every line through its own single choke point
(``tools.delegation_live_log``'s ``_redact``); this module only forwards text
to it.
"""

from __future__ import annotations

import logging
import weakref
from typing import Any

logger = logging.getLogger(__name__)

# kind -> LiveTranscriptWriter method name; anything unmapped falls back to .event(kind, text).
_EMIT_METHODS = {"result": "marker", "error": "marker"}


def _resolve_parent_agent() -> Any:
    """The real parent AIAgent bound to this turn's context, if any.

    Same ambient resolver ``agent.subagent_lifecycle.SubagentLifecycleService``
    itself uses, so a row built here carries the identical weakref shape a real
    delegated child gets at build time.
    """
    try:
        from agent.subagent_lifecycle import get_active_subagent_parent  # noqa: PLC0415 - host-only, optional

        return get_active_subagent_parent()
    except Exception as exc:
        logger.debug("host_bridge: parent agent unresolved: %s", exc)
        return None


class _WorkerShim:
    """Minimal registry-facing stand-in for a worker; never a real AIAgent.

    Carries only what the host's dock/tail path reads: ``_live_transcript_path``
    (the tail source) and ``_delegate_parent_ref`` — the same weakref chain a
    real delegated child is stamped with at build time, so both
    ``delegate_tool_registry._owns_subagent_record`` and
    ``session_ancillary.subagent_records``'s ``_is_descendant_of`` check accept
    this row without any session-db plumbing.
    """

    def __init__(self, live_transcript_path: str, parent_agent: Any) -> None:
        self._live_transcript_path = live_transcript_path
        self._delegate_parent_ref = None
        if parent_agent is not None:
            try:
                self._delegate_parent_ref = weakref.ref(parent_agent)
            except TypeError as exc:
                logger.debug("host_bridge: parent agent is not weakly referenceable: %s", exc)


def _hermes_home() -> Any:
    """The host's Hermes home, resolved the way the host itself resolves it (None if unavailable)."""
    try:
        from hermes_constants import get_hermes_home  # noqa: PLC0415 - host-only, optional

        return get_hermes_home()
    except Exception as exc:
        logger.debug("host_bridge: get_hermes_home unavailable: %s", exc)
        return None


def open_writer(*, goal: str, model: str | None) -> Any:
    """Build one host-format live transcript writer, or None when the host is unavailable."""
    try:
        from tools.delegation_live_log import create_live_transcripts  # noqa: PLC0415 - host-only, optional
    except Exception as exc:
        logger.debug("host_bridge: delegation_live_log unavailable: %s", exc)
        return None
    try:
        _, writers, _ = create_live_transcripts([{"goal": goal}], model=model, home=_hermes_home())
        return writers[0] if writers else None
    except Exception as exc:
        logger.debug("host_bridge: writer creation failed: %s", exc)
        return None


def register_row(
    *,
    subagent_id: str,
    parent_session_id: str | None,
    goal: str,
    model: str | None,
    owner_session_id: str | None,
    live_transcript_path: str | None,
) -> Any:
    """Register one live dock row; returns the shim used to unregister it, or None on any failure."""
    if not subagent_id or not live_transcript_path:
        return None
    try:
        from tools.delegate_tool_registry import _register_subagent  # noqa: PLC0415 - host-only, optional
    except Exception as exc:
        logger.debug("host_bridge: delegate_tool_registry unavailable: %s", exc)
        return None
    shim = _WorkerShim(live_transcript_path, _resolve_parent_agent())
    try:
        _register_subagent(
            {
                "subagent_id": subagent_id,
                "parent_id": parent_session_id or "",
                "goal": goal,
                "model": model,
                "status": "running",
                "owner_agent_session_id": owner_session_id,
                "accepting_steer": False,  # the shim has no .steer(); never claim it does
                "agent": shim,
            }
        )
        return shim
    except Exception as exc:
        logger.debug("host_bridge: registration failed for %s: %s", subagent_id, exc)
        return None


def update_fields(subagent_id: str | None, **fields: Any) -> None:
    """Best-effort merge of extra fields (status, tool_count, last_tool, ...) onto a registered row."""
    if not subagent_id:
        return
    try:
        from tools.delegate_tool_registry import _active_subagents, _active_subagents_lock  # noqa: PLC0415

        with _active_subagents_lock:
            record = _active_subagents.get(subagent_id)
            if record is not None:
                record.update(fields)
    except Exception as exc:
        logger.debug("host_bridge: field update failed for %s: %s", subagent_id, exc)


def update_status(subagent_id: str | None, status: str) -> None:
    """Best-effort status update on an already-registered row."""
    update_fields(subagent_id, status=status)


def unregister_row(subagent_id: str, shim: Any) -> None:
    """Drop a worker's dock row; a no-op once already gone."""
    try:
        from tools.delegate_tool_registry import _unregister_subagent  # noqa: PLC0415 - host-only, optional

        _unregister_subagent(subagent_id, agent=shim)
    except Exception as exc:
        logger.debug("host_bridge: unregister failed for %s: %s", subagent_id, exc)


def emit(writer: Any, kind: str, text: str) -> None:
    """Tee one transcript line into the host-format writer, best-effort."""
    if writer is None or not text:
        return
    try:
        method_name = _EMIT_METHODS.get(kind)
        if method_name:
            getattr(writer, method_name)(text)
        else:
            writer.event(kind, text)
    except Exception as exc:
        logger.debug("host_bridge: emit(%s) failed: %s", kind, exc)


__all__ = ["emit", "open_writer", "register_row", "unregister_row", "update_fields", "update_status"]
