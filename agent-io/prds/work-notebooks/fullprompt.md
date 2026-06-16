# PRD: Work-Notebooks — a light notebook authoring mode

## Problem Statement

Chris runs two very different kinds of notebook work, and the current KBUtilLib
tooling forces both through one heavy "co-scientist" pipeline that only fits one
of them.

- **BERIL science projects** (BERIL-research-observatory): genuinely narrow,
  hypothesis-driven research that benefits from the full kbu co-scientist
  machinery — the `plan → p-review → build → b-review → run → synthesize →
  s-review → complete` state machine, `kbu-project.toml`/`kbu-subproject.toml`
  manifests, the `kbu-*` skill suite, and the `kbu-sub-*` subagents.

- **"Work notebooks"**: the larger volume of his work — running a lot of compute
  and producing scientifically-ready *data products* for later analysis. This is
  the pattern he had success with before: build APIs/utilities, then use
  notebooks to execute, test, apply, and review the resulting data. It does not
  want a state machine, a manifest, a subprojects folder, or BERIL emulation.

The heavy machinery (manifest, state machine, subagents, the multi-command
`kbu-*` suite) adds ceremony and rigidity that actively gets in the way of work
notebooks. Chris wants to drop back to the simpler, proven loop — "the AI and I
iteratively build and run notebooks together" — governed by a single skill
describing notebook design (`jupyter-dev`) plus one command that orchestrates
the build/run/synthesize cycle.

Crucially, the two systems must stay **cleanly separated**: work-notebook skills
must never deploy to BERIL, and BERIL skills must never deploy to work-notebook
repos. They are two systems that happen to share one underlying cache/provenance
library.

## Solution

Add a **light "work-notebook" mode** to the `kbu` toolchain that runs *alongside*
the existing BERIL path (the BERIL path is left untouched). It has three pieces:

1. **A scaffolding command** — one idempotent `kbu notebook-init <repo> --project
   <topic>` that creates or extends a work-notebook repo with the desired
   directory convention, deploys the work-notebook skill bundle into the repo's
   `.claude`, and binds the repo to an AIAssistant registry project.

2. **An in-repo authoring loop** — a `kbu-run` command (deployed into each
   work-notebook repo) that runs the interactive build→run→synthesize cycle: it
   grills Chris about each notebook one at a time, scaffolds it, hands off to
   JupyterLab for execution, helps debug, and synthesizes results — saving an
   AIAssistant session record each session.

3. **A maintainer console** — a new home command (`kbu-workbench`) that Chris runs
   inside the KBUtilLib repo to create/extend work-notebook repos and refresh
   their deployed skill bundle.

Underneath, the genuinely valuable `NotebookSession` cache/serialization layer is
**kept and adapted** (not removed) — only its cache directory name is
parametrized so work notebooks can use `NBCache/` while BERIL keeps `.kbcache/`.

### Work-notebook directory convention

```
<repo>/
  notebooks/
    .kbu-run.json            # one-line project binding {"project_id": "..."} — NOT a manifest
    models/                  # shared across PRJs
    genomes/                 # shared across PRJs
    data/                    # shared across PRJs
    PRJ-<topic1>/
      util.py                # %run-loaded; defines path constants + NotebookSession
      01_<snake_case_title>.ipynb
      02_<snake_case_title>.ipynb
      NBCache/               # per-PRJ NotebookSession cache (gitignored)
      NBOutput/              # per-PRJ cell outputs (gitignored)
    PRJ-<topic2>/
      ...
```

This resembles ModelingLOE's current layout but differs deliberately in four
spots: `PRJ-` prefix on project folders, `NBCache/` (was `.kbcache/`), `NBOutput/`
(was `nboutput/`), and `models/genomes/data` all sit under `notebooks/`.

## User Stories

1. As Chris, I want to spin up a brand-new work-notebook repo with one command,
   so that I get a git repo, a Cursor workspace, a `.claude` directory, and a
   first `PRJ-<topic>` folder without hand-assembly.
