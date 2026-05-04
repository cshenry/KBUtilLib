# Notebook Engine Redesign — Full PRD

**Status**: Design approved; Phase 1 ready for implementation.
**Owner**: Chris Henry
**Repo**: KBUtilLib (`/Users/chenry/Dropbox/Projects/KBUtilLib`)
**Proving ground**: ADP1Notebooks
**Design session**: 2026-05-02 → 2026-05-03 (`/ai-design`)

---

## 1. Problem statement

The current `notebook_utils.py` (840 lines) and the `/jupyter-dev` skill that codifies its use have evolved into a load-bearing but fragile foundation for every notebook project. ADP1Notebooks is the richest example: a 1132-line per-project `util.py` that conflates project-specific analysis methods, multi-inheritance composition, and a JSON-only cache layer with no provenance. Real bugs are visible in the wild (e.g., undefined `model` references; broken orchestrators referencing unassigned `averaged_expression`).

### Concrete pain points
- **Mixed concerns** in one 840-line module: env detection + display + cache I/O + scientific schema.
- **JSON-only serialization** — no DataFrame/numpy/KBase-object roundtripping.
- **Hard-coded scientific enums** (`NumberType` {NR, AA, Log2}, `DataType` {TRANS, PROT, MGR, RxnTRANS, RxnPROT, RxnMGR}) — extending requires library edits.
- **No provenance** — no record of which notebook/cell created or read a cache object, no content-hashing, no freshness tracking.
- **Implicit cross-mixin coupling** (`self.kb_object_factory` referenced but defined in sibling KBWSUtils).
- **Per-project `util.py` is a god-class** — 1100+ lines of project biology code with no other home, untestable in isolation, multi-inheritance over 5 base classes (MSFBAUtils + AICurationUtils + NotebookUtils + KBPLMUtils + EscherUtils).
- **Silent fallback** between namespaced and base datacache directories (exercised in real use, mixed namespacing is the norm).
- **No path** for migrating mature notebook functions into permanent libraries.

### Strengths to preserve
- The `save`/`load` workflow that enables cell independence (run notebooks from the middle).
- The `{prefix}-{scale}-{domain}` filename convention is load-bearing — actual datacache uses it.
- Composition-based mixins for ModelSEED/KBase domain modules still have ergonomic value when used in moderation.
- The vision of `util.py` as a holding pen for tacit functions that mature into permanent homes.

---

## 2. Goals

1. **Formal provenanced foundation** — every cached object has automatic metadata (creator notebook, cell, timestamp, content hash, type).
2. **Typed scientific data model** — `Experiment` (Sample | Computation | ExternalDataset) with `Vector` numerical data attached, formal storage in SQLite + Parquet.
3. **Proper serialization** for non-JSON types (MSGenome, COBRA models, MSExpression, DataFrames) — no pickle.
4. **Manifest notebook per project** — lists all subnotebooks, last-run timestamps, data objects, freshness state, browseable API.
5. **Function migration lifecycle** — clear path from `util.py` (testable functions) → permanent library module.
6. **End-to-end tests** at multiple layers (storage, schema, util.py functions, notebook execution, migration gate).
7. **Refactor ADP1Notebooks** end-to-end as the proving ground.
8. **Rewrite `/jupyter-dev`** to teach the new pattern.

## 3. Non-goals (v1)

- Visualization helpers — split out to a separate module, not redesigned here.
- Composition refactor of existing `KBUtilLib` mixin classes — deferred. New engine is a *peer*, not a replacement for the existing modules.
- Cell-level dependency DAG with full content-hash invalidation — Phase 5+ stretch goal. Manifest v1 uses mtime-based heuristics.
- Multi-machine catalog sync — single-machine, in-tree catalog only.

---

## 4. Architecture overview

### 4.1 Module structure

```
kbutillib/notebook/
├── __init__.py               # public exports: NotebookSession, schema types
├── session.py                # NotebookSession — entry point
├── cache.py                  # Cache — generic blob save/load
├── vector_store.py           # VectorStore — typed numerical data
├── experiment_store.py       # ExperimentStore, StrainStore
├── manifest.py               # Phase 3
├── migration.py              # Phase 5: util.py → permanent module helpers
├── detect.py                 # notebook env/name detection (split out)
├── display.py                # display_dataframe, display_json, etc. (existing methods, split out)
├── vector_types.yaml         # open registry of VectorType domain/scale/projection
├── schema/
│   ├── entity.py             # EntityKind, EntityRef
│   ├── strain.py             # Strain, Mutation
│   ├── media.py              # Media
│   ├── experiment.py         # Experiment, Sample, Computation, ExternalDataset
│   └── vector.py             # Vector, VectorType
├── storage/
│   ├── catalog.py            # SQLite catalog connection + DDL
│   ├── vectors.py            # parquet read/write for vector data
│   └── blobs.py              # filesystem blobs keyed by content_hash
└── serialization/
    ├── __init__.py           # registry, register_serializer, auto_dispatch
    ├── serialize_json.py
    ├── serialize_dict.py
    ├── serialize_dataframe.py
    ├── serialize_text.py
    ├── serialize_msgenome.py
    ├── serialize_cobra_model.py
    ├── serialize_msmodelutil.py
    └── serialize_msexpression.py
```

