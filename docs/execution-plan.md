# deep-think-mcp — Execution Plan (v1 build)

Derived strictly from `docs/build-plan.md` (canonical design doc, copied from the
Obsidian vault). Tasks below transcribe that plan into dispatchable units — no
redesign. Where the build plan gives an example ("e.g."), this plan fixes a
concrete value and marks it **[derived]**; everything else is plan-mandated.

Milestone map: T1–T4 = M1 · T5–T8 = M2 · T9–T11 = M3 · T12 = M4 · T13 = M5 ·
T14 = M6 · T15 = M7-lite (benchmarks deferred — they require blind human rating).

## Global Constraints

These bind every task. Copy verbatim into reviewer dispatches.

- Single MCP server named `deep-think-mcp`. Python, `src/` layout exactly per
  `docs/build-plan.md` § Project layout. Core deps: `mcp`, `pydantic`,
  `portalocker`. TOML parsing via stdlib `tomllib` (we require Python >=3.11,
  so the plan's `tomli` dep is unnecessary — documented adaptation). `httpx`
  only as an optional `autopilot` extra. Package manager: `uv`.
- Two execution modes: `serial` and `subagent`, chosen per session.
  `start_session` without a mode returns a mode-required payload (available
  modes + one-line descriptions + exact next tool `set_session_mode`); the
  server refuses thought tools until a mode is set. Once set, mode is
  **immutable** for the session.
- Persistent by default. Data root: `~/deep-think-mcp/` containing
  `sessions/` (one JSON per session), `index.json`, `config.toml`, `logs/`.
  Root resolution order: `DEEP_THINK_HOME` env var → `[store].root` in config →
  `~/deep-think-mcp` **[derived]**. Tests must NEVER touch the real home
  directory — always inject a tmp root.
- On every mutation: write a `.bak` sibling, then the new file, then remove the
  `.bak`. Concurrent access guarded with Portalocker.
- Default stages: `Problem Definition, Research, Analysis, Synthesis,
  Conclusion` — customizable per session via `start_session(stages=[...])`.
- Utility score schema shared across modes — exactly 7 dimensions:
  `Correctness, Evidence, Novelty, Clarity, Bias-resistance, Actionability,
  Coverage`.
- Config defaults: `[serial] max_rounds=3, score_threshold=0.05`;
  `[subagent] max_rounds=2, equilibrium_threshold=0.75, sequential_fallback=true`;
  `[modes] default_prompt_user=true` (server always asks even if a default is
  configured). All settings per-session-overridable via `start_session` args.
- NECoRT vendored as a git submodule at `vendor/necort/` pinned to
  `f4d290ceb086d47bb0f872164344836c47134452` (head of
  PhialsBasement/Chain-of-Recursive-Thoughts PR #7).
- The server never touches the network unless `autopilot.enabled=true`.
- Tolerant input handling (from M5 on): every tool accepts JSON or plaintext;
  malformed input returns a `retry_with_clarification` template, never a raw
  error.
- Tool responses are short, directive, and template-driven (`prompts.py`) —
  built for weak local models. `next_action()` is authoritative.
- TDD for all engine/store/lifecycle logic. Test command: `uv run pytest`.
  Test output must be pristine.

## Task 1: Project scaffold + config loading

**Goal:** uv-managed Python project skeleton with layered config.

- `pyproject.toml`: name `deep-think-mcp`, `requires-python = ">=3.11"`, deps
  `mcp`, `pydantic`, `portalocker`; dev group `pytest`, `pytest-asyncio`;
  optional extra `autopilot = ["httpx"]`.
- Create `src/deep_think_mcp/__init__.py` and `config.py` **[derived: config
  loader module, not in plan's file list but required by its config surface]**.
  Do NOT stub the other modules yet — later tasks create them.
- `config/default.toml` with every section and default from
  `docs/build-plan.md` § Configuration surface: `[store]` (root, sessions_dir,
  index_path), `[modes]`, `[serial]` (incl. `fast_mode=false`,
  `default_lenses`), `[subagent]` (incl. `agents=[...]` — default list matches
  PR #7; use placeholder `["Analysis", "Creativity"]` until T9 recon confirms,
  marked with a TODO comment), `[stages]`, `[autopilot]` (enabled=false,
  endpoint, model, temperature).
- `config.py`: load packaged defaults ← overlay user config
  (`<root>/config.toml` if present) ← overlay per-session overrides dict.
  Root resolution per Global Constraints. `bootstrap()` ensures
  `<root>/sessions/` and `<root>/logs/` exist and writes `config.toml` from
  defaults if missing; idempotent.
- Tests (TDD): layering precedence, `DEEP_THINK_HOME` override, bootstrap
  idempotence, nothing written outside the injected tmp root.

## Task 2: Data model + session store + index

**Goal:** the persistence layer M1 requires before any engines exist.

- `session.py` — Pydantic models exactly per `docs/build-plan.md` § Data model:
  - `Session`: `id` (uuid4 hex **[derived]**), `question`, `created_at`,
    `mode: Literal["serial","subagent"] | None` (None = awaiting mode),
    `expected_stages: list[str]`, `current_stage`, `current_thought_id`,
    `status: Literal["active","finalized","archived"]`, `save_path`,
    `move_history: list` (entries record from-path, to-path, timestamp
    **[derived]**), plus `thoughts: list[Thought]` and a `decisions` audit list
    (for keep_here records) **[derived]**.
  - `Thought`: `id`, `stage`, `position`, `timestamp`, `content`, `tags[]`,
    `axioms[]`, `challenged_assumptions[]`, `critique_rounds[]` (serial) OR
    `specialist_rounds[]` (subagent) — mode-tagged, `final_utility_scores{}`,
    `committed: bool`.
  - `CritiqueRound`: `round_index, lens, critique_text, refined_content,
    delta_score`.
  - `SpecialistRound`: `round_index, agent_role, candidate_content,
    utility_vector, equilibrium_state, was_selected`.
  - `UtilityScore`: the 7 dimensions from Global Constraints, floats in [0,1]
    **[derived range]**.
- `store.py` — JSON-per-session persistence: Portalocker file locks; the
  `.bak` mutation protocol from Global Constraints; on load, if the main file
  is corrupt and a `.bak` exists, recover from the `.bak` **[derived]**.
- `index.py` — `<root>/index.json`: id → {path, mode, status, created_at,
  updated_at}; locked read-modify-write; survives sessions living at arbitrary
  absolute paths outside the root (post-move).
- Tests (TDD): model roundtrips, store roundtrip, `.bak` recovery, index
  integrity, and the plan's load test — rapid concurrent writes to one session
  under Portalocker contention (threads or processes).

## Task 3: MCP server skeleton + session lifecycle tools

**Goal:** a bootable MCP server enforcing the mode-selection contract.

- `server.py`: stdio MCP server via the official `mcp` Python SDK (FastMCP).
  Registers session-lifecycle tools; engine tools arrive in later tasks.
- `prompts.py` started: response templates live here, not inline.
- Tools:
  - `start_session(question, mode?, stages?, overrides?)` — creates + persists
    a session, bootstraps the store on first use. Without `mode`: returns the
    mode-required payload (modes + one-line descriptions the model can read to
    the user verbatim; `next_tool: "set_session_mode"`; the new `session_id`).
    With a valid mode: proceeds immediately.
  - `set_session_mode(session_id, mode)` — sets mode only if unset; rejects
    changes once set (immutability).
  - `list_modes()` — descriptions + recommendations (serial: single-GPU/small
    models/transparency; subagent: harder questions, more compute — wording
    per `docs/build-plan.md` § The two execution modes).
  - `resume_session(session_id)`, `list_sessions()`, `clear_session(session_id)`.
- Any thought tool called while `mode is None` returns a directive payload
  pointing at `set_session_mode` (enforced centrally in the dispatcher —
  Layer 2 of the architecture).
- Tests: MCP contract round-trips against the real SDK (in-memory client):
  mode-required flow, immutability rejection, resume/list/clear.

## Task 4: Finalize / move / keep lifecycle

**Goal:** M1 mandates the persistence+move UX works end-to-end BEFORE any
thinking loops exist.

- `lifecycle.py` + tools:
  - `finalize_session(session_id)` — sets status=finalized; returns payload:
    `current_path`, `human_prompt` (canned text per `docs/build-plan.md`
    § Finalize + move flow: *"Your reasoning is saved at `<path>`. Would you
    like to move it elsewhere (a project folder, your Documents, etc.), or
    leave it where it is?"*), `available_tools` pointing at `move_session` and
    `keep_here`.
  - `move_session(session_id, new_path, force?)` — validate destination
    (writable directory, no clobber unless `force=true`); move atomically and
    cross-filesystem-safely (write to destination, verify, unlink original);
    append to `move_history`; update index; return confirmation with new
    absolute path.
  - `keep_here(session_id)` — records "user declined to move" in the session's
    audit trail; no filesystem change.
- Sessions moved outside the root stay fully functional: `list_sessions` and
  `resume_session` find them via the index's absolute paths.
- Tests (TDD): finalize→move→resume works; finalize→keep_here→file stays put
  and stays indexed; destination-exists without force fails cleanly; with
  force succeeds; unwritable destination fails cleanly; simulated
  cross-filesystem move (copy+verify+unlink path, not `rename`).

## Task 5: Stage machine

**Goal:** shared Layer 3 both engines sit on.

- `stages.py`: default stage list from Global Constraints; per-session custom
  stages honored from `start_session`; `advance_stage()` tool moves the
  cursor; each stage holds multiple committed thoughts; cannot advance past
  the final stage (directive payload suggests `finalize_session`) **[derived]**.
- Stage-appropriate defaults as data tables consumed by later tasks:
  - Serial lens defaults: Analysis → `[weak_evidence, overconfidence]`,
    Synthesis → `[missing_perspective, unstated_assumption]` (both
    plan-mandated); Problem Definition → `[unstated_assumption, scope_creep]`,
    Research → `[weak_evidence, missing_perspective]`, Conclusion →
    `[steel_man, overconfidence]` **[derived]**.
  - Subagent stage weighting: Creativity weighted higher in Synthesis,
    Analysis weighted higher in Analysis (plan-mandated examples); neutral
    weights elsewhere **[derived]**.
- Tests: default + custom progression, cursor integrity across persistence,
  end-of-stages behavior.

## Task 6: Critique lens library

**Goal:** the 8 bundled lenses + drop-in discovery.

- `lenses/` — 8 `.md` files: `overconfidence, weak_evidence,
  missing_perspective, unstated_assumption, scope_creep, alternative_framing,
  steel_man, first_principles`. Each is a real, high-quality critique prompt
  template (a directive template a small local model can follow: what to
  attack, what to produce), not a stub.
- Loader: auto-discover `*.md` in the package `lenses/` dir at startup; users
  can drop additional `.md` files into `<root>/lenses/` **[derived: user dir
  mirrors the package dir — plan says "same directory" but the package may be
  read-only when installed; support both, user dir wins on name collision]**.
- Tests: discovery of all 8, custom drop-in discovery, collision behavior.

## Task 7: Serial engine

**Goal:** M2's core — the critique-lens loop with convergence.

- `serial_engine.py` + tools (serial-mode sessions only; subagent sessions get
  a directive rejection):
  - `begin_thought(content, tags?, axioms?)`
  - `critique_current_thought(lens)` — returns the lens template (server picks
    a stage-appropriate lens if `lens` omitted **[derived]**)
  - `submit_critique(text)`
  - `refine_current_thought(new_content, challenged_assumptions?)` — server
    records a diff/delta vs the prior version
  - `score_current_thought(scores{})` — the 7 dimensions, tolerant of partial
    input (missing dims carried forward) **[derived]**
  - `commit_thought()` — locks the thought; advances position within stage
- Convergence rules exactly per `docs/build-plan.md` § The serial loop:
  - score improved ≥ `score_threshold` → continue with next lens
  - two consecutive flat/dropped rounds → converged, commit
  - normalized edit distance of refined content < ε (default `0.05`
    **[derived]**, configurable) → fixed point, commit
  - rounds ≥ `max_rounds` (default 3) → commit and flag (`converged_reason:
    "max_rounds"` **[derived]**)
- Lens rotation: stage-appropriate defaults from T5, then remaining lenses in
  `default_lenses` order.
- Tests (TDD): each convergence rule in isolation; full
  begin→critique→submit→refine→score→commit loop as an MCP contract test;
  mode-gate rejection.

## Task 8: Meta tools + import/export

**Goal:** the tools that make small-context local models workable.

- `next_action()` — authoritative: given session state + mode, returns the
  exact next tool to call and a one-line directive. Covers: awaiting mode,
  mid-critique-loop (which sub-step), thought committed (next thought or
  advance_stage), final stage done (finalize_session), finalized (move/keep).
- `summarize_session(scope="stage"|"all")` — deterministic extractive summary
  from committed thoughts (no LLM calls — the server never does inference
  outside autopilot).
- `compress_history(target_tokens)` — 200–400 token digest of prior stages;
  only the current thought's rounds included by default in server responses.
- `export_session(session_id)` / `import_session(json)` — validated on import;
  collision-safe (new id on conflict **[derived]**).
- Tests: next_action truth table across states × modes; digest length bounds;
  export→import roundtrip.

## Task 9: Vendor NECoRT submodule

**Goal:** reproducible pin of PR #7.

- **Read `.superpowers/sdd/necort-recon.md` first** — the recon report on what
  PR #7 actually contains. If it contradicts this task, STOP and report
  BLOCKED with specifics.
- `git submodule add https://github.com/PhialsBasement/Chain-of-Recursive-Thoughts vendor/necort`;
  fetch `refs/pull/7/head`; checkout
  `f4d290ceb086d47bb0f872164344836c47134452`; commit the gitlink at that SHA.
- Merge NECoRT's requirements into `pyproject.toml` with explicit pins;
  document every pin and conflict-resolution in `docs/necort_deps.md`
  (conflicts resolve in favor of stability).
- Copy NECoRT's LICENSE per plan; create `LICENSE-NOTICES` referencing it.
- Write `docs/repinning_necort.md`: the manual re-pin process (`git -C
  vendor/necort fetch && git -C vendor/necort checkout <new_sha>`, run adapter
  test suite, commit new SHA). Note that pull-request refs need an explicit
  fetch spec.
- Tests: smoke-import of the vendored modules the adapter will need (as
  identified by the recon report).

## Task 10: NECoRT adapter

**Goal:** all schema drift absorbed in one file.

- Read `.superpowers/sdd/necort-recon.md` and the vendored source first.
- `necort_adapter.py`: translate between our `Thought`/`SpecialistRound`/
  `UtilityScore` schema and NECoRT's actual types (specialist results, utility
  matrix, equilibrium state — whatever the real code exposes). Map NECoRT's
  scoring dimensions onto our 7; document the mapping in the module docstring.
- Adapter constructs NECoRT's inference client pointed at configured
  endpoint(s) — must support an arbitrary OpenAI-compatible base URL. No
  hardcoded providers.
- Tests (TDD): adapter contract against a mocked NECoRT layer (schema
  stability); a real-NECoRT test with a fake/local endpoint (no network),
  skipped gracefully if vendored deps unavailable.

## Task 11: Subagent engine

**Goal:** M3's engine — NECoRT behind our tool surface.

- `subagent_engine.py` + tools (subagent-mode sessions only):
  - `begin_subagent_thought(content?, prompt_focus?)` — constructs the NECoRT
    invocation: compressed session context, stage-specific prompt template,
    specialist list from config (stage weighting from T5 applied).
  - `advance_subagent_round()` — next specialist in sequence (single
    endpoint) or all concurrently (multiple endpoints configured).
  - `inspect_utility_matrix()` — current scoring state.
  - `commit_subagent_thought()` — accepts current equilibrium as committed.
  - Typical path `begin → commit` must work without the intermediate tools.
- Convergence inside NECoRT (equilibrium threshold 0.75 from config); hard
  round cap `subagent.max_rounds=2` enforced by US even if NECoRT wants more.
- Sequential fallback when one endpoint configured — semantics identical,
  wall-clock longer.
- Tests (TDD): full loop against mocked adapter; round-cap enforcement;
  sequential vs multi-endpoint dispatch; mode-gate rejection; begin→commit
  short path.

## Task 12: Finalize/move UX polish (M4)

**Goal:** edge-case sweep of the lifecycle shipped in T4.

- `human_prompt` wording review against plan text; payload wording covered by
  a test.
- Edge cases with tests: destination exists (with/without force), permission
  denied, cross-filesystem move, repeated moves (move_history accumulates,
  index always points at the latest), moving a not-yet-finalized session
  (allowed, recorded — the move machinery is status-independent
  **[derived]**), move to a path whose parent doesn't exist (create? NO —
  fail with directive payload **[derived]**), keep_here on an already-moved
  session (valid no-op, recorded).

## Task 13: Local-model hardening (M5)

**Goal:** the plan's § Local-model accommodations, end to end.

- Tolerant parsing on every tool: JSON or plaintext fallback parsed
  tolerantly; malformed input → `retry_with_clarification` template from
  `prompts.py` with the exact expected shape, never a raw error/traceback.
- Audit all tool signatures: flat (no nested required objects); response
  templates short and directive.
- `subagent_fallback` manual path (plan's open-decision recommendation for
  M5): config `[subagent] engine="necort"|"manual"` **[derived name]**. In
  manual mode the server hands the model each specialist's prompt template in
  turn (the model plays the specialists itself); scoring collected via the
  same utility schema; no NECoRT import required. This is the insurance
  policy if a NECoRT re-pin breaks.
- Tests: malformed-input matrix across all tools; plaintext roundtrip of the
  serial loop; manual-mode subagent loop end-to-end without vendored code.

## Task 14: Autopilot (M6)

**Goal:** optional internal driving, feature-flagged, zero network when off.

- `autopilot.py`: OpenAI-compatible client via `httpx` (the `autopilot`
  extra). Config `[autopilot] enabled/endpoint/model/temperature`.
- When enabled, two additional tools register:
  `run_stage_autopilot(stage, initial_content)` — drives the serial loop
  internally; `run_subagent_autopilot(stage, initial_content)` — drives the
  NECoRT loop internally.
- Autopilot honors the session's mode: `run_stage_autopilot` on a subagent
  session (and vice versa) is rejected with a directive payload.
- When disabled: the tools are not registered and no network code paths are
  importable/reachable (httpx import stays lazy inside autopilot.py).
- Tests: mock OpenAI-compatible endpoint (local in-process server); tool
  visibility on/off; mode guard; a full autopilot stage run against the mock.

## Task 15: Docs + wiring guides (M7-lite)

**Goal:** everything except the benchmark (deferred: needs blind human
rating).

- `README.md`: what it is, install (uv), quickstart for both modes, the
  mode-selection contract, data locations (`~/deep-think-mcp/`), finalize/move
  UX, autopilot setup, config reference table (every key + default).
- Wiring guides (README section or `docs/wiring.md`): Claude Desktop, Claude
  Code, Cursor, Continue, LibreChat — exact JSON/TOML snippets.
- Verify `docs/necort_deps.md` and `docs/repinning_necort.md` are complete.
- `LICENSE` (MIT **[derived — confirm with owner at review]**),
  `LICENSE-NOTICES` complete.
- Note in README: benchmarks (serial vs subagent head-to-head on 3 canonical
  prompts) are planned but not yet run.
