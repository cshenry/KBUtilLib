# KBUtilLib Common Patterns and Workflows

Practical patterns and complete workflows for using KBUtilLib (post-2026-05 composition refactor).

> The legacy multi-inheritance pattern (`class MyTools(A, B): pass`) is **deprecated**. Use the `KBUtilLib` facade. See PRD §6 for the architecture.

## Pattern 1: The KBUtilLib facade

The single entry point for all KBUtilLib utilities. Sub-utilities are lazy-instantiated; composed dependencies wired automatically.

### Basic Usage
```python
from kbutillib import KBUtilLib

kbu = KBUtilLib()  # constructs facade with default SharedEnvUtils

# Access any sub-utility as a property; first access constructs it
genome = kbu.genome.get_genome(workspace_id=12345, genome_ref="MyGenome/1")
features = kbu.genome.get_features_by_type(genome, "CDS")
reactions = kbu.biochem.search_reactions("transport")
```

### Custom env / explicit config
```python
from kbutillib import KBUtilLib, SharedEnvUtils

env = SharedEnvUtils(config_file="/path/to/my_config.yaml")
kbu = KBUtilLib(env=env)
```

### Sharing one env across utilities (the whole point of composition)
```python
# All sub-utilities share the same SharedEnvUtils — no duplicate token loads,
# no duplicate config reads. The facade does this automatically.
assert kbu.fba.env is kbu.biochem.env is kbu.ws.env
```

### Combining at the call site (instead of inheritance)
```python
# Before (deprecated):
# class MyTools(KBWSUtils, KBGenomeUtils, MSBiochemUtils): pass
# tools = MyTools(); features = tools.get_features_by_type(genome, "CDS")

# After:
kbu = KBUtilLib()
features = kbu.genome.get_features_by_type(genome, "CDS")
reactions = kbu.biochem.search_reactions(feature['function'])
```

## Pattern 2: Configuration Management

### Configuration is on the held SharedEnvUtils
```python
from kbutillib import KBUtilLib

# Option 1: Auto-detect (loads from ~/kbutillib_config.yaml or repo defaults)
kbu = KBUtilLib()

# Option 2: Explicit configuration
from kbutillib import SharedEnvUtils
env = SharedEnvUtils(config_file="/path/to/my_config.yaml")
kbu = KBUtilLib(env=env)

# Option 3: Runtime token override
kbu = KBUtilLib()
kbu.env.set_token("my_token", namespace="kbase")
```

### Configuration File Template
```yaml
# ~/kbutillib_config.yaml
kbase:
  endpoint: https://kbase.us/services
  workspace_url: https://kbase.us/services/ws
  auth_service_url: https://kbase.us/services/auth

argo:
  endpoint: https://api.cels.anl.gov/argo/api/v1
  default_model: gpt4o

modelseed:
  database_path: ~/ModelSEEDDatabase

logging:
  level: INFO

my_analysis:
  output_dir: ~/analysis_results
  cache_enabled: true
```

### Accessing Configuration
```python
# Modern API — dot-notation get_config_value (SharedEnvUtils.get_config_value):
endpoint = kbu.env.get_config_value("kbase.endpoint")
output_dir = kbu.env.get_config_value("my_analysis.output_dir")
cache = kbu.env.get_config_value("my_analysis.cache_enabled", default=False)

# get_config(section, key) exists for INI-file compatibility but is deprecated.
# Do not use it in new code.
```

### Injecting SDK Clients in Notebook Contexts

The `kbu.callback` sub-utility provides wrappers around the KBase SDK callback clients (`gfu_client()`, `afu_client()`, etc.). These normally require a **callback URL** that is only available inside an actively running SDK app container. Notebook authors running outside that context will get `ImportError` or `RuntimeError` when calling these methods directly.

The injection escape hatch: `kbu.callback.set_callback_client(name, client)` lets you plug in a pre-built client object so the callback accessor returns your instance instead of attempting auto-construction.

