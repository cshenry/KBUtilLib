# Audit — KBUtilLib KEEP/DISCARD vs BERIL directives

**Date:** 2026-06-13
**Author:** grounding audit for the kbu→BERIL augmentation PRD
**Scope:** Decide what to keep vs discard from the current kbu notebook/co-scientist
machinery, characterize the kept `NotebookSession`/cache API surface, distill the
canonical `util.py` pattern, inventory BERIL's notebook/project directives, and resolve
every conflict between a kept kbu directive and a BERIL directive.

**Governing principle (applied throughout):**
> **BERIL owns the project skeleton, directory layout, execution entry, and
> artifact/run-state tracking. kbu owns in-notebook construction (`util.py` + `%run`),
> caching, and object provenance.**

Decisions in the prompt (DISCARD orchestration/org/exec; KEEP construction-directives,
cache infra + `NotebookSession`, the provenance-DAG slice of `Manifest`, FBA/recon
science) are treated as fixed. This audit grounds and refines them; it does not
relitigate.

---

## 1. KEEP / DISCARD ledger

Legend: **KEEP** = survives into the BERIL world as a skill directive or imported
library; **DISCARD** = deleted from the kbu→BERIL surface; **SPLIT** = one element whose
two roles diverge.

### 1a. kbu skills / commands / subagents

| Element | Role today | Verdict | Rationale | Lands in BERIL world as |
|---|---|---|---|---|
| `/kbu-start` (tier-1, `.claude/commands/kbu-start.md`) | Machine setup + `kbu new-project`/`bootstrap`/doctor dashboard | **DISCARD** | Org/install orchestration; BERIL scaffolds projects itself (`/berdl_start` Phase 0). `kbu doctor`/`kbu init` venv plumbing is irrelevant to a Claude-Code-native BERIL deploy. | nothing |
| `/kbu-start` (tier-2 / project-side state-machine dashboard) | Per-subproject workflow dashboard | **DISCARD** | The plan/build/run state machine is BERIL's job (`beril.yaml` status ladder). | nothing |
| `/kbu-plan` | 4-step grilled plan → `RESEARCH_PLAN.md` + literature + tasks | **DISCARD** | BERIL `/berdl_start` Phases A/B + Checkpoint already own hypothesis grilling, literature (`/literature-review`), `RESEARCH_PLAN.md`, and the plan-review gate. Direct overlap, BERIL wins. | nothing (capability already in BERIL) |
| `/kbu-build` | Conduct/scaffold notebooks from buildplan | **DISCARD** | BERIL Phase C drives notebook authoring/execution. The kbu-build conductor loop (helper-fn + fast-test assembly) is a *technique* worth porting into the `kbu-notebook` guideline prose, but the **command** is discarded. | technique folded into `kbu-notebook` skill |
| `/kbu-migrate` (`.claude/commands/kbu-migrate.md`) | Adopt `archive/` notebooks → structured subproject; path rewrites, `util.py` audit, `NotebookSession` insertion | **DISCARD** (command); **HARVEST** two ideas | The `kbu subproject adopt` org flow + manifest population is discarded. But two construction-relevant ideas survive: (a) the `PROJECT_ROOT = Path(__file__).resolve().parents[N]` anchor pattern, (b) the `util.py` dedup-against-library audit. | those two ideas become directives in `kbu-notebook` |
| `kbu-sub-literature-review` subagent | Literature search subagent | **DISCARD** | BERIL ships `/literature-review` (`.claude/skills/literature-review/`). | BERIL's own |
| `kbu-sub-build` / `kbu-sub-review` / `kbu-sub-diagnose` subagents (`templates/research-project/.claude/agents/`) | Plan/build/run pipeline subagents | **DISCARD** | Part of the discarded state-machine pipeline; BERIL has `/berdl-review`, `/synthesize`, `/submit`. | nothing |
| `jupyter-dev` skill (ClaudeCommands, `agent-io/skills/jupyter-dev.md`) | "How to build notebooks against NotebookSession" universal skill | **DISCARD / SUPERSEDE** | It is the closest analog to the planned `kbu-notebook`, but it is wired to the discarded org model (`subprojects/<name>/notebooks/`, `kbu init-notebook`, `kbu subproject create`) **and it actively forbids `%run util.py`** — the exact opposite of the canonical KEEP directive. See §6. | replaced by `kbu-notebook` skill |
| (new) `/kbu` primer | — | **NEW** | Planned. | new skill |
| (new) `kbu-notebook` guideline skill | — | **NEW** | Planned — carries the canonical construction directives (§3) + the harvested `kbu-build`/`kbu-migrate` techniques. | new skill |
| (new) `kbu-fba` guideline skill | — | **NEW** | Planned — instructs BERIL to call the kept FBA/recon science via `session.kbu.*`. | new skill |

