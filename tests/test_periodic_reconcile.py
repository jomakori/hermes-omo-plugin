"""The periodic orphan sweep: a dead worker is settled without waiting for the cap.

Issue #59 fixed the record going stale once, at start. A gateway that stays up for
hours never looked again, so a worker whose process died at minute ten read RUNNING
until the 1800s cap released whatever was waiting on it. These tests pin the pass
itself: what it settles, what it leaves alone, and that repeating it is a no-op.
"""

import pathlib
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from orchestrator import session  # noqa: E402
from orchestrator.engine import OmoEngine  # noqa: E402
from orchestrator.models import (  # noqa: E402
    INTERRUPTED,
    PENDING,
    RUNNING,
    SUCCEEDED,
    Run,
    Worker,
)


class FakeCtx:
    def __init__(self, config=None):
        self._config = config or {}
        self.events = []

    def get_config(self, key, default=None):
        return self._config.get(key, default)

    def emit(self, event, payload=None):
        self.events.append((event, payload))

    def spawn_task(self, coro):
        coro.close()


class FakeStore:
    """A record on disk: a dict of runs, read fresh on every load like the real one."""

    def __init__(self):
        self.runs = {}
        self.saves = 0

    def load(self):
        return dict(self.runs)

    def save(self, runs):
        self.runs = dict(runs)
        self.saves += 1


def an_engine(config=None):
    """An engine whose start-up pass has already run against an empty record.

    Constructing restores, and restoring reconciles, so a row has to go on disk
    afterwards for the periodic pass to be the one that settles it.
    """
    engine = OmoEngine(FakeCtx(config), store=FakeStore())
    if engine._reconcile_stop is not None:
        engine._reconcile_stop.set()
    return engine


def a_dead_run(engine, *, status=RUNNING, session_id=None, run_id="omo_dead"):
    worker = Worker(run_id=run_id, agent_name="debugger", task="t", chain=("m",), status=status)
    run = Run(run_id=run_id, goal="g", workers=[worker], session_id=session_id)
    engine._store.runs[run_id] = run
    return run, worker


def test_a_tick_settles_a_running_worker_whose_session_is_gone():
    engine = an_engine()
    run, worker = a_dead_run(engine)

    engine._reconcile_tick()

    assert worker.status == INTERRUPTED
    assert engine._store.runs[run.run_id].workers[0].status == INTERRUPTED
    assert engine._store.saves == 1


def test_a_tick_leaves_a_run_this_process_is_executing_alone():
    engine = an_engine()
    run, worker = a_dead_run(engine)
    engine.runs[run.run_id] = run

    engine._reconcile_tick()

    assert worker.status == RUNNING
    assert engine._store.saves == 0


def test_a_tick_leaves_a_run_with_a_live_session_hold_alone():
    engine = an_engine()
    run, worker = a_dead_run(engine, session_id="sess_live")

    session.hold_run(run.run_id, run.session_id)
    try:
        engine._reconcile_tick()
    finally:
        session.release_run(run.run_id)

    assert worker.status == RUNNING, "a held session is live work, not a corpse"
    assert engine._store.saves == 0


def test_a_second_tick_does_not_settle_or_persist_again():
    engine = an_engine()
    run, worker = a_dead_run(engine)

    engine._reconcile_tick()
    engine._reconcile_tick()

    assert engine._store.saves == 1, "the adopted run is in memory now, so the second pass finds nothing"
    assert [w.status for w in run.workers] == [INTERRUPTED]


def test_a_settled_run_is_adopted_so_status_answers_from_it():
    engine = an_engine()
    run, _worker = a_dead_run(engine)

    engine._reconcile_tick()

    assert engine.runs[run.run_id].workers[0].status == INTERRUPTED


def test_a_worker_that_never_started_is_settled_too():
    engine = an_engine()
    _run, worker = a_dead_run(engine, status=PENDING)

    engine._reconcile_tick()

    assert worker.status == INTERRUPTED


def test_a_finished_worker_is_never_touched():
    engine = an_engine()
    _run, worker = a_dead_run(engine, status=SUCCEEDED)

    engine._reconcile_tick()

    assert worker.status == SUCCEEDED
    assert engine._store.saves == 0


def test_the_sweep_is_armed_by_default_and_can_be_disabled():
    engine = an_engine()

    assert engine._reconcile_thread is not None, "a tick must be running without anyone asking"
    assert engine._reconcile_thread.is_alive()
    assert engine._reconcile_thread.daemon

    disabled = an_engine({"reconcile_seconds": 0})

    assert disabled._reconcile_thread is None


def test_the_loop_survives_a_failing_pass():
    engine = an_engine()

    def broken():
        raise RuntimeError("store unreadable")

    engine._reconcile_tick = broken
    stop = threading.Event()
    thread = threading.Thread(target=engine._reconcile_loop, args=(stop, 0.01), daemon=True)
    thread.start()
    time.sleep(0.1)
    stop.set()
    thread.join(timeout=2)

    assert not thread.is_alive(), "one bad pass must not kill the sweep"
    assert any(event == "reconcile_failed" for event, _ in engine._ctx.events)
