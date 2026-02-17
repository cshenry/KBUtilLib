# KBDatalakeApps Architecture Reference

## Complete File Map

```
KBDatalakeApps/
├── KBDatalakeApps.spec              # KIDL specification (1 method: build_genome_datalake_tables)
├── kbase.yml                        # Module metadata (v0.0.1, owners: chenry, filipeliu, vibhavsetlur)
├── Dockerfile                       # Multi-env container (SDK Python + berdl_genomes venv + modelseedpy_ml venv)
├── Makefile                         # SDK build commands
├── requirements.txt                 # SDK env: pandas, cobra, ModelSEEDpy, cobrakbase, polars, berdl-genomes
├── requirements_kbase.txt           # KBase SDK base requirements
├── README.md                        # Module documentation
│
├── lib/
│   ├── KBDatalakeApps/
│   │   ├── __init__.py
│   │   ├── KBDatalakeAppsImpl.py    # SDK entry point - orchestrates all pipeline stages
│   │   ├── KBDatalakeAppsServer.py  # Auto-generated server (do not edit)
│   │   ├── KBDatalakeUtils.py       # MAIN UTILITIES (~1700 lines) - KBDataLakeUtils class + standalone functions
│   │   ├── ontology_enrichment.py   # OntologyEnrichment class for BERDL/KEGG/NCBI API queries
│   │   ├── build_berdl_db.py        # SQLite database builder from pipeline outputs
│   │   ├── authclient.py            # Auto-generated auth client
│   │   ├── annotation/
│   │   │   ├── __init__.py
│   │   │   └── annotation.py        # run_rast, run_kofam, run_bakta, run_psortb annotation functions
│   │   └── executor/
│   │       ├── __init__.py
│   │       ├── task_executor.py      # TaskExecutor - ThreadPoolExecutor wrapper with TaskHandle tracking
│   │       └── task.py              # task_rast, task_kofam, task_bakta, task_psortb task wrappers
│   ├── installed_clients/           # Auto-generated KBase service clients
│   │   ├── RAST_SDKClient.py
│   │   ├── kb_baktaClient.py
│   │   ├── kb_psortbClient.py
│   │   ├── kb_kofamClient.py
│   │   ├── DataFileUtilClient.py
│   │   ├── KBaseReportClient.py
│   │   └── WorkspaceClient.py
│   └── kbase_catalog_client.py      # KBase catalog API client
│
├── berdl/                           # BERDL Python library (installed as berdl-genomes package)
│   ├── setup.py                     # Package setup (name: berdl-genomes)
│   ├── __init__.py
│   └── berdl/
│       ├── __init__.py
│       ├── pipeline.py              # Genome pipeline entry point (called by run_genome_pipeline.sh)
│       ├── prep_genome_set.py       # BERDLPreGenome: genome extraction, skani ANI, clade assignment
│       ├── genome_paths.py          # GenomePaths: directory structure constants
│       ├── hash_seq.py              # ProteinSequence: MD5 hashing for protein dedup
│       ├── fitness.py               # Fitness data processing
│       ├── pangenome/
│       │   ├── __init__.py
│       │   ├── pangenome.py         # Pangenome clustering with MMseqs2
│       │   └── paths_pangenome.py   # PathsPangenome: pangenome directory constants
│       ├── query/                   # BERDL query modules
│       │   ├── __init__.py
│       │   ├── query_genome.py
│       │   ├── query_genome_local.py
│       │   ├── query_pangenome.py
│       │   ├── query_pangenome_berdl.py
│       │   ├── query_pangenome_local.py
│       │   ├── query_pangenome_parquet.py
│       │   ├── query_ontology.py
│       │   └── query_ontology_local.py
│       ├── tables/
│       │   ├── __init__.py
│       │   └── datalake_table.py    # DatalakeTableBuilder: assembles SQLite from pipeline output
│       ├── ontology/
│       │   └── transform.py         # Ontology data transformations
│       ├── prediction/
│       │   └── phenotype.py         # Phenotype prediction utilities
│       ├── ontology_enrichment.py   # Standalone ontology enrichment module
│       ├── ontology_enrichment_local.py
│       └── bin/                     # CLI entry points
│           ├── pangenome.py         # Pangenome pipeline CLI (called by run_pangenome_pipeline.sh)
│           ├── annotation.py        # Annotation pipeline CLI (called by run_annotation.sh)
│           ├── model_pipeline.py    # Model pipeline CLI (called by run_model_pipeline.sh)
│           ├── table.py             # Table generation CLI (called by run_generate_table.sh)
│           ├── enrich_pangenome_features.py
│           └── phenotype_ml_predict.py
│
├── scripts/                         # Shell scripts for subprocess pipeline stages
│   ├── entrypoint.sh               # Docker entrypoint
│   ├── run_genome_pipeline.sh       # Activates berdl_genomes venv -> berdl/pipeline.py
│   ├── run_pangenome_pipeline.sh    # Activates berdl_genomes venv -> berdl/bin/pangenome.py
│   ├── run_annotation.sh           # Activates berdl_genomes venv -> berdl/bin/annotation.py
│   ├── run_model_pipeline.sh       # Uses SDK Python -> berdl/bin/model_pipeline.py
│   ├── run_generate_table.sh       # Activates berdl_genomes venv -> berdl/bin/table.py
│   └── run_async.sh
│
├── ui/narrative/methods/build_genome_datalake_tables/
│   ├── spec.json                    # 13 parameters: input_refs, suffix, save_models, 4 skip_*, 6 export_*
│   └── display.yaml                # UI labels and descriptions
│
├── data/                            # Reference data (shipped in Docker image)
│   ├── html/                        # HTML viewer template (index.html for BERDL table viewer)
│   ├── full_phenotype_set.json      # Phenotype definitions for simulation
│   ├── experimental_data.json       # Experimental growth data for validation
│   ├── essential_genes.csv          # Essential gene list for essentiality analysis
│   ├── knn_ACNP_RAST_full_01_17_2023_features.json  # ML classifier features
│   └── knn_ACNP_RAST_full_01_17_2023.pickle         # ML classifier model
│
├── test/
│   └── KBDatalakeApps_server_test.py
│
├── notebooks/                       # Development/testing notebooks
│   ├── RunBERDLTablesPipeline.ipynb
│   ├── test_pipeline_steps.ipynb
│   └── module_registration_testing.ipynb
│
└── biokbase/                        # KBase base library overlay
    └── user-env.sh
```

