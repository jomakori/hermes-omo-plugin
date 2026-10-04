from __future__ import annotations

import dataclasses
import json
import threading
import unittest.mock as mock

import pytest

from jev.metrics import agreement, brier, ece, format_report, summarize
from jev.shadow import (
    ShadowLogger,
    decisions_agree,
    pack_confidence,
    run_shadow,
    run_shadow_pack,
    state_hash,
)
from orchestrator.chains import ChainResolver
from orchestrator.engine import OmoEngine

_ROUTE_INTENT_RESPONSE = {
    "model": "jev-latest",
    "answers": {
        "tier": {"choice": "scoped", "confidence": 0.87},
        "domain": {"choice": "logic", "confidence": 0.91},
    },
}

# Hand-computed fixture: agreement 0.5, Brier 0.1425, ECE 0.325 (one record/bin).
_FIXTURE = [
    {"pack": "route_intent", "jev_confidence": 0.8, "agreed": True, "ts": "2026-10-01T00:00:00+00:00"},
    {"pack": "route_intent", "jev_confidence": 0.6, "agreed": False, "ts": "2026-10-02T00:00:00+00:00"},
    {"pack": "route_intent", "jev_confidence": 0.4, "agreed": False, "ts": "2026-10-03T00:00:00+00:00"},
    {"pack": "gate_risk", "jev_confidence": 0.9, "agreed": True, "ts": "2026-10-04T00:00:00+00:00"},
]


class _FakeClient:
    def __init__(self, response=None, raises=False):
        self._response = response
        self._raises = raises
        self.calls = []

    def call(self, payload):
        self.calls.append(payload)
        if self._raises:
            raise RuntimeError("shadow transport exploded")
        return self._response, 12.5


