# KBUtilLib Module Reference

Complete reference for all utility modules in KBUtilLib.

> **Architecture note (2026-05):** KBUtilLib was refactored from multi-inheritance to **composition**. Each utility class is now `*Impl` (e.g. `MSFBAUtilsImpl`, `KBWSUtilsImpl`) and **holds** a `SharedEnvUtils` rather than inheriting from it. The `KBUtilLib` facade in `src/kbutillib/toolkit.py` lazy-instantiates all sub-utilities and wires composed dependencies automatically.
> Legacy class names (`KBWSUtils`, `MSFBAUtils`, etc.) are still exported as aliases in `__init__.py` for import compatibility, but constructor signatures changed — prefer the facade.

## Composition graph (PRD §6.3)

The `KBUtilLib` facade exposes 25 sub-utilities as lazy properties. Each `*Impl` holds `SharedEnvUtils` and any composed sibling Impls per the table below.

```
KBUtilLib (toolkit.py)
├── env: SharedEnvUtils
└── lazy properties:
    ├── ws → KBWSUtilsImpl(env)
    ├── callback → KBCallbackUtilsImpl(env, ws)
    ├── annotation → KBAnnotationUtilsImpl(env, ws, callback)
    ├── biochem → MSBiochemUtilsImpl(env)
    ├── model → KBModelUtilsImpl(env, ws, annotation, biochem)
    ├── fba → MSFBAUtilsImpl(env, model)
    ├── recon → MSReconstructionUtilsImpl(env, model)
    ├── escher → EscherUtilsImpl(env, model, biochem)
    ├── standardize → ModelStandardizationUtilsImpl(env, biochem)
    ├── genome → KBGenomeUtilsImpl(env, ws)
    ├── plm → KBPLMUtilsImpl(env, genome)
    ├── bvbrc → BVBRCUtilsImpl(env, genome, annotation)
    ├── reads → KBReadsUtilsImpl(env, ws)
    ├── sdk → KBSDKUtilsImpl(env, ws)
    ├── argo → ArgoUtilsImpl(env)
    ├── curation → AICurationUtilsImpl(env, argo)
    ├── thermo → ThermoUtilsImpl(env, biochem)
    ├── mmseqs → MMSeqsUtilsImpl(env)
    ├── skani → SKANIUtilsImpl(env)
    ├── berdl → KBBERDLUtilsImpl(env)
    ├── patric → PatricWSUtilsImpl(env)
    ├── uniprot → KBUniProtUtilsImpl(env)
    ├── pdb → RCSBPDBUtilsImpl(env)
    ├── catalog → CatalogClient (standalone)
    └── jobs → KBJobUtils(env)  # composition reference
```

## Foundation Layer

### BaseUtils
**Location:** `src/kbutillib/base_utils.py`
**Purpose:** Base class still inherited where appropriate (e.g. `SharedEnvUtils`). Not part of `*Impl` chain — the new pattern composes `SharedEnvUtils` rather than inheriting from `BaseUtils`.

**Key Methods:**
- `initialize_call(method_name, params)` - Start provenance tracking
- `log_info(message)` / `log_debug(message)` / `log_error(message)` - Logging
- `validate_args(required_args, provided_args)` - Argument validation
- `save_util_data(filename, data)` - Save JSON data
- `load_util_data(filename)` - Load JSON data

**Attributes:**
- `logger` - Configured logging instance
- `provenance` - List of tracked method calls

### SharedEnvUtils
**Location:** `src/kbutillib/shared_env_utils.py`
**Inherits:** BaseUtils
**Purpose:** Configuration and authentication management. **HELD by every `*Impl`** — never inherit from this in new code.

**Key Methods:**
- `load_config(config_file=None)` - Load YAML configuration
- `get_token(namespace="kbase")` - Get authentication token
- `set_token(token, namespace="kbase")` - Set authentication token
- `get_config_value(key)` - Get config value by dot-notation path

**Configuration Priority:**
1. Explicit `config_file` parameter
2. `~/kbutillib_config.yaml` (user config)
3. `repo/config/default_config.yaml` (defaults)

