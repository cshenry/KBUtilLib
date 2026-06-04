# kbu-start v1 — full PRD

## Problem Statement

Students attempting to use KBUtilLib to build analysis notebooks face a steep
on-ramp. The two existing skills (`kbutillib-expert`, `kbutillib-dev`)
assume the user already knows what they're doing — neither is a front door.
Students lack a canonical workflow for plan → build → run → synthesize, lack
environment setup, and lack a project layout convention for organising
notebook work that scales beyond ad-hoc scripts.

Additional constraint: this system must work for users *outside* Chris's
environment. They will not have ClaudeCommands installed, will not run
`claude-skills sync`, and may not have AIAssistant orchestration tools.
KBUtilLib must ship a complete, self-contained Claude-Code-based workflow as
part of the repo itself.

## Solution

A two-tier skill system shipped *inside KBUtilLib's own `.claude/`* — a
deliberate exception to the usual ClaudeCommands sync policy because
KBUtilLib is a distributable module that must work for cloned-and-run users.

**Tier 1** (`KBUtilLib/.claude/commands/kbu-start.md`): root dashboard with
Help / Initialize / New project / Update items. Run from inside the
KBUtilLib repo itself.

**Tier 2** (`templates/student-project/.claude/commands/kbu-start.md`):
project-scoped dashboard with Plan / Build / Run / Synthesize / Review /
Literature review / Diagnose / Update items, status-gated against a linear
state machine. Copied verbatim into each new student project by tier-1
"New project".

The state machine, session tracking, notebook-tracking, AIAssistant routing,
and venv management all live behind a single CLI surface (`kbu`). Skills
themselves are pure prose — they decide *what to think about*, then call
`kbu <subcommand>` to record state transitions and sessions.

## User Stories

1. As a new student, I want to clone KBUtilLib, run `claude` inside, type
   `/kbu-start`, pick "Initialize", and get a working environment.
2. As a new student, I want `/kbu-start` → "New project" to create a working
   project at a path I specify, with its own venv (KBUtilLib editable-installed
   from the source path) and an `/kbu-start` skill ready to use inside it.
3. As a student starting an analysis, I want `/kbu-start` → "Plan" to grill
   me on my research question and write `RESEARCH_PLAN.md` into a subproject.
4. As a student who's planned, I want the state machine to refuse to let me
   skip directly to "Build" until I've reviewed the plan via "Review".
5. As a student building notebooks, I want `/kbu-start` → "Build" to scaffold
   notebooks per the plan, putting `01_*.ipynb`, `util.py`, and
   `nboutput/` inside `subprojects/<name>/notebooks/`.
6. As a student who's run their notebooks, I want `/kbu-start` → "Run" to
   show a dashboard of notebooks with last-run timestamps and a ⚠ flag
   when the notebook has been modified since last execution.
7. As a student who finishes an analysis, I want `/kbu-start` → "Synthesize"
   to read my notebook outputs, cross-reference literature, and draft a
   `REPORT.md`.
8. As a student preparing to publish, I want `/kbu-start` → "Review" to give
   me an independent AI review of my plan / build / report at each stage.
9. As a student who's stuck, I want `/kbu-start` → "Diagnose" to walk me
   through a structured debug loop without dragging in heavy infrastructure.
10. As a student who needs literature context, I want `/kbu-start` →
    "Literature review" to search PubMed / bioRxiv / arXiv / Semantic Scholar
    and append to `references.md`.
11. As a student updating to a newer KBUtilLib, I want `/kbu-start` →
    "Update" to pull skill + template changes from the parent install,
    warning me before clobbering any skill file I edited locally.
12. As Chris using KBUtilLib for his own work, I want the tier-2 skills to
    be tight enough that I use them in ADP1Notebooks / SalternsNotebooks /
    ModelingLOE.
13. As a tier-2 user, I want each `/kbu-*` skill to save a session
    automatically when it ends.
14. As Chris on primary-laptop, I want tier-2 sessions to route to
    AIAssistant's `state/sessions.db` so they show up in the AIAssistant
    dashboard.
15. As a student without AIAssistant, I want sessions saved as YAML files
    inside the subproject directory so I can grep / inspect them.
16. As a tier-2 user, I want the dashboard to show recent sessions across
    all subprojects, like the `/ai-design` dashboard does.
17. As a tier-2 user, I want "Synthesize" to refuse if state ≠ `synthesize`,
    preventing fabricated interpretations against absent results.
18. As a tier-2 user, I want "Review" to be stage-aware: reviewing the plan
    vs. report uses different criteria.
19. As a student on macOS using Cursor, I want "Initialize" to detect
    Cursor + the Claude extension and tell me exactly what to install if
    missing.
20. As a student new to slash commands, I want "Help" to explain (a) what
    `/kbu-start` is, (b) the subproject state machine, (c) the file layout,
    (d) how to open Claude in Cursor — in one screen.
21. As a student moving the project to a new machine, I want
    `kbu update --set-source <new-path>` to relocate the parent KBUtilLib
    install pointer without breaking the workflow.
22. As Chris, I want the lean-fork skills (kbu-plan / kbu-build /
    kbu-diagnose) to evolve independently of the upstream /ai-design /
    /ai-conductor / /diagnose, with a comment at the top of each noting
    the fork source and the last review date so drift stays visible.
23. As a student, I want subproject status visible in the dashboard so I
    can see at a glance which subprojects are in which stage.
