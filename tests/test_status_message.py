"""The status message format is a contract, so it is asserted byte-for-byte.

Every case below pins the exact string the renderer must produce for the v2
template: the goal stated once in the run heading, one condensed label per
worker, the dispatch/review omissions, the real tool-call activity line, the
serving model and the chain hop, and the over-limit collapse Discord would
otherwise reject. v2 changes the shape; the coverage of v1 is kept and extended,
never deleted.
"""

from __future__ import annotations

from orchestrator.status_message import (
    COMPACT_LABEL_CHARS,
    DEFAULT_LABEL_CHARS,
    DEFAULT_MAX_ACTIVITY_CHARS,
    HEADER_PREFIX,
    STATUS_EMOJI,
    condense_label,
    label_limit,
    render_activity,
    render_block,
    render_status,
    tool_emoji,
)
from orchestrator.status_rotation import ROTATE_INTERVAL_SECONDS


def _phase(name, status, **extra):
    return {"name": name, "status": status, **extra}


def _run(*blocks, run_id="omo_304bf8e5", goal="shell parity across the fleet", review=True):
    return {"run_id": run_id, "goal": goal, "review": review, "workers": list(blocks)}


def running_block():
    """Block 1 of the template: a worker mid-run."""
    return {
        "agent": "hephaestus",
        "display": "hephaestus · Deep Agent",
        "process": "OKT-161 — shell parity",
        "review": True,
        "phases": [
            _phase("dispatch", "done"),
            _phase(
                "run",
                "current",
                run_id="omo_304bf8e5",
                task_id="t4",
                detail="wiring the nav badge semantics",
                model="minimax-m3",
                phrase="patching components/shell.rs…",
            ),
            _phase("review", "pending"),
        ],
    }


def reviewed_block():
    """Block 2: the run finished and is under review."""
    return {
        "agent": "hephaestus",
        "display": "hephaestus · Deep Agent",
        "process": "OKT-171 — namespace bar",
        "review": True,
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
        "display": "metis · Plan Consultant",
        "process": "analysis stage — openagent chart",
        "review": True,
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
        "display": "oracle · Architecture / Reasoning",
        "process": "architecture",
        "review": True,
        "phases": [
            _phase("dispatch", "done"),
            _phase("run", "interrupted", cause="gateway restart"),
            _phase("review", "done", reviewer="momus", verdict="problems", cycle=1),
        ],
    }


def test_single_running_worker_golden():
    assert render_status(_run(running_block())) == (
        "🏗️ omo · omo_304bf8e5 — shell parity across the fleet\n"
        "\n"
        "OKT-161 · shell parity · hephaestus · Deep Agent\n"
        "- run 🔁 shell parity · minimax-m3\n"
        "  ↳ cycling: patching components/shell.rs… (3s)\n"
        "- review ⏳"
    )


def test_mixed_multi_agent_run_golden():
    assert render_status(_run(running_block(), reviewed_block(), blocked_block(), interrupted_block())) == (
        "🏗️ omo · omo_304bf8e5 — shell parity across the fleet · 4 workers\n"
        "\n"
        "OKT-161 · shell parity · hephaestus · Deep Agent\n"
        "- run 🔁 shell parity · minimax-m3\n"
        "  ↳ cycling: patching components/shell.rs… (3s)\n"
        "- review ⏳\n"
        "\n"
        "OKT-171 · namespace bar · hephaestus · Deep Agent\n"
        "- run ✅ 22m\n"
        "- review 🔍 momus · omo_304bf8e5 · t14:review\n"
        "\n"
        "analysis stage · metis · Plan Consultant\n"
        "- run ⛔ waiting on prometheus\n"
        "- review ⏳\n"
        "\n"
        "architecture · oracle · Architecture / Reasoning\n"
        "- run ⚠️ gateway restart\n"
        "- review ✅ momus · problems (cycle 1)"
    )


