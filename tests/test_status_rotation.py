"""Rotation rules for the cycling line: naming the work, never echoing the task.

``status_rotation`` is a leaf module (no I/O, no tracker), so the phrase rules
that the live message depends on are asserted here on their own: a phrase names
the work's title, never the raw ticket/task text, and a phrase is cut on a word
boundary rather than mid-token.
"""

from __future__ import annotations

from orchestrator.status_rotation import cycling_phrase, derive_title


def test_derive_title_strips_a_leading_ticket_and_paren_aside():
    assert derive_title("OMR-11 (repo /x): do the thing") == "do the thing"


def test_derive_title_takes_the_first_clause():
    assert derive_title("shell parity — and then some") == "shell parity"


def test_derive_title_is_word_bounded_and_never_mid_token():
    source = "implement the whole navigation badge semantics end to end"
    title = derive_title(source)
    assert len(title) <= 20
    assert source.startswith(title)
    # The cut fell between two words, never through one.
    assert source[len(title)] == " "


def test_cycling_phrase_prefers_the_observed_activity():
    assert cycling_phrase("run", 0, activity="📖 read_file a.py", task="OMR-11 (repo /x): do it") == (
        "📖 read_file a.py"
    )


def test_cycling_phrase_names_the_work_and_never_echoes_the_task():
    phrase = cycling_phrase("run", 0, task="OMR-11 (repo /x): do the thing")
    assert "task:" not in phrase
    assert "OMR-11" not in phrase
    assert "(repo" not in phrase
    # The phrase is tied to the derived title, not the raw task text.
    assert phrase == "reading the code: do the"


def test_cycling_phrase_is_phase_aware_and_bounded():
    # Each phase rotates its own phrase set, each naming the derived title.
    assert cycling_phrase("dispatch", 0, task="wiring the change") == "handing off to the worker:"
    assert cycling_phrase("review", 0, task="review the config") == "reviewing the change: review"
    for phase in ("dispatch", "run", "review"):
        phrase = cycling_phrase(phase, 0, task="implement " + "verylongword " * 20)
        assert len(phrase) <= 29, phrase
