# PRD: kbu-harness

> Build a per-project, local, programmatic-execution **harness** that pairs with a
> KBase BERIL project: rsync a modeling project out of a local BERIL clone, run its
> notebooks programmatically (which BERIL forbids), keep a markdown dev-log, and
> push executed results back so BERIL can commit notebooks-with-outputs. Companion
> **PRD B** to `kbu-beril-augmentation` (PRD A); ships after A.

## Problem Statement

PRD A (`kbu-beril-augmentation`, shipped 2026-06-14) made BERIL build the lab's
metabolic-modeling notebooks with the canonical `%run util.py` + provenanced-cache
discipline, and baked in a **graduated execution policy**: BERIL runs cheap/certain
cells freely, but for anything slow, algorithmically uncertain, large fan-out, or
compute-heavy it runs only a **sample**, caches it, and **stops to consult**. PRD A
deliberately left open *where the full expensive run happens* — "typically in the
HARNESS" — and scoped the harness itself to PRD B.

There is currently nowhere automated for that full run to go:

- **BERIL is cloud JupyterHub (BERDL/Spark), on ephemeral Kubernetes pods**, and
  **explicitly forbids programmatic notebook execution** — "No programmatic notebook
  execution (must use web UI)" (BERIL `PROJECT.md`). Execution is manual: JupyterHub
  "Restart & Run All" or a human-run `nbconvert`. File transfer off the hub is manual
  and capped (~1GB).
- The lab's kbu skills target **local COBRA/MSModelUtil modeling notebooks**, which
  need a **local LP solver**, not Spark. They don't belong on the Spark hub at all.
- So a validated sample in BERIL has no automated, reproducible path to a full run.
  The old kbu co-scientist tried to own this with a `plan/build/run` state machine and
  a `kbu notebook exec` headless executor; that machinery is being retired (PRD A +
  the 2026-06-13 audit) precisely because it over-reached.

CRAFT — the reference for PRD A's deployer — offers **no harness pattern**: it is
platform-deployment-scoped (install three skills into a BERIL root), with no
per-project container, no rsync, no project scaffolding. The per-project harness is
genuinely new; only CRAFT's `install/doctor` CLI-surface convention carries over.

## Solution

A **per-project harness**: its own git repo, one per BERIL project, living on the
Dropbox/Projects surface so it is automatically synced to h100. You `cd` into the
harness and its own `kbu-run` skill runs the loop; **BERIL never knows the harness
exists** and stays at exactly its three PRD-A skills.

KBUtilLib gains:

1. A **`kbu harness` CLI command group** (parallel to PRD A's `kbu beril`) over a
   self-contained **`kbutillib/harness/` library**:
   - **`init <BERIL_ROOT> <project-id> [--harness-root PATH]`** — scaffold the full
     container (git repo, venv, BERIL-mirror dirs, `.claude/skills/` skill bundle,
     empty `DEVLOG.md`, `harness.toml` source record) and do an initial pull.
   - **`pull` / `push`** — rsync `<BERIL_ROOT>/projects/{id}/` ⇄ the harness, whole
     tree, both directions, `.kbcache/` included, excluding `.git/.venv/__pycache__`.
   - **`run [notebooks…] [--on local|h100]`** — `local`: execute each notebook via
     `jupyter nbconvert --to notebook --execute --inplace`, capture exit code +
     traceback, verify expected outputs exist; return a structured `RunResult`.
     `h100`: write an ai-cowork task pointed at the Dropbox-synced harness path.
   - **`doctor`** — report venv present, `import kbutillib` works, `harness.toml`
     valid, `<BERIL_ROOT>` reachable, `nbconvert` available. Pure read.

2. A single **`kbu-run` skill** deployed into the harness's `.claude/skills/` by
   `init` (auto-discoverable + user-invocable). It drives the whole loop and carries
   the **judgment** the CLI deliberately lacks: pull (the "design-deploy" step) →
   classify the run via the graduated-execution policy (reading the pulled
   `preferences.md` thresholds) → choose local vs h100 → call `kbu harness run` →
   verify outputs → append a `DEVLOG.md` entry → **on success: stop, report, and on
   user OK push back to BERIL**; **on failure: stop and report the traceback,
   editing no code.**

The division of labor: the **CLI/library** does everything mechanical (scaffold,
rsync, nbconvert, dev-log I/O); the **`kbu-run` skill** does everything that needs
judgment (classification, local-vs-h100 choice, verify, confirm-before-push,
escalate-on-failure). This mirrors how `ai-conductor` wraps Maestro.

## User Stories

1. As a researcher who has built+sampled a modeling project in BERIL, I want to run
   `kbu harness init <BERIL_ROOT> <project-id>` once and get a ready-to-run sibling
   repo with my project pulled into it, so I can execute the full run off BERIL.
2. As a researcher, I want the harness to be its own git repo under a harness root on
   the Dropbox/Projects surface, so it is version-controlled separately from BERIL and
   automatically reaches h100.
3. As a researcher, I want `kbu harness pull` to rsync the latest project state out of
   my local BERIL clone, so the harness always runs the notebooks I just designed.
4. As a researcher, I want `.kbcache/` to sync both ways, so the sample work BERIL
   already cached carries into the full run (cache-as-you-go resumes) and the full
   run's cache comes back.
5. As a researcher, I want `kbu harness run` to execute notebooks **programmatically**
   (`nbconvert --execute --inplace`) — exactly what BERIL forbids — so the full run is
   automated and reproducible, not a manual web-UI click-through.
6. As a researcher, I want every executed notebook to keep its saved outputs in place,
   so when I push back to BERIL I satisfy BERIL's hard "commit notebooks **with
   outputs**" rule.
7. As a researcher, I want `kbu-run` to classify the run with the same graduated
   policy as PRD A (reading `preferences.md` thresholds) and let me choose local vs
   h100 each time, so I keep control of where compute happens.
8. As a researcher with a heavy run, I want `kbu harness run --on h100` to dispatch an
   ai-cowork task pointed at the Dropbox-synced harness path, so h100 executes in the
   same directory and results return via Dropbox with no new transport.
9. As a researcher, I want `kbu-run` to **stop and report the traceback** on the first
   execution failure and never edit my code, so a bad run can't silently mutate my
   science.
10. As a researcher, I want `kbu-run` to stop and confirm before pushing results back
    to BERIL, so I review the run before it touches my BERIL tree, and then be reminded
    to commit in BERIL.
11. As a researcher, I want a `DEVLOG.md` at the harness root that gets an append-only
    entry per pull/run/push (notebooks, scope, where it ran, outcome, runtime,
    blocker/traceback-ref), so the harness keeps a durable narrative of what happened.
12. As a researcher, I want `kbu harness doctor` to tell me whether the venv,
    `kbutillib` import, source record, and BERIL path are all healthy, so I can
    diagnose a broken harness fast.
13. As another KBase user, I want `pip install kbutillib` to give me `kbu harness` so I
    can run the local-execution loop against my own BERIL clone (local execution is the
    portable default; the h100 dispatch is lab-specific and degrades gracefully).
14. As a maintainer, I want the harness library to reuse the venv/template helpers from
    the `kbu-bootstrap` work rather than reinventing env setup, so there is one way to
    build a project venv.

## Implementation Decisions

### Repo, location & distribution
- **Harness = its own git repo, one per BERIL project**, created under a harness root
  on the Dropbox/Projects surface. Default harness root `~/Dropbox/Projects/kbu-harness/`,
  harness dir `<harness-root>/<project-id>/`, overridable via `--harness-root`. On the
  Dropbox surface so it is automatically synced to h100 (enables the ai-cowork dispatch
  path with no new transport).
- **All-harness-side.** No new BERIL skill; the BERIL bundle stays at the three PRD-A
  skills. The harness carries its own `kbu-run` skill, deployed by `init`. BERIL has no
  knowledge of the harness.
- Shipped as part of the existing pip/pipx-installable `kbutillib`; `kbu harness` is a
  new subcommand group on the existing `kbu` CLI (parallel to PRD A's `kbu beril`).
- Local execution is the **portable default** for any KBase user; the **h100 ai-cowork
  dispatch is Chris-lab-specific** (it writes to `~/Dropbox/Projects/AIAssistant/cowork-inbox/h100/`).
  `--on h100` must fail with a clear message when that inbox path is absent, never crash.

### Module 1 — `kbu harness` CLI + `kbutillib/harness/` library (deep module)
- New package `kbutillib/harness/` with internal files (implementation detail, not a
  public multi-module surface): `scaffold.py`, `sync.py`, `runner.py`, `devlog.py`,
  `config.py`. A thin `kbutillib/cli/harness.py` exposes the verbs and is registered on
  the `kbu` CLI group.
- CLI shape follows CRAFT/PRD-A convention: per-step `── name`, ✓/✗ result lines, a
  summary block, return codes `0` all-ok / `1` partial / `2` none.
- **`harness.toml`** (`config.py`) at the harness root records: `beril_root` (absolute),
  `project_id`, `harness_root`, `created_at`, `kbutillib_version`. `pull/push/run/doctor`
  read it (so they take no path args when run from inside the harness). Round-trips via a
  load/save pair.
- **`init <BERIL_ROOT> <project-id> [--harness-root]`** (`scaffold.py`):
  - Validate `<BERIL_ROOT>` is a BERIL deployment (`PROJECT.md` + `.claude/skills/`
    present — same rule as PRD A's deployer) and that `<BERIL_ROOT>/projects/<project-id>/`
    exists; exit non-zero with a clear message otherwise.
  - Refuse if the target harness dir already exists and is non-empty (additive-only;
    direct user to `pull` instead) unless `--force`.
  - Create the harness dir, `git init`, scaffold the BERIL-mirror dirs
    (`notebooks/ data/ user_data/ figures/`), a `.gitignore` that excludes
    `.venv/ __pycache__/ **/.kbcache/`, an empty `DEVLOG.md`, write `harness.toml`.
  - Build a **venv** reusing the `kbu-bootstrap` venv/template helpers (`_template_ops.py`),
    installing `kbutillib` + the project's `requirements.txt` (if present). Solver is
    whatever the env provides (cobra bundles GLPK via optlang); do not mandate CPLEX/Gurobi.
  - Copy the `kbu-run` skill bundle into `<harness>/.claude/skills/`.
  - Copy `<BERIL_ROOT>/.claude/kbu/preferences.md` (PRD A's file) into
    `<harness>/.claude/kbu/preferences.md` so `kbu-run` reads the same graduated-execution
    thresholds. If absent in BERIL, render the PRD-A default template.
  - Do an initial `pull`.
- **`pull` / `push`** (`sync.py`): a single `rsync -a --delete`-style wrapper with a fixed
  exclude list (`.git`, `.venv`, `__pycache__`); `.kbcache/` is **not** excluded (syncs
  both ways). `pull` = `<BERIL_ROOT>/projects/<id>/` → `<harness>/`; `push` = reverse.
  `push` is artifact-safe: it writes executed notebooks (with outputs), `data/`, `figures/`,
  `.kbcache/` back, never deleting BERIL-only files outside the project subtree. Print the
  exact rsync command. Provide `--dry-run` (prints planned transfer, copies nothing) for
  tests and previews.
- **`run [notebooks…] [--on local|h100]`** (`runner.py`):
  - Default `--on local`. If no notebooks named, run all `notebooks/*.ipynb` in numeric
    order (`00_`, `01_`, …), stopping at the first failure (BERIL's numbered-notebook
    convention).
  - `local`: for each notebook, `jupyter nbconvert --to notebook --execute --inplace
    <nb>` in the harness venv; capture exit code + stderr/traceback; mark
    `outputs_present` true iff the executed notebook has ≥1 cell with non-empty outputs.
    Return a structured `RunResult` per notebook `{notebook, executed, exit_code, error,
    outputs_present, runtime_s}`. **Never edit notebook or `util.py` source.**
  - `h100`: write an ai-cowork task file to the h100 inbox (default
    `~/Dropbox/Projects/AIAssistant/cowork-inbox/h100/`, overridable via `--h100-inbox PATH`
    or `KBU_H100_INBOX`) whose body instructs only:
    `cd '<harness absolute path>'; kbu harness run --on local <notebooks>` — **no `git
    commit`** (the harness is Dropbox-synced, so the in-place executed notebooks + `.kbcache/`
    return via Dropbox; committing is the user's later step). Print the task path. Do **not**
    sleep-poll; `run --on h100` returns after dispatch and the user re-invokes `kbu-run`
    (or `kbu harness status`) to pick up the completed run.
  - `run` itself does **not** auto-push; pushing is a separate step the `kbu-run` skill
    gates behind user confirmation.
- **`doctor`** (read-only): ✓/✗ for venv present, `import kbutillib` under the harness
  venv, `harness.toml` valid + `beril_root` exists, `nbconvert` importable. Return 0 only
  when all ✓.
- **`status`** (optional, low-cost): print source record + last `DEVLOG.md` entry. Include
  if cheap; not a blocking requirement.

### Module 2 — `kbu-run` skill (markdown, deployed into the harness)
- One `SKILL.md` (+ references) at `kbutillib/harness/skills/kbu-run/`; `init` copies it to
  `<harness>/.claude/skills/kbu-run/`. Frontmatter uses BERIL's real Claude Code schema
  (`name`, `description` with a "Use when …" trigger, `allowed-tools`, `user-invocable: true`).
  Description scopes it to "running a local COBRA/MSModelUtil modeling project pulled from
  BERIL in a kbu harness."
- Workflow the skill drives:
  1. **pull** (the design-deploy step) — `kbu harness pull` to refresh from BERIL.
  2. **classify** — apply the PRD-A graduated-execution policy using the thresholds in the
     harness's `preferences.md` (🟢 cheap/certain, 🟡 sample-then-consult, 🔴 full). The
     harness is the place 🔴 full runs are meant to happen, but the skill still estimates
     cost and surfaces it.
  3. **choose location** — present local vs h100 (no encoded default); for h100 it dispatches
     the ai-cowork task.
  4. **run** — call `kbu harness run`.
  5. **verify** — read the `RunResult`; confirm `executed` + `outputs_present` for each.
  6. **dev-log** — append a `DEVLOG.md` entry for the run.
  7. **branch**: **success → stop, report the outcome, and on user OK call `kbu harness
     push`** then remind the user to `git add`/commit the executed notebooks in BERIL
     (BERIL's commit-with-outputs rule). **failure → stop, append the traceback to
     `DEVLOG.md`, and escalate a BLOCKED report; edit no code.**
- The skill must NOT call the Anthropic API or subprocess `claude` (Max-plan constraint;
  the worker IS Claude).

### MD dev-log specification
- `DEVLOG.md` at the harness root, **append-only**. One entry per pull/run/push:
  `## <ISO-8601 timestamp> — <pull|run|push>` followed by a short body with: notebooks
  involved, scope (`sample`|`full`), where (`local`|`h100`), outcome (`ok`|`failed`),
  runtime, and on failure a traceback reference (inline fenced block or pointer). The
  `devlog.py` writer only ever appends; existing entries are never rewritten.
- **Exact entry shape (confront #13):** `## <ISO-8601 UTC, trailing Z> — <pull|run|push>`
  followed by a fenced ```yaml``` block with keys `notebooks: [...]`, `scope: sample|full`,
  `where: local|h100`, `outcome: ok|failed`, `runtime_s: <float>`, and optional
  `traceback: |` (literal block). Single-writer assumption — no file locking (local,
  one user at a time).

### Confront round 1 — folded resolutions (concrete values for autonomous build)

- **Project-id → path mapping & collisions (#1):** `<project-id>` is lowercased, trimmed,
  with any char outside `[a-z0-9._-]` replaced by `-`. `--harness-root` may be absolute or
  relative (relative resolves against CWD); **default `~/Dropbox/Projects/kbu-harness/`,
  created if absent**. Dropbox is **not required** — the default is a convention, not a
  dependency (folds the truncated round-1 stall). If the target harness dir exists and is
  non-empty, `init` refuses unless `--force` (which empties it first).
- **BERIL validation & preferences (#2):** `init` requires only `PROJECT.md` +
  `.claude/skills/` (warn-not-fail on missing `.git`). If
  `<BERIL_ROOT>/.claude/kbu/preferences.md` exists, copy it to
  `<harness>/.claude/kbu/preferences.md`; else render the packaged PRD-A default template.
- **Venv policy (#3):** try the `kbu-bootstrap` venvman path, fall back to a plain `venv`;
  target Python `3.11`; after creation `pip install -U pip wheel`, then `pip install
  kbutillib` (skip when running from a KBUtilLib source checkout — dev mode), then
  `pip install -r requirements.txt` if the project has one. Solver = whatever the env
  provides (cobra bundles GLPK); CPLEX/Gurobi not mandated.
- **`kbu-run` skill artifact (#4):** ship `src/kbutillib/harness/skills/kbu-run/SKILL.md`
  in this repo; frontmatter `name: kbu-run`, `description` starting "Use when …",
  `allowed-tools` (Read, Bash), `user-invocable: true`. `init` copies the dir verbatim.
- **rsync command & containment (#5, #6, #37):** `rsync -aH --delete --info=stats2
  --exclude .git/ --exclude .venv/ --exclude __pycache__/ --exclude .ipynb_checkpoints/
  --exclude .DS_Store`. **Both** src and dest carry a **trailing slash on the project dir**
  so `--delete` is contained to that subtree and never references a parent (pull:
  `<BERIL_ROOT>/projects/<id>/` → `<harness>/`; push: reverse). `.kbcache/` is NOT excluded.
  Symlinks preserved via `-a` (no `-L`). Print the exact command. If `rsync` is absent,
  exit 1 with a clear message (no Python fallback) (#42).
- **Dry-run (#33):** `--dry-run` maps to `rsync --dry-run --itemize-changes`, prints the
  full command, copies nothing, exits 0 regardless of pending changes.
- **Sync safety (#40, #46):** `pull` refuses if the harness worktree has uncommitted
  tracked changes (`git status --porcelain`) unless `--force`; `push` refuses if the BERIL
  project subtree has uncommitted changes or files newer than the harness copies unless
  `--force`.
- **Preferences direction (#24, #34):** preferences sync **one-way** BERIL→harness only;
  `pull` refreshes `preferences.md` only if the BERIL source is newer and never overwrites
  local harness edits unless `--force`; `push` never writes preferences back to BERIL.
- **`--exclude-kbcache` (#41):** `pull`/`push` accept `--exclude-kbcache` to skip the cache
  when it is huge; default includes it.
- **Notebook discovery (#7):** `sorted(Path('notebooks').glob('*.ipynb'))`, excluding names
  starting with `.` and any path under `.ipynb_checkpoints`; lexicographic order (authors
  prefix `00_`/`01_` to control it). Stop at the first failure.
- **`outputs_present` (#8):** parse the in-place-updated `.ipynb` with `nbformat>=5`;
  `True` iff any **code** cell has a non-empty `outputs` list (markdown ignored).
- **Runner interpreter & kernel (#23, #38):** `runner.py` locates the harness venv
  interpreter (`harness.toml.python` or `<harness>/.venv/bin/python`) and runs
  `python -m jupyter nbconvert --to notebook --execute --inplace
  --ExecutePreprocessor.kernel_name=python3 <nb>` under it.
- **`RunResult` & `--json` (#28, #32, #39):** `run` returns an ordered list of
  `{notebook, executed, exit_code, error, outputs_present, runtime_s}`, stopping after the
  first failure. With `--json` it prints `{"results":[...], "overall_status":"ok|partial|failed"}`.
  Human-readable summary otherwise. `run` never auto-pushes.
- **"Never edit source" clarified (#35):** means no edits to code/markdown cell *source* or
  `util.py`; executing and saving **outputs** in place is intended and required by BERIL's
  commit-with-outputs rule.
- **No auto-cleanup (#48):** the runner deletes nothing nbconvert creates; `.gitignore`
  covers backups/checkpoints; tests assert no unintended files are left.
- **h100 cowork task (#9, #17, #43, #47, #21):** write a UTF-8 file
  `kbu-<project-id>-<timestamp>.task.md` to `~/Dropbox/Projects/AIAssistant/cowork-inbox/h100/`
  containing a fenced shell block: `cd '<abs harness path>'; kbu harness run --on local
  <notebooks>` (each notebook single-quoted; embedded quotes escaped `'"'"'`). **No
  auto-push** in the dispatched task — the skill pushes after verification. If the inbox
  dir is absent, print `✗ h100 inbox not found at <path>` and exit 1 (never crash). The
  harness absolute path is recorded as seen on the submitting machine (Dropbox keeps the
  same tree on h100 — lab standard, documented). All subprocess calls pass args as lists
  (no `shell=True`).
- **`doctor` (#10, #18, #45):** runs under the harness venv interpreter (✗ + non-zero if
  absent); checks venv present, `import kbutillib`, `harness.toml` valid + `beril_root`
  exists, `nbconvert` importable; CRAFT-style ✓/✗ lines + summary; exit 0 only if all ✓.
  Test matrix covers: missing venv, import-fails, malformed `harness.toml`, missing
  `beril_root`, nbconvert missing.
- **`harness.toml` (#11):** at the harness repo root; `beril_root`/`harness_root` absolute;
  `created_at` ISO-8601 UTC `Z`; plus `project_id`, `kbutillib_version`, and optional
  `python` (abs venv interpreter). Commands search **upward** from CWD for the nearest
  `harness.toml` (#49); if none found, require explicit path args.
- **`kbutillib_version` provenance (#50):** record the installed distribution version if
  available; else record `source_commit` (KBUtilLib HEAD sha) when running from a checkout.
- **CLI surface (#20, #27, #36):** register `harness_cmd` in `src/kbutillib/cli/harness.py`
  via `main.add_command(harness_cmd, name="harness")`, matching the existing `kbu beril`
  command's output helpers (step header prefix `── `, check lines `✓ `/`✗ `, `Summary:`
  block; errors prefixed `✗ `). Exit codes: `pull`/`push` 0 ok / 1 partial-rsync-error /
  2 source-missing; `run` 0 all-executed-with-outputs / 1 some-failed / 2 none-matched;
  `doctor` 0 all-✓ / 1 otherwise.
- **`kbu-run` classification (#12):** classification is the **skill's judgment** applying
  the PRD-A graduated policy (🟢/🟡/🔴), **not** a deterministic CLI algorithm; it reads the
  threshold values (`execution.runtime_threshold_seconds`, `execution.fanout_threshold`)
  from the harness `preferences.md`, honors any author hint in a notebook's first markdown
  cell, and **defaults to 🔴 (full / consult) when uncertain** (conservative).
- **`status` (optional, #19):** if implemented, print `harness.toml` fields + the last
  `## <ts> — <type>` header from `DEVLOG.md`; exit 0; nothing more.

### Confront round 2 — folded resolutions (concrete values for autonomous build)

- **h100 inbox override (r2 #1, #14):** the h100 inbox path is overridable via
  `--h100-inbox PATH` and the `KBU_H100_INBOX` env var; the default
  `~/Dropbox/Projects/AIAssistant/cowork-inbox/h100/` is used only when neither is set.
  Absence of the resolved inbox is a non-crashing error (`✗ h100 inbox not found at <path>`,
  exit 1). Tests always pass a temp-dir override and never reference the real Dropbox path.
- **rsync flags relaxed (r2 free-critique):** use `rsync -aH --delete …` (drop `-AX`) so the
  sync does not fail on filesystems lacking xattrs/ACLs.
- **No git commit in the h100 task (r2 free-critique):** the dispatched task body runs only
  `kbu harness run --on local <notebooks>`; it does **not** `git commit`. Dropbox returns the
  in-place executed notebooks; committing is the user's later step.
- **Dev-checkout detection (r2 #2):** skip the `pip install kbutillib` step when running from
  a source checkout — detected when `Path(kbutillib.__file__).parents[1].name == 'src'` and a
  `pyproject.toml` exists two levels up.
- **push-newer comparison (r2 #3):** `push` refuses when `rsync --dry-run --itemize-changes`
  in the BERIL→harness direction reports any incoming change (BERIL is newer/divergent) unless
  `--force`; `pull` refuses when `git -C <harness> status --porcelain` is non-empty unless
  `--force`.
- **preferences freshness (r2 #4):** BERIL `preferences.md` is "newer" when its mtime exceeds
  the harness copy's by ≥1 second; only then is it overwritten (never overwriting local edits
  unless `--force`).
- **rsync availability (r2 #5):** probe `shutil.which('rsync')`; if `None`, print
  `✗ rsync not found on PATH` and exit 1.
- **no-match message (r2 #6):** when no notebooks match, print
  `✗ no notebooks matched in notebooks/` and exit 2.
- **`RunResult.error` shape (r2 #7, #13):** `error` is the captured nbconvert **stderr**
  (UTF-8, trimmed to 10k bytes); on failure the DEVLOG `traceback: |` inline block carries the
  same text (≤10k bytes); no separate traceback file is created.
- **`outputs_present` output types (r2 #8):** count outputs of type `stream`,
  `execute_result` (with non-empty `data`), or `display_data` (with `data`); ignore `error`
  outputs.
- **`python` path recording (r2 #9):** `init` writes the absolute venv interpreter path to
  `harness.toml.python`; later commands never mutate it and fall back to
  `<harness>/.venv/bin/python` when it is missing; `doctor` reports ✗ when `python` is missing
  or non-existent.
- **`doctor` summary lines (r2 #10):** fixed output — `kbu harness doctor summary:` then
  `  Checks OK: X/Y` and `  Checks FAIL: Z`; exit 0 iff `Z == 0`, else 1.
- **preferences YAML schema (r2 #11):** the skill parses only the fenced ```yaml``` block in
  `preferences.md` for `execution.runtime_threshold_seconds` and `execution.fanout_threshold`
  (the PRD-A schema); if the block is absent it defaults to 🔴.
- **push confirmation modality (r2 #12):** the `kbu-run` skill emits a single-line question
  `Push results back to BERIL now? (y/N)`; only `y`/`Y` proceeds, any other input skips push.
- **bundled preferences template (r2 #15):** the PRD-A default template is bundled at
  `src/kbutillib/beril/skills/kbu/preferences.md`; `init` copies it verbatim to
  `<harness>/.claude/kbu/preferences.md` when BERIL has none.

## Testing Decisions

Tests assert external behavior against temp dirs (prior art: KBUtilLib `tests/`, the
composition smoke fixtures `mini_model`/`shared_env`, and PRD A's fake-BERIL-root fixture).

- **Module 1 — harness library core** (highest value): against a temp fake BERIL root
  (`PROJECT.md` + `.claude/skills/` + `projects/<id>/` + `git init`) and a temp harness
  root: `init` produces the expected tree (git repo, mirror dirs, `.claude/skills/kbu-run`,
  `DEVLOG.md`, `harness.toml`, `preferences.md`) and an initial pull lands the project
  files; `pull`/`push` round-trip a file **both ways** with `.kbcache/` included and the
  exclude list (`.git/.venv/__pycache__`) honored; `harness.toml` load/save round-trips;
  `doctor` reports green on a healthy harness and red on a missing venv / bad source path;
  `init --force` / non-empty-refusal behaves; `--dry-run` on `pull` copies nothing.
- **Module 1 — notebook runner**: `kbu harness run` on a **tiny real `.ipynb`** (one cell
  computing a value) returns `executed=True, outputs_present=True`; an intentionally
  **throwing** notebook returns `executed=False` with the traceback captured in the
  `RunResult` and the source file unchanged (assert no code edit). Mock/guard the h100 path
  so the test asserts `--on h100` writes a well-formed cowork task file (to a temp inbox)
  and performs no local execution.
- **Module 1 — dev-log writer**: appending two entries yields a `DEVLOG.md` with both,
  well-formed headers, and the first entry byte-unchanged after the second append.
- **Module 2 — skill-bundle smoke**: the `kbu-run` `SKILL.md` parses with valid frontmatter
  (`name`, `description` with "Use when", `allowed-tools`, `user-invocable: true`).
- **Fixtures & hygiene (confront #15, #16, #26, #45):** the fake BERIL root is a temp dir
  with `PROJECT.md`, `.claude/skills/`, `.claude/kbu/preferences.md`, `git init`, and under
  `projects/<id>/`: `notebooks/00_hello.ipynb` (single code cell), `data/.gitkeep`,
  `figures/.gitkeep`, a `.kbcache/.gitkeep`, and an optional `requirements.txt` to exercise
  the venv-install branch. Test notebooks are generated on the fly via
  `nbformat.v4.new_notebook()` — no binary `.ipynb` is committed. All tests operate
  **entirely in temp dirs** and never reference real `~/Dropbox` paths. The `doctor` matrix
  covers missing venv, import-fail, malformed `harness.toml`, missing `beril_root`, and
  missing `nbconvert`.

## Out of Scope

- **Spark / BERDL-query notebooks and the cloud JupyterHub.** The harness runs **local
  COBRA/MSModelUtil modeling notebooks** only; Spark notebooks stay on BERIL's web-UI path.
- **Syncing against the remote JupyterHub instance.** rsync endpoints are the **local BERIL
  clone** ⇄ harness only. No scp/MinIO/remote-download transport.
- **Autonomous failure repair.** `kbu-run` never edits code; diagnose-and-fix loops are
  explicitly excluded (the conservative decision). Fixing a failed run happens back in BERIL.
- **Headless Maestro/AgentForge dispatch.** Cross-machine execution is via the ai-cowork
  inbox only; a Maestro-backed path is a possible later PRD.
- **Re-introducing a plan/build/run state machine.** The retired co-scientist's orchestration
  is gone for good; the harness is execute-and-report, not a workflow engine.
- **Changing BERIL's tracked tree or its commit/`/submit` conventions.** The harness pushes
  files into `projects/{id}/`; the human commits and submits in BERIL.
- **Stripping the old kbu co-scientist** (`kbu-start/plan/build/migrate`, `kbu-sub-*`, and
  the org/exec CLI `cli/notebook.py`, `subproject.py`, `manifest.py`, `layout.py`).
  **Deferred to its own follow-up PRD.** The confront established the strip is *load-bearing*,
  not a clean excision: kept commands built in the v2/bootstrap work
  (`cli/bootstrap.py`, `cli/update.py`, `cli/new_project.py`) import
  `..layout.DEFAULT_SHARED_DIRS` and `.manifest`, and ~11 test files reference the targets.
  Folding a half-coherent strip into the harness build would derail the autonomous conductor;
  the strip's full scope (which of the old org surface goes vs stays) is decided in a
  dedicated PRD.
- **Windows.** PRD B targets macOS/Linux (`rsync`, POSIX paths); Windows is out of scope.

## Further Notes

- **Grounding artifacts:** PRD A at `agent-io/prds/kbu-beril-augmentation/`; the keep/discard
  + conflict-precedence audit at `agent-io/audits/2026-06-13-kbu-vs-beril-directive-audit.md`;
  BERIL at `~/Dropbox/Projects/BERIL-research-observatory` (`PROJECT.md` for the
  no-programmatic-execution rule, the `projects/{id}/` layout, and the commit-with-outputs
  rule); CRAFT at `~/Dropbox/Projects/craft` (CLI `install/doctor` surface convention only —
  it has no harness/per-project-container pattern).
- **Canonical `util.py`:** `~/Dropbox/Projects/ModelingLOE/notebooks/gapfill_loe/util.py`.
- **Reuse:** the venv/template helpers extracted during `kbu-bootstrap`
  (`kbutillib/cli/_template_ops.py`) build the harness venv — do not reinvent env setup.
- **Privacy/runtime:** the worker IS Claude (Max plan) — skills/CLI must not call the
  Anthropic API or subprocess `claude`.
- **Distribution caveat:** local execution is the portable default for any KBase user; the
  h100 ai-cowork dispatch is Chris-lab-specific and must degrade gracefully (clear message,
  no crash) when the cowork inbox path is absent.

## Acceptance Criteria

1. `kbu harness` is a new subcommand group registered via `main.add_command(harness_cmd, name="harness")` in `src/kbutillib/cli/harness.py`, with subcommands `init`, `pull`, `push`, `run`, `doctor` (and optional `status`).
2. `kbu harness init <BERIL_ROOT> <project-id> [--harness-root PATH] [--force]` validates `<BERIL_ROOT>` by presence of both `PROJECT.md` and `.claude/skills/` (warns but does not fail when `<BERIL_ROOT>/.git` is absent) and that `<BERIL_ROOT>/projects/<project-id>/` exists; it exits non-zero with a clear message otherwise.
3. `init` resolves the harness dir as `<harness-root>/<sanitized-project-id>/` where `<harness-root>` defaults to `~/Dropbox/Projects/kbu-harness/` (created if absent, Dropbox not required), `--harness-root` may be absolute or relative, and `<project-id>` is lowercased/trimmed with chars outside `[a-z0-9._-]` replaced by `-`.
4. `init` refuses when the target harness dir exists and is non-empty unless `--force` (which empties it first).
5. `init` scaffolds: a `git init` repo (no auto-commit), the BERIL-mirror dirs `notebooks/ data/ user_data/ figures/`, a `.gitignore` containing `.venv/`, `__pycache__/`, `*.egg-info/`, `.ipynb_checkpoints/`, `.DS_Store`, `**/.kbcache/`, an empty `DEVLOG.md`, a `harness.toml`, the `.claude/skills/kbu-run/` bundle, and `.claude/kbu/preferences.md`.
6. `init` copies `<BERIL_ROOT>/.claude/kbu/preferences.md` into the harness when it exists, else renders the packaged PRD-A default template.
7. `init` builds a Python 3.11 venv (venvman path with plain-`venv` fallback), runs `pip install -U pip wheel`, installs `kbutillib` (skipped when run from a KBUtilLib source checkout), and installs the project `requirements.txt` if present; it does not mandate CPLEX/Gurobi.
8. `init` performs an initial `pull` so the project files land in the harness.
9. `harness.toml` lives at the harness repo root and records `beril_root` (absolute), `harness_root` (absolute), `project_id`, `created_at` (ISO-8601 UTC with `Z`), `kbutillib_version`, and optional `python` (absolute venv interpreter path); it round-trips through a load/save pair.
10. `kbutillib_version` is recorded as the installed distribution version when available, else `source_commit` (KBUtilLib HEAD sha) when running from a checkout.
11. `pull`/`push`/`run`/`doctor`/`status` locate the harness by searching upward from CWD for the nearest `harness.toml`; if none is found they require explicit path arguments.
12. `pull` rsyncs `<BERIL_ROOT>/projects/<id>/` → `<harness>/` and `push` rsyncs the reverse, both using `rsync -aH --delete` with excludes `.git/ .venv/ __pycache__/ .ipynb_checkpoints/ .DS_Store`, trailing slashes on the project dir on both sides so `--delete` is contained to that subtree, `.kbcache/` included, symlinks preserved (no `-L`); the exact rsync command is printed.
13. `pull`/`push` accept `--exclude-kbcache` to skip the cache (default includes it) and `--dry-run` (maps to `rsync --dry-run --itemize-changes`), which copies nothing, prints the full command, and exits 0 regardless of pending changes.
14. `pull` refuses when the harness worktree has uncommitted tracked changes unless `--force`; `push` refuses when the BERIL project subtree has uncommitted changes or files newer than the harness copies unless `--force`.
15. Preferences sync one-way BERIL→harness only: `pull` refreshes `preferences.md` only when the BERIL source is newer and never overwrites local harness edits unless `--force`; `push` never writes `preferences.md` back to BERIL.
16. `kbu harness run [notebooks…] [--on local|h100] [--json]` with no notebooks named discovers `sorted(notebooks/*.ipynb)` excluding dot-files and `.ipynb_checkpoints`, runs them in lexicographic order, and stops at the first failure.
17. `run --on local` executes each notebook via the harness venv interpreter as `python -m jupyter nbconvert --to notebook --execute --inplace --ExecutePreprocessor.kernel_name=python3 <nb>`, never editing cell source or `util.py`; saving outputs in place is the intended behavior.
18. `run` returns an ordered list of `RunResult` `{notebook, executed, exit_code, error, outputs_present, runtime_s}`; `outputs_present` is true iff (parsing with `nbformat>=5`) any code cell has a non-empty `outputs` list; with `--json` it prints `{"results":[...], "overall_status":"ok|partial|failed"}`.
19. `run` never auto-pushes; exit codes are `run` 0 all-executed-with-outputs / 1 some-failed / 2 none-matched.
20. `run --on h100` writes a UTF-8 file `kbu-<project-id>-<timestamp>.task.md` to the resolved h100 inbox containing a fenced shell block `cd '<abs harness path>'; kbu harness run --on local <notebooks>` (notebooks single-quoted, embedded quotes escaped) with **no `git commit`** and no auto-push, and performs no local execution; if the resolved inbox dir is absent it prints `✗ h100 inbox not found at <path>` and exits 1 without crashing.
21. All subprocess invocations pass arguments as lists (no `shell=True`) and use absolute paths.
22. `kbu harness doctor` runs under the harness venv interpreter and reports ✓/✗ for: venv present, `import kbutillib` succeeds, `harness.toml` valid with an existing `beril_root`, and `nbconvert` importable; it returns 0 only when all are ✓ and never mutates anything.
23. CLI output matches the existing `kbu beril` convention: step header prefix `── `, check lines prefixed `✓ ` / `✗ `, a `Summary:` block, and error messages prefixed `✗ `; `pull`/`push` exit `0` ok / `1` partial rsync error / `2` source missing; `doctor` exits `0` all-✓ / `1` otherwise.
24. `DEVLOG.md` is append-only; each entry is `## <ISO-8601 UTC Z> — <pull|run|push>` followed by a fenced YAML block with keys `notebooks`, `scope` (`sample|full`), `where` (`local|h100`), `outcome` (`ok|failed`), `runtime_s`, and optional `traceback`; existing entries are never rewritten; no file locking is used.
25. The deployed `kbu-run` `SKILL.md` (shipped at `src/kbutillib/harness/skills/kbu-run/`) has valid Claude Code frontmatter: `name: kbu-run`, a `description` starting "Use when …" scoped to running a local COBRA/MSModelUtil project pulled from BERIL, `allowed-tools`, and `user-invocable: true`.
26. The `kbu-run` skill drives the loop pull → classify (graduated policy by the skill's judgment, reading `preferences.md` thresholds, defaulting to 🔴 when uncertain) → choose local vs h100 → `kbu harness run` → verify outputs → append `DEVLOG.md`; on success it stops, reports, and only on user confirmation calls `kbu harness push` then reminds the user to commit in BERIL; on the first failure it stops, appends the traceback to `DEVLOG.md`, escalates a BLOCKED report, and edits no code.
27. Tests run entirely in temp dirs (never touching real `~/Dropbox` paths) and cover: `init` scaffold against a fake BERIL root, pull/push round-trip both ways with `.kbcache/` included and excludes honored, `harness.toml` round-trip, `--dry-run` copying nothing, `--force`/non-empty-refusal, the runner on a generated clean notebook (`executed/outputs_present` true) and a throwing notebook (failure `RunResult`, source unchanged), `--on h100` writing a well-formed task file to a temp inbox with no execution, dev-log append-only, the `kbu-run` SKILL.md frontmatter smoke, and the `doctor` matrix (missing venv, import-fail, malformed `harness.toml`, missing `beril_root`, missing `nbconvert`).
28. Test notebooks are generated on the fly via `nbformat.v4.new_notebook()`; no binary `.ipynb` is committed to the repo.
29. PRD B does not delete or modify the old co-scientist surface (`kbu-start/plan/build/migrate`, `kbu-sub-*`, `cli/notebook.py`, `cli/subproject.py`, `cli/manifest.py`, `layout.py`); that strip is deferred to a separate follow-up PRD.
30. Windows is out of scope; the harness targets macOS/Linux with `rsync` and POSIX paths, and exits 1 with a clear message if `rsync` is not found.
31. The h100 inbox path resolves from `--h100-inbox PATH` then `KBU_H100_INBOX` then the default `~/Dropbox/Projects/AIAssistant/cowork-inbox/h100/`; tests exercise `run --on h100` only against a temp-dir override and never the real Dropbox path.
32. rsync uses `-aH` (not `-aHAX`) to avoid xattr/ACL failures, and availability is probed via `shutil.which('rsync')`, printing `✗ rsync not found on PATH` and exiting 1 when absent.
33. `init` skips `pip install kbutillib` when run from a source checkout, detected by `Path(kbutillib.__file__).parents[1].name == 'src'` plus a `pyproject.toml` two levels up.
34. `push` refuses when a BERIL→harness `rsync --dry-run --itemize-changes` reports incoming changes unless `--force`; `pull` refuses when `git -C <harness> status --porcelain` is non-empty unless `--force`; BERIL `preferences.md` is treated as newer only when its mtime exceeds the harness copy's by ≥1 second.
35. `RunResult.error` is the nbconvert stderr trimmed to 10k bytes, and the DEVLOG failure `traceback: |` block carries the same text (≤10k bytes) inline with no separate file; `outputs_present` counts `stream`/`execute_result`-with-data/`display_data` outputs and ignores `error` outputs.
36. `doctor` prints the fixed summary `kbu harness doctor summary:` / `  Checks OK: X/Y` / `  Checks FAIL: Z`, exiting 0 iff `Z == 0`; it reports ✗ when `harness.toml.python` is missing or points to a non-existent interpreter.
37. The `kbu-run` skill, on success, emits the single-line prompt `Push results back to BERIL now? (y/N)` and pushes only on `y`/`Y`; the bundled default preferences template at `src/kbutillib/beril/skills/kbu/preferences.md` is copied verbatim by `init` when BERIL lacks one.
