<!--
kbu skill provenance
type: lean-fork
source_repo: AIAssistant
source_commit: 3fb8137604798fce4d29cf14d0041eb52aa25773
source_path: agent-io/skills/ai-conductor.md
last_reviewed: 2026-06-05
-->

# /kbu-build — Scaffold Subproject Notebooks

You read the subproject's `RESEARCH_PLAN.md` and scaffold the full notebook
structure so the student can start doing science immediately. You implement
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

## Phase 2: Decompose Into Tasks

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

## Phase 3: Scaffold util.py

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

## Phase 4: Scaffold Notebooks

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
where the student writes their science. Do NOT fill in scientific logic that
the plan did not specify.

## Phase 5: Verify Structure

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

## Phase 6: Advance and Save Session

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
2. **Stubs only.** Scientific logic belongs to the student, not this scaffold.
3. **Dependency order.** Always write `util.py` before the notebooks that import it.
4. **Valid JSON.** Every `.ipynb` file must be parseable before you advance.
5. **No files outside `subprojects/<name>/`.** Do not write to `data/` or repo root.
6. **Cross-reference by slash-command.** Use `/kbu-plan`, `/kbu-run`, `/kbu-diagnose`
   when pointing to other skills.