```python
# If you have a GenomeFileUtil client built against a callback URL you control:
from installed_clients.GenomeFileUtilClient import GenomeFileUtil

gfu = GenomeFileUtil(url=my_callback_url, token=my_token)
kbu.callback.set_callback_client("GenomeFileUtil", gfu)

# Now kbu.callback.gfu_client() returns your pre-built client:
client = kbu.callback.gfu_client()
```

> **Note:** `AssemblyUtilClient` and `GenomeFileUtilClient` are NOT shipped under `installed_clients/`; they require a separate KBase SDK install. Without that install, the import inside `kb_callback_utils.py` raises `ImportError`. For notebook-only assembly/genome save workflows, prefer `kbu.genome.save_assembly_from_fasta` / `kbu.genome.save_genome_with_assembly` which go through EE2 and direct Workspace respectively — no callback URL required.
>
> See `/kbase-genome-expert` for the complete notebook genome save workflow.

## Pattern 3: Genome Analysis Workflow

```python
from kbutillib import KBUtilLib

kbu = KBUtilLib()

# Step 1: Retrieve genome
genome = kbu.genome.get_genome(12345, "MyGenome/1")
print(f"Genome: {genome['scientific_name']}")
print(f"Features: {len(genome['features'])}")

# Step 2: Extract coding sequences
cds_features = kbu.genome.get_features_by_type(genome, "CDS")
print(f"CDS count: {len(cds_features)}")

# Step 3: Translate to proteins
proteins = kbu.genome.translate_features(cds_features)

# Step 4: Analyze annotations
annotations = kbu.annotation.get_annotations(genome)
ec_annotations = kbu.annotation.filter_annotations_by_ontology(annotations, "EC")

# Step 5: Map functional roles to reactions (uses kbu.annotation + kbu.biochem)
for feature in cds_features[:10]:
    ec_nums = kbu.annotation.get_ec_numbers(feature)
    if ec_nums:
        reactions = kbu.annotation.map_function_to_reactions(feature['function'])
        print(f"{feature['id']}: {len(reactions)} reactions")
```

## Pattern 4: Metabolic Model Analysis (preserves AP3 carve-outs)

### FBA Pipeline
```python
from kbutillib import KBUtilLib

kbu = KBUtilLib()

# Step 1: Get model
model = kbu.model.get_model(12345, "MyModel/1")
print(f"Model has {len(kbu.model.get_model_reactions(model))} reactions")

# Step 2: Set up simulation
kbu.fba.set_media(model, "Complete")
kbu.fba.set_objective(model, "bio1")

# Step 3: Run FBA
solution = kbu.fba.run_fba(model, biomass_reaction="bio1")
print(f"Growth rate: {solution.objective_value}")

# Step 4: Analyze flux distribution
for reaction_id, flux in solution.fluxes.items():
    if abs(flux) > 0.1:
        rxn_info = kbu.biochem.get_reaction(reaction_id.split("_")[0])
        print(f"{reaction_id}: {flux:.2f}  ({rxn_info.get('name')})")

# Step 5: Flux Variability Analysis (AP3 — uses kbu.fba.run_fva, NOT cobra)
kbu.fba.set_fraction_of_optimum(model, 0.9)
fva = kbu.fba.run_fva(model)
for rxn, (min_flux, max_flux) in fva.items():
    if min_flux != max_flux:
        print(f"{rxn}: [{min_flux:.2f}, {max_flux:.2f}]")
```

### Model Comparison
```python
model1 = kbu.model.get_model(12345, "Model1/1")
model2 = kbu.model.get_model(12345, "Model2/1")

rxns1 = set(r['id'] for r in kbu.model.get_model_reactions(model1))
rxns2 = set(r['id'] for r in kbu.model.get_model_reactions(model2))

print(f"Shared: {len(rxns1 & rxns2)}")
print(f"Unique to Model1: {len(rxns1 - rxns2)}")
print(f"Unique to Model2: {len(rxns2 - rxns1)}")
```

## Pattern 5: AI-Powered Curation

