"""Tests for OMR-12: health-aware candidate filtering in Jev routing and packs."""

from __future__ import annotations

import unittest.mock as mock

from jev.packs import agent_candidates
from jev.routing import route_agent

_VALID = {"sisyphus", "prometheus", "explore", "quick"}


def _route(response=None, raises=False):
    client = mock.MagicMock()
    if raises:
        client.call.side_effect = RuntimeError("boom")
    else:
        client.call.return_value = (response, 5.0)
    return client


class TestRouteAgentUnavailable:
    """route_agent must drop unavailable candidates before scoring."""

    def test_unavailable_candidate_is_dropped_from_valid_targets(self):
        """A candidate in unavailable must never appear in valid_targets passed to authoritative_target."""
        response = {"model": "jev-latest", "answers": {"agent": {"choice": "prometheus", "confidence": 0.9}}}
        with mock.patch("jev.routing.JevClient", return_value=_route(response)):
            with mock.patch.dict("os.environ", {"TYPESAFE_AI_API_KEY": "k"}):
                target, _confidence, status = route_agent(
                    state={"user_message": "plan"},
                    valid_targets=_VALID,
                    candidates={name: name for name in _VALID},
                    unavailable={"prometheus"},
                    threshold=0.75,
                    base_url="https://api.typesafe.ai",
                    model="jev-latest",
                    timeout_s=5.0,
                )
        # prometheus was marked unavailable, so it must not be the pick even though Jev scored it 0.9.
        assert target is None
        assert status == "ok"

    def test_unavailable_candidate_dropped_from_criteria(self):
        """A candidate in unavailable must never appear in Jev's criteria dict."""
        with mock.patch("jev.routing.JevClient") as mock_client_class:
            client = mock.MagicMock()
            mock_client_class.return_value = client
            client.call.return_value = (
                {"model": "jev-latest", "answers": {"agent": {"choice": "explore", "confidence": 0.9}}},
                5.0,
            )
            with mock.patch.dict("os.environ", {"TYPESAFE_AI_API_KEY": "k"}):
                route_agent(
                    state={"user_message": "scan"},
                    valid_targets=_VALID,
                    candidates={
                        "sisyphus": "sisyphus",
                        "prometheus": "prometheus",
                        "explore": "explore",
                        "quick": "quick",
                    },
                    unavailable={"sisyphus", "prometheus"},
                    threshold=0.75,
                    base_url="https://api.typesafe.ai",
                    model="jev-latest",
                    timeout_s=5.0,
                )
            # Check the payload Jev received; unavailable candidates must not be in criteria.
            call_args = client.call.call_args
            assert call_args is not None
            payload = call_args[0][0]
            criteria = payload["questions"]["agent"].get("criteria", {})
            assert "sisyphus" not in criteria
            assert "prometheus" not in criteria
            assert "explore" in criteria
            assert "quick" in criteria

    def test_all_unavailable_returns_unavailable_without_network_call(self):
        """When all candidates are unavailable, return (None, 0.0, 'unavailable') without calling Jev."""
        with mock.patch("jev.routing.JevClient") as mock_client_class:
            client = mock.MagicMock()
            mock_client_class.return_value = client
            with mock.patch.dict("os.environ", {"TYPESAFE_AI_API_KEY": "k"}):
                target, confidence, status = route_agent(
                    state={"user_message": "anything"},
                    valid_targets=_VALID,
                    candidates={name: name for name in _VALID},
                    unavailable=_VALID,  # all candidates are dead
                    threshold=0.75,
                    base_url="https://api.typesafe.ai",
                    model="jev-latest",
                    timeout_s=5.0,
                )
            # No network call made, fail-open with unavailable status.
            assert target is None
            assert confidence == 0.0
            assert status == "unavailable"
            client.call.assert_not_called()

    def test_empty_unavailable_has_no_effect(self):
        """Empty unavailable set must not filter anything."""
        response = {"model": "jev-latest", "answers": {"agent": {"choice": "prometheus", "confidence": 0.9}}}
        with mock.patch("jev.routing.JevClient", return_value=_route(response)):
            with mock.patch.dict("os.environ", {"TYPESAFE_AI_API_KEY": "k"}):
                target, _confidence, status = route_agent(
                    state={"user_message": "plan"},
                    valid_targets=_VALID,
                    candidates={name: name for name in _VALID},
                    unavailable=set(),  # empty
                    threshold=0.75,
                    base_url="https://api.typesafe.ai",
                    model="jev-latest",
                    timeout_s=5.0,
                )
        assert target == "prometheus"
        assert status == "ok"

    def test_none_unavailable_has_no_effect(self):
        """None unavailable set must not filter anything."""
        response = {"model": "jev-latest", "answers": {"agent": {"choice": "prometheus", "confidence": 0.9}}}
        with mock.patch("jev.routing.JevClient", return_value=_route(response)):
            with mock.patch.dict("os.environ", {"TYPESAFE_AI_API_KEY": "k"}):
                target, _confidence, status = route_agent(
                    state={"user_message": "plan"},
                    valid_targets=_VALID,
                    candidates={name: name for name in _VALID},
                    unavailable=None,  # default
                    threshold=0.75,
                    base_url="https://api.typesafe.ai",
                    model="jev-latest",
                    timeout_s=5.0,
                )
        assert target == "prometheus"
        assert status == "ok"


