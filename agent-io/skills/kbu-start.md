<!--
kbu skill provenance
type: net-new
created: 2026-06-05
-->
---
name: KBU Start
description: KBUtilLib setup dashboard — initialize, create projects, and update KBUtilLib
scope: project
---

# /kbu-start — KBUtilLib Setup Dashboard

You are the tier-1 setup assistant for KBUtilLib. Run from inside the KBUtilLib repo.

Present the menu below using AskUserQuestion. Check initialization status first by running
`kbu init --status` (exit 0 = initialized, non-zero = not initialized), then render the
appropriate menu.

## Step 1: Check initialization status

```bash
kbu init --status
```

Capture the exit code. If exit 0, mark Initialize as unavailable (already done).
If exit non-zero, mark Initialize as available.

## Step 2: Present the menu

Ask the user:

> **KBUtilLib Setup Dashboard**
>
> What would you like to do?
>
> 1. **Help** — What is KBUtilLib and how does the workflow work?
> 2. **Initialize** — Set up KBUtilLib on this machine (venv, editable install, Jupyter kernel)
>    *(not available: already-initialized)* [show this note only when kbu init --status exits 0]
> 3. **New project** — Create a new KBUtilLib student project
> 4. **Update** — Pull the latest KBUtilLib source and reinstall

## Menu handlers

### 1. Help

Display this explainer in a single screen:

---
**What is KBUtilLib?**

KBUtilLib is a modular Python utility framework for scientific computing and bioinformatics
developed at Argonne National Laboratory. It provides composable sub-utilities for KBase
workspace access, ModelSEED biochemistry, FBA, genome analysis, and AI-powered curation.

**The tier-2 student workflow** (available after "New project"):

```
plan → p-review → build → b-review → run → synthesize → s-review → complete
```

Each step is a `/kbu-*` slash command inside your project. The state machine enforces the
order — you cannot skip steps. Reviews can fail and route you back to the prior action step.

**File layout inside your project:**
```
<project_root>/
├── kbu-project.toml          # project manifest
├── subprojects/
│   └── <name>/
│       ├── kbu-subproject.toml   # subproject state + session refs
│       ├── RESEARCH_PLAN.md      # written by /kbu-plan
│       ├── REPORT.md             # written by /kbu-synthesize
│       ├── references.md         # managed by /kbu-literature-review
│       ├── notebooks/            # .ipynb files + util.py
│       ├── nboutput/             # cell outputs
│       └── sessions/             # session records (YAML)
└── .claude/commands/         # /kbu-start and all tier-2 skill files
```

**How to open Claude in Cursor:**
1. Open your project workspace: `cursor <path>/<name>.code-workspace`
2. Open the integrated terminal
3. Run `claude`
4. Type `/kbu-start`

**Read more:** `README.md` in your project root, or KBUtilLib's own README.
---

### 2. Initialize

Run initialization. This is idempotent — safe to re-run if something was interrupted.

```bash
kbu init
```

Watch the output. After `kbu init` completes successfully, invoke the `cursor-setup` skill
to check for Cursor and the Claude extension:

```
/cursor-setup
```

If `cursor-setup` is not available in this environment, manually verify:
- **Cursor**: `which cursor` (macOS: should return a path)
- **Claude extension**: Open Cursor → Extensions → search "Claude" → confirm "Claude" by
  Anthropic is installed and enabled

Confirm success by running:

```bash
kbu init --status
echo "Exit code: $?"
```

Exit 0 = success. Exit 2 means the venv was deleted — re-run `kbu init`.

### 3. New project

Collect the following via AskUserQuestion (ask all in one prompt):

- **Project name** (snake_case, e.g. `henry_lab_studies`)
- **Target path** (where to create the project; default `~/<project_name>`)
- **Author name**
- **Affiliation**
- **ORCID** (optional; press Enter to skip)
- **First subproject name** (optional; press Enter to skip — you can add subprojects later)

Then run:

```bash
kbu new-project <target_path> \
  --name <project_name> \
  --author "<author_name>" \
  --affiliation "<affiliation>" \
  [--orcid "<orcid>"] \
  [--first-subproject "<first_subproject>"]
```

After the command returns, print the following instructions verbatim:

---
**Your project is ready. To start working:**

1. Open it in Cursor:
   ```
   cursor <target_path>/<project_name>.code-workspace
   ```
2. In Cursor, open the integrated terminal (View → Terminal or `` Ctrl+` ``).
3. Run `claude` in the terminal.
4. Type `/kbu-start` to open the project dashboard.
---

### 4. Update

Pull the latest KBUtilLib source and reinstall the editable package:

```bash
kbu init --update
```

Watch the output for any errors. After completion, confirm the updated version:

```bash
python -c "import kbutillib; print(kbutillib.__version__)"
```

If the update fails due to a missing venv (e.g., after a machine migration), run
`kbu init` first to recreate it, then retry `kbu init --update`.

## $ARGUMENTS
