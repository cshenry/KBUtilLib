<!--
kbu skill provenance
type: rewrite
source_repo: KBUtilLib
last_reviewed: 2026-06-09
-->

# /kbu-build — Build Subproject Helpers (Conductor)

You are the build conductor. You fan out work to specialist subagents, gate on
verified test results, and assemble each notebook as a thin orchestration layer
calling the verified helpers. You do NOT implement helper functions, write
tests, or review code in this thread.

---

## MANDATORY SUBAGENT DELEGATION RULE

Every delegated step in this skill runs through an explicit
`Agent(subagent_type="kbu-sub-…", prompt=…)` call written at the exact point
it executes. A prose instruction ("implement this helper"), a mental summary,
or a /slash cross-reference that you satisfy inline does NOT count.

> **STOP tell-sign:** If you are building notebook code, writing util.py
> implementations, reviewing test output, or diagnosing a failure in the main
> thread — STOP. You skipped the subagent. Go back and issue the `Agent(...)`
> call.

---

## Precondition

The current subproject must be in state `build`. Verify:

```bash
kbu subproject status <name>
```

If the state is not `build`, tell the user to run `/kbu-plan` first (which
advances the subproject through `plan` → `p-review` → `build`).

Also confirm that `buildplan.json` exists:

```bash
ls subprojects/<name>/buildplan.json
```

If either precondition is unmet, stop and tell the user what is missing.

---

## Phase 1: Load and Validate the Buildplan

Read `subprojects/<name>/buildplan.json`:

```bash
cat subprojects/<name>/buildplan.json
```

Then run the authoritative validator:

```bash
kbu buildplan validate subprojects/<name>/buildplan.json
```

**If validation fails:** Show all errors to the user. Do NOT proceed with
building until every error is resolved and `kbu buildplan validate` exits 0.
The buildplan is the contract for all downstream subagents — a corrupt
contract produces corrupt implementations.

Parse out the notebook list with its `depends_on` topology. Present a summary:

- Number of notebooks to build
- Each notebook's slug, purpose, and how many helpers it defines
- Any dependency chains (notebook B depends on notebook A)

---

## Phase 2: Fan-Out Builder Subagents (depends_on topological order)

Process notebooks in the order they appear in `buildplan.json["notebooks"]`
(the validator guarantees this is already topologically sorted — each entry's
`depends_on` items appear strictly earlier in the list).

Group notebooks by their position in the dependency chain:

- **Independent notebooks** (those whose `depends_on` list is empty, or whose
  every dependency has already been processed) can be dispatched in parallel.
- **Dependent notebooks** must wait until all notebooks they depend on have a
  green build before dispatching.

For each notebook (or each parallel group), spawn the developer subagent:

```
Agent(subagent_type="kbu-sub-build", prompt="Implement helpers for one notebook.

subproject_path: subprojects/<name>
buildplan_entry: <full JSON object for this notebook, verbatim from buildplan.json>
")
```

### Handling BLOCKED signals

If a subagent's final message begins with the literal token `BLOCKED:`, it has
hit a genuine scientific or algorithmic fork. The format is:

```
BLOCKED: <helper_name> — <one-sentence description of the fork>.
options:
  A) <option A description>
  B) <option B description>
```

When you receive a `BLOCKED:` signal:

1. Present the decision statement and all options to the researcher verbatim.
   Do not paraphrase or resolve the fork yourself.
2. Wait for their answer (e.g. "A" or "option B").
3. Re-dispatch the **same** subagent with `DECISION: <chosen option>` appended
   to the original prompt:

```
Agent(subagent_type="kbu-sub-build", prompt="Implement helpers for one notebook.

subproject_path: subprojects/<name>
buildplan_entry: <full JSON object for this notebook, verbatim from buildplan.json>

DECISION: <chosen option as the researcher stated it>
")
```

A `BLOCKED:` signal is not an error — it is the subagent doing its job by
surfacing a decision that only the researcher can make.

### Handling work-records with failures

If the subagent returns a work-record (normal path) but marks any helper as
`fail`, proceed to Phase 3 (per-notebook review + diagnose loop) for that
notebook immediately. Don't wait to batch failures.

---

## Phase 3: Per-Notebook Review and Diagnose Loop

After each notebook's builder subagent returns a work-record (not `BLOCKED:`),
spawn the reviewer subagent to verify the helpers against the buildplan test
cases:

```
Agent(subagent_type="kbu-sub-review", prompt="<name>")
```