24. As a tier-2 user, I want `kbu subproject create <name>` to scaffold the
    full subproject layout (notebooks/util.py/nboutput/data/user_data/
    figures/references.md/sessions/kbu-subproject.toml) on creation.
25. As a tier-2 user, I want every menu transition (state advance, review
    pass/fail) committed via `kbu` CLI, so the state machine is single
    source of truth for the workflow.

## Implementation Decisions

### Distribution & sync policy

- **KBUtilLib `.claude/` is checked in to git** — exception to the
  ClaudeCommands sync policy. Tier-1 skill source lives at
  `KBUtilLib/.claude/commands/kbu-start.md`, committed and authored
  directly there. Tier-2 template skills live at
  `KBUtilLib/templates/student-project/.claude/commands/*.md`, also
  committed.
- `claude-skills sync` does **not** target KBUtilLib for these skills.
  KBUtilLib does not register them in `skill_registry.json` as
  sync-managed skills. (The pre-existing `kbutillib-dev` and
  `kbutillib-expert` skills are unaffected; they continue to use the
  agent-io/skills/ → sync → `.claude/commands/` pipeline.)
- Tier-2 template files at `templates/student-project/` are copied
  verbatim into student projects by `kbu new-project`. The template tree
  is treated as data, not source — no `agent-io/skills/` equivalent.

### Subproject state machine

States (linear): `plan` → `p-review` → `build` → `b-review` → `run` →
`synthesize` → `s-review` → `complete`.

Reviews can fail and route backward to the immediately prior action state
(`p-review` → `plan` on fail; `b-review` → `build` on fail; `s-review` →
`synthesize` on fail). Reviews accumulate as numbered files per stage:
`REVIEW_plan_1.md`, `REVIEW_plan_2.md`, `REVIEW_build_1.md`, etc.

The state-transition table is hard-coded in
`src/kbutillib/cli/subproject.py`. `kbu subproject advance <name>` reads
current state from `kbu-subproject.toml`, looks up next state in the
table, validates artifact preconditions
(`RESEARCH_PLAN.md` exists for `plan → p-review`, `REPORT.md` exists for
`synthesize → s-review`, etc.), and writes the new state back.

`kbu subproject set-status <name> <state>` is an admin override; bypasses
validation. Used for recovery and testing.

### Tier-1 skill (`KBUtilLib/.claude/commands/kbu-start.md`)

Markdown skill, user-invocable via `/kbu-start`. Four menu items via
AskUserQuestion:

1. **Help** — single-screen explainer: what KBUtilLib is, the tier-2
   workflow it produces, where to read more.
2. **Initialize** — invokes `kbu init` for machine-level setup; calls
   `cursor-setup` skill for Cursor + extension detection.
3. **New project** — collects name, target path, author info (name /
   affiliation / ORCID), optional first subproject name via
   AskUserQuestion; invokes `kbu new-project /path/to/<name> --name <name>
   --author ... --orcid ...`; prints exact `cursor /path/to/<name>/
   <name>.code-workspace` command and instructions to type `/kbu-start`
   in the integrated terminal.
4. **Update** — invokes `kbu init --update` (or equivalent) to pull
   KBUtilLib master + reinstall editable.

### Tier-2 skill (`templates/student-project/.claude/commands/kbu-start.md`)

Status-aware dashboard. On invocation:

1. Run `kbu subproject list` → table of all subprojects + current state +
   next-action.
2. Run `kbu session list --limit 5` → recent sessions across subprojects.
3. Present both tables, then AskUserQuestion menu — disabled items are
   marked "(not available: <reason>)".
4. Route to the chosen skill via slash invocation.

The menu items map to skill files in the same directory:

| Menu item | Skill file | Valid states |
|---|---|---|
| Plan | `kbu-plan.md` | `plan` |
| Build | `kbu-build.md` | `build` |
| Run | `kbu-run.md` | `run` |
| Synthesize | `kbu-synthesize.md` | `synthesize` |
| Review | `kbu-review.md` | `p-review`, `b-review`, `s-review` |
| Literature review | `kbu-literature-review.md` | any |
| Diagnose | `kbu-diagnose.md` | any |
| Update | `kbu-update.md` | any |

### Tier-2 skill files (lean forks + harvests)

- **`kbu-plan.md`** — lean fork of `/ai-design`. Strips: confront step,
  taskplan generation, PRD-registry registration, AIAssistant-state
  imports, conductor handoff, profile/blind-spot loading. Keeps: grill
  pattern, problem-framing prompts, decision capture. Output:
  `subprojects/<name>/RESEARCH_PLAN.md`. End-of-skill: calls
  `kbu subproject advance <name>` (`plan → p-review`) and
  `kbu session save --skill kbu-plan --subproject <name>`.

- **`kbu-build.md`** — lean fork of `/ai-conductor`. Strips: Maestro lane,
  taskplan validation, `register_prd` / `set_prd_status`, cross-machine
  dispatch, h100 / emailmac references. Keeps: read the plan, decompose
  to notebook-cell scaffolds, write `01_*.ipynb`, `02_*.ipynb`, `util.py`
  per the plan. Output: scaffolded notebooks under
  `subprojects/<name>/notebooks/`. End: `kbu subproject advance` +
  `kbu session save`.

- **`kbu-run.md`** — net-new. Deliberate notebook dashboard. Lists
  notebooks across subprojects via `kbu notebook list`; presents menu;
  student picks one; skill executes cell-by-cell using
  Jupyter / nbclient (via `kbu notebook exec` CLI wrapper), reading
  outputs and narrating key results between cells. On notebook
  completion, calls `kbu notebook mark-run <path>` and, if all notebooks
  in the subproject have been run, `kbu subproject advance` (`run →
  synthesize`). End: `kbu session save`.

