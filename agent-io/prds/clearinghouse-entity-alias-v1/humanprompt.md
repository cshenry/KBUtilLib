# clearinghouse-entity-alias-v1 -- the identifier-carriage facility

A genome is known by eight different names, and the clearinghouse can currently
record none of them.

This PRD adds **FIVE tables, one per entity type** -- `genome_alias`,
`protein_alias`, `gene_alias`, `function_alias`, `ontology_term_alias` -- plus an
`all_alias` view, and the accessors, validators, write boundary, read surface, CLI
verbs and health report that make them usable. `alias` becomes the fourth KIND
alongside `entity`, `content` and `result`, taking the clearinghouse from sixteen
tables to twenty-one. They carry a closed `alias_source` vocabulary and answer the
question the whole system cannot answer today: **"I have a KBDL object id -- what
is this?"**

## Why now, and not later

Every genome table in the clearinghouse is **0 rows** (measured live 2026-10-25
17:36 CDT). The adapters that would write aliases -- `clearinghouse-backfill-v1`'s
`arc` and `kbdl_hash` -- are at `ready` and unbuilt. So the writers are cheapest
to specify today and a later retrofit means a backfill pass over whatever has
loaded by then. This is item 3 in the sequencing list of the integration-vision
document, ordered by when the opportunity disappears rather than by importance.

## What Chris decided, and what this PRD does with it

Chris, 2026-10-25, decision (3): *a genome's multiple identifiers must be saved
**together** in the clearinghouse*, and the mechanism was the design session's to
produce. That session produced it to PRD-ready precision as section 6 of
`2026-10-25-integration-vision-kbdl-clearinghouse-companions-arc.md` (D3.1-D3.8,
blast radii and confidences already assigned).

**This PRD transcribes that design. It does not re-decide it.** Where it goes
beyond section 6 it says so explicitly, in Open Questions, and names what it
derived rather than quietly presenting a derivation as a settled call. There are
three such derivations and they are all mechanical consequences of where the
table has to live in code: the write verb cannot be `register()`, the manifest
needs a fourth block kind, and the health report must NOT inherit the
parameter-set registry's fail-the-build posture.

## Round 1 absorbed your reversal

**You were right and my precedent was wrong.** You asked for the alias table to be
broken up by entity type; it now is, as five per-type tables with `alias` as a
fourth kind. I had reasoned from `parameter_set`, but that table is keyed by a hash
and holds one row per distinct parameter set -- it is not entity-keyed, which is
exactly why it is the lone non-entity-typed table. An alias table IS entity-keyed,
so `<type>_entity` was always the right analogue.

I also checked the one thing that might have saved a shared table, and it does not:
four of the ten alias sources (`arc_record`, `arc_analysis`, `kbase_ws_ref`,
`ncbi_taxon`) are entity-type-agnostic, so partitioning on `alias_source` would
still mix genome and protein rows in those four partitions. Your performance
argument holds even with the partitioning in place.

**The split made three things simpler, not harder:** the bespoke alias-only constant
and accessors are deleted (`table_name`/`table_columns` serve them now), the
manifest block validates through the standard accessor with no special case, and the
orphan report becomes a straight five-pair join. It also **collapsed Q3's original
argument** -- `register()` can now name the table -- so that entry is rewritten to
stand on the two narrower reasons that survive, with the collapse recorded rather
than hidden. One new entry, **Q1a**, decides which alias sources are legal for which
entity type; that only became an answerable question once the tables were split.

## Two other things to look at

**1. Q2a is the ONE place this PRD goes beyond your settled design.** Section 6
specifies six columns and says nothing about retraction. This PRD has **seven**: it
adds `observation` (`present` | `withdrawn`), so a row is an observation rather than
a standing fact and a withdrawn accession can be expressed without ever updating a
row. I did not originate the argument -- I flagged the gap, routed it to the session
designing the KBDL obsolete-ID service, and it came back with a case I found
persuasive: a `retracted_at` column would have to be UPDATED (which append-only
forbids) and would make withdrawal terminal, when NCBI genuinely un-suppresses
accessions. I adopted it because the cheapest moment to add a column is before any
row exists, and every clearinghouse genome table is 0 rows today. **If you disagree,
Q2a names exactly what to delete, and the cost of being wrong either way is low** --
`null` reads naturally as `present`, so unlike most of this schema it is reversible
in both directions.

**2. Q2, the missing `confidence`/`asserted_by` column, is still the highest-risk
entry.** MEDIUM blast radius at LOW confidence -- the only such combination here --
and the design session recorded it as the one it was least sure of. Cheapest to
reverse now.

## One thing that is missing, and you should know before you read

**The external research never landed.** It was dispatched three times: the first
died on the expired codex credential, the second spent its whole budget researching
and was killed before writing a word (my dispatch prompt's fault -- it never said to
write early), and a third is running. So this draft is informed by the internal
corpus only. Four judgement calls are internally-reasoned rather than
externally-corroborated, and each says so: Q2, Q2a, Q5 and Q11.2. **The confront
step also did not run** -- the cross-family rail was down on the dead credential, and
the invariant forbids a Claude adversary attacking a Claude-written PRD. Codex is
working again now, so a confront round is the obvious first thing to do against this
document.
