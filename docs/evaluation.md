# Evaluation Runbook — does the tool actually help?

**Purpose.** A repeatable A/B/C experiment that measures whether driving a model
through deep-think-mcp produces *better reasoning* than the same model answering
directly — on a hard problem, graded blind, against a rubric that is independent
of the tool's own scores.

This document is written to be **executed by an agent** (or a human) end to end.
It is self-contained: the test prompt, the rubric, the judge instructions, the
output layout, and the report template are all here.

---

## 0. For the agent running this test — start here

You are the **conductor**. Your job is to run the experiment and produce a
report. **Critical rule: you generate none of the reasoning being tested.**
Every answer, thought, critique, refinement, and score in every arm must come
from the **model under test (MUT)** — the local model this experiment is about.
You only orchestrate, relay, collect, blind, and (optionally) invoke the judge.

Do this, in order:

1. **Fill in the [Parameters](#1-parameters)** block by asking the human for
   anything you don't know (MUT endpoint, model name, judge model). Write it to
   `eval-runs/<run-id>/params.json`.
2. **Pick the tool-arm version** — [§2](#2-choose-the-with-tool-version): 
   **Conductor** (default, reproducible) or **Autopilot** (server-driven). You
   may run both.
3. **Run the three arms** ([§4](#4-run-the-arms)), `N` trials each, writing raw
   outputs to `eval-runs/<run-id>/raw/`.
4. **Blind the outputs** ([§5](#5-blind-the-outputs)) into
   `eval-runs/<run-id>/blind/` + a private `key.json`.
5. **Grade** ([§6](#6-grade)) with the judge model into `scores/`, and have the
   human spot-check a sample.
6. **Aggregate + interpret** ([§7](#7-aggregate-and-interpret)) into
   `aggregate.json`.
7. **Write the report** ([§8](#8-report-template)) to
   `eval-runs/<run-id>/report.md` and hand the human the headline finding.

**Definition of done:** `report.md` exists, states per-arm means with variance,
the premise-challenged rates, and a one-line verdict following the decision
rules in §7. Hand the human the verdict + the path to the report.

**Output tree** (write everything under one run directory; `eval-runs/` is
git-ignored so runs never pollute the repo):

```
eval-runs/<run-id>/            # <run-id> = a human-supplied label or timestamp you are given
├── params.json               # the filled Parameters block
├── raw/  A-1.txt … C-3.txt   # final blocks, arm-labeled (PRIVATE — do not grade from here)
├── blind/ response-01.txt …  # shuffled + relabeled copies (what the judge sees)
├── key.json                  # response-NN → {arm, trial}
├── scores/ response-01.json… # judge output per response (avg of 3 judge runs)
├── aggregate.json            # per-arm means, ranges, premise rates
└── report.md                 # the writeup
```

> This runbook uses **serial mode** — the critique loop — because the test is a
> single hard reasoning problem. (An optional subagent-mode Arm D is sketched in
> the [Appendix](#appendix).)

---

## 1. Parameters

Collect these and save as `params.json`. Ask the human for anything unknown.

```json
{
  "run_id": "REPLACE — label or timestamp",
  "mut": {
    "name": "REPLACE — e.g. the local Qwen 27B under test",
    "endpoint": "REPLACE — OpenAI-compatible /v1 base URL, e.g. http://localhost:8080/v1",
    "api_key": "",
    "temperature": 0.7,
    "top_p": 0.9,
    "max_tokens": 4096
  },
  "judge": {
    "name": "REPLACE — a DIFFERENT, stronger model than the MUT (avoid self-preference)",
    "endpoint": "REPLACE",
    "temperature": 0.0
  },
  "trials_per_arm": 3,
  "with_tool_version": "conductor",   // or "autopilot", or "both"
  "stages": ["Problem Definition", "Research", "Analysis", "Synthesis", "Conclusion"],
  "notes": "Use the SAME mut.temperature/top_p/max_tokens across ALL arms — otherwise you are testing sampling, not the tool."
}
```

**Fairness invariants (do not violate):**
- Same MUT, same decoding params, in every arm. The only difference is the
  arm-specific instruction / the tool.
- `max_tokens` must be generous enough that no arm is truncated mid-answer.
- The **judge model must not be the MUT** (self-preference bias).
- **Lite option** (faster, for a 27B or for iteration): set
  `stages = ["Analysis", "Synthesis", "Conclusion"]`. Note it in the report.

---

## 2. Choose the with-tool version

Arms A and B (no tool) are identical regardless. Only **Arm C** (the tool arm)
has two runnable versions. Pick one (or run both and report separately).

| Version | How Arm C runs | Use when |
|---|---|---|
| **Conductor** (default) | You (the agent) follow `next_action`, and for each step you send the tool's directive/lens/draft to the **MUT endpoint**, take the MUT's text, and submit it back through the matching tool. The MUT produces all content; you never reason about the problem. | You want a reproducible, deterministic run that works for any model size and isolates *reasoning-under-structure* from the MUT's tool-calling ability. |
| **Autopilot** | You enable deep-think-mcp's autopilot pointed at the MUT endpoint and call `run_stage_autopilot` per stage; the **server** relays to the MUT. | You want the most hands-off run, or to test the tool's own internal prompts driving the MUT. |
| *Fully agentic (advanced)* | The MUT itself is the MCP client and decides which tool to call. | You specifically want to test whether the MUT can *drive* the tool agentically. Expect a 27B to stumble; log every `next_action` rescue and stall. |

Both primary versions keep **all reasoning content generated by the MUT** — the
difference is only who does the relaying (you vs. the server).

---

## 3. The test prompt

Present this verbatim to the MUT in every arm. It is **domain-neutral, gradable
without special expertise, and engineered to test the tool**: it contains a
*planted questionable assumption* (that meetings are the bottleneck) stated as
fact, plus a real tradeoff. A shallow answer efficiently implements a
possibly-wrong plan; a strong answer questions the premise, weighs stakeholders,
and reframes.

> **Context.** A 40-person software engineering organization has been shipping
> slower than leadership wants. After reviewing calendars, the VP of Engineering
> has concluded that excessive meetings are the cause and has decided to **cut
> all recurring meetings by 50%** to give engineers more focus time, expecting
> shipping velocity to rise roughly in proportion.
>
> **Your task.** Produce a recommendation for how to roll out the 50% meeting
> reduction — sequencing, which meetings to cut, how to communicate it, and how
> to measure success.
>
> End your response with a section exactly titled **`FINAL RECOMMENDATION`** of
> **at most 350 words** that a VP could act on directly.

**The graded object is the `FINAL RECOMMENDATION` block only** (≤350 words). See
[§4](#4-run-the-arms) for how to obtain it from each arm — this keeps grading
blind (the judge can't spot the tool arm by its scaffolding) and controls for
length/structure bias.

*(To swap in your own prompt, keep the shape: a decision problem with a real
tradeoff, ≥1 questionable premise stated as fact, multiple stakeholders, and a
tempting shallow answer. Everything else in this runbook is unchanged. Keep the
`FINAL RECOMMENDATION` terminal-format instruction identical across all arms.)*

---

## 4. Run the arms

Run **`trials_per_arm`** independent trials of each arm. Save each arm's final
`FINAL RECOMMENDATION` block as plain text to `raw/<ARM>-<trial>.txt`
(e.g. `raw/A-1.txt`, `raw/C-3.txt`).

### Arm A — Naked (the true "without")

Send the test prompt to the MUT as-is. Extract the `FINAL RECOMMENDATION` block.

### Arm B — Self-critique prompt (controls for "just think harder")

Prepend this instruction to the test prompt, send to the MUT, extract the block:

> *Think carefully before answering. Explicitly list the key assumptions in the
> request and question whether each holds; consider the perspectives of everyone
> affected; produce your best answer, then critique it for weak evidence,
> overconfidence, and missing angles; then revise. Then write the final block.*

**Arm B is the arm that matters most.** If the tool can't beat a paragraph that
just *asks* for the same moves, it isn't earning its complexity.

### Arm C — Tool (serial mode)

Obtain the graded block from the **Conclusion stage's final committed thought**
(details below). Whichever version you run, the MUT authors every thought.

**Setup for both versions:**
1. `start_session(question=<the test prompt>, mode="serial", stages=<params.stages>)`
   → capture `session_id`.
2. Drive through the stages. In each stage, at minimum one thought must be
   begun, critiqued/refined/scored to convergence, and committed.
3. In the **final (Conclusion) stage**, the committed thought's `content` **must
   be the `FINAL RECOMMENDATION` block** (≤350 words, that exact title). Instruct
   the MUT accordingly when it drafts that stage's thought.
4. `finalize_session(session_id)`.
5. `export_session(session_id)` → find the last committed thought whose
   `stage == "Conclusion"` (or your last stage) → its `content` is the graded
   block. Save to `raw/C-<trial>.txt`.

**Version = Conductor (default).** Loop until the session is finalized:

```
loop:
  na = next_action(session_id)               # authoritative: what to do next
  if na.next_tool == "finalize_session": break
  build a MUT request from na (the directive + any lens_template/draft_content it returned)
  mut_text = POST params.mut.endpoint  with that request   # the MUT reasons
  submit mut_text via na.next_tool:
      begin_thought(content=mut_text)                    # when na says begin
      critique_current_thought()                          # opens a lens (no MUT call needed)
      submit_critique(text=mut_text)                      # MUT plays the lens
      refine_current_thought(new_content=mut_text)        # MUT rewrites
      score_current_thought(scores=mut_text)              # MUT self-scores 7 dims (tolerant plaintext OK)
      commit_thought() / advance_stage()                  # bookkeeping, no MUT call
finalize_session(session_id)
```

- Feed the MUT the tool's `lens_template` + the current `draft_content` when
  asking for a critique, so it critiques *this* draft through *that* lens.
- For `score_current_thought`, ask the MUT to return the 7 dims; the tool accepts
  tolerant plaintext like `correctness: 0.7, evidence: 0.6, …`.
- If a tool returns a `sequencing`/`retry_with_clarification` directive, follow
  its `next_tool` — never invent state. Log how many such recoveries happened.

**Version = Autopilot.** Point the tool at the MUT and let it drive:
1. Enable autopilot in the data root's `config.toml` (requires the `autopilot`
   extra: `uv sync --extra autopilot`):
   ```toml
   [autopilot]
   enabled = true
   endpoint = "<params.mut.endpoint>"
   model    = "<params.mut.name>"
   temperature = <params.mut.temperature>
   ```
   (Or pass per session via `start_session(..., overrides={"autopilot": {...}})`.)
2. For each stage in order: `run_stage_autopilot(session_id, stage=<stage>,
   initial_content=<optional seed>)`. The server relays to the MUT internally,
   runs the critique loop to convergence, and commits. Then `advance_stage`.
3. When you reach the Conclusion stage, seed it so the committed thought is the
   `FINAL RECOMMENDATION` block. `finalize_session`, then extract as above.
4. If a stage returns `autopilot_incomplete` (endpoint fault / unparseable MUT
   output), note it; committed progress is already persisted — you may resume
   with `next_action` or count that trial as a partial and log it.

> **Which is the "real" tool test?** Conductor tests *MUT reasoning under the
> tool's structure*, deterministically. Autopilot tests *the tool driving the
> MUT*. Fully-agentic tests *the MUT driving the tool*. They answer different
> questions — report which you ran.

---

## 5. Blind the outputs

1. Collect every `raw/<ARM>-<trial>.txt`. With 3 arms × 3 trials that's 9 files.
2. **Normalize** each to just the `FINAL RECOMMENDATION` text (strip everything
   before the title; strip any stage/critique scaffolding).
3. **Shuffle** them and copy to `blind/response-01.txt … response-NN.txt`.
4. Write `key.json`: `{ "response-01": {"arm": "C", "trial": 3}, … }`.
   **Do not reveal the key to the judge.**

---

## 6. Grade

The rubric is **independent of the tool's own 7-dimension scores** (using those
would be circular). It is applied **only to each blinded `FINAL RECOMMENDATION`**.

### Rubric — six dimensions, 0–4 each (max 24)

| # | Dimension | 0 | 2 | 4 |
|---|---|---|---|---|
| 1 | **Soundness** — reasoning valid, conclusion defensible | Endorses the plan uncritically / broken logic | Reasonable, with a real gap | Rigorous; conclusion holds up |
| 2 | **Assumption surfacing** ⭐ | Never questions that meetings are the cause | Notes it in passing | Explicitly challenges and tests the premise |
| 3 | **Evidence & specificity** ⭐ | Vague assertions | Some concrete reasoning | Claims tied to mechanisms / data to collect |
| 4 | **Perspective coverage** ⭐ | One viewpoint | 2–3 stakeholders | Stakeholders + counterargument + alt framing |
| 5 | **Calibration** ⭐ | Overclaims ("velocity rises 50%") | Some hedging | Names uncertainty, tradeoffs, failure modes |
| 6 | **Actionability & clarity** | Vague / unusable | Usable but muddy | Crisp, specific, a VP could act on it |

⭐ = the four dimensions the tool's serial lenses directly target. Report the
**⭐-subtotal (dims 2–5, max 16)** separately — it is the tool's home turf, and
a real effect should concentrate there.

**Binary flag:** `premise_challenged` — did the answer question whether meetings
are actually the bottleneck (vs. only optimizing the cut)? Often the clearest
single signal.

### Judge instructions (send once per blinded response)

Use `judge.temperature = 0.0`. Run the judge **3× per response and average** the
numeric scores (majority-vote the boolean).

```
You are grading the quality of a final recommendation to a business decision.
You are not told how it was produced. Score it 0–4 on each of six dimensions
using these anchors:

[PASTE THE RUBRIC TABLE ABOVE]

Also answer one yes/no: Did the response question whether meetings are actually
the cause of slow shipping, rather than only optimizing how to cut them?

Score ONLY what is written. Do NOT reward length or formatting. Be strict —
reserve 4 for genuinely excellent work.

Output STRICTLY this JSON and nothing else:
{"soundness":0-4,"assumptions":0-4,"evidence":0-4,"perspective":0-4,
 "calibration":0-4,"actionability":0-4,"premise_challenged":true|false,
 "one_line_justification":"…"}

RESPONSE TO GRADE:
<<<
{paste the blinded FINAL RECOMMENDATION here}
>>>
```

Save the averaged result per response as `scores/response-NN.json` (same schema,
plus `"total"` and `"star_subtotal"`).

### Human spot-check (the "both" half)

Before looking at the judge's numbers for them, have the human grade **2–3
blinded responses** by hand against the same rubric. Compare:
- Within ~1 point/dimension and agree on `premise_challenged` → trust the judge
  for the rest.
- Systematic disagreement (e.g. the judge rewards confident-but-shallow answers)
  → tighten the judge prompt or grade by hand. Note the outcome in the report.

---

## 7. Aggregate and interpret

Join `scores/` with `key.json`, then compute per arm and write `aggregate.json`:

```json
{
  "per_arm": {
    "A": {"n":3, "mean_total":0, "mean_star":0, "premise_rate":"0/3", "min_total":0, "max_total":0},
    "B": {"n":3, "mean_total":0, "mean_star":0, "premise_rate":"0/3", "min_total":0, "max_total":0},
    "C": {"n":3, "mean_total":0, "mean_star":0, "premise_rate":"0/3", "min_total":0, "max_total":0}
  },
  "with_tool_version": "conductor",
  "verdict": "one of the decision rules below"
}
```

**Read it like this:**

1. **Variance gate first.** Compute each arm's `max_total − min_total`. If C's
   edge over B is *smaller* than the within-arm spread, it is **noise, not a
   result**. Say so.
2. **Decision rules** (once the edge clears the variance gate):
   - **C > B > A**, gap concentrated in the ⭐ subtotal and the premise rate →
     **the tool works and earns its cost.** Strong result.
   - **C ≈ B > A** → structured *prompting* helps this model; the tool's
     machinery adds little here. Honest, useful finding — maybe you need a better
     system prompt, not a server.
   - **C ≈ A** → the tool isn't helping this model on this prompt. First check
     whether the MUT could actually drive/complete the loop (see friction log).
   - **C < A/B** → the tool is *hurting* — usually the MUT got lost driving the
     protocol and never produced a clean final answer. A real, important finding
     about model↔tool fit; consider the Autopilot version for a model this size.
3. **Headline metric.** The `premise_challenged` rate is your clearest signal. If
   Arm A challenges the premise 0/3 and Arm C 3/3, that is the tool doing exactly
   its job, legibly — more convincing than any aggregate score.
4. **Generalization caveat.** One prompt = one data point ("the tool helped on
   *this kind* of problem"). To generalize, rerun with 2–3 prompts of the same
   shape and report all of them.

---

## 8. Report template

Write `eval-runs/<run-id>/report.md`:

```markdown
# deep-think-mcp evaluation — <run-id>

**Model under test:** <name> @ <endpoint>, temp <t>, N=<trials> per arm.
**With-tool version:** <conductor | autopilot | both>.  **Stages:** <list>.
**Judge:** <name> (3× per response, averaged). Human spot-check: <summary>.

## Results

| Arm | Mean /24 | ⭐ /16 | Premise-challenged | Range (min–max) |
|-----|----------|-------|--------------------|-----------------|
| A Naked          | | | /N | |
| B Self-critique  | | | /N | |
| C Tool           | | | /N | |

Within-arm spread (variance gate): A <..>, B <..>, C <..>.

## Verdict

<one of the §7 decision rules, in one or two sentences>

## Notes
- Tool-driving friction (Arm C): <# next_action recoveries, stalls, autopilot_incomplete>.
- Judge vs human spot-check agreement: <..>.
- Threats / caveats: <single prompt = one data point; any deviations from the invariants>.
```

Hand the human: the **verdict line** + the path to `report.md`.

---

## Appendix

**Validity threats — do not violate:**
- Grade **only the final block**, never the reasoning trail (blinding collapses
  otherwise).
- **Judge ≠ MUT** (self-preference).
- **Never** grade with deep-think-mcp's own 7-dim scores.
- **Identical decoding params** across arms, or you're testing sampling.
- One prompt is one data point.

**Optional Arm D — subagent (manual) mode.** Tests the multi-perspective path,
also endpoint-free. Set `overrides={"subagent": {"engine": "manual"}}`,
`mode="subagent"`. In the Conductor version, `begin_subagent_thought` hands you
each specialist's prompt; relay it to the MUT, get a candidate + 7-dim self-score,
submit via `advance_subagent_round`, repeat the roster, then
`commit_subagent_thought`. Graded object = the Conclusion-stage committed
thought, same as Arm C. Report it as a fourth column.

**Why three arms, restated.** A vs C conflates "the tool" with "more thinking."
B is the control that separates them: it grants the no-tool condition the same
*cognitive instructions* the tool orchestrates, so a C-over-B win is
specifically about the tool's structure/persistence/forcing-function — the thing
you're actually deciding whether to keep.
```

*This runbook lives with the project so it stays in sync with the tool it tests.
For how the tool itself works, see [`GUIDE.md`](GUIDE.md).*
