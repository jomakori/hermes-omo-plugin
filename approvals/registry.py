from __future__ import annotations

import dataclasses
import threading
import time
from typing import Any


@dataclasses.dataclass
class PendingApproval:
    request_id: str
    short_id: str
    digest: str
    description: str
    pattern_key: str
    choices: tuple[str, ...]
    session_key: str | None
    deadline: float
    event: threading.Event = dataclasses.field(default_factory=threading.Event)
    decision: str | None = None

    def as_row(self) -> dict[str, Any]:
        return {
            "id": self.short_id,
            "description": self.description,
            "pattern_key": self.pattern_key,
            "choices": list(self.choices),
            "seconds_left": max(0, int(self.deadline - time.time())),
        }


class PendingRegistry:
    def __init__(self, capacity: int = 6) -> None:
        self._lock = threading.Lock()
        self._capacity = max(1, capacity)
        self._items: dict[str, PendingApproval] = {}

    @property
    def capacity(self) -> int:
        return self._capacity

    def _short_id(self, request_id: str) -> str:
        for length in range(6, len(request_id) + 1):
            candidate = request_id[:length]
            if not any(item.short_id == candidate for item in self._items.values()):
                return candidate
        return request_id

    def add(
        self, request: Any, *, choices: tuple[str, ...], session_key: str | None, timeout_seconds: float
    ) -> PendingApproval | None:
        with self._lock:
            live = [item for item in self._items.values() if item.decision is None and item.deadline > time.time()]
            if len(live) >= self._capacity:
                return None
            pending = PendingApproval(
                request_id=request.request_id,
                short_id=self._short_id(request.request_id),
                digest=request.digest,
                description=str(getattr(request, "description", "") or ""),
                pattern_key=str(getattr(request, "pattern_key", "") or ""),
                choices=choices,
                session_key=session_key,
                deadline=time.time() + max(float(timeout_seconds), 0.0),
            )
            self._items[pending.request_id] = pending
            return pending

    def _find(self, short_id: str) -> PendingApproval | None:
        target = short_id.strip().lower()
        for item in self._items.values():
            if item.short_id == target or item.request_id == target:
                return item
        return None

    def resolve(self, short_id: str, choice: str) -> tuple[bool, str]:
        want = str(choice or "").strip().lower()
        with self._lock:
            pending = self._find(short_id)
            if pending is None:
                return False, "no pending approval with that id"
            if pending.decision is not None:
                return False, "that approval was already answered"
            if want not in pending.choices:
                return False, f"choice must be one of: {', '.join(pending.choices)}"
            pending.decision = want
            pending.event.set()
            return True, f"approval {pending.short_id}: {want}"

    def expire(self) -> list[PendingApproval]:
        now = time.time()
        with self._lock:
            stale = [item for item in self._items.values() if item.decision is None and item.deadline <= now]
            for item in stale:
                item.decision = "deny"
                item.event.set()
            return stale

    def deny_all(self, reason: str = "plugin unloaded") -> int:
        with self._lock:
            live = [item for item in self._items.values() if item.decision is None]
            for item in live:
                item.decision = "deny"
                item.event.set()
            self._items.clear()
            return len(live)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [item.as_row() for item in self._items.values() if item.decision is None]
