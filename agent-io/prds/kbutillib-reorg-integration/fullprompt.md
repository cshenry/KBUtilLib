# PRD — KBUtilLib Reorg Integration (`feature/reorg-api-mcp-explore`)

## Problem Statement

An external contributor (VibhavSetlur) has produced a large reorganization branch,
`vibhav/feature/reorg-api-mcp-explore` (fork of `cshenry/KBUtilLib`), that restructures
KBUtilLib from a flat `src/kbutillib/*_utils.py` module layout into a `domains/` +
`core/` + `interfaces/` + `agents/` package hierarchy, and layers on a genuinely new
**capability-registry-with-many-transports** architecture (Python-import, CLI, MCP,
HTTP). The branch is 361 files / +43,120 / −4,139 relative to its base `aa98d89`.

Chris wants to adopt this branch as the new KBUtilLib mainline. Three complications:

1. **The branch does not contain our recent work.** It was forked at `aa98d89`, before
   the Remote LP-Solver stack and the ARGO httpx-0.28 fix landed. `main` already carries
   the entire LP-solver service+client+`remote_solve`; the ARGO fix (`ece70da`) is
   `wip`-only. This delta must be re-landed onto the reorg or it is lost.
2. **The reorg deletes the flat submodule shims**, so every `from kbutillib.<x>_utils
   import …` deep import across the ecosystem breaks — including a flat import baked into
   a *shipped* BERIL skill and several notebook-workspace `util.py` files. The break is
   insidious because top-level `__init__.py` re-exports are `try/except → None`, so a
   *missed* re-export degrades to a confusing `NoneType` call-site error, not a clean
   `ImportError`.
3. **The reorg bundles net-new science** (cheminformatics/verab, predictive-thermo,
   network-expansion) and **unauthenticated MCP+HTTP servers** into the same branch as
   the structural change.

Downstream we cannot grill anyone at build time: the migration must be fully specified
now, with a safety net so nothing shatters silently.

## Solution

Adopt Vibhav's branch **whole, as-is** (all three layers) as the new mainline, then:

- **Re-land our delta** — the LP-solver service layer + client + `remote_solve` wiring +
  ARGO httpx fix — into the new `domains/`/`services/` locations.
- **Net the downstream** — fix the in-repo BERIL/kbu skill sources and known consumer
  `util.py` reach-ins, and add a **flat-submodule deprecation shim** so legacy
  `from kbutillib.<x>_utils import …` imports *warn and re-export* instead of failing.
- **Validate** — the full test suite plus a BERIL `kbu-fba` end-to-end smoke, the `kbu
  model` verb tree, and the KING `kbu model --help` verify, all pass.
- **Land** the integration onto `main`, merge `main → wip` so it is live on the local
  parking repo, and re-sync the skills.

Chris's decisions this session (binding):
- **Scope = whole branch as-is.** Do not split verab/cheminformatics or the servers out.
- **Servers = accept as-is**, with one non-reversing refinement: default-bind the MCP/HTTP
  servers to loopback (`127.0.0.1`) so starting the systemd unit does not immediately
  expose every capability on the network. Deliberate widening to `0.0.0.0` stays a config
  flip. Authentication is explicit follow-up debt, not in scope here.
- **Migration = fix known breaks + safety-net sweep** (deprecation shim + grep sweep),
  NOT a break-loudly posture, and NOT asking Vibhav to keep his flat shims.

## User Stories

1. As Chris, I want the reorg branch adopted as the new KBUtilLib mainline so the library
   gains the `domains/` structure and the capability-registry/transport architecture.
2. As Chris, I want my Remote LP-Solver service (`services/lp_solver/app.py`, `worker.py`,
   `deploy/`) preserved, because the reorg silently lacks that FastAPI service layer.
3. As Chris, I want the LP-solver client (`ms_remote_solver_utils`, `ms_remote_solve_utils`)
   and `remote_solve`/`remote_solver` facade wiring re-landed into the new
   `domains/modeling/` location with correct import paths.
4. As Chris, I want the ARGO httpx-0.28 proxy fix re-applied to the renamed
   `domains/ai/argo_utils.py`, and the `httpx[socks]>=0.28` dependency preserved, because
   the fix is inert without it.
