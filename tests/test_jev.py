from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import unittest.mock as mock
import urllib.error

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from jev.envelope import build_envelope, unavailable_envelope
from jev.packs import PACKS
from jev.policy import apply_policy
from jev.redact import redact_state
from omo_tools.jev_tool import JEV_SCHEMA, make_jev_handler

_ROUTE_INTENT_RESPONSE = {
    "model": "jev-latest",
    "answers": {
        "tier": {"choice": "scoped", "confidence": 0.87, "probabilities": {"scoped": 0.87, "quick": 0.1}},
        "domain": {"choice": "logic", "confidence": 0.91, "probabilities": {"logic": 0.91}},
    },
    "usage": {"input_tokens": 120, "output_tokens": 45},
}

_GATE_RISK_RESPONSE = {
    "model": "jev-latest",
    "answers": {
        "irreversible": {"noul": 0.95},
        "external_side_effect": {"noul": 0.2},
        "destructive": {"noul": 0.93},
        "secrets_involved": {"noul": 0.1},
    },
    "usage": {"input_tokens": 80, "output_tokens": 30},
}


def _mock_urlopen(response_dict: dict, status: int = 200):
    raw = json.dumps(response_dict).encode()

    cm = mock.MagicMock()
    cm.__enter__ = mock.Mock(return_value=cm)
    cm.__exit__ = mock.Mock(return_value=False)
    cm.read.return_value = raw
    cm.status = status
    return mock.patch("urllib.request.urlopen", return_value=cm)


def _make_handler(*, enabled: bool = True, api_key: str = "test-key"):
    return make_jev_handler(
        base_url="https://api.typesafe.ai",
        model="jev-latest",
        timeout_s=5.0,
        api_key_env="TYPESAFE_AI_API_KEY",
        threshold_overrides=None,
        enabled=enabled,
    ), api_key


def test_success_shape_route_intent():
    handler, key = _make_handler()
    with _mock_urlopen(_ROUTE_INTENT_RESPONSE):
        with mock.patch.dict(os.environ, {"TYPESAFE_AI_API_KEY": key}):
            result = asyncio.run(handler({"pack": "route_intent", "state": {"user_message": "fix the bug"}}))
    envelope = json.loads(result)
    assert envelope["status"] == "ok"
    assert envelope["pack"] == "route_intent"
    assert envelope["pack_version"] == 1
    assert envelope["backend"] == "typesafe"
    assert envelope["model"] == "jev-latest"
    assert "decisions" in envelope
    assert "tier" in envelope["decisions"]
    assert "domain" in envelope["decisions"]
    assert envelope["decisions"]["tier"]["value"] == "scoped"
    assert envelope["decisions"]["tier"]["confident"] is True
    assert envelope["decisions"]["domain"]["value"] == "logic"
    assert "usage" in envelope
    assert envelope["latency_ms"] >= 0


def test_timeout_returns_unavailable_with_defaults():
    handler, key = _make_handler()
    with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
        with mock.patch.dict(os.environ, {"TYPESAFE_AI_API_KEY": key}):
            result = asyncio.run(handler({"pack": "route_intent", "state": {"user_message": "x"}}))
    envelope = json.loads(result)
    assert envelope["status"] == "unavailable"
    pack = PACKS["route_intent"]
    for q in pack.questions:
        assert envelope["decisions"][q.id]["source"] == "default"
        assert envelope["decisions"][q.id]["value"] == pack.defaults[q.id]


def test_missing_api_key_returns_unavailable():
    handler, _ = _make_handler()
    env = {k: v for k, v in os.environ.items() if k != "TYPESAFE_AI_API_KEY"}
    with mock.patch.dict(os.environ, env, clear=True):
        result = asyncio.run(handler({"pack": "gate_risk", "state": {"action_text": "rm -rf /"}}))
    envelope = json.loads(result)
    assert envelope["status"] == "unavailable"
    assert "TYPESAFE_AI_API_KEY" in envelope["notes"]


def test_disabled_returns_unavailable():
    handler, key = _make_handler(enabled=False)
    with mock.patch.dict(os.environ, {"TYPESAFE_AI_API_KEY": key}):
        result = asyncio.run(handler({"pack": "route_intent", "state": {}}))
    envelope = json.loads(result)
    assert envelope["status"] == "unavailable"
    assert "false" in envelope["notes"].lower()


