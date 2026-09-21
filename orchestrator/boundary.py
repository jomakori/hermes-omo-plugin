"""Claim boundaries for OMO results.

A worker's summary is a claim, not evidence: it is text a model wrote about work
the host never watched happen. Every payload that describes worker activity
therefore names the boundary explicitly - which fields the engine read from the
host's own record, and which the worker merely asserted about itself - so a
caller cannot read a prepared claim as observed execution.
"""

from __future__ import annotations

from typing import Any

NOT_EVIDENCE_UNTIL_OBSERVED = "not_evidence_until_observed"

# Read from the host's own record of the run: the engine took the worker's word
# for none of these. Names are field kinds, so a payload may carry only some.
OBSERVED = (
    "status",
    "model",
    "cancelled",
    "result.terminal_state",
    "result.usage_metadata",
    "result.tool_execution_summary",
)

# Authored by the worker. Check it against something the host can see - the
# working tree, the tests, git - before repeating it to the user.
SELF_REPORTED = (
    "result.summary",
    "result.structured_payload",
    "result.error_message",
)


def claim_boundary() -> dict[str, Any]:
    """The boundary carried by every payload that reports worker activity."""
    return {
        "boundary": NOT_EVIDENCE_UNTIL_OBSERVED,
        "observed": list(OBSERVED),
        "self_reported": list(SELF_REPORTED),
    }


__all__ = ["NOT_EVIDENCE_UNTIL_OBSERVED", "OBSERVED", "SELF_REPORTED", "claim_boundary"]
