<!--
kbu skill provenance
type: harvested
source_repo: BERIL-research-observatory
source_commit: 940c3b0ee7bbf63bc576bd6e8c25210ad692df8e
source_path: .claude/skills/berdl-review/SKILL.md
last_reviewed: 2026-06-05
-->

---
name: kbu-sub-review
type: agent
description: Run an AI review of a subproject plan or report. Use when the researcher wants structured feedback during development. Produces a numbered REVIEW_<stage>_<n>.md file with a verdict comment.
allowed-tools: Bash, Read, Write
---

# kbu-sub-review

Run an independent AI review of a KBU subproject plan or report. Each run produces a
numbered `REVIEW_<stage>_<n>.md` file with a verdict comment at the top:

```
<!-- kbu-review:verdict: pass|fail -->
```

Use to iterate on feedback before advancing to the next stage.

## Usage

Invoked as a subagent or directly by the researcher:

```
Agent(subagent_type="kbu-sub-review", prompt="<subproject_name>")
```

If no `<subproject_name>` is provided, detect from the current working directory
(if inside `subprojects/<name>/`).

## Workflow

### Step 1: Detect Stage

Check the current stage:

```bash
kbu subproject status <name>
```

The review stage is auto-detected from the output:

| Current stage | Review type | Source document | Output file |
|---|---|---|---|
| `p-review` | Plan review | `RESEARCH_PLAN.md` | `REVIEW_plan_<n>.md` |
| `b-review` | Build review | Scaffolded notebooks + `buildplan.json` | `REVIEW_build_<n>.md` |
| `s-review` | Synthesis review | `REPORT.md` | `REVIEW_synthesis_<n>.md` |

If the stage is not one of the three above, stop and tell the researcher:

> "This subproject is not at a review stage. Check `kbu subproject status <name>`
> to see what step is next."

### Step 2: Read the Source Document

Depending on the detected stage:

**p-review (plan):** Read `subprojects/<name>/RESEARCH_PLAN.md`. Assess:
- Is the research question clear and focused?
- Is the hypothesis testable with the available data and tools?
- Is the analysis plan concrete (which notebooks, which outputs)?
- Are limitations acknowledged?

**b-review (build):** See [Step 2b: Build Review Against buildplan.json](#step-2b-build-review-against-buildplanjson) below.

**s-review (synthesis):** Read `subprojects/<name>/REPORT.md`. Assess:
- Do the Key Findings answer the research question?
- Are results stated with specific numbers (p-values, effect sizes, counts)?
- Is each finding traced to a notebook and figure?
- Does the Interpretation section engage with the literature?
- Are limitations acknowledged?
- Is the References section populated?

---

### Step 2b: Build Review Against buildplan.json

This section applies only when the stage is `b-review`.

#### 2b-1: Load the buildplan

Read and parse `subprojects/<name>/buildplan.json`. If the file does not exist,
treat this as a critical issue: the build cannot be reviewed without a buildplan.

Validate it is structurally sound by running:

```bash
kbu buildplan validate subprojects/<name>/buildplan.json
```

If validation fails, record each error as a critical issue and skip to Step 3
(the verdict will be `fail`).

The buildplan has this shape:

```json
{ "subproject": "<name>", "notebooks": [
  { "slug": "...", "purpose": "...", "depends_on": [],
    "helpers": [ { "name": "...", "signature": "...", "contract": "...",
      "test": { "data_source": "sampled-real|synthetic", "data_spec": "...",
                "assertions": ["..."] } } ] } ] }
```

#### 2b-2: Check that test_util.py exists

```bash
ls subprojects/<name>/notebooks/test_util.py
```

If `test_util.py` does not exist, record this as a critical issue and skip
helper-level checks (they cannot pass without a test file).

#### 2b-3: Per-helper test coverage check

For each notebook entry in the buildplan, for each helper in `helpers`:

1. **Test exists**: Search `test_util.py` for a test function that covers this
   helper. A test "exists" if `test_util.py` contains a function whose name
   includes `test_` and references the helper's `name` field (e.g. a function
   named `test_<helper_name>` or a function body that calls `<helper_name>`).
   Record any helper whose test is missing as a critical issue:
   > `helpers[<name>] (notebook: <slug>): no test found in test_util.py`

2. **Test passes**: Run pytest against `test_util.py` for each helper:

   ```bash
   cd subprojects/<name>/notebooks && python -m pytest test_util.py -v -k "<helper_name>" 2>&1
   ```

   If the test exists but fails, record it as a critical issue:
   > `helpers[<name>] (notebook: <slug>): test exists but FAILED`

   If the test cannot be run (import error, missing dependency), record it as
   a critical issue with the error message.

After iterating all helpers, also run the full test suite to catch any
regressions:

```bash
cd subprojects/<name>/notebooks && python -m pytest test_util.py -v 2>&1
```

Record the summary line (e.g. `5 passed, 1 failed`) in the review.

#### 2b-4: Declared outputs check

For each notebook in the buildplan, read the notebook file
`subprojects/<name>/notebooks/<slug>.ipynb` and identify any output paths it
writes (look for cells that write to `../data/`, `../figures/`, or other paths
relative to `notebooks/`). Then verify each output exists:

```bash
ls subprojects/<name>/data/
ls subprojects/<name>/figures/
```

