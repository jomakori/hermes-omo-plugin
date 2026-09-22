"""Task graphs, review cycles, role aliases and the internal-worker contract."""

from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import json
import threading
import time
from typing import Any

import pytest

from omo_tools.omo_tool import make_omo_handler
from orchestrator.chains import ChainResolver
from orchestrator.engine import OmoEngine
from orchestrator.guards import GuardError
from orchestrator.models import BLOCKED, SUCCEEDED
from orchestrator.personas import AGENTS_DIR, compose_context
from roster import AGENTS, ROLE_ALIASES


@dataclasses.dataclass
class FakeRequest:
    goal: str
    context: str | None
    role: str
    model: str | None
    allowed_toolsets: tuple
    metadata: dict


@dataclasses.dataclass
class FakeHandle:
    model: str
    goal: str


@dataclasses.dataclass
class FakeResult:
    summary: str = "done"
    structured_payload: Any = None
    error_message: str = ""


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
    """One scripted result per launch, in launch order."""

    def __init__(self, results=None, always_fail=False, delay=0.0, fail_goals=()):
        self.results = list(results or [])
        self.always_fail = always_fail
        self.delay = delay
        self.fail_goals = set(fail_goals)
        self.launches: list[FakeRequest] = []
        self.peak = 0
        self._inflight = 0
        self._returned = 0
        self._lock = threading.Lock()

    def launch(self, request):
        with self._lock:
            self.launches.append(request)
            self._inflight += 1
            self.peak = max(self.peak, self._inflight)
        if self.always_fail or request.goal in self.fail_goals:
            with self._lock:
                self._inflight -= 1
            raise RuntimeError("402 payment required: add credits to continue")
        if self.delay:
            time.sleep(self.delay)
        return FakeHandle(request.model or "", request.goal)

    def wait(self, handle, *, timeout_seconds=None):
        return None

    def result(self, handle):
        with self._lock:
            self._inflight -= 1
            index = self._returned
            self._returned += 1
        if index < len(self.results):
            return self.results[index]
        return FakeResult()

    def status_of(self, handle):
        return True

    def cancel(self, handle, *, reason=""):
        return None

    def goals(self) -> list[str]:
        return [launch.goal for launch in self.launches]


# The host binds the parent session for a turn in a ContextVar; a worker thread
# that does not inherit it gets "No active Hermes parent session is available."
PROBE: contextvars.ContextVar[str] = contextvars.ContextVar("probe", default="unset")


class ContextProbeLifecycle(ScriptedLifecycle):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.seen: list[str] = []

    def launch(self, request):
        self.seen.append(PROBE.get())
        return super().launch(request)


class FakeCtx:
    def __init__(self, lifecycle, config=None):
        self.subagent_lifecycle = lifecycle
        self._config = config or {}
        self.spawned: list[Any] = []
        self.events: list[Any] = []

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
    return engine


# ── task graph ────────────────────────────────────────────────────────
def test_graph_runs_dependents_after_their_dependency():
    lifecycle = ScriptedLifecycle()
    engine = make_engine(lifecycle)
    payload = engine.dispatch_graph(
        tasks=[
            {"id": "explore", "agent": "explore", "prompt": "map the repo"},
            {"id": "write", "agent": "hephaestus", "prompt": "write the fix", "depends_on": ["explore"]},
        ],
        goal="fix the bug",
    )

    assert payload["status"] == "succeeded"
    assert lifecycle.goals() == ["map the repo", "write the fix"]
    rows = {row["task_id"]: row for row in payload["workers"] if "task_id" in row}
    assert rows["write"]["depends_on"] == ["explore"]
    assert all(row["status"] == SUCCEEDED for row in rows.values())
    assert "depends: explore" in payload["tree"]


