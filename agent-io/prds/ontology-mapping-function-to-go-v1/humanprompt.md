# OntologyMapping: `function_to_go` for KBUtilLib

**The ask (verbatim):** Create a new API library called "OntologyMapping" in KBUtilLib and
integrate the code here:
`/scratch/vsetlur/function-ontology-mapping-program/studies/01_function_to_go/pipelines/type_4`
to create a `function_to_go` function.

**In-session steer (Chris, 2026-10-18):** "if there are already capabilities, build on those."

## What this builds

A capability domain `ontology_mapping` on KBUtilLib's existing `@capability` registry, surfaced
as `KBUtilLib().ontology_mapping` and reachable from the library, CLI, MCP and HTTP API with no
new transport code.

```python
k = KBUtilLib()
k.ontology_mapping.function_to_go(
    ["ATP synthase subunit alpha", "Arylsulfatase B precursor (EC 3.1.6.12)"]
)
```

Two routes, tried in order:

1. **`ec2go`** — a description carrying an EC number resolves through the GO Consortium's
   curated EC→GO mapping. Deterministic, no similarity scoring.
2. **BM25** — everything else is ranked against GO `name` / `synonyms` / `definition` by a
   fit-free character-n-gram BM25 scorer (the research program's Type 1 method, ported).

By default the routes combine by **precedence**: curated `ec2go` terms take the top ranks and
the lexical route fills the rest. Reciprocal rank fusion (the Type 4 algorithm, k=60) ships as an
opt-in `combine="rrf"` mode, because the external scan supplied arithmetic showing equal-weight
RRF can demote a correct leader below a wrong one on exactly our route shape. Every candidate
says which route produced it; every result names the GO release and corpus SHA-256 it came from.

**`fullprompt.md` is authoritative wherever these two documents disagree.**

## Two things that changed during research, and why

**The `type_4` code cannot answer `function_to_go`.** It is a *fusion* stage: it consumes two
already-complete parent runs and fuses their stored rankings after verifying a provenance
contract. It computes no scores, and `run()` refuses anything outside a manifest of ≤1,000
pre-selected frozen test queries — its own README says full production is "deliberately blocked".
Wrapped as-is, `function_to_go` could only answer questions it already had the answers to. The
scoring logic lives one level up in Type 1, which is fit-free and portable. So: port Type 1's
BM25 and Type 4's ~20-line RRF kernel; depend on neither package at runtime.

**`ontomap` is not an option.** `OntomapUtils` already exists in KBUtilLib and wraps Vibhav's
`ontomap` package — but it maps to ModelSEED *reactions*, not GO terms, and `ontomap` imports in
**none** of the 20 venvs on poplar, including all three that import `kbutillib`. PRD
`gaa-ontomap-council-mapping` was archived in July for exactly this reason ("blocked on Vibhav
non-public assets"). This build adds **no new dependency at all** — stdlib over staged data files
— specifically so it cannot fail that way. `OntomapUtils` is left untouched.

## What we are reusing rather than rebuilding

- `OntologyDictionary` — GO id→name lookups, and its missing-file-vs-zero-entry degradation
  contract, copied rather than reinvented.
- `build_go_description_map.py` — the OBO parser and its don't-commit-the-output discipline;
  a sibling `build_go_corpus.py` is added beside it rather than widening its shipped contract.
- `EcRoleResolver` — the house precedent for curated identifier mapping with no fuzzy matching.
- `Term` / `AnnotationRecord` — the existing annotation-term shape, so results compose.
- The `@capability` registry and `interfaces/api` — this *is* the "API library" machinery.

## The risk this PRD is written around

The sibling PRD `kbdl-ontology-descriptions-v1` shipped seven reviewed tasks and measured a
production effect of **zero**, because every test injected its dependency at the constructor seam
while the real adapter passed nothing. The final task here is therefore an acceptance gate that
calls through `KBUtilLib()` with **nothing injected** — a test that passes a corpus into a
constructor explicitly does not satisfy it.

## What this PRD does not claim

No accuracy number, for any route. None was measurable from anything readable, and the research
program's own benchmark is self-described as "an operational benchmark, not a final production
claim". Task 6 ships an evaluation harness instead — per-route and fused top-1, MRR, recall@k,
and `is_a`-closure hierarchy metrics against the existing frozen gold set. If a go/no-go quality
threshold is wanted before this merges, it has to be set by Chris.

## Open items worth your eye

- **Q4 (precedence vs RRF)** — high confidence that equal-weight RRF should not be the default;
  low confidence that precedence is the *best* alternative. A tuned combination may beat it, and
  only the evaluation harness can say.
- **Q3 (is `ec2go` worth it)** rests on EC coverage over our real descriptions, which is
  unmeasured. Task 6's per-route reporting settles it.
- **Q11 (synonym-scope multipliers)** — that scope should modulate weight is well supported; the
  four numbers (exact 1.0 / narrow 0.75 / related 0.5 / broad 0.25) are invented.
- **Q14 (KO→GO)** is deferred on a **licensing** question, not a technical one, and that question
  is yours: the real KO crosswalk is KEGG-hosted under terms separate from GO's CC BY 4.0.

A cross-family adversary (codex, Maestro `task-16d54bf7`) attacked the draft before commit and
returned 12 binding stalls — all accepted, all folded, and recorded as a numbered
`## Acceptance Criteria` list in `fullprompt.md`. Three of them caught real contradictions in my
own draft: the two documents disagreed on the default combine mode, the result schema demanded a
GO release and corpus hash that an `ec2go`-only result cannot supply, and `top1_margin` was
presented as a confidence signal across scores the same document called incomparable. It also
read the Type 1 source I had not, and corrected a factual claim in Q11.
