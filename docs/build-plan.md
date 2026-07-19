# deep-think-mcp — Build Plan

A single MCP server that combines **Sequential Thinking** (structured, staged, persistent reasoning) with **Chain-of-Recursive-Thoughts / NECoRT** (self-critique, multi-agent utility scoring) into one tool. Designed for local models. Offers both **serial** and **subagent** execution modes, chosen per session. Saves reasoning by default and offers to relocate it when done.

---

## Reasoning trail (drafted using the Sequential Thinking method)

**Problem Definition**

- Need one MCP that unifies durable staged reasoning with recursive self-critique.
- Must offer two execution modes — serial (one line of reasoning, iterated) and subagent (NECoRT-style specialist agents) — and let the user pick per session.
- Subagent mode must specifically use the NECoRT implementation from PhialsBasement/Chain-of-Recursive-Thoughts PR #7.
- Reasoning artifacts must persist by default to a home-directory folder, and the user must be given the option to relocate them at session end.

**Research**

- Sequential Thinking MCP (arben-adm) — small MCP, JSON store, Pydantic + Portalocker, five tools, stage taxonomy (Problem Definition → Research → Analysis → Synthesis → Conclusion).
- CoRT base (PhialsBasement) — iterative self-critique loop over one model.
- NECoRT (PR #7) — specialist agents, seven-dimensional utility matrix, bias detection, continuous learning pipeline. Unmerged; commit-pinned dependency required.
- Local-model constraints — small context, weaker instruction-following, single-GPU inference, no reliable JSON mode, slower response.

**Analysis**

- Two modes with a shared session file is the right factoring: same schema, same lens library, same stage machine — only the inner loop differs.
- NECoRT vendored as a git submodule pinned to the PR head SHA. An adapter layer keeps our schema stable if the upstream PR rebases.
- Save location must be **visible** (non-hidden) so users can browse and move files themselves. `~/deep-think-mcp/sessions/` fits.
- The "want to move?" prompt is a soft finalize gate: session is already durable; the tool call just moves the file and updates the index.
- Mode selection is a first-class step in `start_session` — server refuses to accept thoughts until a mode is set, so nothing runs half-configured.

**Synthesis**

- One MCP server, one config file, one JSON-per-session store, one lens library.
- Mode dispatch happens at start; both modes emit identical session artifacts, so results are comparable.
- Serial loop uses critique lenses run sequentially. Subagent loop calls into the vendored NECoRT code and adapts its equilibrium output back into our thought/round schema.
- Finalize path: `finalize_session` returns a move suggestion payload; `move_session` performs it and updates the session index.

**Conclusion**

- Ship serial mode first (M1–M2). Vendor NECoRT and add subagent mode (M3). Add finalize/move (M4). Then autopilot and polish. Benchmark both modes on the same prompts to compare quality vs cost honestly.

---

## Design goals

- **Single MCP called `deep-think-mcp`** — one install, one config file, one on-disk store.
- **Two execution modes, chosen per session** — `serial` (one line of reasoning, self-critiqued in rounds) and `subagent` (NECoRT specialist agents from PR #7).
- **Local-first.** Neither mode assumes hosted models or fast inference. Subagent mode collapses to sequential specialist execution when only one endpoint is available.
- **Persistent by default.** Every session is saved as a JSON file under `~/deep-think-mcp/sessions/`. Nothing is ephemeral unless the user explicitly clears it.
- **User controls the artifact.** At session finalize, the server explicitly offers to move the file elsewhere; the model relays this to the user.
- **Model-driven, not server-driven.** Server is a state machine + prompt library. Optional autopilot mode drives internally.

---

## The two execution modes

### Mode: `serial`

- One line of reasoning, critiqued in sequential rounds.
- The model wears N critique-lens "hats" one at a time (overconfidence, weak evidence, unstated assumption, missing perspective, etc.).
- Same tool loop for each thought: `begin → critique → refine → score → commit`.
- Convergence: stops when scoring plateaus, content stabilizes across two rounds, or `max_rounds` is hit.
- **Best for:** single-GPU local models, small-context models (7B/8B), transparent step-by-step reasoning, cases where you want to see every intermediate thought in the log.

### Mode: `subagent`

- **Vendored implementation of NECoRT from PhialsBasement/Chain-of-Recursive-Thoughts PR #7.**
- Specialist agents (Analysis, Creativity, etc.) generate candidate thoughts for the same question.
- Seven-dimensional utility matrix scores candidates; Nash-equilibrium-inspired convergence selects the consensus thought.
- Continuous learning pipeline updates agent parameters within a session (per PR #7's design).
- **Sequential fallback:** if only one inference endpoint is configured, specialist agents are queried one after another. The equilibrium logic still works — wall-clock is just longer.
- **Best for:** harder single-question stages where diverse framings matter; users with more compute or a hosted-model endpoint they can point at.

### Mode selection contract

- `start_session(question)` **without a mode** returns a "mode required" payload containing:
  - Available modes and one-line descriptions the model can read to the user verbatim.
  - The exact next tool to call: `set_session_mode(session_id, mode)`.
- `start_session(question, mode="serial"|"subagent")` proceeds immediately without a prompt.
- Once set, the mode is **immutable** for the session — a new session must be started to switch. Prevents mid-session state corruption.
- Rationale: forces the model to surface the choice to the user rather than defaulting silently.

---

## Vendoring the NECoRT branch

- **Method:** git submodule at `vendor/necort/` pinned to the head SHA of PhialsBasement/Chain-of-Recursive-Thoughts PR #7 at build time.
- **Why submodule (not `pip install git+...@branch`):** the PR is unmerged and may rebase; a submodule pin is reproducible even if the branch history changes.
- **Adapter layer:** `src/deep_think_mcp/necort_adapter.py` translates between our `Thought` / `CritiqueRound` schema and NECoRT's `specialist_result` / `utility_matrix` types. All schema drift is absorbed here.
- **Dependency handling:** NECoRT's `requirements.txt` is merged into our `pyproject.toml` with explicit version pins. Conflicts resolve in favor of stability; each pin is documented in `docs/necort_deps.md`.
- **Re-pinning:** documented process — `git -C vendor/necort fetch && git -C vendor/necort checkout <new_sha>`, run the adapter test suite, commit the new SHA. No auto-update.
- **Licensing:** copy NECoRT's LICENSE into `vendor/necort/LICENSE` at pin time; reference it in our top-level `LICENSE-NOTICES` file.

---

## Architecture at a glance

- **Layer 1 — Session store.** JSON per session, Pydantic-validated, Portalocker file locks. Session index at `~/deep-think-mcp/index.json`.
- **Layer 2 — Mode dispatcher.** Reads `session.mode` and routes tool calls to either the serial engine or the subagent engine.
- **Layer 3 — Stage machine.** Same for both modes: Problem Definition → Research → Analysis → Synthesis → Conclusion, customizable per session.
- **Layer 4 — Serial engine.** Critique-lens loop.
- **Layer 5 — Subagent engine.** Wraps vendored NECoRT via the adapter; returns thoughts + utility-matrix scores in our schema.
- **Layer 6 — Lifecycle manager.** Handles finalize, move prompt, move action, session index updates.
- **Layer 7 — Optional autopilot.** OpenAI-compatible endpoint for either mode; off by default.

---

## Data model

- **Session**
  - `id`, `question`, `created_at`, `mode: "serial" | "subagent"`, `expected_stages[]`, `current_stage`, `current_thought_id`, `status: active | finalized | archived`
  - `save_path` — current filesystem path (updates on move)
  - `move_history[]` — record of prior paths for audit
- **Thought**
  - `id`, `stage`, `position`, `timestamp`, `content`, `tags[]`, `axioms[]`, `challenged_assumptions[]`
  - `critique_rounds[]` (serial) OR `specialist_rounds[]` (subagent) — mode-tagged for clarity
  - `final_utility_scores{}` — shared schema across modes
  - `committed: bool`
- **CritiqueRound** (serial) — `round_index, lens, critique_text, refined_content, delta_score`
- **SpecialistRound** (subagent) — `round_index, agent_role, candidate_content, utility_vector, equilibrium_state, was_selected`
- **UtilityScore** — 7 dimensions from NECoRT (Correctness, Evidence, Novelty, Clarity, Bias-resistance, Actionability, Coverage). Both modes populate this schema; sources differ.

---

## Tool API surface

### Session lifecycle

- `start_session(question, mode?, stages?)` — creates a session. If `mode` is omitted, returns mode-selection prompt.
- `set_session_mode(session_id, mode)` — sets mode if not yet set.
- `list_modes()` — returns descriptions + recommendations (for the model to relay to the user).
- `resume_session(session_id)`
- `list_sessions()`
- `finalize_session(session_id)` — marks status = finalized; returns move-prompt payload.
- `move_session(session_id, new_path, force?)` — physically moves the JSON file; records in `move_history`; updates the index.
- `keep_here(session_id)` — no-op that records "user declined to move" for audit.
- `clear_session(session_id)` — wipes.

### Thought loop (serial mode)

- `begin_thought(content, tags?, axioms?)`
- `critique_current_thought(lens)` — server returns critique template
- `submit_critique(text)`
- `refine_current_thought(new_content, challenged_assumptions?)`
- `score_current_thought(scores{})`
- `commit_thought()`

### Thought loop (subagent mode)

- `begin_subagent_thought(content?, prompt_focus?)` — kicks off NECoRT run
- `advance_subagent_round()` — runs the next specialist in sequence (or all in parallel if multiple endpoints are configured)
- `inspect_utility_matrix()` — returns current scoring state
- `commit_subagent_thought()` — accepts current equilibrium as committed
- Typical usage: model only needs `begin_subagent_thought` → `commit_subagent_thought`; the intermediate tools exist for stepwise inspection when desired.

### Meta

- `advance_stage()`
- `summarize_session(scope="stage"|"all")`
- `compress_history(target_tokens)` — critical for small-context local models
- `next_action()` — server tells the model what to call next given current state and mode. Removes the "did I remember to critique?" burden from weaker local models.

### Import/export

- `export_session(session_id)`
- `import_session(json)`

---

## Save location and lifecycle

- **Default root:** `~/deep-think-mcp/`
  - `sessions/` — one JSON per session
  - `index.json` — session index tracking id → current path, mode, status, timestamps
  - `config.toml` — user config
  - `logs/` — server logs
- **Non-hidden, home-directory, single top-level folder** — chosen so the user can browse and manage files directly without needing terminal tricks.
- **On every mutation** — server writes a `.bak` sibling, then the new file, then removes the `.bak`.

### Finalize + move flow

- **On `finalize_session`** — server returns a payload:
  - `current_path`
  - `human_prompt` — canned text the model reads to the user, e.g. *"Your reasoning is saved at `~/deep-think-mcp/sessions/<id>.json`. Would you like to move it elsewhere (a project folder, your Documents, etc.), or leave it where it is?"*
  - `available_tools` — points at `move_session(session_id, new_path)` and `keep_here(session_id)`.
- **On `move_session`** — server:
  1. Validates destination (writable directory, no clobber unless `force=true`).
  2. Moves the file atomically (write to destination, verify, unlink original).
  3. Records the move in `session.move_history`.
  4. Updates the session index so `list_sessions` and `resume_session` still find it.
  5. Returns confirmation with the new absolute path.
- **On `keep_here`** — records the decision; no filesystem change.
- **Sessions moved outside `~/deep-think-mcp/`** — the index still tracks them by absolute path, so cross-device sync tools (Dropbox, iCloud, Syncthing, git) work with zero server changes.

---

## The serial loop — one stage, done well

- `begin_thought(content)` — model drafts.
- Server picks a stage-appropriate critique lens; model calls `critique_current_thought(lens)`, receives template, calls `submit_critique(text)`.
- `refine_current_thought(new_content)` — server diffs against prior version.
- `score_current_thought(scores{})` — model self-scores across the 7 utility dimensions.
- **Convergence rule:**
  - Score improved ≥ threshold → continue with next lens.
  - Two flat/dropped rounds → converged, commit.
  - Content essentially unchanged (normalized edit distance < ε) → fixed point, commit.
  - Round count ≥ `max_rounds` (default **3** for local models) → commit and flag.
- `commit_thought()` — locks it; server advances position within stage or advances the stage cursor.

## The subagent loop — one stage, done via NECoRT

- `begin_subagent_thought(content?, prompt_focus?)` — adapter constructs a NECoRT invocation with:
  - Current session context (compressed if needed).
  - The stage-specific prompt template.
  - The list of specialist agents to run (from config; default matches PR #7).
- Adapter runs specialists **in sequence** (single endpoint) or **concurrently** (multiple endpoints if configured).
- NECoRT computes the utility matrix, runs its equilibrium check, updates its continuous-learning parameters.
- Adapter translates the equilibrium result into our `Thought` with `specialist_rounds[]` and `final_utility_scores{}`.
- `commit_subagent_thought()` — locks it.
- **Convergence** — handled inside NECoRT (equilibrium stability threshold from PR #7, default 0.75).
- **Round cap** — hard ceiling from our config (`subagent.max_rounds`, default **2**) as a safety net even if NECoRT wants more.

---

## Stage progression

- Default stages inherited from Sequential Thinking: Problem Definition, Research, Analysis, Synthesis, Conclusion.
- Configurable per session via `start_session(stages=[...])`.
- Each stage can hold multiple committed thoughts.
- **Stage-appropriate lens defaults for serial mode** (e.g., `weak_evidence` and `overconfidence` in Analysis; `missing_perspective` and `unstated_assumption` in Synthesis).
- **Stage-appropriate agent weighting for subagent mode** (e.g., Creativity weighted higher in Synthesis; Analysis weighted higher in Analysis).

---

## Local-model accommodations

- **Small context** — `compress_history` returns a 200–400 token digest of prior stages; only the current thought's rounds are included by default in server responses.
- **Weaker instruction-following** — `next_action()` is authoritative; tool signatures are flat; response templates are short and directive.
- **Slower inference** — `max_rounds` defaults are aggressive (serial 3, subagent 2); convergence commits at the first sign of stability.
- **Unreliable JSON mode** — all tools accept either JSON or a plain-text fallback the server parses tolerantly; malformed input returns a `retry_with_clarification` template rather than an error.
- **Single GPU** — subagent mode falls back to sequential specialist execution if only one endpoint is configured; wall-clock is longer but semantics are preserved.

---

## Critique lens library

- Bundled prompt templates in `src/deep_think_mcp/lenses/` as `.md` files:
  - `overconfidence.md`, `weak_evidence.md`, `missing_perspective.md`, `unstated_assumption.md`, `scope_creep.md`, `alternative_framing.md`, `steel_man.md`, `first_principles.md`.
- Users can add their own by dropping a `.md` file in the same directory; server auto-discovers on startup.
- Serial mode rotates through lenses per round.
- Subagent mode uses lenses as additional prompt scaffolding for specialists (e.g., the Analysis specialist gets `weak_evidence` framing prepended to its prompt).

---

## Optional autopilot

- Config: `autopilot.enabled=true`, `autopilot.endpoint="http://localhost:11434/v1"`, `autopilot.model="qwen2.5:14b"`.
- Two new tools appear when enabled:
  - `run_stage_autopilot(stage, initial_content)` — runs the serial loop internally.
  - `run_subagent_autopilot(stage, initial_content)` — runs the NECoRT loop internally.
- **Off by default;** when off, the server never touches the network.
- Autopilot honors the session's mode setting — you can't `run_stage_autopilot` on a subagent session or vice versa.

---

## Project layout

```
deep-think-mcp/
  src/deep_think_mcp/
    __init__.py
    server.py                # MCP tool registration + dispatcher
    session.py               # Session/Thought Pydantic models
    store.py                 # JSON + Portalocker persistence
    index.py                 # Session index management
    stages.py                # Stage machine (shared)
    serial_engine.py         # Serial critique loop
    subagent_engine.py       # Wraps NECoRT via adapter
    necort_adapter.py        # Schema translation to/from PR #7
    lifecycle.py             # finalize / move / keep flow
    lenses/                  # Critique lens templates
    autopilot.py             # Optional local-model client
    prompts.py               # Tool-response templates
  vendor/
    necort/                  # Git submodule → PR #7 head SHA
  config/
    default.toml
  tests/
    test_serial_convergence.py
    test_subagent_adapter.py
    test_mode_selection.py
    test_move_session.py
    test_local_model_roundtrip.py
  docs/
    necort_deps.md
    repinning_necort.md
  README.md
  LICENSE
  LICENSE-NOTICES
  pyproject.toml
  .gitmodules
```

- **Language:** Python (matches both source projects and the official MCP Python SDK).
- **Core dependencies:** `mcp`, `pydantic`, `portalocker`, `tomli`, plus NECoRT's transitive deps via the submodule.
- **Optional dependencies:** `httpx` for autopilot only.

---

## Build milestones

- **M1 — Skeleton MCP + mode selection** (day 1–3). Server boots; `start_session` enforces mode selection; `list_modes` returns descriptions; session store + index work; `finalize_session` and `move_session` implemented end-to-end **before** any thinking loops exist. This guarantees the persistence + move UX is solid regardless of engine correctness.
- **M2 — Serial engine** (day 4–7). Critique loop with one lens, then the full lens library; convergence rules; `next_action`; `compress_history`.
- **M3 — NECoRT submodule + subagent engine** (day 8–12). Pin PR #7 SHA; write adapter; implement `begin_subagent_thought` / `commit_subagent_thought`; sequential fallback for single-endpoint setups; utility matrix passthrough into our schema.
- **M4 — Finalize/move UX polish** (day 13). `human_prompt` payload wording; `keep_here` no-op; edge cases (destination exists, permission denied, cross-filesystem move).
- **M5 — Local-model hardening** (day 14–15). Tolerant parsing, retry templates, defaults tuning against a real 7B model.
- **M6 — Autopilot** (day 16–17). Both `run_stage_autopilot` and `run_subagent_autopilot`; feature-flagged.
- **M7 — Docs, benchmarks, polish** (day 18+). Wiring guides for Claude Desktop, Cursor, Continue, LibreChat; head-to-head benchmark of serial vs subagent on 3 canonical prompts.

---

## Testing strategy

- **Unit** — convergence logic, mode dispatch, lens rotation, tolerant parsing, index integrity across moves.
- **Contract** — MCP tool round-trips against the real SDK.
- **Adapter** — subagent engine tested against a mocked NECoRT to confirm schema translation is stable, then against real NECoRT.
- **Integration** — three canonical hard questions run in both modes against a local 7B, a local 14B, and a hosted model. Compared on quality (blind-rated by the user), tokens, and wall-clock.
- **Move UX** — simulate finalize → user says "yes" → move → confirm `resume_session` still works; also user says "no" → confirm the file stays put and stays indexed.
- **Load** — rapid tool calls to confirm Portalocker holds under contention.

---

## Configuration surface

- `~/deep-think-mcp/config.toml`
- Sections:
  - `[store]` — `root`, `sessions_dir`, `index_path`
  - `[modes]` — `default_prompt_user=true` (server always asks even if config sets a default)
  - `[serial]` — `max_rounds=3`, `score_threshold=0.05`, `fast_mode=false`, `default_lenses=[...]`
  - `[subagent]` — `max_rounds=2`, `equilibrium_threshold=0.75`, `agents=[...]`, `sequential_fallback=true`
  - `[stages]` — `default=["Problem Definition", "Research", "Analysis", "Synthesis", "Conclusion"]`
  - `[autopilot]` — `enabled`, `endpoint`, `model`, `temperature`
- All settings per-session-overridable via `start_session` args.

---

## Open decisions still on the table

- **Rendered Markdown transcript on finalize** — should `finalize_session` also offer a "render as human-readable Markdown" action alongside the move prompt? Nice for humans, extra work. Recommend deferring to M7.
- **Subagent fallback path** — should we build a "manual" subagent path where the model plays each specialist itself (no NECoRT-vendored code)? Useful as a fallback if the vendored code breaks on a NECoRT rebase. Recommend building this as `subagent_fallback` in M5.
- **"Resume in a different mode"** — I recommend no. Forces users to start a new session and keeps sessions internally consistent.
- **Stage profile presets** — decision / research / coding / writing profiles that swap stage lists and lens defaults. Recommend M7.
- **NECoRT re-pin cadence** — manual only, or a `check_necort_upstream` maintenance script that warns when the PR branch has advanced? Recommend manual for v1.
