# MSReactionSimilarityUtils (`kbu.rxnsim`)

Retrieve chemically similar reactions and cluster reaction sets by chemical
distinctness, backed by the reaction-similarity table in the BERDL data lake
(`kbase_msd_biochemistry`). Built for the inner-loop annotation-refinement
pipeline: expand hypotheses from the function-annotation tools to chemically
similar reactions, and group reaction-mapping output by how distinct the
chemistry is.

## Authentication

Uses the standard KBase token, resolved the canonical way — the `KB_AUTH_TOKEN`
environment variable set by the KBase runtime (and used by the KBase base
client), or the `kbase` token namespace (`~/.kbase/token`) locally. No token
path is hardcoded.

```python
from kbutillib import KBUtilLib
kbu = KBUtilLib()          # picks up KB_AUTH_TOKEN from the environment
rs = kbu.rxnsim
```

## Quick start

```python
# 1. Enter with a reaction ID OR a SMARTS -> similar reactions
rs.find_similar("seed.reaction:rxn00001", top_k=10)            # ID  -> stored BERDL similarity
rs.find_similar("O.<...>>><...>",                              # SMARTS -> recompute over a
                candidate_ids=my_reaction_ids, top_k=10)      #          candidate set

# 2. Reaction ID -> stored near-neighbours (authoritative metric)
rs.similar_reactions("seed.reaction:rxn00001", min_similarity=0.9, top_k=10)

# 3. Reaction ID -> reaction SMILES (reconstructed from reagents)
rs.resolve_to_smarts("seed.reaction:rxn00001")

# 4. Reaction IDs -> distance matrix -> clustering by chemical distinctness
D, ids, info = rs.distance_matrix(my_reaction_ids, source="berdl")
result = rs.cluster(my_reaction_ids, source="berdl",
                    algorithm="agglomerative", distance_threshold=0.4)
result["clusters"]          # {label: [reaction_ids]}
result["representatives"]   # {label: medoid reaction_id}
result["transport"]         # transport/identity reactions, segregated
```

## Design (grounded in the data)

The live `reaction_similarity` table is a **full** pairwise similarity matrix
(~6.7×10⁸ pairs, full similarity range). Two regimes are exposed, **never mixed
on one scale**:

| regime | `method`/`source` | use |
|---|---|---|
| **Stored similarity** (default) | `"berdl"` | near-neighbour expansion, pairwise lookup, distance matrix, clustering — the authoritative metric |
| **Client-side recompute** | `"drfp"` | reactions/SMARTS absent from the table (novel queries); a DRFP reaction-difference fingerprint on its own scale |

`distance = 1 − clip(similarity, 0, 1)`; transport/identity reactions (no net
transformation) are detected via `reaction.is_transport` and segregated rather
than clustered, so "chemical distinctness" reflects real chemistry.

**Entry by ID vs SMARTS.** A reaction id is looked up in the stored table
(authoritative). A SMARTS/SMILES has no id, so it is scored by a recomputed
fingerprint against an explicit `candidate_ids` set (e.g. the reaction-mapping
output): chemically meaningful (top-10 neighbours enrich ~3.7× for the query's
enzyme class) but on the recompute scale, reported as `method="drfp"`.

## Public API

- `find_similar(query, *, candidate_ids=None, top_k=25, min_similarity=0.0)` —
  unified entry; routes ID→stored, SMARTS→recompute
- `similar_reactions(reaction_id, *, min_similarity=0.0, top_k=50, exclude_self=True, both_directions=True)`
- `similar_to_smarts(query, candidate_ids, *, top_k=25)` — recompute ranking
- `expand_reactions(reaction_ids, *, min_similarity=0.7, top_k_per=5)` — inner-loop batch expansion
- `similarity(a, b, *, method="berdl")` — `None` if a `berdl` pair is absent
- `resolve_to_smarts(reaction_id_or_smarts)` / `get_reaction_smiles(reaction_id)`
- `reaction_fingerprint(reaction_id_or_smarts, *, method="drfp")`
- `distance_matrix(reaction_ids, *, source="berdl", fill_missing=1.0)` → `(D, ids, info)`
- `cluster(reaction_ids, *, source="berdl", algorithm="agglomerative"|"butina"|"hdbscan", distance_threshold=0.4, min_cluster_size=5, segregate_transport=True)`

## Operation notes

- **Remote-first.** All data is read live from BERDL; no local checkout. The
  shared BERDL engine can be slow/intermittent, so queries use a timeout with
  retry. Equality predicates (`reaction_1 = 'id'`) are fast; the module avoids
  un-narrowed `IN`/double-`IN` scans over the 6.7×10⁸-row table.
- **Dependencies.** `numpy` (matrices), `scikit-learn`/`scipy` (clustering),
  `rdkit`+`drfp` (the `drfp` recompute path), `hdbscan` (optional). All imported
  lazily — the module imports fine without them; a method that needs a missing
  extra raises a clear `ImportError`. Install with
  `pip install KBUtilLib[reaction_similarity]`.
- **Composition.** `MSReactionSimilarityUtilsImpl(env, biochem=None, berdl=None)`
  follows the facade pattern; `kbu.rxnsim` wires it to the shared environment's
  BERDL client.

## Open items for the data owner / requesting PI

1. Exact fingerprint method/parameters behind the stored `similarity` (a naive
   client-side recompute does not reproduce it — see `agent-io/research/`), so
   stored and recomputed scales are kept separate.
2. Preferred handling of transport/identity reactions in clustering.
3. Facade attribute name sign-off (`kbu.rxnsim`).
