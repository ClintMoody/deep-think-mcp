"""Finalize / move / keep-here session lifecycle logic.

Layer 6 (lifecycle manager) per `docs/execution-plan.md` Task 4 -- the
M1-mandated persistence+move UX that has to work end-to-end before any
thinking engine exists. This module owns the *logic*; `server.py` only
registers the three MCP tools and wires this module's return values /
raised errors into `prompts.py` templates -- one responsibility per file,
per the repo's established convention (see `store.py`, `index.py`).

Persistence pattern, matching every existing lifecycle tool in server.py:

  - `finalize()` and `keep_here()` are pure in-memory mutations of a
    `Session` the caller already loaded. They never touch disk. The caller
    (server.py) is responsible for `store.save(session, session.save_path)`
    and `index.upsert(data_root, session)` afterward, same as
    `set_session_mode` already does.
  - `move()` is the one exception, and deliberately so: a move is not a
    simple field mutation, it is "validate a destination, write the
    session there, verify the write, then unlink the original" -- that
    whole sequence has to happen as one unit so a caller can never
    persist a session that claims to live somewhere the write didn't
    actually succeed. So `move()` does its own `store.save`/`store.load`
    internally and returns a session whose `save_path` already points at
    the new location; the caller still owns `index.upsert` afterward (the
    index-update step is identical in shape to every other tool, so it
    stays consolidated in server.py rather than duplicated here).

Path handling is deliberately conservative, since this is the one part of
the server that deletes/moves files based on tool input:

  - `~` is expanded, but a still-relative path after that is rejected
    outright (`destination_not_absolute`) rather than resolved against the
    server process's cwd, which the calling model/user has no visibility
    into and no control over.
  - Symlinks are resolved (`Path.resolve()`) before any check runs, so
    validation and the actual write always agree on where a path really
    points.
  - No destination directory is ever auto-created. `docs/build-plan.md`'s
    "validate destination (writable directory ...)" is read literally: the
    parent must already exist and already be writable, or the move fails
    cleanly. Silently creating arbitrary directory trees from
    model-supplied input is out of scope.
  - A destination that resolves to an *existing directory* is treated
    mv(1)-style: the session moves into that directory under its current
    filename. This directly serves the finalize `human_prompt` wording
    ("a project folder, your Documents, etc.") and the "built for weak
    local models" constraint -- the model can pass a bare folder path
    without first computing a full destination filename itself. A
    destination that does not yet exist is always treated as a literal
    target file path; no attempt is made to infer directory intent (e.g.
    a trailing slash) for a path that doesn't exist yet.
  - Moving to the session's own current path is rejected
    (`destination_same_as_current`) rather than silently treated as a
    no-op. This isn't just a UX nicety: the write-then-unlink sequence
    below would otherwise write to `target`, verify it (trivially, since
    it's the same file), and then unlink `current` -- deleting the
    session it just "moved". Rejecting the case up front means that
    write/unlink code never has to reason about source and destination
    aliasing the same inode.
"""

from __future__ import annotations

import os
from pathlib import Path

from deep_think_mcp import store
from deep_think_mcp.session import DecisionRecord, MoveRecord, Session


class MoveError(Exception):
    """Raised by `move()` when a destination fails validation, or when the
    write/verify step fails after validation passed. Carries a
    machine-readable `code` (for prompts.py to key its template on) and a
    human-readable `message` fallback.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def finalize(session: Session) -> Session:
    """Mark `session` finalized. Pure mutation; caller persists."""
    session.status = "finalized"
    return session


def keep_here(session: Session) -> Session:
    """Record a `keep_here` decision in the session's audit trail. Pure
    mutation; caller persists.
    """
    session.decisions.append(DecisionRecord(action="keep_here"))
    return session


def _resolve_destination(session: Session, new_path: str) -> Path:
    """Turn the raw `new_path` argument into a concrete destination file
    path, per the directory-vs-file rules documented at module level.
    """
    raw = Path(new_path).expanduser()
    if not raw.is_absolute():
        raise MoveError(
            "destination_not_absolute",
            f"Destination path '{new_path}' must be an absolute path "
            "(after expanding '~').",
        )
    resolved = raw.resolve()
    if resolved.is_dir():
        return resolved / Path(session.save_path).name
    return resolved


def _validate_destination(current: Path, target: Path, force: bool) -> None:
    """Raise `MoveError` if `target` isn't a safe, writable, non-clobbering
    destination. Called before any mutation of `session` or the
    filesystem, so a validation failure never leaves partial state behind.
    """
    if target == current:
        raise MoveError(
            "destination_same_as_current",
            f"'{target}' is already this session's current location.",
        )

    parent = target.parent
    if not parent.exists():
        raise MoveError(
            "destination_parent_missing",
            f"Directory '{parent}' does not exist.",
        )
    if not parent.is_dir():
        raise MoveError(
            "destination_parent_not_a_directory",
            f"'{parent}' is not a directory.",
        )
    if not os.access(parent, os.W_OK):
        raise MoveError(
            "destination_not_writable",
            f"Directory '{parent}' is not writable.",
        )
    if target.exists() and not force:
        raise MoveError(
            "destination_exists",
            f"'{target}' already exists. Pass force=true to overwrite it.",
        )


def move(session: Session, new_path: str, *, force: bool = False) -> Session:
    """Move `session`'s on-disk file to `new_path`.

    Cross-filesystem-safe by construction: this never calls `os.rename`,
    `Path.rename`, or `shutil.move` (all of which raise `OSError(EXDEV)`
    moving across filesystem boundaries). Instead it writes a fresh copy
    at the destination via `store.save` (which already carries the
    `.bak`-protocol + Portalocker guarantees `docs/execution-plan.md`
    requires of every mutation), reads it back to verify the write
    actually landed, and only then removes the original file.

    Raises `MoveError` (never a raw OSError) if the destination fails
    validation. `session` is only mutated (its `move_history` and
    `save_path`) once validation has passed; if the write/verify step
    itself fails, the mutation is rolled back before re-raising, so a
    caller that doesn't catch the error can't accidentally persist a
    session claiming to live somewhere the move never actually completed.
    """
    current = Path(session.save_path).resolve()
    target = _resolve_destination(session, new_path)
    _validate_destination(current, target, force)

    session.move_history.append(MoveRecord(from_path=str(current), to_path=str(target)))
    session.save_path = str(target)

    try:
        store.save(session, target)
        written_ok = store.load(target) == session
    except OSError as exc:
        session.move_history.pop()
        session.save_path = str(current)
        raise MoveError(
            "destination_write_failed", f"Could not write to '{target}': {exc}"
        ) from exc

    if not written_ok:
        session.move_history.pop()
        session.save_path = str(current)
        raise MoveError(
            "verification_failed",
            f"Wrote '{target}' but could not verify its content afterward.",
        )

    try:
        current.unlink(missing_ok=True)
    except OSError:
        # Best-effort cleanup only: the session is already safely written
        # and verified at `target`, which is what matters. A failure to
        # remove the now-superseded original (e.g. a permissions race)
        # leaves a harmless stray duplicate rather than losing data or
        # forcing the caller to retry a move that actually already
        # succeeded.
        pass

    return session
