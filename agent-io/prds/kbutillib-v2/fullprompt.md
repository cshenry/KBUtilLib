# KBUtilLib 2.0 — research-project layout, plan workflow, and adoption

## Problem Statement

A researcher using `kbu new-project` or `kbu bootstrap` to set up a
research repo today encounters three friction points and one naming-hygiene
problem:

1. **Planning is one grilled pass, one artifact.** `/kbu-plan` produces
   a single `RESEARCH_PLAN.md` from a goals grill. Literature review,
   detailed-plan grilling, and machine-readable task decomposition either
   happen ad hoc or not at all. The downstream skill `/kbu-build` re-derives
   the notebook list from RESEARCH_PLAN.md prose.
2. **Inputs are duplicated across subprojects.** The canonical layout
   puts `data/` inside each subproject (`subprojects/<name>/data/`),
   so when two subprojects use the same fitness dataset, the data is
   either copied or relative-pathed across the tree. Models and genomes
   have the same shared-input nature with no shared home.
3. **No way to onboard existing notebook directories into a bootstrapped
   repo.** Researchers with directories like `ModelingLOE/notebooks/
   fitness_loe/` have no command to convert them into
   `subprojects/fitness_loe/` with the canonical tree. Hand-migration is
   error-prone and doesn't get done.
4. **No naming distinction between user-invocable slash commands and
   helper skills.** `/kbu-literature-review`, `/kbu-review`, and
   `/kbu-diagnose` are not meant to be user-invoked but appear as
   first-class slash commands. They also pollute the parent context
   because they run as slash commands in the main thread — research
   content, review notes, and diagnosis output all land in the user's
   conversation.

## Solution

A coordinated set of changes to KBUtilLib's research-project template,
CLI, skills/agents, and the `claude-skills` sync tool. The user-visible
solution from the researcher's perspective:

1. `/kbu-plan` becomes a 4-step grilled flow producing four artifacts
   (`RESEARCH_PLAN.md`, `literature/` directory with per-topic syntheses,
   manifest `notebooks: [...]`, and `TASKS.md`).
2. The repo grows root-level `data/`, `models/`, `genomes/` shared dirs
   (extensible via a `[layout.shared_dirs]` table). Per-subproject
   `data/` and `user_data/` go away. The per-subproject tree gains
   `literature/` and a hidden `.cache/` for KBase tool fetches.
3. A new `kbu subproject adopt <path> --name <name>` command onboards
   an existing notebook directory: dumb-moves it to
   `subprojects/<name>/archive/`, scaffolds the canonical tree, emits a
   `.adoption-notes.md` worksheet, and lands the subproject in a new
   `migrate` state. A new `/kbu-migrate` skill walks the user through
   the four design steps plus migration-specific integration (path
   rewrites, `util.py` audit, NotebookSession migration, data relocation).
4. A naming policy: `kbu-*` slash commands in `.claude/commands/` are
   user-invocable; `kbu-sub-*` subagents in `.claude/agents/` run with
   their own context. `claude-skills` sync gains a `type: command | agent`
   frontmatter field that routes sources to the right destination.

## User Stories

1. As a researcher starting a new subproject, I want `/kbu-plan` to grill
   me first on goals, then run a literature review subagent that
   doesn't pollute my main thread with paper text, then grill me on the
   detailed plan, and finally decompose into a machine-readable notebook
   list — so the plan-to-build handoff is mechanical, not a re-derive.

2. As a researcher running `/kbu-plan` Step 2, I want the literature
   review to produce one `literature/<topic-slug>.md` per discrete
   topic plus a `literature/index.md` listing topics — so I can come
   back later and re-run a single topic without re-doing the others.

3. As a researcher running `/kbu-plan` Step 3, I want the plan structure
   to be grilled before the file is written — so when I disagree with
   the proposed structure I correct it conversationally rather than
   editing the written file.

4. As a researcher running `/kbu-plan` Step 4, I want the manifest's
   `notebooks: [...]` to be populated with one entry per intended
   notebook, plus a parallel `TASKS.md` for human reading — so
   `/kbu-build` consumes the manifest as the source of truth and
   the human reader has a flat task list to review.

5. As a researcher with two subprojects analyzing the same dataset, I
   want shared `data/`, `models/`, `genomes/` at the repo root — so I
   don't duplicate inputs and my notebooks reference a single canonical
   path.

6. As a researcher needing to add a new shared dir type (e.g.
   `proteomes/`), I want `[layout.shared_dirs]` in `kbu-project.toml`
   to be extensible — so I add it via manifest edit and the layout
   tooling picks it up.

7. As a researcher running a subproject, I want a clear separation
   between automated intermediates (`nboutput/`), curated final figures
   (`figures/`), KBase tool cache (`.cache/`), and shared inputs (root
   `data/`/`models/`/`genomes/`) — so my notebook code never has to
   guess where to write something.

8. As a researcher in a pre-existing bootstrapped repo with the old
   layout, I want `kbu migrate` to walk me through retrofitting the
   layout — so I opt in when ready, file by file.

