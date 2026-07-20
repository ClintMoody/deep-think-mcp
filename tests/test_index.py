"""Tests for deep_think_mcp.index: <root>/index.json session index.

id -> {path, mode, status, created_at, updated_at}. Locked read-modify-write
(Portalocker), same `.bak` mutation protocol as store.py. Must survive
sessions living at arbitrary absolute paths outside `root` (post-move).
"""

import concurrent.futures
import json
import threading

import pytest

from deep_think_mcp import index
from deep_think_mcp.session import Session


def _make_session(**overrides):
    base = dict(
        question="indexed?",
        expected_stages=["Research"],
        current_stage="Research",
        save_path="/some/default/path.json",
    )
    base.update(overrides)
    return Session(**base)


# ---------------------------------------------------------------------------
# index_path helper
# ---------------------------------------------------------------------------


def test_index_path_is_root_index_json(tmp_path):
    assert index.index_path(tmp_path) == tmp_path / "index.json"


# ---------------------------------------------------------------------------
# upsert / get
# ---------------------------------------------------------------------------


def test_upsert_creates_index_file(tmp_path):
    session = _make_session()
    index.upsert(tmp_path, session)
    assert (tmp_path / "index.json").is_file()


def test_upsert_then_get_roundtrips_expected_fields(tmp_path):
    session = _make_session(mode="serial", save_path="/abs/path/session.json")
    index.upsert(tmp_path, session)

    entry = index.get(tmp_path, session.id)

    assert entry is not None
    assert entry["path"] == "/abs/path/session.json"
    assert entry["mode"] == "serial"
    assert entry["status"] == "active"
    assert entry["created_at"] == session.created_at.isoformat()
    assert "updated_at" in entry


def test_get_returns_none_for_unknown_id(tmp_path):
    assert index.get(tmp_path, "does-not-exist") is None


def test_get_on_missing_index_file_returns_none(tmp_path):
    assert index.get(tmp_path, "anything") is None


def test_index_json_is_valid_json_on_disk(tmp_path):
    session = _make_session()
    index.upsert(tmp_path, session)

    parsed = json.loads((tmp_path / "index.json").read_text())
    assert session.id in parsed


# ---------------------------------------------------------------------------
# upsert preserves other entries; re-upsert updates in place
# ---------------------------------------------------------------------------


def test_upsert_preserves_other_entries(tmp_path):
    session_a = _make_session(question="a")
    session_b = _make_session(question="b")

    index.upsert(tmp_path, session_a)
    index.upsert(tmp_path, session_b)

    assert index.get(tmp_path, session_a.id) is not None
    assert index.get(tmp_path, session_b.id) is not None


def test_reupsert_updates_status_and_leaves_others_alone(tmp_path):
    session_a = _make_session(question="a")
    session_b = _make_session(question="b")
    index.upsert(tmp_path, session_a)
    index.upsert(tmp_path, session_b)

    session_a.status = "finalized"
    index.upsert(tmp_path, session_a)

    assert index.get(tmp_path, session_a.id)["status"] == "finalized"
    assert index.get(tmp_path, session_b.id)["status"] == "active"


