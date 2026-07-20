"""Unit tests for deep_think_mcp.meta's pure logic: next_action(), the
extractive summarize_session()/compress_history() digests, and the
export/import parsing helpers.

No filesystem, no MCP -- Session/Thought/CritiqueRound objects and a plain
cfg dict, same convention `test_serial_engine.py` uses. The MCP-level
round trips (real tool calls, persistence, id-collision-safe import) live
in `test_meta_tools.py`.
"""

from __future__ import annotations

import pytest

from deep_think_mcp import meta, serial_engine
from deep_think_mcp.session import CritiqueRound, Session, Thought, UtilityScore


def _cfg(max_rounds=3, score_threshold=0.05, epsilon=0.05):
    return {
        "serial": {
            "max_rounds": max_rounds,
            "score_threshold": score_threshold,
            "edit_distance_epsilon": epsilon,
        }
    }


def _session(mode="serial", stages=None, current_stage=None, status="active"):
    stages = stages or ["Problem Definition", "Research", "Analysis"]
    return Session(
        question="q",
        mode=mode,
        expected_stages=stages,
        current_stage=current_stage or stages[0],
        status=status,
    )


def _score(**overrides):
    base = dict(
        correctness=0.5,
        evidence=0.5,
        novelty=0.5,
        clarity=0.5,
        bias_resistance=0.5,
        actionability=0.5,
        coverage=0.5,
    )
    base.update(overrides)
    return UtilityScore(**base)


def _committed_thought(stage, position, content, *, scores=None, tags=None):
    t = Thought(
        stage=stage,
        position=position,
        content=content,
        committed=True,
        tags=list(tags or []),
        final_utility_scores=scores,
    )
    return t


# ---------------------------------------------------------------------------
# next_action(): truth table across states x modes -- the test that matters
# most per the brief. Each row is built via the real serial_engine calls
# (not hand-rolled fixtures) so the loop-phase state actually matches what
# the engine would produce.
# ---------------------------------------------------------------------------


def test_next_action_no_mode_directs_to_set_session_mode():
    session = _session(mode=None)
    result = meta.next_action(session, _cfg())
    assert result.code == "mode_required"
    assert result.next_tool == "set_session_mode"


def test_next_action_subagent_mode_reports_engine_not_available():
    session = _session(mode="subagent")
    result = meta.next_action(session, _cfg())
    assert result.code == "subagent_not_available"
    assert result.next_tool is None


def test_next_action_subagent_mode_not_available_even_when_finalized():
    """Coarse-but-truthful judgment call (per the brief): today, ANY
    subagent-mode session reports 'not available', regardless of status --
    there's no engine yet to have produced any other state. T11 revisits
    this once the subagent engine exists.
    """
    session = _session(mode="subagent", status="finalized")
    result = meta.next_action(session, _cfg())
    assert result.code == "subagent_not_available"


def test_next_action_serial_no_thought_not_final_stage_directs_begin_thought():
    session = _session()
    result = meta.next_action(session, _cfg())
    assert result.code == "loop_no_thought_begin"
    assert result.next_tool == "begin_thought"
    assert result.detail["alternative_tool"] == "advance_stage"


def test_next_action_serial_no_thought_final_stage_directs_finalize():
    session = _session(current_stage="Analysis")  # last of the 3 stages
    result = meta.next_action(session, _cfg())
    assert result.code == "loop_no_thought_final_stage"
    assert result.next_tool == "finalize_session"


def test_next_action_serial_zero_rounds_directs_critique():
    session = _session()
    serial_engine.begin_thought(session, "a draft")
    result = meta.next_action(session, _cfg())
    assert result.code == "loop_zero_rounds"
    assert result.next_tool == "critique_current_thought"


def test_next_action_serial_await_critique_directs_submit_critique():
    session = _session()
    serial_engine.begin_thought(session, "a draft")
    serial_engine.start_critique(session, "weak_evidence", {"weak_evidence": "tmpl"}, _cfg())
    result = meta.next_action(session, _cfg())
    assert result.code == "loop_await_critique"
    assert result.next_tool == "submit_critique"


def test_next_action_serial_await_refine_directs_refine():
    session = _session()
    serial_engine.begin_thought(session, "a draft")
    serial_engine.start_critique(session, "weak_evidence", {"weak_evidence": "tmpl"}, _cfg())
    serial_engine.submit_critique(session, "the evidence is thin")
    result = meta.next_action(session, _cfg())
    assert result.code == "loop_await_refine"
    assert result.next_tool == "refine_current_thought"


def test_next_action_serial_await_score_directs_score():
    session = _session()
    serial_engine.begin_thought(session, "a draft")
    serial_engine.start_critique(session, "weak_evidence", {"weak_evidence": "tmpl"}, _cfg())
    serial_engine.submit_critique(session, "the evidence is thin")
    serial_engine.refine_current_thought(session, "a refined draft")
    result = meta.next_action(session, _cfg())
    assert result.code == "loop_await_score"
    assert result.next_tool == "score_current_thought"


