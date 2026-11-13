"""Host-dock bridge: registration, transcript tee, settle, and a host that is not there.

``orchestrator.host_bridge`` and the three engine seams that call it are an
observability bridge into *another* process's registry, so every test here pins
one of two promises: with the host present the worker gets exactly one dock row
fed from the same transcript lines the private feed sees, and with the host
absent (or a handle that names no subagent) the run is untouched. A fake host is
installed through ``sys.modules``; the real Hermes tree is never imported.
"""

import asyncio
import dataclasses
import json
import pathlib
import sys
import threading
import types

import pytest

from orchestrator import host_bridge, live_transcript
from orchestrator.chains import ChainResolver
from orchestrator.engine import OmoEngine
from orchestrator.models import FAILED, Run, Worker

_HOST_MODULES = (
    "tools",
    "tools.delegation_live_log",
    "tools.delegate_tool_registry",
    "agent",
    "agent.subagent_lifecycle",
    "hermes_constants",
)


class FakeWriter:
    """The slice of ``LiveTranscriptWriter`` the bridge is allowed to touch."""

    def __init__(self, path):
        self.path = path
        self.calls = []

    def event(self, role, text):
        self.calls.append(("event", role, text))

    def marker(self, text):
        self.calls.append(("marker", text))


class FakeHost:
    """A stand-in Hermes: one writer factory and one live-subagent registry."""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.path = tmp_path / "host-live" / "deleg_1" / "0.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("", encoding="utf-8")
        self.writers = []
        self.created = []
        self.records = {}
        self.registered = []
        self.unregistered = []
        self.parent = _FakeParentAgent()
        self.lock = threading.Lock()

    # ── the host surface host_bridge imports ───────────────────────────
    def create_live_transcripts(
        self, task_list, context=None, delegation_id=None, model=None, provider=None, home=None
    ):
        self.created.append({"tasks": [dict(task) for task in task_list], "model": model, "home": home})
        writers = [FakeWriter(self.path) for _ in task_list]
        self.writers.extend(writers)
        return "deleg_1", writers, [str(self.path) for _ in task_list]

    def register(self, record):
        self.records[record["subagent_id"]] = record
        entry = {key: value for key, value in record.items() if key != "agent"}
        entry["live_transcript"] = getattr(record.get("agent"), "_live_transcript_path", None)
        entry["agent"] = record.get("agent")
        self.registered.append(entry)

    def unregister(self, subagent_id, *, agent=None):
        record = self.records.get(subagent_id)
        if record is None or (agent is not None and record.get("agent") is not agent):
            return
        del self.records[subagent_id]
        self.unregistered.append((subagent_id, agent))

    def get_parent(self):
        return self.parent

    def modules(self):
        return {
            "tools": types.ModuleType("tools"),
            "tools.delegation_live_log": _module(
                "tools.delegation_live_log", create_live_transcripts=self.create_live_transcripts
            ),
            "tools.delegate_tool_registry": _module(
                "tools.delegate_tool_registry",
                _register_subagent=self.register,
                _unregister_subagent=self.unregister,
                _active_subagents=self.records,
                _active_subagents_lock=self.lock,
            ),
            "agent": types.ModuleType("agent"),
            "agent.subagent_lifecycle": _module("agent.subagent_lifecycle", get_active_subagent_parent=self.get_parent),
            "hermes_constants": _module("hermes_constants", get_hermes_home=lambda: self.tmp_path),
        }


class _FakeParentAgent:
    """Weakly referenceable, like the real parent, so the shim keeps its ref shape."""

    session_id = "sess_parent"


def _register(host, **overrides):
    """``register_row`` with the boring fields filled in, so each test names only what it cares about."""
    fields = {
        "subagent_id": "sa_1",
        "parent_session_id": None,
        "goal": "g",
        "model": None,
        "owner_session_id": None,
        # A real path string either way: the blocked-host case still names one.
        "live_transcript_path": str(host.path) if host is not None else "/tmp/live/0.jsonl",
    }
    fields.update(overrides)
    return host_bridge.register_row(**fields)


def _module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


@pytest.fixture()
def host(monkeypatch, tmp_path):
    """The fake host installed in ``sys.modules`` for the duration of one test."""
    fake = FakeHost(tmp_path)
    for name, module in fake.modules().items():
        monkeypatch.setitem(sys.modules, name, module)
    return fake


