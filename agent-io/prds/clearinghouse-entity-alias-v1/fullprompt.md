# clearinghouse-entity-alias-v1

The identifier-carriage facility for the BERDL clearinghouse: five per-entity-type
`<type>_alias` tables and an `all_alias` view, their closed `alias_source`
vocabulary, the write boundary, the read surface including a reverse lookup, and a
health report.

## Research Report

### Important findings

**1. The precedent for a non-entity-typed clearinghouse table is already in code,
built, and live in the lake.** `clearinghouse_schema.py` declares
`PARAMETER_SET_TABLE = "parameter_set"` as a module constant precisely BECAUSE it
is not entity-typed, with a docstring explaining that `table_name()` composes
`f"{entity_type}_{kind}"` and validates both halves, so a non-entity-typed table
cannot go through it. It is deliberately absent from `ENTITY_TYPES` and from all
three `all_<kind>` union views. A live read of the lake on 2026-10-25 17:36 CDT
returned 19 objects in `kbaseincubator.clearinghouse` -- the fifteen per-type
tables, the three union views, and `parameter_set`.

**ROUND 1 CORRECTION, and it inverts what this finding was used for.** The first
draft read `parameter_set` as the precedent for the alias table and concluded it
should likewise be one non-entity-typed table. That was wrong: `parameter_set` is
keyed by `parameter_set_hash`, not by an entity, and holds one row per distinct
parameter set ever used -- which is exactly WHY it is the single non-entity-typed
table. An alias table is entity-KEYED, so the applicable precedent is the
`<type>_entity` family and the correct shape is **five per-type tables plus a
fourth union view**. `parameter_set` remains the only exception, and this finding
now stands for the opposite conclusion: the alias tables are ORDINARY
entity-keyed tables and need no special machinery at all.

**2. The write verb cannot be `register()`, and that is a structural fact rather
than a preference.** `ClearinghouseCapability.register(entity_type, rows, *, kind)`
selects its table through `table_name(entity_type, kind)`, which raises
`ValueError` on anything not in `ENTITY_TYPES` x `_KINDS`. `parameter_set` rows are
therefore NOT written by `register()`; they are written by the private
`_write_parameter_set_registry()`, which calls `capability.load()` directly with
`{"name": PARAMETER_SET_TABLE, "mode": "append"}` and projects rows through
`parameter_set_column_names()`. The alias table needs its own public verb for
exactly the same reason, and `_write_parameter_set_registry` is the shape to
follow.

**3. The read surface's filter semantics live on `results()`, not on the
`sources()` verb.** The integration-vision document says "`sources`-identical
semantics". The actual contract is the `sources=` / `result_types=` /
`parameter_set_hashes=` PARAMETERS of `ClearinghouseCapability.results()` and
`.current_state()`, whose docstring states it exactly: "`None` applies no filter;
`[]` is a request to match nothing (the builder emits `WHERE 1 = 0`)". The helper
`_in_literal()` implements the same rule for literal-valued filters -- an empty
list yields `1 = 0` rather than an invalid `IN ()`. `aliases()` must follow THAT,
and the PRD states it that way so a developer reading "like `sources()`" does not
implement the wrong thing.

**4. The health report must NOT inherit the parameter-set registry's posture, and
copying the obvious precedent would be a defect.** `parameter_set_orphans()`
documents its findings as "FAILURES and never warnings", the CLI `health` verb
carries them in `data.parameter_set_integrity`, and `_exit_on_integrity_failure()`
raises `SystemExit(1)` whenever `report["failures"]` is non-empty. Design decision
D3.5 is the OPPOSITE call for aliases: an alias row whose `entity_hash` has no
entity row is LEGAL, because an identifier legitimately arrives before the entity
is hashed. So the alias integrity report must be a report that does not fail the
verb -- and the single most likely implementation error in this whole PRD is a
developer copying `parameter_set_orphans` wholesale, including the exit path.

**5. `ontology_term_content.namespace` is live, which is what killed the obvious
column name.** The content schema for `ontology_term` is
`(entity_hash, term_id, name, namespace, definition, standardizer_version,
observed_at, ingest_batch_id)` -- verified against
`_CONTENT_TYPE_HEAD["ontology_term"]` on `main` and against the live lake. There
`namespace` means an *ontology* namespace. Naming the alias vocabulary column
`namespace` would put two columns of that name with different meanings in one
schema, which is how a wrong join gets written by someone reading a column list.

**6. The two live precedents disagree with each other, so the column names had to
be chosen rather than inherited.** Read live at 17:36 CDT:
`kbaseincubator.inner_loop.v2_genome_aliases` is
`(alias_id, genome_id, alias_source, created_at)`;
`kbaseincubator.fitness.gene_alias` is `(gene_id, alias, type)`. The design follows
`v2_genome_aliases` on the two name choices, and follows the *clearinghouse's own*
convention on time (`observed_at` + `ingest_batch_id`) rather than either
precedent's `created_at`-or-nothing -- `gene_alias` has no timestamp at all, which
is exactly the omission that makes "when did we learn this accession" unanswerable
there.

**CORRECTION, and it weakens this evidence rather than strengthening it:**
`fitness.gene_alias` holds **ZERO rows** (measured by session `368c658e`,
2026-10-25, reported to this session before commit). So the in-namespace precedent
for an alias table is a *proposed* pattern, not a validated one, and the column
names above are chosen on the strength of `v2_genome_aliases` plus the
clearinghouse's own conventions -- **not** on `gene_alias` having worked in
production. The original draft of this section leaned on it slightly harder than
the evidence supports. What `gene_alias` still legitimately shows is the SHAPE a
reasonable person reached for and the timestamp omission to avoid; what it cannot
show is that the shape survives use.

**7. KBDL's `genome_id` is literally `fasta_object_id[:16]`, which removes a
vocabulary entry rather than adding one.** `build_genome.py:1549` is
`genome_id=content_hash[:_INTERNAL_ID_HASH_CHARS]` with
`_INTERNAL_ID_HASH_CHARS = 16` at `:484`. It is a derivable prefix, not an
independent identifier, so there is no `kbdl_genome_id` alias source. Storing a
derivable prefix invites a writer to treat it as independent.

**8. The manifest grammar needs a fourth block kind, and the parameter-set
precedent does NOT cover it.** `clearinghouse_manifest.py` declares
`KINDS = ("entity", "content", "result")`, and `_plan_content` / `_plan_result`
validate declared columns against `schema.column_names(etype, kind)` -- the same
definition the DDL is built from. The parameter set is NOT a fourth kind: it rides
inside the `[source.result]` block via `_PARAMETER_SET_KEYS` and is validated by
`_plan_parameter_set`. Aliases cannot ride along that way, because a source emits
MANY alias rows per entity (one per `alias_source`) rather than one extra column
value, so the cardinality is different. This PRD therefore adds a real fourth
kind. See Q4.

**9. The null-pointer hazard D3.3 cites is real and is documented in the
manifest.** `@prefix(text, col)` and `@concat(...)` are null-preserving
derivations, and the module comment says why in so many words: `"kbdl:obj:" + None`
would write the literal `"kbdl:obj:None"`, "a reference to an object that does not
exist, indistinguishable in the lake from a real one." An alias table makes that
failure mode multi-valued, which is why `alias_id` must REFUSE an empty value
rather than skip the row.

**10. `bootstrap()` is the sanctioned way to add the five alias tables to a
namespace that already holds sixteen, and it is already safe for this.** It is an
idempotent create-or-verify: it creates tables that do not exist and verifies the
partition spec of those that do, raising `BootstrapPartitionSpecMismatchError`
rather than attempting an unsupported live partition-spec change, and
`BootstrapIndeterminateStateError` rather than treating an ambiguous existence
probe as "absent". It explicitly **cannot add a column to an existing table** --
which does not matter here, because all five `<type>_alias` tables are wholly new.

**11. `_assert_lake5_schema` does not need changing, by luck rather than by
design.** It iterates `table_configs()` and skips any name that is neither
`PARAMETER_SET_TABLE` nor `*_result`. The five new `<type>_alias` configs all fall
through that filter untouched -- note that `*_alias` does not match `*_result`, so
adding a fourth kind does not disturb it. This is worth stating because the
preflight *looks* like something that should learn about every new table, and
teaching it about the alias tables would make every existing namespace fail
*result* ingest until an operator ran OP-C5. See Gotchas.

### Links to full research output

- [External prior-art scan](research/external-prior-art-scan.md) -- **LANDED, on the
  fifth dispatch.** Surveys Ensembl `xref`/`object_xref`, UniProt idmapping and its
  ID Mapping service, NCBI `gene2accession` and `assembly_summary`, GO/OBO
  obsolescence relations, GA4GH refget, BioMart and InterMine, identifiers.org,
  Bioregistry, SSSOM and a commercial MDM crosswalk, plus the `curies`,
  `bioregistry`, `prefixmaps` and `pyobo` packages. **It found one genuine defect**
  -- `ncbi_taxon` is a membership relation and not an invertible alias (Q10, and the
  `INVERTIBLE_ALIAS_SOURCES` tuple exists because of it) -- **independently
  corroborated Q2a** without having been shown it, **reshaped Q2** (the missing
  thing is method and actor, not a confidence float), **lowered Q6's confidence**
  as the one property with no validating precedent, **closed Q11.2**, and
  **declined to invent a war story** where it had no evidence, saying so in as many
  words about Ensembl's `info_type` and about partitioning benchmarks. Its own
  `WHAT I DID NOT SEARCH` section is unusually candid and worth reading.

  Getting it took five attempts, and the failures are recorded because the lesson
  outlived them: `task-c06a5d7a` (codex) died in 35s on the expired credential;
  `task-c9a6e008` (claude) spent its whole 1200s budget researching -- 85+ tool
  calls, reaching exactly the right sources -- and was killed having committed
  nothing, which was **a defect in my dispatch prompt rather than in the research**,
  because it never told the worker to write early; `task-8a36f389` failed to launch
  on a Maestro-side error; and `task-e34c7004` succeeded on the codex rail once
  Chris restored the credential, committing **five times incrementally** under an
  explicit write-first instruction.

### Quality report

**What was searched, and with what result.** The internal corpus was read directly
and in full for the parts that bear on the design: `clearinghouse_schema.py` (all
1,014 lines of structure -- constants, every column tuple, `table_name`,
`table_columns`, `parameter_set_columns`, `table_configs`, `union_view_sql`,
`encode_entity_hash`, both bootstrap error classes, `bootstrap`);
`clearinghouse_capability.py` (`register`, `_write_parameter_set_registry`,
`results`, `sources`, `parameter_sets`, `parameter_set_orphans`,
`_assert_lake5_schema`, `_live_columns`, `_in_literal`,
`_registry_representative_sql`); `clearinghouse_manifest.py` (the derivation
vocabulary and its null semantics, `KINDS`, `_plan_result`, `_plan_parameter_set`);
`interfaces/cli/clearinghouse.py` (the envelope contract, `_build_health`,
`_exit_on_integrity_failure`, `_exit_on_load_failure`); `standardizers.py`
(structure and `STANDARDIZER_VERSION = "1.1"`); `tests/berdl/test_clearinghouse_schema.py`
(`TestParameterSetRegistry` in full, as the test shape to mirror); and
`agent-io/docs/clearinghouse-schema-operator-runbook.md` (heading structure and the
OP-C1..OP-C4 numbering). All reads were taken from `KBUtilLib` **`main` at
`b63eb6a2`** via `git show`, not from a working tree.

**Where coverage is thin, and it is thin in three specific places.**

1. **No live lake read was performed by THIS session.** Every live fact quoted
   above (the 19 objects, the two precedent schemas, `ontology_term_content`'s
   columns, the 0-row genome tables) is inherited from session `f84749dc`'s
   read-only pod read at 2026-10-25 17:36 CDT and from session `590fd2c5`'s
   17:07 CDT probe, as recorded in the integration-vision document. They are
   roughly six hours old as of writing. They are reported as **records**, not as
   this session's own measurements. Nothing in the design turns on them having not
   changed since -- the tables being empty is the only one that would matter, and
   it would make the design *more* safely applicable, not less.

2. **The `arc` and `kbdl_hash` adapters were not read in detail.** D3.7 asserts the
   `arc` adapter "already has all four in hand per DB.11", and that assertion was
   taken from the integration-vision document rather than re-verified against
   `clearinghouse_backfill.py` and the TOML presets. This PRD does not edit those
   presets (see Out of Scope), so the assertion does not gate anything here -- but
   a future session wiring the writers must check it rather than inherit it.

3. **No performance or row-distribution measurement exists for the partition
   choice.** Nobody has measured how `alias_source` values will distribute, because
   there are no rows. The design assigns that MEDIUM blast radius and MEDIUM
   confidence honestly, and this PRD does not improve on it. The external scan was
   asked specifically for war stories on partitioning a crosswalk table by its
   source vocabulary.