### 1b. Notebook-engine modules / classes (`src/kbutillib/notebook/`)

| Element | Role | Verdict | Rationale | Lands as |
|---|---|---|---|---|
| `NotebookSession` (`session.py`) | Entry point: locates `.kbcache/`, lazy-inits cache/vectors/experiments/strains/manifest, `.kbu` facade | **KEEP** | The spine of the kept infra. `.for_notebook(__file__)` anchors `.kbcache/` next to the notebook — composes cleanly with BERIL `projects/{id}/notebooks/`. | imported library; instantiated in `util.py` |
| `Cache` / `CacheEntry` (`cache.py`) | Content-hashed blob cache + `@cached` decorator, provenance logging | **KEEP** | Core cache-as-you-go primitive. No org coupling. | imported library |
| `VectorStore` (`vector_store.py`) | Typed Parquet-backed numeric vectors | **KEEP** | Useful for fitness/omics matrices; optional in any given notebook. | imported library |
| `ExperimentStore` / `StrainStore` (`experiment_store.py`) | Register Sample/Computation/ExternalDataset/Strain in catalog | **KEEP** | Provenance + experiment metadata; optional. | imported library |
| `Manifest` (`manifest.py`) | Browseable view of notebooks+objects+freshness **AND** `render()` of a `Manifest.ipynb` org dashboard | **SPLIT** — see §1c | The provenance-DAG/freshness queries are KEEP; the org/run-state dashboard role is DISCARD. | provenance API kept; dashboard role dropped |
| `serialization/` (json, dict, dataframe, text, msgenome, cobra_model, msmodelutil, msexpression; **no pickle**) | Type-specific (de)serializers, auto-dispatch registry | **KEEP** | Lets `cache.save(model)` round-trip COBRA/MSModelUtil/MSGenome/MSExpression natively — exactly the modeling objects BERIL's metabolic projects handle. | imported library |
| `storage/` (`catalog.py` SQLite, `blobs.py`, `vectors.py`) | Backing stores for the above | **KEEP** | Implementation of cache/vectors/catalog. | imported library (internal) |
| `schema/` (entity, experiment, strain, media, vector, manifest, validation) | Pydantic models for cached objects + `VectorType`/`EntityKind`/`EntityRef` | **KEEP** | Typed vocabulary the cache/vector/experiment APIs require. | imported library |
| `helpers/` (compartment, reaction, fva) | Pure promoted notebook helpers (`COMPARTMENT_MAP`, `classify_fva_flux`, etc.) | **KEEP** | Generic modeling helpers; imported in `util.py`'s `from kbutillib.notebook.helpers import ...` block. | imported library |
| `detect.py` | Notebook env + name + cell-index/hash detection | **KEEP** | Required for `for_notebook()` anchoring and access-log provenance. | imported library (internal) |
| `vector_store.validate_entities` / `ValidationReport` | Cross-checks EntityRefs resolve against cached namespaces | **KEEP** | Provenance-integrity check; optional. | imported library |

### 1c. The Manifest SPLIT (explicit)

| `Manifest` capability | Method(s) | KEEP/DISCARD | Why |
|---|---|---|---|
| Provenance DAG / producer-consumer edges | `objects()` (`.parents`), `what_writes()`, `what_reads()`, `dot()` | **KEEP** | Pure object-provenance over the cache access-log. This is the "what produced/consumed object X" slice — kbu's legitimate territory. |
| Freshness / staleness | `stale()`, `objects()[].is_stale`, `_check_stale()` | **KEEP** | Staleness is *object-level* (a cached blob is stale if a declared input is newer) — provenance, not project run-state. Keep. |
| Per-notebook run roll-up | `notebooks()` (last_run / write_count / read_count from access_log) | **KEEP (as provenance)** | Derived from the cache access-log, not from a TOML manifest. It is a *read view* of provenance, harmless. Distinguish from the discarded `kbu notebook list --last_run_at` org tracker (§1d), which is a separate filesystem/TOML mechanism. |
| Org / run-state dashboard | `render()` → writes a `Manifest.ipynb` at project root | **DISCARD** | This is an organizational artifact (a generated project-overview notebook with a PRD link). BERIL owns project dashboards; do not emit `Manifest.ipynb` into a BERIL project. The underlying query methods stay; the `render()` org surface goes. |

### 1d. CLI org/exec commands (DISCARD wholesale)