def test_dependency_blocked_worker_golden():
    assert render_block(blocked_block()) == (
        "analysis stage · metis · Plan Consultant\n- run ⛔ waiting on prometheus\n- review ⏳"
    )


def test_interrupted_worker_with_cause_golden():
    assert render_block(interrupted_block()) == (
        "architecture · oracle · Architecture / Reasoning\n"
        "- run ⚠️ gateway restart\n"
        "- review ✅ momus · problems (cycle 1)"
    )


def test_review_verdict_rows_golden():
    # A live review names the reviewer and the refs; a settled one names the verdict.
    assert "- review 🔍 momus · omo_304bf8e5 · t14:review" in render_block(reviewed_block())
    assert "- review ✅ momus · problems (cycle 1)" in render_block(interrupted_block())


def test_header_prefix_is_exact():
    assert HEADER_PREFIX == "🏗️ omo"
    # The v2 heading states the run once; a worker block is headed by its condensed
    # label plus the roster display, never by a second copy of the goal.
    assert render_block(running_block()).splitlines()[0] == "OKT-161 · shell parity · hephaestus · Deep Agent"


def test_blocks_separated_by_one_blank_line():
    text = render_status(_run(running_block(), blocked_block()))
    assert "\n\n" in text
    assert "\n\n\n" not in text
    parts = text.split("\n\n")
    assert parts[0].startswith("🏗️ omo · omo_304bf8e5 — ")
    assert parts[1].startswith("OKT-161 · shell parity")
    assert parts[2].startswith("analysis stage · metis")


def test_cycling_line_only_under_the_running_run_row():
    text = render_status(_run(running_block(), reviewed_block(), blocked_block()))
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
    # v2 never dots a value out; the header is cut on a word boundary instead.
    header = render_block({"agent": "a", "process": "x" * 200, "phases": []}).splitlines()[0]
    assert "…" not in header
    assert len(header) < 120


def test_missing_agent_and_process_still_render_a_header():
    assert render_block({"agent": "", "process": "", "phases": []}) == "?"


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
    text = render_status(_run(*blocks, goal="giant run"))
    assert len(text) <= 2000
    assert "more" in text


def test_collapse_keeps_single_long_block_under_the_limit():
    phases = [_phase(f"p{i}", "pending") for i in range(500)]
    text = render_status(_run({"agent": "a", "process": "p", "phases": phases}))
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


# ── v2: T1 condensed render ───────────────────────────────────────────────
def test_goal_stated_once_and_never_repeated_per_worker():
    text = render_status(_run(running_block(), reviewed_block()))
    assert text.count("shell parity across the fleet") == 1
    assert "- dispatch" not in text  # the run row implies a finished dispatch


def test_condensed_label_prefers_the_ticket_then_the_first_clause():
    assert condense_label("OKT-161 — shell parity across the fleet", DEFAULT_LABEL_CHARS) == (
        "OKT-161 · shell parity across the fleet"
    )
    assert condense_label("wiring the navigation badge semantics", COMPACT_LABEL_CHARS) == "wiring the navigation"
    assert condense_label("fix null deref: patch the parser", DEFAULT_LABEL_CHARS) == "fix null deref"
    # A ticket id wins even when it is not the first token.
    assert condense_label("rework module OKT-42 for parity", DEFAULT_LABEL_CHARS) == "OKT-42"


def test_label_budget_tightens_only_on_wide_fanout():
    assert label_limit(8) == DEFAULT_LABEL_CHARS
    assert label_limit(9) == COMPACT_LABEL_CHARS
    assert label_limit(24) == COMPACT_LABEL_CHARS


def test_condensed_label_is_cut_on_a_word_boundary_not_mid_word():
    label = condense_label("wiring the navigation badge semantics", COMPACT_LABEL_CHARS)
    assert label == "wiring the navigation"
    assert len(label) <= COMPACT_LABEL_CHARS
    # A single unbreakable token is sliced, still without an ellipsis.
    assert condense_label("x" * 100, 10) == "x" * 10


