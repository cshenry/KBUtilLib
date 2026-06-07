# kbu-bootstrap v1 — full PRD

## Problem Statement

`kbu new-project` (kbu-start-v1) is greenfield-only: it refuses unless the
target path does not exist, because it owns directory creation + `git init`
+ initial commit. There is no path to apply the kbu workflow — tier-2 skill
files, the subproject state machine, manifest tracking, kbu CLI commands —
to a repo that already exists.

Three real research repos (ModelingLOE, ADP1PhenotypeAnalysis,
MappingEmbeddings) need this immediately. Each has its own git history,
its own gitignore, its own scattered analysis notebooks, possibly an
existing venv (ModelingLOE has a venvman `activate.sh`; others have
nothing), no `.claude/commands/`, and no `kbu-project.toml`.

A retrofit subcommand that is **additive** (never deletes user content,
never auto-commits) and **reuses existing venvs** is needed.

## Solution

A new subcommand `kbu bootstrap` that runs in cwd and:

- Refuses unless cwd is a git repo with no existing `kbu-project.toml`.
- Copies `templates/student-project/.claude/commands/`, `.vscode/`, and
  `subprojects/.gitkeep` into cwd, with a per-file conflict policy that
  never silently overwrites user content.
- Appends a marked block to `.gitignore` (idempotent — re-running does
  nothing if the marker is present).
- Detects an existing venv via a fixed-order probe; refuses to install
  into Python <3.11 unless `--force`; creates a new venv only if no
  existing one is found.
- pip-installs KBUtilLib editable into the (re)used venv, registers a
  Jupyter kernel, writes `kbu-project.toml` (with
  `[project].bootstrapped=true` and `[update.file_hashes]` covering only
  the files bootstrap actually wrote), then exits.
- Does not git commit. Prints a summary and a suggested commit message.

After bootstrap, `kbu update`, `kbu subproject create`, `kbu notebook
list`, the tier-2 `/kbu-start` dashboard, and the eight tier-2 skill
files all work in the repo. The state machine still only governs
`subprojects/<name>/` — existing notebooks at `notebooks/` or elsewhere
remain unmanaged.

## User Stories

1. As Chris using ModelingLOE, I want `kbu bootstrap` to retrofit kbu
   onto the live repo without disturbing the existing notebooks under
   `notebooks/` or the existing venvman `activate.sh`.
2. As Chris running bootstrap in ADP1PhenotypeAnalysis, I want it to
   create a fresh venvman venv (no existing one detected) and install
   KBUtilLib editable, just like new-project would.
3. As Chris running bootstrap in MappingEmbeddings, I want it to detect
   the existing `requirements.txt` is irrelevant to kbu and not touch it.
4. As Chris re-running bootstrap (e.g., after a sync collision left a
   partial deploy), I want every file already-correct to be skipped
   silently and every file differing from the template to surface a
   prompt before overwrite.
5. As Chris running bootstrap with `--check`, I want a list of every
   action that would be taken and zero filesystem writes.
6. As Chris running bootstrap with `--force`, I want all conflict
   prompts skipped (overwrite-with-`.bak`) and the venv compat check
   bypassed.
7. As Chris running bootstrap, I want author info auto-filled from
   `git config user.name` and `user.email` when present, prompted only
   for fields that aren't.
8. As Chris running bootstrap in a repo that already has a
   `.code-workspace` file at root (all three target repos do), I want
   bootstrap to skip generating `{{project_name}}.code-workspace`.
9. As Chris running bootstrap in a repo with a custom
   `.vscode/extensions.json`, I want bootstrap to leave it alone and
   print a one-line note suggesting I merge `anthropic.claude-code`
   into my existing recommendations.
10. As Chris running bootstrap in a repo with an existing `subprojects/`
    directory (created by a prior bootstrap or by hand), I want bootstrap
    to not touch its contents.
11. As Chris bootstrapping a repo that has a Python 3.10 venv,
    I want bootstrap to refuse with a clear message naming the version
    and the override flags (`--force`, `--no-venv`).
12. As Chris running `kbu update` on a bootstrapped repo after bootstrap
    deliberately skipped `.vscode/extensions.json`, I want update to
    NOT propose adding it (the file isn't in `[update.file_hashes]`, so
    bootstrap explicitly opted out).
13. As Chris running `kbu update --add-untracked` on a bootstrapped
    repo, I want update to propose adding template files bootstrap
    skipped, in case I've decided I want them now.
14. As Chris running bootstrap, I want the success message to tell me
    (a) exactly what files were touched, (b) the suggested commit
    message, (c) how to enter the tier-2 workflow.
15. As Chris later deciding to undo bootstrap, I want the success message
    to document the manual undo (`rm kbu-project.toml
    .claude/commands/kbu-*.md` + revert the gitignore block).
