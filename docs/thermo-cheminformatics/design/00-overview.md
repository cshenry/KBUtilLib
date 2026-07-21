# Overview: thermo + cheminformatics expansion of KBUtilLib

## What Henry asked for (parsed from Slack, 9:55-9:57 AM)

Sender: Christopher Henry (PI).

1. Add a cheminformatics MODULE to KBUtilLib. Start with the pickaxe tool (reimplemented
   in Python), then expand to other methods: retrorules, the Tyo-lab pickaxe (a branch of
   ours), and other literature methods. Goal: run on metagenomics datasets to study
   accessible organic matter.
2. PREREQUISITE (explicit, stated before the cheminformatics work): a THERMODYNAMICS
   module. Ping Andrew. It must run:
     - equilibrator
     - dGPredictor
     - molGPK   (Andrew knows this method)
   on compounds and reactions to get: pKa, predominant ions, deltaG of formation,
   deltaG of reaction.
3. Architectural mandate: Andrew has these in SEPARATE modules. Henry wants ONE module in
   KBUtilLib that puts all three under "a common installation footprint and API". This
   makes them available in KBase to a set of agent skills, and gives a single umbrella for
   maintaining and testing the packages.
4. After thermo + cheminformatics: TEST the reaction-similarity tools already added
   (PR #44, kbu.rxnsim / MSReactionSimilarityUtils).

## How KBUtilLib is structured (verified by reading the repo)

- Every utility is a class `XxxUtils(SharedEnvUtils)` in `src/kbutillib/xxx_utils.py`.
  SharedEnvUtils -> BaseUtils gives logging, config (config.yaml + ~/.kbutillib/config.yaml),
  token handling, provenance tracking (initialize_call), and data dir helpers.
- Newer modules ALSO ship a composition wrapper `XxxUtilsImpl` at the bottom of the same
  file: holds `env` (+ any sibling utils it needs) and delegates to an internal legacy
  instance via `__getattr__`. thermo_utils.py already follows this dual pattern.
- The umbrella facade is `toolkit.py::KBUtilLib`. Each sub-util is a lazy `@property`
  that constructs the `Impl` on first access, passing `self.env` and dependencies.
  Example already present: `kbu.thermo -> ThermoUtilsImpl(self.env, self.biochem)`.
- External-binary tools (skani_utils.py) follow a clear contract: config-driven
  executable path, a `_check_xxx_availability()` probe at init, graceful messaging when
  the tool is absent. This is the template for "common installation footprint".
- Optional heavy deps are handled via `dependencies.yaml` + a dependency_manager and an
  optional-import demo (`demo_optional_imports.py`), so a missing tool does not break import.

## Key finding: thermo_utils.py already exists but is NOT what Henry wants

The current `thermo_utils.py` only does:
  - ModelSEED-DB compound deltaG lookups (reads `compound.deltag` from the MS database)
  - reaction deltaG by summing stored formation energies
  - ion-transfer accounting across compartments

It does NOT run equilibrator, dGPredictor, or molGPK. It has no pKa or predominant-ion
prediction. So Henry's request is a real expansion, not already done. Decision (see
03-decisions): keep the existing class as a "modelseed_db" backend and add the three
predictor backends alongside it under one umbrella API, rather than replacing it.

## Proposed package layout (lands in KBUtilLib, not here)

A SUBPACKAGE rather than one giant file, because three heavy external tools + the existing
DB backend is too much for a single module:

  src/kbutillib/thermo/
    __init__.py                 re-exports ThermoUtils / ThermoUtilsImpl (back-compat)
    base.py                     ThermoUtils umbrella: unified API, backend dispatch
    backends/
      __init__.py
      modelseed_db.py           existing logic moved here (lookup formation energies)
      equilibrator_backend.py   equilibrator-api wrapper (MIT, pip) -- BUILDABLE NOW
      dgpredictor_backend.py    dGPredictor wrapper           -- NEEDS ANDREW
      molgpk_backend.py         molGPK wrapper (pKa/ions)      -- NEEDS ANDREW

Back-compat: `from kbutillib.thermo_utils import ThermoUtils` keeps working (shim that
imports from the subpackage). Existing test `tests/test_ms_biochem_deltag.py` must stay green.

Unified umbrella API (the "common API" Henry wants), tool-agnostic at the call site:

  thermo.compound_pka(cpd, ph=7.0)               -> pKa list + predominant ion at pH      (molGPK)
  thermo.predominant_ions(cpd, ph=7.0)           -> dominant protonation state            (molGPK / equilibrator)
  thermo.formation_energy(cpd, backend="auto")   -> dG_f (kJ/mol) + uncertainty           (equilibrator / dGPredictor / modelseed_db)
  thermo.reaction_energy(rxn, ph, ionic, backend)-> dG_r (kJ/mol) + uncertainty           (equilibrator / dGPredictor)

`backend="auto"` picks the best available installed tool with a documented fallback order;
each call result records which backend produced it (provenance, via initialize_call).

## Cheminformatics module (follow-on, after thermo)

  src/kbutillib/cheminformatics/
    __init__.py
    base.py                     CheminformaticsUtils umbrella
    pickaxe_backend.py          MINE pickaxe (reaction-rule network expansion) -- FIRST
    retrorules_backend.py       RetroRules reaction-rule sets
    tyo_pickaxe_backend.py      Tyo-lab pickaxe branch
  Exposed as `kbu.network_expansion` on the toolkit facade
  (design notes originally planned `kbu.chem`; the implemented name is
  `kbu.network_expansion` to avoid shadowing `kbu.thermo` legacy semantics).
  Depends on thermo for thermodynamic feasibility filtering of generated reactions
  (which is precisely why Henry ordered thermo first).

## Build order (do not reorder -- it is Henry's)

1. thermo umbrella + equilibrator backend (buildable now; validate on a known compound)
2. dGPredictor + molGPK backends (after Andrew confirms install + API)
3. cheminformatics: pickaxe backend first, validate, then retrorules + tyo
4. test the rxnsim tools (PR #44)
