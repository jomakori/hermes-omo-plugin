from __future__ import annotations

from typing import Any

from approvals.registry import PendingRegistry


def make_omo_approve(registry: PendingRegistry) -> Any:
    def handler(raw_args: str = "") -> str:
        parts = str(raw_args or "").split()
        if len(parts) != 2:
            return "usage: /omo-approve <id> <once|session|always|deny>"
        short_id, choice = parts
        ok, message = registry.resolve(short_id, choice)
        return message if ok else f"not approved: {message}"

    return handler


def make_omo_approvals(registry: PendingRegistry) -> Any:
    def handler(raw_args: str = "") -> str:
        rows = registry.snapshot()
        if not rows:
            return "no approvals waiting"
        lines = ["id | what | match | choices | seconds left"]
        for row in rows:
            lines.append(
                f"{row['id']} | {row['description']} | {row['pattern_key']} | "
                f"{', '.join(row['choices'])} | {row['seconds_left']}"
            )
        return "\n".join(lines)

    return handler