9. As a researcher with existing notebooks in
   `notebooks/fitness_loe/`, I want
   `kbu subproject adopt notebooks/fitness_loe --name fitness_loe` to
   move them into `subprojects/fitness_loe/archive/`, scaffold the
   canonical tree, and emit a worksheet — so I can run `/kbu-migrate`
   afterward to do the actual integration with agent help.

10. As a researcher running `kbu subproject adopt` against a path
    tracked by a different git repo, I want the command to refuse
    rather than corrupt the other repo's working tree.

11. As a researcher running `/kbu-migrate` after `adopt`, I want the
    skill to read existing notebooks and `.adoption-notes.md`, infer my
    apparent hypothesis from the content, and let me confirm or correct
    it — so I don't have to re-explain what the notebooks already say.

12. As a researcher running `/kbu-migrate`, I want it to walk through
    each non-notebook item in `archive/` (data dirs, figures, caches)
    and propose where they belong in the new layout — so I'm prompted
    on each decision rather than having to plan the move myself.

13. As a researcher running `/kbu-migrate`, I want the skill to rewrite
    in-notebook relative paths after relocating data — so my adopted
    notebooks still run.

14. As a researcher running `/kbu-build` on a migrated subproject, I
    want it to verify the existing notebooks match the manifest list,
    add any net-new notebooks per the plan, and warn if the manifest
    lists notebooks that aren't present — so `/kbu-build` for migrated
    projects is "verify and extend," not "scaffold from scratch."

15. As a researcher reading `.claude/` deployed by `claude-skills sync`,
    I want user-invocable commands to look obviously different from
    subagents — so I know which ones I can type `/` and invoke.

16. As a researcher invoking `/kbu-plan` Step 2's literature review, I
    want it to run in its own context window — so paper text and search
    output never pollute my main conversation.

17. As a developer modifying a skill source, I want `type: command | agent`
    in the frontmatter to be the only thing that determines whether the
    deployed file lands at `.claude/commands/` or `.claude/agents/` —
    so I can move a skill between layers by editing one frontmatter line.

18. As the maintainer of `claude-skills`, I want the new `type` field
    to be backward-compatible (default `command`) — so existing skill
    sources keep deploying to the same place.

19. As a researcher with an `archive/` directory left after migration,
    I want it tracked by default so the migration is reviewable in
    diffs — but with large binaries flagged for manual `.gitignore`/LFS
    decisions.

## Implementation Decisions

### Layout module (NEW deep module)

A new `kbutillib.layout` module owns the canonical-layout knowledge.
Every other module asks `Layout` rather than hardcoding paths.

Interface:

```python
from pathlib import Path

DEFAULT_SHARED_DIRS = ("data", "models", "genomes")

def read_shared_dirs(project_root: Path) -> list[str]:
    """Return [layout.shared_dirs] from kbu-project.toml, or the
    DEFAULT_SHARED_DIRS tuple as a list if the field is absent."""

def subproject_subdirs(*, adopted: bool) -> list[str]:
    """Return the canonical subproject subdir names. Includes
    'archive' iff adopted is True. Always includes:
    notebooks, figures, nboutput, '.cache', literature, sessions."""

def subproject_gitignore_lines() -> list[str]:
    """Return the gitignore patterns added per subproject:
    .cache/, nboutput/, .adoption-notes.md."""

def root_gitignore_lines(shared_dirs: list[str]) -> list[str]:
    """Return root-level gitignore patterns: shared dirs are NOT
    gitignored at root, but large-file patterns (*.h5, *.pkl, *.parquet)
    inside shared_dirs are. Specific patterns documented in tests."""
```

Layout is small interface, lots of behavior — every consumer asks
`Layout`. Hardcoded constants (`DEFAULT_SHARED_DIRS`, dir name strings)
live here and only here.

### `kbu-project.toml` schema addition

Add `[layout]` table:

```toml
[layout]
shared_dirs = ["data", "models", "genomes"]
```

Absent `[layout]` table or absent `shared_dirs` key → `Layout.read_shared_dirs`
returns the default. New `kbu new-project` and `kbu bootstrap` write
the default list explicitly. `kbu migrate` adds the table to existing
manifests during retrofit.

### Subproject manifest schema additions

`kbu-subproject.toml` already has `subproject.status`. Two new status
values:
- `migrate` — set by `kbu subproject adopt`, advances to `p-review`
  with the same `RESEARCH_PLAN.md` precondition as `plan`.

`notebooks: [...]` already exists; behavior unchanged. `last_run_at`
and `modified_since_run` semantics unchanged.

### `/kbu-plan` 4-step flow

Single slash command in `.claude/commands/`. Internal flow:

1. **Goals + grill goals.** Phase output: `## Goals` section drafted
   in memory, not yet written.
2. **Literature review.** Invoke `Agent(subagent_type="kbu-sub-literature-review",
   prompt=<topic list + depth tier + subproject path>)`. Subagent
   writes `subprojects/<name>/literature/<topic-slug>.md` and
   `literature/index.md`, returns short summary of topics covered.
3. **Detailed plan + grill plan.** Walk the plan dependency tree
   (hypothesis → success criteria → data inputs → methods → notebooks
   → outputs → out-of-scope), grilling each. Then write `RESEARCH_PLAN.md`
   from confirmed structure.