@pytest.fixture()
def omo_live_dir(tmp_path, monkeypatch):
    """A throwaway private feed dir, so no test writes under ``~/.omo``."""
    live_dir = tmp_path / "omo-live"
    monkeypatch.setattr(live_transcript, "DEFAULT_LIVE_DIR", str(live_dir))
    return live_dir


# ── host_bridge against a present host ────────────────────────────────


def test_open_writer_asks_the_host_for_one_writer(host):
    writer = host_bridge.open_writer(goal="scan the repo", model="deepseek-v4-flash-direct")

    assert writer is host.writers[0]
    assert host.created == [
        {"tasks": [{"goal": "scan the repo"}], "model": "deepseek-v4-flash-direct", "home": host.tmp_path}
    ]


def test_register_row_carries_the_handle_identity_and_a_tailable_path(host):
    shim = host_bridge.register_row(
        subagent_id="sa_1",
        parent_session_id="sess_parent",
        goal="scan the repo",
        model="deepseek-v4-flash-direct",
        owner_session_id="sess_owner",
        live_transcript_path=str(host.path),
    )

    record = host.records["sa_1"]
    assert shim is record["agent"]
    assert record["goal"] == "scan the repo"
    assert record["model"] == "deepseek-v4-flash-direct"
    assert record["parent_id"] == "sess_parent"
    assert record["owner_agent_session_id"] == "sess_owner"
    assert record["status"] == "running"
    # The shim has no .steer(), so the row must never claim it accepts one.
    assert record["accepting_steer"] is False
    assert shim._live_transcript_path == str(host.path)
    assert shim._delegate_parent_ref() is host.parent


def test_register_row_without_a_subagent_or_a_path_registers_nothing(host):
    assert _register(host, subagent_id="") is None
    assert _register(host, live_transcript_path=None) is None
    assert host.records == {}


def test_emit_maps_kinds_onto_writer_methods(host):
    writer = FakeWriter(host.path)

    host_bridge.emit(writer, "tool", "read_file x.py")
    host_bridge.emit(writer, "result", "done")
    host_bridge.emit(writer, "error", "boom")
    host_bridge.emit(writer, "status", "t2 -> RUNNING")

    host_bridge.emit(writer, "launch", "explore · model")

    assert writer.calls == [
        ("event", "tool", "read_file x.py"),
        ("event", "result", "done"),
        ("event", "error", "boom"),
        ("event", "status", "t2 -> RUNNING"),
        ("event", "start", "explore · model"),
    ]


def test_emit_without_a_writer_or_text_is_inert(host):
    writer = FakeWriter(host.path)

    host_bridge.emit(None, "tool", "read_file x.py")
    host_bridge.emit(writer, "result", "")

    assert writer.calls == []


def test_update_fields_merges_onto_the_live_row_and_status_is_a_special_case(host):
    _register(host)

    host_bridge.update_fields("sa_1", tool_count=3, last_tool="read_file x.py")
    host_bridge.update_status("sa_1", "succeeded")

    assert host.records["sa_1"]["tool_count"] == 3
    assert host.records["sa_1"]["last_tool"] == "read_file x.py"
    assert host.records["sa_1"]["status"] == "succeeded"


def test_update_fields_on_an_unknown_row_is_a_no_op(host):
    host_bridge.update_fields("sa_missing", status="succeeded")
    host_bridge.update_fields(None, status="succeeded")

    assert host.records == {}


def test_unregister_row_drops_the_row_with_the_same_shim(host):
    shim = _register(host)

    host_bridge.unregister_row("sa_1", shim)
    host_bridge.unregister_row("sa_1", shim)  # already gone: still a no-op

    assert host.unregistered == [("sa_1", shim)]
    assert host.records == {}


# ── host_bridge against an absent host ────────────────────────────────


def test_every_entry_point_degrades_when_the_host_import_is_blocked(monkeypatch):
    for name in _HOST_MODULES:
        monkeypatch.setitem(sys.modules, name, None)

    assert host_bridge.open_writer(goal="g", model="m") is None
    assert _register(None) is None
    host_bridge.update_fields("sa_1", status="succeeded")
    host_bridge.update_status("sa_1", "succeeded")
    host_bridge.unregister_row("sa_1", object())
    host_bridge.emit(_ExplodingWriter(), "tool", "read_file x.py")


