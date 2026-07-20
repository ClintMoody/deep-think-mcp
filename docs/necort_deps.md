# NECoRT dependency merge (Task 9)

`vendor/necort/` is a git submodule pinned to
`f4d290ceb086d47bb0f872164344836c47134452` — the head of
`PhialsBasement/Chain-of-Recursive-Thoughts` PR #7 — per the Global
Constraints in `docs/execution-plan.md`.

## Scope: which files, and why that limits the dependency set

Per the HYBRID DECISION recorded in `.superpowers/sdd/task-9-brief.md`
(owner-approved 2026-07-19, informed by `.superpowers/sdd/necort-recon.md`),
we vendor the whole PR #7 tree (so the submodule stays a faithful, re-pinnable
mirror of the upstream branch) but only *use* two files from it:

- `vendor/necort/recursive_thinking_ai.py`
- `vendor/necort/nash_recursive_thinking.py`

Everything else in the tree — `enhanced-implementations/*.py` (specialist
agents, 7-D utility matrix, bias detection, continuous-learning pipeline),
the FastAPI web app (`necort_web.py`, `recthink_web.py`), and the React
frontend (`frontend/`) — is present in the submodule but **not imported by
anything in this project**. Per the recon report, the `enhanced-implementations/`
code is disconnected filler (zero LLM calls, no `__init__.py`, invalid
hyphenated module filenames) and is explicitly out of scope for T9–T11.

Because our dependency footprint tracks *usage*, not the full upstream
`requirements.txt`, we do **not** pull in `fastapi`, `uvicorn[standard]`,
`websockets`, or `python-dotenv` — those exist only to run `necort_web.py` /
the web UI, which this project never imports or runs. `pydantic` is already
one of this project's own core dependencies (see root `pyproject.toml`), so
no separate action was needed for it.

## What the two used modules actually import

Confirmed by direct inspection of the vendored source at the pinned SHA
(`grep -n "^import\|^from"` against both files):

```
recursive_thinking_ai.py:
    import openai            # stdlib: no. third-party: yes (see below)
    import os                # stdlib
    from typing import List, Dict   # stdlib
    import json              # stdlib
    import requests          # third-party
    from datetime import datetime   # stdlib
    import sys               # stdlib
    import time              # stdlib

nash_recursive_thinking.py:
    from recursive_thinking_ai import EnhancedRecursiveThinkingChat  # sibling, flat import
    import numpy as np       # third-party
    from typing import List, Dict, Tuple   # stdlib
    import json              # stdlib
```

Third-party imports needed to *import* (not necessarily fully exercise) both
modules: **`requests`, `numpy`, `openai`**. Nothing else.

### `openai` is a required import-time dependency despite being dead code

The recon report flags `import openai` in `recursive_thinking_ai.py` as dead
code: grepping the whole upstream repo for `openai\.` usage returns zero
hits — all real HTTP calls go through `requests` to a hardcoded OpenRouter
URL (`self.base_url = "https://openrouter.ai/api/v1/chat/completions"`,
`_call_api()`). That's a true statement about *runtime behavior*.

It is not, however, a reason to omit the package from our dependency set.
The statement `import openai` executes unconditionally at module load —
Python must be able to resolve it whether or not the name is ever used
afterward. Verified empirically while building this task:

```
$ uv run python -c "
import sys; sys.path.insert(0, 'vendor/necort')
import nash_recursive_thinking
"
Traceback (most recent call last):
  ...
  File ".../vendor/necort/recursive_thinking_ai.py", line 1, in <module>
    import openai
ModuleNotFoundError: No module named 'openai'
```
Reproduced with `openai` removed from the environment; the import chain
`nash_recursive_thinking` → `recursive_thinking_ai` → `import openai` fails
immediately. After adding `openai` back, both modules import cleanly and
`NashEquilibriumRecursiveChat` is confirmed to be a subclass of
`EnhancedRecursiveThinkingChat` (see `tests/test_necort_vendor.py`).

So: "`import openai` is dead code" only means *the adapter (Task 10) must
not assume the OpenAI SDK is actually driving inference* — it drives
nothing. It does **not** mean the dependency can be dropped; without it,
the two modules this project actually needs cannot be imported at all. We
add `openai` to `pyproject.toml` for exactly this reason, and no other: it
is present to satisfy an import statement, not because anything in this
codebase calls it.

## Pins added to `pyproject.toml`

Upstream `requirements.txt` (post-PR, verbatim from the recon report) pins
everything as loose, unbounded lower bounds:

```
fastapi>=0.95.0
uvicorn[standard]>=0.21.0
websockets>=11.0.3
pydantic>=1.10.7
python-dotenv>=1.0.0
requests>=2.28.0
openai
numpy>=1.20.0
```

Of those, only `requests`, `numpy`, and `openai` are relevant here (per
scope above). Per the Task 9 brief ("conflicts resolve in favor of
stability") each was added via `uv add <package>` — i.e. resolved against
this project's *existing* pinned stack (`mcp>=1.28.1`, `pydantic>=2.13.4`,
`portalocker>=3.2.0`) rather than against NECoRT's loose floors — and the
resulting resolved version was written back as this project's explicit
lower-bound pin, matching the pin style already used for the other core
deps in `pyproject.toml`:

| Package    | Upstream (PR #7) floor | Our pin        | Note |
|------------|-------------------------|----------------|------|
| `requests` | `>=2.28.0`               | `>=2.34.2`     | No conflict; our floor is simply newer/more current than upstream's. |
| `numpy`    | `>=1.20.0`               | `>=2.4.6`      | No conflict; upstream never advertised an upper bound, so resolving to a current NumPy 2.x is compatible with the loose floor. Nothing in the two vendored modules uses NumPy 1.x-only API — only `np.zeros`, elementwise array ops, and `float()`/`np.argmax`-style reductions in `nash_recursive_thinking.py`, all stable across the 1.x → 2.x transition. |
| `openai`   | unpinned (no floor at all) | `>=2.46.0` | Added purely to satisfy the dead `import openai` statement (see above). No conflict possible since upstream declared no constraint. |

**Conflict-resolution outcome**: no actual version conflicts arose between
NECoRT's floors and this project's existing dependency stack (`mcp`,
`pydantic`, `portalocker`) — `uv add` resolved all three new packages
without needing to override or downgrade anything already pinned. The
"favor stability" rule from the brief was therefore applied at the
selection level (pin what `uv` resolved against our real, already-pinned
stack, not upstream's loosest-possible floor) rather than needing to
adjudicate an actual clash.

`httpx` (already an existing `autopilot`-extra-only dependency per Global
Constraints) ends up present in the resolved environment regardless, as a
transitive dependency of `mcp` itself — unrelated to this task's changes,
noted here only to avoid confusion when reading `uv.lock`.

## Where these deps live

Added as core (non-optional) `dependencies` in `pyproject.toml`, not under
`[project.optional-dependencies]`. This matches `docs/build-plan.md`'s
statement that core dependencies are "`mcp`, `pydantic`, `portalocker`,
`tomli`, plus NECoRT's transitive deps via the submodule" — subagent mode
is a first-class, always-available execution mode (Task 11), not an opt-in
extra like `autopilot`.

## What was deliberately NOT added

- `fastapi`, `uvicorn[standard]`, `websockets`, `python-dotenv` — only used
  by `necort_web.py` (the vendored web UI backend), which this project
  never imports.
- Anything from `enhanced-implementations/` or its would-be dependencies —
  out of scope per the HYBRID DECISION; those files aren't imported and (per
  recon) aren't even validly importable by name (hyphenated filenames, no
  `__init__.py`).
