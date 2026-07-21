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

import argparse
import asyncio
import functools
import os
import re
import uuid
from pathlib import Path
from typing import Any, Callable, Literal

from mcp.server.fastmcp import FastMCP
from portalocker.exceptions import LockException

from deep_think_mcp import (
    autopilot,
    config,
    index,
    lens_loader,
    lifecycle,
    manual_engine,
    meta,
    prompts,
    serial_engine,
    stages,
    store,
    subagent_engine,
    tolerant,
)
from deep_think_mcp.session import Session
from deep_think_mcp.tolerant import TolerantParseError

SERVER_NAME = "deep-think-mcp"

# [F6 SECURITY] A session id is used verbatim to build the on-disk save path
# (`store.session_path`). Only a plain token (letters/digits) is accepted on
# import, so a crafted id can never smuggle path separators or `..` that would
# let the write escape the data root's `sessions/` dir. Every id this server
# mints is `uuid4().hex`, which matches.
_SAFE_SESSION_ID_RE = re.compile(r"\A[A-Za-z0-9]+\Z")


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

    Async support (T11): the subagent engine's `run()` is `async` (it offloads
    the vendored blocking Nash I/O onto a worker thread), so its two tools are
    `async def`. `mode_gate` was originally sync-only; it is extended here
    MINIMALLY -- when the wrapped function is a coroutine function it returns an
    `async` wrapper that `await`s it, otherwise the original sync wrapper is
    unchanged. The gate check itself (`_load_session` + mode checks) is a fast
    local read either way and stays synchronous inside the async wrapper. This
    is the `docs/execution-plan.md` Task 7 `[derived]` note's anticipated
    extension; sync engine tools (serial, `advance_stage`) are entirely
    unaffected.
    """
    root = Path(data_root).expanduser().resolve()

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        def _gate(session_id: str) -> dict[str, Any] | None:
            """Run the mode-gate checks; return a directive payload to
            short-circuit with, or None to let the wrapped function proceed."""
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
            return None

        if asyncio.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(session_id: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
                blocked = _gate(session_id)
                if blocked is not None:
                    return blocked
                return await fn(session_id, *args, **kwargs)

            return async_wrapper

        @functools.wraps(fn)
        def wrapper(session_id: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
            blocked = _gate(session_id)
            if blocked is not None:
                return blocked
            return fn(session_id, *args, **kwargs)

        return wrapper

    return decorator


def storage_guard(
    fn: Callable[..., Any],
) -> Callable[..., Any]:
    """Tool-boundary decorator turning a storage fault into a directive.

    Task 13 hardening item #6: the `.bak`-protocol persistence layer
    (`store.py`, `index.py`) guards every mutation with a Portalocker lock,
    and a lock that can't be acquired within its timeout raises
    `portalocker.exceptions.LockException` -- which is NOT an `OSError`
    subclass. The existing per-tool catches are all shaped for `OSError`
    (or `lifecycle.MoveError`), so a lock timeout in `finalize` / `keep_here`
    / `clear` / `import` / a `move`'s internal `store.save`, or in any
    engine tool's persist step, would escape as a raw traceback -- exactly
    what the local-model directive design forbids. This wraps a tool so any
    `LockException` (or bare `OSError`) that reaches the boundary degrades to
    a clean, retryable `storage_unavailable` directive instead.

    [F1] Also catches the `ValueError` family so a *corrupt* session/index
    file at the load boundary degrades the same way: `store.load` raises
    pydantic `ValidationError` (a `ValueError` subclass) on invalid session
    JSON, and `index._read_locked` raises `json.JSONDecodeError` (likewise a
    `ValueError`) on a corrupt `index.json` with no recoverable `.bak` --
    both are storage faults, not programming bugs. Genuine `TypeError`s (real
    programming errors) are deliberately left to surface.

    Stacked OUTSIDE `mode_gate` (so it also catches faults in the gate's own
    session load) and BELOW `@mcp.tool()` (so FastMCP still introspects the
    real signature via the `functools.wraps` `__wrapped__` chain, same
    mechanism `mode_gate` relies on). Errors a tool already handles and
    returns as a payload are untouched -- only a genuinely-escaped storage
    fault is caught here.
    """

    def _session_id(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str | None:
        if "session_id" in kwargs:
            return kwargs["session_id"]
        if args and isinstance(args[0], str):
            return args[0]
        return None

    if asyncio.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
            try:
                return await fn(*args, **kwargs)
            except (LockException, OSError, ValueError) as exc:
                return prompts.storage_unavailable(
                    _session_id(args, kwargs), f"{type(exc).__name__}: {exc}"
                )

        return async_wrapper

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return fn(*args, **kwargs)
        except (LockException, OSError, ValueError) as exc:
            return prompts.storage_unavailable(
                _session_id(args, kwargs), f"{type(exc).__name__}: {exc}"
            )

    return wrapper


def _clarify(
    exc: TolerantParseError,
    *,
    session_id: str | None = None,
    next_tool: str | None = None,
) -> dict[str, Any]:
    """Map a `tolerant.TolerantParseError` to the `retry_with_clarification`
    directive -- the single boundary translation for every tolerant-parse
    failure across the tool surface (Task 13 Half A).
    """
    return prompts.retry_with_clarification(
        exc.param, exc.expected, exc.example, session_id=session_id, next_tool=next_tool
    )


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
    @storage_guard
    def start_session(
        question: str,
        mode: Literal["serial", "subagent"] | None = None,
        stages: list[str] | str | None = None,
        overrides: dict[str, Any] | str | None = None,
    ) -> dict[str, Any]:
        """Create a new session. Bootstraps the data store on first use.

        Without `mode`, returns a mode-required directive payload. With a
        valid `mode`, the session is created with that mode already set and
        the tool proceeds immediately -- no separate `set_session_mode`
        call needed. `stages` accepts a JSON array or a comma/newline
        list; `overrides` a JSON object or its string form (tolerant input,
        Task 13).
        """
        try:
            stages = tolerant.parse_string_list(stages, param="stages")
            overrides = tolerant.parse_json_or_text(overrides, param="overrides")
        except TolerantParseError as exc:
            return _clarify(exc, next_tool="start_session")
        # [task 13 hardening #4] Reject duplicate custom stage names: stage
        # names key the position/cursor logic (stages.advance, per-stage
        # thought positions), so a duplicate would make "which stage are we in"
        # ambiguous. Caught here at the boundary with a retry_with_clarification
        # rather than corrupting the session's stage machine.
        if stages:
            dupes = sorted({s for s in stages if stages.count(s) > 1})
            if dupes:
                return prompts.retry_with_clarification(
                    "stages",
                    expected="a list of UNIQUE stage names",
                    example='["Problem Definition", "Analysis", "Conclusion"]',
                    next_tool="start_session",
                )

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
    @storage_guard
    def set_session_mode(
        session_id: str, mode: Literal["serial", "subagent"]
    ) -> dict[str, Any]:
        """Set a session's mode. Only succeeds if no mode is set yet --
        once set, mode is immutable for the life of the session.
        """
        session, error = _load_session(data_root, session_id)
        if error is not None:
            return error
        # [task 13 hardening #2] The load-check-mutate-save below is not atomic
        # across the two Portalocker acquisitions (`_load_session`'s read, then
        # `store.save`'s write), so in principle two truly-concurrent
        # set_session_mode calls could both observe `mode is None` and race.
        # This is documented as an ACCEPTED single-client limitation, not
        # code-defended: deep-think-mcp speaks MCP over a stdio transport, which
        # is one client per server process issuing strictly serialized tool
        # calls -- there is no concurrent second caller to race against. A true
        # fix (holding one lock across read+write) would mean threading a
        # session-scoped lock through store.py for a race that the transport
        # model already precludes. See .superpowers/sdd/task-13-report.md.
        if session.mode is not None:
            return prompts.mode_already_set(session_id, session.mode)

        session.mode = mode
        store.save(session, session.save_path)
        index.upsert(data_root, session)
        return prompts.mode_set(session)

    @mcp.tool()
    @storage_guard
    def list_modes() -> dict[str, Any]:
        """Return both modes' descriptions + recommendations, for the
        model to relay to the user.
        """
        return prompts.list_modes()

    @mcp.tool()
    @storage_guard
    def resume_session(session_id: str) -> dict[str, Any]:
        """Return a session's persisted state."""
        session, error = _load_session(data_root, session_id)
        if error is not None:
            return error
        return prompts.session_resumed(session)

    @mcp.tool()
    @storage_guard
    def list_sessions() -> dict[str, Any]:
        """List every session in the index."""
        return prompts.session_list(index.list_all(data_root))

    @mcp.tool()
    @storage_guard
    def clear_session(session_id: str) -> dict[str, Any]:
        """Wipe a session: deletes its file and removes it from the index."""
        entry = index.get(data_root, session_id)
        if entry is None:
            return prompts.session_not_found(session_id)
        Path(entry["path"]).unlink(missing_ok=True)
        index.remove(data_root, session_id)
        return prompts.session_cleared(session_id)

    @mcp.tool()
    @storage_guard
    def finalize_session(session_id: str) -> dict[str, Any]:
        """Mark a session finalized. Returns the finalize+move payload:
        where the session is saved, the canned human_prompt asking whether
        to move it, and the two tools (`move_session`, `keep_here`) that
        answer that question.
        """
        session, error = _load_session(data_root, session_id)
        if error is not None:
            return error
        uncommitted = lifecycle.has_uncommitted_thought(session)
        session = lifecycle.finalize(session)
        store.save(session, session.save_path)
        index.upsert(data_root, session)
        return prompts.session_finalized(session, uncommitted_thought=uncommitted)

    @mcp.tool()
    @storage_guard
    def move_session(
        session_id: str, new_path: str, force: bool | str | None = False
    ) -> dict[str, Any]:
        """Move a session's file to `new_path`.

        `new_path` must be an absolute path (`~` is expanded). If it names
        an existing directory, the session moves into that directory under
        its current filename. Fails cleanly -- without touching the
        session or the filesystem -- if the destination already exists
        (unless `force=true`), isn't writable, or doesn't exist. `force`
        accepts a real bool or a word ("true"/"yes"/...) -- tolerant input.
        """
        try:
            force = bool(tolerant.parse_bool(force, param="force"))
        except TolerantParseError as exc:
            return _clarify(exc, session_id=session_id, next_tool="move_session")
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
    @storage_guard
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
    @storage_guard
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
    @storage_guard
    @mode_gate(data_root, require_mode="serial")
    def begin_thought(
        session_id: str,
        content: str,
        tags: list[str] | str | None = None,
        axioms: list[str] | str | None = None,
    ) -> dict[str, Any]:
        """Draft a new thought in the session's current stage. Fails with a
        directive if a thought is already in progress (commit it first).
        `tags`/`axioms` accept a JSON array or a comma/newline list.
        """
        session, error = _load_session(data_root, session_id)
        if error is not None:
            return error
        try:
            tags = tolerant.parse_string_list(tags, param="tags")
            axioms = tolerant.parse_string_list(axioms, param="axioms")
        except TolerantParseError as exc:
            return _clarify(exc, session_id=session_id, next_tool="begin_thought")
        try:
            thought = serial_engine.begin_thought(session, content, tags, axioms)
        except serial_engine.SerialSequencingError as exc:
            return prompts.serial_directive(session_id, exc.code, **exc.detail)
        _persist(session)
        return prompts.thought_begun(session, thought)

    @mcp.tool()
    @storage_guard
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
    @storage_guard
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
    @storage_guard
    @mode_gate(data_root, require_mode="serial")
    def refine_current_thought(
        session_id: str,
        new_content: str,
        challenged_assumptions: list[str] | str | None = None,
    ) -> dict[str, Any]:
        """Rewrite the thought to address the critique. The server records
        the new version and its normalized edit distance vs. the prior one.
        `challenged_assumptions` accepts a JSON array or a comma/newline list.
        """
        session, error = _load_session(data_root, session_id)
        if error is not None:
            return error
        try:
            challenged_assumptions = tolerant.parse_string_list(
                challenged_assumptions, param="challenged_assumptions"
            )
        except TolerantParseError as exc:
            return _clarify(exc, session_id=session_id, next_tool="refine_current_thought")
        try:
            rnd, edit_distance = serial_engine.refine_current_thought(
                session, new_content, challenged_assumptions
            )
        except serial_engine.SerialSequencingError as exc:
            return prompts.serial_directive(session_id, exc.code, **exc.detail)
        _persist(session)
        return prompts.thought_refined(session_id, rnd.round_index, edit_distance)

    @mcp.tool()
    @storage_guard
    @mode_gate(data_root, require_mode="serial")
    def score_current_thought(
        session_id: str, scores: dict[str, Any] | str | None = None
    ) -> dict[str, Any]:
        """Self-score the refined thought across the 7 utility dimensions
        (partial input is tolerated -- missing dims carry forward). `scores`
        accepts a JSON object, fenced JSON, or "correctness: 0.8, ..." text.
        Returns the convergence verdict: whether to commit or run another lens.
        """
        session, error = _load_session(data_root, session_id)
        if error is not None:
            return error
        try:
            scores = tolerant.parse_scores(scores, param="scores")
        except TolerantParseError as exc:
            return _clarify(exc, session_id=session_id, next_tool="score_current_thought")
        try:
            result = serial_engine.score_current_thought(
                session, scores, _cfg(session)
            )
        except serial_engine.SerialSequencingError as exc:
            return prompts.serial_directive(session_id, exc.code, **exc.detail)
        _persist(session)
        return prompts.thought_scored(session_id, result)

    @mcp.tool()
    @storage_guard
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


