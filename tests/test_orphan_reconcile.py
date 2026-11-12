"""A record left RUNNING by a process that is gone must not keep holding a slot.

Issue #59: a run-store row could still read `RUNNING` hours after the process that
wrote it had exited, and every later dispatch counted that row against capacity.
These tests pin the one reconciliation pass that runs at gateway start, between
loading the record and adopting it: what it settles, what it must leave alone, and
that it changes nothing when there is nothing to settle.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import sys
import types

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from orchestrator import engine as engine_module
from orchestrator import session
from orchestrator.chains import ChainResolver
from orchestrator.crash_reason import RETAINED
from orchestrator.engine import ORPHANED_CAUSE, ORPHANED_ERROR, OmoEngine
from orchestrator.models import INTERRUPTED, RUNNING, SUCCEEDED, Worker
from orchestrator.store import RunStore


# ── the host bridge and its stand-ins ────────────────────────────────────
def bind_session(monkeypatch, session_id):
    """Install a stand-in host bridge, the way the gateway binds a turn."""
    module = types.ModuleType("gateway.session_context")
    values = {"HERMES_SESSION_ID": session_id}
    module.get_session_env = lambda name, default="": values.get(name, default)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gateway", types.ModuleType("gateway"))
    monkeypatch.setitem(sys.modules, "gateway.session_context", module)


def unbind_session(monkeypatch):
    """A host with no bridge: the session that owned the record is not here any more."""
    monkeypatch.setitem(sys.modules, "gateway.session_context", None)


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


class FakeLifecycle:
    def __init__(self):
        self.launches = []
        self.cancelled = []

    def launch(self, request):
        self.launches.append(request.model)
        return FakeHandle(request.model)

    def wait(self, handle, *, timeout_seconds=None):
        return None

    def result(self, handle):
        return {"summary": "done", "model": handle.model}

    def create(self, *args, **kwargs):
        raise AssertionError("unexpected create")

    def cancel(self, handle, *, reason=""):
        self.cancelled.append(handle.model)
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


@pytest.fixture(autouse=True)
def _one_process_per_test(monkeypatch):
    """Holds are process memory, so every test starts from an empty process."""
    monkeypatch.setattr(session, "_HELD_RUNS", {})


def make_engine(path, config=None):
    """One engine with its own record — two of these are a restart."""
    lifecycle = FakeLifecycle()
    ctx = FakeCtx(lifecycle, config)
    engine = OmoEngine(ctx, lifecycle=lifecycle, request_factory=request_factory, store=RunStore(path=path))
    engine.chains = ChainResolver(config or {})
    return engine


def read_record(path):
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def interrupted_causes(engine):
    return [payload["cause"] for event, payload in engine._ctx.events if event == "worker_interrupted"]


def idle_worker(run_id="omo_r"):
    """A worker mid-run: no activity stamp, no result, no finish time."""
    return Worker(run_id=run_id, agent_name="hephaestus", task="fix it", chain=("hephaestus",))


# ── the pass ─────────────────────────────────────────────────────────────
def test_a_running_record_with_no_live_session_is_settled(tmp_path, monkeypatch):
    path = tmp_path / "runs.json"
    bind_session(monkeypatch, "sess-gone")
    engine = make_engine(path)
    run_id = engine.dispatch(goal="fix the flaky test", target="hephaestus")["run_id"]
    worker = engine.runs[run_id].workers[0]
    # The shape the dead process left: RUNNING, and nothing else written since.
    worker.status = RUNNING
    worker.finished_at = None
    worker.error = ""
    engine._persist()
    del engine
    # The process is gone: no bridge in this one, and nothing here holds the run.
    unbind_session(monkeypatch)

    restarted = make_engine(path)
    settled = restarted.runs[run_id].workers[0]

    assert settled.status == INTERRUPTED
    assert settled.error.startswith("orphaned record: no live session for this run")
    assert settled.error == ORPHANED_ERROR.format(claims="")
    assert "unverified" in settled.error
    assert settled.crash_reason["status"] == RETAINED
    assert "orphaned record: no live session for this run" in settled.crash_reason["reason"]
    assert settled.finished_at is not None
    assert restarted.runs[run_id].recovered is True
    # Written back, so the next reader does not have to rediscover it.
    assert read_record(path)["runs"][0]["workers"][0]["status"] == INTERRUPTED
    # Named on the seam the gateway already listens to, once.
    assert interrupted_causes(restarted) == [ORPHANED_CAUSE]
    # Reading the record again must not settle or re-announce it a second time.
    restarted.status(run_id)
    assert interrupted_causes(restarted) == [ORPHANED_CAUSE]
    assert read_record(path)["runs"][0]["workers"][0]["finished_at"] == settled.finished_at


def test_a_record_a_live_session_still_holds_is_left_untouched(tmp_path, monkeypatch):
    path = tmp_path / "runs.json"
    bind_session(monkeypatch, "sess-live")
    first = make_engine(path)
    # Background: the run is still in flight here, so this process holds its session.
    run_id = first.dispatch(goal="long job", target="hephaestus", background=True)["run_id"]
    worker = first.runs[run_id].workers[0]
    worker.status = RUNNING
    worker.finished_at = None
    worker.error = ""
    first._persist()
    assert session.run_is_held(run_id)

    second = make_engine(path)
    recorded = read_record(path)["runs"][0]

    assert recorded["workers"][0]["status"] == RUNNING
    assert recorded["workers"][0]["error"] == ""
    assert recorded["workers"][0]["finished_at"] is None
    assert recorded["recovered"] is False
    assert interrupted_causes(second) == []
    for coro in first._ctx.spawned:
        coro.close()


def test_a_record_with_nothing_to_settle_is_left_alone(tmp_path):
    path = tmp_path / "runs.json"
    engine = make_engine(path)
    run_id = engine.dispatch(goal="scan the repo", target="explore")["run_id"]
    assert engine.runs[run_id].workers[0].status == SUCCEEDED
    before = read_record(path)["runs"][0]

    restarted = make_engine(path)
    after = read_record(path)["runs"][0]

    assert [worker["status"] for worker in after["workers"]] == [worker["status"] for worker in before["workers"]]
    assert after["recovered"] is False
    assert after["workers"][0]["finished_at"] == before["workers"][0]["finished_at"]
    assert restarted.runs[run_id].workers[0].status == SUCCEEDED
    assert restarted.runs[run_id].recovered is False
    assert interrupted_causes(restarted) == []


def test_the_settled_wording_names_the_branch_the_record_claimed():
    claiming = idle_worker()
    claiming.verification = {
        "verifiable": True,
        "verified": True,
        "artifacts": [
            {"claimed": "fix/orphan-running-reconcile", "kind": "branch", "exists": True},
            {"claimed": "jomakori/hermes-omo-plugin#59", "kind": "pr", "exists": True},
        ],
    }

    error = engine_module._orphaned_error(claiming)
    assert error.startswith("orphaned record: no live session for this run")
    assert "(claimed branch fix/orphan-running-reconcile, pr jomakori/hermes-omo-plugin#59)" in error
    assert "unverified — check the working tree before re-dispatching" in error
    assert engine_module._orphaned_error(idle_worker()) == ORPHANED_ERROR.format(claims="")
