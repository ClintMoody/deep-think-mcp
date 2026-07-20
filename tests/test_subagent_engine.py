"""Unit tests for deep_think_mcp.subagent_engine's pure/engine logic (Task 11).

No filesystem, no MCP, no network, no vendored code: these drive the engine
against a MOCKED adapter (monkeypatching `subagent_engine._make_adapter`) over
plain `Session` objects -- the same convention `test_serial_engine.py` uses for
the serial engine. The MCP-level round trips (real tool calls, persistence,
mode-gate) live in `test_subagent_loop.py`; the real-vendored + loopback path
(and the stdout shim) lives in `test_necort_adapter.py`.
"""

from __future__ import annotations

import pytest

from deep_think_mcp import prompts, subagent_engine
from deep_think_mcp.necort_adapter import NECoRTResult
from deep_think_mcp.session import Session, SpecialistRound, UtilityScore
from deep_think_mcp.subagent_engine import MatrixState


# ---------------------------------------------------------------------------
# Fixtures: a subagent Session, a cfg dict, and a synthetic adapter result.
# ---------------------------------------------------------------------------


def _session(stages=None, current_stage=None):
    stages = stages or ["Problem Definition", "Research", "Analysis"]
    return Session(
        question="What is the best approach?",
        mode="subagent",
        expected_stages=stages,
        current_stage=current_stage or stages[0],
    )


def _cfg(*, max_rounds=2, threshold=0.75, endpoint="http://ep-a", endpoints=None, agents=None):
    return {
        "subagent": {
            "max_rounds": max_rounds,
            "equilibrium_threshold": threshold,
            "agents": agents or ["Analysis", "Creativity"],
            "sequential_fallback": True,
            "engine": "necort",
            "endpoint": endpoint,
            "endpoints": endpoints or [],
            "model": "m",
            "api_key": "",
            "timeout": 120.0,
        }
    }


def _uscore(v: float) -> UtilityScore:
    return UtilityScore(
        correctness=v, clarity=v, coverage=v,
        evidence=0.5, novelty=0.5, bias_resistance=0.5, actionability=0.5,
    )


def _fake_result(*, strength: float, content: str, n_agents: int = 2) -> NECoRTResult:
    """A NECoRTResult shaped like one capped (max_rounds=1) adapter run:
    round 0 (initial, neutral) + round 1 (rated), with agent 0 the winner."""
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
    def __init__(self, result: NECoRTResult, recorder: list | None, key: str) -> None:
        self._result = result
        self._recorder = recorder
        self._key = key

    async def run(self, user_input: str, max_rounds=None) -> NECoRTResult:
        if self._recorder is not None:
            self._recorder.append((self._key, user_input, max_rounds))
        return self._result


def _patch(monkeypatch, *, result=None, results_by_url=None, recorder=None, raises=None):
    """Monkeypatch the adapter factory. Either one shared `result`, or a
    per-endpoint map `results_by_url`, or an exception to raise on run."""

    def fake_make(base_url, cfg, agent_roles):
        if raises is not None:
            class _Boom:
                async def run(self, *a, **k):
                    raise raises
            return _Boom()
        res = results_by_url[base_url] if results_by_url else result
        return _FakeAdapter(res, recorder, key=base_url)

    monkeypatch.setattr(subagent_engine, "_make_adapter", fake_make)


# ---------------------------------------------------------------------------
# begin -> commit short path (must work with no intermediate tools)
# ---------------------------------------------------------------------------


async def test_begin_then_commit_short_path(monkeypatch):
    _patch(monkeypatch, result=_fake_result(strength=0.9, content="the synthesis"))
    session = _session()
    cfg = _cfg()

    result = await subagent_engine.begin(session, "seed draft", None, cfg)
    assert result.rounds_run == 1
    assert result.converged is True  # 0.9 >= 0.75
    assert session.current_thought_id is not None
    assert len(session.thoughts) == 1
    assert session.thoughts[0].specialist_rounds  # populated

    thought = subagent_engine.commit(session)
    assert thought.committed is True
    assert thought.content == "the synthesis"  # winning candidate written back
    assert session.current_thought_id is None


# ---------------------------------------------------------------------------
# full loop: begin -> inspect -> advance -> commit (mocked adapter)
# ---------------------------------------------------------------------------


