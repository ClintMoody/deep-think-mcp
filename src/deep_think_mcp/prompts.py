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

from typing import Any

from deep_think_mcp.session import Session

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