5. As a BERIL modeling user, I want `session.kbu.fba.run_fva(...)`, `session.kbu.recon.
   build_metabolic_model(...)`, and the whole `session.kbu.*` idiom to keep working
   unchanged (the reorg updated `toolkit.py` in lockstep, so this is a preserve-and-verify
   story, not a rewrite).
6. As a BERIL modeling user, I want the shipped `kbu-fba` skill to stop importing a deleted
   flat module (`kbu-fba/SKILL.md:62` — `from kbutillib.kb_berdl_utils import KBBERDLUtils`)
   so the skill body does not instruct agents to write broken imports.
7. As a notebook author with existing `util.py` files that do `from kbutillib.ms_fba_utils
   import MSFBAUtils` (ModelingLOE, WatershedPhenotypeReplication, KBDatalakeApps,
   BVBRCHackathon, several NotebookWorkspaces), I want those imports to keep working with a
   deprecation warning rather than crash, so my notebooks do not break the day the reorg
   lands.
8. As a KING user, I want `kbu model reconstruct|gapfill|fba|fva|exec`, the `--json`
   envelopes, and the `MAX{}/MIN{}` objective DSL to keep working, and `kbu model --help`
   (the KING install-verify command in `~/king-apps/registry.json`) to keep succeeding.
9. As Chris, I want the `kbu` CLI name and all existing verb groups (`model`, `king`,
   `beril`, `harness`, `notebook`, `subproject`, `session`, `migrate`, …) preserved; the
   new `verab` group is additive, not a rename.
10. As a KBUtilLib maintainer, I want a guard test that every historically top-level-
    exported class (`MSFBAUtils`, `KBModelUtils`, `KBWSUtils`, `KBBERDLUtils`, …) resolves
    to a non-`None` object after the reorg, so a missed `__init__.py` re-export fails the
    build loudly instead of at a downstream call site.
11. As a KBUtilLib maintainer, I want the flat-submodule deprecation shims to be
    table-driven from the old→new mapping, so the back-compat surface is one small deep
    mechanism rather than dozens of hand-written stub files.
12. As a BERIL/notebook user, I want the core `pip install` to stay lean — rdkit / pickaxe
    / retrorules / fastapi / mcp must remain behind extras (`[cheminformatics]`/`[mcp]`/
    `[api]`/`[all]`), never core dependencies — so adopting the reorg does not force heavy
    chem/server deps onto every modeling install.
13. As Chris, I want the full test suite (the branch's ~2,500 tests plus our re-landed
    LP-solver tests, relocated to match the new `tests/` subdir layout) to pass, with any
    pre-existing xfails documented.
14. As Chris, I want a consumer-migration report enumerating every external
    `from kbutillib.<flat>` reach-in found across `~/Dropbox/Projects`, so I know what the
    deprecation shim is covering and what to clean up later.
15. As Chris, I want the integration landed on `main` and `main` merged into `wip` so the
    reorg is live on the local parking repo, and the fixed skill sources re-synced via
    `claude-skills sync`.
16. As Chris, I want the MCP/HTTP servers to default-bind to loopback so `kbu-api`/`kbu-mcp`
    and the poplar systemd unit do not expose the open API on the network by default.

## Implementation Decisions

### Integration end-state (what the mainline tree must equal)

The final mainline tree equals Vibhav's reorg (`e23caa5`) **plus** our re-applied delta.
The git mechanics (merge-commit vs. reset-and-re-land) are the developer's choice, but the
resulting tree and the four conflict hotspots below are binding. Merge-base is `aa98d89`;
`main` = `d1bcbcb` (has the LP-solver stack); `wip` = `main` + ARGO fix (`ece70da`) + a
merge commit.

**Delta to re-apply onto the reorg:**

1. **LP-solver service layer (reorg LACKS this — do not lose it):** re-add
   `src/kbutillib/services/lp_solver/app.py`, `worker.py`, and the `deploy/`
   (`enable_lp_solver.sh`, `lp-solver.service`). The shared
   `services/lp_solver/{__init__,job_store,solver_backends}.py` exist on both branches.
2. **LP-solver client → `domains/modeling/`:** place `ms_remote_solver_utils.py` and
   `ms_remote_solve_utils.py` under `domains/modeling/` (NOT flat root), and re-point their
   internal imports to the new domain/core paths.
3. **`remote_solve` / `remote_solver` facade wiring** in `toolkit.py`: re-apply the lazy
   `remote_solver` property + `remote_solve()` method against the reorg's repointed
   `toolkit.py` (same lazy-property paradigm; only import paths differ).
