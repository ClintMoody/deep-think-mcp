"""Tolerant boundary parsers -- the M5 "unreliable JSON mode" accommodation.

`docs/build-plan.md` § "Local-model accommodations": "all tools accept either
JSON or a plain-text fallback the server parses tolerantly; malformed input
returns a `retry_with_clarification` template rather than an error." This
module is that fallback layer, and *only* that layer: a handful of small,
pure parsers that turn a weak local model's messy input for a structured tool
parameter into the clean Python value the engine expects.

Division of labor (deliberate, per the task brief's controller guidance):

  - Tolerance is a **boundary** concern. These parsers are applied in
    `server.py`'s tool wrappers, at the very edge, and nowhere else. Engine
    internals (`serial_engine`, `subagent_engine`, `manual_engine`, `meta`,
    `lifecycle`, ...) stay strict -- they receive already-clean values and
    never second-guess their types. A single place to reason about "what did
    the model actually mean" keeps the strict core small and testable.
  - On malformed input a parser raises `TolerantParseError`, carrying the
    exact `param` name, the `expected` shape, and a concrete `example`. The
    server maps that to `prompts.retry_with_clarification` -- a directive
    naming the expected shape, never a raw traceback and never a silent
    default (a silent default would let a weak model's mistake corrupt the
    reasoning record invisibly, which is worse than asking it to retry).

Every parser is idempotent on already-structured input: a real JSON array
arriving as a Python `list` passes straight through, a plaintext
`"a, b, c"` is split, and only genuinely unparseable input raises. That dual
acceptance is what lets one widened annotation (`list[str] | str | None`)
serve both the JSON-mode and the plaintext-mode caller.
"""

from __future__ import annotations

import json
import re
from typing import Any

# A fenced code block, optionally language-tagged (```json ... ```), captured
# so we can pull the JSON body out of a chatty model's prose.
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
# The first {...} object literal embedded anywhere in a string (non-greedy on
# the outside, but balanced enough for the flat score/override objects we
# accept -- we only ever json.loads the captured span, which validates it).
_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

_TRUE_WORDS = {"true", "t", "yes", "y", "1", "on"}
_FALSE_WORDS = {"false", "f", "no", "n", "0", "off"}


class TolerantParseError(Exception):
    """A tool parameter could not be parsed from the model's input.

    Carries the `param` name, the `expected` shape (human-readable), and a
    concrete `example` -- exactly the three ingredients
    `prompts.retry_with_clarification` needs to tell the model what to send
    instead. Raised only from this module; caught only at the tool boundary
    in `server.py`. Never surfaced as a raw error to the model.
    """

    def __init__(self, param: str, *, expected: str, example: str) -> None:
        self.param = param
        self.expected = expected
        self.example = example
        super().__init__(f"could not parse '{param}' (expected {expected})")


# ---------------------------------------------------------------------------
# JSON extraction helper -- tolerant of chatty models
# ---------------------------------------------------------------------------


def _try_extract_json(text: str) -> Any | None:
    """Best-effort: pull a JSON value out of `text`, tolerating code fences
    and surrounding prose. Returns the parsed value, or None if no JSON could
    be recovered. Never raises.
    """
    candidates: list[str] = [text]
    fence = _FENCE_RE.search(text)
    if fence:
        candidates.append(fence.group(1))
    obj = _OBJECT_RE.search(text)
    if obj:
        candidates.append(obj.group(0))
    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except (ValueError, TypeError):
            continue
    return None


# ---------------------------------------------------------------------------
# parse_string_list -- tags / axioms / stages / challenged_assumptions
# ---------------------------------------------------------------------------


def parse_string_list(raw: Any, *, param: str) -> list[str] | None:
    """Normalize `raw` into a `list[str]` (or None if `raw` is None).

    Accepts: a real list (members coerced to str); a JSON-array string
    (`'["a", "b"]'`); or a plain comma/newline-separated string
    (`"a, b, c"`). Blank members are dropped. An empty/whitespace string
    yields `[]`. Anything else raises `TolerantParseError`.
    """
    if raw is None:
        return None
    if isinstance(raw, list):
        return [str(item) for item in raw]
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return []
        extracted = _try_extract_json(stripped)
        if isinstance(extracted, list):
            return [str(item) for item in extracted]
        # Plaintext fallback: split on newlines and commas.
        parts = [piece.strip() for piece in re.split(r"[,\n]", stripped)]
        return [piece for piece in parts if piece]
    raise TolerantParseError(
        param,
        expected="a JSON array of strings, or a comma/newline-separated list",
        example='["assumption A", "assumption B"]  (or:  assumption A, assumption B)',
    )


