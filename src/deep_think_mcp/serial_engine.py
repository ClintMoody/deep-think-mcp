"""Layer 4 (serial mode) -- the critique-lens loop with convergence.

This is M2's core. `docs/build-plan.md` § "The serial loop -- one stage,
done well" describes one round as: draft, pick a stage-appropriate lens,
critique, refine, self-score, then converge or continue. This module owns
that logic; `server.py` only registers the six tools and persists around
these calls, and `prompts.py` owns every word of wording. The functions here
are session-in / mutate-in-place / small-value-out -- the same "engines are
pure-ish, callers persist" division `stages.py`/`lifecycle.py` already use.

State machine (controller resolution): the in-flight critique loop state
lives ON `Thought.critique_rounds` -- there is no parallel state container to
drift out of sync. A round is progressively filled across four tool calls:

    critique_current_thought  -> append round (lens set, rest placeholder)
    submit_critique           -> fill critique_text
    refine_current_thought    -> fill refined_content
    score_current_thought     -> fill delta_score, evaluate convergence

The phase of the tail round is *derived* from which fields are still at
their placeholder (see `_round_phase`). `delta_score` uses the sentinel
`UNSCORED` (-2.0, outside the real [-1, 1] delta range) to mark "not scored
yet"; every other placeholder is the empty string. This keeps `CritiqueRound`
exactly the five fields `docs/build-plan.md` § Data model mandates -- no
extra schema field -- while still giving an unambiguous per-round phase.

`Thought.content` holds the *current best* content: it starts as the raw
draft and each round's refined version is recorded in that round's
`refined_content`. `latest_content()` returns the newest refinement (or the
draft if none yet); `commit_thought` writes that final version back into
`Thought.content` so a committed thought's `content` is its final form and
`critique_rounds` is the full history.

Sequencing violations (refine before critique, score before refine, begin
while an uncommitted thought exists, commit with zero rounds, ...) raise
`SerialSequencingError`, which `server.py` maps to a directive payload
telling the model the exact next call. Per the local-model philosophy these
are never errors -- they are what makes the loop usable by a weak model.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Any

from deep_think_mcp import stages
from deep_think_mcp.session import CritiqueRound, Session, Thought, UtilityScore

# The seven utility dimensions, in the canonical order (Global Constraints).
DIMENSIONS: tuple[str, ...] = (
    "correctness",
    "evidence",
    "novelty",
    "clarity",
    "bias_resistance",
    "actionability",
    "coverage",
)

# delta_score sentinel for an in-flight (not-yet-scored) round. A real delta
# is overall(new) - overall(prev), both in [0, 1], so it lives in [-1, 1];
# -2.0 can never be a genuine score and so unambiguously means "unscored".
UNSCORED: float = -2.0

# Neutral value for a dimension the model never scores on the very first
# round (nothing earlier to carry forward from). On later rounds a missing
# dimension carries forward from the previous round instead.
_DEFAULT_DIM: float = 0.5

_DEFAULT_EPSILON: float = 0.05


class SerialSequencingError(Exception):
    """A serial tool was called out of order. Carries a machine `code` (and
    optional `detail`) the server maps to a directive payload via
    `prompts.serial_directive`. Never surfaced as a raw error to the model.
    """

    def __init__(self, code: str, **detail: Any) -> None:
        self.code = code
        self.detail = detail
        super().__init__(code)


# ---------------------------------------------------------------------------
# Small result records returned to server.py (which turns them into wording)
# ---------------------------------------------------------------------------


@dataclass
class CritiquePrompt:
    thought_id: str
    lens: str
    round_index: int
    draft_content: str
    lens_template: str


@dataclass
class ScoreResult:
    thought_id: str
    round_index: int
    scores: dict[str, float]
    overall: float
    delta: float
    converged: bool
    converged_reason: str | None


# ---------------------------------------------------------------------------
# Edit distance
# ---------------------------------------------------------------------------


def normalized_edit_distance(a: str, b: str) -> float:
    """Normalized edit distance in [0.0, 1.0]: 0.0 identical, 1.0 totally
    different. Implemented as `1 - SequenceMatcher(None, a, b).ratio()`
    (stdlib difflib -- no new dependency, per the brief).
    """
    return 1.0 - difflib.SequenceMatcher(None, a, b).ratio()


# ---------------------------------------------------------------------------
# Thought / round helpers -- the "small set of derived checks"
# ---------------------------------------------------------------------------


def current_thought(session: Session) -> Thought | None:
    """The session's in-flight (uncommitted) thought, or None.

    Addressed via `session.current_thought_id`; `commit_thought` and
    `advance_stage` both clear that id, so a non-None cursor pointing at an
    uncommitted thought is the single signal that a thought is in progress.
    """
    if session.current_thought_id is None:
        return None
    for thought in session.thoughts:
        if thought.id == session.current_thought_id and not thought.committed:
            return thought
    return None


def _round_phase(rnd: CritiqueRound) -> str:
    """Derive an in-flight round's phase from its placeholder fields."""
    if not rnd.critique_text:
        return "await_critique"
    if not rnd.refined_content:
        return "await_refine"
    if rnd.delta_score == UNSCORED:
        return "await_score"
    return "complete"


