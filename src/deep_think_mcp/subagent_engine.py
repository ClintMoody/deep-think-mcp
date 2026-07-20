"""Layer 4 (subagent mode) -- the wrapped NECoRT Nash core behind our tool
surface (M3, per the HYBRID DECISION in `docs/execution-plan.md` Task 9).

This is the subagent-mode counterpart to `serial_engine.py`: session-in /
mutate-in-place / small-value-out, with `server.py` owning load/persist and
`prompts.py` owning every word of wording. The one structural difference is
that this engine's operations that talk to the Nash core are `async` -- the
T10 adapter's `run()` offloads the vendored blocking I/O onto a worker thread,
so these engine ops `await` it and the server registers the two tools that
call them as `async def` (see `server.py`'s minimal `mode_gate` async
extension).


Round-stepping vs. the adapter's one-shot reality (the key reconciliation)
--------------------------------------------------------------------------
The T10 adapter's `run()` is one-shot: one call runs a FULL Nash negotiation
(all internal rounds) and returns an equilibrium. The brief's begin / advance
round-stepping surface is reconciled with that HONESTLY -- we do NOT fake
per-round stepping inside a single Nash call. Instead:

  - `begin` runs the FIRST adapter call capped at `max_rounds=1` (the cheapest
    honest single-round Nash negotiation: initial candidates -> one utility
    matrix + equilibrium + improvement). That is US round 1.
  - `advance` runs ANOTHER bounded (`max_rounds=1`) adapter call whose prompt
    RE-SEEDS the prior US round's winning candidate (the vendored
    `think_and_respond` takes a prompt; embedding the current best in it is a
    genuine further round of refinement, not a replayed one). That is US round
    N+1.
  - `commit` accepts the current equilibrium.

Each adapter run marks exactly one `was_selected` candidate, so the count of
US rounds performed is simply the number of `was_selected` rounds accumulated
on the thought -- no side-channel counter needed. `subagent.max_rounds` (=2)
is the hard cap on that count, enforced HERE by US even if the Nash core would
keep going; a further `advance` past the cap returns a directive, never a
silent extra call.


Threshold semantics (config equilibrium_threshold vs adapter convergence)
-------------------------------------------------------------------------
These are two DIFFERENT quantities and are wired separately (see the T10
adapter docstring / report):

  - The adapter's `convergence_threshold` is the vendored core's matrix-diff
    epsilon (mean-abs-diff of successive 0-10 rating matrices; smaller =
    stricter; default 0.05). We leave it at the adapter default -- and with
    `max_rounds=1` stepping the internal convergence check never even fires
    (it only runs for round_num > 1), so it is effectively inert per US round.
  - Config `[subagent].equilibrium_threshold` (0.75) is OUR OWN commit-gate
    criterion. It is compared against the winning candidate's *populated* Nash
    dimension (correctness, which carries the real normalised peer-rating
    signal), i.e. "the winner's Nash peer rating >= 7.5/10", NOT the 7-dim
    overall mean. That mean structurally cannot exceed (3*1.0 + 4*0.5)/7 =
    0.714 -- four of the seven dims are unpopulatable neutral sentinels -- so a
    0.75 gate on the mean could NEVER pass and would be a broken gate. Gating
    the populated dim is the honest mapping. At/above threshold the engine
    reports the equilibrium "converged" (commit); below it (with budget
    remaining) it recommends `advance`.

Per-engine gate divergence (necort vs manual -- T13 fix round 1)
----------------------------------------------------------------
This correctness-dim gate is the NECORT gate. `manual_engine.py` (the
endpoint-free path where the calling model scores all 7 dims for real) gates on
the 7-dim MEAN instead -- the same metric its deterministic selection ranks by
-- because in manual mode the mean carries genuine signal and can reach 0.75.
Both engines resolve their metric through `strength_metric(cfg)` /
`selected_strength(thought, metric)` here, so `loop_state` / `_round_result` /
`inspect` (reused by manual) stay consistent, and the verdict wording names
each engine's own metric (`metric_label`). See `manual_engine.py`'s docstring.


Sequential vs multi-endpoint dispatch
-------------------------------------
`[subagent].endpoint` (single) / `endpoints` (list) resolve to a list of
OpenAI-compatible base URLs. With one endpoint a US round is one adapter run
(sequential -- `sequential_fallback`; identical semantics, longer wall-clock).
With several, a US round fans one FULL bounded Nash negotiation out to EACH
endpoint concurrently (`asyncio.gather`) and keeps the strongest result --
"alternatives generated concurrently". Each adapter builds a fresh chat
against its own base_url, so the concurrent runs share no mutable vendored
state.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from deep_think_mcp import config, prompts, stages
from deep_think_mcp.necort_adapter import NECoRTAdapter, NECoRTResult, NECoRTUnavailable
from deep_think_mcp.serial_engine import DIMENSIONS, current_thought, overall_score
from deep_think_mcp.session import Session, SpecialistRound, Thought, UtilityScore

# The populated dimension that carries the real Nash peer-rating signal (see
# the module docstring's threshold section). correctness == clarity == coverage
# by construction; we read correctness as the representative.
_STRENGTH_DIM = "correctness"


# ---------------------------------------------------------------------------
# Per-engine commit-gate metric (T13 fix round 1)
# ---------------------------------------------------------------------------
# necort and manual gate convergence on DIFFERENT metrics, on purpose:
#
#   - necort: the winner's `correctness` dim -- the ONLY real Nash signal. The
#     7-dim mean is structurally capped at (3*1.0 + 4*0.5)/7 == 0.714 (four
#     dims are unpopulatable 0.5 sentinels; see necort_adapter.py), so a 0.75
#     gate on the mean could never pass -- correctness is the honest metric.
#   - manual: the winner's 7-dim MEAN (overall_score) -- the SAME metric the
#     deterministic selection ranks by. In manual mode the model scores all
#     seven dims for real, so the mean carries genuine signal and can reach the
#     threshold; gating on it keeps "what we selected" and "what we commit on"
#     one consistent quantity (T13 fix round 1 adjudication).
#
# `strength_metric(cfg)` resolves which to use from `[subagent].engine`;
# `selected_strength(thought, metric)` reads it. Keeping this in one place
# means `loop_state` / `_round_result` / `inspect` (and manual_engine, which
# reuses `inspect`/`commit`) all agree on the gate per engine.
METRIC_CORRECTNESS = "correctness"
METRIC_MEAN = "mean"

_METRIC_LABELS = {METRIC_CORRECTNESS: "peer rating", METRIC_MEAN: "mean utility"}


def strength_metric(cfg: dict[str, Any]) -> str:
    """The commit-gate metric for this session's subagent engine."""
    return METRIC_MEAN if cfg.get("subagent", {}).get("engine") == "manual" else METRIC_CORRECTNESS


