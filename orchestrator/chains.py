from __future__ import annotations

import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

RETRYABLE_STATUS: frozenset[int] = frozenset({400, 401, 403, 404, 408, 425, 429, 500, 502, 503, 504, 529})
NON_RETRYABLE: frozenset[str] = frozenset({"abort"})
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
# A hop dropped before the walk started because it is served by the same
# provider layer as the hop kept just before it. Nothing was asked of it, so it
# is not a failure and never spends the fallback budget; it rides the hop trail
# so a chain shorter than the configured one says where the rest went.
HOP_REASON_SAME_LAYER = "same_layer"

# --- provider layers --------------------------------------------------------
#
# A fallback hop is only a fallback when it sits in a different layer from the
# hop before it: the same key, quota or proxy behind two aliases means the
# second hop inherits the first one's refusal. `layer_of` is the table that
# decides this, and it is the only place the stack's lanes are named.
#
# `deepseek-v4-flash-go` (opencode-Go plan) and `deepseek-v4-flash-direct` (the
# direct DeepSeek key) serve the same model through different lanes, so they are
# two layers; `claude-sonnet-5` and `claude-haiku-4-5` are one proxy and one
# quota window, so they are one layer.
LAYER_CLAUDE_PROXY = "claude-proxy"
LAYER_OPENCODE_GO = "opencode-go"
LAYER_DEEPSEEK_DIRECT = "deepseek-direct"
LAYER_KILO = "kilo"

# Kilo serves this whole family through one gateway key, so one drained window
# covers every alias in it.
_KILO_PREFIXES = (
    "kimi-",
    "bytedance-",
    "byteplus-coding/",
    "glm-",
    "minimax-",
    "qwen",
    "gemini-",
    "gpt-",
)

# --- layer benches ----------------------------------------------------------
#
# A drained window belongs to a layer, not to one alias and not to a brand:
# claude-sonnet-5 and claude-haiku-4-5 report the same five-hour reset, so
# benching whichever alias failed first just hands the refusal to its sibling.
# Two aliases that merely share an upstream brand are *not* one layer, though —
# a Go-plan drain says nothing about the direct DeepSeek key. These benches are
# held at module level, so they are shared across walks: the next dispatch
# starts already benched rather than paying for the same dead hop again. Expiry
# is the only thing that lifts one.

_LAYER_BENCH: dict[str, float] = {}


# "You've hit your session limit - resets 10:30am (UTC)": the proxy's own words.
_RESET_CLOCK_PATTERN = re.compile(r"resets?\s+(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m", re.IGNORECASE)
# The same fact when the body carries it machine-readably.
_RESETS_AT_PATTERN = re.compile(r"resets?_?at[\"\s:=]+(\d{10,})", re.IGNORECASE)

# A quota refusal that states no reset (Copilot's monthly `quota_exceeded`) is
# benched for a bounded window rather than a guessed one: writing the seat off
# for a month would forfeit it if the window reopened sooner, so the provider
# comes back on its own and the next attempt that reaches it re-probes it.
BOUNDED_BENCH_SECONDS = 900.0

# No subscription window runs longer than hours; a parsed clock further out is
# a misread, not a schedule.
MAX_BENCH_SECONDS = 6 * 3600.0


def bare_alias(model: str) -> str:
    """The alias as the chain names it, without a `litellm/` routing prefix."""
    raw = (model or "").strip()
    while raw.lower().startswith("litellm/"):
        raw = raw[len("litellm/") :]
    return raw.lower()


def layer_of(model: str) -> str:
    """Which key/quota/proxy serves this alias - the unit a fallback must leave.

    A hop only falls back when it is served somewhere else than the hop before
    it. Two aliases on the same key (the Claude proxy's sonnet and haiku share
    one five-hour window) are one layer however different their names look; the
    DeepSeek Go plan and the direct DeepSeek key serve the same model through
    different lanes and are therefore two.

    The table is exhaustive by prefix, with the alias itself as the fallback
    layer for anything unlisted - an unknown model is never silently merged into
    a neighbour it may not share a quota with.
    """
    alias = bare_alias(model)
    if not alias:
        return ""
    if alias.startswith("claude-"):
        return LAYER_CLAUDE_PROXY
    if alias.endswith("-go") or alias.startswith("mimo-"):
        return LAYER_OPENCODE_GO
    if alias.endswith("-direct"):
        return LAYER_DEEPSEEK_DIRECT
    for prefix in _KILO_PREFIXES:
        if alias.startswith(prefix):
            return LAYER_KILO
    return alias


