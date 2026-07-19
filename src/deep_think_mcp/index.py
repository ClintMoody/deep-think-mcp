"""Session index: `<root>/index.json` maps session id -> {path, mode,
status, created_at, updated_at}.

Read-modify-write is guarded by a dedicated `.lock` sibling (Portalocker),
and mutations follow the same `.bak` protocol as store.py (see
`docs/execution-plan.md` "Global Constraints" -- "on every mutation").

Entries carry whatever `path` a session reports via `Session.save_path`
verbatim, including absolute paths outside `root` once a session has been
moved -- this module never validates or rewrites paths, it only tracks
whatever it's told, which is what lets `list_sessions`/`resume_session`
keep working after a session file has moved anywhere on disk.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import portalocker

from deep_think_mcp.session import Session

_INDEX_FILENAME = "index.json"
_LOCK_TIMEOUT = 10  # seconds


def index_path(root: Path | str) -> Path:
    """Location of the index file for a given data root."""
    return Path(root) / _INDEX_FILENAME


def _lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


def _bak_path(path: Path) -> Path:
    return path.with_name(path.name + ".bak")


def _read_locked(path: Path, bak: Path) -> dict[str, Any]:
    """Read the index dict, recovering from `.bak` if the main file is
    missing/empty/corrupt. Must be called while holding the lock.
    """
    if path.exists():
        text = path.read_text().strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except ValueError:
            if not bak.exists():
                raise
    elif not bak.exists():
        return {}

    # Recover from .bak: either the main file was missing, or corrupt and a
    # .bak exists. Restore it as the main file and return its contents.
    data = bak.read_text()
    parsed = json.loads(data)
    path.write_text(data)
    bak.unlink()
    return parsed


def _write_locked(path: Path, bak: Path, data: dict[str, Any]) -> None:
    """Write the index dict following the `.bak` mutation protocol. Must be
    called while holding the lock.
    """
    text = json.dumps(data, indent=2, sort_keys=True)
    if path.exists():
        shutil.copyfile(path, bak)
    path.write_text(text)
    if bak.exists():
        bak.unlink()


def upsert(root: Path | str, session: Session) -> None:
    """Create or update the index entry for `session`, keyed by its id."""
    path = index_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    bak = _bak_path(path)

    with portalocker.Lock(str(_lock_path(path)), "a", timeout=_LOCK_TIMEOUT):
        data = _read_locked(path, bak)
        data[session.id] = {
            "path": session.save_path,
            "mode": session.mode,
            "status": session.status,
            "created_at": session.created_at.isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_locked(path, bak, data)


def remove(root: Path | str, session_id: str) -> None:
    """Remove the index entry for `session_id`, if present. No-op otherwise."""
    path = index_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    bak = _bak_path(path)

    with portalocker.Lock(str(_lock_path(path)), "a", timeout=_LOCK_TIMEOUT):
        data = _read_locked(path, bak)
        if session_id in data:
            del data[session_id]
            _write_locked(path, bak, data)


def get(root: Path | str, session_id: str) -> dict[str, Any] | None:
    """Return the index entry for `session_id`, or None if not present."""
    path = index_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    bak = _bak_path(path)

    with portalocker.Lock(str(_lock_path(path)), "a", timeout=_LOCK_TIMEOUT):
        data = _read_locked(path, bak)
    return data.get(session_id)


def list_all(root: Path | str) -> dict[str, Any]:
    """Return the full index dict (empty if `index.json` doesn't exist)."""
    path = index_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    bak = _bak_path(path)

    with portalocker.Lock(str(_lock_path(path)), "a", timeout=_LOCK_TIMEOUT):
        return _read_locked(path, bak)