def test_graph_runs_independent_tasks_in_parallel():
    lifecycle = ScriptedLifecycle(delay=0.05)
    engine = make_engine(lifecycle)
    payload = engine.dispatch_graph(
        tasks=[
            {"id": "a", "agent": "explore", "prompt": "task a"},
            {"id": "b", "agent": "librarian", "prompt": "task b"},
        ],
        max_parallel=2,
    )

    assert payload["status"] == "succeeded"
    assert lifecycle.peak == 2, "independent tasks should overlap"


def test_graph_tasks_inherit_the_callers_context():
    """Dependency work must run inside the turn's context, not a bare thread."""
    token = PROBE.set("bound")
    try:
        lifecycle = ContextProbeLifecycle()
        engine = make_engine(lifecycle)
        engine.dispatch_graph(
            tasks=[
                {"id": "a", "agent": "explore", "prompt": "task a"},
                {"id": "b", "agent": "tester", "prompt": "task b", "depends_on": ["a"]},
            ],
            max_parallel=2,
        )
    finally:
        PROBE.reset(token)

    assert lifecycle.seen == ["bound", "bound"], f"worker context not inherited: {lifecycle.seen}"


def test_graph_blocks_dependents_of_a_failed_task():
    lifecycle = ScriptedLifecycle(always_fail=True)
    engine = make_engine(lifecycle)
    payload = engine.dispatch_graph(
        tasks=[
            {"id": "a", "agent": "explore", "prompt": "task a"},
            {"id": "b", "agent": "hephaestus", "prompt": "task b", "depends_on": ["a"]},
            {"id": "c", "agent": "momus", "prompt": "task c", "depends_on": ["b"]},
        ]
    )

    assert payload["status"] == "failed"
    rows = {row["task_id"]: row for row in payload["workers"] if "task_id" in row}
    assert rows["a"]["status"] == "FAILED"
    assert rows["b"]["status"] == BLOCKED
    assert rows["c"]["status"] == BLOCKED
    assert "task b" not in lifecycle.goals(), "a blocked task must not be launched"


@pytest.mark.parametrize(
    ("tasks", "message"),
    [
        ([{"id": "a", "agent": "explore", "prompt": "x", "depends_on": ["ghost"]}], "unknown task"),
        ([{"id": "a", "agent": "explore", "prompt": "x"}, {"id": "a", "agent": "momus", "prompt": "y"}], "unique"),
        ([{"id": "a", "agent": "explore", "prompt": ""}], "no prompt"),
        ([{"id": "a", "agent": "explore", "category": "quick", "prompt": "x"}], "exactly one of agent"),
        ([{"id": "a", "agent": "explore", "prompt": "x", "depends_on": ["a"]}], "depends on itself"),
        (
            [
                {"id": "a", "agent": "explore", "prompt": "x", "depends_on": ["b"]},
                {"id": "b", "agent": "momus", "prompt": "y", "depends_on": ["a"]},
            ],
            "cycle",
        ),
    ],
)
def test_graph_rejects_invalid_declarations(tasks, message):
    engine = make_engine(ScriptedLifecycle())
    with pytest.raises(GuardError, match=message):
        engine.dispatch_graph(tasks=tasks)


# ── review / verification cycles ──────────────────────────────────────
def test_review_problems_rerun_the_task_with_notes():
    lifecycle = ScriptedLifecycle(
        results=[
            FakeResult("first pass"),
            FakeResult(structured_payload={"verdict": "problems", "problems": ["no test for the failure path"]}),
            FakeResult("second pass"),
        ]
    )
    engine = make_engine(lifecycle)
    payload = engine.dispatch_graph(
        tasks=[{"id": "impl", "agent": "hephaestus", "prompt": "implement the fix"}],
        review=True,
        max_review_cycles=1,
    )

    assert len(lifecycle.launches) == 3, "implement, review, re-implement"
    assert lifecycle.goals()[1] == "review impl"
    assert "REVIEW NOTES" in lifecycle.goals()[2]
    assert "no test for the failure path" in lifecycle.goals()[2]
    reviewed = [row for row in payload["workers"] if row.get("task_id") == "impl"][0]
    assert reviewed["review_cycles"] == 1
    assert reviewed["status"] == SUCCEEDED
    assert not [row for row in payload["results"] if row["task_id"].endswith(":review")]