def _register_subagent_tools(mcp: FastMCP, data_root: Path) -> None:
    """Register Task 11's four subagent-engine tools (Layer 4, subagent mode).

    Each is gated by `mode_gate(data_root, require_mode="subagent")`: a
    no-mode session gets `mode_required`, a serial-mode session gets
    `wrong_mode`, and neither reaches the engine. `begin_subagent_thought` and
    `advance_subagent_round` are `async def` (they drive the T10 adapter's
    async `run()`, which offloads the vendored blocking Nash I/O to a worker
    thread) -- `mode_gate`'s async extension wraps them. `inspect_utility_matrix`
    and `commit_subagent_thought` do no network I/O and stay synchronous.

    These wrappers only load the session, call the engine, map any
    `SubagentSequencingError`/`SubagentAdapterError` to a directive payload
    (never a raw traceback -- T11 hard contract #3), and, on success, persist
    and format via `prompts.py` -- the same load/mutate/persist division the
    serial tools follow.
    """

    def _persist(session: Session) -> None:
        store.save(session, session.save_path)
        index.upsert(data_root, session)

    def _cfg(session: Session) -> dict[str, Any]:
        return config.load_config(root=data_root, overrides=session.overrides)

    @mcp.tool()
    @storage_guard
    @mode_gate(data_root, require_mode="subagent")
    async def begin_subagent_thought(
        session_id: str,
        content: str | None = None,
        prompt_focus: str | None = None,
    ) -> dict[str, Any]:
        """Start a subagent thought. With `engine="necort"` this runs the first
        Nash equilibrium round via the vendored core over the configured
        specialist framings (needs an endpoint; when none is configured it
        points at the manual path). With `engine="manual"` (T13) this hands
        back specialist #1's prompt for the calling model to voice itself --
        no endpoint, no network, no NECoRT code. Fails with a directive if a
        thought is already in progress.
        """
        session, error = _load_session(data_root, session_id)
        if error is not None:
            return error
        cfg = _cfg(session)
        try:
            if cfg["subagent"].get("engine", "necort") == "manual":
                result = manual_engine.begin(session, content, prompt_focus, cfg)
            else:
                result = await subagent_engine.begin(session, content, prompt_focus, cfg)
        except subagent_engine.SubagentSequencingError as exc:
            return prompts.subagent_directive(session_id, exc.code, **exc.detail)
        except subagent_engine.SubagentAdapterError as exc:
            return prompts.subagent_adapter_error(session_id, exc.detail, exc.retryable)
        _persist(session)
        if isinstance(result, manual_engine.ManualPrompt):
            return prompts.manual_specialist_prompt(session, result)
        return prompts.subagent_thought_begun(session, result)

    @mcp.tool()
    @storage_guard
    @mode_gate(data_root, require_mode="subagent")
    async def advance_subagent_round(
        session_id: str,
        candidate: str | None = None,
        scores: dict[str, Any] | str | None = None,
    ) -> dict[str, Any]:
        """Advance the subagent thought by one step.

        `engine="necort"`: runs the next Nash round, re-seeding the current
        best candidate (`candidate`/`scores` are ignored). `engine="manual"`
        (T13): records the current specialist's `candidate` + 7-dim `scores`
        (tolerant input -- JSON or "correctness: 0.8, ..." text) and hands the
        next specialist's prompt, or -- when the roster is exhausted -- runs
        the deterministic selection and returns the round result. Calling with
        no `candidate` at a round boundary (re)starts the next round's first
        specialist. The round budget (`subagent.max_rounds`) is enforced here.
        """
        session, error = _load_session(data_root, session_id)
        if error is not None:
            return error
        cfg = _cfg(session)
        try:
            if cfg["subagent"].get("engine", "necort") == "manual":
                try:
                    parsed = tolerant.parse_scores(scores, param="scores")
                except TolerantParseError as exc:
                    return _clarify(
                        exc, session_id=session_id, next_tool="advance_subagent_round"
                    )
                result = manual_engine.advance(session, candidate, parsed, cfg)
            else:
                result = await subagent_engine.advance(session, cfg)
        except subagent_engine.SubagentSequencingError as exc:
            return prompts.subagent_directive(session_id, exc.code, **exc.detail)
        except subagent_engine.SubagentAdapterError as exc:
            return prompts.subagent_adapter_error(session_id, exc.detail, exc.retryable)
        _persist(session)
        if isinstance(result, manual_engine.ManualPrompt):
            return prompts.manual_specialist_prompt(session, result)
        return prompts.subagent_round_advanced(session, result)

    @mcp.tool()
    @storage_guard
    @mode_gate(data_root, require_mode="subagent")
    def inspect_utility_matrix(session_id: str) -> dict[str, Any]:
        """Return the current Nash scoring state: the latest round's
        per-candidate utility vectors, equilibrium states, and selected winner.
        Read-only (no engine mutation, no network).
        """
        session, error = _load_session(data_root, session_id)
        if error is not None:
            return error
        try:
            state = subagent_engine.inspect(session, _cfg(session))
        except subagent_engine.SubagentSequencingError as exc:
            return prompts.subagent_directive(session_id, exc.code, **exc.detail)
        return prompts.subagent_matrix(session, state)

    @mcp.tool()
    @storage_guard
    @mode_gate(data_root, require_mode="subagent")
    def commit_subagent_thought(session_id: str) -> dict[str, Any]:
        """Accept the current equilibrium: lock the winning candidate as the
        thought's content and clear the current-thought cursor. Fails with a
        directive if no Nash round has run yet.
        """
        session, error = _load_session(data_root, session_id)
        if error is not None:
            return error
        try:
            thought = subagent_engine.commit(session)
        except subagent_engine.SubagentSequencingError as exc:
            return prompts.subagent_directive(session_id, exc.code, **exc.detail)
        _persist(session)
        return prompts.subagent_thought_committed(session, thought)


