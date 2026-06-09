<!--
kbu skill provenance
type: original
source_repo: KBUtilLib
source_commit: 2aba905a467882fd70d76b2fb3863b7a6263a144
source_path: agent-io/prds/kbutillib-v2/fullprompt.md
last_reviewed: 2026-06-09
-->

# /kbu-migrate — Migrate Adopted Subproject

---

## MANDATORY SUBAGENT DELEGATION RULE

Every delegated step in this skill runs through an explicit
`Agent(subagent_type="kbu-sub-…", prompt=…)` call written at the exact point
it executes. A prose instruction ("review the plan"), a mental summary, or a
/slash cross-reference that you satisfy inline does NOT count.

> **STOP tell-sign:** If you are reading papers, writing review prose, building
> notebook code, or diagnosing a failure in the main thread — STOP. You skipped
> the subagent. Go back and issue the `Agent(...)` call.

---

You help a researcher migrate an existing notebook directory that was adopted via
`kbu subproject adopt` into a fully structured KBUtilLib subproject. Unlike `/kbu-plan`
(which starts from scratch), `/kbu-migrate` reads what already exists in `archive/`,
infers the apparent research hypothesis from that content, and walks through both the
standard planning steps and migration-specific integration: path rewrites, `util.py`
audit, and `NotebookSession` initialization.

At the end, the subproject has the same artifact contract as `/kbu-plan`:
`RESEARCH_PLAN.md`, `literature/`, `TASKS.md`, and a populated manifest.

## Precondition

The subproject must be in state `migrate` (set by `kbu subproject adopt`). Verify
before proceeding:

```bash
kbu subproject status <name>
```

If the state is not `migrate`, tell the user which step is needed first. If the
subproject does not exist at all, direct them to `kbu subproject adopt <path> --name <name>`.

## Phase 1: Read Existing Artifacts

Scan `archive/` for all notebooks and supporting content. Read the adoption worksheet:

```bash
cat subprojects/<name>/.adoption-notes.md
find subprojects/<name>/archive -name "*.ipynb" | sort
```

For each `.ipynb` found, read its first markdown cell to extract stated purpose or
description. Read `archive/util.py` if present:

```bash
python3 -c "
import nbformat, pathlib, sys
nb_dir = pathlib.Path('subprojects/<name>/archive')
for nb in sorted(nb_dir.rglob('*.ipynb')):
    if '.ipynb_checkpoints' in nb.parts:
        continue
    try:
        nb_data = nbformat.read(nb, as_version=4)
        md_cells = [c for c in nb_data.cells if c.cell_type == 'markdown']
        first_md = md_cells[0].source[:400] if md_cells else '(no markdown cell)'
        print(f'=== {nb.relative_to(nb_dir)} ===')
        print(first_md)
        print()
    except Exception as e:
        print(f'ERR {nb}: {e}', file=sys.stderr)
"
```

Summarize in one paragraph: how many notebooks, what topics appear to be covered,
what non-notebook content is present in `archive/`, and any oversize files flagged
in `.adoption-notes.md`.

## Phase 2: Infer Hypothesis — Confirm with Researcher

From the notebook content and `.adoption-notes.md`, propose the apparent research
hypothesis or question in one or two sentences. Present it to the user:

> "Based on the notebooks in `archive/`, the apparent research question is: [inferred
> hypothesis]. Does this match your intent, or should we revise it?"

Hold this conversation — do not proceed to Phase 3 until the hypothesis is confirmed
or corrected. Keep grilling until all of these are resolved:

- What is the specific falsifiable hypothesis or research question?
- What are the success criteria — how will you know if the hypothesis is supported?
- What is explicitly out of scope for this subproject?

## Phase 3: Literature Review

Invoke the literature review subagent with the confirmed research topic. The subagent
searches PubMed, arXiv, bioRxiv, and Google Scholar, then writes per-topic synthesis
files into `subprojects/<name>/literature/`.

