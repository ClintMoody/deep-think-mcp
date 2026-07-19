"""MCP contract tests for deep_think_mcp.server.

These drive the real `mcp` SDK's in-memory client/server transport
(mcp.shared.memory.create_connected_server_and_client_session) against the
real FastMCP server object built by `server.create_server()` -- no mocks.
Every test gets its own server instance rooted at a fresh `tmp_path`, per
Global Constraints ("tests must NEVER touch the real home directory --
always inject a tmp root").

Covers the brief's required round-trips (mode-required flow, immutability
rejection, resume/list/clear) plus the central mode-gate mechanism itself
(the "Key architectural note" in the task brief), exercised here through a
throwaway dummy tool registered via `server.mode_gate` -- Task 3 has no real
thought tools of its own, those arrive in Tasks 7/11, but the gate they'll
reuse must be proven now.
"""

from __future__ import annotations

from typing import Any

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from deep_think_mcp import index, prompts, server, store
from deep_think_mcp.session import Session


async def _call(client, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call a tool and return its structuredContent dict, asserting no error."""
    result = await client.call_tool(name, arguments or {})
    assert not result.isError, result.content
    assert result.structuredContent is not None
    return result.structuredContent


# ---------------------------------------------------------------------------
# start_session: mode-required flow
# ---------------------------------------------------------------------------


async def test_start_session_without_mode_returns_mode_required_payload(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        payload = await _call(client, "start_session", {"question": "What now?"})

    assert payload["mode_required"] is True
    assert payload["next_tool"] == "set_session_mode"
    assert "session_id" in payload and payload["session_id"]
    mode_names = {m["name"] for m in payload["modes"]}
    assert mode_names == {"serial", "subagent"}
    for m in payload["modes"]:
        assert isinstance(m["description"], str) and "\n" not in m["description"]

    # the session was actually created + persisted, just awaiting a mode
    session = store.load(store.session_path(tmp_path, payload["session_id"]))
    assert session.question == "What now?"
    assert session.mode is None


async def test_start_session_bootstraps_store_on_first_use(tmp_path):
    assert not (tmp_path / "sessions").exists()
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        await _call(client, "start_session", {"question": "boot?"})

    assert (tmp_path / "sessions").is_dir()
    assert (tmp_path / "logs").is_dir()
    assert (tmp_path / "config.toml").is_file()


async def test_start_session_defaults_expected_stages_from_config(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        payload = await _call(client, "start_session", {"question": "q"})

    session = store.load(store.session_path(tmp_path, payload["session_id"]))
    assert session.expected_stages == [
        "Problem Definition",
        "Research",
        "Analysis",
        "Synthesis",
        "Conclusion",
    ]
    assert session.current_stage == "Problem Definition"


async def test_start_session_honors_explicit_stages(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        payload = await _call(
            client,
            "start_session",
            {"question": "q", "stages": ["Alpha", "Beta"]},
        )

    session = store.load(store.session_path(tmp_path, payload["session_id"]))
    assert session.expected_stages == ["Alpha", "Beta"]
    assert session.current_stage == "Alpha"


async def test_start_session_persists_overrides_on_the_session(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        payload = await _call(
            client,
            "start_session",
            {"question": "q", "overrides": {"serial": {"max_rounds": 1}}},
        )

    session = store.load(store.session_path(tmp_path, payload["session_id"]))
    assert session.overrides == {"serial": {"max_rounds": 1}}


# ---------------------------------------------------------------------------
# start_session: valid mode proceeds immediately
# ---------------------------------------------------------------------------


async def test_start_session_with_valid_mode_proceeds_immediately(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        payload = await _call(
            client, "start_session", {"question": "q", "mode": "serial"}
        )

    assert "mode_required" not in payload
    assert payload["mode"] == "serial"
    assert "session_id" in payload

    session = store.load(store.session_path(tmp_path, payload["session_id"]))
    assert session.mode == "serial"


async def test_start_session_rejects_invalid_mode(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        result = await client.call_tool(
            "start_session", {"question": "q", "mode": "parallel"}
        )
    assert result.isError


# ---------------------------------------------------------------------------
# list_modes
# ---------------------------------------------------------------------------


async def test_list_modes_returns_descriptions_and_recommendations_for_both(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        payload = await _call(client, "list_modes")

    modes = {m["name"]: m for m in payload["modes"]}
    assert set(modes) == {"serial", "subagent"}

    serial_text = modes["serial"]["description"] + modes["serial"]["recommended_for"]
    assert "single-gpu" in serial_text.lower() or "small" in serial_text.lower()

    subagent_text = (
        modes["subagent"]["description"] + modes["subagent"]["recommended_for"]
    )
    assert "compute" in subagent_text.lower()


# ---------------------------------------------------------------------------
# set_session_mode: sets once, immutable after
# ---------------------------------------------------------------------------


async def test_set_session_mode_sets_mode_when_unset(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        started = await _call(client, "start_session", {"question": "q"})
        session_id = started["session_id"]

        payload = await _call(
            client, "set_session_mode", {"session_id": session_id, "mode": "subagent"}
        )

    assert payload["mode"] == "subagent"
    assert "error" not in payload

    session = store.load(store.session_path(tmp_path, session_id))
    assert session.mode == "subagent"


async def test_set_session_mode_rejects_change_once_set(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        started = await _call(
            client, "start_session", {"question": "q", "mode": "serial"}
        )
        session_id = started["session_id"]

        payload = await _call(
            client, "set_session_mode", {"session_id": session_id, "mode": "subagent"}
        )

    assert payload["error"] == "mode_immutable"
    assert payload["current_mode"] == "serial"

    # the rejected change must not have been applied
    session = store.load(store.session_path(tmp_path, session_id))
    assert session.mode == "serial"


async def test_set_session_mode_unknown_session_returns_not_found(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        payload = await _call(
            client, "set_session_mode", {"session_id": "does-not-exist", "mode": "serial"}
        )

    assert payload["error"] == "session_not_found"


async def test_set_session_mode_rejects_invalid_mode_value(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        started = await _call(client, "start_session", {"question": "q"})
        result = await client.call_tool(
            "set_session_mode",
            {"session_id": started["session_id"], "mode": "parallel"},
        )
    assert result.isError


# ---------------------------------------------------------------------------
# resume_session
# ---------------------------------------------------------------------------


async def test_resume_session_returns_persisted_state(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        started = await _call(
            client, "start_session", {"question": "resume me", "mode": "serial"}
        )
        session_id = started["session_id"]

        payload = await _call(client, "resume_session", {"session_id": session_id})

    assert payload["session_id"] == session_id
    assert payload["question"] == "resume me"
    assert payload["mode"] == "serial"
    assert payload["current_stage"] == "Problem Definition"
    assert payload["status"] == "active"


async def test_resume_session_unknown_id_returns_not_found(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        payload = await _call(client, "resume_session", {"session_id": "nope"})

    assert payload["error"] == "session_not_found"


# ---------------------------------------------------------------------------
# list_sessions
# ---------------------------------------------------------------------------


async def test_list_sessions_empty_when_none_created(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        payload = await _call(client, "list_sessions")

    assert payload["sessions"] == []
    assert payload["count"] == 0


async def test_list_sessions_returns_all_created_sessions(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        a = await _call(client, "start_session", {"question": "a", "mode": "serial"})
        b = await _call(client, "start_session", {"question": "b"})

        payload = await _call(client, "list_sessions")

    by_id = {s["id"]: s for s in payload["sessions"]}
    assert set(by_id) == {a["session_id"], b["session_id"]}
    assert payload["count"] == 2
    assert by_id[a["session_id"]]["mode"] == "serial"
    assert by_id[a["session_id"]]["status"] == "active"
    assert by_id[b["session_id"]]["mode"] is None


# ---------------------------------------------------------------------------
# clear_session
# ---------------------------------------------------------------------------


async def test_clear_session_wipes_file_and_index_entry(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        started = await _call(client, "start_session", {"question": "q"})
        session_id = started["session_id"]

        payload = await _call(client, "clear_session", {"session_id": session_id})

        # after clearing, resume/list must both reflect it's gone
        resumed = await _call(client, "resume_session", {"session_id": session_id})
        listed = await _call(client, "list_sessions")

    assert payload["status"] == "cleared"
    assert not (tmp_path / "sessions" / f"{session_id}.json").exists()
    assert index.get(tmp_path, session_id) is None
    assert resumed["error"] == "session_not_found"
    assert listed["sessions"] == []


async def test_clear_session_unknown_id_returns_not_found(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        payload = await _call(client, "clear_session", {"session_id": "nope"})

    assert payload["error"] == "session_not_found"


# ---------------------------------------------------------------------------
# Central mode gate (server.mode_gate) -- the brief's "Key architectural
# note": thought tools (Tasks 7, 11) must inherit this for free. Proven here
# via a throwaway dummy tool on a private test server -- the real
# create_server() tool surface stays exactly the 6 lifecycle tools; no
# stubbing of future engine tools.
# ---------------------------------------------------------------------------


def _build_gated_probe_server(tmp_path):
    from mcp.server.fastmcp import FastMCP

    probe = FastMCP("probe")
    calls: list[str] = []

    @probe.tool()
    @server.mode_gate(tmp_path)
    def begin_thought(session_id: str, content: str) -> dict[str, Any]:
        calls.append(content)
        return {"session_id": session_id, "content": content, "ran": True}

    return probe, calls


async def test_mode_gate_blocks_thought_tool_when_mode_unset(tmp_path):
    lifecycle_srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(lifecycle_srv) as client:
        started = await _call(client, "start_session", {"question": "q"})
    session_id = started["session_id"]

    probe, calls = _build_gated_probe_server(tmp_path)
    async with create_connected_server_and_client_session(probe) as client:
        payload = await _call(
            client, "begin_thought", {"session_id": session_id, "content": "hi"}
        )

    assert payload["mode_required"] is True
    assert payload["next_tool"] == "set_session_mode"
    assert payload["blocked_tool"] == "begin_thought"
    assert calls == []  # the wrapped function never ran


async def test_mode_gate_allows_thought_tool_once_mode_is_set(tmp_path):
    lifecycle_srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(lifecycle_srv) as client:
        started = await _call(
            client, "start_session", {"question": "q", "mode": "serial"}
        )
    session_id = started["session_id"]

    probe, calls = _build_gated_probe_server(tmp_path)
    async with create_connected_server_and_client_session(probe) as client:
        payload = await _call(
            client, "begin_thought", {"session_id": session_id, "content": "hi"}
        )

    assert payload["ran"] is True
    assert payload["content"] == "hi"
    assert calls == ["hi"]


async def test_mode_gate_returns_not_found_for_unknown_session(tmp_path):
    probe, calls = _build_gated_probe_server(tmp_path)
    async with create_connected_server_and_client_session(probe) as client:
        payload = await _call(
            client, "begin_thought", {"session_id": "nope", "content": "hi"}
        )

    assert payload["error"] == "session_not_found"
    assert calls == []


# ---------------------------------------------------------------------------
# create_server() tool surface: exactly the 9 lifecycle tools, no more
# ---------------------------------------------------------------------------


async def test_create_server_registers_exactly_the_lifecycle_tools(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        tools = await client.list_tools()

    names = {t.name for t in tools.tools}
    assert names == {
        "start_session",
        "set_session_mode",
        "list_modes",
        "resume_session",
        "list_sessions",
        "clear_session",
        "finalize_session",
        "move_session",
        "keep_here",
    }


# ---------------------------------------------------------------------------
# finalize_session
# ---------------------------------------------------------------------------


async def test_finalize_session_returns_human_prompt_and_available_tools(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        started = await _call(client, "start_session", {"question": "q", "mode": "serial"})
        session_id = started["session_id"]

        payload = await _call(client, "finalize_session", {"session_id": session_id})

    assert payload["status"] == "finalized"
    assert payload["current_path"] == str(store.session_path(tmp_path, session_id))
    expected_prompt = (
        f"Your reasoning is saved at `{payload['current_path']}`. Would you "
        "like to move it elsewhere (a project folder, your Documents, "
        "etc.), or leave it where it is?"
    )
    assert payload["human_prompt"] == expected_prompt
    tool_names = {t["name"] for t in payload["available_tools"]}
    assert tool_names == {"move_session", "keep_here"}

    session = store.load(store.session_path(tmp_path, session_id))
    assert session.status == "finalized"


async def test_finalize_session_unknown_id_returns_not_found(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        payload = await _call(client, "finalize_session", {"session_id": "nope"})

    assert payload["error"] == "session_not_found"


# ---------------------------------------------------------------------------
# finalize -> move -> resume: the brief's headline round trip. A session
# moved outside the data root must stay fully functional -- list_sessions
# and resume_session find it via the index's absolute path.
# ---------------------------------------------------------------------------


async def test_finalize_then_move_then_resume_works(tmp_path):
    dest_dir = tmp_path / "outside" / "Documents"
    dest_dir.mkdir(parents=True)
    dest = dest_dir / "my-reasoning.json"

    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        started = await _call(
            client, "start_session", {"question": "move me", "mode": "serial"}
        )
        session_id = started["session_id"]
        original_path = store.session_path(tmp_path, session_id)

        await _call(client, "finalize_session", {"session_id": session_id})
        moved = await _call(
            client, "move_session", {"session_id": session_id, "new_path": str(dest)}
        )

        resumed = await _call(client, "resume_session", {"session_id": session_id})
        listed = await _call(client, "list_sessions")

    assert moved["new_path"] == str(dest)
    assert moved["from_path"] == str(original_path)
    assert not original_path.exists()
    assert dest.is_file()

    assert resumed["session_id"] == session_id
    assert resumed["save_path"] == str(dest)
    assert resumed["status"] == "finalized"

    by_id = {s["id"]: s for s in listed["sessions"]}
    assert by_id[session_id]["path"] == str(dest)

    # the moved file itself carries the move in its audit trail
    on_disk = store.load(dest)
    assert len(on_disk.move_history) == 1
    assert on_disk.move_history[0].from_path == str(original_path)
    assert on_disk.move_history[0].to_path == str(dest)


# ---------------------------------------------------------------------------
# finalize -> keep_here: file stays put and stays indexed
# ---------------------------------------------------------------------------


async def test_finalize_then_keep_here_leaves_file_in_place_and_indexed(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        started = await _call(
            client, "start_session", {"question": "stay put", "mode": "serial"}
        )
        session_id = started["session_id"]
        original_path = store.session_path(tmp_path, session_id)

        await _call(client, "finalize_session", {"session_id": session_id})
        payload = await _call(client, "keep_here", {"session_id": session_id})

        resumed = await _call(client, "resume_session", {"session_id": session_id})

    assert payload["save_path"] == str(original_path)
    assert original_path.is_file()

    session = store.load(original_path)
    assert session.save_path == str(original_path)
    assert len(session.decisions) == 1
    assert session.decisions[0].action == "keep_here"

    assert resumed["save_path"] == str(original_path)
    assert index.get(tmp_path, session_id)["path"] == str(original_path)


async def test_keep_here_unknown_id_returns_not_found(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        payload = await _call(client, "keep_here", {"session_id": "nope"})

    assert payload["error"] == "session_not_found"


# ---------------------------------------------------------------------------
# move_session: clobber protection, force override, unwritable destination
# ---------------------------------------------------------------------------


async def test_move_session_fails_cleanly_when_destination_exists_without_force(
    tmp_path,
):
    dest = tmp_path / "elsewhere" / "taken.json"
    dest.parent.mkdir(parents=True)
    dest.write_text("something already lives here")

    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        started = await _call(client, "start_session", {"question": "q", "mode": "serial"})
        session_id = started["session_id"]
        original_path = store.session_path(tmp_path, session_id)

        payload = await _call(
            client, "move_session", {"session_id": session_id, "new_path": str(dest)}
        )

        resumed = await _call(client, "resume_session", {"session_id": session_id})

    assert payload["error"] == "destination_exists"
    assert dest.read_text() == "something already lives here"
    assert original_path.is_file()
    assert resumed["save_path"] == str(original_path)


async def test_move_session_with_force_overwrites_existing_destination(tmp_path):
    dest = tmp_path / "elsewhere" / "taken.json"
    dest.parent.mkdir(parents=True)
    dest.write_text("stale")

    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        started = await _call(client, "start_session", {"question": "q", "mode": "serial"})
        session_id = started["session_id"]
        original_path = store.session_path(tmp_path, session_id)

        payload = await _call(
            client,
            "move_session",
            {"session_id": session_id, "new_path": str(dest), "force": True},
        )

    assert "error" not in payload
    assert payload["new_path"] == str(dest)
    assert not original_path.exists()
    assert store.load(dest).id == session_id


async def test_move_session_fails_cleanly_when_destination_not_writable(tmp_path):
    locked_dir = tmp_path / "locked"
    locked_dir.mkdir()
    locked_dir.chmod(0o555)
    dest = locked_dir / "moved.json"

    srv = server.create_server(root=tmp_path)
    try:
        async with create_connected_server_and_client_session(srv) as client:
            started = await _call(
                client, "start_session", {"question": "q", "mode": "serial"}
            )
            session_id = started["session_id"]
            original_path = store.session_path(tmp_path, session_id)

            payload = await _call(
                client, "move_session", {"session_id": session_id, "new_path": str(dest)}
            )

        assert payload["error"] == "destination_not_writable"
        assert original_path.is_file()
    finally:
        locked_dir.chmod(0o755)  # let pytest clean up tmp_path


async def test_move_session_unknown_id_returns_not_found(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        payload = await _call(
            client,
            "move_session",
            {"session_id": "nope", "new_path": str(tmp_path / "x.json")},
        )

    assert payload["error"] == "session_not_found"


# ---------------------------------------------------------------------------
# move_session: cross-filesystem-safe -- must go through the copy+verify+
# unlink path, never a bare rename (which raises EXDEV crossing devices).
# ---------------------------------------------------------------------------


async def test_move_session_survives_rename_being_unavailable(tmp_path, monkeypatch):
    import os
    import shutil

    def _boom(*args, **kwargs):
        raise AssertionError("move_session must not call rename/shutil.move")

    monkeypatch.setattr(os, "rename", _boom)
    monkeypatch.setattr(shutil, "move", _boom)

    dest = tmp_path / "elsewhere" / "moved.json"
    dest.parent.mkdir(parents=True)

    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        started = await _call(client, "start_session", {"question": "q", "mode": "serial"})
        session_id = started["session_id"]

        payload = await _call(
            client, "move_session", {"session_id": session_id, "new_path": str(dest)}
        )

    assert "error" not in payload
    assert payload["new_path"] == str(dest)
    assert dest.is_file()
    assert store.load(dest).id == session_id