def metric_label(metric: str) -> str:
    """Human-readable name of a gate metric, for the verdict wording."""
    return _METRIC_LABELS.get(metric, "rating")


def _strength_of(score: UtilityScore, metric: str) -> float:
    """The gate value of a `UtilityScore` under `metric`: the 7-dim mean for
    manual, the populated `correctness` dim for necort."""
    if metric == METRIC_MEAN:
        return overall_score(score)
    return float(getattr(score, _STRENGTH_DIM))


class SubagentSequencingError(Exception):
    """A subagent tool was called out of order (or the round budget is spent,
    or no endpoint is configured). Carries a machine `code` (+ optional
    `detail`) the server maps to a directive via `prompts.subagent_directive`.
    Never surfaced as a raw error -- like `serial_engine.SerialSequencingError`.
    """

    def __init__(self, code: str, **detail: Any) -> None:
        self.code = code
        self.detail = detail
        super().__init__(code)


class SubagentAdapterError(Exception):
    """A NECoRT adapter call failed (network error, malformed 200 body ->
    KeyError/TypeError, or the vendored core is unavailable). The server maps
    this to `prompts.subagent_adapter_error` -- a directive, never a traceback
    (T11 hard contract #3). `retryable=False` means "misconfigured/unavailable,
    retrying won't help"; `True` means "transient, retry is reasonable".
    """

    def __init__(self, detail: str, *, retryable: bool = True) -> None:
        self.detail = detail
        self.retryable = retryable
        super().__init__(detail)