async def test_full_loop_begin_inspect_advance_commit(monkeypatch):
    # Below-threshold winner so the loop wants to advance rather than commit.
    _patch(monkeypatch, result=_fake_result(strength=0.5, content="round-a best"))
    session = _session()
    cfg = _cfg()

    r1 = await subagent_engine.begin(session, "seed", "focus here", cfg)
    assert r1.converged is False  # 0.5 < 0.75
    assert subagent_engine.loop_state(session, cfg) == "can_advance"

    state = subagent_engine.inspect(session, cfg)
    assert state.rounds_run == 1
    assert state.selected_content == "round-a best"
    assert any(c["was_selected"] for c in state.candidates)

    # Advance re-seeds with the current best and runs another bounded round.
    _patch(monkeypatch, result=_fake_result(strength=0.5, content="round-b best"))
    r2 = await subagent_engine.advance(session, cfg)
    assert r2.rounds_run == 2
    # Rounds accumulated + re-indexed continuously across the two US rounds.
    idxs = sorted({r.round_index for r in session.thoughts[0].specialist_rounds})
    assert idxs == [0, 1, 2, 3]
    # Exactly two winners (one per US round) -> rounds_run derivation.
    assert sum(1 for r in session.thoughts[0].specialist_rounds if r.was_selected) == 2

    thought = subagent_engine.commit(session)
    assert thought.content == "round-b best"  # latest US round's winner


# ---------------------------------------------------------------------------
# round-cap enforcement (subagent.max_rounds=2, enforced by US)
# ---------------------------------------------------------------------------


async def test_round_cap_enforced_by_us(monkeypatch):
    recorder: list = []
    _patch(monkeypatch, result=_fake_result(strength=0.5, content="c"), recorder=recorder)
    session = _session()
    cfg = _cfg(max_rounds=2)

    await subagent_engine.begin(session, "seed", None, cfg)   # US round 1
    await subagent_engine.advance(session, cfg)               # US round 2
    assert len(recorder) == 2

    # A third advance must be refused by US even though the (mock) core would
    # happily keep going -- and must NOT invoke the adapter again.
    with pytest.raises(subagent_engine.SubagentSequencingError) as exc:
        await subagent_engine.advance(session, cfg)
    assert exc.value.code == "round_budget_exhausted"
    assert len(recorder) == 2  # no extra adapter call
    assert subagent_engine.loop_state(session, cfg) == "budget_exhausted"


async def test_max_rounds_passed_to_adapter_is_one(monkeypatch):
    recorder: list = []
    _patch(monkeypatch, result=_fake_result(strength=0.9, content="c"), recorder=recorder)
    session = _session()
    await subagent_engine.begin(session, "seed", None, _cfg())
    # Each US round drives the adapter with max_rounds=1 (single-round stepping).
    assert recorder[0][2] == 1


# ---------------------------------------------------------------------------
# sequential vs multi-endpoint dispatch
# ---------------------------------------------------------------------------


async def test_single_endpoint_runs_one_adapter(monkeypatch):
    recorder: list = []
    _patch(monkeypatch, result=_fake_result(strength=0.9, content="c"), recorder=recorder)
    session = _session()
    cfg = _cfg(endpoint="http://only-one", endpoints=[])

    result = await subagent_engine.begin(session, "seed", None, cfg)
    assert result.endpoints_used == 1
    assert {k for (k, _p, _m) in recorder} == {"http://only-one"}


async def test_multi_endpoint_dispatches_concurrently_and_selects_best(monkeypatch):
    recorder: list = []
    results = {
        "http://ep-1": _fake_result(strength=0.60, content="weaker"),
        "http://ep-2": _fake_result(strength=0.95, content="stronger"),
    }
    _patch(monkeypatch, results_by_url=results, recorder=recorder)
    session = _session()
    cfg = _cfg(endpoint="", endpoints=["http://ep-1", "http://ep-2"])

    result = await subagent_engine.begin(session, "seed", None, cfg)
    # Both endpoints were dispatched (concurrent alternatives).
    assert {k for (k, _p, _m) in recorder} == {"http://ep-1", "http://ep-2"}
    assert result.endpoints_used == 2
    # The strongest negotiation's winner is selected.
    assert result.selected_content == "stronger"
    assert result.strength == pytest.approx(0.95)


# ---------------------------------------------------------------------------
# no-endpoint directive + adapter-error discipline
# ---------------------------------------------------------------------------


async def test_no_endpoint_raises_directive(monkeypatch):
    # Never reaches the adapter -- factory should not even be called.
    session = _session()
    cfg = _cfg(endpoint="", endpoints=[])
    with pytest.raises(subagent_engine.SubagentSequencingError) as exc:
        await subagent_engine.begin(session, "seed", None, cfg)
    assert exc.value.code == "no_endpoint"
    assert session.thoughts == []  # nothing created


