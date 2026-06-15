# PRD: kbu-beril-augmentation

> Augment KBase's BERIL-research-observatory with a KBUtilLib-owned skill set +
> deploy system, so BERIL builds and runs metabolic-modeling notebooks the way
> Chris's lab actually works. KBUtilLib stops being a notebook co-scientist and
> becomes a science-method + skill-deployment layer that BERIL consumes.

## Problem Statement

Chris has repeatedly tried to build a notebook co-scientist inside KBUtilLib
(the `kbu-start` / `kbu-plan` / `kbu-build` / `kbu-migrate` skills + `kbu-sub-*`
subagents + a plan/build/run state machine). It has underperformed. The
orchestration of an AI research workflow was never KBUtilLib's comparative
advantage — the lab's value is in its **KBase/ModelSEED science methods** (FBA,
gapfilling, reconstruction) and a **hard-won notebook-construction discipline**
(the `%run util.py` + provenanced-cache pattern).

Meanwhile, KBase has invested in **BERIL-research-observatory** (the "Microbial
Discovery Forge") — a mature, Claude-Code-native AI co-scientist with its own
skills (`/berdl`, `/literature-review`, `/synthesize`, `/submit`, …), project
model (`projects/{id}/` + `beril.yaml`), and shared corpus (`docs/pitfalls.md`,
`docs/discoveries.md`). BERIL has **no metabolic-modeling skill, no FBA skill,
and no opinion on how notebooks are constructed** — exactly the gap KBUtilLib
should fill.

The problem: there is no clean, durable way to teach BERIL the lab's modeling
methods and notebook discipline, and no way to distribute that augmentation to
other KBase users. Worse, `beril start` re-checks-out the latest release tag on
every launch, so naive in-tree edits to BERIL are reverted or block the
checkout.

## Solution

Retire KBUtilLib's in-house co-scientist. Build a **CRAFT-style deploy system**
in KBUtilLib that installs a small set of KBUtilLib-owned skills + preferences
into a vanilla, release-tracking BERIL clone, **without ever forking BERIL or
fighting its release re-checkout**.

Concretely, KBUtilLib gains:

1. A **`kbu beril` deployer** (`install` / `configure` / `doctor <BERIL_ROOT>`)
   that copies the skill bundle into `<BERIL_ROOT>/.claude/skills/` as
   **untracked** dirs (which survive `beril start`'s release checkout),
   pip-installs `kbutillib` into the BERIL environment so its functions are
   importable in-notebook, and renders an editable preferences file.

2. A **skill bundle** of three units:
   - **`/kbu`** — a manually-invoked primer that loads the preferences file and
     briefs the active modeling guidelines into the session.
   - **`kbu-notebook`** — how BERIL builds modeling notebooks (the `%run util.py`
     + provenanced-cache + cell-independence discipline), **superseding the
     broken `jupyter-dev` skill**.
   - **`kbu-fba`** — how BERIL runs the full metabolic-modeling arc
     (reconstruct → gapfill → analyze), encoding the lab's FBA conventions.

3. A **graduated execution policy** baked into `kbu-notebook` + `kbu-fba` so the
   notebook-builder (BERIL) keeps its hands free for test-driven development but
   does not waste time/compute plowing full, expensive, or uncertain runs without
   user sign-off.

4. A small change to **`NotebookSession.for_notebook()`** so its provenanced
   cache anchors cleanly onto BERIL's `projects/{id}/` layout.

BERIL remains the brain (orientation, review, synthesis, the shared corpus);
KBUtilLib supplies the modeling methods and the notebook discipline.

This PRD is **A** of two companion PRDs. **B** (`kbu-harness`) — the
independent execution/debugging harness with rsync to/from BERIL — is designed
separately and ships after A.

## User Stories

1. As a lab member, I want to run `kbu beril install <BERIL_ROOT>` and have my
   modeling skills appear in BERIL, so I don't hand-copy skill files.
2. As a lab member, I want the install to survive `beril start`'s release
   re-checkout, so my augmentation isn't silently reverted when BERIL upgrades.
3. As another KBase user, I want to `pip install kbutillib` and run
   `kbu beril install` against my own BERIL deployment, so I can adopt the lab's
   modeling discipline without Chris's Dropbox or machines.
4. As a lab member, I want `kbu beril doctor <BERIL_ROOT>` to tell me whether the
   skills are deployed, `import kbutillib` works in the BERIL kernel, and the
   installed version matches, so I can diagnose a broken augmentation fast.
5. As a lab member, I want `kbu beril install` to pip-install/upgrade `kbutillib`
   into the BERIL environment idempotently (skip if the version already matches,
   fall back to `--break-system-packages` under PEP-668), so modeling functions
   are importable in notebooks without manual setup.
6. As a researcher in a BERIL session, I want to type `/kbu` and have BERIL load
   my preferences (default media, reconstruction template, organism focus,
   solver/gapfill defaults, execution thresholds) and brief the modeling
   guidelines, so the session is primed to work the way I work.
7. As a researcher, I want `kbu-notebook` to make BERIL build notebooks with a
   single `util.py` (all imports + the `NotebookSession`) and every cell starting
   `%run util.py`, so no cell carries import boilerplate.
8. As a researcher, I want every cell to be independently executable — load
   inputs from cache/files, analyze with cache-as-you-go, save results to cache +
   output files — so an interrupted cell loses no completed work.
9. As a researcher, I want a notebook's project directory to be portable to
   another compute environment and re-executable there, so I can move heavy runs
   off my laptop.
10. As a researcher, I want `kbu-fba` to cover the whole arc — build a model from
    a genome, gapfill it (including comprehensive gapfilling), then run FBA / FVA
    / essentiality — so FBA guidance doesn't dead-end when I start from a genome.
11. As a researcher, I want `kbu-fba` to use `MSFBAUtils.run_fva` and never
    `cobra.flux_variability_analysis` (which is broken), so I don't get wrong
    FVA results from canonical-but-broken tooling.
12. As a researcher, I want BERIL to execute cheap, certain cells freely for TDD,
    so building a notebook isn't crippled by a no-execution rule.
13. As a researcher, I want BERIL to run only a **sample** and then stop and
    consult me when a step is slow, algorithmically uncertain, large fan-out, or
    compute/cost-intensive, so it never burns hours doing the wrong thing the
    wrong way.
14. As a researcher, I want the full expensive run to happen only after I sign
    off, and to decide each time whether it runs in BERIL or (later) the harness,
    so I keep control of compute.
15. As a researcher, I want `kbu-notebook`/`kbu-fba` to defer to BERIL's existing
    project skeleton (`projects/{id}/notebooks|data|figures`, `beril.yaml`), so
    my conventions augment BERIL rather than collide with it.
16. As a maintainer, I want the deployed skills to be untracked and the config
    files gitignored, so nothing I add ever blocks or is reverted by BERIL's
    release checkout.
17. As a maintainer, I want `jupyter-dev`'s conflicting directives removed at the
    home-repo source (not the deployed copy), so the next `claude-skills sync`
    doesn't overwrite the fix.

## Implementation Decisions

### Repo & distribution model
- **Track upstream BERIL; never fork.** The augmentation lives entirely outside
  BERIL's tracked tree: skills deploy as **untracked** dirs under
  `<BERIL_ROOT>/.claude/skills/`, and config writes to **gitignored** files
  (`.env`, `.claude/settings.local.json`, `.claude/kbu/`). Verified: `git checkout
  <tag>` neither removes untracked files nor is blocked by them, and BERIL
  gitignores `.env`/`*.env`/`.claude/settings.local.json`.
- **KBUtilLib owns a CRAFT-style deployer** — it does NOT join CRAFT as a
  platform submodule. CRAFT's family is research-artifact production (adversarial
  review, paper/presentation drafting); KBUtilLib's is science methods — a
  distinct concern (KBase already treats `beril-atlas` as "not in CRAFT" for the
  same reason). KBUtilLib borrows CRAFT's *conventions* (untracked `.claude/skills`
  deploy, additive gitignored config, `doctor`/`configure` surface, per-skill
  versioning) without its machinery.
