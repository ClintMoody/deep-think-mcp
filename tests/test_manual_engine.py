"""Unit tests for deep_think_mcp.manual_engine (Task 13 Half B).

No filesystem, no MCP, no network, and -- the load-bearing constraint -- no
vendored NECoRT code: the manual engine plays the specialists via the calling
model, so nothing in `necort_adapter`'s vendored loader may ever run. These
drive the engine directly over plain `Session` objects (the same convention
`test_subagent_engine.py` / `test_serial_engine.py` use), asserting the
deterministic selection arithmetic, the round state machine, and `loop_state`.
"""

from __future__ import annotations

import pytest

from deep_think_mcp import manual_engine, subagent_engine
from deep_think_mcp.manual_engine import ManualPrompt
from deep_think_mcp.session import Session
from deep_think_mcp.subagent_engine import SubagentRoundResult, SubagentSequencingError


def _session(stages=None):
    stages = stages or ["Problem Definition", "Research", "Analysis"]
    return Session(
        question="What is the best approach?",
        mode="subagent",
        expected_stages=stages,
        current_stage=stages[0],
    )


def _cfg(*, max_rounds=2, threshold=0.75, agents=None):
    return {
        "subagent": {
            "engine": "manual",
            "max_rounds": max_rounds,
            "equilibrium_threshold": threshold,
            "agents": ["Analysis", "Creativity"] if agents is None else agents,
        }
    }


def _scores(correctness, other=0.5):
    return {
        "correctness": correctness,
        "evidence": other,
        "novelty": other,
        "clarity": other,
        "bias_resistance": other,
        "actionability": other,
        "coverage": other,
    }


# ---------------------------------------------------------------------------
# begin hands specialist #1's prompt and creates the thought
# ---------------------------------------------------------------------------


def test_begin_hands_first_specialist_prompt():
    session = _session()
    cfg = _cfg()
    mp = manual_engine.begin(session, "seed content", None, cfg)

    assert isinstance(mp, ManualPrompt)
    assert mp.specialist_index == 0
    assert mp.specialist_name == "Analysis"
    assert mp.specialist_total == 2
    assert mp.us_round == 1
    assert mp.rounds_run == 0
    # the thought exists, in-progress, with NO specialist rounds recorded yet
    assert len(session.thoughts) == 1
    assert session.current_thought_id == session.thoughts[0].id
    assert session.thoughts[0].specialist_rounds == []
    # prompt_focus and the seed are threaded into the framing text
    assert "seed content" in mp.prompt_text
    assert "Analysis" in mp.prompt_text


def test_begin_rejects_when_thought_in_progress():
    session = _session()
    cfg = _cfg()
    manual_engine.begin(session, "seed", None, cfg)
    with pytest.raises(SubagentSequencingError) as exc:
        manual_engine.begin(session, "again", None, cfg)
    assert exc.value.code == "uncommitted_exists"


# ---------------------------------------------------------------------------
# advance walks the roster then runs deterministic selection
# ---------------------------------------------------------------------------


def test_advance_walks_roster_then_closes_round():
    session = _session()
    cfg = _cfg(agents=["Analysis", "Creativity", "Skeptic"])
    manual_engine.begin(session, "seed", None, cfg)

    # specialist 0 -> hands specialist 1
    r1 = manual_engine.advance(session, "cand-A", _scores(0.6), cfg)
    assert isinstance(r1, ManualPrompt)
    assert r1.specialist_index == 1 and r1.specialist_name == "Creativity"

    # specialist 1 -> hands specialist 2
    r2 = manual_engine.advance(session, "cand-C", _scores(0.9), cfg)
    assert isinstance(r2, ManualPrompt)
    assert r2.specialist_index == 2 and r2.specialist_name == "Skeptic"

    # specialist 2 -> roster exhausted -> round closes with a verdict
    verdict = manual_engine.advance(session, "cand-S", _scores(0.4), cfg)
    assert isinstance(verdict, SubagentRoundResult)
    assert verdict.rounds_run == 1
    # highest MEAN of the 7 dims wins -> Creativity (corr 0.9, rest 0.5) beats
    # Analysis (0.6) and Skeptic (0.4).
    assert verdict.selected_content == "cand-C"
    # The commit gate reads the SAME 7-dim MEAN selection uses (T13 fix round 1),
    # NOT correctness. Creativity's mean = (0.9 + 6*0.5)/7 ~= 0.557 < 0.75.
    assert verdict.strength == pytest.approx((0.9 + 6 * 0.5) / 7)
    assert verdict.converged is False
    assert verdict.metric_label == "mean utility"


