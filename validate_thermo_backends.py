#!/usr/bin/env python
"""End-to-end validation of the KBUtilLib predictive-thermo backends against
Andrew Freiburger's real repos (OPAM2 / dGPredictor), exercised through the
*public facade* the way downstream KBUtilLib consumers use it.

Usage (from the KBUtilLib repo root, in an env with rdkit+torch+torch-geometric
+ sklearn/scipy/pandas/joblib + openbabel, e.g. conda env 'opam2'):

    OPAM2_REPO=/scratch/vsetlur/andrew-repos/OPAM2 \
    DGPREDICTOR_REPO=/scratch/vsetlur/andrew-repos/dGPredictor \
    python validate_thermo_backends.py

It configures the two subprocess backends via the same env vars / config keys a
real user would set, then calls the high-level PredictiveThermoUtils methods and
prints exactly what the backends return (no fabricated numbers).
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from kbutillib.predictive_thermo_utils import PredictiveThermoUtils  # noqa: E402


def main():
    thermo = PredictiveThermoUtils(
        config_file=False, token_file=None, kbase_token_file=None
    )

    print("=== backend_status ===")
    print(json.dumps(thermo.backend_status(), indent=2))

    print("\n=== molGPK: acetic acid pKa + major microspecies (pH 7) ===")
    est = thermo.compound_microspecies("CC(=O)O", ph=7.0)
    print("pka_values:", est.pka_values)
    print("major_microspecies:", est.major_microspecies)
    print("warnings:", est.warnings)

    print("\n=== molGPK batch: acetic acid + glycine ===")
    ests = thermo.compounds_microspecies(["CC(=O)O", "C(C(=O)O)N"], ph=7.0)
    for e in ests:
        print(f"  {e.compound_id}: pKa={e.pka_values} major={e.major_microspecies}")

    print("\n=== dGPredictor: reaction by ModelSEED accession (rxn00001) ===")
    rest = thermo.reaction_dg_prime("rxn00001", backend="dgpredictor", ph=7.0)
    print("dg_prime:", rest.dg_prime, rest.raw.get("units") if rest.raw else "")
    print("uncertainty:", rest.dg_prime_uncertainty)
    print("warnings:", rest.warnings)

    print("\n=== dGPredictor: ATP hydrolysis by stoichiometry ===")
    # ATP + H2O -> ADP + Pi  (ModelSEED: cpd00002 + cpd00001 -> cpd00008 + cpd00009)
    rest2 = thermo.reaction_dg_prime(
        "atp_hydrolysis",
        stoichiometry={"cpd00002": -1, "cpd00001": -1, "cpd00008": 1, "cpd00009": 1},
        backend="dgpredictor",
        ph=7.0,
    )
    print("dg_prime:", rest2.dg_prime)
    print("uncertainty:", rest2.dg_prime_uncertainty)
    print("warnings:", rest2.warnings)


if __name__ == "__main__":
    main()
