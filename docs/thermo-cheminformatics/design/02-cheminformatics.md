# Cheminformatics module design (follow-on, after thermo)

Built AFTER the thermo module because reaction generation needs thermodynamic feasibility
filtering (the reason Henry ordered thermo first). Goal: run on metagenomics datasets to
study accessible organic matter (which compounds are biochemically reachable from a
starting set of substrates).

## Methods (Henry's list + literature)

### 1. pickaxe (FIRST)
- The MINE-database pickaxe: applies reaction-operator rules (SMARTS/SMIRKS) to a set of
  starting compounds to enumerate a network of predicted products (in-silico metabolism /
  promiscuous reaction expansion).
- Reference implementation: MINE-Database/pickaxe (Tyo lab / Jeffryes et al.), BSD-ish,
  pip-installable as `minedatabase`. Depends on rdkit.
- Henry says "implementing the pickaxe tool in python" -- the canonical pickaxe IS python
  (rdkit-based). So this is wrap-and-integrate, optionally a lean reimplementation if he
  wants a dependency-light version. CONFIRM with Henry which he means (wrap vs reimplement)
  -- but that question is downstream of thermo, so it is NOT a blocker now.

### 2. retrorules
- Curated reaction-rule database (RetroRules, Duigou et al.) with rules at multiple
  diameters for retrosynthesis / metabolic expansion. Consumed AS rule sets fed into the
  pickaxe-style expander.

### 3. Tyo-lab pickaxe (a branch of ours)
- Henry: "the tyo lab pickaxe (a branch of ours)". A fork/branch of the MINE pickaxe with
  lab-specific rules or features. Need the exact branch/repo -- minor, ask alongside the
  pickaxe wrap/reimplement question, after thermo.

## Layout (in KBUtilLib)

  src/kbutillib/cheminformatics/__init__.py
  src/kbutillib/cheminformatics/base.py                CheminformaticsUtils umbrella (kbu.chem)
  src/kbutillib/cheminformatics/pickaxe_backend.py     network expansion (FIRST)
  src/kbutillib/cheminformatics/retrorules_backend.py  rule-set loader
  src/kbutillib/cheminformatics/tyo_pickaxe_backend.py Tyo branch

## Dependency footprint
  [project.optional-dependencies]
  chem = ["minedatabase", "rdkit"]
dependencies.yaml entries for retrorules data + the Tyo branch (git, pinned commit).

## Integration with thermo
After expansion, score generated reactions with `thermo.reaction_energy(...)` to flag
thermodynamically infeasible steps and rank accessible organic matter by feasibility.
This is the concrete payoff of building thermo first.

## Then: test rxnsim (PR #44)
Last step in Henry's sequence. The MSReactionSimilarityUtils / kbu.rxnsim module from
PR #44 gets exercised against the cheminformatics-generated reactions and the BERDL set.
Existing test: tests/test_ms_reaction_similarity.py (on the PR branch).
