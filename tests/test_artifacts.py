"""A brief's declared deliverables gate a worker's SUCCEEDED.

The host returns a worker's result as text plus a duration — no tool names, no
repository state — so a worker that explores for its whole budget and closes
cleanly reported SUCCEEDED with nothing on disk. The brief's `ARTIFACT:` lines
are the only place a deliverable can be stated, so they are what gets checked.
"""

from orchestrator.engine import ARTIFACT_MISSING_ERROR, artifact_failure, declared_artifacts


def test_declared_artifacts_reads_every_marker_in_order():
    brief = "Do the thing.\nARTIFACT: /tmp/a.md\nsome prose\nARTIFACT: /tmp/b.md\n"
    assert declared_artifacts(brief) == ["/tmp/a.md", "/tmp/b.md"]


def test_declared_artifacts_is_case_and_indent_tolerant():
    assert declared_artifacts("  artifact:   /tmp/c.md  \n") == ["/tmp/c.md"]


def test_declared_artifacts_ignores_a_brief_with_no_marker():
    assert declared_artifacts("no marker here") == []
    assert declared_artifacts("") == []


def test_failure_names_only_the_absent_deliverables_in_declared_order():
    brief = "ARTIFACT: /present.md\nARTIFACT: /absent.md"
    present = {"/present.md"}
    assert artifact_failure(brief, exists=lambda path: path in present) == (
        ARTIFACT_MISSING_ERROR.format(paths="/absent.md")
    )


def test_failure_is_silent_when_the_declared_file_exists(tmp_path):
    produced = tmp_path / "report.md"
    produced.write_text("done")
    assert artifact_failure(f"ARTIFACT: {produced}") == ""


def test_failure_is_silent_when_the_brief_declares_nothing():
    assert artifact_failure("read-only recon, nothing to deliver") == ""
    assert artifact_failure("", exists=lambda path: False) == ""


def test_failure_respects_the_config_gate():
    assert artifact_failure("ARTIFACT: /absent.md", enabled=False, exists=lambda path: False) == ""