def test_selection_prefers_higher_mean_over_higher_correctness():
    """F8: a case where mean-order != correctness-order. A spiky candidate
    (highest correctness, everything else low) must LOSE to a well-rounded
    candidate with lower correctness but a higher 7-dim mean. If selection
    ranked by correctness instead of the mean, 'spiky' would (wrongly) win."""
    session = _session()
    cfg = _cfg(agents=["Analysis", "Creativity"])
    manual_engine.begin(session, "seed", None, cfg)
    # spec 0: correctness 0.95, other six dims 0.1 -> mean 0.221 (corr winner)
    manual_engine.advance(session, "spiky", _scores(0.95, other=0.1), cfg)
    # spec 1: correctness 0.55, other six dims 0.95 -> mean 0.893 (mean winner)
    verdict = manual_engine.advance(session, "well_rounded", _scores(0.55, other=0.95), cfg)

    assert verdict.selected_content == "well_rounded"  # mean wins, not correctness
    assert verdict.strength == pytest.approx((0.55 + 6 * 0.95) / 7)
    thought = session.thoughts[0]
    assert subagent_engine.selected_round(thought).candidate_content == "well_rounded"


def test_selection_is_highest_mean_ties_go_to_first():
    session = _session()
    cfg = _cfg(agents=["Analysis", "Creativity"])
    manual_engine.begin(session, "seed", None, cfg)
    # Identical score vectors -> a tie; the FIRST (earliest) specialist wins.
    manual_engine.advance(session, "first", _scores(0.8), cfg)
    verdict = manual_engine.advance(session, "second", _scores(0.8), cfg)
    assert verdict.selected_content == "first"

    thought = session.thoughts[0]
    winners = [r for r in thought.specialist_rounds if r.was_selected]
    assert len(winners) == 1  # exactly-one-winner invariant (hardening #5)
    assert winners[0].candidate_content == "first"


def test_exactly_one_winner_marked_after_close():
    session = _session()
    cfg = _cfg(agents=["Analysis", "Creativity", "Skeptic"])
    manual_engine.begin(session, "seed", None, cfg)
    manual_engine.advance(session, "a", _scores(0.3), cfg)
    manual_engine.advance(session, "b", _scores(0.7), cfg)
    manual_engine.advance(session, "c", _scores(0.5), cfg)
    thought = session.thoughts[0]
    assert sum(1 for r in thought.specialist_rounds if r.was_selected) == 1
    # winner is 'b' (highest correctness -> highest mean here)
    assert subagent_engine.selected_round(thought).candidate_content == "b"


# ---------------------------------------------------------------------------
# mid-round with no candidate -> need_candidate; budget cap enforced
# ---------------------------------------------------------------------------


def test_advance_without_candidate_midround_raises_need_candidate():
    session = _session()
    cfg = _cfg(agents=["Analysis", "Creativity"])
    manual_engine.begin(session, "seed", None, cfg)
    manual_engine.advance(session, "cand-A", _scores(0.6), cfg)  # now mid-round
    with pytest.raises(SubagentSequencingError) as exc:
        manual_engine.advance(session, None, {}, cfg)
    assert exc.value.code == "need_candidate"


def test_second_round_via_no_candidate_advance_then_budget_cap():
    session = _session()
    cfg = _cfg(max_rounds=2, threshold=0.99, agents=["Analysis", "Creativity"])
    manual_engine.begin(session, "seed", None, cfg)
    manual_engine.advance(session, "a0", _scores(0.5), cfg)
    v0 = manual_engine.advance(session, "a1", _scores(0.5), cfg)  # closes round 0
    assert isinstance(v0, SubagentRoundResult)
    assert v0.converged is False and v0.budget_exhausted is False

    # no-candidate advance at the boundary starts round 1 (hands specialist 0)
    start = manual_engine.advance(session, None, {}, cfg)
    assert isinstance(start, ManualPrompt)
    assert start.us_round == 2 and start.specialist_index == 0

    manual_engine.advance(session, "b0", _scores(0.5), cfg)
    v1 = manual_engine.advance(session, "b1", _scores(0.5), cfg)  # closes round 1
    assert v1.rounds_run == 2
    assert v1.budget_exhausted is True  # max_rounds reached

    # a further no-candidate advance is refused: budget spent
    with pytest.raises(SubagentSequencingError) as exc:
        manual_engine.advance(session, None, {}, cfg)
    assert exc.value.code == "round_budget_exhausted"