The existing `notebook_utils.py` becomes a thin shim that re-exports `NotebookSession as NotebookUtils` for backward compat during migration.

### 4.2 On-disk layout (per notebook project)

```
notebooks/
├── util.py                   # project-specific functions; uses NotebookSession
├── *.ipynb                   # the notebooks
├── data/                     # raw inputs (KEEP — input data, not cache)
├── models/                   # raw input model files (KEEP — input, not cache)
├── genomes/                  # raw input genome files (KEEP — input, not cache)
├── nboutput/                 # products: HTML, PNG, XLSX, TSV (unchanged)
└── .kbcache/                 # NEW — gitignored by default
    ├── catalog.sqlite        # the catalog
    ├── blobs/                # generic blobs
    │   └── <content_hash>.<ext>
    └── vectors/              # parquet vector files
        └── <vector_id>.parquet
```

**Inputs vs. cache rule:** `data/`, `models/`, `genomes/` (and similar named input dirs) are **inputs** — the raw material notebooks read from. They stay. Only `datacache/` is replaced by `.kbcache/`. Notebooks are designed to regenerate cached intermediates from inputs on every fresh run; cache migration is **not** a goal.

Legacy `datacache/` content is **not migrated** when refactoring an existing project — notebooks ingest from `data/`/`models/`/`genomes/` afresh and rebuild the cache via the new API. (See Phase 4 §9 below.)

---

## 5. Data model (locked v1)

### 5.1 Core types

```python
class EntityKind(StrEnum):
    GENE = "gene"
    REACTION = "reaction"
    METABOLITE = "metabolite"

class EntityRef(BaseModel):
    kind: EntityKind
    id: str                    # e.g., "ACIAD0123" or "rxn00200_c0"
    namespace: str             # genome_id or model_id this entity belongs to
    # No verification at construction; resolved lazily.

class Mutation(BaseModel):
    kind: Literal["knockout","knockin","point","insertion","deletion","overexpression"]
    target: EntityRef          # gene
    source_organism: Optional[str] = None
    source_gene: Optional[str] = None
    description: Optional[str] = None

class Strain(BaseModel):
    id: str                    # canonical (e.g., "ACN2586")
    parent_genome: str         # name of MSGenome blob in catalog
    mutations: list[Mutation] = []
    description: Optional[str] = None

class Media(BaseModel):
    id: str
    source: Literal["kbase","msmedia","inline"] = "kbase"
    inline_composition: Optional[dict[str, float]] = None  # cpd_id → mM, only if source="inline"

class Sample(BaseModel):                        # wet-lab Experiment
    id: str
    media: Media
    strains: dict[str, float]                   # strain_id → abundance (sums to 1.0)
    replicates: list[str] = []                  # replicate labels, e.g., ["ACN2586_1", ...]
    description: Optional[str] = None

class Computation(BaseModel):                   # in-silico Experiment
    id: str
    model_ref: str                              # name of stored model blob
    media: Media
    parameters: dict[str, Any] = {}
    derived_from_sample: Optional[str] = None
    description: Optional[str] = None

class ExternalDataset(BaseModel):               # literature / public / collaborator
    id: str
    source: Literal["literature","public_db","collaborator","other"]
    citation: Optional[str] = None
    url: Optional[str] = None
    organism: Optional[str] = None
    description: Optional[str] = None

class Experiment(BaseModel):
    id: str
    kind: Literal["sample","computation","external"]
    payload: Sample | Computation | ExternalDataset
    notebook: Optional[str] = None
    parents: list[str] = []                     # other Experiment.ids
    created_at: datetime

class VectorType(BaseModel):                    # validated against vector_types.yaml
    domain: str                                 # transcriptomics|proteomics|metabolomics|flux|mutant_growth_rate|fold_change|essentiality|...
    scale: str                                  # raw|absolute|normalized_relative|log2|z_score|rate|...
    projection: Optional[str] = None            # gpr_max|gpr_min|gpr_mean (only for projected vectors)

class Vector(BaseModel):
    id: str                                     # canonical name
    type: VectorType
    experiment_id: str
    entity_kind: EntityKind
    entity_namespace: str                       # genome or model the entity IDs belong to
    columns: list[str]                          # ["replicate_1", ...] or ["aggregated"] or ["min","max"]
    parquet_path: str                           # relative to .kbcache/
    content_hash: str
    derivation: Optional[str] = None            # "mean" | "log2_fold_change" | None
    parents: list[str] = []                     # parent Vector.ids if derived
    created_at: datetime
```