def _inflight_round(thought: Thought) -> CritiqueRound | None:
    """The tail round if it isn't yet complete, else None."""
    if thought.critique_rounds and _round_phase(thought.critique_rounds[-1]) != "complete":
        return thought.critique_rounds[-1]
    return None


def _completed_rounds(thought: Thought) -> list[CritiqueRound]:
    return [r for r in thought.critique_rounds if _round_phase(r) == "complete"]


# Session-level loop phase (Task 8's next_action() needs this): whether a
# thought is in progress at all, and if so, which of the four in-flight
# round steps it's waiting on, or whether its tail round is fully scored
# and ready for a convergence check. Composed entirely from the helpers
# above -- current_thought / _inflight_round / _round_phase -- so callers
# (meta.next_action) never re-derive what a round's state means; this is
# the one seam Task 8 is meant to call through instead.
LOOP_PHASES: tuple[str, ...] = (
    "no_thought",
    "zero_rounds",
    "await_critique",
    "await_refine",
    "await_score",
    "round_complete",
)


def loop_phase(session: Session) -> str:
    """One of `LOOP_PHASES` for `session`'s current in-progress thought (or
    "no_thought" if none is in progress).
    """
    thought = current_thought(session)
    if thought is None:
        return "no_thought"
    inflight = _inflight_round(thought)
    if inflight is not None:
        return _round_phase(inflight)  # "await_critique" | "await_refine" | "await_score"
    if not thought.critique_rounds:
        return "zero_rounds"
    return "round_complete"


def latest_content(thought: Thought) -> str:
    """The most recent refined content, or the raw draft if none yet."""
    for rnd in reversed(thought.critique_rounds):
        if rnd.refined_content:
            return rnd.refined_content
    return thought.content


def _prior_content_for(thought: Thought, target: CritiqueRound) -> str:
    """The content that existed just before `target`'s refinement: the newest
    refined content of any round before it, or the raw draft.
    """
    idx = thought.critique_rounds.index(target)
    for rnd in reversed(thought.critique_rounds[:idx]):
        if rnd.refined_content:
            return rnd.refined_content
    return thought.content


def overall_score(score: UtilityScore) -> float:
    """The mean of the 7 utility dimensions -- one scalar summary of a
    `UtilityScore`. Public (not `_`-prefixed): Task 8's meta tools
    (`summarize_session`/`compress_history`) reuse this exact definition
    for the score they display alongside each thought's extractive
    snippet, rather than re-deriving "overall" a second way.
    """
    return sum(getattr(score, dim) for dim in DIMENSIONS) / len(DIMENSIONS)


