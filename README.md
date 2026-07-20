# deep-think-mcp

An [MCP](https://modelcontextprotocol.io) server that unifies two reasoning
disciplines behind one tool surface: staged, persistent, sequential-thinking
(à la the classic "Sequential Thinking" MCP) and recursive self-critique
(à la Chain-of-Recursive-Thoughts / NECoRT). It is built for **local models**
— small context, weaker instruction-following, no reliable JSON mode — so
every tool response is short, flat, and directive: it tells the calling
model exactly what to do next rather than assuming it will infer the right
move.

Every session picks one of two execution modes, fixed for the life of the
session:

- **serial** — one line of reasoning, critiqued step by step by rotating
  critique lenses (`overconfidence`, `weak_evidence`,
  `missing_perspective`, ...). Every intermediate draft, critique, and
  refinement stays visible in the session record. Best for single-GPU
  setups, small local models (7B/8B), and anyone who wants transparent,
  inspectable reasoning.
- **subagent** — specialist perspectives (Analysis, Creativity, Skeptic, ...)
  propose competing candidate thoughts, scored on a 7-dimension utility
  matrix and converged on a winner. Best for harder questions where diverse
  framings matter.

**The honest NECoRT story.** `docs/build-plan.md` originally imagined
subagent mode as a full port of PR #7 to
[PhialsBasement/Chain-of-Recursive-Thoughts](https://github.com/PhialsBasement/Chain-of-Recursive-Thoughts)
— specialist agents, a native 7-dimension utility matrix, bias detection,
continuous learning. A code recon during the build
(`.superpowers/sdd/necort-recon.md`) found that most of that PR is
disconnected filler: the `enhanced-implementations/` files that advertise
those features are never imported anywhere, make zero LLM calls, and (for
several) aren't even validly importable Python (hyphenated filenames, no
`__init__.py`). The one part of the PR that actually works is
`NashEquilibriumRecursiveChat` — a real Nash-equilibrium-style recursive
chat core in `recursive_thinking_ai.py` / `nash_recursive_thinking.py`. So
this project vendors PR #7 in full (so the submodule stays a faithful,
re-pinnable mirror of upstream) but *imports* only those two files, wrapped
by a single adapter (`necort_adapter.py`) that shims a real crash bug, a
hardcoded OpenRouter endpoint, and a stdout-corrupts-the-MCP-transport bug —
without editing a single vendored line. Because a single blended 0–10 Nash
rating can only ever inform 3 of the 7 utility dimensions (the other 4 sit
at a neutral 0.5), genuine multi-perspective diversity comes from a second,
**endpoint-free** subagent path built for this project from scratch: the
*manual specialist engine*, where the calling model plays each specialist
itself and self-scores all 7 dimensions for real. Subagent mode therefore
has two engines, chosen in config (`[subagent] engine = "necort" | "manual"`):
`necort` drives the vendored Nash core against an OpenAI-compatible
endpoint; `manual` needs no endpoint, no network, and no vendored code at
all, and is where a session lands automatically if no endpoint is
configured.

Both modes emit the same session schema (a stage machine, thoughts, 7-dim
utility scores, an audit trail), persist to disk by default, and are driven
by the same small, tolerant tool surface — 25 tools normally, 27 with the
optional autopilot feature enabled (see below).

## Install

