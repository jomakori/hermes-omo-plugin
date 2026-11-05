"""build_self_patch_task: a bounded follow-up task spec for a crashed run.

Pure builder -- never dispatches. Only triggers on a FAILED/INTERRUPTED run
whose crash_reason status is RETAINED or EMPTY; WITHHELD must never reach a
new prompt no matter what.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from orchestrator.self_patch import (
    FORBIDDEN_PATHS,
    SELF_PATCH_INSTRUCTION,
    TITLE_MAX_CHARS,
    build_self_patch_task,
)


def test_withheld_crash_reason_refuses_and_returns_none():
    task = build_self_patch_task(
        run_id="omo_abc123",
        terminal_state="FAILED",
        title="Fix the thing",
        crash_reason={"status": "WITHHELD"},
    )
    assert task is None


def test_withheld_crash_reason_refuses_even_on_interrupted():
    task = build_self_patch_task(
        run_id="omo_abc123",
        terminal_state="INTERRUPTED",
        title="Fix the thing",
        crash_reason={"status": "WITHHELD"},
    )
    assert task is None


def test_retained_crash_reason_builds_spec_with_ci_exclusion_verbatim():
    crash_reason = {"status": "RETAINED", "reason": "RuntimeError: connection reset"}
    task = build_self_patch_task(
        run_id="omo_def456",
        terminal_state="FAILED",
        title="Add a new gateway hop",
        crash_reason=crash_reason,
    )
    assert task is not None
    assert task["source_run_id"] == "omo_def456"
    assert task["source_terminal_state"] == "FAILED"
    assert task["crash_reason_status"] == "RETAINED"
    assert "RuntimeError: connection reset" in task["goal"]
    # The CI-config exclusion instruction is present verbatim, not paraphrased.
    assert SELF_PATCH_INSTRUCTION in task["goal"]
    assert task["instruction"] == SELF_PATCH_INSTRUCTION
    assert ".github/workflows" in SELF_PATCH_INSTRUCTION
    assert ".github/workflows" in FORBIDDEN_PATHS
    assert task["auto_dispatch"] is False


def test_empty_crash_reason_still_builds_a_minimal_spec():
    task = build_self_patch_task(
        run_id="omo_ghi789",
        terminal_state="INTERRUPTED",
        title="Some task",
        crash_reason={"status": "EMPTY"},
    )
    assert task is not None
    assert task["crash_reason_status"] == "EMPTY"
    # No reason text to include, but still a narrow diagnostic look is warranted.
    assert "none recorded" in task["goal"].lower()
    assert SELF_PATCH_INSTRUCTION in task["goal"]


def test_missing_crash_reason_is_treated_like_no_status_and_refuses():
    task = build_self_patch_task(
        run_id="omo_jkl000",
        terminal_state="FAILED",
        title="Some task",
        crash_reason=None,
    )
    assert task is None


def test_succeeded_terminal_state_never_triggers():
    task = build_self_patch_task(
        run_id="omo_mno111",
        terminal_state="SUCCEEDED",
        title="Some task",
        crash_reason={"status": "RETAINED", "reason": "irrelevant"},
    )
    assert task is None


def test_long_title_is_truncated_never_raises():
    huge_title = "x" * 5000
    task = build_self_patch_task(
        run_id="omo_pqr222",
        terminal_state="FAILED",
        title=huge_title,
        crash_reason={"status": "EMPTY"},
    )
    assert task is not None
    assert len(task["title"]) <= TITLE_MAX_CHARS
    assert "x" * 5000 not in task["goal"]


def test_long_crash_reason_text_does_not_blow_up_goal_either():
    # crash_reason_for already bounds this upstream, but the builder must not
    # assume that and must never raise even if handed something unbounded.
    huge_reason = "y" * 10000
    task = build_self_patch_task(
        run_id="omo_stu333",
        terminal_state="FAILED",
        title="Some task",
        crash_reason={"status": "RETAINED", "reason": huge_reason},
    )
    assert task is not None
    assert task["goal"]  # does not raise, produces something


def test_empty_title_does_not_raise():
    task = build_self_patch_task(
        run_id="omo_vwx444",
        terminal_state="FAILED",
        title="",
        crash_reason={"status": "EMPTY"},
    )
    assert task is not None
    assert "no title recorded" in task["goal"].lower()