The subagent runs `kbu buildplan validate`, checks `test_util.py`, runs pytest,
and writes `subprojects/<name>/REVIEW_build_<n>.md` with a verdict on the
first line:

```
<!-- kbu-review:verdict: pass|fail -->
```

After the subagent returns, confirm the verdict file exists and read its first
line:

```bash
ls subprojects/<name>/REVIEW_build_*.md | sort -V | tail -1
head -1 "$(ls subprojects/<name>/REVIEW_build_*.md | sort -V | tail -1)"
```

**If `<!-- kbu-review:verdict: pass -->`:** Record the pass for this notebook,
proceed to the next notebook (or Phase 4 if all are done).

**If `<!-- kbu-review:verdict: fail -->`:** Enter the diagnose-retry loop:

### Diagnose-retry loop (max 2 retries)

**Retry attempt (up to 2 total):**

1. Spawn the diagnose subagent:

```
Agent(subagent_type="kbu-sub-diagnose", prompt="Diagnose failing tests for one notebook.

subproject_path: subprojects/<name>
notebook_slug: <slug>
review_file: subprojects/<name>/REVIEW_build_<n>.md
")
```

2. After the diagnose subagent returns, re-dispatch the builder subagent with
   the original prompt (no `DECISION:` suffix unless there is a pending one):

```
Agent(subagent_type="kbu-sub-build", prompt="Implement helpers for one notebook.

subproject_path: subprojects/<name>
buildplan_entry: <full JSON object for this notebook, verbatim from buildplan.json>
")
```

3. Spawn the reviewer subagent again:

```
Agent(subagent_type="kbu-sub-review", prompt="<name>")
```

4. Check the new verdict file.

**After 2 failed retries for the same notebook:** Stop the loop. Do not spin
indefinitely. Present the researcher with:

- The notebook slug that is stuck
- The last review file path (`REVIEW_build_<n>.md`)
- A plain summary of the critical issues from that review
- A recommendation: either fix the buildplan helper contracts (update
  `buildplan.json` and re-run `/kbu-build` from Phase 1), or address the
  implementation issue manually before re-running

Do not advance the subproject until the researcher confirms how to proceed.

---

## Phase 4: Assemble Notebooks

Once all notebooks have a passing per-notebook build (Phase 2–3), assemble
each notebook file. The assembled notebook is a thin orchestration layer —
it calls the verified helpers from `util.py`; it does NOT reimplement them.

For each notebook slug in the buildplan (in order):

Write `subprojects/<name>/notebooks/<slug>.ipynb` as a valid Jupyter notebook
(JSON format, `nbformat: 4`) containing:

1. **Title cell** (markdown): `# <purpose from buildplan entry>`
2. **Project root cell** (code):
   ```python
   from pathlib import Path
   import sys
   PROJECT_ROOT = Path(__file__).resolve().parents[2]
   sys.path.insert(0, str(PROJECT_ROOT / "subprojects" / "<name>" / "notebooks"))
   ```
3. **Imports cell** (code): standard library imports + `import util`
4. **Data loading cell** (code): load inputs from `PROJECT_ROOT / "data" / ...`
5. **Helper calls** (code): one cell per helper defined in the buildplan entry,
   calling `util.<helper_name>(...)` with a `# TODO: supply arguments` comment
   where the caller must fill in the actual arguments
6. **Summary cell** (markdown): what this notebook produces and what the next
   notebook consumes (from `depends_on` of downstream notebooks)

All helper-call cells use `util.<name>` — they call the implementations in
`util.py`, never inline the logic. Never execute the notebook; never use
`kbu notebook run`.

After writing all notebooks, verify each is valid JSON:

```bash
python3 -c "
import json, pathlib, sys
nb_dir = pathlib.Path('subprojects/<name>/notebooks')
for nb in sorted(nb_dir.glob('*.ipynb')):
    try:
        data = json.loads(nb.read_text())
        print(f'OK  {nb.name}  ({len(data[\"cells\"])} cells)')
    except Exception as e:
        print(f'ERR {nb.name}  {e}')
        sys.exit(1)
"
```

Fix any JSON errors before continuing.

---

## Phase 5: Closing Build Review Gate

With all notebooks assembled, spawn one final closing review:

```
Agent(subagent_type="kbu-sub-review", prompt="<name>")
```

This pass confirms that every buildplan helper has a passing test and every
notebook is assembled. After the subagent returns, confirm the verdict file
on disk:

