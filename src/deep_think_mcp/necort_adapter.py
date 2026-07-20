"""The single boundary between deep-think-mcp and the vendored NECoRT code.

This module wraps `NashEquilibriumRecursiveChat` (the *only* working core of
PR #7 -- see the HYBRID DECISION in `docs/execution-plan.md` Task 9 and the
recon in `.superpowers/sdd/necort-recon.md`). It is the ONLY file in this
project that imports the vendored code; every bit of schema/behaviour drift
between "what PR #7 does" and "what our schema needs" is absorbed here so no
other module ever sees the vendored types or their quirks.

Nothing in `vendor/necort/` is modified. All four required shims are applied
by subclassing and module-attribute injection only:

  1. datetime crash shim. `vendor/necort/nash_recursive_thinking.py` calls
     `datetime.now()` in `think_and_respond()` / `save_nash_equilibrium_log()`
     but never imports `datetime` at module scope -- a verified 100%-repro
     `NameError` on every call. We inject the name into the vendored module's
     namespace (`nash_recursive_thinking.datetime = datetime`) once, at load,
     idempotently (see `_ensure_loaded`). (The sibling base module
     `recursive_thinking_ai` *does* import datetime; only the Nash module is
     missing it, because unqualified names resolve against the *defining*
     module's globals, not the base class's.)

  2. endpoint shim. The vendored base class hardcodes OpenRouter
     (`https://openrouter.ai/api/v1/chat/completions`) and always sends an
     OpenRouter-only `"reasoning": {...}` field. `ConfigurableNashChat`
     overrides `_call_api` to POST to a configurable OpenAI-compatible
     `base_url` with configurable `headers`, building a clean payload that
     omits `reasoning` and forces non-streaming (a single capturable JSON
     response, no hand-rolled SSE, no stdout side effects from the call
     itself). The base `__init__`'s OpenRouter `base_url`/`headers` are simply
     reassigned after `super().__init__` -- no vendored edit.

  3. async offload. The vendored core does blocking synchronous `requests`
     I/O. `NECoRTAdapter.run()` is an async entrypoint that offloads the whole
     synchronous Nash call onto a worker thread via `asyncio.to_thread`, so it
     never blocks the MCP server's event loop. `run_sync()` remains available
     for synchronous callers (e.g. T14 autopilot).

  4. stdout->stderr `print` shim (T11 hard contract). Both vendored modules
     (`nash_recursive_thinking` and its base `recursive_thinking_ai`) emit
     progress via bare `print()` calls -- many unconditional -- straight to
     stdout. This server speaks MCP JSON-RPC over stdout, so a single stray
     byte corrupts the transport. `_ensure_loaded()` injects a module-global
     name `print` into BOTH vendored modules (the same module-attribute
     technique as the datetime shim); an unqualified `print(...)` inside a
     function resolves against its *defining module's* globals before the
     builtin, so this transparently routes every vendored print to stderr
     without touching a single vendored line. It is set once at load, before
     any worker thread runs, and is idempotent (identity-guarded).

Round cap (`max_rounds`)
------------------------
The vendored `think_and_respond` asks the model how many rounds to run
(`_determine_thinking_rounds`, 1-5). `NECoRTAdapter(max_rounds=...)` (and the
per-call `run(..., max_rounds=n)` override) hard-caps that: `ConfigurableNashChat`
overrides `_determine_thinking_rounds` to return the cap directly, so US -- not
the model -- decides the round budget (T11 enforces `subagent.max_rounds`), and
the extra meta round-count API call (and its prints) is skipped entirely. This
is what lets T11 do honest single-round stepping (`max_rounds=1` per call).

sys.path handling
-----------------
The vendored repo has no package structure (no `pyproject.toml`, no
`__init__.py`, flat sibling imports: `nash_recursive_thinking` does
`from recursive_thinking_ai import ...`). It is therefore importable only by
putting `vendor/necort/` itself on `sys.path`. `_ensure_loaded()` inserts that
one directory exactly once (guarded against duplicates) and imports the two
modules; this is the only global side effect, is confined to that single
vendor path, and is done lazily (importing THIS module changes nothing) so
that the translation layer is usable with synthetic data and no submodule.

Nash ratings -> our 7 utility dimensions
----------------------------------------
The Nash core's only per-candidate quality signal is a single scalar: for
each ordered pair (rater i, response j) it asks the model to "Rate this
response from 0-10 ... Consider accuracy, relevance, clarity, and
completeness" and returns ONE number. That number is an inseparable blend --
it cannot be decomposed into orthogonal axes. We normalise a candidate's
peer ratings (the off-diagonal mean of its column, /10) into [0, 1] and map
it as follows:

  dimension        source                                  populated?
  ---------------- --------------------------------------- ----------
  correctness      Nash blended score ("accuracy")         YES *
  clarity          Nash blended score ("clarity")          YES *
  coverage         Nash blended score ("completeness")     YES *
  evidence         no Nash prompt solicits it              NO  (0.5)
  novelty          no Nash prompt solicits it              NO  (0.5) **
  bias_resistance  no Nash prompt solicits it              NO  (0.5) ***
  actionability    no Nash prompt solicits it              NO  (0.5)

  *   correctness/clarity/coverage carry the SAME value by construction: the
      three facets the rating prompt names ("accuracy", "clarity",
      "completeness") were rated as one blended number. Their agreement is an
      artefact of Nash producing a single scalar -- it is NOT three
      independent measurements that happened to coincide. ("relevance", also
      named by the prompt, has no corresponding dimension and is absorbed
      into the blend.)
  **  Nash's improvement loop actively drives candidates toward consensus, so
      it structurally cannot measure novelty even in principle.
  *** bias detection exists in PR #7 only in the disconnected, never-imported
      `enhanced-implementations/` heuristic files (recon §2d) -- nothing in
      the vendored core produces a bias signal.

Unpopulatable dimensions are set to the neutral sentinel 0.5 -- the same
"no signal" convention `serial_engine._DEFAULT_DIM` uses -- and are NEVER
given a fabricated Nash-derived value. Consumers (T11) that aggregate the 7
dims should note that even a perfect Nash score yields overall mean
(3*1.0 + 4*0.5)/7 == 0.714; the config `[subagent] equilibrium_threshold`
(0.75) is the vendored core's *matrix-diff* convergence epsilon, a different
quantity from an overall-utility gate -- do not conflate them.

Known vendored quirks preserved (not corrected here)
----------------------------------------------------
- In a `thinking_history` entry for round k>=1, `utility_matrix` was computed
  on the candidates as they stood at the START of round k, but `agent_responses`
  in that same entry are the POST-improvement candidates (the vendored code
  rates, then improves, then stores both under one entry). When the round
  converged, improvement is skipped and the two line up; otherwise the matrix
  slightly predates the stored text. We read each entry's fields as the
  vendored structure presents them (1:1), documenting rather than reshaping.
- `final_response_agent` indexes the FINAL round's `agent_responses`; that one
  (round, agent) cell is the sole `was_selected=True` SpecialistRound.
"""