def _register_meta_tools(mcp: FastMCP, data_root: Path) -> None:
    """Register Task 8's meta tools (Layer 5): `next_action` (the
    authoritative next-step resolver across every session state x mode),
    the two small-context accommodations `summarize_session` /
    `compress_history` (deterministic extractive text ops -- no LLM calls,
    per Global Constraints), and the `export_session` / `import_session`
    portability pair. The logic for all five lives in `meta.py`; this
    function only loads/persists sessions around calls into it and maps
    results to `prompts.py` templates -- same load/mutate/persist division
    `_register_serial_tools` follows.

    None of these are wrapped in `mode_gate`: `next_action` does its own
    mode dispatch (its very first job is telling a mode-less session to
    call `set_session_mode`, and a subagent-mode session to wait on Tasks
    9-11), and the rest operate on committed thoughts / session JSON
    regardless of mode -- gating them would just be a second place that
    same dispatch logic could drift out of sync.
    """

    def _persist(session: Session) -> None:
        store.save(session, session.save_path)
        index.upsert(data_root, session)

    @mcp.tool()
    @storage_guard
    def next_action(session_id: str) -> dict[str, Any]:
        """Authoritative resolver: given this session's persisted state and
        mode, return the exact next tool to call and a one-line directive.
        Safe to call at any point in a session's lifecycle -- before a mode
        is set, mid-critique-loop, right after a thought commits, at the
        final stage, or once finalized.
        """
        session, error = _load_session(data_root, session_id)
        if error is not None:
            return error
        cfg = config.load_config(root=data_root, overrides=session.overrides)
        result = meta.next_action(session, cfg)
        return prompts.next_action_result(session, result)

    @mcp.tool()
    @storage_guard
    def summarize_session(
        session_id: str, scope: Literal["stage", "all"] = "stage"
    ) -> dict[str, Any]:
        """Deterministic extractive digest of this session's committed
        thoughts. `scope="stage"` (default) covers only the current stage;
        `scope="all"` covers every stage. No LLM calls -- this is text
        extraction, not summarization by inference.
        """
        session, error = _load_session(data_root, session_id)
        if error is not None:
            return error
        result = meta.summarize_session(session, scope)
        return prompts.summary_result(session, result)

    @mcp.tool()
    @storage_guard
    def compress_history(
        session_id: str, target_tokens: int = meta.DEFAULT_TARGET_TOKENS
    ) -> dict[str, Any]:
        """Deterministic extractive digest of *prior* stages' committed
        thoughts, capped at `target_tokens` (a cheap len(text)//4 heuristic
        -- no tokenizer dependency). The current stage is left out; its
        detail is already visible via the live loop tools/
        `summarize_session`. For small-context local models that can't
        hold a whole session's history.
        """
        session, error = _load_session(data_root, session_id)
        if error is not None:
            return error
        result = meta.compress_history(session, target_tokens)
        return prompts.compression_result(session, result)

    @mcp.tool()
    @storage_guard
    def export_session(session_id: str) -> dict[str, Any]:
        """Return this session's complete state as a JSON-serializable
        dict, suitable for handing straight to `import_session`.
        """
        session, error = _load_session(data_root, session_id)
        if error is not None:
            return error
        return prompts.session_exported(session, meta.export_session(session))

    @mcp.tool()
    @storage_guard
    def import_session(data: dict[str, Any] | str) -> dict[str, Any]:
        """Recreate a session from a previous `export_session` payload (a
        dict, or its JSON string form). Validated on the way in. If the
        imported session's id collides with one already on this install, a
        fresh id (and save path) is assigned automatically rather than
        overwriting the existing session -- collision-safe import.
        """
        try:
            session = meta.parse_import(data)
        except meta.ImportValidationError as exc:
            return prompts.import_failed(exc.code, exc.message)

        # [F6 SECURITY] Validate the imported id BEFORE it can be used to
        # compute an on-disk path -- reject anything that isn't a plain token
        # (path-traversal / absolute-path id).
        if not _SAFE_SESSION_ID_RE.match(session.id):
            return prompts.retry_with_clarification(
                "id",
                expected=(
                    "a plain session id token (letters and digits only, no "
                    "path separators or dots)"
                ),
                example='"3f9a2c1b8e7d4f60a1b2c3d4e5f60718"',
            )

        id_reassigned = index.get(data_root, session.id) is not None
        if id_reassigned:
            session.id = uuid.uuid4().hex
        save_path = store.session_path(data_root, session.id)
        # [F6 SECURITY] Defense-in-depth: regardless of id, the computed save
        # path MUST resolve inside the data root's sessions dir.
        sessions_dir = (data_root / "sessions").resolve()
        if not save_path.resolve().is_relative_to(sessions_dir):
            return prompts.import_failed(
                "invalid_session_data",
                "the imported session id resolves to a path outside the "
                "session store and was rejected.",
            )
        session.save_path = str(save_path)
        _persist(session)
        return prompts.session_imported(session, id_reassigned)