| Element | Role | Verdict | Rationale | Lands as |
|---|---|---|---|---|
| `cli/notebook.py` → `kbu notebook exec` | Headless in-place nbclient execution + backup + output-truncation + auto mark-run | **DISCARD** | BERIL execution is native (JupyterHub web UI; `jupyter nbconvert --execute --inplace`). The kbu exec discipline is explicitly discarded. | nothing |
| `cli/notebook.py` → `kbu notebook list` / `mark-run` | Scan `subprojects/*/notebooks` + read/write `last_run_at` in `kbu-subproject.toml` | **DISCARD** | Run-state tracking against TOML manifest — BERIL tracks run-state via `beril.yaml` status + `approval.notebook_hashes`. | BERIL's `beril.yaml` + `notebook_hashes` |
| `cli/subproject.py` | `kbu subproject create/adopt/advance/status` | **DISCARD** | Subprojects model is discarded; BERIL uses flat `projects/{id}/`. | BERIL `projects/{id}/` |
| `cli/manifest.py` (`read_project_manifest`, `append_notebook_entry_or_update`, …) | Read/write `kbu-project.toml` / `kbu-subproject.toml` | **DISCARD** | `kbu-project.toml` and the org-manifest are discarded. | BERIL `beril.yaml` |
| `layout.py` | Canonical kbu dir names: `DEFAULT_SHARED_DIRS=(data,models,genomes)`, `subproject_subdirs=[notebooks,figures,nboutput,.cache,literature,sessions]`, gitignore patterns | **DISCARD** | This is the kbu directory contract. BERIL defines its own (`notebooks/ data/ figures/ user_data/`). One latent-conflict note: kbu's `.cache` per-subproject dir vs `NotebookSession`'s `.kbcache/` — see §5. | BERIL layout |
| `kbu-project.toml` + subprojects model + `[layout.shared_dirs]` | Project marker + org tree | **DISCARD** | BERIL's marker is `beril.yaml`; its tree is fixed. | `beril.yaml` |

---

## 2. kbu `NotebookSession` / cache public API surface

This is the exact surface the `kbu-notebook` + `kbu-fba` skills will instruct Claude to
call. Import root: `from kbutillib.notebook import NotebookSession` (+ `schema`,
`helpers`).

### Session entry
```python
from kbutillib.notebook import NotebookSession
session = NotebookSession.for_notebook(__file__, project_name="<id>")
```
- `NotebookSession.for_notebook(notebook_file=None, *, project_name=None)` — classmethod;
  resolves `.kbcache/` as a **sibling of the file** (`<file>.parent/.kbcache`), or `cwd`
  if `notebook_file` is omitted. Opens the SQLite catalog lazily.
- Properties (all lazy): `session.cache`, `session.vectors`, `session.experiments`,
  `session.strains`, `session.manifest`, `session.kbu` (KBUtilLib facade →
  `session.kbu.<sub>.<method>`), `session.kbcache_dir`, `session.notebook_name`,
  `session.in_notebook`.
- `session.validate_entities() -> ValidationReport` — resolve all EntityRefs against
  cached namespaces.
- `session.close()`.

### Cache (`session.cache`) — the cache-as-you-go core
- `save(name, obj, *, type_hint=None, metadata=None) -> CacheEntry` — content-hashed,
  silent overwrite, skip-write if hash unchanged, logs a `write` provenance event.
- `load(name, *, default=_MISSING, expected_type=None)` — raises `KeyError` if absent and
  no `default`; logs a `read` event. **Project-wide flat namespace** — any notebook
  sharing the `.kbcache/` sees the object (notebook_name affects provenance only, not
  visibility).
- `exists(name) -> bool`
- `info(name) -> CacheEntry`
- `list(*, type_filter=None, created_by_notebook=None) -> [CacheEntry]`
- `delete(name)` — removes row; GC's the blob if no other entry shares the hash.
- `@cache.cached(name, *, inputs=None, type_hint=None)` — decorator: return cached value
  if present, else compute → save. `inputs` is persisted to metadata for Manifest
  freshness.

### Vectors (`session.vectors`)
- `from_dataframe(df, *, id, experiment_id, type: VectorType, entity_kind: EntityKind,
  entity_namespace, columns=None) -> Vector` (+ `from_excel(...)`); Parquet-backed.
- `list()`, and load/query helpers in `vector_store.py`.

### Experiments / Strains
- `session.experiments.register(Experiment)`, `.register_sample(Sample, parents=())`,
  `.list()`.
- `session.strains.register(...)`, `.list()`.

### Manifest — provenance only (post-split)
- `manifest.objects() -> [ObjectEntry]` (each has `.parents`, `.is_stale`, access stats)
- `manifest.what_writes(name) / what_reads(name) -> [AccessRecord]`
- `manifest.stale() -> [ObjectEntry]`
- `manifest.notebooks() -> [NotebookEntry]` (read view of access-log)
- `manifest.dot() -> str` (Graphviz DAG)
- **Do not call** `manifest.render()` in a BERIL project (emits an org dashboard notebook — discarded role).