### 5.2 Existing taxonomy → new schema

| Legacy filename token | domain | scale | entity_kind | projection |
|---|---|---|---|---|
| `NR-TRANS` | transcriptomics | normalized_relative | gene | — |
| `AA-PROT` | proteomics | absolute | gene | — |
| `AA-MGR` | mutant_growth_rate | absolute | gene | — |
| `NR-MGR` | mutant_growth_rate | normalized_relative | gene | — |
| `Log2-PROT` | proteomics | log2 | gene | — |
| `NR-RxnTRANS` | transcriptomics | normalized_relative | reaction | gpr_max *(default; configurable)* |
| `NR-RxnPROT` | proteomics | normalized_relative | reaction | gpr_max *(default; configurable)* |
| `NR-RxnMGR` | mutant_growth_rate | normalized_relative | reaction | gpr_max *(default; configurable)* |

Decoder ring (locked):
- `NR` = normalized relative abundance
- `AA` = absolute abundance
- `MGR` = mutant growth rates
- `Rxn`-prefix = projected from gene-level onto reactions via GPR

### 5.3 Parquet vector layout

```
entity_id : string             # primary key (gene_id, rxn_id, met_id)
[col_1]   : float64            # one column per replicate / aggregate axis
[col_2]   : float64
...
```

A 4-replicate proteomics Vector is a 4-column parquet keyed by gene; the `mean`-aggregated derivative is a 1-column parquet keyed by gene with `parents=[<replicate_vector.id>]` and `derivation="mean"`.

---

## 6. Phase 1 — Cache + Catalog interface

### 6.1 Public API

```python
class NotebookSession:
    """Entry point. Holds catalog connection, paths, env detection."""

    @classmethod
    def for_notebook(cls, notebook_file: str | None = None) -> "NotebookSession":
        """Detect notebook context, locate-or-create .kbcache/, open catalog."""

    @property
    def cache(self) -> Cache: ...
    @property
    def vectors(self) -> VectorStore: ...
    @property
    def experiments(self) -> ExperimentStore: ...
    @property
    def strains(self) -> StrainStore: ...
    @property
    def manifest(self) -> Manifest: ...               # Phase 3
```

```python
class Cache:
    """General-purpose blob cache. Backed by .kbcache/blobs/ + catalog.cache_objects."""

    def save(self, name: str, obj: Any, *, type_hint: str | None = None,
             metadata: dict | None = None) -> CacheEntry:
        """Silent overwrite. Picks serializer via type_hint or auto-dispatch.
        Computes content_hash, writes blob if hash is new, updates catalog row.
        Logs write event in access_log."""

    def load(self, name: str, *, default: Any = _MISSING,
             expected_type: str | None = None) -> Any: ...

    def exists(self, name: str) -> bool: ...
    def info(self, name: str) -> CacheEntry: ...
    def list(self, *, type_filter: str | None = None) -> list[CacheEntry]: ...
    def delete(self, name: str): ...

    def cached(self, name: str, *, inputs: list[str] | None = None,
               type_hint: str | None = None):
        """Decorator: returns cached value if present, otherwise computes + saves."""
```

```python
class VectorStore:
    """Numerical Vector operations. Backed by .kbcache/vectors/ + catalog.vectors."""

    # Ingestion
    def from_dataframe(self, df, *, id, experiment_id, type, entity_kind,
                       entity_namespace, columns=None) -> Vector: ...
    def from_excel(self, path, sheet=0, *, id, experiment_id, type,
                   entity_kind, entity_namespace, index_col=0, columns=None) -> Vector: ...
    def from_csv(self, path, *, id, experiment_id, type, entity_kind,
                 entity_namespace, **kwargs) -> Vector: ...

    # Derivation
    def aggregate(self, parents: list[str],
                  op: Literal["mean","median","max","min","sum"], *, id) -> Vector: ...
    def project(self, parent_id, *, id,
                projection: Literal["gpr_max","gpr_min","gpr_mean"],
                target_namespace: str, model_ref: str) -> Vector: ...
    def fold_change(self, numerator_id, denominator_id, *, id,
                    log_base: int | None = 2) -> Vector: ...
    def compute(self, id, *, experiment_id, type, entity_kind, entity_namespace,
                columns, compute_fn, parents=()) -> Vector:
        """Compute-or-load. compute_fn is called only on cache miss."""

    # Retrieval
    def get(self, id) -> tuple[Vector, pd.DataFrame]: ...
    def metadata(self, id) -> Vector: ...
    def list(self, *, experiment_id=None, type=None, entity_kind=None) -> list[Vector]: ...
    def delete(self, id): ...

    # Batch
    def get_many(self, ids) -> pd.DataFrame:
        """Concat-merge multiple Vectors into a single wide DataFrame on entity_id."""
```

