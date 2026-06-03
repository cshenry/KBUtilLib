# KBUtilLib API Quick Reference

Quick reference for the most commonly used APIs in KBUtilLib (post-2026-05 composition refactor).

> Use the `KBUtilLib` facade. The legacy multi-inheritance pattern (`class MyTools(A, B): pass`) is **deprecated** — `__init__.py` aliases keep imports resolving but constructor signatures changed.

## Configuration & Setup

### Initialize the facade

```python
from kbutillib import KBUtilLib

# Default configuration (auto-loads from standard locations)
kbu = KBUtilLib()

# Explicit configuration file
from kbutillib import SharedEnvUtils
env = SharedEnvUtils(config_file="/path/to/config.yaml")
kbu = KBUtilLib(env=env)

# With explicit token (passes through to SharedEnvUtils)
kbu = KBUtilLib(kbase_token="YOUR_TOKEN")
```

### Configuration File Format (YAML)
```yaml
# ~/kbutillib_config.yaml
kbase:
  endpoint: https://kbase.us/services
  workspace_url: https://kbase.us/services/ws

argo:
  endpoint: https://api.cels.anl.gov/argo/api/v1

modelseed:
  database_path: /path/to/ModelSEEDDatabase

logging:
  level: INFO
```

### Token Management
```python
# Get tokens via the held SharedEnvUtils
kbase_token = kbu.env.get_token("kbase")
argo_token = kbu.env.get_token("argo")

# Set tokens programmatically
kbu.env.set_token("NEW_TOKEN", namespace="kbase")

# Tokens can also be set via environment variables:
# KBASE_AUTH_TOKEN, ARGO_API_TOKEN
```

### Configuration Access
```python
# Modern API — use dot notation:
endpoint = kbu.env.get_config_value("kbase.endpoint")
db_path = kbu.env.get_config_value("modelseed.database_path")
cache_on = kbu.env.get_config_value("my_analysis.cache_enabled", default=False)

# get_config(section, key) is deprecated (INI-compatibility only).
# Always use get_config_value("section.key") in new code.
```

## KBase Workspace Operations

### Retrieve Objects
```python
kbu = KBUtilLib()

# Get any object
obj = kbu.ws.get_object(workspace_id=12345, object_ref="MyObject/1")

# Get with specific version
obj = kbu.ws.get_object(12345, "MyObject/3")

# Get object info (metadata only)
info = kbu.ws.get_object_info(12345, "MyObject")

# Check if string is a workspace ref
assert kbu.ws.is_ref("12345/6/7") is True
assert kbu.ws.is_ref("foo") is False
```

### List Objects
```python
# List all objects in workspace
objects = kbu.ws.list_objects(workspace_id=12345)

# Filter by type
genomes = kbu.ws.list_objects(12345, type_filter="KBaseGenomes.Genome")
models = kbu.ws.list_objects(12345, type_filter="KBaseFBA.FBAModel")
```

### Save Objects
```python
kbu.ws.save_object(
    workspace_id=12345,
    obj_type="KBaseGenomes.Genome",
    data=genome_data,
    name="MyNewGenome"
)
```

## Genome Operations

### Get and Analyze Genomes
```python
kbu = KBUtilLib()

# Get genome (delegates to kbu.ws under the hood)
genome = kbu.genome.get_genome(workspace_id=12345, genome_ref="MyGenome/1")

# Get all features
features = kbu.genome.get_features(genome)

# Filter by type
cds_features = kbu.genome.get_features_by_type(genome, "CDS")
rna_features = kbu.genome.get_features_by_type(genome, "rRNA")

# Filter by function
transporters = kbu.genome.get_features_by_function(genome, "transport")
```