def _register_autopilot_tools(mcp: FastMCP, data_root: Path) -> None:
    """Register Task 14's two autopilot tools (Layer 7). Registered ONLY when
    `[autopilot].enabled=true` (see `create_server`) -- when disabled these are
    absent from `list_tools` and no network code path is reachable (the httpx
    import stays lazy inside `autopilot.py`).

    Both are `async def` (they await the OpenAI-compatible endpoint off the
    event loop) and both are gated by `mode_gate`: `run_stage_autopilot`
    requires serial mode, `run_subagent_autopilot` requires subagent mode --
    calling one on the wrong-mode session gets the `wrong_mode` directive, and
    a no-mode session gets `mode_required`, exactly like the manual tools.

    The DRIVING logic lives in `autopilot.py`; these wrappers only: guard the
    optional `stage` argument, build the endpoint client (mapping a missing
    httpx to `autopilot_unavailable`), hand the driver a `persist` closure so
    every committed thought lands on disk exactly as the manual path does, and
    map the driver's outcome (or a propagated engine sequencing/adapter error)
    to `prompts.py` wording -- the same load/mutate/persist division every
    other engine registration follows.
    """

    def _persist(session: Session) -> None:
        store.save(session, session.save_path)
        index.upsert(data_root, session)

    def _cfg(session: Session) -> dict[str, Any]:
        return config.load_config(root=data_root, overrides=session.overrides)

    @mcp.tool()
    @storage_guard
    @mode_gate(data_root, require_mode="serial")
    async def run_stage_autopilot(
        session_id: str,
        stage: str | None = None,
        initial_content: str | None = None,
    ) -> dict[str, Any]:
        """Autopilot the SERIAL loop for the current stage: the server drafts
        (from `initial_content` or an LLM draft), then runs critique -> refine
        -> score rounds against the configured `[autopilot]` endpoint until the
        convergence rules fire, and commits -- all internally. `stage`, if
        given, must equal the session's current stage (autopilot never jumps
        the cursor). Stops with a partial-progress directive (never a
        traceback) on an endpoint fault or unparseable model output; everything
        committed so far is persisted and resumable via next_action.
        """
        session, error = _load_session(data_root, session_id)
        if error is not None:
            return error
        if stage and stage != session.current_stage:
            return prompts.autopilot_stage_mismatch(session_id, stage, session.current_stage)
        cfg = _cfg(session)
        try:
            client = autopilot.client_from_cfg(cfg)
        except autopilot.AutopilotHttpxMissing:
            return prompts.autopilot_unavailable(session_id)
        available = lens_loader.discover_lenses(data_root)
        try:
            outcome = await autopilot.run_stage(
                session, cfg, client, available, _persist, initial_content=initial_content
            )
        except serial_engine.SerialSequencingError as exc:
            return prompts.serial_directive(session_id, exc.code, **exc.detail)
        if outcome.status == "committed":
            return prompts.stage_autopilot_committed(session, outcome)
        return prompts.autopilot_stopped(session_id, outcome)

    @mcp.tool()
    @storage_guard
    @mode_gate(data_root, require_mode="subagent")
    async def run_subagent_autopilot(
        session_id: str,
        stage: str | None = None,
        initial_content: str | None = None,
    ) -> dict[str, Any]:
        """Autopilot the SUBAGENT loop for the current stage. With
        `engine="necort"` the server drives the vendored Nash core (via the
        `[subagent]` endpoint) begin -> advance -> commit; with `engine="manual"`
        the server plays each specialist itself against the `[autopilot]`
        endpoint, feeding candidates + 7-dim scores through the same
        manual_engine functions, then commits the winner. `stage`, if given,
        must equal the current stage. Endpoint/parse failures on the manual path
        stop with a partial-progress directive; a missing necort endpoint yields
        the manual-path directive, never a traceback.
        """
        session, error = _load_session(data_root, session_id)
        if error is not None:
            return error
        if stage and stage != session.current_stage:
            return prompts.autopilot_stage_mismatch(session_id, stage, session.current_stage)
        cfg = _cfg(session)
        engine = cfg["subagent"].get("engine", "necort")
        try:
            if engine == "manual":
                try:
                    client = autopilot.client_from_cfg(cfg)
                except autopilot.AutopilotHttpxMissing:
                    return prompts.autopilot_unavailable(session_id)
                outcome = await autopilot.run_subagent_manual(
                    session, cfg, client, _persist, initial_content=initial_content
                )
            else:
                outcome = await autopilot.run_subagent_necort(
                    session, cfg, _persist, initial_content=initial_content
                )
        except subagent_engine.SubagentSequencingError as exc:
            return prompts.subagent_directive(session_id, exc.code, **exc.detail)
        except subagent_engine.SubagentAdapterError as exc:
            return prompts.subagent_adapter_error(session_id, exc.detail, exc.retryable)
        if outcome.status == "committed":
            return prompts.subagent_autopilot_committed(session, outcome)
        return prompts.autopilot_stopped(session_id, outcome)