def test_next_action_serial_round_complete_converged_directs_commit():
    cfg = _cfg(max_rounds=1)  # single round hits the ceiling -> converged
    session = _session()
    serial_engine.begin_thought(session, "a draft")
    serial_engine.start_critique(session, "weak_evidence", {"weak_evidence": "tmpl"}, cfg)
    serial_engine.submit_critique(session, "the evidence is thin")
    serial_engine.refine_current_thought(session, "a substantially different refined draft")
    serial_engine.score_current_thought(session, {"correctness": 0.9}, cfg)

    result = meta.next_action(session, cfg)
    assert result.code == "loop_converged"
    assert result.next_tool == "commit_thought"
    assert result.detail["converged_reason"] == "max_rounds"


def test_next_action_serial_round_complete_not_converged_directs_another_critique():
    cfg = _cfg(max_rounds=5, score_threshold=0.05)
    session = _session()
    serial_engine.begin_thought(session, "a draft")
    serial_engine.start_critique(session, "weak_evidence", {"weak_evidence": "tmpl"}, cfg)
    serial_engine.submit_critique(session, "the evidence is thin")
    serial_engine.refine_current_thought(session, "a substantially different refined draft here")
    serial_engine.score_current_thought(session, {"correctness": 0.95}, cfg)

    result = meta.next_action(session, cfg)
    assert result.code == "loop_continue"
    assert result.next_tool == "critique_current_thought"


def test_next_action_never_suggests_advance_stage_while_thought_uncommitted():
    """T7 review flag: advance_stage while a thought is uncommitted orphans
    it. next_action must route back into the loop, never toward
    advance_stage/finalize_session, whenever a thought is in progress --
    even at the final stage.
    """
    session = _session(current_stage="Analysis")  # final stage
    serial_engine.begin_thought(session, "a draft")
    result = meta.next_action(session, _cfg())
    assert result.next_tool not in {"advance_stage", "finalize_session"}
    assert result.code == "loop_zero_rounds"


def test_next_action_finalized_undecided_directs_move_or_keep():
    session = _session(status="finalized")
    result = meta.next_action(session, _cfg())
    assert result.code == "await_move_decision"
    assert result.next_tool == "move_session"
    assert result.detail["alternative_tool"] == "keep_here"


def test_next_action_finalized_kept_reports_session_complete():
    from deep_think_mcp.session import DecisionRecord

    session = _session(status="finalized")
    session.decisions.append(DecisionRecord(action="keep_here"))
    result = meta.next_action(session, _cfg())
    assert result.code == "session_complete"
    assert result.next_tool is None


def test_next_action_finalized_and_moved_reports_session_complete():
    from deep_think_mcp.session import MoveRecord

    session = _session(status="finalized")
    session.move_history.append(MoveRecord(from_path="/a", to_path="/b"))
    result = meta.next_action(session, _cfg())
    assert result.code == "session_complete"
    assert result.next_tool is None


def test_next_action_archived_reports_no_further_action():
    session = _session(status="archived")
    result = meta.next_action(session, _cfg())
    assert result.code == "session_archived"
    assert result.next_tool is None


# ---------------------------------------------------------------------------
# summarize_session()
# ---------------------------------------------------------------------------


def test_summarize_session_stage_scope_only_current_stage():
    session = _session(current_stage="Research")
    session.thoughts = [
        _committed_thought("Problem Definition", 0, "first stage thought"),
        _committed_thought("Research", 0, "research thought one"),
        _committed_thought("Research", 1, "research thought two"),
    ]
    result = meta.summarize_session(session, scope="stage")
    assert result.thought_count == 2
    assert result.stages_covered == ["Research"]
    assert all(e.stage == "Research" for e in result.entries)


def test_summarize_session_all_scope_covers_every_stage_in_order():
    session = _session(current_stage="Research")
    session.thoughts = [
        _committed_thought("Research", 0, "research thought"),
        _committed_thought("Problem Definition", 0, "pd thought"),
    ]
    result = meta.summarize_session(session, scope="all")
    assert result.thought_count == 2
    # stage order follows expected_stages, not insertion order
    assert result.stages_covered == ["Problem Definition", "Research"]
    assert [e.stage for e in result.entries] == ["Problem Definition", "Research"]


def test_summarize_session_excludes_uncommitted_thought():
    session = _session()
    serial_engine.begin_thought(session, "still drafting, not committed")
    result = meta.summarize_session(session, scope="stage")
    assert result.thought_count == 0
    assert result.entries == []


def test_summarize_session_entry_includes_overall_score_when_scored():
    session = _session()
    session.thoughts = [
        _committed_thought(
            "Problem Definition", 0, "a scored thought", scores=_score(correctness=1.0)
        )
    ]
    result = meta.summarize_session(session, scope="stage")
    assert result.entries[0].overall_score is not None
    assert 0.0 <= result.entries[0].overall_score <= 1.0


def test_summarize_session_entry_score_none_when_unscored():
    session = _session()
    session.thoughts = [_committed_thought("Problem Definition", 0, "unscored thought")]
    result = meta.summarize_session(session, scope="stage")
    assert result.entries[0].overall_score is None


