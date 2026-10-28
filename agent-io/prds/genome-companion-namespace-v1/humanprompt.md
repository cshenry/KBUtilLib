# The genome companion namespace — what this is, in one page

**Redesigned at your direction.** The namespace is now an **annotation and occurrence companion** to
the clearinghouse, and it duplicates nothing. Two changes to the clearinghouse, twelve science tables
in the companion, three read projections, and the KBDL load function adapted to feed all of it.

## The thing I found when I went looking, and it changes the pitch

**The mechanism you described already exists.** `clearinghouse_schema._CONTENT_TYPE_HEAD` is a
per-entity-type mapping whose entire purpose is "customize those tables as needed for each entity
type". On `main` it already holds `fasta_reference, taxon_id, assembly_accession, n_contigs,
total_length, is_closed` for genome; `sequence, seq_length` for protein; `sequence, seq_length,
protein_entity_hash` for gene. So this is **under-populated, not absent** — and my previous two drafts
were re-declaring every one of those columns in their own `assembly`, `genome`, `function` and
`ontology_term` tables. That duplication was only visible once I asked the question against the source
instead of against my own document.

## Why this is urgent rather than merely correct

**Measured 2026-10-27: `genome_entity`, `genome_content`, `gene_entity`, `gene_content`,
`protein_entity`, `protein_content` and every `*_result` for those types are all ZERO ROWS.** Only
`function` has data (47,341). The clearinghouse is append-only, fill-only, no production delete — so
a column we add now is free, and a column we add after the first genome load is a corpus-wide
backfill. **This is one operator command away from being permanent.** `function_content` and
`ontology_term_content` are deliberately untouched, because they are the only place a change costs
anything.

## The one load-bearing idea

**A clearinghouse gene entity is a *sequence*, not a located feature.** It is keyed on the content
hash, so the same sequence in a thousand genomes is **one row by design** — which means location is
something the clearinghouse structurally *cannot* hold. The companion exists for exactly that gap:
the occurrence. Which sequence, in which genome, on which contig, at which coordinates, called by
which tool.

And the same logic settles annotation, which is the biggest change: **a BAKTA call on a protein
sequence is a fact about the sequence**, so it goes in `protein_result` — one row per tool, however
many genomes carry that protein. `fitness.function_annotation` holds **1,118,422 rows for 48
genomes**; the per-feature shape multiplies one fact by how often a protein recurs, which at 2M
genomes is roughly the difference between 10⁸ and 10¹⁰ rows. The companion **joins** to annotations
and never copies them — and `published_gene_annotation` reconstitutes the fitness-DB shape by join, so
you get the convenient view without the storage.

## What the companion actually holds

Only four kinds of thing, and if a future table fits none of them it probably belongs in the
clearinghouse:

- **occurrence** — `contig`, `genome_feature` (the core table), `feature_qualifier`
- **genome-scoped provenance** — `genome_build`, `genome_build_provenance`, `genome_caller_run`
- **per-occurrence judgement** — `feature_consensus`, `feature_curation`
- **experiment** — `condition`, `essentiality`, `experimental_fitness`, `propagated_fitness`

Plus `load_request`, `load_ledger`, and two append-only declaration logs. **No mutable column
anywhere.**

## Your two other instructions, taken

**Selective loading.** A load is now a **request** that names genomes, not a snapshot of a store.
`load_request.selector_json` records what was asked for, so a request stays reproducible after the
corpus moves. A genome is simply absent until requested.

**Multiple instances.** The namespace is a **parameter**, defaulting to `genome_companion`. Nothing
hardcodes it. No ACL, quota or cross-instance query is designed — you said not to worry about that
yet. What makes it work is that the clearinghouse stays **single and shared**: companion rows
reference content hashes, which are globally valid, so two instances describing one genome agree about
what it is without coordinating.

## Two things to push back on

**Q3 — I split your example differently from how you phrased it.** You named checkm scores as content
metadata. I put the *intrinsic* metrics in `genome_content` (`gc_content`, `contig_n50`,
`max_contig_length`, `min_contig_length`, `n_ambiguous_bases`, `assembly_level`) and the *assessed*
ones in `genome_result` as a declared `genome_quality@1` type — because CheckM2 1.0 and 1.1
legitimately disagree, and `completeness` is meaningless without `completeness_model_used`. The cost
is real: the payload is a JSON string, so "completeness > 95" is an extraction rather than a column
scan. That is why `published_genome` flattens it back into columns, and why the acceptance task
**measures** the query cost and reports it. **It is free to reverse before the first load and
expensive after** — the alternative is five columns on `genome_content` and deleting the result type.

**Q9 — this design is strictly weaker in one place, and I took the regression deliberately.** The
companion keys genomes on `genome_entity_hash`, which *is* the assembly hash — so two biologically
distinct genomes with identical assemblies collapse to **one** entity. Your `SyringaeB728a` /
`SyringaeB728a_mexBdelta` pair is exactly that case. I took it because a surrogate genome key
re-introduces a genome table, which re-introduces the duplication this whole revision removes — and
because KBDL's registry dedupes that shape anyway, so the previous draft's support for it was
theoretical too. The escape hatch is specified in Q9 (a small `genome_instance` table), and **I would
support building it the first time a real mutant/parent pair needs to load.**

## Status, honestly

**19 judgement calls plus a terminal: 6 IRREVERSIBLE, 13 MEDIUM.** Taskplan re-derived and validated
at 11 tasks across 8 phases. Read Q1–Q9 first; those are the irreversible ones plus the two above.

**This revision has NOT been re-attacked.** Confront round 1's eighteen stalls were folded into the
*previous* design, and several are now obsolete rather than fixed — the Revision Log says which. One
confront round of the three-round budget remains, and running it against this scheme is the obvious
next step before anyone builds.
