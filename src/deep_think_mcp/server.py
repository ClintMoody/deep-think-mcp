"""deep-think-mcp: MCP server -- tool registration + dispatch.

`create_server(root)` builds a FastMCP server instance and registers the
session-lifecycle tools (Layer 6, the lifecycle manager) per
`docs/build-plan.md` § "Tool API surface" > "Session lifecycle": the six
Task 3 tools (`start_session`, `set_session_mode`, `list_modes`,
`resume_session`, `list_sessions`, `clear_session`) plus Task 4's finalize
/ move / keep-here trio (`finalize_session`, `move_session`, `keep_here`).
The finalize/move/keep *logic* lives in `lifecycle.py`; this module only
loads/persists sessions around calls into it and maps results to
`prompts.py` templates -- same division of labor `store.py`/`index.py`
already establish.

The other half of this module is `mode_gate`: Layer 2 (mode dispatcher) per
`docs/build-plan.md` § "Architecture at a glance" -- "Reads session.mode and
routes tool calls to either the serial engine or the subagent engine." Task
3 registers no thought tools of its own (engines arrive in Tasks 7 and 11),
but the mode-selection contract ("any thought tool called while mode is
None returns a directive payload") must be enforced *centrally*, not
per-tool, so those later tasks inherit it for free instead of re-deriving
it. `mode_gate` is that reusable enforcement point -- see its docstring for
the exact usage contract engine tasks are expected to follow.

Task 5 adds Layer 3 (the stage machine)'s one tool, `advance_stage`. Its
cursor logic lives in `stages.py`; this module only registers the tool,
gated by `mode_gate` -- see `_register_stage_tools` for why a stage-machine
tool, not a thought-loop tool, is gated the same way.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any, Callable, Literal

from mcp.server.fastmcp import FastMCP

from deep_think_mcp import (
    config,
    index,
    lens_loader,
    lifecycle,
    prompts,
    serial_engine,
    stages,
    store,
)
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
    require_mode: Literal["serial", "subagent"] | None = None,
) -> Callable[[Callable[..., dict[str, Any]]], Callable[..., dict[str, Any]]]:
    """Decorator factory enforcing the mode contract centrally.

    Apply to any "thought tool" whose first parameter is `session_id: str`
    (the convention every tool in `docs/build-plan.md` § "Thought loop"
    follows), *underneath* `@mcp.tool()`:

        @mcp.tool()
        @mode_gate(data_root, require_mode="serial")
        def begin_thought(session_id: str, content: str) -> dict[str, Any]:
            ...

    Before the wrapped function ever runs it looks the session up (returning
    a `session_not_found` payload if unknown), then enforces two distinct
    conditions the gate deliberately keeps separate:

      1. *No mode yet* (`session.mode is None`): short-circuits with the
         `mode_required` directive pointing at `set_session_mode`. This is
         the Task 3 contract, applied to every thought tool for free.
      2. *Wrong mode* (`require_mode` set and `session.mode != require_mode`):
         short-circuits with the `wrong_mode` directive. This is Task 7's
         addition -- the original gate only handled case 1, so it is
         extended here (minimally, backward-compatibly) rather than adding a
         separate per-tool guard: `require_mode=None` preserves the exact
         old behavior for `advance_stage` and any non-mode-specific tool,
         while serial tools pass `require_mode="serial"` and Task 11's
         subagent tools will pass `require_mode="subagent"`. Keeping both
         checks in the one gate means "no mode" vs "wrong mode" stay two
         clearly different directives, and every future engine tool inherits
         both for free.

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
            if require_mode is not None and session.mode != require_mode:
                return prompts.wrong_mode(
                    session_id,
                    required_mode=require_mode,
                    current_mode=session.mode,
                    blocked_tool=fn.__name__,
                )
            return fn(session_id, *args, **kwargs)

        return wrapper

    return decorator


