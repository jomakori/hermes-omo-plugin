"""Live transcript writer: seq ordering, JSONL shape, redaction, tolerance, no-op.

``orchestrator.live_transcript`` is an observability side channel, so every
test here pins one of its two promises: the file is a clean, ordered JSONL
stream of redacted lines — and any failure to write it is invisible to the
run it describes. ``tmp_path`` is the only live dir used; ``~/.omo`` is never
touched.
"""

import itertools
import json

import pytest

from orchestrator import live_transcript
from orchestrator.engine import OmoEngine, _live_payload_fields
from orchestrator.live_transcript import LiveTranscript, for_run
from orchestrator.models import Run


@pytest.fixture()
def live_dir(tmp_path, monkeypatch):
    """A throwaway live dir, patched in as the module default as well."""
    path = tmp_path / "live"
    monkeypatch.setattr(live_transcript, "DEFAULT_LIVE_DIR", str(path))
    return path


def _lines(path):
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_seq_starts_at_one_and_increases_monotonically(live_dir):
    transcript = LiveTranscript("omo_seq", dir=str(live_dir))

    assert transcript.path.is_file()
    transcript.write({"agent": "hephaestus", "kind": "status", "text": "first"})
    transcript.write({"agent": "hephaestus", "kind": "status", "text": "second"})

    seqs = [json.loads(line)["seq"] for line in _lines(transcript.path)]

    assert seqs[0] == 1
    assert seqs == [1, 2, 3]
    assert all(later > earlier for earlier, later in itertools.pairwise(seqs))


def test_every_line_is_one_json_object(live_dir):
    transcript = LiveTranscript("omo_json", dir=str(live_dir), title="Add the widget")

    transcript.write({"agent": "atlas", "task_id": "t2", "kind": "tool", "text": "read_file x.py"})
    transcript.write({"agent": "", "task_id": None, "kind": "not-a-kind", "text": "multi\nline   text"})

    lines = _lines(transcript.path)

    assert len(lines) == 3
    for line in lines:
        event = json.loads(line)
        assert isinstance(event, dict)
        assert set(event) == {"seq", "ts", "agent", "task_id", "kind", "text"}

    # The launch header carries the title, and an unknown kind degrades to `status`.
    assert json.loads(lines[0])["kind"] == "launch"
    assert json.loads(lines[0])["text"] == "Add the widget"
    assert json.loads(lines[-1])["kind"] == "status"
    assert json.loads(lines[-1])["text"] == "multi line text"


def test_secrets_are_redacted_before_they_reach_disk(live_dir):
    api_key = "sk-proj-9f8a7b6c5d4e3f2a1b0c"
    token = "ghp_A1b2C3d4E5f6G7h8I9j0"
    transcript = LiveTranscript("omo_redact", dir=str(live_dir))

    transcript.write({"kind": "error", "text": f"call failed with api_key={api_key} on retry"})
    transcript.write({"kind": "error", "text": f"header was Authorization: Bearer {token}"})

    raw = transcript.path.read_text(encoding="utf-8")

    assert api_key not in raw
    assert token not in raw
    assert "<redacted>" in raw
    assert raw.count("<redacted>") == 2


def test_half_written_last_line_is_skipped_not_raised(live_dir):
    transcript = LiveTranscript("omo_trunc", dir=str(live_dir))
    transcript.write({"kind": "status", "text": "complete line"})

    with open(transcript.path, "a", encoding="utf-8") as handle:
        handle.write('{"seq": 99, "kind": "statu')

    events = transcript.read_since(0)

    assert [event["seq"] for event in events] == [1, 2]
    assert all(event["text"] != '{"seq": 99, "kind": "statu' for event in events)
    assert transcript.read_since(1) == [events[-1]]
    assert len(transcript.read_since(0, limit=1)) == 1


def test_unusable_live_dir_never_raises_and_degrades_to_none(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    broken = str(blocker / "live")

    transcript = LiveTranscript("omo_broken", dir=broken)

    transcript.write({"kind": "status", "text": "this must not raise"})
    assert transcript.read_since(0) == []
    assert not (blocker / "live").exists()

    assert for_run("omo_broken", dir=broken) is None
    assert blocker.is_file()


def test_unconfigured_transcript_is_a_no_op_in_the_engine(tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    broken_dir = tmp_path / "unusable" / "live"
    broken_dir.parent.mkdir()
    broken_dir.write_text("a file where the live dir should be", encoding="utf-8")
    monkeypatch.setattr(live_transcript, "DEFAULT_LIVE_DIR", str(broken_dir))

    class FakeCtx:
        def get_config(self, key, default=None):
            return default

        def emit(self, event, payload=None):
            return None

    engine = OmoEngine(FakeCtx())
    run = Run(run_id="omo_noop", goal="nothing to record")

    engine._open_transcript(run, title="no transcript")
    engine._write_transcript("omo_noop", kind="status", text="dropped on the floor")

    assert engine._transcripts["omo_noop"] is None
    assert run.transcript_path is None
    assert _live_payload_fields(run) == {"live_transcripts": {}}
    assert list(tmp_path.rglob("*.jsonl")) == []
