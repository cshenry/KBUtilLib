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
| `p-review` | Plan review | `RESEARCH_PLAN.md` | `REVIEW_p-review_<n>.md` |
| `b-review` | Build review | Scaffolded notebooks | `REVIEW_b-review_<n>.md` |
| `s-review` | Synthesis review | `REPORT.md` | `REVIEW_s-review_<n>.md` |

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

**b-review (build):** Read `subprojects/<name>/notebooks/` (list and read each `.ipynb`). Assess:
- Are the notebooks well-structured and commented?
- Does each notebook have a clear purpose linked to the research plan?
- Are outputs (CSVs, figures) written to the correct locations (`data/`, `figures/`)?
- Are there obvious errors or incomplete cells?

**s-review (synthesis):** Read `subprojects/<name>/REPORT.md`. Assess:
- Do the Key Findings answer the research question?
- Are results stated with specific numbers (p-values, effect sizes, counts)?
- Is each finding traced to a notebook and figure?
- Does the Interpretation section engage with the literature?
- Are limitations acknowledged?
- Is the References section populated?

### Step 3: Produce the Review

Determine the next sequential number for this stage by scanning existing review files:

```bash
ls subprojects/<name>/REVIEW_<stage>_*.md 2>/dev/null | sort -V | tail -1
```

If none exist, start at 1.

Write `subprojects/<name>/REVIEW_<stage>_<n>.md` with this structure:

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
    'decisions_made': ['verdict: pass|fail', 'REVIEW_<stage>_<n>.md written'],
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
> `subprojects/<name>/REVIEW_<stage>_<n>.md`, then re-run the review subagent."

## Notes

- Reviews are numbered sequentially per stage: `REVIEW_p-review_1.md`,
  `REVIEW_p-review_2.md`, etc. Previous reviews are preserved.
- Each review file begins with `<!-- kbu-review:verdict: pass|fail -->` so automated
  tools can parse the outcome without reading the full file.
- Re-run `/kbu-synthesize` before requesting a new `s-review` if `REPORT.md` has
  changed significantly since the last review.

## Integration

- **Reads from**: `RESEARCH_PLAN.md` (p-review), `notebooks/` (b-review), `REPORT.md` (s-review)
- **Produces**: `subprojects/<name>/REVIEW_<stage>_<n>.md`
- **Advances or reverses**: stage via `kbu subproject advance [--reverse]`
- **Consumed by**: researcher iterates until pass; final pass unlocks next stage
