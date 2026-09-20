from __future__ import annotations

import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

RETRYABLE_STATUS: frozenset[int] = frozenset({400, 401, 403, 404, 408, 425, 429, 500, 502, 503, 504, 529})
NON_RETRYABLE: frozenset[str] = frozenset({"abort", "context_overflow"})

_RETRYABLE_PATTERN = re.compile(
    r"rate.?limit|quota|overloaded|too many requests|temporarily unavailable|"
    r"service unavailable|bad gateway|gateway timeout",
    re.IGNORECASE,
)

_VARIANT_SUFFIXES = ("-thinking", "-max", "-high", "-medium", "-low", "-xhigh")


def canonical_model(model: str) -> str:
    base = model.rsplit("/", 1)[-1]
    for suffix in _VARIANT_SUFFIXES:
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    return base


def is_retryable(*, status: int | None = None, error_type: str | None = None, message: str = "") -> bool:
    if error_type in NON_RETRYABLE:
        return False
    if status is not None and status in RETRYABLE_STATUS:
        return True
    return bool(_RETRYABLE_PATTERN.search(message or ""))


@dataclass
class FallbackState:
    chain: tuple[str, ...]
    max_attempts: int = 3
    cooldown_seconds: int = 30
    restore_primary_after_cooldown: bool = True
    original: str = ""
    current_index: int = 0
    attempt_count: int = 0
    cooldowns: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.original and self.chain:
            self.original = self.chain[0]

    def in_cooldown(self, model: str, now: float | None = None) -> bool:
        until = self.cooldowns.get(model)
        current = time.time() if now is None else now
        return until is not None and current < until

    def next_model(self, *, now: float | None = None) -> str | None:
        if self.attempt_count >= self.max_attempts:
            return None
        seen = {canonical_model(model) for model in self.chain[: self.current_index + 1]}
        for index in range(self.current_index + 1, len(self.chain)):
            candidate = self.chain[index]
            canonical = canonical_model(candidate)
            if canonical in seen:
                continue
            seen.add(canonical)
            if self.in_cooldown(candidate, now):
                continue
            self.current_index = index
            self.attempt_count += 1
            return candidate
        return None

    def record_failure(self, model: str, *, now: float | None = None) -> None:
        current = time.time() if now is None else now
        self.cooldowns[model] = current + self.cooldown_seconds

    def primary_if_recovered(self, *, now: float | None = None) -> str | None:
        if self.restore_primary_after_cooldown and not self.in_cooldown(self.original, now):
            return self.original
        return None


@dataclass
class ChainResolver:
    config: Mapping[str, Any] = field(default_factory=dict)

    def chain_for(self, name: str, default: Sequence[str]) -> tuple[str, ...]:
        override = (self.config.get("chains") or {}).get(name)
        if override:
            return tuple(str(model) for model in override)
        return tuple(default)

    def state_for(self, name: str, default: Sequence[str]) -> FallbackState:
        return FallbackState(
            chain=self.chain_for(name, default),
            max_attempts=int(self.config.get("max_fallback_attempts", 3)),
            cooldown_seconds=int(self.config.get("cooldown_seconds", 30)),
            restore_primary_after_cooldown=bool(self.config.get("restore_primary_after_cooldown", True)),
        )
