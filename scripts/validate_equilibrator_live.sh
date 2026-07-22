#!/usr/bin/env bash
# Live eQuilibrator validation for KBUtilLib predictive_thermo.
# - Redirects ALL caches to /scratch (home is nearly full).
# - Installs equilibrator-api into the kbutillib conda env if missing.
# - Runs the real ATP-hydrolysis calc THROUGH our EquilibratorBackend.
# - Cleans up the multi-GB cache afterward no matter what.
set -uo pipefail

SCRATCH_CACHE=/scratch/vsetlur/.eq_validation_cache
export XDG_CACHE_HOME="$SCRATCH_CACHE"
export EQUILIBRATOR_CACHE="$SCRATCH_CACHE/equilibrator"
export HOME_REAL="$HOME"
export MPLCONFIGDIR="$SCRATCH_CACHE/mpl"
mkdir -p "$SCRATCH_CACHE"

cleanup() {
  echo "=== CLEANUP: removing $SCRATCH_CACHE ==="
  rm -rf "$SCRATCH_CACHE"
  du -sh "$SCRATCH_CACHE" 2>/dev/null || echo "cache removed"
}
trap cleanup EXIT

source /scratch/vsetlur/anaconda3/etc/profile.d/conda.sh
conda activate kbutillib

echo "=== Ensuring equilibrator-api is installed ==="
python -c "import equilibrator_api" 2>/dev/null || pip install "equilibrator-api>=0.6.0"

echo "=== Running live ATP-hydrolysis validation through EquilibratorBackend ==="
cd /scratch/vsetlur/KBUtilLib
python - <<'PY'
from kbutillib.thermo_predictors import EquilibratorBackend

be = EquilibratorBackend()
print("available:", be.available)
if not be.available:
    print("UNAVAILABLE_REASON:", be.unavailable_reason)
    raise SystemExit("equilibrator did not become available")

# ATP + H2O -> ADP + Pi  (KEGG ids)
stoich = {"kegg:C00002": -1, "kegg:C00001": -1, "kegg:C00008": 1, "kegg:C00009": 1}
est = be.reaction_dg_prime("atp_hydrolysis", stoich, ph=7.0, ionic_strength=0.25, temperature=298.15)
print("RESULT dg_prime (kJ/mol):", est.dg_prime)
print("RESULT uncertainty (kJ/mol):", est.dg_prime_uncertainty)
print("equation:", est.equation)
print("warnings:", est.warnings)

dg = est.dg_prime
if dg is None:
    raise SystemExit("FAIL: no value produced")
# Literature ATP hydrolysis dG'm is roughly -26 to -36 kJ/mol at these conditions.
if -45.0 <= dg <= -15.0:
    print("VALIDATION_PASS: ATP hydrolysis in expected range (-45..-15 kJ/mol)")
else:
    print(f"VALIDATION_WARN: {dg} kJ/mol outside expected -45..-15 window")
PY

echo "=== DONE ==="
