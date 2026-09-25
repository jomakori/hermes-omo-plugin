"""A run belongs to the session that paid for it.

The run registry is process-wide, so an unattributed registry hands one session
another session's work — and lets it cancel that work. These tests pin the
attribution, the default scoping of `status`/`tree`/`cancel`, the opt-in that
reaches across sessions, and the record written before attribution existed.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from omo_tools.omo_task_tool import make_omo_task_handler
from omo_tools.omo_tool import OMO_SCHEMA, make_omo_handler
from orchestrator import session
from orchestrator.chains import ChainResolver
from orchestrator.engine import OmoEngine
from orchestrator.models import CANCELLED, RUNNING, Run
from orchestrator.store import RunStore


# ── the host bridge ──────────────────────────────────────────────────────
def bind_session(monkeypatch, session_id):
    """Install a stand-in host bridge, the way the gateway binds a turn.

    Exercises the real lookup in `orchestrator.session` (a `gateway.session_context`
    import that succeeds), rather than mocking the plugin's own accessor out.
    """
    module = types.ModuleType("gateway.session_context")
    values = {"HERMES_SESSION_ID": session_id}
    module.get_session_env = lambda name, default="": values.get(name, default)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gateway", types.ModuleType("gateway"))
    monkeypatch.setitem(sys.modules, "gateway.session_context", module)


def unbind_session(monkeypatch):
    """A host with no bridge at all: the import raises, as it would on an old host."""
    monkeypatch.setitem(sys.modules, "gateway.session_context", None)
    monkeypatch.delenv(session.SESSION_ID_ENV, raising=False)


def test_no_bridge_resolves_no_session_instead_of_raising(monkeypatch):
    unbind_session(monkeypatch)
    assert session.current_session_id() is None


def test_a_bridge_that_raises_is_not_fatal(monkeypatch):
    module = types.ModuleType("gateway.session_context")

    def boom(name, default=""):
        raise RuntimeError("bridge down")

    module.get_session_env = boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gateway", types.ModuleType("gateway"))
    monkeypatch.setitem(sys.modules, "gateway.session_context", module)

    assert session.current_session_id() is None


# ── engine fakes ─────────────────────────────────────────────────────────
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


class FakeLifecycle:
    def __init__(self):
        self.launches = []
        self.cancelled = []

    def launch(self, request):
        self.launches.append(request.model)
        return None

    def wait(self, handle, *, timeout_seconds=None):
        return None

    def result(self, handle):
        return {"summary": "done"}

    def cancel(self, handle, *, reason=""):
        self.cancelled.append(handle)
        return None


class FakeCtx:
    def __init__(self, lifecycle, config=None):
        self.subagent_lifecycle = lifecycle
        self._config = config or {}
        self.spawned = []

    def get_config(self, key, default=None):
        return self._config.get(key, default)

    def emit(self, event, payload=None):
        return None

    def spawn_task(self, coro):
        self.spawned.append(coro)


def make_engine(path):
    lifecycle = FakeLifecycle()
    ctx = FakeCtx(lifecycle)
    engine = OmoEngine(ctx, lifecycle=lifecycle, request_factory=request_factory, store=RunStore(path=path))
    engine.chains = ChainResolver({})
    return engine


def close_spawned(engine):
    for coro in engine._ctx.spawned:
        coro.close()


def record(path):
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


# ── attribution ──────────────────────────────────────────────────────────
def test_dispatch_records_the_calling_session(tmp_path, monkeypatch):
    bind_session(monkeypatch, "ses_alpha")
    engine = make_engine(tmp_path / "runs.json")

    out = engine.dispatch(goal="scan the repo", target="explore")

    assert engine.runs[out["run_id"]].session_id == "ses_alpha"
    # …and it is on the record, not just in memory.
    assert record(tmp_path / "runs.json")["runs"][0]["session_id"] == "ses_alpha"


def test_a_single_task_dispatch_is_attributed_the_same_way(tmp_path, monkeypatch):
    bind_session(monkeypatch, "ses_alpha")
    engine = make_engine(tmp_path / "runs.json")
    handler = make_omo_task_handler(engine)

    payload = json.loads(asyncio.run(handler({"prompt": "scan the repo", "agent": "explore"})))

    assert engine.runs[payload["run_id"]].session_id == "ses_alpha"


def test_a_graph_dispatch_is_attributed_too(tmp_path, monkeypatch):
    bind_session(monkeypatch, "ses_alpha")
    engine = make_engine(tmp_path / "runs.json")

    out = engine.dispatch_graph(tasks=[{"id": "a", "agent": "explore", "prompt": "recon"}])

    assert engine.runs[out["run_id"]].session_id == "ses_alpha"


def test_dispatch_without_a_bridge_records_no_session_and_still_runs(tmp_path, monkeypatch):
    unbind_session(monkeypatch)
    engine = make_engine(tmp_path / "runs.json")

    out = engine.dispatch(goal="scan the repo", target="explore")

    assert out["status"] == "succeeded"
    assert engine.runs[out["run_id"]].session_id is None


def test_the_session_id_survives_the_restart_path(tmp_path, monkeypatch):
    path = tmp_path / "runs.json"
    bind_session(monkeypatch, "ses_alpha")
    out = make_engine(path).dispatch(goal="scan", target="explore")

    restarted = make_engine(path)
    assert restarted.runs[out["run_id"]].session_id == "ses_alpha"
    assert [run["run_id"] for run in restarted.status()["runs"]] == [out["run_id"]]

    # A different session that adopts the same record does not get to claim it.
    bind_session(monkeypatch, "ses_beta")
    assert restarted.status()["runs"] == []
    assert restarted.status()["hidden_runs"] == 1


# ── status / tree scoping ────────────────────────────────────────────────
def test_status_defaults_to_the_callers_runs(tmp_path, monkeypatch):
    engine = make_engine(tmp_path / "runs.json")
    bind_session(monkeypatch, "ses_alpha")
    mine = engine.dispatch(goal="mine", target="explore")
    bind_session(monkeypatch, "ses_beta")
    theirs = engine.dispatch(goal="theirs", target="explore")

    as_beta = engine.status()
    assert [run["run_id"] for run in as_beta["runs"]] == [theirs["run_id"]]
    assert as_beta["session_id"] == "ses_beta"
    assert as_beta["scope"] == "session"
    assert as_beta["hidden_runs"] == 1

    bind_session(monkeypatch, "ses_alpha")
    as_alpha = engine.status()
    assert [run["run_id"] for run in as_alpha["runs"]] == [mine["run_id"]]


def test_reading_a_foreign_run_is_refused_and_names_its_session(tmp_path, monkeypatch):
    engine = make_engine(tmp_path / "runs.json")
    bind_session(monkeypatch, "ses_beta")
    theirs = engine.dispatch(goal="theirs", target="explore")

    bind_session(monkeypatch, "ses_alpha")
    payload = engine.status(theirs["run_id"])

    assert "ses_beta" in payload["error"]
    assert payload["owner_session_id"] == "ses_beta"
    assert "tree" not in payload


def test_the_opt_in_reaches_every_session(tmp_path, monkeypatch):
    engine = make_engine(tmp_path / "runs.json")
    bind_session(monkeypatch, "ses_alpha")
    mine = engine.dispatch(goal="mine", target="explore")
    bind_session(monkeypatch, "ses_beta")
    theirs = engine.dispatch(goal="theirs", target="explore")

    bind_session(monkeypatch, "ses_alpha")
    every = engine.status(all_sessions=True)

    assert {run["run_id"] for run in every["runs"]} == {mine["run_id"], theirs["run_id"]}
    assert every["scope"] == "all_sessions"
    assert "hidden_runs" not in every
    # A single foreign run opens under the opt-in too.
    assert engine.status(theirs["run_id"], all_sessions=True)["run_id"] == theirs["run_id"]


# ── a record written before attribution existed ──────────────────────────
def test_a_legacy_record_loads_and_is_hidden_until_opted_in(tmp_path, monkeypatch):
    path = tmp_path / "runs.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "runs": [{"run_id": "omo_legacy", "goal": "old work", "created_at": 1.0, "workers": []}],
            }
        ),
        encoding="utf-8",
    )
    bind_session(monkeypatch, "ses_alpha")
    engine = make_engine(path)
    fresh = engine.dispatch(goal="mine", target="explore")

    assert engine.runs["omo_legacy"].session_id is None

    listed = [run["run_id"] for run in engine.status()["runs"]]
    assert listed == [fresh["run_id"]]
    # Not silently gone: the count says another record exists, the opt-in reaches it.
    assert engine.status()["hidden_runs"] == 1
    assert {run["run_id"] for run in engine.status(all_sessions=True)["runs"]} == {
        "omo_legacy",
        fresh["run_id"],
    }
    assert "unattributed" in engine.status("omo_legacy")["error"]


def test_an_unattributed_run_round_trips_as_unattributed():
    restored = Run.from_dict({"run_id": "omo_old", "goal": "old", "workers": []})

    assert restored is not None
    assert restored.session_id is None
    assert Run(run_id="omo_new", goal="g", session_id="ses_alpha").to_dict()["session_id"] == "ses_alpha"
    # A blank/absent field on the record reads as no session, never as "".
    blank = Run.from_dict({"run_id": "omo_blank", "goal": "g", "session_id": "   "})
    assert blank is not None and blank.session_id is None
    null = Run.from_dict({"run_id": "omo_null", "goal": "g", "session_id": None})
    assert null is not None and null.session_id is None


# ── cancel ───────────────────────────────────────────────────────────────
def test_cancel_of_a_foreign_run_is_refused_until_opted_in(tmp_path, monkeypatch):
    engine = make_engine(tmp_path / "runs.json")
    bind_session(monkeypatch, "ses_beta")
    theirs = engine.dispatch(goal="theirs", target="explore", background=True)
    worker = engine.runs[theirs["run_id"]].workers[0]
    assert worker.status == RUNNING

    bind_session(monkeypatch, "ses_alpha")
    refused = engine.cancel(theirs["run_id"])

    assert "ses_beta" in refused["error"]
    assert "ses_alpha" in refused["error"]
    assert "all_sessions=true" in refused["error"]
    # Nothing was cancelled, and nothing was marked for cancellation.
    assert worker.status == RUNNING
    assert worker.cancel_requested is False

    allowed = engine.cancel(theirs["run_id"], all_sessions=True)
    assert allowed["cancelled"] == 1
    assert worker.status == CANCELLED
    close_spawned(engine)


def test_cancel_of_the_callers_own_run_still_works(tmp_path, monkeypatch):
    engine = make_engine(tmp_path / "runs.json")
    bind_session(monkeypatch, "ses_alpha")
    mine = engine.dispatch(goal="mine", target="explore", background=True)

    out = engine.cancel(mine["run_id"])

    assert out["cancelled"] == 1
    assert engine.runs[mine["run_id"]].workers[0].status == CANCELLED
    close_spawned(engine)


# ── the tool surface ─────────────────────────────────────────────────────
class RecordingEngine:
    """Records what the handler asked for; the engine's own scoping is tested above."""

    def __init__(self):
        self.calls = []

    def status(self, run_id=None, all_sessions=False):
        self.calls.append(("status", run_id, all_sessions))
        return {"run_id": run_id, "session_id": "ses_owner", "tree": "tree", "workers": []}

    def cancel(self, run_id, all_sessions=False):
        self.calls.append(("cancel", run_id, all_sessions))
        return {"run_id": run_id, "cancelled": 1}

    def dispatch(self, **kwargs):
        self.calls.append(("dispatch", kwargs.get("goal"), False))
        return {"run_id": "omo_x"}


