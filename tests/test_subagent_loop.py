"""MCP contract tests for the subagent engine (Task 11).

Drive the real `mcp` SDK's in-memory client against `server.create_server()`
-- the same no-mocks-of-the-SDK pattern as `test_serial_loop.py` -- but with
the NECoRT adapter FACTORY monkeypatched to a fake so no vendored code, no
network, and no worker threads are involved. Covers the brief's required
round-trips: full loop, round-cap, sequential vs multi-endpoint dispatch,
mode-gate rejection, begin->commit short path, and the no-endpoint directive.
"""

from __future__ import annotations

from typing import Any

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from deep_think_mcp import server, store, subagent_engine
from deep_think_mcp.necort_adapter import NECoRTResult
from deep_think_mcp.session import SpecialistRound, UtilityScore


# ---------------------------------------------------------------------------
# Fakes: an adapter factory that returns canned NECoRTResults, no I/O.
# ---------------------------------------------------------------------------


def _uscore(v: float) -> UtilityScore:
    return UtilityScore(
        correctness=v, clarity=v, coverage=v,
        evidence=0.5, novelty=0.5, bias_resistance=0.5, actionability=0.5,
    )


def _fake_result(*, strength: float, content: str, n_agents: int = 2) -> NECoRTResult:
    rounds: list[SpecialistRound] = []
    for idx in (0, 1):
        for a in range(n_agents):
            winner = idx == 1 and a == 0
            rounds.append(
                SpecialistRound(
                    round_index=idx,
                    agent_role=f"agent_{a + 1}",
                    candidate_content=content if winner else f"cand-{idx}-{a}",
                    utility_vector=_uscore(strength if a == 0 else 0.3),
                    equilibrium_state=(
                        "initial" if idx == 0 else ("in_equilibrium" if a == 0 else "off_equilibrium")
                    ),
                    was_selected=winner,
                )
            )
    return NECoRTResult(
        response=content,
        specialist_rounds=rounds,
        final_utility_scores=_uscore(strength),
        converged=True,
        convergence_round=1,
        thinking_rounds=1,
        final_response_agent=0,
        raw={},
    )


class _FakeAdapter:
    def __init__(self, result: NECoRTResult, recorder: list, key: str) -> None:
        self._result = result
        self._recorder = recorder
        self._key = key

    async def run(self, user_input: str, max_rounds=None) -> NECoRTResult:
        self._recorder.append((self._key, max_rounds))
        return self._result


def _install_fake(monkeypatch, *, strength=0.9, content="synthesis", results_by_url=None):
    recorder: list = []

    def fake_make(base_url, cfg, agent_roles):
        res = (
            results_by_url[base_url]
            if results_by_url is not None
            else _fake_result(strength=strength, content=content)
        )
        return _FakeAdapter(res, recorder, key=base_url)

    monkeypatch.setattr(subagent_engine, "_make_adapter", fake_make)
    return recorder