- **`kbu-synthesize.md`** — harvested from BERIL `/synthesize` (skill
  prose adapted, BERDL specifics stripped). Two-pass: (1) read notebook
  outputs, CSVs in `data/`, figures in `figures/`, draft findings; (2)
  cross-reference literature via `/kbu-literature-review`, write
  `REPORT.md`. End: `kbu subproject advance` (`synthesize → s-review`)
  + session save.

- **`kbu-review.md`** — harvested from BERIL `/berdl-review`. Stage-aware:
  reads current state from `kbu subproject status <name>`. Reviews
  `RESEARCH_PLAN.md` for `p-review`, scaffolded notebooks for `b-review`,
  `REPORT.md` for `s-review`. Writes `REVIEW_<stage>_<n>.md`. On pass:
  `kbu subproject advance` (forward). On fail: `kbu subproject advance
  --reverse` (backward to action state). End: session save.

- **`kbu-literature-review.md`** — harvested from BERIL
  `/literature-review`. Standalone, callable in any state. Searches
  PubMed / bioRxiv / arXiv / Semantic Scholar / Google Scholar (via the
  same MCP tools BERIL uses). Appends to
  `subprojects/<name>/references.md`. End: session save.

- **`kbu-diagnose.md`** — lean fork of `/diagnose`. Strips: Maestro,
  AgentForge, cowork-inbox references, AIAssistant-state. Keeps:
  reproduce → minimise → hypothesise → instrument → fix loop. End:
  session save.

- **`kbu-update.md`** — net-new. Wraps `kbu update` CLI. Shows the diff,
  prompts the student to confirm, applies updates, reports what
  changed.

Each lean-fork skill file starts with a comment header:

```
<!-- Forked from <upstream-skill> at <commit-sha>; last reviewed
     <YYYY-MM-DD>. Independent thereafter — do not auto-merge upstream
     changes; periodic manual review only. -->
```

### `kbu` CLI surface (added commands)

All new commands live under `src/kbutillib/cli/`. The existing CLI module
gains new subcommand groups; no existing commands change semantics.

```
# Subproject lifecycle
kbu subproject create <name> [--title "..."]
kbu subproject list                        # all subprojects + state + next-action
kbu subproject status <name>               # current state + valid transitions + artifacts
kbu subproject advance <name> [--reverse]  # forward (default) or backward (review fail)
kbu subproject set-status <name> <state>   # admin override; bypasses validation

# Notebook tracking
kbu notebook list                          # path | subproject | last_run | modified_since
kbu notebook mark-run <path>               # record execution timestamp into subproject manifest
kbu notebook exec <path>                   # execute notebook cell-by-cell; emits per-cell output

# Sessions
kbu session save --skill <name> --subproject <name> --summary "..."
                 [--topics "..."] [--decisions "..."] [--next-steps "..."]
                 [--work-completed "..."] [--json -]
kbu session list [--subproject <name>] [--limit N]
kbu session show <id>

# Init / doctor (machine-level)
kbu init                                   # idempotent venv + editable install + jupyter kernel
kbu init --status                          # exit 0 if init done, 1 if not
kbu init --update                          # pull KBUtilLib master + reinstall editable
kbu doctor                                 # cursor presence, claude-extension presence, venv, kbu version

# Tier-1 only
kbu new-project <path> [--name ...] [--author ...] [--affiliation ...] [--orcid ...]
                       [--first-subproject ...]

# Tier-2 only
kbu update                                 # pull updates from parent install
kbu update --set-source <path>             # relocate parent KBUtilLib path
kbu update --check                         # dry-run; report what would change
```

### Manifest formats (TOML)

**Root** (`<project_root>/kbu-project.toml`, written by `kbu new-project`):

```toml
[project]
name = "henry_lab_studies"
title = "Henry Lab studies workspace"
created_at = "2026-06-04T15:30:00Z"

[[project.authors]]
name = "Chris Henry"
affiliation = "Argonne National Laboratory"
orcid = "0000-0001-..."

[kbutillib]
source_path = "/Users/student/Dropbox/Projects/KBUtilLib"
source_commit = "<commit-at-creation>"

[update]
last_pulled_at = "2026-06-04T15:30:00Z"
last_pulled_commit = "<commit-at-creation>"
```

**Subproject** (`subprojects/<name>/kbu-subproject.toml`, written by
`kbu subproject create`):

```toml
[subproject]
name = "metal_cofitness"
title = "Metal co-fitness analysis across ADP1 mutants"
status = "plan"
created_at = "2026-06-04T15:30:00Z"
last_session_at = "2026-06-04T15:30:00Z"

[artifacts]
research_plan = false
report = false
notebooks = []

[artifacts.reviews]
plan = []
build = []
synthesis = []

[[notebooks]]
path = "01_data_exploration.ipynb"
last_run_at = "2026-06-04T16:30:00Z"
modified_since_run = false

[[session_refs]]
id = "7f3a9b2c"
skill = "kbu-plan"
at = "2026-06-04T15:30:00Z"
summary = "Drafted plan for ADP1 metal cofitness"
```

### Session routing

`kbu session save` detection logic (single source in
`src/kbutillib/cli/session.py`):

```python
def _detect_aiassistant() -> Path | None:
    candidates = [
        Path.home() / "Dropbox/Projects/AIAssistant/state/sessions.db",
        Path.home() / "Projects/AIAssistant/state/sessions.db",
    ]
    for p in candidates:
        if p.exists():
            return p.parent.parent  # AIAssistant repo root
    return None
```