4. **Decompose into tasks.** Populate manifest `notebooks: [...]` (one
   entry per intended notebook with `slug`, `purpose`, `last_run_at=None`,
   `modified_since_run=True`). Render `TASKS.md` from the same data for
   human reading. Manifest is source of truth — `TASKS.md` is a view.

State advance: `kbu subproject advance <name>` after the four steps.

### `/kbu-migrate` skill (NEW)

Same 4-step structure as `/kbu-plan` plus migration-specific work:

1. **Read existing artifacts.** Scan `archive/` for `.ipynb` (any depth),
   read first markdown cell of each, read `util.py` if present, read
   `.adoption-notes.md`.
2. **Goals + grill goals (inferred-then-confirmed).** Present apparent
   hypothesis from notebook content; user confirms or corrects.
3. **Literature review** (subagent, topic list seeded from notebook
   subject matter).
4. **Path/data relocation pass.** Walk `.adoption-notes.md`. For each
   non-notebook item under `archive/`:
   - Propose destination (root `data/`, root `data/<subproject>/`,
     subproject `figures/`, subproject `nboutput/`, or stay in `archive/`).
   - On user approval, move the item and rewrite in-notebook relative
     path references (regex sweep of common patterns: `pd.read_*`,
     `open(`, `Path(`, `np.load`, etc).
5. **`util.py` audit.** Scan `archive/util.py` (or root of `archive/`)
   for helpers. Group by similarity to KBUtilLib modules; propose
   deletion/replacement for overlapping helpers; preserve
   project-specific ones in `notebooks/util.py`.
6. **NotebookSession migration.** Scan first cell of each `.ipynb` in
   `archive/`. Flag missing `NotebookSession.kbu` initialization;
   recommend rewrites.
7. **Detailed plan + grill plan.** Same as `/kbu-plan` Step 3 but
   informed by existing content.
8. **Decompose into tasks + relocate notebooks.** Move surviving
   `.ipynb` files from `archive/` to `notebooks/` (renaming per user
   approval), populate manifest `notebooks: [...]` with the actual
   slugs and `last_run_at=None`, render `TASKS.md`.

Result: same artifact contract as `/kbu-plan` (`RESEARCH_PLAN.md`,
`literature/`, `TASKS.md`, populated manifest). Subproject advances
`migrate → p-review`.

### `kbu subproject adopt` CLI command (NEW)

```python
@subproject_cmd.command(name="adopt")
@click.argument("path", type=click.Path(exists=False))
@click.option("--name", required=True, help="Subproject name.")
def adopt_cmd(ctx, path: str, name: str) -> None:
    # 1. Resolve project_root (must have kbu-project.toml).
    # 2. Pre-flight refusal checks (six rules — see below).
    # 3. shutil.move(path, project_root/subprojects/<name>/archive)
    # 4. Scaffold canonical subdirs via Layout.subproject_subdirs(adopted=True)
    # 5. Write kbu-subproject.toml with status="migrate", empty notebooks: [].
    # 6. Write .adoption-notes.md inventory (scan_archive helper).
    # 7. Append subproject gitignore lines (.cache/, nboutput/,
    #    .adoption-notes.md) via the existing helper or add one.
```

**Pre-flight refusal rules** (in order):
1. cwd must be inside a kbu-bootstrapped project — `_find_project_root`
   must locate `kbu-project.toml`.
2. `<path>` must exist and be a directory.
3. `subprojects/<name>/` must NOT already exist.
4. `<path>` must not overlap the resolved destination path.
5. If `<path>` is git-tracked, it must be tracked by the **same** repo
   as the current project (run `git -C <path> rev-parse --show-toplevel`
   and compare to `git -C <project_root> rev-parse --show-toplevel`).
   Refuse if different. Untracked paths are fine.
6. Warn (do not refuse) if `<path>` contains zero `.ipynb` files.

### Adoption inventory scanner

New helper `kbutillib.cli.adopt._inventory.scan_archive`:

```python
@dataclass
class AdoptionInventory:
    notebooks: list[Path]            # all .ipynb under archive_dir, recursive
    subdirs: list[tuple[Path, int]]  # (relative path, total size in bytes)
    oversize_files: list[tuple[Path, int]]  # files >10MB
    path_refs: dict[Path, list[str]] # per-notebook regex hits

def scan_archive(archive_dir: Path) -> AdoptionInventory: ...

def write_adoption_notes(subproject_dir: Path, archive_dir: Path,
                         source_path: Path) -> None: ...
```

Pure function. Easily unit-testable with a fake directory tree.

Path-reference regex set (initial):
- `pd\.read_(csv|tsv|excel|parquet|hdf|json)\s*\(\s*["']([^"']+)["']`
- `open\s*\(\s*["']([^"']+)["']`
- `Path\s*\(\s*["']([^"']+)["']`
- `np\.load\s*\(\s*["']([^"']+)["']`
- `joblib\.load\s*\(\s*["']([^"']+)["']`

Captured paths that don't start with `/`, `~`, or a project-root marker
are flagged as relative.

### State machine update

`kbutillib.cli.subproject`:

```python
_STATES = [
    "plan", "migrate", "p-review", "build", "b-review",
    "run", "synthesize", "s-review", "complete",
]

_FORWARD = {
    "plan": "p-review",
    "migrate": "p-review",   # NEW edge
    "p-review": "build",
    # ... unchanged
}

_NEXT_ACTION["migrate"] = "Migrate"

# _check_forward_preconditions: 'migrate' branch checks RESEARCH_PLAN.md
# (same as 'plan' branch).
```

`_REVERSE` is unchanged — migrate state has no reverse counterpart (the
reverse states are for review-fail, not entry-point switching).

### Subproject scaffolding update

`_scaffold_subproject` in `subproject.py` becomes:

```python
def _scaffold_subproject(subproject_dir: Path, name: str, title: str,
                         adopted: bool = False) -> None:
    subproject_dir.mkdir(parents=True, exist_ok=True)
    for d in layout.subproject_subdirs(adopted=adopted):
        (subproject_dir / d).mkdir(exist_ok=True)
    util_py = subproject_dir / "notebooks" / "util.py"
    if not util_py.exists():
        util_py.write_text(
            f"# {name} — shared notebook utilities\n"
            "# Add project-wide helpers here.\n"
        )
    # references.md is RETIRED — no longer scaffolded.
```

`create_cmd` calls with `adopted=False`; `adopt_cmd` calls with
`adopted=True`.

### `kbu migrate` command (NEW, repo-level)

```python
@cli.command(name="migrate")
def migrate_cmd(ctx) -> None:
    """Retrofit a bootstrapped repo onto KBUtilLib 2.0 layout."""
    # 1. Verify cwd is bootstrapped.
    # 2. If [layout.shared_dirs] absent in kbu-project.toml, add it
    #    with DEFAULT_SHARED_DIRS.
    # 3. For each shared dir not present at root, create + .gitkeep.
    # 4. Walk subprojects/<name>/. For each:
    #    a. If data/ exists, prompt user where to move contents:
    #       (i) root data/<name>/, (ii) root data/ (merge), (iii) keep
    #       as nboutput/, (iv) skip.
    #    b. If user_data/ exists, same prompt.
    #    c. If references.md exists, prompt: convert to literature/index.md
    #       (preserving content), keep as-is, or delete.
    #    d. Add .cache/, literature/ dirs if missing.
    # 5. Append per-subproject gitignore lines to root .gitignore (one
    #    block per subproject, marker-delimited like bootstrap does).
```

User-driven, prompts per item. No silent moves.

### Templates

`templates/research-project/`:
- Add `data/.gitkeep`, `models/.gitkeep`, `genomes/.gitkeep`.
- Drop nothing — the template has no per-subproject defaults
  (`subprojects/.gitkeep` only).
- `kbu-project.toml.template`: append `[layout]\nshared_dirs = ["data",
  "models", "genomes"]\n`.
- `.gitignore`: add root patterns for large-file types in shared dirs
  (`data/**/*.h5`, `models/**/*.pkl`, etc — final set in tests).

### Skills / agents source layout

Sources unified under `KBUtilLib/agent-io/skills/<name>.md` with new
frontmatter field `type: command | agent`:

```yaml
---
name: kbu-sub-literature-review
description: Search and review biological literature using MCP tools.
allowed-tools: Bash, Read, Write, WebSearch, Agent, ToolSearch
type: agent
---
```

`claude-skills sync` reads `type` and routes:
- `type: command` (default if absent) → `.claude/commands/<name>.md`.
- `type: agent` → `.claude/agents/<name>.md`.

Sources to add/modify in this PRD:

| Action | Source file | kind |
|---|---|---|
| Rewrite | `kbu-plan.md` | command |
| Create | `kbu-migrate.md` | command |
| Modify | `kbu-build.md` (add adopted-branch) | command |
| Modify | `kbu-start.md` (sweep for path conventions) | command |
| Modify | `kbu-run.md` (path conventions) | command |
| Modify | `kbu-synthesize.md` (path conventions, lit subagent) | command |
| Modify | `kbu-update.md` (kbu migrate awareness) | command |
| Rename + convert | `kbu-literature-review.md` → `kbu-sub-literature-review.md` | agent |
| Rename + convert | `kbu-review.md` → `kbu-sub-review.md` | agent |
| Rename + convert | `kbu-diagnose.md` → `kbu-sub-diagnose.md` | agent |

The rename also retires the old slash command source — sync will not
deploy a `kbu-literature-review` slash command anymore. (Per Chris's
2026-06-07 decision policy: PRD/agent-io history doesn't get renamed,
but live skill files do.)

### `claude-skills` sync update

In `~/Dropbox/Projects/ClaudeCommands` (where the sync tool lives):
- Parse `type:` from source frontmatter; default to `command` if absent.
- Route deploy destination accordingly.
- Inventory output includes `type` column.
- Drift detection treats source-with-kind=agent vs deployed-at-commands
  as a sync mismatch.

### Subproject scaffold deltas summary

| Dir | Before | After (virgin) | After (adopted) |
|---|---|---|---|
| `notebooks/` | yes | yes | yes |
| `nboutput/` | yes | yes | yes |
| `data/` | yes | **dropped** | **dropped** |
| `user_data/` | yes | **dropped** | **dropped** |
| `figures/` | yes | yes | yes |
| `sessions/` | yes | yes | yes |
| `literature/` | — | **NEW** | **NEW** |
| `.cache/` | — | **NEW (hidden)** | **NEW (hidden)** |
| `archive/` | — | — | **NEW (transient)** |
| `references.md` | yes | **dropped** | **dropped** |