```python
from kbutillib import KBUtilLib

kbu = KBUtilLib()

# Step 1: Get reactions to curate
reactions_to_curate = kbu.biochem.search_reactions("transport")

# Step 2: Curate each
results = []
for rxn in reactions_to_curate[:10]:
    cached = kbu.curation.get_cached_result(rxn['id'])
    if cached:
        results.append(cached)
        continue

    direction = kbu.curation.curate_reaction_direction(rxn)
    category = kbu.curation.categorize_stoichiometry(rxn)

    result = {'reaction_id': rxn['id'], 'direction': direction, 'category': category}
    results.append(result)
    kbu.curation.cache_result(rxn['id'], result)

# Step 3: Analyze
reversible = sum(1 for r in results if r['direction'] == 'reversible')
print(f"Reversible reactions: {reversible}/{len(results)}")
```

### Gene-Reaction Validation
```python
model = kbu.model.get_model(12345, "MyModel/1")

for reaction in model['modelreactions'][:10]:
    for gene in reaction.get('genes', []):
        assessment = kbu.curation.assess_gene_reaction(gene, reaction)
        if assessment['confidence'] < 0.5:
            print(f"Low confidence: {gene['id']} -> {reaction['id']}")
            print(f"  Reason: {assessment['reasoning']}")
```

## Pattern 6: External Database Integration

### BV-BRC Genome Import
```python
from kbutillib import KBUtilLib

kbu = KBUtilLib()

# Step 1: Search
genomes = kbu.bvbrc.search_bvbrc_genomes("Escherichia coli K-12")

# Step 2: Fetch
bvbrc_genome = kbu.bvbrc.get_bvbrc_genome(genomes[0]['genome_id'])

# Step 3: Convert to KBase
kb_genome = kbu.bvbrc.convert_to_kbase(bvbrc_genome)

# Step 4: Save (delegates to kbu.ws)
kbu.ws.save_object(
    workspace_id=12345,
    obj_type="KBaseGenomes.Genome",
    data=kb_genome,
    name="EcoliK12_imported",
)
```

### UniProt Annotation Enhancement
```python
genome = kbu.genome.get_genome(12345, "MyGenome/1")
features = kbu.genome.get_features_by_type(genome, "CDS")

for feature in features[:10]:
    sequence = kbu.genome.translate_feature(feature)
    uniprot_hits = kbu.uniprot.search_uniprot(f"sequence:{sequence[:50]}")
    if uniprot_hits:
        entry = kbu.uniprot.get_uniprot_entry(uniprot_hits[0]['accession'])
        print(f"{feature['id']}: {entry['proteinDescription']}")
```

## Pattern 7: Notebook integration via NotebookSession.kbu

```python
from kbutillib.notebook import NotebookSession

session = NotebookSession(...)  # Builds with its own SharedEnvUtils

# kbu hangs off the session — same facade, scoped to this notebook
genome = session.kbu.genome.get_genome(12345, "MyGenome/1")
features = session.kbu.genome.get_features(genome)

# Session also exposes its own utilities
session.cache.save("genome_intermediate", genome)
session.vectors.add(...)

# DataFrame display + progress bars come from the notebook engine, not a util class
import pandas as pd
features_df = pd.DataFrame(features)
session.cache.save("features_df", features_df)
```

`NotebookSession` replaces the deleted legacy `NotebookUtils`. Cache save/load, vector storage, and provenance tracking now live in the session, not on a multi-inherited utility class.

## Pattern 8: EE2 Job Tracking + Pipelines (KBJobUtils)

KBJobUtils is the canonical composition reference. Use it both as a tool and as a guide for building new modules.

### Submit + track a single job
```python
kbu = KBUtilLib()

# run_job returns a JobRecord with job_id and state (JobState.QUEUED on success)
record = kbu.jobs.run_job(
    method="ModelSEEDpy.build_metabolic_model",
    params=[{"genome_ref": "12345/6/7", "workspace_name": "my_workspace"}],
)

# Local SQLite at ~/.kbjobs/kbjobs.db tracks every job
print(record.job_id, record.state, record.created_at)

# Periodic refresh (no blocking poll inside run_job; caller controls cadence)
kbu.jobs.refresh_active()  # bulk-refreshes all non-terminal jobs from EE2

# Read locally without hitting EE2
job = kbu.jobs.get_record(record.job_id)
active = kbu.jobs.list_active()    # all non-terminal records
all_j = kbu.jobs.list_all()        # everything in the local store
```

