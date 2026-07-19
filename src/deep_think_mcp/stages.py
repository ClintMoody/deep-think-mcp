"""Layer 3 -- stage machine: shared by both future engines (Tasks 7, 11).

This module owns:

  - Moving a session's stage cursor forward (`advance()`), the logic
    behind the `advance_stage` MCP tool `server.py` registers.
  - Two plain-data lookup tables the later engine tasks consume --
    serial-mode critique lens defaults per stage, and subagent-mode agent
    weight multipliers per stage -- plus one lookup function each. These
    are DATA, not engine logic: T7's serial engine decides what to *do*
    with a lens list, T11's subagent engine decides what a weight *means*
    to its equilibrium math. This module only answers "what applies to
    stage X", nothing more.

What this module deliberately does NOT own:

  - The *default* stage list (`Problem Definition, Research, Analysis,
    Synthesis, Conclusion`) is config-owned -- `[stages].default` in
    `config/default.toml`, already wired into `start_session` by Task 3
    (`expected_stages = list(stages) if stages else
    list(cfg["stages"]["default"])`). Duplicating that list here would
    just be a second source of truth to keep in sync; every function
    below works with whatever `session.expected_stages` already holds,
    default or custom, without caring which it is.
  - "Each stage can hold multiple committed thoughts"
    (`docs/build-plan.md` § "Stage progression") is already true of the
    schema Task 2 shipped -- `Thought.stage` + `Thought.position` -- no
    code here is needed to make it true; engines (T7/T11), not this
    module, are what actually create and commit thoughts.
"""

from __future__ import annotations

from deep_think_mcp.session import Session

# ---------------------------------------------------------------------------
# Cursor logic: advance_stage
# ---------------------------------------------------------------------------


class FinalStageReachedError(Exception):
    """Raised by `advance()` when `session` is already at the last stage in
    `session.expected_stages` -- there is nothing further to advance to.
    Carries the stage name so a caller building a directive payload
    doesn't have to re-derive it.
    """

    def __init__(self, stage: str) -> None:
        self.stage = stage
        super().__init__(f"'{stage}' is already the final stage.")


def is_final_stage(session: Session) -> bool:
    """True if `session.current_stage` is the last entry in
    `session.expected_stages`.
    """
    return session.current_stage == session.expected_stages[-1]


def advance(session: Session) -> Session:
    """Move `session.current_stage` to the next entry in
    `session.expected_stages`.

    Pure in-memory mutation -- caller persists (`store.save` +
    `index.upsert`), same pattern every other mutator in this repo follows
    (`lifecycle.finalize`, `lifecycle.keep_here`).

    Also clears `session.current_thought_id`. The cursor this module owns
    is "where in the stage machine are we", and that includes which
    thought is "current" -- a thought belongs to exactly one stage
    (`Thought.stage`), so a pointer into the *previous* stage's thought
    has no meaning once the cursor has moved on. No engine exists yet to
    populate this field (Tasks 7/11 build those), so today this is a
    no-op in practice; it's handled here so those engines inherit the
    correct behavior for free rather than every future call site having
    to remember to clear it itself.

    Raises `FinalStageReachedError` -- never silently no-ops, never a bare
    `ValueError`/`IndexError` -- if `session` is already at its last
    stage. Callers should route that to a directive payload pointing at
    `finalize_session` (see `prompts.final_stage_reached`), per
    `docs/execution-plan.md` Task 5's "cannot advance past the final
    stage" requirement.

    Assumes the invariant every mutator in this codebase that touches
    `current_stage`/`expected_stages` maintains: `current_stage` is always
    a member of `expected_stages`. A session that violates this (e.g. via
    hand-edited JSON) raises a plain `ValueError` from the underlying
    `list.index()` call rather than something more tolerant --
    `docs/execution-plan.md`'s "Tolerant input handling" constraint is
    scoped to malformed *tool input* from Task 13 (M5) onward, not
    corrupted on-disk state.
    """
    stage_list = session.expected_stages
    idx = stage_list.index(session.current_stage)
    if idx >= len(stage_list) - 1:
        raise FinalStageReachedError(session.current_stage)
    session.current_stage = stage_list[idx + 1]
    session.current_thought_id = None
    return session


# ---------------------------------------------------------------------------
# Stage-appropriate defaults: serial-mode critique lenses
#
# Analysis and Synthesis values are plan-mandated
# (docs/build-plan.md § "Stage progression"); Problem Definition, Research,
# and Conclusion are [derived] per docs/execution-plan.md Task 5.
# ---------------------------------------------------------------------------

SERIAL_LENS_DEFAULTS: dict[str, list[str]] = {
    "Problem Definition": ["unstated_assumption", "scope_creep"],
    "Research": ["weak_evidence", "missing_perspective"],
    "Analysis": ["weak_evidence", "overconfidence"],
    "Synthesis": ["missing_perspective", "unstated_assumption"],
    "Conclusion": ["steel_man", "overconfidence"],
}


def lens_defaults_for_stage(stage: str) -> list[str]:
    """Stage-appropriate critique lens defaults for serial mode (T7).

    Returns a fresh list (never the table's own internal list, so a
    caller mutating its result can't corrupt the table for the next
    lookup). Returns `[]` for any stage with no entry -- most importantly
    any custom stage name from `start_session(stages=[...])`, which this
    table has no opinion on. Per `docs/execution-plan.md` Task 7's "Lens
    rotation" note ("stage-appropriate defaults from T5, then remaining
    lenses in `default_lenses` order"), the serial engine is expected to
    treat an empty result as "no stage-specific head start" and fall back
    to config `[serial].default_lenses`'s full rotation, not to error.
    """
    return list(SERIAL_LENS_DEFAULTS.get(stage, []))


# ---------------------------------------------------------------------------
# Stage-appropriate defaults: subagent-mode agent weighting
# ---------------------------------------------------------------------------

NEUTRAL_AGENT_WEIGHT = 1.0
# [derived] docs/build-plan.md § "Stage progression" only gives an example
# ("Creativity weighted higher in Synthesis ... e.g.") with no concrete
# multiplier. 1.5x neutral is a deliberately modest emphasis -- T11, which
# decides what a weight *means* to its equilibrium math, can retune this
# single constant later without any schema change, since every caller only
# ever goes through agent_weight_for_stage() below.
EMPHASIZED_AGENT_WEIGHT = 1.5

SUBAGENT_STAGE_WEIGHTS: dict[str, dict[str, float]] = {
    "Analysis": {"Analysis": EMPHASIZED_AGENT_WEIGHT},
    "Synthesis": {"Creativity": EMPHASIZED_AGENT_WEIGHT},
}


def agent_weight_for_stage(stage: str, agent: str) -> float:
    """`agent`'s weight multiplier for `stage`: a plain lookup into
    `SUBAGENT_STAGE_WEIGHTS`, defaulting to `NEUTRAL_AGENT_WEIGHT` (1.0)
    for every stage/agent combination not explicitly emphasized above --
    which includes every custom stage name and every agent from config
    `[subagent].agents` beyond the two the plan names as examples.
    """
    return SUBAGENT_STAGE_WEIGHTS.get(stage, {}).get(agent, NEUTRAL_AGENT_WEIGHT)
