"""Shared run models for the OMO engine and its task-graph layer.

Split out of ``engine.py`` so the graph scheduler can build runs and workers
without importing the engine that drives them.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from roster import agent

PENDING = "PENDING"
RUNNING = "RUNNING"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"
BLOCKED = "BLOCKED"
RETRYING = "RETRYING"
# A worker whose process is gone: recorded from disk after a restart, never live.
INTERRUPTED = "INTERRUPTED"

# Statuses that only a live process can hold; anything else is a finished record.
UNFINISHED = (PENDING, RUNNING, RETRYING)


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class Worker:
    run_id: str
    agent_name: str
    task: str
    chain: tuple[str, ...]
    model: str | None = None
    status: str = PENDING
    handle: Any = None
    result: Any = None
    error: str = ""
    cancel_requested: bool = False
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    # Task-graph fields. A plain dispatch leaves them empty.
    task_id: str = ""
    depends_on: tuple[str, ...] = ()
    parent_id: str = ""
    review_cycles: int = 0
    review_verdict: str = ""
    hop_history: list[dict[str, str]] = field(default_factory=list)

    def as_row(self) -> dict[str, Any]:
        spec = agent(self.agent_name)
        row: dict[str, Any] = {
            "agent": spec.display if spec else self.agent_name,
            "status": self.status,
            "task": self.task,
            "model": self.model,
            "run_id": self.run_id,
        }
        if self.task_id:
            row["task_id"] = self.task_id
        if self.depends_on:
            row["depends_on"] = list(self.depends_on)
        if self.parent_id:
            row["parent"] = self.parent_id
        if self.review_cycles:
            row["review_cycles"] = self.review_cycles
        if self.review_verdict:
            row["review_verdict"] = self.review_verdict
        if self.hop_history:
            row["hop_history"] = self.hop_history
        return row

    def to_dict(self) -> dict[str, Any]:
        """The durable fields only: a handle and a result die with the process."""
        return {
            "run_id": self.run_id,
            "agent_name": self.agent_name,
            "task": self.task,
            "chain": list(self.chain),
            "model": self.model,
            "status": self.status,
            "error": self.error,
            "task_id": self.task_id,
            "depends_on": list(self.depends_on),
            "parent_id": self.parent_id,
            "review_cycles": self.review_cycles,
            "review_verdict": self.review_verdict,
            "hop_history": [hop for hop in self.hop_history if isinstance(hop, dict)],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Worker | None:
        run_id = str(payload.get("run_id") or "")
        if not run_id:
            return None
        finished = payload.get("finished_at")
        return cls(
            run_id=run_id,
            agent_name=str(payload.get("agent_name") or ""),
            task=str(payload.get("task") or ""),
            chain=tuple(str(model) for model in payload.get("chain") or ()),
            model=payload.get("model") or None,
            status=str(payload.get("status") or PENDING),
            error=str(payload.get("error") or ""),
            task_id=str(payload.get("task_id") or ""),
            depends_on=tuple(str(dep) for dep in payload.get("depends_on") or ()),
            parent_id=str(payload.get("parent_id") or ""),
            review_cycles=as_int(payload.get("review_cycles"), 0),
            review_verdict=str(payload.get("review_verdict") or ""),
            hop_history=[hop for hop in payload.get("hop_history") or () if isinstance(hop, dict)],
            started_at=as_float(payload.get("started_at")),
            finished_at=None if finished is None else as_float(finished),
        )


@dataclass
class Run:
    run_id: str
    goal: str
    workers: list[Worker] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    # Set when the run was adopted from disk with work still unfinished: the
    # record is real, the process behind it is not.
    recovered: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "goal": self.goal,
            "created_at": self.created_at,
            "recovered": self.recovered,
            "workers": [worker.to_dict() for worker in self.workers],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Run | None:
        run_id = str(payload.get("run_id") or "")
        if not run_id:
            return None
        workers = [
            worker
            for worker in (Worker.from_dict(e) for e in payload.get("workers") or () if isinstance(e, dict))
            if worker
        ]
        return cls(
            run_id=run_id,
            goal=str(payload.get("goal") or ""),
            workers=workers,
            created_at=as_float(payload.get("created_at")),
            recovered=bool(payload.get("recovered")),
        )

    def tree(self) -> str:
        lines = ["Hermes", f"└── omo run {self.run_id}: {self.goal[:60]}"]
        for index, worker in enumerate(self.workers):
            branch = "└──" if index == len(self.workers) - 1 else "├──"
            row = worker.as_row()
            suffix = f" · depends: {','.join(row['depends_on'])}" if row.get("depends_on") else ""
            lines.append(f"    {branch} {row['agent']} · {row['status']} · {row['model'] or '-'}{suffix}")
        return "\n".join(lines)