def test_review_pass_leaves_the_task_alone():
    lifecycle = ScriptedLifecycle(results=[FakeResult("done"), FakeResult(structured_payload={"verdict": "pass"})])
    engine = make_engine(lifecycle)
    payload = engine.dispatch_graph(
        tasks=[{"id": "impl", "agent": "hephaestus", "prompt": "implement the fix"}],
        review=True,
        max_review_cycles=2,
    )

    assert len(lifecycle.launches) == 2, "one implement + one review, no retry"
    row = [r for r in payload["workers"] if r.get("task_id") == "impl"][0]
    assert row["status"] == SUCCEEDED
    assert not row.get("review_cycles")


def test_review_stops_at_the_configured_bound():
    problems = FakeResult(structured_payload={"verdict": "problems", "problems": ["still wrong"]})
    lifecycle = ScriptedLifecycle(results=[FakeResult(), problems, FakeResult(), problems, FakeResult()])
    engine = make_engine(lifecycle)
    payload = engine.dispatch_graph(
        tasks=[{"id": "impl", "agent": "hephaestus", "prompt": "implement"}],
        review=True,
        max_review_cycles=2,
    )

    assert len(lifecycle.launches) == 5, "implement + (review, re-implement) x2"
    row = [r for r in payload["workers"] if r.get("task_id") == "impl"][0]
    assert row["review_cycles"] == 2


def test_a_failed_reviewer_does_not_drag_the_run_status():
    """A review that could not run leaves the task as it was, and says so in the tree."""
    lifecycle = ScriptedLifecycle(fail_goals=["review impl"])
    engine = make_engine(lifecycle)
    payload = engine.dispatch_graph(
        tasks=[{"id": "impl", "agent": "hephaestus", "prompt": "implement"}],
        review=True,
        max_review_cycles=1,
    )

    assert payload["status"] == "succeeded", "the task succeeded; its reviewer did not run"
    reviewer = [row for row in payload["workers"] if row.get("task_id") == "impl:review"][0]
    assert reviewer["status"] == "FAILED", "a dead reviewer must still be visible"
    assert lifecycle.goals().count("implement") == 1, "a review that never ran must not re-run the task"
    assert "review impl" in lifecycle.goals(), "the reviewer was attempted"


def test_review_is_off_by_default():
    lifecycle = ScriptedLifecycle()
    engine = make_engine(lifecycle, config={"max_review_cycles": 0})
    engine.dispatch_graph(tasks=[{"id": "impl", "agent": "hephaestus", "prompt": "implement"}], review=True)
    assert len(lifecycle.launches) == 1


# ── configuration surface ─────────────────────────────────────────────
def test_enabled_agents_blocks_everything_else():
    lifecycle = ScriptedLifecycle()
    engine = make_engine(lifecycle, config={"enabled_agents": ["explore"]})
    engine.dispatch(goal="ok", target="explore")
    with pytest.raises(GuardError, match="not enabled"):
        engine.dispatch(goal="nope", target="librarian")
    with pytest.raises(GuardError, match="not enabled"):
        engine.dispatch(goal="nope", category="deep")


def test_role_aliases_resolve_to_the_roster():
    lifecycle = ScriptedLifecycle()
    engine = make_engine(lifecycle)
    assert "hephaestus" in engine.dispatch(goal="build it", target="implementer")["agent"]
    assert "sisyphus-junior" in engine.dispatch(goal="document it", target="documenter")["agent"]
    assert "momus" in engine.dispatch(goal="review it", target="reviewer")["agent"]


