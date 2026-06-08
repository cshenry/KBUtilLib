<!--
kbu skill provenance
type: harvested
source_repo: BERIL-research-observatory
source_commit: 940c3b0ee7bbf63bc576bd6e8c25210ad692df8e
source_path: .claude/skills/synthesize/SKILL.md
last_reviewed: 2026-06-05
-->

---
name: kbu-synthesize
description: Read notebook outputs, compare against literature, and draft findings for a subproject REPORT.md. Use after notebooks have been run and the researcher wants to interpret results and write up findings.
allowed-tools: Bash, Read, Write, Edit, WebSearch, AskUserQuestion
---

# kbu-synthesize

After notebooks have been run, read the outputs, compare against literature, and draft
findings in the subproject's `REPORT.md`. Also update `## Status` in `RESEARCH_PLAN.md`
to reflect that synthesis is complete.

## Usage

```
/kbu-synthesize <subproject_name>
```

If no `<subproject_name>` is provided, detect from the current working directory
(if inside `subprojects/<name>/`).

## Precondition

The subproject must be in the `synthesize` stage. Check via:

```bash
kbu subproject status <name>
```

If the stage is earlier than `synthesize`, stop and tell the researcher:

> "This subproject is not ready to synthesize — run your analysis notebooks first,
> then advance to the synthesize stage via `kbu subproject advance <name>`."

If the stage is `synthesize` or later, proceed. If re-synthesizing (stage already past
`synthesize`), note that existing reviews may become stale.

## Workflow

### Step 1: Gather Subproject Context

Read:
1. `subprojects/<name>/RESEARCH_PLAN.md` — hypothesis, expected outcomes, analysis plan
2. `subprojects/<name>/references.md` — existing literature (if present)

Note the research question and hypothesis from RESEARCH_PLAN.md for use in Steps 3–5.

### Step 2: Read Analysis Outputs

Scan the subproject for results:

1. **CSV files** in `subprojects/<name>/data/`:
   - Read each CSV and interpret: column names, row counts, distributions, key statistics.
   - Identify the main result variables (correlations, counts, p-values, effect sizes).

2. **Figures** in `subprojects/<name>/figures/`:
   - List available figures; infer content from filenames.

3. **Notebook outputs** in `subprojects/<name>/notebooks/`:
   - If executed `.ipynb` files are present, read output cells for results.
   - Look for printed summaries, DataFrames, and statistical test outputs.

### Step 3: Draft Initial Findings

Based on the data, draft findings that address:

1. **Key results**: What did the data show? (specific numbers, correlations, counts)
2. **Hypothesis outcome**: Was H1 supported or H0 not rejected?
3. **Statistical significance**: Report p-values, effect sizes, confidence intervals where available.
4. **Unexpected patterns**: Note any surprising results or anomalies.

### Step 4: Present Draft to Researcher

Show the initial findings interpretation and ask:
- "Does this interpretation look correct?"
- "Are there results I missed or misinterpreted?"
- "Any additional context to include?"

Wait for feedback and revise if needed.

### Step 5: Literature Cross-Reference

Call `/kbu-literature-review` to search for papers that:
- Tested similar hypotheses in related organisms
- Used comparable methods or data
- Reported results that align or conflict with the findings

For each key finding, assess:

| Question | Assessment |
|---|---|
| Does this agree with published work? | Cite supporting papers |
| Does this contradict published work? | Note methodology differences |
| Is this novel? | Identify what the data adds |
| Are there caveats? | Data coverage, confounders, limitations |

### Step 6: Write REPORT.md

Create or update `subprojects/<name>/REPORT.md`:

```markdown
# Report: {Title}

## Key Findings

### {Finding 1 Title}

![Description of figure](figures/relevant_figure.png)

{Statistical result with specific numbers}

*(Notebook: {notebook_name}.ipynb)*

### {Finding 2 Title} (if applicable)

{Statistical result}

*(Notebook: {notebook_name}.ipynb)*

## Results
{Detailed results with embedded figures and markdown tables}

## Interpretation

### Literature Context
- {Finding} aligns with Author et al. (Year) who found {similar result}
- {Finding} contradicts Author et al. (Year) — possible explanation: {methodology difference}

### Novel Contribution
{What this analysis adds that wasn't previously known}

### Limitations
- {Data coverage limitations}
- {Potential confounders}
- {Methodological caveats}

## Data

### Generated Data
| File | Rows | Description |
|------|------|-------------|
| `data/{filename}.csv` | {row_count} | {what the data contains} |

## Supporting Evidence

### Notebooks
| Notebook | Purpose |
|----------|---------|
| `{filename}.ipynb` | {what the notebook does} |

### Figures
| Figure | Description |
|--------|-------------|
| `{filename}.png` | {what the figure shows} |

## Future Directions
1. {Suggested next step based on findings}
2. {Follow-up analysis addressing limitations}

## References
- Author et al. (Year). "Title." *Journal*. PMID: {pmid}
```

**Guidelines:**
- Place `![description](figures/filename.png)` near the finding each figure supports.
- End each finding subsection with `*(Notebook: filename.ipynb)*` for traceability.
- Every figure in `figures/` should appear inline at least once.

### Step 7: Update references.md

Add any new papers found during synthesis to `subprojects/<name>/references.md`.
If the file doesn't exist, create it following the format from `/kbu-literature-review`.

### Step 8: Advance Stage and Save Session

```bash
kbu subproject advance <name>
```

Then save a session record:

```python
from assistant.state import save_session
save_session({
    'project_id': '<subproject_name>',
    'command': 'kbu-synthesize',
    'topics_discussed': ['synthesis', 'literature comparison'],
    'decisions_made': ['findings drafted', 'REPORT.md written'],
    'next_steps': ['run /kbu-review to get feedback on the report'],
    'summary': 'Synthesized analysis outputs into REPORT.md with literature context',
})
```

### Step 9: Suggest Next Steps

> "Findings drafted in `subprojects/<name>/REPORT.md`. Next steps:
> 1. Review the Key Findings and Interpretation sections.
> 2. Run `/kbu-review` to get feedback on the report.
> 3. Iterate on the report until the review passes, then the subproject advances automatically."

## Integration

- **Reads from**: `subprojects/<name>/data/*.csv`, `figures/`, `notebooks/*.ipynb`, `RESEARCH_PLAN.md`, `references.md`
- **Calls**: `/kbu-literature-review`
- **Produces**: `subprojects/<name>/REPORT.md`; updated `references.md`
- **Consumed by**: `/kbu-review`