### Sequence Translation
```python
# Translate single feature
protein_seq = kbu.genome.translate_feature(feature)

# Bulk translation
proteins = kbu.genome.translate_features(cds_features)

# Get contig sequences
contigs = kbu.genome.get_contig_sequences(genome)

# Pure-string helpers
rev = kbu.genome.reverse_complement("ATCG")          # "CGAT"
aa = kbu.genome.translate_sequence("ATGATGATG")
gc = kbu.genome.calculate_gc_content("ATCGATCG")     # 0.5
```

### Feature Lookup Cache
```python
# Load + cache a genome object (by workspace ref, workspace id, or local filename)
kbu.genome.load_kbase_gene_container("12345/6/7", ws=12345, localname="my_genome")

# Access cached feature list
features = kbu.genome.object_to_features("my_genome")

# Single-feature lookup by id
ftr = kbu.genome.get_ftr("my_genome", "gene_0001")

# Alias lookups
aliases = kbu.genome.ftr_to_aliases("my_genome", "gene_0001")  # list of alias strings
ftrs = kbu.genome.alias_to_ftrs("my_genome", "adh1")           # list of features
```

### Protein Extraction and Annotation
```python
# Extract protein sequences from a genome object
proteins = kbu.genome.object_to_proteins("12345/6/7")

# Update genome annotations (REQUIRES SDK callback context):
kbu.genome.add_annotations_to_object(reference, suffix="_reannotated", annotations=ann_dict)
```

### Local File Ingest and Taxonomy
```python
# BV-BRC local-file ingest (builds KBase Genome dict from BV-BRC directory layout)
genome_dict = kbu.genome.load_genome_from_local_files(
    genome_id="83332.12",
    features_dir="/data/bvbrc/features/",
    genomes_dir="/data/bvbrc/genomes/",
    metadata_dir="/data/bvbrc/metadata/",
)

# Taxonomy consensus across multiple genomes
taxonomy_result = kbu.genome.aggregate_taxonomies(genomes, asv_id="ASV_001", output_dir="/tmp/tax/")

# Create a synthetic merged genome from multiple source genomes
synth = kbu.genome.create_synthetic_genome(asv_id="ASV_001", genomes=genome_list)
```

### Save/Load/Validate Genome Objects (new in 2026-06 PRD)
```python
# Validate schema before saving (returns [] if valid)
errors = kbu.genome.validate_genome(genome_dict)
if errors:
    raise ValueError(f"Genome validation failed: {errors}")

# Validate pre-assembly (assembly_ref not yet known):
errors = kbu.genome.validate_genome(genome_dict, require_assembly_ref=False)

# Build a Genome dict from FASTA + optional GFF3
genome_dict = kbu.genome.build_genome_from_fasta_gff(
    fasta_path="/data/my.fasta",
    gff_path="/data/my.gff",             # optional
    scientific_name="Escherichia coli",
    taxonomy="Bacteria;Proteobacteria;...",
    genetic_code=11,
)

# Save genome dict directly to workspace (no EE2 job needed)
genome_ref = kbu.genome.save_genome_object(genome_dict, workspace="my_workspace", name="MyGenome")

# Save FASTA as an Assembly via EE2 job (returns assembly_ref string)
assembly_ref = kbu.genome.save_assembly_from_fasta(
    fasta_path="/data/my.fasta",
    workspace="my_workspace",
    name="MyAssembly",
)

# Orchestrate: save assembly + genome, return (assembly_ref, genome_ref)
assembly_ref, genome_ref = kbu.genome.save_genome_with_assembly(
    fasta_path="/data/my.fasta",
    genome_dict=genome_dict,
    workspace="my_workspace",
    base_name="MyGenome",
)
```

> **Notebook context note:** `save_genome_object` uses direct Workspace transport (no callback URL needed). `save_assembly_from_fasta` submits an EE2 job via `kbu.jobs.run_job`; no callback URL needed but requires EE2 access.
> See `/kbase-genome-expert` for the full genome notebook workflow guide.

## Annotation Operations