def test_the_status_and_tree_actions_pass_the_opt_in_through():
    engine = RecordingEngine()
    handler = make_omo_handler(engine)

    asyncio.run(handler({"action": "status"}))
    asyncio.run(handler({"action": "status", "all_sessions": True}))
    asyncio.run(handler({"action": "tree", "run_id": "omo_x"}))
    asyncio.run(handler({"action": "tree", "run_id": "omo_x", "all_sessions": True}))

    assert engine.calls == [
        ("status", None, False),
        ("status", None, True),
        ("status", "omo_x", False),
        ("status", "omo_x", True),
    ]


def test_the_cancel_action_passes_the_opt_in_through():
    engine = RecordingEngine()
    handler = make_omo_handler(engine)

    asyncio.run(handler({"action": "cancel", "run_id": "omo_x"}))
    asyncio.run(handler({"action": "cancel", "run_id": "omo_x", "all_sessions": True}))

    assert engine.calls == [("cancel", "omo_x", False), ("cancel", "omo_x", True)]


def test_the_opt_in_is_documented_in_the_schema():
    described = OMO_SCHEMA["parameters"]["properties"]["all_sessions"]

    assert described["type"] == "boolean"
    assert "escape" in described["description"]
    assert "another session" in described["description"]
    assert "before session attribution" in described["description"]