def _autopilot_enabled(data_root: Path) -> bool:
    """Whether `[autopilot].enabled` is true in the effective (packaged + user)
    config for this data root. Read once at server creation to decide whether
    the two autopilot tools register at all -- the feature flag's single
    reachable gate, so with it off no autopilot (and no network) code path is
    ever registered or callable."""
    return bool(config.load_config(root=data_root).get("autopilot", {}).get("enabled", False))


def create_server(root: Path | str | None = None) -> FastMCP:
    """Build a fresh `deep-think-mcp` FastMCP server instance.

    `root` is the data root every tool call on this server instance
    operates against; defaults to `config.resolve_root()`. Tests always
    pass an explicit tmp root (Global Constraints: never touch the real
    home directory); the real entrypoint (`main()`) lets it default.

    The two autopilot tools (Layer 7) register ONLY when `[autopilot].enabled`
    is true for this root -- otherwise they are absent from `list_tools` and no
    network code path is reachable.
    """
    data_root = Path(root).expanduser().resolve() if root is not None else config.resolve_root()
    mcp = FastMCP(SERVER_NAME)
    _register_lifecycle_tools(mcp, data_root)
    _register_stage_tools(mcp, data_root)
    _register_serial_tools(mcp, data_root)
    _register_subagent_tools(mcp, data_root)
    _register_meta_tools(mcp, data_root)
    if _autopilot_enabled(data_root):
        _register_autopilot_tools(mcp, data_root)
    return mcp


