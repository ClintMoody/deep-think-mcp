"""Tests for deep_think_mcp.session: the Pydantic data model.

Pure model tests -- no filesystem, no config. Covers construction defaults,
validation constraints, and roundtrips (model -> JSON -> model) for both
execution modes, per the brief's test list ("model roundtrips").
"""

import pytest
from pydantic import ValidationError

from deep_think_mcp.session import (
    CritiqueRound,
    DecisionRecord,
    MoveRecord,
    Session,
    SpecialistRound,
    Thought,
    UtilityScore,
)


def _utility_score(**overrides):
    base = dict(
        correctness=0.9,
        evidence=0.8,
        novelty=0.7,
        clarity=0.6,
        bias_resistance=0.5,
        actionability=0.4,
        coverage=0.3,
    )
    base.update(overrides)
    return UtilityScore(**base)


# ---------------------------------------------------------------------------
# Session defaults
# ---------------------------------------------------------------------------


def test_session_id_defaults_to_uuid4_hex():
    session = Session(
        question="q", expected_stages=["Research"], current_stage="Research"
    )
    assert len(session.id) == 32
    int(session.id, 16)  # raises ValueError if not valid hex


def test_session_ids_are_unique():
    make = lambda: Session(
        question="q", expected_stages=["Research"], current_stage="Research"
    )
    assert make().id != make().id


def test_session_defaults():
    session = Session(
        question="What is the meaning of life?",
        expected_stages=["Problem Definition", "Research"],
        current_stage="Problem Definition",
    )
    assert session.mode is None
    assert session.status == "active"
    assert session.current_thought_id is None
    assert session.save_path == ""
    assert session.overrides == {}
    assert session.move_history == []
    assert session.thoughts == []
    assert session.decisions == []
    assert session.created_at is not None


# ---------------------------------------------------------------------------
# Session validation
# ---------------------------------------------------------------------------


def test_session_rejects_invalid_mode():
    with pytest.raises(ValidationError):
        Session(
            question="q",
            expected_stages=["Research"],
            current_stage="Research",
            mode="parallel",
        )


def test_session_rejects_invalid_status():
    with pytest.raises(ValidationError):
        Session(
            question="q",
            expected_stages=["Research"],
            current_stage="Research",
            status="deleted",
        )


def test_session_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        Session(
            question="q",
            expected_stages=["Research"],
            current_stage="Research",
            bogus_field="nope",
        )


def test_session_requires_question():
    with pytest.raises(ValidationError):
        Session(expected_stages=["Research"], current_stage="Research")


# ---------------------------------------------------------------------------
# Thought defaults
# ---------------------------------------------------------------------------


def test_thought_defaults():
    thought = Thought(stage="Research", position=0, content="draft content")
    assert len(thought.id) == 32
    assert thought.tags == []
    assert thought.axioms == []
    assert thought.challenged_assumptions == []
    assert thought.critique_rounds == []
    assert thought.specialist_rounds == []
    assert thought.final_utility_scores is None
    assert thought.committed is False


# ---------------------------------------------------------------------------
# UtilityScore bounds
# ---------------------------------------------------------------------------


def test_utility_score_accepts_boundary_values():
    score = _utility_score(correctness=0.0, evidence=1.0)
    assert score.correctness == 0.0
    assert score.evidence == 1.0


@pytest.mark.parametrize("bad_value", [-0.01, 1.01, -1, 2])
def test_utility_score_rejects_out_of_range_values(bad_value):
    with pytest.raises(ValidationError):
        _utility_score(correctness=bad_value)


def test_utility_score_requires_all_seven_dimensions():
    with pytest.raises(ValidationError):
        UtilityScore(correctness=0.5)


# ---------------------------------------------------------------------------
# Roundtrips: model -> JSON -> model
# ---------------------------------------------------------------------------


def test_session_roundtrip_serial_mode():
    thought = Thought(
        stage="Analysis",
        position=0,
        content="first draft",
        tags=["core"],
        axioms=["axiom 1"],
        challenged_assumptions=["assumption A"],
        critique_rounds=[
            CritiqueRound(
                round_index=0,
                lens="steel_man",
                critique_text="too weak",
                refined_content="stronger draft",
                delta_score=0.12,
            )
        ],
        final_utility_scores=_utility_score(),
        committed=True,
    )
    session = Session(
        question="Should we ship v1 now?",
        mode="serial",
        expected_stages=["Problem Definition", "Analysis"],
        current_stage="Analysis",
        current_thought_id=thought.id,
        save_path="/tmp/does-not-matter/session.json",
        overrides={"serial": {"max_rounds": 1}},
        move_history=[
            MoveRecord(from_path="/old/path.json", to_path="/new/path.json")
        ],
        thoughts=[thought],
        decisions=[DecisionRecord(action="keep_here")],
    )

    restored = Session.model_validate_json(session.model_dump_json())

    assert restored == session


def test_session_roundtrip_subagent_mode():
    thought = Thought(
        stage="Synthesis",
        position=1,
        content="candidate synthesis",
        specialist_rounds=[
            SpecialistRound(
                round_index=0,
                agent_role="Analysis",
                candidate_content="candidate A",
                utility_vector=_utility_score(),
                equilibrium_state="converged",
                was_selected=True,
            )
        ],
        final_utility_scores=_utility_score(coverage=0.99),
    )
    session = Session(
        question="What's our subagent equilibrium?",
        mode="subagent",
        expected_stages=["Synthesis", "Conclusion"],
        current_stage="Synthesis",
        thoughts=[thought],
    )

    restored = Session.model_validate_json(session.model_dump_json())

    assert restored == session


def test_session_roundtrip_awaiting_mode():
    """mode=None means 'awaiting mode selection' -- must roundtrip cleanly."""
    session = Session(
        question="q",
        expected_stages=["Problem Definition"],
        current_stage="Problem Definition",
    )
    restored = Session.model_validate_json(session.model_dump_json())
    assert restored == session
    assert restored.mode is None