def collapse_same_layer(chain: Sequence[str]) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    """Drop hops served by the same layer as the hop kept just before them.

    Hop 1 is always kept, and the surviving hops keep their configured order.
    The second element names every dropped hop against the kept hop it repeated
    (`skipped_model, kept_model`) so the hop trail can say where the length
    went instead of silently reporting a chain the operator never wrote.
    """
    kept: list[str] = []
    skipped: list[tuple[str, str]] = []
    for model in chain:
        if not kept or layer_of(model) != layer_of(kept[-1]):
            kept.append(model)
        else:
            skipped.append((model, kept[-1]))
    return tuple(kept), tuple(skipped)


def reset_at(message: str | None, *, now: float | None = None) -> float | None:
    """When a refusal says its window refills, or None when it does not say.

    Read from the message on every refusal, never cached: Claude's five-hour
    window rolls, so its reset moves with it (00:30, 05:30 and 10:30 UTC have
    all been observed in one day). Reading it fresh is what keeps the bench
    honest; a stored value would expire a live window early or late.
    """
    text = message or ""
    stamped = _RESETS_AT_PATTERN.search(text)
    if stamped:
        epoch = float(stamped.group(1))
        if epoch > 0:
            return epoch

    clock = _RESET_CLOCK_PATTERN.search(text)
    if not clock:
        return None
    current = time.time() if now is None else now
    hour = int(clock.group(1)) % 12
    minute = int(clock.group(2) or 0)
    if clock.group(3).lower() == "p":
        hour += 12

    # The message states a UTC clock, so build the candidate from the UTC date.
    utc = time.gmtime(current)
    midnight = current - (utc.tm_hour * 3600 + utc.tm_min * 60 + utc.tm_sec)
    candidate = midnight + hour * 3600 + minute * 60
    if candidate <= current:
        # Already past: the next occurrence of that clock time is the one meant.
        candidate += 86400
    # A reading further out than any subscription window is a misread, so treat
    # it as "no timestamp" and let the bounded bench handle it.
    return candidate if candidate - current <= MAX_BENCH_SECONDS else None


def _seat_is_drained(reason: str | None) -> bool:
    """True when a hop's reason says the account itself is out, not this hop.

    A rate limit is deliberately absent. A bare 429 is usually a momentary
    spike that says nothing about the subscription, and taking a whole provider
    out for one would over-reach. When a rate limit *is* a drained window, the
    refusal names its reset and is benched on that evidence instead.
    """
    return str(reason or "") in (HOP_REASON_PROVIDER_UNUSABLE, HOP_REASON_BILLING)


def bench_layer(layer: str, until: float, *, now: float | None = None) -> None:
    """Hold a provider layer out of every chain until `until`."""
    current = time.time() if now is None else now
    if not layer or until <= current:
        return
    # The longer window wins: a short bounded bench must never shorten a reset
    # the layer itself has already stated.
    if until > _LAYER_BENCH.get(layer, 0.0):
        _LAYER_BENCH[layer] = until


def layer_benched_until(layer: str, *, now: float | None = None) -> float | None:
    """When this layer may be tried again, or None when it is not benched."""
    current = time.time() if now is None else now
    until = _LAYER_BENCH.get(layer)
    if until is None or until <= current:
        _LAYER_BENCH.pop(layer, None)
        return None
    return until


def layer_benched(model: str, *, now: float | None = None) -> bool:
    """True while the layer behind this alias is held out."""
    return layer_benched_until(layer_of(model), now=now) is not None


def skip_benched(chain: Sequence[str], *, now: float | None = None) -> tuple[str, ...]:
    """The chain minus every candidate whose provider is benched.

    Applied as each walk is built, so a bench that has expired is simply absent
    from the next walk's chain and recovery needs no separate scheduler. With
    everything benched the chain is returned untouched: there is no live hop to
    move to, and one refusal says so more cheaply than a hard stop.
    """
    models = tuple(chain)
    return tuple(model for model in models if not layer_benched(model, now=now)) or models


def clear_bench() -> None:
    """Drop every bench (tests only)."""
    _LAYER_BENCH.clear()


_RETRYABLE_PATTERN = re.compile(
    r"rate.?limit|quota|overloaded|too many requests|temporarily unavailable|"
    r"service unavailable|bad gateway|gateway timeout|session limit|usage_limited|hit your limit",
    re.IGNORECASE,
)