If AIAssistant detected:
1. Add KBUtilLib src to `sys.path` from the AIAssistant repo root's
   `PYTHONPATH=src` convention (import `assistant.state`).
2. Compute `project_id = f"kbu-{repo_basename}-{subproject}"`.
3. Auto-register the kbu project in AIAssistant registry on first call
   (only if not already registered).
4. Call `save_session(...)` with the session payload.

If not detected, write YAML to
`subprojects/<name>/sessions/<UTC-timestamp>-<skill>.yaml`.

The dashboard listing uses the same detection: when routed,
`get_recent_sessions(project_id=...)`; otherwise glob the local YAML
directory.

### `kbu init` — venv creation

```python
def init() -> None:
    if shutil.which("venvman"):
        run("venvman create kbutillib python=3.11")
        run("venvman use kbutillib")
        venv_python = ...  # discovered via venvman
    else:
        run("python -m venv .venv", cwd=KBUTILLIB_ROOT)
        venv_python = KBUTILLIB_ROOT / ".venv/bin/python"
    run(f"{venv_python} -m pip install -e .", cwd=KBUTILLIB_ROOT)
    run(f"{venv_python} -m ipykernel install --user --name=kbutillib "
        f"--display-name='KBUtilLib (kbu)'")
    # Mark init done
    write_init_marker()
```

Idempotency via init-marker file at `~/.config/kbu/init_done.json`
(records venv path + commit + timestamp). `kbu init --status` returns
exit 0 iff marker present AND venv binary still resolves.

### `kbu new-project` — child venv + editable install

```python
def new_project(path, name, author, affiliation, orcid, first_subproject):
    path.mkdir(exist_ok=False, parents=True)
    copy_tree(KBUTILLIB_ROOT / "templates/student-project", path,
              substitute={"project_name": name})
    if shutil.which("venvman"):
        run(f"venvman create {name} python=3.11")
        venv_python = ...
    else:
        run("python -m venv .venv", cwd=path)
        venv_python = path / ".venv/bin/python"
    run(f"{venv_python} -m pip install -e {KBUTILLIB_ROOT}")
    run(f"{venv_python} -m ipykernel install --user --name={name} "
        f"--display-name='{name} (kbu)'")
    write_kbu_project_toml(path, name, author, affiliation, orcid,
                           source_path=KBUTILLIB_ROOT,
                           source_commit=current_commit())
    run("git init", cwd=path)
    run("git add . && git commit -m 'Initial commit (kbu new-project)'",
        cwd=path)
    if first_subproject:
        run(f"{venv_python} -m kbutillib subproject create {first_subproject}",
            cwd=path)
    print(f"Open in Cursor: cursor {path}/{name}.code-workspace")
    print("Then in Cursor: open terminal → run 'claude' → type '/kbu-start'")
```

### `kbu update` (tier-2) — pull from parent install

```python
def update(set_source=None, check=False):
    cfg = read_kbu_project_toml()
    if set_source:
        cfg["kbutillib"]["source_path"] = str(set_source)
        write_kbu_project_toml(cfg)
    source = Path(cfg["kbutillib"]["source_path"])
    if not source.exists():
        die(f"Parent KBUtilLib not found at {source}. "
            "Run `kbu update --set-source <new-path>`.")
    if (source / ".git").is_dir():
        run("git pull", cwd=source)  # best-effort
    new_commit = current_commit(source)
    last_commit = cfg.get("update", {}).get("last_pulled_commit")
    diff = git_diff_paths(source, last_commit, new_commit,
                          paths=["templates/student-project/.claude/",
                                 "templates/student-project/.vscode/"])
    if not diff:
        print("Already up-to-date.")
        return
    if check:
        print(format_diff_summary(diff))
        return
    # Warn before clobber
    locally_modified = detect_locally_modified_template_files(
        cfg, project_root=Path.cwd())
    if locally_modified:
        print("WARNING: these files were modified locally and will be "
              "overwritten:")
        for f in locally_modified:
            print(f"  {f}")
        if not confirm("Proceed?"):
            return
    apply_diff(source, diff, project_root=Path.cwd())
    cfg["update"]["last_pulled_at"] = now_iso()
    cfg["update"]["last_pulled_commit"] = new_commit
    write_kbu_project_toml(cfg)
    print(format_diff_summary(diff))
```

Locally-modified detection: hash each template file at last-pull time
(stored in `kbu-project.toml` under `[update.file_hashes]`) and compare
against current file hash.

### Template tree (`KBUtilLib/templates/student-project/`)

```
templates/student-project/
├── .claude/
│   └── commands/
│       ├── kbu-start.md            # tier-2 dashboard
│       ├── kbu-plan.md
│       ├── kbu-build.md
│       ├── kbu-run.md
│       ├── kbu-synthesize.md
│       ├── kbu-review.md
│       ├── kbu-literature-review.md
│       ├── kbu-diagnose.md
│       └── kbu-update.md
├── .vscode/
│   └── extensions.json             # recommend anthropic.claude-code
├── {{project_name}}.code-workspace # Cursor workspace
├── subprojects/
│   └── .gitkeep
├── .gitignore                      # standard Python + .venv/ + ipynb_checkpoints
├── README.md                       # student-facing quickstart
└── kbu-project.toml.template       # populated by new-project
```