### Specifics folded from confront round 1

The following decisions were folded after the cross-family confront round
on 2026-06-08 (codex / h100, task-d9876e31).

**Layout module — concrete gitignore + subdir lists.**

```python
DEFAULT_SHARED_DIRS = ("data", "models", "genomes")

# subproject_subdirs(adopted=False) MUST return exactly, in order:
["notebooks", "figures", "nboutput", ".cache", "literature", "sessions"]
# subproject_subdirs(adopted=True) MUST return the same list with "archive"
# appended at the end (7 entries).

# subproject_gitignore_lines() MUST return exactly, in order:
[".cache/", "nboutput/", ".adoption-notes.md"]

# root_gitignore_lines(shared_dirs) MUST return, in order, for each d in
# shared_dirs (in the order they appear in shared_dirs):
#   f"{d}/**/*.h5"
#   f"{d}/**/*.pkl"
#   f"{d}/**/*.parquet"
# and nothing else. No other patterns.
```

**`kbu-project.toml` parsing semantics.**
- Location: repository root (the dir returned by `_find_project_root`).
- Parser: `tomllib` (stdlib, Python ≥3.11) with UTF-8.
- Missing file → `list(DEFAULT_SHARED_DIRS)`.
- Present file with missing `[layout]` or missing `shared_dirs` key →
  `list(DEFAULT_SHARED_DIRS)`.
