"""OMO dashboard plugin — backend API routes, mounted at /api/plugins/omo/.

Every handler is read-only: the run registry (``orchestrator.store.RunStore``)
and the per-run live-transcript JSONL files another worker writes under
``~/.omo/live/<run_id>.jsonl`` are only ever read here, never written, and
nothing here starts a background thread. The orchestrator package may not be
importable in every host (a bare dashboard test harness, or a deploy that
hasn't shipped it yet); every entry point that needs it catches ``ImportError``
and answers 503 with a plain detail instead of 500-looping the dashboard.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query

router = APIRouter()

# The plugin's own package root (``dashboard/..``), so ``orchestrator`` and its
# siblings import the same code the engine runs, not a copy bundled here.
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

_LIVE_DIR = Path("~/.omo/live").expanduser()

_MAX_RUNS = 50
_DEFAULT_TRANSCRIPT_LIMIT = 200
_MAX_TRANSCRIPT_LIMIT = 1000

# Fallback status labels used only when ``orchestrator.models`` itself cannot
# be imported (e.g. a bare FastAPI test harness) — kept in sync with the real
# constants by name, never redefined independently of them when available.
_FALLBACK_UNFINISHED = ("PENDING", "RUNNING", "RETRYING")
_FALLBACK_FAILED = "FAILED"
_FALLBACK_CANCELLED = "CANCELLED"
_FALLBACK_UNAUTHENTICATED = "UNAUTHENTICATED"


def _status_constants() -> dict[str, Any]:
    """The engine's terminal-state names, or the matching plain-string fallback."""
    try:
        from orchestrator import models as _models
    except ImportError:
        return {
            "UNFINISHED": _FALLBACK_UNFINISHED,
            "FAILED": _FALLBACK_FAILED,
            "CANCELLED": _FALLBACK_CANCELLED,
            "UNAUTHENTICATED": _FALLBACK_UNAUTHENTICATED,
        }
    return {
        "UNFINISHED": _models.UNFINISHED,
        "FAILED": _models.FAILED,
        "CANCELLED": _models.CANCELLED,
        "UNAUTHENTICATED": _models.UNAUTHENTICATED,
    }


def _load_store():
    """Import the engine's own run registry; 503 (never 500) when it can't load."""
    try:
        from orchestrator.store import RunStore
    except ImportError as exc:
        raise HTTPException(status_code=503, detail=f"omo orchestrator unavailable: {exc}") from exc
    return RunStore()


def _load_runs() -> dict[str, Any]:
    """The current registry, read-only; a missing/corrupt file already reads as empty
    (``RunStore.load``) — this only guards against an unexpected read-time exception."""
    store = _load_store()
    try:
        return store.load()
    except Exception:
        return {}


def _last_tool(worker: Any) -> str | None:
    """The worker's live tool call, best-effort, via its own profile session store.

    Entirely optional: a missing session, a missing profile DB, or any read
    failure all read as "unknown" rather than surfacing an error to the caller.
    """
    session = getattr(worker, "activity_session", None)
    agent_name = getattr(worker, "agent_name", None)
    if not session or not agent_name:
        return None
    try:
        from orchestrator.worker_activity import read_activity

        pair = read_activity(agent_name, session)
    except Exception:
        return None
    if not pair:
        return None
    name, target = pair
    return f"{name} {target}".strip() if target else name


def _transcript_path(run_id: str) -> Path:
    return _LIVE_DIR / f"{run_id}.jsonl"


def _worker_row(worker: Any) -> dict[str, Any]:
    return {
        "agent": getattr(worker, "agent_name", "") or "",
        "task_id": getattr(worker, "task_id", "") or "",
        "status": getattr(worker, "status", "") or "",
        "last_activity_at": getattr(worker, "last_activity_at", 0.0) or 0.0,
        "last_tool": _last_tool(worker),
    }


def _run_active(workers: list[dict[str, Any]], unfinished: tuple[str, ...]) -> bool:
    return any(w["status"] in unfinished for w in workers)


def _run_state(workers: list[dict[str, Any]], active: bool, consts: dict[str, Any]) -> str:
    if not workers:
        return "PENDING"
    if active:
        return "RUNNING"
    if any(w["status"] == consts["FAILED"] for w in workers):
        return "FAILED"
    if any(w["status"] == consts["CANCELLED"] for w in workers):
        return "CANCELLED"
    if any(w["status"] == consts["UNAUTHENTICATED"] for w in workers):
        return "UNAUTHENTICATED"
    return "SUCCEEDED"


