# kbutillib.domains.thermo — Thermodynamics

ModelSEED thermodynamic data access and multi-backend predictive ΔG computation for compounds and reactions.

## What lives here

Two complementary utilities cover thermodynamics. `ThermoUtils` reads precomputed ΔG values from the ModelSEED biochemistry database — fast, no dependencies, but limited to compounds ModelSEED has measured. `PredictiveThermoUtils` is a dispatcher facade over five pluggable backends (equilibrator, modelseed, modelseed_db, dgpredictor, molgpk) that can predict ΔG for arbitrary compounds and reactions. The `thermo_predictors/` subpackage holds each backend's implementation.

## Modules

| File | Class(es) | Purpose |
|------|-----------|---------|
| `thermo_utils.py` | `ThermoUtils`, `ThermoUtilsImpl` | ModelSEED ΔG lookup (legacy; bundled data) |
| `predictive_thermo_utils.py` | `PredictiveThermoUtils`, `PredictiveThermoUtilsImpl` | Multi-backend ΔG prediction dispatcher |
| `thermo_predictors/base.py` | `ThermoPredictor` | Abstract base class for all predictive backends |
| `thermo_predictors/equilibrator.py` | `EquilibratorBackend` | Equilibrator-powered ΔG prediction |
| `thermo_predictors/modelseed.py` | `ModelseedBackend` | ModelSEED compound data backend |
| `thermo_predictors/modelseed_db.py` | `ModelseedDbBackend` | ModelSEED database backend (offline) |
| `thermo_predictors/dgpredictor.py` | `DgPredictorBackend` | ML-based ΔG prediction |
| `thermo_predictors/molgpk.py` | `MolgpkBackend` | Group-contribution pKa / ΔGf |

## Facade access

```python
from kbutillib.toolkit import KBUtilLib

kbu = KBUtilLib()

# Legacy ModelSEED lookup (always available)
dg = kbu.thermo.get_compound_deltag("cpd00002")

# Predictive ΔG (uses best available backend)
result = kbu.predictive_thermo.compound_dgf("cpd00002")

# Reaction ΔG prime at physiological conditions
rxn_result = kbu.predictive_thermo.reaction_dg_prime(
    reaction_id="rxn00001", ph=7.0, ionic_strength=0.1
)

# Check which backends are ready
status = kbu.predictive_thermo.backend_status()
```

## Capabilities

| Capability name | Summary |
|-----------------|---------|
| `thermo.reaction_dg_prime` | Predict standard transformed Gibbs free energy (ΔG'°) of a reaction |
| `thermo.compound_dgf` | Predict standard Gibbs formation energy of a compound |
| `thermo.backend_status` | Report availability of each thermodynamics backend |

```console
kbu cap list --domain thermo
kbu cap info thermo.compound_dgf
```

## Optional dependencies

| Package | Enables | Install |
|---------|---------|---------|
| `equilibrator-api` | Equilibrator backend | `pip install equilibrator-api` |
| `torch` | ML-based dgpredictor backend | `pip install torch` |
| `rdkit` | Molgpk group-contribution backend | `conda install -c conda-forge rdkit` |

`ThermoUtils` has no optional dependencies. `PredictiveThermoUtils.backend_status()` reports which backends loaded successfully; missing backends do not prevent the others from running.

## Adding a capability here

See root `CONTRIBUTING.md` → "Add a domain capability".

1. For new lookup methods: add to `ThermoUtilsImpl` or `PredictiveThermoUtilsImpl`.
2. For new backends: subclass `ThermoPredictor`, implement `predict_compound_dgf` and `predict_reaction_dg_prime`, register in `thermo_predictors/__init__.py`.
3. Decorate new public methods with `@capability(domain="thermo", summary="...", tags=("thermo",))`.
4. Guard smoke tests with `pytest.importorskip` for the required package.
