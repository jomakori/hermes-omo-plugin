"""Bounded, redacted crash reason attached to a FAILED/INTERRUPTED/cancelled hop.

A raw exception message may carry a secret (API key, bearer token, a password
in a URL) or an absolute filesystem path; neither belongs on a record a reader
might later paste elsewhere, and an unbounded message can blow a status
payload's budget. Reuses jev.redact's secret patterns rather than keeping a
second list that would drift from the first.
"""

from __future__ import annotations

import re

from jev.redact import _SECRET_PATTERNS

# Matches the oh-my-hermes convention this helper is based on.
CRASH_REASON_MAX_CHARS = 200

RETAINED = "RETAINED"
WITHHELD = "WITHHELD"
EMPTY = "EMPTY"

# An absolute unix path. Not preceded by ':' or a word char, so a URL's
# "https://host/path" is left for the secret patterns rather than mangled here.
_ABS_PATH_PATTERN = re.compile(r"(?<![:\w])/(?:[\w.\-]+/)+[\w.\-]*")
_PATH_PLACEHOLDER = "<path>"


def crash_reason_for(message: str | None) -> dict[str, str]:
    """A hop's terminal error, bounded and safe to store on the record.

    ``EMPTY`` when there was no message/exception text at all. ``WITHHELD``
    when the text matched a secret/credential pattern: the whole message is
    dropped rather than redacted in place, since a message that *contains* a
    secret is not trusted to be safe to show with just that one span cut out.
    Otherwise ``RETAINED``, with the text's absolute paths swapped for a
    generic placeholder and the result bounded to `CRASH_REASON_MAX_CHARS`.
    """
    text = (message or "").strip()
    if not text:
        return {"status": EMPTY}
    if any(pattern.search(text) for pattern in _SECRET_PATTERNS):
        return {"status": WITHHELD}
    redacted = _ABS_PATH_PATTERN.sub(_PATH_PLACEHOLDER, text)
    return {"status": RETAINED, "reason": redacted[:CRASH_REASON_MAX_CHARS]}


__all__ = ["crash_reason_for", "RETAINED", "WITHHELD", "EMPTY", "CRASH_REASON_MAX_CHARS"]