# ---------------------------------------------------------------------------
# Small result records returned to server.py (turned into wording there)
# ---------------------------------------------------------------------------


@dataclass
class SubagentRoundResult:
    thought_id: str
    us_round: int
    rounds_run: int
    max_rounds: int
    selected_content: str
    strength: float
    threshold: float
    converged: bool
    budget_exhausted: bool
    endpoints_used: int
    final_utility_scores: dict[str, float] = field(default_factory=dict)
    # The gate metric this verdict's `strength` is measured in ("peer rating"
    # for necort, "mean utility" for manual) -- so the wording names it honestly.
    metric_label: str = "peer rating"


@dataclass
class MatrixState:
    thought_id: str
    us_round: int
    rounds_run: int
    max_rounds: int
    selected_content: str
    strength: float
    threshold: float
    converged: bool
    candidates: list[dict[str, Any]] = field(default_factory=list)
    metric_label: str = "peer rating"


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


def endpoints_from_cfg(cfg: dict[str, Any]) -> list[str]:
    """Resolve the configured NECoRT endpoint(s). `endpoints` (list) wins over
    `endpoint` (single) when both are set; blank entries are dropped. Empty
    result == no endpoint configured (-> the manual-path directive).
    """
    sub = cfg.get("subagent", {})
    multi = [str(e).strip() for e in (sub.get("endpoints") or []) if str(e).strip()]
    if multi:
        return multi
    single = str(sub.get("endpoint", "") or "").strip()
    return [single] if single else []


def _agent_roster(cfg: dict[str, Any]) -> list[str]:
    return [str(a) for a in (cfg.get("subagent", {}).get("agents") or [])]


def _make_adapter(base_url: str, cfg: dict[str, Any], agent_roles: list[str]) -> NECoRTAdapter:
    """Build a NECoRT adapter for one endpoint. Isolated so tests can
    monkeypatch it with a mock (no vendored code / network in unit tests).
    Raises `NECoRTUnavailable` if the vendored core can't be loaded.
    """
    sub = cfg.get("subagent", {})
    api_key = str(sub.get("api_key", "") or "") or None
    # [F7 SECURITY] The operator's api_key travels ONLY to operator-configured
    # endpoints. If `base_url` is here only because a per-session override
    # redirected the endpoint, run keyless -- never leak the operator's
    # credential to a caller-chosen URL.
    if api_key is not None and not config.api_key_allowed_for(cfg, "subagent", base_url):
        api_key = None
    return NECoRTAdapter(
        base_url=base_url,
        model=str(sub.get("model", "") or ""),
        api_key=api_key,
        num_agents=max(1, len(agent_roles)),
        agent_roles=agent_roles or None,
        timeout=float(sub.get("timeout", 120.0)),
    )


# ---------------------------------------------------------------------------
# Thought / round helpers -- the derived-state checks (mirror serial_engine's)
# ---------------------------------------------------------------------------


def rounds_run(thought: Thought) -> int:
    """US rounds performed on this thought == the number of `was_selected`
    candidates accumulated (each adapter run marks exactly one winner)."""
    return sum(1 for r in thought.specialist_rounds if r.was_selected)


def selected_round(thought: Thought) -> SpecialistRound | None:
    """The current best candidate: the `was_selected` round with the highest
    round_index (i.e. the latest US round's winner)."""
    winners = [r for r in thought.specialist_rounds if r.was_selected]
    if not winners:
        return None
    return max(winners, key=lambda r: r.round_index)