Variable substitution at copy time: `{{project_name}}` in filenames and
in `.code-workspace` / README content.

### jupyter-dev skill update

Update `KBUtilLib/agent-io/skills/jupyter-dev.md` (or wherever
jupyter-dev's home_path is) to:

- Mandate `subprojects/<name>/{notebooks,util.py,nboutput}` layout
  unconditionally — single source of truth even with one subproject.
- Add a "create new subproject" recipe pointing at
  `kbu subproject create <name>`.
- Remove any prior guidance permitting root-level `util.py` or
  `nboutput/` under `notebooks/`.
- Reference `kbu-start` and the tier-2 workflow as the canonical way to
  drive a notebook from plan to report.

### Deep modules

- **`kbutillib.cli.subproject`**: state-machine validator + TOML I/O.
  Interface: 5 CLI subcommands. Deep — small surface, state-machine
  precondition rules + manifest mutation behind it.
- **`kbutillib.cli.session`**: AIAssistant detection + dual-mode storage.
  Interface: 3 CLI subcommands. Deep — small surface, routing complexity
  hidden.
- **`kbutillib.cli.notebook`**: filesystem scan + execution wrapper.
  Interface: 3 CLI subcommands. Medium-depth.
- **`kbutillib.cli.init`**: venv creation + idempotency + doctor.
  Interface: 1 CLI subcommand + flags. Deep — subprocess orchestration
  + venvman/venv duality.
- **`kbutillib.cli.new_project`**: template copy + child venv + git init.
  Interface: 1 CLI subcommand. Deep — orchestration of independent
  subsystems.
- **`kbutillib.cli.update`**: parent-pull + diff + clobber-with-warn.
  Interface: 1 CLI subcommand + flags. Deep — diff + hash comparison
  + safe apply.

### Folded from confront round 1 (2026-06-04, task-5fcbca32)

#### State machine field naming (locked)

Manifest key: `[subproject].status` (NOT `state`). String value, hyphen-delimited.

Allowed `status` values (exhaustive):
`plan`, `p-review`, `build`, `b-review`, `run`, `synthesize`, `s-review`, `complete`.

Timestamp format throughout all manifests, sessions, and the init marker:
ISO-8601 UTC with `Z` suffix (e.g. `2026-06-04T15:30:00Z`). All `*_at` fields
use this format unconditionally.

`kbu subproject status <name>` reads `[subproject].status`; rejects unknown
values with a non-zero exit and an error message naming the offending file.

#### AIAssistant session-routing contract (locked)

When `_detect_aiassistant()` returns a non-None path, kbu prepends the detected
AIAssistant repo root + `src` to `sys.path` and imports:

```python
from assistant.state import save_session, get_recent_sessions
```

Required `save_session` payload keys (matching the existing AIAssistant API):
`project_id` (str — set to `f"kbu-{repo_basename}-{subproject}"`), `command`
(str — set to the kbu skill name, e.g. `"kbu-plan"`), `topics_discussed`
(list[str]), `decisions_made` (list[str]), `work_submitted` (list[str] —
file paths modified), `next_steps` (list[str]), `summary` (str).

Optional payload keys: `started_at`, `ended_at` (ISO-8601 UTC).

`save_session(...)` returns a `session_id` string. `get_recent_sessions(
project_id=..., limit=N)` returns a list of session dicts.

**Detection path list configurable** via env var `KBU_AIA_PATHS`
(colon-separated). Default: `~/Dropbox/Projects/AIAssistant/state/sessions.db
:~/Projects/AIAssistant/state/sessions.db`.

If the import fails (AIAssistant present on disk but `assistant.state` import
errors), kbu logs a warning, falls back to local YAML, and continues — session
save MUST NOT silently swallow.

**Auto-register**: on the first AIAssistant-routed `save_session` call for a
kbu project, if the registry has no entry for `project_id`, call
`assistant.state.registry.update_project(project_id, name=..., priority='low',
status='active')`. If that function is not importable, log a warning and skip
registration (the session still saves).

#### Notebook execution defaults (`kbu notebook exec`)

Kernel selection: prefer the project's named ipykernel (registered by
`kbu new-project` / `kbu init`). Resolve via
`jupyter_client.kernelspec.find_kernel_specs()` matching the project name
from `kbu-project.toml [project].name`. If no match, fall back to `python3`
kernel with a stderr warning.

Per-cell timeout: 600 seconds. Override via `KBU_NOTEBOOK_CELL_TIMEOUT`
environment variable.

Error policy: **stop on error by default**. Exception traceback is captured
in the executed notebook output and printed to stdout; exec exits non-zero.
Override with `--allow-errors` to continue past errored cells (recorded with
the original exception in cell outputs).

Output handling: writes executed cells back to the same `.ipynb` file
in place. Backup at `<notebook>.bak.<timestamp>.ipynb` created before write.

Stream output capture: stdout/stderr captured per cell; truncate at 1 MiB
per cell with a warning footer.

#### `kbu init` marker file (locked)

Location: `~/.config/kbu/init_done.json` (respect `XDG_CONFIG_HOME` when set).
Created on successful init; read by `kbu init --status` and `kbu doctor`.

Schema (version 1):

```json
{
  "version": 1,
  "initialized_at": "2026-06-04T15:30:00Z",
  "kbutillib_repo_path": "/Users/chenry/Dropbox/Projects/KBUtilLib",
  "kbutillib_commit": "<commit-sha-at-init>",
  "venv_manager": "venvman",
  "venv_python": "/abs/path/to/python",
  "jupyter_kernel_name": "kbutillib"
}
```