def _env_bool(name: str, default: bool = False) -> bool:
    """Parse a boolean-ish environment variable (1/true/yes/on)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI args for the server entrypoint.

    Every flag falls back to a ``DEEP_THINK_MCP_*`` env var so the same
    binary can be driven from a shell, a systemd unit, or a container without
    changing the command. Defaults reproduce the historical stdio behaviour.
    """
    parser = argparse.ArgumentParser(
        prog="deep_think_mcp.server",
        description="Run the deep-think-mcp server over stdio (default) or "
        "as a long-lived Streamable HTTP daemon.",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http", "sse"],
        default=os.environ.get("DEEP_THINK_MCP_TRANSPORT", "stdio"),
        help="MCP transport. 'stdio' (default) is one client per process. "
        "'streamable-http' runs a shared always-live daemon on --host/--port.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("DEEP_THINK_MCP_HOST", "127.0.0.1"),
        help="Bind host for HTTP transports (default: 127.0.0.1, local-only).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("DEEP_THINK_MCP_PORT", "8182")),
        help="Bind port for HTTP transports (default: 8182).",
    )
    parser.add_argument(
        "--path",
        default=os.environ.get("DEEP_THINK_MCP_PATH", "/mcp"),
        help="Mount path for Streamable HTTP (default: /mcp). A client URL is "
        "then http://<host>:<port><path>.",
    )
    parser.add_argument(
        "--stateless",
        action="store_true",
        default=_env_bool("DEEP_THINK_MCP_STATELESS", False),
        help="Serve Streamable HTTP without a persistent MCP session "
        "(easier for one-shot callers like DAG steps). Session STATE is "
        "unaffected — it always lives on disk keyed by session_id.",
    )
    return parser.parse_args(argv)