from __future__ import annotations

import asyncio
import builtins
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import requests

from deep_think_mcp.session import SpecialistRound, UtilityScore

# vendor/necort lives at the repo root; this file is src/deep_think_mcp/.
_VENDOR_NECORT: Path = Path(__file__).resolve().parents[2] / "vendor" / "necort"

# Neutral "no signal" value for a dimension Nash cannot populate. Matches
# serial_engine._DEFAULT_DIM so both modes agree on what "unknown" means.
NEUTRAL_DIM: float = 0.5

# Max 0-10 rating the vendored evaluator can emit (it clamps to 10).
_MAX_RATING: float = 10.0

# The three dimensions a Nash peer rating legitimately (if blended) informs,
# and the four it cannot -- see the module docstring's mapping table.
POPULATED_DIMS: tuple[str, ...] = ("correctness", "clarity", "coverage")
UNPOPULATED_DIMS: tuple[str, ...] = (
    "evidence",
    "novelty",
    "bias_resistance",
    "actionability",
)

# Default per-request HTTP timeout (seconds) for the OpenAI-compatible call.
DEFAULT_TIMEOUT: float = 120.0

# Equilibrium-state labels stamped onto each SpecialistRound.
EQ_INITIAL = "initial"  # round 0: candidates generated, not yet rated
EQ_IN = "in_equilibrium"  # this candidate is in the round's Nash equilibrium set
EQ_OUT = "off_equilibrium"  # rated but not part of the equilibrium set

# Cache of the dynamically built ConfigurableNashChat subclass (also serves as
# the "already loaded + shimmed" flag so the datetime injection is idempotent).
_nash_chat_cls: type | None = None


