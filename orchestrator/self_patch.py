"""A bounded, structured follow-up task spec for a crashed run's own source tree.

When a run ends FAILED or INTERRUPTED with something to go on -- a RETAINED or
EMPTY crash_reason (see ``orchestrator.crash_reason``) -- the failure is worth a
narrow diagnostic look rather than being silently dropped. This module builds the
follow-up task spec describing that look; it does not dispatch it. Wiring an
automatic self-patch dispatch is a separate, later decision that needs its own
explicit review -- an unattended run that patches its own orchestrator is not
something to back into inside a spec-builder.
"""

from __future__ import annotations

from typing import Any

from orchestrator.crash_reason import EMPTY, RETAINED

FAILED = "FAILED"
INTERRUPTED = "INTERRUPTED"
_TRIGGERING_TERMINAL_STATES = (FAILED, INTERRUPTED)

# The marker a spawned self-patch run's own worker is tagged with (see
# ``orchestrator.models.Worker.origin``). The wiring in ``orchestrator.engine``
# checks this before ever building a new self-patch task for a finished run,
# so a self-patch run's own failure can never spawn another one.
SELF_PATCH_ORIGIN = "self_patch"

# A self-patch must never be able to weaken the CI gate that would catch a bad
# self-patch. Hardcoded, not a config knob a self-patch could itself edit around.
FORBIDDEN_PATHS = (".github/workflows", "ci.yaml", "ci.yml")

SELF_PATCH_INSTRUCTION = (
    "This is a narrow diagnostic follow-up to a failed run, targeting the "
    "orchestrator's own source tree. Investigate the crash reason and, only if a "
    "fix is warranted, land it as a pull request -- never auto-merge it. Do not "
    "touch .github/workflows or any CI configuration: a self-patch must never "
    "weaken its own CI gate."
)

# Matches crash_reason's own bound so neither field can blow a task spec's budget.
TITLE_MAX_CHARS = 200
_TRUNCATION_SUFFIX = "…"


def _bounded(text: str | None, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(_TRUNCATION_SUFFIX))] + _TRUNCATION_SUFFIX


def build_self_patch_task(
    *,
    run_id: str,
    terminal_state: str,
    title: str,
    crash_reason: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """A bounded follow-up task spec for a crashed run, or ``None`` if none is warranted.

    Triggers only for a FAILED or INTERRUPTED ``terminal_state`` whose
    ``crash_reason["status"]`` is RETAINED or EMPTY. A WITHHELD crash_reason (or any
    other/missing status) short-circuits this path and returns ``None``: that text
    was dropped whole because it matched a secret/credential pattern, and must never
    be forwarded into a new prompt no matter how useful it might otherwise be.

    The return value is a plain, pure value -- never dispatched, never validated
    against a live agent roster from here. Automatic dispatch of this spec is
    intentionally out of scope for this builder.
    """
    if terminal_state not in _TRIGGERING_TERMINAL_STATES:
        return None
    status = (crash_reason or {}).get("status")
    if status not in (RETAINED, EMPTY):
        return None

    bounded_title = _bounded(title, TITLE_MAX_CHARS)
    reason_text = (crash_reason or {}).get("reason") if status == RETAINED else None

    goal_lines = [
        f"Diagnose a {terminal_state} run ({run_id}) in the omo orchestrator's own source tree.",
        f"Original task: {bounded_title}" if bounded_title else "Original task: (no title recorded)",
    ]
    if reason_text:
        goal_lines.append(f"Crash reason: {reason_text}")
    else:
        goal_lines.append("Crash reason: none recorded -- investigate from the run_id and logs alone.")
    goal_lines.append(SELF_PATCH_INSTRUCTION)

    follow_up_title = _bounded(
        f"Diagnostic follow-up: {bounded_title}" if bounded_title else f"Diagnostic follow-up: {run_id}",
        TITLE_MAX_CHARS,
    )

    return {
        "kind": "self_patch_diagnostic",
        "source_run_id": run_id,
        "source_terminal_state": terminal_state,
        "crash_reason_status": status,
        "title": follow_up_title,
        "goal": "\n".join(goal_lines),
        "target_path": "/opt/data/plugins/omo",
        "forbidden_paths": FORBIDDEN_PATHS,
        "instruction": SELF_PATCH_INSTRUCTION,
        # Not wired to anything: a separate, explicitly-reviewed task must opt this in.
        "auto_dispatch": False,
    }


__all__ = [
    "build_self_patch_task",
    "SELF_PATCH_INSTRUCTION",
    "SELF_PATCH_ORIGIN",
    "FORBIDDEN_PATHS",
    "TITLE_MAX_CHARS",
]
