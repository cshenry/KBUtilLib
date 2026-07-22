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

---

## verAB methoxy-aromatic Pickaxe rule discovery (`kbu.verab`)

The verAB feature extends the cheminformatics stack with a dedicated workflow for
discovering and validating methoxy-aromatic O-demethylation rules (EC 1.14.13.82)
using Pickaxe network expansion and optionally confirming them via RDKit SMARTS.

### Purpose

`kbu.verab` answers the question: *which Pickaxe rule operators reproduce the
verAB aryl-methyl-ether O-demethylation chemistry on the 5 canonical seed
compounds (vanillate, isovanillate, guaiacol, 4-methoxybenzoate, veratrate)?*
The facade also enumerates all methoxy-aromatic compounds in the biochem DB,
screens predicted products against the ModelSEED database, and optionally
predicts per-genome degradation capacity.

### CLI (`kbu verab`)

```bash
kbu verab --help                           # list subcommands
kbu verab discover --json                  # run Phase-1 rule discovery on 5 seeds
kbu verab enumerate --json                 # RDKit substructure scan of biochem DB
kbu verab screen --operators <id> --json   # cross-reference predicted products
kbu verab emit-king --outdir ./king_out    # write KING coscientist input bundle
```

### RDKit — optional dependency

RDKit is **optional** for the verAB workflow. Without it:
- `kbu verab discover` still runs but uses text-matching against rule
  SMARTS/operator names (confidence 0.5; `method="smarts_text"`).
- `kbu verab enumerate` raises `BackendUnavailableError` (substructure scan
  requires RDKit).
- All other commands function without RDKit.

Install RDKit via conda (recommended) or the pip extra:

```bash
# conda (recommended — avoids binary wheel issues)
conda install -c conda-forge rdkit

# pip extra (pure-Python wheel where available)
pip install "KBUtilLib[cheminformatics]"
```

### MINE-Database / minedatabase — Python version pin

The MINE-Database repo (Tyo lab, `github.com/tyo-nu/MINE-Database`) carries a
`python_requires <3.10` in its `setup.py`. **This pin is a pip metadata gate
only.** KBUtilLib's `DependencyManager` adds the MINE-Database source checkout
directly to `sys.path` at import time, completely bypassing the pip gate.
minedatabase imports cleanly on Python 3.11 and 3.12 (confirmed in the active
dev environment).

There is **no subprocess isolation, no `.pth` shim, and no version pin change
needed.** The mechanism is the same DependencyManager pattern used for all other
KBUtilLib source-checkout dependencies (ModelSEEDpy, cobrakbase, etc.).

The MINE-Database dependency is declared in `dependencies.yaml`:

```yaml
MINE-Database:
  path: "../MINE-Database"
  git: "https://github.com/tyo-nu/MINE-Database.git"
```

Clone and wire it the same way as all other source-checkout backends:

```python
from kbutillib.dependency_manager import get_dependency_manager
get_dependency_manager().initialize_dependencies(checkout_if_missing=True)
```

If the checkout or bundled rule TSVs are missing, `kbu.verab.status()` reports
`pickaxe: unavailable` with an actionable reason; no crash occurs.

### Optional config keys (`config.yaml`)

```yaml
cheminformatics:
  verab:
    # Path to a mechanism-informed rule TSV (e.g. Pate-2026 verAB-specific
    # operators). When null, the bundled metacyc_generalized rule set is used.
    operator_rule_tsv: null        # e.g. "/path/to/verab_mechanism_rules.tsv"
    # Default output directory for `kbu verab emit-king`. When null the caller
    # must pass --outdir or the command defaults to the current directory.
    king_outdir: null              # e.g. "./verab_king_output"
```

Both keys are fully optional. The verAB feature runs with no config changes
beyond what Pickaxe already requires (`cheminformatics.pickaxe.data_dir`).

### Live integration test

```bash
# Requires: MINE-Database checkout + rdkit in PATH
pytest tests/test_verab_live.py -q
```

Asserts ≥1 operator matches verAB O-demethylation across 5 seeds and that
`emit-king` writes a valid manifest.