**What was NOT searched and why.** Ahmed's Protein Clearinghouse (explicitly out of
scope per Chris 2026-10-07 and lake-5 D5.1). The GAA / `V2_Assemblies` store beyond
the `fasta_ref` column-name observation already recorded (it belongs to
genome-identity-v1's Q12.2). KBase workspace API semantics for `kbase_ws_ref`
(accepted as an opaque string by design, so its grammar is deliberately
unvalidated). And `kbdl_service`'s own SQLite clearinghouse, which lake-5 D5.18
declines to retire and which this table does not touch.

**THE EXTERNAL HALF OF THE RESEARCH STEP DID LAND, on the fifth attempt, and it
changed four entries.** Q10 went from NONE to MEDIUM blast radius on a real defect
(`ncbi_taxon` is not invertible); Q2a rose to high confidence on independent
corroboration; Q2 was reshaped (method/actor, not confidence) and its
"cheap to add later" claim read down as unverified; Q6 fell from high to medium as
the only design property with no validating precedent; and Q11.2 closed. Q5 is
unchanged and still MEDIUM, because the scan looked for crosswalk partitioning
guidance and found none.

**What the scan itself did NOT do, from its own disclosure:** it read no internal
source tree by design; it accepted the GTDB 7,225/596,859 figure as requester
context without verifying it, and noted that a missing record can reflect release
or selection differences rather than retraction; it did not inspect KBase/BV-BRC
md5 canonicalization or workspace-ref version semantics, which is **exactly why
`arc_record` and `kbase_ws_ref` are listed as invertible only PROVISIONALLY**; it
performed no live multiplicity census and no Iceberg benchmark; and it installed no
packages, so its tool assessments are documented-capability judgements rather than
measured ones. It rates itself high on the schema and lifecycle distinctions and
medium on the workload and tool-fit recommendations, which matches how they are
used here.

**Confidence the design should carry: HIGH on everything except Q2, Q5 and Q6.** The
table shape, the vocabulary, the fill-only rule and the read surface are
transcriptions of a design that was itself built against live reads, and the three
derivations this PRD adds are each forced by a specific line of existing code
rather than chosen. Q2 (no confidence column) and Q5 (the partition key) are the
two places where a reasonable person could land differently, and both are called
out as such.

## Revision Log

**Round 0 -- 2026-10-26.** Initial single-pass draft.

**Round 0, same session, after two sibling design sessions replied.** Three folds,
all recorded with attribution rather than absorbed silently:
(1) **Q2a added, and it is the one thing here that goes beyond Chris's settled
design** -- an `observation` field (`present` | `withdrawn`) making a row an
observation rather than a standing fact, so retraction works without a mutable
column. Argued by session `793ad471` (KBDL obsolete-ID service); the fill-only
interaction it did not address is resolved here by splitting `observe_alias` from
`register_aliases`. The table is now seven columns and the fill-only key is a
five-tuple. Gotcha 6 is rewritten -- the hazard moved rather than vanished -- and
6a, tests 7a/10a/13a/14a are new.
(2) **Q7 refined** on the same session's evidence: a distinct `AliasAmbiguousError`
type, both hashes named, and the explicit principle that "unknown" and "contested"
must never collapse into one answer.
(3) **H1 resolved mid-authoring.** Chris chose option A and it landed as
`KBUtilLib main` `ed40f28` while this PRD was being written, unifying KBDL object
references on `obj://<id>[#<member>]`. Out of Scope is rewritten from "blocked" to
"resolved", the bare-`alias_id` choice is re-argued on CURIE-versus-URI grounds
rather than on decision-avoidance, and the resulting silently-empty-join hazard is
new Gotcha 12.
(4) **A research finding CORRECTED and weakened**: session `368c658e` measured
`fitness.gene_alias` at ZERO rows, so it is a proposed pattern and not a validated
one. Q1 now also states explicitly, for that session's benefit, that genericity is
firm and a companion `genome_alias` should converge via a view rather than be built
as a projection today. Transcribes section 6
(D3.1-D3.8) of `2026-10-25-integration-vision-kbdl-clearinghouse-companions-arc.md`
into the PRD contract shape. Adds three derivations the source design left
implicit, each flagged as a derivation in Open Questions: the dedicated write verb
(Q3), the manifest's fourth block kind (Q4), and the non-failing health report
(Q6). Carries section 6's own could-not-decide list forward verbatim as the
terminal entry, including the `fitness.assembly_hash` rule-A/rule-B question that
remains a live unknown. Folds in H3's documentation fix as task t7.

**Round 0, confront round 1 folded -- 2026-10-26.** A cross-family (codex) adversary
attacked the draft and returned EIGHT binding stall points
([stall report](research/confront-round-1-stall-report.md), Maestro
`task-a2c90f9f`). **All eight were kept; none was dropped**, because every one was a
contradiction or an unverified claim in my own text rather than a disagreement about
the design. What changed:
(1) Out of Scope excluded retraction wholesale while Q2a added it -- a flat
contradiction; the exclusion is now "mutable retraction COLUMNS, not retraction",
and Q1's stale no-retraction clause is gone.
(2) "Latest by `observed_at`" had no tie rule and no clock policy; the capability now
mints `observed_at`/`ingest_batch_id` per call, REFUSES caller-supplied values, and
every reduction orders by `observed_at DESC, ingest_batch_id DESC, observation DESC`.
(3) `aliases()` claimed to return event history while specifying a five-tuple
reduction, which cannot both hold; it now returns the latest occurrence of each
STATE, and says what it cannot show.
(4) `current_only=True` would have returned a withdrawn row from something called the
live set; it now excludes withdrawn winners.
(5) `resolve()`'s collision semantics admitted three incompatible readings; the
three-step reduction is now explicit, and a corrected collision resolves rather than
raising forever (test 14b).
(6) **"Parameter-bound, never inlined" was unimplementable** -- `_run` has three
binding regimes and only in-pod Trino binds; Spark and off-pod inline via
`_inline_hex_params`. Withdrawn in favour of "the alias builder never interpolates".
(7) The CLI said "exits non-zero only on the collision `ValueError`", wrong twice:
the collision is `AliasAmbiguousError`, and "only" made a typo'd source look like a
successful not-found.
(8) **Gotcha 12's join guidance was materially WRONG.** Stripping `obj://` from
`obj://<archive_id>#<entry>` yields the ARCHIVE id, which is not the member's FASTA
hash and is shared by every genome in the archive. Corrected, with the explicit rule
that an `archive_object_id` is never labelled `kbdl_fasta_object`.
A `## Acceptance Criteria` section (37 items) was appended, and Q11.1 was closed by a
sibling session's measurement.

**Round 0, external prior-art scan folded -- 2026-10-26.** The scan landed on its
fifth dispatch ([report](research/external-prior-art-scan.md), Maestro
`task-e34c7004`) and changed five entries. **It found one genuine defect:**
`ncbi_taxon` names a TAXON rather than an assembly, so it is a one-to-many
membership relation in a table whose `resolve()` promises at most one -- Q10 goes
from NONE to MEDIUM blast radius, a declared `INVERTIBLE_ALIAS_SOURCES` tuple now
exists, and `resolve()` REFUSES a non-invertible source instead of raising a
spurious ambiguity (`arc_analysis` is excluded on the same grounds, by its own
description). A second, smaller defect: `resolve()` now counts
`(entity_type, entity_hash)` PAIRS, because counting bare hashes hid cross-type
ambiguity in a verb that neither takes nor returns an entity type. Q2a rose to HIGH
confidence on independent corroboration -- the scan had not seen it and arrived at
the same assert/retract event-log conclusion unprompted. Q2 was reshaped: the thing
the prior art actually carries is METHOD and ACTOR, not a confidence float, and its
"cheap to add later" claim is read down as unverified. Q6 fell from high to MEDIUM
as the only design property with no validating precedent. Q11.2 closed -- there is
no published threshold, and the premise that registries evolved from in-code tuples
is itself unsupported.

**REVIEW ROUND 1 -- 2026-11-03. Chris reversed Q1.** One annotation, one decision,
and it is the IRREVERSIBLE one: **break the alias table up by entity type**, for the
same reason the other entity types were broken up, because there are far more
proteins than genomes and a shared table would make genome-alias reads pay protein
scale.

**Adopted, and the reasoning in the PRD is now his rather than mine.** My
`parameter_set` analogy was wrong -- that table is keyed by a hash and holds one row
per distinct parameter set, so it is not entity-keyed, which is exactly why it is
the lone exception. An alias table IS entity-keyed, so `<type>_entity` was always
the right precedent. I also checked the one thing that could have rescued a shared
table and it does not: four of the ten sources (`arc_record`, `arc_analysis`,
`kbase_ws_ref`, `ncbi_taxon`) are entity-type-agnostic, so those partitions would
mix genome and protein rows regardless of partitioning on `alias_source`.

**What propagated:** `alias` becomes the fourth KIND, so `_KINDS` and `_VIEW_KINDS`
each gain a member, `table_configs()` goes from seventeen configs to **twenty-one**,
and a fourth `all_alias` union view appears. All three verbs take an `entity_type`
(optional on the two read verbs, which fall back to `all_alias`). **Three things got
SIMPLER:** the bespoke `ENTITY_ALIAS_TABLE` constant and `entity_alias_columns()`
accessors are deleted because `table_name`/`table_columns` now serve them; the
manifest block validates through the standard `column_names(etype, "alias")` with no
special case; and `alias_orphans()` becomes a straight five-pair join. **Q3's
original justification COLLAPSED** -- `register()` can now name the table -- so that
entry is rewritten to stand on the two narrower grounds that survive, with the
collapse recorded. Q1a is new: per-entity-type source legality, which only became an
answerable question once the tables were split. Tests 1, 2 and 22's user story
INVERT, because the alias tables are now first-class rather than deliberately
excluded from the per-type surface.

## Problem Statement

A genome in this system is known by at least eight different names, and the
clearinghouse can record none of them.

It can record exactly one identifier: the `entity_hash`, a sha256 over sorted,
cleaned contig sequences. That hash is the right identity -- it is
order-independent, header-independent, and stable under re-annotation, which is
what makes it a *canonical* key. But it is an identifier nobody outside this system
has ever heard of, and it is not what anyone arrives holding.

What people actually arrive holding is a KBDL object id, or an NCBI assembly
accession, or a GTDB accession, or an NCBI taxon id, or a KBase assembly md5, or a
BV-BRC genome md5, or a KBase workspace ref, or an arc-store record id. Today each
of those lives in whichever system minted it, and the mapping between them lives
nowhere -- or, worse, lives twice: `kbaseincubator.inner_loop.v2_genome_aliases` and
`kbaseincubator.fitness.gene_alias` are two tables, in two namespaces, with two
different column spellings, solving two slices of the same problem for two entity
types.

Three concrete consequences, each of which is a thing somebody cannot do right now:

1. **The reverse lookup is impossible.** "I have a KBDL object id -- what is this?"
   has no answer. `kbdl-add-non-coding-to-existing-genome-v1`'s Q3 resolver needs
   exactly that question answered and currently has to plumb an object id through
   its caller instead.
2. **An identifier learned during a backfill is thrown away.** The `arc` adapter
   holds a genome's KBDL object id, its fasta object id, its taxon id and its arc
   record id in the same loop iteration where it writes `genome_entity` --
   and writes none of them, because there is nowhere to put them.
3. **Foreign interop hashes are homeless by construction.** KBase and BV-BRC md5
   assembly hashes are deliberately fenced OUT of `entity_hash` by
   genome-identity-v1 D4 and enforced by test. That fence is correct -- they are not
   this system's identity rule -- but it leaves them with no home at all, so a
   perfectly good interop key is simply discarded.

And the window is closing in the cheap direction. Every genome, protein, gene and
ontology_term table in the clearinghouse is **0 rows** today. The adapters that
would write aliases are specified and unbuilt. Everything decided now costs
nothing; the same decision made after the first bulk load costs a backfill pass
over the whole corpus.

## Solution

**FIVE new tables, one per entity type** -- `genome_alias`, `protein_alias`,
`gene_alias`, `function_alias`, `ontology_term_alias` -- plus a fourth `all_alias`
union view, and the accessors, validators, write boundary, read surface, CLI verbs
and health report that make them usable. **`alias` becomes the FOURTH KIND**
alongside `entity`, `content` and `result`, so the clearinghouse goes from sixteen
tables to **twenty-one**: the fifteen per-type tables, the parameter-set registry,
and five per-type alias tables.

```
<entity_type>_alias     -- genome_alias, protein_alias, gene_alias,
                           function_alias, ontology_term_alias
  entity_hash     STRING      -- 64 lowercase hex, canonical identity (rule A)
  entity_type     STRING      -- single-valued per table; kept for the all_alias view
  alias_source    STRING      -- the vocabulary the identifier belongs to; PARTITION KEY
  alias_id        STRING      -- the identifier itself, verbatim
  observation     STRING      -- one of ALIAS_OBSERVATIONS: 'present' | 'withdrawn'
  observed_at     TIMESTAMP
  ingest_batch_id STRING
```

Seven properties define them, and each is there for a stated reason:

**They are PER ENTITY TYPE, exactly as every other entity-keyed table in this
schema is.** Round 1 reversed this: the first draft specified ONE generic
`entity_alias` carrying `entity_type` as a column, reasoning from the
`parameter_set` precedent. **That was the wrong precedent, and the correction is
Chris's.** `parameter_set` is keyed by `parameter_set_hash` and holds one row per
distinct parameter set ever used -- it is not keyed by an entity at all, which is
exactly why it is the one non-entity-typed table. An alias table IS entity-keyed,
so its true analogue is `<type>_entity`, and that is per-type.

The volume argument is load-bearing, and the schema module already makes it:
"Because each entity type now has its own physical tables, `entity_type` is no
longer a useful partition key (it is single-valued within each table)". It names
`gene_entity`, `protein_entity`, `gene_content` and `protein_content` as **"the four
high-volume tables"**, against genome, function and ontology_term "whose row counts
are low enough (~1M-10M) that a partition key buys nothing". There are far more
proteins than genomes, so one shared alias table would drag genome-alias reads down
to protein scale -- and would reintroduce, as a multi-valued column, the very
`entity_type` discriminator that per-type tables exist to eliminate.

**And `alias_source` partitioning does NOT rescue a shared table.** That is the
check that settles this rather than leaving it a matter of taste. Four of the ten
sources are entity-type-AGNOSTIC: `arc_record`, `arc_analysis` and `kbase_ws_ref`
are job-and-object provenance that any entity type can carry, and `ncbi_taxon`
applies to genes and proteins as readily as to genomes. Those four partitions would
therefore MIX genome and protein rows, so a genome query filtering
`alias_source = 'arc_record'` would scan overwhelmingly protein rows. Partitioning
cleanly separates only the six genome-specific sources, and those were never the
problem.

**A consequence worth stating because it simplifies three things rather than
complicating them:** once `alias` is a kind, `table_name(entity_type, "alias")`
names these tables through the one sanctioned constructor,
`table_columns(entity_type, "alias")` serves their columns through the one
sanctioned accessor, and the manifest's `[source.alias]` block validates against
that same accessor. The first draft needed a bespoke `ENTITY_ALIAS_TABLE` constant
and bespoke `entity_alias_columns()` accessors precisely BECAUSE the table was not
entity-typed; all of that bespoke machinery now disappears.

**Its `alias_source` vocabulary is closed at the start.** Ten values, declared as a
module-level tuple, validated at the write boundary; adding one is a code change
and a review. An open vocabulary means two writers spell one thing two ways, which
is the harmonization defect this whole effort exists to stop -- there are currently
three spellings of "KBDL object X" inside one library, and that is what an open
vocabulary buys you one level down.

**It is append-only and fill-only on the five-tuple**
`(entity_type, entity_hash, alias_source, alias_id, observation)`. Iceberg offers
no uniqueness constraint and the clearinghouse already lives with that for
`parameter_set`. A retry adds nothing; a concurrent identical insert duplicates a
row harmlessly; readers select distinct.

**A ROW IS AN OBSERVATION, NOT A STANDING FACT -- and that is how retraction works
without a mutable column.** `observation` is a closed two-value vocabulary,
`present` or `withdrawn`. A withdrawal is itself an appended row saying "this alias
was observed to be ABSENT", so nothing is ever updated and append-only survives
intact. Readers take the LATEST observation per
`(entity_type, entity_hash, alias_source, alias_id)` by `observed_at`.

This is the one place this PRD goes BEYOND Chris's settled design, and it is called
out as such in Q2a. It exists because NCBI suppresses accessions, GTDB accessions
vanish between releases (7,225 of 596,859 genomes gone from NCBI), and an alias can
simply be wrong -- and because a `retracted_at` column cannot express any of it: a
column would have to be UPDATED on an existing row, which append-only forbids
outright, and it would make withdrawal TERMINAL when **reinstatement is real** (NCBI
un-suppresses). A sequence of observation rows expresses
`present -> withdrawn -> present` with no schema change and no loss of history.

**Two consequences that have to be stated together, because the obvious
implementation gets them wrong:**

- **Bulk writes are fill-only and always `present`**, so an ordinary retry of a
  backfill still adds nothing. But fill-only on the five-tuple would SUPPRESS a
  reinstatement -- the second `present` row looks like a duplicate of the first --
  so a withdrawal or reinstatement is written by a SEPARATE verb that ALWAYS
  appends and never applies fill-only. Idempotent bulk loading and expressible
  reinstatement are both preserved, and neither is possible if one verb tries to do
  both.
- **`observed_at` is the RIGHT clock here, and a later reader must not "fix" it into
  a `minted_at`.** It is the wrong clock for "which KBDL object is current" --
  that has to come from KBDL's own `object_supersedes`, because an alias can be
  observed long after the object was minted (Gotcha 7). But for "what did the
  upstream authority most recently say about this identifier", observation order IS
  assertion order, so the same column that is wrong for the first question is
  exactly right for the second.

**An alias may arrive before its entity, and that is legal.** There is deliberately
no referential check from an alias row to an entity row. A backfill learns an
accession from a manifest *before* it has hashed the contigs, and refusing that
would force writers to either buffer or drop real information. The integrity check
that does exist is the inverse one and it is a **report, not a refusal**: count
alias rows whose `entity_hash` has no entity row, per `alias_source`. A persistent
non-zero count is a finding about a producer; a transient one is a load in
progress.

**It is partitioned on `alias_source`.** That is the analogue of `source` on the
five result tables and `query_name` on `council.calls`: the slice that keeps one
vocabulary's files off another's.

**It carries a reverse lookup, which is the verb that makes it useful rather than
merely present.** `resolve(alias_source, alias_id) -> entity_hash | None` answers
"I have a KBDL object id, what is this?" -- the question the system cannot
currently answer at all.

### The vocabulary, fixed at the start

| `alias_source` | `alias_id` | shape validator | notes |
|---|---|---|---|
| `kbdl_object` | the KBDL object id | none (opaque) | **the current CanonicalGenome JSON object.** Multi-valued over time: enrichment mints a new one and the old row stays. |
| `kbdl_fasta_object` | `fasta_object_id` (= KBDL `content_hash`, rule B) | `^[0-9a-f]{64}$` | the bytes. **This is also where hash rule B is recorded**, which is how the A/B relationship becomes queryable instead of folklore. |
| `ncbi_assembly` | e.g. `GCF_000091665.1` | none (opaque) | |
| `gtdb_accession` | GTDB's accession | none (opaque) | distinct from NCBI's; GTDB r220 has a hard NCBI coverage ceiling -- 7,225 of 596,859 genomes are gone from NCBI -- so these genuinely diverge |
| `ncbi_taxon` | the taxon id, decimal string | `^[1-9][0-9]*$` | **NOT INVERTIBLE** -- a membership relation, see below. Deliberately redundant with `genome_content.taxon_id`: one query shape for every identifier |
| `kbase_assembly_md5` | the 32-hex md5 | `^[0-9a-f]{32}$` | foreign interop, fenced out of `entity_hash` by genome-identity-v1 D4 and homeless until now |
| `bvbrc_genome_md5` | the 32-hex md5 | `^[0-9a-f]{32}$` | same |
| `arc_record` | the arc store `record_id` | none (opaque) | |
| `arc_analysis` | the arc store `analysis_id` | none (opaque) | the grouping key across re-runs |
| `kbase_ws_ref` | a KBase workspace ref | none (opaque) | |

**There is no `kbdl_genome_id` source, deliberately.** It is `fasta_object_id[:16]`
-- a derivable prefix, not an independent identifier -- and storing a derivable
prefix invites a writer to treat it as independent.

**A source with no validator is accepted as an opaque string, and this table is
where that is declared.** An undeclared validator is a decision, not an omission:
the four opaque sources are opaque because their grammars are owned by other
systems and this library has no business asserting them.

### Not every alias source is INVERTIBLE, and the vocabulary has to say which

**This is a defect the external prior-art scan caught, and it is the most valuable
thing that scan returned.** `resolve(alias_source, alias_id)` promises at most one
entity. That promise is false for `ncbi_taxon`: **a taxonomy id names a TAXON, not
an assembly.** NCBI's own `assembly_summary` documentation separates the versioned
assembly identifier (column 1) from the organism and species taxonomy ids
(columns 6 and 7) precisely because they are different kinds of thing. So
`resolve('ncbi_taxon', '2157')` is a MEMBERSHIP query that legitimately matches
every archaeal genome in the corpus, and under the three-step reduction it would
raise `AliasAmbiguousError` for the entirely normal case of having more than one
genome per taxon. That is not an ambiguity to report; it is a question the verb
should never have accepted.

Three more sources were flagged as needing their own version-and-scope contracts
before scalar inversion can be asserted at all -- `arc_record`, `arc_analysis` and
`kbase_ws_ref`. Of those, `arc_analysis` is **explicitly the grouping key across
re-runs**, so it is one-to-many by construction and by its own description.

So the module declares a second, smaller tuple:

```
INVERTIBLE_ALIAS_SOURCES = (
    "kbdl_object", "kbdl_fasta_object", "ncbi_assembly", "gtdb_accession",
    "kbase_assembly_md5", "bvbrc_genome_md5", "arc_record", "kbase_ws_ref",
)
# NOT invertible: ncbi_taxon (a taxon names many assemblies),
#                 arc_analysis (the grouping key across re-runs)
```

`resolve()` **REFUSES a non-invertible source** with a distinct error naming
`aliases()` as the verb that answers the question. A refusal is right and an
ambiguity error is wrong, because `AliasAmbiguousError` means "this data is
contested and somebody must fix it" while a taxon matching many genomes means
"you asked a set question with a scalar verb" -- conflating them would send an
operator hunting a data defect that does not exist.

`arc_record` and `kbase_ws_ref` are listed as invertible **provisionally**: the scan
could not inspect their version semantics and neither could this PRD. If a workspace
ref turns out to be version-unpinned, or an arc record to cover several entities, the
fix is to move that one string from one tuple to the other -- which is exactly why
invertibility is DATA in a tuple rather than logic in the verb.

## User Stories

1. As a **KBDL job author**, I want to hand a KBDL object id to a resolver and get
   back the clearinghouse `entity_hash`, so that my job can look up what is already
   known about a genome it was given by object reference.
2. As the author of **`kbdl-add-non-coding-to-existing-genome-v1`**, I want that
   reverse lookup to exist as a library verb, so that my Q3 resolver does not have
   to plumb an object id through every caller.
3. As the **`arc` backfill adapter**, I want somewhere to put the KBDL object id,
   fasta object id, taxon id and arc record id I already hold in the loop where I
   write `genome_entity`, so that information I have in hand is not discarded.
4. As the **`kbdl_hash` backfill adapter**, I want to record each archive member's
   `fasta_object_id` as I hash it, so that the rule-A identity and the rule-B
   artifact id are linked at the moment both are known.
5. As **`KBDLHashGenomes`** (genome-identity-v1), I want to write a
   `kbdl_fasta_object` alias as I hash each member, for the same reason.
6. As the **arc indexer** (`kbdl-job-arc-indexer-v1`), I want to record `arc_record`
   and `arc_analysis` ids against an entity, so that a clearinghouse row can be
   traced back to the job that produced it and to the analysis group that job
   belonged to.
7. As an **operator**, I want `kbu clearinghouse resolve --alias-source kbdl_object
   --alias-id <id>` to print the entity hash, so that I can answer "what is this
   object" from a shell without writing SQL.
8. As an **operator**, I want `kbu clearinghouse aliases --type genome --hash <h>`
   to list every identifier known for one genome, so that I can see all eight names
   at once -- which is the thing Chris asked for.
9. As an **operator**, I want to filter that listing by `--alias-source`, so that I
   can ask "which genomes do we have NCBI accessions for" without reading the whole
   table.
10. As a **data engineer**, I want an `entity_hash` with no entity row to be a legal
    alias target, so that a backfill can record an accession it learned from a
    manifest before the contigs have been hashed.
11. As an **operator**, I want the orphan count reported per `alias_source` in
    `kbu clearinghouse health`, so that I can tell a producer that is persistently
    writing aliases for entities nobody ever loads.
12. As an **operator**, I want that orphan finding NOT to fail the `health` verb, so
    that a load in progress does not page me and so that the parameter-set findings
    -- which ARE failures -- stay distinguishable from this, which is not.
13. As a **writer of any kind**, I want an empty or whitespace-only `alias_id` to be
    REFUSED rather than silently skipped, so that a null pointer built by string
    concatenation cannot enter the lake looking like a real identifier.
14. As a **writer**, I want a misspelled `alias_source` to be refused at the write
    boundary naming the legal values, so that a typo cannot create an eleventh
    permanent partition holding one row.
15. As a **writer**, I want the whole call refused with nothing written when any row
    in it is invalid, so that I never have to reason about a half-applied batch.
16. As a **writer**, I want a retry of an identical call to add no rows, so that
    idempotence does not require me to track what I have already sent.
17. As a **consumer of the JSON envelope**, I want the new CLI verbs to be additive
    under an unchanged `schema_version`, so that nothing I already parse changes.
18. As an **operator deploying this**, I want a numbered runbook step that creates
    five new alias tables in a namespace that already holds sixteen, so that the
    deploy is a documented procedure rather than an improvisation.
19. As an **operator**, I want `bootstrap()` to remain safe to re-run after this
    change, so that adding the table cannot disturb the sixteen that exist.
20. As the **author of decision (2)**, I want the rule-A / rule-B distinction
    written down in the operator runbook, so that I can build the genome companion
    namespace on `fitness.assembly_hash` knowing which hash rule that column
    follows.
21. As a **reviewer of this build**, I want the alias integrity report to be
    demonstrably NOT wired into the `health` verb's non-zero exit, so that the one
    likely copy-paste error in this PRD is caught by a test rather than in
    production.
22. As a **future maintainer**, I want the alias tables to be ORDINARY
    entity-keyed tables -- named by `table_name(entity_type, "alias")`, served by
    `table_columns(entity_type, "alias")`, and unioned by `all_alias` exactly as
    `<type>_entity` is by `all_entity` -- so that no bespoke alias-only machinery
    exists for a future maintainer to keep in step.
23. As a **manifest author**, I want to declare alias rows in a backfill preset
    alongside `entity` and `content`, so that a source that knows identifiers can
    emit them without bespoke Python.
24. As a **manifest author**, I want a preset naming an unknown `alias_source` or an
    undeclared column to be refused at PLAN time rather than at write time, so that
    a bad preset fails before a long run starts.

## Implementation Decisions

All work is in `KBUtilLib`. Module paths are given for orientation only; the
binding content is the interfaces and the rules.

### 1. The schema module (`clearinghouse_schema`)

**`alias` becomes the fourth KIND.** That is the whole shape of this change, and
every other item below follows from it:

- **`_KINDS` becomes `("entity", "content", "result", "alias")`**, so
  `table_name(entity_type, "alias")` yields `genome_alias`, `protein_alias`,
  `gene_alias`, `function_alias` and `ontology_term_alias` through the ONE
  sanctioned table-name constructor. No bespoke constant.
- **`_VIEW_KINDS` becomes `("all_entity", "all_content", "all_result",
  "all_alias")`**, and `union_view_sql("all_alias", ...)` unions the five alias
  tables. They CAN be unioned, unlike the content tables, because the alias schema
  is GENERIC across types -- identical seven columns everywhere, like
  `_ENTITY_COLUMNS` and `_RESULT_COLUMNS` and unlike `_CONTENT_TYPE_HEAD`.
- **`_ALIAS_COLUMNS`** -- the seven `(name, sql_type)` pairs in DDL order, declared
  once, so the DDL, `table_columns()` and the tests read one definition:
  `entity_hash`, `entity_type`, `alias_source`, `alias_id`, `observation`,
  `observed_at`, `ingest_batch_id`. **`entity_hash` leads and `entity_type`
  follows, matching `_ENTITY_COLUMNS` exactly** rather than inventing an order.
- **`entity_type` STAYS as a column even though it is single-valued per table**,
  for precisely the reason `_ENTITY_COLUMNS` keeps it on `genome_entity`: the
  `all_alias` view needs a discriminator once the five are unioned. Dropping it
  would make the view's rows unattributable.
- **`table_columns(entity_type, "alias")` returns `_ALIAS_COLUMNS`** -- the generic
  branch, alongside `entity` and `result`. **No `entity_alias_columns()` accessor
  and no `ENTITY_ALIAS_TABLE` constant exist any more**; both were artefacts of the
  table not being entity-typed, and the first draft needed them only for that
  reason.
- **`_ALIAS_PARTITION_BY`** -- `alias_source` on ALL FIVE alias tables. This
  deliberately differs from `_ENTITY_PARTITION_BY`, which partitions only the two
  high-volume types: every alias table is genuinely MULTI-source (that is what the
  table is for), and `alias_source` is the filter essentially every read carries,
  so the pruning is worth having even on the smaller types. `entity_type` is NOT a
  partition key -- it is single-valued per table, which is the module's own stated
  reason for not partitioning on it anywhere.
- **`ALIAS_SOURCES`** -- a module-level tuple of the ten values, in the order the
  Solution table lists them.
- **`ALIAS_OBSERVATIONS = ("present", "withdrawn")`** -- a closed two-value
  vocabulary; the DEFAULT for every bulk write is `"present"`. Keep it a tuple with
  a validator for the same reason `ALIAS_SOURCES` is one: an open observation
  vocabulary lets a writer invent a third state that no reader knows how to order.
- **`ALIAS_SOURCE_PATTERNS`** -- a mapping from `alias_source` to a compiled shape
  regex, containing ONLY the four sources that have one. A source absent from this
  mapping is opaque by declaration. **It must not be defaulted to a permissive
  pattern**, because "opaque" and "validated by a pattern that matches everything"
  are different claims and only the first is honest.
- **`INVERTIBLE_ALIAS_SOURCES`** -- unchanged by this round; see the Solution's
  invertibility section and Q10.
- **`ALIAS_SOURCES_BY_ENTITY_TYPE`** -- NEW, and a direct consequence of splitting
  by type. See the next subsection.
- **`validate_alias(entity_type, entity_hash, alias_source, alias_id,
  observation="present") -> tuple`** -- the one validator. Rules: `entity_type` in
  `ENTITY_TYPES`; `entity_hash` through the existing `encode_entity_hash` 64-hex
  contract; `alias_source` in `ALIAS_SOURCES` **and legal for that `entity_type`**;
  `observation` in `ALIAS_OBSERVATIONS`; `alias_id` non-empty after `.strip()` and,
  where the source has one, matching its pattern. **`alias_id` is stored verbatim,
  not stripped** -- the strip is the emptiness TEST, not a transformation, because
  silently trimming an identifier changes it.

`table_configs()` emits **twenty-one** configs, in a deterministic documented order:
the five `<type>_entity`, the five `<type>_content`, the five `<type>_result`, the
parameter-set registry, then the five `<type>_alias` each carrying
`"partition_by": "alias_source"`. The module docstring, `table_configs()`'s
docstring and `PARAMETER_SET_TABLE`'s docstring all move from "sixteen" to
"twenty-one"; `PARAMETER_SET_TABLE` remains **the only** non-entity-typed table,
and its docstring's claim to that effect becomes true again rather than needing the
"one of two" hedge the first draft introduced.

#### Which sources are legal for which entity type

Splitting by entity type makes a question answerable that a single generic table
could only ignore: **`ncbi_assembly` is meaningless on a protein.** A closed
vocabulary that cannot say so is only half closed, so the module declares:

```
_TYPE_AGNOSTIC_ALIAS_SOURCES = ("arc_record", "arc_analysis", "kbase_ws_ref", "ncbi_taxon")
_GENOME_ONLY_ALIAS_SOURCES  = ("kbdl_object", "kbdl_fasta_object", "ncbi_assembly",
                               "gtdb_accession", "kbase_assembly_md5", "bvbrc_genome_md5")

ALIAS_SOURCES_BY_ENTITY_TYPE = {
    "genome":        _TYPE_AGNOSTIC_ALIAS_SOURCES + _GENOME_ONLY_ALIAS_SOURCES,
    "protein":       _TYPE_AGNOSTIC_ALIAS_SOURCES,
    "gene":          _TYPE_AGNOSTIC_ALIAS_SOURCES,
    "function":      _TYPE_AGNOSTIC_ALIAS_SOURCES,
    "ontology_term": _TYPE_AGNOSTIC_ALIAS_SOURCES,
}
```

**The four type-agnostic sources are agnostic for a reason, not for convenience:**
`arc_record`, `arc_analysis` and `kbase_ws_ref` identify a job record, an analysis
grouping and a workspace object respectively, none of which is specific to what
kind of entity is being described; and a gene or a protein has an NCBI taxon as
genuinely as a genome does.

**The other four types get NO type-specific sources yet, and that is deliberate
rather than an omission.** The Problem Statement motivates them -- `ontology_term`
has alternate ids, `function` has tool-local names -- but **this PRD has not
researched what those vocabularies are**, and inventing `uniprot_accession` or
`ec_number` here would be exactly the unresearched guess the closed-vocabulary rule
exists to prevent. The five tables are created now because schema shape at zero rows
is free; the vocabularies grow by code change and review, one researched source at a
time. **So `protein_alias` is writable only for provenance sources on day one**,
which is honest about what is actually known.

### 2. The write boundary (`ClearinghouseCapability.register_aliases`)

A separate public verb -- **and in round 1 the REASON for that changed, which is
worth stating rather than quietly keeping the conclusion.** The first draft argued
`register()` *structurally cannot* carry aliases, because it routes through
`table_name(entity_type, kind)` which refuses anything outside
`ENTITY_TYPES x _KINDS`. Now that `alias` IS a kind, that argument is simply gone:
`register(entity_type, rows, kind="alias")` would resolve the table perfectly well.

The verb stays separate on two narrower grounds that survive:

1. **`register()` is plain-append; aliases are FILL-ONLY.** Nothing in
   `register()`'s path consults existing rows, and folding a fill-only pre-read into
   it would change the behaviour of the entity, content and result writes that
   currently share it.
2. **`register()` already branches on kind for the parameter set** -- a `result`
   call requires a `parameter_set` input, validates it, hashes it and writes the
   registry first. Adding a third mode with different idempotence semantics makes
   one verb carry three contracts, and the one it would carry worst is the one with
   the subtlest invariant.

```
register_aliases(entity_type, rows, *, tenant=TENANT, dataset=NAMESPACE) -> dict
```

**It now takes `entity_type` and routes through `table_name(entity_type, "alias")`**
-- one call writes one table, exactly as `register()` does. `rows` is a sequence of
mappings each carrying `entity_hash`, `alias_source`, `alias_id`, and optionally
`entity_type`; a row whose `entity_type` disagrees with the argument is REFUSED
rather than silently coerced, because a row claiming a different type than the table
it is being written to is a caller bug and not a formatting nicety.

**THE CLOCK POLICY IS EXPLICIT, because "filled as the other write paths fill them"
turned out to name no policy at all.** `register()` hands prepared mappings straight
to `load()` and the registry projection retains caller-provided values, so the
existing capability writers have no clock of their own; only the sharder does. So,
for both alias verbs: **the capability generates a non-null UTC `observed_at` and a
fresh ULID `ingest_batch_id` per call, and a caller-supplied value for either is
REFUSED, not overridden.** Refusing rather than silently replacing matters because a
caller passing `observed_at` is expressing an intent -- source event time -- that
this design does not implement, and quietly discarding it would make the resulting
ordering wrong in a way nobody could see.

**Every latest-observation reduction in this PRD orders by
`observed_at DESC, ingest_batch_id DESC, observation DESC`**, which makes
`withdrawn` win an exact provenance tie. Equal timestamps are reachable at timestamp
precision under concurrency, and without a tie rule SQL row order decides whether an
alias is live -- arbitrarily, and differently on each read. This defines
**deterministic tie behaviour, not global causal ordering across writers**; two
writers disagreeing about the world is a producer problem the orphan report
surfaces, not something a sort key can fix.

It is modelled on `_write_parameter_set_registry()` -- a direct
`capability.load()` with
`tables=[{"name": table_name(entity_type, "alias"), "mode": "append"}]` and rows
projected through `column_names(entity_type, "alias")` -- and it inherits
`register()`'s four non-negotiable guards:

- **IN-POD ONLY.** Refuses off-pod before any transport call.
- **The dev 1206 pre-write assertion** (`_assert_write_target`): the write-target
  namespace and the existence-probe namespace must be SHOWN to agree, or it raises
  with `load()` uncalled.
- **Postflight** (`_check_postflight`): a null `row_count` is a FAILED load.
- **REFUSE, NEVER WARN.** Every row is validated BEFORE the first append; any
  failure refuses the WHOLE call with nothing written, naming the offending row
  index and the reason.

**Fill-only, and this is what makes a retry free.** Before appending, read the
distinct five-tuples already present for the `alias_source` slices the batch
touches **within this one table** -- `entity_type` no longer needs to be part of
the slice selection, because the table already is that selection -- and append only
the rows not already there. Return a report
shaped like the registry's: `written`, `already_present`, `load_result` (`None`
when every row was already present, in which case **no `load()` is made at all**
-- that is precisely what makes an ordinary retry add no row).