```python
class ExperimentStore:
    def register(self, exp: Experiment) -> Experiment: ...
    def register_sample(self, sample: Sample, *, parents=()) -> Experiment: ...
    def register_computation(self, comp: Computation, *, parents=()) -> Experiment: ...
    def register_external(self, ext: ExternalDataset, *, parents=()) -> Experiment: ...
    def get(self, id) -> Experiment: ...
    def list(self, *, kind=None) -> list[Experiment]: ...

class StrainStore:
    def register(self, strain: Strain) -> Strain: ...
    def get(self, id) -> Strain: ...
    def list(self) -> list[Strain]: ...
```

### 6.2 SQLite DDL

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- ============================================================
-- CATALOG META
-- ============================================================
CREATE TABLE catalog_meta (
    schema_version INTEGER NOT NULL,
    created_at     TIMESTAMP NOT NULL,
    project_name   TEXT
);

-- ============================================================
-- EXPERIMENTS
-- ============================================================
CREATE TABLE experiments (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL CHECK(kind IN ('sample','computation','external')),
    payload_json  TEXT NOT NULL,
    notebook      TEXT,
    description   TEXT,
    created_at    TIMESTAMP NOT NULL
);
CREATE TABLE experiment_parents (
    child_id   TEXT NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    parent_id  TEXT NOT NULL REFERENCES experiments(id),
    PRIMARY KEY (child_id, parent_id)
);
CREATE INDEX idx_experiment_parents_parent ON experiment_parents(parent_id);

-- ============================================================
-- STRAINS
-- ============================================================
CREATE TABLE strains (
    id              TEXT PRIMARY KEY,
    parent_genome   TEXT,
    description     TEXT
);
CREATE TABLE mutations (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    strain_id         TEXT NOT NULL REFERENCES strains(id) ON DELETE CASCADE,
    kind              TEXT NOT NULL,
    target_kind       TEXT NOT NULL,
    target_id         TEXT NOT NULL,
    target_namespace  TEXT NOT NULL,
    source_organism   TEXT,
    source_gene       TEXT,
    description       TEXT
);
CREATE INDEX idx_mutations_strain ON mutations(strain_id);

-- ============================================================
-- VECTORS
-- ============================================================
CREATE TABLE vectors (
    id                TEXT PRIMARY KEY,
    experiment_id     TEXT NOT NULL REFERENCES experiments(id),
    type_domain       TEXT NOT NULL,
    type_scale        TEXT NOT NULL,
    type_projection   TEXT,
    entity_kind       TEXT NOT NULL,
    entity_namespace  TEXT NOT NULL,
    columns_json      TEXT NOT NULL,
    n_entities        INTEGER NOT NULL,
    n_columns         INTEGER NOT NULL,
    parquet_path      TEXT NOT NULL,
    content_hash      TEXT NOT NULL,
    derivation        TEXT,
    created_at        TIMESTAMP NOT NULL
);
CREATE INDEX idx_vectors_experiment ON vectors(experiment_id);
CREATE INDEX idx_vectors_type ON vectors(type_domain, type_scale);
CREATE INDEX idx_vectors_entity ON vectors(entity_kind, entity_namespace);
CREATE TABLE vector_parents (
    child_id   TEXT NOT NULL REFERENCES vectors(id) ON DELETE CASCADE,
    parent_id  TEXT NOT NULL REFERENCES vectors(id),
    PRIMARY KEY (child_id, parent_id)
);
CREATE INDEX idx_vector_parents_parent ON vector_parents(parent_id);

-- ============================================================
-- CACHE OBJECTS (generic blobs)
-- ============================================================
CREATE TABLE cache_objects (
    id             TEXT PRIMARY KEY,
    type           TEXT NOT NULL,
    blob_path      TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    n_bytes        INTEGER NOT NULL,
    metadata_json  TEXT,
    created_at     TIMESTAMP NOT NULL
);
CREATE INDEX idx_cache_type ON cache_objects(type);
CREATE INDEX idx_cache_hash ON cache_objects(content_hash);