Requires Python ≥3.11 and [`uv`](https://docs.astral.sh/uv/). This project
vendors NECoRT's working core as a git submodule, so clone with submodules:

```bash
git clone --recurse-submodules <this-repo-url> deep-think-mcp
cd deep-think-mcp
```

(If you already cloned without `--recurse-submodules`, run
`git submodule update --init` instead.) The submodule is only needed for
`[subagent] engine = "necort"`; the rest of the server — serial mode, the
manual subagent engine, everything else — works with it uninitialized.

Then install dependencies:

```bash
uv sync
```

This installs the core dependency set (`mcp`, `pydantic`, `portalocker`,
plus `requests`/`numpy`/`openai` — needed to *import* the two vendored
NECoRT modules even though this project's own code never calls the OpenAI
SDK; see `docs/necort_deps.md`). The optional autopilot feature needs one
more dependency (`httpx`), pulled in via the `autopilot` extra:

```bash
uv sync --extra autopilot
```

Run the test suite to confirm the install is healthy:

```bash
uv run pytest
```

**Launching the server.** The stdio entrypoint is:

```bash
uv run python -m deep_think_mcp.server
```

This is a dev-checkout tool: it reads `config/default.toml` from the repo
root next to `src/`, not from an installed wheel, so every wiring config
(see `docs/wiring.md`) points `--directory` (or `cwd`) at your cloned repo
path rather than assuming the package is `pip install`-able from PyPI.

## Quickstart

Every deep-think-mcp tool returns a small flat JSON object with a `message`
field (what happened, human-readable) and usually a `next_tool` field
(the exact next call to make) — treat `next_action(session_id)` as the
source of truth any time you're unsure what to call next.

### Serial mode walkthrough

```
start_session(question="Should we cache API responses at the edge or origin?")
  -> {"mode_required": true, "modes": [...], "next_tool": "set_session_mode", ...}

set_session_mode(session_id, mode="serial")
  -> {"mode": "serial", "message": "Mode set to 'serial'. This is permanent ..."}

begin_thought(session_id, content="Cache at the edge: lower latency, ...")
  -> {"next_tool": "critique_current_thought", ...}

critique_current_thought(session_id)
  -> {"lens": "weak_evidence", "draft_content": "...", "lens_template": "...",
      "next_tool": "submit_critique", ...}

submit_critique(session_id, text="No numbers backing the latency claim ...")
  -> {"next_tool": "refine_current_thought", ...}

refine_current_thought(session_id, new_content="Cache at the edge (CDN PoPs ...")
  -> {"edit_distance": 0.31, "next_tool": "score_current_thought", ...}

score_current_thought(session_id, scores={"correctness": 0.8, "evidence": 0.7,
    "novelty": 0.5, "clarity": 0.8, "bias_resistance": 0.6,
    "actionability": 0.7, "coverage": 0.6})
  -> {"converged": false, "next_tool": "critique_current_thought", ...}
  # (repeat critique -> submit -> refine -> score until converged, or
  # max_rounds=3 forces a commit)

commit_thought(session_id)
  -> {"committed": true, "next_tool": "begin_thought", ...}

advance_stage(session_id)     # once the stage's thought(s) are done
  -> {"current_stage": "Research", ...}

# ... repeat through Analysis / Synthesis / Conclusion ...

finalize_session(session_id)
  -> {"human_prompt": "Your reasoning is saved at `~/deep-think-mcp/sessions/<id>.json`.
       Would you like to move it elsewhere (a project folder, your Documents,
       etc.), or leave it where it is?", "available_tools": [...]}

move_session(session_id, new_path="~/Documents/edge-cache-decision.json")
  # or: keep_here(session_id)
```

`begin`/`critique`/`submit`/`refine`/`score`/`commit` accept `tags`,
`axioms`, `challenged_assumptions`, and `scores` in either JSON or tolerant
plaintext form (e.g. `scores="correctness: 0.8, clarity: 0.7"`) — a weak
local model that can't reliably emit JSON still works. Omit `lens` in
`critique_current_thought` to let the server pick a stage-appropriate lens
and rotate through the library automatically.

### Subagent mode walkthrough (manual engine — no endpoint needed)

`[subagent].engine` defaults to `"necort"`, but with no endpoint configured
(the shipped default), `begin_subagent_thought` doesn't fail opaquely — it
returns a directive pointing at the endpoint-free manual path:

```
begin_subagent_thought(session_id)
  -> {"error": "no_endpoint", "message": "Subagent mode has no NECoRT endpoint
       configured ... use the endpoint-free manual specialist path by setting
       [subagent] engine=\"manual\" ..."}
```

Set `[subagent] engine = "manual"` (either in `~/deep-think-mcp/config.toml`,
or per-session via `start_session(..., overrides={"subagent": {"engine": "manual"}})`),
then:

```
start_session(question="Design a rollback strategy for a risky migration.",
               mode="subagent",
               overrides={"subagent": {"engine": "manual"}})
  -> session_id, mode="subagent"

begin_subagent_thought(session_id)
  -> {"specialist": "Analysis", "specialist_prompt": "...", "engine": "manual",
      "next_tool": "advance_subagent_round", ...}
      # the calling model now plays "Analysis": produce a candidate + self-score
      # it on all 7 dimensions

advance_subagent_round(session_id,
    candidate="Roll back via feature-flagged dual-write with a 24h ...",
    scores={"correctness": 0.8, "evidence": 0.7, "novelty": 0.6,
            "clarity": 0.7, "bias_resistance": 0.6, "actionability": 0.8,
            "coverage": 0.7})
  -> {"specialist": "Creativity", "specialist_prompt": "...", ...}
      # repeat for each specialist in [subagent].agents (default: Analysis,
      # Creativity, Skeptic) -- when the roster is exhausted, the server runs
      # the deterministic selection (highest mean of the 7 scores wins) and
      # returns the round verdict instead of another specialist prompt

inspect_utility_matrix(session_id)   # optional: see every candidate's scores

commit_subagent_thought(session_id)
  -> {"committed": true, "next_tool": "begin_subagent_thought", ...}
```

If you *do* have an OpenAI-compatible endpoint to point at (Ollama,
llama.cpp's server, vLLM, ...), set `[subagent].endpoint` (or `endpoints`
for several, fanned out concurrently) and leave `engine = "necort"`; the
same four tools (`begin_subagent_thought`, `advance_subagent_round`,
`inspect_utility_matrix`, `commit_subagent_thought`) then drive the vendored
Nash core instead, capped at `[subagent].max_rounds` US rounds regardless of
how many rounds the Nash core itself would want to run.

## Mode-selection contract

- `start_session(question, mode=None, ...)` without `mode` creates the
  session and returns a `mode_required` directive: both modes' one-line
  descriptions plus `next_tool: "set_session_mode"`. Pass a valid `mode`
  directly to `start_session` to skip the extra round trip.
- `set_session_mode(session_id, mode)` sets the mode once; a second call on
  the same session is rejected (`mode_already_set`) — mode is **immutable**
  for the life of a session. Start a new session to use the other mode.
- Every thought-loop tool (`begin_thought`, `begin_subagent_thought`, ...)
  and `advance_stage` are gated centrally: called on a mode-less session
  they return `mode_required`; called with the wrong mode's tool (e.g. a
  serial tool on a subagent session) they return `wrong_mode`. Neither case
  ever reaches the engine or corrupts session state.