2. As Chris, I want that same command to also work on an *existing* repo (adding
   a `notebooks/` tree), so that I can retrofit the convention onto a project I
   already have.
3. As Chris, I want that same command to add a *second* `PRJ-<topic>` to an
   existing `notebooks/` tree, so that I can grow a repo with multiple project
   threads over time.
4. As Chris, I want the command to refuse to clobber an existing `PRJ-<topic>`,
   so that I never lose work by re-running it.
5. As Chris, I want the command to deploy *only* the work-notebook skill bundle
   (`jupyter-dev`, `kbu-run`, `synthesize`) into the new repo, so that BERIL
   skills never leak into work-notebook repos.
6. As Chris, I want the command to register the repo in my AIAssistant project
   registry (or attach to an existing entry by `repo_path`) and write a one-line
   project binding, so that `kbu-run` sessions have a valid `project_id` and the
   repo shows up in my portfolio.
7. As Chris, I want to run `kbu-run` inside a work-notebook repo and have it
   orient itself entirely from the filesystem (list `PRJ-*` folders, show the
   numbered notebooks in the active PRJ, read `util.py`), so that there is no
   manifest to maintain or drift.
8. As Chris, I want `kbu-run` to grill me about one notebook at a time (what it's
   for, what data in, what analysis, what output) and then assign it the next
   `NN_` number and a snake_case title, so that notebooks are built deliberately
   and enumerated in logical order.
9. As Chris, I want `kbu-run` to scaffold the notebook per the `jupyter-dev` cell
   discipline and extend `util.py` with helpers as needed, so that the structure
   stays consistent.
10. As Chris, I want `kbu-run` to hand off to interactive JupyterLab for *running*
    cells (not execute them programmatically by default), so that I drive the
    kernel myself, consistent with how I like to work.
11. As Chris, I want `kbu-run` to help me debug when I report an error from a run,
    so that I get help without surrendering control of execution.
12. As Chris, I want `kbu-run` to synthesize results into a `REPORT.md` for the
    PRJ once notebooks have been run, reusing an adapted (manifest-free)
    `synthesize` skill, so that I get interpretation without the BERIL lifecycle.
13. As Chris, I want `kbu-run` to save an AIAssistant session record each session,
    so that work-notebook sessions appear in my dashboards like `/ai-*` sessions.
14. As Chris, I want a maintainer console command (`kbu-workbench`) I run inside
    KBUtilLib, so that I can create/extend work-notebook repos and refresh their
    deployed skill bundle from a menu.
15. As Chris, I want to refresh the deployed bundle in an existing work-notebook
    repo via `kbu notebook-init --update`, so that skill updates reach repos I
    created earlier.
16. As Chris, I want the BERIL path (manifest, state machine, `kbu-*` suite,
    subagents, `kbu-start`, `.kbcache/`) to keep working exactly as today, so
    that ModelingLOE/ANME and the research observatory are not disrupted.
17. As Chris, I want `NotebookSession`'s cache, serialization adapters, and
    provenance to be available in work notebooks, so that I keep the
    cache-as-you-go pattern that made the prior workflow productive.
18. As Chris, on a machine without AIAssistant or ClaudeCommands, I want the
    commands to degrade gracefully (skip registry/bundle steps with a clear
    notice) rather than fail, so that the toolchain is portable.

## Implementation Decisions

### Scope and separation

- **Parallel surface, not a replacement.** The work-notebook mode is added
  alongside the BERIL path. The BERIL manifest, state machine, `kbu-*` skill
  suite, `kbu-sub-*` subagents, and `kbu-start` are **left in place and
  untouched**. No BERIL repo is migrated by this PRD.
- **Two disjoint deployment surfaces — a hard invariant.**
  - *Work-notebook bundle* = `jupyter-dev` (skill), `kbu-run` (command),
    `synthesize` (manifest-free fork). Deployed **only** into repos created or
    refreshed by `kbu notebook-init`. **Never** to BERIL.
  - *BERIL bundle* = `kbu`, `kbu-notebook`, `kbu-fba`, `kbu-start`, `kbu-migrate`,
    and the four `kbu-sub-*` subagents. Deployed **only** to BERIL via its
    existing path. **Never** to work-notebook repos.
  - No skill file is shared between the two bundles. Where reuse is desired
    (`synthesize`, the deferred modeling skill), the content is **forked** into a
    separate, BERIL-free file — not deployed from the BERIL source.

