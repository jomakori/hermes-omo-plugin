from __future__ import annotations

from typing import Any

from jev.packs import PACKS

_DEFAULT_THRESHOLDS: dict[str, dict[str, float]] = {
    "route_intent": {
        "tier": 0.5,
        "domain": 0.5,
    },
    "gate_risk": {
        "irreversible": 0.9,
        "external_side_effect": 0.75,
        "destructive": 0.9,
        "secrets_involved": 0.5,
    },
    "pick_skill": {
        "skill": 0.5,
    },
    "pick_agent": {
        "agent": 0.5,
    },
}


def _verdict(question_id: str, answer: dict[str, Any], threshold: float) -> dict[str, Any]:
    q_type = answer.get("_type", "choice")

    if q_type == "noul":
        score = float(answer.get("noul", 0.0))
        confident = score >= threshold
        return {
            "value": score,
            "threshold": threshold,
            "confident": confident,
            "source": "jev",
            "guidance": (
                "Jev flags this risk; code must decide whether to proceed."
                if confident
                else "Jev does not flag this risk at configured threshold."
            ),
        }

    if q_type == "choice":
        choice = answer.get("choice")
        confidence = float(answer.get("confidence", 0.0))
        confident = confidence >= threshold
        return {
            "value": choice,
            "confidence": confidence,
            "threshold": threshold,
            "confident": confident,
            "probabilities": answer.get("probabilities", {}),
            "source": "jev",
            "guidance": (
                f"Jev is confident: {choice}. Code may act on this."
                if confident
                else f"Jev is uncertain (confidence {confidence:.2f} < {threshold}). Treat as advisory only."
            ),
        }

    if q_type == "score":
        score_val = answer.get("score")
        confidence = float(answer.get("confidence", 0.0))
        confident = confidence >= threshold
        return {
            "value": score_val,
            "confidence": confidence,
            "threshold": threshold,
            "confident": confident,
            "legend": answer.get("legend", []),
            "probabilities": answer.get("probabilities", {}),
            "source": "jev",
            "guidance": (
                f"Jev is confident: level {score_val}."
                if confident
                else f"Jev is uncertain (confidence {confidence:.2f} < {threshold}). Treat as advisory only."
            ),
        }

    return {"value": None, "source": "jev", "guidance": "Unknown question type.", "confident": False}


def apply_policy(
    pack_name: str,
    answers: dict[str, Any],
    raw_questions: tuple[Any, ...],
    threshold_overrides: dict[str, float] | None = None,
) -> dict[str, Any]:
    pack = PACKS.get(pack_name)
    if pack is None:
        return {}

    base_thresholds = dict(_DEFAULT_THRESHOLDS.get(pack_name, {}))
    if threshold_overrides:
        base_thresholds.update(threshold_overrides)

    type_map = {q.id: q.type for q in raw_questions}

    decisions: dict[str, Any] = {}
    for q in pack.questions:
        answer = answers.get(q.id)
        if answer is None:
            default_val = pack.defaults.get(q.id)
            decisions[q.id] = {
                "value": default_val,
                "source": "default",
                "confident": False,
                "guidance": "Jev did not return an answer; using pack default.",
            }
            continue
        annotated = dict(answer)
        annotated["_type"] = type_map.get(q.id, "choice")
        threshold = base_thresholds.get(q.id, 0.5)
        decisions[q.id] = _verdict(q.id, annotated, threshold)

    return decisions


__all__ = ["apply_policy", "_DEFAULT_THRESHOLDS"]
