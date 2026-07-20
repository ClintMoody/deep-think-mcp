"""MCP contract tests for the MANUAL specialist engine (Task 13 Half B).

Drive the real `mcp` SDK's in-memory client against `server.create_server()`
with `[subagent] engine="manual"` -- the endpoint-free path where the calling
model plays each specialist itself. The load-bearing assertion of this whole
suite: NO vendored NECoRT code is ever loaded. `necort_adapter._ensure_loaded`
(the one function that imports the vendored core) is monkeypatched to explode
if touched, so a full begin -> N specialist round-trips -> commit that
succeeds proves the manual path never reaches for the network or the
submodule.
"""

from __future__ import annotations

from typing import Any

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from deep_think_mcp import necort_adapter, server, store


@pytest.fixture(autouse=True)
def _forbid_vendored_load(monkeypatch):
    """Fail loudly if anything tries to load the vendored NECoRT core. The
    manual engine must never do so -- no endpoint, no network, no import."""

    def _boom(*args, **kwargs):
        raise AssertionError("manual engine must NOT load vendored NECoRT code")

    monkeypatch.setattr(necort_adapter, "_ensure_loaded", _boom)


async def _call(client, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    result = await client.call_tool(name, arguments or {})
    assert not result.isError, result.content
    assert result.structuredContent is not None
    return result.structuredContent


async def _start_manual(client, *, agents=None, max_rounds=2, threshold=0.75) -> str:
    sub: dict[str, Any] = {"engine": "manual", "max_rounds": max_rounds, "equilibrium_threshold": threshold}
    if agents is not None:
        sub["agents"] = agents
    payload = await _call(
        client,
        "start_session",
        {"question": "How should we design this?", "mode": "subagent", "overrides": {"subagent": sub}},
    )
    return payload["session_id"]


# ---------------------------------------------------------------------------
# Full loop: begin -> 2 specialist round-trips -> converged -> commit
# ---------------------------------------------------------------------------


async def test_full_manual_loop_begin_specialists_commit(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        sid = await _start_manual(client, agents=["Analysis", "Creativity"], threshold=0.75)

        begun = await _call(client, "begin_subagent_thought", {"session_id": sid, "content": "seed"})
        assert begun["engine"] == "manual"
        assert begun["specialist"] == "Analysis"
        assert begun["specialist_index"] == 0
        assert begun["specialist_total"] == 2
        assert begun["next_tool"] == "advance_subagent_round"
        assert "Analysis" in begun["specialist_prompt"]

        # specialist 0 (Analysis) submits -> hands specialist 1 (Creativity)
        s1 = await _call(
            client,
            "advance_subagent_round",
            {"session_id": sid, "candidate": "Analysis candidate", "scores": {"correctness": 0.9, "clarity": 0.8}},
        )
        assert s1["specialist"] == "Creativity"
        assert s1["specialist_index"] == 1

        # specialist 1 (Creativity) submits (weaker) -> roster done -> verdict
        verdict = await _call(
            client,
            "advance_subagent_round",
            {"session_id": sid, "candidate": "Creativity candidate", "scores": {"correctness": 0.5}},
        )
        assert verdict["rounds_run"] == 1
        assert verdict["converged"] is True  # Analysis winner corr 0.9 >= 0.75
        assert verdict["selected_content"] == "Analysis candidate"
        assert verdict["next_tool"] == "commit_subagent_thought"

        committed = await _call(client, "commit_subagent_thought", {"session_id": sid})
        assert committed["committed"] is True

    session = store.load(store.session_path(tmp_path, sid))
    t = session.thoughts[0]
    assert t.committed is True
    assert t.content == "Analysis candidate"  # deterministic winner
    assert sum(1 for r in t.specialist_rounds if r.was_selected) == 1
    assert session.current_thought_id is None
    # vendored core never touched -> the shim class cache was never populated
    # by THIS path (monkeypatch would have raised if it were).


# ---------------------------------------------------------------------------
# Mixed tolerant inputs: plaintext scores for one specialist, JSON for another
# ---------------------------------------------------------------------------


async def test_manual_loop_accepts_mixed_tolerant_score_inputs(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        sid = await _start_manual(client, agents=["Analysis", "Creativity"])

        await _call(client, "begin_subagent_thought", {"session_id": sid, "content": "seed"})
        # plaintext "k: v" scores
        s1 = await _call(
            client,
            "advance_subagent_round",
            {"session_id": sid, "candidate": "A", "scores": "correctness: 0.9, clarity: 0.8"},
        )
        assert s1["specialist_index"] == 1
        # fenced-JSON-in-prose scores
        verdict = await _call(
            client,
            "advance_subagent_round",
            {"session_id": sid, "candidate": "C", "scores": "```json\n{\"correctness\": 0.4}\n```"},
        )
        assert verdict["converged"] is True
        assert verdict["selected_content"] == "A"

    session = store.load(store.session_path(tmp_path, sid))
    rounds = session.thoughts[0].specialist_rounds
    assert rounds[0].utility_vector.correctness == pytest.approx(0.9)
    assert rounds[1].utility_vector.correctness == pytest.approx(0.4)


# ---------------------------------------------------------------------------
# Multi-round: below threshold -> can_advance -> start round 2 -> budget commit
# ---------------------------------------------------------------------------


async def test_manual_loop_multi_round_until_budget(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        sid = await _start_manual(client, agents=["Analysis", "Creativity"], max_rounds=2, threshold=0.99)

        await _call(client, "begin_subagent_thought", {"session_id": sid, "content": "seed"})
        await _call(client, "advance_subagent_round", {"session_id": sid, "candidate": "a0", "scores": {"correctness": 0.5}})
        v0 = await _call(client, "advance_subagent_round", {"session_id": sid, "candidate": "a1", "scores": {"correctness": 0.6}})
        assert v0["converged"] is False
        assert v0["budget_exhausted"] is False
        assert v0["next_tool"] == "advance_subagent_round"

        # start round 2 with NO candidate -> hands specialist 0's prompt again
        start2 = await _call(client, "advance_subagent_round", {"session_id": sid})
        assert start2["us_round"] == 2
        assert start2["specialist_index"] == 0

        await _call(client, "advance_subagent_round", {"session_id": sid, "candidate": "b0", "scores": {"correctness": 0.5}})
        v1 = await _call(client, "advance_subagent_round", {"session_id": sid, "candidate": "b1", "scores": {"correctness": 0.7}})
        assert v1["rounds_run"] == 2
        assert v1["budget_exhausted"] is True
        assert v1["next_tool"] == "commit_subagent_thought"

        committed = await _call(client, "commit_subagent_thought", {"session_id": sid})
        assert committed["committed"] is True


# ---------------------------------------------------------------------------
# next_action drives the manual loop (the manual-mode truth-table rows)
# ---------------------------------------------------------------------------


async def test_next_action_manual_rows(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        sid = await _start_manual(client, agents=["Analysis", "Creativity"], threshold=0.75)

        na = await _call(client, "next_action", {"session_id": sid})
        assert na["next_tool"] == "begin_subagent_thought"

        await _call(client, "begin_subagent_thought", {"session_id": sid, "content": "seed"})
        na = await _call(client, "next_action", {"session_id": sid})
        assert na["code"] == "subagent_awaiting_specialist"
        assert na["next_tool"] == "advance_subagent_round"

        await _call(client, "advance_subagent_round", {"session_id": sid, "candidate": "A", "scores": {"correctness": 0.9}})
        na = await _call(client, "next_action", {"session_id": sid})
        # mid-round: still owes the 2nd specialist's candidate
        assert na["code"] == "subagent_awaiting_specialist"

        await _call(client, "advance_subagent_round", {"session_id": sid, "candidate": "C", "scores": {"correctness": 0.5}})
        na = await _call(client, "next_action", {"session_id": sid})
        assert na["code"] == "subagent_converged"
        assert na["next_tool"] == "commit_subagent_thought"


# ---------------------------------------------------------------------------
# need_candidate directive: mid-round advance with no candidate
# ---------------------------------------------------------------------------


async def test_manual_midround_no_candidate_returns_directive(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        sid = await _start_manual(client, agents=["Analysis", "Creativity"])
        await _call(client, "begin_subagent_thought", {"session_id": sid, "content": "seed"})
        await _call(client, "advance_subagent_round", {"session_id": sid, "candidate": "A", "scores": {"correctness": 0.9}})
        # now mid-round; a bare advance owes the pending specialist a candidate
        directive = await _call(client, "advance_subagent_round", {"session_id": sid})
        assert directive["error"] == "sequencing"
        assert directive["code"] == "need_candidate"
        assert directive["next_tool"] == "advance_subagent_round"


# ---------------------------------------------------------------------------
# no-endpoint necort directive names engine="manual" as the alternative
# (Half B: verify the wording is accurate post-implementation)
# ---------------------------------------------------------------------------


async def test_necort_no_endpoint_points_at_manual(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        payload = await _call(
            client,
            "start_session",
            {"question": "q", "mode": "subagent", "overrides": {"subagent": {"engine": "necort", "endpoint": ""}}},
        )
        sid = payload["session_id"]
        directive = await _call(client, "begin_subagent_thought", {"session_id": sid, "content": "seed"})

    assert directive["error"] == "no_endpoint"
    assert 'engine="manual"' in directive["message"]