For any output path that the notebook declares but that does not exist on disk,
record it as an important issue (not a critical issue, since notebooks may not
have been fully run yet):
> `notebook <slug>: declared output <path> not found on disk`

If outputs are genuinely expected to be absent at this stage (the notebooks
have not been run), note this in the Suggestions section rather than as a
critical issue.

#### 2b-5: Notebook quality checks (existing behavior)

In addition to the buildplan checks, retain the original structural review:
- Are the notebooks well-structured and commented?
- Does each notebook have a clear purpose linked to the research plan?
- Are outputs (CSVs, figures) written to the correct locations (`data/`, `figures/`)?
- Are there obvious errors or incomplete cells?

---

### Step 3: Produce the Review

Determine the next sequential number for this stage by scanning existing review files.
Note: the stage key used in the filename matches what the state gate reads —
use `plan`, `build`, or `synthesis` (not the stage identifiers `p-review`,
`b-review`, `s-review`):

```bash
ls subprojects/<name>/REVIEW_plan_*.md 2>/dev/null | sort -V | tail -1
ls subprojects/<name>/REVIEW_build_*.md 2>/dev/null | sort -V | tail -1
ls subprojects/<name>/REVIEW_synthesis_*.md 2>/dev/null | sort -V | tail -1
```

If none exist for this stage, start at 1.

Write `subprojects/<name>/REVIEW_<file_stage>_<n>.md` where `<file_stage>` is
`plan`, `build`, or `synthesis` (mapped from the current stage):

| Current stage | File stage key |
|---|---|
| `p-review` | `plan` |
| `b-review` | `build` |
| `s-review` | `synthesis` |

The file must follow this structure:

```markdown
<!-- kbu-review:verdict: pass|fail -->

# Review: <Subproject Name> — <Stage> (<n>)

**Reviewer**: Claude (kbu-sub-review)
**Date**: <ISO 8601 date>
**Stage**: <p-review|b-review|s-review>
**Verdict**: PASS / FAIL

## Summary

<2-3 sentence overall assessment>

## Critical Issues *(must fix before passing)*

- <issue 1> (omit section if none)

## Important Issues *(should address)*

- <issue 1> (omit section if none)

## Suggestions *(nice-to-have)*

- <suggestion 1> (omit section if none)

## What Looks Good

- <strength 1>
- <strength 2>
```

**For b-review, include an additional section before "What Looks Good":**

```markdown
## Build / Test Results

| Helper | Notebook | Test found | Test result |
|---|---|---|---|
| <name> | <slug> | yes/no | pass/fail/error |

**pytest summary**: <N passed, M failed, K errors>
```

**Verdict rules:**
- **pass**: No critical issues; the subproject meets the core requirements for this stage.
- **fail**: One or more critical issues that must be addressed before the subproject
  can advance.

The `<!-- kbu-review:verdict: pass|fail -->` comment **must be the first line** of the file.

### Step 4: Advance or Reverse Stage

**On pass:**
```bash
kbu subproject advance <name>
```

**On fail:**
```bash
kbu subproject advance <name> --reverse
```

Then save a session record:

```python
from assistant.state import save_session
save_session({
    'project_id': '<subproject_name>',
    'command': 'kbu-sub-review',
    'topics_discussed': ['review', '<stage>'],
    'decisions_made': ['verdict: pass|fail', 'REVIEW_<file_stage>_<n>.md written'],
    'next_steps': ['<next step based on verdict>'],
    'summary': '<stage> review completed — <pass/fail>',
})
```

### Step 5: Present Summary to Researcher

Present:
- Overall verdict (PASS / FAIL).
- Count of critical / important / suggestion items.
- Key issues to address (on fail) or confirmation to proceed (on pass).

**If PASS:**
> "Review passed. The subproject has advanced to the next stage.
> Run `kbu subproject status <name>` to see what's next."

**If FAIL:**
> "Review did not pass. Address the critical issues listed in
> `subprojects/<name>/REVIEW_<file_stage>_<n>.md`, then re-run the review subagent."

## Notes

- Reviews are numbered sequentially per stage: `REVIEW_plan_1.md`,
  `REVIEW_plan_2.md`, etc. Previous reviews are preserved.
- The filename stage key (`plan`, `build`, `synthesis`) differs from the subproject
  stage identifier (`p-review`, `b-review`, `s-review`). Always use the file-stage
  key in filenames — this is what the subproject state gate globs for.
- Each review file begins with `<!-- kbu-review:verdict: pass|fail -->` so automated
  tools can parse the outcome without reading the full file.
- Re-run `/kbu-synthesize` before requesting a new `s-review` if `REPORT.md` has
  changed significantly since the last review.
- For `b-review`, the buildplan.json must exist and be valid before the review can
  pass. Run `kbu buildplan validate subprojects/<name>/buildplan.json` to check.

## Integration

- **Reads from**: `RESEARCH_PLAN.md` (p-review), `notebooks/` + `buildplan.json` (b-review), `REPORT.md` (s-review)
- **Produces**: `subprojects/<name>/REVIEW_<file_stage>_<n>.md`
- **Advances or reverses**: stage via `kbu subproject advance [--reverse]`
- **Consumed by**: researcher iterates until pass; final pass unlocks next stage
