# Reaction-similarity module — research findings

Concise record of the analysis that informed `MSReactionSimilarityUtils`
(`kbu.rxnsim`). The full, provenance-tracked study (data intake, EDA, metric
validation, clustering robustness, SMARTS-entry validation, figures) lives in a
separate Research OS workspace; this note captures the conclusions that shaped
the code.

## Source request

Use the reaction-similarity data in the KBase DataLake to (a) expand
function-annotation hypotheses to chemically similar reactions for the inner-loop
annotation-refinement pipeline, and (b) cluster reaction-mapping output by how
chemically distinct the reactions are. Enter by **reaction id or SMARTS**;
id/SMARTS → similar reactions; ids → distance matrix → clustering.

## What the data is

- **`kbase_msd_biochemistry.reaction_similarity`** is a **full** pairwise
  similarity matrix: ~6.7×10⁸ stored pairs, similarity range ≈ −0.56 … 1.0
  (the stored RDKit reaction-fingerprint metric). Read live from BERDL.
- Supporting tables: `reaction` (56,012), `reagent` (262,517 — stoichiometry),
  `molecule` (45,708, inline `smiles`), `structure`. Foreign-key integrity is
  clean; ~78% of reactions reconstruct to a reaction-SMILES from reagents +
  molecule SMILES (the rest contain R-groups / `[protein]-` / polymers).

## Key decisions

1. **Two separate similarity regimes, never mixed.** Stored BERDL similarity
   (`method/source="berdl"`) is authoritative — expansion, pairwise lookup,
   distance matrices, clustering. A client-side DRFP recompute (`"drfp"`) serves
   reactions/SMARTS absent from the table (novel queries).
2. **A naive client-side recompute does NOT reproduce the stored score**
   (best Pearson ≈ 0.31 on real-chemistry pairs across RDKit structural,
   difference, and DRFP fingerprints). The stored metric is transformation-centric
   (≈34% of high-similarity pairs are null-transformation transport reactions,
   reproduced only by a difference fingerprint with empty-handling). So the two
   scales are kept separate; the exact stored fingerprint method is an open
   question for the data owner.
3. **SMARTS entry** (no reaction id, so the table cannot be looked up): scoring a
   query by recomputed DRFP against a candidate set is chemically meaningful —
   top-10 recompute neighbours share the query's enzyme class at a median 3.7×
   lift over base rate — though it does not reproduce the stored near-neighbour
   ranking (recall@10 ≈ 0.24). Hence `find_similar` routes **id → stored**,
   **SMARTS → recompute over an explicit candidate set**.
4. **Clustering**: a transformation-centric fingerprint groups reactions by
   enzyme chemistry far better than a molecule-centric one (enzyme-class ARI
   0.31–0.35 vs 0.06–0.15); agglomerative (average linkage) and Butina agree
   (ARI 0.92) and are bootstrap-stable (ARI 0.98). Defaults: agglomerative,
   Butina alternative, HDBSCAN opt-in; transport/identity reactions segregated.
   With live BERDL the stored similarity serves clustering directly on a
   consistent metric (`source="berdl"`); DRFP recompute is the fallback.

## BERDL access engineering (reflected in the code)

- The token is resolved the canonical KBase way: the standard `kbase`
  SharedEnvUtils namespace, falling back to the **`KB_AUTH_TOKEN`** environment
  variable that the KBase runtime sets — exactly as the KBase base client and
  catalog client do. No hardcoded token path. The `kb_berdl_utils` response
  parser was also updated to the API's current `result`/`pagination` shape.
- The API caps `limit` at 1000 (paginate). Equality predicates are index-served
  and fast; un-narrowed / double `IN` over the 6.7×10⁸-row table is not, so the
  module uses per-row `reaction_1 = 'id' AND reaction_2 IN (set)` for distance
  matrices and per-id equality for reagent lookups.
- The shared engine intermittently times out; queries use a 60 s timeout with
  retry.

## Open questions for the data owner / requesting PI

1. Exact fingerprint method/parameters behind the stored `similarity` (would let
   a recompute match the stored scale, unifying the id and SMARTS entries).
2. Transport/identity handling preference in clustering.
3. Facade attribute name (`kbu.rxnsim`) sign-off; PRD-first?