16. As a future me running `kbu subproject create foo` in a bootstrapped
    ModelingLOE, I want it to work identically to a new-project repo —
    the bootstrap marker is invisible to subproject lifecycle code.
17. As a future me running `kbu update` on a bootstrapped repo with a
    `.claude/commands/kbu-plan.md` I've locally edited, I want the
    locally-modified detection to warn me before clobbering — same as
    on a new-project repo.
18. As Chris running bootstrap with `--no-venv`, I want kbu to copy
    templates and write the manifest but skip venv creation, pip
    install, and kernel registration entirely (I'll do those by hand).
19. As Chris running bootstrap with `--no-kernel`, I want everything
    except the Jupyter kernel registration.
20. As Chris bootstrapping a repo where `~/.config/kbu/init_done.json`
    doesn't yet exist, I want bootstrap to NOT create it (init marker
    belongs to `kbu init`, not bootstrap — bootstrap is per-repo, init
    is per-machine).

## Implementation Decisions

### Preconditions

`kbu bootstrap` exits non-zero before any filesystem mutation if either:

1. `(Path.cwd() / ".git").exists()` is False. Error: `kbu bootstrap
   must run inside a git repository. cwd=<path>`.
2. `(Path.cwd() / "kbu-project.toml").exists()` is True. Error: `repo is
   already kbu-aware (kbu-project.toml present). Did you mean to run
   kbu update?`.

The macOS gate from kbu-start-v1 applies: on `sys.platform != "darwin"`
without `KBU_PLATFORM_OVERRIDE=force`, print the v1 macOS-only message
(same string as `new-project`) and exit 1.

### CLI surface

```
kbu bootstrap [--name NAME]
              [--first-subproject NAME]
              [--author NAME] [--affiliation AFF] [--orcid ORCID]
              [--no-venv]
              [--no-kernel]
              [--force-overwrite]
              [--force-venv]
              [--check]
```

- `--name`: project name. Default: `Path.cwd().name`. Used as the
  project name in the manifest and as the Jupyter kernel name.
- `--first-subproject`: optional. If given, runs `kbu subproject create
  <name>` after bootstrap completes successfully.
- Author triple (`--author`, `--affiliation`, `--orcid`): if not given
  on the CLI, bootstrap reads `git config user.name`; uses it as the
  default for `--author` if present; prompts via click for any field
  still missing (affiliation and ORCID have no git defaults).
- `--no-venv`: skip venv detection, venv creation, pip install, and
  kernel registration. Manifest is still written; `[kbutillib]
  .source_path` records the source repo. The user is told to install
  manually.
- `--no-kernel`: register no Jupyter kernel; everything else proceeds.
- `--force-overwrite`: skip per-file conflict prompts (still creates
  `.bak.<UTC>` backups before overwriting).
- `--force-venv`: bypass the Python<3.11 venv refusal; proceed with the
  detected venv.
- `--check`: dry-run. Print every action that would be taken (file
  copies, conflict resolutions, venv decision, pip install, kernel
  registration, manifest write). No filesystem writes; no subprocess
  calls beyond read-only probes already needed to plan.

`--check` composes freely with `--force-overwrite` and `--force-venv`
(check shows what each force-* would do).

### Per-file conflict policy

Applied to each template item in order. Every "would write" or
"would skip" decision is reported in `--check`.

| Target | If absent | If present (identical hash) | If present (different hash) |
|---|---|---|---|
| `.claude/commands/kbu-*.md` (each of the 9 files) | copy | skip silently | prompt overwrite (default y); copy original to `.bak.<UTC>`; `--force-overwrite` skips prompt and proceeds |
| `.vscode/extensions.json` | copy | skip silently | **skip + advise**: print one-line note "kept your existing .vscode/extensions.json; ensure `anthropic.claude-code` is in `recommendations`". This file is NEVER overwritten by bootstrap. |
| `subprojects/.gitkeep` | create | skip | skip (existing `subprojects/` is left intact) |
| `{{project_name}}.code-workspace` | **skip** if any `*.code-workspace` exists at root; else copy with `{{project_name}}` substituted | skip | skip |
| `.gitignore` (append-once block) | write the marker block as the file | append marker block | if marker block already present (re-bootstrap), skip; else append |

The gitignore marker block is:
```
# >>> kbu-managed >>>
.venv/
venv/
.ipynb_checkpoints/
nboutput/
.kbcache/
__pycache__/
*.egg-info/
# <<< kbu-managed <<<
```

Detection: grep for the literal `# >>> kbu-managed >>>` marker.
`--check` reports "would append" or "marker present — skip".

`.bak.<UTC>` backup filenames use the ISO-8601 UTC timestamp
(`YYYYMMDDTHHMMSSZ`, no colons — POSIX-filename-safe).

### venv detection and reuse