### Directory convention (canonical)

- Project folders: `PRJ-<topic>` (hyphen after `PRJ`, topic free-form).
- Notebook files: `NN_<snake_case_title>.ipynb` — **bare** zero-padded two-digit
  number, underscore separator, snake_case title (matches BERIL/ModelingLOE; no
  `N` prefix). Filesystem sort order is the only ordering mechanism.
- Per-PRJ: `util.py`, `NBCache/`, `NBOutput/`.
- Shared roots under `notebooks/`: `models/`, `genomes/`, `data/`.
- `NBCache/` and `NBOutput/` are gitignored in work-notebook repos.
- `notebooks/.kbu-run.json` holds the project binding only
  (`{"project_id": "..."}`). This is explicitly **not** a notebook manifest — it
  records nothing about notebook run-state, ordering, or artifacts.

### Module 1 — `kbu notebook-init` (KBUtilLib CLI; new, adapted from
`new_project.py` + `init_notebook.py` + `layout.py`)

- Signature: `kbu notebook-init <repo> [--project <topic>] [--update]`.
  - `<repo>`: a bare name (resolved to `~/Dropbox/Projects/<name>`) or a full
    path.
  - `--project <topic>`: required for create / add-PRJ; the first/next PRJ topic.
- Behavior branches on detected state:
  - **Repo missing** → full bootstrap: create dir, `git init`, write a Cursor
    `.code-workspace`, create `.claude/` (initialized via ClaudeCommands when
    present — see deployment below), create `notebooks/` with `models/genomes/
    data/` and the first `PRJ-<topic>/`.
  - **Repo present, `notebooks/` missing** → scaffold `notebooks/` + shared roots
    + first `PRJ-<topic>/`.
  - **`notebooks/` present** → add the named `PRJ-<topic>/`. **Refuse** (non-zero
    exit, no writes) if that PRJ already exists.
  - **`--update`** → re-deploy the work-notebook bundle into the repo's `.claude`
    (refresh `jupyter-dev`/`kbu-run`/`synthesize`); do not touch notebooks/PRJs.
- Each new `PRJ-<topic>/` gets a rendered `util.py` (Module 4), empty `NBCache/`
  and `NBOutput/`, and gitignore entries appended idempotently (marker-block
  style, reusing the existing idempotent gitignore helper).
- **Registry + binding** (Module 6 of the prior design; folded here): when
  `assistant.state` is importable, register the repo in the AIAssistant registry
  (or attach to an existing entry whose `repo_path` matches) and write
  `notebooks/.kbu-run.json`. When not importable, write the binding with a
  name-derived `project_id` and print a notice.
- **Bundle deployment**: when ClaudeCommands is installed, deploy the
  work-notebook skill collection via `claude-skills` (define a named
  `work-notebook` collection containing exactly `jupyter-dev`, `kbu-run`,
  `synthesize`; deploy it to the target repo's `.claude`). When ClaudeCommands is
  absent, skip with a clear notice. `--update` re-runs this deployment. The
  developer should confirm the exact `claude-skills` invocation for ad-hoc (non-
  registry) target repos; if `claude-skills` cannot target an arbitrary repo
  path, fall back to a direct copy of the three source artifacts from the
  ClaudeCommands `agent-io/skills/` tree, but the canonical path is
  `claude-skills` to stay consistent with platform convention (deployed
  `.claude` files are sync-managed, never hand-edited).

### Module 2 — work-notebook layout profile (KBUtilLib; adapt `layout.py`)

- Add a work-notebook layout descriptor distinct from BERIL's
  `subproject_subdirs`: shared roots `("models", "genomes", "data")` and per-PRJ
  subdirs `("NBCache", "NBOutput")` plus the rendered `util.py`. Provide the
  gitignore lines for `NBCache/` and `NBOutput/`.
- Do not modify the existing BERIL layout functions.

### Module 3 — `NotebookSession` cache-dir parametrization (KBUtilLib; adapt
`notebook/session.py`)

