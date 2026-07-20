"""Subprocess smoke test for the real stdio entrypoint.

Every other MCP contract test in this suite drives `server.create_server()`
through the SDK's in-memory transport
(`mcp.shared.memory.create_connected_server_and_client_session`) -- fast, but
it never actually launches the process a real MCP client config would. This
test runs the exact command documented in `README.md`'s quickstart and every
wiring guide in `docs/wiring.md` -- `uv run python -m deep_think_mcp.server`
-- as a real subprocess and speaks real stdio MCP to it, so a broken
entrypoint (import error, a stray stdout write corrupting the JSON-RPC
stream, a missing `__main__` guard) fails CI instead of silently drifting
from the docs.

Slower than the in-memory tests (it pays real process startup + `uv run`'s
own overhead), so it stays as a single well-targeted case rather than a
parallel subprocess-based suite.
"""

from __future__ import annotations

import os
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO_ROOT = Path(__file__).resolve().parent.parent


async def test_module_entrypoint_boots_over_stdio_and_responds_to_initialize(tmp_path):
    # DEEP_THINK_HOME keeps this subprocess off the real home directory, same
    # Global Constraint every other test enforces via the `tmp_path` fixture.
    env = {**os.environ, "DEEP_THINK_HOME": str(tmp_path)}
    params = StdioServerParameters(
        command="uv",
        args=["run", "python", "-m", "deep_think_mcp.server"],
        cwd=str(REPO_ROOT),
        env=env,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            result = await session.initialize()
            assert result.serverInfo.name == "deep-think-mcp"

            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            # A handful of tools spanning lifecycle, serial, subagent, and meta
            # -- enough to prove the whole tool surface registered, not just
            # that the process started.
            assert {
                "start_session",
                "set_session_mode",
                "begin_thought",
                "begin_subagent_thought",
                "next_action",
            } <= names
            # Autopilot is off by default (no config.toml exists yet in this
            # fresh tmp_path root -- bootstrap() only seeds one on first
            # start_session call), so its tools must be absent.
            assert "run_stage_autopilot" not in names

            # A real tool call round-trips end to end over the live stdio
            # transport, and bootstraps the data root inside tmp_path --
            # never the real home directory.
            call = await session.call_tool(
                "start_session", {"question": "does the stdio entrypoint work?"}
            )
            assert not call.isError, call.content
            assert call.structuredContent["mode_required"] is True
            assert (tmp_path / "config.toml").is_file()
            assert (tmp_path / "sessions").is_dir()