### Access Annotations
```python
kbu = KBUtilLib()

annotations = kbu.annotation.get_annotations(genome)
events = kbu.annotation.get_annotation_events(genome)
ec_annotations = kbu.annotation.filter_annotations_by_ontology(annotations, "EC")
kegg_annotations = kbu.annotation.filter_annotations_by_ontology(annotations, "KEGG")
```

### Extract Identifiers
```python
ec_numbers = kbu.annotation.get_ec_numbers(feature)         # ["1.1.1.1", "2.3.4.5"]
kegg_ids = kbu.annotation.get_kegg_ids(feature)             # ["K00001", "K00002"]
reactions = kbu.annotation.map_function_to_reactions("alcohol dehydrogenase")
modelseed_id = kbu.annotation.translate_term_to_modelseed("acetyl-CoA")
```

## ModelSEED Biochemistry

### Search Compounds
```python
kbu = KBUtilLib()

compounds = kbu.biochem.search_compounds("glucose")
atp = kbu.biochem.get_compound_by_id("cpd00002")
c6h12o6 = kbu.biochem.search_by_formula("C6H12O6")
compound = kbu.biochem.search_by_inchikey("WQZGKKKJIJFFOK-...")
```

### Search Reactions
```python
reactions = kbu.biochem.search_reactions("glycolysis")
reaction = kbu.biochem.get_reaction("rxn00001")
stoich = kbu.biochem.get_reaction_stoichiometry("rxn00001")
# Returns: {"cpd00001": -1, "cpd00002": 1, ...}

# Direction analysis (delegates to kbutillib.model_directionality)
direction = kbu.biochem.reaction_directionality_from_bounds(reaction)
biochem_dir = kbu.biochem.reaction_biochem_directionality(reaction)
```

## Metabolic Model Operations

### Get and Analyze Models
```python
kbu = KBUtilLib()

model = kbu.model.get_model(workspace_id=12345, model_ref="MyModel/1")
reactions = kbu.model.get_model_reactions(model)
metabolites = kbu.model.get_model_metabolites(model)
genes = kbu.model.get_model_genes(model)
```

### Modify Models
```python
kbu.model.add_reaction(model, reaction_data)
kbu.model.remove_reaction(model, "rxn00001_c0")
template = kbu.model.get_template("GramNegative")
```

## FBA Operations (preserves AP3 carve-outs)

### Run FBA
```python
kbu = KBUtilLib()

# Basic FBA
solution = kbu.fba.run_fba(model, biomass_reaction="bio1")
print(f"Objective value: {solution.objective_value}")

# FBA with specific media
kbu.fba.set_media(model, "Complete")
solution = kbu.fba.run_fba(model, biomass_reaction="bio1")

# Parsimonious FBA
solution = kbu.fba.run_pfba(model)
```

### Flux Analysis (AP3 carve-outs)

```python
# AP3: kbu.fba.run_fva is the WORKING FVA implementation.
# cobra.flux_variability_analysis is broken; do NOT replace.
fva_results = kbu.fba.run_fva(model)
fva_results = kbu.fba.run_fva(model, reactions=["rxn00001", "rxn00002"])

# AP3: KO impact on biomass — NOT cobra.single_reaction_deletion
coupling = kbu.fba.analyzed_reaction_objective_coupling(model)

# AP3: specific science code; do NOT move to ModelSEEDpy.MSExpression
fit = kbu.fba.fit_flux_to_mutant_growth_rate_data(model, mutant_data)

# Set fraction of optimum
kbu.fba.set_fraction_of_optimum(model, 0.9)
fva_results = kbu.fba.run_fva(model)
```

### Constraints and Objectives
```python
kbu.fba.set_objective(model, "bio1")  # Biomass reaction
kbu.fba.add_constraint(model, {
    "reaction": "rxn00001",
    "lower_bound": 0,
    "upper_bound": 10
})
```

## AI Curation

### Reaction Curation
```python
kbu = KBUtilLib()

result = kbu.curation.curate_reaction_direction(reaction_data)
category = kbu.curation.categorize_stoichiometry(reaction)
are_equivalent = kbu.curation.evaluate_equivalence(reaction1, reaction2)
```