- This is a **separate distribution channel** from `claude-skills sync` (which is
  Chris's personal cross-machine deploy to `~/.claude/commands/` over Dropbox
  markers). The BERIL deployer is pip/pipx-installable and targets a BERIL root,
  so other KBase users can use it. Do not conflate the two.

### Module 1 — `kbu beril` deployer (new CLI command group)
- New subcommand group on the existing `kbu` CLI: `kbu beril install`,
  `kbu beril configure`, `kbu beril doctor`, each taking `<BERIL_ROOT>`.
- Modeled on CRAFT's `src/craft/cli.py` (`cmd_install_platform`, `cmd_configure`,
  `cmd_doctor`) shape: per-step `── name`, ✓/✗ result lines, summary block,
  return codes `0` all-ok / `1` partial / `2` none.
- **`install`**:
  - Validate `<BERIL_ROOT>` is a BERIL deployment (presence of `PROJECT.md` +
    `.claude/skills/`).
  - Copy the skill bundle (3 skill dirs) into `<BERIL_ROOT>/.claude/skills/` as
    untracked dirs. Idempotent (overwrite-in-place on re-install).
  - Render `<BERIL_ROOT>/.claude/kbu/preferences.md` from the template **only if
    absent** (never clobber a user-edited preferences file; re-install leaves an
    existing one untouched and says so).
  - **pip-install/upgrade `kbutillib` into the active BERIL environment** with
    guards: detect the target interpreter/env, skip if the installed version
    already matches (idempotent), fall back to `--break-system-packages` on
    PEP-668 lock, surface a clear message on conda/hub envs. Print the exact pip
    command it runs.