# A window too small for the prompt: this rung cannot serve it, a larger one may.
_OVERFLOW_PATTERN = re.compile(r"context_overflow|prompt too long", re.IGNORECASE)

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
        if any(word in message.lower() for word in ("rate", "quota", "too many", "session limit")):
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
    if error_type == "context_overflow" or _OVERFLOW_PATTERN.search(message or ""):
        return True
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
    # Hops dropped at build time for sharing a layer with the hop kept before
    # them, as (dropped_model, kept_model). They were never attempted, so they
    # stay out of `chain`, `current_index` and `attempt_count`; `trace` splices
    # them back into the trail where the chain had them.
    skipped: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.original and self.chain:
            self.original = self.chain[0]

    def retryable(self, *, status: int | None = None, error_type: str | None = None, message: str = "") -> bool:
        if not self.enabled:
            return False
        return is_retryable(status=status, error_type=error_type, message=message, retry_on_errors=self.retry_on_errors)

    def in_cooldown(self, model: str, now: float | None = None) -> bool:
        # Provider benches are applied once, when the walk's chain is built
        # (`skip_benched`), rather than re-consulted per hop: a walk that has
        # started is allowed to finish, and a bench that lifts mid-walk is
        # picked up by the next walk instead of by a half-advanced one.
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

    def record_failure(
        self,
        model: str,
        *,
        now: float | None = None,
        reason: str | None = None,
        message: str | None = None,
    ) -> None:
        """Bench the model and record why this hop was abandoned.

        When the reason says the seat itself is unusable, the bench widens from
        this alias to its whole layer and lasts as long as the refusal says:
        the reset it names, or a bounded window when it names none. Widening to
        the layer - not to the brand - is what keeps a drained Go plan from
        deleting the direct key's hop out of the same walk.
        """
        current = time.time() if now is None else now
        self.cooldowns[model] = current + self.cooldown_seconds
        if reason:
            self.hop_history.append({"model": model, "reason": reason})
        # Two things bench a whole layer, and both are evidence the drain
        # belongs to the layer rather than to this hop: a refusal that names
        # when its window refills (Claude's rolling five hours), or one that
        # says the account is out with no time attached (a quota drain). A bare
        # 429 is neither, so it stays a cooldown on the one model.
        reset = reset_at(message, now=current)
        if reset is not None or _seat_is_drained(reason):
            bench_layer(layer_of(model), reset or (current + BOUNDED_BENCH_SECONDS), now=current)

    def record_success(self, model: str) -> None:
        """Record the hop that served the request — where this walk exited."""
        self.hop_history.append({"model": model, "reason": HOP_REASON_SUCCESS})

    def trace(self) -> list[dict[str, str]]:
        """The walk as a reader sees it, with the dropped hops still in place.

        Attempted hops come in the order they were tried; each hop dropped for
        sharing a layer rides directly behind the kept hop it repeated, so a
        chain shorter than the configured one still accounts for every entry the
        operator wrote. A dropped hop is never a failure: nothing was asked of
        it, and it spent none of the fallback budget.
        """
        trail: list[dict[str, str]] = []
        pending = list(self.skipped)
        for hop in self.hop_history:
            trail.append(hop)
            served = canonical_model(str(hop.get("model") or ""))
            for skipped in tuple(pending):
                if canonical_model(skipped[1]) == served:
                    trail.append({"model": bare_alias(skipped[0]), "reason": HOP_REASON_SAME_LAYER})
                    pending.remove(skipped)
        # A dropped hop whose keeper the walk never reached (a stage that failed
        # on hop 1) leads the trail rather than closing it: the last entry must
        # stay the hop the walk actually ended on, which is what the retry
        # policy reads to decide why the stage stopped.
        trail[:0] = [{"model": bare_alias(dropped), "reason": HOP_REASON_SAME_LAYER} for dropped, _kept in pending]
        return trail

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

        # Built per walk, so a bench that has expired is already absent from
        # this chain and the layer returns to it without any scheduler. A hop
        # sharing the previous hop's layer is not a fallback at all - it would
        # meet the same refusal one hop later - so it is collapsed here and
        # named in `skipped` for the trail.
        chain, skipped = collapse_same_layer(skip_benched(self.chain_for(name, default)))
        return FallbackState(
            chain=chain,
            skipped=skipped,
            enabled=bool(knob("enabled", True)),
            retry_on_errors=parse_retry_on_errors(knob("retry_on_errors", None)),
            max_attempts=int(knob("max_fallback_attempts", 3)),
            cooldown_seconds=int(knob("cooldown_seconds", 30)),
            restore_primary_after_cooldown=bool(knob("restore_primary_after_cooldown", True)),
        )

    def state_for(self, name: str, default: Sequence[str]) -> FallbackState:
        return self.policy(name, default)
