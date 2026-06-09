<!--
kbu skill provenance
type: lean-fork
source_repo: AIAssistant
source_commit: b2d26fb6305b86bb56961e46dfd4e47e22c2ae00
source_path: agent-io/skills/ai-design.md
last_reviewed: 2026-06-09
-->

# /kbu-plan — Research Plan Design Session

You help a researcher design a rigorous research plan for their KBUtilLib subproject.
The flow is six steps: grill goals, run a literature review subagent, grill the
detailed plan, grill the test design, emit the validated build contract, then
decompose into tasks. Each step gates the next — do not skip ahead.

---

## MANDATORY SUBAGENT DELEGATION RULE

Every delegated step in this skill runs through an explicit `Agent(subagent_type="kbu-sub-…", prompt=…)` call written at the exact point it executes. A prose instruction ("review the build"), a mental summary, or a /slash cross-reference that the model satisfies inline does NOT count.

> **STOP tell-sign:** If you are reading papers, writing review prose, building notebook code, or diagnosing a failure in the main thread — STOP. You skipped the subagent. Go back and issue the `Agent(...)` call.

## Precondition

The current subproject must be in state `plan` (created by `kbu subproject add` and
not yet advanced). Verify before proceeding:

```bash
kbu subproject status <name>
```

If the state is not `plan`, tell the user which step is needed first.

Then load subproject context:

```bash
ls subprojects/<name>/
cat subprojects/<name>/SUBPROJECT.md 2>/dev/null || echo "(no SUBPROJECT.md yet)"
```

Present a one-paragraph summary of what you found.

---

## Step 1 — Goals and Goals Grill

Ask the user to describe:
- The scientific question they are investigating
- The dataset(s) or model(s) they plan to use
- Any prior work or starting notebooks they have

After the user responds, grill the goals **one question per round**, always
recommending an answer. Surface answers for all of:

- What is the central research goal in one sentence?
- Who is the intended audience for the outputs (self, lab, publication)?
- What does "done" look like — is there a deliverable date or milestone?
- Are there any hard constraints (data access, compute budget, timeline)?

Skip any question whose answer is already clear from existing files or context.

Do NOT proceed to Step 2 until goals are confirmed.

---

## Step 2 — Literature Review (Subagent)

Derive a concise topic list from the confirmed goals. Select a depth tier:
- **Quick scan** for narrow or well-known topics
- **Standard review** (default) for subproject-based work
- **Deep review** only when explicitly requested

Invoke the literature review subagent via an explicit `Agent` call — never inline:

```
Agent(subagent_type="kbu-sub-literature-review", prompt="Subproject: subprojects/<name>. Topics: <topic1>, <topic2>, .... Depth: <tier>. Review the listed topics and write per-topic synthesis files at subprojects/<name>/literature/<topic-slug>.md and a literature/index.md. Return a short summary of topics covered and total papers found.")
```

The subagent writes:
- `subprojects/<name>/literature/<topic-slug>.md` — one file per discrete topic
- `subprojects/<name>/literature/index.md` — index of all topics reviewed

Wait for the subagent to complete. Present its returned summary to the user.
Ask if any additional topics should be reviewed before moving on.
Run the subagent again for any additional topics requested.

Do NOT proceed to Step 3 until literature coverage is accepted.

---

## Step 3 — Detailed Plan Grill and Write RESEARCH_PLAN.md

Walk the plan dependency tree **one question per round**, always recommending an
answer. Explore the subproject directory for existing data or code to answer
questions rather than asking when you can.

**Hypothesis:**
- What is the specific falsifiable hypothesis or research question?

**Success criteria:**
- How will you know if the hypothesis is supported?
- What externally observable results would confirm it?

**Data inputs:**
- What input data is required, and where does it live (`data/` path or shared dir)?
- Are any datasets shared with other subprojects (root `data/`, `models/`, `genomes/`)?

**Methods:**
- Which KBUtilLib modules or external libraries are central?
- What computational steps are needed (load → transform → analyze → visualize)?

**Notebook structure:**
- How many notebooks are appropriate (1–5 is the guideline)?
- What does each notebook do, in one sentence?
- What shared utilities belong in `util.py` rather than notebooks?

**Outputs:**
- What figures, tables, or files does the plan produce?
- Which outputs are intermediate (consumed by a later notebook) vs final?
- Where do final outputs live (`figures/`, `nboutput/`, or a shared dir)?

**Out of scope:**
- What will this subproject explicitly NOT do?

Keep grilling until every answer above is resolved. Skip a question only when
the answer is already clear from existing files, prior discussion, or literature.

### Sketch the Plan Structure

Before writing, present the proposed structure:

- Hypothesis / research question (one sentence)
- Notebooks: numbered list with one-sentence purpose each
- Shared utilities in `util.py`
- Data inputs (with paths)
- Outputs (with destinations)
- Success criteria (externally observable)
- Out of scope

