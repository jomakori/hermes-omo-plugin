from __future__ import annotations

import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

RETRYABLE_STATUS: frozenset[int] = frozenset({400, 401, 403, 404, 408, 425, 429, 500, 502, 503, 504, 529})
NON_RETRYABLE: frozenset[str] = frozenset({"abort", "context_overflow"})
HOP_REASON_TRANSPORT = "transport"
HOP_REASON_RATE_LIMIT = "rate_limit"
HOP_REASON_SEMANTIC = "semantic"
HOP_REASON_SUCCESS = "success"
# Credits-out is its own bucket: it is the dominant failure on this stack and it
# is not fixable by retrying or by another hop on the same drained account.
HOP_REASON_BILLING = "billing"
# A provider that answers with its own "this account cannot serve right now"
# code — GitHub Copilot's 402 `quota_exceeded` being the verified case (see
# orchestrator/health_probe.py) — is not the same claim as billing: nobody
# necessarily owes money, the account is simply unusable until its quota
# window resets (a seat-based premium-request allowance, not a balance). It
# is kept apart from HOP_REASON_BILLING so a hop dropped for this reason can
# be exempted from the stage's model-capability attempt budget (see
# engine.py) without changing behaviour for the billing bucket, whose
# counting is deliberately left as-is.
HOP_REASON_PROVIDER_UNUSABLE = "provider_unusable"
# A worker whose process exited cleanly but whose claimed branch / PR / commit
# could not be found on the remote (see orchestrator/artifact_verify.py). It is
# never produced by `classify_failure_reason`: no provider failed, the hop
# served the request. It names the one thing a plain SUCCEEDED cannot say —
# that nobody confirmed the work exists — so a reader is not handed a claim as
# though the host had watched it happen.
HOP_REASON_UNVERIFIABLE = "unverifiable"

_RETRYABLE_PATTERN = re.compile(
    r"rate.?limit|quota|overloaded|too many requests|temporarily unavailable|"
    r"service unavailable|bad gateway|gateway timeout",
    re.IGNORECASE,
)

# A depleted provider answers 402, or wraps the same billing body in 403/404/429.
_BILLING_PATTERN = re.compile(
    r"insufficient (?:balance|funds)|add credits|can only afford|payment required|billing|out of funds",
    re.IGNORECASE,
)

# GitHub Copilot's verified 402 body for a drained seat quota (see
# orchestrator/health_probe.py REASON_QUOTA, built on the same signal). This is
# kept apart from `_BILLING_PATTERN`: nobody owes money on a `quota_exceeded`
# seat allowance, the account is simply unusable until the window resets, so
# it is not the same claim a credits-out body makes.
_PROVIDER_UNUSABLE_PATTERN = re.compile(r"quota_exceeded", re.IGNORECASE)

# The harness can close a turn it could not serve as a *succeeded* child, with
# the failure carried only in the summary text ("No reply: every provider in the
# fallback chain kept failing over" - agent/turn_explainers.py). Behind that hop
# nothing answered, so the walk must treat it as a provider failure and continue
# to its next (credit-independent) model rather than accepting an empty turn.
_NO_REPLY_PATTERN = re.compile(r"no reply|kept failing over", re.IGNORECASE)

_VARIANT_SUFFIXES = ("-thinking", "-max", "-high", "-medium", "-low", "-xhigh")

_STATUS_IN_MESSAGE = re.compile(r"\b(?:HTTP\s*)?([45]\d{2})\b")

# A client disconnect is not a provider failure: the caller that asked for the work
# is gone, so the answer has nowhere to land and re-issuing the request can only
# spend tokens nobody can receive. It is therefore never retryable, whatever
# `retry_on_errors` is configured to say.
CLIENT_DISCONNECT_STATUS = 499
_CLIENT_DISCONNECT_PATTERN = re.compile(
    r"client (?:closed|disconnected|gone|went away)|client_closed_request|client_disconnected|"
    r"connection closed (?:by|before) (?:the )?client",
    re.IGNORECASE,
)


def status_from_message(message: str | None) -> int | None:
    match = _STATUS_IN_MESSAGE.search(message or "")
    return int(match.group(1)) if match else None