- `NotebookSession.for_notebook()` (and the constructor it calls) gains an
  optional cache-directory parameter. **Default preserves BERIL behavior**
  (`.kbcache`). The work-notebook `util.py` template passes `NBCache`.
- Behavior is otherwise unchanged; this is a pure parametrization so BERIL repos
  keep `.kbcache/` and work-notebook repos get `NBCache/` with zero behavioral
  drift.
- The serialization adapters, blob/vector/experiment stores, schema models, and
  the read-only cache-introspection `Manifest` view are reused unchanged. (The
  read-only cache `Manifest` view is unrelated to the removed
  `kbu-project.toml`/`kbu-subproject.toml` workflow manifest.)

### Module 4 — work-notebook `util.py` template (KBUtilLib; new template)

- A `%run`-loadable template (not imported) rendered into each `PRJ-<topic>/`.
- Provides path constants anchored relative to the PRJ and notebooks root:
  `PROJECT_ROOT`, `NOTEBOOKS_DIR`, `MODELS_DIR`, `GENOMES_DIR`, `DATA_DIR`,
  `NBOUTPUT_DIR` (the PRJ-local `NBOutput/`), and a `session = NotebookSession.
  for_notebook(__file__, cache_dir="NBCache", ...)` instance whose cache lands in
  the PRJ-local `NBCache/`.
- Preserves a `# === project-specific helpers below ===` marker so re-rendering
  (`--update`) can smart-merge without clobbering hand-written helpers, reusing
  the existing smart-merge logic.

### Module 5 — `kbu-run` command (ClaudeCommands; new; deployed into repos)

Interactive, runs in the live Claude Code conversation inside a work-notebook
repo. Phase flow:

1. **Orient** — detect the work-notebook repo, list `PRJ-*` folders, show the
   numbered notebooks in the active PRJ (filesystem order), read its `util.py`.
   Confirm the active PRJ. Read `notebooks/.kbu-run.json` for the `project_id`.
2. **Grill the next notebook** — one notebook at a time: purpose, input data,
   analysis, expected output. Assign the next `NN_` number + snake_case title.
3. **Build** — scaffold the notebook per `jupyter-dev` cell discipline; extend
   `util.py` helpers as needed.
4. **Run & debug** — hand off to interactive JupyterLab (Chris runs cells); help
   debug when Chris reports an error. No programmatic cell execution by default
   (the CLI `nbclient` path remains available for explicit opt-in batch runs).
5. **Synthesize** — when notebooks have been run, invoke the forked manifest-free
   `synthesize` (Module 7) to write/update `REPORT.md` for the PRJ.
6. **Save session** — save an AIAssistant session record (`assistant.state.
   save_session`) with the bound `project_id`; degrade gracefully if AIAssistant
   is unavailable.
7. **Loop or wrap** — move to the next notebook or end.

### Module 6 — `jupyter-dev` skill (ClaudeCommands; rewrite)

- Rewrite `jupyter-dev` to be the **authoritative work-notebook structure skill**:
  the `PRJ-<topic>/` layout, `util.py` via `%run`, cell-independence /
  cache-as-you-go discipline, `NBCache/`/`NBOutput/` semantics, `NN_` numbering,
  and the shared `models/genomes/data` roots.
- Drop the current "redirect to `kbu-notebook`" content. (`kbu-notebook` remains
  the BERIL structure skill, deployed only to BERIL.)

### Module 7 — `synthesize` work-notebook fork (ClaudeCommands; fork)

- Fork the BERIL `synthesize` skill into a **manifest-free** variant: reads
  `NBOutput/` + notebook results for the active PRJ, interprets against the
  notebook's stated purpose, optionally cross-references literature, and writes
  `REPORT.md` in the PRJ folder.