### Schema / helpers (imported in `util.py`)
- `from kbutillib.notebook.schema import Sample, Computation, ExternalDataset, Experiment,
  Strain, Mutation, Media, Vector, VectorType, EntityKind, EntityRef`
- `from kbutillib.notebook.helpers import COMPARTMENT_MAP, normalize_compartment,
  get_reaction_directionality, standardize_exchange_id, get_exchange_map,
  build_gene_reaction_map, reaction_equation_with_names, is_diffusion_reaction,
  compare_reaction_stoichiometry, find_significant_differences, classify_fva_flux`

### Serializers (auto-dispatched; **no pickle**)
`json, dict, dataframe, text, msgenome, cobra_model, msmodelutil, msexpression` —
so `cache.save(cobra_model)` / `cache.save(msmodelutil_obj)` round-trip natively.

### FBA / reconstruction science (kept; reached via `session.kbu.*`)
- `MSFBAUtils` (`ms_fba_utils.py`): `set_media(model, media)`, `run_fba(model, media=None,
  objective=None, run_pfba=True)`, `run_fva(model, media=None, objective=None,
  fraction_of_optimum=0.9)` (the deliberate working-FVA workaround — `cobra.flux_variability_analysis`
  is broken; do not "fix" by reverting to cobra), `configure_fba_formulation(...)`,
  `constrain_objective_to_fraction_of_optimum(...)`, `unblock_objective_with_exchanges(...)`,
  `fit_flux_to_mutant_growth_rate_data(...)`.
- `MSReconstructionUtils` (`ms_reconstruction_utils.py`): `build_metabolic_model(genome,
  genome_classifier, ...)`, `gapfill_metabolic_model(...)`,
  `run_comprehensive_gapfill_on_model(...)`, `kb_build_metabolic_models(...)`,
  `kb_gapfill_metabolic_models(...)`.

---

## 3. The canonical `util.py` pattern, distilled

Source of truth: `~/Dropbox/Projects/ModelingLOE/notebooks/gapfill_loe/util.py`
(template stub: `src/kbutillib/cli/templates/util.py.tmpl`). Generalized skeleton for any
BERIL modeling notebook:

```python
"""<project>/util.py — runs as `%run util.py` at the top of every cell.

Provides: common imports, the project NotebookSession (named `session`),
root-anchored path constants, and project-specific pure helper functions.
"""
from __future__ import annotations

# === 1. sys.path bootstrap (portability) ===================================
# Reads ~/.kbu-sys-paths (one path per line, # comments OK) and prepends each
# to sys.path BEFORE heavy imports. Silent no-op if the file is absent.
import sys as _sys
from pathlib import Path as _Path
def _bootstrap_sys_paths() -> None:
    f = _Path.home() / ".kbu-sys-paths"
    if not f.exists():
        return
    try:
        for raw in f.read_text().splitlines():
            s = raw.split("#", 1)[0].strip()
            if s and (e := str(_Path(s).expanduser())) not in _sys.path:
                _sys.path.insert(0, e)
    except Exception:
        pass
_bootstrap_sys_paths()

# === 2. imports block (ALL imports live here; cells carry none) ============
import hashlib, json, os, re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import numpy as np
import pandas as pd
try:
    import cobra
    from cobra import Reaction, Metabolite
except ImportError:
    cobra = None
from kbutillib.notebook import NotebookSession
from kbutillib.notebook.helpers import (COMPARTMENT_MAP, normalize_compartment,
    classify_fva_flux, ...)            # generic helpers, free for all notebooks
from kbutillib.notebook.schema import (Sample, Computation, Experiment,
    Vector, VectorType, EntityKind, EntityRef, Media, ...)

# === 3. session (named `session`, __file__-anchored) =======================
session: NotebookSession = NotebookSession.for_notebook(__file__, project_name="<id>")

# === 4. root-anchored path constants =======================================
_NB_DIR       = Path(__file__).resolve().parent          # this notebook's dir
_PROJECT_ROOT = _NB_DIR.parent                            # projects/<id>/
DATA_DIR      = _PROJECT_ROOT / "data"                    # agent-derived inputs
USER_DATA_DIR = _PROJECT_ROOT / "user_data"              # user-supplied inputs
FIGURES_DIR   = _PROJECT_ROOT / "figures"                # saved PNGs

# === 5. project-specific pure helper functions =============================
# Free functions only — never methods on a god-class. Testable in isolation.
def my_helper(model, threshold=0.1) -> ...: ...
```

### The four canonical directives this encodes
1. **One `util.py`** holds ALL imports + helpers + the `session` init (named `session`).
2. **Every cell starts with `%run util.py`**; cells carry no import boilerplate.
3. **Every cell is independently executable**: load inputs from cache/files →
   compute with **cache-as-you-go** (`session.cache.save` after each completed sub-step,
   so an interrupted cell loses no completed work) → on completion save results to cache +
   output files.