# ---------------------------------------------------------------------------
# Convergence -- the four rules from docs/build-plan.md § "The serial loop"
# ---------------------------------------------------------------------------


def evaluate_convergence(thought: Thought, cfg: dict[str, Any]) -> tuple[bool, str | None]:
    """Decide whether the loop has converged after the latest scored round.

    Returns `(converged, reason)`. Reasons, in precedence order:

      - "fixed_point": the latest refinement changed the content by less
        than `edit_distance_epsilon` -- a fixed point (rule 3).
      - "diminishing_returns": the two most recent rounds were both flat or
        dropped (delta < `score_threshold`) -- rule 2.
      - "max_rounds": the round count reached `max_rounds` -- rule 4, the
        "we'd have kept going but hit the ceiling" flag.
      - otherwise not converged: the latest round improved by >= threshold,
        or was a single (first) flat round -- continue with the next lens
        (rule 1).

    Natural convergence (fixed_point / diminishing_returns) outranks the
    max_rounds cap so the reported reason is the informative one whenever a
    real signal coincides with the ceiling.
    """
    serial = cfg["serial"]
    threshold = float(serial["score_threshold"])
    max_rounds = int(serial["max_rounds"])
    epsilon = float(serial.get("edit_distance_epsilon", _DEFAULT_EPSILON))

    completed = _completed_rounds(thought)
    if not completed:
        return False, None
    current = completed[-1]

    edit_distance = normalized_edit_distance(
        _prior_content_for(thought, current), current.refined_content
    )
    if edit_distance < epsilon:
        return True, "fixed_point"

    consecutive_flat = 0
    for rnd in reversed(completed):
        if rnd.delta_score < threshold:
            consecutive_flat += 1
        else:
            break
    if consecutive_flat >= 2:
        return True, "diminishing_returns"

    if len(completed) >= max_rounds:
        return True, "max_rounds"

    return False, None


# ---------------------------------------------------------------------------
# Lens rotation
# ---------------------------------------------------------------------------


def choose_lens(
    session: Session, thought: Thought, available: dict[str, str], cfg: dict[str, Any]
) -> str:
    """Pick the next critique lens for `thought`.

    Rotation (docs/execution-plan.md Task 7): the current stage's
    lens_defaults first, then the remaining `[serial].default_lenses` order,
    filtered to lenses that actually exist (`available`), skipping any lens
    already used on this thought. If every available lens has been used
    (rare -- `max_rounds` defaults to 3, well under the 8 bundled lenses),
    the rotation restarts from the top so the loop never stalls for lack of
    an unused lens.
    """
    serial = cfg["serial"]
    stage_defaults = stages.lens_defaults_for_stage(session.current_stage)
    config_order = list(serial.get("default_lenses", []))
    rotation = stage_defaults + [n for n in config_order if n not in stage_defaults]
    rotation = [name for name in rotation if name in available]
    # Fall back to alphabetical discovery order if config named nothing that
    # actually exists (e.g. a hand-trimmed default_lenses).
    if not rotation:
        rotation = sorted(available)

    used = {r.lens for r in thought.critique_rounds}
    for name in rotation:
        if name not in used:
            return name
    return rotation[0]


# ---------------------------------------------------------------------------
# The six operations. Each mutates `session` in place; the server persists.
# ---------------------------------------------------------------------------


def begin_thought(
    session: Session,
    content: str,
    tags: list[str] | None = None,
    axioms: list[str] | None = None,
) -> Thought:
    """Start a fresh draft thought in the current stage.

    Raises `SerialSequencingError("uncommitted_exists")` if a thought is
    already in progress -- the model must commit (or keep refining) it first.
    """
    if current_thought(session) is not None:
        raise SerialSequencingError("uncommitted_exists")

    position = sum(
        1
        for t in session.thoughts
        if t.stage == session.current_stage and t.committed
    )
    thought = Thought(
        stage=session.current_stage,
        position=position,
        content=content,
        tags=list(tags or []),
        axioms=list(axioms or []),
    )
    session.thoughts.append(thought)
    session.current_thought_id = thought.id
    return thought


