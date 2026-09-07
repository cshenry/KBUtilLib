# kbutillib.domains.cheminformatics — Network Expansion & Rule-Based Screening

Metabolic network expansion via reaction-rule engines (Pickaxe, RetroRules) and SMARTS-based
compound screening via the Verab subpackage.

---

## What lives here

Two distinct capabilities live here. `NetworkExpansionUtils` expands a seed set of compounds
through reaction rules to discover reachable metabolites and reactions. `VerabUtils` (backed by
`verab/`) provides SMARTS rule discovery, substructure screening, KIND artifact generation, and
substructure analysis. Both share a common abstract backend interface defined in `base.py`.

## Canonical imports

```python
from kbutillib.domains.cheminformatics.network_expansion_utils import NetworkExpansionUtils
from kbutillib.domains.cheminformatics.verab.facade import VerabFacade
```

---

## Modules

| File | Class(es) | Purpose |
|------|-----------|---------|
| `network_expansion_utils.py` | `NetworkExpansionUtils`, `NetworkExpansionUtilsImpl` | Orchestrates expansion; dispatches to Pickaxe or RetroRules backend |
| `base.py` | `ExpansionBackend` | Abstract base class for expansion backends |
| `pickaxe_backend.py` | `PickaxeBackend` | Pickaxe-powered metabolic expansion (requires `minedatabase`) |
| `retrorules_backend.py` | `RetroRulesBackend` | RetroRules-powered expansion (requires `retrorules`) |
| `verab_utils.py` | `VerabUtils` | Thin shim → canonical implementation is `verab/facade.py` |
| `verab/` | `VerabFacade`, rule_discovery, screening, smarts, substructure | Full Verab rule-based screening pipeline |

---

## Optional dependencies

| Package | Enables | Install |
|---------|---------|---------|
| `minedatabase` | Pickaxe expansion backend | `pip install minedatabase` or conda |
| `retrorules` | RetroRules backend | `pip install retrorules` |
| `rdkit` | SMARTS/substructure operations in Verab | `conda install -c conda-forge rdkit` |

All classes implement `available` / `unavailable_reason`. The domain constructs cleanly even
when backends are absent — `backend_status()` reports which are ready.

---

## Usage example

```python
from kbutillib.domains.cheminformatics.network_expansion_utils import NetworkExpansionUtils

ne = NetworkExpansionUtils()
print(ne.backend_status())  # {'pickaxe': True, 'retrorules': False}

# Expand a seed set (requires at least one backend)
result = ne.expand(
    seed_compounds=["cpd00001", "cpd00002"],
    generations=2,
)
print(result["new_compounds"])

# Verab rule-based screening
from kbutillib.domains.cheminformatics.verab.facade import VerabFacade

verab = VerabFacade()
rules = verab.discover_rules(generations=1)
```

Via the facade:

```python
from kbutillib import KBUtilLib

kbu = KBUtilLib()
status = kbu.network_expansion.backend_status()
result = kbu.network_expansion.expand(seed_compounds=["cpd00001"], generations=1)
```

---

## Available capabilities

| Capability name | Summary |
|-----------------|---------|
| `cheminformatics.expand` | Expand a metabolic network via reaction rules |
| `cheminformatics.backend_status` | Report availability of each cheminformatics backend |

```bash
kbu cap list --domain cheminformatics
kbu cap info cheminformatics.expand
```

---

## Adding a capability here

See root `CONTRIBUTING.md` → "Add a domain capability".

1. Add the method to `NetworkExpansionUtilsImpl` (or `VerabFacade` for Verab operations).
2. Decorate with `@capability(domain="cheminformatics", summary="...", tags=(...))`.
3. Run `kbu cap list --domain cheminformatics` to confirm.
4. Use `pytest.importorskip("rdkit")` in the smoke test if the method requires rdkit.
