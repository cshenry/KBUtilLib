<!--
kbu skill provenance
type: lean-fork
source_repo: AIAssistant
source_commit: 3fb8137604798fce4d29cf14d0041eb52aa25773
source_path: agent-io/skills/ai-conductor.md
last_reviewed: 2026-06-08
-->

# /kbu-build — Scaffold Subproject Notebooks

You read the subproject's `RESEARCH_PLAN.md` and scaffold the full notebook
structure so the researcher can start doing science immediately. You implement
directly in this context — no sub-agents, no background tasks.

## Precondition

The current subproject must be in state `build`. Verify:

```bash
kbu subproject status <name>
```

Also confirm that `subprojects/<name>/RESEARCH_PLAN.md` exists:

```bash
ls subprojects/<name>/RESEARCH_PLAN.md
```

If either precondition is unmet, tell the user to run `/kbu-plan` first.

## Phase 1: Load the Plan

Read the full `RESEARCH_PLAN.md`:

```bash
cat subprojects/<name>/RESEARCH_PLAN.md
```

Parse out:
- The notebook outline (numbered list → filenames and purposes)
- The `util.py` function list
- Data inputs and expected outputs
- Success criteria

Present a one-paragraph summary of what you are about to build. Confirm with
the user before proceeding.

## Phase 2: Detect Adopted-Branch (Notebooks Already Present)

After loading the plan, check whether the subproject already has notebooks:

```bash
ls subprojects/<name>/notebooks/*.ipynb 2>/dev/null
```

### If `.ipynb` files are present — verify-and-extend mode (warn only)

The subproject is already populated (likely from `kbu subproject adopt` +
`/kbu-migrate`, or a prior build pass). Do NOT scaffold; instead verify
alignment between the manifest and the filesystem:

**Step 1.** Collect manifest slugs from the `[[notebooks]]` entries
parsed in Phase 1.

**Step 2.** Collect filenames from `subprojects/<name>/notebooks/*.ipynb`.

**Step 3.** For each manifest entry whose slug does not correspond to a
`.ipynb` file in `notebooks/`, emit:

```
Manifest lists missing notebook: <slug>
```

**Step 4.** For each `.ipynb` file in `notebooks/` whose filename (without
`.ipynb`) does not appear as a slug in the manifest, emit:

```
Notebook present but not in manifest: <filename>
```

**Step 5.** Report the warning summary to the user. Do NOT auto-create
any missing notebooks. Inform the user that a future `--scaffold-missing`
flag will handle auto-creation.

**Step 6.** Skip to Phase 7 (Advance and Save Session). Phases 3–6
(scaffold util.py, scaffold notebooks, verify structure) do not run in
this mode.

### If no `.ipynb` files are present — continue to Phase 3

This is a virgin subproject. Proceed through the full scaffold flow below.

## Phase 3: Decompose Into Tasks

From the plan, derive a concrete build task list in dependency order:

1. Create the `subprojects/<name>/notebooks/` directory structure
2. Write `util.py` with stub functions (one function per bullet in the plan)
3. Write each `01_*.ipynb` … `0N_*.ipynb` in order

Each notebook must satisfy:
- Loads only from `../data/` or calls functions from `../util.py`
- Has a markdown cell at the top with the notebook's purpose (from the plan)
- Has clearly labelled sections matching the plan's description
- Ends with a markdown cell naming its outputs and where they are written

Present the task list to the user and ask: "Anything to add or change before I
build?"

## Phase 4: Scaffold util.py

Write `subprojects/<name>/notebooks/util.py`. For each function in the plan:

```python
def <function_name>(<params>):
    """
    <One-line docstring from the plan.>

    Parameters
    ----------
    <param> : <type>
        <description>

    Returns
    -------
    <type>
        <description>
    """
    raise NotImplementedError("TODO: implement <function_name>")
```

Include standard imports at the top (`pathlib`, `numpy`, `pandas`) as
appropriate for the project's domain. Keep imports minimal — only what the
function stubs actually reference.

## Phase 5: Scaffold Notebooks

For each notebook in the plan (in order), write
`subprojects/<name>/notebooks/<slug>.ipynb` as a valid Jupyter notebook (JSON
format, `nbformat: 4`).

Each notebook must contain:

1. **Title cell** (markdown): `# <Notebook Purpose from plan>`
2. **Imports cell** (code): standard library imports + `import util`
3. **Data loading cell** (code): load the relevant input from `../data/`
4. **Analysis section(s)** (interleaved code + markdown): one section per
   logical step described in the plan
5. **Outputs cell** (code): save results to the path named in the plan
6. **Summary cell** (markdown): what this notebook produces and what the next
   step is (reference the next notebook by filename)

All cells are stubs — they establish structure and have a `# TODO` comment
where the researcher writes their science. Do NOT fill in scientific logic that
the plan did not specify.

## Phase 6: Verify Structure

After writing all files, verify:

```bash
ls subprojects/<name>/notebooks/
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

If any notebook fails JSON parse, fix it before advancing.

## Phase 7: Advance and Save Session

```bash
kbu subproject advance <name>
kbu session save --skill kbu-build --subproject <name> --summary "<one-sentence summary: N notebooks + util.py scaffolded>"
```

Tell the user:
- Files written: list each notebook and `util.py`
- The new subproject state (should be `run`)
- That they can now open the notebooks in JupyterLab and start implementing

Next step: `kbu notebook launch` to open JupyterLab, or `/kbu-run` for
guided execution of the notebooks.

## Rules

1. **Execute the plan, don't re-plan.** If you notice a gap, flag it to the user
   and add a `# TODO` cell in the affected notebook rather than redesigning.
2. **Stubs only.** Scientific logic belongs to the researcher, not this scaffold.
3. **Dependency order.** Always write `util.py` before the notebooks that import it.
4. **Valid JSON.** Every `.ipynb` file must be parseable before you advance.
5. **No files outside `subprojects/<name>/`.** Do not write to `data/` or repo root.
6. **Cross-reference by slash-command.** Use `/kbu-plan`, `/kbu-run`, `/kbu-diagnose`
   when pointing to other skills.
7. **Adopted-branch is warn-only.** When notebooks are already present (Phase 2),
   emit warnings only — never auto-create missing notebooks. A future
   `--scaffold-missing` flag is out of scope for the current implementation.