**And a SECOND verb, which must not be folded into the first:**

```
observe_alias(entity_type, entity_hash, alias_source, alias_id, observation,
              *, tenant=TENANT, dataset=NAMESPACE) -> dict
```

This is how a withdrawal or a reinstatement is recorded. It runs the SAME
validation and the SAME four guards, and it differs in exactly one way: **it ALWAYS
APPENDS and never applies fill-only.** That is not an inconsistency to tidy up --
it is the whole reason it is a separate verb. Fill-only on the five-tuple would
make a reinstatement (a second `present` after a `withdrawn`) look like a duplicate
of the original `present` row and suppress it, losing the very transition the
observation model exists to express. So: `register_aliases` is the idempotent bulk
path and is always `present`; `observe_alias` is the deliberate single-fact path and
carries any observation value.

**It does NOT call `_assert_lake5_schema`.** That guard is about the result slot
key and is deliberately left alone (see Gotchas). It SHOULD assert, cheaply, that
`table_name(entity_type, "alias")` exists in the write namespace, and refuse with a
message naming the runbook step if it does not -- the same posture `_assert_lake5_schema`
takes for an un-rebuilt result table, for the same reason: an append into a table
that is not there either fails obscurely or creates one with the wrong spec.

### 3. The read surface (`ClearinghouseCapability`)

