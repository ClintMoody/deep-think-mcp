"""Smoke-import test for the vendored NECoRT submodule (Task 9).

Per docs/execution-plan.md Task 9 / the HYBRID DECISION in
`.superpowers/sdd/task-9-brief.md`, we vendor only the Nash-equilibrium
core of PR #7 (`recursive_thinking_ai.py` + `nash_recursive_thinking.py`)
as a git submodule at `vendor/necort/`, pinned to
`f4d290ceb086d47bb0f872164344836c47134452`. Per the recon report
(`.superpowers/sdd/necort-recon.md`), the `enhanced-implementations/` files
are disconnected heuristic filler -- zero LLM calls, no `__init__.py`,
invalid hyphenated module filenames -- and are deliberately NOT vendored
or imported here.

The vendored repo has no package structure at all (no `pyproject.toml` /
`setup.py`, no `__init__.py`, flat imports): `nash_recursive_thinking.py`
imports its sibling via a bare `from recursive_thinking_ai import ...`,
which only resolves if `vendor/necort` itself is on `sys.path`. This is
exactly how Task 10's adapter must load it too --
`sys.path.insert(0, <path to vendor/necort>)` before importing, NOT a
`vendor.necort.recursive_thinking_ai` package import.

This test covers IMPORT ONLY. Per the recon report, `nash_recursive_thinking.py`
never imports `datetime` at module scope yet calls `datetime.now()` inside
`think_and_respond()` / `save_nash_equilibrium_log()` -- a verified,
100%-reproducible `NameError` at CALL time, not import time. Shimming that
crash is Task 10's job (the adapter), not this test's.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

VENDOR_NECORT = Path(__file__).resolve().parent.parent / "vendor" / "necort"
EXPECTED_PIN_SHA = "f4d290ceb086d47bb0f872164344836c47134452"


def _submodule_populated() -> bool:
    return (VENDOR_NECORT / "recursive_thinking_ai.py").is_file() and (
        VENDOR_NECORT / "nash_recursive_thinking.py"
    ).is_file()


@pytest.fixture
def necort_on_path():
    """Splice vendor/necort onto sys.path for one test, exactly as Task
    10's adapter will, then remove it and drop the modules it caused to be
    imported -- so this doesn't leak import state into other tests."""
    if not _submodule_populated():
        pytest.skip(
            "vendor/necort submodule not initialized -- run "
            "`git submodule update --init` first"
        )
    path_str = str(VENDOR_NECORT)
    sys.path.insert(0, path_str)
    injected = ("recursive_thinking_ai", "nash_recursive_thinking")
    try:
        yield
    finally:
        sys.path.remove(path_str)
        for name in injected:
            sys.modules.pop(name, None)


def test_recursive_thinking_ai_imports(necort_on_path):
    import recursive_thinking_ai

    assert hasattr(recursive_thinking_ai, "EnhancedRecursiveThinkingChat")


def test_nash_recursive_thinking_imports(necort_on_path):
    import nash_recursive_thinking

    assert hasattr(nash_recursive_thinking, "NashEquilibriumRecursiveChat")

    # Confirms the flat sibling import actually resolved through sys.path
    # (not e.g. a stale cached module) -- the subclass really does extend
    # the base chat class.
    from recursive_thinking_ai import EnhancedRecursiveThinkingChat

    assert issubclass(
        nash_recursive_thinking.NashEquilibriumRecursiveChat,
        EnhancedRecursiveThinkingChat,
    )


def test_pinned_sha_matches_expected():
    """Guards the "reproducible pin" goal of Task 9: the submodule gitlink
    must point at the exact SHA recorded in docs/execution-plan.md's
    Global Constraints and docs/repinning_necort.md."""
    if not VENDOR_NECORT.is_dir():
        pytest.skip("vendor/necort submodule not initialized")
    result = subprocess.run(
        ["git", "-C", str(VENDOR_NECORT), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("vendor/necort submodule not initialized")
    assert result.stdout.strip() == EXPECTED_PIN_SHA