- Unknown keys in `[layout]` → ignored silently (not an error).
- Malformed TOML → re-raise `tomllib.TOMLDecodeError` (caller's problem).

**Adoption inventory scanner — precise rules.**
- `AdoptionInventory.notebooks` paths are **relative to `archive_dir`**.
- `AdoptionInventory.subdirs` paths are **relative to `archive_dir`**.
- Traversal does **not** follow symlinks (`os.walk(..., followlinks=False)`).
- Skip directories named `.ipynb_checkpoints` and any dir starting with `.`
  except the top-level `archive_dir` itself.
- Oversize file threshold: strictly `> 10_000_000` bytes (10 million bytes,
  not 10 MiB).
- Notebook reading: `nbformat.read(path, as_version=4)`; first markdown
  cell's `source` field. If no markdown cells, empty string.
- Path-reference classification: a captured path string is "relative" iff
  it does NOT start with `/`, does NOT start with `~`, and does NOT contain
  the literal token `{PROJECT_ROOT}`.

**`kbu-subproject.toml` schema and source-of-truth boundary.**
- Path: `subprojects/<name>/kbu-subproject.toml`.
- TOML v1.0.0; ISO-8601 UTC datetimes; lowercase boolean literals.
- Schema (existing fields + this PRD's additions):

```toml
[subproject]
name = "fitness_loe"
title = "Fitness LOE analysis"
status = "migrate"                       # plan|migrate|p-review|...|complete
created_at = "2026-06-08T18:30:00Z"
last_session_at = "2026-06-08T18:30:00Z"

[artifacts]
research_plan = false
report = false

[artifacts.reviews]
plan = []
build = []
synthesis = []

[[notebooks]]
slug = "01_load"
purpose = "Load fitness data"
last_run_at = ""                         # empty string when None
modified_since_run = true

session_refs = []
```
- **Independence from `NotebookSession` SQLite catalog**: the TOML manifest
  is the CLI's lifecycle/state record. The SQLite catalog (via
  `NotebookSession.kbu`) is the notebook engine's execution state. They are
  **independent** — the CLI never reads/writes the SQLite catalog; the
  notebook engine never reads/writes the TOML manifest. No auto-sync. If
  they drift, that's a feature: the CLI tracks "is this notebook in the
  plan" and the engine tracks "what did this notebook produce."

**Oversize-flagging mechanism.**
- `write_adoption_notes()` writes an `## Oversize files (>10MB)` section
  listing `<archive-relative path>` and `<size in bytes>` per entry.
- No stdout warning emitted.
- No automatic `.gitignore` updates for oversize files. The agent
  (during `/kbu-migrate`) may propose `.gitignore` additions interactively,
  but adopt CLI does nothing.

**`/kbu-build` adopted-branch — warn-only by default.**
- When `notebooks/` already contains `.ipynb` files at `/kbu-build` invocation:
  - Verify each manifest `[[notebooks]]` entry has a matching file in
    `notebooks/`. For each missing file, log:
    `"Manifest lists missing notebook: <slug>"`
  - For each `.ipynb` in `notebooks/` not in the manifest, log:
    `"Notebook present but not in manifest: <filename>"`
  - **Do NOT auto-create missing notebooks** in this mode.
  - A future `--scaffold-missing` flag (out of scope for this PRD)
    will create missing notebooks. For this PRD, missing-notebook warnings
    are advisory only.

**Path-rewrite policy in `/kbu-migrate`.**
- Rewrites target **project-root-relative** paths, expressed as
  `Path(__file__).resolve().parents[N] / "data" / ...` in notebook code
  (where N counts directories from the notebook to the project root —
  for `subprojects/<name>/notebooks/foo.ipynb`, N=2).
- Default rewrite target: root `data/<original-filename>`. If the user
  selects per-subproject namespacing, root `data/<subproject>/<original-filename>`.
  `data/` is always the prefix unless the user explicitly chooses `models/`
  or `genomes/` for that file.
- Rewriting `pd.read_csv("data/foo.tsv")` → `pd.read_csv(PROJECT_ROOT / "data" / "foo.tsv")`,
  where `PROJECT_ROOT` is a constant the migration agent inserts at the
  top of the notebook (or imports from `util.py`).
- The agent must update `util.py` with a `PROJECT_ROOT` definition if
  it doesn't already exist.

**`util.py` scaffold source.**
- `_scaffold_subproject` MUST render `notebooks/util.py` from the existing
  template at `src/kbutillib/cli/templates/util.py.tmpl` (Jinja-style
  `{{ project_name }}` substitution). Do not write a divergent inline stub.
- The current inline stub in `_scaffold_subproject` lines 178-182 is
  REPLACED with a render of the template.

## Testing Decisions

Tests are unit-level, covering pure logic and CLI command behavior.
Skills/agents (markdown) are not auto-tested.

### `kbutillib.layout` (NEW module — full coverage)

- `read_shared_dirs` returns defaults when `[layout]` absent.
- `read_shared_dirs` returns user list when `[layout.shared_dirs]` set.
- `subproject_subdirs(adopted=False)` returns exactly the virgin subdir
  list (six entries).
- `subproject_subdirs(adopted=True)` includes `archive`.
- `subproject_gitignore_lines` returns the documented set.
- `root_gitignore_lines` includes large-file patterns under shared dirs.

### `kbutillib.cli.adopt._inventory` (NEW — full coverage)

- `scan_archive` on a fixture dir with mixed content returns correct
  notebook list, subdirs+sizes, oversize files (>10MB threshold), and
  per-notebook regex hits.
- Regex patterns capture each documented form (read_csv, open, Path,
  np.load, joblib.load).
- Absolute paths and `~`-prefixed paths are NOT flagged as relative.

### `kbutillib.cli.subproject` (MODIFY — extend existing tests)

- `_check_forward_preconditions` returns None when `migrate` state with
  `RESEARCH_PLAN.md` present, returns "missing-artifact" otherwise.
- `_FORWARD["migrate"] == "p-review"`.
- `_STATES` order unchanged for existing states; new `migrate` inserted
  after `plan`.
- `_scaffold_subproject(adopted=True)` creates `archive/`;
  `_scaffold_subproject(adopted=False)` does not.

### `kbu subproject adopt` (NEW — full coverage)

- Refusal: not in bootstrapped project → exit 1 with clear error.
- Refusal: `<path>` doesn't exist → exit 1.
- Refusal: `subprojects/<name>/` already exists → exit 1.
- Refusal: `<path>` resolves to inside `subprojects/<name>/` → exit 1.
- Refusal: `<path>` tracked by different git repo → exit 1.
- Allow: `<path>` inside same git repo → success.
- Allow: `<path>` not in any git repo → success.
- Warn (proceed): `<path>` has zero `.ipynb` files.
- Success: archive/ contains moved content; canonical subdirs created;
  `kbu-subproject.toml` has `status="migrate"`; `.adoption-notes.md`
  written.
- Success: manifest `notebooks: []` is empty (NOT auto-populated).

### `kbu migrate` (LIGHT integration test)

- On a fixture repo with per-subproject `data/` and a `references.md`,
  the command prompts (with mocked stdin) and produces the expected
  layout. Not exhaustive — the command is largely interactive.

### `claude-skills` sync `type` routing (already present in ClaudeCommands — NO CHANGE)

The existing `claude-skills` tooling already supports `type: agent`
routing — see `claude_skills/inventory.py:297-303` (frontmatter parse)
and `claude_skills/sync.py:683` (agents_target routing). **No
ClaudeCommands code change is required by this PRD.** Existing tests
in `claude-skills/tests/test_sync_agents_lane.py` already cover the
routing.

What KBUtilLib's subagent sources must do to use the existing infra:
- Add `type: agent` to the frontmatter of each subagent .md file.
- Place the file at `templates/research-project/.claude/agents/<name>.md`
  (new subdir created by this PRD).
- Bootstrap then copies it into each new project's `.claude/agents/`.

Routing key is `type` (not `kind` — earlier drafts of this PRD used
`kind`; aligned to the existing field name).

### What is NOT tested

- Skill/agent markdown content (no automated test framework for prompts).
- `/kbu-plan` and `/kbu-migrate` end-to-end conversational flow
  (LLM-driven, manual verification).
- Adoption-notes regex grep on real notebook corpus (manual verification
  via Chris's `ModelingLOE/notebooks/fitness_loe` case after merge).

## Out of Scope

- Auto-migration of existing repos. `kbu migrate` is opt-in only; `kbu
  update` and `kbu doctor` do NOT touch layout.
- Renaming or moving the `agent-io/prds/` and `agent-io/work-records/`
  trees, even though they pre-date the new naming policy.
- Adding new layers to the `claude-skills` sync model beyond `command`
  vs `agent` (e.g. there is no `skill` layer for Skill-tool registration).
- Changes to `/kbu-run`, `/kbu-synthesize`, `/kbu-update`,
  `/kbu-start` beyond path-convention sweeps (those skills can evolve
  in separate PRDs).
- A `kbu archive clean <name>` command for clearing residual archive/
  content. Users manually clean up.
- Migration of any other repo's skills (e.g. `/ai-design`, `/ai-conductor`)
  to the `type: command | agent` model — only KBUtilLib's skills migrate
  in this PRD.
- LFS configuration for oversize files in `archive/` or shared dirs.
  Flagging only; user decides.
- `kbu subproject adopt` accepting individual `.ipynb` files. Directory
  only; user wraps a single file in a temp dir first.

## Further Notes

- The PRD touches **one repo**: **KBUtilLib** (code, templates, skill
  sources, agent sources). The ClaudeCommands sync tool already supports
  `type: agent` routing — no code change there. KBUtilLib's subagent
  sources just need `type: agent` in their frontmatter.
- The state machine change is small but touches multiple call sites
  (`_check_forward_preconditions`, `_NEXT_ACTION`, `_FORWARD`,
  `_REVERSE`). Tests for state machine should be tightened to cover
  the new `migrate` entry before the CLI work.
- The `kbu-bootstrap-v1` PRD (merged 2026-06-07) handles existing-repo
  retrofit for the bootstrap surface. `kbu migrate` in this PRD is a
  separate concept: retrofitting the *layout* of a repo that's already
  been bootstrapped under the v1 layout.
- The `kbu subproject adopt` use case driving this PRD is Chris's
  `ModelingLOE/notebooks/fitness_loe` migration. After merge, validate
  by running the adopt on that path and walking through `/kbu-migrate`.
- Skill sources currently live at
  `KBUtilLib/templates/research-project/.claude/commands/` (the templates
  that get copied into each kbu-project) AND in `~/Dropbox/Projects/
  ClaudeCommands` (some shared sources). The unified `agent-io/skills/`
  location proposed here may be a future cleanup — for this PRD, keep
  KBUtilLib skill sources where they are (in `templates/research-project/
  .claude/commands/`), but introduce `templates/research-project/.claude/
  agents/` for the new subagent destination. The `type:` frontmatter
  decision still applies — both directories' sources include the field,
  and `claude-skills sync` reads it.

## Acceptance Criteria

1. `kbutillib.layout.DEFAULT_SHARED_DIRS == ("data", "models", "genomes")`.
2. `kbutillib.layout.subproject_subdirs(adopted=False)` returns exactly `["notebooks", "figures", "nboutput", ".cache", "literature", "sessions"]` in that order.
3. `kbutillib.layout.subproject_subdirs(adopted=True)` returns the same list with `"archive"` appended (7 entries).
4. `kbutillib.layout.subproject_gitignore_lines()` returns exactly `[".cache/", "nboutput/", ".adoption-notes.md"]` in that order.
5. `kbutillib.layout.root_gitignore_lines(["data","models","genomes"])` returns exactly the 9-entry list `["data/**/*.h5","data/**/*.pkl","data/**/*.parquet","models/**/*.h5","models/**/*.pkl","models/**/*.parquet","genomes/**/*.h5","genomes/**/*.pkl","genomes/**/*.parquet"]` in that order.
6. `kbutillib.layout.read_shared_dirs(<root>)` returns `list(DEFAULT_SHARED_DIRS)` when `kbu-project.toml` is missing, when the `[layout]` table is absent, and when `shared_dirs` key is absent.
7. `kbutillib.layout.read_shared_dirs(<root>)` returns the user list verbatim when `[layout.shared_dirs]` is set.
8. `kbutillib.layout.read_shared_dirs` uses `tomllib` (stdlib) and silently ignores unknown keys in `[layout]`.
9. `kbu subproject` state machine `_STATES` list includes `"migrate"` immediately after `"plan"`; the remaining order is unchanged.
10. `_FORWARD["migrate"] == "p-review"`; no `_REVERSE` entry for `migrate`.
11. `_NEXT_ACTION["migrate"] == "Migrate"`.
12. `_check_forward_preconditions` for `migrate` returns `None` when `RESEARCH_PLAN.md` exists in the subproject dir and `"missing-artifact"` otherwise.
13. `_scaffold_subproject(adopted=False)` creates directories listed by `subproject_subdirs(adopted=False)` and no others; `_scaffold_subproject(adopted=True)` also creates `archive/`.
14. `_scaffold_subproject` renders `notebooks/util.py` from `src/kbutillib/cli/templates/util.py.tmpl` (Jinja `{{ project_name }}` substitution); does not write a divergent inline stub.
15. `_scaffold_subproject` does not create `references.md` (retired).
16. `kbu subproject create` writes `kbu-subproject.toml` with `status="plan"`.
17. `kbu subproject adopt <path> --name <name>` exits 1 with a clear error when cwd is not in a kbu-bootstrapped project.
18. `kbu subproject adopt` exits 1 when `<path>` does not exist or is not a directory.
19. `kbu subproject adopt` exits 1 when `subprojects/<name>/` already exists.
20. `kbu subproject adopt` exits 1 when `<path>` resolves to inside the destination.
21. `kbu subproject adopt` exits 1 when `<path>` is tracked by a different git repo (different `git rev-parse --show-toplevel` than the project root).
22. `kbu subproject adopt` succeeds when `<path>` is inside the same git repo as the project root.
23. `kbu subproject adopt` succeeds (with warning) when `<path>` is not in any git repo.
24. `kbu subproject adopt` succeeds (with warning) when `<path>` contains zero `.ipynb` files.
25. After successful `kbu subproject adopt`, `subprojects/<name>/archive/` contains the moved content with internal structure preserved; the source `<path>` no longer exists.
26. After successful `kbu subproject adopt`, the canonical subproject subdirs (`notebooks/`, `figures/`, `nboutput/`, `.cache/`, `literature/`, `sessions/`) are present and empty (except `notebooks/util.py` rendered from template).
27. After successful `kbu subproject adopt`, `kbu-subproject.toml` has `subproject.status == "migrate"`, empty `notebooks: []`.
28. After successful `kbu subproject adopt`, `.adoption-notes.md` exists in `subprojects/<name>/` and contains sections for notebooks found, subdirs found, oversize files (>10MB), and per-notebook path-reference grep hits.
29. After successful `kbu subproject adopt`, `subproject_gitignore_lines()` entries are appended to root `.gitignore` (idempotent if already present).
30. `kbu subproject adopt` does NOT auto-populate `[[notebooks]]` in the manifest at adopt time.
31. `kbu subproject adopt` does NOT auto-write `.gitignore` entries for oversize files; only the inventory note records them.
32. `scan_archive(archive_dir)` returns paths relative to `archive_dir` in `notebooks` and `subdirs` fields.
33. `scan_archive` does not follow symlinks.
34. `scan_archive` skips directories named `.ipynb_checkpoints` and any other dotted directory inside `archive_dir`.
35. `scan_archive` oversize-files threshold is strictly `> 10_000_000` bytes.
36. `scan_archive` reads notebooks with `nbformat.read(..., as_version=4)`; first markdown cell `source` is captured.
37. `scan_archive` flags a captured path as relative iff it does not start with `/`, `~`, and does not contain `{PROJECT_ROOT}`.
38. `scan_archive` regex set matches `pd.read_*`, `open(`, `Path(`, `np.load`, and `joblib.load` forms documented in the PRD.
39. `kbu migrate` (repo-level) prompts per subproject and per non-canonical item; no operations execute without user confirmation; `--apply` is not required for prompts (interactive by default).
40. `kbu migrate` adds `[layout.shared_dirs] = ["data","models","genomes"]` to `kbu-project.toml` when absent; leaves it unchanged when present.
41. `kbu migrate` creates root shared dirs (`data/`, `models/`, `genomes/`) with `.gitkeep` when missing.
42. `kbu new-project` and `kbu bootstrap` scaffold root `data/`, `models/`, `genomes/` with `.gitkeep`.
43. `kbu new-project` and `kbu bootstrap` write `[layout.shared_dirs] = ["data","models","genomes"]` into the new `kbu-project.toml`.
44. KBUtilLib subagent sources (`kbu-sub-literature-review.md`, `kbu-sub-review.md`, `kbu-sub-diagnose.md`) include `type: agent` in their YAML frontmatter.
45. KBUtilLib subagent sources live at `templates/research-project/.claude/agents/<name>.md` (new subdir).
46. (No claude-skills code change required — existing routing in `claude_skills/inventory.py` and `claude_skills/sync.py` handles `type: agent` already.)
47. `kbu-literature-review.md` source is renamed to `kbu-sub-literature-review.md` with `type: agent` frontmatter; same for `kbu-review.md` → `kbu-sub-review.md` and `kbu-diagnose.md` → `kbu-sub-diagnose.md`. Old slash-command source files are removed.
48. `/kbu-plan` is rewritten to a 4-step grilled flow producing `RESEARCH_PLAN.md`, `literature/index.md` + `literature/<topic-slug>.md`, populated manifest `[[notebooks]]`, and `TASKS.md`.
49. `/kbu-migrate` skill exists as a new slash command source with `type: command`.
50. `/kbu-plan` Step 2 and `/kbu-migrate` invoke `kbu-sub-literature-review` via the Agent tool, not as a slash command.
51. `/kbu-build` adopted-branch (notebooks already present) emits warnings `"Manifest lists missing notebook: <slug>"` and `"Notebook present but not in manifest: <filename>"`; does NOT auto-create notebooks.
52. Path rewrites performed by `/kbu-migrate` are project-root relative using `Path(__file__).resolve().parents[N]` anchoring; default destination is root `data/<filename>` unless user selects per-subproject namespacing.
53. The `kbu-subproject.toml` manifest is independent of the `NotebookSession` SQLite catalog; the CLI does not read/write the SQLite catalog and the notebook engine does not read/write the TOML manifest. No auto-sync exists.
54. The PRD-driven changes in KBUtilLib do not break any existing tests in `tests/` (smoke + unit tests pass after refactor).
