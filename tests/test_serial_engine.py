"""Unit tests for deep_think_mcp.serial_engine's pure convergence logic.

These exercise the four convergence rules from `docs/build-plan.md` § "The
serial loop" *in isolation* (the brief's test list: "each convergence rule
in isolation"), plus the normalized edit-distance helper they rely on. No
filesystem, no MCP -- just Thought objects and a plain cfg dict, so each
rule can be pinned down without the rest of the loop in the way.
"""

from __future__ import annotations

import pytest

from deep_think_mcp import serial_engine
from deep_think_mcp.session import CritiqueRound, Thought


def _cfg(max_rounds=3, score_threshold=0.05, epsilon=0.05):
    return {
        "serial": {
            "max_rounds": max_rounds,
            "score_threshold": score_threshold,
            "edit_distance_epsilon": epsilon,
        }
    }


def _complete_round(index, *, refined, delta, lens="weak_evidence"):
    """A fully-scored (complete) CritiqueRound."""
    return CritiqueRound(
        round_index=index,
        lens=lens,
        critique_text="a critique",
        refined_content=refined,
        delta_score=delta,
    )


# ---------------------------------------------------------------------------
# normalized_edit_distance
# ---------------------------------------------------------------------------


def test_normalized_edit_distance_identical_is_zero():
    assert serial_engine.normalized_edit_distance("same text", "same text") == 0.0


def test_normalized_edit_distance_both_empty_is_zero():
    assert serial_engine.normalized_edit_distance("", "") == 0.0


def test_normalized_edit_distance_grows_with_change():
    small = serial_engine.normalized_edit_distance("abcdefghij", "abcdefghiJ")
    large = serial_engine.normalized_edit_distance("abcdefghij", "zyxwvutsrq")
    assert 0.0 < small < large <= 1.0


# ---------------------------------------------------------------------------
# Rule 1: score improved >= threshold -> continue (not converged)
# ---------------------------------------------------------------------------


def test_rule_improved_score_does_not_converge():
    thought = Thought(stage="Analysis", position=0, content="the original draft text")
    thought.critique_rounds = [
        _complete_round(0, refined="a substantially rewritten first version", delta=0.30),
        _complete_round(1, refined="an even more different second rewrite entirely", delta=0.20),
    ]
    converged, reason = serial_engine.evaluate_convergence(thought, _cfg(max_rounds=5))
    assert converged is False
    assert reason is None


# ---------------------------------------------------------------------------
# Rule 2: two consecutive flat/dropped rounds -> converged, commit
# ---------------------------------------------------------------------------


def test_rule_two_flat_rounds_converges_diminishing_returns():
    thought = Thought(stage="Analysis", position=0, content="the original draft text")
    thought.critique_rounds = [
        _complete_round(0, refined="a substantially rewritten first version", delta=0.40),
        _complete_round(1, refined="second version changed quite a lot again here", delta=0.01),
        _complete_round(2, refined="third version also changed a great deal more now", delta=0.00),
    ]
    # max_rounds high so ONLY the two-flat rule can be responsible.
    converged, reason = serial_engine.evaluate_convergence(thought, _cfg(max_rounds=9))
    assert converged is True
    assert reason == "diminishing_returns"


def test_rule_single_flat_round_does_not_converge():
    thought = Thought(stage="Analysis", position=0, content="the original draft text")
    thought.critique_rounds = [
        _complete_round(0, refined="a substantially rewritten first version", delta=0.40),
        _complete_round(1, refined="second version changed quite a lot again here", delta=0.01),
    ]
    converged, reason = serial_engine.evaluate_convergence(thought, _cfg(max_rounds=9))
    assert converged is False
    assert reason is None


# ---------------------------------------------------------------------------
# Rule 3: content fixed point (edit distance < epsilon) -> converged, commit
# ---------------------------------------------------------------------------


def test_rule_fixed_point_converges_even_with_improving_score():
    draft = "The answer is 42 because of the following careful reasoning."
    thought = Thought(stage="Analysis", position=0, content=draft)
    thought.critique_rounds = [
        # refined content is essentially the draft (one char) -> fixed point,
        # even though the model reported a large score improvement.
        _complete_round(0, refined=draft + "!", delta=0.50),
    ]
    converged, reason = serial_engine.evaluate_convergence(thought, _cfg(max_rounds=9))
    assert converged is True
    assert reason == "fixed_point"


# ---------------------------------------------------------------------------
# Rule 4: rounds >= max_rounds -> converged and flagged
# ---------------------------------------------------------------------------


def test_rule_max_rounds_converges_and_flags():
    thought = Thought(stage="Analysis", position=0, content="the original draft text")
    thought.critique_rounds = [
        _complete_round(0, refined="a substantially rewritten first version", delta=0.30),
        _complete_round(1, refined="second version changed quite a lot again here", delta=0.20),
        _complete_round(2, refined="third version also changed a great deal more now", delta=0.15),
    ]
    # All rounds improving + big edits -> neither fixed-point nor diminishing
    # can fire; hitting the cap is the only reason left.
    converged, reason = serial_engine.evaluate_convergence(thought, _cfg(max_rounds=3))
    assert converged is True
    assert reason == "max_rounds"


def test_natural_convergence_outranks_max_rounds_reason():
    """When a natural rule and the cap coincide, the informative natural
    reason should win -- max_rounds is the 'we'd have kept going' fallback.
    """
    thought = Thought(stage="Analysis", position=0, content="the original draft text")
    thought.critique_rounds = [
        _complete_round(0, refined="a substantially rewritten first version", delta=0.40),
        _complete_round(1, refined="second version changed quite a lot again here", delta=0.00),
        _complete_round(2, refined="third version also changed a great deal more now", delta=0.00),
    ]
    converged, reason = serial_engine.evaluate_convergence(thought, _cfg(max_rounds=3))
    assert converged is True
    assert reason == "diminishing_returns"
