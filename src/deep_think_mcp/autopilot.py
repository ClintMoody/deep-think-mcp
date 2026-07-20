"""Layer 7 -- optional autopilot (M6). The SERVER drives a whole stage
internally against a configured local model, instead of the calling model
stepping through the loops. Off by default; feature-flagged at server creation.

`docs/build-plan.md` § "Optional autopilot" / § "Architecture at a glance"
(Layer 7). Two tools appear only when `[autopilot].enabled=true`:

  - `run_stage_autopilot`  -> drives the SERIAL loop internally (this module's
    `run_stage`): draft (from `initial_content` or an LLM draft), then
    critique -> submit -> refine -> score rounds, honoring ALL of
    `serial_engine`'s convergence rules, committing via the SAME engine
    functions the manual tools call. The critique/refine/score generations go
    to the configured OpenAI-compatible `[autopilot].endpoint`.
  - `run_subagent_autopilot` -> drives the subagent path (`run_subagent_necort`
    / `run_subagent_manual`): with `engine="necort"` it loops the vendored Nash
    core (via `subagent_engine`, which already talks to its own endpoint);
    with `engine="manual"` the autopilot plays each specialist itself against
    the endpoint, generating per-specialist candidates + 7-dim scores and
    feeding them through the SAME `manual_engine` functions.

Design contracts (from the task brief):

  - httpx is an OPTIONAL extra (`autopilot`). It is imported LAZILY, inside
    `_import_httpx()` -- NEVER at module scope -- so importing THIS module is
    clean without httpx installed. If autopilot is enabled but httpx is
    missing, the tool returns a clear directive payload (`_import_httpx` raises
    `AutopilotHttpxMissing`, which `server.py` maps to `prompts.autopilot_
    unavailable`), never a traceback.
  - Endpoint calls run OFF the event loop: `ChatClient.complete()` offloads the
    synchronous httpx POST onto a worker thread via `asyncio.to_thread` -- the
    same precedent `necort_adapter.NECoRTAdapter.run()` set for its blocking
    `requests` I/O. (necort autopilot inherits that adapter's offload directly.)
  - Every committed thought persists exactly as the manual path does: the
    drivers NEVER touch store.py/index.py themselves -- they call a `persist`
    callback (server.py's load/mutate/persist closure) after every engine
    mutation. So if autopilot stops mid-stage, everything committed so far is
    already on disk and the returned directive says where it stopped
    (resumable manually via `next_action`).
  - The LLM's structured scores parse through the EXISTING tolerant parser
    (`tolerant.parse_scores`) -- reused, not duplicated. Unparseable output
    after `_MAX_PARSE_RETRIES` retries -> the driver returns a
    partial-progress `AutopilotOutcome` (status="stopped"), never an infinite
    loop, never a raw error.

Engine sequencing/adapter errors (`SerialSequencingError`,
`SubagentSequencingError`, `SubagentAdapterError`) are NOT swallowed here --
they propagate to the two `server.py` tools, which map them to the SAME
directive wording (`serial_directive` / `subagent_directive` /
`subagent_adapter_error`) the manual tools already use. Only the two failures
unique to autopilot's own LLM client -- an endpoint fault and unparseable
scores -- become an `AutopilotOutcome(status="stopped")` partial directive.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from deep_think_mcp import manual_engine, meta, prompts, serial_engine, subagent_engine, tolerant

# Bounded retry budget for parsing the LLM's structured (score) output before
# giving up with a partial-progress directive. Config-free constant, per the
# brief: 2 retries after the initial attempt == 3 attempts total.
_MAX_PARSE_RETRIES: int = 2

# Default per-request HTTP timeout (seconds) if config names none.
_DEFAULT_TIMEOUT: float = 120.0


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AutopilotHttpxMissing(RuntimeError):
    """The optional `httpx` dependency (the `autopilot` extra) is not
    installed. Raised by `_import_httpx`; `server.py` maps it to the
    `prompts.autopilot_unavailable` directive -- never a raw ImportError."""


class AutopilotEndpointError(RuntimeError):
    """The autopilot LLM endpoint call failed (connection refused, non-2xx
    status, or a malformed 200 body). The driver converts this into a
    partial-progress `AutopilotOutcome` -- a directive, never a traceback."""


def _import_httpx() -> Any:
    """Import `httpx` lazily. The ONLY place `httpx` is imported in this module,
    so importing the module itself never requires the optional dependency.
    Raises `AutopilotHttpxMissing` (never a bare ImportError) when absent.
    """
    try:
        import httpx  # noqa: PLC0415 -- intentional lazy import (optional extra)
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise AutopilotHttpxMissing(
            "autopilot needs the optional 'httpx' dependency -- install the "
            "'autopilot' extra"
        ) from exc
    return httpx


# ---------------------------------------------------------------------------
# OpenAI-compatible chat client
# ---------------------------------------------------------------------------


@dataclass
class ChatClient:
    """Minimal OpenAI-compatible chat-completions client. `endpoint` is the
    base URL (e.g. `http://localhost:11434/v1`); `/chat/completions` is
    appended. Non-streaming, single-JSON response -- matching the necort
    adapter's `_call_api` shape."""

    endpoint: str
    model: str
    temperature: float = 0.7
    api_key: str | None = None
    timeout: float = _DEFAULT_TIMEOUT

    def _url(self) -> str:
        return self.endpoint.rstrip("/") + "/chat/completions"

    def _complete_sync(self, messages: list[dict[str, str]]) -> str:
        httpx = _import_httpx()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "stream": False,
        }
        try:
            response = httpx.post(self._url(), json=payload, headers=headers, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
            return str(data["choices"][0]["message"]["content"]).strip()
        except AutopilotHttpxMissing:
            raise
        except Exception as exc:  # noqa: BLE001 - network/status/body -> directive
            raise AutopilotEndpointError(f"{type(exc).__name__}: {exc}") from exc

    async def complete(self, messages: list[dict[str, str]]) -> str:
        """Run one chat completion off the event loop (worker thread)."""
        return await asyncio.to_thread(self._complete_sync, messages)


def client_from_cfg(cfg: dict[str, Any]) -> ChatClient:
    """Build a `ChatClient` from `[autopilot]` config. Calls `_import_httpx`
    eagerly so a missing dependency fails FAST (before any work) with
    `AutopilotHttpxMissing`, which the tool turns into a clean directive."""
    _import_httpx()  # fail fast if the optional extra is absent
    ap = cfg.get("autopilot", {})
    api_key = str(ap.get("api_key", "") or "") or None
    return ChatClient(
        endpoint=str(ap.get("endpoint", "") or ""),
        model=str(ap.get("model", "") or ""),
        temperature=float(ap.get("temperature", 0.7)),
        api_key=api_key,
        timeout=float(ap.get("timeout", _DEFAULT_TIMEOUT)),
    )


# ---------------------------------------------------------------------------
# Outcome record (server.py maps this to prompts.py wording)
# ---------------------------------------------------------------------------


@dataclass
class AutopilotOutcome:
    """The stable, wording-free result of an autopilot run. `status` is
    "committed" (the driver drove the loop to a committed thought) or "stopped"
    (an endpoint fault / unparseable scores halted it partway -- everything
    completed so far is already persisted). server.py maps the two to
    `*_autopilot_committed` / `autopilot_stopped` respectively."""

    kind: str  # "serial" | "subagent"
    status: str  # "committed" | "stopped"
    stage: str
    engine: str | None = None  # subagent: "necort" | "manual"
    thought_id: str | None = None
    rounds: int = 0
    final_content: str | None = None
    completed_steps: list[str] = field(default_factory=list)
    committed_thought_ids: list[str] = field(default_factory=list)
    # stopped-only
    stopped_phase: str | None = None
    stopped_detail: str = ""
    # serial-only
    converged_reason: str | None = None
    # subagent-only
    strength: float | None = None
    threshold: float | None = None
    metric_label: str | None = None
    converged: bool = False
    budget_exhausted: bool = False

    def _stop(self, phase: str, detail: str) -> "AutopilotOutcome":
        self.status = "stopped"
        self.stopped_phase = phase
        self.stopped_detail = detail
        return self


# ---------------------------------------------------------------------------
# LLM generation helpers
# ---------------------------------------------------------------------------


async def _generate_text(client: ChatClient, messages: list[dict[str, str]]) -> str:
    return (await client.complete(messages)).strip()


async def _generate_scores(
    client: ChatClient, messages: list[dict[str, str]]
) -> dict[str, Any] | None:
    """Ask the LLM for a 7-dim score vector and parse it through the EXISTING
    `tolerant.parse_scores`. Retries a bounded number of times on unparseable
    output; returns None (never raises, never loops forever) once the budget is
    spent so the caller can stop with a partial directive. Endpoint faults
    (`AutopilotEndpointError`) propagate to the driver's phase handler."""
    for _ in range(_MAX_PARSE_RETRIES + 1):
        raw = await client.complete(messages)
        try:
            return tolerant.parse_scores(raw, param="scores")
        except tolerant.TolerantParseError:
            continue
    return None


# ---------------------------------------------------------------------------
# Serial driver
# ---------------------------------------------------------------------------


async def run_stage(
    session: Any,
    cfg: dict[str, Any],
    client: ChatClient,
    available_lenses: dict[str, str],
    persist: Any,
    *,
    initial_content: str | None = None,
) -> AutopilotOutcome:
    """Drive the serial critique loop for the current stage internally: draft,
    then critique -> submit -> refine -> score rounds until `serial_engine`'s
    convergence rules fire, then commit. `persist(session)` is called after
    every engine mutation. `SerialSequencingError` propagates (server maps it);
    an endpoint fault or unparseable scores -> a stopped outcome.
    """
    out = AutopilotOutcome(kind="serial", status="committed", stage=session.current_stage)

    # --- draft ---
    if initial_content and initial_content.strip():
        draft = initial_content.strip()
    else:
        try:
            draft = await _generate_text(
                client,
                prompts.autopilot_draft_messages(
                    session.question, session.current_stage, meta.compress_history(session).digest
                ),
            )
        except AutopilotEndpointError as exc:
            return out._stop("draft", str(exc))
        if not draft:
            return out._stop("draft", "the model returned an empty draft")
    serial_engine.begin_thought(session, draft)  # SerialSequencingError -> server
    persist(session)
    out.completed_steps.append("draft")

    # --- rounds ---
    while True:
        cp = serial_engine.start_critique(session, None, available_lenses, cfg)
        persist(session)
        try:
            critique = await _generate_text(
                client,
                prompts.autopilot_critique_messages(
                    session.question, session.current_stage, cp.draft_content, cp.lens, cp.lens_template
                ),
            )
        except AutopilotEndpointError as exc:
            return out._stop("critique", str(exc))
        if not critique:
            return out._stop("critique", f"empty critique for lens '{cp.lens}'")
        serial_engine.submit_critique(session, critique)
        persist(session)
        out.completed_steps.append(f"critiqued (lens {cp.lens})")

        try:
            refined = await _generate_text(
                client,
                prompts.autopilot_refine_messages(
                    session.question, session.current_stage, cp.draft_content, critique
                ),
            )
        except AutopilotEndpointError as exc:
            return out._stop("refine", str(exc))
        if not refined:
            return out._stop("refine", "the model returned an empty refinement")
        serial_engine.refine_current_thought(session, refined)
        persist(session)
        out.completed_steps.append("refined")

        try:
            scores = await _generate_scores(client, prompts.autopilot_score_messages(refined))
        except AutopilotEndpointError as exc:
            return out._stop("score", str(exc))
        if scores is None:
            return out._stop(
                "score", f"could not parse scores after {_MAX_PARSE_RETRIES + 1} attempts"
            )
        result = serial_engine.score_current_thought(session, scores, cfg)
        persist(session)
        out.rounds = result.round_index + 1
        out.completed_steps.append(f"scored round {result.round_index}")
        if result.converged:
            out.converged_reason = result.converged_reason
            break

    thought = serial_engine.commit_thought(session)
    persist(session)
    out.thought_id = thought.id
    out.committed_thought_ids.append(thought.id)
    out.final_content = thought.content
    return out


# ---------------------------------------------------------------------------
# Subagent drivers
# ---------------------------------------------------------------------------


async def run_subagent_necort(
    session: Any,
    cfg: dict[str, Any],
    persist: Any,
    *,
    initial_content: str | None = None,
) -> AutopilotOutcome:
    """Drive the NECoRT subagent loop internally: `subagent_engine.begin`, then
    `advance` until the equilibrium converges or the round budget is spent, then
    `commit`. The Nash core talks to the `[subagent]` endpoint via the adapter
    (which already offloads its blocking I/O). `SubagentSequencingError`
    (e.g. `no_endpoint`) and `SubagentAdapterError` propagate to the server.
    """
    out = AutopilotOutcome(
        kind="subagent", status="committed", stage=session.current_stage, engine="necort",
        threshold=float(cfg["subagent"]["equilibrium_threshold"]),
    )
    result = await subagent_engine.begin(session, initial_content, None, cfg)
    persist(session)
    out.completed_steps.append(f"round {result.rounds_run}")
    while not (result.converged or result.budget_exhausted):
        result = await subagent_engine.advance(session, cfg)
        persist(session)
        out.completed_steps.append(f"round {result.rounds_run}")

    thought = subagent_engine.commit(session)
    persist(session)
    _fill_subagent_commit(out, result, thought)
    return out


async def run_subagent_manual(
    session: Any,
    cfg: dict[str, Any],
    client: ChatClient,
    persist: Any,
    *,
    initial_content: str | None = None,
) -> AutopilotOutcome:
    """Drive the manual (endpoint-free-for-the-caller) subagent path with the
    autopilot playing each specialist itself: `manual_engine.begin` hands the
    first specialist's prompt; for each handed prompt the autopilot generates a
    candidate + a 7-dim self-score via the endpoint and feeds them back through
    `manual_engine.advance`; when a round closes below threshold with budget
    left, a bare `advance` starts the next round. `commit` (shared with
    `subagent_engine`) locks the winner. `SubagentSequencingError` /
    `SubagentAdapterError` propagate to the server.
    """
    out = AutopilotOutcome(
        kind="subagent", status="committed", stage=session.current_stage, engine="manual",
        threshold=float(cfg["subagent"]["equilibrium_threshold"]),
    )
    step = manual_engine.begin(session, initial_content, None, cfg)
    persist(session)

    while True:
        if isinstance(step, manual_engine.ManualPrompt):
            name = step.specialist_name
            try:
                candidate = await _generate_text(
                    client, prompts.autopilot_specialist_messages(step.prompt_text)
                )
            except AutopilotEndpointError as exc:
                return out._stop("specialist", str(exc))
            if not candidate:
                return out._stop("specialist", f"empty candidate for specialist '{name}'")
            try:
                scores = await _generate_scores(client, prompts.autopilot_score_messages(candidate))
            except AutopilotEndpointError as exc:
                return out._stop("score", str(exc))
            if scores is None:
                return out._stop("score", f"could not parse scores for specialist '{name}'")
            step = manual_engine.advance(session, candidate, scores, cfg)
            persist(session)
            out.completed_steps.append(f"specialist '{name}' answered")
        else:  # SubagentRoundResult -- a round just closed
            out.rounds = step.rounds_run
            out.completed_steps.append(f"round {step.rounds_run} closed")
            if step.converged or step.budget_exhausted:
                break
            step = manual_engine.advance(session, None, {}, cfg)  # start next round
            persist(session)

    thought = subagent_engine.commit(session)
    persist(session)
    _fill_subagent_commit(out, step, thought)
    return out


def _fill_subagent_commit(out: AutopilotOutcome, result: Any, thought: Any) -> None:
    """Copy a closed subagent round verdict + the committed thought onto the
    outcome (shared by both subagent drivers)."""
    out.rounds = result.rounds_run
    out.strength = result.strength
    out.metric_label = result.metric_label
    out.converged = result.converged
    out.budget_exhausted = result.budget_exhausted
    out.thought_id = thought.id
    out.committed_thought_ids.append(thought.id)
    out.final_content = thought.content
