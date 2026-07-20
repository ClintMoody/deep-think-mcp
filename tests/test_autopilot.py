"""MCP contract tests for autopilot (Task 14, M6).

Autopilot is the optional, feature-flagged mode where the SERVER drives a whole
stage internally against a configured local model, instead of the calling model
stepping through the loops. These tests drive the real `mcp` SDK's in-memory
client against `server.create_server()` -- no mocks of the SDK -- with the
OpenAI-compatible endpoint replaced by an in-process loopback HTTP server
(per the `test_necort_adapter.py` precedent -- NO real network).

Coverage (per the task brief):
  - tool visibility on/off (enabled=false -> absent, enabled=true -> present);
  - a full run_stage_autopilot serial run driving >=2 convergence-relevant
    rounds -> commit (thoughts persisted + convergence honored);
  - run_subagent_autopilot in manual engine mode end-to-end;
  - run_subagent_autopilot in necort engine mode (fake adapter, no network);
  - mode guard both directions;
  - httpx-missing directive (monkeypatch the lazy import);
  - unparseable-LLM-retry-then-partial-directive path.

Tmp roots only (Global Constraints: never touch the real home directory).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from deep_think_mcp import autopilot, server, store, subagent_engine
from deep_think_mcp.necort_adapter import NECoRTResult
from deep_think_mcp.session import SpecialistRound, UtilityScore

_DIMS = (
    "correctness", "evidence", "novelty", "clarity",
    "bias_resistance", "actionability", "coverage",
)


def _score_json(v: float) -> str:
    return json.dumps({d: v for d in _DIMS})


# ---------------------------------------------------------------------------
# In-process, phase-aware OpenAI-compatible mock endpoint (loopback only).
#
# Phase is detected from the request's SYSTEM message, which each autopilot
# prompt builder stamps with a stable, unique sentence. The mock answers each
# phase from a caller-supplied script; score/refine/specialist scripts are
# indexed by per-phase call count so a multi-round run can be driven precisely.
# ---------------------------------------------------------------------------

_PHASE_MARKERS = {
    "draft": "drafting the initial thought",
    "critique": "applying a single critique lens",
    "refine": "revising a draft thought to address",
    "score": "scoring a thought on seven utility dimensions",
    "specialist": "voicing one specialist perspective",
}


class _MockLLM:
    """Loopback OpenAI-compatible chat endpoint answering each autopilot phase
    from a script. `responder(phase, call_index, body) -> content` is fully
    overridable; the default returns fixed text per phase."""

    def __init__(self, responder: Callable[[str, int, dict], str]) -> None:
        self._responder = responder
        self.bodies: list[dict] = []
        self.phase_counts: dict[str, int] = {}
        self._lock = threading.Lock()
        server_self = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 (stdlib name)
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                try:
                    body = json.loads(raw)
                except json.JSONDecodeError:
                    body = {}
                content = server_self._answer(body)
                payload = json.dumps(
                    {"choices": [{"message": {"content": content}}]}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):  # silence access log
                pass

        self._httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def _detect_phase(self, body: dict) -> str:
        systems = " ".join(
            str(m.get("content", ""))
            for m in body.get("messages", [])
            if m.get("role") == "system"
        )
        for phase, marker in _PHASE_MARKERS.items():
            if marker in systems:
                return phase
        return "unknown"

    def _answer(self, body: dict) -> str:
        with self._lock:
            self.bodies.append(body)
            phase = self._detect_phase(body)
            idx = self.phase_counts.get(phase, 0)
            self.phase_counts[phase] = idx + 1
        return self._responder(phase, idx, body)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def __enter__(self) -> "_MockLLM":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


def _write_autopilot_config(tmp_path, *, enabled: bool, endpoint: str = "") -> None:
    """Write a config.toml enabling/disabling autopilot BEFORE create_server
    reads it. Only the [autopilot] section is written; config.load_config deep-
    merges it over the packaged defaults, so every other section is preserved."""
    (tmp_path / "config.toml").write_text(
        "[autopilot]\n"
        f"enabled = {str(enabled).lower()}\n"
        f'endpoint = "{endpoint}"\n'
        'model = "mock-model"\n'
        "temperature = 0.0\n"
    )


async def _call(client, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    result = await client.call_tool(name, arguments or {})
    assert not result.isError, result.content
    assert result.structuredContent is not None
    return result.structuredContent


async def _tool_names(client) -> set[str]:
    tools = await client.list_tools()
    return {t.name for t in tools.tools}


# ===========================================================================
# Tool visibility on/off
# ===========================================================================


async def test_autopilot_tools_absent_when_disabled(tmp_path):
    # Default config (no config.toml) -> [autopilot].enabled=false.
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        names = await _tool_names(client)
    assert "run_stage_autopilot" not in names
    assert "run_subagent_autopilot" not in names
    # sanity: the always-on tools are still there
    assert "start_session" in names


async def test_autopilot_tools_present_when_enabled(tmp_path):
    _write_autopilot_config(tmp_path, enabled=True, endpoint="http://unused.invalid/v1")
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        names = await _tool_names(client)
    assert "run_stage_autopilot" in names
    assert "run_subagent_autopilot" in names


# ===========================================================================
# Full serial run_stage_autopilot: >=2 rounds -> converge (diminishing
# returns) -> commit; thoughts persisted + convergence honored.
# ===========================================================================

_REFINE_TEXTS = [
    "Revision one reframes the claim around measurable outcomes and names two concrete sources.",
    "Revision two narrows the scope to the core mechanism and drops the speculative tangent entirely.",
    "Revision three adds a counterexample and qualifies the conclusion with explicit boundary conditions.",
]
# Overall means 0.60, 0.61, 0.62 -> round0 improves, rounds 1&2 are flat
# (delta 0.01 < 0.05 threshold) -> two consecutive flat rounds converge via
# the diminishing_returns rule (before max_rounds=3 would trip the cap).
_SCORE_VALUES = [0.60, 0.61, 0.62]


def _stage_responder(phase: str, idx: int, body: dict) -> str:
    if phase == "critique":
        return "The draft overstates confidence and leans on an unstated assumption."
    if phase == "refine":
        return _REFINE_TEXTS[min(idx, len(_REFINE_TEXTS) - 1)]
    if phase == "score":
        return _score_json(_SCORE_VALUES[min(idx, len(_SCORE_VALUES) - 1)])
    if phase == "draft":
        return "An initial draft answer that stakes out a clear but improvable position."
    return "UNKNOWN_PHASE"


async def test_run_stage_autopilot_drives_serial_loop_to_commit(tmp_path):
    with _MockLLM(_stage_responder) as mock:
        _write_autopilot_config(tmp_path, enabled=True, endpoint=mock.base)
        srv = server.create_server(root=tmp_path)
        async with create_connected_server_and_client_session(srv) as client:
            started = await _call(
                client, "start_session", {"question": "How to reduce churn?", "mode": "serial"}
            )
            sid = started["session_id"]

            payload = await _call(
                client,
                "run_stage_autopilot",
                {"session_id": sid, "initial_content": "Churn is mostly about onboarding."},
            )

    # Convergence honored: committed via the diminishing_returns rule, 3 rounds.
    assert payload["committed"] is True
    assert payload["rounds"] == 3
    assert payload["converged_reason"] == "diminishing_returns"
    assert payload["next_tool"] == "next_action"
    assert payload["final_content"] == _REFINE_TEXTS[2]

    # Every committed thought persists exactly as the manual path does.
    session = store.load(store.session_path(tmp_path, sid))
    assert len(session.thoughts) == 1
    t = session.thoughts[0]
    assert t.committed is True
    assert t.content == _REFINE_TEXTS[2]
    assert len(t.critique_rounds) == 3
    assert all(r.critique_text and r.refined_content for r in t.critique_rounds)
    assert t.final_utility_scores is not None
    assert session.current_thought_id is None  # cursor cleared on commit

    # No draft call (initial_content supplied): 3 rounds x (critique+refine+score).
    assert mock.phase_counts.get("draft", 0) == 0
    assert len(mock.bodies) == 9
    for body in mock.bodies:
        assert body.get("stream") is False
        assert "reasoning" not in body
        assert body["model"] == "mock-model"


async def test_run_stage_autopilot_drafts_when_no_initial_content(tmp_path):
    with _MockLLM(_stage_responder) as mock:
        _write_autopilot_config(tmp_path, enabled=True, endpoint=mock.base)
        srv = server.create_server(root=tmp_path)
        async with create_connected_server_and_client_session(srv) as client:
            started = await _call(client, "start_session", {"question": "q?", "mode": "serial"})
            sid = started["session_id"]
            payload = await _call(client, "run_stage_autopilot", {"session_id": sid})

    assert payload["committed"] is True
    assert mock.phase_counts.get("draft", 0) == 1  # server drafted via the LLM
    session = store.load(store.session_path(tmp_path, sid))
    # The LLM-drafted content seeded round 0's refinement history.
    assert session.thoughts[0].critique_rounds


# ===========================================================================
# Unparseable LLM scores -> bounded retries -> partial-progress directive
# (never a traceback, never an infinite loop). Progress so far is persisted.
# ===========================================================================


def _garbage_score_responder(phase: str, idx: int, body: dict) -> str:
    if phase == "critique":
        return "A fair critique of the draft."
    if phase == "refine":
        return "A genuinely refined and materially different version of the thought."
    if phase == "score":
        return "Honestly the reasoning looks pretty solid to me overall."  # not parseable
    return "UNKNOWN_PHASE"


async def test_run_stage_autopilot_unparseable_scores_yield_partial_directive(tmp_path):
    with _MockLLM(_garbage_score_responder) as mock:
        _write_autopilot_config(tmp_path, enabled=True, endpoint=mock.base)
        srv = server.create_server(root=tmp_path)
        async with create_connected_server_and_client_session(srv) as client:
            started = await _call(client, "start_session", {"question": "q?", "mode": "serial"})
            sid = started["session_id"]
            payload = await _call(
                client,
                "run_stage_autopilot",
                {"session_id": sid, "initial_content": "A seed thought to refine."},
            )

    # Partial-progress directive, not a traceback.
    assert payload["error"] == "autopilot_incomplete"
    assert payload["stopped_phase"] == "score"
    assert payload["next_tool"] == "next_action"
    # It got through the draft/critique/refine steps before the score wall.
    assert "draft" in payload["completed_steps"]
    assert any("critiqued" in s for s in payload["completed_steps"])
    assert any("refined" in s for s in payload["completed_steps"])

    # Bounded retries: exactly (1 initial + _MAX_PARSE_RETRIES) score attempts,
    # plus one critique and one refine = no infinite loop.
    expected = 1 + 1 + (autopilot._MAX_PARSE_RETRIES + 1)
    assert len(mock.bodies) == expected
    assert mock.phase_counts["score"] == autopilot._MAX_PARSE_RETRIES + 1

    # Everything committed so far is persisted -> resumable manually.
    session = store.load(store.session_path(tmp_path, sid))
    t = session.thoughts[0]
    assert t.committed is False
    assert session.current_thought_id == t.id
    rnd = t.critique_rounds[-1]
    assert rnd.critique_text and rnd.refined_content  # ready for score_current_thought


# ===========================================================================
# run_subagent_autopilot: manual engine mode end-to-end (server plays the
# specialists against the endpoint via the SAME manual_engine functions).
# ===========================================================================

_SPECIALIST_TEXTS = ["Analysis specialist candidate.", "Creativity specialist candidate."]


def _manual_responder(phase: str, idx: int, body: dict) -> str:
    if phase == "specialist":
        return _SPECIALIST_TEXTS[min(idx, len(_SPECIALIST_TEXTS) - 1)]
    if phase == "score":
        # specialist 0 strong (mean 0.9 >= 0.75 gate), specialist 1 weak.
        return _score_json(0.9 if idx == 0 else 0.4)
    return "UNKNOWN_PHASE"


async def test_run_subagent_autopilot_manual_engine_end_to_end(tmp_path):
    with _MockLLM(_manual_responder) as mock:
        _write_autopilot_config(tmp_path, enabled=True, endpoint=mock.base)
        srv = server.create_server(root=tmp_path)
        async with create_connected_server_and_client_session(srv) as client:
            started = await _call(
                client,
                "start_session",
                {
                    "question": "How should we design this?",
                    "mode": "subagent",
                    "overrides": {
                        "subagent": {
                            "engine": "manual",
                            "agents": ["Analysis", "Creativity"],
                            "equilibrium_threshold": 0.75,
                        }
                    },
                },
            )
            sid = started["session_id"]
            payload = await _call(client, "run_subagent_autopilot", {"session_id": sid})

    assert payload["committed"] is True
    assert payload["engine"] == "manual"
    assert payload["converged"] is True
    assert payload["gate_metric"] == "mean utility"
    assert payload["final_content"] == "Analysis specialist candidate."
    assert payload["next_tool"] == "next_action"

    session = store.load(store.session_path(tmp_path, sid))
    t = session.thoughts[0]
    assert t.committed is True
    assert t.content == "Analysis specialist candidate."
    assert sum(1 for r in t.specialist_rounds if r.was_selected) == 1
    assert session.current_thought_id is None
    # Two specialists each got a candidate + a score call = 4 endpoint calls.
    assert mock.phase_counts["specialist"] == 2
    assert mock.phase_counts["score"] == 2


# ===========================================================================
# run_subagent_autopilot: necort engine mode drives the adapter loop (fake
# adapter -> no vendored code, no network, no threads).
# ===========================================================================


def _uscore(v: float) -> UtilityScore:
    return UtilityScore(
        correctness=v, clarity=v, coverage=v,
        evidence=0.5, novelty=0.5, bias_resistance=0.5, actionability=0.5,
    )


def _fake_necort_result(*, strength: float, content: str) -> NECoRTResult:
    rounds: list[SpecialistRound] = []
    for idx in (0, 1):
        for a in range(2):
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
        response=content, specialist_rounds=rounds, final_utility_scores=_uscore(strength),
        converged=True, convergence_round=1, thinking_rounds=1, final_response_agent=0, raw={},
    )


class _FakeAdapter:
    def __init__(self, result: NECoRTResult) -> None:
        self._result = result

    async def run(self, user_input: str, max_rounds=None) -> NECoRTResult:
        return self._result


async def test_run_subagent_autopilot_necort_engine_commits(tmp_path, monkeypatch):
    result = _fake_necort_result(strength=0.9, content="the necort synthesis")
    monkeypatch.setattr(subagent_engine, "_make_adapter", lambda *a, **k: _FakeAdapter(result))

    # necort mode uses the [subagent] endpoint (via the fake adapter), NOT the
    # autopilot httpx client -- so the autopilot endpoint can be a dummy.
    _write_autopilot_config(tmp_path, enabled=True, endpoint="http://unused.invalid/v1")
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        started = await _call(
            client,
            "start_session",
            {
                "question": "q",
                "mode": "subagent",
                "overrides": {"subagent": {"engine": "necort", "endpoint": "http://fake-ep/v1"}},
            },
        )
        sid = started["session_id"]
        payload = await _call(client, "run_subagent_autopilot", {"session_id": sid})

    assert payload["committed"] is True
    assert payload["engine"] == "necort"
    assert payload["final_content"] == "the necort synthesis"

    session = store.load(store.session_path(tmp_path, sid))
    t = session.thoughts[0]
    assert t.committed is True
    assert t.content == "the necort synthesis"
    assert session.current_thought_id is None


async def test_run_subagent_autopilot_necort_no_endpoint_directs_to_manual(tmp_path):
    _write_autopilot_config(tmp_path, enabled=True, endpoint="http://unused.invalid/v1")
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        started = await _call(
            client,
            "start_session",
            {"question": "q", "mode": "subagent", "overrides": {"subagent": {"engine": "necort", "endpoint": ""}}},
        )
        sid = started["session_id"]
        directive = await _call(client, "run_subagent_autopilot", {"session_id": sid})

    assert directive["error"] == "no_endpoint"
    assert 'engine="manual"' in directive["message"]


# ===========================================================================
# Mode guard, both directions
# ===========================================================================


async def test_run_stage_autopilot_rejects_subagent_session(tmp_path):
    _write_autopilot_config(tmp_path, enabled=True, endpoint="http://unused.invalid/v1")
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        started = await _call(client, "start_session", {"question": "q", "mode": "subagent"})
        sid = started["session_id"]
        payload = await _call(client, "run_stage_autopilot", {"session_id": sid})
    assert payload["error"] == "wrong_mode"
    assert payload["required_mode"] == "serial"
    assert payload["current_mode"] == "subagent"
    assert payload["blocked_tool"] == "run_stage_autopilot"


async def test_run_subagent_autopilot_rejects_serial_session(tmp_path):
    _write_autopilot_config(tmp_path, enabled=True, endpoint="http://unused.invalid/v1")
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        started = await _call(client, "start_session", {"question": "q", "mode": "serial"})
        sid = started["session_id"]
        payload = await _call(client, "run_subagent_autopilot", {"session_id": sid})
    assert payload["error"] == "wrong_mode"
    assert payload["required_mode"] == "subagent"
    assert payload["current_mode"] == "serial"


async def test_autopilot_blocked_when_no_mode_set(tmp_path):
    _write_autopilot_config(tmp_path, enabled=True, endpoint="http://unused.invalid/v1")
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        started = await _call(client, "start_session", {"question": "q"})
        sid = started["session_id"]
        payload = await _call(client, "run_stage_autopilot", {"session_id": sid})
    assert payload["mode_required"] is True


# ===========================================================================
# httpx-missing directive (simulate via monkeypatching the lazy import)
# ===========================================================================


async def test_run_stage_autopilot_httpx_missing_directive(tmp_path, monkeypatch):
    _write_autopilot_config(tmp_path, enabled=True, endpoint="http://unused.invalid/v1")

    def _boom():
        raise autopilot.AutopilotHttpxMissing("httpx not installed")

    monkeypatch.setattr(autopilot, "_import_httpx", _boom)

    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        started = await _call(client, "start_session", {"question": "q", "mode": "serial"})
        sid = started["session_id"]
        payload = await _call(client, "run_stage_autopilot", {"session_id": sid})

    assert payload["error"] == "autopilot_unavailable"
    assert "httpx" in payload["message"]
    # Nothing was drafted -- the run never reached the endpoint.
    session = store.load(store.session_path(tmp_path, sid))
    assert session.thoughts == []


# ===========================================================================
# Module import stays clean when httpx is unavailable (lazy import contract).
# ===========================================================================


def test_autopilot_module_imports_without_httpx_at_top_level():
    import ast
    import inspect

    src = inspect.getsource(autopilot)
    tree = ast.parse(src)
    for node in tree.body:  # only module-level statements
        if isinstance(node, ast.Import):
            assert all(alias.name != "httpx" for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            assert node.module != "httpx"
