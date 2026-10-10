"""A finished run's result must stay readable through `status` / `tree`.

A background dispatch answers with a `run_id` and the caller polls it with
`status`/`tree`. That read path is the only place a caller can learn what the
worker produced: the result object lives on the run registry, and if the read
path drops it, a lost result is indistinguishable from a run that produced
nothing — the caller's only workaround is asking every worker to duplicate its
own output into a file by hand.

The contract is the tool's own: `claim_boundary` names `result.summary` and
`result.structured_payload` as the worker's claims, and the return for a
background dispatch carries that boundary with a `run_id` to poll.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from omo_tools.omo_tool import make_omo_handler  # noqa: E402
from orchestrator.chains import ChainResolver  # noqa: E402
from orchestrator.engine import OmoEngine  # noqa: E402
from orchestrator.models import SUCCEEDED  # noqa: E402


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
class TerminalState:
    """The host's terminal state carries its name (mirrors the real result)."""

    name: str = "SUCCEEDED"


@dataclasses.dataclass
class FakeResult:
    """Stands in for the host's SubagentResult: an object, not a dict."""

    summary: str = "did the work"
    structured_payload: dict | None = dataclasses.field(default_factory=lambda: {"findings": ["okt180", "container"]})
    usage_metadata: dict | None = dataclasses.field(default_factory=lambda: {"total_tokens": 42})
    terminal_state: object | None = dataclasses.field(default_factory=TerminalState)
    error_message: str | None = None

    def __str__(self) -> str:  # a host result renders as its repr, not a dict
        return f"SubagentResult(summary={self.summary!r})"


class FakeHandle:
    def __init__(self, model):
        self.model = model


class FakeLifecycle:
    def __init__(self, result=None):
        self.launches = []
        self._result = result if result is not None else FakeResult()

    def launch(self, request):
        self.launches.append(request.model)
        return FakeHandle(request.model)

    def wait(self, handle, *, timeout_seconds=None):
        return None

    def result(self, handle):
        return self._result

    def cancel(self, handle, *, reason=""):
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


def make_engine(lifecycle):
    ctx = FakeCtx(lifecycle)
    engine = OmoEngine(ctx, lifecycle=lifecycle, request_factory=request_factory)
    engine.chains = ChainResolver({})
    return ctx, engine


def finish_background(engine, ctx):
    """Run the spawned background coroutine to completion."""
    assert ctx.spawned, "background dispatch must have spawned the run"
    asyncio.run(ctx.spawned.pop())


def test_status_of_a_finished_background_run_exposes_the_result():
    lifecycle = FakeLifecycle()
    ctx, engine = make_engine(lifecycle)
    out = engine.dispatch(goal="research okt180", target="librarian", background=True)
    run_id = out["run_id"]
    finish_background(engine, ctx)

    worker = engine.runs[run_id].workers[0]
    assert worker.status == SUCCEEDED
    assert worker.result is not None

    row = engine.status(run_id)["workers"][0]

    assert row["status"] == SUCCEEDED
    assert "result" in row, "a finished run's result must survive the status read path"
    assert row["result"]["summary"] == "did the work"
    # The structured payload and the host-observed fields are readable too — named
    # exactly as the claim_boundary names them.
    assert row["result"]["structured_payload"] == {"findings": ["okt180", "container"]}
    assert row["result"]["usage_metadata"] == {"total_tokens": 42}
    assert row["result"]["terminal_state"] == "SUCCEEDED"


def test_tree_action_of_a_finished_background_run_exposes_the_result():
    lifecycle = FakeLifecycle()
    ctx, engine = make_engine(lifecycle)
    out = engine.dispatch(goal="research okt180", target="librarian", background=True)
    finish_background(engine, ctx)

    payload = json.loads(asyncio.run(make_omo_handler(engine)({"action": "tree", "run_id": out["run_id"]})))

    assert payload["workers"][0]["result"]["summary"] == "did the work"


def test_a_running_run_has_no_result_yet():
    lifecycle = FakeLifecycle()
    ctx, engine = make_engine(lifecycle)
    out = engine.dispatch(goal="long job", target="librarian", background=True)

    row = engine.status(out["run_id"])["workers"][0]

    assert "result" not in row or row["result"] is None
    for coro in ctx.spawned:
        coro.close()