```
aliases(entity_type=None, entity_hash=None, *, alias_sources=None,
        alias_ids=None, engine=None) -> list[dict]
```

Four filters composed by `AND`. **`entity_type` now also SELECTS THE TABLE**, and
that is the one behavioural change this round makes to the read surface: with an
`entity_type` given, the verb reads that one `<type>_alias` table; with `None`, it
reads the `all_alias` union view, which exists precisely so a cross-type question
stays answerable in one query. The view is the only reason `entity_type` survives
as a column, and this is what it is for.

**The semantics are `results()`'s, stated exactly**: `None` applies no filter; `[]` is a request to match nothing. For the
list-valued filters use the existing `_in_literal()` helper, which already yields
`1 = 0` for an empty list rather than an invalid `IN ()`. `entity_hash` goes
through `_encode_hashes`. **Build hash predicates with PLACEHOLDERS and
canonicalized params and execute them through the existing `_run` binding regimes;
do not interpolate a hash in the alias SQL builder itself.** The first draft said
"parameter-bound, never inlined", which is unimplementable: `_run` has THREE regimes
and only one binds server-side. In-pod Trino binds truly; **in-pod Spark cannot bind
at all** (`BerdlCapability.query` refuses `params` there) and off-pod has no bind
channel, so both route through `_inline_hex_params`, which inlines the
already-validated 64-hex value. Demanding "never inlined" would mean either refusing
Spark and off-pod hash filters -- capabilities every neighbouring reader has -- or
inventing a remote bind protocol, which is not this change. The invariant that is
both achievable and the one that matters: **the alias builder never does its own
string interpolation**, so validation and inlining stay in the single audited place.
`alias_sources` is the partition column, so it prunes -- and per-type tables mean a
genome read now prunes on BOTH axes, the table and the partition, which is the whole
point of the round-1 split.

Returns one dict per distinct five-tuple plus `observed_at` and `ingest_batch_id`.
**`SELECT DISTINCT` on the five-tuple**, with `MAX(observed_at)` and a
corresponding `ingest_batch_id`, because the table is fill-only and a duplicate
row is expected and harmless -- a reader that returned duplicates would be
surfacing an implementation detail as data.

**`aliases()` returns the most recent occurrence of EACH OBSERVATION STATE per
four-tuple -- not full event history, and not just current state.** Repeated
same-state events are intentionally collapsed by the five-tuple `DISTINCT`: after
`present(t1) -> withdrawn(t2) -> present(t3)` it returns TWO rows, the withdrawn at
t2 and the present at t3, and it cannot show the original t1 registration or
distinguish a genuine re-assertion from a retry.

That limit is stated rather than papered over, because the first draft advertised
"observation history" while specifying a five-tuple reduction, and those are
incompatible -- the reduction necessarily merges the first and third events. **No
public complete-event-history accessor is promised here.** An operator asking "what
happened to this accession" gets the state transitions, which is the useful answer;
an auditor needing every row reads the table directly. Test 10a therefore asserts
the three rows were PERSISTED, via the write path, not that `aliases()` returns
three.

**`current_only=True` first reduces each four-tuple to its single latest observation
and then EXCLUDES withdrawn winners**, so it returns the live set and never hands
back a row marked `withdrawn`. Returning a withdrawn dict and returning nothing are
different public behaviours, and a caller trusting the phrase "live set" would
otherwise act on an explicitly retracted identifier.

```
resolve(alias_source, alias_id, *, entity_type=None, engine=None) -> str | None
```

**`entity_type` is new this round and is OPTIONAL.** With it, `resolve()` reads one
table and the answer cannot be cross-type-ambiguous by construction. Without it, it
reads `all_alias` and the pair-counting below is what keeps a cross-type collision
visible. Defaulting to `None` keeps the verb usable by the caller it was designed
for -- `kbdl-add-non-coding-to-existing-genome-v1`'s Q3 resolver, which holds a KBDL
object id and does NOT know the entity type, since not knowing what the thing is is
the entire reason it is asking.

Its reduction order is specified exactly, because three different readings of "the
latest observation for the pair" are each defensible and they disagree:

1. **Reduce independently per `(entity_type, entity_hash, alias_source, alias_id)`**
   to that four-tuple's latest observation, using the tie order above.
2. **Discard the four-tuples whose winner is `withdrawn`.**
3. **Count distinct `(entity_type, entity_hash)` PAIRS across what survives:**
   zero -> `None`, exactly one -> that hash, more than one -> raise.

**Count the PAIR, not the hash alone** -- a finding from the external scan, and it
matters MORE after the round-1 split, not less: the rows now live in five separate
tables, so a cross-type collision is a collision ACROSS TABLES and is invisible to
any single-table read. `resolve()` returns a bare `entity_hash`, so counting hashes
alone would hide it: one `(alias_source, alias_id)`
resolving to the same hex string under two different entity types would read as
unambiguous and return a hash the caller cannot interpret. Counting pairs makes that
case raise, which is correct -- it genuinely is contested.

**Historical withdrawn mappings therefore do NOT cause ambiguity.** The case that
forced this to be written down: `hA present(t1)`, `hB present(t2)`,
`hA withdrawn(t3)` for one `(alias_source, alias_id)`. A pair-global "latest wins"
returns `None` and loses a perfectly valid mapping; counting every historical hash
raises forever and makes a CORRECTED collision permanently unresolvable; the rule
above returns `hB`, which is the only answer that treats the withdrawal as the
correction it is.

Validates `alias_source` against `ALIAS_SOURCES` and raises `ValueError` on an
unknown one -- a typo must not return `None`, because `None` means "no such alias"
and a caller will treat the two identically otherwise.

**On more than one distinct `entity_hash` for one `(alias_source, alias_id)` it
RAISES, and it raises a DISTINCT exception type.** Define
`AliasAmbiguousError` for this case; do not reuse `ValueError` and do not return
`None`. The principle, and it is worth stating because it generalises: **never
collapse "unknown" and "contested" into one answer, because the correct caller
response differs** -- unknown means retry or ignore, contested means stop and
escalate. The exception names the pair AND every `entity_hash` found, because an
ambiguity you cannot see both sides of costs a second investigation.

**`None` does double duty for "never known" and "withdrawn", deliberately**, because
withdrawal is a NORMAL upstream event and not an error, and forcing every caller
into exception handling for it would be wrong. `resolve()` stays a total function
answering one question -- "is there a current entity for this identifier" -- and a
caller that needs to tell the two apart calls `aliases()` for the history. This
asymmetry with the contested case is intentional: contested is a defect somebody
must act on, withdrawn is the world changing.

Default engine Trino for both, matching every other small aggregate in the class.

### 4. The health report

```
alias_orphans(*, engine=None) -> dict
```

Counts alias rows whose `entity_hash` has no row in the corresponding
`<entity_type>_entity` table, **grouped by `(entity_type, alias_source)`** and
**counting CURRENT observations only** -- reduce to the latest observation per
four-tuple and count only those whose latest is `present`. A withdrawn alias
pointing at a non-existent entity is not a finding about a producer; it is an
identifier the upstream authority retracted, which is precisely the state the table
exists to record. Counting it would make the report grow monotonically with normal
upstream churn and so become useless. The report carries the reduced population
size, so an operator can see what was actually checked.

It is modelled on `parameter_set_orphans()` in SHAPE -- writes nothing, raises
nothing for a finding, returns a report an operator can read -- and **differs from
it in exactly one load-bearing way: its findings are NOT failures.** The report
dict therefore carries `orphans_by_source` and `orphan_total` and **deliberately
does NOT carry a `failures` key**, because `_exit_on_integrity_failure()` reads
`report["failures"]` and a non-empty list there would exit the `health` verb
non-zero.

The CLI `health` verb carries it as `data.alias_integrity`, alongside the existing
`data.parameter_set_integrity`, and `_exit_on_integrity_failure` is **not**
extended to read it. The envelope's `warnings` list is also the wrong home: a
warning is for a degraded answer, and an orphan count is a complete answer about
a legitimate state.

### 5. The CLI verbs (`interfaces/cli/clearinghouse.py`)

Two new commands, each built by a `_build_*` helper emitting ONE envelope under
the existing output contract, with the command layer holding no logic of its own:

- `kbu clearinghouse aliases [--type T] [--hash H] [--alias-source S ...] [--alias-id I ...] [--json]`
- `kbu clearinghouse resolve --alias-source S --alias-id I [--json]`

**`AliasAmbiguousError` is a DISTINCT exception and NOT a `ValueError` subclass**,
so the two cannot be caught together by accident. The exit contract:

- a valid lookup with **no current mapping** emits a SUCCESSFUL envelope and
  **exits 0** -- "this alias is not known" answers the question asked;
- **`AliasAmbiguousError`** emits ONE failure envelope and **exits 1**;
- an **invalid `--alias-source`** likewise emits one failure envelope and
  **exits 1** -- it is bad user input, and the first draft's "exits non-zero only on
  the collision" would have made a typo'd source indistinguishable by exit status
  from a successful not-found;
- transport and query errors also exit non-zero.

No exception may escape as a traceback: that would bypass the one-envelope output
contract every other verb in this module honours.

**`SCHEMA_VERSION` stays at 1.** Both additions are new verbs and a new key inside
`health`'s `data`; nothing a consumer already parses changes shape, which is
exactly the additive case the output contract documents.

### 6. The manifest's fourth block kind (`clearinghouse_manifest`)

`KINDS` becomes `("entity", "content", "result", "alias")`, and a source may
declare `kinds = ["entity", "content", "alias"]`.

The `[source.alias]` block is **list-shaped, not column-shaped**, and that is the
difference from every existing block: a source emits MANY alias rows per entity,
one per `alias_source`, so the block is an array of entries each naming an
`alias_source` and a derivation for its `alias_id`:

```toml
[[source.alias]]
alias_source = "kbdl_fasta_object"
alias_id = "fasta_object_id"

[[source.alias]]
alias_source = "ncbi_assembly"
alias_id = "@column(assembly_accession)"
```

`_plan_alias(source)` validates at PLAN time, before any record is read:
`alias_source` must be in `ALIAS_SOURCES`; `alias_id` must be a parseable
derivation; `entity_type`, `entity_hash`, `observed_at` and `ingest_batch_id` are
filled by the sharder and **must not be mappable** in the block, mirroring
`_plan_result`'s `reserved` set.

**Null-preserving derivations drop the ROW, not the value.** `@prefix` and
`@concat` return `None` when a named column is null, and for an alias that means
the row is not emitted at all. An alias row with a null `alias_id` is exactly the
`"kbdl:obj:None"` pointer-to-nothing the manifest's own comments warn about, made
multi-valued. The sharder skips it; `validate_alias` would refuse it anyway, and
both are correct -- the skip is so a legitimately-sparse source does not fail a
whole run, and the refusal is so a programmatic writer cannot bypass the skip.

### 7. Documentation

- A new numbered runbook section, **OP-C5**, following the OP-C1 pattern exactly
  (dry run showing `action: create` for one table, then the real run, then an
  acceptance read): create the five `<type>_alias` tables and the `all_alias` view
  in a namespace that already holds sixteen, via `bootstrap()`, which is idempotent and will report the sixteen as
  already present.
- **H3's documentation fix**, which has no other home and is one paragraph: state
  in `agent-io/docs/clearinghouse-schema-operator-runbook.md` that **rule A is the
  clearinghouse identity and rule B is a KBDL-local artifact id**, and that
  `kbaseincubator.fitness`'s `genome.assembly_hash` / `assembly.assembly_hash`
  declares neither today. Record also that **KBDL's `genome_id` is literally
  `fasta_object_id[:16]`**, which is why the vocabulary has no `kbdl_genome_id`
  source.
- **The `obj://` pointer rule, in its CORRECTED form** (Gotcha 12 has no other
  home): for a standalone `obj://<fasta_object_id>`, removing the scheme recovers
  `kbdl_fasta_object.alias_id`; for `obj://<archive_object_id>#<entry_name>`,
  parsing recovers an archive-and-member LOCATOR and **not** the member's FASTA
  hash, so such a member must be joined through `entity_hash` or an explicit
  member-mapping that does not yet exist. State plainly that an
  `archive_object_id` must **never** be labelled `kbdl_fasta_object`, because it is
  shared by every genome in the archive.
- `agent-io/docs/kbu-clearinghouse-cli.md` gains the two verbs.

### Where the deep module is

`validate_alias` is the deep module here, and it is deliberately the only one. A
lot of behaviour -- five distinct rules, four per-source patterns, the
strip-as-test-not-transform subtlety, and the refuse-never-skip posture -- sits
behind a four-argument function returning a tuple. Every writer (the capability,
the sharder, the manifest planner) goes through it, so there is exactly one place
the vocabulary and its shapes are enforced and no path around it.

The read verbs are deliberately shallow by comparison: they are SQL builders over
a seven-column table, and giving them more structure than that would be inventing
depth where there is none.

## Testing Decisions

What makes a good test here is a check on EXTERNAL behaviour -- the emitted DDL,
the refusal, the returned rows, the exit code -- rather than on how the module is
organized internally. The schema module is pure and tests against it need no lake;
the capability and CLI tests follow the existing in-repo pattern of a stubbed
capability, with the one caveat below.

**Modules under test:** `clearinghouse_schema`, `ClearinghouseCapability`
(`register_aliases`, `aliases`, `resolve`, `alias_orphans`),
`clearinghouse_manifest` (`_plan_alias` via `shard_plan`), and the
`interfaces/cli/clearinghouse.py` verbs.

**Prior art to mirror, named so it is not reinvented:**
`tests/berdl/test_clearinghouse_schema.py::TestParameterSetRegistry` is the direct
template for the schema tests and already encodes the right instincts -- it asserts
the constant's value, that the config is present and LAST, the exact column tuple
in order, partitioning by KEY ABSENCE rather than a falsy value, that the accessor
renders the same `schema_sql` the config carries, that `table_name()` REFUSES the
name in either argument position, and that the table appears in no union view. The
alias suite should mirror all seven of those, plus a partition-key PRESENCE
assertion (the registry is unpartitioned; this table is not).

Other prior art: `tests/berdl/test_clearinghouse_md5_fence.py` is the model for a
test that fences a value out of a column by construction, which is the shape the
vocabulary-closure test wants. `tests/cli/test_clearinghouse.py` holds the envelope
assertions and is where the additive-`schema_version` check belongs.

**The tests that actually earn their place**, as opposed to restating the schema:

1. **`table_configs()` returns twenty-one, with the five `<type>_alias` configs
   LAST, each partitioned on `alias_source`.** The count is the regression guard: a
   developer adding a kind and forgetting its configs is the failure.
2. **The alias tables ARE first-class, and this test INVERTS from round 0.**
   `table_name(etype, "alias")` returns `<etype>_alias` for all five types;
   `table_columns(etype, "alias")` returns the generic seven columns;
   `union_view_sql("all_alias", ...)` names exactly the five alias tables and
   nothing else; and `PARAMETER_SET_TABLE` is STILL refused by `table_name()` in
   either argument position, because it remains the only non-entity-typed table.
