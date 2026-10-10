"""Provider health probe: is a model's provider actually serving right now.

A chain walk (``orchestrator/chains.py``) only learns a hop is dead *after*
paying for a failed dispatch. This module answers the question up front, for a
fraction of the cost: issue one minimal (1-token) completion against the
model and classify what came back.

Verified behaviour this classification is built on (2026-10-10):

- GitHub Copilot answers **HTTP 402** ``quota_exceeded`` when the account's
  quota is drained, and **200** when it is still serving.
- Claude answers **429** with a ``five_hour`` / ``out_of_credits`` body when
  it is rate-limited or out of credits.

Those are two different kinds of "dead" worth telling apart: a drained quota
(402) will not recover until the billing period rolls over; a rate limit
(429) is the account working but throttled *this instant*, and often clears
within seconds. Anything that never reached the provider at all — a raised
exception, a timeout, no parseable status — is classified separately as
``transport``, because it says nothing about the account's quota or rate
limit; conflating it with either would be a false claim about billing state
nobody verified.

The verdict is cached with a short TTL (default 60s) so a burst of dispatches
does not re-probe the same model on every call, and the cache is the surface
chain construction consults (``alive_candidates``) — passively: reading a
cached verdict never triggers a network call, so consulting health is never
on a dispatch's hot path unless something has already probed that model.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

# Reasons a probe can report. ``alive`` is the only non-dead one.
REASON_ALIVE = "alive"
REASON_QUOTA = "quota_exceeded"
REASON_RATE_LIMIT = "rate_limit"
REASON_TRANSPORT = "transport"

DEAD_REASONS: frozenset[str] = frozenset({REASON_QUOTA, REASON_RATE_LIMIT, REASON_TRANSPORT})

# A probe verdict older than this is no longer trusted; the next consult
# re-probes rather than acting on a stale read of a provider's quota.
DEFAULT_TTL_SECONDS = 60.0

__all__ = [
    "DEAD_REASONS",
    "DEFAULT_TTL_SECONDS",
    "REASON_ALIVE",
    "REASON_QUOTA",
    "REASON_RATE_LIMIT",
    "REASON_TRANSPORT",
    "HealthProbeCache",
    "LiteLLMProbeTransport",
    "ProbeResponse",
    "ProbeTransport",
    "Verdict",
    "alive_candidates",
    "classify",
    "probe_once",
]


@dataclass(frozen=True)
class ProbeResponse:
    """What came back from one minimal completion attempt.

    ``status`` is the HTTP status code; ``message`` is a short, truncated body
    for the record (never trusted for classification beyond the status code,
    since that is the one signal this stack has actually verified).
    """

    status: int | None = None
    message: str = ""


class ProbeTransport(Protocol):
    """The test seam: anything callable as ``transport(model) -> ProbeResponse``.

    Production wires a real 1-token completion through the gateway's
    OpenAI-compatible endpoint (:class:`LiteLLMProbeTransport`); a test wires a
    fake that returns a canned :class:`ProbeResponse`, or raises to simulate a
    transport failure (timeout, connection refused, DNS, …).
    """

    def __call__(self, model: str) -> ProbeResponse: ...


@dataclass(frozen=True)
class Verdict:
    """One provider's classified health at a point in time."""

    model: str
    alive: bool
    reason: str
    status: int | None = None
    message: str = ""
    checked_at: float = field(default_factory=time.monotonic)

    def is_stale(self, ttl_seconds: float, *, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        return (current - self.checked_at) >= ttl_seconds


def classify(response: ProbeResponse) -> tuple[bool, str]:
    """(alive, reason) from one probe response.

    402 is the verified GitHub Copilot drained-quota signal; 429 is Claude's
    verified rate-limit/out-of-credits signal. Both are "dead", but for
    different reasons a caller should treat differently (quota does not clear
    on its own soon; a rate limit often does). Any other status — including
    none at all, which a transport-level failure reports as — is ``transport``:
    it is not a claim about the account's billing state, only about reaching
    the provider.
    """
    if response.status == 200:
        return True, REASON_ALIVE
    if response.status == 402:
        return False, REASON_QUOTA
    if response.status == 429:
        return False, REASON_RATE_LIMIT
    return False, REASON_TRANSPORT


def probe_once(model: str, transport: ProbeTransport, *, now: float | None = None) -> Verdict:
    """Issue one minimal completion through ``transport`` and classify it.

    An exception from the transport (timeout, connection error, DNS failure)
    never reaches the caller: it is itself the ``transport`` verdict, with the
    exception's text kept as the message so the cause is not lost.
    """
    checked_at = time.monotonic() if now is None else now
    try:
        response = transport(model)
    except Exception as exc:  # noqa: BLE001 - any transport failure classifies as transport
        return Verdict(model=model, alive=False, reason=REASON_TRANSPORT, message=str(exc), checked_at=checked_at)
    alive, reason = classify(response)
    return Verdict(
        model=model,
        alive=alive,
        reason=reason,
        status=response.status,
        message=response.message,
        checked_at=checked_at,
    )


@dataclass
class HealthProbeCache:
    """Per-model verdict cache with a short TTL.

    ``verdict`` is the active surface: it probes on a miss or an expired
    entry, and reads the cache otherwise. ``cached_verdict`` is the passive
    surface chain construction should prefer: it never probes, so a chain
    build never pays for a network call it didn't ask for — it either knows
    the last (fresh) verdict or it doesn't, in which case the model is treated
    as alive (unprobed is not evidence of dead).
    """

    transport: ProbeTransport
    ttl_seconds: float = DEFAULT_TTL_SECONDS
    _verdicts: dict[str, Verdict] = field(default_factory=dict)
    _clock: Callable[[], float] = field(default=time.monotonic)

    def verdict(self, model: str, *, force: bool = False) -> Verdict:
        """The model's verdict, probing fresh when there is none cached or it expired."""
        now = self._clock()
        cached = self._verdicts.get(model)
        if not force and cached is not None and not cached.is_stale(self.ttl_seconds, now=now):
            return cached
        fresh = probe_once(model, self.transport, now=now)
        self._verdicts[model] = fresh
        return fresh

    def cached_verdict(self, model: str) -> Verdict | None:
        """The last verdict for ``model`` if it is still fresh; None otherwise.

        Never probes. A caller on a hot path (chain construction) consults
        this, not ``verdict``, so reading health is never the thing that
        makes a dispatch slow or costs a network call.
        """
        cached = self._verdicts.get(model)
        if cached is None or cached.is_stale(self.ttl_seconds, now=self._clock()):
            return None
        return cached

    def is_alive(self, model: str) -> bool:
        return self.verdict(model).alive

    def invalidate(self, model: str | None = None) -> None:
        """Drop a cached verdict (or all of them) so the next consult reprobes."""
        if model is None:
            self._verdicts.clear()
        else:
            self._verdicts.pop(model, None)


def alive_candidates(chain: Sequence[str], cache: HealthProbeCache) -> tuple[str, ...]:
    """The chain's models minus any this cache has *actually* classified dead.

    Passive by design (reads ``cached_verdict``, never ``verdict``): a model
    with no fresh verdict yet is kept, because "not probed" is not "dead".
    This is the surface chain construction consults — e.g. a resolver can
    filter ``ChainResolver.chain_for(...)`` through this before handing the
    chain to a dispatch, without this module ever reaching into dispatch
    itself.
    """
    kept = []
    for model in chain:
        verdict = cache.cached_verdict(model)
        if verdict is not None and not verdict.alive:
            continue
        kept.append(model)
    return tuple(kept)


def _short(raw: bytes, limit: int = 300) -> str:
    text = raw.decode("utf-8", errors="replace")
    return text if len(text) <= limit else text[:limit] + "…"


class LiteLLMProbeTransport:
    """Issues a real 1-token completion against the gateway's OpenAI-compatible
    endpoint and reports the raw status back; no retry — a probe is a
    point-in-time read, and retrying would blur a live rate limit into a false
    "alive".
    """

    def __init__(self, base_url: str, api_key: str, timeout_s: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout_s

    def __call__(self, model: str) -> ProbeResponse:
        url = f"{self._base_url}/chat/completions"
        body = json.dumps(
            {
                "model": model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
            }
        ).encode()
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self._api_key}"}
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return ProbeResponse(status=resp.status, message=_short(resp.read()))
        except urllib.error.HTTPError as exc:
            # 402/429/5xx land here, not in the 2xx branch above: urllib raises on
            # any non-2xx status, so the quota/rate-limit signal must be read off
            # the exception rather than off a response that was never returned.
            return ProbeResponse(status=exc.code, message=_short(exc.read()))
