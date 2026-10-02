"""Progress events reach the host bus with bare names, and the notifier delivers them.

Two contracts are pinned here:

* every real transition emits a **bare** event name (the host namespaces it as
  ``omo:<name>``; a name that already carries the plugin prefix is rejected);
* the notifier posts exactly one message per run and edits it in place, persisting
  the id so a restart keeps editing the same message.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

from orchestrator.chains import ChainResolver
from orchestrator.engine import OmoEngine
from orchestrator.models import Run, Worker
from orchestrator.status_notifier import StatusNotifier
from orchestrator.status_tracker import StatusTracker
from roster import AGENTS


@dataclasses.dataclass
class FakeRequest:
    goal: str
    context: str | None
    role: str
    model: str | None
    allowed_toolsets: tuple
    metadata: dict


@dataclasses.dataclass
class FakeResult:
    summary: str = "done"
    structured_payload: Any = None
    error_message: str = ""


@dataclasses.dataclass
class FakeHandle:
    model: str
    goal: str


def request_factory(*, goal, context, spec, model, toolsets):
    return FakeRequest(
        goal=goal,
        context=context,
        role="orchestrator" if spec.orchestrator else "leaf",
        model=model,
        allowed_toolsets=toolsets,
        metadata={"omo_agent": spec.name},
    )


class ScriptedLifecycle:
    """One scripted outcome per launch; ``fail_from`` rejects every launch at/after it."""

    def __init__(self, results: list[Any] | None = None, fail_from: int | None = None) -> None:
        self.results = list(results or [])
        self.fail_from = fail_from
        self.launches: list[FakeRequest] = []

    def launch(self, request):
        self.launches.append(request)
        if self.fail_from is not None and len(self.launches) >= self.fail_from:
            # A provider rejection: the chain walk stops here.
            raise RuntimeError("provider rejected the model")
        return FakeHandle(request.model, request.goal)

    def wait(self, handle, *, timeout_seconds=None):
        return None

    def result(self, handle):
        if self.results:
            return self.results.pop(0)
        return FakeResult(structured_payload={"verdict": "pass", "problems": []})

    def status_of(self, handle):
        return True

    def cancel(self, handle, *, reason=""):
        return None


class FakeCtx:
    def __init__(self, lifecycle, config=None):
        self.subagent_lifecycle = lifecycle
        self._config = config or {}
        self.spawned: list[Any] = []
        self.events: list[tuple[str, dict]] = []

    def get_config(self, key, default=None):
        return self._config.get(key, default)

    def emit(self, event, payload=None):
        self.events.append((event, payload or {}))

    def spawn_task(self, coro):
        self.spawned.append(coro)

    def names(self):
        return [name for name, _ in self.events]


def make_engine(lifecycle, config=None):
    ctx = FakeCtx(lifecycle, config)
    engine = OmoEngine(ctx, lifecycle=lifecycle, request_factory=request_factory)
    engine.chains = ChainResolver(config or {})
    return ctx, engine


# ── emission ──────────────────────────────────────────────────────────
def test_engine_emits_bare_event_names():
    ctx, engine = make_engine(ScriptedLifecycle())
    engine.dispatch(goal="add a widget", target="hephaestus")

    names = ctx.names()
    assert names[:2] == ["worker_running", "worker_succeeded"]
    # Bare names only: the host prefixes them with the plugin key itself, and
    # rejects a name that already carries one.
    assert all(":" not in name for name in names)


def test_engine_emits_worker_failed_when_the_chain_is_exhausted():
    ctx, engine = make_engine(ScriptedLifecycle(fail_from=1), config={"max_attempts_per_agent": 3})
    engine.dispatch(goal="do the impossible", target="hephaestus")

    names = ctx.names()
    assert names[0] == "worker_running"
    assert names[-1] == "worker_failed"


def test_engine_emits_worker_cancelled_for_a_prestart_cancel():
    ctx, engine = make_engine(ScriptedLifecycle())
    run, worker = _single_worker_run(engine)
    worker.cancel_requested = True

    asyncio.run(engine._run_worker(run, worker, _request_for(worker)))

    assert "worker_cancelled" in ctx.names()


def test_engine_emits_worker_done_bare_on_the_background_path():
    ctx, engine = make_engine(ScriptedLifecycle())
    run, worker = _single_worker_run(engine)

    asyncio.run(engine._run_worker(run, worker, _request_for(worker)))

    assert ctx.names()[-1] == "worker_done"
    assert all(":" not in name for name in ctx.names())


def _request_for(worker):
    return request_factory(
        goal=worker.task, context=None, spec=AGENTS[worker.agent_name], model=worker.model, toolsets=None
    )


def _single_worker_run(engine):
    run = Run(run_id="omo_test", goal="background job")
    worker = Worker(run_id=run.run_id, agent_name="hephaestus", task="background job", chain=("gpt-x",), model="gpt-x")
    run.workers.append(worker)
    engine.runs[run.run_id] = run
    return run, worker


def test_graph_emits_created_started_settled_finished():
    ctx, engine = make_engine(ScriptedLifecycle())
    engine.dispatch_graph(
        tasks=[
            {"id": "explore", "agent": "explore", "prompt": "map the repo"},
            {"id": "write", "agent": "hephaestus", "prompt": "write the fix", "depends_on": ["explore"]},
        ],
        goal="fix the bug",
    )

    names = ctx.names()
    assert names[0] == "run_created"
    assert names[-1] == "run_finished"
    assert names.count("task_started") == 2
    assert names.count("task_settled") == 2
    assert all(":" not in name for name in names)


def test_graph_emits_task_blocked_for_a_failed_dependency():
    lifecycle = ScriptedLifecycle(fail_from=1)
    ctx, engine = make_engine(lifecycle)
    engine.dispatch_graph(
        tasks=[
            {"id": "explore", "agent": "explore", "prompt": "map the repo"},
            {"id": "write", "agent": "hephaestus", "prompt": "write the fix", "depends_on": ["explore"]},
        ],
        goal="fix the bug",
    )

    assert "task_blocked" in ctx.names()


def test_graph_emits_reviewer_failed_when_the_reviewer_itself_fails():
    # Launch 1 = the task (succeeds); every reviewer launch is rejected.
    lifecycle = ScriptedLifecycle(fail_from=2)
    ctx, engine = make_engine(lifecycle)
    engine.dispatch_graph(
        tasks=[{"id": "write", "agent": "hephaestus", "prompt": "write the fix"}],
        goal="review this",
        review=True,
        max_review_cycles=1,
    )

    assert "reviewer_failed" in ctx.names()


def test_graph_emits_review_verdict_on_a_readable_verdict():
    lifecycle = ScriptedLifecycle()
    ctx, engine = make_engine(lifecycle)
    engine.dispatch_graph(
        tasks=[{"id": "write", "agent": "hephaestus", "prompt": "write the fix"}],
        goal="review this",
        review=True,
        max_review_cycles=1,
    )

    verdicts = [payload for name, payload in ctx.events if name == "review_verdict"]
    assert verdicts and verdicts[0]["verdict"] == "pass"


# ── notifier delivery ─────────────────────────────────────────────────
class FakeTransport:
    """Records posts/edits; simulates both a live message and a gone one."""

    def __init__(self, *, gone=False):
        self.posts: list[tuple[str, str]] = []
        self.edits: list[tuple[str, str, str]] = []
        self.gone = gone
        self.next_id = "9001"
        self.last_gone = False
        self.usable = True
        self._n = 0

    async def post(self, run_key, text):
        self._n += 1
        mid = f"{self.next_id}{'' if self._n == 1 else self._n}"
        self.posts.append((run_key, text))
        return mid

    async def edit(self, run_key, message_id, text):
        self.last_gone = self.gone
        if self.gone:
            return False
        self.edits.append((run_key, message_id, text))
        return True


class FakeEngineForStatus:
    def __init__(self, route):
        self._route = route
        self.recorded: list[tuple[str, str | None]] = []

    def run_route(self, run_id):
        return self._route

    def note_status_message(self, run_id, message_id):
        self.recorded.append((run_id, message_id))


def _notifier(engine):
    tracker = StatusTracker(clock=lambda: 1000.0)
    factory = lambda route: FakeTransport()  # noqa: E731 - a one-line test seam
    return StatusNotifier(FakeCtx(None), engine=engine, tracker=tracker, transport_factory=factory)


def test_notifier_posts_once_then_edits_the_same_message():
    engine = FakeEngineForStatus({"platform": "discord", "chat_id": "42", "thread_id": ""})
    notifier = _notifier(engine)
    transport = FakeTransport()
    notifier._transport_factory = lambda route: transport

    notifier.process("worker_running", {"run_id": "omo_1", "goal": "g", "agent": "hephaestus"})
    notifier.process("worker_succeeded", {"run_id": "omo_1", "goal": "g", "agent": "hephaestus"})

    assert len(transport.posts) == 1
    assert [edit[1] for edit in transport.edits] == ["9001"]
    # The id is written back onto the run record so a restart keeps editing it.
    assert engine.recorded[0] == ("omo_1", "9001")


def test_notifier_posts_a_fresh_message_when_the_old_one_is_gone():
    engine = FakeEngineForStatus({"platform": "discord", "chat_id": "42", "thread_id": ""})
    notifier = _notifier(engine)
    transport = FakeTransport(gone=True)
    notifier._transport_factory = lambda route: transport

    notifier.process("worker_running", {"run_id": "omo_1", "goal": "g", "agent": "hephaestus"})
    notifier.process("worker_succeeded", {"run_id": "omo_1", "goal": "g", "agent": "hephaestus"})
    # The edit hit a gone message, so the id was cleared; the next transition
    # posts a fresh one instead of editing nothing.
    notifier.process("worker_done", {"run_id": "omo_1", "goal": "g", "agent": "hephaestus", "status": "SUCCEEDED"})

    assert len(transport.posts) == 2
    # the gone id was cleared before the fresh post was recorded
    assert ("omo_1", None) in engine.recorded


def test_notifier_skips_delivery_without_a_route():
    engine = FakeEngineForStatus(None)
    notifier = _notifier(engine)
    transport = FakeTransport()
    notifier._transport_factory = lambda route: transport

    notifier.process("worker_running", {"run_id": "omo_1", "goal": "g", "agent": "hephaestus"})

    assert transport.posts == []


# ── end-to-end: emitter -> tracker -> publisher (no gateway, no network) ──
def _wired_notifier(engine):
    """Attach a notifier whose transport is a fake and whose edits are never throttled."""
    tracker = StatusTracker(clock=lambda: 1000.0, min_edit_interval=0.0)
    transport = FakeTransport()
    notifier = StatusNotifier(
        engine._ctx if hasattr(engine, "_ctx") else None,
        engine=engine,
        tracker=tracker,
        transport_factory=lambda route: transport,
    )
    engine.set_status_notifier(notifier)
    return notifier, transport


def _drain(notifier):
    while not notifier._queue.empty():
        notifier.process(*notifier._queue.get_nowait())


def test_declared_graph_creates_one_message_and_edits_it_per_state_change():
    ctx, engine = make_engine(ScriptedLifecycle())
    # The real session route comes from the gateway bridge; the test supplies one.
    engine.run_route = lambda run_id: {"platform": "discord", "chat_id": "42", "thread_id": ""}
    notifier, transport = _wired_notifier(engine)

    engine.dispatch_graph(
        tasks=[
            {"id": "explore", "agent": "explore", "prompt": "map the repo"},
            {"id": "write", "agent": "hephaestus", "prompt": "write the fix", "depends_on": ["explore"]},
        ],
        goal="fix the bug",
    )
    _drain(notifier)

    # ONE message per run: a single post, then every later transition is an edit.
    assert len(transport.posts) == 1
    assert len(transport.edits) >= 3
    # and every edit targets that same message id
    assert {edit[1] for edit in transport.edits} == {"9001"}
    # the id was persisted onto the run record (so a restart keeps editing it)
    assert next(iter(engine.runs.values())).status_message_id == "9001"

    final = transport.edits[-1][2]
    assert final.count("🏗️ omo:") == 2
    assert "- run ✅" in final


def test_declared_graph_review_row_folds_into_the_producer_block():
    ctx, engine = make_engine(ScriptedLifecycle())
    engine.run_route = lambda run_id: {"platform": "discord", "chat_id": "42", "thread_id": ""}
    notifier, transport = _wired_notifier(engine)

    engine.dispatch_graph(
        tasks=[{"id": "write", "agent": "hephaestus", "prompt": "write the fix"}],
        goal="review this",
        review=True,
        max_review_cycles=1,
    )
    _drain(notifier)

    texts = [text for _, text in transport.posts] + [text for _, _, text in transport.edits]
    assert len(transport.posts) == 1
    # the reviewer ran: 🔍 named it mid-flight, then the verdict settled the row
    assert any("- review 🔍 momus · " in text for text in texts)
    assert any("- review ✅ momus · pass" in text for text in texts)