-- ============================================================
-- ACCESS LOG (provenance)
-- ============================================================
CREATE TABLE access_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id         TEXT NOT NULL,
    object_kind       TEXT NOT NULL CHECK(object_kind IN ('cache','vector')),
    op                TEXT NOT NULL CHECK(op IN ('write','read','delete')),
    notebook          TEXT,
    cell_index        INTEGER,
    cell_source_hash  TEXT,
    content_hash      TEXT,
    timestamp         TIMESTAMP NOT NULL
);
CREATE INDEX idx_access_object ON access_log(object_id, object_kind);
CREATE INDEX idx_access_notebook ON access_log(notebook, timestamp);
```

### 6.3 Serializer registry

```python
class Serializer(Protocol):
    type_name: str
    file_extension: str
    def can_handle(self, obj: Any) -> bool: ...
    def serialize(self, obj: Any, path: Path) -> dict[str, Any]: ...
    def deserialize(self, path: Path, metadata: dict) -> Any: ...

def register_serializer(s: Serializer) -> None: ...
def get_serializer(type_name: str) -> Serializer: ...
def auto_dispatch(obj: Any) -> Serializer: ...
```

| Module | type_name | extension | Backed by |
|---|---|---|---|
| `serialize_json.py` | `json` | `.json` | stdlib json |
| `serialize_dict.py` | `dict` | `.json` | stdlib json |
| `serialize_dataframe.py` | `dataframe` | `.parquet` | pyarrow |
| `serialize_text.py` | `text` | `.txt` | utf-8 |
| `serialize_msgenome.py` | `msgenome` | `.json` | MSGenome.to_json/from_json |
| `serialize_cobra_model.py` | `cobra_model` | `.json` | cobra.io.json |
| `serialize_msmodelutil.py` | `msmodelutil` | `.json` | wraps cobra.io.json + MSModelUtil restore |
| `serialize_msexpression.py` | `msexpression` | `.parquet` + `.json` | DataFrame for `_data` + JSON for metadata |

**Hard rule**: no pickle. If a type can't be serialized cleanly, the serializer raises with a clear error pointing to a missing serializer module. Users register new serializers via `@register_serializer` at import time.

### 6.4 Identity & content-hashing rules

1. **Names are primary.** `cache.save("foo", obj)` always writes to the catalog row keyed `id="foo"`. `cache.load("foo")` always reads that row. Silent overwrite.
2. **Content hash = SHA-256 of serialized bytes**, computed at save. Stored in `cache_objects.content_hash` and snapshotted in `access_log.content_hash` per write.
3. **Blob file = `<content_hash>.<ext>`** in `.kbcache/blobs/`. Identical bytes share one file across multiple catalog rows.
4. **Skip-write optimization**: if the new content hash matches the existing catalog row's hash, the blob isn't rewritten, but the access_log still gets a `write` entry.
5. **Vectors don't share blobs across IDs.** Vector parquet is `<vector_id>.parquet` for ergonomics. content_hash is still computed and stored.
6. **Cell source hash = SHA-256 of `In[execution_count]`** (IPython); NULL if not in IPython.

### 6.5 Compute-or-load decorator

```python
@session.cache.cached("expression_flux_summary",
                       inputs=["averaged_expression_data", "FullyTranslatedPublishedModel"])
def build_summary():
    ...
    return summary_dict

result = build_summary()  # first call: computes + saves; subsequent: loads from cache
```

`inputs` is purely declarative in v1. Phase 3 manifest uses it for freshness DAG.

---

## 7. Phase 2 — Schema implementation

The schema in §5 lands as Pydantic models with:
- Field validators (e.g., abundances sum to 1.0; replicate labels non-empty for Sample).
- VectorType validation against `vector_types.yaml` registry (open-set: registry can be extended without library edits).
- `Experiment` discriminated union via Pydantic `Discriminator(kind)`.
- Tests for round-tripping each type to/from `payload_json`.
- Optional `session.validate_entities()` pass that resolves all `EntityRef`s against their declared namespaces (catches typos at user request, not at construction).

### 7.1 `vector_types.yaml` initial registry

```yaml
domains:
  transcriptomics: { default_entity_kind: gene }
  proteomics:      { default_entity_kind: gene }
  metabolomics:    { default_entity_kind: metabolite }
  flux:            { default_entity_kind: reaction }
  mutant_growth_rate: { default_entity_kind: gene }   # may also be reaction via projection
  fold_change:     {}
  essentiality:    {}
  growth:          {}

scales:
  raw:                  {}
  absolute:             {}
  normalized_relative:  {}
  log2:                 {}
  z_score:              {}
  rate:                 {}
  fold_change:          {}

projections:
  gpr_max:   {}
  gpr_min:   {}
  gpr_mean:  {}
  gpr_eval:  { description: "Evaluate GPR Boolean expression" }
