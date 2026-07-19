"""MCP contract tests for the serial engine (Task 7).

Drive the real `mcp` SDK's in-memory client against `server.create_server()`
-- same no-mocks pattern as tests/test_server.py -- covering the brief's
required round-trips:

  - the full begin -> critique -> submit -> refine -> score -> commit loop;
  - the adjacency contract (draft content immediately precedes the lens
    template in the critique payload);
  - mode-gate rejection (a subagent-mode session calling a serial tool);
  - the sequencing directives that make the loop usable by weak local
    models (out-of-order calls return the exact right next tool, never an
    error).
"""

from __future__ import annotations

from typing import Any

from mcp.shared.memory import create_connected_server_and_client_session

from deep_think_mcp import server, store


async def _call(client, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    result = await client.call_tool(name, arguments or {})
    assert not result.isError, result.content
    assert result.structuredContent is not None
    return result.structuredContent


async def _start_serial(client, **kwargs) -> str:
    payload = await _call(
        client, "start_session", {"question": "q", "mode": "serial", **kwargs}
    )
    return payload["session_id"]


# ---------------------------------------------------------------------------
# begin_thought
# ---------------------------------------------------------------------------


async def test_begin_thought_creates_uncommitted_thought_and_sets_cursor(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        sid = await _start_serial(client)
        payload = await _call(
            client,
            "begin_thought",
            {"session_id": sid, "content": "my first draft", "tags": ["core"]},
        )

    assert payload["next_tool"] == "critique_current_thought"
    assert payload["position"] == 0
    assert payload["stage"] == "Problem Definition"

    session = store.load(store.session_path(tmp_path, sid))
    assert len(session.thoughts) == 1
    assert session.thoughts[0].content == "my first draft"
    assert session.thoughts[0].committed is False
    assert session.thoughts[0].tags == ["core"]
    assert session.current_thought_id == session.thoughts[0].id


async def test_begin_thought_while_uncommitted_exists_is_directed_not_allowed(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        sid = await _start_serial(client)
        await _call(client, "begin_thought", {"session_id": sid, "content": "draft one"})
        payload = await _call(
            client, "begin_thought", {"session_id": sid, "content": "draft two"}
        )

    assert payload["error"] == "sequencing"
    assert payload["code"] == "uncommitted_exists"
    assert "next_tool" in payload

    # the second draft must NOT have been created
    session = store.load(store.session_path(tmp_path, sid))
    assert len(session.thoughts) == 1
    assert session.thoughts[0].content == "draft one"


# ---------------------------------------------------------------------------
# critique_current_thought: adjacency contract
# ---------------------------------------------------------------------------


async def test_critique_payload_places_draft_immediately_before_template(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        sid = await _start_serial(client)
        await _call(
            client, "begin_thought", {"session_id": sid, "content": "the draft under critique"}
        )
        payload = await _call(
            client,
            "critique_current_thought",
            {"session_id": sid, "lens": "weak_evidence"},
        )

    # the current draft is present and is exactly what the model drafted
    assert payload["draft_content"] == "the draft under critique"
    # the lens template is the real bundled lens text (opens with the
    # positional "draft thought above" claim the adjacency contract protects)
    assert "draft thought above" in payload["lens_template"]
    assert payload["lens"] == "weak_evidence"
    assert payload["next_tool"] == "submit_critique"

    # ADJACENCY: draft_content must sit immediately before lens_template so
    # the template's opening "the draft thought above" doesn't dangle.
    keys = list(payload.keys())
    assert keys.index("draft_content") + 1 == keys.index("lens_template")


async def test_critique_without_lens_picks_stage_appropriate_default(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        # advance to Analysis, whose stage defaults are [weak_evidence, overconfidence]
        sid = await _start_serial(client)
        await _call(client, "advance_stage", {"session_id": sid})  # Research
        await _call(client, "advance_stage", {"session_id": sid})  # Analysis
        await _call(client, "begin_thought", {"session_id": sid, "content": "draft in analysis"})
        payload = await _call(
            client, "critique_current_thought", {"session_id": sid}
        )

    assert payload["lens"] == "weak_evidence"  # first Analysis stage default


async def test_critique_before_begin_is_directed_to_begin_thought(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        sid = await _start_serial(client)
        payload = await _call(
            client, "critique_current_thought", {"session_id": sid}
        )

    assert payload["error"] == "sequencing"
    assert payload["code"] == "begin_first"
    assert payload["next_tool"] == "begin_thought"


# ---------------------------------------------------------------------------
# Sequencing directives (weak-model accommodations)
# ---------------------------------------------------------------------------


async def test_refine_before_critique_is_directed(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        sid = await _start_serial(client)
        await _call(client, "begin_thought", {"session_id": sid, "content": "draft"})
        payload = await _call(
            client,
            "refine_current_thought",
            {"session_id": sid, "new_content": "refined"},
        )

    assert payload["error"] == "sequencing"
    assert payload["code"] == "need_critique"
    assert payload["next_tool"] == "critique_current_thought"


async def test_score_before_refine_is_directed(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        sid = await _start_serial(client)
        await _call(client, "begin_thought", {"session_id": sid, "content": "draft"})
        await _call(client, "critique_current_thought", {"session_id": sid, "lens": "weak_evidence"})
        await _call(client, "submit_critique", {"session_id": sid, "text": "a critique"})
        payload = await _call(
            client, "score_current_thought", {"session_id": sid, "scores": {"correctness": 0.8}}
        )

    assert payload["error"] == "sequencing"
    assert payload["code"] == "need_refine"
    assert payload["next_tool"] == "refine_current_thought"


async def test_commit_with_zero_rounds_is_directed(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        sid = await _start_serial(client)
        await _call(client, "begin_thought", {"session_id": sid, "content": "draft"})
        payload = await _call(client, "commit_thought", {"session_id": sid})

    assert payload["error"] == "sequencing"
    assert payload["code"] == "zero_rounds"
    assert payload["next_tool"] == "critique_current_thought"

    session = store.load(store.session_path(tmp_path, sid))
    assert session.thoughts[0].committed is False


# ---------------------------------------------------------------------------
# Full loop: begin -> critique -> submit -> refine -> score -> commit
# ---------------------------------------------------------------------------


async def test_full_serial_loop_single_round_converges_and_commits(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        # max_rounds=1 -> one round hits the cap and converges immediately.
        sid = await _start_serial(client, overrides={"serial": {"max_rounds": 1}})
        begun = await _call(
            client, "begin_thought", {"session_id": sid, "content": "initial rough draft of the idea"}
        )
        thought_id = begun["thought_id"]

        crit = await _call(
            client, "critique_current_thought", {"session_id": sid, "lens": "weak_evidence"}
        )
        assert crit["round_index"] == 0

        await _call(client, "submit_critique", {"session_id": sid, "text": "the evidence is thin"})

        refined = await _call(
            client,
            "refine_current_thought",
            {
                "session_id": sid,
                "new_content": "a much stronger and more evidence-backed second version",
                "challenged_assumptions": ["assumed X without proof"],
            },
        )
        assert "edit_distance" in refined
        assert refined["edit_distance"] > 0.05

        scored = await _call(
            client,
            "score_current_thought",
            {
                "session_id": sid,
                "scores": {
                    "correctness": 0.8,
                    "evidence": 0.7,
                    "novelty": 0.6,
                    "clarity": 0.7,
                    "bias_resistance": 0.6,
                    "actionability": 0.7,
                    "coverage": 0.6,
                },
            },
        )
        assert scored["converged"] is True
        assert scored["converged_reason"] == "max_rounds"
        assert scored["next_tool"] == "commit_thought"
        assert 0.0 <= scored["overall"] <= 1.0

        committed = await _call(client, "commit_thought", {"session_id": sid})

    assert committed["committed"] is True
    assert committed["thought_id"] == thought_id

    session = store.load(store.session_path(tmp_path, sid))
    t = session.thoughts[0]
    assert t.committed is True
    # the committed content is the refined version, not the raw draft
    assert t.content == "a much stronger and more evidence-backed second version"
    assert t.challenged_assumptions == ["assumed X without proof"]
    assert t.final_utility_scores is not None
    assert len(t.critique_rounds) == 1
    assert t.critique_rounds[0].critique_text == "the evidence is thin"
    # commit clears the current-thought cursor (unambiguous for next_action)
    assert session.current_thought_id is None


async def test_full_serial_loop_multi_round_rotates_lenses_and_continues(tmp_path):
    """With max_rounds=3 and genuinely improving scores, round 0 should
    report continue (not converged) and the server should rotate to a
    different lens when none is specified.
    """
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        sid = await _start_serial(client)  # default max_rounds=3
        await _call(client, "begin_thought", {"session_id": sid, "content": "round zero draft"})

        # Round 0 -- let the server pick the lens (stage default for Problem
        # Definition is unstated_assumption).
        crit0 = await _call(client, "critique_current_thought", {"session_id": sid})
        await _call(client, "submit_critique", {"session_id": sid, "text": "critique zero"})
        await _call(
            client,
            "refine_current_thought",
            {"session_id": sid, "new_content": "a completely rewritten round zero result here"},
        )
        scored0 = await _call(
            client,
            "score_current_thought",
            {"session_id": sid, "scores": {"correctness": 0.5, "evidence": 0.5, "novelty": 0.5,
                                            "clarity": 0.5, "bias_resistance": 0.5,
                                            "actionability": 0.5, "coverage": 0.5}},
        )
        assert scored0["converged"] is False
        assert scored0["next_tool"] == "critique_current_thought"

        # Round 1 -- server must pick a DIFFERENT lens than round 0.
        crit1 = await _call(client, "critique_current_thought", {"session_id": sid})
        assert crit1["round_index"] == 1
        assert crit1["lens"] != crit0["lens"]

    session = store.load(store.session_path(tmp_path, sid))
    lenses_used = [r.lens for r in session.thoughts[0].critique_rounds]
    assert len(lenses_used) == len(set(lenses_used))  # no repeats within a thought


# ---------------------------------------------------------------------------
# Mode-gate rejection: a subagent-mode session may not use serial tools
# ---------------------------------------------------------------------------


async def test_serial_tool_rejects_subagent_mode_session(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        started = await _call(
            client, "start_session", {"question": "q", "mode": "subagent"}
        )
        sid = started["session_id"]
        payload = await _call(
            client, "begin_thought", {"session_id": sid, "content": "should be blocked"}
        )

    assert payload["error"] == "wrong_mode"
    assert payload["required_mode"] == "serial"
    assert payload["current_mode"] == "subagent"
    assert payload["blocked_tool"] == "begin_thought"

    # nothing was created on the subagent session
    session = store.load(store.session_path(tmp_path, sid))
    assert session.thoughts == []


async def test_serial_tool_still_blocked_when_no_mode_set(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        started = await _call(client, "start_session", {"question": "q"})
        sid = started["session_id"]
        payload = await _call(
            client, "begin_thought", {"session_id": sid, "content": "blocked, no mode"}
        )

    assert payload["mode_required"] is True
    assert payload["blocked_tool"] == "begin_thought"
    assert payload["next_tool"] == "set_session_mode"