def selected_strength(thought: Thought, metric: str = METRIC_CORRECTNESS) -> float | None:
    """The winning candidate's gate value under `metric` -- the quantity the
    commit-gate `equilibrium_threshold` is compared against. `metric` is
    `correctness` for necort (its only real signal) and `mean` for manual (the
    7-dim mean it ranks selection by); see the per-engine gate note above."""
    if thought.final_utility_scores is not None:
        return _strength_of(thought.final_utility_scores, metric)
    winner = selected_round(thought)
    if winner is None:
        return None
    return _strength_of(winner.utility_vector, metric)


def loop_state(session: Session, cfg: dict[str, Any]) -> str:
    """One of: "no_thought" | "converged" | "budget_exhausted" | "can_advance"
    for the session's current in-progress subagent thought. Drives
    `meta.next_action`'s subagent rows (mirrors `serial_engine.loop_phase`).
    """
    thought = current_thought(session)
    if thought is None:
        return "no_thought"
    if not thought.specialist_rounds:
        return "can_advance"  # in-flight but no Nash round yet (defensive)
    sub = cfg["subagent"]
    if rounds_run(thought) >= int(sub["max_rounds"]):
        return "budget_exhausted"
    strength = selected_strength(thought, strength_metric(cfg))
    if strength is not None and strength >= float(sub["equilibrium_threshold"]):
        return "converged"
    return "can_advance"


# ---------------------------------------------------------------------------
# Dispatch: build the prompt, run the adapter(s), translate + accumulate
# ---------------------------------------------------------------------------


def _build_prompt(
    session: Session,
    content: str | None,
    prompt_focus: str | None,
    cfg: dict[str, Any],
    agents: list[str],
) -> str:
    """Construct the Nash invocation string: compressed prior-stage context
    (reusing the meta machinery), the current stage, the seed content/focus,
    and the specialist framings with stage weighting injected."""
    # Lazy import breaks the meta <-> subagent_engine cycle (meta.next_action
    # imports us; we only need meta's extractive digest here, at call time).
    from deep_think_mcp import meta

    prior_context = meta.compress_history(session).digest
    framings = [
        {
            "name": name,
            "framing": prompts.specialist_framing(name),
            "weight": stages.agent_weight_for_stage(session.current_stage, name),
        }
        for name in agents
    ]
    return prompts.build_subagent_prompt(
        question=session.question,
        stage=session.current_stage,
        prior_context=prior_context,
        content=content,
        prompt_focus=prompt_focus,
        framings=framings,
    )


def _select_best(results: list[NECoRTResult]) -> NECoRTResult:
    """The strongest of several concurrent Nash negotiations: highest winning
    candidate strength (populated dim). Ties keep the first (stable)."""
    return max(
        results,
        key=lambda r: (
            float(getattr(r.final_utility_scores, _STRENGTH_DIM))
            if r.final_utility_scores is not None
            else 0.0
        ),
    )


async def _dispatch(
    prompt: str, endpoints: list[str], cfg: dict[str, Any], agents: list[str]
) -> tuple[NECoRTResult, int]:
    """Run one US Nash round: one adapter per endpoint (concurrently when
    several), capped at `max_rounds=1`. Returns (best_result, endpoints_used).
    Any adapter/vendored failure is converted to `SubagentAdapterError`.
    """
    try:
        adapters = [_make_adapter(url, cfg, agents) for url in endpoints]
    except NECoRTUnavailable as exc:
        raise SubagentAdapterError(str(exc), retryable=False) from exc

    try:
        if len(adapters) == 1:
            results = [await adapters[0].run(prompt, max_rounds=1)]
        else:
            results = list(
                await asyncio.gather(*(a.run(prompt, max_rounds=1) for a in adapters))
            )
    except NECoRTUnavailable as exc:  # pragma: no cover - construction usually catches it
        raise SubagentAdapterError(str(exc), retryable=False) from exc
    except Exception as exc:  # noqa: BLE001 - network/malformed-body/etc. -> directive
        raise SubagentAdapterError(f"{type(exc).__name__}: {exc}", retryable=True) from exc

    return _select_best(results), len(results)


