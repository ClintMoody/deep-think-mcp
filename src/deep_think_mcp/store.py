"""JSON-per-session persistence: Portalocker locks + the `.bak` mutation
protocol from `docs/execution-plan.md` "Global Constraints".

On every mutation: back up the existing file (if any) to a `.bak` sibling,
write the new content, then remove the `.bak`. A dedicated `.lock` sibling
file, held via Portalocker for the duration of the critical section, guards
against concurrent readers/writers tearing either file.

This module has no opinion on *where* a session lives -- callers always
pass an explicit path. `session_path()` is only the default-location
convention for a brand-new session under a given root; tracking a session's
*current* path (which may move outside the root entirely) is index.py's
job, not this module's.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import portalocker

from deep_think_mcp.session import Session

_SESSIONS_DIRNAME = "sessions"
_LOCK_TIMEOUT = 10  # seconds


def session_path(root: Path | str, session_id: str) -> Path:
    """Default on-disk location for a new session's JSON file under `root`."""
    return Path(root) / _SESSIONS_DIRNAME / f"{session_id}.json"


def _lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


def _bak_path(path: Path) -> Path:
    return path.with_name(path.name + ".bak")


def save(session: Session, path: Path | str) -> None:
    """Persist `session` to `path`, following the `.bak` mutation protocol."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    bak = _bak_path(path)
    data = session.model_dump_json(indent=2)

    with portalocker.Lock(str(_lock_path(path)), "a", timeout=_LOCK_TIMEOUT):
        if path.exists():
            shutil.copyfile(path, bak)
        path.write_text(data)
        if bak.exists():
            bak.unlink()


def load(path: Path | str) -> Session:
    """Load a session from `path`.

    If the main file is missing or corrupt and a `.bak` sibling exists,
    recovers from the `.bak`: restores it as the main file, removes the
    `.bak`, and returns the session it contained. If there is no usable
    `.bak` either, the original error propagates.
    """
    path = Path(path)
    bak = _bak_path(path)

    with portalocker.Lock(str(_lock_path(path)), "a", timeout=_LOCK_TIMEOUT):
        try:
            return Session.model_validate_json(path.read_text())
        except (OSError, ValueError):
            if not bak.exists():
                raise
            data = bak.read_text()
            session = Session.model_validate_json(data)
            path.write_text(data)
            bak.unlink()
            return session
