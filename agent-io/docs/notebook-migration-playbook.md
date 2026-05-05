# Notebook Engine Migration Playbook

A living procedure for transforming a notebook repo from the legacy `util.py` god-class + `%run util.py` + JSON `datacache/` pattern to the new `NotebookSession` API in `kbutillib.notebook`.

**Reference PRD**: [agent-io/prds/notebook-engine-redesign/fullprompt.md](../prds/notebook-engine-redesign/fullprompt.md)
**First proving ground**: ADP1Notebooks (Phases 1–3.5 complete; 4a/4b complete; 4c in progress as of 2026-05-04)
**Future targets**: ANMENotebooks, FitnessDatabaseAnalysis, EcoliPhenotypeAnalysis, ADP1PhenotypeAnalysis, PangenomeAnalysis, BVBRCHackathon, AISynbioPipeline, BERDLTablesPrototype, ModelSEEDNotebooks

This playbook is updated after each phase completes. Section **§13 Lessons learned** at the bottom is the living changelog.

---

## 1. Repo eligibility check

Before starting, confirm the target repo follows the legacy pattern:

```bash
ls notebooks/util.py notebooks/datacache/ 2>/dev/null    # both should exist
grep -l "%run util.py" notebooks/*.ipynb | head -3       # at least one notebook uses it
```

If `util.py` doesn't exist or notebooks already use the new pattern, skip this playbook.

---

## 2. Pre-flight cleanup (mandatory)

Before any AgentForge work, the target repo must be in a clean, pushed state. AgentForge worker worktrees only see what's been committed AND synced via Dropbox. Uncommitted changes are invisible to the worker.

### 2.1 Audit uncommitted state

```bash
cd /Users/chenry/Dropbox/Projects/<RepoName>
git status --porcelain
git stash list
git rev-list --left-right --count main...origin/main      # how far behind/ahead
git branch | head -20                                      # any orphan agent/ branches
```

### 2.2 Push unpushed commits

```bash
git push origin main
```

### 2.3 Decide on uncommitted files

For each `M` (modified) file: commit, stash, or revert. Don't leave them.
For each `??` (untracked) file:

| Size / type | Action |
|---|---|
| **>10MB binary / DB** | Add to `.gitignore` |
| **Word/PowerPoint/PDF docs** | Add to `.gitignore`, action item to upload to DocDB |
| **Leaked agent skills** (e.g., `.claude/commands/worker.md`) | Delete |
| **Code / config** | Decide commit vs gitignore based on intent |

### 2.4 Delete orphan agent branches

```bash
git branch | grep "^[ +] agent/" | xargs -n1 git branch -D
```

### 2.5 Prune dangling worktrees

```bash
git worktree prune --verbose
# For cross-machine worktrees (path prefix doesn't match this machine's tmp):
git worktree list --porcelain | grep "^worktree " | grep -v "^worktree /Users/chenry/Dropbox" \
  | sed 's/^worktree //' | xargs -I{} git worktree remove --force "{}"
```

### 2.6 Final clean check

```bash
git status --porcelain        # ideally empty, or only intentional WIP
git branch                    # only main, no agent/ branches
git worktree list             # only the main worktree
git rev-list --left-right --count main...origin/main    # 0 0
```

---

## 3. `kbu init-notebook` bootstrap

Each notebook project needs a per-project venv with editable installs of sibling repos. The `kbu init-notebook` CLI automates this.

### 3.1 One-time setup (per machine)

```bash
# Symlink the kbu wrapper into your PATH (once per machine)
ln -sf ~/Dropbox/Projects/KBUtilLib/bin/kbu ~/.local/bin/kbu

# Verify
kbu --version
```

### 3.2 Bootstrap a notebook project

```bash
cd /Users/chenry/Dropbox/Projects/<NotebookRepo>
kbu init-notebook
```

This will:
1. Resolve your machine alias from `~/.agentforge/config.yaml` (or hardware UUID, or interactive prompt).
2. Read merged config from `KBUtilLib/machine_configs/_default.yaml` + `<alias>.yaml`.
3. Create a venv via `venvman create --project kbu.nb-<project>`.
4. `pip install -e` each sibling repo (KBUtilLib, cobrakbase, ModelSEEDpy, ModelSEEDDatabase).
5. `pip install` notebook deps (jupyter, ipykernel, ipywidgets, itables, pandas, tqdm).
6. Render `notebooks/util.py` from a Jinja template (NotebookSession-based).
7. Register a Jupyter kernel: `kbu.nb-<project>`.
8. Pin all `*.ipynb` files to that kernel.

