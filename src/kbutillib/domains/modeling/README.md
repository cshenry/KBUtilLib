# kbutillib.domains.modeling — Metabolic Modeling

Flux balance analysis, model reconstruction, reaction template management, model standardization,
and KBase model object I/O for constraint-based metabolic models.

---

## What lives here

The modeling domain covers the full computational stack for metabolic models in the
ModelSEED/KBase ecosystem: building models from genome annotations, running FBA, managing
reaction templates, and standardizing model structure. All backends require COBRApy or
modelseedpy for heavy computation but construct gracefully without them.

## Canonical imports

```python
from kbutillib.domains.modeling.ms_fba_utils import MSFBAUtils
from kbutillib.domains.modeling.ms_reconstruction_utils import MSReconstructionUtils
from kbutillib.domains.modeling.kb_model_utils import KBModelUtils
from kbutillib.domains.modeling.ms_template_utils import MSTemplateUtils
```

---

## Modules

| File | Class(es) | Purpose |
|------|-----------|---------|
| `ms_fba_utils.py` | `MSFBAUtils`, `MSFBAUtilsImpl` | FBA workflows: minimize active reactions, enumerate alternatives, detect flux loops |
| `ms_reconstruction_utils.py` | `MSReconstructionUtils`, `MSReconstructionUtilsImpl` | Draft model reconstruction from genome annotation |
| `ms_template_utils.py` | `MSTemplateUtils`, `MSTemplateUtilsImpl` | Reaction template management (gram+/gram-/plant templates) |
| `kb_model_utils.py` | `KBModelUtils`, `KBModelUtilsImpl` | KBase model object I/O: load, save, compare models in the workspace |
| `model_standardization_utils.py` | `MSModelStandardizationUtils`, `MSModelStandardizationUtilsImpl` | Standardize stoichiometry, compartment labeling, and metabolite IDs |
| `model_directionality.py` | — | Reaction directionality helpers used by FBA and reconstruction |
| `model_helpers.py` | — | Shared utilities: formula parsing, compartment resolution, flux bounds |
| `ec_role_resolver.py` | `EcRoleResolver` | EC number -> ModelSEED role name(s), parsed from a caller-supplied `Annotations/Roles.tsv`; no fuzzy matching, no external dependencies |

---

## Optional dependencies

| Package | Enables | Install |
|---------|---------|---------|
| `cobra` | FBA solving, flux analysis | `pip install cobra` |
| `modelseedpy` | Model reconstruction, templates | `pip install modelseedpy` |
| KBase auth token | `KBModelUtils` workspace calls | `export KB_AUTH_TOKEN=...` |

`MSFBAUtils.available` returns `False` and describes what is missing; operations raise
`BackendUnavailableError` rather than `ImportError`.

---

## Usage example

```python
from kbutillib.domains.modeling.ms_fba_utils import MSFBAUtils
from kbutillib.domains.modeling.kb_model_utils import KBModelUtils

# Check availability (requires cobra)
fba = MSFBAUtils()
print(fba.available)          # True if cobra is installed
print(fba.unavailable_reason) # None, or "cobra is required: pip install cobra"

# Load a model from KBase (requires KB_AUTH_TOKEN)
model_utils = KBModelUtils()
model = model_utils.load_model(workspace="my_ws", model_ref="my_ws/my_model/1")

# Run FBA
solution = fba.minimize_active_reactions(model)
print(solution.objective_value)

# Detect flux loops
loops = fba.find_flux_loops(model)
```

Via the facade:

```python
from kbutillib import KBUtilLib

kbu = KBUtilLib()
model = kbu.model.load_model(workspace="my_ws", model_ref="my_ws/my_model/1")
solution = kbu.fba.minimize_active_reactions(model)
```

---

## Available capabilities

| Capability name | Summary |
|-----------------|---------|
| `modeling.minimize_active_reactions` | Minimize the number of active reactions carrying flux in a model |
| `modeling.find_flux_loops` | Detect thermodynamically infeasible flux loops in a model |
| `modeling.enumerate_alternative_reaction_sets` | Enumerate alternative minimal reaction sets achieving the objective |

```bash
kbu cap list --domain modeling
kbu cap info modeling.minimize_active_reactions
```

---

## Adding a capability here

See root `CONTRIBUTING.md` → "Add a domain capability".

1. Add the method to the appropriate `*Impl` class (`MSFBAUtilsImpl` for analysis,
   `MSReconstructionUtilsImpl` for building).
2. Decorate with `@capability(domain="modeling", summary="...", tags=("modeling",))`.
3. Methods that require cobra: check `self.available` at the top and raise
   `BackendUnavailableError` if absent.
4. Run `kbu cap list --domain modeling` to confirm.
5. Guard smoke tests with `pytest.importorskip("cobra")`.
