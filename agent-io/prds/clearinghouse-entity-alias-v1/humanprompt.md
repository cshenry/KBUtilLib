# clearinghouse-entity-alias-v1 -- the identifier-carriage facility

A genome is known by eight different names, and the clearinghouse can currently
record none of them.

This PRD adds ONE table, `kbaseincubator.clearinghouse.entity_alias`, and the
accessors, validators, write boundary, read surface, CLI verbs and health report
that make it usable. It is generic over entity type, carries a closed
`alias_source` vocabulary, and answers the question the whole system cannot
answer today: **"I have a KBDL object id -- what is this?"**

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

## Two things to look at first

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