def start_critique(
    session: Session,
    lens: str | None,
    available: dict[str, str],
    cfg: dict[str, Any],
) -> CritiquePrompt:
    """Open a critique round and return the chosen lens's template.

    Server picks a stage-appropriate lens when `lens` is omitted. If the
    tail round is still awaiting its critique text (the model re-asked for
    the template), that same round is reused rather than stacking a
    duplicate. Any further-along in-flight round is a sequencing violation
    -- the model must finish it first.

    Raises `SerialSequencingError`:
      - "begin_first" if there is no thought in progress;
      - "unknown_lens" if a named lens isn't in the discovered library;
      - "need_submit"/"need_refine"/"need_score" if an in-flight round is
        past the point where a new critique makes sense.
    """
    thought = current_thought(session)
    if thought is None:
        raise SerialSequencingError("begin_first")

    if lens is not None and lens not in available:
        raise SerialSequencingError("unknown_lens", lenses=sorted(available))

    inflight = _inflight_round(thought)
    if inflight is not None:
        phase = _round_phase(inflight)
        if phase == "await_critique":
            # Re-issuing the template (optionally re-choosing the lens).
            if lens is not None:
                inflight.lens = lens
            rnd = inflight
        else:
            raise SerialSequencingError(_pending_code(phase))
    else:
        chosen = lens if lens is not None else choose_lens(session, thought, available, cfg)
        rnd = CritiqueRound(
            round_index=len(thought.critique_rounds),
            lens=chosen,
            critique_text="",
            refined_content="",
            delta_score=UNSCORED,
        )
        thought.critique_rounds.append(rnd)

    return CritiquePrompt(
        thought_id=thought.id,
        lens=rnd.lens,
        round_index=rnd.round_index,
        draft_content=latest_content(thought),
        lens_template=available[rnd.lens],
    )


def submit_critique(session: Session, text: str) -> CritiqueRound:
    """Record the model's critique text on the in-flight round.

    Raises `SerialSequencingError`: "begin_first" (no thought), "need_critique"
    (no round open), "empty_critique" (blank text).
    """
    thought = current_thought(session)
    if thought is None:
        raise SerialSequencingError("begin_first")
    inflight = _inflight_round(thought)
    if inflight is None:
        raise SerialSequencingError("need_critique")
    # [task 13 hardening #1] Phase guard: `_inflight_round` returns the tail
    # round whenever it isn't yet complete, which includes the await_refine /
    # await_score phases -- rounds that ALREADY have a critique. Without this
    # guard a second submit_critique would silently clobber the critique text
    # of a round the model has already moved past. Route those to the step the
    # model actually owes (refine / score) instead, same directive
    # `commit_thought`/`start_critique` already raise for a pending round.
    phase = _round_phase(inflight)
    if phase != "await_critique":
        raise SerialSequencingError(_pending_code(phase))
    if not text or not text.strip():
        raise SerialSequencingError("empty_critique")
    inflight.critique_text = text
    return inflight


def refine_current_thought(
    session: Session, new_content: str, challenged_assumptions: list[str] | None = None
) -> tuple[CritiqueRound, float]:
    """Record a refined version and its normalized edit distance vs. the
    prior content.

    Raises `SerialSequencingError`: "begin_first", "need_critique" (no round),
    "need_submit" (critique not yet submitted), "empty_refinement" (blank).
    """
    thought = current_thought(session)
    if thought is None:
        raise SerialSequencingError("begin_first")
    inflight = _inflight_round(thought)
    if inflight is None:
        raise SerialSequencingError("need_critique")
    if not inflight.critique_text:
        raise SerialSequencingError("need_submit")
    if not new_content or not new_content.strip():
        raise SerialSequencingError("empty_refinement")

    prior = _prior_content_for(thought, inflight)
    inflight.refined_content = new_content
    if challenged_assumptions:
        thought.challenged_assumptions.extend(challenged_assumptions)
    return inflight, normalized_edit_distance(prior, new_content)


