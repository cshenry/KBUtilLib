# kbutillib.domains.biochem — Biochemistry Search

ModelSEED compound and reaction lookup, biochemistry search, and chemical identifier resolution.

## What lives here

`MSBiochemUtils` wraps the ModelSEED biochemistry database to search compounds by name, formula, or InChI; retrieve reactions by identifier; and resolve cross-database identifiers (KEGG, ChEBI, BiGG). This is the only domain with `@capability`-decorated methods registered out of the box, making biochem the canonical reference implementation of the capability pattern.

## Modules

| File | Class(es) | Purpose |
|------|-----------|---------|
| `ms_biochem_utils.py` | `MSBiochemUtils`, `MSBiochemUtilsImpl` | Compound/reaction search, ID lookup, cross-reference resolution |
| `schemas.py` | `CompoundResult`, `ReactionResult` | Pydantic v2 I/O models for `@capability` typed transports |

## Facade access

```python
from kbutillib.toolkit import KBUtilLib

kbu = KBUtilLib()
biochem = kbu.biochem

# Search by name or partial string
hits = biochem.search_compounds("adenosine triphosphate")

# Look up a specific compound
atp = biochem.get_compound_by_id("cpd00002")

# Look up a reaction
rxn = biochem.get_reaction_by_id("rxn00001")
```

## Capabilities

All three methods are `@capability`-decorated — they appear in `kbu cap list` and are available to MCP and API transports without additional wiring.

| Capability name | Summary |
|-----------------|---------|
| `biochem.search_compounds` | Search ModelSEED biochemistry for compounds matching a query |
| `biochem.get_compound_by_id` | Retrieve a compound record by ModelSEED ID |
| `biochem.get_reaction_by_id` | Retrieve a reaction record by ModelSEED ID |

```console
kbu cap list --domain biochem
kbu cap info biochem.search_compounds
kbu cap run biochem.search_compounds --query atp --limit 5
```

## Optional dependencies

The core ModelSEED data is bundled. No optional packages required for basic search. Network-dependent features (live KEGG/ChEBI sync) need outbound HTTP.

## Adding a capability here

See root `CONTRIBUTING.md` → "Add a domain capability". Biochem is the reference pattern:

```python
from ...core.capability import capability

@capability(
    domain="biochem",
    summary="Get all aliases for a compound.",
    tags=("biochem", "lookup", "readonly"),
    visibility="public",
)
def get_compound_aliases(self, compound_id: str) -> list[str]:
    """Return all database aliases for a ModelSEED compound ID."""
    ...
```

After decorating, run `kbu cap list --domain biochem` to confirm. Add a schema to `schemas.py` if the return type is complex.