**Token Namespaces:**
- `kbase` - KBase authentication
- `argo` - Argo LLM service
- Custom namespaces as needed

## Flat-module helpers (no class — direct function imports)

Extracted during the composition refactor for shared use across `*Impl` classes:

### `kbase_endpoints.py`
URL helpers: `base_url`, `service_url`, `narrative_url`, `env_from_url`. Used by every `*Impl` that hits a KBase service.

### `model_directionality.py`
Reaction direction analysis. Replaces the broken `KBModelUtils.model_reaction_directionality_analysis` and centralizes `direction_conversion`. Functions: `directionality_from_bounds`, `biochem_directionality`, `combine_directionality_signals`.

### `model_helpers.py`
Canonical `_check_and_convert_model(model) -> MSModelUtil` and `_parse_id(object_or_id) -> tuple`. Replaces the previous triplicate copies in `kb_model_utils`, `ms_biochem_utils`, `thermo_utils`.

### `compartments.py`
`compartment_types` mapping (the complete version) and `normalize_compartment(compartment) -> str`.

## Data Access Layer

### KBWSUtilsImpl (legacy alias: `KBWSUtils`)
**Location:** `src/kbutillib/kb_ws_utils.py`
**Composes:** `env: SharedEnvUtils`
**Facade attribute:** `kbu.ws`
**Purpose:** KBase Workspace Service API access.

**Key Methods:**
- `get_object(workspace_id, object_ref)` - Retrieve any workspace object
- `save_object(workspace_id, obj_type, data, name)` - Save object
- `list_objects(workspace_id, type_filter=None)` - List workspace objects
- `get_object_info(workspace_id, object_ref)` - Get object metadata
- `get_type_spec(type_name)` - Get type specification
- `is_ref(s)` - Check if string is a workspace reference

**Workspace Reference Formats:**
- `ws_id/obj_name` - By workspace ID and name
- `ws_id/obj_name/version` - Specific version
- `obj_id` - Direct object ID

### PatricWSUtilsImpl (legacy alias: `PatricWSUtils`)
**Location:** `src/kbutillib/patric_ws_utils.py`
**Composes:** `env`
**Facade attribute:** `kbu.patric`
**Purpose:** PATRIC/BV-BRC Workspace access.

**Key Methods:**
- `get_patric_object(path)` - Get object from PATRIC workspace
- `list_patric_workspace(path)` - List workspace contents
- `get_patric_genome(genome_id)` - Get genome object
- `get_patric_model(model_id)` - Get metabolic model

## Bioinformatics Analysis Layer

### KBGenomeUtilsImpl (legacy alias: `KBGenomeUtils`)
**Location:** `src/kbutillib/kb_genome_utils.py`
**Composes:** `env`, `ws`
**Facade attribute:** `kbu.genome`
**Purpose:** Genome data analysis and manipulation.

**Key Methods:**
- `get_genome(workspace_id, genome_ref)` - Retrieve genome object (uses `self.ws.get_object`)
- `get_features(genome)` - Get all features
- `get_features_by_type(genome, feature_type)` - Filter by type (CDS, rRNA, etc.)
- `get_features_by_function(genome, function_pattern)` - Filter by function
- `translate_feature(feature)` - DNA to protein translation
- `translate_features(features)` - Bulk translation
- `get_contig_sequences(genome)` - Get contig sequences
- `reverse_complement(seq)` - Reverse complement
- `translate_sequence(seq)` - Translate DNA to protein

**Feature Types:**
- `CDS` - Coding sequences
- `rRNA`, `tRNA` - RNA features
- `gene`, `mRNA` - Gene annotations

### KBAnnotationUtilsImpl (legacy alias: `KBAnnotationUtils`)
**Location:** `src/kbutillib/kb_annotation_utils.py`
**Composes:** `env`, `ws`, `callback`
**Facade attribute:** `kbu.annotation`
**Purpose:** Gene and protein annotation management.