4. **Package exports** in `__init__.py`: re-add `RemoteSolveResult`, `remote_solve`,
   `MSRemoteSolverUtils`/`MSRemoteSolverUtilsImpl` against the reorg's rewritten
   `__init__.py`.
5. **ARGO httpx-0.28 fix** applied to `domains/ai/argo_utils.py` (the file was RENAMED from
   flat `argo_utils.py`, ~99% similar): the `proxies={…}` → `proxy=` httpx-0.28 API change.
6. **Dependencies/config:** preserve `httpx[socks]>=0.28` and the `lp_solver` extra in
   `pyproject.toml`, and the `remote_solver:` block in `config.yaml` (reorg did not touch
   `config.yaml`).

**Four conflict hotspots (resolve explicitly, in this order of risk):**
- `domains/ai/argo_utils.py` (rename-hidden; git may treat as add/delete — re-apply the fix
  by hand if rename detection misses it). HIGHEST risk.
- `src/kbutillib/__init__.py` (reorg +99/−69 new import paths; our +16 exports).
- `src/kbutillib/toolkit.py` (reorg +126/−61 repointed imports; our +41 property/method).
- `pyproject.toml` (both append to deps/extras/scripts regions).

### Old → new module mapping (authoritative, for repointing and shim generation)

Import prefix is `kbutillib.`. Consumers importing **classes from the package root**
(`from kbutillib import MSFBAUtils`) or using the `KBUtilLib`/`session.kbu.*` facade keep
working unchanged. Consumers importing **flat submodules** must be repointed per this table
(or rely on the deprecation shim).

| OLD (flat) | NEW (canonical) |
|---|---|
| `base_utils`, `shared_env_utils`, `dependency_manager` | `core.<same>` |
| `argo_utils`, `ai_curation_utils`, `kb_plm_utils` | `domains.ai.<same>` |
| `ms_biochem_utils` | `domains.biochem.ms_biochem_utils` |
| `bvbrc_utils`, `kb_uniprot_utils`, `patric_ws_utils`, `rcsb_pdb_utils` | `domains.external.<same>` |
| `annotator_utils`, `kb_annotation_utils`, `kb_genome_utils`, `mmseqs_utils`, `skani_utils`, `ontomap_utils` | `domains.genome.<same>` |
| `dram2_utils`, `prokka_utils`, `transyt_utils` | `domains.genome.annotation.<same>` |
| `kb_berdl_utils`, `kb_callback_utils`, `kb_narrative_audit`, `kb_reads_utils`, `kb_sdk_utils`, `kb_ws_utils`, `kbase_catalog_client`, `kbase_endpoints` | `domains.kbase.<same>` |
| `kb_model_utils`, `model_directionality`, `model_helpers`, `model_standardization_utils`, `ms_fba_utils`, `ms_reconstruction_utils`, `ms_template_utils` | `domains.modeling.<same>` |
| `escher_utils` | `domains.notebook.escher_utils` |
| `thermo_utils` | `domains.thermo.thermo_utils` |
| `king_install` | `agents.king_install` |

**Surviving package-level shims (do NOT need repointing):** `kbutillib.cli`,
`kbutillib.notebook`, `kbutillib.cheminformatics`, `kbutillib.researchos`,
`kbutillib.thermo_predictors`. **Stayed top-level:** `toolkit.py`, `layout.py`,
`compartments.py`, `__init__.py`, `__main__.py`, and packages `beril/`, `beril_worktree/`,
`harness/`, `installed_clients/`, `kb_app_runner/`, `kb_job_utils/`, `king_app/`, `data/`,
`services/`.

### Flat-submodule deprecation shim (the one genuinely new module we author)

A single **table-driven deprecation-shim mechanism** re-adds the legacy flat submodule
import paths that Vibhav deleted, but *better than his original silent shims*: each emits a
`DeprecationWarning` and re-exports from the new domain path. Prefer a package-level
`__getattr__`/meta-path approach or a generated set of thin modules driven by the old→new
mapping above — one mechanism, not dozens of hand-written stubs (deep module: large
back-compat behavior behind a small table). It must cover at minimum the historically
deep-imported set: `ms_fba_utils`, `kb_model_utils`, `kb_ws_utils`, `kb_genome_utils`,
`kb_berdl_utils`, `ms_biochem_utils`, `annotator_utils`, `kb_annotation_utils`,
`shared_env_utils`, `argo_utils`, `thermo_utils` — and should be generated for the full
mapping so no legacy path 404s. The shim is a migration bridge, expected to be removed a
release later.