def test_redaction_planted_bearer_absent_from_request_body():
    captured_bodies: list[bytes] = []

    original_request = __import__("urllib.request", fromlist=["Request"]).Request

    def capturing_request(url, data=None, headers=None, method=None):
        if data:
            captured_bodies.append(data)
        return original_request(url, data=data, headers=headers, method=method)

    with mock.patch("urllib.request.Request", side_effect=capturing_request):
        with _mock_urlopen(_ROUTE_INTENT_RESPONSE):
            with mock.patch.dict(os.environ, {"TYPESAFE_AI_API_KEY": "legit-key"}):
                state_with_secret = {
                    "user_message": "check this Bearer abc123secret and do stuff",
                    "cwd_basename": "myrepo",
                }
                handler, _ = _make_handler()
                asyncio.run(handler({"pack": "route_intent", "state": state_with_secret}))

    assert captured_bodies, "no request body captured"
    body_text = captured_bodies[0].decode()
    assert "abc123secret" not in body_text
    assert "Bearer abc123secret" not in body_text
    assert "[REDACTED]" in body_text


def test_redaction_api_key_never_in_state():
    captured_bodies: list[bytes] = []

    original_request = __import__("urllib.request", fromlist=["Request"]).Request

    def capturing_request(url, data=None, headers=None, method=None):
        if data:
            captured_bodies.append(data)
        return original_request(url, data=data, headers=headers, method=method)

    api_key = "sk-supersecretsupersecretkey123456789"
    with mock.patch("urllib.request.Request", side_effect=capturing_request):
        with _mock_urlopen(_ROUTE_INTENT_RESPONSE):
            with mock.patch.dict(os.environ, {"TYPESAFE_AI_API_KEY": api_key}):
                state_with_key = {
                    "user_message": f"use api key {api_key} for auth",
                }
                handler, _ = _make_handler()
                asyncio.run(handler({"pack": "route_intent", "state": state_with_key}))

    assert captured_bodies
    body_text = captured_bodies[0].decode()
    assert api_key not in body_text


def test_threshold_policy_gate_risk_high_noul():
    pack = PACKS["gate_risk"]
    answers = {
        "irreversible": {"noul": 0.95},
        "external_side_effect": {"noul": 0.2},
        "destructive": {"noul": 0.93},
        "secrets_involved": {"noul": 0.1},
    }
    decisions = apply_policy("gate_risk", answers, pack.questions)
    assert decisions["irreversible"]["confident"] is True
    assert decisions["external_side_effect"]["confident"] is False
    assert decisions["destructive"]["confident"] is True
    assert decisions["secrets_involved"]["confident"] is False


def test_threshold_policy_gate_risk_borderline():
    pack = PACKS["gate_risk"]
    answers = {
        "irreversible": {"noul": 0.89},
        "external_side_effect": {"noul": 0.75},
        "destructive": {"noul": 0.89},
        "secrets_involved": {"noul": 0.49},
    }
    decisions = apply_policy("gate_risk", answers, pack.questions)
    assert decisions["irreversible"]["confident"] is False
    assert decisions["external_side_effect"]["confident"] is True
    assert decisions["destructive"]["confident"] is False
    assert decisions["secrets_involved"]["confident"] is False


