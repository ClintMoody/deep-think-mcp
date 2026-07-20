"""Pydantic data model for deep-think-mcp sessions.

Models are exactly per `docs/build-plan.md` § Data model, with two small
structural additions the brief calls for but doesn't name: `MoveRecord` for
`Session.move_history` entries and `DecisionRecord` for `Session.decisions`
entries (both marked [derived] below, mirroring the brief's own markers).

This module only defines the schema. It has no persistence logic (see
store.py) and no session-creation/lifecycle logic (later tasks) -- e.g. it
does not decide what a *new* session's `current_stage` should be, only that
the field exists and holds a string.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def _uuid4_hex() -> str:
    return uuid.uuid4().hex


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UtilityScore(BaseModel):
    """The 7 NECoRT utility dimensions (Global Constraints), floats in
    [0, 1] [derived range]. Shared schema across both execution modes --
    `serial` and `subagent` both populate every field here, only the source
    of the numbers (critique rounds vs. specialist rounds) differs.
    """

    model_config = ConfigDict(extra="forbid")

    correctness: float = Field(ge=0.0, le=1.0)
    evidence: float = Field(ge=0.0, le=1.0)
    novelty: float = Field(ge=0.0, le=1.0)
    clarity: float = Field(ge=0.0, le=1.0)
    bias_resistance: float = Field(ge=0.0, le=1.0)
    actionability: float = Field(ge=0.0, le=1.0)
    coverage: float = Field(ge=0.0, le=1.0)


class CritiqueRound(BaseModel):
    """One round of the serial-mode critique loop."""

    model_config = ConfigDict(extra="forbid")

    round_index: int
    lens: str
    critique_text: str
    refined_content: str
    delta_score: float


class SpecialistRound(BaseModel):
    """One round of the subagent-mode NECoRT loop."""

    model_config = ConfigDict(extra="forbid")

    round_index: int
    agent_role: str
    candidate_content: str
    utility_vector: UtilityScore
    equilibrium_state: str
    was_selected: bool


class Thought(BaseModel):
    """A single thought within a session's stage progression."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=_uuid4_hex)
    stage: str
    position: int
    timestamp: datetime = Field(default_factory=_utcnow)
    content: str
    tags: list[str] = Field(default_factory=list)
    axioms: list[str] = Field(default_factory=list)
    challenged_assumptions: list[str] = Field(default_factory=list)
    # Mode-tagged: a serial-mode thought populates critique_rounds, a
    # subagent-mode thought populates specialist_rounds. Both fields exist
    # on every Thought; which one is used follows the owning session's mode.
    critique_rounds: list[CritiqueRound] = Field(default_factory=list)
    specialist_rounds: list[SpecialistRound] = Field(default_factory=list)
    final_utility_scores: UtilityScore | None = None
    committed: bool = False


class MoveRecord(BaseModel):
    """One `Session.move_history` audit entry [derived structure]."""

    model_config = ConfigDict(extra="forbid")

    from_path: str
    to_path: str
    timestamp: datetime = Field(default_factory=_utcnow)


class DecisionRecord(BaseModel):
    """One `Session.decisions` audit entry, e.g. a `keep_here` call
    [derived structure].
    """

    model_config = ConfigDict(extra="forbid")

    action: str
    timestamp: datetime = Field(default_factory=_utcnow)


class Session(BaseModel):
    """A single deep-think reasoning session."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=_uuid4_hex)
    question: str
    created_at: datetime = Field(default_factory=_utcnow)
    mode: Literal["serial", "subagent"] | None = None
    expected_stages: list[str]
    current_stage: str
    current_thought_id: str | None = None
    status: Literal["active", "finalized", "archived"] = "active"
    # When `status` became "finalized" (set by `lifecycle.finalize()`).
    # None for a session finalized before this field existed, or never
    # finalized. [derived, task 8 fix round 1]: next_action() uses this as
    # the cutoff for "was a move_session/keep_here decision made in answer
    # to finalize's own move/keep prompt" -- move_session/keep_here are
    # deliberately status-independent (docs/execution-plan.md Task 12), so
    # an earlier decision on a still-active session must not be mistaken
    # for an answer to a prompt that hadn't been asked yet.
    finalized_at: datetime | None = None
    save_path: str = ""
    # Raw per-session config overrides dict as passed to start_session(),
    # e.g. {"serial": {"max_rounds": 1}} -- Global Constraints requires
    # "all settings per-session-overridable via start_session args", but
    # docs/build-plan.md's Data model section doesn't name a field for it.
    # [derived structure, task 3]: persisted verbatim (not merged/resolved)
    # so later engine tasks (7, 11) can feed it back through
    # config.load_config(root, overrides=session.overrides) whenever they
    # need this session's effective config, without re-deriving it from
    # scratch or losing it between tool calls.
    overrides: dict[str, Any] = Field(default_factory=dict)
    move_history: list[MoveRecord] = Field(default_factory=list)
    thoughts: list[Thought] = Field(default_factory=list)
    decisions: list[DecisionRecord] = Field(default_factory=list)
