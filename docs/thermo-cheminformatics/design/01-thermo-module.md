# Thermo module design (the prerequisite)

This is the module Henry wants built FIRST. It wraps three external thermodynamic tools
under one installation footprint and one API.

## The three tools (what each provides, install reality, license)

### 1. equilibrator (equilibrator-api)
- Source: https://gitlab.com/equilibrator/equilibrator-api (verified: MIT license,
  actively maintained, last commit ~4 days before this writing).
- Install: `pip install equilibrator-api`. On first use it downloads a large
  component-contribution cache (the `equilibrator_cache` / quilt data, hundreds of MB).
- Provides: deltaG of formation and deltaG of reaction with rigorous uncertainty (the
  component-contribution method by Noor et al.), pH / ionic-strength / pMg adjustment,
  compound + reaction search by KEGG/ChEBI/etc.
- API surface (stable, public, can wire confidently):
    from equilibrator_api import ComponentContribution, Q_
    cc = ComponentContribution()
    cc.p_h = Q_(7.0); cc.ionic_strength = Q_("0.25M")
    cmp = cc.get_compound("kegg:C00031")           # glucose
    rxn = cc.parse_reaction_formula("kegg:C00002 + kegg:C00001 = kegg:C00008 + kegg:C00009")
    dg = cc.standard_dg_prime(rxn)                  # returns value +/- uncertainty (pint Quantity)
- STATUS: BUILDABLE NOW. This backend can be implemented and validated this session
  if/when the env is set up. It is the anchor that proves the umbrella API shape.

### 2. dGPredictor
- Group-contribution + machine-learning deltaG predictor (Maranas lab; structure-based,
  uses molecular fingerprints / automated group decomposition). Handles compounds that
  component-contribution cannot (novel/unmapped metabolites) -- important for pickaxe-
  generated compounds that have no database ID.
- Install reality: research repo, NOT a clean pip package. Pulls in rdkit, and historically
  ships model files / a ChemAxon or openbabel dependency for protonation. Exact public
  entry point is not standardized.
- STATUS: NEEDS ANDREW. Need: which fork/commit he runs, how he installs it (conda env?
  vendored?), and the exact function he calls to get dG_f for a SMILES/InChI.

### 3. molGPK
- pKa + predominant-ion / protonation-state predictor (this is the pKa + "predominant ions"
  part of Henry's ask). "andrew knows the method." Likely a graph-neural / group-contribution
  pKa model. Not a mainstream pip package; details are with Andrew.
- STATUS: NEEDS ANDREW. Need: repo/commit, install method, and the call that maps a
  compound -> pKa values + dominant ionic species at a given pH.

## Why we cannot safely write the dGPredictor / molGPK adapters yet

Both are research codebases whose import paths, model-file locations, and call signatures
are not discoverable without Andrew's working setup. Writing adapters from guesses would
mean inventing APIs that may not exist -- the wrong kind of output. equilibrator is the
exception because it is a real, documented, MIT pip package.

## Common installation footprint (Henry's explicit requirement)

Mirror the skani pattern but generalize for python-package deps and optional tools:

- `pyproject.toml` optional-dependency extras so install is opt-in and KBUtilLib import
  never breaks if a tool is missing:
    [project.optional-dependencies]
    thermo      = ["equilibrator-api>=0.6", "rdkit"]
    thermo-full = ["equilibrator-api>=0.6", "rdkit", "<dgpredictor>", "<molgpk>"]   # filled after Andrew
  Install: `pip install KBUtilLib[thermo]`.
- `dependencies.yaml` entries for the git-based tools (dGPredictor, molGPK) so the existing
  dependency_manager can clone/checkout pinned commits into sibling dirs, same mechanism
  already used for ModelSEEDpy / cobrakbase.
- Each backend does a lazy import inside its methods and a `is_available()` probe, so a
  partial install degrades gracefully (equilibrator works even if molGPK is absent).
- One config block in config.yaml:
    thermo:
      default_ph: 7.0
      default_ionic_strength: "0.25M"
      backend_order: ["equilibrator", "dgpredictor", "modelseed_db"]
      equilibrator_cache_dir: "~/.kbutillib/equilibrator_cache"
      dgpredictor_path: "../dGPredictor"      # filled after Andrew
      molgpk_path: "../molGPK"                # filled after Andrew

## Common API (tool-agnostic surface)

ThermoUtils umbrella methods, each dispatching to a backend and recording provenance:

  compound_pka(compound, ph=None)
      -> {"pka": [...], "predominant_ion": "...", "backend": "molgpk"}
  predominant_ions(compound, ph=None)
      -> {"species": "...", "charge": int, "backend": "..."}
  formation_energy(compound, ph=None, ionic_strength=None, backend="auto")
      -> {"dg_f": float_kJ_per_mol, "uncertainty": float, "backend": "..."}
  reaction_energy(reaction, ph=None, ionic_strength=None, backend="auto")
      -> {"dg_r": float_kJ_per_mol, "uncertainty": float, "backend": "...", "warnings": [...]}

Inputs accept: ModelSEED IDs (resolved via existing biochem util), KEGG/ChEBI IDs, SMILES,
InChI, or a cobra/MS compound/reaction object. The umbrella normalizes the identifier,
then routes to a backend that supports that identifier type.

Backend resolution for backend="auto": walk config `backend_order`, skip unavailable
backends and backends that cannot handle the given identifier, use the first that can.
Always return which backend answered.

## Module layout (in KBUtilLib)

  src/kbutillib/thermo/__init__.py            re-export ThermoUtils, ThermoUtilsImpl
  src/kbutillib/thermo/base.py                umbrella + dispatch + identifier normalization
  src/kbutillib/thermo/backends/modelseed_db.py     existing thermo_utils logic, refactored
  src/kbutillib/thermo/backends/equilibrator_backend.py   BUILD NOW
  src/kbutillib/thermo/backends/dgpredictor_backend.py    AFTER ANDREW
  src/kbutillib/thermo/backends/molgpk_backend.py         AFTER ANDREW

Back-compat shim: keep `src/kbutillib/thermo_utils.py` importing the names from the new
subpackage so existing imports and `tests/test_ms_biochem_deltag.py` keep working.
Update toolkit.py `kbu.thermo` property to construct the new ThermoUtilsImpl.

## Tests

- Keep tests/test_ms_biochem_deltag.py green (back-compat).
- New: tests/thermo/test_equilibrator_backend.py -- skip-if-not-installed; on a known
  reaction (ATP hydrolysis) assert dG_r' is in the literature range (~ -30 kJ/mol at pH 7).
- New: tests/thermo/test_umbrella_dispatch.py -- backend selection + provenance, using a
  fake backend so it runs without any heavy tool installed.
- dGPredictor / molGPK tests added once their adapters exist.
