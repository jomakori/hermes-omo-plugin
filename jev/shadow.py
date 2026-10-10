from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jev.client import JevClient
from jev.packs import PACKS, Pack, agent_candidates
from jev.policy import apply_policy
from jev.redact import redact_state

DEFAULT_SHADOW_PATH = "~/.omo/jev-shadow.jsonl"
DEFAULT_SHADOW_PACKS = ("pick_agent",)


def state_hash(state: dict[str, Any]) -> str:
    """A stable sha256 of the shadow input; the raw state is never written.

    A digest is enough to correlate two shadow rows for the same request without
    storing the prompt/user text the state carries. Canonical (sorted-key) JSON
    keeps the digest stable across dict ordering.
    """
    try:
        canonical = json.dumps(state, sort_keys=True, default=str)
    except Exception:
        canonical = repr(state)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def pack_confidence(decisions: dict[str, Any]) -> float:
    """The scalar confidence of a pack's verdict: the highest per-question confidence.

    ``route_intent`` returns two ``choice`` questions, each with its own
    confidence; calibration needs one number per record, so the most confident
    primitive stands in for the verdict as a whole. A ``noul`` answer carries no
    confidence, so it contributes nothing and a pack with none reads 0.0.
    """
    values: list[float] = []
    for decision in decisions.values():
        if not isinstance(decision, dict):
            continue
        raw = decision.get("confidence")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        values.append(max(0.0, min(1.0, float(raw))))
    return max(values) if values else 0.0


def decisions_agree(jev_decisions: dict[str, Any], caller_decision: str) -> bool:
    """Shadow agreement: does Jev's verdict name the decision the engine made?

    In shadow mode the harness runs ``pick_agent``, whose ``agent`` value is drawn
    from the same candidate map (agents + categories) the engine resolves to — so
    the two share a vocabulary and the comparison is a real, like-for-like match.
    It stays a literal, case-insensitive equality against any Jev decision value;
    no mapping is invented (that remains Phase 3's job).
    """
    if not caller_decision:
        return False
    target = str(caller_decision).strip().lower()
    if not target:
        return False
    for decision in jev_decisions.values():
        value = decision.get("value") if isinstance(decision, dict) else decision
        if value is not None and str(value).strip().lower() == target:
            return True
    return False


def build_payload(pack: Pack, model: str, state: dict[str, Any]) -> dict[str, Any]:
    """The request body for one shadow pack: redacted state + the pack's questions.

    Mirrors the ``jev_ask`` tool's payload so a shadow row is comparable with a
    real call. The state is allowlisted and redacted by ``redact_state`` before it
    leaves the process. ``pick_agent``'s candidate map is resolved here, at call
    time, from the roster — never hardcoded in the pack.
    """
    questions: dict[str, Any] = {}
    for question in pack.questions:
        criteria = question.criteria
        if pack.name == "pick_agent" and question.id == "agent":
            criteria = agent_candidates()
        entry: dict[str, Any] = {"type": question.type, "instructions": question.instructions}
        if criteria is not None:
            entry["criteria"] = criteria
        questions[question.id] = entry
    return {"model": model, "state": redact_state(pack.name, state), "questions": questions}