## Data & the finalize/move UX

Everything lives under a single data root, `~/deep-think-mcp/` by default:

```
~/deep-think-mcp/
├── config.toml    # seeded from config/default.toml on first use; edit freely
├── index.json     # session_id -> {path, mode, status, created_at, updated_at}
├── sessions/       # one JSON file per session
│   └── <session_id>.json
├── lenses/         # optional: drop-in .md critique lenses (see below)
└── logs/           # reserved directory, created on bootstrap; unused in v1
```

Override the root with the `DEEP_THINK_HOME` environment variable (highest
precedence — see `docs/wiring.md` for setting it per MCP-client config).
Resolution order: `DEEP_THINK_HOME` → `[store].root` in the packaged
defaults → `~/deep-think-mcp`.

`finalize_session(session_id)` marks a session finalized and returns a
canned `human_prompt` — *"Your reasoning is saved at `<path>`. Would you
like to move it elsewhere (a project folder, your Documents, etc.), or
leave it where it is?"* — plus the two tools that answer it:
`move_session(session_id, new_path, force=false)` (fails cleanly if the
destination exists without `force=true`, isn't writable, or its parent
doesn't exist; moves are atomic — write, verify, then unlink the original)
and `keep_here(session_id)` (a no-op that just records the decision).
Sessions moved outside the data root stay fully functional afterward:
`list_sessions` / `resume_session` find them via `index.json`'s absolute
paths, and a session can be moved more than once (`move_history`
accumulates; the index always tracks the latest path).

## Critique lenses

Serial mode ships 8 bundled lenses in `src/deep_think_mcp/lenses/`
(`overconfidence`, `weak_evidence`, `missing_perspective`,
`unstated_assumption`, `scope_creep`, `alternative_framing`, `steel_man`,
`first_principles`), each a real critique-prompt template, not a stub. Drop
your own `.md` files into `~/deep-think-mcp/lenses/` to add or override
lenses by name — a same-named file there replaces the bundled one entirely.

## Tolerant input

Every tool that accepts structured input (lists, score dicts, booleans,
override objects) accepts **either real JSON or a tolerant plaintext
fallback** — `tags="a, b, c"` works exactly like `tags=["a", "b", "c"]`,
and `scores="correctness: 0.8, clarity: 0.7"` works exactly like the
equivalent JSON object. Input that genuinely can't be parsed never raises a
raw error: it returns a `retry_with_clarification` payload naming the exact
parameter, the expected shape, and a concrete example, so a weak local
model can fix its call and retry in one step.

## Autopilot (optional)

Off by default and, when off, imports zero networking code (`httpx` stays a
lazy, in-function import) — the server never touches the network unless
autopilot is explicitly enabled. When `[autopilot].enabled = true`, two
extra tools register: `run_stage_autopilot(session_id, stage=None,
initial_content=None)` drives the whole serial critique loop for the
current stage internally against the configured `[autopilot]` endpoint
(draft → critique → refine → score, repeated to convergence, then commit);
`run_subagent_autopilot(session_id, ...)` does the same for whichever
subagent engine is configured (`necort` or `manual`). Both stop cleanly
with a resumable partial-progress directive on an endpoint fault or
unparseable model output — everything committed before the stop is already
on disk, and `next_action(session_id)` picks up manually from there.

Install the extra dependency and enable it in your config:

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

If autopilot is enabled but `httpx` isn't installed, the two tools still
register but return a clear `autopilot_unavailable` directive instead of
crashing — no traceback.

## Configuration reference

Config layers, lowest to highest precedence: packaged defaults
(`config/default.toml`) → user config (`<root>/config.toml`, seeded from the
packaged defaults on first use — edit it directly) → per-session overrides
(`start_session(..., overrides={...})`). Every key below and its shipped
default, generated from `config/default.toml`:

| Section | Key | Default | Notes |
|---|---|---|---|
| `[store]` | `root` | `"~/deep-think-mcp"` | Overridden by `DEEP_THINK_HOME`, which always wins. |
| `[store]` | `sessions_dir` | `"sessions"` | Resolved relative to `[store].root`. |
| `[store]` | `index_path` | `"index.json"` | Resolved relative to `[store].root`. |
| `[modes]` | `default_prompt_user` | `true` | The server always asks the user to pick a mode via `start_session`'s mode-required payload, even if this were `false`. |
| `[serial]` | `max_rounds` | `3` | Hard cap on critique rounds per thought; forces commit with `converged_reason: "max_rounds"`. |
| `[serial]` | `score_threshold` | `0.05` | A round's overall-score improvement below this (and the round before it, two in a row) triggers convergence. |
| `[serial]` | `edit_distance_epsilon` | `0.05` | Normalized edit distance (via `difflib`) below which a refinement counts as a fixed point → converged. |
| `[serial]` | `fast_mode` | `false` | Reserved flag; not yet consumed by the engine. |
| `[serial]` | `default_lenses` | the 8 bundled lens names, in library order | Full rotation order once stage-appropriate defaults (`stages.py`) are exhausted. |
| `[subagent]` | `max_rounds` | `2` | Hard cap on US (our) Nash/manual rounds per thought, enforced regardless of what the engine itself would prefer. |
| `[subagent]` | `equilibrium_threshold` | `0.75` | Commit-gate threshold. Compared against the winner's `correctness` dim for `engine="necort"` (its only real Nash signal — the 7-dim mean is structurally capped at 0.714 and could never clear 0.75), or the 7-dim mean for `engine="manual"` (real signal on all 7 dims there). |
| `[subagent]` | `agents` | `["Analysis", "Creativity", "Skeptic"]` | The specialist roster. Each gets a default framing (`prompts.SPECIALIST_FRAMINGS`); an unlisted name gets a generic framing. |
| `[subagent]` | `sequential_fallback` | `true` | With one endpoint configured, rounds run sequentially (identical semantics to concurrent, longer wall-clock). |
| `[subagent]` | `engine` | `"necort"` | `"necort"` drives the vendored Nash core; `"manual"` is the endpoint-free path where the calling model plays each specialist. |
| `[subagent]` | `endpoint` | `""` (empty) | Single OpenAI-compatible base URL for the NECoRT engine. Empty = no endpoint configured → `begin_subagent_thought` directs the caller to the manual path instead of failing. |
| `[subagent]` | `endpoints` | `[]` (empty) | Several base URLs, fanned out concurrently per round (wins over `endpoint` if both are set). |
| `[subagent]` | `model` | `"qwen2.5:14b"` | Model name sent to the configured endpoint(s). |
| `[subagent]` | `api_key` | `""` (empty) | Optional bearer token sent to the endpoint(s). |
| `[subagent]` | `timeout` | `120.0` | Per-request HTTP timeout (seconds) for the NECoRT endpoint call. |
| `[stages]` | `default` | `["Problem Definition", "Research", "Analysis", "Synthesis", "Conclusion"]` | Overridable per-session via `start_session(stages=[...])`. |
| `[autopilot]` | `enabled` | `false` | Feature flag. When `false`, the two autopilot tools don't register and no network code path is reachable. |
| `[autopilot]` | `endpoint` | `"http://localhost:11434/v1"` | Any OpenAI-compatible `/v1`-style base URL. |
| `[autopilot]` | `model` | `"qwen2.5:14b"` | Model name sent to the autopilot endpoint. |
| `[autopilot]` | `temperature` | `0.7` | Sampling temperature for every autopilot-driven generation. |

## Wiring into an MCP client

See [`docs/wiring.md`](docs/wiring.md) for exact, copy-pasteable config
snippets for Claude Desktop, Claude Code, Cursor, Continue, and LibreChat.

## Testing

```bash
uv run pytest
```

The suite (404 tests as of this writing) drives the real MCP SDK's
in-memory client against the real server object for every tool contract,
plus one subprocess test that launches the actual
`uv run python -m deep_think_mcp.server` command and speaks real stdio MCP
to it. Tests never touch the real home directory — every test injects a
`tmp_path` data root (or `DEEP_THINK_HOME`).

## Benchmarks

Not yet run. A head-to-head comparison of serial vs. subagent mode on three
canonical prompts is planned (per `docs/build-plan.md`'s M7 milestone) but
requires blind human rating to be meaningful, and is deliberately deferred
past this v1 build rather than shipped as a self-graded number.

## License

MIT — see [`LICENSE`](LICENSE). This project vendors third-party source
code (`vendor/necort/`, a git submodule of
[PhialsBasement/Chain-of-Recursive-Thoughts](https://github.com/PhialsBasement/Chain-of-Recursive-Thoughts)
PR #7) under its own MIT license; see [`LICENSE-NOTICES`](LICENSE-NOTICES)
for the full attribution and scoping notes.