Probe order (first hit wins; absent means "not detected"):

1. `os.environ.get("VIRTUAL_ENV")` is set and `<VIRTUAL_ENV>/bin/python`
   exists.
2. `<cwd>/activate.sh` exists. Parse `VIRTUAL_ENV=...` via the same
   `_parse_virtual_env_from_activate` helper used by
   `new_project._run_venvman_project`. The resolved `<dir>/bin/python`
   must exist.
3. `<cwd>/.venv/bin/python` exists.
4. `<cwd>/venv/bin/python` exists.

If a venv is detected:

1. Run `<python> -c "import sys; print('%d.%d' % sys.version_info[:2])"`.
2. Parse the major.minor. If `(major, minor) < (3, 11)`:
   - Without `--force`: print "detected venv at `<path>` uses python
     `<x.y>`; kbu requires 3.11+. Pass `--force` to install anyway, or
     `--no-venv` to skip venv work." and exit 1.
   - With `--force`: proceed.
3. Proceed to pip install + kernel registration in this venv.

If no venv is detected:

- Use the same venvman > `.venv` fallback as new-project's `_template_ops`
  (extracted from `new_project.py`). `venvman create --project <name>
  --dir <cwd> --python 3.11`. If venvman fails or is absent, fall back
  to `python -m venv .venv` in cwd.
- After creation: pip install + kernel registration.

Always:

- `<venv_python> -m pip install -e <KBUTILLIB_ROOT>`. KBUTILLIB_ROOT is
  derived from the bootstrap module's own filesystem location (same
  `_kbutillib_root()` helper as new-project).
- `<venv_python> -m ipykernel install --user --name=<name>
  --display-name='<name> (kbu)'` (skipped if `--no-kernel`).
  - jupyter's `install` semantically replaces an existing kernel by
    name. No prompt. Print a one-line note "registered jupyter kernel
    `<name>` (or replaced existing)."

### Manifest

`kbu-project.toml` is written with:

```toml
[project]
name = "<name>"
title = ""                                # empty unless user provides
created_at = "<UTC ISO Z>"
bootstrapped = true
bootstrapped_at = "<UTC ISO Z>"

[[project.authors]]
name = "<author>"
affiliation = "<affiliation>"
orcid = "<orcid>"

[kbutillib]
source_path = "<KBUTILLIB_ROOT>"
source_commit = "<HEAD sha at bootstrap time>"

[update]
last_pulled_at = "<UTC ISO Z>"
last_pulled_commit = "<source_commit>"

[update.file_hashes]
# Only files bootstrap actually wrote. Files in .vscode/extensions.json
# that bootstrap deliberately skipped are NOT recorded here.
".claude/commands/kbu-start.md" = "sha256:..."
".claude/commands/kbu-plan.md" = "sha256:..."
# ... etc
```

`created_at` and `bootstrapped_at` are the same timestamp. The dual
field keeps semantic clarity (created_at = manifest creation;
bootstrapped_at = retrofit operation).

The set of paths recorded in `[update.file_hashes]` is the in-memory
"actually wrote" set built by the per-file conflict loop. Files that
were skipped because they were identical to the template are NOT
recorded (bootstrap is being honest about what it took ownership of —
identical files weren't authored by bootstrap and shouldn't be tracked
as such; a later `kbu update --add-untracked` can opt them in).

Exception: any file that bootstrap overwrote (different hash, user
accepted prompt) IS recorded. The `.bak.<UTC>` backup is left at rest
and the new hash is the recorded one.

### update.py modification

