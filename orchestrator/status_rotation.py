"""Rotation cadence and cycling phrases for the live status message.

The message must visibly *live* while a worker runs: between real transitions
the ``  ↳ cycling: <phrase> (<n>s)`` line is re-rendered on this cadence, walking
through a small phrase set so the edit is never a no-op. Both the interval and
the phrase set are module-level constants (configuration is not required yet),
and this module does no I/O, so rotation is unit-testable on its own.
"""

from __future__ import annotations

# The cadence the cycling line is re-rendered at, in seconds. It is part of the
# rendered line ("(3s)") and of the tracker's rotation timer.
ROTATE_INTERVAL_SECONDS = 3.0

# Cycling phrases per phase. Small, human, and observed to change so the message
# reads as alive between transitions rather than frozen on the last event.
CYCLING_PHRASES: dict[str, tuple[str, ...]] = {
    "dispatch": (
        "handing off to the worker…",
        "opening the session…",
    ),
    "run": (
        "reading the code…",
        "patching the working tree…",
        "wiring the change…",
        "running the checks…",
    ),
    "review": (
        "reviewing the change…",
        "weighing the risks…",
        "checking the edges…",
    ),
}


def cycling_phrase(phase: str, index: int, activity: str | None = None) -> str:
    """The rotating phrase for one phase.

    An explicit ``activity`` (the phase's current real activity, when the caller
    knows it) wins; otherwise the module's phrase set cycles by ``index`` so the
    line keeps changing even when no new event has arrived.
    """
    if activity is not None and str(activity).strip():
        return str(activity).strip()
    options = CYCLING_PHRASES.get(phase) or CYCLING_PHRASES["run"]
    if not options:
        return ""
    return options[int(index) % len(options)]


__all__ = ["CYCLING_PHRASES", "ROTATE_INTERVAL_SECONDS", "cycling_phrase"]