### Bulk submission (1400 metagenomes)
```python
metagenome_refs = [...]  # 1400 entries

records = []
for ref in metagenome_refs:
    rec = kbu.jobs.run_job(
        method="BuildMetagenomeModel",
        params=[{"genome_ref": ref}],
        meta={"project": "metagenome-batch"},
    )
    records.append(rec)

# Cron or in-process watcher does the actual refreshing:
kbu.jobs.start_watcher(interval=300)  # threaded, daemon=True by default
```

### Linear pipelines (Phase 3)
```python
pipeline = kbu.jobs.submit_chain(
    [
        {"method": "BuildModel", "params": [{...}]},
        {"method": "Gapfill", "params": [{...}]},
        {"method": "RunFBA", "params": [{...}]},
    ],
    name="adp1-build-gapfill-fba",
)

# refresh_active() advances chains automatically when each step completes
kbu.jobs.refresh_active()
```

## Pattern 9: Provenance Tracking

`SharedEnvUtils` tracks calls; the held env is the single source of provenance for a session.

```python
from kbutillib import KBUtilLib

kbu = KBUtilLib()

# Each *Impl uses self.env.initialize_call internally on key methods.
# View accumulated provenance via the env:
events = kbu.env.provenance
for event in events:
    print(f"{event['timestamp']}: {event['method']} {event['params']}")
```

## Pattern 10: Error Handling

```python
kbu = KBUtilLib()

def safe_get_object(workspace_id, object_ref):
    try:
        return kbu.ws.get_object(workspace_id, object_ref)
    except ValueError as e:
        kbu.env.logger.error(f"Object not found: {object_ref}")
        return None
    except ConnectionError as e:
        kbu.env.logger.error(f"Connection failed: {e}")
        raise

genome = safe_get_object(12345, "MyGenome/1") or safe_get_object(12345, "MyGenome_backup/1")
```

### Batch Processing with Recovery
```python
def process_batch(refs, workspace_id):
    results, failed = [], []
    for ref in refs:
        try:
            obj = kbu.ws.get_object(workspace_id, ref)
            results.append(process_object(obj))
        except Exception as e:
            kbu.env.logger.error(f"Failed {ref}: {e}")
            failed.append({"ref": ref, "error": str(e)})
    kbu.env.logger.info(f"Processed {len(results)}/{len(refs)}; failed {len(failed)}")
    return results, failed
```

## Migration from legacy multi-inheritance

If you find old code using the legacy pattern:

```python
# Old (deprecated):
class MyTools(KBWSUtils, KBGenomeUtils, MSBiochemUtils):
    pass
tools = MyTools()
features = tools.get_features_by_type(genome, "CDS")

# New:
kbu = KBUtilLib()
features = kbu.genome.get_features_by_type(genome, "CDS")
```

The legacy class names (`KBWSUtils`, `MSFBAUtils`, etc.) still resolve — they're aliased to the new `*Impl` classes in `__init__.py`. But constructor signatures changed (composition takes deps explicitly), so direct instantiation of the legacy names won't work the same way. Always prefer the facade.

## Example Notebooks Reference

| Notebook | Purpose | Key Patterns |
|----------|---------|--------------|
| `ConfigureEnvironment.ipynb` | Initial setup | `kbu = KBUtilLib()`, tokens via `kbu.env` |
| `BVBRCGenomeConversion.ipynb` | Import genomes | `kbu.bvbrc.*`, `kbu.ws.save_object` |
| `AssemblyUploadDownload.ipynb` | Assembly handling | `kbu.ws.*` |
| `SKANIGenomeDistance.ipynb` | Genome similarity | `kbu.skani.*` |
| `ProteinLanguageModels.ipynb` | PLM analysis | `kbu.plm.*` |
| `StoichiometryAnalysis.ipynb` | Reaction analysis | `kbu.biochem.*` |
| `AICuration.ipynb` | AI curation | `kbu.curation.*` |
| `KBaseWorkspaceUtilities.ipynb` | Workspace ops | `kbu.ws.*` |