def _append_rounds(thought: Thought, result: NECoRTResult) -> None:
    """Append a US round's specialist rounds to the thought, re-indexing their
    round_index to continue monotonically after any existing rounds so the
    accumulated history reads as one continuous progression. Each appended
    run keeps its own single `was_selected` winner (used to count US rounds).
    """
    # [task 13 hardening #5] Assert the exactly-one-winner invariant at the
    # engine boundary, BEFORE mutating the thought. US round bookkeeping
    # (`rounds_run`, `selected_round`, `selected_strength`, `commit`) all
    # assume each adapter run marks exactly one `was_selected` candidate; a
    # malformed translation (0 or >1 winners -- e.g. a `final_response_agent`
    # index that never lands, or duplicated selection) would silently corrupt
    # the round count and the committed content. Checking the batch before it
    # touches the thought means a violation raises a clean directive
    # (SubagentAdapterError -> not-a-traceback) and leaves the session
    # untouched, rather than persisting a broken equilibrium.
    winners = sum(1 for r in result.specialist_rounds if r.was_selected)
    if winners != 1:
        raise SubagentAdapterError(
            f"equilibrium selected {winners} winning candidates, expected "
            "exactly 1 -- the Nash result is malformed",
            retryable=False,
        )
    existing = thought.specialist_rounds
    offset = (max(r.round_index for r in existing) + 1) if existing else 0
    for rnd in result.specialist_rounds:
        thought.specialist_rounds.append(
            rnd.model_copy(update={"round_index": rnd.round_index + offset})
        )
    thought.final_utility_scores = result.final_utility_scores


def _round_result(
    thought: Thought, cfg: dict[str, Any], *, us_round: int, endpoints_used: int
) -> SubagentRoundResult:
    sub = cfg["subagent"]
    max_r = int(sub["max_rounds"])
    threshold = float(sub["equilibrium_threshold"])
    metric = strength_metric(cfg)
    rr = rounds_run(thought)
    strength = selected_strength(thought, metric) or 0.0
    winner = selected_round(thought)
    scores: dict[str, float] = {}
    if thought.final_utility_scores is not None:
        scores = {d: float(getattr(thought.final_utility_scores, d)) for d in DIMENSIONS}
    return SubagentRoundResult(
        thought_id=thought.id,
        us_round=us_round,
        rounds_run=rr,
        max_rounds=max_r,
        selected_content=winner.candidate_content if winner else "",
        strength=strength,
        threshold=threshold,
        converged=strength >= threshold,
        budget_exhausted=rr >= max_r,
        endpoints_used=endpoints_used,
        final_utility_scores=scores,
        metric_label=metric_label(metric),
    )


# ---------------------------------------------------------------------------
# The four operations. Each mutates `session` in place; the server persists.
# ---------------------------------------------------------------------------


async def begin(
    session: Session, content: str | None, prompt_focus: str | None, cfg: dict[str, Any]
) -> SubagentRoundResult:
    """Start a fresh subagent thought and run US round 1 (a single bounded Nash
    negotiation). The thought is only added to the session AFTER the adapter
    succeeds, so a failed begin leaves the session clean (retryable).

    Raises `SubagentSequencingError("uncommitted_exists")` if a thought is
    already in progress, `("no_endpoint")` if none is configured, or
    `SubagentAdapterError` on an adapter failure.
    """
    if current_thought(session) is not None:
        raise SubagentSequencingError("uncommitted_exists")
    endpoints = endpoints_from_cfg(cfg)
    if not endpoints:
        raise SubagentSequencingError("no_endpoint")

    agents = _agent_roster(cfg)
    prompt = _build_prompt(session, content, prompt_focus, cfg, agents)
    result, used = await _dispatch(prompt, endpoints, cfg, agents)

    position = sum(
        1 for t in session.thoughts if t.stage == session.current_stage and t.committed
    )
    thought = Thought(stage=session.current_stage, position=position, content=content or "")
    _append_rounds(thought, result)
    session.thoughts.append(thought)
    session.current_thought_id = thought.id
    return _round_result(thought, cfg, us_round=1, endpoints_used=used)


