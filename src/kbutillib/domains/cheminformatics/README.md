# kbutillib.domains.cheminformatics — Network Expansion & Rule-Based Screening

Metabolic network expansion via reaction-rule engines (Pickaxe, RetroRules) and rule-based compound screening via the Verab subpackage.

## What lives here

Two distinct capabilities live here. `NetworkExpansionUtils` expands a seed set of compounds through reaction rules to discover reachable metabolites and reactions. `VerabUtils` (backed by `verab/`) provides rule-based SMARTS screening, KING artifact generation, and substructure analysis. Both share a common abstract backend interface defined in `base.py`.

## Modules

| File | Class(es) | Purpose |
|------|-----------|---------|
| `network_expansion_utils.py` | `NetworkExpansionUtils`, `NetworkExpansionUtilsImpl` | Orchestrates expansion runs; dispatches to pickaxe or retrorules backend |
| `base.py` | `ExpansionBackend` | Abstract base class for expansion backends |
| `pickaxe_backend.py` | `PickaxeBackend` | Pickaxe-powered metabolic expansion (requires `minedatabase`) |
| `retrorules_backend.py` | `RetroRulesBackend` | RetroRules-powered expansion (requires `retrorules`) |
| `verab_utils.py` | `VerabUtils` | Thin shim → canonical implementation is `verab/facade.py` |
| `verab/` | `VerabFacade`, models, rule_discovery, screening, smarts, substructure | Full Verab rule-based screening pipeline |

## Facade access

```python
from kbutillib.toolkit import KBUtilLib

kbu = KBUtilLib()

# Network expansion (alias: kbu.chem)
status = kbu.network_expansion.backend_status()
result = kbu.network_expansion.expand(
    seed_compounds=["cpd00001", "cpd00002"],
    generations=2,
)

# Verab screening
from kbutillib.domains.cheminformatics.verab.facade import VerabFacade
verab = VerabFacade()
rules = verab.discover_rules(generations=1)
```

## Capabilities

| Capability name | Summary |
|-----------------|---------|
| `cheminformatics.expand` | Expand a metabolic network via pickaxe/retrorules reaction rules |
| `cheminformatics.backend_status` | Report availability of each cheminformatics backend |

```console
kbu cap list --domain cheminformatics
kbu cap info cheminformatics.expand
```

## Optional dependencies

| Package | Enables | Note |
|---------|---------|------|
| `minedatabase` | Pickaxe expansion backend | conda or pip |
| `retrorules` | RetroRules backend | pip |
| `rdkit` | SMARTS/substructure operations in Verab | conda recommended |

All classes implement `available` / `unavailable_reason`. The domain constructs cleanly even when backends are absent — `backend_status()` reports which are ready.

## Adding a capability here

See root `CONTRIBUTING.md` → "Add a domain capability".

1. Add the method to `NetworkExpansionUtilsImpl` (or `VerabFacade` for Verab operations).
2. Decorate with `@capability(domain="cheminformatics", summary="...", tags=(...))`.
3. Run `kbu cap list --domain cheminformatics` to confirm.
4. Use `pytest.importorskip("rdkit")` in the smoke test if the method requires rdkit.

Use `kbu new-capability cheminformatics.<name>` to scaffold the stub.
