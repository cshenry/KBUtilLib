<!--
kbu skill provenance
type: net-new
created: 2026-06-05
-->

# /kbu-run — execute project notebooks

Use this command to run the analysis notebooks for a subproject that is in the
`run` state. It presents a menu, executes the chosen notebook, narrates the
output, and advances the subproject when all notebooks are complete.

## Precondition

The target subproject must be in state `run`. If no subproject is in `run`
state, inform the student and suggest they complete the build and build-review
steps first (via `/kbu-build` and `/kbu-review`), then exit.

## Step 1 — list notebooks

```bash
kbu notebook list --json
```

Parse the JSON array. Each record has:

- `path` — absolute path to the `.ipynb` file
- `subproject` — subproject name
- `last_run_at` — ISO-8601 timestamp or empty string
- `modified_since_run` — boolean

Filter to subprojects that are in `run` state (use the subproject list from
`kbu subproject list --json` to identify them).

If no notebooks are found for any `run`-state subproject, tell the student
that no notebooks are registered. They may need to create notebooks under
`subprojects/<name>/notebooks/` and re-enter the build step.

## Step 2 — present the notebook menu

Build a display label for each notebook:

```
<basename>  (subproject: <name>)  [last run: <last_run_at or "never">]  [modified: <yes/no>]
```

Use AskUserQuestion with one entry per notebook plus a "Cancel" option.

Notebooks that have `modified_since_run=true` or `last_run_at=""` are
highlighted as needing a run. Present all notebooks regardless of run status
so the student can re-run if desired.

## Step 3 — execute the chosen notebook

Run:

```bash
kbu notebook exec <path>
```

Where `<path>` is the absolute path from the notebook record.

While the notebook is executing, narrate progress. `kbu notebook exec` runs
all cells sequentially and writes output back to the notebook file. After it
returns, read the executed notebook's output cells (the `.ipynb` JSON) to
summarise results.

For each code cell that produced output, write at most one short paragraph
describing what happened (what the code computed, key numbers or plots
produced, any warnings or errors). Skip markdown-only cells. Keep the total
narration concise — prefer bullet points for multi-value outputs.

If `kbu notebook exec` exits with a non-zero status (cell error or timeout),
report the error message verbatim, tell the student which cell failed, and
suggest they fix the notebook before re-running.

## Step 4 — check completion

After a successful exec, re-run:

```bash
kbu notebook list --json
```

Check all notebooks in the subproject:

- `last_run_at` is set (non-empty) for every notebook, AND
- `modified_since_run` is `false` for every notebook.

If both conditions hold for every notebook in the subproject, the subproject
can advance to synthesize. Tell the student, then run:

```bash
kbu subproject advance <name>
```

On success (`<name>: run → synthesize`), immediately invoke `/kbu-synthesize`
to continue the workflow.

If some notebooks still need to be run, show the updated table and offer to
run another.

## Step 5 — save session

At the end of the skill (whether a notebook was executed or the student
cancelled), save a session record:

```bash
kbu session save \
  --skill kbu-run \
  --subproject <name> \
  --summary "<one-sentence summary of what was run and its outcome>"
```

If the student cancelled before running anything, use `<name>` for the
subproject that was selected (or the first run-state subproject if none was
selected) and note "Session ended without executing a notebook."
