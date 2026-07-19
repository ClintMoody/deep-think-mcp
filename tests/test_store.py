"""Tests for deep_think_mcp.store: JSON-per-session persistence.

Per Global Constraints (docs/execution-plan.md), every mutation follows the
`.bak` protocol (back up existing file, write new content, remove backup)
guarded by a Portalocker file lock. Tests always use tmp_path -- never the
real home directory.
"""

import concurrent.futures
import json
import threading

import pytest
from pydantic import ValidationError

from deep_think_mcp import store
from deep_think_mcp.session import Session


def _make_session(**overrides):
    base = dict(
        question="does this roundtrip?",
        expected_stages=["Research"],
        current_stage="Research",
    )
    base.update(overrides)
    return Session(**base)


# ---------------------------------------------------------------------------
# session_path helper
# ---------------------------------------------------------------------------


def test_session_path_is_root_sessions_id_json(tmp_path):
    session = _make_session()
    path = store.session_path(tmp_path, session.id)
    assert path == tmp_path / "sessions" / f"{session.id}.json"


# ---------------------------------------------------------------------------
# save / load roundtrip
# ---------------------------------------------------------------------------


def test_save_then_load_roundtrip(tmp_path):
    session = _make_session()
    path = store.session_path(tmp_path, session.id)

    store.save(session, path)
    loaded = store.load(path)

    assert loaded == session


def test_save_creates_parent_directories(tmp_path):
    session = _make_session()
    path = tmp_path / "nested" / "dir" / "session.json"

    store.save(session, path)

    assert path.is_file()


def test_save_writes_valid_json(tmp_path):
    session = _make_session()
    path = tmp_path / "session.json"

    store.save(session, path)

    parsed = json.loads(path.read_text())
    assert parsed["id"] == session.id
    assert parsed["question"] == session.question


def test_save_does_not_leave_a_bak_file_behind(tmp_path):
    session = _make_session()
    path = tmp_path / "session.json"

    store.save(session, path)
    store.save(session, path)  # second save exercises the backup-then-remove path

    assert not path.with_name(path.name + ".bak").exists()
    assert path.exists()


def test_save_overwrites_existing_file_with_new_content(tmp_path):
    session = _make_session()
    path = tmp_path / "session.json"
    store.save(session, path)

    session.thoughts = []
    session.status = "finalized"
    store.save(session, path)

    reloaded = store.load(path)
    assert reloaded.status == "finalized"


# ---------------------------------------------------------------------------
# .bak recovery
# ---------------------------------------------------------------------------


def test_load_recovers_from_bak_when_main_file_is_corrupt(tmp_path):
    good_session = _make_session(question="the good, pre-mutation state")
    path = tmp_path / "session.json"
    bak_path = path.with_name(path.name + ".bak")

    bak_path.write_text(good_session.model_dump_json())
    path.write_text("{not valid json::")

    recovered = store.load(path)

    assert recovered == good_session
    # Recovery should also repair the main file and clean up the .bak.
    assert store.load(path) == good_session
    assert not bak_path.exists()


def test_load_recovers_from_bak_when_main_file_is_missing(tmp_path):
    good_session = _make_session(question="only the backup survived")
    path = tmp_path / "session.json"
    bak_path = path.with_name(path.name + ".bak")
    bak_path.write_text(good_session.model_dump_json())

    recovered = store.load(path)

    assert recovered == good_session
    assert path.exists()


def test_load_raises_when_main_file_corrupt_and_no_bak(tmp_path):
    path = tmp_path / "session.json"
    path.write_text("{not valid json::")

    with pytest.raises(ValidationError):
        store.load(path)


def test_load_raises_when_file_missing_and_no_bak(tmp_path):
    path = tmp_path / "session.json"

    with pytest.raises(FileNotFoundError):
        store.load(path)


# ---------------------------------------------------------------------------
# Load test: rapid concurrent writes to one session under Portalocker
# contention (threads) -- file integrity must survive.
# ---------------------------------------------------------------------------


def test_concurrent_saves_to_same_session_never_corrupt_the_file(tmp_path):
    session = _make_session()
    path = tmp_path / "session.json"
    store.save(session, path)

    barrier = threading.Barrier(8)

    def hammer(worker_id: int) -> None:
        barrier.wait()
        for i in range(15):
            variant = _make_session(
                question=f"worker {worker_id} iteration {i}",
                current_stage="Research",
            )
            variant.id = session.id
            store.save(variant, path)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(hammer, worker_id) for worker_id in range(8)]
        for future in futures:
            future.result()

    # File must be readable and a valid Session -- never left torn/corrupt
    # by interleaved writers.
    final = store.load(path)
    assert final.id == session.id
    assert not path.with_name(path.name + ".bak").exists()
