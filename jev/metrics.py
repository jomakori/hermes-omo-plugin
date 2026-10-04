from __future__ import annotations

from typing import Any


def _labeled(records: list[dict[str, Any]] | None) -> list[tuple[float, float]]:
    """(confidence, outcome) pairs a calibration metric can use; malformed rows dropped."""
    labeled: list[tuple[float, float]] = []
    for record in records or []:
        if not isinstance(record, dict):
            continue
        agreed = record.get("agreed")
        confidence = record.get("jev_confidence")
        if agreed is None or confidence is None:
            continue
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            continue
        labeled.append((max(0.0, min(1.0, float(confidence))), 1.0 if agreed else 0.0))
    return labeled


def agreement(records: list[dict[str, Any]] | None) -> float:
    """Fraction of labeled records where Jev and the engine made the same call."""
    values = [
        1.0 if record.get("agreed") else 0.0
        for record in records or []
        if isinstance(record, dict) and record.get("agreed") is not None
    ]
    if not values:
        return 0.0
    return sum(values) / len(values)


def ece(records: list[dict[str, Any]] | None, bins: int = 10) -> float:
    """Expected Calibration Error: mean |confidence - accuracy| over confidence bins.

    Confidence 1.0 falls into the last bin; each bucket is weighted by its share
    of labeled records, so a single overconfident row cannot dominate.
    """
    labeled = _labeled(records)
    if not labeled or bins <= 0:
        return 0.0
    total = len(labeled)
    buckets: list[list[tuple[float, float]]] = [[] for _ in range(bins)]
    for confidence, outcome in labeled:
        index = min(int(confidence * bins), bins - 1)
        buckets[index].append((confidence, outcome))
    error = 0.0
    for bucket in buckets:
        if not bucket:
            continue
        mean_confidence = sum(confidence for confidence, _ in bucket) / len(bucket)
        mean_accuracy = sum(outcome for _, outcome in bucket) / len(bucket)
        error += (len(bucket) / total) * abs(mean_confidence - mean_accuracy)
    return error


def brier(records: list[dict[str, Any]] | None) -> float:
    """Brier score: mean squared error of confidence against the 0/1 outcome."""
    labeled = _labeled(records)
    if not labeled:
        return 0.0
    return sum((confidence - outcome) ** 2 for confidence, outcome in labeled) / len(labeled)


def summarize(records: list[dict[str, Any]] | None) -> dict[str, Any]:
    """The report's numbers plus the disagreements, newest first."""
    rows = [record for record in records or [] if isinstance(record, dict)]
    per_pack: dict[str, int] = {}
    for record in rows:
        pack = str(record.get("pack") or "unknown")
        per_pack[pack] = per_pack.get(pack, 0) + 1
    disagreements = [record for record in rows if record.get("agreed") is False]
    disagreements.sort(key=lambda record: str(record.get("ts") or ""), reverse=True)
    return {
        "total": len(rows),
        "per_pack": per_pack,
        "agreement": agreement(rows),
        "ece": ece(rows),
        "brier": brier(rows),
        "disagreements": disagreements,
    }


def _decision_label(record: dict[str, Any]) -> str:
    """A one-line rendering of Jev's verdict for the disagreement list."""
    decisions = record.get("jev_decisions")
    if not isinstance(decisions, dict) or not decisions:
        return "unavailable"
    parts = []
    for name, decision in decisions.items():
        value = decision.get("value") if isinstance(decision, dict) else decision
        parts.append(f"{name}={value}")
    return ", ".join(parts)


def format_report(records: list[dict[str, Any]] | None, limit: int = 10) -> str:
    """Render a ``jev_report`` payload: totals, calibration, and recent disagreements."""
    summary = summarize(records)
    per_pack = ", ".join(f"{pack}={count}" for pack, count in sorted(summary["per_pack"].items())) or "(none)"
    lines = [
        "Jev shadow report",
        f"  records:    {summary['total']}",
        f"  per-pack:   {per_pack}",
        f"  agreement:  {summary['agreement'] * 100:.1f}%",
        f"  ECE:        {summary['ece']:.4f}",
        f"  Brier:      {summary['brier']:.4f}",
    ]
    disagreements = summary["disagreements"][: max(0, limit)]
    if disagreements:
        lines.append(f"  recent disagreements (up to {limit}):")
        for record in disagreements:
            lines.append(
                f"    {record.get('ts', '?')}  {record.get('pack', '?')}  "
                f"jev[{_decision_label(record)}] vs caller={record.get('caller_decision', '?')}"
            )
    else:
        lines.append("  recent disagreements: none")
    return "\n".join(lines)


__all__ = ["agreement", "brier", "ece", "format_report", "summarize"]
