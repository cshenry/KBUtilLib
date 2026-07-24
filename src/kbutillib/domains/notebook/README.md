# kbutillib.domains.notebook — Provenanced Notebook Utilities

Provenanced caching, typed vector storage, experiment tracking, and Escher metabolic map
rendering for Jupyter notebook workflows.

---

## What lives here

`NotebookSession` is the main entry point: it wraps a SQLite-backed catalog, a content-addressed
blob cache, a typed Parquet vector store, and an experiment registry into one session object. A
`@session.cache.cached` decorator lets you annotate expensive functions so their results are
loaded from disk on subsequent runs. `EscherUtils` provides Escher metabolic map rendering
integrated with KBase model data.

## Canonical imports

```python
from kbutillib.domains.notebook.notebook_session import NotebookSession
from kbutillib.domains.notebook.escher_utils import EscherUtils
```

---

## Modules

| File | Class(es) | Purpose |
|------|-----------|---------|
| `notebook_session.py` | `NotebookSession` | Session entry point — holds catalog, cache, vectors, experiments, strains |
| `cache.py` | `Cache` | Generic blob save/load with content-hashing and provenance tracking |
| `vector_store.py` | `VectorStore` | Typed numerical data storage (Parquet-backed) |
| `experiment_store.py` | `ExperimentStore` | Register and retrieve samples, computations, external datasets |
| `strain_store.py` | `StrainStore` | Register biological strains with mutations |
| `schema.py` | `VectorType`, `EntityKind`, `Sample`, `Media`, `Computation` | Pydantic v2 models for typed storage |
| `escher_utils.py` | `EscherUtils`, `EscherUtilsImpl` | Escher metabolic map rendering |

---

## Optional dependencies

| Package | Enables | Install |
|---------|---------|---------|
| `pyarrow` | Parquet-backed vector store | included in core dependencies |
| `pandas` | DataFrame serializer | `pip install pandas` (or `notebook` dev group) |
| `ipywidgets` | Interactive Jupyter widgets | `pip install ipywidgets` |
| `escher` | Metabolic map rendering | `pip install escher` |

---

## Usage example

```python
from kbutillib.domains.notebook.notebook_session import NotebookSession

session = NotebookSession.for_notebook()

# Generic cache: save and load arbitrary objects
session.cache.save("my_data", {"key": "value"})
obj = session.cache.load("my_data")

# Compute-or-load: run once, reload from disk on subsequent calls
@session.cache.cached("expensive_result")
def compute_something():
    return run_heavy_computation()

result = compute_something()

# Typed vector store
from kbutillib.domains.notebook.schema import VectorType, EntityKind

vtype = VectorType(domain="transcriptomics", scale="normalized_relative")
session.vectors.from_dataframe(
    df,
    id="expr_data",
    experiment_id="exp1",
    type=vtype,
    entity_kind=EntityKind.GENE,
    entity_namespace="ADP1",
)

# Register an experiment sample
from kbutillib.domains.notebook.schema import Sample, Media

sample = Sample(id="exp1", media=Media(id="LB"), strains={"wt": 1.0})
session.experiments.register_sample(sample)
```

```python
# Escher metabolic map
from kbutillib.domains.notebook.escher_utils import EscherUtils

escher = EscherUtils()
escher.render_map(model=my_model, fluxes=solution.fluxes)
```

---

## On-disk layout

```
.kbcache/
├── catalog.sqlite       # SQLite catalog with provenance
├── blobs/               # content-addressed generic blobs
│   └── <sha256>.<ext>
└── vectors/             # per-vector Parquet files
    └── <vector_id>.parquet
```

---

## Registered serializers

`json`, `dict`, `dataframe`, `text`, `msgenome`, `cobra_model`, `msmodelutil`, `msexpression`.
No pickle — all serializers produce human-inspectable or interoperable formats.

---

## Available capabilities

Capabilities are registered on each `*Impl` class where they are present. Run:

```bash
kbu cap list --domain notebook
```

---

## Adding a capability here

See root `CONTRIBUTING.md` → "Add a domain capability".

1. Add the method to `EscherUtilsImpl` or a new `*Impl` class in this domain.
2. Decorate with `@capability(domain="notebook", summary="...", tags=("notebook",))`.
3. Run `kbu cap list --domain notebook` to confirm registration.
