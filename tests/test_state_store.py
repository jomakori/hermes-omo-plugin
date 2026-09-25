"""The registry has to outlive the process that wrote it.

A restart used to answer `runs: []` while the work it had already paid for sat on
disk, so the caller re-dispatched it. These tests pin the record: what is kept,
what a damaged file does, and what a worker that has no process behind it reads
as after a restart.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from orchestrator.chains import ChainResolver
from orchestrator.engine import INTERRUPTED_ERROR, OmoEngine
from orchestrator.models import BLOCKED, INTERRUPTED, PENDING, RUNNING, SUCCEEDED, Run, Worker
from orchestrator.store import RunStore


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


def make_engine(path, config=None):
    """One engine with its own record — two of these are a restart."""
    lifecycle = FakeLifecycle()
    ctx = FakeCtx(lifecycle, config)
    engine = OmoEngine(
        ctx,
        lifecycle=lifecycle,
        request_factory=request_factory,
        store=RunStore(path=path),
    )
    engine.chains = ChainResolver(config or {})
    return engine


def read_record(path):
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


# ── the record ───────────────────────────────────────────────────────────
def test_the_record_is_durable_and_leaves_no_partial_file(tmp_path):
    path = tmp_path / "runs.json"
    lifecycle = FakeLifecycle()
    ctx = FakeCtx(lifecycle)
    engine = OmoEngine(ctx, lifecycle=lifecycle, request_factory=request_factory, store=RunStore(path=path))
    engine.chains = ChainResolver({})
    out = engine.dispatch(goal="scan the repo", target="explore")

    assert path.is_file()
    # Only the record, never a half-written sibling.
    assert [entry.name for entry in tmp_path.iterdir()] == ["runs.json"]

    record = read_record(path)
    assert record["version"] == 1
    stored = record["runs"][0]
    assert stored["run_id"] == out["run_id"]
    assert stored["workers"][0]["status"] == SUCCEEDED
    assert "handle" not in stored["workers"][0]
    assert "result" not in stored["workers"][0]


def test_hop_history_survives_the_round_trip(tmp_path):
    path = tmp_path / "runs.json"
    engine = make_engine(path)
    out = engine.dispatch(goal="scan", target="explore")
    hops = [{"model": "deepseek-v4-flash", "reason": "success"}]
    engine.runs[out["run_id"]].workers[0].hop_history = hops
    engine._persist()

    adopted = RunStore(path=path).load()
    assert adopted[out["run_id"]].workers[0].hop_history == hops


def test_a_damaged_record_reads_as_empty_not_as_a_crash(tmp_path):
    path = tmp_path / "runs.json"
    for damaged in ("", '{"version": 1, "runs": [', '["not", "a", "registry"]', '{"runs": {"a": 1}}', "null"):
        path.write_text(damaged, encoding="utf-8")
        assert RunStore(path=path).load() == {}

    # A record with one unreadable entry keeps the entries it can read.
    path.write_text(
        json.dumps({"version": 1, "runs": ["nonsense", {"run_id": ""}, {"run_id": "omo_kept", "goal": "g"}]}),
        encoding="utf-8",
    )
    runs = RunStore(path=path).load()
    assert set(runs) == {"omo_kept"}

    # A missing file is the same answer.
    assert RunStore(path=tmp_path / "nope.json").load() == {}


def test_the_record_is_bounded_newest_first(tmp_path):
    path = tmp_path / "runs.json"
    runs = {f"omo_{index}": Run(run_id=f"omo_{index}", goal=f"g{index}", created_at=float(index)) for index in range(3)}
    RunStore(path=path, max_runs=2).save(runs)

    kept = list(RunStore(path=path).load())
    assert sorted(kept) == ["omo_1", "omo_2"]


# ── restart recovery ─────────────────────────────────────────────────────
def test_restart_answers_with_the_record_and_names_unfinished_work(tmp_path):
    path = tmp_path / "runs.json"
    engine = make_engine(path)
    out = engine.dispatch(goal="scan the repo", target="explore")
    run_id = out["run_id"]
    # The shape a restart actually interrupts: live work, last checkpoint mid-flight.
    engine.runs[run_id].workers[0].status = RUNNING
    engine._persist()

    restarted = make_engine(path)
    status = restarted.status(run_id)

    assert status["run_id"] == run_id
    assert status["workers"][0]["status"] == INTERRUPTED
    assert "explore" in status["tree"]
    assert INTERRUPTED in status["tree"]
    worker = restarted.runs[run_id].workers[0]
    assert worker.error == INTERRUPTED_ERROR
    assert "unverified" in worker.error
    assert restarted.runs[run_id].recovered is True
    # The registry lists it too, so `status` with no id is not an empty answer.
    assert [run["run_id"] for run in restarted.status()["runs"]] == [run_id]


def test_a_worker_that_never_launched_is_interrupted_too(tmp_path):
    path = tmp_path / "runs.json"
    engine = make_engine(path)
    out = engine.dispatch(goal="long job", target="hephaestus", background=True)
    assert engine.runs[out["run_id"]].workers[0].status in (PENDING, RUNNING)

    restarted = make_engine(path)
    assert restarted.runs[out["run_id"]].workers[0].status == INTERRUPTED
    for coro in engine._ctx.spawned:
        coro.close()


def test_finished_work_stays_finished_across_a_restart(tmp_path):
    path = tmp_path / "runs.json"
    engine = make_engine(path)
    out = engine.dispatch(goal="scan", target="explore")

    restarted = make_engine(path)
    assert restarted.runs[out["run_id"]].workers[0].status == SUCCEEDED
    assert restarted.runs[out["run_id"]].recovered is False
    assert restarted.status(out["run_id"])["workers"][0]["status"] == SUCCEEDED


def test_a_blocked_worker_is_not_reported_as_interrupted(tmp_path):
    path = tmp_path / "runs.json"
    engine = make_engine(path)
    out = engine.dispatch_graph(
        tasks=[
            {"id": "a", "agent": "explore", "prompt": "recon"},
            {"id": "b", "agent": "debugger", "prompt": "fix", "depends_on": ["a"]},
        ]
    )
    assert out["run_id"]

    restarted = make_engine(path)
    # Nothing was left running by the graph, so nothing is rewritten as interrupted.
    assert all(worker.status != INTERRUPTED for worker in restarted.runs[out["run_id"]].workers)


def test_the_live_run_wins_over_the_record_it_replaces(tmp_path):
    path = tmp_path / "runs.json"
    first = make_engine(path)
    old = first.dispatch(goal="scan", target="explore")
    first.runs[old["run_id"]].workers[0].status = RUNNING
    first._persist()

    second = make_engine(path)
    fresh = second.dispatch(goal="build", target="hephaestus")
    listed = {run["run_id"]: run for run in second.status()["runs"]}

    assert set(listed) == {old["run_id"], fresh["run_id"]}
    assert listed[fresh["run_id"]]["tree"].count(SUCCEEDED) == 1
    assert second.runs[old["run_id"]].workers[0].status == INTERRUPTED


def test_a_run_without_a_store_lives_only_in_memory(tmp_path):
    lifecycle = FakeLifecycle()
    engine = OmoEngine(FakeCtx(lifecycle), lifecycle=lifecycle, request_factory=request_factory)
    engine.chains = ChainResolver({})
    out = engine.dispatch(goal="scan", target="explore")

    assert engine.status(out["run_id"])["workers"][0]["status"] == SUCCEEDED
    assert list(tmp_path.iterdir()) == []


# ── shutdown ─────────────────────────────────────────────────────────────
def test_shutdown_checkpoints_and_stops_paying_for_live_work(tmp_path):
    path = tmp_path / "runs.json"
    engine = make_engine(path)
    out = engine.dispatch(goal="scan", target="explore")
    worker = engine.runs[out["run_id"]].workers[0]
    worker.status = RUNNING
    worker.handle = FakeHandle(worker.model or "")
    engine._persist()

    engine.shutdown()

    assert worker.status == INTERRUPTED
    assert worker.cancel_requested is True
    assert worker.handle.model in engine._ctx.subagent_lifecycle.cancelled
    assert "shut down mid-run" in worker.error
    assert RunStore(path=path).load()[out["run_id"]].workers[0].status == INTERRUPTED


def test_shutdown_leaves_finished_work_alone(tmp_path):
    path = tmp_path / "runs.json"
    engine = make_engine(path)
    out = engine.dispatch(goal="scan", target="explore")

    engine.shutdown()

    assert engine.runs[out["run_id"]].workers[0].status == SUCCEEDED
    assert RunStore(path=path).load()[out["run_id"]].workers[0].status == SUCCEEDED


# ── the payload contract ─────────────────────────────────────────────────
def test_restoring_adds_no_new_payload_keys(tmp_path):
    path = tmp_path / "runs.json"
    engine = make_engine(path)
    out = engine.dispatch(goal="scan", target="explore")

    restarted = make_engine(path)
    assert set(restarted.status()) == set(engine.status())
    assert set(restarted.status(out["run_id"])) == set(engine.status(out["run_id"]))
    assert set(restarted.status(out["run_id"])["claim_boundary"]) == {
        "boundary",
        "observed",
        "self_reported",
    }


def test_a_worker_round_trips_every_durable_field():
    worker = Worker(
        run_id="omo_x",
        agent_name="explore",
        task="scan",
        chain=("a", "b"),
        model="b",
        status=BLOCKED,
        error="dependency failed",
        task_id="recon",
        depends_on=("a",),
        parent_id="a",
        review_cycles=2,
        review_verdict="problems",
        hop_history=[{"model": "a", "reason": "rate_limit"}],
        finished_at=123.5,
    )
    restored = Worker.from_dict(json.loads(json.dumps(worker.to_dict())))

    assert restored is not None
    assert restored == worker
    assert restored.depends_on == ("a",)
    assert restored.finished_at == 123.5