def test_24_worker_run_fits_the_cap_with_every_worker_named():
    # A 24-worker run must fit Discord's 2000-char cap with every worker named.
    # Naming the work (T1b) grows each header to `TICKET · title`, so at this
    # width the collapse trims trailing rows — but it never drops a worker: every
    # header stays visible, and trimmed rows are reported by the ``…N more``
    # marker rather than silently vanishing.
    workers = []
    for i in range(24):
        status = "done" if i % 3 else "current"
        workers.append(
            {
                "agent": "hephaestus",
                "display": "hephaestus · Deep Agent",
                "process": f"OKT-{100 + i} — shell parity fix number {i}",
                "review": True,
                "label_limit": COMPACT_LABEL_CHARS,
                "phases": [
                    _phase("dispatch", "done"),
                    _phase("run", status, task_id=f"t{i}", detail="wiring", phrase="reading the code…"),
                    _phase("review", "pending"),
                ],
            }
        )
    text = render_status(_run(*workers, goal="shell parity across the whole fleet"))
    assert len(text) <= 2000
    for i in range(24):
        assert f"OKT-{100 + i}" in text
    assert "more" in text


def test_no_rendered_line_ends_in_the_ellipsis_character():
    text = render_status(_run(running_block(), reviewed_block(), blocked_block(), interrupted_block()))
    for line in text.splitlines():
        assert not line.rstrip().endswith("…"), line


def test_collapse_marker_is_the_only_permitted_ellipsis():
    blocks = [
        {"agent": f"agent-{i}", "process": f"task {i} " + "x" * 80, "phases": [_phase("run", "done", duration="1m")]}
        for i in range(80)
    ]
    text = render_status(_run(*blocks, goal="giant"))
    stripped = [line.rstrip() for line in text.splitlines()]
    assert any(line.startswith("…") and line.endswith("more agents") for line in stripped)
    for line in stripped:
        assert not line.endswith("…"), line


# ── v2: T2 review row gated on the payload flag ───────────────────────────
def test_no_review_row_at_all_when_review_is_false():
    block = running_block()
    block["review"] = False
    text = render_status(_run(block, review=False))
    assert "- review" not in text


def test_review_row_renders_when_the_flag_is_true():
    text = render_status(_run(interrupted_block(), review=True))
    assert "- review ✅ momus · problems (cycle 1)" in text


# ── v2: T4 real activity line ─────────────────────────────────────────────
def test_activity_line_prefers_the_real_tool_call_over_the_canned_phrase():
    block = running_block()
    block["activity"] = "📖 read_file orchestrator/status_message.py"
    text = render_block(block)
    assert "  ↳ 📖 read_file orchestrator/status_message.py" in text
    assert "cycling:" not in text


def test_activity_line_is_never_blank_without_activity():
    text = render_block(running_block())
    assert "  ↳ cycling: patching components/shell.rs… (3s)" in text


def test_activity_is_capped_at_48_chars_on_a_word_boundary():
    long_target = "src/orchestrator/status_message.py and then some more words"
    line = render_activity("read_file", long_target, emoji_resolver=lambda name, default: "📖")
    assert len(line) <= 48
    assert not line.endswith("…")


def test_tool_emoji_comes_from_an_injected_resolver_not_a_hardcoded_map():
    def resolver(name, default):
        return "🧪" if name == "patch" else default

    assert tool_emoji("patch", resolver=resolver) == "🧪"
    assert tool_emoji("unknown-tool", resolver=resolver) == "⚡"
    assert render_activity("patch", "a/b.py", emoji_resolver=resolver) == "🧪 patch a/b.py"


# ── v2: T5 model and hop ──────────────────────────────────────────────────
def test_serving_model_is_shown_on_the_run_row():
    text = render_block(running_block())
    assert "- run 🔁 shell parity · minimax-m3" in text


