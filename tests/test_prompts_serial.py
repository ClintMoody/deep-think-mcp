"""Unit tests for the Task 7 serial-loop payload shapes in prompts.py.

Transport-independent: these pin the payload *shape* directly on the prompts
functions, so the adjacency contract and directive routing are guaranteed at
the source, not only as observed through the MCP client (which the loop test
also checks end to end).
"""

from __future__ import annotations

from deep_think_mcp import prompts
from deep_think_mcp.serial_engine import CritiquePrompt, ScoreResult


def test_critique_ready_places_draft_immediately_before_template():
    prompt = CritiquePrompt(
        thought_id="tid",
        lens="weak_evidence",
        round_index=0,
        draft_content="the draft under critique",
        lens_template="You are critiquing the draft thought above ...",
    )
    payload = prompts.critique_ready("sid", prompt)

    keys = list(payload.keys())
    # ADJACENCY: nothing may sit between the draft and the template that
    # opens with the positional "the draft thought above" claim.
    assert keys.index("draft_content") + 1 == keys.index("lens_template")
    assert payload["draft_content"] == "the draft under critique"
    assert payload["lens_template"].startswith("You are critiquing the draft thought above")
    assert payload["next_tool"] == "submit_critique"


def test_thought_scored_directs_to_commit_when_converged():
    result = ScoreResult(
        thought_id="tid",
        round_index=2,
        scores={"correctness": 0.9},
        overall=0.9,
        delta=0.01,
        converged=True,
        converged_reason="diminishing_returns",
    )
    payload = prompts.thought_scored("sid", result)
    assert payload["converged"] is True
    assert payload["converged_reason"] == "diminishing_returns"
    assert payload["next_tool"] == "commit_thought"


def test_thought_scored_directs_to_next_lens_when_not_converged():
    result = ScoreResult(
        thought_id="tid",
        round_index=0,
        scores={"correctness": 0.5},
        overall=0.5,
        delta=0.5,
        converged=False,
        converged_reason=None,
    )
    payload = prompts.thought_scored("sid", result)
    assert payload["converged"] is False
    assert payload["next_tool"] == "critique_current_thought"


def test_serial_directive_routes_known_code():
    payload = prompts.serial_directive("sid", "zero_rounds")
    assert payload["error"] == "sequencing"
    assert payload["code"] == "zero_rounds"
    assert payload["next_tool"] == "critique_current_thought"


def test_serial_directive_unknown_lens_includes_available_list():
    payload = prompts.serial_directive("sid", "unknown_lens", lenses=["a", "b"])
    assert payload["available_lenses"] == ["a", "b"]


def test_serial_directive_unknown_code_degrades_to_next_action_not_error():
    payload = prompts.serial_directive("sid", "some_future_code")
    assert payload["next_tool"] == "next_action"
    assert payload["code"] == "some_future_code"