- **`configure`**: **OUT OF SCOPE for PRD A** (confront round 1, stall #5).
  BERIL already owns provider/model configuration (Vertex / CBORG / `.env` via its
  own `beril setup`); the KBUtilLib skills are markdown + a pip install and need no
  provider config. The deployer surface for PRD A is **`install` + `doctor` only**.
  Do not implement `configure`.
- **`doctor`**: report (a) each skill dir present under `.claude/skills/`,
  (b) `import kbutillib` succeeds in the target kernel, (c) installed `kbutillib`
  version matches the deployer's, (d) `preferences.md` present. Pure read; never
  mutates.

### Module 2 — Skill bundle (new in-tree dir in KBUtilLib)
- Lives in KBUtilLib's tree (e.g. `src/kbutillib/beril/skills/`); the deployer
  copies from here. Three units, each a `SKILL.md` + references:
  - **`/kbu`** (primer, manually invoked): reads `.claude/kbu/preferences.md`
    into the session, briefs the active guidelines, points at `kbu-notebook` /
    `kbu-fba`. Does NOT patch BERIL's `/berdl_start` onboarding.
  - **`kbu-notebook`** (auto-discoverable): the notebook-construction discipline
    (below). Supersedes `jupyter-dev`.
  - **`kbu-fba`** (auto-discoverable): the modeling arc (below).
- **Preferences** (`.claude/kbu/preferences.md`, editable markdown template):
  user identity, default media, default reconstruction template, organism focus,
  solver/gapfill defaults, notebook-convention toggles, and the **execution
  thresholds** (runtime threshold, fan-out count) used by the graduated execution
  policy. Markdown so the agent reads + follows it directly.

#### `kbu-notebook` directives (canonical — KEEP from current kbu)
- A single **`util.py`** per notebook dir (modeled on
  `ModelingLOE/notebooks/gapfill_loe/util.py`) holding ALL imports + utility
  functions + creating/initializing the kbu `NotebookSession` (named `session`).
- **Every cell starts with `%run util.py`**; cells carry no import boilerplate.
- **Every cell is independently executable**: load needed inputs from cache/files
  → analyze with **cache-as-you-go** (interrupt-safe partial work) → on
  completion save results to cache + output files.
- **Portability**: the project dir is movable to another compute env and
  re-executable there (the `~/.kbu-sys-paths` sys.path bootstrap in `util.py` is
  the existing mechanism — preserve it).
- **Coexistence with BERIL (audit-derived precedence):** BERIL owns the project
  skeleton/dirs/artifact-tracking; kbu owns in-notebook construction (`util.py` +
  `%run`), caching, and object provenance. `util.py` lives in the BERIL notebook
  dir; `.kbcache/` (gitignored, derived) anchors beside it; path constants point
  at BERIL's `data/`. Nothing in BERIL's tree is renamed or moved. Scope
  `kbu-notebook`/`kbu-fba` to **local COBRA/MSModelUtil modeling notebooks**;
  leave BERIL's Spark/query notebooks on their existing convention.
- Backed by the kept `kbutillib/notebook/` infra: `NotebookSession.for_notebook`,
  `.cache` (save/load/`@cached`, serializers for MSModelUtil/cobra Model/MSMedia/
  MSExpression/MSGenome/dataframe), `.experiments`, `.vectors`, and the Manifest
  **provenance-DAG** methods (`what_writes`/`what_reads`/`stale`). Do NOT use the
  discarded org/run-state Manifest role (`Manifest.render()` dashboard).

#### `kbu-fba` directives (full arc)
- Build: `MSReconstructionUtils.build_metabolic_model` from a genome.
- Gapfill: `gapfill_metabolic_model` and `run_comprehensive_gapfill_on_model`
  (the two-stage comprehensive mode).
- Analyze: `MSFBAUtils.run_fba` (pFBA by default), `MSFBAUtils.run_fva`
  (**never** `cobra.flux_variability_analysis` — it is broken; `run_fva` is the
  deliberate in-house workaround), `set_media`, `set_objective_from_string`
  (supports `MAX{rxn}` / `MIN{rxn}` syntax), essentiality.
- BERDL access (where needed) routes through `KBBERDLUtils` (`query`,
  `get_database_list`, `get_table_columns`, access-aware) — do not reinvent it.

#### Graduated execution policy (in BOTH `kbu-notebook` and `kbu-fba`)
Followed by BERIL during build AND by the harness later. Before running a
cell/notebook, the agent classifies it:
- **🟢 Cheap & certain** (fast, deterministic, well-understood): **run freely** —
  this is the TDD loop; never tie the builder's hands here.
- **🟡 Run-a-sample-then-consult**, triggered by **ANY** of: estimated runtime
  over the preferences threshold; agent **self-flagged** algorithmic uncertainty;
  large fan-out over the preferences count; or compute/cost intensity (GPU, heavy
  LP/FVA, remote compute). Action: run a **sample** at reduced scope (e.g. 1
  organism, capped iterations, one medium), **cache** the result, then **STOP and
  consult** the user.
- **🔴 Full execution**: only after explicit user sign-off. At each checkpoint the
  **user decides where it runs** (BERIL-here vs the harness) — no encoded default.
- Thresholds live in `preferences.md` and are tunable. The agent must estimate
  runtime and self-flag uncertainty. Because of cache-as-you-go, the validated
  sample work carries forward into the full run.
- BERIL is encouraged to be **conservative**: when in doubt, sample and report
  the blocker rather than plow ahead.

### Module 3 — `NotebookSession` BERIL mapping (modify `notebook/session.py`)
- `NotebookSession.for_notebook(__file__, project_name=...)` must resolve its
  storage root + `.kbcache/` onto a BERIL `projects/{id}/`-shaped layout (anchor
  beside `util.py` inside the BERIL project's notebook dir), not via
  `kbu-project.toml`. Preserve the existing `__file__`-anchored, cwd-independent
  behavior. Keep the `~/.kbu-sys-paths` portability bootstrap.
- Split the Manifest: keep the provenance-DAG reads; do not require the
  org/run-state (`kbu-project.toml`/subproject) machinery to be present.

### Module 4 — Supersede `jupyter-dev` (home-repo source edit)
- Remove `jupyter-dev`'s directives that conflict with `kbu-notebook`:
  forbidding `%run util.py`, mandating `from util import session`, the stale
  `NotebookSession(name=, notebook_folder=)` signature, and the subprojects/
  `kbu init-notebook`/`nboutput/` wiring. Point users to `kbu-notebook`.
- **Edit at the home-repo source**, not the deployed `.claude/commands/` copy
  (it is `claude-skills sync`-managed; deployed edits are overwritten). Look up
  `jupyter-dev`'s `home_repo`/`home_path` in the skill registry before editing.

### Confront round 1 — folded resolutions (concrete values for autonomous build)

- **Interpreter discovery (stalls #1, #2):** the deployer selects the target Python
  by this rule, in order: `<BERIL_ROOT>/.venv/bin/python` if it exists; else the
  interpreter currently running the deployer; else `python3` on PATH. It prints the
  chosen path and records it in `<BERIL_ROOT>/.claude/kbu/install.json` so `doctor`
  reuses the same interpreter. `doctor`'s import check runs
  `<py> -c "import kbutillib, importlib.metadata as m; print(m.version('KBUtilLib'))"`.
- **Version probe (stall #15):** installed version = `importlib.metadata.version('KBUtilLib')`
  (distribution name is `KBUtilLib`) run under the chosen interpreter; ImportError /
  PackageNotFound ⇒ treat as not installed. `install` skips the pip step when the
  installed version already equals the deployer's `KBUtilLib` version.
- **PEP-668 fallback (stall #1):** on a PEP-668 "externally-managed" error, retry the
  pip install once with `--break-system-packages`. (Free-critique option: gate this
  behind `--allow-break-system-packages`; default-on is acceptable for v1.)
- **BERIL root validation (stall #3):** valid iff `<BERIL_ROOT>/.claude/skills/` exists
  AND `<BERIL_ROOT>/PROJECT.md` exists (PROJECT.md is BERIL's real repo marker, used by
  `beril start`'s `_find_repo_root`). Warn — do not fail — if `<BERIL_ROOT>/.git` is absent.
- **SKILL.md frontmatter (stall #6):** use BERIL's real Claude Code schema — `name`,
  `description` (containing a "Use when …" trigger for auto-discovery), `allowed-tools`;
  `/kbu` additionally sets `user-invocable: true`. No invented keys.
- **Skill scoping (stall #17, dropped-mechanism):** scope to local COBRA/MSModelUtil
  modeling notebooks **via the `description` text** (e.g. "Use when building or running
  local COBRA/MSModelUtil metabolic-modeling notebooks"). There is NO domain-tag filter —
  Claude auto-discovery is description-driven.
- **Preferences schema (stall #7):** `.claude/kbu/preferences.md` contains a fenced
  ```yaml``` block with keys: `execution.runtime_threshold_seconds`,
  `execution.fanout_threshold`, `sampling.reconstruction_n`, `sampling.gapfill_media_n`,
  `sampling.gapfill_max_solutions`, `sampling.fva_reaction_n`, `solver.name`,
  `gapfill.comprehensive` (bool), `organism.focus`, `media.default`, `version`. The
  `/kbu` SKILL.md instructs reading this YAML block into the session.
- **util.py skeleton (stalls #9, #18):** `kbu-notebook` SKILL.md inlines the minimal
  `util.py` skeleton verbatim — `~/.kbu-sys-paths` sys.path bootstrap, the full import
  block (numpy/pandas/cobra-guarded + `from kbutillib.notebook import NotebookSession`
  and helpers/schema), `session = NotebookSession.for_notebook(__file__, project_name=...)`,
  and root-anchored path constants — modeled on `ModelingLOE/notebooks/gapfill_loe/util.py`.
- **FBA sample defaults (stall #10):** reconstruction sample = 1 genome; gapfill sample
  = 1 medium with `max_solutions=1`; FVA sample = top-10 reactions by |flux|. All
  overridable via the `sampling.*` preferences keys. The "never `cobra.flux_variability_analysis`,
  use `MSFBAUtils.run_fva`" rule (stall #11) is **guidance text** in `kbu-fba`'s SKILL.md,
  not a runtime guard.
- **Module 3 is confirm-and-test, not change (stalls #12, #13):** `NotebookSession.for_notebook()`
  already anchors `.kbcache/` at `Path(notebook_file).parent/'.kbcache'` and `Manifest`
  already does not reference `kbu-project.toml`. PRD A makes **no behavior change** here;
  it only adds tests proving the BERIL-path case works. Any org/run-state work is deferred
  to PRD B.
- **Module 4 is a tracked cross-repo task (stalls #8, #14):** resolve `jupyter-dev`'s
  `home_repo`/`home_path` from `~/Dropbox/Projects/ClaudeCommands/state/skill_registry.json`,
  edit the source there, and re-sync — never edit the deployed `.claude/commands/` copy.
  This task lands in a separate phase with its own acceptance check; it does not block the
  deployer or skill-bundle phases.
- **Test fixtures (stalls #4, #16):** the fake BERIL root is a temp dir with `PROJECT.md`
  + `.claude/skills/` + `git init` + one empty commit + tag `v0`; the checkout-survival
  test runs `git checkout v0` and asserts the three skill dirs and `.claude/kbu/` survive.
  Untracked assertions use `git -C <BERIL_ROOT> status --porcelain` (skip with a notice if
  no `.git`).

### Confront round 1 — adopted advisories (from FREE CRITIQUE)

- **Time-estimate rubric:** the graduated-execution runtime classification has
  concrete defaults — `<5s` 🟢, `5–60s` 🟡, `>60s` 🔴 — so
  `execution.runtime_threshold_seconds` defaults to `60` (the 🟡→🔴 boundary; the
  🟢→🟡 boundary is `5`). These are tunable in `preferences.md`.
- **`install --dry-run`:** `kbu beril install` accepts `--dry-run` that prints the
  planned actions and resolved paths (chosen interpreter, skill dirs to copy,
  preferences render-or-preserve, pip command) **without** running pip or copying
  files — used by CI tests for a side-effect-free assertion of intent.
- **`.kbcache/` coexistence note:** `kbu-notebook` documents that `.kbcache/` is
  gitignored and derived; it must not be committed or double-tracked by BERIL's
  artifact/backup routines. Curated outputs go to BERIL's `data/`/`figures/`; the
  cache is regenerable and stays local.

## Testing Decisions

Tests should assert **external behavior**, not implementation detail. Three
modules get coverage (prior art: KBUtilLib's existing `tests/` + the composition
smoke-test fixtures `mini_model` / `shared_env`).

- **Module 1 — deployer** (highest value; it's the automation other users rely
  on). Against a **temp fake BERIL root** (a dir with `PROJECT.md` +
  `.claude/skills/` + a git repo): assert (a) the 3 skill dirs land under
  `.claude/skills/` and are untracked by the fake BERIL git; (b) `preferences.md`
  is rendered when absent and **left untouched** when already present; (c)
  re-install is idempotent; (d) pip-install is skipped when the installed version
  already matches (mock the version probe); (e) `doctor` reports a clean install
  green and a missing-skill/failed-import red. Simulate `git checkout <tag>` and
  assert deployed skills + gitignored config survive.
- **Module 3 — NotebookSession BERIL mapping**: given a BERIL
  `projects/{id}/notebooks/<nb>/util.py`-shaped temp dir, assert `for_notebook()`
  resolves the storage root + `.kbcache/` to the right place and the Manifest
  provenance reads work without `kbu-project.toml` present.
- **Module 2 — skill content smoke**: assert the deployed bundle is well-formed —
  each `SKILL.md` parses (valid frontmatter), the `util.py` template imports
  cleanly, `preferences.md` template is present and contains the threshold keys
  the execution policy reads.

## Out of Scope

- **PRD B — `kbu-harness`**: the per-project container repo, rsync to/from BERIL,
  the design-deploy skill (step 1 of the workflow), in-harness execution
  (ai-cowork / kbu-run), and the MD dev-log rule. Designed and shipped separately
  after A.
- Contributing the KBUtilLib skills upstream into KBase's BERIL repo (we deploy
  into a vanilla clone; upstream contribution is a future, different goal).
- Joining CRAFT as a platform submodule.
- Changing BERIL's `/berdl_start` onboarding or any tracked BERIL file.
- BERIL's Spark/query notebook conventions (kbu skills target local
  COBRA/MSModelUtil modeling notebooks only).
- Rewriting the kept `kbutillib/notebook/` engine internals beyond the
  `for_notebook()` BERIL-root mapping.

## Further Notes

- **Grounding artifacts**: the keep/discard + conflict-precedence audit is at
  `agent-io/audits/2026-06-13-kbu-vs-beril-directive-audit.md`. CRAFT
  (`~/Dropbox/Projects/craft`) is the reference deployer; BERIL
  (`~/Dropbox/Projects/BERIL-research-observatory`) is the deploy target.
- **Stripping the old co-scientist** (`kbu-start`/`kbu-plan`/`kbu-build`/
  `kbu-migrate` + `kbu-sub-*`) is part of this initiative but is a clean-up task
  the audit enumerates; it can ride along in this PRD's phases or be a fast
  follow. The kept notebook engine + science methods are untouched by the strip.
- The canonical `util.py` to model is
  `~/Dropbox/Projects/ModelingLOE/notebooks/gapfill_loe/util.py`.
- Privacy/runtime: the worker IS Claude (Max plan) — skills must not call the
  Anthropic API or subprocess `claude`.

## Acceptance Criteria

1. `kbu beril install <BERIL_ROOT>` validates the root by presence of both `.claude/skills/` and `PROJECT.md`, and exits non-zero with a clear message if either is missing; it warns but does not fail when `<BERIL_ROOT>/.git` is absent.
2. install copies exactly three skill directories — `kbu`, `kbu-notebook`, `kbu-fba` — into `<BERIL_ROOT>/.claude/skills/`, overwriting in place on re-install (idempotent).
3. install renders `<BERIL_ROOT>/.claude/kbu/preferences.md` from the template only when absent; an existing `preferences.md` is left byte-for-byte unchanged and the command reports it was preserved.
4. install selects the target interpreter as `<BERIL_ROOT>/.venv/bin/python` if present, else the interpreter running the deployer, else `python3` on PATH; it prints the chosen interpreter path and records it in `<BERIL_ROOT>/.claude/kbu/install.json`.
5. install pip-installs/upgrades the `KBUtilLib` distribution into the chosen interpreter, skips the pip step when the installed version already equals the deployer's version, and retries once with `--break-system-packages` on a PEP-668 externally-managed error.
6. The deployer reads the installed version via `importlib.metadata.version('KBUtilLib')` run under the chosen interpreter, treating ImportError/PackageNotFound as not-installed.
7. The PRD-A deployer exposes only `install` and `doctor`; there is no `configure` command.
8. `kbu beril doctor <BERIL_ROOT>` reports each of the following as ✓/✗: the three skill dirs present under `.claude/skills/`, `import kbutillib` succeeds under the chosen interpreter, installed version matches the deployer, and `preferences.md` present; it returns 0 only when all are ✓, non-zero otherwise; it never mutates the BERIL tree.
9. Each deployed `SKILL.md` has valid Claude Code frontmatter with `name`, a `description` containing a "Use when …" trigger, and `allowed-tools`; `/kbu`'s frontmatter additionally sets `user-invocable: true`.
10. The `kbu-notebook` and `kbu-fba` skill descriptions scope them to local COBRA/MSModelUtil modeling notebooks; no domain-tag or filter mechanism is added.
11. `<BERIL_ROOT>/.claude/kbu/preferences.md` contains a fenced YAML block with keys `execution.runtime_threshold_seconds`, `execution.fanout_threshold`, `sampling.reconstruction_n`, `sampling.gapfill_media_n`, `sampling.gapfill_max_solutions`, `sampling.fva_reaction_n`, `solver.name`, `gapfill.comprehensive`, `organism.focus`, `media.default`, and `version`; the `/kbu` SKILL.md instructs reading this block into the session.
12. `kbu-notebook`'s SKILL.md inlines a minimal `util.py` skeleton (`~/.kbu-sys-paths` bootstrap, full import block, `session = NotebookSession.for_notebook(__file__, project_name=...)`, root-anchored path constants) and states the every-cell `%run util.py` + independent-cell + cache-as-you-go directives, mapped onto BERIL's `projects/{id}/` dirs.
13. `kbu-fba`'s SKILL.md states the full reconstruct→gapfill→analyze arc, mandates `MSFBAUtils.run_fva` and forbids `cobra.flux_variability_analysis` as guidance text, and documents the graduated-execution sample defaults (reconstruction=1 genome, gapfill=1 medium with `max_solutions=1`, FVA=top-10 reactions) overridable via the `sampling.*` preferences keys.
14. Both `kbu-notebook` and `kbu-fba` encode the graduated execution policy: 🟢 run cheap/certain cells freely; 🟡 on ANY of {est. runtime over `execution.runtime_threshold_seconds`, self-flagged algorithmic uncertainty, fan-out over `execution.fanout_threshold`, compute/cost intensity} run a sample, cache it, and stop to consult; 🔴 full run only after user sign-off with the user choosing where it runs.
15. PRD A makes no behavior change to `NotebookSession.for_notebook()` or `Manifest`; a test asserts `for_notebook()` anchors `.kbcache/` beside a `util.py` at a BERIL `projects/{id}/notebooks/<nb>/` path and that Manifest provenance reads (`what_writes`/`what_reads`/`stale`) work without `kbu-project.toml` present.
16. The deployer test runs against a temp fake BERIL root (`PROJECT.md` + `.claude/skills/` + `git init` + empty commit + tag `v0`) and asserts: the three skill dirs land and are untracked (`git status --porcelain`), `preferences.md` renders-if-absent and is never clobbered, re-install is idempotent, the pip step is skipped when a mocked version probe matches, and after `git checkout v0` the skill dirs and `.claude/kbu/` survive.
17. The skill-bundle smoke test asserts each `SKILL.md` parses with valid frontmatter, the `util.py` template imports cleanly, and `preferences.md` contains the threshold keys named in AC #11.
18. Module 4 (jupyter-dev supersede) is implemented as a separate phase that resolves `jupyter-dev`'s `home_repo`/`home_path` from `ClaudeCommands/state/skill_registry.json`, edits the source there (not the deployed `.claude/commands/` copy), and verifies the conflicting directives (`%run`-forbid, `from util import session`, stale signature, subprojects wiring) are removed.
19. `kbu beril install --dry-run` prints the planned actions and resolved paths (chosen interpreter, skill dirs, preferences render-or-preserve, pip command) and performs no pip install and no file copy; a test asserts the dry run leaves a fake BERIL root unchanged.
20. The graduated-execution runtime rubric defaults are encoded in `preferences.md` and the skills: `<5s` 🟢, `5–60s` 🟡, `>60s` 🔴, with `execution.runtime_threshold_seconds` defaulting to `60`.
