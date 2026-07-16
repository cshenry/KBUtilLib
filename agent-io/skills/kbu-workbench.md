<!--
kbu skill provenance
type: net-new
created: 2026-06-16
scope-note: work-notebook maintainer console; never deploy to BERIL repos
-->
---
name: KBU Workbench
description: Work-notebook maintainer console — create repos, add PRJs, refresh bundles
scope: repo:KBUtilLib
---

# /kbu-workbench — Work-Notebook Maintainer Console

You are the maintainer console for the **work-notebook** mode of KBUtilLib. Run from
inside the KBUtilLib repo (`~/Dropbox/Projects/KBUtilLib`).

This skill is for creating and maintaining **work-notebook repos** — the lightweight,
manifest-free notebook workflow. It is **not** the BERIL setup tool; for BERIL projects
use `/kbu-start` instead.

## What is a work-notebook repo?

A work-notebook repo is a git repository with a `notebooks/` tree organized around
one or more `PRJ-<topic>/` project folders. Each PRJ contains numbered Jupyter
notebooks (`01_<title>.ipynb`, `02_<title>.ipynb`, …), a `%run`-loadable `util.py`,
and per-PRJ cache and output directories (`NBCache/`, `NBOutput/`). The root-level
`notebooks/` directory also holds shared `models/`, `genomes/`, and `data/` folders.

The flow looks like this:

```
1. Create the repo (kbu-workbench → kbu notebook-init <repo> --project <topic>)
2. Open the repo in Cursor and run /kbu-run to start building notebooks
3. Notebooks go into PRJ-<topic>/ as NN_<snake_case_title>.ipynb
4. Run notebooks interactively in JupyterLab; kbu-run helps debug
5. Synthesize results into PRJ-<topic>/REPORT.md via kbu-run's synthesize phase
6. Add more PRJs to the same repo as the work grows
```

There is no state machine, no manifest file (`kbu-project.toml`), and no required
review gates. Sessions save an AIAssistant record automatically (if AIAssistant is
available). Work-notebook repos never contain BERIL skills.

## Step 1: Check machine initialization

Before presenting the menu, probe whether `kbu init` has been run on this machine:

```bash
kbu init --status
```

Capture the exit code. Exit 0 means initialized; non-zero means not yet initialized.
Mark the **Machine initialize** action as unavailable when already initialized.

## Step 2: Present the menu

Ask the user via AskUserQuestion:

> **KBU Workbench — Work-Notebook Maintainer Console**
>
> What would you like to do?
>
> 1. **Help** — What is the work-notebook flow and directory convention?
> 2. **Create new work-notebook repo** — Bootstrap a brand-new repo with a first PRJ
> 3. **Add a PRJ to an existing repo** — Add a new `PRJ-<topic>/` to an existing work-notebook repo
> 4. **Refresh deployed bundle** — Re-deploy the work-notebook skill bundle (`jupyter-dev`, `kbu-run`, `synthesize`) into an existing repo's `.claude`
> 5. **Machine initialize** — Run `kbu init` to set up the venv, editable install, and Jupyter kernel on this machine
>    *(not available: already initialized)* [show only when `kbu init --status` exits 0]

---

## Menu handlers

### 1. Help

Display the following in a single screen (do not truncate):

---
**Work-notebook flow**

Work-notebook repos are lightweight analysis environments built around Jupyter
notebooks. Unlike BERIL science projects, they have no state machine, no manifest,
and no required review steps — just a clean directory convention and a build/run/
synthesize loop.

**Directory convention:**
```
<repo>/
  notebooks/
    .kbu-run.json         # project binding {"project_id": "worknb-<repo>"}
    models/               # shared model files across all PRJs
    genomes/              # shared genome files across all PRJs
    data/                 # shared data files across all PRJs
    PRJ-<topic1>/
      util.py             # %run-loaded; path constants + NotebookSession
      01_<title>.ipynb
      02_<title>.ipynb
      NBCache/            # per-PRJ NotebookSession cache (gitignored)
      NBOutput/           # per-PRJ cell outputs (gitignored)
    PRJ-<topic2>/
      ...
```