# ---------------------------------------------------------------------------
# loop_state drives next_action
# ---------------------------------------------------------------------------


def test_loop_state_transitions():
    session = _session()
    cfg = _cfg(max_rounds=2, threshold=0.75, agents=["Analysis", "Creativity"])

    assert manual_engine.loop_state(session, cfg) == "no_thought"

    manual_engine.begin(session, "seed", None, cfg)
    assert manual_engine.loop_state(session, cfg) == "awaiting_specialist"

    manual_engine.advance(session, "a0", _scores(0.6), cfg)  # mid-round
    assert manual_engine.loop_state(session, cfg) == "awaiting_specialist"

    manual_engine.advance(session, "a1", _scores(0.6), cfg)  # closes round 0
    # winner corr 0.6 < 0.75, budget remains -> can_advance
    assert manual_engine.loop_state(session, cfg) == "can_advance"


def test_loop_state_converged_and_budget():
    session = _session()
    cfg = _cfg(max_rounds=1, threshold=0.75, agents=["Analysis", "Creativity"])
    manual_engine.begin(session, "seed", None, cfg)
    manual_engine.advance(session, "a0", _scores(0.9), cfg)
    manual_engine.advance(session, "a1", _scores(0.6), cfg)  # closes round 0
    # winner corr 0.9 >= 0.75 -> converged (and also budget_exhausted; converged
    # takes precedence only if below max -- here max_rounds=1 so both true, but
    # loop_state checks budget first).
    assert manual_engine.loop_state(session, cfg) == "budget_exhausted"


def test_no_specialists_configured_is_a_directive_not_a_crash():
    session = _session()
    cfg = _cfg(agents=[])
    with pytest.raises(subagent_engine.SubagentAdapterError):
        manual_engine.begin(session, "seed", None, cfg)


def _full(**dims):
    base = {d: 0.5 for d in (
        "correctness", "evidence", "novelty", "clarity",
        "bias_resistance", "actionability", "coverage",
    )}
    base.update(dims)
    return base


def test_lens_scaffolding_woven_into_manual_specialist_prompt():
    """F4: the manual specialist prompt also carries the stage's critique
    lenses (Analysis stage -> weak_evidence + overconfidence)."""
    session = _session(stages=["Analysis", "Synthesis"])
    cfg = _cfg(agents=["Analysis", "Creativity"])
    mp = manual_engine.begin(session, "seed", None, cfg)
    assert "weak_evidence" in mp.prompt_text
    assert "overconfidence" in mp.prompt_text


def test_manual_gate_uses_the_seven_dim_mean_not_correctness():
    """T13 fix round 1 adjudication: manual gates on the 7-dim MEAN, the same
    metric selection uses -- NOT correctness (that stays necort's gate)."""
    cfg = _cfg(max_rounds=2, threshold=0.75, agents=["Solo"])

    # (a) mean 0.8 but correctness only 0.6 -> CONVERGES in manual mode
    #     (would NOT converge under a correctness gate).
    s1 = _session()
    manual_engine.begin(s1, "seed", None, cfg)
    high_mean_low_corr = _full(
        correctness=0.6, evidence=0.85, novelty=0.85, clarity=0.85,
        bias_resistance=0.85, actionability=0.85, coverage=0.85,
    )  # mean = (0.6 + 6*0.85)/7 = 0.814
    v1 = manual_engine.advance(s1, "cand", high_mean_low_corr, cfg)
    assert v1.converged is True
    assert v1.strength == pytest.approx((0.6 + 6 * 0.85) / 7)

    # (b) the inverse -- correctness 0.9 but mean well below threshold -> does
    #     NOT converge (proves the gate is not reading correctness).
    s2 = _session()
    manual_engine.begin(s2, "seed", None, cfg)
    high_corr_low_mean = _full(correctness=0.9)  # rest 0.5 -> mean 0.557
    v2 = manual_engine.advance(s2, "cand", high_corr_low_mean, cfg)
    assert v2.converged is False
    assert v2.strength == pytest.approx((0.9 + 6 * 0.5) / 7)


def test_utility_from_scores_defaults_and_clamps():
    us = manual_engine._utility_from_scores({"correctness": 1.5, "Clarity": 0.7})
    assert us.correctness == 1.0  # clamped
    assert us.clarity == 0.7
    assert us.evidence == 0.5  # omitted -> neutral default
