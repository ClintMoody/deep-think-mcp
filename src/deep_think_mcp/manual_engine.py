"""Layer 4 (subagent mode) -- the endpoint-free MANUAL specialist engine (M5).

Activated by config `[subagent] engine = "manual"`. This is subagent mode's
endpoint-free path, PROMOTED to first-class by the HYBRID DECISION
(`docs/execution-plan.md` Task 9): the vendored NECoRT core produces a single
blended Nash rating and cannot populate four of our seven utility dimensions,
so genuine specialist diversity comes from *the calling model playing each
specialist itself*. No endpoint, no network, no NECoRT import -- when
`engine="manual"` nothing in `necort_adapter.py`'s vendored loader ever runs.

How the round-trip fits the existing four-tool surface
------------------------------------------------------
The tool surface is unchanged (`begin` / `advance` / `inspect` / `commit`);
`engine="manual"` only changes what `begin`/`advance` DO. The server hands the
model one specialist's prompt at a time and the model answers one candidate at
a time, pipelined one step apart:

  - `begin` creates the thought and HANDS specialist #1's prompt (framing from
    config `agents` + `stages.agent_weight_for_stage` emphasis + stage +
    compressed prior context). It stores no candidate yet.
  - `advance(candidate, scores)` records the candidate + the model's own 7-dim
    self-scores for the specialist whose prompt was last handed, then HANDS the
    next specialist's prompt -- until every specialist in the roster has gone,
    at which point the server runs the deterministic selection and returns the
    round verdict.
  - `advance()` with NO candidate at a round boundary re-hands / starts the
    next round's specialist #1 prompt (this is how the model refines past a
    completed round: same cadence as `begin`).
  - `commit` / `inspect` are reused verbatim from `subagent_engine` (they read
    `selected_round` / `selected_strength`, which this engine populates the
    same way).

Round bookkeeping (no side-channel state)
-----------------------------------------
All specialists of US round *k* share `round_index == k` on
`Thought.specialist_rounds`. The round currently being built is always
`round_index == rounds_run(thought)` (rounds_run counts `was_selected`
candidates == completed rounds), and the number of specialists submitted so
far in it is simply how many rounds carry that index. When that count reaches
the roster size the round is CLOSED: exactly one candidate is marked
`was_selected` and `Thought.final_utility_scores` is set to its vector -- the
same shape `subagent_engine` leaves behind, so every downstream helper
(`rounds_run`, `selected_round`, `selected_strength`, `commit`, `inspect`,
`meta.next_action`) works unchanged. `subagent.max_rounds` caps the number of
US rounds; `equilibrium_threshold` is the commit gate, compared against the
winner's populated Nash dim (`correctness`) exactly as `subagent_engine` does.

The 7-dim asymmetry vs necort mode (documented deliberately)
------------------------------------------------------------
In `necort` mode only three dims carry real signal (correctness/clarity/
coverage, from the single blended Nash rating) and the other four are neutral
0.5 sentinels (see `necort_adapter.py`). In MANUAL mode the model CAN and DOES
score all seven dimensions for real -- so the deterministic selection here
ranks candidates by the **mean of all seven** submitted scores (highest wins,
ties -> the first/earliest specialist).

Per-engine commit-gate metric (T13 fix round 1)
-----------------------------------------------
Manual gates convergence on the SAME metric its selection uses: the 7-dim MEAN
(`overall_score`) vs `equilibrium_threshold`. This diverges DELIBERATELY from
necort, which gates on the winner's `correctness` dim -- necort's only real
signal, whose 7-dim mean is structurally capped at 0.714 and so could never
clear a 0.75 gate (see `subagent_engine.py`'s "Per-engine gate divergence"
note). Because manual scores all seven dims for real, its mean carries genuine
signal and can reach the threshold, and gating on it keeps "what we selected"
and "what we commit on" one consistent quantity. The verdict wording names the
metric honestly per engine ("winning candidate's mean utility X >= threshold Y"
for manual). Both engines resolve the metric through
`subagent_engine.strength_metric(cfg)` / `selected_strength(thought, metric)`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from deep_think_mcp import prompts, stages
from deep_think_mcp.serial_engine import DIMENSIONS, current_thought, overall_score
from deep_think_mcp.session import Session, SpecialistRound, Thought, UtilityScore
from deep_think_mcp.subagent_engine import (
    METRIC_MEAN,
    SubagentAdapterError,
    SubagentRoundResult,
    SubagentSequencingError,
    metric_label,
    rounds_run,
    selected_round,
    selected_strength,
)

# Neutral "no signal" value for a dimension the model omits -- matches
# serial_engine._DEFAULT_DIM / necort_adapter.NEUTRAL_DIM so every mode agrees
# on what an unscored dimension means.
NEUTRAL_DIM: float = 0.5

# equilibrium_state labels stamped onto each manual SpecialistRound. Reuse the
# necort labels for the winner/loser so `inspect_utility_matrix` reads the same
# for both engines; "pending" marks a candidate submitted into a round that has
# not been closed by selection yet.
MANUAL_PENDING = "pending"
MANUAL_IN = "in_equilibrium"
MANUAL_OUT = "off_equilibrium"


@dataclass
class ManualPrompt:
    """A specialist prompt handed to the calling model (which plays the
    specialist). `server.py` turns this into `prompts.manual_specialist_prompt`.
    Distinct from `SubagentRoundResult` (the round verdict) so the server can
    tell "here is the next specialist to voice" from "the round is complete."
    """

    thought_id: str
    us_round: int  # 1-based round being worked
    specialist_index: int  # 0-based position in the roster
    specialist_total: int
    specialist_name: str
    weight: float
    prompt_text: str
    rounds_run: int
    max_rounds: int


# ---------------------------------------------------------------------------
# Config + scoring helpers
# ---------------------------------------------------------------------------


def _roster(cfg: dict[str, Any]) -> list[str]:
    return [str(a) for a in (cfg.get("subagent", {}).get("agents") or [])]


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _utility_from_scores(raw: dict[str, Any]) -> UtilityScore:
    """Build a full 7-dim `UtilityScore` from the model's submitted self-scores.

    Keys are normalized (lowercased, '-'/' ' -> '_') so "Bias-resistance" and
    "bias_resistance" both land; values are coerced to float and clamped to
    [0, 1] (a weak model's out-of-range guess becomes a boundary value, never
    an error). A dimension the model omits defaults to a neutral 0.5. Unlike
    `serial_engine._merge_scores` there is no carry-forward: each manual
    specialist scores its own candidate from scratch.
    """
    normalized: dict[str, float] = {}
    for key, value in (raw or {}).items():
        canonical = str(key).strip().lower().replace("-", "_").replace(" ", "_")
        if canonical not in DIMENSIONS:
            continue
        try:
            normalized[canonical] = _clamp01(float(value))
        except (TypeError, ValueError):
            continue
    resolved = {dim: normalized.get(dim, NEUTRAL_DIM) for dim in DIMENSIONS}
    return UtilityScore(**resolved)


def _submitted_in_current_round(thought: Thought) -> int:
    """How many specialists have been recorded into the round currently being
    built (`round_index == rounds_run`). Zero at a round boundary (right after
    `begin`, or right after a round closed)."""
    current = rounds_run(thought)
    return sum(1 for r in thought.specialist_rounds if r.round_index == current)


# ---------------------------------------------------------------------------
# Prompt handing
# ---------------------------------------------------------------------------


def _hand_specialist(
    session: Session,
    thought: Thought,
    cfg: dict[str, Any],
    roster: list[str],
    *,
    spec_index: int,
    prompt_focus: str | None,
) -> ManualPrompt:
    """Build the ManualPrompt for `roster[spec_index]` in the current round.

    Every specialist in a round sees the same seed: the latest CLOSED round's
    winning candidate if one exists (so round k>1 refines round k-1's winner),
    otherwise the thought's original draft content. `prompt_focus` is only
    threaded through `begin`'s first specialist (there is no schema field to
    persist it for the rest of the round -- a documented, minor asymmetry).
    """
    # Lazy import breaks the meta <-> engine cycle (meta imports us).
    from deep_think_mcp import meta

    name = roster[spec_index]
    weight = stages.agent_weight_for_stage(session.current_stage, name)
    winner = selected_round(thought)
    seed = winner.candidate_content if winner is not None else (thought.content or None)
    prior_context = meta.compress_history(session).digest
    rr = rounds_run(thought)
    prompt_text = prompts.build_manual_specialist_prompt(
        question=session.question,
        stage=session.current_stage,
        prior_context=prior_context,
        seed_content=seed,
        prompt_focus=prompt_focus,
        specialist_name=name,
        framing=prompts.specialist_framing(name),
        weight=weight,
        specialist_index=spec_index,
        specialist_total=len(roster),
        round_num=rr + 1,
        # [F4] Weave the stage's critique-lens defaults in as specialist
        # scaffolding (build-plan.md:251).
        lenses=stages.lens_defaults_for_stage(session.current_stage),
    )
    return ManualPrompt(
        thought_id=thought.id,
        us_round=rr + 1,
        specialist_index=spec_index,
        specialist_total=len(roster),
        specialist_name=name,
        weight=weight,
        prompt_text=prompt_text,
        rounds_run=rr,
        max_rounds=int(cfg["subagent"]["max_rounds"]),
    )


# ---------------------------------------------------------------------------
# Selection + verdict
# ---------------------------------------------------------------------------


def _close_round(
    session: Session, thought: Thought, cfg: dict[str, Any], round_idx: int
) -> SubagentRoundResult:
    """Run the deterministic selection over the round's candidates and record
    the outcome. Highest MEAN of the 7 submitted utility dims wins; ties keep
    the first (earliest) specialist. Marks exactly one `was_selected`, sets the
    thought's `final_utility_scores`, and asserts the one-winner invariant
    (Task 13 hardening #5) before returning the round verdict.
    """
    group = [r for r in thought.specialist_rounds if r.round_index == round_idx]
    best = 0
    for i in range(1, len(group)):
        if overall_score(group[i].utility_vector) > overall_score(group[best].utility_vector):
            best = i
    for i, rnd in enumerate(group):
        rnd.was_selected = i == best
        rnd.equilibrium_state = MANUAL_IN if i == best else MANUAL_OUT
    thought.final_utility_scores = group[best].utility_vector

    winners = sum(1 for r in group if r.was_selected)
    if winners != 1:  # pragma: no cover - deterministic single argmax
        raise SubagentAdapterError(
            f"manual selection marked {winners} winners, expected exactly 1",
            retryable=False,
        )
    return _round_result(thought, cfg)


def _round_result(thought: Thought, cfg: dict[str, Any]) -> SubagentRoundResult:
    sub = cfg["subagent"]
    max_r = int(sub["max_rounds"])
    threshold = float(sub["equilibrium_threshold"])
    rr = rounds_run(thought)
    # Manual gates on the SAME metric its selection ranks by: the 7-dim MEAN
    # (all seven are real here), NOT correctness -- see the module docstring's
    # per-engine divergence note (T13 fix round 1).
    strength = selected_strength(thought, METRIC_MEAN) or 0.0
    winner = selected_round(thought)
    scores: dict[str, float] = {}
    if thought.final_utility_scores is not None:
        scores = {d: float(getattr(thought.final_utility_scores, d)) for d in DIMENSIONS}
    return SubagentRoundResult(
        thought_id=thought.id,
        us_round=rr,
        rounds_run=rr,
        max_rounds=max_r,
        selected_content=winner.candidate_content if winner else "",
        strength=strength,
        threshold=threshold,
        converged=strength >= threshold,
        budget_exhausted=rr >= max_r,
        endpoints_used=0,
        final_utility_scores=scores,
        metric_label=metric_label(METRIC_MEAN),
    )


# ---------------------------------------------------------------------------
# loop_state -- drives meta.next_action's manual-mode rows
# ---------------------------------------------------------------------------


def loop_state(session: Session, cfg: dict[str, Any]) -> str:
    """One of "no_thought" | "awaiting_specialist" | "converged" |
    "budget_exhausted" | "can_advance" for the session's current manual
    subagent thought. Mirrors `subagent_engine.loop_state` but adds
    "awaiting_specialist" -- the manual-only state where the model owes the
    current specialist's candidate.
    """
    thought = current_thought(session)
    if thought is None:
        return "no_thought"
    sub = cfg["subagent"]
    submitted = _submitted_in_current_round(thought)
    rr = rounds_run(thought)
    # Mid-round, or freshly begun (rr == 0, prompt #1 already handed): the
    # model owes a specialist candidate.
    if submitted > 0 or rr == 0:
        return "awaiting_specialist"
    # A round just closed (rr >= 1, none submitted into the next round yet).
    if rr >= int(sub["max_rounds"]):
        return "budget_exhausted"
    # Gate on the 7-dim MEAN (the metric selection uses), not correctness.
    strength = selected_strength(thought, METRIC_MEAN)
    if strength is not None and strength >= float(sub["equilibrium_threshold"]):
        return "converged"
    return "can_advance"


# ---------------------------------------------------------------------------
# The two operations that differ by engine (begin / advance). inspect + commit
# are reused verbatim from subagent_engine.
# ---------------------------------------------------------------------------


def begin(
    session: Session, content: str | None, prompt_focus: str | None, cfg: dict[str, Any]
) -> ManualPrompt:
    """Start a manual subagent thought and hand specialist #1's prompt.

    Raises `SubagentSequencingError("uncommitted_exists")` if a thought is
    already in progress, or `SubagentAdapterError` if no specialists are
    configured (a misconfiguration, not a transient fault).
    """
    if current_thought(session) is not None:
        raise SubagentSequencingError("uncommitted_exists")
    roster = _roster(cfg)
    if not roster:
        raise SubagentAdapterError(
            "no specialists configured ([subagent].agents is empty)", retryable=False
        )
    position = sum(
        1 for t in session.thoughts if t.stage == session.current_stage and t.committed
    )
    thought = Thought(stage=session.current_stage, position=position, content=content or "")
    session.thoughts.append(thought)
    session.current_thought_id = thought.id
    return _hand_specialist(session, thought, cfg, roster, spec_index=0, prompt_focus=prompt_focus)


def advance(
    session: Session,
    candidate: str | None,
    scores: dict[str, Any],
    cfg: dict[str, Any],
) -> ManualPrompt | SubagentRoundResult:
    """Record the current specialist's candidate + self-scores and hand the
    next specialist's prompt, or -- when the roster is exhausted -- run the
    deterministic selection and return the round verdict.

    `advance()` with no candidate at a round boundary (re)hands the next
    round's specialist #1 prompt (how the model refines past a completed
    round). Raises `SubagentSequencingError`: "begin_first" (no thought),
    "need_candidate" (mid-round with no candidate), "round_budget_exhausted"
    (US cap reached).
    """
    thought = current_thought(session)
    if thought is None:
        raise SubagentSequencingError("begin_first")
    roster = _roster(cfg)
    if not roster:
        raise SubagentAdapterError(
            "no specialists configured ([subagent].agents is empty)", retryable=False
        )

    max_r = int(cfg["subagent"]["max_rounds"])
    submitted = _submitted_in_current_round(thought)
    text = (candidate or "").strip()

    if not text:
        # No candidate: only meaningful as a "start / re-hand the next round"
        # request at a round boundary. Mid-round, the model owes a candidate.
        if submitted != 0:
            raise SubagentSequencingError("need_candidate", specialist=roster[submitted])
        if rounds_run(thought) >= max_r:
            raise SubagentSequencingError("round_budget_exhausted", max_rounds=max_r)
        return _hand_specialist(session, thought, cfg, roster, spec_index=0, prompt_focus=None)

    # A candidate is provided -> submit it for the pending specialist.
    if submitted == 0 and rounds_run(thought) >= max_r:
        # Would open a round beyond the US budget cap.
        raise SubagentSequencingError("round_budget_exhausted", max_rounds=max_r)

    round_idx = rounds_run(thought)  # the round currently being built
    name = roster[submitted]
    thought.specialist_rounds.append(
        SpecialistRound(
            round_index=round_idx,
            agent_role=name,
            candidate_content=candidate,
            utility_vector=_utility_from_scores(scores),
            equilibrium_state=MANUAL_PENDING,
            was_selected=False,
        )
    )
    submitted += 1
    if submitted < len(roster):
        return _hand_specialist(
            session, thought, cfg, roster, spec_index=submitted, prompt_focus=None
        )
    return _close_round(session, thought, cfg, round_idx)