class _ExplodingWriter:
    path = "/tmp/exploding.jsonl"

    def event(self, role, text):
        raise RuntimeError("writer is gone")

    def marker(self, text):
        raise RuntimeError("writer is gone")


# ── engine seams ──────────────────────────────────────────────────────


@dataclasses.dataclass
class FakeRequest:
    goal: str
    context: str | None
    role: str
    model: str | None
    allowed_toolsets: tuple
    metadata: dict


def request_factory(*, goal, context, spec, model, toolsets):
    return FakeRequest(
        goal=goal,
        context=context,
        role="orchestrator" if spec.orchestrator else "leaf",
        model=model,
        allowed_toolsets=toolsets,
        metadata={"omo_agent": spec.name},
    )


@dataclasses.dataclass
class FakeHandle:
    model: str
    subagent_id: str | None
    parent_session_id: str | None = None


class FakeLifecycle:
    """Launch handles that name a dock identity, unless a test says otherwise."""

    def __init__(self, fail_times=0, error="429 rate limit exceeded", subagent_id: str | None = "sa_default"):
        self.fail_times = fail_times
        self.error = error
        self.subagent_id = subagent_id
        self.launches = []
        self.handles = []

    def launch(self, request):
        self.launches.append(request.model)
        if len(self.launches) <= self.fail_times:
            raise RuntimeError(self.error)
        handle = FakeHandle(request.model, self.subagent_id, "sess_parent")
        self.handles.append(handle)
        return handle

    def wait(self, handle, *, timeout_seconds=None):
        return None

    def result(self, handle):
        return {"summary": "done", "model": handle.model}

    def status_of(self, handle):
        return True

    def cancel(self, handle, *, reason=""):
        return None


class FakeCtx:
    def __init__(self, lifecycle, config=None):
        self.subagent_lifecycle = lifecycle
        self._config = config or {}
        self.spawned = []
        self.events = []

    def get_config(self, key, default=None):
        return self._config.get(key, default)

    def emit(self, event, payload=None):
        self.events.append((event, payload))

    def spawn_task(self, coro):
        self.spawned.append(coro)


def make_engine(lifecycle, config=None):
    ctx = FakeCtx(lifecycle, config)
    engine = OmoEngine(ctx, lifecycle=lifecycle, request_factory=request_factory)
    engine.chains = ChainResolver(config or {})
    return ctx, engine


def _run_lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_a_sync_dispatch_registers_one_row_and_tees_the_same_lines(host, omo_live_dir):
    lifecycle = FakeLifecycle()
    _, engine = make_engine(lifecycle)

    out = engine.dispatch(goal="scan the repo", target="explore")

    assert out["status"] == "succeeded"
    assert [entry["subagent_id"] for entry in host.registered] == ["sa_default"]
    entry = host.registered[0]
    assert entry["goal"] == "scan the repo"
    assert entry["model"] == "deepseek-v4-flash-direct"
    assert entry["owner_agent_session_id"] == engine.runs[out["run_id"]].session_id
    assert entry["live_transcript"] == str(host.path)
    # Same seam: the private feed still has its own launch and result lines.
    run = engine.runs[out["run_id"]]
    private = _run_lines(pathlib.Path(run.transcript_path))
    worker_lines = [line for line in private if line["agent"] == "explore" and line["kind"] != "status"]
    assert [line["kind"] for line in worker_lines] == ["launch", "result"]
    # And the host writer saw that same pair, through its own methods.
    launch = worker_lines[0]
    assert ("event", "start", launch["text"]) in host.writers[0].calls
    result_line = worker_lines[-1]
    assert ("event", "result", result_line["text"]) in host.writers[0].calls


def test_a_settled_sync_run_leaves_no_row_behind(host, omo_live_dir):
    lifecycle = FakeLifecycle()
    _, engine = make_engine(lifecycle)

    engine.dispatch(goal="scan the repo", target="explore")

    assert host.records == {}
    assert [subagent_id for subagent_id, _ in host.unregistered] == ["sa_default"]


def test_a_background_run_holds_its_row_while_running_and_drops_it_at_settle(host, omo_live_dir):
    lifecycle = FakeLifecycle()
    ctx, engine = make_engine(lifecycle)
    seen = {}

    def result(handle):
        seen["rows"] = dict(host.records)
        return {"summary": "done", "model": handle.model}

    lifecycle.result = result

    out = engine.dispatch(goal="long job", target="hephaestus", background=True)
    asyncio.run(ctx.spawned[0])

    assert list(seen["rows"]) == ["sa_default"]
    assert host.records == {}
    assert engine.status(out["run_id"])["workers"][0]["status"] == "SUCCEEDED"


