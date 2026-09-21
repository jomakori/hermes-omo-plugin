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

_STATUS_IN_MESSAGE = re.compile(r"\b(?:HTTP\s*)?([45]\d{2})\b")


def status_from_message(message: str | None) -> int | None:
    match = _STATUS_IN_MESSAGE.search(message or "")
    return int(match.group(1)) if match else None


def canonical_model(model: str) -> str:
    base = model.rsplit("/", 1)[-1]
    for suffix in _VARIANT_SUFFIXES:
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    return base


def is_retryable(
    *,
    status: int | None = None,
    error_type: str | None = None,
    message: str = "",
    retry_on_errors: frozenset[int] | None = None,
) -> bool:
    if error_type in NON_RETRYABLE:
        return False
    codes = retry_on_errors if retry_on_errors is not None else RETRYABLE_STATUS
    if status is not None and status in codes:
        return True
    return bool(_RETRYABLE_PATTERN.search(message or ""))


def parse_retry_on_errors(value: Any) -> frozenset[int]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return RETRYABLE_STATUS
    codes = {int(item) for item in value if str(item).strip().lstrip("-").isdigit()}
    return frozenset(codes) if codes else RETRYABLE_STATUS


@dataclass
class FallbackState:
    chain: tuple[str, ...]
    enabled: bool = True
    retry_on_errors: frozenset[int] = RETRYABLE_STATUS
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

    def retryable(self, *, status: int | None = None, error_type: str | None = None, message: str = "") -> bool:
        if not self.enabled:
            return False
        return is_retryable(status=status, error_type=error_type, message=message, retry_on_errors=self.retry_on_errors)

    def in_cooldown(self, model: str, now: float | None = None) -> bool:
        until = self.cooldowns.get(model)
        current = time.time() if now is None else now
        return until is not None and current < until

    def next_model(self, *, now: float | None = None) -> str | None:
        if not self.enabled or self.attempt_count >= self.max_attempts:
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

    def category_chain(self, category: str, default: Sequence[str]) -> tuple[str, ...]:
        override = (self.config.get("categories") or {}).get(category)
        if override:
            return tuple(str(model) for model in override)
        return tuple(default)

    def policy(self, name: str, default: Sequence[str]) -> FallbackState:
        runtime = self.config.get("runtime_fallback")
        runtime = runtime if isinstance(runtime, Mapping) else {}

        def knob(key: str, fallback: Any) -> Any:
            if key in runtime:
                return runtime[key]
            return self.config.get(key, fallback)

        return FallbackState(
            chain=self.chain_for(name, default),
            enabled=bool(knob("enabled", True)),
            retry_on_errors=parse_retry_on_errors(knob("retry_on_errors", None)),
            max_attempts=int(knob("max_fallback_attempts", 3)),
            cooldown_seconds=int(knob("cooldown_seconds", 30)),
            restore_primary_after_cooldown=bool(knob("restore_primary_after_cooldown", True)),
        )

    def state_for(self, name: str, default: Sequence[str]) -> FallbackState:
        return self.policy(name, default)
