"""The activity reader: profile state DB -> the worker's real tool call.

No host, no gateway: a throwaway sqlite file shaped like the Hermes profile store
(``sessions`` + ``messages``) is enough to pin the read, the target extraction and
the missing/locked-DB fallback that keeps the canned phrase.
"""

from __future__ import annotations

import json
import sqlite3

from orchestrator.worker_activity import (
    make_activity_provider,
    read_activity,
    session_at_launch,
)


def _profile(tmp_path, agent="hephaestus", *, sessions=(), messages=()):
    root = tmp_path / "profiles"
    db = root / agent / "state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sessions (id TEXT, started_at REAL)")
    conn.execute("CREATE TABLE messages (session_id TEXT, tool_name TEXT, tool_calls TEXT, timestamp REAL)")
    conn.executemany("INSERT INTO sessions VALUES (?, ?)", sessions)
    conn.executemany("INSERT INTO messages VALUES (?, ?, ?, ?)", messages)
    conn.commit()
    conn.close()
    return root


def test_session_at_launch_reads_the_newest_session(tmp_path):
    root = _profile(
        tmp_path,
        sessions=[("old", 1.0), ("newest", 3.0), ("middle", 2.0)],
        messages=[("newest", None, None, 5.0)],
    )
    assert session_at_launch("hephaestus", root) == "newest"


def test_session_at_launch_falls_back_to_message_timestamps(tmp_path):
    root = _profile(tmp_path, sessions=[], messages=[("s1", None, None, 1.0), ("s2", None, None, 2.0)])
    assert session_at_launch("hephaestus", root) == "s2"


def test_read_activity_parses_the_newest_tool_call_and_its_target(tmp_path):
    args = json.dumps({"path": "orchestrator/status_message.py"})
    calls = json.dumps([{"id": "1", "function": {"name": "read_file", "arguments": args}}])
    root = _profile(tmp_path, messages=[("s1", None, calls, 10.0)])
    assert read_activity("hephaestus", "s1", root) == ("read_file", "orchestrator/status_message.py")


def test_read_activity_uses_the_newest_call_not_the_first(tmp_path):
    older = json.dumps([{"function": {"name": "search_files", "arguments": {"pattern": "old"}}}])
    newer = json.dumps([{"function": {"name": "patch", "arguments": {"path": "a.py"}}}])
    root = _profile(tmp_path, messages=[("s1", None, older, 1.0), ("s1", None, newer, 2.0)])
    assert read_activity("hephaestus", "s1", root) == ("patch", "a.py")


def test_read_activity_falls_back_to_a_tool_result_row(tmp_path):
    root = _profile(tmp_path, messages=[("s1", "kanban_complete", None, 3.0)])
    assert read_activity("hephaestus", "s1", root) == ("kanban_complete", "")


def test_read_activity_returns_none_for_an_unknown_session(tmp_path):
    root = _profile(tmp_path, messages=[("s1", None, json.dumps([{"function": {"name": "x"}}]), 1.0)])
    assert read_activity("hephaestus", "other", root) is None


def test_missing_profile_db_is_none(tmp_path):
    assert session_at_launch("ghost", tmp_path / "profiles") is None
    assert read_activity("ghost", "s1", tmp_path / "profiles") is None


def test_make_provider_reads_the_db_and_formats_the_line(tmp_path):
    calls = json.dumps([{"function": {"name": "patch", "arguments": {"path": "a/b.py"}}}])
    root = _profile(tmp_path, messages=[("s1", None, calls, 1.0)])

    def resolver(name, default):
        return {"patch": "\U0001f527"}.get(name, default)

    provider = make_activity_provider(profiles_dir=root, emoji_resolver=resolver)
    assert provider("s1", "hephaestus") == "\U0001f527 patch a/b.py"
    # Nothing observable yet -> None, so the tracker keeps the canned phrase.
    assert provider("missing-session", "hephaestus") is None