def _read_lines(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_record_is_written_without_raw_state(tmp_path):
    path = tmp_path / "shadow.jsonl"
    logger = ShadowLogger(path)
    client = _FakeClient(_ROUTE_INTENT_RESPONSE)
    state = {"user_message": "TOPSECRETPROMPT do the thing", "cwd_basename": "repo"}

    run_shadow_pack("route_intent", state, "logic", client=client, model="jev-latest", logger=logger)

    records = _read_lines(path)
    assert len(records) == 1
    record = records[0]
    assert record["pack"] == "route_intent"
    assert record["caller_decision"] == "logic"
    assert record["status"] == "ok"
    assert record["agreed"] is True
    assert record["model"] == "jev-latest"
    assert record["latency_ms"] == 12.5
    assert len(record["state_hash"]) == 64
    raw_text = path.read_text()
    assert "TOPSECRETPROMPT" not in raw_text
    assert state_hash(state) == record["state_hash"]


def test_state_hash_is_stable_and_state_sensitive():
    assert state_hash({"a": 1, "b": 2}) == state_hash({"b": 2, "a": 1})
    assert state_hash({"a": 1}) != state_hash({"a": 2})


def test_pack_confidence_is_the_max_choice_confidence():
    decisions = {
        "tier": {"value": "scoped", "confidence": 0.87},
        "domain": {"value": "logic", "confidence": 0.91},
    }
    assert pack_confidence(decisions) == pytest.approx(0.91)
    assert pack_confidence({"irreversible": {"value": 0.95, "confident": True}}) == 0.0
    assert pack_confidence({}) == 0.0


def test_decisions_agree_matches_jev_verdict_value():
    decisions = {
        "tier": {"value": "scoped", "confidence": 0.87},
        "domain": {"value": "logic", "confidence": 0.91},
    }
    assert decisions_agree(decisions, "logic") is True
    assert decisions_agree(decisions, "LOGIC") is True
    assert decisions_agree(decisions, "scoped") is True
    assert decisions_agree(decisions, "explore") is False
    assert decisions_agree(decisions, "") is False


def test_run_shadow_writes_record_with_computed_agreement(tmp_path):
    path = tmp_path / "shadow.jsonl"
    client = _FakeClient(_ROUTE_INTENT_RESPONSE)
    with mock.patch("jev.shadow.JevClient", return_value=client):
        with mock.patch.dict("os.environ", {"TYPESAFE_AI_API_KEY": "test-key"}):
            run_shadow(
                packs=["route_intent"],
                state={"user_message": "fix the bug"},
                caller_decision="logic",
                path=path,
            )

    records = _read_lines(path)
    assert len(records) == 1
    assert records[0]["agreed"] is True
    assert records[0]["jev_confidence"] == pytest.approx(0.91)
    assert client.calls and client.calls[0]["model"] == "jev-latest"


def test_run_shadow_missing_key_logs_unavailable(tmp_path):
    path = tmp_path / "shadow.jsonl"
    with mock.patch.dict("os.environ", {}, clear=True):
        run_shadow(packs=["route_intent"], state={"user_message": "x"}, caller_decision="logic", path=path)

    records = _read_lines(path)
    assert len(records) == 1
    assert records[0]["status"] == "unavailable"
    assert records[0]["agreed"] is False


def test_run_shadow_swallows_client_errors(tmp_path):
    path = tmp_path / "shadow.jsonl"
    client = _FakeClient(raises=True)
    with mock.patch("jev.shadow.JevClient", return_value=client):
        with mock.patch.dict("os.environ", {"TYPESAFE_AI_API_KEY": "test-key"}):
            run_shadow(packs=["route_intent"], state={}, caller_decision="logic", path=path)
    assert not path.exists()


def test_metrics_math_on_fixed_fixture():
    assert agreement(_FIXTURE) == pytest.approx(0.5)
    assert brier(_FIXTURE) == pytest.approx(0.1425)
    assert ece(_FIXTURE) == pytest.approx(0.325)
    assert agreement([]) == 0.0
    assert brier([]) == 0.0
    assert ece([]) == 0.0


def test_summarize_and_report_shape():
    summary = summarize(_FIXTURE)
    assert summary["total"] == 4
    assert summary["per_pack"] == {"route_intent": 3, "gate_risk": 1}
    assert summary["agreement"] == pytest.approx(0.5)
    assert len(summary["disagreements"]) == 2

    report = format_report(_FIXTURE, limit=5)
    assert "records:    4" in report
    assert "route_intent=3" in report
    assert "agreement:  50.0%" in report
    assert "ECE:" in report and "Brier:" in report
    assert "recent disagreements" in report


@dataclasses.dataclass
class _FakeRequest:
    goal: str
    context: str | None
    role: str
    model: str | None
    allowed_toolsets: tuple
    metadata: dict


def _request_factory(*, goal, context, spec, model, toolsets):
    return _FakeRequest(
        goal=goal,
        context=context,
        role="orchestrator" if spec.orchestrator else "leaf",
        model=model,
        allowed_toolsets=toolsets,
        metadata={"omo_agent": spec.name},
    )


class _FakeHandle:
    def __init__(self, model):
        self.model = model


class _FakeLifecycle:
    def __init__(self):
        self.launches = []

    def launch(self, request):
        self.launches.append(request.model)
        return _FakeHandle(request.model)

    def wait(self, handle, *, timeout_seconds=None):
        return None

    def result(self, handle):
        return {"summary": "done", "model": handle.model}

    def cancel(self, handle, *, reason=""):
        return None


class _FakeCtx:
    def __init__(self, lifecycle, config):
        self.subagent_lifecycle = lifecycle
        self._config = config

    def get_config(self, key, default=None):
        return self._config.get(key, default)

    def emit(self, event, payload=None):
        return None

    def spawn_task(self, coro):
        return None


def test_dispatch_stays_fail_open_when_shadow_raises(tmp_path):
    lifecycle = _FakeLifecycle()
    ctx = _FakeCtx(
        lifecycle,
        {
            "jev_shadow_enabled": True,
            "jev_shadow_path": str(tmp_path / "shadow.jsonl"),
            "jev_shadow_packs": ["route_intent"],
        },
    )
    engine = OmoEngine(ctx, lifecycle=lifecycle, request_factory=_request_factory)
    engine.chains = ChainResolver({})

    called = threading.Event()

    class _RaisingClient:
        def __init__(self, **_kwargs):
            pass

        def call(self, _payload):
            called.set()
            raise RuntimeError("shadow transport exploded")

    with mock.patch("jev.shadow.JevClient", _RaisingClient):
        with mock.patch.dict("os.environ", {"TYPESAFE_AI_API_KEY": "test-key"}):
            out = engine.dispatch(goal="scan the repo", target="explore")
            assert called.wait(2.0), "shadow path was never invoked"

    assert out["status"] == "succeeded"
    assert lifecycle.launches == ["deepseek-v4-flash"]
