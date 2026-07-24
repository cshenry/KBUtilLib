# kbutillib.domains.thermo — Thermodynamics

ModelSEED thermodynamic data access and multi-backend predictive ΔG computation for compounds
and reactions.

---

## What lives here

Two complementary utilities cover thermodynamics. `ThermoUtils` reads precomputed ΔG values
from the ModelSEED biochemistry database — fast, no dependencies, limited to ModelSEED-measured
compounds. `PredictiveThermoUtils` is a dispatcher facade over five pluggable backends
(equilibrator, modelseed, modelseed_db, dgpredictor, molgpk) that predicts ΔG for arbitrary
compounds and reactions. The `thermo_predictors/` subpackage holds each backend's implementation.

## Canonical imports

```python
from kbutillib.domains.thermo.thermo_utils import ThermoUtils
from kbutillib.domains.thermo.predictive_thermo_utils import PredictiveThermoUtils
```

Or via top-level re-exports:

```python
from kbutillib import ThermoUtils, PredictiveThermoUtils
```

---

## Key classes and methods

| Class | Method | Description |
|-------|--------|-------------|
| `ThermoUtils` / `ThermoUtilsImpl` | `get_compound_deltag(compound_id)` | ModelSEED ΔGf lookup from bundled data |
| | `get_reaction_deltag(reaction_id)` | ModelSEED reaction ΔG lookup |
| `PredictiveThermoUtils` / `PredictiveThermoUtilsImpl` | `compound_dgf(compound_id)` | Predict ΔGf via best available backend |
| | `reaction_dg_prime(reaction_id, ph, ionic_strength)` | Predict ΔG'° at specified conditions |
| | `backend_status()` | Report which backends loaded successfully |
| `ThermoPredictor` (abstract) | `predict_compound_dgf` / `predict_reaction_dg_prime` | Interface for custom backends |

**Backends in `thermo_predictors/`:**

| Backend class | Requires | Notes |
|---------------|---------|-------|
| `EquilibratorBackend` | `equilibrator-api` | Equilibrator compound/reaction ΔG |
| `ModelseedBackend` | bundled data | ModelSEED compound data lookup |
| `ModelseedDbBackend` | bundled data | Offline ModelSEED database backend |
| `DgPredictorBackend` | `torch` | ML-based ΔG prediction |
| `MolgpkBackend` | `rdkit` | Group-contribution pKa / ΔGf |

---

## Optional dependencies

| Package | Enables | Install |
|---------|---------|---------|
| `equilibrator-api` | Equilibrator backend | `pip install equilibrator-api` |
| `torch` | DgPredictor ML backend | `pip install torch` |
| `rdkit` | Molgpk group-contribution backend | `conda install -c conda-forge rdkit` |

`ThermoUtils` has no optional dependencies. `PredictiveThermoUtils.backend_status()` reports
which backends loaded; missing backends do not prevent the others from running.

---

## Usage example

```python
from kbutillib.domains.thermo.thermo_utils import ThermoUtils
from kbutillib.domains.thermo.predictive_thermo_utils import PredictiveThermoUtils

# Legacy ModelSEED lookup — always available
thermo = ThermoUtils()
dg = thermo.get_compound_deltag("cpd00002")
print(dg)  # ΔGf in kJ/mol

# Predictive — selects best available backend
pred = PredictiveThermoUtils()
print(pred.backend_status())  # {'equilibrator': True, 'molgpk': False, ...}

result = pred.compound_dgf("cpd00002")
rxn_result = pred.reaction_dg_prime(
    reaction_id="rxn00001", ph=7.0, ionic_strength=0.1
)
```

Via the facade:

```python
from kbutillib import KBUtilLib

kbu = KBUtilLib()
dg = kbu.thermo.get_compound_deltag("cpd00002")
status = kbu.predictive_thermo.backend_status()
```

---

## Available capabilities

| Capability name | Summary |
|-----------------|---------|
| `thermo.compound_dgf` | Predict standard Gibbs formation energy of a compound |
| `thermo.reaction_dg_prime` | Predict standard transformed ΔG'° of a reaction |
| `thermo.backend_status` | Report availability of each thermodynamics backend |

```bash
kbu cap list --domain thermo
kbu cap info thermo.compound_dgf
```

---

## Adding a capability here

See root `CONTRIBUTING.md` → "Add a domain capability".

1. For lookup methods: add to `ThermoUtilsImpl` or `PredictiveThermoUtilsImpl`.
2. For new backends: subclass `ThermoPredictor`, implement `predict_compound_dgf` and
   `predict_reaction_dg_prime`, register in `thermo_predictors/__init__.py`.
3. Decorate with `@capability(domain="thermo", summary="...", tags=("thermo",))`.
4. Guard smoke tests with `pytest.importorskip("equilibrator_api")` (or the relevant package).