async def advance(session: Session, cfg: dict[str, Any]) -> SubagentRoundResult:
    """Run the next US Nash round, re-seeding the prior winner into the prompt.

    Raises `SubagentSequencingError`: "begin_first" (no thought in progress),
    "round_budget_exhausted" (US cap reached -- enforced even if the core would
    continue), "no_endpoint"; or `SubagentAdapterError` on adapter failure.
    """
    thought = current_thought(session)
    if thought is None:
        raise SubagentSequencingError("begin_first")

    max_r = int(cfg["subagent"]["max_rounds"])
    if rounds_run(thought) >= max_r:
        raise SubagentSequencingError("round_budget_exhausted", max_rounds=max_r)

    endpoints = endpoints_from_cfg(cfg)
    if not endpoints:
        raise SubagentSequencingError("no_endpoint")

    agents = _agent_roster(cfg)
    winner = selected_round(thought)
    reseed = winner.candidate_content if winner else (thought.content or "")
    prompt = _build_prompt(
        session,
        reseed,
        "Build on and improve the current best synthesis shown above; keep what "
        "is strong, fix what is weak.",
        cfg,
        agents,
    )
    result, used = await _dispatch(prompt, endpoints, cfg, agents)
    _append_rounds(thought, result)
    return _round_result(thought, cfg, us_round=rounds_run(thought), endpoints_used=used)


def inspect(session: Session, cfg: dict[str, Any]) -> MatrixState:
    """The current scoring state: the latest US round's per-candidate utility
    vectors, equilibrium states, and the selected winner.

    Raises `SubagentSequencingError`: "begin_first" / "no_rounds".
    """
    thought = current_thought(session)
    if thought is None:
        raise SubagentSequencingError("begin_first")
    if not thought.specialist_rounds:
        raise SubagentSequencingError("no_rounds")

    sub = cfg["subagent"]
    max_idx = max(r.round_index for r in thought.specialist_rounds)
    latest = [r for r in thought.specialist_rounds if r.round_index == max_idx]
    candidates = [
        {
            "agent_role": r.agent_role,
            "equilibrium_state": r.equilibrium_state,
            "was_selected": r.was_selected,
            "utility": {d: float(getattr(r.utility_vector, d)) for d in DIMENSIONS},
            "content": r.candidate_content,
        }
        for r in latest
    ]
    winner = selected_round(thought)
    metric = strength_metric(cfg)
    strength = selected_strength(thought, metric) or 0.0
    threshold = float(sub["equilibrium_threshold"])
    return MatrixState(
        thought_id=thought.id,
        us_round=rounds_run(thought),
        rounds_run=rounds_run(thought),
        max_rounds=int(sub["max_rounds"]),
        selected_content=winner.candidate_content if winner else "",
        strength=strength,
        threshold=threshold,
        converged=strength >= threshold,
        candidates=candidates,
        metric_label=metric_label(metric),
    )


def commit(session: Session) -> Thought:
    """Accept the current equilibrium: write the winning candidate back into
    `Thought.content`, mark it committed, clear the current-thought cursor.

    Raises `SubagentSequencingError`: "begin_first" / "no_rounds".
    """
    thought = current_thought(session)
    if thought is None:
        raise SubagentSequencingError("begin_first")
    winner = selected_round(thought)
    if winner is None:
        raise SubagentSequencingError("no_rounds")

    thought.content = winner.candidate_content
    thought.committed = True
    session.current_thought_id = None
    return thought