def test_hop_is_named_on_the_activity_line_when_the_chain_fell_back():
    block = running_block()
    block["activity"] = "📖 read_file a.py"
    block["hop"] = "claude-sonnet-5 (rate limit)"
    text = render_block(block)
    assert "  ↳ 📖 read_file a.py · ⤵ claude-sonnet-5 (rate limit)" in text


def test_hop_shares_the_canned_activity_line_too():
    block = running_block()
    block["hop"] = "glm-5.3 (billing)"
    text = render_block(block)
    assert "  ↳ cycling: patching components/shell.rs… (3s) · ⤵ glm-5.3 (billing)" in text


# ── v3: T1b the label names the work, not the bare ticket ─────────────────────
def test_an_explicit_title_heads_the_block_and_the_run_row():
    block = {
        "agent": "sisyphus-junior",
        "display": "sisyphus-junior · Specialized Execution Worker",
        "process": "OMR-11 — make the status name the work",
        "title": "Add a provider-unusable hop reason",
        "review": False,
        "phases": [
            _phase(
                "run",
                "current",
                task_id="omr11",
                detail="editing",
                model="claude-sonnet-5",
                elapsed="3m12s",
            ),
        ],
    }
    lines = render_block(block).splitlines()
    assert lines[0] == "OMR-11 · Add a provider-unusable hop reason · sisyphus-junior · Specialized Execution Worker"
    # The run row names the work and its elapsed time, never the opaque task ref.
    assert lines[1] == "- run 🔁 Add a provider-unusable hop reason · claude-sonnet-5 (3m12s)"
    assert "omr11" not in "\n".join(lines)


def test_leading_ticket_and_paren_are_stripped_into_a_ticket_title_label():
    assert condense_label("OMR-11 (repo /x): do the thing", DEFAULT_LABEL_CHARS) == "OMR-11 · do the thing"


def test_a_ticket_leading_prompt_never_renders_the_raw_task_echo():
    block = {
        "agent": "sisyphus-junior",
        "display": "sisyphus-junior · Specialized Execution Worker",
        "process": "OMR-11 (repo /x): do the thing",
        "review": False,
        "phases": [_phase("run", "current", task_id="omr11", detail="OMR-11 (repo /x): do the thing")],
    }
    text = render_block(block)
    assert "OMR-11 · do the thing" in text
    assert "task: OMR-11 (repo" not in text
    assert "(repo /x)" not in text


def test_no_rendered_line_exceeds_its_budget():
    title = "implement the whole navigation badge semantics end to end and then some"
    block = {
        "agent": "sisyphus-junior",
        "display": "sisyphus-junior · Specialized Execution Worker",
        "process": "OMR-11 (repo /x): " + title,
        "title": title,
        "label_limit": DEFAULT_LABEL_CHARS,
        "phases": [
            _phase(
                "run",
                "current",
                task_id="omr11",
                detail="x",
                model="claude-sonnet-5",
                elapsed="3m12s",
            ),
        ],
    }
    header, run_row = render_block(block).splitlines()[:2]
    label = header.split(" · sisyphus-junior")[0]
    assert len(label) <= DEFAULT_LABEL_CHARS
    run_title = run_row[len("- run 🔁 ") :].split(" · claude-sonnet-5")[0]
    assert len(run_title) <= DEFAULT_MAX_ACTIVITY_CHARS


def test_truncation_lands_on_a_word_boundary():
    title = "implement the whole navigation badge semantics end to end and then some"
    block = {
        "agent": "sisyphus-junior",
        "display": "sisyphus-junior · Specialized Execution Worker",
        "process": "OMR-11 (repo /x): " + title,
        "title": title,
        "review": False,
        "phases": [_phase("run", "current", task_id="omr11", detail="x")],
    }
    run_row = render_block(block).splitlines()[1]
    run_title = run_row[len("- run 🔁 ") :]
    assert title.startswith(run_title)
    assert len(run_title) < len(title)
    # The cut fell between two words, never through one.
    assert title[len(run_title)] == " "
