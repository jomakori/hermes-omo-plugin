"""crash_reason_for: bounded, redacted summary attached to a terminal hop.

Closed vocabulary: RETAINED (safe text, truncated/redacted), WITHHELD (matched
a secret/credential pattern, dropped whole), EMPTY (no text at all).
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from orchestrator.crash_reason import CRASH_REASON_MAX_CHARS, EMPTY, RETAINED, WITHHELD, crash_reason_for


def test_a_normal_exception_message_is_retained():
    result = crash_reason_for("RuntimeError: connection reset while streaming the response")
    assert result == {
        "status": RETAINED,
        "reason": "RuntimeError: connection reset while streaming the response",
    }


def test_a_long_message_is_truncated_to_the_bound():
    # Words, not one long alnum run: a 40+ char unbroken run reads as a
    # base64-ish secret (see jev.redact's generic high-entropy pattern) and
    # must not trip that path here — this message is long for a plain reason.
    message = "RuntimeError: the worker closed without producing output " * 5
    assert len(message) > CRASH_REASON_MAX_CHARS
    result = crash_reason_for(message)
    assert result["status"] == RETAINED
    assert len(result["reason"]) == CRASH_REASON_MAX_CHARS
    assert result["reason"] == message.strip()[:CRASH_REASON_MAX_CHARS]


def test_a_message_with_an_api_key_is_withheld_entirely():
    result = crash_reason_for("auth failed: Bearer sk-abcdef1234567890abcdef1234567890 rejected")
    assert result == {"status": WITHHELD}
    assert "sk-abcdef1234567890abcdef1234567890" not in str(result)


def test_a_message_with_a_bare_credential_assignment_is_withheld():
    result = crash_reason_for("startup failed: api_key=supersecretvalue1234 invalid")
    assert result["status"] == WITHHELD
    assert "supersecretvalue1234" not in str(result)


def test_no_message_at_all_is_empty():
    assert crash_reason_for(None) == {"status": EMPTY}
    assert crash_reason_for("") == {"status": EMPTY}
    assert crash_reason_for("   ") == {"status": EMPTY}


def test_an_absolute_path_is_redacted_in_retained_output():
    result = crash_reason_for("could not write artifact to /home/user/secret-project/output.json")
    assert result["status"] == RETAINED
    assert "/home/user/secret-project/output.json" not in result["reason"]
    assert "<path>" in result["reason"]
