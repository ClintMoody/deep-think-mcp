"""Tool-response templates for deep_think_mcp.

Every dict returned by an MCP tool in server.py is built here, not inlined
in server.py -- one responsibility per file, per docs/execution-plan.md
Task 3's "Code Organization" note. Per Global Constraints, "tool responses
are short, directive, and template-driven ... built for weak local models",
so every payload here favors a handful of flat, obviously-named keys over
nested structure, and directive payloads always carry a `next_tool` /
`message` pair telling the model exactly what to do next.

This module has no filesystem access and no dependency on config.py or
store.py -- it only ever formats data it's handed (a Session, an id, an
index dict), so it stays trivially unit-testable and reusable by any future
tool (Tasks 4, 7, 8, 11, ...) that needs the same wording.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from deep_think_mcp.session import Session, Thought

if TYPE_CHECKING:
    from deep_think_mcp.autopilot import AutopilotOutcome
    from deep_think_mcp.meta import CompressResult, NextAction, SummaryResult
    from deep_think_mcp.serial_engine import CritiquePrompt, ScoreResult
    from deep_think_mcp.subagent_engine import MatrixState, SubagentRoundResult

# ---------------------------------------------------------------------------
# Mode descriptions -- single source of truth for list_modes() and every
# mode-required directive payload, so wording never drifts between the two.
# Wording per docs/build-plan.md § "The two execution modes".
# ---------------------------------------------------------------------------

MODES: dict[str, dict[str, str]] = {
    "serial": {
        "description": (
            "One line of reasoning, critiqued step by step by rotating "
            "critique lenses (overconfidence, weak evidence, unstated "
            "assumptions, ...) -- every intermediate thought stays visible."
        ),
        "recommended_for": (
            "Single-GPU setups, small-context local models (7B/8B), and "
            "anyone who wants transparent, step-by-step reasoning."
        ),
    },
    "subagent": {
        "description": (
            "Specialist agents (Analysis, Creativity, ...) propose "
            "competing candidate thoughts, scored on a 7-dimension utility "
            "matrix and converged via Nash-equilibrium-style consensus "
            "(NECoRT)."
        ),
        "recommended_for": (
            "Harder questions where diverse framings matter, and users "
            "with more compute or a hosted-model endpoint to point at."
        ),
    },
}


def _one_line(mode: str) -> str:
    """Collapse a mode's description + recommendation into one line, safe
    for a weak local model to read to the user verbatim (no embedded
    newlines).
    """
    info = MODES[mode]
    return f"{info['description']} Best for: {info['recommended_for']}"


# ---------------------------------------------------------------------------
# list_modes()
# ---------------------------------------------------------------------------


def list_modes() -> dict[str, Any]:
    return {
        "modes": [{"name": name, **info} for name, info in MODES.items()],
    }


# ---------------------------------------------------------------------------
# Mode-selection directive -- start_session() without a mode, and the
# central mode gate blocking a thought tool while session.mode is None.
# ---------------------------------------------------------------------------


def mode_required(session_id: str, blocked_tool: str | None = None) -> dict[str, Any]:
    message = (
        "This session has no mode set yet. Read the mode descriptions to "
        "the user, ask them to choose, then call "
        "set_session_mode(session_id, mode)."
    )
    if blocked_tool is not None:
        message = f"'{blocked_tool}' requires a mode to be set first. " + message

    payload: dict[str, Any] = {
        "mode_required": True,
        "session_id": session_id,
        "modes": [{"name": name, "description": _one_line(name)} for name in MODES],
        "next_tool": "set_session_mode",
        "message": message,
    }
    if blocked_tool is not None:
        payload["blocked_tool"] = blocked_tool
    return payload


# ---------------------------------------------------------------------------
# set_session_mode()
# ---------------------------------------------------------------------------


def mode_set(session: Session) -> dict[str, Any]:
    return {
        "session_id": session.id,
        "mode": session.mode,
        "message": (
            f"Mode set to '{session.mode}'. This is permanent for this "
            "session -- start a new session to use a different mode."
        ),
    }


def mode_already_set(session_id: str, current_mode: str) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "error": "mode_immutable",
        "current_mode": current_mode,
        "message": (
            f"Mode is already set to '{current_mode}' for this session and "
            "cannot be changed. Start a new session to use a different mode."
        ),
    }


# ---------------------------------------------------------------------------
# start_session() with a valid mode -- proceeds immediately
# ---------------------------------------------------------------------------


def session_started(session: Session) -> dict[str, Any]:
    return {
        "session_id": session.id,
        "mode": session.mode,
        "status": session.status,
        "current_stage": session.current_stage,
        "expected_stages": list(session.expected_stages),
        "message": (
            f"Session created in '{session.mode}' mode, ready at stage "
            f"'{session.current_stage}'."
        ),
    }


# ---------------------------------------------------------------------------
# resume_session()
# ---------------------------------------------------------------------------


def session_resumed(session: Session) -> dict[str, Any]:
    return {
        "session_id": session.id,
        "question": session.question,
        "mode": session.mode,
        "status": session.status,
        "current_stage": session.current_stage,
        "expected_stages": list(session.expected_stages),
        "current_thought_id": session.current_thought_id,
        "thought_count": len(session.thoughts),
        "save_path": session.save_path,
    }


# ---------------------------------------------------------------------------
# list_sessions()
# ---------------------------------------------------------------------------


def session_list(entries: dict[str, dict[str, Any]]) -> dict[str, Any]:
    sessions = [
        {"id": session_id, **entry}
        for session_id, entry in sorted(
            entries.items(), key=lambda kv: kv[1].get("created_at", "")
        )
    ]
    return {"sessions": sessions, "count": len(sessions)}


# ---------------------------------------------------------------------------
# clear_session()
# ---------------------------------------------------------------------------


def session_cleared(session_id: str) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "status": "cleared",
        "message": f"Session '{session_id}' has been wiped.",
    }


# ---------------------------------------------------------------------------
# Shared not-found payload -- any lifecycle tool (or the mode gate) that
# looks a session up by id and can't find it in the index.
# ---------------------------------------------------------------------------


def session_not_found(session_id: str) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "error": "session_not_found",
        "message": f"No session found with id '{session_id}'.",
    }


# ---------------------------------------------------------------------------
# Task 13: tolerant-input + storage-fault directives. Every tool accepts JSON
# or plaintext for its structured params (tolerant.py); a value that can't be
# parsed returns THIS directive -- naming the exact expected shape + an
# example -- rather than a raw error/traceback or a silent default.
# ---------------------------------------------------------------------------


def retry_with_clarification(
    param: str,
    expected: str,
    example: str,
    *,
    session_id: str | None = None,
    next_tool: str | None = None,
) -> dict[str, Any]:
    """The `retry_with_clarification` template Global Constraints mandates for
    malformed tool input. Directive, not an error: it names the parameter, the
    exact shape expected, and a concrete example, so a weak local model can
    fix its call and retry in one step.
    """
    payload: dict[str, Any] = {
        "error": "retry_with_clarification",
        "parameter": param,
        "expected": expected,
        "example": example,
        "message": (
            f"Could not parse the '{param}' argument. Expected {expected}. "
            f"For example: {example}. Please call again with '{param}' in "
            "that form (JSON or the plaintext form shown both work)."
        ),
    }
    if session_id is not None:
        payload["session_id"] = session_id
    if next_tool is not None:
        payload["next_tool"] = next_tool
    return payload


def storage_unavailable(session_id: str | None, detail: str) -> dict[str, Any]:
    """A persistence step failed with a storage fault -- a Portalocker lock
    timeout (`LockException`, which is NOT an `OSError` subclass, so it would
    otherwise escape the OSError-shaped catches) or a raw filesystem error.
    Surfaced as a retryable directive, never a raw traceback (Task 13
    hardening item #6).
    """
    return {
        "session_id": session_id,
        "error": "storage_unavailable",
        "retryable": True,
        "next_tool": None,
        "message": (
            "The session store was temporarily unavailable (a file lock timed "
            "out or a filesystem error occurred) and this call was not "
            "completed. This is usually transient -- retry the same call in a "
            f"moment. Detail: {detail}"
        ),
    }


# ---------------------------------------------------------------------------
# finalize_session() -- Task 4. Wording is the exact canned text from
# `docs/build-plan.md` § "Finalize + move flow", verbatim per the task
# brief: the model reads `human_prompt` to the user unmodified.
# ---------------------------------------------------------------------------


def session_finalized(session: Session, *, uncommitted_thought: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "session_id": session.id,
        "status": session.status,
        "current_path": session.save_path,
        "human_prompt": (
            f"Your reasoning is saved at `{session.save_path}`. Would you "
            "like to move it elsewhere (a project folder, your Documents, "
            "etc.), or leave it where it is?"
        ),
        "available_tools": [
            {
                "name": "move_session",
                "description": "Move the session file to a new location.",
            },
            {
                "name": "keep_here",
                "description": "Leave the session file where it is.",
            },
        ],
    }
    # [task 12] finalize() never hard-blocks on an in-progress (uncommitted)
    # thought -- see lifecycle.finalize()'s docstring for why -- but it also
    # must not silently finalize over one. This warning is the middle
    # ground: the finalize the caller asked for still happens, and they're
    # told plainly that a thought is still open.
    if uncommitted_thought:
        payload["warning"] = "uncommitted_thought_exists"
        payload["warning_message"] = (
            f"Session '{session.id}' still has an in-progress thought (id "
            f"`{session.current_thought_id}`) that was never committed. "
            "Finalizing does not discard it, but it is not reflected in "
            "the session's committed record. Call commit_thought (serial "
            "mode) or commit_subagent_thought (subagent mode) first if you "
            "want it included, or proceed with move_session/keep_here if "
            "this is intentional."
        )
    return payload


# ---------------------------------------------------------------------------
# move_session()
# ---------------------------------------------------------------------------


def session_moved(session: Session) -> dict[str, Any]:
    last_move = session.move_history[-1]
    payload: dict[str, Any] = {
        "session_id": session.id,
        "status": session.status,
        "from_path": last_move.from_path,
        "new_path": session.save_path,
        "message": f"Session moved to `{session.save_path}`.",
    }
    # [task 12] The move itself is a real, verified success either way --
    # `lifecycle.move()`'s final unlink-of-the-original step is best-effort
    # and never rolled back on failure. This warning is how that failure
    # stops being silently swallowed: the caller is told the old file is
    # still there instead of just trusting it's gone.
    if last_move.unlink_failed:
        payload["warning"] = "original_file_left_behind"
        payload["old_path"] = last_move.from_path
        payload["message"] = (
            f"Session moved to `{session.save_path}`. The original file "
            f"at `{last_move.from_path}` could not be removed and was "
            "left behind."
        )
    return payload


def move_failed(session_id: str, code: str, message: str) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "error": code,
        "message": message,
    }


def move_index_update_failed(session: Session, error: str, index_path: Any) -> dict[str, Any]:
    """The move itself succeeded and was verified -- `session.save_path`
    below is real and truthful -- but the session index couldn't be
    updated to match afterward, so `resume_session`/`list_sessions` may
    still be pointing at the old (now-deleted) path for this session
    until it's corrected.
    """
    return {
        "session_id": session.id,
        "status": session.status,
        "new_path": session.save_path,
        "error": "index_update_failed",
        "index_path": str(index_path),
        "message": (
            f"The session file was moved to `{session.save_path}` and "
            f"verified successfully, but the session index could not be "
            f"updated afterward ({error}). The session's data is safe at "
            "its new location, but resume_session/list_sessions may not "
            "find it there until the index is corrected -- an operator "
            f"can manually fix this session's `path` entry in "
            f"`{index_path}`."
        ),
    }


# ---------------------------------------------------------------------------
# keep_here()
# ---------------------------------------------------------------------------


def session_kept(session: Session) -> dict[str, Any]:
    return {
        "session_id": session.id,
        "status": session.status,
        "save_path": session.save_path,
        "message": "Session will stay at its current location.",
    }


# ---------------------------------------------------------------------------
# advance_stage() -- Task 5, Layer 3 (stage machine).
# ---------------------------------------------------------------------------


def stage_advanced(session: Session) -> dict[str, Any]:
    return {
        "session_id": session.id,
        "current_stage": session.current_stage,
        "expected_stages": list(session.expected_stages),
        "message": f"Advanced to stage '{session.current_stage}'.",
    }


def final_stage_reached(session: Session) -> dict[str, Any]:
    return {
        "session_id": session.id,
        "current_stage": session.current_stage,
        "final_stage": True,
        "next_tool": "finalize_session",
        "message": (
            f"'{session.current_stage}' is the final stage -- there is no "
            "next stage to advance to. Call finalize_session(session_id) "
            "when this session's reasoning is complete."
        ),
    }


# ---------------------------------------------------------------------------
# Wrong-mode directive -- Task 7's mode gate. A serial tool called on a
# subagent-mode session (or vice versa in Task 11) never runs; the model is
# told which mode this session is fixed in and to use that mode's tools.
# ---------------------------------------------------------------------------


def wrong_mode(
    session_id: str, required_mode: str, current_mode: str, blocked_tool: str
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "error": "wrong_mode",
        "required_mode": required_mode,
        "current_mode": current_mode,
        "blocked_tool": blocked_tool,
        "message": (
            f"'{blocked_tool}' is a {required_mode}-mode tool, but this "
            f"session is in '{current_mode}' mode (fixed for the life of the "
            f"session). Use the {current_mode}-mode tools instead, or start a "
            f"new session in {required_mode} mode."
        ),
    }


# ---------------------------------------------------------------------------
# Task 7: the serial critique-lens loop. Success payloads first, then the
# out-of-order sequencing directives (serial_directive), which turn a
# weak-model mistake into "here is the exact next call", never an error.
# ---------------------------------------------------------------------------


def thought_begun(session: Session, thought: Thought) -> dict[str, Any]:
    return {
        "session_id": session.id,
        "thought_id": thought.id,
        "stage": thought.stage,
        "position": thought.position,
        "next_tool": "critique_current_thought",
        "message": (
            "Draft recorded. Now stress-test it: call "
            "critique_current_thought(session_id) to get a critique lens "
            "(omit `lens` to let the server pick a stage-appropriate one)."
        ),
    }


def critique_ready(session_id: str, prompt: CritiquePrompt) -> dict[str, Any]:
    """The critique payload. The lens templates open with a positional claim
    ("the draft thought above"), so `draft_content` MUST sit immediately
    before `lens_template` here -- the model reads the draft, then the
    template that critiques it, with nothing in between. This ordering is
    the adjacency contract from Task 6's review; `test_prompts` and the MCP
    contract test both pin it.
    """
    return {
        "session_id": session_id,
        "thought_id": prompt.thought_id,
        "lens": prompt.lens,
        "round_index": prompt.round_index,
        "draft_content": prompt.draft_content,
        "lens_template": prompt.lens_template,
        "next_tool": "submit_critique",
        "message": (
            "Apply the critique lens template to the draft content shown "
            "immediately above it, then return your critique via "
            "submit_critique(session_id, text)."
        ),
    }


def critique_submitted(session_id: str, round_index: int, lens: str) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "round_index": round_index,
        "lens": lens,
        "next_tool": "refine_current_thought",
        "message": (
            "Critique recorded. Now rewrite the thought to address it: call "
            "refine_current_thought(session_id, new_content)."
        ),
    }


def thought_refined(
    session_id: str, round_index: int, edit_distance: float
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "round_index": round_index,
        "edit_distance": edit_distance,
        "next_tool": "score_current_thought",
        "message": (
            "Refinement recorded (normalized edit distance vs. the prior "
            f"version: {edit_distance:.3f}). Now self-score the refined "
            "thought across the 7 utility dimensions via "
            "score_current_thought(session_id, scores)."
        ),
    }


def thought_scored(session_id: str, result: ScoreResult) -> dict[str, Any]:
    """The convergence verdict. `converged`/`converged_reason` tell the model
    outright whether to commit or run another critique lens -- directive, per
    the local-model philosophy (the model shouldn't have to infer it).
    """
    if result.converged:
        message = (
            f"Converged ({result.converged_reason}). This thought is done -- "
            "call commit_thought(session_id) to lock it."
        )
        next_tool = "commit_thought"
    else:
        message = (
            "Not yet converged -- the thought is still improving. Run another "
            "critique lens: call critique_current_thought(session_id) "
            "(omit `lens` to rotate to the next one automatically)."
        )
        next_tool = "critique_current_thought"
    return {
        "session_id": session_id,
        "round_index": result.round_index,
        "scores": result.scores,
        "overall": result.overall,
        "delta": result.delta,
        "converged": result.converged,
        "converged_reason": result.converged_reason,
        "next_tool": next_tool,
        "message": message,
    }


def thought_committed(session: Session, thought: Thought) -> dict[str, Any]:
    return {
        "session_id": session.id,
        "thought_id": thought.id,
        "stage": thought.stage,
        "position": thought.position,
        "committed": True,
        # Two legitimate next steps (another thought in this stage, or move
        # on): point at the more common one and spell out both. Once Task 8
        # lands, next_action() becomes the authoritative resolver of this
        # fork; until then both named tools already exist and work.
        "next_tool": "begin_thought",
        "message": (
            f"Thought committed at '{thought.stage}' position "
            f"{thought.position}. Start another thought in this stage with "
            "begin_thought(session_id, content), or move on with "
            "advance_stage(session_id) when the stage is done."
        ),
    }


# Per-code directive wording for out-of-order serial calls. Each entry is
# (next_tool, message) -- the exact call the model should make instead.
_SERIAL_DIRECTIVES: dict[str, tuple[str, str]] = {
    "begin_first": (
        "begin_thought",
        "No thought is in progress. Draft one first with "
        "begin_thought(session_id, content).",
    ),
    "uncommitted_exists": (
        "commit_thought",
        "A thought is already in progress in this stage. Finish it -- keep "
        "refining and scoring it, then commit_thought(session_id) -- before "
        "beginning a new one.",
    ),
    "need_critique": (
        "critique_current_thought",
        "No critique round is open. Open one with "
        "critique_current_thought(session_id) first.",
    ),
    "need_submit": (
        "submit_critique",
        "You opened a critique lens but haven't submitted the critique yet. "
        "Call submit_critique(session_id, text).",
    ),
    "empty_critique": (
        "submit_critique",
        "The critique text was empty. Submit an actual critique via "
        "submit_critique(session_id, text).",
    ),
    "need_refine": (
        "refine_current_thought",
        "Rewrite the thought to address the critique before scoring: call "
        "refine_current_thought(session_id, new_content).",
    ),
    "empty_refinement": (
        "refine_current_thought",
        "The refined content was empty. Provide the improved thought text via "
        "refine_current_thought(session_id, new_content).",
    ),
    "need_score": (
        "score_current_thought",
        "Score the refined thought to finish this round: call "
        "score_current_thought(session_id, scores).",
    ),
    "zero_rounds": (
        "critique_current_thought",
        "A thought must survive at least one critique round before it can be "
        "committed. Start one with critique_current_thought(session_id).",
    ),
    "unknown_lens": (
        "critique_current_thought",
        "That lens name isn't in the library. Retry "
        "critique_current_thought(session_id, lens) with one of the available "
        "lenses (or omit `lens` to let the server pick).",
    ),
}


def serial_directive(session_id: str, code: str, **detail: Any) -> dict[str, Any]:
    """Map a `serial_engine.SerialSequencingError` code to a directive
    payload. Unknown codes degrade to a generic 'call next_action' nudge
    rather than raising -- a directive is never allowed to become an error.
    """
    next_tool, message = _SERIAL_DIRECTIVES.get(
        code,
        ("next_action", "Call next_action(session_id) to get the right next step."),
    )
    payload: dict[str, Any] = {
        "session_id": session_id,
        "error": "sequencing",
        "code": code,
        "next_tool": next_tool,
        "message": message,
    }
    if code == "unknown_lens" and "lenses" in detail:
        payload["available_lenses"] = detail["lenses"]
    return payload


# ---------------------------------------------------------------------------
# Task 8: meta tools (next_action, summarize_session, compress_history) +
# import/export. All wording for `meta.py`'s pure results lives here, same
# division of labor Task 7 established.
# ---------------------------------------------------------------------------

# Static message per `meta.NextAction.code`. Codes whose wording needs a
# session-specific value (the current stage name, the converged reason) are
# NOT here -- `next_action_result` builds those inline -- everything else
# is fixed text, same shape as `_SERIAL_DIRECTIVES`.
_NEXT_ACTION_MESSAGES: dict[str, str] = {
    "mode_required": (
        "No mode is set yet. Read the mode descriptions to the user, then "
        "call set_session_mode(session_id, mode)."
    ),
    "subagent_converged": (
        "The winning candidate has reached the commit threshold. Call "
        "commit_subagent_thought(session_id) to lock it in."
    ),
    "subagent_budget_exhausted": (
        "The subagent round budget (max_rounds) is spent. Accept the current "
        "equilibrium: call commit_subagent_thought(session_id)."
    ),
    "subagent_can_advance": (
        "The equilibrium hasn't reached the commit threshold yet and rounds "
        "remain. Refine it with advance_subagent_round(session_id), or accept "
        "it now with commit_subagent_thought(session_id)."
    ),
    "subagent_awaiting_specialist": (
        "This manual subagent round is mid-way: the current specialist owes a "
        "candidate. Voice it and call advance_subagent_round(session_id, "
        "candidate=..., scores=...) to submit its candidate and 7-dim "
        "self-scores."
    ),
    "loop_zero_rounds": (
        "A thought is drafted but hasn't been critiqued yet. Call "
        "critique_current_thought(session_id)."
    ),
    "loop_await_critique": (
        "A critique lens is open but its critique hasn't been submitted "
        "yet. Call submit_critique(session_id, text)."
    ),
    "loop_await_refine": (
        "The critique is in; the thought hasn't been refined yet. Call "
        "refine_current_thought(session_id, new_content)."
    ),
    "loop_await_score": (
        "The thought was refined; it hasn't been scored yet. Call "
        "score_current_thought(session_id, scores)."
    ),
    "loop_continue": (
        "Not yet converged -- the thought is still improving. Call "
        "critique_current_thought(session_id) for another round."
    ),
    "await_move_decision": (
        "This session is finalized but its final location hasn't been "
        "decided yet. Call move_session(session_id, new_path) to relocate "
        "it, or keep_here(session_id) to leave it where it is."
    ),
    "session_complete": (
        "This session is finalized and its location is settled. There is "
        "nothing further to do."
    ),
    "session_archived": "This session is archived. There is nothing further to do.",
}


def next_action_result(session: Session, result: NextAction) -> dict[str, Any]:
    """The authoritative next-step payload for `meta.next_action`. Message
    wording is static per `_NEXT_ACTION_MESSAGES` except for the three
    codes below, whose text needs a value only known at call time (the
    current stage name, or the specific convergence reason).
    """
    if result.code == "loop_no_thought_final_stage":
        message = (
            f"'{session.current_stage}' is the final stage and no thought "
            "is in progress. Call finalize_session(session_id) when this "
            "session's reasoning is complete (or begin_thought(session_id, "
            "content) for one more thought in this stage first)."
        )
    elif result.code == "loop_no_thought_begin":
        message = (
            "No thought is in progress. Start one with "
            "begin_thought(session_id, content), or "
            f"advance_stage(session_id) if '{session.current_stage}' is done."
        )
    elif result.code == "loop_converged":
        message = (
            f"Converged ({result.detail.get('converged_reason')}). Call "
            "commit_thought(session_id) to lock it in."
        )
    elif result.code == "subagent_no_thought_final_stage":
        message = (
            f"'{session.current_stage}' is the final stage and no subagent "
            "thought is in progress. Call finalize_session(session_id) when "
            "this session's reasoning is complete (or "
            "begin_subagent_thought(session_id) for one more thought in this "
            "stage first)."
        )
    elif result.code == "subagent_no_thought_begin":
        message = (
            "No subagent thought is in progress. Start one with "
            "begin_subagent_thought(session_id, content), or "
            f"advance_stage(session_id) if '{session.current_stage}' is done."
        )
    else:
        message = _NEXT_ACTION_MESSAGES.get(
            result.code, "Call the indicated tool to continue."
        )

    payload: dict[str, Any] = {
        "session_id": session.id,
        "code": result.code,
        "next_tool": result.next_tool,
        "message": message,
    }
    payload.update(result.detail)
    return payload


# ---------------------------------------------------------------------------
# summarize_session(scope="stage"|"all")
# ---------------------------------------------------------------------------


def summary_result(session: Session, result: SummaryResult) -> dict[str, Any]:
    if result.thought_count == 0:
        message = f"No committed thoughts yet for scope '{result.scope}'."
    else:
        message = (
            f"Digest of {result.thought_count} committed thought(s) across "
            f"{len(result.stages_covered)} stage(s)."
        )
    return {
        "session_id": session.id,
        "scope": result.scope,
        "stages_covered": result.stages_covered,
        "thought_count": result.thought_count,
        "digest": result.digest,
        "entries": [
            {
                "thought_id": e.thought_id,
                "stage": e.stage,
                "position": e.position,
                "line": e.line,
                "overall_score": e.overall_score,
                "tags": e.tags,
            }
            for e in result.entries
        ],
        "message": message,
    }


# ---------------------------------------------------------------------------
# compress_history(target_tokens)
# ---------------------------------------------------------------------------


def compression_result(session: Session, result: CompressResult) -> dict[str, Any]:
    if not result.included_thought_ids:
        message = "No prior-stage history yet to compress."
    else:
        message = (
            f"Digest covers {len(result.included_thought_ids)} prior-stage "
            f"thought(s) (~{result.estimated_tokens} tokens, target "
            f"{result.target_tokens})."
        )
        if result.omitted_count:
            message += (
                f" {result.omitted_count} older thought(s) omitted to stay "
                "in budget."
            )
    return {
        "session_id": session.id,
        "digest": result.digest,
        "stages_covered": result.stages_covered,
        "included_thought_ids": result.included_thought_ids,
        "estimated_tokens": result.estimated_tokens,
        "target_tokens": result.target_tokens,
        "omitted_count": result.omitted_count,
        "message": message,
    }


# ---------------------------------------------------------------------------
# export_session() / import_session()
# ---------------------------------------------------------------------------


def session_exported(session: Session, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": session.id,
        "export": data,
        "message": (
            "Full session export below. Pass this whole payload's `export` "
            "value to import_session(data) to recreate this session (on "
            "this or another deep-think-mcp install)."
        ),
    }


def import_failed(code: str, message: str) -> dict[str, Any]:
    return {
        "error": code,
        "message": f"Could not import session: {message}",
    }


def session_imported(session: Session, id_reassigned: bool) -> dict[str, Any]:
    message = f"Session imported as '{session.id}', saved at `{session.save_path}`."
    if id_reassigned:
        message += (
            " Its original id collided with an existing session here, so a "
            "new id was assigned."
        )
    return {
        "session_id": session.id,
        "save_path": session.save_path,
        "id_reassigned": id_reassigned,
        "message": message,
    }


# ---------------------------------------------------------------------------
# Task 11: subagent (NECoRT) mode. Wording for the four tools' success
# payloads, the Nash-invocation prompt template, and the sequencing/adapter
# directives -- same "wording lives here, engine lives in subagent_engine.py"
# division Task 7 established.
# ---------------------------------------------------------------------------

# Default specialist framings, keyed by the config `[subagent].agents` roster.
# Each is the perspective that specialist argues from when generating a
# candidate thought; stage weighting (stages.agent_weight_for_stage) is layered
# on top by the engine. A roster name with no entry here gets `_GENERIC_FRAMING`.
SPECIALIST_FRAMINGS: dict[str, str] = {
    "Analysis": (
        "Reason rigorously and analytically. Decompose the problem into parts, "
        "trace cause and effect, demand evidence for each claim, and prize "
        "correctness and completeness over flourish."
    ),
    "Creativity": (
        "Reason divergently and imaginatively. Reframe the problem, surface "
        "non-obvious angles and analogies, and propose novel approaches others "
        "would overlook -- prize originality and fresh perspective."
    ),
    "Skeptic": (
        "Reason adversarially. Hunt for the hidden assumption, the weak link, "
        "the missing counter-case, and the bias in the framing; state what "
        "would have to be true for the answer to be wrong -- prize "
        "bias-resistance and robustness."
    ),
}

_GENERIC_FRAMING = (
    "Reason carefully from your own distinct perspective, contributing an "
    "angle the other specialists would not."
)


def specialist_framing(name: str) -> str:
    """The default framing text for a configured specialist name."""
    return SPECIALIST_FRAMINGS.get(name, _GENERIC_FRAMING)


def build_subagent_prompt(
    *,
    question: str,
    stage: str,
    prior_context: str,
    content: str | None,
    prompt_focus: str | None,
    framings: list[dict[str, Any]],
) -> str:
    """Assemble the single `user_input` string handed to the Nash core.

    `framings` is a list of `{"name", "framing", "weight"}` dicts (the
    engine computes each weight via `stages.agent_weight_for_stage`). The
    weighting is expressed in-prompt (a weak local model can't be handed a
    real utility multiplier, so we tell it in words which perspectives to
    lean on harder in this stage).
    """
    parts: list[str] = [
        f"Question under deep-think reasoning:\n{question}",
        f"\nCurrent reasoning stage: {stage}.",
    ]
    if prior_context:
        parts.append(f"\nEstablished context from earlier stages:\n{prior_context}")
    if content:
        parts.append(f"\nStarting point to develop:\n{content}")
    if prompt_focus:
        parts.append(f"\nFocus this thought specifically on: {prompt_focus}")
    parts.append(
        "\nGenerate the strongest possible thought for this stage, drawing on "
        "these specialist perspectives (lean harder on the emphasized ones):"
    )
    for framing in framings:
        weight = float(framing["weight"])
        emphasis = f" [emphasis x{weight:g}]" if weight != 1.0 else ""
        parts.append(f"- {framing['name']}{emphasis}: {framing['framing']}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Task 13: manual specialist engine ([subagent] engine="manual"). The server
# hands the calling model one specialist's prompt at a time (the model plays
# the specialist itself -- no endpoint, no network). `build_manual_specialist_
# prompt` is the framing text the model applies; `manual_specialist_prompt` is
# the tool response wrapping it with the directive to produce a candidate +
# self-scores and call advance_subagent_round.
# ---------------------------------------------------------------------------

# The 7 utility dimensions the model self-scores each candidate on, in
# canonical order (mirrors serial_engine.DIMENSIONS). Named here so the manual
# prompt can spell them out for a weak model without importing the engine.
_UTILITY_DIMENSIONS: tuple[str, ...] = (
    "correctness",
    "evidence",
    "novelty",
    "clarity",
    "bias_resistance",
    "actionability",
    "coverage",
)


def build_manual_specialist_prompt(
    *,
    question: str,
    stage: str,
    prior_context: str,
    seed_content: str | None,
    prompt_focus: str | None,
    specialist_name: str,
    framing: str,
    weight: float,
    specialist_index: int,
    specialist_total: int,
    round_num: int,
) -> str:
    """The instruction text the model reads to voice one specialist.

    Assembled from the question, the current stage, compressed prior-stage
    context, the seed to develop (the thought's draft, or the prior round's
    winning candidate on later rounds), and this specialist's framing with its
    stage weighting expressed in words (a weak local model can't be handed a
    real multiplier). The stage-appropriate emphasis comes from
    `stages.agent_weight_for_stage`, computed by the engine.
    """
    emphasis = (
        f" This is the emphasized perspective for the '{stage}' stage -- lean "
        "into it especially hard."
        if weight != 1.0
        else ""
    )
    parts: list[str] = [
        f"Question under deep-think reasoning:\n{question}",
        f"\nCurrent reasoning stage: {stage}.",
        f"\nYou are specialist {specialist_index + 1} of {specialist_total} "
        f"(round {round_num}): {specialist_name}.",
        f"\nReason from THIS specialist's perspective:\n{framing}{emphasis}",
    ]
    if prior_context:
        parts.append(f"\nEstablished context from earlier stages:\n{prior_context}")
    if seed_content:
        parts.append(f"\nDevelop / improve this current best synthesis:\n{seed_content}")
    if prompt_focus:
        parts.append(f"\nFocus this thought specifically on: {prompt_focus}")
    dims = ", ".join(_UTILITY_DIMENSIONS)
    parts.append(
        "\nProduce this specialist's strongest candidate thought for this "
        "stage, then self-score it on all 7 utility dimensions (0.0-1.0 each): "
        f"{dims}. Submit both by calling advance_subagent_round(session_id, "
        'candidate="<your candidate>", scores={"correctness": 0.8, ...}).'
    )
    return "\n".join(parts)


def manual_specialist_prompt(session: Session, prompt: Any) -> dict[str, Any]:
    """The tool response handing the model the next specialist to voice
    (a `manual_engine.ManualPrompt`). Directive: produce the candidate + 7-dim
    self-scores, then call advance_subagent_round to submit them.
    """
    return {
        "session_id": session.id,
        "thought_id": prompt.thought_id,
        "stage": session.current_stage,
        "engine": "manual",
        "us_round": prompt.us_round,
        "rounds_run": prompt.rounds_run,
        "max_rounds": prompt.max_rounds,
        "specialist": prompt.specialist_name,
        "specialist_index": prompt.specialist_index,
        "specialist_total": prompt.specialist_total,
        "stage_weight": prompt.weight,
        "utility_dimensions": list(_UTILITY_DIMENSIONS),
        "specialist_prompt": prompt.prompt_text,
        "next_tool": "advance_subagent_round",
        "message": (
            f"You are playing specialist {prompt.specialist_index + 1} of "
            f"{prompt.specialist_total} ({prompt.specialist_name}). Read "
            "`specialist_prompt`, produce that specialist's candidate thought, "
            "self-score it on the 7 utility dimensions, then call "
            "advance_subagent_round(session_id, candidate=..., scores=...) to "
            "submit it and get the next specialist (or the round result)."
        ),
    }


def _subagent_round_common(result: SubagentRoundResult) -> dict[str, Any]:
    """The fields common to both begin/advance success payloads."""
    return {
        "thought_id": result.thought_id,
        "us_round": result.us_round,
        "rounds_run": result.rounds_run,
        "max_rounds": result.max_rounds,
        "equilibrium_strength": round(result.strength, 3),
        # The metric `equilibrium_strength` is measured in -- "peer rating"
        # (necort, correctness dim) or "mean utility" (manual, 7-dim mean) --
        # so the model reads the gate honestly per engine (T13 fix round 1).
        "gate_metric": result.metric_label,
        "commit_threshold": result.threshold,
        "converged": result.converged,
        "budget_exhausted": result.budget_exhausted,
        "endpoints_used": result.endpoints_used,
        "selected_content": result.selected_content,
        "final_utility_scores": result.final_utility_scores,
    }


def _subagent_next_step(result: SubagentRoundResult) -> tuple[str, str]:
    """(next_tool, message) after a subagent round, from the equilibrium state.
    The wording names this engine's own gate metric (`result.metric_label`) so
    a manual verdict says "mean utility" and a necort verdict "peer rating"."""
    label = result.metric_label
    if result.converged:
        return (
            "commit_subagent_thought",
            f"The equilibrium is strong (winning candidate's {label} "
            f"{result.strength:.2f} >= threshold {result.threshold:.2f}). Accept "
            "it with commit_subagent_thought(session_id), or inspect it first "
            "with inspect_utility_matrix(session_id).",
        )
    if result.budget_exhausted:
        return (
            "commit_subagent_thought",
            f"The round budget (max_rounds={result.max_rounds}) is spent and the "
            f"winning candidate's {label} is {result.strength:.2f}. Accept it "
            "with commit_subagent_thought(session_id).",
        )
    return (
        "advance_subagent_round",
        f"The winning candidate's {label} ({result.strength:.2f}) is below the "
        f"commit threshold ({result.threshold:.2f}) and rounds remain "
        f"({result.rounds_run}/{result.max_rounds}). Refine it with "
        "advance_subagent_round(session_id), or accept it now with "
        "commit_subagent_thought(session_id).",
    )


def subagent_thought_begun(session: Session, result: SubagentRoundResult) -> dict[str, Any]:
    next_tool, message = _subagent_next_step(result)
    return {
        "session_id": session.id,
        "stage": session.current_stage,
        **_subagent_round_common(result),
        "next_tool": next_tool,
        "message": "Subagent thought started. " + message,
    }


def subagent_round_advanced(session: Session, result: SubagentRoundResult) -> dict[str, Any]:
    next_tool, message = _subagent_next_step(result)
    return {
        "session_id": session.id,
        "stage": session.current_stage,
        **_subagent_round_common(result),
        "next_tool": next_tool,
        "message": "Round advanced. " + message,
    }


def subagent_matrix(session: Session, state: MatrixState) -> dict[str, Any]:
    """The current scoring state (inspect_utility_matrix)."""
    return {
        "session_id": session.id,
        "thought_id": state.thought_id,
        "us_round": state.us_round,
        "rounds_run": state.rounds_run,
        "max_rounds": state.max_rounds,
        "equilibrium_strength": round(state.strength, 3),
        "gate_metric": state.metric_label,
        "commit_threshold": state.threshold,
        "converged": state.converged,
        "selected_content": state.selected_content,
        "candidates": state.candidates,
        # [F2] Commit not only when converged but also once the round budget is
        # spent -- advance_subagent_round would refuse (round_budget_exhausted)
        # there, contradicting next_action and the round verdict which both say
        # commit for this same state.
        "next_tool": (
            "commit_subagent_thought"
            if state.converged or state.rounds_run >= state.max_rounds
            else "advance_subagent_round"
        ),
        "message": (
            f"Current equilibrium: {state.rounds_run}/{state.max_rounds} round(s) "
            f"run, winning candidate's {state.metric_label} {state.strength:.2f} "
            f"(threshold {state.threshold:.2f})."
        ),
    }


def subagent_thought_committed(session: Session, thought: Thought) -> dict[str, Any]:
    return {
        "session_id": session.id,
        "thought_id": thought.id,
        "stage": thought.stage,
        "position": thought.position,
        "committed": True,
        "next_tool": "begin_subagent_thought",
        "message": (
            f"Subagent thought committed at '{thought.stage}' position "
            f"{thought.position}. Start another with "
            "begin_subagent_thought(session_id, content), or move on with "
            "advance_stage(session_id) when the stage is done."
        ),
    }


# Per-code directive wording for out-of-order / budget subagent calls. Same
# shape as `_SERIAL_DIRECTIVES`. (`no_endpoint` is handled separately below --
# it needs config-pointing wording, not a next tool.)
_SUBAGENT_DIRECTIVES: dict[str, tuple[str, str]] = {
    "begin_first": (
        "begin_subagent_thought",
        "No subagent thought is in progress. Start one with "
        "begin_subagent_thought(session_id, content).",
    ),
    "uncommitted_exists": (
        "commit_subagent_thought",
        "A subagent thought is already in progress. Finish it -- advance or "
        "accept it with commit_subagent_thought(session_id) -- before "
        "beginning a new one.",
    ),
    "no_rounds": (
        "begin_subagent_thought",
        "This thought has no Nash round yet. Start the equilibrium with "
        "begin_subagent_thought(session_id, content).",
    ),
    "round_budget_exhausted": (
        "commit_subagent_thought",
        "The subagent round budget (max_rounds) is spent -- US caps it even "
        "when the Nash core would keep going. Accept the current equilibrium "
        "with commit_subagent_thought(session_id).",
    ),
    # [task 13] manual engine: the model owes the current specialist's candidate.
    "need_candidate": (
        "advance_subagent_round",
        "This specialist hasn't produced a candidate yet. Voice the current "
        "specialist, then call advance_subagent_round(session_id, "
        "candidate=..., scores=...) with its candidate and 7-dim self-scores.",
    ),
}


def subagent_no_endpoint(session_id: str) -> dict[str, Any]:
    """No endpoint configured for the NECoRT subagent engine: point the caller
    at the endpoint-free manual specialist path rather than failing opaquely.
    """
    return {
        "session_id": session_id,
        "error": "no_endpoint",
        "next_tool": None,
        "message": (
            "Subagent mode has no NECoRT endpoint configured "
            "([subagent].endpoint / [subagent].endpoints are empty). Either set "
            "an OpenAI-compatible endpoint in config, or use the endpoint-free "
            "manual specialist path by setting [subagent] engine=\"manual\" "
            "(the model plays each specialist itself -- no network)."
        ),
    }


def subagent_adapter_error(session_id: str, detail: str, retryable: bool) -> dict[str, Any]:
    """A NECoRT adapter failure (network error, malformed 200 body, vendored
    core unavailable, ...) surfaced as a directive -- never a raw traceback.
    """
    if retryable:
        message = (
            "The NECoRT endpoint call failed. This is usually transient (the "
            "endpoint was unreachable or returned an unexpected body). Retry "
            "begin_subagent_thought / advance_subagent_round, and if it keeps "
            f"failing check the [subagent] endpoint/model config. Detail: {detail}"
        )
    else:
        message = (
            "The NECoRT subagent core is unavailable (its vendored code or its "
            "dependencies could not be loaded). Use the endpoint-free manual "
            "specialist path ([subagent] engine=\"manual\") or repair the "
            f"vendored submodule. Detail: {detail}"
        )
    return {
        "session_id": session_id,
        "error": "adapter_error",
        "retryable": retryable,
        "next_tool": None,
        "message": message,
    }


def subagent_directive(session_id: str, code: str, **detail: Any) -> dict[str, Any]:
    """Map a `subagent_engine.SubagentSequencingError` code to a directive
    payload. `no_endpoint` routes to the manual-path directive; unknown codes
    degrade to a generic next_action nudge -- a directive is never an error.
    """
    if code == "no_endpoint":
        return subagent_no_endpoint(session_id)
    next_tool, message = _SUBAGENT_DIRECTIVES.get(
        code,
        ("next_action", "Call next_action(session_id) to get the right next step."),
    )
    return {
        "session_id": session_id,
        "error": "sequencing",
        "code": code,
        "next_tool": next_tool,
        "message": message,
    }


# ---------------------------------------------------------------------------
# Task 14: autopilot (M6). The internal LLM prompt templates (drafting,
# critiquing-as-lens, refining, scoring, playing a specialist) live HERE with
# every other template -- `autopilot.py` owns the driving logic, `prompts.py`
# owns every word, the same division Tasks 7/11/13 established. Each builder
# returns an OpenAI-style `messages` list; its SYSTEM message opens with a
# stable phrase identifying the phase.
# ---------------------------------------------------------------------------

# The 7 utility dimensions the scoring prompt demands, in the exact form
# `tolerant.parse_scores` -> the engines accept (mirrors serial_engine.DIMENSIONS).
_AUTOPILOT_DIMENSIONS: tuple[str, ...] = (
    "correctness",
    "evidence",
    "novelty",
    "clarity",
    "bias_resistance",
    "actionability",
    "coverage",
)


def _prior_block(prior_context: str) -> str:
    return f"\n\nEstablished context from earlier stages:\n{prior_context}" if prior_context else ""


def autopilot_draft_messages(
    question: str, stage: str, prior_context: str
) -> list[dict[str, str]]:
    """Prompt to draft the initial thought for a stage (when no
    `initial_content` was supplied)."""
    return [
        {
            "role": "system",
            "content": (
                "You are an automated reasoning engine drafting the initial thought "
                "for one stage of a structured deep-think process. Reply with the "
                "draft prose only -- no preamble, no headings, no meta commentary."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Question:\n{question}\n\nCurrent stage: {stage}."
                f"{_prior_block(prior_context)}\n\n"
                "Write the strongest initial draft thought for this stage."
            ),
        },
    ]


def autopilot_critique_messages(
    question: str, stage: str, draft: str, lens_name: str, lens_template: str
) -> list[dict[str, str]]:
    """Prompt to critique the current draft through one lens. The lens template
    (from the discovered library) IS the critique instruction -- reused verbatim,
    the same text the manual `critique_current_thought` hands the calling model."""
    return [
        {
            "role": "system",
            "content": (
                "You are an automated reasoning engine applying a single critique lens "
                "to a draft thought. Follow the lens instructions exactly and reply "
                "with the critique only."
            ),
        },
        {
            "role": "user",
            "content": (
                f"{lens_template}\n\n---\nStage: {stage}. Critique lens: {lens_name}.\n"
                f"Draft thought to critique:\n{draft}\n\n"
                "Write your critique of the draft above, following the lens instructions."
            ),
        },
    ]


def autopilot_refine_messages(
    question: str, stage: str, draft: str, critique: str
) -> list[dict[str, str]]:
    """Prompt to rewrite the draft addressing the critique."""
    return [
        {
            "role": "system",
            "content": (
                "You are an automated reasoning engine revising a draft thought to "
                "address a critique. Reply with the full rewritten thought only -- "
                "keep what is strong, fix what the critique exposed."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Question:\n{question}\n\nStage: {stage}.\n\n"
                f"Current draft:\n{draft}\n\nCritique to address:\n{critique}\n\n"
                "Rewrite the thought to resolve the critique."
            ),
        },
    ]


def autopilot_score_messages(content: str) -> list[dict[str, str]]:
    """Prompt to self-score a thought on the 7 utility dimensions. Demands the
    exact JSON shape `tolerant.parse_scores` accepts (dimension -> 0..1)."""
    dims = ", ".join(_AUTOPILOT_DIMENSIONS)
    example = '{"correctness": 0.8, "evidence": 0.7, "novelty": 0.6, "clarity": 0.8, "bias_resistance": 0.7, "actionability": 0.6, "coverage": 0.7}'
    return [
        {
            "role": "system",
            "content": (
                "You are an automated reasoning engine scoring a thought on seven "
                "utility dimensions. Reply with ONLY a JSON object -- no prose, no "
                "code fence."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Reasoning to score:\n{content}\n\n"
                f"Score it on these seven dimensions, each a number from 0.0 to 1.0: "
                f"{dims}. Respond with ONLY a JSON object mapping each dimension name "
                f"to its score. For example: {example}"
            ),
        },
    ]


def autopilot_specialist_messages(specialist_prompt: str) -> list[dict[str, str]]:
    """Prompt handing the autopilot one manual specialist's framing to voice.
    `specialist_prompt` is the exact text `manual_engine` builds (via
    `build_manual_specialist_prompt`); the autopilot replies with that
    specialist's candidate thought only (scoring is a separate score call)."""
    return [
        {
            "role": "system",
            "content": (
                "You are an automated reasoning engine voicing one specialist "
                "perspective in a multi-agent deep-think process. Reply with that "
                "specialist's candidate thought only -- prose, no scores, no preamble."
            ),
        },
        {"role": "user", "content": specialist_prompt},
    ]


# ---- autopilot result / directive payloads ----


def stage_autopilot_committed(session: Session, outcome: AutopilotOutcome) -> dict[str, Any]:
    """`run_stage_autopilot` success: the server drove the serial loop to
    convergence and committed a thought."""
    return {
        "session_id": session.id,
        "mode": "serial",
        "autopilot": True,
        "stage": outcome.stage,
        "thought_id": outcome.thought_id,
        "committed": True,
        "rounds": outcome.rounds,
        "converged_reason": outcome.converged_reason,
        "final_content": outcome.final_content,
        "completed_steps": outcome.completed_steps,
        "next_tool": "next_action",
        "message": (
            f"Autopilot drove the serial critique loop to convergence "
            f"({outcome.converged_reason}) over {outcome.rounds} round(s) and "
            f"committed the thought at stage '{outcome.stage}'. Call "
            "next_action(session_id) for the next step (another thought, "
            "advance_stage, or finalize_session)."
        ),
    }


def subagent_autopilot_committed(session: Session, outcome: AutopilotOutcome) -> dict[str, Any]:
    """`run_subagent_autopilot` success: the server drove the subagent path
    (necort or manual) to a committed thought."""
    strength = round(outcome.strength, 3) if outcome.strength is not None else None
    return {
        "session_id": session.id,
        "mode": "subagent",
        "autopilot": True,
        "engine": outcome.engine,
        "stage": outcome.stage,
        "thought_id": outcome.thought_id,
        "committed": True,
        "rounds": outcome.rounds,
        "equilibrium_strength": strength,
        "gate_metric": outcome.metric_label,
        "commit_threshold": outcome.threshold,
        "converged": outcome.converged,
        "budget_exhausted": outcome.budget_exhausted,
        "final_content": outcome.final_content,
        "completed_steps": outcome.completed_steps,
        "next_tool": "next_action",
        "message": (
            f"Autopilot drove the {outcome.engine} subagent loop over "
            f"{outcome.rounds} round(s) and committed the winning candidate at "
            f"stage '{outcome.stage}'. Call next_action(session_id) for the next "
            "step (another thought, advance_stage, or finalize_session)."
        ),
    }


def autopilot_stopped(session_id: str, outcome: AutopilotOutcome) -> dict[str, Any]:
    """Partial-progress directive: autopilot stopped part-way (an endpoint fault
    or unparseable LLM output). Everything completed so far is already
    persisted; the caller can resume manually via `next_action`. Never a
    traceback."""
    done = ", ".join(outcome.completed_steps) if outcome.completed_steps else "nothing yet"
    return {
        "session_id": session_id,
        "error": "autopilot_incomplete",
        "retryable": True,
        "stopped_phase": outcome.stopped_phase,
        "stopped_detail": outcome.stopped_detail,
        "completed_steps": outcome.completed_steps,
        "committed_thought_ids": outcome.committed_thought_ids,
        "rounds": outcome.rounds,
        "next_tool": "next_action",
        "message": (
            f"Autopilot stopped during the '{outcome.stopped_phase}' step "
            f"({outcome.stopped_detail}). Progress so far is saved ({done}). "
            "Continue manually: call next_action(session_id) for the exact next "
            "tool, or re-run autopilot to retry from here."
        ),
    }


def autopilot_unavailable(session_id: str) -> dict[str, Any]:
    """Autopilot is enabled but its optional HTTP dependency (httpx) is missing.
    A directive pointing at the fix / the manual path -- never a raw ImportError."""
    return {
        "session_id": session_id,
        "error": "autopilot_unavailable",
        "next_tool": "next_action",
        "message": (
            "Autopilot is enabled but its HTTP client dependency (httpx) is not "
            "installed. Install the optional 'autopilot' extra (e.g. "
            "`uv sync --extra autopilot`), or drive the loop manually with the "
            "standard step tools -- next_action(session_id) shows each step."
        ),
    }


def autopilot_stage_mismatch(
    session_id: str, requested_stage: str, current_stage: str
) -> dict[str, Any]:
    """`run_*_autopilot(stage=...)` named a stage other than the session's
    current one. Autopilot never silently jumps the stage cursor -- it directs
    the caller to advance first (or to omit `stage`)."""
    return {
        "session_id": session_id,
        "error": "stage_mismatch",
        "requested_stage": requested_stage,
        "current_stage": current_stage,
        "next_tool": "advance_stage",
        "message": (
            f"Autopilot was asked to run stage '{requested_stage}', but this "
            f"session is at stage '{current_stage}'. Advance to the target stage "
            "with advance_stage(session_id) first, or omit `stage` to run the "
            "current one."
        ),
    }