### 3.3 Options

| Flag | Effect |
|---|---|
| `--project NAME` | Override project name (default: cwd basename) |
| `--python VER` | Override Python version (default: from machine config) |
| `--alias NAME` | Override machine alias resolution |
| `--force` | Overwrite util.py header (preserves custom code below marker); force-pin all kernels |
| `--no-pin-kernels` | Skip kernel registration and .ipynb metadata pinning |
| `--no-venv` | Skip venv creation; only generate template files |

### 3.4 AgentForge environment prerequisites

The submitting machine must be configured to write task records to the Dropbox-synced fleet location, otherwise the worker on `emailmac` can't see submissions.

```bash
cat ~/.agentforge/config.yaml
```

Must contain:

```yaml
central_tasks_dir: /Users/chenry/Dropbox/Jobs/agentforge/tasks
central_plans_dir: /Users/chenry/Dropbox/Jobs/agentforge/plans
worker:
  machine_alias: "primary-laptop"   # or whichever machine you're on
  default_machine: "emailmac"
  machines:
    - emailmac
    - h100
    - primary-laptop
```

If `central_tasks_dir` is missing, submissions land in local `~/.agentforge/tasks/` (invisible to other machines).

---

## 4. Phasing strategy

For a notebook repo with ≥10 notebooks, use this 4-stage decomposition. Smaller repos (<5 notebooks) can collapse 4b/4c into one task.

| Phase | Scope | Est. effort | Outputs |
|---|---|---|---|
| **Pre-flight** | §2 cleanup of target repo | 5 min manual | clean repo synced to origin |
| **4-bootstrap** | `kbu init-notebook` creates per-project venv, renders `util.py` from template, registers Jupyter kernel, pins notebooks. | 0.5 wk agent | `bin/kbu`, `src/kbutillib/cli/`, `machine_configs/`, tests |
| **4a — util.py shell** | Replace 1000+ line `util.py` god-class with `NotebookSession` + free helper functions + tests. NO `.ipynb` changes. | 0.5 wk agent | `util.py` (slim), `util_legacy.py` (preserved), `test_util.py`, `conftest.py`, `UTIL_README.md` |
| **4b — pilot notebook** | Migrate ONE representative notebook end-to-end. Surfaces API gaps. | 0.5–1 wk agent | migrated notebook, ported util.py functions, `PHASE_4B_MIGRATION_LOG.md` documenting deferrals + gaps |
| **3.5-style gap-fix** | Address API gaps surfaced by the pilot. Verify-first; some "gaps" are misunderstandings. | 0.5 wk agent | KBUtilLib API extensions + tests + PRD §15 updates |
| **4c-i, 4c-ii, ...** | Bulk migrate remaining notebooks in groups of 3–4. Each group reviewed before next launches. | 1 wk per group | migrated notebooks, util.py extensions, group-level migration log |
| **4d — cleanup** | Delete superseded notebooks (`*Old*`, `*Mockup*`, dated copies, non-BERDL siblings), drop `util_legacy.py`, remove old `datacache/`, papermill smoke tests | 0.5 wk agent | clean tree, all notebooks pass papermill smoke |
| **4.5 — `/jupyter-dev` skill rewrite** | Update the skill to teach the new pattern | 0.5 wk | new skill that scaffolds NotebookSession-based projects |

Total: ~7-9 weeks for a ~15-notebook repo. Smaller repos compress proportionally.

---

## 5. Pilot notebook selection (Phase 4b)

Pick a notebook that:
- Is **current** (prefer BERDL/newer over legacy/older variants).
- **Exercises multiple API surfaces** — Vector ingestion, fold_change, Sample registration, cross-notebook references.
- Has **manageable complexity** — NOT the most tangled pipeline (those tend to have legacy bugs that distract). For ADP1Notebooks, `ADP1BERDLFitnessFluxFitting.ipynb` would have been too complex; `ADP1BERDLFoldChangeAnalysis.ipynb` was right.
- Surfaces **cross-notebook dependencies** so we learn whether the cache layer handles them.

