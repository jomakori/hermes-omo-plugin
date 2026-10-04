from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any

from jev.client import JevClient
from jev.packs import PACKS
from jev.policy import apply_policy, authoritative_target
from jev.shadow import build_payload


def route_agent(
    *,
    state: dict[str, Any],
    valid_targets: Iterable[str],
    threshold: float,
    base_url: str,
    model: str,
    timeout_s: float,
    api_key_env: str = "TYPESAFE_AI_API_KEY",
    candidates: dict[str, str] | None = None,
) -> tuple[str | None, float, str]:
    """Ask ``pick_agent`` for a dispatch target, synchronously and fail-open.

    Returns ``(target, confidence, status)``. ``target`` is non-None only when Jev
    answered, the pick exists in ``valid_targets``, and its confidence reaches
    ``threshold`` — the authority rule lives in ``policy.authoritative_target``.
    Every failure (missing key, timeout, transport error, malformed body) returns
    ``(None, 0.0, "unavailable"|"error")`` so the caller keeps its static target;
    this function never raises and never blocks past the client's ``timeout_s``.
    """
    try:
        api_key = os.environ.get(api_key_env, "")
        if not api_key:
            return None, 0.0, "unavailable"
        pack = PACKS["pick_agent"]
        payload = build_payload(pack, model, state)
        if candidates is not None:
            payload["questions"]["agent"]["criteria"] = {
                str(key)[:255]: str(value)[:255] for key, value in list(candidates.items())[:255]
            }
        client = JevClient(base_url=base_url, api_key=api_key, timeout_s=float(timeout_s))
        response, _latency_ms = client.call(payload)
        if response is None:
            return None, 0.0, "unavailable"
        decisions = apply_policy("pick_agent", response.get("answers", {}), pack.questions)
        decision = decisions.get("agent", {})
        confidence = 0.0
        if isinstance(decision, dict) and isinstance(decision.get("confidence"), (int, float)):
            confidence = float(decision["confidence"])
        target = authoritative_target(decision, set(valid_targets), float(threshold))
        return target, confidence, "ok"
    except Exception:
        return None, 0.0, "error"


__all__ = ["route_agent"]