def test_a_hop_fallback_does_not_register_a_second_row(host, omo_live_dir):
    lifecycle = FakeLifecycle(fail_times=1)
    _, engine = make_engine(lifecycle)

    engine.dispatch(goal="scan the repo", target="explore")

    assert len(lifecycle.launches) == 2
    assert len(host.registered) == 1
    assert len(host.writers) == 1


def test_a_handle_without_a_subagent_id_degrades_silently(host, omo_live_dir):
    lifecycle = FakeLifecycle(subagent_id=None)
    _, engine = make_engine(lifecycle)

    out = engine.dispatch(goal="scan the repo", target="explore")

    assert out["status"] == "succeeded"
    assert host.registered == []
    assert host.writers == []
    worker = engine.runs[out["run_id"]].workers[0]
    assert worker.host_bridge_attempted is True
    assert worker.host_subagent_id == ""
    assert worker.host_writer is None


def test_a_tool_line_reaches_the_host_writer_and_bumps_the_row_count(host):
    engine = OmoEngine(FakeCtx(FakeLifecycle()))
    writer = FakeWriter(host.path)
    worker = Worker(run_id="omo_tools", agent_name="explore", task="scan", chain=("m",))
    worker.host_writer = writer
    worker.host_subagent_id = "sa_tools"
    run = Run(run_id="omo_tools", goal="scan")
    run.workers.append(worker)
    engine.runs["omo_tools"] = run
    engine._transcripts["omo_tools"] = None
    host.register({"subagent_id": "sa_tools", "status": "running", "agent": object()})

    engine._write_transcript("omo_tools", agent="explore", kind="tool", text="read_file x.py")
    engine._write_transcript("omo_tools", agent="explore", kind="tool", text="grep -rn foo")

    assert writer.calls == [
        ("event", "tool", "read_file x.py"),
        ("event", "tool", "grep -rn foo"),
    ]
    assert host.records["sa_tools"]["tool_count"] == 2
    assert host.records["sa_tools"]["last_tool"] == "grep -rn foo"


def test_a_walk_that_ends_without_an_outcome_still_retires_the_row(host):
    engine = OmoEngine(FakeCtx(FakeLifecycle()))
    writer = FakeWriter(host.path)
    worker = Worker(run_id="omo_lost", agent_name="explore", task="scan", chain=("m",))
    shim = object()
    worker.host_writer = writer
    worker.host_subagent_id = "sa_lost"
    worker.host_shim = shim
    run = Run(run_id="omo_lost", goal="scan")
    run.workers.append(worker)
    engine.runs["omo_lost"] = run
    host.register({"subagent_id": "sa_lost", "status": "running", "agent": shim})

    engine._settle_unfinished(run, "the walk ended before this worker settled")
    engine._wake_when_settled(run)

    assert worker.status == FAILED
    assert host.records == {}
    assert [subagent_id for subagent_id, _ in host.unregistered] == ["sa_lost"]


def test_host_bridge_state_never_reaches_the_persisted_record(host):
    worker = Worker(run_id="omo_persist", agent_name="explore", task="scan", chain=("m",))
    worker.host_writer = FakeWriter(host.path)
    worker.host_shim = object()
    worker.host_subagent_id = "sa_persist"
    worker.host_tool_count = 2
    worker.host_bridge_attempted = True

    payload = worker.to_dict()

    assert not [key for key in payload if key.startswith("host_")]
    restored = Worker.from_dict(payload)
    assert restored is not None
    assert restored.host_subagent_id == ""


def test_a_still_running_worker_keeps_its_row(host):
    engine = OmoEngine(FakeCtx(FakeLifecycle()))
    worker = Worker(run_id="omo_live", agent_name="explore", task="scan", chain=("m",))
    worker.host_writer = FakeWriter(host.path)
    worker.host_subagent_id = "sa_live"
    run = Run(run_id="omo_live", goal="scan")
    run.workers.append(worker)
    engine.runs["omo_live"] = run
    host.register({"subagent_id": "sa_live", "status": "running", "agent": object()})

    engine._wake_when_settled(run)