def classify_failure_reason(*, status: int | None = None, error_type: str | None = None, message: str = "") -> str:
    """Why one hop was abandoned: billing, rate_limit, transport, semantic, or
    provider_unusable.

    `provider_unusable` is tested before billing even though both can arrive on
    a 402: GitHub Copilot's `quota_exceeded` body is a seat-quota drain, not a
    credit balance, so it must not collapse into the billing bucket (see
    `_PROVIDER_UNUSABLE_PATTERN`). Billing is tested before the remaining
    retryable patterns because a credit body often arrives wrapped in a
    retryable status, and a drained account must not be recorded as a
    transient one.
    """
    if error_type in NON_RETRYABLE:
        return HOP_REASON_SEMANTIC
    if _PROVIDER_UNUSABLE_PATTERN.search(message or ""):
        return HOP_REASON_PROVIDER_UNUSABLE
    if provider_exhausted(message):
        return HOP_REASON_PROVIDER_UNUSABLE
    if status == 402 or _BILLING_PATTERN.search(message or ""):
        return HOP_REASON_BILLING
    if status == 429:
        return HOP_REASON_RATE_LIMIT
    if status and status >= 500:
        return HOP_REASON_TRANSPORT
    if _RETRYABLE_PATTERN.search(message or ""):
        if "rate" in message.lower() or "quota" in message.lower() or "too many" in message.lower():
            return HOP_REASON_RATE_LIMIT
        return HOP_REASON_TRANSPORT
    return HOP_REASON_SEMANTIC


def client_disconnected(message: str | None) -> bool:
    """True when the failure says the client that wanted the answer has gone away.

    A 499 is the proxy's report that the caller closed the request; the CLI behind
    it is gone and its answer can never be delivered, so the walk must not pay for
    another hop.
    """
    if status_from_message(message) == CLIENT_DISCONNECT_STATUS:
        return True
    return bool(_CLIENT_DISCONNECT_PATTERN.search(message or ""))


def provider_exhausted(message: str | None) -> bool:
    """True when the failure says no provider behind the hop had an answer left."""
    return bool(_NO_REPLY_PATTERN.search(message or ""))


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
    if provider_exhausted(message):
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
    # Per-hop outcome of this walk, in order, as {model, reason}: copied onto the
    # worker row so a cascade is legible after the fact.
    hop_history: list[dict[str, str]] = field(default_factory=list)

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

    def record_failure(self, model: str, *, now: float | None = None, reason: str | None = None) -> None:
        """Bench the model and record why this hop was abandoned."""
        current = time.time() if now is None else now
        self.cooldowns[model] = current + self.cooldown_seconds
        if reason:
            self.hop_history.append({"model": model, "reason": reason})

    def record_success(self, model: str) -> None:
        """Record the hop that served the request — where this walk exited."""
        self.hop_history.append({"model": model, "reason": HOP_REASON_SUCCESS})

    def primary_if_recovered(self, *, now: float | None = None) -> str | None:
        if self.restore_primary_after_cooldown and not self.in_cooldown(self.original, now):
            return self.original
        return None


# The separator joining hops in a rendered trail. Exported so the renderer
# (orchestrator.status_message) can degrade a too-long trail by dropping whole
# hops on this boundary rather than word-cutting a model name in half.
HOP_ARROW = " → "


def render_hop_trail(hop_history: Sequence[Mapping[str, Any]]) -> str:
    """The whole chain walk, in order, as ``model (reason) → model (reason) → ...``.

    Every hop that was tried rides here, not just the last failed one: a
    cascade is only legible after the fact if the row says which models were
    dropped, why, and which one finally answered. The hop that served is
    spelled ``served`` rather than echoing the internal ``success`` token.

    Empty when the walk never left the primary — a single hop that served the
    request outright says nothing a reader needs — and empty for an empty
    history. A worker whose primary failed with no further hop to try still
    gets its one entry named: that failure *is* the worker's whole story.
    """
    hops = [hop for hop in hop_history if isinstance(hop, dict)]
    if not hops:
        return ""
    if len(hops) == 1 and str(hops[0].get("reason") or "").strip() == HOP_REASON_SUCCESS:
        return ""
    parts: list[str] = []
    for hop in hops:
        reason = str(hop.get("reason") or "").strip()
        if not reason:
            continue
        model = str(hop.get("model") or "").strip()
        label = "served" if reason == HOP_REASON_SUCCESS else reason.replace("_", " ")
        parts.append(f"{model} ({label})" if model else f"({label})")
    return HOP_ARROW.join(parts)


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