async def _call(client, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    result = await client.call_tool(name, arguments or {})
    assert not result.isError, result.content
    assert result.structuredContent is not None
    return result.structuredContent


async def _start_subagent(client, **overrides_sub) -> str:
    overrides = {"subagent": {"endpoint": "http://fake-ep", **overrides_sub}}
    payload = await _call(
        client,
        "start_session",
        {"question": "q", "mode": "subagent", "overrides": overrides},
    )
    return payload["session_id"]


# ---------------------------------------------------------------------------
# begin -> commit short path (typical path works without intermediate tools)
# ---------------------------------------------------------------------------


async def test_begin_then_commit_short_path(tmp_path, monkeypatch):
    _install_fake(monkeypatch, strength=0.9, content="the committed synthesis")
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        sid = await _start_subagent(client)

        begun = await _call(client, "begin_subagent_thought", {"session_id": sid, "content": "seed"})
        assert begun["converged"] is True
        assert begun["rounds_run"] == 1
        assert begun["next_tool"] == "commit_subagent_thought"

        committed = await _call(client, "commit_subagent_thought", {"session_id": sid})
        assert committed["committed"] is True
        assert committed["next_tool"] == "begin_subagent_thought"

    session = store.load(store.session_path(tmp_path, sid))
    assert len(session.thoughts) == 1
    t = session.thoughts[0]
    assert t.committed is True
    assert t.content == "the committed synthesis"
    assert t.specialist_rounds  # persisted after every mutation
    assert session.current_thought_id is None


# ---------------------------------------------------------------------------
# full loop: begin -> inspect -> advance -> commit
# ---------------------------------------------------------------------------


async def test_full_loop_begin_inspect_advance_commit(tmp_path, monkeypatch):
    _install_fake(monkeypatch, strength=0.5, content="round-a")  # below threshold
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        sid = await _start_subagent(client)

        begun = await _call(client, "begin_subagent_thought", {"session_id": sid, "content": "seed"})
        assert begun["converged"] is False
        assert begun["next_tool"] == "advance_subagent_round"

        matrix = await _call(client, "inspect_utility_matrix", {"session_id": sid})
        assert matrix["rounds_run"] == 1
        assert matrix["selected_content"] == "round-a"
        assert any(c["was_selected"] for c in matrix["candidates"])

        _install_fake(monkeypatch, strength=0.5, content="round-b")
        adv = await _call(client, "advance_subagent_round", {"session_id": sid})
        assert adv["rounds_run"] == 2
        assert adv["budget_exhausted"] is True  # max_rounds default 2
        assert adv["next_tool"] == "commit_subagent_thought"

        committed = await _call(client, "commit_subagent_thought", {"session_id": sid})
        assert committed["committed"] is True

    session = store.load(store.session_path(tmp_path, sid))
    t = session.thoughts[0]
    assert t.content == "round-b"  # latest US round's winner
    assert sorted({r.round_index for r in t.specialist_rounds}) == [0, 1, 2, 3]


# ---------------------------------------------------------------------------
# round-cap enforcement: US refuses a 3rd round even if the core would continue
# ---------------------------------------------------------------------------


async def test_round_cap_enforced(tmp_path, monkeypatch):
    recorder = _install_fake(monkeypatch, strength=0.5, content="c")
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        sid = await _start_subagent(client, max_rounds=2)
        await _call(client, "begin_subagent_thought", {"session_id": sid, "content": "seed"})
        await _call(client, "advance_subagent_round", {"session_id": sid})
        assert len(recorder) == 2

        # Third advance -> budget directive, NOT another adapter call.
        blocked = await _call(client, "advance_subagent_round", {"session_id": sid})
        assert blocked["error"] == "sequencing"
        assert blocked["code"] == "round_budget_exhausted"
        assert blocked["next_tool"] == "commit_subagent_thought"
        assert len(recorder) == 2

    # Each round drove the adapter with max_rounds=1 (single-round stepping).
    assert all(m == 1 for (_url, m) in recorder)


# ---------------------------------------------------------------------------
# sequential vs multi-endpoint dispatch
# ---------------------------------------------------------------------------


async def test_single_endpoint_is_sequential(tmp_path, monkeypatch):
    recorder = _install_fake(monkeypatch, strength=0.9, content="c")
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        sid = await _start_subagent(client)  # one endpoint
        begun = await _call(client, "begin_subagent_thought", {"session_id": sid, "content": "seed"})
    assert begun["endpoints_used"] == 1
    assert {url for (url, _m) in recorder} == {"http://fake-ep"}


async def test_multi_endpoint_dispatches_concurrently(tmp_path, monkeypatch):
    results = {
        "http://ep-1": _fake_result(strength=0.60, content="weaker"),
        "http://ep-2": _fake_result(strength=0.95, content="stronger"),
    }
    recorder = _install_fake(monkeypatch, results_by_url=results)
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        payload = await _call(
            client,
            "start_session",
            {
                "question": "q",
                "mode": "subagent",
                "overrides": {"subagent": {"endpoint": "", "endpoints": ["http://ep-1", "http://ep-2"]}},
            },
        )
        sid = payload["session_id"]
        begun = await _call(client, "begin_subagent_thought", {"session_id": sid, "content": "seed"})

    assert begun["endpoints_used"] == 2
    assert begun["selected_content"] == "stronger"  # best negotiation kept
    assert {url for (url, _m) in recorder} == {"http://ep-1", "http://ep-2"}


# ---------------------------------------------------------------------------
# no-endpoint directive (points at the manual specialist path, not a failure)
# ---------------------------------------------------------------------------


async def test_no_endpoint_directs_to_manual_path(tmp_path, monkeypatch):
    # Factory should never be called; make it explode if it is.
    monkeypatch.setattr(
        subagent_engine,
        "_make_adapter",
        lambda *a, **k: pytest.fail("adapter must not be built with no endpoint"),
    )
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        payload = await _call(
            client,
            "start_session",
            {"question": "q", "mode": "subagent", "overrides": {"subagent": {"endpoint": ""}}},
        )
        sid = payload["session_id"]
        directive = await _call(client, "begin_subagent_thought", {"session_id": sid, "content": "seed"})

    assert directive["error"] == "no_endpoint"
    assert directive["next_tool"] is None
    assert "manual" in directive["message"].lower()

    session = store.load(store.session_path(tmp_path, sid))
    assert session.thoughts == []  # nothing created


# ---------------------------------------------------------------------------
# mode-gate rejection: a serial-mode / no-mode session may not use these tools
# ---------------------------------------------------------------------------


async def test_subagent_tool_rejects_serial_mode_session(tmp_path, monkeypatch):
    _install_fake(monkeypatch)
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        started = await _call(client, "start_session", {"question": "q", "mode": "serial"})
        sid = started["session_id"]
        payload = await _call(
            client, "begin_subagent_thought", {"session_id": sid, "content": "blocked"}
        )
    assert payload["error"] == "wrong_mode"
    assert payload["required_mode"] == "subagent"
    assert payload["current_mode"] == "serial"
    assert payload["blocked_tool"] == "begin_subagent_thought"

    session = store.load(store.session_path(tmp_path, sid))
    assert session.thoughts == []


async def test_subagent_tool_blocked_when_no_mode_set(tmp_path, monkeypatch):
    _install_fake(monkeypatch)
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        started = await _call(client, "start_session", {"question": "q"})
        sid = started["session_id"]
        payload = await _call(
            client, "commit_subagent_thought", {"session_id": sid}
        )
    assert payload["mode_required"] is True


# ---------------------------------------------------------------------------
# adapter failure -> directive, never a traceback (hard contract #3)
# ---------------------------------------------------------------------------


async def test_adapter_failure_returns_directive(tmp_path, monkeypatch):
    class _Boom:
        async def run(self, *a, **k):
            raise ValueError("choices key missing from 200 body")

    monkeypatch.setattr(subagent_engine, "_make_adapter", lambda *a, **k: _Boom())
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        sid = await _start_subagent(client)
        payload = await _call(client, "begin_subagent_thought", {"session_id": sid, "content": "seed"})

    assert payload["error"] == "adapter_error"
    assert payload["retryable"] is True
    assert payload["next_tool"] is None

    session = store.load(store.session_path(tmp_path, sid))
    assert session.thoughts == []  # failed begin left the session clean


# ---------------------------------------------------------------------------
# next_action drives the whole subagent loop end-to-end
# ---------------------------------------------------------------------------


async def test_next_action_walks_subagent_loop(tmp_path, monkeypatch):
    _install_fake(monkeypatch, strength=0.9, content="done")
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        sid = await _start_subagent(client)

        na = await _call(client, "next_action", {"session_id": sid})
        assert na["next_tool"] == "begin_subagent_thought"

        await _call(client, "begin_subagent_thought", {"session_id": sid, "content": "seed"})
        na = await _call(client, "next_action", {"session_id": sid})
        assert na["code"] == "subagent_converged"
        assert na["next_tool"] == "commit_subagent_thought"

        await _call(client, "commit_subagent_thought", {"session_id": sid})
        na = await _call(client, "next_action", {"session_id": sid})
        # committed, not final stage -> begin another or advance the stage
        assert na["next_tool"] == "begin_subagent_thought"