The pilot's goal is not just to migrate one notebook — it's to **find the cracks** before we run the same migration across 10+ files.

---

## 6. Group selection heuristics (Phase 4c)

Group ~3 notebooks per task by:
1. **Cross-notebook dependency chains** — group together if A reads from B's cache. Migrate the producer first within each group.
2. **Topical similarity** — gene expression notebooks together, model notebooks together, phenotype notebooks together. Ports of the same legacy functions can be batched.
3. **Drop superseded variants** during the appropriate group rather than as a separate task. Rule of thumb: **BERDL > non-BERDL**; un-dated > dated copies; primary > "Old" / "Mockup".

Run groups **sequentially**, not in parallel. Each group's review surfaces patterns that should inform the next. Use a manual gate (review the merged result before kicking off the next group) — automated chaining defeats the learning loop.

---

## 7. AgentForge submission template

For every notebook-migration task, use this structure (writing the prompt to `/tmp/*.md` first to avoid bash backtick interpretation):

```bash
# 1. Write the prompt to a file
$EDITOR /tmp/phaseXY-prompt.md

# 2. Submit with the standard flag set
PROMPT="$(cat /tmp/phaseXY-prompt.md)" && agentforge submit \
  --role developer \
  --machine emailmac \
  --repo <TargetRepo> \
  --repo KBUtilLib:ro \              # read-only access to PRD + new API
  --auto-review \
  --auto-merge \
  --timeout 900 \
  --priority high \
  --summary "Phase X.Y: <one-line scope>" \
  --tag notebook-engine \
  --tag phase-X-Y \
  "$PROMPT"

# 3. Verify the prompt landed clean
cat ~/Dropbox/Jobs/agentforge/tasks/task-XXXXXXXX.json | python3 -c "
import json, sys
d = json.load(sys.stdin)
print('Length:', len(d['prompt']))
print('Repos:', [(r['name'], r.get('writable')) for r in d['repos']])
print('Target:', d.get('target_machine'))
"
```

### 7.1 Standard prompt skeleton

Each prompt should include:

1. **PRD pointer** — link to `agent-io/prds/notebook-engine-redesign/fullprompt.md`, list relevant section numbers.
2. **State of repo** — what's already in place (Phase 4a/4b/etc.) and what to build on.
3. **High-level goal** — one sentence.
4. **Cell-by-cell migration guide** for each notebook in scope (when migrating notebooks). For each cell: legacy behavior → new behavior, function calls to replace, deferrals/skips with rationale.
5. **`util.py` additions** — which legacy functions to port, with required tests. Which to defer.
6. **Process** — verify steps, test pass criteria, deliverables (notebook, util.py, log file).
7. **Strict out-of-scope** — list everything NOT to touch. This prevents scope drift.
8. **Constraints** — Python version, no-pickle rule, code style match, tests must pass.

For pilot/gap-fix tasks, include a **VERIFY FIRST** clause for each gap: agent confirms current behavior before "fixing" — distinguishes real bugs from misunderstandings.

---

## 8. Known AgentForge auto-merge bug + workaround

The chain reports `merge ✓` but the actual commit doesn't reach `origin/main`. This happens **every time** a developer task with `--auto-merge` succeeds. Manual merge is required.

### 8.1 Symptoms

- Chain status: `dev ✓ → review ✓ → merge ✓` (looks fine).
- BUT: `git log origin/main` shows main hasn't moved.
- The agent's branch exists locally (synced via Dropbox) but was never pushed.

OR a similar pattern with a different cause:

- Chain status: `dev ✓ → review ✗ → troubleshoot queued`.
- Reviewer task ended in `failed`, but the reviewer's substantive verdict was `APPROVE`.
- The failure was the AgentForge meta-error: "Agent completed but has uncommitted changes" (the reviewer makes trivial cleanup edits in its worktree but doesn't commit them).

### 8.2 Workaround procedure

```bash
cd /Users/chenry/Dropbox/Projects/<TargetRepo>
git fetch --all

# Identify branches: dev's branch and any reviewer auto-review branch
git branch | grep "^[ +] agent/"

# Are dev and reviewer branches siblings (both off main) or is reviewer downstream of dev?
git merge-base --is-ancestor agent/developer/<dev-branch> agent/reviewer/<rev-branch> && echo "rev includes dev" || echo "siblings"

# Common path: fast-forward dev's branch to main
git rev-list --left-right --count main...origin/main    # confirm 0 0 (no surprises)
git merge --ff-only agent/developer/<dev-branch>

# If reviewer made a fix-up commit on a separate sibling branch: cherry-pick it
git cherry-pick <reviewer-fix-sha>
# Resolve any conflicts (typo fixes, SQL improvements, etc.) — usually take reviewer's version
git cherry-pick --continue --no-edit

git push origin main

# Cleanup
git worktree prune --verbose
git branch | grep "^[ +] agent/" | xargs -n1 git branch -D
```

### 8.3 If your working tree has WIP

If you have your own uncommitted WIP on the target repo, stash it first:

```bash
git stash push -m "wip-during-merge" -- <list of M files>
# do the merge dance
git stash pop
```

Cherry-picks may conflict with WIP (the WIP touches files the cherry-pick also touches). Resolve manually.

---

## 9. Inputs vs cache rule (load-bearing)

**Per PRD §4.2**: `data/`, `models/`, `genomes/` are **inputs** — they stay. Only `datacache/` is replaced by `.kbcache/`.

**No cache migration.** Notebooks must regenerate intermediates from raw inputs on every fresh run. If a notebook can't reproduce its outputs from `data/`/`models/`/`genomes/`, that's a notebook bug to fix — never a migration to write.

This is the most important rule. Several agent attempts have proposed building "cache migration" logic; reject these every time.

---

## 10. util.py lifecycle

1. **Phase 4-bootstrap**: `kbu init-notebook` renders the initial `notebooks/util.py` from a Jinja template (`src/kbutillib/cli/templates/util.py.tmpl`). The template provides common imports, a module-level `session` (NotebookSession), `session_for()` back-compat shim, and a `# === project-specific helpers below ===` marker separating generated header from project-specific code. No sys.path tricks — the per-project venv handles imports.

2. **Phase 4a**: Old `util.py` (1000+ lines, multi-inheritance god-class) → renamed `util_legacy.py` (preserved for porting reference). New `util.py` builds on the template from 4-bootstrap:
   - `from kbutillib.notebook import NotebookSession`
   - `session_for(file: str) -> NotebookSession` convenience constructor
   - A handful of pure helper functions ported from legacy (with tests)

2. **Phases 4b, 4c-***: As each notebook is migrated, port the functions it actually uses from `util_legacy.py` into `util.py`, **always with tests**.

3. **Phase 4d**: After all notebooks are migrated and pass smoke tests, delete `util_legacy.py`. Any function still referenced means a notebook wasn't fully migrated.

4. **Phase 5**: Mature `util.py` functions migrate via `@migration_target("kbutillib.kb_genome_utils")` to permanent KBUtilLib/ModelSEEDpy modules. The `util.py` re-exports them so notebook imports stay stable.

**Hard rule**: util.py functions are **free functions or methods on small helper classes**, never bolted onto a god-class. They must be unit-testable in isolation.

---

## 11. Per-task cleanup checklist

After every successfully merged migration task:

```bash
cd /Users/chenry/Dropbox/Projects/<TargetRepo>
git worktree list --porcelain | grep "^worktree " | grep -v "^worktree /Users/chenry/Dropbox" \
  | sed 's/^worktree //' | xargs -I{} git worktree remove --force "{}"
git worktree prune --verbose
git branch | grep "^[ +] agent/" | xargs -n1 git branch -D

# Final state should be:
git branch                                          # only main
git worktree list                                   # only the main worktree
git rev-list --left-right --count main...origin/main  # 0 0
```

Also: cancel any monitoring cron job for this task (`CronDelete <id>`).

---

## 12. Checklist for adapting this playbook to a new repo

For each new target repo (ANMENotebooks, etc.):

- [ ] Run §2 pre-flight cleanup. Repo state must be clean.
- [ ] Confirm §3 environment is correct on this machine.
- [ ] Choose pilot notebook per §5. Don't pick the most complex.
- [ ] Submit Phase 4a (util.py shell). One agent task. Manually merge if auto-merge fails (§8).
- [ ] Submit Phase 4b (pilot). Read the migration log carefully — it lists deferrals and gaps.
- [ ] Submit gap-fix follow-up to KBUtilLib (Phase 3.5-style). Verify-first instructions per §7.1.
- [ ] Group remaining notebooks per §6. Submit group-by-group. **Manual gate between groups.**
- [ ] Submit Phase 4d (cleanup + papermill smoke).
- [ ] Update §13 of this playbook with anything that surprised you. The next repo benefits.

---

## 13. Lessons learned (living changelog)

Append entries here after each phase completes. Date-stamp each entry.

### 2026-05-03 — Phase 1 (KBUtilLib)
- Auto-merge bug encountered. Manual merge dance documented in §8.
- Reviewer's "Agent completed but has uncommitted changes" failure mode is a meta-error, not a substantive review rejection. Always read the reviewer's `summary` field for the actual verdict.
- Local `~/.agentforge/tasks/` vs Dropbox `~/Dropbox/Jobs/agentforge/tasks/` config issue surfaced. Documented in §3.

### 2026-05-04 — Phase 4a (ADP1Notebooks)
- One-shot success: `dev ✓ → review ✓ → merge ✓` first try. The pattern (rename legacy, slim new util.py with NotebookSession + free functions + tests) is solid.
- Auto-merge still didn't reach origin/main. §8 workaround needed.

### 2026-05-04 — Phase 4b (ADP1Notebooks pilot — `ADP1BERDLFoldChangeAnalysis`)
- Reviewer-meta-error pattern again: substantive verdict APPROVE, task status `failed`. Manual merge.
- Three "gaps" surfaced in the pilot's migration log:
  1. `vectors.fold_change` rejected multi-column inputs (real gap — fixed in Phase 3.5 with `aggregate=` parameter).
  2. "No cross-notebook cache resolution" — turned out to be agent misunderstanding (cache IS project-wide). Added regression test pinning the invariant + filter helper for discoverability.
  3. "Sample requires Media composition" — also misunderstanding. Sample registration accepts Media without composition; added regression test.
- **Verify-first instructions in the gap-fix prompt are essential** — caught 2 of 3 misunderstandings before they became "fixes" that broke the design.

### 2026-05-04 — Phase 3.5 (KBUtilLib gap fixes)
- Reviewer made a separate fix-up commit on a SIBLING branch (not on top of dev's branch). Required cherry-pick rather than fast-forward. Two trivial conflicts resolved (typo + SQL fix). Reviewer's correlated-subquery is the canonical "first row per group" SQL pattern.
- Lesson: when chain emits a reviewer fix-up commit, expect cherry-pick + possible conflicts during manual merge.

### 2026-05-04 — Pre-Phase-4 cleanup (ADP1Notebooks)
- Found 148 MB SQLite untracked. Gitignored. Don't commit big binaries.
- Found leaked `.claude/commands/worker.md` (32 KB). Same pattern from the KBUtilLib skills audit. Delete on sight.
- Found archival Word doc. Action item to upload to DocDB.
- Took ~5 minutes total. Worth doing manually rather than as an agent task.

### 2026-05-04 — Phase 4c-i (ADP1Notebooks BERDL trio)

Migrated ADP1BERDLFitnessFluxFitting (9 cells), ADP1BERDLAnalysis (22 cells), ADP1BERDLCrossSampleAnalysis (6 cells). Net diff: +531 / -2153 — massive simplification.

**Key patterns / lessons:**

1. **`_legacy = NotebookUtil()` shim emerged organically** for cells using KBase API methods with no NotebookSession equivalent yet (`get_media`, `constrain_objective_to_fraction_of_optimum`, `get_msgenome_from_dict`, `create_map_html2`). Cells initialize `session` for cache I/O (migrated) AND `_legacy` for the un-wrapped APIs (deferred). Clean pragmatic pattern; preserve until Phase 4d wraps these in KBUtilLib.

2. **No new util.py functions needed.** The 3 notebooks were thin enough that just replacing `util.save/load` with `session.cache.save/load` was the bulk of the work. Phase 4a's ported helpers + 4b's three additions covered everything. Surprising — implies the per-project god-class util.py was mostly *cache shim*, not unique business logic.

3. **Cross-notebook cache references work in practice.** CrossSampleAnalysis loads `ADP1BERDLFoldChangeAnalysis/...`-prefixed keys saved by another notebook; loaded cleanly because the cache is project-wide (Phase 3.5 invariant proved). The `notebook_name=` argument from the legacy API is genuinely unnecessary in the new design.

4. **Free side-benefit cleanups.** The agent noticed and removed hardcoded `sys.path.insert(0, '/Users/chenry/Dropbox/Projects/ModelSEEDpy')` lines (cells were assuming a developer's local layout). Available via venv on emailmac/h100 too. Worth scanning for these in every migration.

5. **nbformat version bumps.** One notebook needed upgrade from 4.4 → 4.5 to validate cell IDs. Future migrations should anticipate this — add to migration recipe in §7.1.

**Deferred APIs needing KBUtilLib wrappers** (candidates for a Phase 3.5-ii follow-up before Phase 4d):
- `get_media()` (KBWS) → `session.media.get_kbase(id)` or similar
- `constrain_objective_to_fraction_of_optimum()` (MSFBAUtils) → util.py port or KBUtilLib helper
- `get_msgenome_from_dict()` (KBPLMUtils) → util.py port or KBUtilLib helper
- `create_map_html2()` (EscherUtils) — existing `generate_escher_map()` lacks badges/advanced features

**Process notes:**
- Bumped `--timeout` to 1800s (from 900s default). Dev took 1091s. The 900s budget would have been borderline with a 22-cell notebook in the mix. Recommend 1800s for any 3+ notebook task.
- `dev ✓ → review ✓ → merge ✓` first try (clean review). But auto-merge bug still required §8 manual merge.
- Reviewer made no fix-up commit (its branch was identical to main, not a sibling with fixes). Simpler manual-merge case than Phase 3.5.

### 2026-05-05 — Phase 4-bootstrap (kbu CLI)

Implemented the `kbu init-notebook` CLI to solve the bootstrap chicken-egg problem: Phase 4a dropped `sys.path` setup from `util.py`, but running migrated notebooks fails with `ModuleNotFoundError` because `kbutillib` isn't importable without a properly configured venv. The chosen solution is per-notebook-project venvs with `pip install -e` of sibling repos, orchestrated by a new `kbu` CLI.

**Bootstrap chicken-egg solution**: The `bin/kbu` shell wrapper (~5 lines) sets `PYTHONPATH` to `KBUtilLib/src` before invoking `python -m kbutillib`, so it works on a fresh machine without any prior installation. Symlink it once into `~/.local/bin/` and it's available everywhere.

**Machine alias resolution**: 4-level fallback chain: (1) AgentForge `config.load_config()` Python import, (2) direct YAML parse of `~/.agentforge/config.yaml`, (3) hardware UUID match against `machine_configs/*.yaml`, (4) interactive prompt. The AgentForge import path catches `pydantic.ValidationError` and falls through gracefully. Hardware UUID extraction uses `ioreg` on macOS and `/etc/machine-id` on Linux.

**Per-machine config**: `machine_configs/_default.yaml` defines the baseline (Python version, editable installs, notebook deps). Per-machine overrides (e.g., `emailmac.yaml`, `h100.yaml`) deep-merge on top. Dropbox conflicted-copy files (`*(Conflict*).yaml`) are excluded from all glob operations.

**Smart-merge for util.py**: When `--force` is used and `notebooks/util.py` already exists, the template header above the `# === project-specific helpers below ===` marker is replaced while preserving all custom code below it. Without `--force`, existing `util.py` is never touched. Without the marker in the existing file, `--force` refuses with a clear error.

**Kernel pinning policy**: By default, only `kbu.nb-*` kernels and unset kernels are overwritten. External kernels (e.g., KBase narrative kernel) are preserved unless `--force` is specified.

**Implementation notes**:
- All subprocess calls (venvman, pip, ipykernel) are mocked in tests via `unittest.mock.patch`. 40 tests total covering the full resolution chain, smart-merge edge cases, slugification, idempotence, broken venv detection, and kernel pinning policy.
- Jinja2 added as a main dependency for template rendering.
- `jupyter` and `ipykernel` added to the `[dependency-groups] notebook` group in `pyproject.toml`.
- The `[project.scripts]` entry renamed from `KBUtilLib` to `kbu` pointing at the same `__main__:main` entry point (now a Click group instead of a single command).