```
Agent(subagent_type="kbu-sub-literature-review", prompt="Conduct a standard review for the following subproject.

Subproject path: subprojects/<name>
Research question: <confirmed hypothesis from Phase 2>
Topics to cover (seed from notebook subject matter): <topic 1>, <topic 2>, ...
Output directory: subprojects/<name>/literature/
Review depth: standard review
")
```

Wait for the subagent to complete. It writes:
- `subprojects/<name>/literature/<topic-slug>.md` — one file per discrete topic
- `subprojects/<name>/literature/index.md` — topic index

Report back to the user with a brief summary of topics covered and any gaps the
literature review surfaced.

## Phase 4: Path and Data Relocation Pass

This phase walks through the non-notebook content in `archive/` and proposes
destinations, then rewrites in-notebook relative paths. This implements AC #52:
path rewrites are project-root relative.

### Step 4a: Inventory non-notebook items

For each non-notebook item listed in `.adoption-notes.md` (data directories, figures,
cache directories, individual data files), propose a destination from this set:

| Destination | When to use |
|---|---|
| Root `data/<filename>` | Shared or reusable input data (default) |
| Root `data/<subproject>/<filename>` | Input data scoped to this subproject |
| Root `models/<filename>` | Trained model files (.pkl, .h5) |
| Root `genomes/<filename>` | Genome or reference sequence files |
| `subprojects/<name>/figures/<filename>` | Pre-existing figure outputs |
| `subprojects/<name>/nboutput/<filename>` | Intermediate notebook outputs |
| Stay in `archive/` | Items to review later or discard |

For each item, present the proposal and ask the user to confirm, redirect, or skip.
Do not move anything without explicit user confirmation.

### Step 4b: Execute approved moves

For each approved relocation, move the item:

```bash
mv subprojects/<name>/archive/<item> <destination>
```

### Step 4c: Rewrite in-notebook path references (AC #52)

For each notebook in `archive/`, after relocating data items, rewrite any relative
path references so they resolve from the project root. The rewrite uses the
`PROJECT_ROOT` anchor pattern:

- Anchoring formula: for a notebook at `subprojects/<name>/notebooks/foo.ipynb`,
  the project root is `Path(__file__).resolve().parents[2]` (counting up two
  directory levels: `notebooks/` → `subprojects/<name>/` → project root).
- Rewrite `pd.read_csv("data/foo.tsv")` →
  `pd.read_csv(PROJECT_ROOT / "data" / "foo.tsv")`
- Rewrite `open("../data/foo.tsv", ...)` →
  `open(PROJECT_ROOT / "data" / "foo.tsv", ...)`
- Rewrite `Path("data/foo.tsv")` → `Path(PROJECT_ROOT / "data" / "foo.tsv")`
- Rewrite `np.load("data/foo.npy")` → `np.load(PROJECT_ROOT / "data" / "foo.npy")`
- Apply the same pattern to `joblib.load(...)` and other `pd.read_*` variants.

For each rewrite, present the before/after to the user before writing. If the user
selects per-subproject namespacing for a data file, use `PROJECT_ROOT / "data" / "<subproject>" / "<filename>"` instead.

After completing rewrites in a notebook, insert a `PROJECT_ROOT` constant at the
top of the notebook's first code cell (or add a new first code cell if needed):

```python
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
```

If `util.py` already exists in `archive/`, add or update `PROJECT_ROOT` there
instead and have notebooks import it:

```python
from util import PROJECT_ROOT
```

## Phase 5: `util.py` Audit

Scan `archive/util.py` (or any `util.py` at the top level of `archive/`) for
helper functions. Categorize each:

| Category | Action |
|---|---|
| Duplicates a KBUtilLib module function | Propose deletion; show the equivalent `kbutillib` import |
| Generic utility not in KBUtilLib | Propose moving to `subprojects/<name>/notebooks/util.py` |
| Data-loading helper (path-specific) | Update to use `PROJECT_ROOT` anchor; move to `notebooks/util.py` |
| Dead code / unused | Propose deletion |

