"""Unit tests for deep_think_mcp.tolerant -- the boundary parsers (Task 13).

These are the small, pure parsers that let every tool accept JSON *or*
plaintext-ish input from a weak local model with unreliable JSON mode
(`docs/build-plan.md` § "Local-model accommodations": "all tools accept
either JSON or a plain-text fallback the server parses tolerantly").

No filesystem, no MCP, no network: each parser is exercised across the
happy (already-structured), plaintext, and malformed axes. Malformed input
must raise `TolerantParseError` (which the server maps to a
`retry_with_clarification` directive) -- never silently default, never a
raw `ValueError`.
"""

from __future__ import annotations

import pytest

from deep_think_mcp import tolerant
from deep_think_mcp.tolerant import TolerantParseError


# ---------------------------------------------------------------------------
# parse_string_list
# ---------------------------------------------------------------------------


def test_parse_string_list_passthrough_list():
    assert tolerant.parse_string_list(["a", "b"], param="tags") == ["a", "b"]


def test_parse_string_list_coerces_non_str_members():
    assert tolerant.parse_string_list([1, 2], param="tags") == ["1", "2"]


def test_parse_string_list_none_returns_none():
    assert tolerant.parse_string_list(None, param="tags") is None


def test_parse_string_list_json_array_string():
    assert tolerant.parse_string_list('["x", "y"]', param="tags") == ["x", "y"]


def test_parse_string_list_comma_separated():
    assert tolerant.parse_string_list("x, y, z", param="tags") == ["x", "y", "z"]


def test_parse_string_list_newline_separated():
    assert tolerant.parse_string_list("x\ny\nz", param="tags") == ["x", "y", "z"]


def test_parse_string_list_mixed_separators_and_blank_drop():
    assert tolerant.parse_string_list("x,\n, y ,\n\nz,", param="tags") == ["x", "y", "z"]


def test_parse_string_list_empty_string_returns_empty_list():
    assert tolerant.parse_string_list("   ", param="tags") == []


def test_parse_string_list_rejects_wrong_type():
    with pytest.raises(TolerantParseError) as exc:
        tolerant.parse_string_list(3.5, param="tags")
    assert exc.value.param == "tags"
    assert exc.value.example


# ---------------------------------------------------------------------------
# parse_scores
# ---------------------------------------------------------------------------


def test_parse_scores_passthrough_dict():
    assert tolerant.parse_scores({"correctness": 0.8}, param="scores") == {"correctness": 0.8}


def test_parse_scores_none_returns_empty_dict():
    assert tolerant.parse_scores(None, param="scores") == {}


def test_parse_scores_json_object_string():
    got = tolerant.parse_scores('{"correctness": 0.8, "clarity": 0.7}', param="scores")
    assert got == {"correctness": 0.8, "clarity": 0.7}


def test_parse_scores_fenced_json_in_prose():
    raw = "Here are my scores:\n```json\n{\"correctness\": 0.9}\n```\nDone."
    assert tolerant.parse_scores(raw, param="scores") == {"correctness": 0.9}


def test_parse_scores_bare_object_embedded_in_prose():
    raw = "scores {\"clarity\": 0.6} ok"
    assert tolerant.parse_scores(raw, param="scores") == {"clarity": 0.6}


def test_parse_scores_colon_lines():
    raw = "correctness: 0.8\nclarity: 0.7\nnovelty: 0.5"
    got = tolerant.parse_scores(raw, param="scores")
    assert got == {"correctness": 0.8, "clarity": 0.7, "novelty": 0.5}


def test_parse_scores_colon_comma_separated():
    got = tolerant.parse_scores("correctness: 0.8, clarity: 0.7", param="scores")
    assert got == {"correctness": 0.8, "clarity": 0.7}


def test_parse_scores_empty_string_returns_empty_dict():
    assert tolerant.parse_scores("   ", param="scores") == {}


def test_parse_scores_rejects_unparseable():
    with pytest.raises(TolerantParseError) as exc:
        tolerant.parse_scores("this has no numbers at all", param="scores")
    assert exc.value.param == "scores"


def test_parse_scores_rejects_wrong_type():
    with pytest.raises(TolerantParseError):
        tolerant.parse_scores(7, param="scores")


# ---------------------------------------------------------------------------
# parse_bool
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [True, "true", "True", "yes", "y", "1", "on"])
def test_parse_bool_truthy(raw):
    assert tolerant.parse_bool(raw, param="force") is True


@pytest.mark.parametrize("raw", [False, "false", "No", "n", "0", "off"])
def test_parse_bool_falsy(raw):
    assert tolerant.parse_bool(raw, param="force") is False


def test_parse_bool_none_returns_none():
    assert tolerant.parse_bool(None, param="force") is None


def test_parse_bool_rejects_gibberish():
    with pytest.raises(TolerantParseError) as exc:
        tolerant.parse_bool("maybe", param="force")
    assert exc.value.param == "force"


# ---------------------------------------------------------------------------
# parse_json_or_text
# ---------------------------------------------------------------------------


def test_parse_json_or_text_passthrough_dict():
    assert tolerant.parse_json_or_text({"a": 1}, param="overrides") == {"a": 1}


def test_parse_json_or_text_none():
    assert tolerant.parse_json_or_text(None, param="overrides") is None


def test_parse_json_or_text_json_object_string():
    assert tolerant.parse_json_or_text('{"a": 1}', param="overrides") == {"a": 1}


def test_parse_json_or_text_fenced():
    assert tolerant.parse_json_or_text("```json\n{\"a\": 1}\n```", param="overrides") == {"a": 1}


def test_parse_json_or_text_rejects_non_object():
    with pytest.raises(TolerantParseError):
        tolerant.parse_json_or_text("not json", param="overrides")


# ---------------------------------------------------------------------------
# TolerantParseError carries the retry_with_clarification ingredients
# ---------------------------------------------------------------------------


def test_error_carries_param_expected_example():
    err = TolerantParseError("scores", expected="a JSON object of dimension:score", example='{"correctness": 0.8}')
    assert err.param == "scores"
    assert "JSON" in err.expected
    assert err.example == '{"correctness": 0.8}'