```bash
ls subprojects/<name>/REVIEW_build_*.md | sort -V | tail -1
head -1 "$(ls subprojects/<name>/REVIEW_build_*.md | sort -V | tail -1)"
```

**If fail:** Present the critical issues to the researcher. Address them (add
another diagnose + builder cycle if needed), then re-spawn the reviewer. Do
NOT advance until a `pass` verdict file exists.

**If pass:** Confirm by grepping for the verdict marker:

```bash
grep "kbu-review:verdict: pass" "$(ls subprojects/<name>/REVIEW_build_*.md | sort -V | tail -1)"
```

This must return a match before advancing. A passing inline assessment does
not satisfy this gate.

---

## Phase 6: Advance and Save Session

Only after `grep` confirms a `REVIEW_build_*.md` file contains
`<!-- kbu-review:verdict: pass -->`, advance the subproject **through its
review stage to `run`** (so it is ready for `/kbu-run`, whose precondition is
state `run`). Both gated transitions are satisfied — the assembled notebooks +
`util.py` exist (Phase 4) and a passing `REVIEW_build_*.md` exists (Phase 5):
- `build → b-review` (gate: assembled notebooks + `util.py` exist)
- `b-review → run` (gate: a passing `REVIEW_build_*.md` exists)

```bash
kbu subproject advance <name>   # build → b-review
kbu subproject advance <name>   # b-review → run
kbu subproject status <name>    # confirm: state must now be "run"
kbu session save --skill kbu-build --subproject <name> --summary "<one-sentence summary: N notebooks assembled, all helpers tested>"
```

If `kbu subproject status` does not report `run` after the two advances, STOP
and report the actual state — do not claim the build stage is complete.

Tell the user:
- Files written: list each `<slug>.ipynb`, `util.py`, `test_util.py`, and all
  `REVIEW_build_<n>.md` files
- The new subproject state (should be `run`)
- That they can now open notebooks in JupyterLab and supply the TODO arguments,
  then run `/kbu-run` for guided execution

---

## Verify-and-Extend Mode (Adopted Branches)

After Phase 1, check whether notebooks already exist:

```bash
ls subprojects/<name>/notebooks/*.ipynb 2>/dev/null
```

If `.ipynb` files are present (from `kbu subproject adopt` + `/kbu-migrate` or
a prior build pass):

1. Collect slugs from `buildplan.json["notebooks"]`.
2. Collect filenames from `subprojects/<name>/notebooks/*.ipynb`.
3. For each buildplan slug that has no corresponding `.ipynb`, emit:
   `Manifest lists missing notebook: <slug>` — these will be built in Phase 4.
4. For each `.ipynb` whose name does not appear as a buildplan slug, emit:
   `Notebook present but not in buildplan: <filename>` — warn only, do not delete.
5. Report the warning summary.
6. Proceed through Phases 2–6 normally for slugs that need helpers built.
   Skip Phase 4 notebook assembly for slugs whose `.ipynb` already exists
   (unless the user explicitly requests a re-assembly).

---

## Rules

1. **No inline implementation.** Helper functions, test code, and review
   assessments run in subagents. If you write `def <helper_name>` or
   `assert ...` or read pytest output in this thread, stop and re-read the
   STOP tell-sign.
2. **No notebook execution.** Use `pytest test_util.py` (in the builder
   subagent). Never use `kbu notebook run`.
3. **Topological order.** Dispatch notebooks in depends_on order. Never
   dispatch a dependent notebook until all its dependencies have a green build.
4. **Verdict file is the gate.** `kbu subproject advance` runs exactly once,
   after `grep` confirms a `REVIEW_build_*.md` file contains
   `<!-- kbu-review:verdict: pass -->`. An inline assessment is not a verdict.
5. **The skill advances, not the reviewer.** `kbu-sub-review` writes the
   verdict file and stops. Stage advancement belongs here.
6. **2-retry bound.** The diagnose-retry loop runs at most 2 times per
   notebook. After that, escalate to the researcher.
7. **BLOCKED escalation is mandatory.** Never resolve a `BLOCKED:` fork
   yourself. Always surface it to the researcher verbatim and wait.
8. **No files outside `subprojects/<name>/`.** Do not write to repo root or
   shared `data/`.
9. **Cross-reference by slash-command.** Use `/kbu-plan`, `/kbu-run`,
   `/kbu-diagnose` when pointing to other skills.
