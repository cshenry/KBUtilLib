# kbutillib.domains.biochem — Biochemistry Search

ModelSEED compound and reaction lookup, biochemistry search, and chemical identifier resolution.

---

## What lives here

`MSBiochemUtils` wraps the ModelSEED biochemistry database to search compounds by name, formula,
or InChI; retrieve reactions by identifier; and resolve cross-database identifiers (KEGG, ChEBI,
BiGG). Biochem is the canonical reference implementation of the `@capability` pattern — its three
registered capabilities demonstrate the full decorator and schema workflow.

## Canonical import

```python
from kbutillib.domains.biochem.ms_biochem_utils import MSBiochemUtils
```

Or via the top-level re-export (stable across releases):

```python
from kbutillib import MSBiochemUtils
```

---

## Key classes and methods

| Class | Method | Description |
|-------|--------|-------------|
| `MSBiochemUtils` / `MSBiochemUtilsImpl` | `search_compounds(query, limit)` | Search by name, formula, or InChI |
| | `get_compound_by_id(compound_id)` | Retrieve a compound record by ModelSEED ID |
| | `get_reaction_by_id(reaction_id)` | Retrieve a reaction record by ModelSEED ID |

`schemas.py` provides pydantic v2 `CompoundResult` and `ReactionResult` models used by the
capability's typed transports.

---

## Optional dependencies

None. The core ModelSEED data is bundled with the package. Network-dependent features (live
KEGG/ChEBI sync) need outbound HTTP but degrade gracefully when offline.

---

## Usage example

```python
from kbutillib.domains.biochem.ms_biochem_utils import MSBiochemUtils

biochem = MSBiochemUtils()

# Search by name or partial string
hits = biochem.search_compounds("adenosine triphosphate")
print(hits[0])  # {'id': 'cpd00002', 'name': 'ATP', 'formula': 'C10H12N5O13P3', ...}

# Look up a specific compound
atp = biochem.get_compound_by_id("cpd00002")
print(atp["formula"])  # C10H12N5O13P3

# Look up a reaction
rxn = biochem.get_reaction_by_id("rxn00001")
print(rxn["equation"])
```

Via the facade:

```python
from kbutillib import KBUtilLib

kbu = KBUtilLib()
hits = kbu.biochem.search_compounds("glucose")
```

---

## Available capabilities

All three methods are `@capability`-decorated and appear in every transport automatically.

| Capability name | Summary |
|-----------------|---------|
| `biochem.search_compounds` | Search ModelSEED biochemistry for compounds matching a query |
| `biochem.get_compound_by_id` | Retrieve a compound record by ModelSEED ID |
| `biochem.get_reaction_by_id` | Retrieve a reaction record by ModelSEED ID |

```bash
kbu cap list --domain biochem
kbu cap info biochem.search_compounds
kbu cap run biochem.search_compounds --query atp --limit 5
```

---

## Adding a capability here

Biochem is the reference pattern. See root `CONTRIBUTING.md` for the full guide.

```python
from ...core.capability import capability

@capability(
    domain="biochem",
    summary="Return all database aliases for a ModelSEED compound ID.",
    tags=("biochem", "lookup", "readonly"),
    visibility="public",
)
def get_compound_aliases(self, compound_id: str) -> list[str]:
    """Return all database aliases for a ModelSEED compound ID."""
    ...
```

After adding, run `kbu cap list --domain biochem` to confirm registration.
