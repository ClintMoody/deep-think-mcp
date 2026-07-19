"""deep-think-mcp: MCP server -- tool registration + dispatch.

`create_server(root)` builds a FastMCP server instance and registers the
session-lifecycle tools (Layer 6 -- the lifecycle-manager half this task
owns; finalize/move/keep arrive in Task 4) per
`docs/build-plan.md` § "Tool API surface" > "Session lifecycle".

The other half of this module is `mode_gate`: Layer 2 (mode dispatcher) per
`docs/build-plan.md` § "Architecture at a glance" -- "Reads session.mode and
routes tool calls to either the serial engine or the subagent engine." Task
3 registers no thought tools of its own (engines arrive in Tasks 7 and 11),
but the mode-selection contract ("any thought tool called while mode is
None returns a directive payload") must be enforced *centrally*, not
per-tool, so those later tasks inherit it for free instead of re-deriving
it. `mode_gate` is that reusable enforcement point -- see its docstring for
the exact usage contract engine tasks are expected to follow.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any, Callable, Literal

from mcp.server.fastmcp import FastMCP

from deep_think_mcp import config, index, prompts, store
from deep_think_mcp.session import Session

SERVER_NAME = "deep-think-mcp"


def _load_session(
    data_root: Path, session_id: str
) -> tuple[Session, None] | tuple[None, dict[str, Any]]:
    """Look up `session_id` via the index and load it from its current path.

    Returns `(session, None)` on success, or `(None, error_payload)` if the
    id isn't in the index. Looking up via the index (rather than assuming
    the default `sessions/<id>.json` location) is what keeps this working
    after Task 4's `move_session` relocates a session's file outside root.
    """
    entry = index.get(data_root, session_id)
    if entry is None:
        return None, prompts.session_not_found(session_id)
    return store.load(entry["path"]), None


def mode_gate(
    data_root: Path | str,
) -> Callable[[Callable[..., dict[str, Any]]], Callable[..., dict[str, Any]]]:
    """Decorator factory enforcing the mode-selection contract centrally.

    Apply to any "thought tool" whose first parameter is `session_id: str`
    (the convention every tool in `docs/build-plan.md` § "Thought loop"
    follows), *underneath* `@mcp.tool()`:

        @mcp.tool()
        @mode_gate(data_root)
        def begin_thought(session_id: str, content: str) -> dict[str, Any]:
            ...

    Before the wrapped function ever runs: looks the session up (returning
    a `session_not_found` payload if unknown), then checks `session.mode`.
    If it's still `None`, short-circuits with a directive payload pointing
    at `set_session_mode` -- the wrapped function never executes. This is
    the single enforcement point Tasks 7 (serial engine) and 11 (subagent
    engine) are expected to route every thought-loop tool through, so the
    gate only has to be right once.

    `functools.wraps` preserves the wrapped function's real signature
    (verified against the SDK: FastMCP's schema introspection follows
    `__wrapped__`), so `@mcp.tool()` above this decorator still sees the
    original parameter names/types, not a generic `*args, **kwargs`.
    """
    root = Path(data_root).expanduser().resolve()

    def decorator(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
        @functools.wraps(fn)
        def wrapper(session_id: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
            session, error = _load_session(root, session_id)
            if error is not None:
                return error
            if session.mode is None:
                return prompts.mode_required(session_id, blocked_tool=fn.__name__)
            return fn(session_id, *args, **kwargs)

        return wrapper

    return decorator


def _register_lifecycle_tools(mcp: FastMCP, data_root: Path) -> None:
    """Register the six session-lifecycle tools per
    `docs/execution-plan.md` Task 3. None of these are gated by
    `mode_gate` -- they're exactly the tools that must keep working
    *before* a mode is chosen (`start_session`, `set_session_mode`,
    `list_modes`) or regardless of it (`resume_session`, `list_sessions`,
    `clear_session`).
    """

    @mcp.tool()
    def start_session(
        question: str,
        mode: Literal["serial", "subagent"] | None = None,
        stages: list[str] | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a new session. Bootstraps the data store on first use.

        Without `mode`, returns a mode-required directive payload. With a
        valid `mode`, the session is created with that mode already set and
        the tool proceeds immediately -- no separate `set_session_mode`
        call needed.
        """
        config.bootstrap(data_root)
        cfg = config.load_config(root=data_root, overrides=overrides)
        expected_stages = list(stages) if stages else list(cfg["stages"]["default"])

        session = Session(
            question=question,
            mode=mode,
            expected_stages=expected_stages,
            current_stage=expected_stages[0],
            overrides=overrides or {},
        )
        session.save_path = str(store.session_path(data_root, session.id))
        store.save(session, session.save_path)
        index.upsert(data_root, session)

        if mode is None:
            return prompts.mode_required(session.id)
        return prompts.session_started(session)

    @mcp.tool()
    def set_session_mode(
        session_id: str, mode: Literal["serial", "subagent"]
    ) -> dict[str, Any]:
        """Set a session's mode. Only succeeds if no mode is set yet --
        once set, mode is immutable for the life of the session.
        """
        session, error = _load_session(data_root, session_id)
        if error is not None:
            return error
        if session.mode is not None:
            return prompts.mode_already_set(session_id, session.mode)

        session.mode = mode
        store.save(session, session.save_path)
        index.upsert(data_root, session)
        return prompts.mode_set(session)

    @mcp.tool()
    def list_modes() -> dict[str, Any]:
        """Return both modes' descriptions + recommendations, for the
        model to relay to the user.
        """
        return prompts.list_modes()

    @mcp.tool()
    def resume_session(session_id: str) -> dict[str, Any]:
        """Return a session's persisted state."""
        session, error = _load_session(data_root, session_id)
        if error is not None:
            return error
        return prompts.session_resumed(session)

    @mcp.tool()
    def list_sessions() -> dict[str, Any]:
        """List every session in the index."""
        return prompts.session_list(index.list_all(data_root))

    @mcp.tool()
    def clear_session(session_id: str) -> dict[str, Any]:
        """Wipe a session: deletes its file and removes it from the index."""
        entry = index.get(data_root, session_id)
        if entry is None:
            return prompts.session_not_found(session_id)
        Path(entry["path"]).unlink(missing_ok=True)
        index.remove(data_root, session_id)
        return prompts.session_cleared(session_id)


def create_server(root: Path | str | None = None) -> FastMCP:
    """Build a fresh `deep-think-mcp` FastMCP server instance.

    `root` is the data root every tool call on this server instance
    operates against; defaults to `config.resolve_root()`. Tests always
    pass an explicit tmp root (Global Constraints: never touch the real
    home directory); the real entrypoint (`main()`) lets it default.
    """
    data_root = Path(root).expanduser().resolve() if root is not None else config.resolve_root()
    mcp = FastMCP(SERVER_NAME)
    _register_lifecycle_tools(mcp, data_root)
    return mcp


def main() -> None:
    """Stdio entrypoint: `uv run python -m deep_think_mcp.server`."""
    create_server().run()


if __name__ == "__main__":
    main()