### Silent-`None` guard

Add a test that imports every historically top-level-exported class name from `kbutillib`
and asserts each is not `None` (the `try/except → None` re-export pattern otherwise hides a
missed re-export until a downstream `NoneType` call). This makes a broken re-export a build
failure.

### Consumer netting (fix the knowns; shim covers the rest)

- **In-repo, shipped skills (must fix — they instruct agents):**
  `src/kbutillib/beril/skills/kbu-fba/SKILL.md:62` (`from kbutillib.kb_berdl_utils import
  KBBERDLUtils`) → repoint to top-level `from kbutillib import KBBERDLUtils` (survives) or
  `domains.kbase.kb_berdl_utils`. Sweep `kbu`, `kbu-fba`, `kbu-notebook`
  (`src/kbutillib/beril/skills/`), `kbu-run` (`src/kbutillib/harness/skills/`) and
  `king_app/skill.md` for flat-submodule imports AND stale file-path prose (e.g.
  `ms_fba_utils.py:75/86`, `ms_reconstruction_utils.py:176/685`, `kb_berdl_utils.py:705`,
  `cli/model.py` citations) and correct to the new domain paths.
- **External consumer `util.py` reach-ins (shim keeps them alive; report + optional fix):**
  produce a grep sweep of `~/Dropbox/Projects` for `from kbutillib.<flat>` and a report of
  every hit (known: ModelingLOE `PRJ-*/util.py`, WatershedPhenotypeReplication `util.py`,
  KBDatalakeApps, BVBRCHackathon, EnsembleNotebooks/ModelSEEDNotebooks/PangenomeAnalysis).
  These do NOT block the merge — the deprecation shim keeps them running with a warning.
  Fixing them is a lower-priority follow-up.

### Servers / privacy

Default-bind `kbu-api` and `kbu-mcp` HTTP transports to `127.0.0.1`; the `deploy/poplar`
systemd unit inherits loopback unless a config/env explicitly sets a wide bind. No auth is
added in this PRD (accepted debt); just do not expose by default.

### Dependency hygiene

Verify (and, if the branch does not already, enforce) that cheminformatics deps (rdkit,
pickaxe, retrorules) and server deps (fastapi, uvicorn, mcp/fastmcp) live behind extras,
never in core `[project].dependencies`. Core modeling/notebook install must not pull them.

### Landing (parking-branch model)

After validation: land the integration onto `main`, then fast-forward/merge `main → wip`
so the reorg is live on the local Dropbox parking repo (`~/Dropbox/Projects/KBUtilLib`
runs the `wip` tree). Then re-sync the corrected skill sources with `claude-skills sync
<machine> --apply`. Do not `git checkout main` on the parking repo; commit on the branch
`symbolic-ref` reports.

### Confront-hardened resolutions (round 1, folded wholesale)

Cross-family confront (task-62d8c425, h100 codex) surfaced 10 binding stalls; all folded.