def _vendored_print(*args: Any, **kwargs: Any) -> None:
    """Replacement for the builtin `print`, injected into both vendored
    modules' globals (shim #4). Forces `file=sys.stderr` so the vendored
    core's progress output never lands on stdout -- which this MCP server
    reserves for the JSON-RPC transport. All other `print` kwargs the
    vendored code passes (`end`, `flush`, ...) are honoured unchanged.

    `sys.stderr` is looked up fresh on every call (not captured at import)
    so pytest's per-test capture still sees the output.
    """
    kwargs["file"] = sys.stderr
    builtins.print(*args, **kwargs)


class NECoRTUnavailable(RuntimeError):
    """Raised when the vendored NECoRT core cannot be loaded (submodule not
    initialized, or its import deps missing). Callers (T11) should treat this
    as "endpoint/subagent NECoRT path unavailable" and fall back to the manual
    specialist path -- never surface it as a raw traceback.
    """


# ---------------------------------------------------------------------------
# Translation result
# ---------------------------------------------------------------------------


@dataclass
class NECoRTResult:
    """The adapter's stable, vendor-free output. T11 maps `specialist_rounds`
    onto a `Thought.specialist_rounds` and `final_utility_scores` onto the
    thought's `final_utility_scores`."""

    response: str
    specialist_rounds: list[SpecialistRound]
    final_utility_scores: UtilityScore
    converged: bool
    convergence_round: int | None
    thinking_rounds: int | None
    final_response_agent: int | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _score_from_value(value: float | None) -> UtilityScore:
    """Build a 7-dim `UtilityScore`. `value` (already in [0, 1]) fills the
    three populatable dims; `None` means "no rating available" so they too
    fall to neutral. The four unpopulatable dims are always neutral.
    """
    dims: dict[str, float] = {d: NEUTRAL_DIM for d in UNPOPULATED_DIMS}
    populated = NEUTRAL_DIM if value is None else _clamp01(value)
    for d in POPULATED_DIMS:
        dims[d] = populated
    return UtilityScore(**dims)


def _column_mean(matrix: Any, col: int) -> float | None:
    """Off-diagonal mean of `matrix[:, col]` (how every *other* agent rated
    candidate `col`), or None when there is no peer signal (no matrix, or a
    single agent so no off-diagonal raters, or a malformed row).
    """
    if not matrix:
        return None
    ratings: list[float] = []
    for i, row in enumerate(matrix):
        if i == col:  # skip self-evaluation (vendored keeps its diagonal at 0)
            continue
        if row is None or col >= len(row):
            continue
        try:
            ratings.append(float(row[col]))
        except (TypeError, ValueError):
            continue
    if not ratings:
        return None
    return sum(ratings) / len(ratings)


def _utility_for_candidate(matrix: Any, col: int) -> UtilityScore:
    mean = _column_mean(matrix, col)
    if mean is None:
        return _score_from_value(None)
    return _score_from_value(mean / _MAX_RATING)


def _equilibrium_state(matrix: Any, eq_indices: set[int], col: int) -> str:
    if not matrix:
        return EQ_INITIAL
    return EQ_IN if col in eq_indices else EQ_OUT


def _role_for(agent_roles: Sequence[str] | None, index: int) -> str:
    """Positional role name. Configured roles are used where they exist; if
    there are more agents than named roles, extra agents get `agent_{n}`.
    """
    if agent_roles and index < len(agent_roles):
        return str(agent_roles[index])
    return f"agent_{index + 1}"


