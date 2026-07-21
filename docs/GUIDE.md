# deep-think-mcp — The Complete Guide

*A teaching document: what it is, why it exists, how it works, how to install it, and how to use every part of it.*

This guide is self-contained. If you read it top to bottom you will understand the system's philosophy, its architecture, both reasoning modes, every tool, every configuration key, and how to extend it — without needing to read the source. Reference tables live near the end; the earlier sections teach concepts.

**Contents**

1. [What this is, in one minute](#1-what-this-is-in-one-minute)
2. [The problem it solves](#2-the-problem-it-solves)
3. [The two reasoning traditions it fuses](#3-the-two-reasoning-traditions-it-fuses)
4. [Core concepts (the mental model)](#4-core-concepts-the-mental-model)
5. [The honest NECoRT story (why "hybrid")](#5-the-honest-necort-story-why-hybrid)
6. [Architecture: the seven layers](#6-architecture-the-seven-layers)
7. [Installation](#7-installation)
8. [Launching and wiring into an MCP client](#8-launching-and-wiring-into-an-mcp-client)
9. [Serial mode, in depth](#9-serial-mode-in-depth)
10. [Subagent mode, in depth](#10-subagent-mode-in-depth)
11. [`next_action`: the north star](#11-next_action-the-north-star)
12. [Sessions, persistence, and the finalize/move lifecycle](#12-sessions-persistence-and-the-finalizemove-lifecycle)
13. [Tolerant input (built for weak models)](#13-tolerant-input-built-for-weak-models)
14. [Autopilot](#14-autopilot)
15. [Complete configuration reference](#15-complete-configuration-reference)
16. [Complete tool reference](#16-complete-tool-reference)
17. [Directive and error code reference](#17-directive-and-error-code-reference)
18. [Data model reference](#18-data-model-reference)
19. [Extending the system](#19-extending-the-system)
20. [Testing and development](#20-testing-and-development)
21. [Design philosophy: why it looks the way it does](#21-design-philosophy-why-it-looks-the-way-it-does)
22. [Troubleshooting & FAQ](#22-troubleshooting--faq)
23. [Glossary](#23-glossary)

---

## 1. What this is, in one minute

**deep-think-mcp is a [Model Context Protocol](https://modelcontextprotocol.io) server that gives a language model a structured, persistent scratchpad for hard thinking.** Instead of a model reasoning in one unbroken stream of tokens, it works a problem in *stages* (Problem Definition → Research → Analysis → Synthesis → Conclusion), and within each stage it either (a) drafts one line of reasoning and sharpens it through rounds of self-critique, or (b) spins up several specialist perspectives that compete and converge on the strongest candidate. Every intermediate step is scored, saved to disk, and inspectable.

It is deliberately built for **local models** — 7B/8B-class models with small context windows, shaky instruction-following, and no reliable JSON mode. That single constraint shapes almost every design decision: tool responses are short and flat; every response tells the model *exactly which tool to call next*; inputs are accepted as JSON *or* loose plaintext; and there is a single tool, `next_action`, that answers "what do I do now?" from any state.

You talk to it the way you talk to any MCP server: through tool calls. It has 25 tools (27 with the optional autopilot feature turned on).

---

## 2. The problem it solves

A capable model asked a hard question will often produce a fluent answer that *sounds* reasoned but skipped the hard parts: it assumed something it never checked, leaned on a weak analogy, ignored a stakeholder, or converged on the first framing that came to mind. The usual fixes — "think step by step," "consider alternatives," "critique your answer" — work unevenly, and they leave nothing behind: the reasoning evaporates with the context window.

Three concrete gaps:

1. **Reasoning is ephemeral.** Once the conversation scrolls away, the chain of thought is gone. You can't revisit *why* a conclusion was reached, or resume a half-finished analysis tomorrow.
2. **Self-critique is unstructured.** "Critique yourself" gives a model too much latitude. It will critique whatever is easiest, not what is most load-bearing. There's no guarantee it stress-tests its evidence, its assumptions, and its blind spots in turn.
3. **Local models make both worse.** A small model asked to run a multi-step reasoning protocol *and* remember where it is in that protocol *and* emit clean JSON at each step will drop one of those balls. It needs the protocol externalized and the next step handed to it.

deep-think-mcp addresses all three. Reasoning is **persistent** (one JSON file per session, saved on every mutation). Critique is **structured** (a rotating library of named critique "lenses," each a directive prompt that tells the model exactly what failure mode to hunt for). And the whole thing is **externalized into a state machine** the server runs on the model's behalf, so the model never has to remember protocol state — it just calls `next_action` and does the one thing it's told.

---

## 3. The two reasoning traditions it fuses

The project unifies two ideas that existed separately in the MCP/LLM ecosystem.

### Sequential Thinking

The "Sequential Thinking" family of MCP servers gave models a **staged, revisable scratchpad**: a problem is worked through explicit stages, each stage holds one or more numbered thoughts, and thoughts can be revised. It contributes to deep-think-mcp:

- the **stage taxonomy** (Problem Definition → Research → Analysis → Synthesis → Conclusion, customizable per session),
- **durable persistence** (JSON store, file locks),
- the idea that reasoning should be a **first-class artifact** you can save, resume, and move.

### Chain-of-Recursive-Thoughts / NECoRT

Chain-of-Recursive-Thoughts (CoRT) is the idea that a model should **generate candidate answers, critique them, and iterate toward a better one** — recursive self-improvement within a single question. Its "Nash-equilibrium" variant (NECoRT) frames this as several perspectives proposing candidates that get **peer-rated** and converged on. It contributes:

- **multi-perspective candidate generation** (specialists),
- **utility scoring** of candidates,
- **equilibrium-style convergence** (stop when the field stabilizes on a winner).

### The fusion

deep-think-mcp puts both under one roof with a **shared session schema**. A session picks one *execution mode* at creation and keeps it for life:

- **serial mode** realizes the Sequential-Thinking-plus-self-critique idea: one line of reasoning, sharpened by rotating critique lenses.
- **subagent mode** realizes the NECoRT idea: competing specialist perspectives scored and converged.

Because both modes emit the *same* artifacts (a stage machine, committed thoughts, 7-dimension utility scores, an audit trail), you can run the same question through both and compare honestly.

---

## 4. Core concepts (the mental model)

Six concepts carry the whole system. Learn these and everything else is detail.

### 4.1 Session

A **session** is one reasoning effort about one `question`. It owns: the mode, the list of expected stages, a cursor into those stages, a list of thoughts, an audit trail, and a status (`active` → `finalized` → optionally `archived`). It is persisted as a single JSON file and tracked in a central index. Every session has a stable `id` (a uuid4 hex string).

### 4.2 Mode (immutable)

At creation a session is either **serial** or **subagent**. This is chosen once and **can never change** — a second attempt to set the mode is rejected. This immutability is deliberate: it means every tool in a session operates on a consistent state machine and can never half-configure the session into an inconsistent hybrid. To use the other mode, start a new session.

If you create a session *without* a mode, the server refuses to run any thinking tool and instead returns a **mode-required** directive that names both modes and tells you to call `set_session_mode`. This forces the choice to surface to the human rather than being silently defaulted.

### 4.3 Stage machine

Every session has an ordered list of **stages** — by default `Problem Definition, Research, Analysis, Synthesis, Conclusion`, overridable per session. A cursor (`current_stage`) points at the active one. `advance_stage` moves it forward; at the last stage it refuses and points you at `finalize_session`. Each stage can hold multiple committed thoughts. Stages matter beyond bookkeeping: they bias *how* the model thinks. In serial mode each stage has default critique lenses (e.g. Analysis defaults to `weak_evidence` + `overconfidence`); in subagent mode each stage weights a specialist higher (e.g. Creativity is emphasized in Synthesis).

### 4.4 Thought

A **thought** is one committed unit of reasoning inside a stage. In serial mode a thought accretes a list of **critique rounds** (each: a lens, the critique text, the refined content, a delta score). In subagent mode a thought accretes **specialist rounds** (each: a specialist role, its candidate, a 7-dim utility vector, an equilibrium flag, a was-selected flag). Both kinds end with a `final_utility_scores` object and a `committed` flag. The thought's `content` is set to the winning/final text at commit.

### 4.5 The 7-dimension utility score

Every thought is scored on the same seven dimensions, always in this order:

```
correctness · evidence · novelty · clarity · bias_resistance · actionability · coverage
```

Each is a float in `[0, 1]`. This shared schema is what lets serial and subagent results be compared. **Important nuance:** the two subagent engines populate these dimensions differently (see §10) — the manual engine fills all seven with real self-scores; the necort engine can only produce genuine signal for three of them and leaves the other four at a neutral `0.5`. This is disclosed honestly everywhere it matters, including in the commit-gate logic.

### 4.6 Directive-driven control (the key idea)

This is the concept that makes the system usable by weak models. **The server does not assume the model knows the protocol.** Instead, *every* tool response is a small flat JSON object that includes:

- a human-readable `message` (what just happened),
- usually a `next_tool` field (the exact tool to call next),
- and, when something can't proceed, a **directive** — never a raw error or traceback, but a payload that names what's wrong and how to fix the call.

If the model is ever unsure, it calls **`next_action(session_id)`**, which is the *authoritative* resolver: given any session state, it returns the exact next tool and a one-line instruction. You can drive an entire session by alternating "do the thing" with "ask `next_action` what's next." This is the single most important usability property of the system.

---

## 5. The honest NECoRT story (why "hybrid")

The original design (`docs/build-plan.md`) imagined subagent mode as a full port of [PhialsBasement/Chain-of-Recursive-Thoughts PR #7](https://github.com/PhialsBasement/Chain-of-Recursive-Thoughts): specialist agents, a native 7-dimension utility matrix, bias detection, and a continuous-learning pipeline.

A code reconnaissance during the build (preserved at `.superpowers/sdd/necort-recon.md`) found that most of PR #7 is **disconnected filler**. The `enhanced-implementations/` files that advertise those features:

- are never imported by anything,
- make **zero** LLM calls (they're pure keyword heuristics),
- and several aren't even importable Python (hyphenated filenames, no `__init__.py`),
- with file headers pointing at an unrelated repository.

The *one* part of the PR that genuinely works is **`NashEquilibriumRecursiveChat`** in `recursive_thinking_ai.py` / `nash_recursive_thinking.py` — a real, LLM-driven, Nash-equilibrium-style recursive chat core that generates candidate responses, has them peer-rate each other, and converges.

So the project made a deliberate, owner-approved decision:

1. **Vendor PR #7 in full** as a git submodule pinned to the exact commit (`f4d290ce…`) so the vendored tree is a faithful, re-pinnable mirror of upstream.
2. **Import only the two working files**, wrapped by a single adapter (`necort_adapter.py`) that shims three real defects **without editing a single vendored line**: a `datetime` crash bug, a hardcoded OpenRouter endpoint, and a `print()`-to-stdout bug that would corrupt the MCP stdio transport.
3. Because a single blended 0–10 Nash peer-rating can honestly inform only **3 of the 7** utility dimensions, provide genuine multi-perspective diversity through a **second, endpoint-free engine built from scratch** — the *manual specialist engine* — where the calling model plays each specialist itself and self-scores all seven dimensions for real.

That's why **subagent mode has two engines**, selected in config:

- `engine = "necort"` drives the vendored Nash core against an OpenAI-compatible endpoint.
- `engine = "manual"` needs no endpoint, no network, and no vendored code at all — and is where a subagent session lands automatically if no endpoint is configured.

The lesson baked into the codebase: *verify third-party code against reality before building on its advertised behavior.* The recon changed the entire subagent design.

---

## 6. Architecture: the seven layers

The system is layered. Reading a tool call top to bottom, it passes through:

```
                    MCP client (Claude Desktop, Cursor, a local model, …)
                                        │  tool call over stdio
                                        ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ server.py — TOOL REGISTRATION + DISPATCH                                  │
│   • registers 25 (or 27) FastMCP tools                                     │
│   • mode_gate: rejects wrong-mode / no-mode calls before they touch logic  │
│   • storage_guard: turns storage faults into directives, never tracebacks  │
│   • tolerant.*: parses JSON-or-plaintext params at the boundary            │
│   • loads the session, calls the engine, persists, formats via prompts.py  │
└───────────────┬───────────────────────────────────────────┬───────────────┘
                │                                           │
   ┌────────────▼───────────┐               ┌───────────────▼───────────────┐
   │ ENGINES (the thinking)  │               │ meta.py — GUIDANCE + I/O        │
   │  serial_engine.py       │               │  next_action (authoritative)    │
   │  subagent_engine.py     │               │  summarize / compress (no LLM)  │
   │  manual_engine.py       │               │  export / import                │
   │  necort_adapter.py ─────┼──▶ vendor/necort (Nash core, pinned submodule) │
   │  autopilot.py (opt.)    │               └─────────────────────────────────┘
   └────────────┬───────────┘
                │ operates on
   ┌────────────▼──────────────────────────────────────────────────────────┐
   │ DOMAIN + PERSISTENCE                                                      │
   │  session.py  — Pydantic data model (schema only, no logic)               │
   │  stages.py   — stage cursor + stage→lens and stage→agent-weight tables    │
   │  lens_loader.py — discover bundled + user critique lenses                 │
   │  store.py    — JSON-per-session persistence (Portalocker + .bak protocol) │
   │  index.py    — the session index (id → path/mode/status/timestamps)       │
   │  lifecycle.py— finalize / keep / atomic move                              │
   │  config.py   — layered TOML config + root resolution + bootstrap          │
   │  prompts.py  — every response/directive template (wording lives here)     │
   │  tolerant.py — JSON-or-plaintext boundary parsers                         │
   └──────────────────────────────────────────────────────────────────────────┘
```

The original design doc numbers these as seven conceptual layers: (1) session store, (2) mode dispatcher, (3) stage machine, (4) serial engine, (5) subagent engine, (6) lifecycle manager, (7) optional autopilot. The file layout above is how those layers actually landed.

**Two cross-cutting rules worth internalizing:**

- **`prompts.py` owns all wording.** No engine or server function inlines a response string; they build structured results and hand them to `prompts.py`. This keeps the model-facing contract in one auditable place.
- **`necort_adapter.py` is the *only* file that imports vendored code.** All the third-party mess is quarantined behind that one boundary. If PR #7 is re-pinned, only the adapter can break.

---

## 7. Installation

### Requirements

- **Python ≥ 3.11** (the project uses stdlib `tomllib`, which is 3.11+).
- **[`uv`](https://docs.astral.sh/uv/)** for dependency and environment management. (The codebase deliberately never invokes bare `python`/`pip`; always `uv run …`.)
- **git** with submodule support (only needed for the `necort` subagent engine; everything else works without it).

### Clone with submodules

The vendored NECoRT core is a git submodule, so clone recursively:

```bash
git clone --recurse-submodules <repo-url> deep-think-mcp
cd deep-think-mcp
```

If you already cloned without submodules:

```bash
git submodule update --init
```

You can **skip** the submodule entirely if you never use `engine = "necort"`. Serial mode, the manual subagent engine, autopilot, and everything else work with `vendor/necort/` uninitialized.

### Install dependencies

```bash
uv sync
```

This installs the core set: `mcp`, `pydantic`, `portalocker`, plus `requests` / `numpy` / `openai`. The last three exist only because **importing** the two vendored NECoRT modules executes their top-level `import` statements — `openai` in particular is dead code at runtime (the vendored core uses raw `requests`), but its `import openai` line runs at module load, so the package must be present. This is documented in `docs/necort_deps.md`.

To enable autopilot later, add its one extra dependency (`httpx`):

```bash
uv sync --extra autopilot
```

### Verify the install

```bash
uv run pytest
```

A healthy install runs the full suite green (423 tests as of this writing) with no warnings. Every test injects a temporary data root, so **running the suite never touches your real `~/deep-think-mcp/`**.

### What "installed" means here

This is a **dev-checkout tool**, not a PyPI package. The server reads `config/default.toml` from the repo root next to `src/`, so you run it *from the clone*. Every client wiring (next section) points at your clone's path rather than assuming a `pip install`.

---

## 8. Launching and wiring into an MCP client

### Launching manually

The stdio entrypoint is:

```bash
uv run python -m deep_think_mcp.server
```

That command speaks MCP over stdin/stdout. You normally don't run it by hand — an MCP client launches it for you.

To run **one shared, always-live server** that several clients reach over a URL (instead of each spawning its own stdio process), launch it as a Streamable HTTP daemon. This is also the fix when a long-lived agent host intermittently drops deep-think's tools from its cached tool schema — see [`docs/http-transport.md`](http-transport.md):

```bash
python -m deep_think_mcp.server --transport streamable-http --host 127.0.0.1 --port 8182
```

### The data root

On first use the server **bootstraps** a data root (default `~/deep-think-mcp/`), creating `sessions/` and `logs/` and seeding `config.toml` from the packaged defaults. Override the location with the `DEEP_THINK_HOME` environment variable, which always wins. Resolution order:

```
DEEP_THINK_HOME  →  [store].root in config  →  ~/deep-think-mcp
```

Setting `DEEP_THINK_HOME` per client is the clean way to keep separate data roots for separate clients.

### Wiring into clients

Exact, copy-pasteable snippets for **Claude Desktop, Claude Code, Cursor, Continue, and LibreChat** live in [`docs/wiring.md`](wiring.md). The shape for a `mcpServers`-style client (Claude Desktop, Cursor) is:

```json
{
  "mcpServers": {
    "deep-think": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/deep-think-mcp", "run", "python", "-m", "deep_think_mcp.server"],
      "env": { "DEEP_THINK_HOME": "/absolute/path/to/your/data-root" }
    }
  }
}
```

The `--directory` flag is what lets the dev-checkout server find `config/default.toml`. Consult `docs/wiring.md` for the clients whose schema differs (Continue and LibreChat use different config shapes).

---

## 9. Serial mode, in depth

Serial mode develops **one line of reasoning** and sharpens it through rounds of self-critique, one critique lens at a time. It is the recommended mode for single-GPU setups, small (7B/8B) local models, and any situation where you want every intermediate step visible.

### The loop

Within a stage, one thought goes through this cycle:

```
begin_thought ──▶ critique_current_thought ──▶ submit_critique ──▶ refine_current_thought ──▶ score_current_thought
                        ▲                                                                            │
                        └──────────────────── (not converged: next lens) ◀───────────────────────────┤
                                                                                                     │
                                                              (converged, or max_rounds) ────────────▼
                                                                                              commit_thought
```

Step by step:

1. **`begin_thought(session_id, content, tags?, axioms?)`** — you draft the thought. `tags` and `axioms` are optional metadata (accepted as JSON arrays *or* comma/newline-separated plaintext).
2. **`critique_current_thought(session_id, lens?)`** — opens a critique round. If you omit `lens`, the server picks a stage-appropriate default and rotates through the library on later rounds. The response places your **current draft immediately before the lens template text** — the templates open with wording like "the draft thought above," so this adjacency is a deliberate contract. The lens template is a directive prompt telling the model exactly what failure mode to hunt for.
3. **`submit_critique(session_id, text)`** — you (playing the lens) record the critique. Blank text is rejected with a directive.
4. **`refine_current_thought(session_id, new_content, challenged_assumptions?)`** — you rewrite the thought to address the critique. The server computes and returns the **normalized edit distance** between the prior and refined content (via stdlib `difflib`) — a measure of how much actually changed.
5. **`score_current_thought(session_id, scores?)`** — you self-score the refined thought on the 7 dimensions. Scores are accepted as a JSON object *or* plaintext (`"correctness: 0.8, clarity: 0.7"`). Omitted dimensions carry forward from the previous round. The server then **evaluates convergence** and tells you whether to continue (another lens) or commit.
6. **`commit_thought(session_id)`** — locks the thought: its final refined content becomes its `content`, `committed` is set, and the cursor clears so the next `begin_thought` starts fresh.

### The four convergence rules (and their precedence)

After each `score_current_thought`, the engine decides whether the thought has converged. The rules are checked **in this order**, and the first that fires wins (so the *reason* reported is the informative one):

1. **`fixed_point`** — if the normalized edit distance between the prior and refined content is **below `edit_distance_epsilon`** (default `0.05`), the critique produced essentially no change. It's a fixed point → converge and commit.
2. **`diminishing_returns`** — walking completed rounds newest-to-oldest, if the last **two rounds in a row** each improved the overall score by **less than `score_threshold`** (default `0.05`), further rounds aren't helping → converge and commit.
3. **`max_rounds`** — if the number of completed rounds reaches **`max_rounds`** (default `3`), stop regardless → commit, flagged `converged_reason: "max_rounds"`.
4. **Otherwise** — not converged. A single flat round, or an improving round, means "keep going": rotate to the next lens.

Natural convergence (rules 1–2) outranks the `max_rounds` ceiling when both would fire, so you learn *why* it stopped, not just that a budget ran out. The aggressive default caps (3 rounds) exist because local-model inference is slow; the loop commits at the first genuine sign of stability.

### Lens rotation and the lens library

Serial mode ships **8 bundled critique lenses** (each a real `.md` prompt template in `src/deep_think_mcp/lenses/`, not a stub):

`overconfidence` · `weak_evidence` · `missing_perspective` · `unstated_assumption` · `scope_creep` · `alternative_framing` · `steel_man` · `first_principles`

When you omit the `lens` argument, the server picks a **stage-appropriate default first**, then rotates through the remaining lenses in library order:

| Stage | Default lenses |
|---|---|
| Problem Definition | `unstated_assumption`, `scope_creep` |
| Research | `weak_evidence`, `missing_perspective` |
| Analysis | `weak_evidence`, `overconfidence` |
| Synthesis | `missing_perspective`, `unstated_assumption` |
| Conclusion | `steel_man`, `overconfidence` |

(A custom stage not in this table simply falls back to the full `[serial].default_lenses` rotation.)

You can **add or override lenses**: drop a `.md` file into `~/deep-think-mcp/lenses/`. A same-named file there replaces the bundled one entirely; a new name adds a lens the server will discover at startup.

### A worked serial session

```text
start_session(question="Should we cache API responses at the edge or origin?")
  → { "mode_required": true, "next_tool": "set_session_mode", "session_id": "…" }

set_session_mode(session_id, mode="serial")
  → { "mode": "serial", "message": "Mode set to 'serial'. This is permanent…" }

begin_thought(session_id, content="Cache at the edge: lower latency for users…")
  → { "next_tool": "critique_current_thought" }

critique_current_thought(session_id)                       # lens omitted → server picks
  → { "lens": "weak_evidence", "draft_content": "Cache at the edge…",
      "lens_template": "The draft thought above claims…", "next_tool": "submit_critique" }

submit_critique(session_id, text="No numbers back the latency claim; edge caching…")
  → { "next_tool": "refine_current_thought" }

refine_current_thought(session_id, new_content="Cache at the edge (CDN PoPs) when…")
  → { "edit_distance": 0.31, "next_tool": "score_current_thought" }

score_current_thought(session_id, scores="correctness: 0.8, evidence: 0.7, novelty: 0.5,
                                          clarity: 0.8, bias_resistance: 0.6,
                                          actionability: 0.7, coverage: 0.6")
  → { "converged": false, "next_tool": "critique_current_thought" }        # go again

# … another critique→submit→refine→score round …
  → { "converged": true, "converged_reason": "diminishing_returns",
      "next_tool": "commit_thought" }

commit_thought(session_id)
  → { "committed": true, "next_tool": "begin_thought" }

advance_stage(session_id)                                   # this stage is done
  → { "current_stage": "Research" }

# … repeat through the remaining stages …

finalize_session(session_id)
  → { "human_prompt": "Your reasoning is saved at …/sessions/<id>.json. Would you like
       to move it elsewhere…?", "available_tools": ["move_session", "keep_here"] }
```

---

## 10. Subagent mode, in depth

Subagent mode develops a thought by having **several specialist perspectives propose competing candidates**, scoring them on the 7-dim utility matrix, and converging on the strongest. It's best for harder single questions where diverse framings matter.

Subagent mode has **two engines** selected by `[subagent].engine`. Both are driven by the **same four tools** — `begin_subagent_thought`, `advance_subagent_round`, `inspect_utility_matrix`, `commit_subagent_thought` — so the tool surface is identical; only what happens inside differs.

### 10a. The manual engine (`engine = "manual"`) — endpoint-free

This is the recommended default when you don't have a separate inference endpoint to point at. **No network, no vendored code** — the *calling model itself* plays each specialist.

The roster comes from `[subagent].agents` (default `["Analysis", "Creativity", "Skeptic"]`). Each specialist gets a **framing** (from `prompts.SPECIALIST_FRAMINGS`), weighted higher in its home stage (Analysis in the Analysis stage, Creativity in Synthesis).

The flow:

1. **`begin_subagent_thought(session_id, content?, prompt_focus?)`** — creates the thought and hands you the **first specialist's prompt** (specialist #0, e.g. "Analysis"). No candidate is recorded yet. Your job now: read that prompt, produce a candidate answer *as that specialist*, and self-score it on all 7 dimensions.
2. **`advance_subagent_round(session_id, candidate, scores)`** — records that specialist's candidate and scores, then hands you the **next specialist's prompt**. Repeat until the roster is exhausted.
3. When the last specialist has gone, `advance_subagent_round` instead runs the **deterministic selection** and returns the round verdict: **highest 7-dim mean wins; ties keep the earliest specialist** (the comparison uses strict `>`, so an equal score never displaces the incumbent). The winner is marked `was_selected`, its vector becomes the thought's `final_utility_scores`.
4. **`inspect_utility_matrix(session_id)`** (optional) — shows every candidate's scores and which one won.
5. **`commit_subagent_thought(session_id)`** — locks the winning candidate as the thought's content.

In manual mode **all 7 dimensions carry real signal** because the model actually scored them. An omitted dimension defaults to a neutral `0.5` (no carry-forward — each specialist scores from scratch).

A worked manual round:

```text
start_session(question="Design a rollback strategy for a risky migration.",
               mode="subagent",
               overrides={"subagent": {"engine": "manual"}})
  → session_id, mode="subagent"

begin_subagent_thought(session_id)
  → { "specialist": "Analysis", "specialist_prompt": "As the Analysis specialist…",
      "engine": "manual", "next_tool": "advance_subagent_round" }

advance_subagent_round(session_id,
    candidate="Roll back via feature-flagged dual-write with a 24h bake…",
    scores={"correctness":0.8,"evidence":0.7,"novelty":0.6,"clarity":0.7,
            "bias_resistance":0.6,"actionability":0.8,"coverage":0.7})
  → { "specialist": "Creativity", "specialist_prompt": "…" }        # next specialist

# … repeat for Creativity, then Skeptic …

advance_subagent_round(session_id, candidate="…", scores={…})       # last specialist
  → { "round_verdict": …, "next_tool": "commit_subagent_thought" }  # selection ran

commit_subagent_thought(session_id)
  → { "committed": true, "next_tool": "begin_subagent_thought" }
```

### 10b. The necort engine (`engine = "necort"`) — vendored Nash core

If you *do* have an OpenAI-compatible endpoint (Ollama, llama.cpp's server, vLLM, …), set `[subagent].endpoint` (or `endpoints` for several, fanned out concurrently) and keep `engine = "necort"`. Now the same four tools drive the **vendored Nash core** instead of the calling model.

- **`begin_subagent_thought`** builds a Nash prompt (compressed prior context + per-agent framing + stage weighting + lens scaffolding), runs **one bounded Nash negotiation** against the endpoint via the adapter, and keeps the strongest result. If multiple endpoints are configured, negotiations fan out concurrently and the best is kept.
- **`advance_subagent_round`** re-seeds the prior winner's content into a "build on and improve this" prompt and runs another bounded negotiation.
- Convergence inside the Nash core is capped hard by **`[subagent].max_rounds`** (default `2`) — *our* round budget wins even if the Nash core itself would keep going. A call past the cap returns `round_budget_exhausted` and points you at commit.
- **`inspect_utility_matrix`** and **`commit_subagent_thought`** work exactly as in manual mode (they're literally the same shared functions).

**The 3-of-7 honesty.** The Nash core produces a single blended 0–10 peer-rating per candidate. The adapter maps that onto only three dimensions — **`correctness`, `clarity`, `coverage`** — all set to the same value, and leaves the other four (`evidence`, `novelty`, `bias_resistance`, `actionability`) at the neutral sentinel `0.5`. It never fabricates signal it doesn't have. This has a real consequence for the commit gate (next).

### 10c. The commit gate (`equilibrium_threshold`)

`[subagent].equilibrium_threshold` (default `0.75`) is **our** commit-gate criterion — *not* the Nash core's internal matrix-diff epsilon (a different quantity entirely). It's the minimum strength the winning candidate must reach for the engine to report the equilibrium as strong enough to commit. Crucially, the **metric it's compared against differs per engine**:

- **necort** gates on the winner's **`correctness` dimension** — the one that carries real Nash signal. It deliberately does *not* gate on the 7-dim mean, because with 4 of 7 dims pinned at `0.5`, that mean is **structurally capped at `(3·1.0 + 4·0.5)/7 = 0.714`** and could never clear a `0.75` gate.
- **manual** gates on the **7-dim mean** — the same metric its own selection ranks by, which is honest here because all seven dims carry real signal.

Each engine's verdict wording names its own gate metric, so a reader is never misled about what "converged" measured.

### 10d. The no-endpoint safety net

`engine` defaults to `"necort"` but `endpoint` defaults to empty. So out of the box, calling `begin_subagent_thought` doesn't fail opaquely — it returns a `no_endpoint` directive that explains the situation and points you at the endpoint-free manual path (`engine = "manual"`). You're never stuck with a cryptic failure.

---

## 11. `next_action`: the north star

`next_action(session_id)` is the tool that makes the whole system drivable by a model that can't hold the protocol in its head. It reads the session's state and returns a `code` plus the exact `next_tool`. Here is its complete truth table.

Resolution order: mode-less first, then finalized/archived (mode-independent), then active-session per-mode dispatch.

| Session state | `code` | `next_tool` |
|---|---|---|
| No mode set | `mode_required` | `set_session_mode` |
| Finalized, no move/keep decision yet | `await_move_decision` | `move_session` (or `keep_here`) |
| Finalized, decision already made | `session_complete` | — |
| Archived | `session_archived` | — |
| **serial**: no thought, final stage | `loop_no_thought_final_stage` | `finalize_session` |
| **serial**: no thought, not final stage | `loop_no_thought_begin` | `begin_thought` (or `advance_stage`) |
| **serial**: thought open, no rounds | `loop_zero_rounds` | `critique_current_thought` |
| **serial**: awaiting critique text | `loop_await_critique` | `submit_critique` |
| **serial**: awaiting refinement | `loop_await_refine` | `refine_current_thought` |
| **serial**: awaiting score | `loop_await_score` | `score_current_thought` |
| **serial**: round complete, converged | `loop_converged` | `commit_thought` |
| **serial**: round complete, not converged | `loop_continue` | `critique_current_thought` |
| **subagent**: no thought, final stage | `subagent_no_thought_final_stage` | `finalize_session` |
| **subagent**: no thought, not final stage | `subagent_no_thought_begin` | `begin_subagent_thought` (or `advance_stage`) |
| **subagent** (manual): awaiting a specialist's candidate | `subagent_awaiting_specialist` | `advance_subagent_round` |
| **subagent**: converged (strength ≥ threshold) | `subagent_converged` | `commit_subagent_thought` |
| **subagent**: round budget spent | `subagent_budget_exhausted` | `commit_subagent_thought` |
| **subagent**: can run another round | `subagent_can_advance` | `advance_subagent_round` (or `commit_subagent_thought`) |

The `awaiting_specialist` state exists only for the manual engine (the necort engine has no per-specialist pause). **Design guidance:** when a model is driving a session, alternate between doing the named action and re-consulting `next_action`. You cannot get lost.

---

## 12. Sessions, persistence, and the finalize/move lifecycle

### The data root layout

```
~/deep-think-mcp/                (or $DEEP_THINK_HOME)
├── config.toml     seeded from config/default.toml on first use; edit freely
├── index.json      session_id → { path, mode, status, created_at, updated_at }
├── sessions/       one JSON file per session
│   └── <session_id>.json
├── lenses/         optional: drop-in .md critique lenses (override by name)
└── logs/           reserved directory (created on bootstrap; unused in v1)
```

### Persistence discipline

Every mutating tool call persists the session before returning. Writes use a **`.bak` protocol under a Portalocker file lock**: write a `.bak` sibling, then the new file, then remove the `.bak`; on load, if the main file is unreadable and a `.bak` exists, recover from it. This is what makes the store crash-safe. Storage faults (lock timeouts, corrupt JSON) are caught at the tool boundary by `storage_guard` and returned as a `storage_unavailable` directive — never a traceback.

### Finalize → move / keep

When reasoning is done:

1. **`finalize_session(session_id)`** marks the session finalized and returns a canned `human_prompt` — *"Your reasoning is saved at `<path>`. Would you like to move it elsewhere (a project folder, your Documents, etc.), or leave it where it is?"* — plus the two tools that answer it.
2. **`move_session(session_id, new_path, force=false)`** relocates the session file. It validates the destination (must be absolute; parent must exist and be a writable directory; won't clobber an existing file unless `force=true`) and moves **atomically**: write to the destination, verify it reads back correctly, *then* unlink the original. It records the move in `move_history` and updates the index. A session can be moved repeatedly; the index always points at the latest path.
3. **`keep_here(session_id)`** is a no-op that just records the "declined to move" decision for the audit trail.

Sessions moved **outside** the data root stay fully functional: `list_sessions` and `resume_session` find them via the index's absolute paths. This is what lets you tuck a finished analysis into a project folder or a synced drive (Dropbox, git) with zero special handling.

### Import / export

- **`export_session(session_id)`** returns the complete session state as a JSON-serializable dict.
- **`import_session(data)`** recreates a session from such a payload, validating it and reassigning a fresh `id` on collision. The imported `id` is sanitized so a crafted payload can't write a session file outside the sessions directory.

---

## 13. Tolerant input (built for weak models)

Every tool parameter that expects structure — lists (`tags`, `axioms`, `stages`, `challenged_assumptions`), score dicts (`scores`), booleans (`force`), override objects (`overrides`), and the import payload — accepts **either real JSON or a tolerant plaintext fallback**:

- `tags=["a","b","c"]` and `tags="a, b, c"` are equivalent.
- `scores={"correctness":0.8,"clarity":0.7}` and `scores="correctness: 0.8, clarity: 0.7"` are equivalent.
- A `` ```json … ``` ``-fenced object embedded in prose is unwrapped.
- Common weak-model JSON defects like a trailing comma (`{"correctness": 0.8,}`) are normalized before parsing.

When input genuinely can't be parsed, the tool does **not** raise. It returns a **`retry_with_clarification`** payload that names the exact parameter, the expected shape, and a concrete example — so a small model can fix its call and retry in a single step. And the parser fails *loudly* rather than silently: malformed input that looks like broken JSON is routed to `retry_with_clarification` rather than being coerced into a plausible-but-wrong value. (Tolerance lives strictly at the tool boundary; the engines themselves stay strict.)

---

## 14. Autopilot

Autopilot lets the **server itself drive a whole stage internally** against a configured local model, instead of the calling model stepping through the loop tool by tool. It is **off by default**, and when off it imports zero networking code (`httpx` is a lazy in-function import) — with autopilot disabled and no subagent endpoint configured, the server never touches the network at all.

When `[autopilot].enabled = true`, two extra tools register:

- **`run_stage_autopilot(session_id, stage?, initial_content?)`** (serial sessions) — the server runs draft → critique → refine → score internally, round after round, honoring all the normal convergence rules, then commits. Each generation is an endpoint call to the `[autopilot]` model.
- **`run_subagent_autopilot(session_id, stage?, initial_content?)`** (subagent sessions) — the same idea for whichever subagent engine is configured (`necort` or `manual`).

Crucially, autopilot **drives the exact same engine functions** the manual tools call — it's pure orchestration, so it can't drift from hand-driven behavior — and it persists after every committed thought. If the endpoint faults or the model emits unparseable output mid-run, autopilot stops cleanly with a **resumable partial-progress directive** (`autopilot_incomplete`): everything committed before the stop is already on disk, and `next_action` picks up manually from there. If autopilot is enabled but `httpx` isn't installed, the tools still register but return a clean `autopilot_unavailable` directive rather than crashing.

Enable it:

```bash
uv sync --extra autopilot
```

```toml
# ~/deep-think-mcp/config.toml
[autopilot]
enabled = true
endpoint = "http://localhost:11434/v1"   # any OpenAI-compatible /v1 endpoint
model = "qwen2.5:14b"
temperature = 0.7
```

Autopilot honors the session's mode: `run_stage_autopilot` on a subagent session (or vice versa) is rejected with a directive.

---

## 15. Complete configuration reference

Config is layered, lowest to highest precedence:

```
packaged defaults (config/default.toml)  <  user config (<root>/config.toml)  <  per-session overrides (start_session(overrides={…}))
```

The user config is seeded from the packaged defaults on first use — edit it directly. Per-session overrides let you change behavior for one session without touching any file.

| Section | Key | Default | Meaning |
|---|---|---|---|
| `[store]` | `root` | `"~/deep-think-mcp"` | Data root. `DEEP_THINK_HOME` overrides it and always wins. |
| `[store]` | `sessions_dir` | `"sessions"` | **Reserved** — not yet honored (v1 always uses `sessions/`). |
| `[store]` | `index_path` | `"index.json"` | **Reserved** — not yet honored (v1 always uses `index.json`). |
| `[modes]` | `default_prompt_user` | `true` | The server always surfaces the mode choice via the mode-required payload. |
| `[serial]` | `max_rounds` | `3` | Hard cap on critique rounds per thought; forces commit with `converged_reason: "max_rounds"`. |
| `[serial]` | `score_threshold` | `0.05` | Two consecutive rounds each improving less than this → `diminishing_returns` convergence. |
| `[serial]` | `edit_distance_epsilon` | `0.05` | Normalized edit distance (via `difflib`) below which a refinement is a `fixed_point` → converge. |
| `[serial]` | `fast_mode` | `false` | **Reserved** flag; not yet consumed. |
| `[serial]` | `default_lenses` | the 8 bundled lens names, in library order | Full rotation order after stage-appropriate defaults are exhausted. |
| `[subagent]` | `max_rounds` | `2` | Hard cap on *our* Nash/manual rounds per thought, enforced regardless of what the engine wants. |
| `[subagent]` | `equilibrium_threshold` | `0.75` | Commit gate. Compared against the winner's `correctness` dim (necort) or 7-dim mean (manual). |
| `[subagent]` | `agents` | `["Analysis", "Creativity", "Skeptic"]` | Specialist roster; each gets a default framing, weighted higher in its home stage. |
| `[subagent]` | `sequential_fallback` | `true` | With one endpoint, rounds run sequentially (same semantics as concurrent, longer wall-clock). |
| `[subagent]` | `engine` | `"necort"` | `"necort"` drives the vendored Nash core; `"manual"` is the endpoint-free path. |
| `[subagent]` | `endpoint` | `""` | Single OpenAI-compatible base URL for necort. Empty → the no-endpoint → manual directive. |
| `[subagent]` | `endpoints` | `[]` | Several base URLs, fanned out concurrently (wins over `endpoint` if both set). |
| `[subagent]` | `model` | `"qwen2.5:14b"` | Model name sent to the endpoint(s). |
| `[subagent]` | `api_key` | `""` | Optional bearer token for the endpoint(s). **Only sent to operator-configured endpoints** — a per-session override that redirects the endpoint runs keyless (a deliberate exfiltration guard). |
| `[subagent]` | `timeout` | `120.0` | Per-request HTTP timeout (seconds) for the necort endpoint call. |
| `[stages]` | `default` | `["Problem Definition","Research","Analysis","Synthesis","Conclusion"]` | Overridable per session via `start_session(stages=[…])`. |
| `[autopilot]` | `enabled` | `false` | Feature flag. When `false`, the two autopilot tools don't register and no network path is reachable. |
| `[autopilot]` | `endpoint` | `"http://localhost:11434/v1"` | OpenAI-compatible base URL for autopilot generations. |
| `[autopilot]` | `model` | `"qwen2.5:14b"` | Model name sent to the autopilot endpoint. |
| `[autopilot]` | `temperature` | `0.7` | Sampling temperature for autopilot generations. |

---

## 16. Complete tool reference

**25 tools always registered; 27 when `[autopilot].enabled = true`.** A `●` marks a required parameter, `○` optional.

### Session lifecycle (not mode-gated)

| Tool | Parameters | What it does |
|---|---|---|
| `start_session` | ● `question` · ○ `mode` · ○ `stages` · ○ `overrides` | Create a session and bootstrap the store. Proceeds if `mode` given, else returns `mode_required`. |
| `set_session_mode` | ● `session_id` · ● `mode` | Set the mode once; immutable thereafter. |
| `list_modes` | — | Return both modes' descriptions and recommendations. |
| `resume_session` | ● `session_id` | Return a session's persisted state. |
| `list_sessions` | — | List every session in the index. |
| `clear_session` | ● `session_id` | Delete a session (file + index entry). |
| `finalize_session` | ● `session_id` | Mark finalized; return the finalize/move prompt. |
| `move_session` | ● `session_id` · ● `new_path` · ○ `force` | Atomically move the session file to `new_path`. |
| `keep_here` | ● `session_id` | Record "declined to move" (no filesystem change). |

### Stage cursor (requires *any* mode)

| Tool | Parameters | What it does |
|---|---|---|
| `advance_stage` | ● `session_id` | Advance the stage cursor; at the last stage, points to `finalize_session`. |

### Serial thought loop (serial mode only)

| Tool | Parameters | What it does |
|---|---|---|
| `begin_thought` | ● `session_id` · ● `content` · ○ `tags` · ○ `axioms` | Draft a new thought in the current stage. |
| `critique_current_thought` | ● `session_id` · ○ `lens` | Open a critique round; returns the lens template (server-picked if omitted). |
| `submit_critique` | ● `session_id` · ● `text` | Record the critique for the open round. |
| `refine_current_thought` | ● `session_id` · ● `new_content` · ○ `challenged_assumptions` | Rewrite addressing the critique; returns edit distance. |
| `score_current_thought` | ● `session_id` · ○ `scores` | Self-score on 7 dims; returns the convergence verdict. |
| `commit_thought` | ● `session_id` | Lock the current thought; clear the cursor. |

### Subagent thought loop (subagent mode only)

| Tool | Parameters | What it does |
|---|---|---|
| `begin_subagent_thought` | ● `session_id` · ○ `content` · ○ `prompt_focus` | Start a subagent thought (necort runs round 1; manual hands specialist #1). |
| `advance_subagent_round` | ● `session_id` · ○ `candidate` · ○ `scores` | Advance a step (necort re-seeds+reruns; manual records + hands next specialist or the verdict). |
| `inspect_utility_matrix` | ● `session_id` | Read-only view of the latest round's candidate scores and equilibrium. |
| `commit_subagent_thought` | ● `session_id` | Accept the equilibrium; lock the winner; clear the cursor. |

### Meta / guidance / I-O (not mode-gated)

| Tool | Parameters | What it does |
|---|---|---|
| `next_action` | ● `session_id` | Authoritative resolver: the exact next tool for any state (see §11). |
| `summarize_session` | ● `session_id` · ○ `scope` (`"stage"`\|`"all"`) | Deterministic extractive digest of committed thoughts (no LLM). |
| `compress_history` | ● `session_id` · ○ `target_tokens` (default 300) | Budget-capped digest of *prior*-stage thoughts, for small context windows. |
| `export_session` | ● `session_id` | Full session state as a JSON-serializable dict. |
| `import_session` | ● `data` | Recreate a session from an export payload; collision-safe id reassignment. |

### Autopilot (only when `[autopilot].enabled = true`)

| Tool | Parameters | What it does |
|---|---|---|
| `run_stage_autopilot` | ● `session_id` · ○ `stage` · ○ `initial_content` | Serial only. Drive the whole critique loop internally to convergence, then commit. |
| `run_subagent_autopilot` | ● `session_id` · ○ `stage` · ○ `initial_content` | Subagent only. Drive the configured subagent engine internally, then commit. |

---

## 17. Directive and error code reference

Every tool response is a flat object. When something can't proceed, the payload carries a directive — never a traceback. These are the codes you'll see and what they mean.

### Mode & session

| Code | Fires when | Points to |
|---|---|---|
| `mode_required` | A thought/stage tool called on a mode-less session (or `start_session` without `mode`) | `set_session_mode` |
| `wrong_mode` | A mode-gated tool called on a session fixed to the other mode | (use the current mode's tools) |
| `mode_immutable` | `set_session_mode` called when the mode is already set | — |
| `session_not_found` | Any lookup on an unknown `session_id` | — |

### Input & storage

| Code | Fires when | Points to |
|---|---|---|
| `retry_with_clarification` | A structured param can't be parsed (JSON or plaintext) | the same tool (with the fix) |
| `storage_unavailable` | A lock timeout / OS error / corrupt-JSON fault at the tool boundary | — (retryable) |

### Serial sequencing (all carry `error: "sequencing"` + a `code`)

| `code` | Fires when | Points to |
|---|---|---|
| `begin_first` | Any loop tool with no in-flight thought | `begin_thought` |
| `uncommitted_exists` | `begin_thought` while a thought is already open | `commit_thought` |
| `need_critique` | `submit`/`refine`/`score` with no critique round open | `critique_current_thought` |
| `need_submit` | `refine`/`score` before submitting the critique | `submit_critique` |
| `empty_critique` | `submit_critique` with blank text | `submit_critique` |
| `need_refine` | `score` before refining | `refine_current_thought` |
| `empty_refinement` | `refine_current_thought` with blank content | `refine_current_thought` |
| `need_score` | `commit_thought` with an unscored round | `score_current_thought` |
| `zero_rounds` | `commit_thought` with no completed rounds | `critique_current_thought` |
| `unknown_lens` | `critique_current_thought(lens=…)` names a lens not in the library (payload adds `available_lenses`) | `critique_current_thought` |

### Subagent sequencing (also `error: "sequencing"` + `code`)

| `code` | Fires when | Points to |
|---|---|---|
| `begin_first` | Any subagent op with no in-flight thought | `begin_subagent_thought` |
| `uncommitted_exists` | `begin_subagent_thought` while a thought is open | `commit_subagent_thought` |
| `no_rounds` | `inspect`/`commit` with zero rounds recorded | `begin_subagent_thought` |
| `round_budget_exhausted` | `advance_subagent_round` past `max_rounds` | `commit_subagent_thought` |
| `need_candidate` | (manual) `advance_subagent_round` mid-round with no `candidate` | `advance_subagent_round` |

### Subagent engine faults

| Code | Fires when | Points to |
|---|---|---|
| `no_endpoint` | necort `begin`/`advance` with no endpoint configured | (message points at `engine="manual"`) |
| `adapter_error` | NECoRT network failure, malformed response, or vendored core unavailable | — |

### Move / import / autopilot

| Code | Fires when | Points to |
|---|---|---|
| `destination_not_absolute` / `destination_same_as_current` / `destination_parent_missing` / `destination_parent_not_a_directory` / `destination_not_writable` / `destination_exists` / `destination_write_failed` / `verification_failed` | `move_session` destination validation or write/verify failure | — |
| `index_update_failed` | Move succeeded on disk but the index update raised | — |
| `invalid_json` / `invalid_session_data` | `import_session` given unparseable JSON, a non-object, or a payload failing validation | — |
| `autopilot_incomplete` | Autopilot endpoint fault or unparseable output mid-run (partial progress persisted) | `next_action` |
| `autopilot_unavailable` | Autopilot enabled but `httpx` not installed | `next_action` |
| `stage_mismatch` | `run_*_autopilot(stage=…)` names a stage ≠ the current stage | `advance_stage` |

### Stage cursor

| Payload key | Meaning | Points to |
|---|---|---|
| `final_stage: true` | `advance_stage` at the last stage | `finalize_session` |

---

## 18. Data model reference

All models are Pydantic with `extra="forbid"` (unknown fields are rejected, so a typo fails loudly).

```
UtilityScore
  correctness, evidence, novelty, clarity, bias_resistance, actionability, coverage
  — each: float in [0.0, 1.0]

CritiqueRound                          (serial mode)
  round_index: int
  lens: str
  critique_text: str
  refined_content: str
  delta_score: float                   (sentinel -2.0 = "unscored"; real range [-1, 1])

SpecialistRound                        (subagent mode)
  round_index: int
  agent_role: str
  candidate_content: str
  utility_vector: UtilityScore
  equilibrium_state: str               ("pending" | "in_equilibrium" | "off_equilibrium")
  was_selected: bool

Thought
  id: str                              (uuid4 hex)
  stage: str
  position: int
  timestamp: datetime
  content: str                         (set to the final/winning text at commit)
  tags: list[str]
  axioms: list[str]
  challenged_assumptions: list[str]
  critique_rounds: list[CritiqueRound]     (serial)
  specialist_rounds: list[SpecialistRound] (subagent)
  final_utility_scores: UtilityScore | None
  committed: bool = False

MoveRecord                             (audit)
  from_path: str, to_path: str, timestamp: datetime, unlink_failed: bool = False

DecisionRecord                         (audit — e.g. keep_here)
  action: str, timestamp: datetime

Session
  id: str                              (uuid4 hex)
  question: str
  created_at: datetime
  mode: "serial" | "subagent" | None
  expected_stages: list[str]
  current_stage: str
  current_thought_id: str | None
  status: "active" | "finalized" | "archived" = "active"
  finalized_at: datetime | None
  save_path: str
  overrides: dict                      (raw per-session config overrides)
  move_history: list[MoveRecord]
  thoughts: list[Thought]
  decisions: list[DecisionRecord]
```

---

## 19. Extending the system

### Add or override a critique lens

Drop a Markdown file into `~/deep-think-mcp/lenses/`. The stem becomes the lens name; the file content is the critique-prompt template. A same-named file overrides the bundled lens entirely; a new name adds a lens. The loader discovers `.md` files at startup, user directory winning on name collision. A good lens template is directive: tell the model exactly what failure mode to attack in "the draft thought above" and exactly what to produce — short and imperative, so a 7B model can follow it. Study the bundled lenses in `src/deep_think_mcp/lenses/` as templates.

### Customize stages per session

Pass `stages` to `start_session`:

```text
start_session(question="…", mode="serial",
              stages=["Frame", "Evidence", "Options", "Decision"])
```

Custom stages that aren't in the built-in stage→lens table simply fall back to the full `default_lenses` rotation, and to neutral agent weights.

### Change the specialist roster

Set `[subagent].agents` (in `config.toml` or per-session overrides). Each name gets a default framing; the two "home stage" weightings (Analysis→Analysis, Creativity→Synthesis) apply only to those names. Adding your own specialist name works — it just gets a generic framing and neutral weight.

### Point at your own inference endpoint

For the necort engine, set `[subagent].endpoint` (or `endpoints` for concurrent fan-out) to any OpenAI-compatible `/v1` base URL — Ollama, llama.cpp's server, vLLM, etc. For autopilot, set `[autopilot].endpoint`. Note the credential guard: an `api_key` is only sent to operator-configured endpoints; a per-session override that redirects the endpoint runs keyless.

### Re-pin the vendored NECoRT core

See `docs/repinning_necort.md`. In short: fetch the new commit into the submodule (remember pull-request refs need an explicit fetch spec), check it out, run the adapter test suite, and commit the new gitlink SHA. Because `necort_adapter.py` is the only file importing vendored code, that's the only place a re-pin can break.

---

## 20. Testing and development

```bash
uv run pytest            # full suite
uv run pytest -q -W error   # the CI bar: pristine, warnings are errors
```

The suite (423 tests) drives the **real MCP SDK's in-memory client against the real server object** for every tool contract, plus one subprocess test that launches the actual `uv run python -m deep_think_mcp.server` command and speaks real stdio MCP to it. Tests never touch your real home directory — each injects a `tmp_path` data root (or `DEEP_THINK_HOME`), so you can run the suite freely.

**How the codebase was built.** The project was implemented task-by-task with a "fresh implementer → adversarial spec+quality review → fix loop" discipline; the per-task review ledger is at `.superpowers/sdd/progress.md`, and the design/spec docs are `docs/build-plan.md` (the original design, copied from an Obsidian vault) and `docs/execution-plan.md` (the task breakdown with global constraints). A final whole-branch multi-lens review with adversarial verification of every finding closed out the build; its findings and fixes are recorded under `.superpowers/sdd/`. If you're modifying the code, that ledger tells you which invariants each module is responsible for and which known-minor items were consciously accepted.

Dependency notes live in `docs/necort_deps.md`; client wiring in `docs/wiring.md`; third-party attribution in `LICENSE-NOTICES`.

---

## 21. Design philosophy: why it looks the way it does

Five principles explain almost every non-obvious choice.

1. **Local models are the target, not an afterthought.** Small context, weak instruction-following, no reliable JSON mode. Consequences everywhere: flat tool signatures (no nested required objects); short directive responses; `next_action` as an authoritative crutch; tolerant JSON-or-plaintext input; aggressive convergence caps (slow inference deserves early stops); `compress_history` to fit prior stages into a small window.

2. **Never a raw error.** A weak model can't recover from a traceback. Every failure path returns a directive that says what's wrong and what to call next. Sequencing mistakes, parse failures, storage faults, missing endpoints, autopilot faults — all degrade to instructions, not exceptions.

3. **The server holds the protocol, not the model.** State lives in the persisted session and is *derived*, not tracked in a parallel structure that could drift. The serial loop's phase is derived from the critique rounds; the subagent round count is derived from the was-selected markers. This is why `next_action` can always tell you the truth — it's reading the same state the engines act on.

4. **Honesty over polish.** The system refuses to fabricate signal it doesn't have. The necort engine leaves 4 of 7 utility dimensions at a neutral sentinel because the Nash core genuinely can't score them, and the commit gate is engineered around that cap rather than pretending the mean is meaningful. The README tells the real NECoRT story — that most of PR #7 is filler — rather than the flattering one. Benchmarks are deferred rather than shipped as a self-graded number.

5. **Quarantine the third-party mess.** All vendored code sits behind one adapter, shimmed without editing a vendored line, pinned to an exact commit, re-pinnable by a documented process. The rest of the codebase never imports it.

---

## 22. Troubleshooting & FAQ

**"`begin_subagent_thought` returns `no_endpoint`."** Expected out of the box: `engine` defaults to `necort` but no endpoint is configured. Either set `[subagent].engine = "manual"` (endpoint-free) or configure `[subagent].endpoint`. You can do either per session via `overrides={"subagent": {"engine": "manual"}}`.

**"I want to switch a session from serial to subagent."** You can't — mode is immutable by design. Start a new session with the other mode. (Run the same question through both and compare; both emit the same schema.)

**"A tool returned a `sequencing` directive."** You called a loop tool out of order. Read its `next_tool` and call that, or just call `next_action(session_id)` and follow it. You cannot get stuck.

**"The submodule is empty / necort tests are skipped."** You cloned without `--recurse-submodules`. Run `git submodule update --init`. If you never use the necort engine, you can ignore this — everything else works without it.

**"Does it need a GPU / an API key / the network?"** No, in the default configuration. Serial mode and the manual subagent engine are fully local and offline. The network is touched only by (a) the necort engine against a configured endpoint, or (b) autopilot when enabled. With neither configured, the server never makes an outbound call.

**"Where did my session go after `move_session`?"** Wherever you moved it — and it's still fully usable there. `list_sessions` and `resume_session` find it via the index's absolute path. `move_history` records every move.

**"Can I edit the config while the server is running?"** Edit `~/deep-think-mcp/config.toml`; it's read through the layered loader. Per-session overrides (`start_session(overrides={…})`) change one session without touching any file.

**"How do I fit a long session into a small context window?"** Use `compress_history(session_id, target_tokens)` to get a budget-capped extractive digest of the prior stages, and `summarize_session` for a stage or whole-session summary. Both are deterministic — no LLM call.

**"Autopilot enabled but nothing happens / `autopilot_unavailable`."** You need the extra dependency: `uv sync --extra autopilot`. The tools register even without `httpx` but return `autopilot_unavailable` until it's installed.

---

## 23. Glossary

- **Session** — one reasoning effort about one question; the top-level persisted unit.
- **Mode** — `serial` or `subagent`, chosen at creation and immutable.
- **Stage** — an ordered phase of reasoning (Problem Definition → … → Conclusion); a cursor points at the active one.
- **Thought** — one committed unit of reasoning inside a stage.
- **Critique round** (serial) — one lens's critique + the refinement it produced + its score delta.
- **Specialist round** (subagent) — one specialist's candidate + its 7-dim utility vector + whether it was selected.
- **Lens** — a named critique-prompt template targeting one failure mode (e.g. `weak_evidence`).
- **Specialist** — a named perspective (e.g. Analysis, Creativity, Skeptic) that proposes a candidate in subagent mode.
- **Utility score** — the shared 7-dimension `[0,1]` scoring vector (`correctness, evidence, novelty, clarity, bias_resistance, actionability, coverage`).
- **Engine** — the machinery behind a mode. Serial has one engine; subagent has two (`necort`, `manual`).
- **Nash core** — the vendored `NashEquilibriumRecursiveChat` that the `necort` engine drives.
- **Directive** — a structured tool response that tells the model what to do next instead of raising an error.
- **`next_action`** — the authoritative tool that returns the exact next call for any session state.
- **Data root** — the on-disk directory holding config, index, sessions, lenses, logs (`~/deep-think-mcp/` by default).
- **Commit gate** — the `equilibrium_threshold` check deciding whether a subagent round is strong enough to commit.
- **Tolerant input** — the boundary layer that accepts JSON or loose plaintext for structured parameters.

---

*This guide describes deep-think-mcp v1. For the original design rationale see `docs/build-plan.md`; for the task-level build record see `docs/execution-plan.md` and `.superpowers/sdd/progress.md`.*
