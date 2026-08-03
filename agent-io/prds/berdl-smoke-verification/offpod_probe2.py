"""Off-pod BERDL smoke probe — items 2 and 5 (REST read against hub.berdl.kbase.us)."""
import os, sys, json, traceback

from kbutillib.domains.kbase.berdl.capability import BerdlCapability
from kbutillib.domains.kbase.berdl import naming as N

print("=" * 70)
print("ITEM 2 + 5: REST databases() off-pod — personal catalog + dual-name")
print("=" * 70)

cap = BerdlCapability()
print(f"  locus() -> {cap.locus()!r}")
try:
    dbs = cap.databases()
except Exception as e:
    print(f"  FAIL cap.databases(): {type(e).__name__}: {e}")
    traceback.print_exc()
    sys.exit(0)

print(f"  normalized entries: {len(dbs)}")
print()
print("  --- normalized (canonical name | iceberg? | legacy alias) ---")
for d in dbs:
    print(f"    {d.name:<42} iceberg={d.is_iceberg!s:<5} legacy={d.legacy_alias}")

print()
print("  --- ITEM 5 checks: dual-name disambiguation ---")
dotted = [d for d in dbs if d.is_iceberg]
legacy_only = [d for d in dbs if not d.is_iceberg]
paired = [d for d in dbs if d.legacy_alias]
print(f"    dotted/Iceberg canonical entries : {len(dotted)}")
print(f"    legacy-underscored-only entries  : {len(legacy_only)}")
print(f"    entries carrying a legacy_alias  : {len(paired)}")
# The failure mode: same dataset appearing as two unrelated entries.
names = [d.name for d in dbs]
collisions = [n for n in names if N._dotted_to_underscored(n) in names and "." in n]
print(f"    dotted names whose underscored twin ALSO appears as a separate entry: {collisions}")
print(f"    -> {'FAIL (not folded)' if collisions else 'OK (folded, no duplicate entries)'}")

print()
print("  --- ITEM 2 checks: personal catalog visibility ---")
me = os.environ.get("USER") or "chenry"
personal_hits = [d for d in dbs if d.name.split(".")[0] in (me, "my", f"u_{me}")
                 or d.name.startswith((f"{me}_", f"u_{me}_", f"{me}."))]
print(f"    username assumed: {me!r}")
print(f"    entries matching the personal catalog: {len(personal_hits)}")
for d in personal_hits:
    print(f"      {d.name}")
if not personal_hits:
    print("    -> personal catalog NOT visible off-pod (design-time observation CONFIRMED)")
else:
    print("    -> personal catalog IS visible off-pod (design-time observation CONTRADICTED)")

print()
print("  --- distinct catalog prefixes seen ---")
prefixes = sorted({(d.name.split(".")[0] if "." in d.name else d.name.split("_")[0]) for d in dbs})
print(f"    {prefixes}")