**Key Methods:**
- `get_annotations(genome)` - Get all annotations
- `get_annotation_events(genome)` - Get annotation event history
- `filter_annotations_by_ontology(annotations, ontology)` - Filter by source
- `get_ec_numbers(feature)` - Extract EC numbers
- `get_kegg_ids(feature)` - Extract KEGG identifiers
- `map_function_to_reactions(function)` - Map functional role to reactions
- `translate_term_to_modelseed(term)` - Term → ModelSEED ID

**Supported Ontologies:** EC, KEGG, MetaCyc, UniProt, GO

### MSBiochemUtilsImpl (legacy alias: `MSBiochemUtils`)
**Location:** `src/kbutillib/ms_biochem_utils.py`
**Composes:** `env`
**Facade attribute:** `kbu.biochem`
**Purpose:** ModelSEED biochemistry database access.

**Key Methods:**
- `search_compounds(query)` - Search compounds by name/ID/formula
- `search_reactions(query)` - Search reactions by name/equation
- `get_compound_by_id(compound_id)` - Get compound by ID (cpd00001)
- `get_reaction(reaction_id)` - Get reaction by ID (rxn00001)
- `get_reaction_stoichiometry(reaction_id)` - Get stoichiometry dict
- `search_by_formula(formula)` - Find compounds by molecular formula
- `search_by_inchikey(inchikey)` - Find by structure
- `reaction_directionality_from_bounds(reaction)` - Wraps `model_directionality.directionality_from_bounds`
- `reaction_biochem_directionality(reaction)` - Wraps `model_directionality.biochem_directionality`

**ID Formats:**
- Compounds: `cpd#####` (e.g., cpd00001 = H2O)
- Reactions: `rxn#####` (e.g., rxn00001)

## Metabolic Modeling Layer

### KBModelUtilsImpl (legacy alias: `KBModelUtils`)
**Location:** `src/kbutillib/kb_model_utils.py`
**Composes:** `env`, `ws`, `annotation`, `biochem`
**Facade attribute:** `kbu.model`
**Purpose:** Metabolic model analysis and manipulation.

**Key Methods:**
- `get_model(workspace_id, model_ref)` - Get FBA model
- `get_model_reactions(model)` - List model reactions
- `get_model_metabolites(model)` - List model metabolites
- `get_model_genes(model)` - List model genes
- `add_reaction(model, reaction)` - Add reaction to model
- `remove_reaction(model, reaction_id)` - Remove reaction
- `get_template(template_name)` - Get reconstruction template

**Model Object Structure:**
- `modelreactions` - List of reactions
- `modelcompounds` - List of metabolites
- `modelgenes` - List of genes
- `biomasses` - Biomass objective functions

### MSFBAUtilsImpl (legacy alias: `MSFBAUtils`)
**Location:** `src/kbutillib/ms_fba_utils.py`
**Composes:** `env`, `model`
**Facade attribute:** `kbu.fba`
**Purpose:** Flux Balance Analysis operations.

**Key Methods:**
- `run_fba(model, biomass_reaction="bio1", media=None)` - Run FBA simulation
- `run_pfba(model)` - Parsimonious FBA
- `run_fva(model, reactions=None)` — **AP3 carve-out**: working FVA implementation; `cobra.flux_variability_analysis` is broken; do not replace.
- `set_media(model, media_id)` - Configure growth media
- `set_objective(model, reaction_id)` - Set objective function
- `add_constraint(model, constraint)` - Add flux constraint
- `set_fraction_of_optimum(model, fraction)` - Set optimality fraction
- `analyzed_reaction_objective_coupling(model)` — **AP3 carve-out**: KO-impact-on-biomass; not the same operation as `cobra.single_reaction_deletion`.
- `fit_flux_to_mutant_growth_rate_data(...)` — **AP3 carve-out**: specific science code; do NOT move into ModelSEEDpy.MSExpression.

**Media Options:**
- `Complete` - Rich media
- `Minimal` - Minimal glucose
- Custom media definitions

