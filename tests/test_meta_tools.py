"""MCP contract tests for Task 8's meta tools: `next_action`,
`summarize_session`, `compress_history`, `export_session`/`import_session`.

Same no-mocks pattern as `test_server.py`/`test_serial_loop.py`: drive the
real `mcp` SDK in-memory client against `server.create_server()`, each test
on its own fresh `tmp_path` root.
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


async def _commit_a_thought(client, sid: str, content: str = "a committed thought") -> None:
    """Drive begin -> critique -> submit -> refine -> score -> commit with
    max_rounds effectively irrelevant (single round always converges via
    the default max_rounds=3 only after 3 rounds -- so force a 1-round
    session via overrides at start_session time instead when the caller
    needs a fast single-round commit). This helper assumes the session was
    started with overrides={"serial": {"max_rounds": 1}}.
    """
    await _call(client, "begin_thought", {"session_id": sid, "content": content})
    await _call(
        client, "critique_current_thought", {"session_id": sid, "lens": "weak_evidence"}
    )
    await _call(client, "submit_critique", {"session_id": sid, "text": "a critique"})
    await _call(
        client,
        "refine_current_thought",
        {"session_id": sid, "new_content": content + " (refined substantially)"},
    )
    await _call(
        client,
        "score_current_thought",
        {"session_id": sid, "scores": {"correctness": 0.8, "evidence": 0.7}},
    )
    await _call(client, "commit_thought", {"session_id": sid})


# ---------------------------------------------------------------------------
# next_action(): the truth table, wired through the real tool
# ---------------------------------------------------------------------------


async def test_next_action_no_mode_directs_to_set_session_mode(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        started = await _call(client, "start_session", {"question": "q"})
        payload = await _call(
            client, "next_action", {"session_id": started["session_id"]}
        )
    assert payload["code"] == "mode_required"
    assert payload["next_tool"] == "set_session_mode"


async def test_next_action_subagent_mode_not_available(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        started = await _call(
            client, "start_session", {"question": "q", "mode": "subagent"}
        )
        payload = await _call(
            client, "next_action", {"session_id": started["session_id"]}
        )
    assert payload["code"] == "subagent_not_available"
    assert payload["next_tool"] is None


async def test_next_action_unknown_session_returns_not_found(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        payload = await _call(client, "next_action", {"session_id": "nope"})
    assert payload["error"] == "session_not_found"


async def test_next_action_walks_the_full_serial_loop(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        sid = await _start_serial(client, overrides={"serial": {"max_rounds": 1}})

        assert (await _call(client, "next_action", {"session_id": sid}))["code"] == (
            "loop_no_thought_begin"
        )

        await _call(client, "begin_thought", {"session_id": sid, "content": "draft"})
        assert (await _call(client, "next_action", {"session_id": sid}))["next_tool"] == (
            "critique_current_thought"
        )

        await _call(
            client, "critique_current_thought", {"session_id": sid, "lens": "weak_evidence"}
        )
        assert (await _call(client, "next_action", {"session_id": sid}))["next_tool"] == (
            "submit_critique"
        )

        await _call(client, "submit_critique", {"session_id": sid, "text": "a critique"})
        assert (await _call(client, "next_action", {"session_id": sid}))["next_tool"] == (
            "refine_current_thought"
        )

        await _call(
            client,
            "refine_current_thought",
            {"session_id": sid, "new_content": "a substantially refined draft here"},
        )
        assert (await _call(client, "next_action", {"session_id": sid}))["next_tool"] == (
            "score_current_thought"
        )

        scored = await _call(
            client,
            "score_current_thought",
            {"session_id": sid, "scores": {"correctness": 0.8}},
        )
        assert scored["converged"] is True  # max_rounds=1 -> soft ceiling hit
        after_score = await _call(client, "next_action", {"session_id": sid})
        assert after_score["code"] == "loop_converged"
        assert after_score["next_tool"] == "commit_thought"

        await _call(client, "commit_thought", {"session_id": sid})
        after_commit = await _call(client, "next_action", {"session_id": sid})
        assert after_commit["next_tool"] == "begin_thought"
        assert after_commit["alternative_tool"] == "advance_stage"


async def test_next_action_at_final_stage_after_commit_directs_finalize(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        sid = await _start_serial(
            client, stages=["OnlyStage"], overrides={"serial": {"max_rounds": 1}}
        )
        await _commit_a_thought(client, sid)

        payload = await _call(client, "next_action", {"session_id": sid})

    assert payload["code"] == "loop_no_thought_final_stage"
    assert payload["next_tool"] == "finalize_session"


async def test_next_action_uncommitted_thought_never_points_at_advance_stage(tmp_path):
    """T7 review flag, exercised end-to-end: a thought in progress at the
    final stage must route back into the loop, not toward advance_stage or
    finalize_session (which would orphan it).
    """
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        sid = await _start_serial(client, stages=["OnlyStage"])
        await _call(client, "begin_thought", {"session_id": sid, "content": "draft"})

        payload = await _call(client, "next_action", {"session_id": sid})

    assert payload["next_tool"] not in {"advance_stage", "finalize_session"}
    assert payload["code"] == "loop_zero_rounds"


async def test_next_action_finalized_undecided_directs_move_or_keep(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        sid = await _start_serial(client)
        await _call(client, "finalize_session", {"session_id": sid})

        payload = await _call(client, "next_action", {"session_id": sid})

    assert payload["code"] == "await_move_decision"
    assert payload["next_tool"] == "move_session"
    assert payload["alternative_tool"] == "keep_here"


async def test_next_action_finalized_and_kept_reports_complete(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        sid = await _start_serial(client)
        await _call(client, "finalize_session", {"session_id": sid})
        await _call(client, "keep_here", {"session_id": sid})

        payload = await _call(client, "next_action", {"session_id": sid})

    assert payload["code"] == "session_complete"
    assert payload["next_tool"] is None


# ---------------------------------------------------------------------------
# Fix round 1 (reviewer-flagged, Important): move_session/keep_here are
# deliberately status-independent (docs/execution-plan.md Task 12 -- "the
# move machinery is status-independent"), so calling either on a still-
# ACTIVE session records a real MoveRecord/DecisionRecord. A later
# finalize_session must still surface its own move/keep prompt -- an
# earlier decision must not be silently read as having already answered
# it. Regression tests for the reviewer's exact probe, plus confirmation
# that the intended flow (finalize, THEN move/keep) still settles.
# ---------------------------------------------------------------------------


async def test_next_action_move_before_finalize_still_prompts_for_decision(tmp_path):
    """Reviewer probe: move-before-finalize previously returned
    'session_complete' (wrong); must return 'await_move_decision'.
    """
    dest = tmp_path / "elsewhere" / "moved.json"
    dest.parent.mkdir(parents=True)

    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        sid = await _start_serial(client)
        await _call(
            client, "move_session", {"session_id": sid, "new_path": str(dest)}
        )
        await _call(client, "finalize_session", {"session_id": sid})

        payload = await _call(client, "next_action", {"session_id": sid})

    assert payload["code"] == "await_move_decision"
    assert payload["next_tool"] == "move_session"


async def test_next_action_keep_here_before_finalize_still_prompts_for_decision(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        sid = await _start_serial(client)
        await _call(client, "keep_here", {"session_id": sid})
        await _call(client, "finalize_session", {"session_id": sid})

        payload = await _call(client, "next_action", {"session_id": sid})

    assert payload["code"] == "await_move_decision"
    assert payload["next_tool"] == "move_session"


async def test_next_action_finalize_then_move_reports_complete(tmp_path):
    """Existing behavior preserved: a move AFTER finalize -- the flow
    finalize_session's prompt is actually meant to trigger -- still
    settles the session.
    """
    dest = tmp_path / "elsewhere" / "moved.json"
    dest.parent.mkdir(parents=True)

    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        sid = await _start_serial(client)
        await _call(client, "finalize_session", {"session_id": sid})
        await _call(
            client, "move_session", {"session_id": sid, "new_path": str(dest)}
        )

        payload = await _call(client, "next_action", {"session_id": sid})

    assert payload["code"] == "session_complete"
    assert payload["next_tool"] is None


# ---------------------------------------------------------------------------
# summarize_session
# ---------------------------------------------------------------------------


async def test_summarize_session_reflects_committed_thoughts_only(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        sid = await _start_serial(client, overrides={"serial": {"max_rounds": 1}})
        await _commit_a_thought(client, sid, content="the committed thought")
        await _call(
            client, "begin_thought", {"session_id": sid, "content": "an uncommitted draft"}
        )

        payload = await _call(
            client, "summarize_session", {"session_id": sid, "scope": "stage"}
        )

    assert payload["thought_count"] == 1
    assert "the committed thought" in payload["digest"]
    assert "uncommitted draft" not in payload["digest"]


async def test_summarize_session_all_scope_spans_stages(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        sid = await _start_serial(
            client, stages=["Alpha", "Beta"], overrides={"serial": {"max_rounds": 1}}
        )
        await _commit_a_thought(client, sid, content="alpha stage thought")
        await _call(client, "advance_stage", {"session_id": sid})
        await _commit_a_thought(client, sid, content="beta stage thought")

        stage_only = await _call(
            client, "summarize_session", {"session_id": sid, "scope": "stage"}
        )
        everything = await _call(
            client, "summarize_session", {"session_id": sid, "scope": "all"}
        )

    assert stage_only["thought_count"] == 1
    assert everything["thought_count"] == 2
    assert everything["stages_covered"] == ["Alpha", "Beta"]


async def test_summarize_session_unknown_session_returns_not_found(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        payload = await _call(client, "summarize_session", {"session_id": "nope"})
    assert payload["error"] == "session_not_found"


# ---------------------------------------------------------------------------
# compress_history: digest length bounds, over the real tool
# ---------------------------------------------------------------------------


async def test_compress_history_excludes_current_stage_and_respects_target(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        sid = await _start_serial(
            client, stages=["Alpha", "Beta"], overrides={"serial": {"max_rounds": 1}}
        )
        await _commit_a_thought(
            client, sid, content="alpha stage reasoning " * 30
        )
        await _call(client, "advance_stage", {"session_id": sid})
        await _commit_a_thought(client, sid, content="beta stage reasoning, still fresh")

        payload = await _call(
            client, "compress_history", {"session_id": sid, "target_tokens": 50}
        )

    assert "alpha stage" in payload["digest"]
    assert "beta stage" not in payload["digest"]
    assert payload["estimated_tokens"] <= 50


async def test_compress_history_empty_before_any_prior_stage(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        sid = await _start_serial(client)
        payload = await _call(client, "compress_history", {"session_id": sid})

    assert payload["digest"] == ""
    assert payload["estimated_tokens"] == 0


async def test_compress_history_unknown_session_returns_not_found(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        payload = await _call(client, "compress_history", {"session_id": "nope"})
    assert payload["error"] == "session_not_found"


# ---------------------------------------------------------------------------
# export_session -> import_session round trip
# ---------------------------------------------------------------------------


async def test_export_then_import_recreates_session_with_new_id(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        sid = await _start_serial(client, overrides={"serial": {"max_rounds": 1}})
        await _commit_a_thought(client, sid, content="reasoning to export")

        exported = await _call(client, "export_session", {"session_id": sid})
        imported = await _call(client, "import_session", {"data": exported["export"]})

        # both are independently resumable/listed
        listed = await _call(client, "list_sessions")

    # Importing back into the SAME store collides on id -> reassigned.
    assert imported["id_reassigned"] is True
    assert imported["session_id"] != sid

    new_session = store.load(store.session_path(tmp_path, imported["session_id"]))
    original_session = store.load(store.session_path(tmp_path, sid))
    assert new_session.question == original_session.question
    assert new_session.mode == original_session.mode
    assert len(new_session.thoughts) == len(original_session.thoughts)
    assert new_session.thoughts[0].content == original_session.thoughts[0].content

    ids = {s["id"] for s in listed["sessions"]}
    assert {sid, imported["session_id"]} <= ids


async def test_import_session_accepts_json_string_form(tmp_path):
    import json

    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        sid = await _start_serial(client)
        exported = await _call(client, "export_session", {"session_id": sid})

        imported = await _call(
            client, "import_session", {"data": json.dumps(exported["export"])}
        )

    assert imported["id_reassigned"] is True
    assert "error" not in imported


async def test_import_session_into_fresh_store_keeps_original_id(tmp_path):
    """No collision (different root entirely) -> the imported session keeps
    its original id.
    """
    src_root = tmp_path / "source"
    dst_root = tmp_path / "dest"
    src_root.mkdir()
    dst_root.mkdir()

    src_srv = server.create_server(root=src_root)
    async with create_connected_server_and_client_session(src_srv) as client:
        sid = await _start_serial(client)
        exported = await _call(client, "export_session", {"session_id": sid})

    dst_srv = server.create_server(root=dst_root)
    async with create_connected_server_and_client_session(dst_srv) as client:
        imported = await _call(
            client, "import_session", {"data": exported["export"]}
        )

    assert imported["id_reassigned"] is False
    assert imported["session_id"] == sid


async def test_import_session_malformed_json_string_returns_clean_error(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        payload = await _call(
            client, "import_session", {"data": "{not valid json at all"}
        )
    assert payload["error"] == "invalid_json"


async def test_import_session_schema_violation_returns_clean_error(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        payload = await _call(
            client, "import_session", {"data": {"question": "q", "mode": "not-a-mode"}}
        )
    assert payload["error"] == "invalid_session_data"