4. **Portability**: the project dir can be moved to another machine and re-run there; the
   `~/.kbu-sys-paths` bootstrap repairs `sys.path` per-machine.

### Mapping onto BERIL `projects/{id}/`
BERIL's tree is `projects/{id}/{notebooks,data,figures,user_data}` (per
`/berdl_start` Phase 0). `util.py` lives at **`projects/{id}/notebooks/util.py`** (beside
the `.ipynb` files), so:
- `_NB_DIR = projects/{id}/notebooks/`, `_PROJECT_ROOT = projects/{id}/`.
- `.kbcache/` is created by `for_notebook(__file__)` at **`projects/{id}/notebooks/.kbcache/`**
  (sibling of `util.py`). This must be **gitignored** (it is derived, not an artifact —
  see §5 output-placement) — distinct from BERIL's committed `data/figures/`.
- Path constants point at `_PROJECT_ROOT / "data"`, `/ "user_data"`, `/ "figures"` —
  exactly BERIL's directories. No new directories are introduced; kbu adds only the
  `.kbcache/` sidecar + `util.py`.

> Refinement vs `gapfill_loe`: that util.py anchors at `parents/parents` because
> ModelingLOE nests `notebooks/<subproject>/`. In BERIL the notebook dir is one level
> under the project root, so the anchor is `parent` (notebook dir) / `parent.parent`
> (project root). The `kbu-notebook` skill must state the BERIL-specific anchor depth
> explicitly so Claude doesn't copy the LOE `parents[2]` value.

---

## 4. BERIL notebook / project directives inventory

Sourced from `.claude/skills/berdl_start/SKILL.md`, `PROJECT.md`, `synthesize/SKILL.md`,
`submit/SKILL.md`, and a real notebook (`projects/essential_metabolome/notebooks/
01_extract_essential_reactions.ipynb`).

- **Project marker = `beril.yaml`** at `projects/{id}/beril.yaml`; required by `/submit`
  (a project without it is rejected). (`berdl_start` Phase 0 step 5; `submit` 1a.)
- **Fixed flat layout**: `projects/{id}/{README.md, RESEARCH_PLAN.md, REPORT.md, REVIEW*.md,
  notebooks/, data/, user_data/, figures/, requirements.txt}`. `.gitkeep` in each empty dir
  at scaffold. (`berdl_start` Phase 0 step 4; `PROJECT.md` Project Structure.)
- **Status ladder in `beril.yaml`**: `exploration → proposed → active → analysis →
  reviewed → complete`, with a mandatory plan-review checkpoint between `proposed` and
  `active`. (`berdl_start` Phase 2 table; PROJECT.md.)
- **Notebooks are the audit trail**, numbered `00_` (exploration) then `01_`, `02_`…
  Each self-contained with a clear purpose. (`berdl_start` Key Principle 3.)
- **Notebooks MUST be committed with saved outputs** — hard requirement; "a notebook with
  only source code and no outputs should never be committed." Provenance + human review +
  machine-readable for `/synthesize`. (`PROJECT.md` Reproducibility Standards.)
- **Execution is native/manual**: JupyterHub web UI (Restart & Run All) or
  `jupyter nbconvert --to notebook --execute --inplace`. **No programmatic execution from
  the agent.** (`PROJECT.md` "No programmatic notebook execution".)
- **Figures saved as standalone PNGs to `projects/{id}/figures/`**; referenced inline in
  REPORT.md. (`PROJECT.md`; `synthesize` step.)
- **Data placement rule**: agent-derived → `projects/{id}/data/`; user-supplied →
  `projects/{id}/user_data/` (never mixed); cross-project reusable → top-level `data/`.
  Large files gitignored. (`PROJECT.md` Data Provenance.)
- **`requirements.txt`** per project; **README `## Reproduction`** section required.
- **Artifact/run-state tracking = `beril.yaml.artifacts` flags + `approval.notebook_hashes`**
  (canonical `sha256:` per-notebook hashes via `tools/notebook_hash.py`, checked at
  `/submit` to catch re-execution drift). (`submit`/`berdl_start` resume logic.)
- **Import convention (de facto, not mandated)**: real notebooks put imports **in the
  first code cell per notebook** (`import pandas as pd`; `from get_spark_session import
  get_spark_session`). BERIL has **no opinion** on a shared `util.py`, on `%run`, on
  cell-level independence, or on caching. Spark init (`spark = get_spark_session()`) is the
  one required boilerplate, at the top of every Spark notebook. (`PROJECT.md` Spark
  Notebooks; observed notebook.)