def _register_lifecycle_tools(mcp: FastMCP, data_root: Path) -> None:
    """Register the nine session-lifecycle tools: the six from
    `docs/execution-plan.md` Task 3, plus Task 4's finalize/move/keep-here
    trio. None of these are gated by `mode_gate` -- they're exactly the
    tools that must keep working *before* a mode is chosen
    (`start_session`, `set_session_mode`, `list_modes`) or regardless of it
    (`resume_session`, `list_sessions`, `clear_session`,
    `finalize_session`, `move_session`, `keep_here`).
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

    @mcp.tool()
    def finalize_session(session_id: str) -> dict[str, Any]:
        """Mark a session finalized. Returns the finalize+move payload:
        where the session is saved, the canned human_prompt asking whether
        to move it, and the two tools (`move_session`, `keep_here`) that
        answer that question.
        """
        session, error = _load_session(data_root, session_id)
        if error is not None:
            return error
        session = lifecycle.finalize(session)
        store.save(session, session.save_path)
        index.upsert(data_root, session)
        return prompts.session_finalized(session)

    @mcp.tool()
    def move_session(session_id: str, new_path: str, force: bool = False) -> dict[str, Any]:
        """Move a session's file to `new_path`.

        `new_path` must be an absolute path (`~` is expanded). If it names
        an existing directory, the session moves into that directory under
        its current filename. Fails cleanly -- without touching the
        session or the filesystem -- if the destination already exists
        (unless `force=true`), isn't writable, or doesn't exist.
        """
        session, error = _load_session(data_root, session_id)
        if error is not None:
            return error
        try:
            session = lifecycle.move(session, new_path, force=force)
        except lifecycle.MoveError as exc:
            return prompts.move_failed(session_id, exc.code, exc.message)
        # By this point the move itself is done and verified: the session
        # file exists at its new path and `session` (including
        # move_history/save_path) reflects it -- lifecycle.move() already
        # persisted that. Only the index needs to catch up, and that's a
        # separate write (lock contention, disk-full, permissions) that
        # must not turn a successful move into a crashed tool call or a
        # session the index still points at a now-deleted path. Catching
        # broadly here is deliberate: any failure of this specific call
        # (OSError, portalocker's LockException, ...) must degrade to a
        # clean directive payload, never a raw traceback.
        try:
            index.upsert(data_root, session)
        except Exception as exc:  # noqa: BLE001 -- see comment above
            return prompts.move_index_update_failed(session, str(exc), index.index_path(data_root))
        return prompts.session_moved(session)

    @mcp.tool()
    def keep_here(session_id: str) -> dict[str, Any]:
        """Record that the user declined to move the session. No
        filesystem change.
        """
        session, error = _load_session(data_root, session_id)
        if error is not None:
            return error
        session = lifecycle.keep_here(session)
        store.save(session, session.save_path)
        index.upsert(data_root, session)
        return prompts.session_kept(session)


def _register_stage_tools(mcp: FastMCP, data_root: Path) -> None:
    """Register Layer 3's one tool: `advance_stage`.

    Gated by `mode_gate`, like every future thought-loop tool (Tasks 7,
    11) -- even though `advance_stage` isn't itself a thought tool. This
    is a Task 5 judgment call the brief doesn't spell out: a session with
    `mode is None` has no engine to hand its committed thoughts to yet, so
    "which stage are we reasoning in" has no meaningful answer either --
    gating it keeps the mode-required directive the very first thing a
    caller sees for *any* session-state-progressing tool, not just the
    ones that happen to exist already. See `.superpowers/sdd/task-5-
    report.md` for the full reasoning, flagged there for reviewer
    sign-off.
    """

    @mcp.tool()
    @mode_gate(data_root)
    def advance_stage(session_id: str) -> dict[str, Any]:
        """Advance the session's stage cursor to the next stage in its
        `expected_stages`. Fails cleanly with a directive payload pointing
        at `finalize_session` if the session is already at its final
        stage -- there is nowhere further to advance to.
        """
        session, error = _load_session(data_root, session_id)
        if error is not None:
            return error
        try:
            session = stages.advance(session)
        except stages.FinalStageReachedError:
            return prompts.final_stage_reached(session)
        store.save(session, session.save_path)
        index.upsert(data_root, session)
        return prompts.stage_advanced(session)


def _register_serial_tools(mcp: FastMCP, data_root: Path) -> None:
    """Register Task 7's six serial-engine thought tools (Layer 4, serial
    mode). Each is gated by `mode_gate(data_root, require_mode="serial")`:
    a session with no mode gets the `mode_required` directive, a
    subagent-mode session gets the `wrong_mode` directive, and neither ever
    reaches the engine. The loop logic lives in `serial_engine.py`; these
    wrappers only load the session, call the engine, map any
    `SerialSequencingError` to a directive payload (never a raw error), and
    -- on success -- persist and format the result via `prompts.py`. This is
    the same load/mutate/persist division `_register_stage_tools` follows.
    """

    def _persist(session: Session) -> None:
        store.save(session, session.save_path)
        index.upsert(data_root, session)

    def _cfg(session: Session) -> dict[str, Any]:
        return config.load_config(root=data_root, overrides=session.overrides)

    @mcp.tool()
    @mode_gate(data_root, require_mode="serial")
    def begin_thought(
        session_id: str,
        content: str,
        tags: list[str] | None = None,
        axioms: list[str] | None = None,
    ) -> dict[str, Any]:
        """Draft a new thought in the session's current stage. Fails with a
        directive if a thought is already in progress (commit it first).
        """
        session, error = _load_session(data_root, session_id)
        if error is not None:
            return error
        try:
            thought = serial_engine.begin_thought(session, content, tags, axioms)
        except serial_engine.SerialSequencingError as exc:
            return prompts.serial_directive(session_id, exc.code, **exc.detail)
        _persist(session)
        return prompts.thought_begun(session, thought)

    @mcp.tool()
    @mode_gate(data_root, require_mode="serial")
    def critique_current_thought(
        session_id: str, lens: str | None = None
    ) -> dict[str, Any]:
        """Open a critique round and return the lens template to apply. Omit
        `lens` to let the server pick a stage-appropriate one and rotate
        through the library. The response places the current draft content
        immediately before the lens template (adjacency contract).
        """
        session, error = _load_session(data_root, session_id)
        if error is not None:
            return error
        available = lens_loader.discover_lenses(data_root)
        try:
            prompt = serial_engine.start_critique(
                session, lens, available, _cfg(session)
            )
        except serial_engine.SerialSequencingError as exc:
            return prompts.serial_directive(session_id, exc.code, **exc.detail)
        _persist(session)
        return prompts.critique_ready(session_id, prompt)

    @mcp.tool()
    @mode_gate(data_root, require_mode="serial")
    def submit_critique(session_id: str, text: str) -> dict[str, Any]:
        """Record the critique produced by applying the current lens."""
        session, error = _load_session(data_root, session_id)
        if error is not None:
            return error
        try:
            rnd = serial_engine.submit_critique(session, text)
        except serial_engine.SerialSequencingError as exc:
            return prompts.serial_directive(session_id, exc.code, **exc.detail)
        _persist(session)
        return prompts.critique_submitted(session_id, rnd.round_index, rnd.lens)

    @mcp.tool()
    @mode_gate(data_root, require_mode="serial")
    def refine_current_thought(
        session_id: str,
        new_content: str,
        challenged_assumptions: list[str] | None = None,
    ) -> dict[str, Any]:
        """Rewrite the thought to address the critique. The server records
        the new version and its normalized edit distance vs. the prior one.
        """
        session, error = _load_session(data_root, session_id)
        if error is not None:
            return error
        try:
            rnd, edit_distance = serial_engine.refine_current_thought(
                session, new_content, challenged_assumptions
            )
        except serial_engine.SerialSequencingError as exc:
            return prompts.serial_directive(session_id, exc.code, **exc.detail)
        _persist(session)
        return prompts.thought_refined(session_id, rnd.round_index, edit_distance)

    @mcp.tool()
    @mode_gate(data_root, require_mode="serial")
    def score_current_thought(
        session_id: str, scores: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Self-score the refined thought across the 7 utility dimensions
        (partial input is tolerated -- missing dims carry forward). Returns
        the convergence verdict: whether to commit or run another lens.
        """
        session, error = _load_session(data_root, session_id)
        if error is not None:
            return error
        try:
            result = serial_engine.score_current_thought(
                session, scores or {}, _cfg(session)
            )
        except serial_engine.SerialSequencingError as exc:
            return prompts.serial_directive(session_id, exc.code, **exc.detail)
        _persist(session)
        return prompts.thought_scored(session_id, result)

    @mcp.tool()
    @mode_gate(data_root, require_mode="serial")
    def commit_thought(session_id: str) -> dict[str, Any]:
        """Lock the current thought (writing its final refined content back
        as the thought's content) and clear the current-thought cursor.
        Fails with a directive if no critique round has completed yet.
        """
        session, error = _load_session(data_root, session_id)
        if error is not None:
            return error
        try:
            thought = serial_engine.commit_thought(session)
        except serial_engine.SerialSequencingError as exc:
            return prompts.serial_directive(session_id, exc.code, **exc.detail)
        _persist(session)
        return prompts.thought_committed(session, thought)


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
    _register_stage_tools(mcp, data_root)
    _register_serial_tools(mcp, data_root)
    return mcp


def main() -> None:
    """Stdio entrypoint: `uv run python -m deep_think_mcp.server`."""
    create_server().run()


if __name__ == "__main__":
    main()
