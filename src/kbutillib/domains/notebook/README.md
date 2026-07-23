# kbutillib.notebook — Provenanced Notebook Cache Engine

Entry point for provenanced caching, typed vector storage, and experiment tracking in Jupyter notebooks.

## Quick start

```python
from kbutillib.notebook import NotebookSession

session = NotebookSession.for_notebook()

# Generic cache
session.cache.save("my_data", {"key": "value"})
obj = session.cache.load("my_data")

# Typed vectors
from kbutillib.notebook.schema import VectorType, EntityKind
vtype = VectorType(domain="transcriptomics", scale="normalized_relative")
session.vectors.from_dataframe(df, id="expr_data", experiment_id="exp1",
                                type=vtype, entity_kind=EntityKind.GENE,
                                entity_namespace="ADP1")

# Experiments
from kbutillib.notebook.schema import Sample, Media
sample = Sample(id="exp1", media=Media(id="LB"), strains={"wt": 1.0})
session.experiments.register_sample(sample)

# Compute-or-load decorator
@session.cache.cached("expensive_result")
def compute_something():
    return heavy_computation()
```

## Key classes

- **NotebookSession** — entry point; holds catalog, paths, env detection
- **Cache** — generic blob save/load with content-hashing and provenance
- **VectorStore** — typed numerical data (Parquet-backed)
- **ExperimentStore** — register Sample, Computation, ExternalDataset
- **StrainStore** — register biological strains with mutations

## On-disk layout

```
.kbcache/
├── catalog.sqlite       # SQLite catalog with provenance
├── blobs/               # content-addressed generic blobs
│   └── <hash>.<ext>
└── vectors/             # per-vector Parquet files
    └── <vector_id>.parquet
```

## Serializers

Registered: json, dict, dataframe, text, msgenome, cobra_model, msmodelutil, msexpression. No pickle.