- Remove all BERIL coupling: no `beril.yaml`, no project status lifecycle, no
  manifest status checks, no `RESEARCH_PLAN.md` dependency.

### Module 8 — `kbu-workbench` maintainer console (ClaudeCommands/KBUtilLib home;
new, distinct from `kbu-start`)

- A **new, distinctly named** home command (working name `kbu-workbench`; Chris
  may rename) run inside `~/Dropbox/Projects/KBUtilLib`. `kbu-start` is left as
  BERIL's command and is not touched.
- Menu: create a new work-notebook repo (`kbu notebook-init <repo> --project
  <topic>`), add a PRJ to an existing repo, refresh a repo's deployed bundle
  (`kbu notebook-init <repo> --update`), and machine initialize (`kbu init`).
- Help text describes the work-notebook flow (not the BERIL state machine).

### Retained (adapt, do not remove)

- `kbu init` (machine venv/kernel setup) is **kept** — running notebooks needs
  KBUtilLib installed and a kernel. Adapt only if `notebook-init` requires it.
- The `NotebookSession` cache/serialization/provenance layer is kept (Module 3).

### Removed-from-the-work-notebook-path (still present for BERIL)

The manifest/state-machine code (`kbu-project.toml`, `kbu-subproject.toml`,
`buildplan.json`, the 9-state machine, `kbu update`, `kbu-sub-*` subagents, the
`kbu-*` tier-2 skill suite) is **not invoked by any work-notebook command**.
Work-notebook repos contain none of these files. The code remains in the
repository for BERIL's use.

### Confront round-1 folded clarifications

Pinned values resolving the 12 binding stall points from the cross-family
confront (task-f63e208f). Where the codex agent's proposed resolution conflicted
with a decision already made, the corrected resolution is used (noted).

1. **Cursor workspace file** — named `<repo_basename>.code-workspace` at the repo
   root; content is minimal JSON `{"folders": [{"path": "."}]}`, UTF-8,
   newline-terminated.
2. **`project_id` scheme** — `worknb-<repo_basename>`. When `assistant.state` is
   importable, register/attach through the documented `assistant.state.registry`
   API (confirm the exact function signature in-repo): if an existing registry
   entry's `repo_path` matches the absolute repo path, attach to it and reuse its
   id; otherwise create an entry with id `worknb-<repo_basename>`. When not
   importable, write `{"project_id": "worknb-<repo_basename>"}` to
   `notebooks/.kbu-run.json`.