class TestAgentCandidatesUnavailable:
    """agent_candidates must drop unavailable candidates and apply capability hints."""

    def test_unavailable_dropped_from_agents(self):
        """Unavailable agents must not appear in the returned map."""
        candidates = agent_candidates(unavailable={"prometheus", "explore"})
        assert "prometheus" not in candidates
        assert "explore" not in candidates
        assert "sisyphus" in candidates
        assert "quick" in candidates  # category

    def test_unavailable_dropped_from_categories(self):
        """Unavailable categories must not appear in the returned map."""
        candidates = agent_candidates(unavailable={"quick", "deep"})
        assert "quick" not in candidates
        assert "deep" not in candidates
        assert "sisyphus" in candidates
        assert "ultrabrain" in candidates

    def test_capability_hints_replace_bare_display(self):
        """Default descriptions must contain the capability hint, not bare 'Name · Role'."""
        candidates = agent_candidates()
        # sisyphus should have a hint appended.
        sisyphus_desc = candidates.get("sisyphus", "")
        assert "primary ultraworker" in sisyphus_desc
        assert " · " in sisyphus_desc  # the original display is preserved

    def test_per_task_descriptions_override_hints(self):
        """Caller-supplied descriptions must override capability hints."""
        custom = {"prometheus": "specialized plan agent for migrations only"}
        candidates = agent_candidates(descriptions=custom)
        assert candidates["prometheus"] == custom["prometheus"]

    def test_per_task_descriptions_override_for_unavailable_too(self):
        """Per-task descriptions are checked before unavailable filtering."""
        custom = {"explore": "custom explore desc"}
        candidates = agent_candidates(descriptions=custom, unavailable={"prometheus"})
        assert "prometheus" not in candidates
        assert candidates.get("explore") == custom["explore"]

    def test_enabled_agents_filters_before_unavailable(self):
        """enabled set filters agents before unavailable filtering is applied."""
        candidates = agent_candidates(enabled={"sisyphus", "prometheus"}, unavailable={"prometheus"})
        assert "sisyphus" in candidates
        assert "prometheus" not in candidates
        assert "explore" not in candidates

    def test_category_descriptions_get_hints(self):
        """Categories must also receive capability hints as default descriptions."""
        candidates = agent_candidates()
        deep_desc = candidates.get("deep", "")
        assert "deep-reasoning model chain" in deep_desc
        assert "deep" in deep_desc  # the original name is preserved

    def test_none_unavailable_has_no_effect(self):
        """None unavailable set must not filter anything."""
        candidates = agent_candidates(unavailable=None)
        assert "sisyphus" in candidates
        assert "explore" in candidates
        assert "quick" in candidates

    def test_empty_unavailable_has_no_effect(self):
        """Empty unavailable set must not filter anything."""
        candidates = agent_candidates(unavailable=set())
        assert "sisyphus" in candidates
        assert "explore" in candidates
        assert "quick" in candidates


__all__ = ["TestRouteAgentUnavailable", "TestAgentCandidatesUnavailable"]