**The `/kbu-run` loop (inside a work-notebook repo):**
1. Orient — detect PRJ folders and numbered notebooks; read `util.py` and `.kbu-run.json`
2. Grill — one notebook at a time: what is it for, what data in, what analysis, what output
3. Build — scaffold the notebook per the `jupyter-dev` cell discipline; extend `util.py` helpers
4. Run — hand off to interactive JupyterLab (you run the cells); help debug errors you report
5. Synthesize — write/update `REPORT.md` in the PRJ from `NBOutput/` + notebook results
6. Save session — save an AIAssistant session record with the bound `project_id`

**What this is not:**
- Not a BERIL project (no `kbu-project.toml`, no state machine, no subagents)
- Not a replacement for BERIL — BERIL science projects still use `/kbu-start`
- Not programmatic notebook execution — you run cells in JupyterLab yourself
---

### 2. Create new work-notebook repo

Collect the following via AskUserQuestion (ask all in one prompt):

- **Repo name** — bare name (resolved to `~/Dropbox/Projects/<name>`) or full path
- **First PRJ topic** — free-form topic name (will be normalized to `PRJ-<lowercase_slug>`)

Confirm the resolved path and PRJ name, then run:

```bash
kbu notebook-init <repo> --project <topic>
```

Watch for output confirming:
- Git repo created (or existing repo detected)
- `<repo_basename>.code-workspace` written at repo root
- `notebooks/` tree created with `models/`, `genomes/`, `data/`, and `PRJ-<topic>/`
- `PRJ-<topic>/util.py` rendered
- `notebooks/.kbu-run.json` written with `project_id`
- Work-notebook skill bundle deployed to `.claude/` (or skipped with a notice if ClaudeCommands unavailable)
- `.gitignore` updated with the `kbu work-notebook` marker block

After success, print the following next steps:

---
**Your work-notebook repo is ready. To start building notebooks:**

1. Open it in Cursor:
   ```
   cursor <repo_path>/<repo_basename>.code-workspace
   ```
2. In Cursor's integrated terminal, run `claude`.
3. Type `/kbu-run` to start the interactive notebook loop.
---

### 3. Add a PRJ to an existing repo

Collect the following via AskUserQuestion:

- **Repo path** — bare name or full path to the existing work-notebook repo
- **New PRJ topic** — topic name for the new `PRJ-<topic>/` folder

Then run:

```bash
kbu notebook-init <repo> --project <topic>
```

The command detects that `notebooks/` already exists and adds only the new PRJ.
It refuses (non-zero exit, no writes) if that PRJ already exists — report the
refusal clearly and ask the user to choose a different topic name.

After success, print:

---
**PRJ added. To start working in it:**

1. Open the repo in Cursor (it may already be open).
2. In the integrated terminal, run `claude`.
3. Type `/kbu-run` — it will list all PRJs and let you select the new one.
---

### 4. Refresh deployed bundle

Collect via AskUserQuestion:

- **Repo path** — bare name or full path to the existing work-notebook repo

Then run:

```bash
kbu notebook-init <repo> --update
```

This re-deploys `jupyter-dev`, `kbu-run`, and `synthesize` into the repo's `.claude`
without touching any `notebooks/`, `PRJ-*/`, or `util.py` content.

Confirm success from the command output. If ClaudeCommands is unavailable, the
command will print a notice and exit 0 — report that to the user.

### 5. Machine initialize

Run machine initialization (idempotent — safe to re-run):

```bash
kbu init
```

Watch the output for errors. After completion, invoke the `cursor-setup` skill to
verify Cursor and the Claude extension:

```
/cursor-setup
```

If `cursor-setup` is not available, manually verify:
- **Cursor**: `which cursor` (should return a path)
- **Claude extension**: Open Cursor → Extensions → search "Claude" → confirm it is installed

Confirm the venv and kernel are active:

```bash
kbu init --status
echo "Exit code: $?"
```

Exit 0 confirms success. Exit 2 means the venv was deleted — re-run `kbu init` to
recreate it.

---

## Notes for maintainers

- `kbu-start` is BERIL's setup command and is not affected by this skill.
- The work-notebook skill bundle (`jupyter-dev`, `kbu-run`, `synthesize`) is sourced
  from `~/Dropbox/Projects/ClaudeCommands/agent-io/skills/`. Never deploy BERIL
  skills (`kbu`, `kbu-notebook`, `kbu-fba`, `kbu-start`, `kbu-migrate`, `kbu-sub-*`)
  into work-notebook repos.
- `kbu notebook-init` is the Python CLI subcommand (in KBUtilLib) that this skill
  drives. This skill itself is a markdown command, not a Python CLI subcommand.

## $ARGUMENTS