2a. **Every alias table carries `entity_type`, and `all_alias` selects it.** The
    column is single-valued per table and exists only so the union view's rows stay
    attributable -- a regression that dropped it would make `all_alias` useless
    while every single-table read kept working, so the test has to be on the view.
3. **The vocabulary is closed, by behaviour.** A write and a manifest plan naming
   `kbdl_genome` (a plausible misspelling of `kbdl_object`) must both be refused,
   and the message must name the legal values. Asserting the tuple's contents is
   not the same test and is worth much less.
4. **There is NO `kbdl_genome_id` source.** A deliberate absence test, because the
   reason for the absence is subtle (it is a derivable prefix) and the next person
   to read the vocabulary will want to add it.
5. **Every pattern-validated source rejects a near-miss**: a 63-char hex for
   `kbdl_fasta_object`, a 31-char hex and an uppercase md5 for the two md5 sources,
   and `0` and `01` for `ncbi_taxon`.
6. **An opaque source accepts a string no pattern would**, so "opaque by
   declaration" is a tested property rather than an accident of there being no
   pattern.
7. **An empty, whitespace-only and `None` `alias_id` are each REFUSED, not
   skipped**, at the programmatic write boundary. This is story 13 and the
   `"kbdl:obj:None"` hazard; a test that only checks the empty string misses the
   case the manifest comments actually warn about.
7a. **The `observation` vocabulary is closed.** A write naming `retracted` (a
    plausible synonym for `withdrawn`) is refused, and the message names the two
    legal values.
8. **`alias_id` is stored VERBATIM.** A value with leading or trailing whitespace
   that is non-empty after strip must come back byte-identical. The strip is the
   emptiness test, not a transformation, and the obvious implementation gets this
   wrong.
9. **One invalid row refuses the whole call with `load()` uncalled**, naming the
   row index. Assert the call count on the stub, not just the exception.
10. **A retry of an identical call makes NO `load()` call at all** and reports every
    row as `already_present`. Fill-only is the property; "it did not duplicate" is
    a weaker test that a `load()` with zero surviving rows would also pass.
10a. **A `present -> withdrawn -> present` sequence survives, and `resolve()`
     tracks it.** Write `present` via the bulk path, then `withdrawn` via
     `observe_alias`, then `present` again via `observe_alias`. Assert all three
     rows exist, that `resolve()` returns the hash at step 1, `None` at step 2, and
     the hash again at step 3. **This is the test that catches the single most
     tempting refactor in this PRD** -- folding `observe_alias` into
     `register_aliases` -- because fill-only would suppress the third row and
     `resolve()` would stay stuck on `withdrawn`. Nothing else in the suite fails
     if that refactor is made.
11. **`aliases()` filter semantics**: `None` applies no filter, `[]` matches
    nothing, and the four filters compose by `AND`. Assert against the emitted SQL
    for the `[]` case (it must contain `1 = 0` and must NOT contain `IN ()`), and
    against returned rows for composition.
11a. **The alias SQL builder never interpolates a hash itself.** Assert the builder
     emits a PLACEHOLDER and passes canonicalized params, on every engine. Do NOT
     assert "the hash is absent from the final SQL text" -- that is false by design
     off-pod and on in-pod Spark, where `_run` inlines via `_inline_hex_params`
     because neither can bind. The property under test is where interpolation
     happens, not whether it happens.
12. **`aliases()` returns one row per distinct five-tuple** when the stub returns a
    duplicated row, so fill-only's duplicates do not leak to a reader.
13. **`resolve()` returns `None` for an unknown alias but RAISES `ValueError` for an
    unknown `alias_source`.** The two must be distinguishable; conflating them is
    the bug this test exists for.
13a. **`aliases()` returns per-state latest by default; `current_only=True`
     EXCLUDES withdrawn winners.** With a `present` and a later `withdrawn` row for
     one four-tuple, the default call returns BOTH, and `current_only=True` returns
     an EMPTY result -- not the withdrawn row. Returning a withdrawn dict from
     something documented as the live set is the specific defect this test prevents.
13b. **Repeated same-state events are collapsed, and that is asserted rather than
     discovered.** After `present -> withdrawn -> present`, the default
     `aliases()` returns TWO rows (the t2 withdrawn and the t3 present), not three.
     This pins the contract that `aliases()` is not an event-history accessor.
14. **`resolve()` RAISES on two distinct hashes for one pair**, and the message
    names both. See Q7.
14a. **The contested raise is a DISTINCT exception type and NOT a `ValueError`
     subclass.** Assert `AliasAmbiguousError` specifically, assert it is NOT what an
     unknown `alias_source` raises, and assert `issubclass(AliasAmbiguousError,
     ValueError)` is FALSE -- otherwise a single `except ValueError` catches both and
     the distinction the type exists for is lost silently.
14b. **A CORRECTED collision resolves rather than raising forever.** The sequence
     `hA present(t1)`, `hB present(t2)`, `hA withdrawn(t3)` for one
     `(alias_source, alias_id)` must return `hB`. Assert it does NOT return `None`
     (a pair-global latest-wins reading, which loses a valid mapping) and does NOT
     raise (a count-every-historical-hash reading, which makes the correction
     permanently unresolvable). This is the test that pins the three-step reduction
     order in the read surface.
14b2. **`resolve()` REFUSES a non-invertible source, and the refusal is not an
      ambiguity error.** `resolve('ncbi_taxon', ...)` and
      `resolve('arc_analysis', ...)` must raise a distinct non-invertible error
      naming `aliases()` as the right verb -- NOT `AliasAmbiguousError`, and NOT a
      `None`. Assert this even when only ONE genome carries that taxon, because the
      refusal is about the source's CONTRACT and not about how many rows happen to
      match today; a test that seeds one row and passes would hide the defect.
14b3. **`resolve()` counts `(entity_type, entity_hash)` PAIRS, not hashes.** Seed
      the same hex string under two different `entity_type` values for one
      `(alias_source, alias_id)` and assert `AliasAmbiguousError`. Counting hashes
      alone would read as unambiguous and return a hash the caller cannot interpret,
      since `resolve()` takes and returns no entity type.
14c. **The tie order is deterministic.** Given a `present` and a `withdrawn` for one
     four-tuple with IDENTICAL `observed_at`, the reduction is stable across repeated
     calls and `withdrawn` wins. Without a tie rule, SQL row order decides whether an
     alias is live, differently on each read.
14d. **Caller-supplied provenance is REFUSED.** A row carrying `observed_at` or
     `ingest_batch_id` is rejected by both write verbs, rather than silently
     overridden, and each verb generates a non-null UTC timestamp and a fresh ULID
     batch id per call.
15. **`alias_orphans()` reports a non-zero orphan count and the `health` verb still
    exits ZERO.** This is the single most important test in the PRD. Assert the exit
    code, and assert that the returned report has no `failures` key -- the second
    assertion is what stops a later refactor reintroducing the exit by renaming a
    field.
16. **A parameter-set finding still exits NON-ZERO with an alias orphan count
    present.** The companion to 15: the two reports must stay independent, and a
    developer who suppressed the alias exit by weakening
    `_exit_on_integrity_failure` would break this.
17. **An alias orphan is not reported as a `warning`.** Keeps the envelope's
    `warnings` meaning "degraded answer".
18. **`_assert_lake5_schema` is unchanged by the new config.** Build the preflight
    against a namespace holding the sixteen old tables and NONE of the five
    `<type>_alias` tables, and
    assert it still passes. This is the regression guard on Gotcha 2 -- it fails
    loudly if someone "helpfully" teaches the preflight about every table.
19. **Manifest plan-time refusals**: an unknown `alias_source`, an unparseable
    `alias_id` derivation, and an attempt to map a sharder-filled column
    (`entity_hash`, `observed_at`) in a `[source.alias]` block.
20. **A null-producing `@concat` in an alias block drops the ROW** and does not
    emit an `alias_id` containing the literal `None`.
21. **`SCHEMA_VERSION` is still 1** and the pre-existing `health` envelope keys are
    all still present with the new one added.

**Framing, binding:** every test criterion is regression-framed. This repo carries
pre-existing failures (a recorded machine-local baseline of 6 `test_types_config`
failures), so "the suite is green" is unmeetable and must not be written. The bar
is: the tests a task adds pass, and no test that passed on the task's base commit
fails on its branch.

**What is NOT unit-tested, stated rather than hidden:** the live Iceberg
create-table path, the real partition spec as Iceberg records it, and the actual
row counts. Those are the OP-C5 acceptance step, in-pod, and no amount of stubbed
testing substitutes for them. This matters because of a live precedent:
`kbdl-file-load-and-genome-sources-v1` has every task merged and is still
unfinished, because its acceptance is an operator load nobody has run. **This PRD
is not finished when its tasks merge.**

## Open Questions and Judgement Calls

**Every entry below is already DECIDED and the decision is already reflected in
the PRD body and the taskplan. This PRD is buildable exactly as it stands.** The
section is a manifest of the choices made, not a queue of holes. To change one,
edit its `DECIDED:` line -- and then the `If you disagree` line names what else
moves.

Entries are ordered by BLAST RADIUS DESCENDING, which means by the cost of
REVERSING the decision after the work ships, not by the size of the change now.

### Q1. ONE generic alias table carrying `entity_type`, or one per entity type? -- DECIDED (REVERSED IN ROUND 1): five per-type tables, with `alias` as a fourth KIND

**Blast radius:** IRREVERSIBLE
**REVERSED BY CHRIS IN REVIEW ROUND 1**, and the reversal is correct. The first
draft chose ONE generic `entity_alias` carrying `entity_type` as a column, reasoning
from `parameter_set`. **That was the wrong precedent.** `parameter_set` is keyed by
`parameter_set_hash` and holds one row per distinct parameter set ever used; it is
not entity-keyed, which is exactly why it is the single non-entity-typed table. An
alias table IS entity-keyed, so its analogue is `<type>_entity` -- and that is
per-type.

**Why:** two reasons, and the second is the one that makes it more than a
consistency preference.
(1) **Volume.** The schema module names `gene_entity`, `protein_entity`,
`gene_content` and `protein_content` as "the four high-volume tables" against
genome, function and ontology_term "whose row counts are low enough (~1M-10M) that a
partition key buys nothing". There are far more proteins than genomes, so one shared
alias table drags genome-alias reads to protein scale. The module also states the
structural half: "Because each entity type now has its own physical tables,
`entity_type` is no longer a useful partition key (it is single-valued within each
table)" -- a generic table reintroduces as a multi-valued column the very
discriminator per-type tables removed.
(2) **`alias_source` partitioning does NOT rescue a shared table**, which is the
check that settles it. Four of the ten sources are entity-type-AGNOSTIC --
`arc_record`, `arc_analysis`, `kbase_ws_ref` and `ncbi_taxon` -- so those partitions
would MIX genome and protein rows and a genome query on
`alias_source = 'arc_record'` would scan overwhelmingly protein rows. Partitioning
separates only the six genome-specific sources, which were never the problem.

**What it moved, and it moved less than the first draft predicted:** `_KINDS` gains
`"alias"`, `_VIEW_KINDS` gains `"all_alias"`, `table_configs()` goes from seventeen
configs to **twenty-one**, and `register_aliases`/`aliases`/`resolve` take an
`entity_type`. But three things got SIMPLER, because the tables are now ordinary:
the bespoke `ENTITY_ALIAS_TABLE` constant and `entity_alias_columns()` accessors are
DELETED (`table_name` and `table_columns` serve them); the manifest's `[source.alias]`
block validates against the standard `column_names(etype, "alias")` with no special
case; and `alias_orphans()` becomes a straight five-pair join instead of switching
its join target on a column value. `entity_type` is KEPT as a column -- exactly as
`_ENTITY_COLUMNS` keeps it on `genome_entity` -- because `all_alias` needs a
discriminator.

**If you disagree** (i.e. to go back to one generic table): revert `_KINDS` and
`_VIEW_KINDS`, reinstate `ENTITY_ALIAS_TABLE` plus the two bespoke accessors, drop
`ALIAS_SOURCES_BY_ENTITY_TYPE` and Q1a, return `table_configs()` to seventeen, drop
the `entity_type` argument from the three verbs, and restore the manifest special
case. **But note it is no longer free:** the reason the first draft called this
IRREVERSIBLE holds in both directions, and every genome table is still 0 rows only
until the first load.
**Confidence:** high. The volume premise is the schema module's own, and the
partition-does-not-rescue-it check was performed rather than assumed.

**A sibling design depended on the old answer, and the dependency is now moot.**
Session `368c658e` asked whether its own `genome_alias` should be a PROJECTION of a
generic `entity_alias`. It has since told me (2026-10-27) that Chris rescoped its
PRD to duplicate nothing the clearinghouse holds, so `genome_alias` was DELETED and
its loader writes to the clearinghouse alias tables directly. Under this round's
reversal the convergence is cleaner still: the clearinghouse table it writes is
`genome_alias`, per-type, which is the shape that session originally proposed.

### Q1a. Which `alias_source` values are legal for which entity type? -- DECIDED: declared per type, with four type-agnostic sources and genome-only for the rest

**Blast radius:** LOW
**Why:** Splitting by entity type makes answerable a question a generic table could
only ignore -- `ncbi_assembly` is meaningless on a protein -- and a closed
vocabulary that cannot say so is only half closed. `arc_record`, `arc_analysis`,
`kbase_ws_ref` and `ncbi_taxon` are declared type-AGNOSTIC because they identify a
job record, an analysis grouping, a workspace object and a taxon respectively, none
of which is specific to the kind of entity being described. The other six are
genome-only.
**The other four types therefore start with NO type-specific sources, deliberately.**
The Problem Statement motivates them -- `ontology_term` has alternate ids, `function`
has tool-local names -- but this PRD has NOT researched those vocabularies, and
inventing `uniprot_accession` or `ec_number` here would be precisely the unresearched
guess a closed vocabulary exists to prevent. The five tables are created now because
schema shape at zero rows is free; the vocabularies grow one researched source at a
time.
**If you disagree:** dropping the per-type map makes `validate_alias` accept any
source for any type and deletes one test; the cost is that a writer can put an
assembly accession on a protein and nothing refuses it.
**Confidence:** high on the mechanism, medium on the partition of the ten -- the
four type-agnostic ones are a judgement from what the identifiers denote, not from a
measurement, and a reviewer who knows the arc store better may move one.

### Q2. Should the alias tables carry a `confidence` or `asserted_by` column? -- DECIDED: no such column

**Blast radius:** MEDIUM
**Why:** Both live precedents (`v2_genome_aliases`, `gene_alias`) have neither, and
an alias is normally a fact rather than a claim. The counter-argument is real and
should be read before accepting this: `arc_record` and `arc_analysis` ARE
attributions, and `kbdl-job-arc-indexer-v1`'s precedence ladder (stamp -> existing
row -> arc-tree mention -> unattributed) is exactly a confidence gradient. The
decision rests on that ladder's OWN rule -- attribution is never guessed and never
downgraded -- so anything the indexer stores is already the confident tier, and a
column to say so would always hold the same value.
**If you disagree:** adding a nullable column later is a DDL change Iceberg
supports, so the cost is not the column -- it is that every row written before it
existed is `NULL`, and `NULL` is indistinguishable from "asserted with unknown
confidence". Concretely: it adds one entry to `_ENTITY_ALIAS_COLUMNS` in t1, a
field to `register_aliases`'s row contract and `validate_alias`'s signature in t2,
a filter and a returned key in t3, and an `[[source.alias]]` key in t6. Doing it
NOW costs about an hour; doing it after the first bulk load costs a backfill or a
permanently ambiguous column.
**Confidence:** LOW, and the external scan has now RESHAPED rather than resolved
this entry. Three things it established, all of which change the argument:

- **"Confidence" was the wrong name for what is missing.** The surveyed precedents
  do not carry a floating confidence; they carry METHOD and ACTOR. Ensembl's
  `info_type` distinguishes a DIRECT link from a PROJECTION or a
  COORDINATE_OVERLAP, `identity_xref` carries alignment score and percent identity,
  and `analysis_id` traces which analysis asserted the row; SSSOM separates
  predicate, justification, author and reviewer, with confidence merely OPTIONAL.
  **A confidence number alone would not recover method or actor**, so the column
  this entry declines is not the column the prior art actually has.
- **The concretely lost operations, which the first draft could only gesture at:**
  selecting DIRECT rather than inferred links; filtering by alignment score;
  tracing which analysis or which human asserted a mapping; and selecting
  assertions by justification or reviewer. None of those is answerable from the
  seven columns, and no "retained elsewhere" store exists for them in this design.
- **The "cheap to add later" claim is UNVERIFIED and should be read down.** The
  scan found **no quantified retrospective migration cost** in any surveyed system,
  and no evidence that Ensembl ever regretted its schema -- it says so explicitly
  rather than inventing a war story. The one verified later addition is SSSOM's
  set-level confidence field. So "adding a nullable column later is cheap" remains
  a plausible inference about Iceberg, not a precedent.