### MSReconstructionUtilsImpl (legacy alias: `MSReconstructionUtils`)
**Location:** `src/kbutillib/ms_reconstruction_utils.py`
**Composes:** `env`, `model`
**Facade attribute:** `kbu.recon`
**Purpose:** Genome-scale model reconstruction.

**Key Methods:**
- `build_model_from_genome(genome, template)` - Build draft model
- `gapfill_model(model, media)` - Gap-fill model
- `prune_model(model)` - Remove unnecessary reactions
- `integrate_phenotypes(model, phenotype_data)` - Add phenotype constraints

### EscherUtilsImpl (legacy alias: `EscherUtils`)
**Location:** `src/kbutillib/escher_utils.py`
**Composes:** `env`, `model`, `biochem`
**Facade attribute:** `kbu.escher`
**Purpose:** Escher pathway map visualization.

**Key Methods:**
- `create_map(reactions, layout=None)` - Create Escher map
- `create_map_html2(model, fluxes, ...)` - Enhanced map with badges/legends
- `visualize_fluxes(map, fba_solution)` - Overlay flux values
- `set_reaction_colors(map, color_dict)` - Custom reaction coloring
- `save_map(map, filename)` - Save to file
- `load_map(filename)` - Load existing map
- `list_available_maps()` - List built-in maps

### ModelStandardizationUtilsImpl (legacy alias: `ModelStandardizationUtils`)
**Location:** `src/kbutillib/model_standardization_utils.py`
**Composes:** `env`, `biochem`
**Facade attribute:** `kbu.standardize`
**Purpose:** Standardize model IDs / compartments / reaction directions.

## External API Layer

### BVBRCUtilsImpl (legacy alias: `BVBRCUtils`)
**Location:** `src/kbutillib/bvbrc_utils.py`
**Composes:** `env`, `genome`, `annotation`
**Facade attribute:** `kbu.bvbrc`
**Purpose:** BV-BRC (formerly PATRIC) API access.

**Key Methods:**
- `get_bvbrc_genome(genome_id)` - Fetch genome by ID
- `search_bvbrc_genomes(query)` - Search genomes
- `get_genome_features(genome_id)` - Get genome features
- `get_genome_sequences(genome_id)` - Get contig sequences
- `convert_to_kbase(bvbrc_genome)` - Convert to KBase format

### KBUniProtUtilsImpl (legacy alias: `KBUniProtUtils`)
**Location:** `src/kbutillib/kb_uniprot_utils.py`
**Composes:** `env`
**Facade attribute:** `kbu.uniprot`
**Purpose:** UniProt REST API integration.

### RCSBPDBUtilsImpl (legacy alias: `RCSBPDBUtils`)
**Location:** `src/kbutillib/rcsb_pdb_utils.py`
**Composes:** `env`
**Facade attribute:** `kbu.pdb`
**Purpose:** RCSB PDB structure database access.

## AI/ML Layer

### ArgoUtilsImpl (legacy alias: `ArgoUtils`)
**Location:** `src/kbutillib/argo_utils.py`
**Composes:** `env`
**Facade attribute:** `kbu.argo`
**Purpose:** Argo LLM service integration. Gateway client lazy-constructed (Task 3 fix).

**Key Methods:**
- `query_argo(prompt, model="gpt4o")` - Send LLM query
- `query_argo_async(prompt, model)` - Async query with polling
- `get_available_models()` - List available models

**Available Models:** gpt4o, gpt3mini, o1, o1-mini, o3-mini

### AICurationUtilsImpl (legacy alias: `AICurationUtils`)
**Location:** `src/kbutillib/ai_curation_utils.py`
**Composes:** `env`, `argo`
**Facade attribute:** `kbu.curation`
**Purpose:** AI-powered biochemistry curation.

**Key Methods:**
- `curate_reaction_direction(reaction)` - Determine reaction reversibility
- `categorize_stoichiometry(reaction)` - Categorize reaction type
- `evaluate_equivalence(rxn1, rxn2)` - Check reaction equivalence
- `assess_gene_reaction(gene, reaction)` - Validate gene-reaction association
- `get_cached_result(query_hash)` - Get cached curation result
- `cache_result(query_hash, result)` - Cache curation result

