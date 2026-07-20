"""Task 13 hardening-sweep tests -- one per accumulated review-ledger item.

Each test closes a specific item from the brief's hardening sweep:

  1. submit_critique phase guard (a second submit must not clobber a round
     already past await_critique).
  3. naive-timestamp compare in meta._has_post_finalize_decision (must not
     crash on a naive datetime).
  4. duplicate custom stage names rejected at start_session.
  5. was_selected exactly-one-winner invariant asserted at the engine boundary.
  6. portalocker LockException (NOT an OSError) at a persist step degrades to a
     storage_unavailable directive, never a raw traceback.
  7. lens_loader reads templates as UTF-8 regardless of platform locale.

(Item 2 -- set_session_mode concurrent-call race -- is documented as an
accepted single-client-stdio limitation in the task report, not code-changed.)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from portalocker.exceptions import LockException

from deep_think_mcp import lens_loader, meta, server, store, subagent_engine
from deep_think_mcp.necort_adapter import NECoRTResult
from deep_think_mcp.session import MoveRecord, Session, SpecialistRound, UtilityScore


async def _call(client, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    result = await client.call_tool(name, arguments or {})
    assert not result.isError, result.content
    assert result.structuredContent is not None
    return result.structuredContent


# ---------------------------------------------------------------------------
# #1 submit_critique phase guard
# ---------------------------------------------------------------------------


async def test_submit_critique_twice_does_not_clobber_and_directs_to_refine(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        started = await _call(client, "start_session", {"question": "q", "mode": "serial"})
        sid = started["session_id"]
        await _call(client, "begin_thought", {"session_id": sid, "content": "draft"})
        await _call(client, "critique_current_thought", {"session_id": sid})
        await _call(client, "submit_critique", {"session_id": sid, "text": "the real critique"})

        # second submit on a round already past await_critique -> directive
        again = await _call(client, "submit_critique", {"session_id": sid, "text": "OVERWRITE"})
        assert again["error"] == "sequencing"
        assert again["code"] == "need_refine"
        assert again["next_tool"] == "refine_current_thought"

    session = store.load(store.session_path(tmp_path, sid))
    rnd = session.thoughts[0].critique_rounds[0]
    assert rnd.critique_text == "the real critique"  # NOT clobbered


# ---------------------------------------------------------------------------
# #3 naive-timestamp compare hardening
# ---------------------------------------------------------------------------


def test_has_post_finalize_decision_tolerates_naive_timestamps():
    finalized = datetime(2026, 1, 1, tzinfo=timezone.utc)
    session = Session(
        question="q",
        mode="serial",
        expected_stages=["A"],
        current_stage="A",
        status="finalized",
        finalized_at=finalized,
        # a NAIVE timestamp (as a hand-edited/older-import session might carry),
        # timestamped after finalization -> counts as a post-finalize decision.
        move_history=[
            MoveRecord(
                from_path="/x",
                to_path="/y",
                timestamp=datetime(2026, 1, 2),  # naive, no tzinfo
            )
        ],
    )
    # Must not raise "can't compare offset-naive and offset-aware datetimes".
    assert meta._has_post_finalize_decision(session) is True

    # A naive timestamp BEFORE finalization does not count.
    session.move_history[0].timestamp = finalized.replace(tzinfo=None) - timedelta(days=1)
    assert meta._has_post_finalize_decision(session) is False


def test_next_action_does_not_crash_on_naive_timestamp(tmp_path):
    cfg = {"subagent": {}, "serial": {}}
    session = Session(
        question="q",
        mode="serial",
        expected_stages=["A"],
        current_stage="A",
        status="finalized",
        finalized_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        move_history=[MoveRecord(from_path="/x", to_path="/y", timestamp=datetime(2026, 1, 2))],
    )
    result = meta.next_action(session, cfg)  # must not raise
    assert result.code == "session_complete"


# ---------------------------------------------------------------------------
# #4 duplicate custom stage names rejected at start_session
# ---------------------------------------------------------------------------


async def test_start_session_rejects_duplicate_stage_names(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        payload = await _call(
            client,
            "start_session",
            {"question": "q", "mode": "serial", "stages": ["Analysis", "Research", "Analysis"]},
        )
    assert payload["error"] == "retry_with_clarification"
    assert payload["parameter"] == "stages"
    assert "unique" in payload["expected"].lower()


async def test_start_session_rejects_duplicate_stage_names_from_plaintext(tmp_path):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        payload = await _call(
            client,
            "start_session",
            {"question": "q", "mode": "serial", "stages": "Analysis, Research, Analysis"},
        )
    assert payload["error"] == "retry_with_clarification"


# ---------------------------------------------------------------------------
# #5 was_selected exactly-one-winner invariant
# ---------------------------------------------------------------------------


def _result_with_n_winners(n_winners: int) -> NECoRTResult:
    def _u(v):
        return UtilityScore(
            correctness=v, clarity=v, coverage=v,
            evidence=0.5, novelty=0.5, bias_resistance=0.5, actionability=0.5,
        )

    rounds = [
        SpecialistRound(
            round_index=0,
            agent_role=f"agent_{i}",
            candidate_content=f"c{i}",
            utility_vector=_u(0.8),
            equilibrium_state="in_equilibrium",
            was_selected=(i < n_winners),
        )
        for i in range(3)
    ]
    return NECoRTResult(
        response="r",
        specialist_rounds=rounds,
        final_utility_scores=_u(0.8),
        converged=True,
        convergence_round=0,
        thinking_rounds=1,
        final_response_agent=0,
        raw={},
    )


class _FakeAdapter:
    def __init__(self, result):
        self._result = result

    async def run(self, user_input, max_rounds=None):
        return self._result


async def test_zero_winner_equilibrium_returns_adapter_directive(tmp_path, monkeypatch):
    monkeypatch.setattr(
        subagent_engine, "_make_adapter", lambda *a, **k: _FakeAdapter(_result_with_n_winners(0))
    )
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        started = await _call(
            client,
            "start_session",
            {"question": "q", "mode": "subagent", "overrides": {"subagent": {"endpoint": "http://ep"}}},
        )
        sid = started["session_id"]
        payload = await _call(client, "begin_subagent_thought", {"session_id": sid, "content": "seed"})

    assert payload["error"] == "adapter_error"
    assert payload["retryable"] is False
    session = store.load(store.session_path(tmp_path, sid))
    assert session.thoughts == []  # invariant checked before mutation -> clean


async def test_two_winner_equilibrium_returns_adapter_directive(tmp_path, monkeypatch):
    monkeypatch.setattr(
        subagent_engine, "_make_adapter", lambda *a, **k: _FakeAdapter(_result_with_n_winners(2))
    )
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        started = await _call(
            client,
            "start_session",
            {"question": "q", "mode": "subagent", "overrides": {"subagent": {"endpoint": "http://ep"}}},
        )
        sid = started["session_id"]
        payload = await _call(client, "begin_subagent_thought", {"session_id": sid, "content": "seed"})

    assert payload["error"] == "adapter_error"
    assert payload["retryable"] is False


# ---------------------------------------------------------------------------
# #6 LockException at a persist step -> storage_unavailable directive
# ---------------------------------------------------------------------------


async def test_lock_timeout_on_persist_returns_storage_directive_not_traceback(tmp_path, monkeypatch):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        started = await _call(client, "start_session", {"question": "q", "mode": "serial"})
        sid = started["session_id"]

        def _boom(*args, **kwargs):
            # portalocker.exceptions.LockException is NOT an OSError subclass --
            # this is exactly the escape storage_guard exists to catch.
            raise LockException("simulated lock timeout")

        monkeypatch.setattr(server.store, "save", _boom)

        payload = await _call(client, "finalize_session", {"session_id": sid})

    assert payload["error"] == "storage_unavailable"
    assert payload["retryable"] is True
    assert payload["session_id"] == sid


async def test_lock_timeout_on_read_returns_storage_directive(tmp_path, monkeypatch):
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        started = await _call(client, "start_session", {"question": "q", "mode": "serial"})
        sid = started["session_id"]

        def _boom(*args, **kwargs):
            raise LockException("simulated lock timeout on read")

        monkeypatch.setattr(server.index, "get", _boom)

        payload = await _call(client, "resume_session", {"session_id": sid})

    assert payload["error"] == "storage_unavailable"


# ---------------------------------------------------------------------------
# F1 corrupt JSON (ValueError family) at the load boundary -> storage directive
# ---------------------------------------------------------------------------


async def test_corrupt_session_file_no_bak_returns_storage_directive(tmp_path):
    """F1: a corrupt session JSON with no `.bak` raises pydantic
    ValidationError (a ValueError subclass) at store.load; storage_guard must
    degrade it to a storage_unavailable directive, not a raw isError."""
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        started = await _call(client, "start_session", {"question": "q", "mode": "serial"})
        sid = started["session_id"]

        path = store.session_path(tmp_path, sid)
        bak = path.with_name(path.name + ".bak")
        if bak.exists():
            bak.unlink()  # no .bak sibling can recover the corruption
        path.write_text("{ this is not valid session json")

        payload = await _call(client, "resume_session", {"session_id": sid})

    assert payload["error"] == "storage_unavailable"
    assert payload["retryable"] is True
    assert payload["session_id"] == sid


async def test_corrupt_index_file_no_bak_returns_storage_directive(tmp_path):
    """F1: a corrupt index.json with no `.bak` raises json.JSONDecodeError (a
    ValueError subclass) at index._read_locked; storage_guard must degrade it
    to a storage_unavailable directive, not a raw isError."""
    srv = server.create_server(root=tmp_path)
    async with create_connected_server_and_client_session(srv) as client:
        await _call(client, "start_session", {"question": "q", "mode": "serial"})

        idx = server.index.index_path(tmp_path)
        bak = idx.with_name(idx.name + ".bak")
        if bak.exists():
            bak.unlink()
        idx.write_text("{ not valid json either")

        payload = await _call(client, "list_sessions")

    assert payload["error"] == "storage_unavailable"
    assert payload["retryable"] is True


# ---------------------------------------------------------------------------
# #7 lens_loader UTF-8 encoding pin
# ---------------------------------------------------------------------------


def test_lens_loader_reads_utf8_user_lens(tmp_path):
    user_lenses = tmp_path / "lenses"
    user_lenses.mkdir()
    # em-dash + arrow + curly quotes: valid UTF-8, not representable in ascii
    content = "This lens uses — an em-dash, → an arrow, and “curly quotes”."
    (user_lenses / "custom_lens.md").write_text(content, encoding="utf-8")

    lenses = lens_loader.discover_lenses(tmp_path)
    assert lenses["custom_lens"] == content  # exact round-trip, no mojibake