- **Step 0 — adopt the branch explicitly (do NOT assume the reorg is already present).**
  The builder begins by fetching remote `vibhav`
  (`https://github.com/VibhavSetlur/KBUtilLib`) branch `feature/reorg-api-mcp-explore` and
  adopting it as the integration base. It MUST assert `src/kbutillib/domains/` exists before
  doing any delta work; if `domains/` is absent, fetch+merge the branch rather than treating
  the current flat worktree as post-reorg. (The confront agent, running off `main`, saw only
  the flat tree — this precondition is the root of stalls #1/#2/#7/#8.)
- **ARGO fix path (#2):** apply the httpx-0.28 `proxies=` → `proxy=` change to
  `src/kbutillib/domains/ai/argo_utils.py` *after* adoption (the file is the renamed
  `argo_utils.py`). If rename detection fails and the fix must be staged pre-merge, re-apply
  it to the domains path during the merge.
- **Shim mechanism (#3) — BOUND: generated thin modules.** Implement the flat-submodule
  deprecation shim as one real generated `.py` module per legacy `*_utils` path, each doing
  `warnings.warn(..., DeprecationWarning)` (once per import) then `from kbutillib.<new>
  import *`. Do NOT use a package `__getattr__` or a meta-path finder — a submodule import
  (`from kbutillib.ms_fba_utils import X`) requires a real module object, and generated
  modules preserve module semantics (`__file__`, `help()`, `import kbutillib.<x>` works).
- **Silent-None guard list (#4) — BOUND:** the canonical name set is the class entries of
  `src/kbutillib/__init__.py`'s `__all__` (exclude plain functions/constants). The guard test
  iterates those and asserts each imports non-`None`.
- **Skill import form (#5) — BOUND: top-level facade.** All shipped skills standardize on
  `from kbutillib import <Class>`; no domain paths in shipped skill bodies (they are internal
  and would rot again).
- **`httpx[socks]>=0.28` placement (#6) — BOUND: behind an `ai` extra**, not core (argo is
  lazy-imported via the facade); mark ArgoUtils construction tests to require the extra. Only
  if a core (non-lazy) path imports httpx unconditionally does it move to core.
- **Server loopback (#7):** set the default bind host to `127.0.0.1` in the actual server
  entrypoints — `interfaces/api/app.py` (`build_app`/`main`) and `interfaces/mcp/server.py`
  (`run_http`) — verified by a test asserting the effective bind host is loopback.
- **Test relocation (#8):** merge the reorg first, then add new tests under `tests/guard/`
  (silent-None) and `tests/core/` (deprecation shim); relocate the re-landed LP-solver tests
  into the new subdir layout. Do NOT pre-relocate in the flat tree.
- **grep sweep availability (#9):** the sweep runs in_context on primary-laptop where
  `~/Dropbox/Projects` exists; write `agent-io/reports/consumer-migration.md`; if the root is
  absent, write the report marked "skipped".
- **Landing mechanics (#10) — BOUND:** produce the integration branch, **merge-commit** it
  into `main` (NO rebase), then merge `main → wip` (fast-forward if possible) on the parking
  repo. **Do not push to `origin`** in this PRD — Chris pushes deliberately later
  (committed-unpushed is fine on the Dropbox-synced repo).
- **Minimum landing bar (free critique):** before landing, the in_context gate is
  silent-None guard + deprecation-shim tests + LP-solver client smoke + ArgoUtils construct;
  the full ~2,500-test suite runs on h100 as next-stage verification, not as a landing
  blocker.

## Testing Decisions

Test external behavior, not the reorg's internal moves. Modules to test:

1. **Flat-submodule deprecation shim** — for the historically-deep-imported set, assert
   `from kbutillib.<flat> import <Class>` (a) succeeds, (b) returns the same object as the
   new `domains.<...>` path, and (c) emits a `DeprecationWarning`. This is the load-bearing
   safety net; test it directly.
2. **Silent-`None` guard** — assert every historically top-level-exported class resolves
   non-`None` from `kbutillib`.
3. **LP-solver client from its new home** — import and construct `MSRemoteSolverUtils` /
   call `remote_solve` from `domains.modeling` (or via the facade), matching the pre-reorg
   client tests (`tests/test_ms_remote_solver_utils.py`, `test_ms_remote_solve_utils.py`)
   relocated into the new `tests/modeling/` layout.
4. **ARGO construction** — `ArgoUtils` constructs without the httpx-0.28 `proxies` crash
   (port `tests/test_argo_utils_construct.py`), confirming the fix survived the rename.
5. **BERIL end-to-end smoke** (behavioral, may be a scripted check rather than pytest):
   a `session.kbu.fba.run_fba/run_fva` + `session.kbu.recon.build_metabolic_model/
   gapfill_metabolic_model` flow on a small sample genome, and the `kbu model reconstruct|
   gapfill|fba|fva` verbs producing valid `--json`.
6. **Full suite** — the branch's ~2,500 tests plus the above pass; document any pre-existing
   xfails.

Prior art: the branch already ships `tests/domains/test_<domain>_smoke.py` per domain and a
large `tests/verab/` suite; the deprecation-shim and silent-`None` tests are new and belong
in `tests/core/` or `tests/guard/`.

## Out of Scope

- Adding authentication to the MCP/HTTP servers (explicit accepted debt; loopback-default
  only).
- Fixing every external consumer `util.py` reach-in (the shim covers them; only known
  in-repo shipped skills are fixed here; external fixes are a follow-up).
- Any redesign or curation of Vibhav's verab/cheminformatics/predictive-thermo science — it
  is adopted as-is; correctness review of that new science is not part of this integration.
- Removing the deprecation shim (a later release does that once consumers migrate).
- Deploying the poplar server stack to production (this PRD lands the code loopback-safe;
  actual poplar deployment is separate).

## Further Notes

- This is a large, delicate, big-bang merge with a history event on a Dropbox-synced repo
  with a real origin (`cshenry/KBUtilLib`). It fits Chris's "short-sharp big-bang refactor"
  philosophy, but the blind-spot ("trusts status when underlying state is broken") argues
  for verifying the landed tree by content (grep for the domains/ layout AND our
  `services/lp_solver/app.py`) rather than trusting a green status. Recommend landing via
  **interactive `/ai-conductor`** (held, not auto_conduct) so Chris watches it land.
- The `kbu` CLI, `session.kbu.*`, top-level re-exports, and KING coupling all SURVIVE the
  reorg — the real breakage is narrowly the flat-submodule import surface, which the
  deprecation shim neutralizes. The migration risk is far smaller than the 361-file
  diffstat implies.
- Do not trust the branch's reported "~2,500 passing" — run the suite ourselves post-merge;
  the branch is heavily agent-generated (WP0–WP20).

## Acceptance Criteria

1. The builder fetches remote `vibhav` (`https://github.com/VibhavSetlur/KBUtilLib`) branch `feature/reorg-api-mcp-explore` and adopts it as the integration base, asserting `src/kbutillib/domains/` exists before any delta work; if absent, it fetches+merges rather than assuming the reorg is present.
2. The ARGO httpx-0.28 `proxies=`→`proxy=` fix is present in `src/kbutillib/domains/ai/argo_utils.py` post-adoption (grep confirms `proxy=` and no residual `proxies=` kwarg).
3. The flat-submodule deprecation shim is implemented as generated thin modules — one real `.py` per legacy `*_utils` path — each emitting `DeprecationWarning` once per import and re-exporting via `from kbutillib.<new> import *`; no `__getattr__`/meta-path mechanism is used.
4. For the full legacy set, `from kbutillib.<x>_utils import <Class>` succeeds, returns the same object as the `domains.*` path, and emits a `DeprecationWarning`.
5. A guard test iterates the class entries of `src/kbutillib/__init__.py`'s `__all__` and asserts each imports non-`None`.
6. All shipped skill sources (`beril/skills/{kbu,kbu-fba,kbu-notebook}`, `harness/skills/kbu-run`, `king_app/skill.md`) use `from kbutillib import <Class>` and contain no `from kbutillib.<x>_utils import` and no stale flat file-path prose.
7. `httpx[socks]>=0.28` is declared behind an `ai` extra (not core) unless a core non-lazy path imports httpx unconditionally; ArgoUtils construction tests are marked to require that extra.
8. `kbu-api` and `kbu-mcp` HTTP transports default-bind to `127.0.0.1` (in `interfaces/api/app.py` and `interfaces/mcp/server.py`), verified by a test asserting the effective bind host is loopback.
9. cheminformatics deps (rdkit/pickaxe/retrorules) and server deps (fastapi/uvicorn/mcp) remain behind extras; a bare `pip install .` core install imports `kbutillib` and runs `kbu model --help` without them.
10. The LP-solver service layer (`services/lp_solver/app.py`, `worker.py`, `deploy/`) and client (`ms_remote_solver_utils`, `ms_remote_solve_utils`) are present post-integration, the client living under `domains/modeling/`, and `remote_solve` is callable via the facade.
11. The in_context minimum landing bar passes before landing: silent-None guard + deprecation-shim tests + LP-solver client smoke + ArgoUtils construct.
12. The full ~2,500-test suite runs on h100 (maestro) as next-stage verification; regressions from the re-landed delta/shim are fixed; remaining failures are documented xfails.
13. The consumer grep sweep runs against `~/Dropbox/Projects` and writes `agent-io/reports/consumer-migration.md` (or a report marked "skipped" if the root is absent).
14. `kbu --help`, `kbu model --help`, `kbu cap list`, and the `kbu beril`/`kbu harness`/`kbu notebook` verb groups all succeed post-integration (CLI name and verb tree preserved).
15. Landing is a merge-commit of the integration branch into `main` (no rebase), then `main→wip` fast-forward on the parking repo; nothing is pushed to `origin` in this PRD.
