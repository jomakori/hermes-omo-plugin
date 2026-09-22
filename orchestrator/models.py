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
        return row


@dataclass
class Run:
    run_id: str
    goal: str
    workers: list[Worker] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def worker_for(self, task_id: str) -> Worker | None:
        for worker in self.workers:
            if worker.task_id == task_id:
                return worker
        return None

    def tree(self) -> str:
        lines = ["Hermes", f"└── omo run {self.run_id}: {self.goal[:60]}"]
        for index, worker in enumerate(self.workers):
            branch = "└──" if index == len(self.workers) - 1 else "├──"
            row = worker.as_row()
            suffix = f" · depends: {','.join(row['depends_on'])}" if row.get("depends_on") else ""
            lines.append(f"    {branch} {row['agent']} · {row['status']} · {row['model'] or '-'}{suffix}")
        return "\n".join(lines)
