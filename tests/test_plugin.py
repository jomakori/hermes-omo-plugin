import pytest

import roster
from orchestrator.chains import FallbackState, canonical_model, is_retryable
from orchestrator.guards import READ_ONLY_WORKERS, GuardError, check_delegation, read_only_pre_tool_call

EXPECTED = {
    "sisyphus",
    "hephaestus",
    "prometheus",
    "atlas",
    "metis",
    "momus",
    "oracle",
    "librarian",
    "explore",
    "multimodal-looker",
    "sisyphus-junior",
}


def test_roster_is_complete_and_unique():
    assert set(roster.AGENTS) == EXPECTED
    assert len(roster.AGENTS) == 11


def test_every_agent_has_a_model_chain():
    for name, spec in roster.AGENTS.items():
        assert spec.chain, f"{name} has no model chain"
        assert all(model.startswith("litellm/") for model in spec.chain), name


def test_read_only_tier_matches_omo():
    read_only = {name for name, spec in roster.AGENTS.items() if spec.read_only}
    assert read_only == {"oracle", "librarian", "explore", "metis", "momus", "prometheus", "multimodal-looker"}


def test_only_orchestrator_tier_gets_mcp():
    with_mcp = {name for name, spec in roster.AGENTS.items() if spec.mcp}
    assert with_mcp == {"sisyphus", "hephaestus", "prometheus", "atlas"}


def test_sisyphus_junior_is_category_only():
    assert roster.AGENTS["sisyphus-junior"].accepts_subagent_type is False


def test_categories_exist():
    assert set(roster.CATEGORIES) == {"quick", "deep", "ultrabrain", "visual-engineering", "writing"}


def test_retryable_classification():
    assert is_retryable(status=429)
    assert is_retryable(status=503)
    assert is_retryable(message="Rate limit exceeded")
    assert is_retryable(message="provider overloaded")
    assert not is_retryable(error_type="abort")
    assert not is_retryable(error_type="context_overflow")
    assert not is_retryable(status=200, message="fine")


def test_canonicalisation_collapses_variants():
    assert canonical_model("litellm/claude-opus-5-max") == canonical_model("litellm/claude-opus-5")
    assert canonical_model("litellm/deepseek-v4-pro-thinking") == "deepseek-v4-pro"


def test_fallback_walks_chain_then_stops():
    state = FallbackState(chain=("a", "b", "c"), max_attempts=3, cooldown_seconds=0)
    assert state.next_model() == "b"
    assert state.next_model() == "c"
    assert state.next_model() is None


def test_fallback_respects_cooldown():
    state = FallbackState(chain=("a", "b"), max_attempts=3, cooldown_seconds=60)
    state.record_failure("b", now=0)
    assert state.next_model(now=1) is None
    assert state.next_model(now=1000) == "b"


def test_guard_requires_exactly_one_target():
    with pytest.raises(GuardError):
        check_delegation(target=None, category=None)
    with pytest.raises(GuardError):
        check_delegation(target="explore", category="quick")


def test_guard_rejects_invalid_targets():
    with pytest.raises(GuardError):
        check_delegation(target="sisyphus-junior", category=None)
    with pytest.raises(GuardError):
        check_delegation(target="nope", category=None)


def test_guard_allows_every_roster_worker_as_a_target():
    for name in ("sisyphus", "hephaestus", "prometheus", "atlas", "metis", "momus", "oracle", "librarian", "explore"):
        assert check_delegation(target=name, category=None) == name


def test_guard_blocks_plan_family_recursion():
    with pytest.raises(GuardError):
        check_delegation(target="prometheus", category=None, parent_agent="plan")
    with pytest.raises(GuardError):
        check_delegation(target="prometheus", category=None, parent_agent="prometheus")
    assert check_delegation(target="metis", category=None, parent_agent="plan") == "metis"


def test_guard_allows_research_tier():
    assert check_delegation(target="explore", category=None) == "explore"
    assert check_delegation(target="oracle", category=None) == "oracle"


def test_guard_category_maps_to_junior():
    assert check_delegation(target=None, category="quick") == "sisyphus-junior"


def test_read_only_guard_blocks_writes_only_for_marked_sessions():
    READ_ONLY_WORKERS.mark("sa-1", "oracle")
    assert read_only_pre_tool_call(tool_name="write_file", session_id="sa-1")["action"] == "block"
    assert read_only_pre_tool_call(tool_name="patch", session_id="sa-1")["action"] == "block"
    assert read_only_pre_tool_call(tool_name="read_file", session_id="sa-1") is None
    assert read_only_pre_tool_call(tool_name="write_file", session_id="other") is None
    READ_ONLY_WORKERS.clear("sa-1")
    assert read_only_pre_tool_call(tool_name="write_file", session_id="sa-1") is None