def score_current_thought(
    session: Session, raw_scores: dict[str, Any], cfg: dict[str, Any]
) -> ScoreResult:
    """Record the self-scored 7-dim utility vector for the in-flight round,
    carrying forward any dimension the model omitted, then evaluate
    convergence.

    Raises `SerialSequencingError`: "begin_first", "need_critique",
    "need_submit", "need_refine" (nothing refined to score yet).
    """
    thought = current_thought(session)
    if thought is None:
        raise SerialSequencingError("begin_first")
    inflight = _inflight_round(thought)
    if inflight is None:
        raise SerialSequencingError("need_critique")
    if not inflight.critique_text:
        raise SerialSequencingError("need_submit")
    if not inflight.refined_content:
        raise SerialSequencingError("need_refine")

    previous = thought.final_utility_scores  # last completed round's score, or None
    score = _merge_scores(raw_scores, previous)
    overall = overall_score(score)
    prev_overall = overall_score(previous) if previous is not None else 0.0
    delta = overall - prev_overall

    inflight.delta_score = delta
    thought.final_utility_scores = score

    converged, reason = evaluate_convergence(thought, cfg)
    return ScoreResult(
        thought_id=thought.id,
        round_index=inflight.round_index,
        scores={dim: getattr(score, dim) for dim in DIMENSIONS},
        overall=overall,
        delta=delta,
        converged=converged,
        converged_reason=reason,
    )


def commit_thought(session: Session) -> Thought:
    """Lock the in-flight thought: write its final refined content back into
    `Thought.content`, mark it committed, and clear the current-thought
    cursor (so `next_action` unambiguously sees "no thought in progress").

    Raises `SerialSequencingError`: "begin_first" (no thought), "zero_rounds"
    (no completed critique round -- a thought must survive at least one
    critique before it can be committed), or the pending-step code if an
    in-flight round is still incomplete.
    """
    thought = current_thought(session)
    if thought is None:
        raise SerialSequencingError("begin_first")

    inflight = _inflight_round(thought)
    if inflight is not None:
        raise SerialSequencingError(_pending_code(_round_phase(inflight)))

    if not _completed_rounds(thought):
        raise SerialSequencingError("zero_rounds")

    thought.content = latest_content(thought)
    thought.committed = True
    session.current_thought_id = None
    return thought


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _pending_code(phase: str) -> str:
    """Map an in-flight round phase to the directive code that names the
    step the model still owes.
    """
    return {
        "await_critique": "need_submit",
        "await_refine": "need_refine",
        "await_score": "need_score",
    }[phase]


def _merge_scores(
    raw_scores: dict[str, Any], previous: UtilityScore | None
) -> UtilityScore:
    """Build a full 7-dim `UtilityScore` from partial input.

    Keys are normalized (lowercased, '-'/' ' -> '_') so "Bias-resistance"
    and "bias_resistance" both land. A provided value is coerced to float
    and clamped to [0, 1] (tolerant, per the directive philosophy -- a weak
    model's out-of-range guess becomes a boundary value, never a hard
    error). A missing dimension carries forward from `previous`, or defaults
    to a neutral 0.5 on the very first round.
    """
    normalized: dict[str, float] = {}
    for key, value in (raw_scores or {}).items():
        canonical = str(key).strip().lower().replace("-", "_").replace(" ", "_")
        if canonical not in DIMENSIONS:
            continue
        try:
            normalized[canonical] = _clamp(float(value))
        except (TypeError, ValueError):
            continue  # uncoercible -> treat as missing (carried forward)

    resolved: dict[str, float] = {}
    for dim in DIMENSIONS:
        if dim in normalized:
            resolved[dim] = normalized[dim]
        elif previous is not None:
            resolved[dim] = getattr(previous, dim)
        else:
            resolved[dim] = _DEFAULT_DIM
    return UtilityScore(**resolved)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))
