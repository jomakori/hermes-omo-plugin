"""The status message format is a contract, so it is asserted byte-for-byte.

Every case below pins the exact string the renderer must produce for the
confirmed template — a single running worker, the mixed multi-agent run, a
dependency-blocked worker, an interrupted worker with its cause, review rows,
and the over-limit truncation Discord would otherwise reject.
"""

from __future__ import annotations

from orchestrator.status_message import (
    HEADER_PREFIX,
    STATUS_EMOJI,
    render_block,
    render_status,
)
from orchestrator.status_rotation import ROTATE_INTERVAL_SECONDS


def _phase(name, status, **extra):
    return {"name": name, "status": status, **extra}


def running_block():
    """Block 1 of the template: a worker mid-run."""
    return {
        "agent": "hephaestus",
        "process": "OKT-161 — shell parity",
        "phases": [
            _phase("dispatch", "done"),
            _phase(
                "run",
                "current",
                run_id="omo_304bf8e5",
                task_id="t4",
                detail="wiring the nav badge semantics",
                phrase="patching components/shell.rs…",
            ),
            _phase("review", "pending"),
        ],
    }


def reviewed_block():
    """Block 2: the run finished and is under review."""
    return {
        "agent": "hephaestus",
        "process": "OKT-171 — namespace bar",
        "phases": [
            _phase("dispatch", "done"),
            _phase("run", "done", duration="22m"),
            _phase("review", "reviewing", reviewer="momus", run_id="omo_304bf8e5", task_id="t14:review"),
        ],
    }


def blocked_block():
    """Block 3: a worker blocked on a dependency."""
    return {
        "agent": "metis",
        "process": "analysis stage — openagent chart",
        "phases": [
            _phase("dispatch", "done"),
            _phase("run", "blocked", cause="waiting on prometheus"),
            _phase("review", "pending"),
        ],
    }


def interrupted_block():
    """Block 4: an interrupted worker with its cause and a verdict."""
    return {
        "agent": "oracle",
        "process": "architecture",
        "phases": [
            _phase("dispatch", "done"),
            _phase("run", "interrupted", cause="gateway restart"),
            _phase("review", "done", reviewer="momus", verdict="problems", cycle=1),
        ],
    }


def test_single_running_worker_golden():
    assert render_status([running_block()]) == (
        "🏗️ omo: hephaestus: OKT-161 — shell parity\n"
        "- dispatch ✅\n"
        "- run 🔁 omo_304bf8e5 · t4 — wiring the nav badge semantics\n"
        "  ↳ cycling: patching components/shell.rs… (3s)\n"
        "- review ⏳"
    )


def test_mixed_multi_agent_run_golden():
    assert render_status([running_block(), reviewed_block(), blocked_block(), interrupted_block()]) == (
        "🏗️ omo: hephaestus: OKT-161 — shell parity\n"
        "- dispatch ✅\n"
        "- run 🔁 omo_304bf8e5 · t4 — wiring the nav badge semantics\n"
        "  ↳ cycling: patching components/shell.rs… (3s)\n"
        "- review ⏳\n"
        "\n"
        "🏗️ omo: hephaestus: OKT-171 — namespace bar\n"
        "- dispatch ✅\n"
        "- run ✅ 22m\n"
        "- review 🔍 momus · omo_304bf8e5 · t14:review\n"
        "\n"
        "🏗️ omo: metis: analysis stage — openagent chart\n"
        "- dispatch ✅\n"
        "- run ⛔ waiting on prometheus\n"
        "- review ⏳\n"
        "\n"
        "🏗️ omo: oracle: architecture\n"
        "- dispatch ✅\n"
        "- run ⚠️ gateway restart\n"
        "- review ✅ momus · problems (cycle 1)"
    )


def test_dependency_blocked_worker_golden():
    assert render_block(blocked_block()) == (
        "🏗️ omo: metis: analysis stage — openagent chart\n- dispatch ✅\n- run ⛔ waiting on prometheus\n- review ⏳"
    )