def _run_to_dict(run: Any) -> dict[str, Any]:
    consts = _status_constants()
    workers = [_worker_row(w) for w in getattr(run, "workers", None) or []]
    active = _run_active(workers, consts["UNFINISHED"])
    created_at = getattr(run, "created_at", 0.0) or 0.0
    updated_candidates = [created_at]
    for worker in getattr(run, "workers", None) or []:
        updated_candidates.append(getattr(worker, "started_at", 0.0) or 0.0)
        finished_at = getattr(worker, "finished_at", None)
        if finished_at:
            updated_candidates.append(finished_at)
    run_id = getattr(run, "run_id", "") or ""
    path = _transcript_path(run_id)
    return {
        "run_id": run_id,
        "goal": getattr(run, "goal", "") or "",
        "state": _run_state(workers, active, consts),
        "created_at": created_at,
        "updated_at": max(updated_candidates),
        "workers": workers,
        "transcript_path": str(path),
        "active": active,
        "event_count": _count_events(path),
    }


def _count_events(path: Path) -> int:
    if not path.is_file():
        return 0
    count = 0
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    count += 1
    except OSError:
        return 0
    return count


@router.get("/runs")
def list_runs() -> dict[str, Any]:
    runs = _load_runs()
    ordered = sorted(
        runs.values(),
        key=lambda r: (getattr(r, "created_at", 0.0) or 0.0, getattr(r, "run_id", "") or ""),
        reverse=True,
    )
    return {"runs": [_run_to_dict(r) for r in ordered[:_MAX_RUNS]]}


@router.get("/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    run = _load_runs().get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run {run_id!r} not found")
    return _run_to_dict(run)


def _parse_line(raw: str) -> dict[str, Any] | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        event = json.loads(raw)
    except ValueError:
        # A truncated final line (writer mid-append) is skipped, not an error.
        return None
    return event if isinstance(event, dict) else None


def _read_tail(path: Path, since: int, limit: int) -> list[dict[str, Any]]:
    """Minimal local tail reader for the writer's JSONL line shape
    (``{"seq","ts","agent","task_id","kind","text"}``), used when
    ``orchestrator.live_transcript`` isn't importable yet."""
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for raw in lines:
        event = _parse_line(raw)
        if event is None:
            continue
        try:
            seq = int(event.get("seq"))
        except (TypeError, ValueError):
            continue
        if seq <= since:
            continue
        events.append(event)
    events.sort(key=lambda e: e.get("seq", 0))
    return events[:limit]


def _read_transcript(run_id: str, since: int, limit: int) -> tuple[list[dict[str, Any]], Path | None]:
    path = _transcript_path(run_id)
    try:
        from orchestrator.live_transcript import LiveTranscript

        live = LiveTranscript(run_id)
        events = list(live.read_since(since, limit=limit))
        transcript_path = getattr(live, "transcript_path", path) or path
        return events[:limit], Path(transcript_path)
    except Exception:
        # Module absent, writer not wired up yet, or any read-time failure —
        # fall back to the local tail reader over the same JSONL shape.
        return _read_tail(path, since, limit), (path if path.is_file() else None)


@router.get("/runs/{run_id}/transcript")
def get_transcript(
    run_id: str,
    since: int = Query(0, ge=0),
    limit: int = Query(_DEFAULT_TRANSCRIPT_LIMIT, ge=1, le=_MAX_TRANSCRIPT_LIMIT),
) -> dict[str, Any]:
    events, path = _read_transcript(run_id, since, limit)
    next_since = since
    for event in events:
        try:
            seq = int(event.get("seq"))
        except (TypeError, ValueError):
            continue
        next_since = max(next_since, seq)
    active = False
    try:
        run = _load_runs().get(run_id)
    except HTTPException:
        run = None
    if run is not None:
        consts = _status_constants()
        active = any(getattr(w, "status", "") in consts["UNFINISHED"] for w in getattr(run, "workers", None) or [])
    return {
        "events": events,
        "next_since": next_since,
        "active": active,
        "path": str(path) if path else None,
    }
