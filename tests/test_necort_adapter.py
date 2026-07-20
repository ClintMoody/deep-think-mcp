"""Tests for the NECoRT adapter (Task 10).

Three categories, per the brief:

1. Contract / schema-stability tests against a MOCKED Nash layer -- feed
   `translate()` synthetic Nash-shaped dicts and assert the mapping to
   `SpecialistRound[]` / `UtilityScore` is valid and deterministic. No
   vendored code, no HTTP -- these always run.
2. A real-vendored-code test driving the REAL
   `NashEquilibriumRecursiveChat` through the adapter against a FAKE local
   loopback HTTP endpoint (no outbound network). Skipped gracefully if the
   `vendor/necort` submodule isn't populated.
3. An explicit datetime-shim regression test: proves the vendored
   `NameError: datetime` crash site (`datetime.now()` in
   `think_and_respond`) no longer fires once the adapter's
   module-attribute injection is applied.

No test in this file touches the outside network. The "real endpoint" is a
`http.server` bound to 127.0.0.1 on an ephemeral port.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from deep_think_mcp import necort_adapter
from deep_think_mcp.necort_adapter import (
    NEUTRAL_DIM,
    POPULATED_DIMS,
    UNPOPULATED_DIMS,
    NECoRTAdapter,
    NECoRTResult,
    translate,
)
from deep_think_mcp.session import SpecialistRound, UtilityScore


# ---------------------------------------------------------------------------
# Synthetic Nash-shaped fixtures (mirror the real vendored return structure)
# ---------------------------------------------------------------------------


def _synthetic_nash_result() -> dict:
    """A 3-agent, 2-round Nash result shaped exactly like the vendored
    `think_and_respond` return value (round 0 has no matrix/equilibrium;
    later rounds carry an n*n utility matrix with a zero diagonal)."""
    return {
        "response": "improved answer from agent 1",
        "thinking_rounds": 2,
        "converged": True,
        "convergence_round": 2,
        "final_response_agent": 1,
        "thinking_history": [
            {
                "round": 0,
                "agent_responses": ["a0", "a1", "a2"],
                "equilibrium_indices": [],
                "utility_matrix": None,
            },
            {
                "round": 1,
                "agent_responses": ["b0", "b1", "b2"],
                "equilibrium_indices": [1],
                "utility_matrix": [[0, 8, 6], [7, 0, 5], [6, 9, 0]],
            },
            {
                "round": 2,
                "agent_responses": ["c0", "improved answer from agent 1", "c2"],
                "equilibrium_indices": [1],
                "utility_matrix": [[0, 9, 6], [8, 0, 5], [7, 10, 0]],
            },
        ],
    }


# ---------------------------------------------------------------------------
# 1. Contract / schema-stability tests (mocked Nash layer)
# ---------------------------------------------------------------------------


def test_translate_returns_result_with_all_rounds():
    result = translate(_synthetic_nash_result())
    assert isinstance(result, NECoRTResult)
    # 3 agents * 3 history rounds = 9 SpecialistRounds.
    assert len(result.specialist_rounds) == 9
    assert all(isinstance(r, SpecialistRound) for r in result.specialist_rounds)
    assert isinstance(result.final_utility_scores, UtilityScore)


def test_translate_round_zero_is_neutral_and_initial():
    result = translate(_synthetic_nash_result())
    round0 = [r for r in result.specialist_rounds if r.round_index == 0]
    assert len(round0) == 3
    for r in round0:
        assert r.equilibrium_state == "initial"
        assert r.was_selected is False
        for dim in POPULATED_DIMS + UNPOPULATED_DIMS:
            assert getattr(r.utility_vector, dim) == NEUTRAL_DIM


def test_translate_marks_exactly_one_selected_candidate():
    result = translate(_synthetic_nash_result())
    selected = [r for r in result.specialist_rounds if r.was_selected]
    assert len(selected) == 1
    winner = selected[0]
    assert winner.round_index == 2  # last round
    assert winner.agent_role  # non-empty
    assert winner.candidate_content == "improved answer from agent 1"


def test_translate_equilibrium_state_labels():
    result = translate(_synthetic_nash_result())
    per_round: dict[int, list[SpecialistRound]] = {}
    for r in result.specialist_rounds:
        per_round.setdefault(r.round_index, []).append(r)
    # equilibrium_indices for round 1 == [1]
    assert per_round[1][1].equilibrium_state == "in_equilibrium"
    assert per_round[1][0].equilibrium_state == "off_equilibrium"
    assert per_round[1][2].equilibrium_state == "off_equilibrium"


def test_translate_utility_vector_is_off_diagonal_column_mean():
    result = translate(_synthetic_nash_result())
    per_round: dict[int, list[SpecialistRound]] = {}
    for r in result.specialist_rounds:
        per_round.setdefault(r.round_index, []).append(r)
    # round 1, agent 0: column 0 off-diagonal = matrix[1][0]=7, matrix[2][0]=6
    # mean = 6.5 -> /10 = 0.65
    agent0 = per_round[1][0].utility_vector
    for dim in POPULATED_DIMS:
        assert agent0.correctness == pytest.approx(0.65)
        assert getattr(agent0, dim) == pytest.approx(0.65)
    for dim in UNPOPULATED_DIMS:
        assert getattr(agent0, dim) == NEUTRAL_DIM


def test_translate_final_utility_is_winner_column_mean():
    result = translate(_synthetic_nash_result())
    # winner = agent 1, final round matrix column 1 off-diagonal:
    # matrix[0][1]=9, matrix[2][1]=10 -> mean 9.5 -> /10 = 0.95
    fus = result.final_utility_scores
    for dim in POPULATED_DIMS:
        assert getattr(fus, dim) == pytest.approx(0.95)
    for dim in UNPOPULATED_DIMS:
        assert getattr(fus, dim) == NEUTRAL_DIM


def test_translate_all_dims_in_unit_range():
    result = translate(_synthetic_nash_result())
    for r in result.specialist_rounds:
        for dim in POPULATED_DIMS + UNPOPULATED_DIMS:
            v = getattr(r.utility_vector, dim)
            assert 0.0 <= v <= 1.0


def test_translate_is_deterministic():
    a = translate(_synthetic_nash_result())
    b = translate(_synthetic_nash_result())
    assert [r.model_dump() for r in a.specialist_rounds] == [
        r.model_dump() for r in b.specialist_rounds
    ]
    assert a.final_utility_scores.model_dump() == b.final_utility_scores.model_dump()


def test_translate_agent_role_mapping_and_positional_fallback():
    # 3 agents, only 2 named roles -> 3rd agent gets a positional name.
    result = translate(_synthetic_nash_result(), agent_roles=["Analysis", "Creativity"])
    per_round: dict[int, list[SpecialistRound]] = {}
    for r in result.specialist_rounds:
        per_round.setdefault(r.round_index, []).append(r)
    assert per_round[0][0].agent_role == "Analysis"
    assert per_round[0][1].agent_role == "Creativity"
    assert per_round[0][2].agent_role == "agent_3"


def test_translate_passthrough_scalar_fields():
    result = translate(_synthetic_nash_result())
    assert result.response == "improved answer from agent 1"
    assert result.converged is True
    assert result.convergence_round == 2
    assert result.thinking_rounds == 2
    assert result.final_response_agent == 1


def test_translate_single_agent_matrix_is_neutral():
    # n == 1: no off-diagonal raters -> no peer signal -> neutral.
    nash = {
        "response": "solo",
        "thinking_rounds": 1,
        "converged": False,
        "convergence_round": None,
        "final_response_agent": 0,
        "thinking_history": [
            {"round": 0, "agent_responses": ["solo"], "equilibrium_indices": [], "utility_matrix": None},
            {"round": 1, "agent_responses": ["solo"], "equilibrium_indices": [0], "utility_matrix": [[0]]},
        ],
    }
    result = translate(nash)
    for r in result.specialist_rounds:
        for dim in POPULATED_DIMS + UNPOPULATED_DIMS:
            assert getattr(r.utility_vector, dim) == NEUTRAL_DIM


def test_translate_empty_history_is_safe():
    nash = {
        "response": "",
        "thinking_rounds": 0,
        "converged": False,
        "convergence_round": None,
        "final_response_agent": 0,
        "thinking_history": [],
    }
    result = translate(nash)
    assert result.specialist_rounds == []
    assert isinstance(result.final_utility_scores, UtilityScore)
    for dim in POPULATED_DIMS + UNPOPULATED_DIMS:
        assert getattr(result.final_utility_scores, dim) == NEUTRAL_DIM


def test_translate_handles_numpy_style_indices():
    # equilibrium_indices / final_response_agent can arrive as numpy ints.
    np = pytest.importorskip("numpy")
    nash = _synthetic_nash_result()
    nash["final_response_agent"] = np.int64(1)
    nash["thinking_history"][2]["equilibrium_indices"] = [np.int64(1)]
    result = translate(nash)
    selected = [r for r in result.specialist_rounds if r.was_selected]
    assert len(selected) == 1
    assert selected[0].round_index == 2


# ---------------------------------------------------------------------------
# Fake local OpenAI-compatible endpoint (loopback only, no outbound network)
# ---------------------------------------------------------------------------


class _RecordingOpenAIServer:
    """A loopback HTTP server that answers every POST with a fixed
    OpenAI-compatible chat-completion and records each request body so the
    test can assert the adapter's `_call_api` stripped the OpenRouter
    `reasoning` field and did not stream."""

    def __init__(self, content: str = "2") -> None:
        self.content = content
        self.bodies: list[dict] = []
        server_self = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 (stdlib name)
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                try:
                    server_self.bodies.append(json.loads(raw))
                except json.JSONDecodeError:
                    server_self.bodies.append({})
                payload = json.dumps(
                    {"choices": [{"message": {"content": server_self.content}}]}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):  # silence stderr access log
                pass

        self._httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1/chat/completions"

    def __enter__(self) -> "_RecordingOpenAIServer":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


# ---------------------------------------------------------------------------
# 2. Real-vendored-code test against the fake local endpoint
# ---------------------------------------------------------------------------


def _require_vendored():
    if not necort_adapter.is_vendored_available():
        pytest.skip("vendor/necort submodule not initialized")


def test_real_vendored_run_sync_translates_to_valid_rounds():
    _require_vendored()
    with _RecordingOpenAIServer(content="2") as server:
        adapter = NECoRTAdapter(
            base_url=server.base_url,
            model="local-test-model",
            num_agents=3,
            agent_roles=["Analysis", "Creativity"],
        )
        result = adapter.run_sync("What is 2+2?")

    assert isinstance(result, NECoRTResult)
    assert result.specialist_rounds  # non-empty
    assert all(isinstance(r, SpecialistRound) for r in result.specialist_rounds)
    assert isinstance(result.final_utility_scores, UtilityScore)
    # Exactly one selected candidate across the whole run.
    assert sum(1 for r in result.specialist_rounds if r.was_selected) == 1
    # Fake endpoint always returns "2" -> that is the candidate content.
    assert result.response == "2"


def test_real_vendored_call_api_strips_reasoning_and_does_not_stream():
    _require_vendored()
    with _RecordingOpenAIServer(content="2") as server:
        adapter = NECoRTAdapter(
            base_url=server.base_url, model="local-test-model", num_agents=2
        )
        adapter.run_sync("hello")
        bodies = server.bodies

    assert bodies, "adapter should have made at least one request"
    for body in bodies:
        # Shim #2: OpenRouter-only field stripped, non-streaming forced.
        assert "reasoning" not in body
        assert body.get("stream") is False
        assert body["model"] == "local-test-model"


async def test_real_vendored_async_run_offloads_and_translates():
    _require_vendored()
    with _RecordingOpenAIServer(content="2") as server:
        adapter = NECoRTAdapter(
            base_url=server.base_url, model="local-test-model", num_agents=2
        )
        result = await adapter.run("async question")

    assert isinstance(result, NECoRTResult)
    assert result.specialist_rounds
    assert isinstance(result.final_utility_scores, UtilityScore)


# ---------------------------------------------------------------------------
# 3. datetime-shim regression test (explicit)
# ---------------------------------------------------------------------------


def test_datetime_shim_injected_into_vendored_module():
    _require_vendored()
    necort_adapter._ensure_loaded()
    import nash_recursive_thinking  # noqa: E402 (loaded onto sys.path by adapter)

    # Module-attribute injection made the missing top-level import resolvable.
    assert getattr(nash_recursive_thinking, "datetime", None) is datetime


def test_datetime_crash_site_no_longer_raises():
    _require_vendored()
    cls = necort_adapter._ensure_loaded()
    chat = cls(base_url="http://unused.invalid", model="m")

    # Stub the network so this stays offline; drive the REAL vendored
    # think_and_respond, whose tail calls datetime.now() -- the verified
    # crash site. Without the shim this raises NameError.
    calls = {"n": 0}

    def fake_call_api(messages, temperature=0.7, stream=True):
        calls["n"] += 1
        return "2"

    chat._call_api = fake_call_api
    result = chat.think_and_respond("hi", verbose=False)
    assert "response" in result
    assert calls["n"] > 0
