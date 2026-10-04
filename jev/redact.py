from __future__ import annotations

import re
from typing import Any

from jev.packs import PACKS

_SECRET_PATTERNS = [
    re.compile(r"Bearer\s+\S+", re.IGNORECASE),
    re.compile(r"(api[_-]?key|token|secret|password|passwd|credential)\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"[A-Za-z0-9+/]{40,}={0,2}"),
]

_REDACTED = "[REDACTED]"


def _redact_string(value: str) -> str:
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub(_REDACTED, value)
    return value


def _build_state(pack_name: str, state_input: dict[str, Any]) -> str:
    pack = PACKS.get(pack_name)
    if pack is None:
        return ""
    allowed = {k: state_input.get(k, "") for k in pack.state_keys}
    parts = []
    for k, v in allowed.items():
        if v:
            parts.append(f"{k}: {v}")
    return "\n".join(parts)


def redact_state(pack_name: str, state_input: dict[str, Any]) -> str:
    raw = _build_state(pack_name, state_input)
    return _redact_string(raw)


__all__ = ["redact_state"]