### Gene-Reaction Assessment
```python
assessment = kbu.curation.assess_gene_reaction(gene_info, reaction_info)
```

### Caching
```python
cached = kbu.curation.get_cached_result(query_hash)
kbu.curation.clear_cache()
```

## External APIs

### BV-BRC
```python
kbu = KBUtilLib()
genome = kbu.bvbrc.get_bvbrc_genome("83332.12")
genomes = kbu.bvbrc.search_bvbrc_genomes("Escherichia coli")
kb_genome = kbu.bvbrc.convert_to_kbase(genome)
```

### UniProt
```python
entry = kbu.uniprot.get_uniprot_entry("P00533")
sequence = kbu.uniprot.get_protein_sequence("P00533")
results = kbu.uniprot.search_uniprot("alcohol dehydrogenase AND organism:ecoli")
mapped = kbu.uniprot.map_ids(["P00533", "P12345"], from_db="UniProtKB_AC", to_db="PDB")
```

### PDB
```python
structure = kbu.pdb.get_structure("1HHO")
structures = kbu.pdb.search_structures("hemoglobin")
sequence = kbu.pdb.get_sequence("1HHO", chain="A")
```

## Visualization

### Escher Maps
```python
kbu = KBUtilLib()

map = kbu.escher.create_map(reaction_list)
kbu.escher.visualize_fluxes(map, fba_solution)
kbu.escher.set_reaction_colors(map, {"rxn00001": "red", "rxn00002": "blue"})
kbu.escher.save_map(map, "my_map.json")

# Enhanced map with badges/legends:
html = kbu.escher.create_map_html2(model, fluxes, ...)
```

## EE2 Job Tracking (KBJobUtils)

### Submit and Track Jobs
```python
kbu = KBUtilLib()

# Submit a job (auto-persists to ~/.kbjobs/kbjobs.db)
record = kbu.jobs.run_job(
    method="ModelSEEDpy.build_metabolic_model",
    params=[{"genome_ref": "12345/6/7", "workspace_name": "my_workspace"}],
)
print(record.job_id, record.state)

# Refresh state from EE2
record = kbu.jobs.check_job(record.job_id)          # single job
records = kbu.jobs.refresh_active()                  # bulk refresh non-terminal jobs
records = kbu.jobs.refresh_all()                     # bulk refresh every tracked job

# Read from local store (no EE2 hit)
job = kbu.jobs.get_record(record.job_id)
active = kbu.jobs.list_active()                      # non-terminal jobs only
all_jobs = kbu.jobs.list_all()                       # every tracked job

# Manage
kbu.jobs.cancel_job(job_id)
kbu.jobs.cleanup(older_than_days=30, terminal_only=True)   # returns count deleted
logs = kbu.jobs.get_job_logs(job_id, skip_lines=0)
```

> **Note:** `forget(job_id)` is available via the `kbu jobs forget` CLI but not as a direct KBJobUtils Python method — use `kbu.jobs.store.delete(job_id)` to remove a record from the local SQLite store programmatically.

### Linear Pipelines
```python
# Submit a chain — only step 0 starts; advancement happens via refresh_active
pipeline = kbu.jobs.submit_chain(
    [
        {"method": "BuildModel", "params": [{...}]},
        {"method": "Gapfill", "params": [{...}]},
        {"method": "RunFBA", "params": [{...}]},
    ],
    name="my_chain",
)

# Periodic refresh advances the pipeline:
kbu.jobs.refresh_active()
# Or run the in-process watcher:
kbu.jobs.start_watcher(interval=300)
```

## CLI

The `kbu` CLI is the entry point for job management and notebook scaffolding.

### Job subcommands (`kbu jobs ...`)

