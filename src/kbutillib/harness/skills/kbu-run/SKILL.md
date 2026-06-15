---
name: kbu-run
description: >
  Use when running a local COBRA/MSModelUtil modeling project that has been
  pulled from a BERIL clone into a kbu harness — to execute notebooks
  programmatically, verify outputs, keep the DEVLOG, and push results back
  to BERIL after user confirmation.
allowed-tools:
  - Read
  - Bash
user-invocable: true
---

# /kbu-run — kbu harness execution loop

This skill drives the full harness workflow for a project that was designed
and sampled in BERIL and is now ready for a complete local run.  It carries
the judgment the CLI deliberately lacks: classification, compute-location
choice, output verification, and escalation on failure.

Do NOT call the Anthropic API or invoke `claude` as a subprocess.  You ARE
the Claude worker; read all content directly into this context.

---

## Step 1 — Pull (design-deploy step)

Refresh the harness from the BERIL clone:

```bash
kbu harness pull
```

This rsyncs `<BERIL_ROOT>/projects/<project-id>/` into the harness working
tree (notebooks, data, figures, user_data, .kbcache/).  If `pull` exits
non-zero, stop here and report the error verbatim; do not proceed to
classification.

---

## Step 2 — Classify the run

Read the graduated-execution policy thresholds from the harness preferences
file:

```
Read .claude/kbu/preferences.md
```

Parse **only the fenced ```yaml``` block** in that file.  Extract the two
classification thresholds:

- `execution.runtime_threshold_seconds` — the wall-clock boundary (seconds)
  above which a run is considered expensive.
- `execution.fanout_threshold` — the number of notebook-level tasks above
  which a run is considered large fan-out.

If the file is absent, the YAML block is missing, or either key is absent,
**default to 🔴 full / consult** (most conservative).

Also read the first markdown cell of each notebook for author-supplied
execution hints (e.g., `<!-- scope: full -->` or `<!-- scope: sample -->`).
Explicit author hints override the threshold-based estimate.

Apply the PRD-A graduated-execution tiers:

| Tier | Colour | Condition | Action |
|------|--------|-----------|--------|
| cheap / certain | 🟢 | Estimated wall-clock < threshold AND notebook count < fanout limit AND no 🟡/🔴 author hint | Run without consulting; note in report |
| sample-then-consult | 🟡 | Estimated wall-clock between threshold and 3× threshold OR fan-out at limit | Describe the concern; ask the user before running |
| full / consult | 🔴 | Estimated wall-clock > 3× threshold OR fan-out exceeds limit OR uncertain | Stop, describe the expected cost, ask the user to confirm before proceeding |

The harness is the designated location for 🔴 full runs; it is appropriate
to run 🔴 work here.  Still surface the estimated cost so the user can
choose compute location.

---

## Step 3 — Choose compute location

Present the cost estimate from Step 2 and ask the user to choose where the
run should execute.  Offer both options every time; do not encode a default:

- **local** — run on this machine now using `kbu harness run --on local`
- **h100** — dispatch to the lab h100 via ai-cowork using `kbu harness run --on h100`

For 🟢 cheap/certain runs you may suggest local as the obvious choice, but
still ask.

If the user chooses **h100**:

```bash
kbu harness run --on h100
```

This writes a cowork task file to the h100 inbox and returns immediately.
There is no local execution.  Print the task file path from the command
output, then STOP and tell the user to re-invoke `/kbu-run` after Dropbox
returns the completed run (the executed notebooks will appear in the harness
tree via Dropbox sync; no polling is needed here).

---

## Step 4 — Run

For a **local** run:

```bash
kbu harness run --on local
```

If specific notebooks should be targeted (e.g., only a subset after a prior
failure), pass them explicitly:

```bash
kbu harness run --on local notebooks/00_setup.ipynb notebooks/01_model.ipynb
```

Capture the full terminal output.  `kbu harness run` stops at the first
failure and returns a structured `RunResult` per notebook.

---

## Step 5 — Verify outputs

Read the RunResult output from Step 4.  For each notebook in the result,
confirm both:

1. `executed: true` — nbconvert completed without error.
2. `outputs_present: true` — the executed notebook has at least one code
   cell with non-empty outputs (stream, execute_result, or display_data).

If `--json` output is available, parse it; otherwise read the human-readable
summary lines.

If ALL notebooks pass both checks → proceed to Step 6 (SUCCESS path).

If ANY notebook has `executed: false` or `outputs_present: false` → go
directly to the FAILURE path below.

---

## Step 6 — Append DEVLOG entry

Append a run entry to `DEVLOG.md` at the harness root:

```bash
kbu harness run  # (already ran above; this step records the outcome)
```

The DEVLOG entry is written automatically by `kbu harness run`.  Verify the
entry was appended by reading the last entry in `DEVLOG.md`:

```
Read DEVLOG.md
```

The entry must follow this format:

```
## <ISO-8601 UTC timestamp with trailing Z> — run

```yaml
notebooks: [<list of notebooks that ran>]
scope: sample|full
where: local|h100
outcome: ok|failed
runtime_s: <float>
```
```

If the entry is missing, append it manually with the correct fields.

---

## Step 7 — Branch: SUCCESS path

If all notebooks executed with outputs:

1. Report the outcome: list each notebook with its `runtime_s` and confirm
   `outputs_present: true`.

2. Emit this exact single-line prompt and wait for the user:

   ```
   Push results back to BERIL now? (y/N)
   ```

3. If the user responds `y` or `Y`:

   ```bash
   kbu harness push
   ```

   This rsyncs the executed notebooks (with outputs), data, figures, and
   .kbcache back into `<BERIL_ROOT>/projects/<project-id>/`.

   After push completes, remind the user:

   > Notebooks are now in BERIL.  Run `git add` and commit the executed
   > notebooks there — BERIL requires committing notebooks WITH outputs
   > before `/submit`.

4. If the user responds anything other than `y` or `Y`, skip the push and
   tell the user they can run `kbu harness push` manually when ready.

---

## Step 7 — Branch: FAILURE path

If any notebook failed (non-zero exit code or outputs absent):

1. **Stop immediately.  Edit no code.**  Do not attempt to diagnose or fix
   the failure.  Do not modify any notebook cell source or `util.py`.

2. Append the traceback to the DEVLOG entry.  The DEVLOG `traceback: |`
   block must contain the nbconvert stderr (trimmed to 10 000 bytes) for the
   failing notebook.  If `kbu harness run` has not already written this,
   append it to the most recent `DEVLOG.md` entry as:

   ```yaml
   traceback: |
     <stderr content, ≤10 000 bytes>
   ```

3. Escalate a BLOCKED report:

   ```
   BLOCKED — <notebook name> failed.

   Outcome: <exit_code>, outputs_present: <true|false>
   Traceback (first 500 chars):
   <first 500 chars of stderr>

   No code was edited.  Fix the failure in BERIL, then re-run /kbu-run.
   ```

4. Do not attempt to push partial results.  Stop here.

---

## Reference — graduated-execution policy tiers (PRD A)

These tiers originate in `kbu-beril-augmentation` (PRD A) and are honoured
here in the harness:

- **🟢 cheap / certain:** minimal cost, well-understood; run freely.
- **🟡 sample-then-consult:** run a representative sample, then pause and
  report before committing to the full computation.
- **🔴 full / consult:** expensive or uncertain; stop and confirm with the
  user before running.  The harness is the appropriate location for 🔴 runs;
  confirming here means confirming compute location and cost, not deferring
  to BERIL.

Classification is the skill's judgment.  When uncertain, always choose 🔴.
