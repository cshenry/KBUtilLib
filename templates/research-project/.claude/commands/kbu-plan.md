<!--
kbu skill provenance
type: lean-fork
source_repo: AIAssistant
source_commit: b2d26fb6305b86bb56961e46dfd4e47e22c2ae00
source_path: agent-io/skills/ai-design.md
last_reviewed: 2026-06-08
-->

# /kbu-plan — Research Plan Design Session

You help a researcher design a rigorous research plan for their KBUtilLib subproject.
The flow is four steps: grill goals, run a literature review subagent, grill the
detailed plan, then decompose into tasks. Each step gates the next — do not skip ahead.

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

Invoke the literature review subagent, passing the subproject path, topic list, and
depth tier:

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

Do NOT proceed to Step 4 until `RESEARCH_PLAN.md` is written and the user
has reviewed it.

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

After Step 4 is complete, advance the subproject state and save the session:

```bash
kbu subproject advance <name>
kbu session save --skill kbu-plan --subproject <name> --summary "<one-sentence summary of what was designed>"
```

Tell the user:
- Files written: `subprojects/<name>/RESEARCH_PLAN.md`, `subprojects/<name>/TASKS.md`,
  `subprojects/<name>/literature/` (per-topic files + `index.md`), manifest
  `[[notebooks]]` entries in `kbu-subproject.toml`
- The new subproject state (should be `p-review`)
- That they can now run `/kbu-build` to scaffold the notebooks

---

## Rules

1. **Grill first.** Do not write any artifact before its grill step is complete.
2. **No code.** You are a planner — write plan documents, not implementations.
3. **Stay in the subproject.** All file writes go under `subprojects/<name>/`
   (or root shared dirs when relocating shared data — but no data moves in this skill).
4. **Literature review runs in a subagent.** Never inline paper text into the
   main thread — always delegate to `kbu-sub-literature-review` via the Agent tool.
5. **Manifest is the source of truth for notebooks.** Write `TASKS.md` from the
   manifest, not the other way around.
6. **Cross-reference skills by slash-command name.** Use `/kbu-build`, `/kbu-run`,
   `/kbu-diagnose` when pointing the user to next steps.
7. **Be honest about gaps.** If the user cannot answer a grill question, mark it
   `TBD` in the Notes section rather than guessing.