## Class Hierarchy and Inheritance

```
KBUtilLib BaseUtils
├── SharedEnvUtils
│   ├── KBWSUtils
│   │   └── KBGenomeUtils                    (genome operations, TSV export)
│   ├── MSReconstructionUtils                 (model building, gapfilling)
│   └── MSFBAUtils                           (FBA analysis, media handling)
│
└── KBDataLakeUtils(KBGenomeUtils, MSReconstructionUtils, MSFBAUtils)
    ├── Inherits genome parsing from KBGenomeUtils
    ├── Inherits model building from MSReconstructionUtils
    ├── Inherits FBA/media from MSFBAUtils
    ├── run_user_genome_to_tsv()             # Export genome to TSV
    ├── build_phenotype_tables()             # 3 TSV tables from phenosim results
    ├── build_model_tables()                 # Reaction tables + gene-reaction mapping
    ├── add_model_data_to_genome_table()     # Augment genome table with model stats
    ├── pipeline_build_sqllite_db()          # Full SQLite assembly
    └── pipeline_save_kbase_report()         # HTML report generation
```

## Data Flow Through the Pipeline

```
User Input (Genome/GenomeSet refs)
         │
         ▼
┌─────────────────────────────────┐
│  1. GENOME PIPELINE             │  Environment: berdl_genomes
│  - Export genomes from WS       │  Script: run_genome_pipeline.sh
│  - Run skani ANI                │  Entry: berdl/pipeline.py
│  - Assign to BERDL clades       │
│  - Output: .faa, .fna, TSVs    │
│  - Output: user_to_clade.json  │
└─────────┬───────────────────────┘
          │
          ▼
┌─────────────────────────────────┐
│  2. ANNOTATION PIPELINE         │  Environment: SDK + berdl_genomes
│  (parallel per genome)          │  TaskExecutor: 4 threads
│  - RAST (SDK env, threaded)     │
│  - KOfam (SDK env, threaded)    │  Input: .faa files
│  - Bakta (SDK env, threaded)    │  Output: _rast.tsv, _annotation_*.tsv
│  - PSORTb (SDK env, threaded)   │
│  Also: berdl annotation script  │  Script: run_annotation.sh
│  for pangenome members          │  Entry: berdl/bin/annotation.py
└─────────┬───────────────────────┘
          │
          ▼
┌─────────────────────────────────┐
│  3. PANGENOME PIPELINE          │  Environment: berdl_genomes
│  (per clade directory)          │  Script: run_pangenome_pipeline.sh
│  - MMseqs2 protein clustering   │  Entry: berdl/bin/pangenome.py
│  - Core/accessory classification│
│  - Output: parquet clusters     │
└─────────┬───────────────────────┘
          │
          ▼
┌─────────────────────────────────┐
│  4. MODELING PIPELINE           │  Environment: SDK Python
│  (ProcessPoolExecutor: 10)      │  Script: run_model_pipeline.sh
│  4a. Model Reconstruction       │  Entry: berdl/bin/model_pipeline.py
│    - MSGenome from TSV          │
│    - Genome classification      │  Functions: run_model_reconstruction()
│    - MSBuilder model build      │             run_phenotype_simulation()
│    - ATP correction + gapfill   │
│    - pFBA + FVA analysis        │
│  4b. Phenotype Simulation       │
│    - Load phenotype set         │
│    - Gapfill per condition      │
│    - Classify growth/no-growth  │
└─────────┬───────────────────────┘
          │
          ▼
┌─────────────────────────────────┐
│  5. TABLE GENERATION            │  Environment: berdl_genomes
│  (per clade)                    │  Script: run_generate_table.sh
│  - DatalakeTableBuilder.build() │  Entry: berdl/bin/table.py
│  - ANI table                    │
│  - user_feature table           │  Also: KBDatalakeUtils methods
│  - pangenome_feature table      │  build_model_tables()
│  - Ontology enrichment          │  build_phenotype_tables()
│  - Model/reaction tables        │  generate_ontology_tables()
└─────────┬───────────────────────┘
          │
          ▼
┌─────────────────────────────────┐
│  6. REPORT GENERATION           │  Environment: SDK Python
│  - Copy HTML template           │  In: KBDatalakeAppsImpl.py
│  - Upload to Shock              │
│  - Create file_links            │
│  - create_extended_report()     │
└─────────────────────────────────┘
```