`_build_diff` in [update.py:111-219](src/kbutillib/cli/update.py#L111-L219)
currently treats every file present in the source template as "added"
when no recorded hash exists. On a bootstrapped repo, this proposes to
add files bootstrap deliberately skipped.

Behavior change: split the "added" candidate set against
`[update.file_hashes]`:

- If `[update.file_hashes]` is empty (legacy new-project manifests with
  no recorded hashes — possible during the kbu-start-v1 rollout):
  preserve old behavior (all templates treated as added). Detect via
  the manifest field's absence or empty dict.
- If `[update.file_hashes]` is non-empty: by default, only propose
  "modified" diffs for paths already in the recorded set. Files in the
  source template that are absent from the recorded set are NOT
  proposed as additions unless `--add-untracked` is passed.

`--add-untracked` is a new flag on `kbu update`. Default off. When on,
revert to the old behavior (all source files are candidate additions).

`--check` and `--add-untracked` compose freely. `--yes` and
`--add-untracked` compose freely. The mutual exclusion is only
`--check` vs `--yes`, unchanged.

After a `kbu update` that adds previously-untracked files, those files
are added to `[update.file_hashes]` going forward — the user has now
opted them in.

### `kbu doctor` extension

`kbu doctor` (already in kbu-start-v1) should report whether the
current repo is bootstrapped vs new-project (read `[project]
.bootstrapped` from the manifest). This is informational only — no
behavior change. One line in the output:

```
project origin: bootstrap (2026-06-06T15:30:00Z)
```

or:

```
project origin: new-project (2026-06-05T12:00:00Z)
```

### Modules

| Module | Responsibility |
|---|---|
| `kbutillib.cli.bootstrap` (new) | Orchestration: precondition checks, per-file conflict loop, venv probe + compat check, pip + kernel, manifest write, success summary. Pure orchestration — delegates to `_template_ops` and `manifest`. |
| `kbutillib.cli._template_ops` (new) | Three internal helpers extracted from `new_project.py`: `copy_template_tree`, `compute_file_hashes`, and the venv helpers (`run_venvman_project`, `create_plain_venv`, `parse_virtual_env_from_activate`). Module-private to `kbutillib.cli`. |
| `kbutillib.cli.new_project` (modified) | Import the three helpers from `_template_ops` instead of defining them inline. No external behavior change. |
| `kbutillib.cli.update` (modified) | New `--add-untracked` flag. `_build_diff` learns to filter "added" candidates against `[update.file_hashes]` unless the flag is set or the hash dict is empty. |
| `kbutillib.cli.doctor` (modified) | One-line "project origin" report reading `[project].bootstrapped`. |

CLI registration: `kbu bootstrap` registered in
`src/kbutillib/cli/__init__.py` (or wherever the existing CLI group is
defined) the same way `new-project` and `update` are registered.

### Success summary (printed at the end)

```
kbu bootstrap complete in <cwd>

Files written:
  .claude/commands/kbu-start.md
  .claude/commands/kbu-plan.md
  ... (one line per file that was added/modified)

Files left alone (already differed; backup created):
  .claude/commands/kbu-build.md  (backup: .bak.20260606T153000Z)

Files left alone (user-owned):
  .vscode/extensions.json  (ensure anthropic.claude-code is recommended)

Manifest written: kbu-project.toml ([project].bootstrapped=true)
Jupyter kernel registered: <name>

Review with `git status`, then commit:
  git add -A && git commit -m 'chore(kbu): bootstrap kbu-awareness'

Enter the workflow:
  open Claude Code → /kbu-start

To undo bootstrap:
  rm kbu-project.toml .claude/commands/kbu-*.md
  edit .gitignore to remove the `# >>> kbu-managed >>>` block
  (manual; v1 has no `kbu unbootstrap` command)
```

### Edge cases (locked)

- Re-running `kbu bootstrap` after an aborted prior run: the precondition
  refuses (manifest present). User must `rm kbu-project.toml` to retry.
  This is intentional — auto-resuming a half-done bootstrap is too
  fragile.
- `--check` does NOT make subprocess calls for the venv compat check
  beyond reading the python binary and asking for its version (that
  probe is needed to plan accurately). It does NOT run `pip install`
  or `ipykernel install`.
- If the cwd git repo is on a detached HEAD or has no commits, bootstrap
  still proceeds — bootstrap doesn't commit, so the head state is
  irrelevant.
- If `.git` is a file (worktree pointer) rather than a directory, treat
  it as "is a git repo" — the precondition is `(cwd/.git).exists()`,
  not `is_dir()`.
- If `subprojects/` already exists (e.g., from a hand-created layout),
  bootstrap leaves it alone — no `.gitkeep` is written if the directory
  has any contents.

### Folded from confront round 1 (2026-06-07, task-ce409fbd)

#### Exact macOS-only message (stall 1)

On `sys.platform != "darwin"` without `KBU_PLATFORM_OVERRIDE=force`, bootstrap
prints this exact message and exits 1:

```
v1 currently targets macOS. Linux/Windows support is planned for v2.
To use kbu manually on this platform: pip install -e <path-to-KBUtilLib>
into your existing venv, then run `kbu bootstrap --no-venv` to copy
templates and write kbu-project.toml. Tier-2 skills work cross-platform
once the template files are in place.
```

Parallel-structured to but distinct from `new-project`'s message; bootstrap
has a meaningful `--no-venv` escape that `new-project` does not.

#### Template source enumeration (stalls 2, 12)

The canonical template root is `<KBUTILLIB_ROOT>/templates/student-project/`
(resolved relative to the bootstrap module's filesystem location). Bootstrap
is responsible for exactly this closed set of template entries:

- `.claude/commands/kbu-start.md`
- `.claude/commands/kbu-plan.md`
- `.claude/commands/kbu-build.md`
- `.claude/commands/kbu-run.md`
- `.claude/commands/kbu-synthesize.md`
- `.claude/commands/kbu-review.md`
- `.claude/commands/kbu-literature-review.md`
- `.claude/commands/kbu-diagnose.md`
- `.claude/commands/kbu-update.md`
- `.vscode/extensions.json`
- `subprojects/.gitkeep`
- `{{project_name}}.code-workspace`
- `.gitignore` (treated as marker-block source — not copied wholesale)

`kbu-project.toml.template` is read by bootstrap as the schema source for
`kbu-project.toml`; it is never copied verbatim into the destination.

Bootstrap never reads or writes any path outside this enumerated set. Template
files added in later KBUtilLib versions are NOT auto-tracked by bootstrap; the
list above is the closed set for v1. Adding a new tracked file is a PRD change.

The success summary's "Files written" list draws from exactly these paths.

#### .gitignore append semantics (stall 3)

If `.gitignore` is absent: create it as UTF-8 with the marker block followed
by a trailing newline.

If `.gitignore` is present: open as UTF-8 (`errors="replace"`); search literally
for `# >>> kbu-managed >>>` (anywhere in the file). If found, skip — no write.
Otherwise, append. Blank-line spacing rule before the appended marker:

- File ends with `\n\n` (or more) → append marker block directly.
- File ends with single `\n` → prepend one extra `\n` then the marker block.
- File does not end with `\n` → prepend `\n\n` then the marker block.

Always end the written file with a trailing newline.

#### venv creation fallback — exact commands (stall 5)

When no venv is detected (or detected venv fails compat unless `--force-venv`):

1. Detect venvman availability via `shutil.which("venvman")`. On `sys.platform
   != "darwin"` under `KBU_PLATFORM_OVERRIDE=force`, venvman is treated as
   absent regardless of `shutil.which` result.
2. If venvman is available, run:
   ```
   venvman create --project <name> --dir <cwd> --python 3.11
   ```
   On exit 0, parse `<cwd>/activate.sh` via the existing
   `_parse_virtual_env_from_activate` helper in `new_project.py` and resolve
   `<dir>/bin/python`. If parse fails, emit a stderr warning naming the issue
   and fall through to step 3.
3. If venvman is unavailable or step 2 failed, run:
   ```
   <sys.executable> -m venv .venv
   ```
   in cwd. Use `<cwd>/.venv/bin/python` as the resolved interpreter. Hard-fail
   on non-zero exit (no further fallback).

#### KBUTILLIB_ROOT + source_commit resolution (stall 6)

`KBUTILLIB_ROOT` resolves as:
```python
Path(__file__).resolve().parents[3]
```
from `src/kbutillib/cli/bootstrap.py` (cli → kbutillib → src → repo_root).

`source_commit` is the stripped stdout of `git -C <KBUTILLIB_ROOT> rev-parse
HEAD`. If KBUTILLIB_ROOT is not a git repo, `source_commit` is the empty
string (matches `new_project._git_commit` behavior).

#### --check never prompts (stall 7)

Under `--check`, bootstrap MUST NOT call `click.prompt()` for any field.
Missing author fields render in the dry-run plan as:

```
Author: Chris Henry (from git config user.name)
Affiliation: (TODO — would prompt: 'Author affiliation')
ORCID: (TODO — would prompt: 'Author ORCID')
```

The dry-run plan exits 0 even with TODOs present. The actual non-check run
reports the same TODOs and then prompts. `--check --force-overwrite` still
elides no field prompts (force-overwrite only governs file-conflict prompts).

#### SHA-256 byte normalization (stall 8)

All file hashes hash the raw bytes of the file as stored on disk: read mode
`"rb"`, no newline normalization, no encoding conversion, no BOM stripping.
The recorded value is `sha256:<lowercase-hex>`. This is what the existing
`sha256_file` helper in
[manifest.py:32-41](src/kbutillib/cli/manifest.py#L32-L41) already does;
bootstrap uses it directly (no parallel implementation).

#### CLI registration (stall 9)

The command is registered in `src/kbutillib/cli/__init__.py` under the name
`bootstrap`:
```python
main.add_command(bootstrap_command, name="bootstrap")
```
`bootstrap_command` is the click command exported from
`kbutillib.cli.bootstrap`.

#### Split --force into --force-overwrite and --force-venv (cherry-picked from free critique A)

The original PRD's single `--force` flag is replaced by two narrower flags:

- `--force-overwrite`: skip per-file conflict prompts (still creates
  `.bak.<UTC>` backups before overwriting).
- `--force-venv`: bypass the Python<3.11 venv refusal.

Both default off. They compose freely with each other and with `--check`.
No single `--force` exists — users explicitly opt into each override. This
prevents the failure mode of intending to overwrite a stale skill file and
accidentally installing into an old Python at the same time.

The CLI surface section above already reflects this split; user stories 4,
5, 6, 11 still hold (the original `--force` references are interpreted as
"the appropriate `--force-*` flag for the resolution being applied").

## Testing Decisions

### What good tests look like here

External behavior. Mock subprocess + filesystem, not internal helpers.
Each test sets up a fixture cwd with a known prior state (git repo +
optional venv + optional pre-existing .claude/commands files) and
asserts the resulting filesystem state + manifest.

### Test targets

1. **Precondition refusal**: non-git-repo cwd → exit 1, no writes; cwd
   with existing `kbu-project.toml` → exit 1, no writes.
2. **Per-file conflict matrix**: table-driven test with rows for each
   target file × (absent / identical / different). Asserts the expected
   filesystem outcome and the `[update.file_hashes]` membership.
3. **Gitignore append-once**: first run appends marker block; second
   run with marker present does not duplicate.
4. **`.code-workspace` skip**: fixture with `Foo.code-workspace` at
   root; bootstrap does not create `{{project_name}}.code-workspace`.
5. **venv detection order**: four fixtures (env var set; activate.sh;
   .venv/; venv/), each with the others absent. Detect-and-use the
   right one. Plus a fifth fixture with none — falls into create-new.
6. **venv compat refusal**: fixture .venv/ with python 3.10. Without
   `--force-venv` exit 1; with `--force-venv` proceed.
7. **`--no-venv` skips venv work**: manifest written, templates copied,
   no pip / kernel calls made (subprocess mock asserts).
8. **`--check` writes nothing**: full action plan printed; filesystem
   unchanged before/after; no subprocess writes (pip, ipykernel).
9. **Manifest file_hashes membership**: only paths bootstrap wrote
   appear in `[update.file_hashes]`; skipped `.vscode/extensions.json`
   does NOT appear.
10. **Author auto-fill**: fixture with `git config user.name` set;
    bootstrap does not prompt for author; manifest contains the
    config value.
11. **update.py bootstrap-aware "added" filter**: bootstrapped manifest
    with non-empty file_hashes excluding `.vscode/extensions.json`;
    template source has the file; `kbu update` does NOT propose to add
    it; `kbu update --add-untracked` does.
12. **update.py legacy empty-hashes behavior**: empty `[update.file_hashes]`
    preserves old "add everything in source template" behavior
    (kbu-start-v1 compatibility).
13. **doctor reports origin**: bootstrapped manifest → "project origin:
    bootstrap"; new-project manifest → "project origin: new-project".

### Tests deliberately not written

- No tests on Cursor / Claude Code IDE integration (out of our control).
- No tests on actual venvman invocation success — we mock subprocess
  and assert the right command shape; venvman is upstream.
- No tests on jupyter kernel runtime — `ipykernel install` is upstream.
- No tests verifying that bootstrapped + `subproject create` works
  end-to-end at the CLI level beyond a single smoke assertion (covered
  by kbu-start-v1 tests for `subproject create`).

### Prior art

- `KBUtilLib/tests/` existing CLI fixture conventions (used by
  kbu-start-v1 tests for new-project, update, subproject, session,
  notebook).
- `AIAssistant/assistant/state/` test patterns for manifest TOML I/O.

## Out of Scope

- Adopting legacy notebooks into `subprojects/legacy/`. The state
  machine governs `subprojects/<name>/` only; existing notebooks at
  `notebooks/` or elsewhere remain unmanaged.
- A `kbu unbootstrap` command. Manual removal is small and documented
  in the bootstrap success message.
- Tier-1 `/kbu-start` menu changes to surface bootstrap. Deferred to
  a follow-up once bootstrap shape is proven.
- Cross-machine bootstrap (Maestro lane).
- Multi-repo workspace bootstrap.
- Linux / Windows support. Inherits v1 macOS-only gate from kbu-start-v1.
- Migrating an existing repo's venv across Python versions. If the user
  has Python 3.10, they either upgrade their venv (out of our scope) or
  pass `--force-venv` (accept the risk) or pass `--no-venv` (manage their
  own install).
- Auto-merging `anthropic.claude-code` into an existing `.vscode
  /extensions.json`. Bootstrap leaves it alone and advises.
- Modifying a repo's existing `.code-workspace` file to add the kbu
  venv. The user does this themselves if they want.
- An init-marker write. The init marker at `~/.config/kbu/init_done.json`
  is per-machine, owned by `kbu init`. Bootstrap is per-repo.
- Backporting `[update.file_hashes]` filtering behavior to
  pre-kbu-bootstrap-v1 `kbu update` callers. The empty-hashes branch
  preserves old behavior; if someone is on an old new-project repo
  with a deliberately-empty file_hashes dict, they keep the old
  "add everything" semantics. Migrate-by-rerunning-bootstrap is not
  supported (precondition refuses).

## Further Notes

- The cross-cutting cost of this PRD is the surgical change to
  `update.py`. That change keeps backwards compatibility for the
  recently-shipped kbu-start-v1 new-project flow (file_hashes empty →
  old behavior) and is the cleanest way to honor bootstrap's
  "additive-only" promise across the lifecycle.
- The `[project].bootstrapped` field is intentionally a manifest-level
  marker, not just a state-machine concern. It's there so future
  tooling (doctor, audit, conductor) can distinguish bootstrap-origin
  manifests from new-project-origin manifests without inferring from
  side channels.
- The `.bak.<UTC>` backup pattern uses POSIX-safe filenames (no colons,
  Z suffix) so they're git-add-able and don't collide on case-folding
  filesystems.
- Future `kbu adopt <dir> --as <subproject>` is the right shape for
  legacy notebook adoption. It's a separate command because the rules
  for "adopting" (do we move? copy? leave in place?) and "managing"
  (state machine, notebooks list, run dashboard) are independent
  decisions deserving their own design.

## Acceptance Criteria

1. `kbu bootstrap` is exported as `bootstrap_command` from `kbutillib.cli.bootstrap` and registered in `src/kbutillib/cli/__init__.py` via `main.add_command(bootstrap_command, name="bootstrap")`.
2. `kbu bootstrap` exits 1 with an error message containing "must run inside a git repository" if `(Path.cwd() / ".git").exists()` is False; zero filesystem writes occur.
3. `kbu bootstrap` exits 1 with an error message naming `kbu-project.toml` if that file already exists at cwd; zero filesystem writes occur.
4. On `sys.platform != "darwin"` without `KBU_PLATFORM_OVERRIDE=force`, `kbu bootstrap` prints the exact macOS-only message (per Implementation Decisions › Folded round 1 › Exact macOS-only message) and exits 1.
5. The CLI accepts exactly these flags: `--name`, `--first-subproject`, `--author`, `--affiliation`, `--orcid`, `--no-venv`, `--no-kernel`, `--force-overwrite`, `--force-venv`, `--check`. No single `--force` flag is exposed.
6. `--name` defaults to `Path.cwd().name` when not provided.
7. Author triple: `--author` defaults from `git config user.name` when missing; `--affiliation` and `--orcid` always prompt interactively when missing (no git defaults); all three are persisted to `[[project.authors]]` in the written manifest.
8. Under `--check`, no `click.prompt()` call is invoked; missing author fields render as `(TODO — would prompt: '<field-label>')` in the plan output; no filesystem writes; no subprocess calls other than read-only probes (`git rev-parse`, `python --version`, `shutil.which`).
9. The template payload bootstrap handles is exactly the 13 entries enumerated in Implementation Decisions › Template source enumeration; bootstrap never reads or writes paths outside this set.
10. For each of the 9 `.claude/commands/kbu-*.md` files: absent → copy; present-and-hash-matches → silent skip; present-and-hash-differs → prompt overwrite (default y) with `.bak.<UTC>` backup, OR with `--force-overwrite` skip the prompt and proceed (still creating the backup).
11. `.vscode/extensions.json`: absent → copy; present (any content) → never overwritten by bootstrap, prints the one-line advice message, NOT recorded in `[update.file_hashes]`.
12. `subprojects/.gitkeep`: if `subprojects/` already exists with any content → skip; else create the directory and write `.gitkeep`.
13. `{{project_name}}.code-workspace`: if any `*.code-workspace` exists at repo root → skip generation; else copy the template with `{{project_name}}` substituted by `--name`.
14. `.gitignore` handling: absent → create UTF-8 with marker block and trailing newline; present-with-marker → skip (idempotent); present-without-marker → append per the blank-line spacing rule in Implementation Decisions › .gitignore append semantics.
15. The gitignore marker block contents are exactly the seven-entry block specified in the Per-file conflict policy section (`.venv/`, `venv/`, `.ipynb_checkpoints/`, `nboutput/`, `.kbcache/`, `__pycache__/`, `*.egg-info/`) bracketed by `# >>> kbu-managed >>>` and `# <<< kbu-managed <<<`.
16. `.bak.<UTC>` backup filenames use the format `<orig-name>.bak.YYYYMMDDTHHMMSSZ` (no colons, POSIX-safe).
17. venv detection probe order is: `$VIRTUAL_ENV` → `<cwd>/activate.sh` (parsed via the existing `_parse_virtual_env_from_activate` helper in `new_project.py`) → `<cwd>/.venv/bin/python` → `<cwd>/venv/bin/python`; first hit wins.
18. If a detected venv reports Python `<3.11` (major.minor parsed from `<python> -c "import sys; print('%d.%d' % sys.version_info[:2])"`): without `--force-venv` exit 1 with an actionable message naming the version and the override flags; with `--force-venv` proceed.
19. If no venv is detected and venvman is available (per `shutil.which("venvman")` and the macOS-or-override gate), run `venvman create --project <name> --dir <cwd> --python 3.11` and resolve the python via `<cwd>/activate.sh`; on failure fall through to `<sys.executable> -m venv .venv`. If venvman is unavailable, go directly to the python -m venv path. Hard-fail on `python -m venv` failure (no further fallback).
20. With `--no-venv`: skip venv detection, venv creation, pip install, and kernel registration; still copy templates and write the manifest.
21. With `--no-kernel`: skip only the kernel registration; everything else proceeds (including pip install).
22. pip install runs as `<venv_python> -m pip install -e <KBUTILLIB_ROOT>`. `KBUTILLIB_ROOT` resolves as `Path(__file__).resolve().parents[3]` from `src/kbutillib/cli/bootstrap.py`.
23. Kernel registration runs as `<venv_python> -m ipykernel install --user --name=<name> --display-name='<name> (kbu)'` and prints "registered jupyter kernel `<name>` (or replaced existing)".
24. The written `kbu-project.toml` contains `[project]` with `name`, `created_at`, `bootstrapped=true`, `bootstrapped_at` (same UTC ISO-Z timestamp as `created_at`); `[[project.authors]]` with `name`/`affiliation`/`orcid`; `[kbutillib]` with `source_path`/`source_commit`; `[update]` with `last_pulled_at`/`last_pulled_commit`; `[update.file_hashes]` with one entry per file bootstrap actually wrote.
25. `source_commit` is the stripped stdout of `git -C <KBUTILLIB_ROOT> rev-parse HEAD`, or empty string if KBUTILLIB_ROOT is not a git repo.
26. `[update.file_hashes]` membership: includes every path bootstrap WROTE (added or overwrote on prompt or `--force-overwrite`); EXCLUDES `.vscode/extensions.json` when bootstrap skipped it; EXCLUDES `subprojects/.gitkeep` when bootstrap skipped it; EXCLUDES `{{project_name}}.code-workspace` when bootstrap skipped it; EXCLUDES `.gitignore` (user-owned, even after marker append).
27. All file hashes are computed via the existing `kbutillib.cli.manifest.sha256_file` helper (raw bytes, no normalization, lowercase hex), recorded as `sha256:<hex>`.
28. All timestamp fields use ISO-8601 UTC with `Z` suffix via the existing `kbutillib.cli.manifest.now_utc_iso` helper.
29. Bootstrap does NOT run `git add`, `git commit`, or any other write-side git command; the working tree is left dirty for the user to review.
30. Bootstrap does NOT create or modify the per-machine init marker at `~/.config/kbu/init_done.json` — that file is owned exclusively by `kbu init`.
31. The success summary prints (in order): a "Files written" list (one line per `[update.file_hashes]` entry); a "Files left alone (already differed; backup created)" list with `.bak.<UTC>` references; a "Files left alone (user-owned)" list naming `.vscode/extensions.json` if it was skipped; a manifest-written line; a Jupyter-kernel line; a "Review with git status, then commit" hint with the suggested message `chore(kbu): bootstrap kbu-awareness`; the Cursor + `/kbu-start` entry instructions; the manual-undo documentation.
32. `kbu update` gains a `--add-untracked` flag (default off). When `[update.file_hashes]` is non-empty AND `--add-untracked` is off, `_build_diff` MUST NOT emit `status="added"` entries for source-template paths absent from `[update.file_hashes]`. When `[update.file_hashes]` is empty OR `--add-untracked` is on, the legacy "all source files are candidate additions" behavior is preserved.
33. `kbu update`: `--check` and `--yes` remain mutually exclusive; `--add-untracked` composes freely with both. After a successful `--add-untracked` run, the newly-added files are recorded in `[update.file_hashes]` going forward.
34. `kbu doctor` prints one extra line — `project origin: bootstrap (<bootstrapped_at>)` if `[project].bootstrapped` is true; `project origin: new-project (<created_at>)` otherwise. No behavior change beyond this line.
35. New module `kbutillib.cli.bootstrap` exposes `bootstrap_command` (the click command) and a `bootstrap()` orchestration function callable from tests.
36. New module `kbutillib.cli._template_ops` exposes `copy_template_tree`, `compute_file_hashes`, `run_venvman_project`, `create_plain_venv`, and `parse_virtual_env_from_activate`. `kbutillib.cli.new_project` is refactored to import these from `_template_ops` instead of defining them inline, with no external behavior change.
37. With `--first-subproject NAME` (and bootstrap otherwise succeeds), bootstrap invokes `kbu subproject create <NAME>` as a final step (post-manifest, post-kernel). Failure of that step is reported on stderr but does not roll back the bootstrap.
38. Under `--check --first-subproject NAME`, the plan output reports "would create first subproject `<NAME>` after bootstrap" but does NOT invoke `subproject create`.