async def test_adapter_exception_becomes_typed_error(monkeypatch):
    _patch(monkeypatch, raises=ValueError("malformed 200 body"))
    session = _session()
    with pytest.raises(subagent_engine.SubagentAdapterError) as exc:
        await subagent_engine.begin(session, "seed", None, _cfg())
    assert exc.value.retryable is True
    assert session.thoughts == []  # failed begin left the session clean


# ---------------------------------------------------------------------------
# sequencing guards
# ---------------------------------------------------------------------------


async def test_begin_rejects_when_thought_already_in_progress(monkeypatch):
    _patch(monkeypatch, result=_fake_result(strength=0.5, content="c"))
    session = _session()
    cfg = _cfg()
    await subagent_engine.begin(session, "seed", None, cfg)
    with pytest.raises(subagent_engine.SubagentSequencingError) as exc:
        await subagent_engine.begin(session, "again", None, cfg)
    assert exc.value.code == "uncommitted_exists"


async def test_advance_without_thought_says_begin_first():
    session = _session()
    with pytest.raises(subagent_engine.SubagentSequencingError) as exc:
        await subagent_engine.advance(session, _cfg())
    assert exc.value.code == "begin_first"


def test_commit_without_thought_says_begin_first():
    session = _session()
    with pytest.raises(subagent_engine.SubagentSequencingError) as exc:
        subagent_engine.commit(session)
    assert exc.value.code == "begin_first"


# ---------------------------------------------------------------------------
# loop_state transitions (drives next_action's subagent rows)
# ---------------------------------------------------------------------------


async def test_loop_state_no_thought_then_converged(monkeypatch):
    session = _session()
    cfg = _cfg()
    assert subagent_engine.loop_state(session, cfg) == "no_thought"
    _patch(monkeypatch, result=_fake_result(strength=0.9, content="c"))
    await subagent_engine.begin(session, "seed", None, cfg)
    assert subagent_engine.loop_state(session, cfg) == "converged"


# ---------------------------------------------------------------------------
# stage weighting reaches the prompt (Analysis emphasized in Analysis stage)
# ---------------------------------------------------------------------------


async def test_stage_weighting_injected_into_prompt(monkeypatch):
    recorder: list = []
    _patch(monkeypatch, result=_fake_result(strength=0.9, content="c"), recorder=recorder)
    session = _session(current_stage="Analysis")
    cfg = _cfg(agents=["Analysis", "Creativity"])
    await subagent_engine.begin(session, "seed", None, cfg)
    prompt = recorder[0][1]
    # The Analysis specialist is emphasized in the Analysis stage (weight 1.5).
    assert "Analysis" in prompt
    assert "1.5" in prompt


# ---------------------------------------------------------------------------
# F2 inspect_utility_matrix directive honesty at budget exhaustion
# ---------------------------------------------------------------------------


def _matrix_state(*, rounds_run, max_rounds, strength, threshold, converged):
    return MatrixState(
        thought_id="t1",
        us_round=rounds_run,
        rounds_run=rounds_run,
        max_rounds=max_rounds,
        selected_content="best so far",
        strength=strength,
        threshold=threshold,
        converged=converged,
    )


def test_subagent_matrix_commits_at_budget_exhaustion_below_threshold():
    """F2: inspect_utility_matrix must NOT name advance_subagent_round once the
    round budget is spent -- that tool refuses with round_budget_exhausted,
    contradicting next_action and the round verdict which both say commit."""
    state = _matrix_state(
        rounds_run=2, max_rounds=2, strength=0.60, threshold=0.75, converged=False
    )
    payload = prompts.subagent_matrix(_session(), state)
    assert payload["next_tool"] == "commit_subagent_thought"


def test_subagent_matrix_advances_when_budget_remains_below_threshold():
    state = _matrix_state(
        rounds_run=1, max_rounds=2, strength=0.60, threshold=0.75, converged=False
    )
    payload = prompts.subagent_matrix(_session(), state)
    assert payload["next_tool"] == "advance_subagent_round"


def test_subagent_matrix_commits_when_converged():
    state = _matrix_state(
        rounds_run=1, max_rounds=2, strength=0.90, threshold=0.75, converged=True
    )
    payload = prompts.subagent_matrix(_session(), state)
    assert payload["next_tool"] == "commit_subagent_thought"
