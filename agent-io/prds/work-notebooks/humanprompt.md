# Work-Notebooks — light notebook authoring mode (human brief)

## What I'm asking for

I run two kinds of notebook work. **BERIL** is narrow, hypothesis-driven science
that suits the full kbu co-scientist machinery. The bigger share of my work is
**"work notebooks"** — running a lot of compute to produce scientifically-ready
data products. That pattern worked best when I just built APIs/utilities and used
notebooks to execute, test, and review the data, with a skill describing notebook
design and the AI helping me build/run/debug iteratively.

I want to **adapt** (not rip out) the KBUtilLib notebook tooling into a light
"work-notebook" mode that runs alongside BERIL, drops the manifest / state machine
/ subprojects folder / BERIL emulation, and gives me back the simpler loop.

## The structure I want

```
notebooks/
  models/  genomes/  data/        # shared across projects
  PRJ-<topic>/
    util.py                       # %run-loaded
    01_<title>.ipynb  02_<title>.ipynb
    NBCache/   NBOutput/
```

(Resembles ModelingLOE but: `PRJ-` prefix, `NBCache`, `NBOutput`, and
models/genomes/data all under `notebooks/`.)

## The pieces

1. **`kbu notebook-init <repo> --project <topic>`** — one idempotent command.
   Bare name → `~/Dropbox/Projects/<name>`, or full path. Creates the repo if
   missing (git + Cursor workspace + `.claude` via ClaudeCommands + notebooks
   tree), adds `notebooks/` to an existing repo, or adds a new PRJ — and refuses
   to clobber an existing PRJ. Deploys **only** the work-notebook skill bundle.
   Registers the repo in my AIAssistant registry and writes a one-line project
   binding. `--update` refreshes the deployed bundle.

2. **`kbu-run`** — deployed into each work-notebook repo. The AI grills me about
   one notebook at a time, builds it, hands off to JupyterLab so I run it, helps
   me debug, then synthesizes results into `REPORT.md`. Saves an AIAssistant
   session record. No manifest — the filesystem is the memory.

3. **`kbu-workbench`** — a new maintainer console I run inside KBUtilLib to
   create/extend work-notebook repos and refresh their bundle. (`kbu-start` stays
   BERIL's — untouched.)

## Hard rules

- **Two separate systems.** Work-notebook skills (`jupyter-dev`, `kbu-run`,
  `synthesize`) deploy **only** to work-notebook repos. BERIL skills deploy
  **only** to BERIL. No shared skill files, no cross-contamination.
- **BERIL untouched** — its manifest, state machine, `kbu-*` suite, subagents,
  `kbu-start`, and `.kbcache/` keep working exactly as today.
- **Keep the cache layer.** `NotebookSession` (cache + serialization + provenance)
  stays; only its cache directory is parametrized (`NBCache` for work, `.kbcache`
  for BERIL).
- **`jupyter-dev` is rewritten** to be the authoritative work-notebook structure
  skill.
- **`synthesize` is forked** into a manifest-free version that writes `REPORT.md`.
- Graceful degradation when AIAssistant or ClaudeCommands isn't present.

## Deferred

- Forking `kbu-fba` into a BERIL-free modeling skill (fast-follow PRD).
- Migrating any BERIL repo.