Present the categorization to the user. For each group, confirm before deleting,
moving, or rewriting. Write the final `notebooks/util.py` with only the surviving
helpers plus any `PROJECT_ROOT` definition needed.

If no `archive/util.py` exists, write `notebooks/util.py` from the project template
(standard KBUtilLib stub with `PROJECT_ROOT` included).

## Phase 6: NotebookSession Migration Scan

Scan the first code cell of each `.ipynb` in `archive/` for `NotebookSession.kbu`
initialization. For each notebook:

```python
# Pattern to look for:
from kbutillib import NotebookSession
nb = NotebookSession.kbu(name="<slug>")
```

For each notebook missing this initialization:
- Flag it: "Notebook `<filename>` does not initialize `NotebookSession.kbu`."
- Recommend the initialization block to add as the first code cell.
- Note that `NotebookSession` tracks execution state independently from the
  `kbu-subproject.toml` manifest — the TOML records which notebooks are in the plan,
  while `NotebookSession` records what each notebook produced during a run. No
  auto-sync exists between them (AC #53).

Present the full scan result to the user. Do not write any notebook changes in this
phase — changes are deferred until Phase 8 when notebooks move to `notebooks/`.

## Phase 7: Detailed Plan and Grill

With the hypothesis confirmed (Phase 2), the literature review complete (Phase 3),
data paths resolved (Phase 4), and utility inventory done (Phases 5–6), now grill
the full research plan. Walk each branch of the research design dependency tree,
**one question per round**, recommending an answer. Use the existing notebook
content to fill in answers rather than asking when you can infer.

Grill all of:

**Scientific scope (already seeded from Phase 2 — confirm or refine):**
- What is the specific falsifiable hypothesis or research question?
- What are the success criteria — how will you know if the hypothesis is supported?
- What is explicitly out of scope for this subproject?

**Data and methods:**
- What input data is now in root `data/` (or `models/`, `genomes/`) after relocation?
- Which KBUtilLib modules or external libraries are central to the adopted notebooks?
- What computational steps are performed across the notebooks (load → transform → analyze → visualize)?

**Notebook structure:**
- How many notebooks will survive into `notebooks/`? Which are redundant or exploratory and can stay in `archive/` as reference?
- What does each surviving notebook do, in one sentence?
- What shared utilities belong in `notebooks/util.py`?

**Outputs:**
- What figures, tables, or files does the plan produce?
- Which outputs are intermediate (consumed by a later notebook) vs final?

Keep grilling until every answer is resolved or explicitly marked `TBD`.

Before writing, present the proposed plan structure and ask: "Does this match your
intent, or should we adjust?" Revise until the user confirms.

Write `subprojects/<name>/RESEARCH_PLAN.md`:

```markdown
# Research Plan: <Subproject Name>

## Hypothesis / Research Question
<One or two sentences — specific and falsifiable.>

## Success Criteria
<Numbered list — what an external reader would check to verify the plan succeeded.>

## Data Inputs
<Bullet list: path (root-relative), description, format.>

## Notebook Outline
1. `01_<slug>.ipynb` — <purpose>
2. `02_<slug>.ipynb` — <purpose>
... (surviving notebooks, max 5 recommended)

## Shared Utilities (`util.py`)
<Bullet list of functions with one-line descriptions.>

## Outputs
<Bullet list: filename, description, which notebook produces it.>

## Out of Scope
<What this subproject explicitly will not do.>

## Migration Notes
<Brief record of what was in archive/, what was relocated, what was retired.>

## Notes
<Any caveats, open decisions, or TBD items.>
```

## Phase 8: Decompose Into Tasks — Relocate Notebooks — Populate Manifest

### Step 8a: Relocate surviving notebooks

For each surviving notebook identified in Phase 7, propose the target filename in
`notebooks/` (renaming for clarity if the original name is not a clean slug):

```bash
mv subprojects/<name>/archive/<original>.ipynb subprojects/<name>/notebooks/<slug>.ipynb
```

Present each rename to the user for confirmation before executing.

### Step 8b: Apply NotebookSession initialization

For each notebook moved to `notebooks/`, if it was flagged in Phase 6 as missing
`NotebookSession.kbu` initialization, insert the initialization block now (user
confirmed in Phase 6).

### Step 8c: Populate manifest

Update `subprojects/<name>/kbu-subproject.toml` — replace the empty `notebooks: []`
with one entry per notebook moved to `notebooks/`:

```toml
[[notebooks]]
slug = "<slug>"
purpose = "<one-sentence purpose from Phase 7>"
last_run_at = ""
modified_since_run = true
```

The manifest is the source of truth for the CLI's lifecycle tracking. It is
independent of `NotebookSession` SQLite (AC #53) — do not read or write the
SQLite catalog here.

### Step 8d: Write TASKS.md

Render `subprojects/<name>/TASKS.md` as a human-readable view of the manifest:

```markdown
# Tasks: <Subproject Name>

Generated from `kbu-subproject.toml` manifest. Edit the manifest to change this list.

## Notebooks

| # | File | Purpose | Status |
|---|---|---|---|
| 1 | `01_<slug>.ipynb` | <purpose> | Not run |
| 2 | `02_<slug>.ipynb` | <purpose> | Not run |
...

## Migration Checklist

- [ ] Verify all `PROJECT_ROOT` path rewrites produce correct absolute paths
- [ ] Confirm `NotebookSession.kbu` initialization in each notebook
- [ ] Review and prune `archive/` — commit or `.gitignore` large binaries
- [ ] Run first notebook end-to-end and verify outputs
```

### Step 8e: Advance state and save session

```bash
kbu subproject advance <name>
kbu session save --skill kbu-migrate --subproject <name> --summary "<one-sentence summary: N notebooks migrated, hypothesis confirmed>"
```

The subproject advances from `migrate` to `p-review`.

Tell the user:
- Files written: `RESEARCH_PLAN.md`, `TASKS.md`, `literature/index.md`, `notebooks/util.py`, each relocated notebook
- The new subproject state (`p-review`)
- That they should review `archive/` for any remaining content — large binaries in particular should be added to `.gitignore` or moved to Git LFS
- That they can run `/kbu-build` if net-new notebooks are needed, or proceed directly to `/kbu-run` if all notebooks are migrated

## Rules

1. **Delegated steps run in explicit subagents.** Literature review must be
   invoked through `Agent(subagent_type="kbu-sub-literature-review", prompt=…)`.
   Any future review or diagnose steps implied by this skill must likewise be
   explicit `Agent(subagent_type="kbu-sub-…", prompt=…)` calls — never inline.
   See the MANDATORY SUBAGENT DELEGATION RULE at the top.
2. **Infer before asking.** Read what is in `archive/` before asking the researcher
   to explain it. Come with an informed proposal, not a blank grill.
3. **No silent moves.** Every file relocation in Phase 4 and Phase 8 requires user
   confirmation before execution.
4. **Path rewrites are project-root relative (AC #52).** Never rewrite to a
   hard-coded absolute path or a naive relative `../` path. Always anchor to
   `PROJECT_ROOT = Path(__file__).resolve().parents[N]`.
5. **TOML manifest is independent of NotebookSession SQLite (AC #53).** Do not
   read or write the SQLite catalog. Do not auto-sync the two systems.
6. **`archive/` is not deleted.** Surviving notebooks move to `notebooks/`; the
   rest of `archive/` stays for the researcher to review. Flag large binaries but
   do not add `.gitignore` entries without user approval.
7. **One question per grill round.** Walk the grill tree one branch at a time;
   do not dump all questions at once.
8. **Cross-reference by slash-command.** Use `/kbu-build`, `/kbu-run`, and
   `/kbu-synthesize` when pointing to next steps.