### KBPLMUtilsImpl (legacy alias: `KBPLMUtils`)
**Location:** `src/kbutillib/kb_plm_utils.py`
**Composes:** `env`, `genome`
**Facade attribute:** `kbu.plm`
**Purpose:** Protein language model integration.

## Other Utilities

### KBJobUtils (composition pilot)
**Location:** `src/kbutillib/kb_job_utils/`
**Composes:** `env`
**Facade attribute:** `kbu.jobs`
**Purpose:** EE2 job submission and local SQLite tracking. **Reference shape for the composition pattern** — read this when building new modules.

**Key Methods:**
- `submit(params, *, name=None, project=None, ...)` - Submit + track
- `refresh(job_id)` / `refresh_active()` / `refresh_all()` - Update local tracker
- `get(job_id)` / `list(...)` / `summary()` - Read from local store
- `cancel(job_id)` / `forget(job_id)` / `cleanup(...)` - Manage records
- `submit_chain(steps, ...)` / `advance_pipelines()` - Linear pipelines (Phase 3)
- `start_watcher(interval=300)` / `stop_watcher()` - In-process watcher thread

**CLI:** `kbu jobs status/list/refresh/logs/cancel/chain/...`, `kbu jobdaemon`

### MMSeqsUtilsImpl (legacy alias: `MMSeqsUtils`)
**Location:** `src/kbutillib/mmseqs_utils.py`
**Composes:** `env`
**Facade attribute:** `kbu.mmseqs`
**Purpose:** MMseqs2 sequence clustering / search.

### SKANIUtilsImpl (legacy alias: `SKANIUtils`)
**Location:** `src/kbutillib/skani_utils.py`
**Composes:** `env`
**Facade attribute:** `kbu.skani`
**Purpose:** Fast genome distance computation using skani.

### ThermoUtilsImpl (legacy alias: `ThermoUtils`)
**Location:** `src/kbutillib/thermo_utils.py`
**Composes:** `env`, `biochem`
**Facade attribute:** `kbu.thermo`
**Purpose:** Thermodynamic calculations on metabolic reactions/compounds.

### KBBERDLUtilsImpl (legacy alias: `KBBERDLUtils`)
**Location:** `src/kbutillib/kb_berdl_utils.py`
**Composes:** `env`
**Facade attribute:** `kbu.berdl`
**Purpose:** BERDL datalake integration.

### NotebookSession + helpers
**Location:** `src/kbutillib/notebook/`
**Purpose:** Jupyter notebook engine with cache, vectors, and `session.kbu` facade integration. Replaces the deleted `notebook_utils.py`.

## Import Patterns

```python
# Preferred: facade
from kbutillib import KBUtilLib
kbu = KBUtilLib()
kbu.fba.run_fba(model)

# Legacy aliases still work (constructor signatures changed):
from kbutillib import KBWSUtils, MSFBAUtils  # = KBWSUtilsImpl, MSFBAUtilsImpl

# Flat-module helpers — direct function imports:
from kbutillib.kbase_endpoints import base_url
from kbutillib.compartments import normalize_compartment
from kbutillib.model_directionality import directionality_from_bounds
from kbutillib.model_helpers import _parse_id
```

## Composition examples (no more multi-inheritance!)

```python
# Genomics workflow — facade
kbu = KBUtilLib()
genome = kbu.genome.get_genome(ws_id, "MyGenome/1")
annotations = kbu.annotation.get_annotations(genome)

# Metabolic modeling workflow — facade
model = kbu.model.get_model(ws_id, "MyModel/1")
solution = kbu.fba.run_fba(model, biomass_reaction="bio1")
fva = kbu.fba.run_fva(model)  # AP3 carve-out, working version

# AI curation workflow — facade
result = kbu.curation.curate_reaction_direction(reaction_data)

# Inside a notebook — session.kbu
from kbutillib.notebook import NotebookSession
session = NotebookSession(...)
session.kbu.fba.run_fba(model)
session.cache.save("fluxes", solution)  # session also exposes cache, vectors
```
