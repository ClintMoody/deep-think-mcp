"""Tests for deep_think_mcp.lifecycle: finalize / move / keep-here logic.

`finalize()` and `keep_here()` are pure in-memory mutations -- per the
persistence pattern every lifecycle tool in server.py follows (mutate, then
the *caller* does `store.save` + `index.upsert`), they do not touch the
filesystem themselves. `move()` is the exception: because a move must
validate a destination, write there, verify the write, and only then unlink
the original, that whole sequence has to happen atomically-in-spirit inside
one function -- so its tests exercise the filesystem for real, always via
tmp_path, never the real home directory (Global Constraints).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from deep_think_mcp import lifecycle, store
from deep_think_mcp.session import Session


def _make_saved_session(tmp_path, **overrides) -> Session:
    base = dict(
        question="does this move?",
        expected_stages=["Research"],
        current_stage="Research",
    )
    base.update(overrides)
    session = Session(**base)
    session.save_path = str(store.session_path(tmp_path, session.id))
    store.save(session, session.save_path)
    return session


# ---------------------------------------------------------------------------
# finalize()
# ---------------------------------------------------------------------------


def test_finalize_sets_status_finalized(tmp_path):
    session = _make_saved_session(tmp_path)
    lifecycle.finalize(session)
    assert session.status == "finalized"


def test_finalize_returns_the_same_session():
    session = Session(
        question="q", expected_stages=["Research"], current_stage="Research"
    )
    result = lifecycle.finalize(session)
    assert result is session


# ---------------------------------------------------------------------------
# keep_here()
# ---------------------------------------------------------------------------


def test_keep_here_appends_a_decision_record():
    session = Session(
        question="q", expected_stages=["Research"], current_stage="Research"
    )
    lifecycle.keep_here(session)
    assert len(session.decisions) == 1
    assert session.decisions[0].action == "keep_here"


def test_keep_here_does_not_touch_the_filesystem(tmp_path):
    session = _make_saved_session(tmp_path)
    before = (tmp_path / "sessions" / f"{session.id}.json").read_text()

    lifecycle.keep_here(session)

    after = (tmp_path / "sessions" / f"{session.id}.json").read_text()
    assert before == after  # lifecycle.keep_here doesn't persist; caller does


# ---------------------------------------------------------------------------
# move(): happy path
# ---------------------------------------------------------------------------


def test_move_writes_session_to_new_location(tmp_path):
    session = _make_saved_session(tmp_path)
    old_path = session.save_path
    dest = tmp_path / "elsewhere" / "moved.json"
    dest.parent.mkdir()

    lifecycle.move(session, str(dest))

    assert dest.is_file()
    assert store.load(dest) == session
    assert session.save_path == str(dest)
    assert old_path != session.save_path


def test_move_removes_the_original_file(tmp_path):
    session = _make_saved_session(tmp_path)
    old_path = session.save_path
    dest = tmp_path / "elsewhere" / "moved.json"
    dest.parent.mkdir()

    lifecycle.move(session, str(dest))

    assert not os.path.exists(old_path)


def test_move_appends_a_move_history_record(tmp_path):
    session = _make_saved_session(tmp_path)
    old_path = session.save_path
    dest = tmp_path / "elsewhere" / "moved.json"
    dest.parent.mkdir()

    lifecycle.move(session, str(dest))

    assert len(session.move_history) == 1
    record = session.move_history[0]
    assert record.from_path == old_path
    assert record.to_path == str(dest)


def test_move_returns_the_same_session(tmp_path):
    session = _make_saved_session(tmp_path)
    dest = tmp_path / "elsewhere" / "moved.json"
    dest.parent.mkdir()

    result = lifecycle.move(session, str(dest))

    assert result is session


def test_move_expands_user_tilde(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    session = _make_saved_session(tmp_path)
    dest_dir = tmp_path / "docs"
    dest_dir.mkdir()

    lifecycle.move(session, "~/docs/moved.json")

    assert (dest_dir / "moved.json").is_file()
    assert session.save_path == str(dest_dir / "moved.json")


def test_move_to_an_existing_directory_keeps_the_original_filename(tmp_path):
    session = _make_saved_session(tmp_path)
    original_name = os.path.basename(session.save_path)
    dest_dir = tmp_path / "Documents"
    dest_dir.mkdir()

    lifecycle.move(session, str(dest_dir))

    expected = dest_dir / original_name
    assert expected.is_file()
    assert session.save_path == str(expected)


# ---------------------------------------------------------------------------
# move(): validation failures -- must fail cleanly (a raised MoveError with
# a machine-readable code + message), never a raw unhandled exception, and
# must never mutate the session or touch disk when validation fails first.
# ---------------------------------------------------------------------------


def test_move_rejects_relative_path(tmp_path):
    session = _make_saved_session(tmp_path)
    with pytest.raises(lifecycle.MoveError) as exc_info:
        lifecycle.move(session, "relative/path.json")

    assert exc_info.value.code == "destination_not_absolute"
    assert session.move_history == []
    assert os.path.exists(session.save_path)


def test_move_rejects_destination_same_as_current_path(tmp_path):
    session = _make_saved_session(tmp_path)
    with pytest.raises(lifecycle.MoveError) as exc_info:
        lifecycle.move(session, session.save_path)

    assert exc_info.value.code == "destination_same_as_current"
    assert session.move_history == []


def test_move_fails_when_destination_parent_missing(tmp_path):
    session = _make_saved_session(tmp_path)
    dest = tmp_path / "does" / "not" / "exist" / "moved.json"

    with pytest.raises(lifecycle.MoveError) as exc_info:
        lifecycle.move(session, str(dest))

    assert exc_info.value.code == "destination_parent_missing"
    assert session.move_history == []
    assert os.path.exists(session.save_path)


def test_move_fails_when_destination_parent_is_a_file(tmp_path):
    session = _make_saved_session(tmp_path)
    not_a_dir = tmp_path / "im_a_file"
    not_a_dir.write_text("surprise")
    dest = not_a_dir / "moved.json"

    with pytest.raises(lifecycle.MoveError) as exc_info:
        lifecycle.move(session, str(dest))

    assert exc_info.value.code == "destination_parent_not_a_directory"


def test_move_fails_cleanly_when_destination_exists_without_force(tmp_path):
    session = _make_saved_session(tmp_path)
    dest = tmp_path / "elsewhere" / "moved.json"
    dest.parent.mkdir()
    dest.write_text("already here")

    with pytest.raises(lifecycle.MoveError) as exc_info:
        lifecycle.move(session, str(dest))

    assert exc_info.value.code == "destination_exists"
    assert session.move_history == []
    assert os.path.exists(session.save_path)
    assert dest.read_text() == "already here"  # untouched


def test_move_succeeds_with_force_overwriting_existing_destination(tmp_path):
    session = _make_saved_session(tmp_path)
    dest = tmp_path / "elsewhere" / "moved.json"
    dest.parent.mkdir()
    dest.write_text("stale content")

    lifecycle.move(session, str(dest), force=True)

    assert store.load(dest) == session
    assert not os.path.exists(session.move_history[0].from_path)


def test_move_fails_cleanly_when_destination_not_writable(tmp_path):
    session = _make_saved_session(tmp_path)
    old_path = session.save_path
    locked_dir = tmp_path / "locked"
    locked_dir.mkdir()
    locked_dir.chmod(0o555)
    dest = locked_dir / "moved.json"

    try:
        with pytest.raises(lifecycle.MoveError) as exc_info:
            lifecycle.move(session, str(dest))
        assert exc_info.value.code == "destination_not_writable"
        assert session.move_history == []
        assert os.path.exists(old_path)
    finally:
        locked_dir.chmod(0o755)  # let pytest clean up tmp_path


# ---------------------------------------------------------------------------
# move(): cross-filesystem-safe -- must use copy+verify+unlink, never a bare
# rename (which raises EXDEV across filesystems). Proven by making rename
# and shutil.move explode if called at all; the move must still succeed.
# ---------------------------------------------------------------------------


def test_move_never_uses_rename_or_shutil_move(tmp_path, monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("move() must not call rename/shutil.move")

    monkeypatch.setattr(os, "rename", _boom)
    monkeypatch.setattr(shutil, "move", _boom)

    session = _make_saved_session(tmp_path)
    old_path = session.save_path
    dest = tmp_path / "elsewhere" / "moved.json"
    dest.parent.mkdir()

    lifecycle.move(session, str(dest))

    assert dest.is_file()
    assert not os.path.exists(old_path)
    assert store.load(dest) == session


# ---------------------------------------------------------------------------
# move(): write/verify failure after validation passes -- defense-in-depth
# for races (permissions changing between the pre-flight check and the
# actual write, disk full, etc.). Must roll back the in-memory mutation and
# leave the original file untouched, not just raise.
# ---------------------------------------------------------------------------


def test_move_rolls_back_mutation_when_write_fails(tmp_path, monkeypatch):
    session = _make_saved_session(tmp_path)
    old_path = session.save_path
    dest = tmp_path / "elsewhere" / "moved.json"
    dest.parent.mkdir()

    def _explode(*args, **kwargs):
        raise OSError("disk on fire")

    monkeypatch.setattr(store, "save", _explode)

    with pytest.raises(lifecycle.MoveError) as exc_info:
        lifecycle.move(session, str(dest))

    assert exc_info.value.code == "destination_write_failed"
    assert session.move_history == []
    assert session.save_path == old_path
    assert os.path.exists(old_path)


def test_move_rolls_back_mutation_when_verify_read_raises_value_error(tmp_path, monkeypatch):
    """`store.load` can raise a `ValueError` (invalid JSON, or a pydantic
    `ValidationError` -- a `ValueError` subclass -- from unparseable
    content with no usable `.bak`) just as easily as an `OSError`. That
    must roll back and raise `MoveError` exactly like an `OSError` does,
    not propagate past `move()` as a raw `ValueError`.
    """
    session = _make_saved_session(tmp_path)
    old_path = session.save_path
    dest = tmp_path / "elsewhere" / "moved.json"
    dest.parent.mkdir()

    real_save = store.save

    def _corrupt_load(path):
        raise ValueError("invalid JSON, no usable .bak")

    monkeypatch.setattr(store, "save", real_save)
    monkeypatch.setattr(store, "load", _corrupt_load)

    with pytest.raises(lifecycle.MoveError) as exc_info:
        lifecycle.move(session, str(dest))

    assert exc_info.value.code == "destination_write_failed"
    assert session.move_history == []
    assert session.save_path == old_path
    assert os.path.exists(old_path)


def test_move_rolls_back_mutation_when_verification_fails(tmp_path, monkeypatch):
    session = _make_saved_session(tmp_path)
    old_path = session.save_path
    dest = tmp_path / "elsewhere" / "moved.json"
    dest.parent.mkdir()

    real_save = store.save
    monkeypatch.setattr(store, "load", lambda path: _make_saved_session(tmp_path))
    monkeypatch.setattr(store, "save", real_save)

    with pytest.raises(lifecycle.MoveError) as exc_info:
        lifecycle.move(session, str(dest))

    assert exc_info.value.code == "verification_failed"
    assert session.move_history == []
    assert session.save_path == old_path
    assert os.path.exists(old_path)


# ---------------------------------------------------------------------------
# move(): final-unlink failure -- the write to `target` is already done and
# verified by this point, so the move is still a success. This must not be
# a *silent* success, though: `MoveRecord.unlink_failed` is the signal a
# caller (server.py) uses to warn instead of swallowing it. Task 12 ledger
# item (T4 flagged this branch as untested).
# ---------------------------------------------------------------------------


def test_move_records_unlink_failed_when_original_removal_raises_oserror(
    tmp_path, monkeypatch
):
    session = _make_saved_session(tmp_path)
    old_path = Path(session.save_path).resolve()
    dest = tmp_path / "elsewhere" / "moved.json"
    dest.parent.mkdir()

    real_unlink = Path.unlink

    def _flaky_unlink(self, *args, **kwargs):
        if self.resolve() == old_path:
            raise OSError("simulated permissions race")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _flaky_unlink)

    result = lifecycle.move(session, str(dest))

    # Still a success: the verified copy is what matters.
    assert result is session
    assert dest.is_file()
    assert store.load(dest) == session
    # But the original was left behind, and that fact is recorded rather
    # than swallowed.
    assert os.path.exists(old_path)
    assert session.move_history[-1].unlink_failed is True


def test_move_leaves_unlink_failed_false_on_the_ordinary_success_path(tmp_path):
    session = _make_saved_session(tmp_path)
    dest = tmp_path / "elsewhere" / "moved.json"
    dest.parent.mkdir()

    lifecycle.move(session, str(dest))

    assert session.move_history[-1].unlink_failed is False


# ---------------------------------------------------------------------------
# move(): symlink aliasing -- a destination that is a symlink pointing at
# the session's own current file must be caught by the same-path guard
# (`destination_same_as_current`), not treated as a distinct, writable
# target. `_resolve_destination`/`_validate_destination` both run paths
# through `Path.resolve()`, which is documented to follow symlinks -- this
# is the real-filesystem test proving that stdlib guarantee actually holds
# for this module's usage (T4 left this documented-but-untested).
# ---------------------------------------------------------------------------


def test_move_rejects_a_symlink_that_resolves_to_the_current_file(tmp_path):
    session = _make_saved_session(tmp_path)
    current = Path(session.save_path)
    alias_dir = tmp_path / "aliases"
    alias_dir.mkdir()
    alias = alias_dir / "alias.json"
    alias.symlink_to(current)

    with pytest.raises(lifecycle.MoveError) as exc_info:
        lifecycle.move(session, str(alias))

    assert exc_info.value.code == "destination_same_as_current"
    assert session.move_history == []
    assert os.path.exists(session.save_path)


# ---------------------------------------------------------------------------
# move(): status-independence -- moving a not-yet-finalized (still
# "active") session must succeed exactly like a finalized one; the move
# machinery has no opinion on `status` (docs/execution-plan.md Task 12).
# Every other move() test in this file already happens to exercise this
# (none of them call finalize first), but that fact was never pinned down
# by an explicit assertion -- this test makes the intent load-bearing.
# ---------------------------------------------------------------------------


def test_move_of_a_not_yet_finalized_session_succeeds_and_status_is_unaffected(
    tmp_path,
):
    session = _make_saved_session(tmp_path)
    assert session.status == "active"
    dest = tmp_path / "elsewhere" / "moved.json"
    dest.parent.mkdir()

    lifecycle.move(session, str(dest))

    assert session.status == "active"
    assert len(session.move_history) == 1


# ---------------------------------------------------------------------------
# keep_here(): a no-op on an already-moved session -- keep_here only ever
# records a decision; it has no opinion on move_history either, and calling
# it after a move must not raise or mutate save_path/move_history.
# ---------------------------------------------------------------------------


def test_keep_here_on_an_already_moved_session_is_a_valid_no_op(tmp_path):
    session = _make_saved_session(tmp_path)
    dest = tmp_path / "elsewhere" / "moved.json"
    dest.parent.mkdir()
    lifecycle.move(session, str(dest))
    moved_path = session.save_path
    move_history_len = len(session.move_history)

    lifecycle.keep_here(session)

    assert session.save_path == moved_path
    assert len(session.move_history) == move_history_len
    assert len(session.decisions) == 1
    assert session.decisions[0].action == "keep_here"


# ---------------------------------------------------------------------------
# has_uncommitted_thought() -- the mode-agnostic signal `finalize_session`
# uses to decide whether to warn about an in-progress thought (Task 12
# ledger item, rooted in T8's finalize-doesn't-guard-in-progress-thought
# note).
# ---------------------------------------------------------------------------


def test_has_uncommitted_thought_false_by_default():
    session = Session(
        question="q", expected_stages=["Research"], current_stage="Research"
    )
    assert lifecycle.has_uncommitted_thought(session) is False


def test_has_uncommitted_thought_true_when_current_thought_id_is_set():
    session = Session(
        question="q", expected_stages=["Research"], current_stage="Research"
    )
    session.current_thought_id = "some-thought-id"
    assert lifecycle.has_uncommitted_thought(session) is True