def test_retry_once_on_429():
    call_count = 0
    raw_ok = json.dumps(_ROUTE_INTENT_RESPONSE).encode()

    def urlopen_side_effect(req, timeout=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            err = urllib.error.HTTPError(
                url="https://api.typesafe.ai/v1/systemone",
                code=429,
                msg="Too Many Requests",
                hdrs=None,
                fp=None,
            )
            err.read = lambda: b"{}"
            raise err
        cm = mock.MagicMock()
        cm.__enter__ = mock.Mock(return_value=cm)
        cm.__exit__ = mock.Mock(return_value=False)
        cm.read.return_value = raw_ok
        cm.status = 200
        return cm

    handler, key = _make_handler()
    with mock.patch("urllib.request.urlopen", side_effect=urlopen_side_effect):
        with mock.patch.dict(os.environ, {"TYPESAFE_AI_API_KEY": key}):
            result = asyncio.run(handler({"pack": "route_intent", "state": {"user_message": "test"}}))

    assert call_count == 2
    envelope = json.loads(result)
    assert envelope["status"] == "ok"


def test_retry_once_on_429_still_fails_returns_unavailable():
    raw_429 = json.dumps({"error": "rate limited"}).encode()

    def urlopen_side_effect(req, timeout=None):
        err = urllib.error.HTTPError(
            url="https://api.typesafe.ai/v1/systemone",
            code=429,
            msg="Too Many Requests",
            hdrs=None,
            fp=None,
        )
        err.read = lambda: raw_429
        raise err

    handler, key = _make_handler()
    with mock.patch("urllib.request.urlopen", side_effect=urlopen_side_effect):
        with mock.patch.dict(os.environ, {"TYPESAFE_AI_API_KEY": key}):
            result = asyncio.run(handler({"pack": "route_intent", "state": {"user_message": "test"}}))

    envelope = json.loads(result)
    assert envelope["status"] == "unavailable"


def test_unknown_pack_returns_unavailable():
    handler, key = _make_handler()
    with mock.patch.dict(os.environ, {"TYPESAFE_AI_API_KEY": key}):
        result = asyncio.run(handler({"pack": "nonexistent_pack", "state": {}}))
    envelope = json.loads(result)
    assert envelope["status"] == "unavailable"
    assert "nonexistent_pack" in envelope["notes"]


def test_envelope_shape_always_valid_json_on_error():
    handler, key = _make_handler()
    with mock.patch("urllib.request.urlopen", side_effect=Exception("boom")):
        with mock.patch.dict(os.environ, {"TYPESAFE_AI_API_KEY": key}):
            result = asyncio.run(handler({"pack": "gate_risk", "state": {}}))
    envelope = json.loads(result)
    assert "status" in envelope
    assert "decisions" in envelope
    assert "latency_ms" in envelope
    assert "backend" in envelope
    assert envelope["backend"] == "typesafe"


def test_jev_schema_shape():
    assert JEV_SCHEMA["name"] == "jev_ask"
    assert "description" in JEV_SCHEMA
    params = JEV_SCHEMA["parameters"]
    assert params["type"] == "object"
    assert "pack" in params["properties"]
    assert "state" in params["properties"]
    assert params["required"] == ["pack", "state"]
    assert set(params["properties"]["pack"]["enum"]) == {"route_intent", "gate_risk", "pick_skill", "pick_agent"}


def test_redact_state_allowlist_filters_unknown_keys():
    state = {
        "user_message": "hello",
        "secret_token": "Bearer supertoken999",
        "cwd_basename": "myrepo",
    }
    result = redact_state("route_intent", state)
    assert "supertoken999" not in result
    assert "secret_token" not in result
    assert "hello" in result
    assert "myrepo" in result


def test_gate_risk_response_shape():
    handler, key = _make_handler()
    with _mock_urlopen(_GATE_RISK_RESPONSE):
        with mock.patch.dict(os.environ, {"TYPESAFE_AI_API_KEY": key}):
            result = asyncio.run(
                handler({"pack": "gate_risk", "state": {"action_text": "kubectl delete namespace prod"}})
            )
    envelope = json.loads(result)
    assert envelope["status"] == "ok"
    assert "irreversible" in envelope["decisions"]
    assert "destructive" in envelope["decisions"]
    assert envelope["decisions"]["irreversible"]["value"] == 0.95
    assert envelope["decisions"]["irreversible"]["confident"] is True
    assert envelope["decisions"]["external_side_effect"]["confident"] is False


def test_cost_usd_computed_from_usage():
    env = build_envelope(
        pack="route_intent",
        pack_version=1,
        model="jev-latest",
        status="ok",
        decisions={},
        usage={"input_tokens": 100, "output_tokens": 50},
        latency_ms=190.0,
    )
    d = json.loads(env)
    assert d["cost_usd"] is not None
    assert d["cost_usd"] > 0


def test_unavailable_envelope_has_null_cost():
    env = unavailable_envelope(
        pack="route_intent",
        pack_version=1,
        model="jev-latest",
        decisions={},
        latency_ms=0.0,
    )
    d = json.loads(env)
    assert d["status"] == "unavailable"
    assert d["cost_usd"] is None
