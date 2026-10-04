from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from jev.client import JevClient
from jev.envelope import build_envelope, unavailable_envelope
from jev.packs import PACKS, agent_candidates
from jev.policy import apply_policy
from jev.redact import redact_state

JEV_SCHEMA: dict[str, Any] = {
    "name": "jev_ask",
    "description": (
        "Ask the TypeSafe Jev typed-decision classifier for a probabilistic verdict on a pack "
        "(route_intent, gate_risk, pick_skill, pick_agent). Returns a code-authored advisory envelope — "
        "Jev never executes anything; code owns the action."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "pack": {
                "type": "string",
                "enum": ["route_intent", "gate_risk", "pick_skill", "pick_agent"],
                "description": "Which decision pack to run.",
            },
            "state": {
                "type": "object",
                "description": (
                    "State keys for the pack. "
                    "route_intent / pick_agent: {user_message, last_question, cwd_basename}. "
                    "gate_risk: {action_text, dry_run}. "
                    "pick_skill: {user_request, shortlist}."
                ),
            },
            "skill_shortlist": {
                "type": "object",
                "description": "For pick_skill only: map of skill_name -> one-line description (≤255 entries).",
            },
            "agent_candidates": {
                "type": "object",
                "description": (
                    "For pick_agent only: map of agent/category name -> short description "
                    "(≤255 entries). Omit to use the enabled OMO roster."
                ),
            },
        },
        "required": ["pack", "state"],
    },
}


def make_jev_handler(
    base_url: str,
    model: str,
    timeout_s: float,
    api_key_env: str,
    threshold_overrides: dict[str, float] | None,
    enabled: bool,
) -> Callable[..., Any]:
    async def handler(args: dict[str, Any] | None = None, **_kwargs: Any) -> str:
        params = args or {}
        pack_name = str(params.get("pack") or "").strip()
        state_input: dict[str, Any] = params.get("state") or {}

        pack = PACKS.get(pack_name)
        if pack is None:
            return build_envelope(
                pack=pack_name,
                pack_version=0,
                model=model,
                status="unavailable",
                decisions={},
                usage=None,
                latency_ms=0.0,
                notes=f"Unknown pack '{pack_name}'.",
            )

        defaults_decisions = {
            qid: {
                "value": v,
                "source": "default",
                "confident": False,
                "guidance": "Jev unavailable; using pack default.",
            }
            for qid, v in pack.defaults.items()
        }

        if not enabled:
            return unavailable_envelope(
                pack=pack_name,
                pack_version=pack.version,
                model=model,
                decisions=defaults_decisions,
                latency_ms=0.0,
                notes="jev_enabled=false.",
            )

        api_key = os.environ.get(api_key_env, "")
        if not api_key:
            return unavailable_envelope(
                pack=pack_name,
                pack_version=pack.version,
                model=model,
                decisions=defaults_decisions,
                latency_ms=0.0,
                notes=f"Env var {api_key_env} not set.",
            )

        redacted_state = redact_state(pack_name, state_input)

        questions_payload: dict[str, Any] = {}
        for q in pack.questions:
            criteria = q.criteria
            if pack_name == "pick_skill" and q.id == "skill":
                shortlist = params.get("skill_shortlist") or {}
                criteria = {str(k)[:255]: str(v)[:255] for k, v in list(shortlist.items())[:255]}
            if pack_name == "pick_agent" and q.id == "agent":
                candidates = params.get("agent_candidates") or agent_candidates()
                criteria = {str(k)[:255]: str(v)[:255] for k, v in list(candidates.items())[:255]}
            entry: dict[str, Any] = {"type": q.type, "instructions": q.instructions}
            if criteria is not None:
                entry["criteria"] = criteria
            questions_payload[q.id] = entry

        payload: dict[str, Any] = {
            "model": model,
            "state": redacted_state,
            "questions": questions_payload,
        }

        client = JevClient(base_url=base_url, api_key=api_key, timeout_s=timeout_s)
        response, latency_ms = client.call(payload)

        if response is None:
            return unavailable_envelope(
                pack=pack_name,
                pack_version=pack.version,
                model=model,
                decisions=defaults_decisions,
                latency_ms=latency_ms,
                notes="Jev request failed or timed out.",
            )

        raw_answers: dict[str, Any] = response.get("answers", {})
        usage: dict[str, Any] = response.get("usage", {})
        decisions = apply_policy(
            pack_name=pack_name,
            answers=raw_answers,
            raw_questions=pack.questions,
            threshold_overrides=threshold_overrides,
        )

        return build_envelope(
            pack=pack_name,
            pack_version=pack.version,
            model=response.get("model", model),
            status="ok",
            decisions=decisions,
            usage=usage,
            latency_ms=latency_ms,
        )

    return handler


__all__ = ["JEV_SCHEMA", "make_jev_handler"]