def _as_int(value: Any) -> int | None:
    """Coerce a possibly-numpy index to a plain int (None-safe)."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Translation: vendored Nash result -> our schema
# ---------------------------------------------------------------------------


def translate(
    nash_result: dict[str, Any], agent_roles: Sequence[str] | None = None
) -> NECoRTResult:
    """Translate a `NashEquilibriumRecursiveChat.think_and_respond()` return
    value into a vendor-free `NECoRTResult`. Pure and deterministic -- no
    vendored code required, so this is directly testable with synthetic
    Nash-shaped dicts.
    """
    history: list[dict[str, Any]] = list(nash_result.get("thinking_history") or [])
    final_agent = _as_int(nash_result.get("final_response_agent"))
    last_round_index: int | None = history[-1].get("round") if history else None

    rounds: list[SpecialistRound] = []
    for entry in history:
        round_index = int(entry.get("round", 0))
        responses: list[Any] = list(entry.get("agent_responses") or [])
        matrix = entry.get("utility_matrix")
        eq_indices = {i for i in (_as_int(x) for x in (entry.get("equilibrium_indices") or [])) if i is not None}

        for col, content in enumerate(responses):
            was_selected = (
                last_round_index is not None
                and round_index == last_round_index
                and final_agent is not None
                and col == final_agent
            )
            rounds.append(
                SpecialistRound(
                    round_index=round_index,
                    agent_role=_role_for(agent_roles, col),
                    candidate_content=str(content),
                    utility_vector=_utility_for_candidate(matrix, col),
                    equilibrium_state=_equilibrium_state(matrix, eq_indices, col),
                    was_selected=was_selected,
                )
            )

    # Final utility = the winning candidate's column mean in the final round.
    final_scores = _score_from_value(None)
    if history and final_agent is not None:
        final_scores = _utility_for_candidate(history[-1].get("utility_matrix"), final_agent)

    return NECoRTResult(
        response=str(nash_result.get("response", "")),
        specialist_rounds=rounds,
        final_utility_scores=final_scores,
        converged=bool(nash_result.get("converged", False)),
        convergence_round=_as_int(nash_result.get("convergence_round")),
        thinking_rounds=_as_int(nash_result.get("thinking_rounds")),
        final_response_agent=final_agent,
        raw=nash_result,
    )


# ---------------------------------------------------------------------------
# Vendored loading + the configurable subclass (shims #1 and #2)
# ---------------------------------------------------------------------------


def is_vendored_available() -> bool:
    """True if the `vendor/necort` submodule is populated. Pure filesystem
    check -- no import, no `sys.path` mutation -- so tests can skip gracefully
    without side effects.
    """
    return (_VENDOR_NECORT / "recursive_thinking_ai.py").is_file() and (
        _VENDOR_NECORT / "nash_recursive_thinking.py"
    ).is_file()


def _ensure_loaded() -> type:
    """Idempotently load the vendored core, apply the datetime shim, and
    return the `ConfigurableNashChat` subclass. Raises `NECoRTUnavailable`
    if the submodule/deps are missing.
    """
    global _nash_chat_cls
    if _nash_chat_cls is not None:
        return _nash_chat_cls

    if not is_vendored_available():
        raise NECoRTUnavailable(
            f"vendor/necort not initialized at {_VENDOR_NECORT} "
            "-- run `git submodule update --init`"
        )

    vendor_str = str(_VENDOR_NECORT)
    if vendor_str not in sys.path:
        sys.path.insert(0, vendor_str)

    try:
        import recursive_thinking_ai as _rta
        import nash_recursive_thinking as _nrt
    except ImportError as exc:  # pragma: no cover - exercised only w/o deps
        raise NECoRTUnavailable(f"could not import vendored NECoRT: {exc}") from exc

    # Shim #1: inject the missing top-level import (idempotent).
    from datetime import datetime as _datetime

    if getattr(_nrt, "datetime", None) is not _datetime:
        _nrt.datetime = _datetime

    # Shim #4: route both vendored modules' bare print()s to stderr, protecting
    # the MCP stdout transport. Module-global `print` shadows the builtin for
    # all code defined in that module. Set once at load (before any thread),
    # identity-guarded so it is idempotent.
    for _mod in (_rta, _nrt):
        if getattr(_mod, "print", None) is not _vendored_print:
            _mod.print = _vendored_print

    base = _nrt.NashEquilibriumRecursiveChat

    class ConfigurableNashChat(base):  # type: ignore[valid-type, misc]
        """`NashEquilibriumRecursiveChat` with a configurable, OpenAI-compatible
        `_call_api` (shim #2). No vendored file is touched: the OpenRouter
        `base_url`/`headers` set by the base `__init__` are reassigned here.
        """

        def __init__(
            self,
            *,
            base_url: str,
            model: str,
            api_key: str | None = None,
            headers: dict[str, str] | None = None,
            num_agents: int = 3,
            convergence_threshold: float = 0.05,
            timeout: float = DEFAULT_TIMEOUT,
            max_rounds: int | None = None,
        ) -> None:
            super().__init__(
                api_key=api_key,
                model=model,
                num_agents=num_agents,
                convergence_threshold=convergence_threshold,
            )
            # Replace the base class's hardcoded OpenRouter endpoint/headers.
            self.base_url = base_url
            self._timeout = timeout
            self._max_rounds = max_rounds
            if headers is not None:
                self.headers = dict(headers)
            else:
                resolved = {"Content-Type": "application/json"}
                if api_key:
                    resolved["Authorization"] = f"Bearer {api_key}"
                self.headers = resolved

        def _determine_thinking_rounds(self, prompt: str) -> int:
            """US round-cap override (see module docstring). When `max_rounds`
            is set, return it directly -- US, not the model, owns the round
            budget -- which also skips the base class's extra meta round-count
            API call (and its prints). Falls back to the vendored behaviour
            when uncapped.
            """
            if self._max_rounds is not None:
                return max(1, int(self._max_rounds))
            return super()._determine_thinking_rounds(prompt)

        def _call_api(
            self, messages: list, temperature: float = 0.7, stream: bool = True
        ) -> str:
            """OpenAI-compatible chat completion. Builds a clean payload that
            OMITS the OpenRouter-only `reasoning` field and forces
            non-streaming (`stream` is accepted for signature parity with the
            vendored base but never honoured -- we always take the single-JSON
            path for a clean, capturable response with no stdout side effects).
            HTTP/JSON errors propagate so the caller can surface a directive
            payload rather than a silent "Error:" candidate string.
            """
            payload = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "stream": False,
            }
            response = requests.post(
                self.base_url, headers=self.headers, json=payload, timeout=self._timeout
            )
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"].strip()

    _nash_chat_cls = ConfigurableNashChat
    return _nash_chat_cls


# ---------------------------------------------------------------------------
# The adapter (shim #3 + translation)
# ---------------------------------------------------------------------------


class NECoRTAdapter:
    """Stable interface over the vendored Nash core.

    Construction fails fast with `NECoRTUnavailable` if the vendored code
    can't be loaded (this is the vendored boundary; T11 only builds an adapter
    when an endpoint is configured, and handles the no-endpoint/no-vendored
    case with its own directive payload).

    Each `run`/`run_sync` builds a FRESH chat instance, so the vendored
    `conversation_history`/`full_thinking_log` never accumulate across calls
    and concurrent runs share no mutable vendored state (sidestepping recon
    red-flag #6). The vendored `think_and_respond`'s progress `print()`s are
    routed to stderr by shim #4 (applied at load in `_ensure_loaded`), so
    driving this adapter never writes to stdout and the MCP JSON-RPC transport
    stays clean.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        headers: dict[str, str] | None = None,
        num_agents: int = 3,
        convergence_threshold: float = 0.05,
        agent_roles: Sequence[str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_rounds: int | None = None,
        verbose: bool = False,
    ) -> None:
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.headers = headers
        self.num_agents = num_agents
        self.convergence_threshold = convergence_threshold
        self.agent_roles = list(agent_roles) if agent_roles else None
        self.timeout = timeout
        self.max_rounds = max_rounds
        self.verbose = verbose
        _ensure_loaded()  # fail fast + apply the datetime/print shims

    def _new_chat(self, max_rounds: int | None = None):
        cls = _ensure_loaded()
        return cls(
            base_url=self.base_url,
            model=self.model,
            api_key=self.api_key,
            headers=self.headers,
            num_agents=self.num_agents,
            convergence_threshold=self.convergence_threshold,
            timeout=self.timeout,
            max_rounds=max_rounds if max_rounds is not None else self.max_rounds,
        )

    def run_sync(self, user_input: str, max_rounds: int | None = None) -> NECoRTResult:
        """Run one full Nash think-and-respond synchronously and translate the
        result. Blocking; use `run()` from async code. `max_rounds` overrides
        the adapter default for this call (T11 passes 1 for single-round
        stepping); `None` uses the adapter's configured cap (or uncapped).
        """
        chat = self._new_chat(max_rounds=max_rounds)
        raw = chat.think_and_respond(user_input, verbose=self.verbose)
        return translate(raw, self.agent_roles)

    async def run(self, user_input: str, max_rounds: int | None = None) -> NECoRTResult:
        """Async entrypoint (shim #3): offloads the blocking synchronous Nash
        call onto a worker thread so the event loop is never blocked.
        """
        return await asyncio.to_thread(self.run_sync, user_input, max_rounds)
