# kbutillib.domains.modeling — Metabolic Modeling

Flux balance analysis, model reconstruction, template management, model standardization, and KBase model object utilities for constraint-based metabolic models.

## What lives here

The modeling domain provides the full computational stack for metabolic models in the ModelSEED/KBase ecosystem: building models from genome annotations, running FBA, managing reaction templates, and standardizing model structure for downstream analysis. All backends require COBRApy or modelseedpy for heavy computation, but construct gracefully without them.

## Modules

| File | Class(es) | Purpose |
|------|-----------|---------|
| `ms_fba_utils.py` | `MSFBAUtils`, `MSFBAUtilsImpl` | FBA workflows: minimize active reactions, enumerate alternative solutions, detect flux loops |
| `ms_reconstruction_utils.py` | `MSReconstructionUtils`, `MSReconstructionUtilsImpl` | Draft model reconstruction from genome annotation |
| `ms_template_utils.py` | `MSTemplateUtils`, `MSTemplateUtilsImpl` | Reaction template management (gram+/gram-/plant templates) |
| `kb_model_utils.py` | `KBModelUtils`, `KBModelUtilsImpl` | KBase model object I/O: load, save, compare models in the workspace |
| `model_standardization_utils.py` | `MSModelStandardizationUtils`, `MSModelStandardizationUtilsImpl` | Standardize stoichiometry, compartment labeling, and metabolite IDs |
| `model_directionality.py` | — | Reaction directionality helpers used by FBA and reconstruction |
| `model_helpers.py` | — | Shared utilities: formula parsing, compartment resolution, flux bounds |

## Facade access

```python
from kbutillib.toolkit import KBUtilLib

kbu = KBUtilLib()

# Load a model and run FBA
model = kbu.model.load_model(workspace="my_ws", model_ref="my_ws/my_model/1")

# Minimize active reactions (returns a COBRApy solution)
solution = kbu.fba.minimize_active_reactions(model)

# Reconstruct a draft model from genome features
draft = kbu.recon.reconstruct_from_genome(genome_id="GCF_000195955.2")
```

## Capabilities

| Capability name | Summary |
|-----------------|---------|
| `modeling.minimize_active_reactions` | Minimize the number of active reactions carrying flux in a model |
| `modeling.find_flux_loops` | Detect thermodynamically infeasible flux loops in a model |
| `modeling.enumerate_alternative_reaction_sets` | Enumerate alternative minimal reaction sets achieving the objective |

```console
kbu cap list --domain modeling
kbu cap info modeling.minimize_active_reactions
```

## Optional dependencies

| Package | Enables | Install |
|---------|---------|---------|
| `cobra` | FBA solving, flux analysis | `pip install cobra` |
| `modelseedpy` | Model reconstruction, templates | `pip install modelseedpy` |
| KBase token | `KBModelUtils` workspace calls | env `KB_AUTH_TOKEN` |

`MSFBAUtils.available` returns `False` and explains which package is missing; operations raise `BackendUnavailableError` rather than `ImportError`.

## Adding a capability here

See root `CONTRIBUTING.md` → "Add a domain capability".

1. Add the method to the appropriate `*Impl` class (usually `MSFBAUtilsImpl` for analysis, `MSReconstructionUtilsImpl` for building).
2. Decorate with `@capability(domain="modeling", summary="...", tags=("modeling",))`.
3. Methods that require cobra: call `self._require_available()` or check `self.available` at the top.
4. Run `kbu cap list --domain modeling` to confirm.
5. Guard smoke tests with `pytest.importorskip("cobra")`.