`venv_manager` is one of `"venvman"` or `".venv"`.

`kbu init --status` exit codes:
- `0` — marker present AND `venv_python` resolves to an executable.
- `1` — marker missing.
- `2` — marker present but `venv_python` no longer resolves (e.g., venv
  deleted; user should re-run `kbu init`).

venvman detection: `shutil.which("venvman")`. No `PATH` manipulation beyond
what venvman's own activation does.

#### `kbu update` diff representation and hashing

Tracked paths (under student-repo root): `.claude/commands/` and `.vscode/`.

Hash algorithm: **SHA-256** over file bytes.

Storage in `kbu-project.toml`:

```toml
[update.file_hashes]
".claude/commands/kbu-start.md" = "sha256:abc..."
".claude/commands/kbu-plan.md" = "sha256:def..."
".vscode/extensions.json" = "sha256:..."
```

Hashes recorded at end of `kbu new-project` and at end of `kbu update`.

Diff entry struct (in-memory, passed to `apply_diff`):

```python
@dataclass
class TemplateDiff:
    path: str               # relative to template root
    status: str             # "added" | "modified" | "deleted"
    old_hash: str | None    # sha256:... or None if new
    new_hash: str | None    # sha256:... or None if deleted
```

Locally-modified detection: for each path in `[update.file_hashes]`,
compute current on-disk SHA-256 and compare against recorded hash. Mismatch
classifies the file `locally_modified`. Update prompts before overwriting any
locally-modified file; abort if user declines.

#### `kbu session list` output schema

Default format: TSV to stdout, header row, columns in this order:

```
id<TAB>at<TAB>subproject<TAB>skill<TAB>summary
```

`id`: short hex (first 8 chars of `session_id`).
`at`: ISO-8601 UTC.
`subproject`: subproject name (empty if session is project-level).
`skill`: kbu skill name (e.g. `kbu-plan`).
`summary`: single-line string; embedded tabs/newlines collapsed to single
spaces; truncated to 120 chars with `…` if longer.

Order: most recent first by `at`.

Flags: `--limit N` caps rows (default 20). `--subproject <name>` filters.
`--json` switches to JSON output (list of session dicts; full payload, no
truncation).

The tier-2 `/kbu-start` dashboard parses the TSV form.

#### jupyter-dev source location (corrected)

The jupyter-dev skill is homed in **ClaudeCommands**, not KBUtilLib.
Canonical source: `ClaudeCommands/agent-io/skills/jupyter-dev.md`
(skill_registry.json: `home_repo=ClaudeCommands`,
`home_path=/Users/chenry/Dropbox/Projects/ClaudeCommands/agent-io/skills/jupyter-dev.md`).

The file at `KBUtilLib/.claude/commands/jupyter-dev.md` is a sync-deployed
copy — **DO NOT EDIT IT** (the next `claude-skills sync` overwrites edits).

Implementation task list updated accordingly:

- `ClaudeCommands` is added to the PRD's `repos` field (alongside `KBUtilLib`).
- The jupyter-dev formalization task edits
  `ClaudeCommands/agent-io/skills/jupyter-dev.md` and commits in
  ClaudeCommands.
