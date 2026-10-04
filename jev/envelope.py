from __future__ import annotations

import json
from typing import Any


def build_envelope(
    pack: str,
    pack_version: int,
    model: str,
    status: str,
    decisions: dict[str, Any],
    usage: dict[str, Any] | None,
    latency_ms: float,
    notes: str = "",
) -> str:
    cost_usd: float | None = None
    if usage:
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        cost_usd = round((input_tokens * 0.000002) + (output_tokens * 0.000008), 8)

    envelope: dict[str, Any] = {
        "pack": pack,
        "pack_version": pack_version,
        "backend": "typesafe",
        "model": model,
        "status": status,
        "decisions": decisions,
        "usage": usage or {},
        "cost_usd": cost_usd,
        "latency_ms": latency_ms,
        "notes": notes,
    }
    return json.dumps(envelope, default=str)


def unavailable_envelope(
    pack: str,
    pack_version: int,
    model: str,
    decisions: dict[str, Any],
    latency_ms: float,
    notes: str = "",
) -> str:
    return build_envelope(
        pack=pack,
        pack_version=pack_version,
        model=model,
        status="unavailable",
        decisions=decisions,
        usage=None,
        latency_ms=latency_ms,
        notes=notes,
    )


__all__ = ["build_envelope", "unavailable_envelope"]
