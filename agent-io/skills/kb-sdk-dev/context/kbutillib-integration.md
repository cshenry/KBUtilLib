# KBUtilLib Integration Guide

## Overview

KBUtilLib is a modular utility framework that should be used in ALL KBase SDK applications to avoid code duplication. **Post-2026-05 refactor**, the library uses a **composition** pattern with the `KBUtilLib` facade — multi-inheritance is deprecated. Legacy class-name aliases remain in `__init__.py` for import compatibility, but new code should use the facade.

**Repository:** `/Users/chenry/Dropbox/Projects/KBUtilLib`
**GitHub:** https://github.com/cshenry/KBUtilLib

## Installation in Dockerfile

**ALWAYS include KBUtilLib in your Dockerfile:**

```dockerfile
# Install KBUtilLib for shared utilities
RUN cd /kb/module && \
    git clone https://github.com/cshenry/KBUtilLib.git && \
    cd KBUtilLib && \
    pip install -e .
```

## Available sub-utilities (via `kbu.<attr>`)

The `KBUtilLib` facade exposes 25 lazy-property sub-utilities. Each `*Impl` class holds a `SharedEnvUtils` and any composed sibling Impls; the facade wires them automatically.

### Core Foundation

| Sub-utility | Held by facade as | Purpose |
|--------|--------|---------|
| `BaseUtils` | (still inherited inside) | Logging, error handling |
| `SharedEnvUtils` | `kbu.env` | Configuration files, authentication tokens |

The legacy `NotebookUtils` god-class was deleted in the 2026-05 refactor. For Jupyter integration, use `kbutillib.notebook.NotebookSession` instead.

### KBase Data Access

| Attribute | Impl class | Purpose |
|--------|--------|---------|
| `kbu.ws` | `KBWSUtilsImpl` | Workspace operations: get/save objects |
| `kbu.callback` | `KBCallbackUtilsImpl` | Callback server handling for SDK apps |
| `kbu.sdk` | `KBSDKUtilsImpl` | SDK development utilities |

### Analysis Utilities

| Attribute | Impl class | Purpose |
|--------|--------|---------|
| `kbu.genome` | `KBGenomeUtilsImpl` | Genome parsing, feature extraction, translation |
| `kbu.annotation` | `KBAnnotationUtilsImpl` | Gene/protein annotation workflows |
| `kbu.model` | `KBModelUtilsImpl` | Metabolic model analysis |
| `kbu.fba` | `MSFBAUtilsImpl` | FBA — preserves AP3 carve-outs (`run_fva`, `analyzed_reaction_objective_coupling`, `fit_flux_to_mutant_growth_rate_data`) |
| `kbu.biochem` | `MSBiochemUtilsImpl` | ModelSEED biochemistry database access |
| `kbu.reads` | `KBReadsUtilsImpl` | Reads processing and QC |

### External Integrations

| Attribute | Impl class | Purpose |
|--------|--------|---------|
| `kbu.argo` | `ArgoUtilsImpl` | Argo LLM gateway (lazy client init) |
| `kbu.curation` | `AICurationUtilsImpl` | AI-powered curation |
| `kbu.bvbrc` | `BVBRCUtilsImpl` | BV-BRC database access |
| `kbu.patric` | `PatricWSUtilsImpl` | PATRIC workspace utilities |
| `kbu.uniprot` | `KBUniProtUtilsImpl` | UniProt REST API |
| `kbu.pdb` | `RCSBPDBUtilsImpl` | RCSB PDB structures |
| `kbu.jobs` | `KBJobUtils` | EE2 job submission + local SQLite tracking |

(Full list of 25 in PRD §6.3 at `KBUtilLib/agent-io/prds/kbutillib-composition-refactor/fullprompt.md`.)

## Usage Patterns

### Pattern 1: Single sub-utility access
```python
from kbutillib import KBUtilLib

class MyApp:
    def __init__(self, callback_url):
        self.kbu = KBUtilLib(callback_url=callback_url)

    def run(self, params):
        obj = self.kbu.ws.get_object(params['workspace'], params['ref'])
```

