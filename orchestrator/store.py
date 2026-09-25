"""Durable run registry: what the engine ran survives a gateway restart.

The registry used to be process memory only, so a restart answered `runs: []`
while work the caller had already paid for sat on disk — and got re-dispatched.
The store keeps the record instead, and a restored worker whose process is gone
is named interrupted rather than left reading as running.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from orchestrator.models import Run, as_int

DEFAULT_STATE_PATH = "~/.omo/runs.json"
DEFAULT_MAX_PERSISTED_RUNS = 50
SCHEMA_VERSION = 1


class RunStore:
    """Atomic, bounded JSON record of the engine's runs."""

    def __init__(self, *, path: Any = None, max_runs: Any = None) -> None:
        self.path = Path(str(path or DEFAULT_STATE_PATH)).expanduser()
        # `0` keeps everything, like the engine's other bounds.
        self.max_runs = max(
            0, as_int(max_runs if max_runs is not None else DEFAULT_MAX_PERSISTED_RUNS, DEFAULT_MAX_PERSISTED_RUNS)
        )

    def load(self) -> dict[str, Run]:
        """Last written runs; a missing or damaged file reads as an empty registry."""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict) or not isinstance(raw.get("runs"), list):
            return {}
        runs: dict[str, Run] = {}
        for entry in raw["runs"]:
            if not isinstance(entry, dict):
                continue
            run = Run.from_dict(entry)
            if run is not None:
                runs[run.run_id] = run
        return runs

    def save(self, runs: dict[str, Run]) -> None:
        """Write the registry atomically, newest first, bounded by `max_runs`."""
        ordered = sorted(runs.values(), key=lambda run: run.created_at, reverse=True)
        if self.max_runs > 0:
            ordered = ordered[: self.max_runs]
        payload = {"version": SCHEMA_VERSION, "runs": [run.to_dict() for run in ordered]}
        temp: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
                encoding="utf-8",
            ) as handle:
                temp = handle.name
                json.dump(payload, handle, default=str)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
        except OSError:
            # A registry that cannot be written must not take the run down with it.
            if temp is not None:
                Path(temp).unlink(missing_ok=True)


__all__ = ["DEFAULT_MAX_PERSISTED_RUNS", "DEFAULT_STATE_PATH", "RunStore"]