def test_interrupted_worker_with_cause_golden():
    assert render_block(interrupted_block()) == (
        "🏗️ omo: oracle: architecture\n- dispatch ✅\n- run ⚠️ gateway restart\n- review ✅ momus · problems (cycle 1)"
    )


def test_review_verdict_rows_golden():
    # A live review names the reviewer and the refs; a settled one names the verdict.
    assert "- review 🔍 momus · omo_304bf8e5 · t14:review" in render_block(reviewed_block())
    assert "- review ✅ momus · problems (cycle 1)" in render_block(interrupted_block())


def test_header_prefix_is_exact():
    assert HEADER_PREFIX == "🏗️ omo:"
    assert render_block(running_block()).splitlines()[0] == "🏗️ omo: hephaestus: OKT-161 — shell parity"


def test_blocks_separated_by_one_blank_line():
    text = render_status([running_block(), blocked_block()])
    assert "\n\n" in text
    assert "\n\n\n" not in text
    assert text.split("\n\n")[1].startswith("🏗️ omo: metis:")


def test_cycling_line_only_under_the_running_run_row():
    text = render_status([running_block(), reviewed_block(), blocked_block()])
    cycling = [line for line in text.splitlines() if line.startswith("  ↳ cycling:")]
    assert cycling == ["  ↳ cycling: patching components/shell.rs… (3s)"]


def test_status_emoji_table_matches_the_legend():
    assert STATUS_EMOJI == {
        "pending": "⏳",
        "current": "🔁",
        "running": "🔁",
        "done": "✅",
        "reviewing": "🔍",
        "blocked": "⛔",
        "interrupted": "⚠️",
        "failed": "❌",
        "cancelled": "⏸",
        "skipped": "⏭",
    }


def test_every_legend_emoji_renders_on_a_run_row():
    caserows = {
        "current": "- run 🔁",
        "pending": "- run ⏳",
        "done": "- run ✅",
        "blocked": "- run ⛔",
        "interrupted": "- run ⚠️",
        "failed": "- run ❌",
        "cancelled": "- run ⏸",
    }
    for status, expected in caserows.items():
        text = render_block({"agent": "a", "process": "p", "phases": [_phase("run", status, cause="why")]})
        assert expected in text, status


def test_long_process_is_truncated_to_the_cap():
    header = render_block({"agent": "a", "process": "x" * 200, "phases": []}).splitlines()[0]
    assert header.endswith("…")
    assert len(header) < 120


def test_missing_agent_and_process_still_render_a_header():
    assert render_block({"agent": "", "process": "", "phases": []}) == "🏗️ omo: ?"


def test_unknown_status_renders_as_pending():
    text = render_block({"agent": "a", "process": "p", "phases": [_phase("run", "bogus")]})
    assert "- run ⏳" in text


def test_over_2000_chars_is_truncated_with_a_more_marker():
    blocks = [
        {
            "agent": f"agent-{i}",
            "process": f"task {i} " + "x" * 60,
            "phases": [_phase("dispatch", "done"), _phase("run", "done", duration="1m"), _phase("review", "pending")],
        }
        for i in range(80)
    ]
    text = render_status(blocks)
    assert len(text) <= 2000
    assert "more" in text


def test_collapse_keeps_single_long_block_under_the_limit():
    phases = [_phase(f"p{i}", "pending") for i in range(500)]
    text = render_status([{"agent": "a", "process": "p", "phases": phases}])
    assert len(text) <= 2000
    assert "more" in text


def test_rotate_interval_is_the_rendered_cadence():
    assert ROTATE_INTERVAL_SECONDS == 3
    text = render_block(
        {
            "agent": "a",
            "process": "p",
            "phases": [_phase("run", "current", run_id="r", task_id="t", detail="d", phrase="ph")],
        }
    )
    assert "  ↳ cycling: ph (3s)" in text


def test_empty_state_renders_empty():
    assert render_status([]) == ""
    assert render_status(None) == ""