**The decision still stands** -- a strict alias is a fact, and these ten sources are
strict aliases rather than inferred mappings -- but if Chris reverses it, the thing
to add is a METHOD/ACTOR column pair, not a confidence float.
[The scan](research/external-prior-art-scan.md) is the evidence.

### Q2a. How is a WITHDRAWN or superseded alias expressed, given append-only? -- DECIDED: an `observation` field, so a row is an observation rather than a standing fact

**Blast radius:** MEDIUM
**THIS IS THE ONE PLACE THIS PRD GOES BEYOND CHRIS'S SETTLED DESIGN.** Section 6
specifies six columns and is silent on retraction. The first draft of this PRD
recorded that silence as Gotcha 6 -- "a wrong alias is permanent" -- and routed the
question to session `793ad471`, which is designing the KBDL obsolete-ID service and
therefore owns the adjacent rail. It answered, in detail, and the answer is adopted
here rather than parked, because the cheapest moment to add a column is before any
row exists and every clearinghouse genome table is 0 rows today. **If Chris
disagrees, this is the entry to strike**, and striking it is a small, clean edit --
see below.

**Why:** three reasons, in increasing weight.
(1) A `retracted_at` or `replaced_by` COLUMN is incompatible with append-only, not
merely awkward: stamping it means UPDATING an existing row, which append-only
forbids, so the alternative is a column nothing can ever set.
(2) **Reinstatement is real.** NCBI suppresses assemblies and later un-suppresses
them. A `retracted_at` timestamp makes withdrawal terminal; a sequence of
observation rows expresses `present -> withdrawn -> present` with no schema change
and no loss of history. This is the reason the column is actively WRONG rather than
just clumsy.
(3) `replaced_by` would badly duplicate what the table already says. Where an
authority names a replacement, that replacement IS another alias row, which
append-only already holds; where it names none, `replaced_by` is null -- and the
no-replacement case is the COMMON one, since 7,225 of 596,859 GTDB r220 genomes are
simply gone from NCBI with nothing standing in for them. A column optimised for the
rare case and null for the common one is the wrong shape.

**And it is this table's job, not the obsolete-ID service's**, because there are two
different obsolescences and the function goes where the KEY lives. INTERNAL
SUPERSESSION -- KBDL minted new bytes for the same logical thing -- is keyed on a
KBDL object id, is functional (`old_object_id` is a primary key in the live
`object_supersedes`), and its content equivalence is provable per object. EXTERNAL
RETRACTION is keyed on `(alias_source, alias_id)`, usually has NO successor, and is
only knowable by re-reading the upstream source. A retracted GTDB accession is not a
KBDL object id and has no `object_supersedes` edge, so it can never enter that
service's walk.

**If you disagree:** strike this entry, drop `observation` from
`_ENTITY_ALIAS_COLUMNS` and `ALIAS_OBSERVATIONS` from the schema module (t1),
revert the fill-only key from the five-tuple to the four-tuple and delete
`observe_alias` (t2), drop the latest-observation reduction and `current_only` from
the read surface (t3), and delete tests 7a, 10a, 13a and 14a. The table returns to
exactly section 6's six columns. **The cost of being wrong in this direction is
low**, which is worth knowing: because `null` reads naturally as `present`, adding
the field LATER is one of the few schema changes here that does not strand existing
rows -- so this is reversible in both directions, unlike Q1 or Q5.
**INDEPENDENTLY CORROBORATED, and by a reader that had not seen this entry.** The
external prior-art scan was dispatched against the SIX-column design, before Q2a
existed, and asked how surveyed systems express "this alias was once asserted and is
no longer true". It concluded unprompted that a positive-only table "answers 'ever
observed,' not 'currently valid'", that `observed_at` "dates the observation; it does
not encode withdrawal", that `SELECT DISTINCT` "cannot repair a wrong edge", and
that the escape route is **"an append-only event log with assert/retract
operations"** -- precisely what Q2a adopted. Prior art agrees: NCBI uses status and
history, GO distinguishes obsoletion from replacement, MDM products carry deletion
dates. It also supplies the warning that matters most for a retrofit: **"a future
nullable evidence column can describe new rows, but old evidence needs source replay
and may be unrecoverable"** -- the argument for doing this now rather than later, in
sharper form than I had it.
**Confidence:** raised from medium to HIGH on the shape. What remains uncertain is
only the vocabulary SIZE -- whether a third state (`superseded`, distinct from
`withdrawn`) earns its place once the obsolete-ID service exists. I deliberately did
NOT add one: two writers inventing states no reader can order is the failure mode,
and `ALIAS_OBSERVATIONS` is a code change away from growing.

### Q3. Should alias writes go through `register()` or their own verb? -- DECIDED: their own verb, but the REASON changed in round 1

**Blast radius:** MEDIUM
**THE ORIGINAL REASON IS GONE, and saying so matters more than keeping the
conclusion.** The first draft argued `register()` *structurally cannot* carry
aliases: it routes through `table_name(entity_type, kind)`, which refuses anything
outside `ENTITY_TYPES x _KINDS`. Round 1 made `alias` a KIND, so
`register(entity_type, rows, kind="alias")` would now resolve the table perfectly
well and that argument no longer exists.
**Why the verb stays separate anyway:** two narrower grounds that do survive.
(1) `register()` is PLAIN-APPEND and aliases are FILL-ONLY; nothing in `register()`'s
path consults existing rows, and folding a fill-only pre-read into it would change
the behaviour of the entity, content and result writes that share it.
(2) `register()` already branches on kind for the parameter set -- a `result` call
requires, validates, hashes and pre-writes a `parameter_set`. A third mode with
different idempotence semantics makes one verb carry three contracts, and the one it
would carry worst is the one with the subtlest invariant.
**If you disagree:** fold it in as `register(entity_type, rows, kind="alias")` with a
fill-only branch. That is now a genuinely available option -- it was not before --
and it would delete `register_aliases` while leaving `observe_alias` separate
regardless, since that one must NOT be fill-only. Weigh it as a real alternative
rather than a strawman.
**Confidence:** high on the conclusion, and this entry is now a DERIVATION whose
stated justification has been corrected once. That history is kept deliberately: a
conclusion that survives after its original argument collapses deserves more
scrutiny, not less.

### Q4. How do backfill presets declare aliases? -- DECIDED: a fourth manifest kind with a list-shaped `[[source.alias]]` block

**Blast radius:** MEDIUM
**Why:** D3.7 names `clearinghouse-backfill-v1`'s `arc` and `kbdl_hash` adapters as
writers, and those adapters are driven by TOML presets, so the manifest grammar has
to be able to express aliases or the named writers cannot write. It cannot ride
inside `[source.result]` the way the parameter set does, because the cardinality is
different: the parameter set is ONE extra value per source, whereas a source emits
one alias row per `alias_source` per entity. Hence a real fourth kind, and an array
block rather than a column map.
**If you disagree:** the alternative is bespoke Python per adapter, which means
every future alias writer re-implements validation and the vocabulary is enforced
in N places instead of one. Dropping this removes t6 entirely and leaves the
writers named in D3.7 unable to write without code changes in a repo this PRD does
not touch.
**ROUND 1 SIMPLIFIED THIS.** The fourth kind is no longer something this block has
to invent against the grain of the schema module -- `_KINDS` now contains `"alias"`
for Q1's reasons, so the manifest's own `KINDS` simply mirrors it, and `_plan_alias`
validates against the STANDARD `schema.column_names(entity_type, "alias")` that
`_plan_content` and `_plan_result` already use. The first draft needed a bespoke
accessor here purely because the table was not entity-typed; that special case is
deleted.
**Confidence:** medium. The fourth-kind decision is forced and now also consistent
with the schema module rather than additive to it, but **the exact TOML spelling is
still mine and no preset exists to validate it against** -- a reviewer who knows the
preset authors' habits should check the shape. This remains a DERIVATION beyond the
source design.

### Q5. What is the partition key? -- DECIDED: `alias_source`

**Blast radius:** MEDIUM
**Why:** It is the analogue of `source` on the five `<type>_result` tables and
`query_name` on `council.calls`. **Round 1 made it per-table rather than global**,
and it is now applied to ALL FIVE alias tables -- deliberately unlike
`_ENTITY_PARTITION_BY`, which partitions only the two high-volume types, because
every alias table is genuinely multi-source (that is what the table is for) and
`alias_source` is the filter essentially every read carries. Note the reinforcing
effect: a genome read now prunes on both the table and the partition -- the slice that keeps one vocabulary's files off
another's -- and `alias_sources` is the filter most reads will carry, so it prunes.
**If you disagree:** repartitioning a live Iceberg table is a full rewrite, and
`bootstrap()` will refuse the change rather than attempt it
(`BootstrapPartitionSpecMismatchError`), so reversing this after the table is
created means dropping and recreating it. Today that is free -- the table does not
exist. The alternative worth considering is `entity_type`, which has exactly five
values and therefore a bounded partition count; `alias_source` has ten today and
grows by code change.
**One more reversal cost, from the external scan, which makes this worse than "a
full rewrite" suggests:** Iceberg supports partition-spec EVOLUTION as a
metadata-only change, but **older data remains under the OLD spec and is not
reclustered**. A later repartition therefore does not fix the files already written;
it splits the table into two layouts and leaves the badly-clustered history in place
until somebody rewrites it deliberately.
**Confidence:** MEDIUM. The external scan was asked specifically for war stories on
partitioning a crosswalk by its source vocabulary and found **no benchmark and no
crosswalk-specific guidance at all** -- only general engine documentation on
partition boundaries and small-file costs, plus Delta's warning about small
partitions. So this remains an analogy to `source` and `query_name` with **nobody
having measured how the ten values will distribute, because there are no rows.** If
`kbdl_fasta_object` ends up holding 95% of them, this is a skewed partition that buys
little. Treat it as a workload assumption to be measured after the first real load,
not a settled fact.

### Q6. Is an alias row with no matching entity row an ERROR? -- DECIDED: no -- it is legal, and the integrity check is a non-failing report

**Blast radius:** LOW
**Why:** The alias facility is how an identifier arrives BEFORE the entity does: a
backfill learns an accession from a manifest before it has hashed the contigs.
Refusing that would force every writer to either buffer or discard real
information. The check that exists is the inverse -- count alias rows whose
`entity_hash` has no entity row, per source -- and it is a `health` report because
the operator response is to read it and go and talk to a producer, which they
cannot do if the verb raises. This is the OPPOSITE call from lake-5's orphan check,
which FAILS `verify` on a result hash missing from the registry, and the asymmetry
is the point: an opaque result is unrecoverable, an early alias is merely early.
**If you disagree:** adding a refusal later is a validator change in t2 and a
report-to-failure change in t4, plus deleting tests 15 and 17. It would also
require every writer to order its appends entity-first, which `register_aliases`
currently does not demand.
**Confidence:** LOWERED from high to MEDIUM by the external scan, which is the one
place it contradicted a design property rather than supporting it. Of the seven
properties put to it, legal orphans was **the only one with no validating
precedent**: relational cross-reference links and mapping-service failures "are not
evidence for orphan hash rows", and an entity still existing does not mean every
identifier link to it remains valid -- so the inverse health report answers a
different question from the one a reader might assume. The asymmetry with the
result-hash case is still well argued and the reversal is still cheap, so the
decision stands; what does not stand is any claim that others do it this way.

### Q7. Can one `(alias_source, alias_id)` pair identify more than one entity? -- DECIDED: treat it as a defect -- `resolve()` RAISES rather than choosing

**Blast radius:** LOW
**Why:** `resolve()`'s signature promises at most one `entity_hash`, and silently
returning the first of several is how a wrong genome gets enriched -- a failure
that is invisible at the call site and expensive downstream. Raising makes the
collision an operator's problem at the moment it is detected rather than a silent
wrong answer. Nothing REFUSES such a pair at write time, deliberately: the writers
cannot see each other, and refusing on write would make a legitimate concurrent
load fail.
**If you disagree:** if real alias vocabularies routinely map one external id to
several internal entities -- which the external scan was asked about directly --
then raising is wrong and the verb should return a LIST, renamed to say so. That
changes t3's signature and tests 13 and 14, and every caller. The related question
is whether `resolve()` should take an optional `entity_type` to disambiguate,
which would make a cross-type collision resolvable rather than fatal.
**Confidence:** medium -- the behaviour is right for the genome case this was
designed for, but whether many-to-many is normal in other alias vocabularies is
genuinely open. **Independently corroborated**, which raises this from a lone
judgement: session `793ad471`'s supersession resolver refuses a multi-child id and
names every child for the same stated reason -- there is no defensible precedence
rule, so guessing makes the answer depend on row insertion order. Two refinements
were taken from it and are now in the body: the exception names BOTH `entity_hash`
values, and it is a DISTINCT type (`AliasAmbiguousError`) so a caller can tell "I do
not know this id" from "this id is contested". Returning a list was considered and
rejected on its advice: a list makes every caller decide, and most callers will take
the first element. This is the third DERIVATION beyond the source design.

### Q8. Does the new CLI output bump `schema_version`? -- DECIDED: no, it stays at 1

**Blast radius:** LOW
**Why:** The output contract bumps `schema_version` only on a BREAKING change to
`data`. Two new verbs and one new key inside `health`'s `data` are additive:
nothing a consumer already parses changes shape.
**If you disagree:** bumping it forces every consumer to handle version 2 for no
benefit, and makes a future genuinely-breaking change indistinguishable from this
one.
**Confidence:** high -- it is the documented rule, applied.

### Q9. Does `alias_id` get stripped before storage? -- DECIDED: no -- the strip is the emptiness TEST only; the value is stored verbatim

**Blast radius:** LOW
**Why:** An identifier is owned by the system that minted it, and trimming it
changes it. But an `alias_id` that is empty after strip is the
`"kbdl:obj:None"`-class pointer-to-nothing the manifest's own comments warn about,
so emptiness must be tested against the stripped form.
**If you disagree:** storing the stripped form is one line, and it would make
`" GCF_1 "` and `"GCF_1"` the same alias instead of two. Changes `validate_alias`
in t1 and test 8.
**Confidence:** high -- but note this is a genuinely arguable call, and the test
exists precisely because the obvious implementation gets it the other way round.

### Q10. Is `ncbi_taxon` redundant with `genome_content.taxon_id`? -- DECIDED: yes, store it anyway, and declare it NOT INVERTIBLE

**Blast radius:** MEDIUM -- raised from NONE after the external scan
**Why:** One query shape for every identifier is worth more than the saved rows: an
operator asking "what do we know this genome by" should not have to know that nine
answers come from one table and the tenth from another. **But "redundant and
harmless" was WRONG, and that was a real defect.** A taxonomy id names a TAXON, not
an assembly -- NCBI's `assembly_summary` documentation separates the versioned
assembly identifier from the organism and species taxonomy ids for exactly that
reason -- so `ncbi_taxon` is a ONE-TO-MANY membership relation sitting in a table
whose reverse lookup promises at most one. Left unaddressed,
`resolve('ncbi_taxon', ...)` would raise `AliasAmbiguousError` for the ordinary case
of two genomes sharing a taxon, sending an operator to hunt a data defect that does
not exist. Hence `INVERTIBLE_ALIAS_SOURCES` and a REFUSAL rather than an ambiguity
error; see the Solution's invertibility section. `arc_analysis` is excluded for the
same reason and by its own description -- it is the grouping key across re-runs.
**If you disagree:** dropping `ncbi_taxon` removes one vocabulary entry, its
validator, its row and its exclusion from `INVERTIBLE_ALIAS_SOURCES`; the
invertibility machinery itself must STAY, because `arc_analysis` needs it
independently.
**Confidence:** high on the invertibility split; medium on `arc_record` and
`kbase_ws_ref` being invertible at all -- neither the scan nor this PRD could inspect
their version semantics, and moving a string between the two tuples is the whole
remedy if either turns out to be scoped differently.

### Q11. What I could not decide and did not guess

Two items. Neither blocks the build; both are recorded so that nobody later
mistakes them for having been settled.