```bash
# Show status of a single job
kbu jobs status <job_id>

# List tracked jobs (default: all; --active for non-terminal only)
kbu jobs list
kbu jobs list --active
kbu jobs list --status running
kbu jobs list --limit 100

# Per-status counts
kbu jobs summary

# Force a refresh of job states from EE2
kbu jobs refresh                        # refresh active jobs (default)
kbu jobs refresh --all                  # refresh all tracked jobs
kbu jobs refresh <job_id1> <job_id2>    # refresh specific jobs

# Stream log lines for a job
kbu jobs logs <job_id>
kbu jobs logs <job_id> --follow         # poll until terminal

# Cancel a running job
kbu jobs cancel <job_id>
kbu jobs cancel <job_id> --force        # skip confirmation

# Delete record(s) from local store (does NOT cancel on server)
kbu jobs forget <job_id>

# Remove old records from local store
kbu jobs cleanup --older-than-days 30
kbu jobs cleanup --older-than-days 7 --all-statuses --force
```

### Pipeline subcommands (`kbu jobs chain ...`)

```bash
# Submit a pipeline from a JSON file (bare list of EE2 param dicts, or dict with 'steps' key)
kbu jobs chain submit my_chain.json
echo '[{"method":"step1","params":[{}]}]' | kbu jobs chain submit -

# List pipelines
kbu jobs chain list
kbu jobs chain list --active
kbu jobs chain list --status running

# Show detailed status for a pipeline
kbu jobs chain status <pipeline_id>

# Cancel a running pipeline
kbu jobs chain cancel <pipeline_id>

# Force a one-shot advancement pass
kbu jobs chain advance
```

### Foreground watcher daemon (`kbu jobdaemon`)

```bash
kbu jobdaemon                           # default 300s interval
kbu jobdaemon --interval 60             # refresh every 60s
kbu jobdaemon --kb-version appdev       # target KBase appdev environment
kbu jobdaemon --log-level DEBUG
```

Runs `KBJobUtils.start_watcher()` in the foreground; exits cleanly on SIGINT/SIGTERM. Equivalent to the in-process watcher but as an OS daemon.

### Notebook scaffolding (`kbu init-notebook`)

```bash
kbu init-notebook                       # scaffold util.py for a new notebook project
```

The generated `util.py` reads `~/.kbu-sys-paths` at import time to prepend machine-specific Python paths before any heavy imports. Add one path per line (comments with `#` are OK).

## Notebook Integration

### NotebookSession + facade
```python
from kbutillib.notebook import NotebookSession

session = NotebookSession(...)
# session.kbu is the KBUtilLib facade scoped to this session's env
session.kbu.fba.run_fba(model)
session.kbu.biochem.search_compounds("glucose")

# session also exposes its own utilities:
session.cache.save("intermediate_data", df)
session.vectors.add(...)
```

`NotebookSession` replaces the deleted legacy `NotebookUtils`. See `src/kbutillib/notebook/` for the full session API.

## Flat-module helpers

```python
# URL helpers
from kbutillib.kbase_endpoints import base_url, service_url, narrative_url

# Compartment normalization
from kbutillib.compartments import compartment_types, normalize_compartment

# Model directionality
from kbutillib.model_directionality import (
    direction_conversion, directionality_from_bounds,
    biochem_directionality, combine_directionality_signals,
)

# Model helpers (deduped from triplicate)
from kbutillib.model_helpers import _check_and_convert_model, _parse_id
```

## Error Handling

```python
try:
    genome = kbu.genome.get_genome(12345, "NonExistentGenome")
except ValueError as e:
    print(f"Object not found: {e}")

try:
    result = kbu.curation.curate_reaction_direction(bad_data)
except Exception as e:
    kbu.env.logger.error(f"Curation failed: {e}")
```

## Logging

```python
# Logger is on the held SharedEnvUtils
kbu.env.logger.setLevel("DEBUG")
kbu.env.logger.info("Starting analysis")

# Each *Impl exposes the same logger as self.env.logger
kbu.fba.logger is kbu.env.logger  # True
```