- **No caching opinion**: BERIL has no cache concept. Re-runs re-query Spark. (No directive
  anywhere on intermediate-result caching.)

---

## 5. CONFLICT TABLE (the heart of the audit)

Precedence rule applied: **BERIL owns skeleton/dirs/execution-entry/artifact-tracking;
kbu owns in-notebook construction (`util.py` + `%run`), caching, object provenance.**

| Concern | BERIL says | kbu says | Conflict? | Precedence (who wins, why) | How they coexist |
|---|---|---|---|---|---|
| **Project marker** | `beril.yaml` is the marker; required by `/submit`. | `kbu-project.toml` marks a kbu project. | **none** (kbu marker DISCARDED) | **BERIL.** Project identity is skeleton. | kbu emits no marker; reads nothing from `beril.yaml`. The kept infra never needs a project marker — `NotebookSession` anchors on `__file__`, not a marker. |
| **Directory layout** | Fixed `projects/{id}/{notebooks,data,figures,user_data}`. | `subprojects/<name>/{notebooks,figures,nboutput,.cache,literature,sessions}` + root `data/models/genomes` (`layout.py`). | **none** (kbu layout DISCARDED) | **BERIL.** Dirs are skeleton. | kbu adds only two paths *inside* BERIL's tree: `notebooks/util.py` (committed) and `notebooks/.kbcache/` (gitignored). No `nboutput/`, no `subprojects/`, no root `models/genomes`. |
| **Where notebooks live** | `projects/{id}/notebooks/`, flat, numbered `00_/01_…`. | `subprojects/<name>/notebooks/`; jupyter-dev *forbids* a root-level `notebooks/`. | **real (with jupyter-dev) / none (with BERIL principle)** | **BERIL.** | `kbu-notebook` must drop the subprojects nesting entirely and target `projects/{id}/notebooks/`. The kbu numbering convention happens to match BERIL's — no friction. |
| **Run-state / artifact tracking** | `beril.yaml.artifacts` + `approval.notebook_hashes` (canonical hash at submit). | `kbu-subproject.toml` `[[notebooks]].last_run_at` + `modified_since_run`; `Manifest.render()` dashboard. | **latent** | **BERIL** for project/notebook run-state; **kbu** for *object* provenance only. | kbu keeps `Manifest.{objects,what_writes,what_reads,stale,dot}` (object DAG over the cache log) and **drops** `last_run_at` tracking and `Manifest.render()`. The two never write the same store: BERIL hashes `.ipynb` files; kbu logs cache-object access in `.kbcache/catalog.sqlite`. Object provenance is *additive* to BERIL, not competing. |
| **Notebook execution entry** | Native: JupyterHub UI or `nbconvert --execute --inplace`; **no agent-programmatic execution**. | `kbu notebook exec` (nbclient, in-place, auto mark-run). | **real** (head-on) | **BERIL.** Execution-entry is BERIL's. | `kbu notebook exec` DISCARDED. `kbu-notebook` must instruct execution via BERIL's path only. Note a *latent tension*: BERIL bans agent-driven execution, but the conductor "fast-test helper functions" technique harvested from `/kbu-build` runs **pytest on helpers**, not notebooks — so it stays within BERIL's rule (tests ≠ notebook execution). Flag for §7. |
| **Imports convention** | De facto: imports in first code cell **per notebook**; no shared-util opinion; Spark init required per notebook. | **One `util.py` with ALL imports; every cell `%run util.py`; cells carry no imports.** | **real** | **kbu** — imports/in-notebook construction is kbu's domain, and BERIL has *no mandate* here (only a de-facto habit). | `kbu-notebook` introduces `util.py` + `%run`. For Spark notebooks, the required `spark = get_spark_session()` init moves **into `util.py`** (or stays in cell 1 if the user prefers) — coexists since `%run util.py` executes in the cell's namespace, so `spark` becomes available exactly as a per-cell import would. **This is the single most important precedence call in the audit** and the one place to verify the user wants kbu's convention to override BERIL's existing notebooks. See §7 Q1. |
| **Cell independence** | Notebooks "self-contained"/"audit trail"; no per-*cell* independence rule. | **Every cell independently re-runnable** via cache load/save. | **none** (compatible, kbu is stricter) | **kbu** (additive). | kbu's cell-independence is a superset of BERIL's notebook-level self-containment. No conflict; kbu strengthens it. |
| **Caching** | No caching concept; re-runs re-query Spark. | **cache-as-you-go** to `.kbcache/`; `@cached`; flat project-wide namespace. | **none** (BERIL silent) | **kbu** — caching is explicitly kbu's domain. | `.kbcache/` is a new gitignored sidecar under `notebooks/`. It never competes with BERIL `data/` (committed agent-derived outputs). Rule: **cache = derived/intermediate (gitignored); `data/`/`figures/` = committed artifacts.** A Spark query result a downstream cell needs → cache it (avoids re-query); a *finding-grade* CSV → also write to `data/`. |
| **Output-file placement** | Agent-derived → `data/`; user input → `user_data/`; figures → `figures/` (PNG); large files gitignored. | jupyter-dev: non-JSON → `nboutput/`; cache → `.kbcache/`; inputs immutable in `data/`/`user_data/`. | **real** (`nboutput/`) / **latent** (`.cache` vs `.kbcache`) | **BERIL** for artifact dirs; **kbu** for the cache sidecar. | Drop `nboutput/` entirely — final outputs go to BERIL's `data/`/`figures/`. Keep only `.kbcache/` for cache. **Latent name clash:** kbu `layout.py` reserves a per-subproject `.cache` dir, but `NotebookSession` actually writes `.kbcache/` — since `layout.py` is DISCARDED, only `.kbcache/` remains; ensure BERIL's `.gitignore` excludes `**/.kbcache/`. See §7 Q2. |
| **Provenance DAG** | None. | `Manifest` object DAG + freshness. | **none** | **kbu** (additive). | Pure addition; BERIL gains object-level provenance it didn't have. `Manifest.render()` (org dashboard) stays disabled. |
| **Portability bootstrap** | None (`get_spark_session` handles connectivity; `.venv-berdl`). | `~/.kbu-sys-paths` sys.path prepend in `util.py`. | **none** | **kbu** (additive). | The bootstrap is inert if `~/.kbu-sys-paths` is absent (silent no-op), so it is safe to ship in every BERIL `util.py`. |