# ---------------------------------------------------------------------------
# compress_history(): digest length bounds
# ---------------------------------------------------------------------------


def test_compress_history_empty_when_no_prior_stages():
    session = _session(current_stage="Problem Definition")  # first stage -> nothing prior
    session.thoughts = [_committed_thought("Problem Definition", 0, "current stage thought")]
    result = meta.compress_history(session)
    assert result.digest == ""
    assert result.estimated_tokens == 0
    assert result.included_thought_ids == []


def test_compress_history_excludes_current_stage():
    session = _session(current_stage="Analysis")
    session.thoughts = [
        _committed_thought("Problem Definition", 0, "prior stage thought"),
        _committed_thought("Analysis", 0, "current stage thought, must not appear"),
    ]
    result = meta.compress_history(session, target_tokens=1000)
    assert "current stage" not in result.digest
    assert "prior stage" in result.digest


def test_compress_history_never_exceeds_target_tokens():
    long_content = "word " * 500  # long enough to blow any small budget
    session = _session(current_stage="Analysis")
    session.thoughts = [
        _committed_thought("Problem Definition", i, long_content) for i in range(6)
    ] + [
        _committed_thought("Research", i, long_content) for i in range(6)
    ]
    for target in (10, 50, 200, 300, 1000):
        result = meta.compress_history(session, target_tokens=target)
        assert result.estimated_tokens <= target, target


def test_compress_history_default_target_lands_in_the_documented_sweet_spot():
    """docs/build-plan.md: 'compress_history returns a 200-400 token
    digest of prior stages'. With abundant prior-stage content and the
    default target_tokens, the digest should land in that documented range.
    """
    long_content = "substantial reasoning content that takes up real space " * 6
    session = _session(current_stage="Analysis")
    session.thoughts = [
        _committed_thought("Problem Definition", i, long_content) for i in range(4)
    ] + [
        _committed_thought("Research", i, long_content) for i in range(4)
    ]
    result = meta.compress_history(session)  # default target_tokens
    assert 200 <= result.estimated_tokens <= 400
    assert result.estimated_tokens <= result.target_tokens


def test_compress_history_omits_oldest_when_over_budget():
    long_content = "word " * 80
    session = _session(current_stage="Analysis")
    session.thoughts = [
        _committed_thought("Problem Definition", i, long_content) for i in range(10)
    ]
    result = meta.compress_history(session, target_tokens=60)
    assert result.omitted_count > 0
    assert len(result.included_thought_ids) < 10


def test_compress_history_single_huge_thought_still_respects_cap():
    huge = "word " * 5000
    session = _session(current_stage="Analysis")
    session.thoughts = [_committed_thought("Problem Definition", 0, huge)]
    result = meta.compress_history(session, target_tokens=50)
    assert result.estimated_tokens <= 50
    assert result.included_thought_ids  # still included, just clipped


def test_estimate_tokens_is_cheap_length_heuristic():
    assert meta.estimate_tokens("") == 0
    assert meta.estimate_tokens("abcd") == 1
    assert meta.estimate_tokens("a" * 400) == 100


# ---------------------------------------------------------------------------
# export_session() / parse_import()
# ---------------------------------------------------------------------------


def test_export_session_is_json_serializable_dict():
    import json

    session = _session()
    session.thoughts = [_committed_thought("Problem Definition", 0, "a thought")]
    data = meta.export_session(session)
    assert data["id"] == session.id
    assert data["question"] == "q"
    # must round-trip through real JSON (datetimes etc. all serializable)
    json.dumps(data)


def test_parse_import_accepts_dict():
    session = _session()
    data = meta.export_session(session)
    imported = meta.parse_import(data)
    assert imported.id == session.id
    assert imported.question == session.question


def test_parse_import_accepts_json_string():
    import json

    session = _session()
    raw = json.dumps(meta.export_session(session))
    imported = meta.parse_import(raw)
    assert imported.id == session.id


def test_parse_import_rejects_malformed_json_string():
    with pytest.raises(meta.ImportValidationError) as exc_info:
        meta.parse_import("{not valid json")
    assert exc_info.value.code == "invalid_json"


def test_parse_import_rejects_non_object_json():
    with pytest.raises(meta.ImportValidationError) as exc_info:
        meta.parse_import("[1, 2, 3]")
    assert exc_info.value.code == "invalid_session_data"


def test_parse_import_rejects_schema_violation():
    with pytest.raises(meta.ImportValidationError) as exc_info:
        meta.parse_import({"question": "q", "mode": "not-a-real-mode"})
    assert exc_info.value.code == "invalid_session_data"


def test_parse_import_roundtrips_full_session_including_thoughts():
    session = _session()
    session.thoughts = [
        _committed_thought(
            "Problem Definition", 0, "a thought", scores=_score(), tags=["core"]
        )
    ]
    session.thoughts[0].critique_rounds = [
        CritiqueRound(
            round_index=0,
            lens="weak_evidence",
            critique_text="crit",
            refined_content="refined",
            delta_score=0.1,
        )
    ]
    data = meta.export_session(session)
    imported = meta.parse_import(data)
    assert imported == session
