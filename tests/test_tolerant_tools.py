"""MCP-boundary tolerant-input matrix + flatness audit (Task 13 Half A).

Half A mandates every tool accept JSON *or* plaintext-ish input for its
structured params, and that malformed input returns the
`retry_with_clarification` template (never a raw error). These drive the real
`mcp` SDK's in-memory client to prove the tolerance holds end-to-end through
FastMCP's own schema validation + the server wrappers, across representative
tools and both the happy-plaintext and malformed axes. The final test is the
flatness audit: no tool exposes a REQUIRED nested-object parameter.
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


async def _serial_to_score_step(client, sid: str) -> None:
    """Walk a serial session up to the point where score_current_thought is
    the expected next call."""
    await _call(client, "begin_thought", {"session_id": sid, "content": "draft"})
    await _call(client, "critique_current_thought", {"session_id": sid})
    await _call(client, "submit_critique", {"session_id": sid, "text": "a critique"})
    await _call(client, "refine_current_thought", {"session_id": sid, "new_content": "refined"})


# ---------------------------------------------------------------------------
# score_current_thought: JSON / plaintext / fenced / malformed
# ---------------------------------------------------------------------------


async def test_score_accepts_plaintext_pairs(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        started = await _call(client, "start_session", {"question": "q", "mode": "serial"})
        sid = started["session_id"]
        await _serial_to_score_step(client, sid)
        scored = await _call(
            client,
            "score_current_thought",
            {"session_id": sid, "scores": "correctness: 0.8, clarity: 0.7"},
        )
    assert scored["scores"]["correctness"] == 0.8
    assert scored["scores"]["clarity"] == 0.7


async def test_score_accepts_fenced_json(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        started = await _call(client, "start_session", {"question": "q", "mode": "serial"})
        sid = started["session_id"]
        await _serial_to_score_step(client, sid)
        scored = await _call(
            client,
            "score_current_thought",
            {"session_id": sid, "scores": "```json\n{\"correctness\": 0.95}\n```"},
        )
    assert scored["scores"]["correctness"] == 0.95


async def test_score_malformed_returns_retry_with_clarification(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        started = await _call(client, "start_session", {"question": "q", "mode": "serial"})
        sid = started["session_id"]
        payload = await _call(
            client,
            "score_current_thought",
            {"session_id": sid, "scores": "this has no numbers whatsoever"},
        )
    assert payload["error"] == "retry_with_clarification"
    assert payload["parameter"] == "scores"
    assert "example" in payload and payload["example"]


# ---------------------------------------------------------------------------
# begin_thought tags/axioms: plaintext list
# ---------------------------------------------------------------------------


async def test_begin_thought_accepts_plaintext_tags(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        started = await _call(client, "start_session", {"question": "q", "mode": "serial"})
        sid = started["session_id"]
        await _call(
            client,
            "begin_thought",
            {"session_id": sid, "content": "c", "tags": "alpha, beta, gamma", "axioms": "x\ny"},
        )
    session = store.load(store.session_path(tmp_path, sid))
    t = session.thoughts[0]
    assert t.tags == ["alpha", "beta", "gamma"]
    assert t.axioms == ["x", "y"]


async def test_begin_thought_accepts_json_array_tags(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        started = await _call(client, "start_session", {"question": "q", "mode": "serial"})
        sid = started["session_id"]
        await _call(
            client,
            "begin_thought",
            {"session_id": sid, "content": "c", "tags": ["a", "b"]},
        )
    session = store.load(store.session_path(tmp_path, sid))
    assert session.thoughts[0].tags == ["a", "b"]


# ---------------------------------------------------------------------------
# start_session stages/overrides: plaintext + JSON-string forms
# ---------------------------------------------------------------------------


async def test_start_session_accepts_plaintext_stages(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        started = await _call(
            client,
            "start_session",
            {"question": "q", "mode": "serial", "stages": "Alpha, Beta, Gamma"},
        )
    session = store.load(store.session_path(tmp_path, started["session_id"]))
    assert session.expected_stages == ["Alpha", "Beta", "Gamma"]


async def test_start_session_accepts_json_string_overrides(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        started = await _call(
            client,
            "start_session",
            {"question": "q", "mode": "serial", "overrides": '{"serial": {"max_rounds": 1}}'},
        )
    session = store.load(store.session_path(tmp_path, started["session_id"]))
    assert session.overrides == {"serial": {"max_rounds": 1}}


async def test_start_session_malformed_overrides_returns_retry(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        payload = await _call(
            client,
            "start_session",
            {"question": "q", "mode": "serial", "overrides": "not json at all"},
        )
    assert payload["error"] == "retry_with_clarification"
    assert payload["parameter"] == "overrides"


# ---------------------------------------------------------------------------
# move_session force: word form + malformed
# ---------------------------------------------------------------------------


async def test_move_session_accepts_word_force(tmp_path):
    dest = tmp_path / "elsewhere" / "taken.json"
    dest.parent.mkdir(parents=True)
    dest.write_text("already here")

    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        started = await _call(client, "start_session", {"question": "q", "mode": "serial"})
        sid = started["session_id"]
        # "yes" -> force=True -> overwrites the existing destination
        moved = await _call(
            client,
            "move_session",
            {"session_id": sid, "new_path": str(dest), "force": "yes"},
        )
    assert "error" not in moved
    assert moved["new_path"] == str(dest)


async def test_move_session_malformed_force_returns_retry(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        started = await _call(client, "start_session", {"question": "q", "mode": "serial"})
        sid = started["session_id"]
        payload = await _call(
            client,
            "move_session",
            {"session_id": sid, "new_path": str(tmp_path / "x.json"), "force": "maybe"},
        )
    assert payload["error"] == "retry_with_clarification"
    assert payload["parameter"] == "force"


# ---------------------------------------------------------------------------
# Flatness audit: no tool exposes a REQUIRED nested-object parameter.
# ---------------------------------------------------------------------------


async def test_flatness_audit_no_required_nested_object_params(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        tools = await client.list_tools()

    offenders: list[str] = []
    for tool in tools.tools:
        schema = tool.inputSchema
        required = set(schema.get("required", []))
        for name, pschema in schema.get("properties", {}).items():
            if name not in required:
                continue  # optional object params (scores/overrides) are fine
            # A required param is a violation only if it is a nested object with
            # NO scalar/string alternative -- i.e. the model is forced to build a
            # nested object. `import_session.data` is object-OR-string (tolerant),
            # so it is not a violation.
            anyof = pschema.get("anyOf")
            types = (
                {s.get("type") for s in anyof}
                if anyof
                else {pschema.get("type")}
            )
            if "object" in types and "string" not in types:
                offenders.append(f"{tool.name}.{name}")

    assert offenders == [], f"required nested-object params found: {offenders}"
