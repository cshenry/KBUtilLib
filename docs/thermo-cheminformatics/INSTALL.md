# Thermo + cheminformatics backends — install & footprint

This documents how KBUtilLib's predictive-thermodynamics (`kbu.predictive_thermo`)
and cheminformatics network-expansion (`kbu.network_expansion`) backends are
installed and wired, so the whole thing is reproducible and self-contained the
way the rest of KBUtilLib's external dependencies are (ModelSEEDpy,
ModelSEEDDatabase, cobrakbase).

> **API note:** `kbu.thermo` is the *legacy* ModelSEED-lookup ThermoUtils facade
> (always present, no prediction). The new predictive backends live under
> `kbu.predictive_thermo`. `kbu.network_expansion` is the cheminformatics
> expansion facade (replaces the earlier `kbu.chem` name used in design notes).

## What Henry asked for

One module under a common installation footprint and API that runs equilibrator,
dGPredictor, and molGPK on compounds/reactions (pKa, predominant ions, dGf, dGr),
plus a cheminformatics module starting with pickaxe. The backends below all sit
behind the `PredictiveThermoUtils` / `NetworkExpansionUtils` facades, so callers
use one API regardless of which tool answers, and a missing tool degrades
gracefully (never fabricates a value).

## The five thermo backends + pickaxe

| backend       | tool                                   | install kind                  |
|---------------|----------------------------------------|-------------------------------|
| equilibrator  | equilibrator-api (MIT)                 | pip: `KBUtilLib[thermo]`      |
| modelseed_db  | Andrew's MSDB fork (baked dG TSVs)     | data dir (config/env)         |
| modelseed     | existing ThermoUtils DB lookups        | none (always available)       |
| dgpredictor   | Andrew's dGPredictor ModelSEED fork    | source checkout (dependencies)|
| molgpk        | Andrew's OPAM2 (MolGpKa-on-MSDB)       | source checkout (dependencies)|
| pickaxe       | Tyo-NU MINE-Database                   | source checkout (dependencies)|

## Source-checkout backends (dGPredictor, OPAM2, MINE-Database)

These three are research repos that are NOT cleanly pip-installable, so KBUtilLib
consumes them as **source checkouts via the DependencyManager** — the identical
mechanism used for ModelSEEDpy etc. They are declared in `dependencies.yaml`:

```yaml
dependencies:
  dGPredictor:   { path: "../dGPredictor",   git: "https://github.com/freiburgermsu/dGPredictor.git" }
  OPAM2:         { path: "../OPAM2",          git: "https://github.com/freiburgermsu/OPAM2.git" }
  MINE-Database: { path: "../MINE-Database",  git: "https://github.com/tyo-nu/MINE-Database.git" }
```

Clone them (or let the manager clone on demand):

```python
from kbutillib.dependency_manager import get_dependency_manager
get_dependency_manager().initialize_dependencies(checkout_if_missing=True)
```

Once present, the backends resolve their repo paths automatically from the
DependencyManager — no `thermo.*.repo_path` config key, no environment variable,
and no `pip install` / `.pth` shim is required. (The MINE-Database repo pins
`python_requires <3.10`; the DependencyManager puts it on `sys.path` directly so
that pin does not block use on 3.11.)

Override paths if you keep the clones elsewhere, via either
`~/.kbutillib/dependencies.yaml` (recommended) or, per-backend,
`thermo.dgpredictor.repo_path` / `thermo.molgpk.repo_path` in config or the
`DGPREDICTOR_REPO` / `MOLGPK_REPO` (a.k.a. `OPAM2_REPO`) / `KBUTILLIB_PICKAXE_DATA_DIR`
environment variables.

## Runtime (Python) dependencies — one common env

A single conda env serves all backends (Henry's "common installation footprint"):

```bash
# rdkit + the thermo predictors
conda install -c conda-forge rdkit openbabel
pip install torch torch-geometric            # OPAM2 / molGPK
pip install scikit-learn scipy pandas joblib # dGPredictor
pip install python-libsbml lxml pymongo      # MINE-Database / pickaxe
pip install "KBUtilLib[thermo]"              # equilibrator-api (optional)
```

## Model artifacts

- **OPAM2**: ships `models/weight_acid_modelseed.pth` + `weight_base_modelseed.pth`
  (real files in the repo — no extra step).
- **dGPredictor**: needs `model/modelseed_M12_model_BR.pkl`. This is a generated
  artifact; the upstream Git-LFS object 404s, so rebuild it from the in-repo
  training matrix using the script that ships **inside the dGPredictor repo**:

  ```bash
  cd ../dGPredictor          # the checkout declared in dependencies.yaml
  python retrain_modelseed.py
  # Produces model/modelseed_M12_model_BR.pkl (R²≈0.9997, ~several MB)
  ```

  The backend reports a precise "model artifact missing" reason if it is absent
  and never fabricates a value.

## Verifying

```python
from kbutillib.predictive_thermo_utils import PredictiveThermoUtils
from kbutillib.network_expansion_utils import NetworkExpansionUtils
print(PredictiveThermoUtils().backend_status())   # molgpk / dgpredictor -> available
print(NetworkExpansionUtils().backend_status())   # pickaxe -> available
```

Validated results (acetic acid pKa 2.01 → `CC(=O)[O-]`; glycine zwitterion;
dGPredictor rxn00001 −15.76 ± 3.63 kJ/mol, ATP hydrolysis −25.46 kJ/mol; pickaxe
glucose 1-gen → 1696 reactions). Live integration tests: `test_molgpk_live_*`,
`test_dgpredictor_live_*`, `test_pickaxe_live_expansion`.