### Pattern 2: Facade composition (Recommended)

The facade exposes everything via lazy properties. No more inheritance — just access what you need:

```python
from kbutillib import KBUtilLib

class MyApp:
    def __init__(self, callback_url):
        self.kbu = KBUtilLib(callback_url=callback_url)

    def run(self, params):
        # Access multiple sub-utilities via the same facade
        genome = self.kbu.ws.get_object(params['workspace'], params['ref'])
        features = self.kbu.genome.extract_features_by_type(genome, 'CDS')
        report = self.kbu.callback.create_extended_report({...})
```

The facade auto-wires composed dependencies. `kbu.fba` → `MSFBAUtilsImpl(env, model)` where `model` is the lazy `KBModelUtilsImpl(env, ws, annotation, biochem)`. You don't manage the wiring.

### Pattern 3: SDK Implementation File
```python
#BEGIN_HEADER
import os
from kbutillib import KBUtilLib, SharedEnvUtils
#END_HEADER

class MyModule:
    def __init__(self, config):
        #BEGIN_CONSTRUCTOR
        self.callback_url = os.environ['SDK_CALLBACK_URL']
        self.scratch = config['scratch']
        # Construct an SDK-flavored env then build the facade
        env = SharedEnvUtils(
            callback_url=self.callback_url,
            scratch=self.scratch,
        )
        self.kbu = KBUtilLib(env=env)
        #END_CONSTRUCTOR

    def my_method(self, ctx, params):
        #BEGIN my_method
        workspace = params['workspace_name']

        # Get genome via kbu.ws
        genome = self.kbu.ws.get_object(workspace, params['genome_ref'])

        # Parse genome via kbu.genome
        features = self.kbu.genome.extract_features_by_type(genome, 'CDS')

        # Create report via kbu.callback
        report_info = self.kbu.callback.create_extended_report({
            'message': f'Found {len(features)} CDS features',
            'workspace_name': workspace
        })

        return [{
            'report_name': report_info['name'],
            'report_ref': report_info['ref']
        }]
        #END my_method
```

### Legacy alias compatibility

If migrating an existing SDK app written against the old pattern, the legacy class names still resolve via `__init__.py` aliases:

```python
# Still works (resolves to *Impl):
from kbutillib import KBWSUtils, KBCallbackUtils, KBGenomeUtils
# But constructor signatures changed — composition takes deps explicitly.
# DO NOT do this in new code:
# class AppUtils(KBWSUtils, KBCallbackUtils, KBGenomeUtils): pass
# Use the facade instead.
```

## Key Methods Reference

### KBWSUtilsImpl (`kbu.ws`)

```python
# Get a single object
obj_data = kbu.ws.get_object(workspace, object_ref)

# Get object with metadata
obj, info = kbu.ws.get_object_with_info(workspace, object_ref)

# Save an object
info = kbu.ws.save_object(workspace, obj_type, obj_name, obj_data)

# List objects in workspace
objects = kbu.ws.list_objects(workspace, type_filter='KBaseGenomes.Genome')

# Workspace ref check
assert kbu.ws.is_ref("12345/6/7") is True
```

### KBCallbackUtilsImpl (`kbu.callback`)

```python
# Create a report
report_info = kbu.callback.create_extended_report({
    'message': 'Analysis complete',
    'workspace_name': workspace,
    'objects_created': [{'ref': new_ref, 'description': 'My output'}],
    'file_links': [{'path': '/path/to/file.txt', 'name': 'results.txt'}],
    'html_links': [{'path': '/path/to/report.html', 'name': 'report'}]
})

# Download staging file
local_path = kbu.callback.download_staging_file(staging_file_path)

# Upload file to shock
shock_id = kbu.callback.upload_to_shock(file_path)
```

### KBGenomeUtilsImpl (`kbu.genome`)

```python
# Extract all features of a type
cds_features = kbu.genome.extract_features_by_type(genome_data, 'CDS')

# Translate DNA sequence
protein = kbu.genome.translate_sequence(dna_seq)

# Find ORFs in sequence
orfs = kbu.genome.find_orfs(sequence, min_length=100)

# Parse genome object
genome_info = kbu.genome.parse_genome_object(genome_data)
```