def _configure_transport(server: FastMCP, args: argparse.Namespace) -> str:
    """Apply CLI args to ``server.settings`` and return the transport name.

    Pure and side-effect-free apart from mutating ``server.settings``, so the
    wiring can be unit-tested without actually binding a socket.
    """
    if args.transport in ("streamable-http", "sse"):
        server.settings.host = args.host
        server.settings.port = args.port
        server.settings.streamable_http_path = args.path
        server.settings.stateless_http = args.stateless
    return args.transport


def main(argv: list[str] | None = None) -> None:
    """Entrypoint for ``python -m deep_think_mcp.server``.

    Transport model
    ---------------
    * ``stdio`` (default) — one client per server process, tool calls strictly
      serialized. This is the model deep-think's session code assumes (see the
      single-client note in ``set_session_mode``): the safest option when a
      single agent spawns the server itself.
    * ``streamable-http`` — one long-lived daemon that several clients (e.g. a
      Hermes agent *and* a Dagu DAG) reach over a stable URL. Because every
      session's state is persisted to disk under ``config.resolve_root()`` and
      guarded by ``portalocker`` file locks, keyed by ``session_id``, sharing
      one daemon is safe as long as callers don't drive the *same* session_id
      truly concurrently. Run it behind a process supervisor (see
      ``deploy/deep-think-mcp.service``).

    Backward compatibility: invoked with no arguments this is identical to the
    previous stdio-only entrypoint.
    """
    args = _parse_args(argv)
    server = create_server()
    transport = _configure_transport(server, args)
    server.run(transport=transport)


if __name__ == "__main__":
    main()