Ask: "Does this match your intent, or should we adjust?"
Revise until the user confirms.

### Write RESEARCH_PLAN.md

Write `subprojects/<name>/RESEARCH_PLAN.md` with this structure:

```markdown
# Research Plan: <Subproject Name>

## Hypothesis / Research Question
<One or two sentences — specific and falsifiable.>

## Success Criteria
<Numbered list — what an external reader would check to verify the plan succeeded.>

## Data Inputs
<Bullet list: path (relative to project root), description, format.>

## Notebook Outline
1. `01_<slug>.ipynb` — <purpose>
2. `02_<slug>.ipynb` — <purpose>
... (as many as needed, max 5)

## Shared Utilities (`util.py`)
<Bullet list of functions with one-line descriptions. "None" if not applicable.>

## Outputs
<Bullet list: filename/path, description, which notebook produces it.>

## Out of Scope
<What this subproject explicitly will not do.>

## Literature
<One-sentence summary of literature reviewed; pointer to literature/index.md.>

## Notes
<Any caveats, known data quality issues, follow-up questions, or open decisions.>
```

Do not write production code or notebooks — that is `/kbu-build`'s job.

Do NOT proceed to Step 3b until `RESEARCH_PLAN.md` is written and the user
has reviewed it.

---

## Step 3b — Test-Design Grill

For each helper function listed in the `util.py` section of the confirmed plan,
grill the test design **one question per round**, always recommending an answer.
The goal is to pin down the exact test specification for every helper so the
build contract is unambiguous.

For each helper, work through:

**Data source:**
- Should the test use `sampled-real` data (a slice of an actual dataset already in
  `data/`) or `synthetic` data (a small hand-crafted or randomly generated fixture)?
- Recommend `sampled-real` when real data is available and small enough to include;
  recommend `synthetic` when the function is pure computation or no real data is
  available yet.

**Data spec:**
- If `sampled-real`: which file and how to sample it (e.g. `data/raw.tsv head -200`)?
- If `synthetic`: exact dimensions and generation rule (e.g. `10×5 random float
  matrix`, `["gene_a", "gene_b"] mapped to {"ko": True, "wt": False}`)?

**Assertions:**
- What specific, checkable properties must hold after the function runs?
  (e.g. "result has shape (200, 3)", "no NaN values in column 'fitness'",
  "total rows equals input rows minus header")
- Pin down at least one assertion per helper; aim for two or three.

Work through every helper in turn. Do not proceed to the next helper until the
current one has a confirmed `data_source`, `data_spec`, and at least one
non-empty assertion.

After all helpers are resolved, present a consolidated summary:

```
Helper: <name>
  data_source: sampled-real | synthetic
  data_spec: <...>
  assertions:
    - <assertion 1>
    - <assertion 2>
```

Ask: "Are these test specs correct, or should we revise any?"
Revise until the user confirms.

Do NOT proceed to Step 3c until all helper test specs are confirmed.

---

## Step 3c — Emit buildplan.json and Validate

Compose `subprojects/<name>/buildplan.json` from the confirmed plan structure
and the confirmed test specs. The file must conform to the KBU conductor schema:

```json
{
  "subproject": "<name>",
  "notebooks": [
    {
      "slug": "01_<slug>",
      "purpose": "<one-sentence purpose>",
      "depends_on": [],
      "helpers": [
        {
          "name": "<function_name>",
          "signature": "<function_name>(<params>) -> <return_type>",
          "contract": "<what the function must do, in prose>",
          "test": {
            "data_source": "sampled-real | synthetic",
            "data_spec": "<e.g. 'data/raw.tsv head -200' or '10x5 random matrix'>",
            "assertions": ["<exact checkable assertion>", "..."]
          }
        }
      ]
    }
  ]
}
```

Rules:
- `depends_on` entries must reference notebook slugs that appear STRICTLY earlier
  in the `notebooks` list (no forward references, no self-references).
- Each helper's `test.assertions` must be non-empty.
- `test.data_source` must be exactly `sampled-real` or `synthetic`.
- `RESEARCH_PLAN.md` is the prose artifact; the manifest `[[notebooks]]` entries
  are a lightweight run-ledger. Do NOT embed build spec in either — it belongs
  exclusively in `buildplan.json`.

Write the file, then validate it:

```bash
kbu buildplan validate subprojects/<name>/buildplan.json
```

If validation fails, show the errors to the user, fix each one, and re-run
`kbu buildplan validate` until it exits 0 before advancing.

Do NOT proceed to Step 3d until `kbu buildplan validate` exits 0.

---

## Step 3d — Plan Review (Subagent, Closed-Loop)

With `RESEARCH_PLAN.md` written and `buildplan.json` validated, spawn the plan
reviewer subagent via an explicit `Agent` call:

```
Agent(subagent_type="kbu-sub-review", prompt="<name>")
```

The subagent writes `subprojects/<name>/REVIEW_plan_<n>.md` with a verdict
comment on the first line:

```
<!-- kbu-review:verdict: pass|fail -->
```

After the subagent returns, confirm the verdict file exists and read its first
line:

```bash
ls subprojects/<name>/REVIEW_plan_*.md | sort -V | tail -1
head -1 "$(ls subprojects/<name>/REVIEW_plan_*.md | sort -V | tail -1)"
```

- If the first line is `<!-- kbu-review:verdict: pass -->` — proceed to Step 4.
- If the first line is `<!-- kbu-review:verdict: fail -->` — present the critical
  issues to the user, revise `RESEARCH_PLAN.md` accordingly, and re-spawn the
  reviewer subagent. Repeat until a `pass` verdict file exists on disk.

Do NOT call `kbu subproject advance` until a `pass` verdict file exists on disk.
An inline assessment ("the plan looks good") is not a verdict file and does not
satisfy this gate.

Do NOT proceed to Step 4 until a `pass` verdict file exists.

---

## Step 4 — Decompose into Tasks

Populate the manifest `[[notebooks]]` entries and render `TASKS.md`.

### Manifest update

For each notebook in the plan outline, append a `[[notebooks]]` entry to
`subprojects/<name>/kbu-subproject.toml`:

```toml
[[notebooks]]
slug = "01_<slug>"
purpose = "<one-sentence purpose>"
last_run_at = ""
modified_since_run = true
```

Rules:
- `slug` matches the notebook filename without `.ipynb`.
- `purpose` is the one-sentence description from the plan outline.
- `last_run_at` is always the empty string at plan time (notebook does not
  yet exist).
- `modified_since_run` is always `true` at plan time.
- The manifest is the source of truth — write it first.

Read the current `kbu-subproject.toml`, append the entries, and write the
file back. Do not alter any other fields.

### Write TASKS.md

Write `subprojects/<name>/TASKS.md` as a human-readable flat numbered list
derived directly from the manifest entries. Format:

```markdown
# Tasks: <Subproject Name>

Generated from manifest. Source of truth is `kbu-subproject.toml`.

## Notebooks

1. `01_<slug>.ipynb` — <purpose>
2. `02_<slug>.ipynb` — <purpose>
...

## Next Step

Run `/kbu-build` to scaffold the notebook files from this plan.
```

`TASKS.md` is a view — if manifest and `TASKS.md` ever diverge, the
manifest wins.

---

## Phase 5: Advance and Save Session

After Step 4 is complete and a `pass` verdict file exists on disk
(confirmed in Step 3d), advance the subproject state and save the session:

```bash
kbu subproject advance <name>
kbu session save --skill kbu-plan --subproject <name> --summary "<one-sentence summary of what was designed>"
```

Tell the user:
- Files written:
  - `subprojects/<name>/RESEARCH_PLAN.md`
  - `subprojects/<name>/buildplan.json` (validated)
  - `subprojects/<name>/REVIEW_plan_<n>.md` (pass verdict)
  - `subprojects/<name>/TASKS.md`
  - `subprojects/<name>/literature/` (per-topic files + `index.md`)
  - manifest `[[notebooks]]` entries in `kbu-subproject.toml`
- The new subproject state (should be `p-review`)
- That they can now run `/kbu-build` to scaffold the notebooks

---

## Rules

1. **Grill first.** Do not write any artifact before its grill step is complete.
2. **No code.** You are a planner — write plan documents, not implementations.
3. **Stay in the subproject.** All file writes go under `subprojects/<name>/`
   (or root shared dirs when relocating shared data — but no data moves in this skill).
4. **Delegated steps run in explicit subagents.** Literature review and plan review
   must be invoked through `Agent(subagent_type="kbu-sub-…", prompt=…)` calls.
   Never inline either step — see the MANDATORY SUBAGENT DELEGATION RULE at the top.
5. **Closed-loop review gate.** The subproject may not advance until a `pass`
   verdict file (`REVIEW_plan_<n>.md`) exists on disk. An inline assessment
   does not satisfy this gate.
6. **buildplan.json must validate.** `kbu buildplan validate` must exit 0 before
   advancing. Never skip this check.
7. **Build spec belongs in buildplan.json only.** Do not put helper contracts or
   test specs in `RESEARCH_PLAN.md` or the manifest `[[notebooks]]` entries.
8. **Manifest is the source of truth for notebooks.** Write `TASKS.md` from the
   manifest, not the other way around.
9. **Cross-reference skills by slash-command name.** Use `/kbu-build`, `/kbu-run`,
   `/kbu-diagnose` when pointing the user to next steps.
10. **Be honest about gaps.** If the user cannot answer a grill question, mark it
    `TBD` in the Notes section rather than guessing.