### Where the governing principle does NOT cleanly resolve (flagged)
1. **Imports convention (`%run util.py` vs BERIL's per-cell imports).** The principle says
   kbu owns in-notebook construction, so kbu wins — but BERIL has ~75 existing projects
   whose notebooks use per-cell imports and a `spark = get_spark_session()` cell. Applying
   `%run util.py` to *new* notebooks is clean; *retrofitting* existing ones is a migration
   the principle doesn't adjudicate. Recommendation: kbu convention applies to new modeling
   notebooks; leave existing BERIL notebooks alone. (§7 Q1.)
2. **Spark-init placement.** `get_spark_session()` is BERIL's one mandated per-notebook
   boilerplate, but kbu's directive is "no boilerplate in cells." These collide only in
   form: putting the Spark init inside `util.py` satisfies both, but Spark notebooks are
   JupyterHub-only and may not want the `~/.kbu-sys-paths` bootstrap. Recommend: kbu-fba/
   kbu-notebook target **local modeling notebooks** (COBRA/MSModelUtil, no Spark); Spark
   query notebooks keep BERIL's convention. The two skill sets address different notebook
   classes. (§7 Q3.)

---

## 6. jupyter-dev gap analysis

`jupyter-dev` (`ClaudeCommands/agent-io/skills/jupyter-dev.md`) is the nearest analog to
the planned `kbu-notebook`, and the user reports it is "not working right." Itemized
defects vs the canonical directives — `kbu-notebook` must supersede each:

1. **Forbids `%run util.py` outright** (lines 11, 152–153, 168, 291, 449). It mandates
   `from util import session` as the cell opener and lists "no `%run util.py` legacy magic"
   as a validation gate. This is the **direct inverse** of canonical directive #2. The
   `from util import session` form also breaks cell independence subtly: a bare `import`
   does not re-execute `util.py`'s side-effecting bootstrap (`_bootstrap_sys_paths()`) on
   each cell the way `%run` does, and only pulls `session` — not the imports/helpers — into
   the cell namespace, so cells still need their own `import pandas`, etc. `%run` puts the
   *entire* util namespace (imports + helpers + session) into the cell. **Supersede.**
2. **Wired to the discarded subprojects org** (lines 20–47, 64–85): hard-codes
   `subprojects/<name>/notebooks/`, declares a root-level `notebooks/` "not permitted,"
   and depends on `kbu subproject create`. All discarded. `kbu-notebook` must target
   `projects/{id}/notebooks/` (flat, BERIL).
3. **Depends on `kbu init-notebook` + per-project venv + pinned kernel** (lines 222–245,
   438–441). That bootstrap CLI is part of the discarded org/install layer. BERIL manages
   environments (`.venv-berdl`, JupyterHub kernel); `kbu-notebook` must not invoke
   `kbu init-notebook`.
4. **Introduces `nboutput/`** for non-JSON output (lines 74, 178, 239, 279). Conflicts with
   BERIL's `figures/` + `data/` placement. Drop `nboutput/`.
5. **Carries a `session_for(file)` back-compat shim and a hand-authored module-level
   `NotebookSession(name=..., notebook_folder=...)` signature** (lines 109–117) that does
   **not match** the real API (`NotebookSession.for_notebook(__file__, project_name=...)`).
   The skill's template would fail against current `session.py`. `kbu-notebook` must use the
   real classmethod signature.
6. **Generated-header / sentinel-marker machinery** (`kbu init-notebook --force` smart-merge,
   lines 87–131, 242–245) presumes the discarded template-management CLI. Drop; ship a
   plain hand-maintained `util.py` per the §3 skeleton.

What jupyter-dev gets *right* and `kbu-notebook` should keep: the cell-independence
philosophy, the `session.cache.save/load` checkpoint pattern, the "pure free functions, no
god-class" rule, the `session.kbu.*` facade for FBA/recon, and the five FBA antipatterns
(hardcoded `GROWTH_DASH_RXN`, missing `set_media`, reimplementing canonical methods,
slash-prefixed cache keys on a flat namespace, `sol.fluxes.values()` misuse) — these belong
in `kbu-fba`.

---

## 7. Open questions for the PRD

1. **Does `%run util.py` override BERIL's per-cell-import convention for *new* modeling
   notebooks, and are existing BERIL notebooks left untouched?**
   *Recommend: yes and yes.* `kbu-notebook` mandates `%run util.py` for new
   modeling/reconstruction notebooks; existing Spark/query notebooks keep their per-cell
   imports. No mechanical retrofit.

2. **What gitignore entry covers the cache, and is it `.kbcache/`?**
   *Recommend:* confirm the kept `NotebookSession` writes `.kbcache/` (it does — `session.py`
   `for_notebook` → `base_dir/".kbcache"`), and add `**/.kbcache/` to BERIL's `.gitignore`.
   The DISCARDED `layout.py` `.cache` name is a red herring; don't carry it over.

3. **Do `kbu-notebook`/`kbu-fba` target local COBRA/MSModelUtil modeling notebooks only,
   leaving Spark/BERDL-query notebooks to BERIL's existing convention?**
   *Recommend: yes.* The two skill sets address different notebook classes; this cleanly
   sidesteps the Spark-init-placement collision (§5). State the boundary explicitly in
   `/kbu` primer.

4. **Keep `Manifest.notebooks()` (the access-log roll-up) even though it superficially
   resembles the discarded run-state tracker?**
   *Recommend: keep.* It is a read-only provenance view over the cache log, not a
   competing run-state store; it never writes `beril.yaml` or `.ipynb` hashes. Document it
   as provenance, not project tracking, so it isn't mistaken for the discarded
   `kbu notebook list`.

5. **Does the harvested `/kbu-build` "fast-test helper functions" conductor technique
   violate BERIL's no-programmatic-notebook-execution rule?**
   *Recommend: no.* It runs `pytest` against pure helpers in `util.py`, never executes the
   notebook. `kbu-notebook` should state this distinction so a future maintainer doesn't
   conflate "run the tests" with "run the notebook."

6. **Should the CRAFT-style deployer ship the `kbutillib.notebook` package as a pip
   dependency of BERIL, or vendor it?**
   *Recommend: pip dependency (editable or pinned).* The serializers pull in
   COBRA/MSModelUtil/MSGenome/MSExpression; vendoring would fork the science. Out of scope
   for this audit but blocks the skills (they import `kbutillib.notebook`).

---

### Appendix — key file references
- Canonical util.py: `~/Dropbox/Projects/ModelingLOE/notebooks/gapfill_loe/util.py:1-93`
  (bootstrap + imports + session + path constants); template stub
  `src/kbutillib/cli/templates/util.py.tmpl`.
- Kept infra: `src/kbutillib/notebook/{session.py,cache.py,manifest.py,vector_store.py,
  experiment_store.py}`, `serialization/`, `schema/`, `helpers/`, `storage/`.
- Discarded CLI/org: `src/kbutillib/cli/{notebook.py,subproject.py,manifest.py}`,
  `src/kbutillib/layout.py`; `.claude/commands/{kbu-start.md,kbu-migrate.md}`.
- Kept science: `src/kbutillib/{ms_fba_utils.py,ms_reconstruction_utils.py}`.
- jupyter-dev: `~/Dropbox/Projects/ClaudeCommands/agent-io/skills/jupyter-dev.md`.
- BERIL directives: `BERIL-research-observatory/.claude/skills/berdl_start/SKILL.md`,
  `PROJECT.md`, `.claude/skills/{synthesize,submit}/SKILL.md`,
  `projects/essential_metabolome/notebooks/01_extract_essential_reactions.ipynb`,
  `projects/annotation_gap_discovery/beril.yaml`.