def test_reupsert_bumps_updated_at(tmp_path, monkeypatch):
    """F9: step the clock so a distinct timestamp is guaranteed, then assert a
    STRICT bump. A regression that froze updated_at (or copied created_at)
    would produce first == second and now FAIL -- which the old `>=` (satisfied
    by equality) could never catch."""
    from datetime import datetime, timezone

    ticks = iter(
        [
            datetime(2020, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            datetime(2020, 1, 1, 0, 0, 5, tzinfo=timezone.utc),
        ]
    )

    class _SteppedClock:
        @staticmethod
        def now(tz=None):
            return next(ticks)

    # index.upsert stamps updated_at via `datetime.now(timezone.utc)` (the ONLY
    # datetime.now call in the module); patch it to a deterministic stepped clock.
    monkeypatch.setattr(index, "datetime", _SteppedClock)

    session = _make_session()
    index.upsert(tmp_path, session)
    first_updated_at = index.get(tmp_path, session.id)["updated_at"]

    session.status = "finalized"
    index.upsert(tmp_path, session)
    second_updated_at = index.get(tmp_path, session.id)["updated_at"]

    assert first_updated_at == "2020-01-01T00:00:00+00:00"
    assert second_updated_at == "2020-01-01T00:00:05+00:00"
    assert second_updated_at > first_updated_at


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


def test_remove_deletes_entry(tmp_path):
    session = _make_session()
    index.upsert(tmp_path, session)

    index.remove(tmp_path, session.id)

    assert index.get(tmp_path, session.id) is None


def test_remove_of_unknown_id_is_a_noop(tmp_path):
    index.remove(tmp_path, "never-existed")  # must not raise


def test_remove_preserves_other_entries(tmp_path):
    session_a = _make_session(question="a")
    session_b = _make_session(question="b")
    index.upsert(tmp_path, session_a)
    index.upsert(tmp_path, session_b)

    index.remove(tmp_path, session_a.id)

    assert index.get(tmp_path, session_a.id) is None
    assert index.get(tmp_path, session_b.id) is not None


# ---------------------------------------------------------------------------
# list_all
# ---------------------------------------------------------------------------


def test_list_all_on_missing_index_returns_empty_dict(tmp_path):
    assert index.list_all(tmp_path) == {}


def test_list_all_returns_every_entry(tmp_path):
    session_a = _make_session(question="a")
    session_b = _make_session(question="b")
    index.upsert(tmp_path, session_a)
    index.upsert(tmp_path, session_b)

    entries = index.list_all(tmp_path)

    assert set(entries) == {session_a.id, session_b.id}


# ---------------------------------------------------------------------------
# Survives sessions living at arbitrary absolute paths outside root
# (post-move).
# ---------------------------------------------------------------------------


def test_index_tracks_arbitrary_absolute_path_outside_root(tmp_path):
    root = tmp_path / "the-root"
    elsewhere = tmp_path / "completely" / "different" / "place" / "moved.json"
    session = _make_session(save_path=str(elsewhere))

    index.upsert(root, session)
    entry = index.get(root, session.id)

    assert entry["path"] == str(elsewhere)
    assert not str(elsewhere).startswith(str(root))


# ---------------------------------------------------------------------------
# .bak recovery
# ---------------------------------------------------------------------------


def test_index_recovers_from_bak_when_main_file_corrupt(tmp_path):
    session = _make_session()
    index.upsert(tmp_path, session)
    good_content = (tmp_path / "index.json").read_text()

    bak_path = tmp_path / "index.json.bak"
    bak_path.write_text(good_content)
    (tmp_path / "index.json").write_text("{not valid json::")

    entries = index.list_all(tmp_path)

    assert session.id in entries
    assert not bak_path.exists()


def test_index_raises_when_main_file_corrupt_and_no_bak(tmp_path):
    (tmp_path / "index.json").write_text("{not valid json::")

    with pytest.raises(json.JSONDecodeError):
        index.list_all(tmp_path)


@pytest.mark.parametrize("empty_content", ["", "   ", "\n"])
def test_index_recovers_from_bak_when_main_file_is_empty(tmp_path, empty_content):
    """An existing-but-empty index.json is exactly what a crash mid-write
    (truncate-then-write) leaves behind -- it must trigger .bak recovery
    the same as corrupt JSON, not be silently treated as an empty index.
    """
    session = _make_session()
    index.upsert(tmp_path, session)
    good_content = (tmp_path / "index.json").read_text()

    bak_path = tmp_path / "index.json.bak"
    bak_path.write_text(good_content)
    (tmp_path / "index.json").write_text(empty_content)

    entries = index.list_all(tmp_path)

    assert session.id in entries
    assert not bak_path.exists()
    # The main file must have been repaired, not left empty.
    assert json.loads((tmp_path / "index.json").read_text())


def test_index_raises_when_main_file_empty_and_no_bak(tmp_path):
    (tmp_path / "index.json").write_text("")

    with pytest.raises(json.JSONDecodeError):
        index.list_all(tmp_path)


def test_upsert_after_empty_main_file_recovery_preserves_recovered_entries(
    tmp_path,
):
    """Regression: upsert() must not destroy the last-good .bak by copying
    an empty main file over it before recovery has restored good content.
    """
    session_a = _make_session(question="a")
    index.upsert(tmp_path, session_a)
    good_content = (tmp_path / "index.json").read_text()

    bak_path = tmp_path / "index.json.bak"
    bak_path.write_text(good_content)
    (tmp_path / "index.json").write_text("")

    session_b = _make_session(question="b")
    index.upsert(tmp_path, session_b)

    entries = index.list_all(tmp_path)
    assert session_a.id in entries
    assert session_b.id in entries


def test_index_recovers_from_bak_when_main_file_is_missing(tmp_path):
    session = _make_session()
    bak_path = tmp_path / "index.json.bak"
    bak_path.write_text(json.dumps({session.id: {"path": session.save_path}}))

    entries = index.list_all(tmp_path)

    assert session.id in entries
    assert (tmp_path / "index.json").exists()
    assert not bak_path.exists()


# ---------------------------------------------------------------------------
# Locked read-modify-write holds under concurrent contention: N distinct
# concurrent upserts must not lose entries to the classic read-modify-write
# race.
# ---------------------------------------------------------------------------


def test_concurrent_upserts_of_distinct_sessions_lose_no_entries(tmp_path):
    sessions = [_make_session(question=f"session {i}") for i in range(20)]
    barrier = threading.Barrier(len(sessions))

    def do_upsert(session):
        barrier.wait()
        index.upsert(tmp_path, session)

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(sessions)) as pool:
        futures = [pool.submit(do_upsert, s) for s in sessions]
        for future in futures:
            future.result()

    entries = index.list_all(tmp_path)
    assert set(entries) == {s.id for s in sessions}
