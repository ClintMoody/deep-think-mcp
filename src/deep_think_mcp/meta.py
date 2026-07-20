"""Layer 5 (meta tools) -- `docs/execution-plan.md` Task 8: "the tools that
make small-context local models workable."

Four responsibilities, one module, because all four are small, share no
state, and share the same "session-in / small-value-out, no filesystem" -ish
shape every other engine module in this codebase already follows
(`stages.py`, `lifecycle.py`, `serial_engine.py`): `server.py` only
registers tools, loads/persists sessions around calls into here, and maps
results to `prompts.py` templates -- same division of labor established
since Task 3.

  - `next_action()` -- the authoritative "what do I call next" resolver.
    Its truth table is the centerpiece of this task; see its docstring.
  - `summarize_session()` / `compress_history()` -- deterministic
    *extractive* text ops over already-committed thoughts. No LLM calls
    anywhere in this module: Global Constraints is explicit that "the
    server never touches the network unless autopilot.enabled=true", and
    the build plan frames both of these as compression/selection, not
    inference ("critical for small-context local models" -- the point is
    to hand a weak model *less* text, not to have some other model write
    a better one).
  - `export_session()` / `parse_import()` -- session portability. This
    module only parses/validates; the id-collision-safe reassignment and
    all persistence are server.py's job (see `parse_import`'s docstring),
    same as every other module here staying out of store.py/index.py's
    lane.

`next_action` and `parse_import` are the only two functions that can fail
on legitimate input (an unresolvable state / malformed import payload);
neither raises to reach the model as a raw error -- `next_action` always
returns *some* `NextAction`, and `parse_import` raises the module's own
`ImportValidationError`, which `server.py` maps to a clean directive
payload, exactly the `SerialSequencingError` convention `serial_engine.py`
established.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from deep_think_mcp import serial_engine, stages
from deep_think_mcp.session import Session, Thought

# ---------------------------------------------------------------------------
# next_action()
# ---------------------------------------------------------------------------


@dataclass
class NextAction:
    """The resolved next step. `next_tool` is `None` only when there is
    genuinely nothing left to do (a settled, finalized session, or an
    archived one). `detail` carries whatever extra context a given `code`
    needs (e.g. `converged_reason`, or an `alternative_tool` for the
    two-legitimate-next-steps forks) -- `prompts.next_action_result` merges
    it straight into the response payload.
    """

    code: str
    next_tool: str | None
    detail: dict[str, Any] = field(default_factory=dict)


def next_action(session: Session, cfg: dict[str, Any]) -> NextAction:
    """Resolve the exact next tool to call for `session`, given its
    persisted state and mode. Authoritative: a weak local model should
    never have to infer this from the rest of a payload.

    Truth table (`docs/execution-plan.md` Task 8, minimum required rows):

      - `mode is None`                          -> set_session_mode
      - `mode == "subagent"`                    -> "not yet available"
        (Tasks 9-11 haven't built the subagent engine yet, so there is no
        real tool to point at. This is a deliberately coarse judgment
        call: EVERY subagent-mode session gets this answer regardless of
        `status`, not just ones mid-loop -- there's no loop to be mid of.
        T11 will replace this branch with the real subagent truth table
        once `begin_subagent_thought`/etc. exist; until then this is the
        only truthful thing next_action can say.)
      - serial mode, `status == "active"`, a thought is in progress
        (`serial_engine.loop_phase` != "no_thought") -> the exact
        sub-step the loop is waiting on (critique / submit / refine /
        score), or -- once the tail round is scored ("round_complete")
        -- `commit_thought` if `evaluate_convergence` says converged,
        else `critique_current_thought` for another round. This branch is
        checked BEFORE the final-stage/no-thought branch below on
        purpose: a thought in progress must never be routed toward
        `advance_stage`/`finalize_session` (the T7 review flag --
        advancing the stage while a thought is uncommitted orphans it).
      - serial mode, `status == "active"`, no thought in progress:
          - at the final stage -> `finalize_session`
          - otherwise -> `begin_thought` (with `advance_stage` named as
            the alternative -- two legitimate next steps, same fork
            `prompts.thought_committed` already documents)
      - serial mode, `status == "finalized"`:
          - the move decision hasn't been made yet (no `move_history` and
            no `keep_here` in `decisions`) -> `move_session` (with
            `keep_here` as the alternative)
          - already decided -> nothing left to do
      - `status == "archived"` -> nothing left to do (no tool sets this
        status today; handled for completeness since the schema allows it)

    The `max_rounds` ceiling is soft (T7 report): once
    `evaluate_convergence` reports `converged=True` for ANY reason
    (including hitting the cap), this function always points at
    `commit_thought`, never another critique round -- it never re-checks
    round counts itself, it only relays the engine's verdict.
    """
    if session.mode is None:
        return NextAction("mode_required", "set_session_mode")

    if session.mode == "subagent":
        return NextAction("subagent_not_available", None)

    # session.mode == "serial" from here on.
    if session.status == "finalized":
        decided = bool(session.move_history) or any(
            d.action == "keep_here" for d in session.decisions
        )
        if decided:
            return NextAction("session_complete", None)
        return NextAction(
            "await_move_decision", "move_session", {"alternative_tool": "keep_here"}
        )

    if session.status != "active":
        # Only "archived" remains (Session.status's third literal). No tool
        # sets it today, but a hand-edited/imported session could carry it.
        return NextAction("session_archived", None)

    phase = serial_engine.loop_phase(session)

    if phase == "no_thought":
        if stages.is_final_stage(session):
            return NextAction("loop_no_thought_final_stage", "finalize_session")
        return NextAction(
            "loop_no_thought_begin", "begin_thought", {"alternative_tool": "advance_stage"}
        )

    if phase == "zero_rounds":
        return NextAction("loop_zero_rounds", "critique_current_thought")
    if phase == "await_critique":
        return NextAction("loop_await_critique", "submit_critique")
    if phase == "await_refine":
        return NextAction("loop_await_refine", "refine_current_thought")
    if phase == "await_score":
        return NextAction("loop_await_score", "score_current_thought")

    # phase == "round_complete": the tail round is fully scored. Ask the
    # engine for the convergence verdict rather than re-deriving it.
    thought = serial_engine.current_thought(session)
    assert thought is not None  # loop_phase == "round_complete" implies this
    converged, reason = serial_engine.evaluate_convergence(thought, cfg)
    if converged:
        return NextAction("loop_converged", "commit_thought", {"converged_reason": reason})
    return NextAction("loop_continue", "critique_current_thought")


# ---------------------------------------------------------------------------
# Shared extractive helper -- summarize_session() and compress_history()
# both reduce a committed Thought to one deterministic text line. Purely
# textual (truncation + string joins); no inference of any kind.
# ---------------------------------------------------------------------------

_SNIPPET_MAX_CHARS = 240


def _thought_snippet(thought: Thought, max_chars: int = _SNIPPET_MAX_CHARS) -> str:
    """One deterministic line summarizing a committed thought: its stage
    position, a length-capped excerpt of its final content (whitespace
    collapsed), its overall utility score if scored, and its tags if any.
    """
    content = " ".join(thought.content.split())
    if len(content) > max_chars:
        content = content[:max_chars].rstrip() + "..."
    bits = [f"[{thought.stage} #{thought.position}]", content]
    if thought.final_utility_scores is not None:
        bits.append(f"(score {serial_engine.overall_score(thought.final_utility_scores):.2f})")
    if thought.tags:
        bits.append(f"(tags: {', '.join(thought.tags)})")
    return " ".join(bits)


def _stage_order(stage_names: list[str]) -> dict[str, int]:
    return {name: i for i, name in enumerate(stage_names)}


# ---------------------------------------------------------------------------
# summarize_session(scope="stage"|"all")
# ---------------------------------------------------------------------------


@dataclass
class SummaryEntry:
    thought_id: str
    stage: str
    position: int
    line: str
    overall_score: float | None
    tags: list[str]


@dataclass
class SummaryResult:
    scope: str
    stages_covered: list[str]
    thought_count: int
    digest: str
    entries: list[SummaryEntry]


def summarize_session(session: Session, scope: str = "stage") -> SummaryResult:
    """Deterministic extractive digest of `session`'s committed thoughts.

    `scope="stage"` (default) covers only `session.current_stage`;
    `scope="all"` covers every stage in `session.expected_stages` order.
    Never includes an in-progress (uncommitted) thought -- this is a
    summary of what's been decided, not a preview of what's in flight.
    """
    target_stages = [session.current_stage] if scope == "stage" else list(session.expected_stages)
    order = _stage_order(target_stages)

    thoughts = sorted(
        (t for t in session.thoughts if t.committed and t.stage in order),
        key=lambda t: (order[t.stage], t.position),
    )
    entries = [
        SummaryEntry(
            thought_id=t.id,
            stage=t.stage,
            position=t.position,
            line=_thought_snippet(t),
            overall_score=(
                round(serial_engine.overall_score(t.final_utility_scores), 2)
                if t.final_utility_scores is not None
                else None
            ),
            tags=list(t.tags),
        )
        for t in thoughts
    ]
    return SummaryResult(
        scope=scope,
        stages_covered=sorted({t.stage for t in thoughts}, key=lambda s: order[s]),
        thought_count=len(thoughts),
        digest="\n".join(e.line for e in entries),
        entries=entries,
    )


# ---------------------------------------------------------------------------
# compress_history(target_tokens)
# ---------------------------------------------------------------------------

# docs/build-plan.md § "Local-model accommodations": "compress_history
# returns a 200-400 token digest of prior stages". 300 (the midpoint) is
# [derived] as the tool's default target_tokens -- callers can override.
DEFAULT_TARGET_TOKENS = 300

# Cheap token-count heuristic -- deliberately NOT a real tokenizer (the
# brief: "no tokenizer dependency ... do NOT add a tokenizer package").
# ~4 characters/token is the commonly-cited rule of thumb for English text;
# good enough for a soft context budget, not meant to match any specific
# model's real vocabulary.
_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """`len(text) // 4` -- see module-level `_CHARS_PER_TOKEN` for why."""
    return len(text) // _CHARS_PER_TOKEN


@dataclass
class CompressResult:
    digest: str
    included_thought_ids: list[str]
    stages_covered: list[str]
    estimated_tokens: int
    target_tokens: int
    omitted_count: int


def _stages_before_current(session: Session) -> list[str]:
    """Every stage strictly before `session.current_stage`, in
    `expected_stages` order -- the "prior stages" `compress_history`
    digests. The current stage is deliberately excluded: its detail is
    already visible through the live loop tools/`summarize_session`, so
    compressing it too would just spend budget re-saying what's already
    on-screen.
    """
    idx = session.expected_stages.index(session.current_stage)
    return list(session.expected_stages[:idx])


def compress_history(
    session: Session, target_tokens: int = DEFAULT_TARGET_TOKENS
) -> CompressResult:
    """A budget-capped, deterministic extractive digest of `session`'s
    committed thoughts from stages *before* the current one.

    Selection is recency-biased: prior-stage thoughts are considered
    most-recent-first (so what's freshest -- and most likely still
    relevant to the current line of reasoning -- survives the cut), then
    re-sorted chronologically for the final digest text. The most recent
    candidate is always included even if its own snippet alone would
    blow the budget (so the digest is never empty when prior history
    exists); a final hard clamp on the assembled text guarantees
    `estimated_tokens` never exceeds `target_tokens` regardless.
    """
    target_tokens = max(0, target_tokens)
    prior_stages = _stages_before_current(session)
    order = _stage_order(prior_stages)

    candidates = sorted(
        (t for t in session.thoughts if t.committed and t.stage in order),
        key=lambda t: (order[t.stage], t.position),
    )
    if not candidates:
        return CompressResult(
            digest="",
            included_thought_ids=[],
            stages_covered=[],
            estimated_tokens=0,
            target_tokens=target_tokens,
            omitted_count=0,
        )

    budget = target_tokens
    selected: list[Thought] = []
    for t in reversed(candidates):  # most-recent prior-stage thought first
        cost = estimate_tokens(_thought_snippet(t))
        if not selected or cost <= budget:
            selected.append(t)
            budget -= cost
    selected_ids = {t.id for t in selected}
    ordered = [t for t in candidates if t.id in selected_ids]  # chronological again
    digest = "\n".join(_thought_snippet(t) for t in ordered)

    # Hard cap: guarantees estimated_tokens(digest) <= target_tokens even
    # when the lone most-recent candidate's own snippet already exceeds
    # the budget (the "always include at least one" rule above).
    if estimate_tokens(digest) > target_tokens:
        char_budget = target_tokens * _CHARS_PER_TOKEN
        digest = digest[:char_budget].rstrip() + "..."

    return CompressResult(
        digest=digest,
        included_thought_ids=[t.id for t in ordered],
        stages_covered=sorted({t.stage for t in ordered}, key=lambda s: order[s]),
        estimated_tokens=estimate_tokens(digest),
        target_tokens=target_tokens,
        omitted_count=len(candidates) - len(ordered),
    )


# ---------------------------------------------------------------------------
# export_session() / import_session()
# ---------------------------------------------------------------------------


class ImportValidationError(Exception):
    """Raised by `parse_import` when the supplied data isn't valid session
    JSON. Carries a machine `code` + human `message`; `server.py` maps this
    to a directive payload, the same "never a raw error" convention
    `serial_engine.SerialSequencingError` established.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def export_session(session: Session) -> dict[str, Any]:
    """The full session, JSON-serializable (`mode="json"` turns datetimes
    etc. into plain strings). Deliberately the *complete* record -- every
    thought, every critique round -- unlike a normal tool response, which
    only ever shows the current round's data (Global Constraints' small-
    context accommodation). Export's whole purpose is a full backup/
    transfer, so that accommodation doesn't apply here.
    """
    return session.model_dump(mode="json")


def parse_import(raw: dict[str, Any] | str) -> Session:
    """Parse + validate `raw` (a dict, or a JSON string of one) as a
    `Session`.

    Pure parsing/validation only -- no filesystem or index access, same
    convention every other module here follows. The caller (`server.py`)
    owns collision-checking the parsed session's `id` against the index
    and assigning a fresh `id` + `save_path` before persisting; that's an
    index/filesystem concern this module deliberately stays out of (see
    `server.py`'s `import_session` tool).

    Raises `ImportValidationError` (never a raw `json.JSONDecodeError` or
    pydantic `ValidationError`) on malformed JSON, a non-object payload,
    or a payload that doesn't validate as a `Session`.
    """
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ImportValidationError("invalid_json", str(exc)) from exc
    else:
        parsed = raw

    if not isinstance(parsed, dict):
        raise ImportValidationError(
            "invalid_session_data", "Imported data must be a JSON object."
        )

    try:
        return Session.model_validate(parsed)
    except ValidationError as exc:
        raise ImportValidationError("invalid_session_data", str(exc)) from exc