3. **`claude-skills` deployment (corrected)** — the *gap* is real but the exact
   invocation is NOT pinned to the agent's guessed CLI. The work-notebook
   collection contains exactly `jupyter-dev`, `kbu-run`, `synthesize` and is
   deployed into the target repo's `.claude`. The developer must confirm the
   exact `claude-skills` command for targeting an arbitrary repo path via the
   `claude-commands-expert` skill. If `claude-skills` cannot target an arbitrary
   path, use the direct-copy fallback (#6). If ClaudeCommands is absent, skip
   deployment with a notice and exit 0.
4. **Skill source homing (corrected)** — the three work-notebook skill *sources*
   are homed in **ClaudeCommands**, not KBUtilLib: `jupyter-dev` (already homed
   there; rewritten in place), and the new `kbu-run` and `synthesize` forks
   created alongside it under ClaudeCommands `agent-io/skills/`. Each file must
   carry a "work-notebook scope only" header and must not reference BERIL.
   `kbu notebook-init` deploys them into target repos' `.claude` as the bundle.
5. **gitignore marker** — a single root-level block delimited by
   `# >>> kbu work-notebook gitignore >>>` and
   `# <<< kbu work-notebook gitignore <<<`, containing exactly
   `notebooks/PRJ-*/NBCache/`, `notebooks/PRJ-*/NBOutput/`, and
   `.ipynb_checkpoints/`. Appended idempotently (replace the block if present).
6. **Fallback copy source paths (corrected to ClaudeCommands)** — relative to the
   ClaudeCommands install root: `agent-io/skills/jupyter-dev.md`,
   `agent-io/skills/kbu-run.md`, `agent-io/skills/synthesize.md` (confirm exact
   filenames against the ClaudeCommands `skill_registry.json` `home_path`).
7. **snake_case normalization** — lowercase ASCII; replace any character not in
   `[a-z0-9]` with `_`; collapse runs of `_`; strip leading/trailing `_`.
8. **`.claude/` initialization (corrected)** — when ClaudeCommands is present,
   initialize `.claude/` via the canonical `claude-skills` init path (confirm the
   exact command via `claude-commands-expert`); when absent, create an empty
   `.claude/` directory and print a notice.
9. **Cache parameter name** — extend
   `NotebookSession.for_notebook(notebook_file=None, *, project_name=None,
   cache_dir: str | None = None)`. `cache_dir=None` defaults to `.kbcache` (BERIL
   back-compat); a non-None value names the cache directory placed alongside the
   notebook. Thread `cache_dir` through to the underlying constructor (currently
   `kbcache_dir`) while preserving the default.
10. **`kbu-workbench` surface** — a ClaudeCommands **skill** (markdown command),
    homed in ClaudeCommands `agent-io/skills/kbu-workbench.md`, run inside the
    KBUtilLib repo. It is **not** a Python CLI subcommand; it calls the Python
    `kbu notebook-init` subcommand. (Only `notebook-init` is Python CLI.)
11. **`project_name` in work `util.py`** — the rendered template calls
    `NotebookSession.for_notebook(__file__, project_name=<repo_basename>,
    cache_dir="NBCache")`, so the cache catalog is namespaced by repo.
12. **Test assertions** — tests assert `<repo_basename>.code-workspace` exists at
    repo root with content `{"folders":[{"path":"."}]}`, and that
    `notebooks/.kbu-run.json` carries a `project_id` of the form
    `worknb-<repo_basename>`.

### Confront round-1 advisory items adopted (non-binding → now binding)

1. **`PRJ-<topic>` normalization** — the topic is normalized with the same rule as
   notebook titles (#7: lowercase ASCII, non-`[a-z0-9]` → `_`, collapse, trim)
   before forming the folder name `PRJ-<normalized-topic>`, so PRJ folders are
   always path-safe (no spaces/odd characters).
2. **`NN_` collision tie-break** — when assigning a new notebook number, `kbu-run`
   scans existing `NN_` prefixes and uses the next free zero-padded number,
   incrementing past any collision rather than reusing a number.
3. **Session-schema alignment** — `kbu-run`'s `save_session` payload uses the
   standard AIAssistant session shape (`project_id`, `command="kbu-run"`,
   `topics_discussed`, `decisions_made`, `work_submitted`, `next_steps`,
   `summary`), consistent with the `/ai-*` and `kbu session save` conventions, so
   work-notebook sessions render in the same dashboards.
4. **Workspace ergonomics** — the `<repo_basename>.code-workspace` MAY additionally
   include a `recommendations`/extensions block and a Jupyter run task; the
   required, asserted content is the `{"folders": [{"path": "."}]}` entry (tests
   assert the `folders` entry, not exact-equality, so extra keys are allowed).

## Testing Decisions

Test external behavior, not implementation details. Two modules carry the
regression risk and get tests:

- **Module 1 — `kbu notebook-init` scaffolding.** Test the three branch cases
  against a temp directory: (a) repo missing → bootstrap produces git repo +
  `.code-workspace` + `notebooks/{models,genomes,data,PRJ-<topic>}` with rendered
  `util.py` + empty `NBCache/`/`NBOutput/`; (b) repo present, `notebooks/` missing
  → scaffolds notebooks tree + first PRJ; (c) `notebooks/` present → adds named
  PRJ. Plus: **clobber-refusal** (adding an existing PRJ exits non-zero and
  writes nothing) and **idempotency** (`--update` re-deploys without disturbing
  notebooks/PRJs). Assert on the produced filesystem tree and gitignore entries.
  Registry/bundle steps are exercised in their AIAssistant-absent / ClaudeCommands-
  absent degraded form so the test does not require those services.
- **Module 3 — cache-dir parametrization.** Test that `NotebookSession.
  for_notebook()` writes its cache under the passed cache-dir name, and that the
  **default remains `.kbcache`** (BERIL back-compat). A behavioral test that a
  cached object round-trips from the parametrized directory.

Prior art: existing KBUtilLib CLI tests around `init_notebook`/`new_project`
scaffolding and existing `notebook/session.py` cache tests are the models to
follow.

Skills, templates, and commands (Modules 4–8) are prompt/markdown artifacts and
are **not** unit-tested. (Optional, if cheap: a smoke test that the rendered
work-notebook `util.py` imports and exposes the expected path constants without
error — included only if it does not require a live kernel.)

## Out of Scope

- **Migrating BERIL repos** (ModelingLOE, ANME, the research observatory) to the
  work-notebook convention. BERIL is untouched.
- **Forking `kbu-fba` into a BERIL-free modeling-method skill.** Deferred to a
  fast-follow PRD once the new flow has been used once or twice.
- **Removing** the BERIL manifest/state-machine/subagent code from the codebase.
  It stays for BERIL; the work-notebook path simply never uses it.
- **Programmatic notebook execution as a default.** Hand-off to JupyterLab is the
  default; batch `nbclient` execution remains an opt-in CLI path, unchanged.
- **Cross-machine deployment of work-notebook repos.** First target is
  primary-laptop (where AIAssistant + ClaudeCommands + interactive work live).

## Further Notes

- `NBCache/` is non-hidden (vs the hidden `.kbcache/`) per Chris's explicit
  convention; gitignoring it keeps it out of version control and `git status`.
- The home command name `kbu-workbench` is a working name; rename freely — it has
  no source collision with `kbu-start`.
- The `claude-skills` "work-notebook collection" deployment is the one genuine
  integration unknown; the developer should confirm `claude-skills` can deploy a
  named collection to an arbitrary repo path and, if not, either extend
  `claude-skills` minimally or fall back to direct artifact copy. Consult the
  `claude-commands-expert` skill.
- Graceful degradation (AIAssistant-absent, ClaudeCommands-absent) is a
  first-class requirement so the toolchain is portable across machines.

## Acceptance Criteria

1. `kbu notebook-init <bare-name> --project <topic>` resolves a bare name to `~/Dropbox/Projects/<bare-name>` and a full path verbatim.
2. When the target repo does not exist, `kbu notebook-init` creates it with `git init`, a `<repo_basename>.code-workspace` file at the repo root containing at least the entry `"folders": [{"path": "."}]` (additional keys such as recommended extensions or a Jupyter run task are permitted), a `.claude/` directory, and a `notebooks/` tree.
3. The created `notebooks/` tree contains `models/`, `genomes/`, `data/`, and a first `PRJ-<topic>/` folder.
4. Each created `PRJ-<topic>/` contains a rendered `util.py`, an empty `NBCache/`, and an empty `NBOutput/`.
5. When the repo exists but `notebooks/` does not, `kbu notebook-init` scaffolds the `notebooks/` tree and first `PRJ-<topic>/` without altering unrelated repo contents.
6. When `notebooks/` already exists, `kbu notebook-init --project <topic>` adds the named `PRJ-<topic>/`.
7. `kbu notebook-init --project <topic>` exits non-zero and writes nothing when `PRJ-<topic>/` already exists (clobber-refusal).
8. `kbu notebook-init <repo> --update` re-deploys the work-notebook skill bundle into the repo's `.claude` without modifying any `notebooks/`, `PRJ-*/`, or `util.py` content.
9. The deployed bundle in a work-notebook repo's `.claude` consists of exactly `jupyter-dev`, `kbu-run`, and `synthesize`; no BERIL skill (`kbu`, `kbu-notebook`, `kbu-fba`, `kbu-start`, `kbu-migrate`, `kbu-sub-*`) is present.
10. No BERIL repo's `.claude` gains any of `jupyter-dev`, `kbu-run`, or `synthesize` as a result of this work.
11. `kbu notebook-init` writes `notebooks/.kbu-run.json` containing a `project_id` of the form `worknb-<repo_basename>` (or the id of an attached pre-existing registry entry whose `repo_path` matches).
12. When `assistant.state` is not importable, `kbu notebook-init` still writes `.kbu-run.json` with `project_id` `worknb-<repo_basename>` and prints a notice; it does not error.
13. When ClaudeCommands is not installed, `kbu notebook-init` skips bundle deployment with a notice and exits 0.
14. A work-notebook repo's `.gitignore` contains a single marker block delimited by `# >>> kbu work-notebook gitignore >>>` / `# <<< kbu work-notebook gitignore <<<` listing `notebooks/PRJ-*/NBCache/`, `notebooks/PRJ-*/NBOutput/`, and `.ipynb_checkpoints/`; re-running `kbu notebook-init` does not duplicate the block.
15. `NotebookSession.for_notebook()` accepts a `cache_dir` keyword; when omitted it defaults to `.kbcache` (unchanged BERIL behavior); when set to `NBCache` the cache catalog and blobs are written under `NBCache/` alongside the notebook.
16. A cached object written through a `NotebookSession` with `cache_dir="NBCache"` round-trips (write then read) from that directory.
17. The rendered work-notebook `util.py` is `%run`-loadable and exposes `PROJECT_ROOT`, `NOTEBOOKS_DIR`, `MODELS_DIR`, `GENOMES_DIR`, `DATA_DIR`, `NBOUTPUT_DIR`, and a `session` object whose cache resolves to the PRJ-local `NBCache/`.
18. `kbu-run` orients solely from the filesystem (lists `PRJ-*` folders and numbered notebooks, reads `util.py` and `notebooks/.kbu-run.json`) with no manifest file read or written.
19. `kbu-run` assigns new notebooks the next zero-padded `NN_<snake_case_title>.ipynb` name using the normalization: lowercase ASCII, non-`[a-z0-9]` → `_`, collapsed `_`, trimmed `_`; on a number collision it advances to the next free number rather than reusing one.
20. `kbu-run` does not execute notebook cells programmatically by default; it hands off to JupyterLab and assists debugging on reported errors.
21. `kbu-run` saves an AIAssistant session record (via `assistant.state.save_session`) using the bound `project_id`, and degrades gracefully (notice, no error) when AIAssistant is unavailable.
22. The rewritten `jupyter-dev` skill describes the work-notebook structure (`PRJ-<topic>/`, `util.py` via `%run`, `NBCache/`, `NBOutput/`, `NN_` numbering, `models/genomes/data` roots) and contains no redirect to `kbu-notebook`.
23. The forked `synthesize` skill writes/updates a `REPORT.md` in the active `PRJ-<topic>/` from `NBOutput/` + notebook results, with no reference to `beril.yaml`, a status lifecycle, or `RESEARCH_PLAN.md`.
24. `kbu-workbench` exists as a ClaudeCommands skill at `agent-io/skills/kbu-workbench.md`, offers create / add-PRJ / refresh-bundle / machine-init actions, and is not a Python CLI subcommand.
25. The BERIL path is unmodified: BERIL repos retain `.kbcache/`, their manifest/state-machine files, the `kbu-*` suite, and `kbu-sub-*` subagents, and no work-notebook command reads or writes a `kbu-project.toml`/`kbu-subproject.toml`/`buildplan.json`.
26. The `PRJ-<topic>` folder name is the normalized topic (lowercase ASCII, non-`[a-z0-9]` → `_`, collapsed/trimmed `_`); a topic containing spaces or path-unfriendly characters produces a path-safe folder, never a literal space.
27. `kbu-run`'s saved session record carries `command="kbu-run"` and the standard session fields (`project_id`, `topics_discussed`, `decisions_made`, `work_submitted`, `next_steps`, `summary`), so it appears in the same dashboards as `/ai-*` sessions.