# ---------------------------------------------------------------------------
# parse_scores -- the 7-dimension self-score vector
# ---------------------------------------------------------------------------

_SCORES_EXPECTED = "a JSON object mapping dimension names to 0-1 scores"
_SCORES_EXAMPLE = '{"correctness": 0.8, "clarity": 0.7}  (or:  correctness: 0.8, clarity: 0.7)'


def parse_scores(raw: Any, *, param: str = "scores") -> dict[str, Any]:
    """Normalize `raw` into a `dict` of dimension -> score.

    Accepts: a real dict (passthrough -- the engine does the dimension
    normalization + clamping); a JSON object string, optionally fenced or
    embedded in prose; or plaintext `"correctness: 0.8, clarity: 0.7"`
    lines/comma-separated pairs. None or an empty string yields `{}` (the
    engine treats an empty score dict as "carry everything forward" -- a
    legitimate no-op, not a malformed input). Unparseable non-empty input
    raises `TolerantParseError`.
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return {}
        extracted = _try_extract_json(stripped)
        if isinstance(extracted, dict):
            return extracted
        parsed = _parse_score_pairs(stripped)
        if parsed:
            return parsed
        raise TolerantParseError(param, expected=_SCORES_EXPECTED, example=_SCORES_EXAMPLE)
    raise TolerantParseError(param, expected=_SCORES_EXPECTED, example=_SCORES_EXAMPLE)


def _parse_score_pairs(text: str) -> dict[str, float]:
    """Parse `"correctness: 0.8, clarity: 0.7"` style pairs into a dict.

    Splits on commas and newlines, then each piece on its first ':'. Values
    that don't parse as a float are skipped (the caller raises if nothing at
    all parsed). Keys are left exactly as written -- the engine's own score
    merge normalizes them (lowercasing, '-'/' ' -> '_').
    """
    out: dict[str, float] = {}
    for piece in re.split(r"[,\n]", text):
        if ":" not in piece:
            continue
        key, _, value = piece.partition(":")
        key = key.strip()
        if not key:
            continue
        try:
            out[key] = float(value.strip())
        except (TypeError, ValueError):
            continue
    return out


# ---------------------------------------------------------------------------
# parse_bool -- move_session(force), any future boolean flag
# ---------------------------------------------------------------------------


def parse_bool(raw: Any, *, param: str) -> bool | None:
    """Normalize `raw` into a bool (or None if `raw` is None).

    Accepts a real bool, an int (0/1), or a word: true/false, yes/no, y/n,
    on/off, 1/0 (case-insensitive). Anything else raises
    `TolerantParseError`.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int):
        return raw != 0
    if isinstance(raw, str):
        word = raw.strip().lower()
        if word in _TRUE_WORDS:
            return True
        if word in _FALSE_WORDS:
            return False
    raise TolerantParseError(
        param,
        expected="a boolean: true/false (also yes/no, 1/0)",
        example="true",
    )


# ---------------------------------------------------------------------------
# parse_json_or_text -- start_session(overrides), any JSON-object param
# ---------------------------------------------------------------------------


def parse_json_or_text(raw: Any, *, param: str) -> dict[str, Any] | None:
    """Normalize `raw` into a `dict` (or None if `raw` is None).

    Accepts a real dict (passthrough) or a JSON-object string (optionally
    fenced / embedded in prose). Anything that isn't a JSON object raises
    `TolerantParseError` -- unlike the list/score parsers there is no
    plaintext fallback shape for an arbitrary nested config object.
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        extracted = _try_extract_json(raw)
        if isinstance(extracted, dict):
            return extracted
    raise TolerantParseError(
        param,
        expected="a JSON object",
        example='{"serial": {"max_rounds": 1}}',
    )