- Post-edit, `claude-skills sync primary-laptop --apply` redeploys to all
  machines (including KBUtilLib's `.claude/commands/`).

#### Platform scope (v1)

v1 targets **macOS only**. Linux + Windows support is deferred to v2.

Rationale: KBUtilLib is currently used primarily on macOS lab machines, and
pinning cross-platform behavior requires Linux/Windows testing which is out
of v1 scope. Better to ship a tight macOS-only v1 than a half-cross-platform
v1 that breaks for the long tail of student environments.

Concrete v1 behavior on non-macOS (`sys.platform != "darwin"`):

- `kbu init` prints a one-screen message:
  > "v1 currently targets macOS. Linux/Windows support is planned for v2. To
  > install KBUtilLib manually for now: `python -m venv .venv && source
  > .venv/bin/activate && pip install -e <path-to-KBUtilLib>`. Then register
  > a Jupyter kernel: `python -m ipykernel install --user --name=kbutillib`.
  > You can use the tier-2 skills once `/path/to/KBUtilLib/.claude/` is on
  > your Claude Code skill search path."
  Exits with code 1.

- `kbu new-project` issues the same v1 warning before creating the venv,
  then proceeds with the `python -m venv` path only (no venvman detection
  on non-macOS). Cursor instructions are printed verbatim with a note that
  VS Code is the cross-platform equivalent.

- Tier-2 skills work cross-platform (no shell-specific commands).

- `KBU_PLATFORM_OVERRIDE=force` env var bypasses the macOS check for users
  willing to try Linux/Windows in v1 (best-effort; no support).

#### Literature-review MCP tool availability

The harvested `kbu-literature-review` skill references PubMed, bioRxiv, arXiv,
Semantic Scholar, and Google Scholar via MCP tools. BERIL ships these via its
own MCP server configuration; KBUtilLib does not.

v1 behavior:

- At skill invocation, check whether the expected MCP tools are available
  (via discovered tool list). If absent, the skill degrades gracefully:
  - Prints a "to enable structured literature search, install the
    `<server-name>` MCP server: `<install command>`" message. The exact
    server names and install commands are determined at implementation time
    by reading BERIL's `.mcp.json` and documenting verbatim.
  - Falls back to direct `WebSearch` for the requested query (less
    structured; no citation snowballing).
  - Still appends results to `references.md` with an explicit note
    `"[fallback path — install MCP servers for richer results]"`.

- The kbu-literature-review skill is callable in any state. Calling it
  without MCP tools available is NOT an error.

- Implementation task: at fork time, read BERIL's `.mcp.json` to identify the
  exact MCP server names and install commands; document them in the
  kbu-literature-review skill body verbatim.

## Testing Decisions

### What good tests look like here

External behaviour. Mock subprocess + filesystem, not internal helpers.

### Test targets

- **State-machine transitions**: `advance` from each state moves to the
  correct next state; `--reverse` from each review state moves to
  correct prior action state; admin `set-status` bypasses validation;
  forward `advance` rejected when artifact preconditions unmet
  (`plan → p-review` requires `RESEARCH_PLAN.md`).
- **Session routing detection**: when both
  `~/Dropbox/Projects/AIAssistant/state/sessions.db` and
  `~/Projects/AIAssistant/state/sessions.db` are absent, falls back to
  local YAML; when present, calls into AIAssistant. Mock
  `Path.exists` and `assistant.state.save_session`.
- **Manifest TOML round-trip**: write + read of root manifest and
  subproject manifest preserves all fields; partial writes
  (e.g., new session_ref append) don't drop other state.
- **Notebook list + modified-since**: given fixture notebooks with
  known mtimes and recorded `last_run_at`, `kbu notebook list` returns
  correct `modified_since_run` flags.
- **`kbu init --status`**: returns exit 0 when init-marker present and
  venv resolves; exit 1 when marker missing; exit 1 when marker
  present but venv binary gone.
- **`kbu new-project` template substitution**: `{{project_name}}` in
  workspace filename and content replaced correctly; venv created;
  kbu-project.toml has correct `source_path` and `source_commit`.
- **`kbu update` clobber-with-warn**: given a locally-modified template
  file (hash mismatch vs. recorded), update is rejected without
  `--force`; with confirmation prompt accepted, the file is overwritten
  and the new hash recorded.
- **Update `--set-source`**: relocates source path and clears stale
  `last_pulled_commit` so next `update` re-evaluates against the new
  parent.

### Tests deliberately not written

- No tests on the *quality* of skill output (RESEARCH_PLAN.md prose,
  REPORT.md prose) — those are skill-prose effects, not deterministic
  code paths.
- No tests on Claude-Code's slash-command routing (out of our control).
- No tests on Jupyter cell-execution mechanics beyond "the CLI invokes
  nbclient with the correct arguments" (nbclient is upstream).

### Prior art

- AIAssistant's `assistant/state/` test suite for manifest /
  TOML / SQLite I/O patterns.
- KBUtilLib's existing `tests/test_composition_smoke.py` for CLI
  fixture conventions.

## Out of Scope

- Pitfall capture as a cross-cutting skill (BERIL has it; deferred for
  v1).
- Per-project `template_version` migration support (defer to first
  breaking template change).
- `kbu update --from-github` fallback (deferred to v2; until then,
  `--set-source` covers moved repos).
- Vendoring KBUtilLib into student projects (always editable-installed).
- Cursor auto-opening the Claude panel on workspace open (give student
  instructions instead).
- Auto-running notebooks in Plan or Build (only Run executes cells).
- Tier-2 skills importing from `assistant.state` for anything other
  than session routing.
- Backporting state-machine support to AIAssistant's own PRD lifecycle.
- A `/kbu-start` agent in `.claude/agents/` (slash command only).
- Per-cell narration depth tuning (use a sensible default — concise
  one-paragraph-per-cell — and iterate via student feedback).
- Multi-user collaboration on a single student project
  (single-user assumption).
- Notebook execution that uses remote compute (h100 / Maestro / CTS).
  Local kernel only.

## Further Notes

- The lean-fork skill files (kbu-plan, kbu-build, kbu-diagnose) carry a
  comment header naming their source skill, source commit, and last
  review date. They evolve independently after fork; periodic manual
  review only. This is the agreed approach (Q5).
- The harvested skill files (kbu-synthesize, kbu-review,
  kbu-literature-review) carry a similar header naming BERIL as source.
  Strip BERDL-specific references at harvest time
  (Spark, MinIO, beril.yaml, projects/ layout).
- The state machine's discipline is the central learning device: the
  student can't skip from Plan to Synthesize because the menu won't
  let them. This is intentional — the BERIL precedent demonstrated
  this is the mechanism that prevents fabricated interpretations.
- All session payloads are valid in both modes (local YAML vs.
  AIAssistant SQLite) — the schema overlap is the AIAssistant `sessions`
  table column set. New fields go to YAML first; AIAssistant migration
  follows if needed.
- KBUtilLib at this design point: composition refactor complete (per
  project_state); tier-2 skills will exercise the
  `KBUtilLib` facade as their canonical entry point.

## Acceptance Criteria

1. KBUtilLib `.claude/commands/kbu-start.md` exists (tier-1) and is invocable as `/kbu-start` from within the KBUtilLib repo. Menu: Help, Initialize, New project, Update.
2. `KBUtilLib/templates/student-project/.claude/commands/kbu-start.md` exists and contains the 8-item tier-2 dashboard (Plan, Build, Run, Synthesize, Review, Literature review, Diagnose, Update), status-gated.
3. All 8 tier-2 skill files exist at `templates/student-project/.claude/commands/`: kbu-plan, kbu-build, kbu-run, kbu-synthesize, kbu-review, kbu-literature-review, kbu-diagnose, kbu-update.
4. The state machine has 8 states (plan, p-review, build, b-review, run, synthesize, s-review, complete). Forward transitions are validated against artifact preconditions. Review-fail transitions return to the prior action state. Manifest key is `[subproject].status` (NOT `state`).
5. All timestamp fields throughout manifests, sessions, and the init marker use ISO-8601 UTC with `Z` suffix.
6. Subproject manifest is a TOML file at `subprojects/<name>/kbu-subproject.toml` with the schema specified in Implementation Decisions.
7. Root project manifest is a TOML file at `<project_root>/kbu-project.toml` with the schema specified in Implementation Decisions.
8. `kbu subproject {create,list,status,advance,set-status}` are registered subcommands. `advance` validates artifact preconditions; `set-status` bypasses validation. `advance --reverse` moves from a review state to its prior action state.
9. `kbu notebook {list,mark-run,exec}` are registered subcommands. `exec` defaults: project-named kernel (fallback python3), 600s per-cell timeout (overridable via `KBU_NOTEBOOK_CELL_TIMEOUT`), stop-on-error (overridable via `--allow-errors`), in-place write with `.bak.<timestamp>.ipynb` backup.
10. `kbu session {save,list,show}` are registered subcommands. `list` outputs TSV with columns `id<TAB>at<TAB>subproject<TAB>skill<TAB>summary`, recent-first, default limit 20. `--json` switches to JSON.
11. Session save routes to AIAssistant if any path in `KBU_AIA_PATHS` (default: `~/Dropbox/Projects/AIAssistant/state/sessions.db:~/Projects/AIAssistant/state/sessions.db`) exists.
12. Session save payload uses `project_id`, `command`, `topics_discussed`, `decisions_made`, `work_submitted`, `next_steps`, `summary` keys (matching `assistant.state.save_session`).
13. AIAssistant session import is `from assistant.state import save_session, get_recent_sessions`. If the import fails, kbu falls back to local YAML with a warning; never silently swallows.
14. On first AIAssistant-routed session for a kbu project, auto-register the project as `kbu-<repo_basename>-<subproject>` via `assistant.state.registry.update_project` (skip with warning if function not found).
15. `kbu init` creates a venv (venvman if `shutil.which("venvman")` returns non-None, else `python -m venv .venv` in KBUtilLib root), installs KBUtilLib editable, registers Jupyter kernel `kbutillib`, writes marker at `~/.config/kbu/init_done.json` (XDG_CONFIG_HOME respected).
16. The init marker matches the schema specified in Implementation Decisions (version 1; fields: initialized_at, kbutillib_repo_path, kbutillib_commit, venv_manager, venv_python, jupyter_kernel_name).
17. `kbu init --status` returns exit 0 if marker present AND `venv_python` resolves; exit 1 if marker missing; exit 2 if marker present but `venv_python` no longer resolves.
18. `kbu new-project <path>` creates a child venv (venvman if present else `.venv`), pip-installs KBUtilLib editable from the source path (NOT vendored), registers a Jupyter kernel named after the project, writes kbu-project.toml with `[kbutillib].source_path` and `[kbutillib].source_commit`, runs `git init` + initial commit, prints exact Cursor + claude instructions.
19. `kbu update` reads `[kbutillib].source_path` from kbu-project.toml; pulls source repo if it's a git repo; diffs `templates/student-project/.claude/` and `.vscode/` between recorded `last_pulled_commit` and current HEAD; presents a diff summary; applies on confirmation.
20. `kbu update` records SHA-256 hashes of all tracked template files under `[update.file_hashes]` in kbu-project.toml. Locally-modified files (hash mismatch) trigger a clobber-with-warn prompt; abort if user declines.
21. `kbu update --set-source <path>` relocates `[kbutillib].source_path`; clears `[update].last_pulled_commit` so the next `update` re-evaluates against the new parent.
22. `kbu update --check` performs a dry-run; prints the diff summary; does not write.
23. `kbu new-project` substitutes `{{project_name}}` in both filenames (e.g., `{{project_name}}.code-workspace`) and file content within the copied template tree.
24. The jupyter-dev formalization edits the canonical source at `ClaudeCommands/agent-io/skills/jupyter-dev.md` (NOT the sync-deployed copy at `KBUtilLib/.claude/commands/jupyter-dev.md`). Post-edit, run `claude-skills sync primary-laptop --apply` to redeploy.
25. The repos list for this PRD includes BOTH `KBUtilLib` and `ClaudeCommands`.
26. v1 targets macOS only. On non-macOS (`sys.platform != "darwin"`), `kbu init` and `kbu new-project` print the v1 message and exit non-zero unless `KBU_PLATFORM_OVERRIDE=force` is set.
27. `kbu-literature-review` skill checks for MCP tool availability at invocation; on absence, falls back to `WebSearch`, appends results to `references.md` with a `[fallback path]` note, and never errors out.
28. Each lean-fork skill file (`kbu-plan.md`, `kbu-build.md`, `kbu-diagnose.md`) begins with a comment header naming the upstream source skill, the source commit SHA, and the last review date.
29. Each harvested skill file (`kbu-synthesize.md`, `kbu-review.md`, `kbu-literature-review.md`) begins with a comment header naming BERIL-research-observatory as source plus the BERIL commit SHA.
30. The Maestro `status=failed` completion sentinel is treated as a false-negative when a fresh, well-formed stall-report (containing both `## STALL REPORT` and `## FREE CRITIQUE` headers) is committed to the task branch. Implementation tasks that rely on Maestro status MUST verify via git, not status alone.