1. **SETTLED SINCE THE FIRST DRAFT, and favourably:
   `kbaseincubator.fitness.assembly_hash` IS rule A.** Measured by session
   `368c658e` on 2026-10-26, n=3, by recomputing BOTH rules from the real bytes --
   fetch the assembly object, pull the FASTA from the blobstore behind
   `fasta_handle_ref`, run `standardizers.entity_hash('genome',
   parse_fasta_contigs(raw))` and `sha256(raw)`, and compare each to the stored
   value. Refs `262939/41/3` (1 contig), `262939/27/2` (16 contigs) and
   `262939/85/3` (7 contigs) all MATCH rule A; none matches rule B. **Two of the
   three are multi-contig**, so rule A's sort step is load-bearing in those cases
   and the stored hash still matches -- which a single-contig test could NOT have
   shown, because with one contig sorting is a no-op and the two rules differ only
   by header inclusion. So H3 is a DOCUMENTATION gap, not a live data problem, and
   `fitness.assembly_hash` already IS this table's `entity_hash`.

   **AND A METHOD CORRECTION THAT MATTERS MORE THAN THE ANSWER.** This entry
   originally said it was "settled by one `COUNT(*)`". **That was wrong**, and the
   same session proved it by running exactly that test: intersecting the 47
   `fitness.assembly_hash` values against KBDL's 4,865 `content_hash` values (rule B
   by construction) returned 0, and against `fasta_object_id` also 0. **Both zeros
   are UNINFORMATIVE**, because the corpora are DISJOINT -- 48 fitnessDB genomes in
   KBase workspace 262939 versus 4,865 KBDL-built genomes -- so an empty
   intersection was the expected result under BOTH hypotheses. Reported as
   "settled, not rule B" it would have read as authoritative and been unsupported.
   **A hash-rule question is settled by recomputing from the bytes, never by an
   intersection between corpora that may not overlap**, and the corpus-overlap
   precondition is the thing to check before believing any such intersection.

   **Scope of the claim, stated honestly:** n=3 of 47 is strong evidence and a
   refutation of "some rows are rule B", but it is not a proof over all 47. The
   remaining 44 cost roughly 5 MB of blobstore traffic each by that recipe. The
   parallel question for the GAA store is genome-identity-v1's Q12.2 and is still
   open there.

2. **CLOSED by the external scan, in the only way it honestly could be: there IS
   no published threshold.** It looked for a corpus-size or organizational point at
   which an in-code tuple should become a registry and found none -- and it also
   found **no evidence that identifiers.org or Bioregistry ever evolved FROM such a
   tuple**, saying plainly that "their existence alone does not establish that
   history". So the premise of my original question was itself unsupported. What it
   did establish is positive and actionable: those registries curate namespaces and
   review prefix changes OUTSIDE their consumers, so **a ten-value tuple is
   defensible, and the thing to preserve is the verbatim source spelling** -- which
   lets a registry govern the same keys later without rewriting a single row. That
   independently supports Q9 (store `alias_id` verbatim) and the bare-`alias_id`
   choice in Out of Scope. **And no surveyed tool replaces this table**: `curies`,
   `bioregistry`, `prefixmaps` and `pyobo` handle lexical namespaces and ontology
   ingestion, not biological identity or Iceberg persistence. `curies` is named as
   the best optional helper IF CURIE/URI conversion is ever needed, at the
   ingestion or export boundary only, with the explicit caveat that **normalization
   must not overwrite a verbatim `alias_id`**. The growth path stays "a code change
   and a review", now as a researched position rather than a default.

**No live measurement was taken by this session**, and three facts the design rests
on are therefore inherited records from 2026-10-25 17:07 and 17:36 CDT rather than
present-tense observations: the 19 objects in the namespace, the two precedent
schemas, and the 0-row genome tables. That is stated in the Research Report's
quality section and repeated here because it is the kind of thing that silently
becomes false.

## Gotchas and Unintuitive Consequences

These follow whether or not anyone changes a decision. None of them is a
restatement of an Open Question.

**1. Copying `parameter_set_orphans()` wholesale will break the `health` verb, and
it will look right.** It is the nearest precedent, its shape is correct, and its
POSTURE is the opposite of what is wanted: its docstring says findings are
"FAILURES and never warnings", and `_exit_on_integrity_failure()` exits non-zero on
any non-empty `report["failures"]`. An alias orphan is a legal state. A developer
who copies the method and keeps the `failures` key will make
`kbu clearinghouse health` exit non-zero during every normal bulk load, and
whatever monitors that verb will page on a correct system. This is why the report
deliberately has NO `failures` key and why test 15 asserts the exit code rather
than the report contents alone.

**2. `_assert_lake5_schema` passes the new table by accident, and "fixing" that
breaks every existing namespace.** The preflight iterates `table_configs()` and
`continue`s on any name that is neither `PARAMETER_SET_TABLE` nor `*_result`, so a
five new `<type>_alias` configs are all skipped with no change -- `*_alias` does not
match `*_result`. It LOOKS like a
guard that should learn about every new table. Teaching it would make every
namespace that has not yet run OP-C5 fail *result* ingest -- work that has nothing
to do with aliases. Test 18 is the guard against this specific helpfulness.

**3. The table can exist and be unwritable, and the error will not say so.**
`bootstrap()` creates tables that do not exist but **cannot add a column to one
that does**. If anyone hand-creates a `<type>_alias` with six columns, or with no
partitioning, `bootstrap()` will either verify it as present (column drift is not
checked) or refuse on the partition spec. The first case is the dangerous one: an
append of seven-column rows into a six-column table either fails obscurely or drops
a column's data. This is the same class of hazard that produced
`_assert_lake5_schema` in the first place, which is why `register_aliases` asserts
the table's existence and why OP-C5's acceptance step must read the live columns
and the partition spec, not just confirm the table is there.

**4. Partitioning on `alias_source` means a typo is permanent.** A misspelled value
that reaches `load()` creates an eleventh partition holding one row, and Iceberg
has no cheap way to remove it. The vocabulary check at the write boundary is the
only thing preventing that, which is why it is a refusal and not a warning, and why
test 3 checks the refusal behaviourally rather than asserting the tuple's contents.
The same hazard is why `@lower` exists for the result `source` column -- `BAKTA/1.9`
and `bakta/1.9` were two permanent partitions for one tool. **Note the asymmetry:
`alias_source` is NOT lowercased, because the vocabulary is closed and
case-exact, whereas `source` is open and had to be normalized.**

**5. Fill-only makes "nothing was written" and "everything was already there"
indistinguishable in the load result, and that already bit this codebase.**
`_load_failures()` exists because a batch refused by join assertions looked like a
clean idempotent no-op under fill-only. `register_aliases` returns `load_result:
None` when every row was present, which is the honest encoding -- but a caller that
checks only `load_result` will read a successful full-duplicate call as a failure
and a refused call as a success. Callers must read `written` and `already_present`.

**6. A withdrawal is a row, so "the alias set" and "the rows in the table" are not
the same thing -- and a naive reader gets stale answers that look fine.** Retraction
is now expressible (Q2a), which moves the hazard rather than removing it. Any query
that selects alias rows WITHOUT reducing to the latest `observation` per
`(entity_type, entity_hash, alias_source, alias_id)` will return withdrawn aliases
as though they were live, and it will do so silently and plausibly -- a withdrawn
NCBI accession is a perfectly well-formed accession. `resolve()` and
`aliases(current_only=True)` apply the reduction; raw SQL against the table does
not, and neither does `aliases()` by default, which returns history ON PURPOSE.
Anything downstream that joins a `<type>_alias` table, or `all_alias`, directly
must do its own reduction.
This is the standard cost of an append-only observation log and it is the right
trade, but it is a NEW failure mode that the six-column version did not have.

**6a. Reinstatement is the case that breaks the obvious implementation.** Fill-only
on the five-tuple makes an ordinary bulk retry free, and it ALSO makes a second
`present` row -- after a `withdrawn` -- look like a duplicate of the first one and
suppress it, losing the transition entirely. That is why `observe_alias` exists as a
separate verb that never applies fill-only, and why test 10a asserts a
`present -> withdrawn -> present` sequence survives. A developer who "simplifies"
the two verbs into one will pass every other test in this PRD and silently destroy
reinstatement.

**7. `kbdl_object` is multi-valued over time BY DESIGN, and that makes "the current
object" unanswerable from this table alone.** Enrichment mints a new KBDL object
and the old row stays, which is correct -- both objects existed and both are real
aliases. But a caller asking "what is the current CanonicalGenome object for this
genome" gets N rows ordered only by `observed_at`, and `observed_at` is when the
alias was OBSERVED, not when the object was minted. Those differ whenever a
backfill records an old object late. Anything needing currency must get it from
KBDL's own `object_supersedes` (28,051 rows), not from here.

**8. Storing rule B in `kbdl_fasta_object` makes the A/B relationship queryable,
and also makes it look like two identities for one thing.** That is the intended
benefit -- the relationship stops being folklore -- but a reader who joins
`genome_alias` to itself on `alias_id` across sources will find a 64-hex rule-B
value sitting in `alias_id` and a 64-hex rule-A value in `entity_hash` on the same
row, both sha256, both lowercase hex, with nothing in the data saying they are
computed by different rules over different bytes. That is precisely the join that
H3 says is waiting to fail silently, and t7's documentation is the only thing that
prevents it. **The documentation is load-bearing, not a nicety.**

**9. `resolve()` raising on a collision means a bad producer can break a good
caller.** If any writer ever records the same `(alias_source, alias_id)` against two
hashes, every `resolve()` for that pair starts raising -- including calls from code
that has nothing to do with the bad write. That is the intended trade (a loud stop
beats a silent wrong genome), but it means the orphan/integrity report should be
watched, because the first symptom of a duplicate-mapping producer will be an
unrelated job failing.

**10. The build can be fully merged and the feature still absent.** Nothing in the
taskplan creates the table in the lake; OP-C5 is an in-pod operator step and it is
not a build task. `kbdl-file-load-and-genome-sources-v1` is the live precedent --
every task merged, PRD still unfinished, because acceptance was an operator load
nobody ran. **A `done` status on this PRD before OP-C5 has run is a false claim.**

**11. Three workstreams are reading and writing `KBUtilLib main` right now, and
only two of them can see each other.** The council-record tranche, an
`/ai-conductor` run of `kbdl-job-arc-indexer-v1` landing from another machine
(believed kbhub, UNCONFIRMED) with phases still to go, and whatever builds this.
`main` moved 36 commits between the version the h100 install serves (`ec78ea7`) and
the version this PRD was written against (`b63eb6a2`), and it moved twice in two
minutes on 2026-10-25. **Anything cut from this PRD must re-read `main` in the same
minute it is cut.** This is a coordination hazard, not a design one.

**12. `fasta_reference` and `alias_id` are NOT two spellings of one value, and for
archive members they are not even the same KIND of thing -- so no prefix trick
joins them.** This gotcha was materially WRONG in the first draft, which said
stripping `obj://` and splitting the `#<entry>` fragment would bridge the join. It
will not, and the corrected version is the one that matters:

- **Standalone object.** `fasta_reference` is `obj://<fasta_object_id>` and
  `alias_id` under `alias_source = 'kbdl_fasta_object'` is the bare
  `<fasta_object_id>`. Here removing the scheme DOES recover the alias value; the
  only hazard is the prefix, and a direct join silently returns zero rows.
- **Archive member -- the part that was wrong.** `fasta_reference` is
  `obj://<archive_object_id>#<entry_name>`, which the preset's own comment calls
  "A POINTER INTO THE ARCHIVE, not to a standalone object: a genome in a
  GenomeArchive is one MEMBER of it". The adapter emits `archive_object_id` and
  `entry_name` separately and **emits no `fasta_object_id` for that record at all**.
  So stripping the scheme yields the ARCHIVE id, which is NOT the member's rule-B
  FASTA content hash and is **shared by every genome in that archive**. Treating it
  as a per-genome identifier produces empty joins or, worse, many-entity collisions.

**Therefore: never label an `archive_object_id` as `kbdl_fasta_object`.** Join an
archive member through `entity_hash`, or through an explicit
archive-and-member-to-member-hash mapping that does not exist yet. Building that
mapping is a separately scoped follow-on and is NOT in this PRD.

A silently-empty join remains the most likely way a downstream reader gets this
wrong, and it is the H1 failure mode reappearing one level down at the CURIE/URI
boundary -- which is why t7 must state the rule, in the corrected form.

## Sources Consulted

Everything below was read for this PRD. Items marked *(uninformative)* were read
and did not bear on the design -- listed so that an absence is visible and
correctable.

**The source design, which this PRD transcribes:**
- `miles` repo `agent-io/docs/2026-10-25-integration-vision-kbdl-clearinghouse-companions-arc.md`
  at commit `cfbb34f` (section 6 in full: D3.1-D3.8, the vocabulary table, the
  blast-radius table, and the could-not-decide list; plus section 2 for what is
  already decided, section 3's seam map, section 4's live measurements, section 5's
  H3, section 8's sequencing, section 9's exclusions). Readable copy at
  `~/Dropbox/AIPlatform/artifacts/integration-vision-kbdl-clearinghouse-companions-arc-2026-10-25/`.

**`KBUtilLib` source, all at `main` = `b63eb6a2`:**
- `src/kbutillib/domains/kbase/berdl/clearinghouse_schema.py` -- `TENANT`,
  `NAMESPACE`, `ENTITY_TYPES`, `_KINDS`, `_VIEW_KINDS`, `_ENTITY_COLUMNS`,
  `_RESULT_COLUMNS`, `PARAMETER_SET_TABLE` (:247) and its docstring,
  `_PARAMETER_SET_COLUMNS`, `_CONTENT_TYPE_HEAD` (for
  `ontology_term`'s `namespace`), `_ENTITY_PARTITION_BY`, `_CONTENT_PARTITION_BY`,
  `table_name`, `table_columns`, `column_names`, `parameter_set_columns`,
  `parameter_set_column_names`, `table_configs`, `union_view_sql`,
  `encode_entity_hash`, `BootstrapIndeterminateStateError`,
  `BootstrapPartitionSpecMismatchError`, `_normalize_partition_columns`,
  `bootstrap`.
- `src/kbutillib/domains/kbase/berdl/clearinghouse_capability.py` -- `register`
  (:1600) and its full guard contract, `_write_parameter_set_registry` (:1919),
  `results` (:883), `sources` (:1011), `parameter_sets` (:1109),
  `parameter_set_orphans` (:1175), `_assert_lake5_schema` (:2938), `_live_columns`,
  `_in_literal` (:3488), `_registry_representative_sql`, `_encode_hashes`,
  `_resolve_engine`, `_page_offpod`.
- `src/kbutillib/domains/kbase/berdl/clearinghouse_manifest.py` -- the derivation
  vocabulary and the `@prefix`/`@concat` null-preserving rationale (:175-215),
  `KINDS` (:224), `_plan_result` (:1153), `constant_result_source`,
  `_plan_parameter_set` (:1228), `PARAMETER_SET_COLUMN_KEY`.
- `src/kbutillib/interfaces/cli/clearinghouse.py` -- the output contract and
  `SCHEMA_VERSION` (:77), `_envelope`, `_build_health` (:1015), `health_cmd`,
  `_exit_on_integrity_failure` (:435), `_load_failures`, `_exit_on_load_failure`,
  and the `_build_*`-per-verb pattern.
- `src/kbutillib/domains/identity/standardizers.py` -- structure,
  `STANDARDIZER_VERSION = "1.1"`, `entity_hash`, `genome_hash_from_fasta`.
- `src/kbutillib/domains/kbase/berdl/clearinghouse_backfill.py` -- `Preset`,
  `available_presets`, `_target_tables`, `plan_units`, the adapter/slice/single
  unit kinds. Read for the preset mechanism Q4 depends on; *the `arc` and
  `kbdl_hash` presets themselves were NOT read in detail* -- see the Research
  Report's quality section.
- `tests/berdl/test_clearinghouse_schema.py` -- `TestParameterSetRegistry` (:397)
  in full as the template, plus the class list (`TestModuleConstants`,
  `TestTableName`, `TestTableConfigs`, `TestEntityHashRoundTrip`,
  `TestNoBinaryColumnSurvives`, `TestHexBridgeToStandardizers`,
  `TestParityFixtureMatchesResultSchema`, `TestUnionViewSql`).
- `agent-io/docs/clearinghouse-schema-operator-runbook.md` -- heading structure and
  the OP0 / OP1 / OP2 / OP2R / OP3 / OP-C1 / OP-C2 / OP-C3 / OP-C4 numbering, and
  the shape of OP-C1 as the model for OP-C5.
- Repo state: `git log ec78ea7..main` (36 commits), confirming the live
  coordination hazard and that `ec78ea7` is an ancestor of `b63eb6a2`.

**Sibling PRDs, by their recorded decisions rather than re-read in full:**
`clearinghouse-lake-5-convergence` (D5.1, D5.2, D5.6's fill-only rule, D5.16/D5.17,
D5.18, OP-C1/OP-C2), `clearinghouse-backfill-v1` (the `arc` and `kbdl_hash`
adapters, DB.11, G3), `genome-identity-and-hash-job-v1` (D4's md5 fence, Q12.2,
Q13's seqcol refusal), `kbdl-job-arc-indexer-v1` (Q1's post-hoc indexing, the
precedence ladder), `council-record-lakehouse-v1` (Q1's companion-namespace
reference, Q15), `kbdl-add-non-coding-to-existing-genome-v1` (Q3's resolver, the
driving consumer of `resolve()`), `koros-arc-store-v1` (the frozen three-form URI
vocabulary).

**Memory nodes bearing on this work:** `gtdb-r220-has-a-hard-ncbi-coverage-ceiling`
(the 7,225 / 596,859 figure), `codec-choice-is-set-by-the-deployed-read-path` (test
the running code), `a-symbol-from-a-comment-grep-is-not-the-api` (why every symbol
above was resolved from a definition, not a mention),
`taskplan-validator-rejects-same-repo-parallelism` (why this plan is seven
sequential phases), `full-suite-exceeds-subagent-bash-ceiling` (why test criteria
are targeted and regression-framed),
`provenance-inside-a-content-addressed-object-kills-idempotence` (why `observed_at`
and `ingest_batch_id` are outside the fill-only key),
`a-mutable-pointer-is-not-enough-when-ids-are-also-frozen` (Gotcha 7),
`check-ignore-lies-on-parking-branch-repos` and
`read-other-repos-with-git-dash-c` (why every read above used `git show main:`),
`design-artifacts-from-h100-are-invisible-to-chris` (why the readable copy exists).

**Measurements received from concurrent sessions, attributed because this session did
NOT take them:**
- Session `368c658e` (the genome companion namespace design), 2026-10-25:
  `kbaseincubator.fitness.gene_alias` exists, is `(gene_id, alias, type)`, and holds
  **ZERO rows** with no `observed_at`/`ingest_batch_id`. This CORRECTS the weight the
  first draft of the Research Report put on that precedent -- see the correction in
  finding 6.
- Session `793ad471` (the KBDL obsolete-ID service design), 2026-10-26 03:29Z: an
  independent reproduction of the codex credential failure
  (`maestro submit --backend codex` exiting 1 in under 60s on
  `task-9fba5f0e`), which is why that cause is recorded as a measured fact rather
  than this session's inference.

**Read and uninformative, listed deliberately:**
`clearinghouse_ingest.py`'s `key_columns_for_table` / `fill_only_survivors` /
`join_assertions` *(uninformative for this PRD -- they operate on the shard/ingest
path, which `register_aliases` does not use; a future alias-via-shards path would
need them)*. `clearinghouse_derivation.py`'s `current_state_sql` *(uninformative --
the alias table has no supersession window; it is append-only and read DISTINCT)*.
`clearinghouse_parameters.py` *(uninformative -- parameter-set rules do not apply
to aliases)*.

