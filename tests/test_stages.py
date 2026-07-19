"""Tests for deep_think_mcp.stages: Layer 3, the stage machine shared by
both future engines (Tasks 7, 11) -- cursor-advance logic plus the two
stage-appropriate data lookup tables those tasks consume.

`stages.advance()` is a pure in-memory mutation, same persistence pattern
as `lifecycle.finalize()`/`lifecycle.keep_here()`: the caller (server.py)
does `store.save` + `index.upsert` afterward. Persistence itself is only
exercised here as a roundtrip sanity check ("cursor integrity across
persistence" from the task brief's test list) -- the MCP-level version of
that same guarantee (advance via the real tool, then resume_session) lives
in test_server.py alongside the tool's mode-gate/directive-payload tests.
"""

from __future__ import annotations

import pytest

from deep_think_mcp import stages, store
from deep_think_mcp.session import Session

DEFAULT_STAGES = ["Problem Definition", "Research", "Analysis", "Synthesis", "Conclusion"]


def _make_session(expected_stages: list[str], current_stage: str | None = None) -> Session:
    return Session(
        question="q",
        expected_stages=expected_stages,
        current_stage=current_stage or expected_stages[0],
    )


# ---------------------------------------------------------------------------
# advance(): default stage progression
# ---------------------------------------------------------------------------


def test_advance_moves_through_default_stages_in_order():
    session = _make_session(DEFAULT_STAGES)
    for expected_next in DEFAULT_STAGES[1:]:
        stages.advance(session)
        assert session.current_stage == expected_next


def test_advance_returns_the_same_session():
    session = _make_session(DEFAULT_STAGES)
    result = stages.advance(session)
    assert result is session


def test_advance_clears_current_thought_id():
    session = _make_session(DEFAULT_STAGES)
    session.current_thought_id = "some-thought-id"
    stages.advance(session)
    assert session.current_thought_id is None


# ---------------------------------------------------------------------------
# advance(): custom stage progression
# ---------------------------------------------------------------------------


def test_advance_moves_through_custom_stages_in_order():
    session = _make_session(["Alpha", "Beta", "Gamma"])
    stages.advance(session)
    assert session.current_stage == "Beta"
    stages.advance(session)
    assert session.current_stage == "Gamma"


def test_advance_on_a_single_custom_stage_is_immediately_final():
    session = _make_session(["OnlyStage"])
    assert stages.is_final_stage(session)
    with pytest.raises(stages.FinalStageReachedError):
        stages.advance(session)


# ---------------------------------------------------------------------------
# advance() / is_final_stage(): end-of-stages behavior
# ---------------------------------------------------------------------------


def test_advance_raises_at_final_stage():
    session = _make_session(DEFAULT_STAGES, current_stage="Conclusion")
    with pytest.raises(stages.FinalStageReachedError) as exc_info:
        stages.advance(session)
    assert exc_info.value.stage == "Conclusion"


def test_advance_does_not_mutate_session_when_raising():
    session = _make_session(DEFAULT_STAGES, current_stage="Conclusion")
    session.current_thought_id = "keep-me"
    with pytest.raises(stages.FinalStageReachedError):
        stages.advance(session)
    assert session.current_stage == "Conclusion"
    assert session.current_thought_id == "keep-me"


def test_is_final_stage_true_only_on_last_stage():
    session = _make_session(DEFAULT_STAGES)
    assert not stages.is_final_stage(session)
    session.current_stage = "Conclusion"
    assert stages.is_final_stage(session)


# ---------------------------------------------------------------------------
# advance(): cursor integrity across persistence
# ---------------------------------------------------------------------------


def test_advanced_cursor_survives_a_store_roundtrip(tmp_path):
    session = _make_session(DEFAULT_STAGES)
    session.save_path = str(store.session_path(tmp_path, session.id))
    stages.advance(session)
    stages.advance(session)
    store.save(session, session.save_path)

    reloaded = store.load(session.save_path)
    assert reloaded.current_stage == "Analysis"
    assert reloaded.expected_stages == DEFAULT_STAGES


# ---------------------------------------------------------------------------
# lens_defaults_for_stage()
# ---------------------------------------------------------------------------


def test_lens_defaults_for_default_stages_match_the_brief():
    assert stages.lens_defaults_for_stage("Problem Definition") == [
        "unstated_assumption",
        "scope_creep",
    ]
    assert stages.lens_defaults_for_stage("Research") == [
        "weak_evidence",
        "missing_perspective",
    ]
    assert stages.lens_defaults_for_stage("Analysis") == [
        "weak_evidence",
        "overconfidence",
    ]
    assert stages.lens_defaults_for_stage("Synthesis") == [
        "missing_perspective",
        "unstated_assumption",
    ]
    assert stages.lens_defaults_for_stage("Conclusion") == [
        "steel_man",
        "overconfidence",
    ]


def test_lens_defaults_for_unknown_stage_is_empty():
    assert stages.lens_defaults_for_stage("Some Custom Stage") == []


def test_lens_defaults_for_stage_returns_a_copy_not_the_internal_list():
    result = stages.lens_defaults_for_stage("Analysis")
    result.append("mutated")
    assert stages.lens_defaults_for_stage("Analysis") == ["weak_evidence", "overconfidence"]


# ---------------------------------------------------------------------------
# agent_weight_for_stage()
# ---------------------------------------------------------------------------


def test_analysis_agent_weighted_higher_in_analysis_stage():
    weight = stages.agent_weight_for_stage("Analysis", "Analysis")
    assert weight == stages.EMPHASIZED_AGENT_WEIGHT
    assert weight > stages.NEUTRAL_AGENT_WEIGHT


def test_creativity_agent_weighted_higher_in_synthesis_stage():
    weight = stages.agent_weight_for_stage("Synthesis", "Creativity")
    assert weight == stages.EMPHASIZED_AGENT_WEIGHT
    assert weight > stages.NEUTRAL_AGENT_WEIGHT


def test_agent_weights_are_neutral_elsewhere():
    assert stages.agent_weight_for_stage("Research", "Analysis") == stages.NEUTRAL_AGENT_WEIGHT
    assert stages.agent_weight_for_stage("Research", "Creativity") == stages.NEUTRAL_AGENT_WEIGHT
    # cross combinations within the emphasized stages stay neutral too --
    # only the plan-mandated (stage, agent) pairs get the emphasis.
    assert stages.agent_weight_for_stage("Analysis", "Creativity") == stages.NEUTRAL_AGENT_WEIGHT
    assert stages.agent_weight_for_stage("Synthesis", "Analysis") == stages.NEUTRAL_AGENT_WEIGHT


def test_agent_weight_for_unknown_stage_or_agent_is_neutral():
    assert stages.agent_weight_for_stage("Custom Stage", "Analysis") == stages.NEUTRAL_AGENT_WEIGHT
    assert stages.agent_weight_for_stage("Analysis", "SomeOtherAgent") == stages.NEUTRAL_AGENT_WEIGHT