### KBModelUtilsImpl (`kbu.model`) + MSFBAUtilsImpl (`kbu.fba`)

```python
# Load model data
model_data = kbu.model.get_model(workspace, model_ref)

# Get reactions/metabolites
reactions = kbu.model.get_model_reactions(model_data)
metabolites = kbu.model.get_model_metabolites(model_data)

# FBA — kbu.fba.run_fva is the canonical FVA (cobra version is broken — AP3 carve-out)
solution = kbu.fba.run_fba(model_data, biomass_reaction="bio1")
fva = kbu.fba.run_fva(model_data)
```

### MSBiochemUtilsImpl (`kbu.biochem`)

```python
# Search compounds
compounds = kbu.biochem.search_compounds("glucose")

# Get reaction info
reaction = kbu.biochem.get_reaction("rxn00001")

# Search reactions by compound
reactions = kbu.biochem.find_reactions_with_compound("cpd00001")
```

### KBJobUtils (`kbu.jobs`)

```python
# Submit + track an EE2 job (auto-persists to ~/.kbjobs/kbjobs.db)
state = kbu.jobs.submit({"method": "MyApp/run", "params": [{...}]}, name="my-job")
states = kbu.jobs.refresh_active()  # bulk refresh non-terminal jobs
```

## When to Add Code to KBUtilLib

If you're writing a function that:
1. Could be used in multiple KBase apps
2. Performs a common operation (parsing, converting, validating)
3. Wraps a KBase service in a cleaner way
4. Provides utility for a common data type

**Consider adding it to KBUtilLib instead of your app.**

### How to Add (composition pattern)

1. Identify which `*Impl` class it belongs in (or design a new one composing `env` plus any sibling Impls).
2. Add the method to the appropriate `*Impl` class (NOT to a multi-inherited subclass).
3. Wire the new sub-utility into the `KBUtilLib` facade in `toolkit.py` (lazy property).
4. Add a legacy alias in `__init__.py` (`MyUtils = MyUtilsImpl`).
5. Add tests under `tests/` — both via `KBUtilLib()` facade and direct `Impl` construction.
6. Update PRD §6.3 sub-utility namespace table if adding a new top-level attribute.
7. Push to GitHub. Update your app's Dockerfile to get latest.

Reference: `src/kbutillib/kb_job_utils/utils.py` is the canonical composition pilot. See `kbutillib-dev:context:development-guide` for the full step-by-step.

## Configuration

KBUtilLib can be configured via `config.yaml`:

```yaml
kbase:
  endpoint: https://kbase.us/services
  token_env: KB_AUTH_TOKEN

scratch: /kb/module/work/tmp

logging:
  level: INFO
  format: "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
```

Load configuration:
```python
from kbutillib import KBUtilLib, SharedEnvUtils

env = SharedEnvUtils(config_file='config.yaml')
kbu = KBUtilLib(env=env)
# Or:
kbu = KBUtilLib(callback_url=url, scratch=scratch_dir)
```

## Error Handling

KBUtilLib provides consistent error handling:

```python
from kbutillib.base_utils import KBUtilLibError

try:
    result = kbu.ws.get_object(workspace, ref)
except KBUtilLibError as e:
    # Handle KBUtilLib-specific errors
    kbu.env.logger.error(f"KBUtilLib error: {e}")
except Exception as e:
    # Handle other errors
    kbu.env.logger.error(f"Unexpected error: {e}")
```

## Testing

Test your integration:

```python
import pytest
from kbutillib import KBUtilLib

def test_workspace_access():
    kbu = KBUtilLib(callback_url=test_callback_url)
    obj = kbu.ws.get_object('test_workspace', 'test_object')
    assert obj is not None

def test_facade_lazy():
    """Lazy properties: kbu.ws is constructed on first access, cached after."""
    kbu = KBUtilLib(callback_url=test_callback_url)
    assert kbu.ws is kbu.ws  # same instance on second access
```
