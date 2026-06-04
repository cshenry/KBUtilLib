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