class ShadowLogger:
    """Append-only JSONL sink: one shadow record per line, best-effort.

    Writes are guarded by a lock (the engine calls from a daemon thread) and never
    raise — a bad path, a full disk, or a host without ``$HOME`` must degrade to a
    silent no-op rather than touch dispatch.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path or DEFAULT_SHADOW_PATH).expanduser()
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def log(
        self,
        *,
        pack: str,
        state: dict[str, Any],
        jev_decisions: dict[str, Any],
        jev_confidence: float,
        caller_decision: str,
        agreed: bool,
        latency_ms: float,
        model: str,
        status: str,
        dispatch_id: str = "",
        source: str = "",
    ) -> bool:
        """Append one record. Returns True on write, False on any failure."""
        entry: dict[str, Any] = {
            "ts": utc_now(),
            "pack": pack,
            # A digest, never the raw state: the state can carry user text.
            "state_hash": state_hash(state),
            "jev_decisions": jev_decisions,
            "jev_confidence": float(jev_confidence),
            "caller_decision": caller_decision,
            "agreed": bool(agreed),
            "latency_ms": float(latency_ms),
            "model": model,
            "status": status,
            # Who chose this dispatch (`jev` when routing ran, else the caller) and
            # which dispatch it was, so the outcome record can join back to it.
            "dispatch_id": str(dispatch_id),
            "source": str(source),
        }
        return self.write(entry)

    def outcome(
        self,
        *,
        dispatch_id: str,
        delivered: bool,
        status: str = "",
        agent: str = "",
        caller_decision: str = "",
        source: str = "",
    ) -> bool:
        """Append what a dispatch actually produced, joining it to its shadow row.

        A shadow record is written before the work exists, so on its own it can
        only say what Jev expected. This is the other half. Agreement against the
        caller measures conformity to the caller's own pick; joining these two on
        ``dispatch_id`` is what makes the report score a routing source against
        delivery instead.
        """
        return self.write(
            {
                "_type": "outcome",
                "ts": utc_now(),
                "dispatch_id": str(dispatch_id),
                "delivered": bool(delivered),
                "status": str(status),
                "agent": str(agent),
                "caller_decision": str(caller_decision),
                "source": str(source),
            }
        )

    def write(self, entry: dict[str, Any]) -> bool:
        """Append a pre-built record. Best-effort: never raises."""
        try:
            line = json.dumps(entry, default=str)
            with self._lock:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            return True
        except Exception:
            return False

    def read(self) -> list[dict[str, Any]]:
        """Read every parseable record. A missing or unreadable sink reads empty."""
        records: list[dict[str, Any]] = []
        try:
            with self._path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(record, dict):
                        records.append(record)
        except FileNotFoundError:
            return []
        except Exception:
            return records
        return records


def run_shadow_pack(
    pack_name: str,
    state: dict[str, Any],
    caller_decision: str,
    *,
    client: JevClient,
    model: str,
    logger: ShadowLogger,
    thresholds: dict[str, float] | None = None,
    dispatch_id: str = "",
    source: str = "",
) -> None:
    """Ask Jev one pack and append the shadow record. Never raises.

    Runs off the hot path: every failure — unknown pack, transport error, policy
    error, a disk that refuses the write — is swallowed, because shadow mode must
    not influence dispatch.
    """
    try:
        pack = PACKS.get(pack_name)
        if pack is None:
            return
        response, latency_ms = client.call(build_payload(pack, model, state))
        if response is None:
            logger.log(
                pack=pack_name,
                state=state,
                jev_decisions={},
                jev_confidence=0.0,
                caller_decision=caller_decision,
                agreed=False,
                latency_ms=latency_ms,
                model=model,
                status="unavailable",
                dispatch_id=dispatch_id,
                source=source,
            )
            return
        decisions = apply_policy(
            pack_name=pack_name,
            answers=response.get("answers", {}),
            raw_questions=pack.questions,
            threshold_overrides=thresholds,
        )
        logger.log(
            pack=pack_name,
            state=state,
            jev_decisions=decisions,
            jev_confidence=pack_confidence(decisions),
            caller_decision=caller_decision,
            agreed=decisions_agree(decisions, caller_decision),
            latency_ms=latency_ms,
            model=str(response.get("model", model)),
            status="ok",
            dispatch_id=dispatch_id,
            source=source,
        )
    except Exception:
        return


def run_shadow(
    *,
    packs: list[str] | tuple[str, ...],
    state: dict[str, Any],
    caller_decision: str,
    path: str | Path | None = None,
    base_url: str = "https://api.typesafe.ai",
    model: str = "jev-latest",
    timeout_s: float = 10.0,
    api_key_env: str = "TYPESAFE_AI_API_KEY",
    thresholds: dict[str, float] | None = None,
    dispatch_id: str = "",
    source: str = "",
) -> None:
    """Run every configured shadow pack and append one record each. Never raises.

    Reads the API key from the environment at call time; a missing key writes an
    ``unavailable`` record instead of failing, so a shadow row still names the
    request that ran without a verdict. Meant to be handed to a daemon thread by
    the engine — it must never block or fail a dispatch.
    """
    try:
        logger = ShadowLogger(path)
        api_key = os.environ.get(api_key_env, "")
        if not api_key:
            for pack_name in packs:
                logger.log(
                    pack=pack_name,
                    state=state,
                    jev_decisions={},
                    jev_confidence=0.0,
                    caller_decision=caller_decision,
                    agreed=False,
                    latency_ms=0.0,
                    model=model,
                    status="unavailable",
                    dispatch_id=dispatch_id,
                    source=source,
                )
            return
        client = JevClient(base_url=base_url, api_key=api_key, timeout_s=float(timeout_s))
        for pack_name in packs:
            run_shadow_pack(
                pack_name,
                state,
                caller_decision,
                client=client,
                model=model,
                logger=logger,
                thresholds=thresholds,
                dispatch_id=dispatch_id,
                source=source,
            )
    except Exception:
        return


__all__ = [
    "DEFAULT_SHADOW_PACKS",
    "DEFAULT_SHADOW_PATH",
    "ShadowLogger",
    "build_payload",
    "decisions_agree",
    "pack_confidence",
    "run_shadow",
    "run_shadow_pack",
    "state_hash",
    "utc_now",
]