## Out of Scope

**Editing `clearinghouse-backfill-v1`'s presets to actually emit aliases.** This
PRD builds the facility and the manifest grammar that lets a preset declare
aliases; it does not edit `berdl/backfills/arc-kbdl.toml` or
`kbdl-genome-archives.toml`. Two reasons, and the first is the binding one:
`clearinghouse-backfill-v1` is **Jane's PRD and is at `ready`**, so changing its
presets is her call, and it is entangled with the H1 URI-spelling decision that is
blocked on her (continuation 199). The second is that those presets are one
operator command away from freezing a spelling permanently, which is not a thing to
touch in passing. **Consequence, stated plainly: when this PRD's tasks merge, the
table exists and nothing writes to it.** The writers are a follow-on.

**H1, the KBDL-object-URI spelling -- RESOLVED AND LANDED WHILE THIS PRD WAS BEING
WRITTEN, so it is no longer out of scope so much as no longer open.** Chris chose
option A on 2026-10-25, answering the H1 envelope this PRD's authoring session
raised, and it landed on `KBUtilLib main` as **`ed40f28`** at 22:54 CDT: the two
presets now emit `fasta_reference = @prefix(obj://,fasta_object_id)` and
`@concat(obj://,archive_object_id,#,entry_name)`, and `koros_arc_store`'s
`_OBJ_URI_RE` was widened to admit an optional non-empty `#<fragment>`. So there is
now **one** spelling for a KBDL object reference: `obj://<id>[#<member>]`.

**This PRD still stores `kbdl_object`'s `alias_id` BARE -- as `<id>`, with no
`obj://` prefix -- and the reason is now a positive one rather than
decision-avoidance.** `alias_source` and `alias_id` are already a (namespace,
identifier) pair, so putting `obj://` inside `alias_id` says "this is a KBDL object"
twice: once in the column that exists to say it, and once in the value. That is the
CURIE-versus-URI distinction, and the alias table is the CURIE side by construction.
`fasta_reference` is the URI side because it is a POINTER in a column with no
accompanying vocabulary column to disambiguate it.

**The consequence is a join asymmetry, and it is now a known one rather than a
deferred one -- see Gotcha 12.**

**Decision (2), the genome companion namespace.** Its own `d` session, downstream
of `kbdl-add-non-coding-to-existing-genome-v1` reaching a decision. It builds on
`fitness.assembly_hash`, which is why t7's documentation fix is ordered before it.

**Fixing any rule-B value that may already be in `kbaseincubator.fitness`.** t7
documents the A/B distinction. If a `COUNT(*)` shows real rule-B values in
`assembly_hash`, that is a live data problem and its own piece of work. See Q11.1.

**Retiring `v2_genome_aliases` or `gene_alias`.** The two existing alias tables stay
exactly as they are. `inner_loop` is the retiring side on its own schedule, and
`fitness.gene_alias` serves a live consumer. This PRD does not migrate either, and
does not backfill the alias tables from them.

**Retrofitting the existing 47,341 `function_entity` / `function_content` rows and
20,036 `function_result` rows.** D3.7: nothing is retrofitted. Those stay as they
are.

**Mutable retraction columns -- but NOT retraction itself.** This exclusion was
rewritten in confront round 1, because the first draft excluded retraction
wholesale and then Q2a added it, which is a flat contradiction a build agent cannot
resolve. The correct statement: **observation-based withdrawal and reinstatement are
IN SCOPE** (Q2a, Implementation Decisions 1-4). What is OUT of scope is a mutable
`retracted_at` timestamp and a successor-link `replaced_by` column -- the first
because stamping it requires UPDATING a row, which append-only forbids, and the
second because a named replacement is already just another alias row. **Q2a and
Implementation Decisions 1-4 supersede every earlier statement in this document
that the alias tables have no retraction.**

**Ahmed's Protein Clearinghouse.** Explicitly out per Chris, 2026-10-07, and
lake-5 D5.1.

**Retiring KBDL's own SQLite clearinghouse.** lake-5 D5.18, Chris's call.

**GA4GH seqcol as identity.** genome-identity-v1 Q13. `entity_hash` is 64-char
lowercase hex across every table and a seqcol digest is `sha512t24u`, so adopting
it is a column-contract change to every entity type. Worth re-costing periodically
and nobody has costed the curve.

## Further Notes

### This PRD cannot be registered from the machine that wrote it

It was written on **h100**, where two mechanical things are true and neither is a
bug in this document:

1. `register_prd` / `set_prd_status` do not work usefully from here. The h100
   `prd_registry.json` is a stale local copy holding 1 PRD against the Dropbox
   copy's 250, and the `ready` gate resolves the registered `prd_dir` to a git work
   tree and refuses when it cannot -- which it cannot, because every Dropbox
   checkout on this machine is parked on `wip` while this bundle is committed on
   `main`. **This PRD is therefore NOT registered.** Registration is a
   primary-laptop action and is named as such in the closing output.
2. A commit on `main` from h100 reaches no machine's working tree. That is why a
   byte-verified readable copy is written to a Dropbox path outside any git work
   tree, with a README saying where the real bundle is and why the copy must not be
   edited.

### Why the taskplan is seven sequential phases

The taskplan validator refuses parallel groups whose tasks share a repo, because
two developer worktrees on one repo collide on branches (dev 1032). Every task here
targets `KBUtilLib`, so the plan is seven phases of one task each. That is the
validator's rule applied, not a sequencing judgement -- t3 and t4 are genuinely
independent of each other and would run in parallel if the validator allowed it.

### No real-dependency smoke task, and why that is correct here

The schema-and-plan rules require an early smoke task against any external package
the repo does not already exercise. This PRD introduces **no new external
dependency**: it uses `capability.load()`, Spark and Trino, all of which are
exercised throughout the existing clearinghouse code and tests. The thing that is
genuinely unexercised is the LIVE table creation, and that is not a package API --
it is OP-C5, an in-pod operator step, and no build task can stand in for it.

### The acceptance step, named so it is not forgotten

**OP-C5, in-pod, after the tasks merge.** `bootstrap()` dry run showing
`action: create` for the FIVE `<type>_alias` tables ONLY and the sixteen others as
already present;
then the real run; then an acceptance read of the live columns AND the partition
spec (not merely that the table exists -- see Gotcha 3); then
`kbu clearinghouse health` exiting ZERO with `alias_integrity` present and an
orphan total of 0. It requires in-pod credentials on kbhub, so its route is
Albert's and his to state. **Until it has run, this PRD is `continue`, not
`finished`.**

## Acceptance Criteria

1. `clearinghouse_schema._KINDS == ("entity", "content", "result", "alias")` and `_VIEW_KINDS == ("all_entity", "all_content", "all_result", "all_alias")`; it declares `_ALIAS_COLUMNS` with exactly seven columns in DDL order (`entity_hash` first, `entity_type` second), `ALIAS_SOURCES` with exactly ten values and no `kbdl_genome_id`, `ALIAS_OBSERVATIONS == ("present", "withdrawn")`, `ALIAS_SOURCE_PATTERNS` with exactly four entries, `INVERTIBLE_ALIAS_SOURCES`, `ALIAS_SOURCES_BY_ENTITY_TYPE`, and `validate_alias()`.
1a. No `ENTITY_ALIAS_TABLE` constant and no `entity_alias_columns()` / `entity_alias_column_names()` accessors exist anywhere in the delivered code; `table_name(entity_type, "alias")` and `table_columns(entity_type, "alias")` serve those roles.
2. `table_configs()` returns twenty-one configs, with the five `<type>_alias` configs last, each carrying `partition_by == "alias_source"`.
3. `table_name(entity_type, "alias")` returns `<entity_type>_alias` for all five entity types; `union_view_sql("all_alias", ...)` names exactly those five tables and no others; `PARAMETER_SET_TABLE` remains absent from every union view and is still refused by `table_name()` in either argument position.
3a. `validate_alias()` refuses an `alias_source` that is not legal for the given `entity_type` per `ALIAS_SOURCES_BY_ENTITY_TYPE` -- specifically, `ncbi_assembly` on `protein` is refused while `arc_record` on `protein` is accepted.
4. `validate_alias()` defaults `observation` to `"present"`, refuses an unknown `alias_source` and an unknown `observation` naming the legal values in each case, refuses an `alias_id` that is empty, whitespace-only or `None`, and returns a whitespace-padded `alias_id` byte-identical.
5. Observation-based withdrawal and reinstatement are implemented; no `retracted_at` and no `replaced_by` column exists anywhere in the schema.
6. No statement anywhere in the committed PRD or the code comments asserts that the alias tables have no retraction mechanism.
7. `register_aliases()` is fill-only on the five-tuple, always writes `observation = "present"`, and makes ZERO `load()` calls on an identical retry, reporting every row as `already_present`.
8. `observe_alias()` always appends and never applies fill-only, so a `present -> withdrawn -> present` sequence persists all three rows.
9. Both write verbs generate a non-null UTC `observed_at` and a fresh ULID `ingest_batch_id` per call, and REFUSE a caller-supplied value for either rather than overriding it.
10. Every latest-observation reduction orders by `observed_at DESC, ingest_batch_id DESC, observation DESC`, making `withdrawn` win an exact provenance tie, and the result is stable across repeated calls.
11. Both write verbs validate every row before any append and refuse the whole call with `load()` uncalled on any invalid row, naming the offending row index.
12. Both write verbs refuse off-pod, run the write-target assertion, apply the postflight null-`row_count` check, and refuse with a message naming OP-C5 when `table_name(entity_type, "alias")` is absent.
12a. Both write verbs take `entity_type` and route through `table_name(entity_type, "alias")`; a row whose own `entity_type` disagrees with the argument is refused rather than coerced.
13. Neither write verb calls `_assert_lake5_schema`, and `_assert_lake5_schema` still passes against a namespace holding the sixteen pre-existing tables and none of the five `<type>_alias` tables.
14. `aliases()` returns the most recent occurrence of each observation state per four-tuple: after `present -> withdrawn -> present` it returns two rows, not three.
15. `aliases(current_only=True)` reduces to the latest observation per four-tuple and then excludes withdrawn winners, returning an empty result for a finally-withdrawn four-tuple.
16. `aliases()` applies no filter for `None` and matches nothing for `[]`, with the emitted SQL containing `1 = 0` and never `IN ()`, and composes its filters by `AND`.
16a. `aliases(entity_type=<t>)` reads the single `<t>_alias` table and `aliases(entity_type=None)` reads the `all_alias` view; `resolve()` accepts an optional `entity_type` and reads one table when given it.
17. The alias SQL builder emits placeholders and canonicalized params and performs no string interpolation of its own; inlining occurs only inside the existing `_run` / `_inline_hex_params` seam.
18. `resolve()` reduces per four-tuple, discards withdrawn winners, then counts distinct `(entity_type, entity_hash)` PAIRS across survivors: zero returns `None`, one returns that hash, more than one raises `AliasAmbiguousError`.
18a. `INVERTIBLE_ALIAS_SOURCES` is declared and excludes exactly `ncbi_taxon` and `arc_analysis`; `resolve()` refuses a non-invertible source with a distinct error naming `aliases()`, and that refusal is neither `AliasAmbiguousError` nor `None`, and fires even when only one row currently matches.
18b. Seeding one hex string under two different `entity_type` values for one `(alias_source, alias_id)` raises `AliasAmbiguousError`, proving pairs rather than hashes are counted.
19. `resolve()` returns `hB` for the sequence `hA present`, `hB present`, `hA withdrawn` on one `(alias_source, alias_id)` pair.
20. `AliasAmbiguousError` is not a subclass of `ValueError`, and its message names the pair and every `entity_hash` found.
21. `resolve()` raises `ValueError` for an unknown `alias_source` and returns `None` for a valid-but-unknown alias, and the two are distinguishable.
22. `alias_orphans()` returns a report carrying `orphans_by_source`, `orphan_total` and the reduced population size, and carrying NO `failures` key.
23. `alias_orphans()` counts only four-tuples whose latest observation is `present`: a withdrawn orphan yields `orphan_total == 0`, and the same alias re-observed as present is counted.
24. `kbu clearinghouse health` exits ZERO with a non-zero alias orphan count present, and still exits NON-ZERO when a parameter-set integrity finding is present.
25. The alias orphan report appears in the health envelope as `data.alias_integrity` and never in `warnings`.
26. `kbu clearinghouse aliases` and `kbu clearinghouse resolve` each emit exactly one envelope under the existing output contract in both human and `--json` modes, with no exception escaping as a traceback.
27. `kbu clearinghouse resolve` exits 0 for a valid lookup with no current mapping, and exits 1 for `AliasAmbiguousError` and for an invalid `--alias-source`.
28. An unpassed `--alias-source` is not collapsed to `[]`, and repeated values reach the capability as a list.
29. `SCHEMA_VERSION` is still 1, and every pre-existing `health` envelope key is still present with `alias_integrity` added.
30. `clearinghouse_manifest.KINDS` includes `"alias"`, and a preset declaring a list-shaped `[[source.alias]]` block plans successfully and yields a `<entity_type>_alias` `TablePlan` validated against `schema.column_names(entity_type, "alias")` with no alias-specific special case.
31. The manifest refuses, at PLAN time, an unknown `alias_source`, an unparseable `alias_id` derivation, a mapping of any sharder-filled column including `observation`, and `"alias"` declared in `kinds` with no block.
32. Planned alias rows carry `observation = "present"` unconditionally, and a preset cannot declare `observation`.
33. A null-producing `@concat` in an alias block drops the row and emits no `alias_id` containing the literal string `"None"`.
33a. OP-C5 creates five tables and one view, and its dry run reports `action: create` for exactly those five and `already present` for the pre-existing sixteen.
34. The operator runbook contains an `OP-C5` section after `OP-C4`, following `OP-C1`'s dry-run / real-run / acceptance structure, whose acceptance step reads the live columns AND the live partition spec and states why existence alone is insufficient.
35. The operator runbook states that rule A is the clearinghouse identity and rule B is a KBDL-local artifact id, that `kbaseincubator.fitness`'s `assembly_hash` columns declare neither rule in their own documentation, and that KBDL's `genome_id` is literally `fasta_object_id[:16]`.
36. The operator runbook states the corrected `obj://` pointer rule: that removing the scheme recovers `alias_id` only for a standalone `obj://<fasta_object_id>`, that `obj://<archive_object_id>#<entry_name>` yields an archive-and-member locator and NOT the member's FASTA hash, and that an `archive_object_id` must never be labelled `kbdl_fasta_object`.
37. No test criterion anywhere in the delivered work requires a fully green test suite; every test-related criterion is regression-framed.