def test_role_aliases_are_configurable():
    lifecycle = ScriptedLifecycle()
    engine = make_engine(lifecycle, config={"roles": {"implementer": "atlas"}})
    assert "atlas" in engine.dispatch(goal="build it", target="implementer")["agent"]


def test_every_requested_role_dispatches():
    """A role in the schema and the README must survive a dispatch, not just resolve."""
    for role, target in ROLE_ALIASES.items():
        assert target in AGENTS or target in {"quick", "deep", "ultrabrain", "visual-engineering", "writing"}, role
        engine = make_engine(ScriptedLifecycle())
        outcome = engine.dispatch(goal=f"probe {role}", target=role)
        assert outcome["status"] == "succeeded", f"{role} failed to dispatch: {outcome['error']}"


def test_every_roster_entry_has_a_persona():
    missing = [name for name in AGENTS if not (AGENTS_DIR / f"{name}.md").is_file()]
    assert not missing, f"roster entries without a persona: {missing}"


# ── internal-worker contract ──────────────────────────────────────────
def test_worker_context_disables_user_facing_communication():
    context = compose_context("explore", "repo conventions here")
    assert "USER-FACING COMMUNICATION: DISABLED" in context
    assert "Never address a user" in context
    assert 'binding="authoritative"' in context
    assert context.index("internal_worker") < context.index("persona")
    assert context.rstrip().endswith("</task_context>")


def test_worker_context_keeps_the_contract_without_a_persona():
    assert "USER-FACING COMMUNICATION: DISABLED" in compose_context("no-such-agent", None)


# ── tool surface ──────────────────────────────────────────────────────
def test_tool_dispatches_a_graph_and_renders_its_tree():
    lifecycle = ScriptedLifecycle()
    engine = make_engine(lifecycle)
    run_id = json.loads(
        asyncio.run(
            make_omo_handler(engine)(
                {
                    "action": "dispatch",
                    "goal": "ship it",
                    "tasks": [
                        {"id": "a", "agent": "explore", "prompt": "task a"},
                        {"id": "b", "agent": "tester", "prompt": "task b", "depends_on": ["a"]},
                    ],
                }
            )
        )
    )["run_id"]
    rendered = asyncio.run(make_omo_handler(engine)({"action": "tree", "run_id": run_id}))
    assert "omo run" in rendered
    assert "task_id" in rendered


def test_tool_reviews_a_single_task_when_asked():
    """The review controls must not be accepted and then ignored on the single path."""
    lifecycle = ScriptedLifecycle(
        results=[
            FakeResult("first"),
            FakeResult(structured_payload={"verdict": "problems", "problems": ["no test"]}),
            FakeResult("second"),
        ]
    )
    engine = make_engine(lifecycle)
    payload = json.loads(
        asyncio.run(
            make_omo_handler(engine)(
                {
                    "action": "dispatch",
                    "goal": "single task",
                    "agent": "tester",
                    "review": True,
                    "max_review_cycles": 1,
                }
            )
        )
    )

    reviewed = [row for row in payload["workers"] if row.get("task_id") == "task-1"][0]
    assert reviewed["review_cycles"] == 1, "the review must actually have run"
    assert len(lifecycle.launches) == 3, "implement, review, re-implement"


def test_tool_keeps_the_plain_path_without_review_controls():
    engine = make_engine(ScriptedLifecycle())
    payload = json.loads(asyncio.run(make_omo_handler(engine)({"action": "dispatch", "goal": "x", "agent": "explore"})))
    assert "agent" in payload and "workers" not in payload


def test_tool_renders_a_rejected_graph_instead_of_raising():
    engine = make_engine(ScriptedLifecycle())
    payload = json.loads(
        asyncio.run(
            make_omo_handler(engine)(
                {"action": "dispatch", "tasks": [{"id": "a", "agent": "explore", "prompt": "x", "depends_on": ["a"]}]}
            )
        )
    )
    assert payload["rejected"] is True
    assert "itself" in payload["error"]
