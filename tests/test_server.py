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
# create_server() tool surface: exactly the 6 lifecycle tools, no more
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
    }