```

---

## 8. Phase 3 — Manifest notebook

Each project has a `Manifest.ipynb` (auto-generated and refreshable). The manifest:

1. Lists all notebooks in the project with last-run timestamp (from access_log).
2. Lists all cache objects and Vectors, with creator notebook, last write, last read, and a freshness flag.
3. Provides an interactive browse API:
   ```python
   manifest.notebooks()           # list of NotebookEntry
   manifest.objects()             # list of cache + vector objects
   manifest.what_writes(name)     # which notebooks/cells write this object
   manifest.what_reads(name)      # which notebooks/cells read it
   manifest.stale()               # objects whose inputs are newer than themselves
   manifest.dot()                 # graphviz of producer-consumer DAG
   ```

### 8.1 Freshness rules — v1 (pragmatic)
- Stale = any object whose declared `inputs` (from `cached(name, inputs=[...])`) have a more recent `created_at` than the object itself.
- mtime-based; no cell-source-hash dependency yet.
- Purely informational; users still trigger reruns manually.

### 8.2 Freshness rules — v2 (Phase 5)
- Cell-source-hash invalidation: if the source of the cell that wrote an object has changed (sha256 of `In[exec_count]` differs from the value last logged), object is flagged stale.
- Optional `--strict` mode that raises on `load()` of stale objects.

---

## 9. Phase 4 — ADP1Notebooks refactor

End-to-end migration as the proving ground. Surfaces every gap in Phases 1–3.

### 9.1 Target structure

```
ADP1Notebooks/notebooks/
├── util.py                   # NotebookSession + project-specific functions
├── test_util.py              # pytest tests for util.py functions
├── Manifest.ipynb            # auto-generated
├── ADP1ExpressionAnalysis.ipynb
├── ADP1BERDLFoldChangeAnalysis.ipynb
├── ... (existing notebooks, refactored)
├── data/                     # KEEP — input data
├── models/                   # KEEP — input model files
├── genomes/                  # KEEP — input genome files
├── nboutput/
└── .kbcache/                 # NEW (gitignored)
```

### 9.2 Migration steps

**No cache migration.** The new system is designed to regenerate all intermediates from raw inputs (`data/`, `models/`, `genomes/`) on every fresh run. Do not attempt to import legacy `datacache/` content. The clean path is to start with an empty `.kbcache/` and let notebooks rebuild from inputs.

1. Refactor `util.py`:
   - Replace multi-inheritance god-class with a thin `NotebookSession` instance + free functions / small helper classes.
   - Each function gets a docstring and a pytest test in `test_util.py`.
   - Fix the broken `model` reference (line 213) and missing `averaged_expression` (lines 895–920).
2. Drop redundant/superseded notebooks (use newer BERDL versions in preference to older non-BERDL siblings):
   - Drop `ADP1OldExpressionAnalysis.ipynb` (clearly superseded).
   - Drop `BERDLMockup.ipynb` (mockup, not real analysis).
   - For BERDL vs non-BERDL pairs (e.g., `ADP1FoldChangeAnalysis.ipynb` vs `ADP1BERDLFoldChangeAnalysis.ipynb`), keep the BERDL version.
   - Drop dated copies (e.g., `ADP1ExpressionAnalysis_20251024.ipynb`) when the un-dated version is current.
3. Convert each remaining notebook cell-by-cell to use:
   - `session.cache.save("name", obj)` / `session.cache.load("name")` (instead of `util.save`/`util.load`).
   - `session.vectors.from_*(...)` for proteomics/transcriptomics/MGR ingestion from `data/` files.
   - `session.experiments.register_*(...)` for Sample/Computation/ExternalDataset registration.
   - `@session.cache.cached(...)` for compute-or-load methods.
4. Validate notebooks run end-to-end via papermill smoke tests, ingesting from `data/`/`models/`/`genomes/` only.
5. Delete the old `datacache/` directory after smoke tests confirm the new pipeline reconstitutes everything needed (do NOT migrate its contents — it's expected scratch).
6. **Keep** `data/`, `models/`, `genomes/` as inputs. They are NOT removed.

---

## 10. Phase 4.5 — `/jupyter-dev` skill rewrite

The current skill (`~/.claude/commands/jupyter-dev.md`) bakes in: util.py as god-class, `%run util.py` as activation pattern, JSON-only datacache, separate `models/` and `genomes/` subdirs. All of this changes.

### 10.1 New skill principles
- **NotebookSession** is the entry point, not `%run util.py`.
- **util.py** holds testable free functions (or methods on small helper classes), not a god-class.
- **Each cell**: imports from `util`, uses `session.cache.load/save` for state, `session.vectors.*` for numerical data, `session.experiments.*` for registration.
- **Markdown precedes code** (preserved from old skill).
- **Cell independence** preserved as a virtue.
- **Migration lifecycle**: util.py functions are explicitly marked with `@migration_target("kbutillib.kb_genome_utils")` when ready; CLI moves them.
- **Inputs vs. cache**: `data/`, `models/`, `genomes/` directories stay as raw inputs and are read directly by ingestion cells. Only intermediates and computed artifacts go through the cache. No cache migration tooling — notebooks regenerate everything from inputs on fresh runs.
- **`datacache/` is replaced** by `.kbcache/` (gitignored). The new skill scaffolds projects with `.kbcache/` only.

The old skill is archived for reference, not deleted.

---

## 11. Migration tooling

### 11.1 Legacy datacache import — NOT IMPLEMENTED

Cache migration is intentionally **not** in scope. Notebooks are designed to regenerate all intermediates from raw inputs in `data/`, `models/`, and `genomes/` on every fresh run. There is no automated path from legacy `datacache/` content to the new `.kbcache/` — re-run notebooks against raw inputs to rebuild.

This is a deliberate design rule: cached intermediates are scratch, not source. If a notebook can't reproduce them from its inputs, that's a notebook bug to fix, not a migration to write.

### 11.2 Function migration CLI (Phase 5)

```bash
kbutillib migrate-function notebooks/util.py:my_func --to kbutillib.kb_genome_utils
```

Preconditions:
- Tests exist in `notebooks/test_util.py` covering `my_func`.
- Function is annotated `@migration_target("kbutillib.kb_genome_utils")`.
- Target module exists.

Action:
- Move function source to target module (best-effort AST move).
- Update util.py to re-export: `from kbutillib.kb_genome_utils import my_func`.
- Move tests to `kbutillib/tests/test_kb_genome_utils.py`.
- Open a PR for review.

---

## 12. Test framework

| Layer | What | Tool | Speed |
|---|---|---|---|
| 0. Storage | save/load roundtrip per supported serializer; SQLite catalog DDL/migration | pytest | fast |
| 1. Schema | Sample/Vector/Experiment validation, registry validation, relational integrity | pytest + Pydantic | fast |
| 2. util.py functions | project-specific functions tested as plain functions | pytest in `notebooks/test_util.py` | fast |
| 3. Notebook smoke | full notebook executes top-to-bottom | papermill on CI | slow |
| 4. Migration gate | function only leaves util.py when tests exist + target module imports cleanly | CLI + CI check | medium |

Layers 0–2 run pre-commit. Layer 3 runs nightly CI. Layer 4 runs on the migration PR.

### 12.1 Storage tests (Layer 0)
- Roundtrip: `save(obj) → load(name) == obj` for each serializer (json, dict, dataframe, msgenome, cobra_model, msmodelutil, msexpression, text).
- Content-hash determinism: same object serialized twice produces same bytes (where applicable).
- Skip-write: writing identical content does not duplicate the blob file.
- access_log: every write/read produces exactly one log row with correct fields.
- Foreign key integrity: cascade deletes work as expected.

### 12.2 Schema tests (Layer 1)
- Sample with `strains.values().sum() != 1.0` raises validation error.
- Sample with empty `replicates` is valid (allows pure-spec Samples without data yet).
- VectorType with unknown `domain` or `scale` raises.
- Experiment discriminated union round-trips cleanly through `payload_json`.
- `validate_entities()` flags typos in EntityRef IDs.

### 12.3 util.py function tests (Layer 2)
- Each project-specific function in `notebooks/util.py` has at least one test.
- Tests use a tmpdir-backed `NotebookSession` for isolation.

### 12.4 Notebook smoke tests (Layer 3)
- Each notebook in the project executes end-to-end via papermill against a fresh `.kbcache/`.
- CI fails on any cell error.

### 12.5 Migration gate (Layer 4)
- CLI rejects migration if Layer 2 tests don't exist for the function.
- CLI rejects if target module doesn't exist or doesn't import.
- Post-move, util.py re-export must round-trip imports.

---

## 13. Phasing and timeline

| Phase | Scope | Est. effort |
|---|---|---|
| 1. Data engine core | SQLite catalog, parquet vectors, blob store, save/load with provenance, NotebookSession API, serializer registry, backward-compat shim, Layer 0 + 1 tests | 2 weeks |
| 2. Schema | Pydantic models, vector_types.yaml registry, validate_entities, Layer 1 tests | 1 week |
| 3. Manifest v1 | Manifest notebook generator, browse API, mtime-based stale detection | 1 week |
| 4a. ADP1 util.py shell | New util.py with NotebookSession + free helper functions; test_util.py | 0.5 week |
| 4b. ADP1 pilot notebook | Migrate one notebook end-to-end against new API; surface gaps | 0.5–1 week |
| 4c. ADP1 remaining notebooks | Bulk migration of the remaining BERDL-era notebooks | 1 week |
| 4d. ADP1 final cleanup | Drop legacy datacache/, papermill smoke tests, drop redundant/old notebooks | 0.5 week |
| 4.5. /jupyter-dev rewrite | New skill teaching the new pattern; archive old skill | 0.5 week |
| 5. Manifest v2 + function migration CLI | Cell-source-hash freshness, util.py → permanent module migration tooling | 1 week |

Total: ~7-9 weeks of focused work. Phases are sequential except Phase 2 can overlap with Phase 1.

---

## 14. Open considerations (deferred, not blocking)

- **Multi-machine catalog sync**: current design is single-machine. If a project lives in Dropbox and is opened from multiple machines, the SQLite WAL may conflict. Future: optional Courier-backed sync, or read-only mode when not on the home machine.
- **Cross-vector materialized views**: if per-vector parquet reads become a bottleneck for large analyses, add `session.materialize_view([v1, v2, ...])` that builds a denormalized parquet. Catalog tracks freshness.
- **Metabolomics support**: not in legacy ADP1 data, but `metabolomics` is in the registry. Test once real data appears.
- **GPR Boolean evaluation**: `gpr_eval` projection is in the registry but implementation deferred — `gpr_max/min/mean` are simpler and cover ADP1's needs.
- **Notebook templates**: a `kbutillib new-notebook <name>` CLI that scaffolds a notebook with the standard cell pattern. Stretch goal for Phase 4.5.

---

## 15. Resolved decisions (audit trail)

All from the design session:

- **Storage**: SQLite (catalog) + Parquet (vectors) + filesystem blobs (everything else, content-addressed).
- **Vector storage**: one parquet file per Vector. Cross-vector reads via batch concat or opt-in materialized views.
- **Cache identity**: name is primary; content_hash is provenance metadata.
- **NotebookSession**: standalone object passed by composition; thin BaseUtils mixin shim for backward compat.
- **No pickle**: per-type serializer modules.
- **VectorType domain/scale**: open registry (`vector_types.yaml`), not closed enum.
- **Replicate model**: just labels (`list[str]`) on Sample. No first-class Replicate entity.
- **Aggregated vectors**: separate Vectors with `parents` + `derivation` lineage.
- **Existing taxonomy decoder**: NR=normalized_relative, AA=absolute, MGR=mutant_growth_rate (not metabolomics). Rxn-prefix = projected from gene-level via GPR.
- **EntityRef resolution**: lazy strings at construction; opt-in `session.validate_entities()`.
- **Mutation kinds (v1)**: knockout, knockin, point, insertion, deletion, overexpression.
- **Experiment**: 3-arm discriminated union (Sample, Computation, ExternalDataset).
- **Cache write semantics**: silent overwrite. access_log preserves write history.
- **Catalog location**: per-project, in-tree at `notebooks/.kbcache/catalog.sqlite`.
- **`/jupyter-dev` skill**: rewritten as Phase 4.5 deliverable.
- **Composition refactor**: deferred. New engine is a peer to existing mixins, not a replacement.
- **Inputs vs. cache**: `data/`, `models/`, `genomes/` (and similar input dirs) are raw inputs and stay. Only `datacache/` is replaced by `.kbcache/`. Notebooks must regenerate intermediates from inputs on every fresh run.
- **No cache migration**: the migration CLI from §11.1 is removed from scope. Refactored projects start with empty `.kbcache/` and rebuild via fresh notebook runs against raw inputs.
- **Phase 4 split**: Phase 4 is decomposed into 4a (util.py shell + tests), 4b (one pilot notebook end-to-end), 4c (remaining notebooks), 4d (cleanup + papermill smoke). Reduces single-task risk.
- **Notebook redundancy**: BERDL versions supersede non-BERDL siblings; dated copies (`*_20251024.ipynb`) and `*Old*` and `*Mockup*` notebooks are dropped during Phase 4.
- **fold_change multi-column support (Phase 3.5)**: `VectorStore.fold_change()` now accepts `aggregate` (Literal["mean","median","max","min"] | None). When set, multi-column inputs are auto-aggregated to single-column via intermediate vectors (ids `{id}__num_agg` / `{id}__den_agg`) before computing fold change. Derivation chain is preserved in the catalog.
- **Cross-notebook cache resolution (Phase 3.5)**: Verified that the cache is project-wide by design — all notebooks sharing the same `.kbcache/` dir share one SQLite catalog. The `notebook_name` parameter only affects access_log provenance, not data visibility. Added `Cache.list(created_by_notebook=)` filter that queries access_log for the originating notebook. Updated `Cache.load()` docstring to document this behavior.
- **Media composition resolution (Phase 3.5)**: Verified that `Media(source="kbase")` with no `inline_composition` is valid at construction and can be registered in a Sample without error. Added `Media.resolve_composition(session)` method: returns `inline_composition` for inline source, raises `NotImplementedError` stubs for kbase/msmedia sources (to be wired to real lookups later).