## Dockerfile Architecture

The Dockerfile is complex because it builds multiple environments:

```dockerfile
# Base: Python 3.10 slim
FROM python:3.10-slim-bullseye

# System deps: build-essential, git, openjdk-11, wget, curl, gcc, cmake

# 1. UV package manager for venv creation
# 2. berdl_genomes venv (/opt/env/berdl_genomes/) - for BERDL library
# 3. modelseedpy_ml venv (/opt/env/modelseedpy_ml/) - for ML classifier
# 4. ML classifier data files (knn_ACNP_RAST features + pickle)
# 5. KBase SDK from kbase/kb-sdk:1.2.1
# 6. KBase requirements
# 7. Rust toolchain (for skani compilation)
# 8. skani built from source
# 9. MMseqs2 pre-built binary
# 10. /deps/ directory:
#     - ModelSEEDDatabase (pinned commit)
#     - ModelSEEDpy (cshenry fork)
#     - cobrakbase (cshenry fork, pinned commit)
#     - KBUtilLib (cshenry fork)
#     - cb_annotation_ontology_api
# 11. Module code (COPY ./ /kb/module)
# 12. berdl_genomes requirements installed
# 13. make all (SDK compilation)
```

**Important paths in the container:**
- `/kb/module/` - Module root
- `/kb/module/data/` - Reference data
- `/data/` - Mounted reference data (runtime)
- `/data/reference_data/berdl_db/` - BERDL reference database
- `/data/reference_data/phenotype_data/` - Fitness and phenotype reference data
- `/deps/` - Git-cloned dependencies (ModelSEEDpy, cobrakbase, KBUtilLib, etc.)
- `/opt/env/berdl_genomes/` - BERDL Python venv
- `/opt/env/modelseedpy_ml/` - ModelSEEDpy ML venv

## KBDataLakeUtils Initialization

```python
# In KBDatalakeAppsImpl.__init__:
self.util = KBDataLakeUtils(
    kbendpoint=config["kbase-endpoint"],
    reference_path="/data/",
    module_path="/kb/module"
)
self.util.set_token(get_berdl_token(), namespace="berdl")

# KBDataLakeUtils.__init__ calls super().__init__() which initializes:
# - KBGenomeUtils: genome parsing methods, build_genome_tsv()
# - MSReconstructionUtils: build_metabolic_model(), gapfill_metabolic_model(), get_template(), get_classifier()
# - MSFBAUtils: set_media(), get_media(), FBA utilities
# - SharedEnvUtils (via chain): token management, config
# - BaseUtils (via chain): logging, provenance
```

## Communication Between Pipelines

Pipelines communicate via the filesystem (JSON files in $scratch):

1. **input_params.json**: Written by Impl, read by all subprocess pipelines. Contains:
   - `params` (user parameters)
   - `_ctx` (SDK context with token)
   - `_config` (SDK config with endpoints)
   - `_genome_refs` (resolved genome references)

2. **model_pipeline_params.json**: Written by Impl for model pipeline. Contains:
   - `input_refs`, `token`, `scratch`, `kbase_endpoint`, `kbversion`, `max_phenotypes`

3. **user_to_clade.json**: Written by genome pipeline, read by Impl and table builder.
   Maps genome names to pangenome clade IDs.

4. **\*_data.json files**: Written by model reconstruction, read by table builders.
   Contains model info, reactions, metabolites, flux analysis, gapfilled reactions.

5. **\*_phenosim.json files**: Written by phenotype simulation, read by table builders.
   Contains per-phenotype growth class, objective values, gapfilling details.
