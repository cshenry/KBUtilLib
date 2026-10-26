# clearinghouse-entity-alias-v1

The identifier-carriage facility for the BERDL clearinghouse: one generic
`entity_alias` table, its closed `alias_source` vocabulary, its write boundary,
its read surface including a reverse lookup, and its health report.

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
tables, the three union views, and `parameter_set`. **`entity_alias` is the same
shape one step further on**, which is why this is a small change rather than a new
pattern.

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

**10. `bootstrap()` is the sanctioned way to add the seventeenth table to a
namespace that already holds sixteen, and it is already safe for this.** It is an
idempotent create-or-verify: it creates tables that do not exist and verifies the
partition spec of those that do, raising `BootstrapPartitionSpecMismatchError`
rather than attempting an unsupported live partition-spec change, and
`BootstrapIndeterminateStateError` rather than treating an ambiguous existence
probe as "absent". It explicitly **cannot add a column to an existing table** --
which does not matter here, because `entity_alias` is a wholly new table.

**11. `_assert_lake5_schema` does not need changing, by luck rather than by
design.** It iterates `table_configs()` and skips any name that is neither
`PARAMETER_SET_TABLE` nor `*_result`. A seventeenth config named `entity_alias`
falls through that filter untouched. This is worth stating because the preflight
*looks* like something that should learn about every new table, and teaching it
about `entity_alias` would make every existing namespace fail ingest until an
operator ran the new table's creation step. See Gotchas.

### Links to full research output

- [External prior-art scan](research/external-prior-art-scan.md) -- **NOT PRESENT AT
  COMMIT TIME. Read this before relying on the link.** The scan was dispatched three
  times and the first two produced nothing:
  `task-c06a5d7a` (codex) died in 35s on an expired credential, and `task-c9a6e008`
  (claude) spent its entire 1200s budget researching -- 85+ tool calls, reaching
  exactly the right material, including Ensembl's `info_type` patch timeline and the
  UniProt `delac_sp`/`delac_tr` deletion lists -- and was killed having written
  nothing. **That second failure was a defect in the dispatch prompt, not in the
  research**: it never told the worker to write early, so a perfect uncommitted
  report was worth nothing. `task-8a36f389` was re-dispatched with an explicit
  write-first-and-commit-often instruction and a 2400s budget. If this link is dead
  when you read it, that third attempt also failed and the external scan is simply
  missing.
  **What it was asked, and therefore what is still unverified:** Ensembl
  `xref`/`object_xref` and specifically what `info_type`/`info_text` are FOR (bears
  directly on Q2); UniProt idmapping; NCBI `gene2accession`; OBO
  `alt_id`/`replaced_by` obsolescence modelling (bears on Q2a); identifiers.org and
  Bioregistry on closed-vs-open vocabularies (bears on Q11.2); whether one external
  id routinely maps to several internal entities (bears on Q7); partitioning war
  stories for a crosswalk table keyed on its source vocabulary (bears on Q5); and
  whether `curies`/`bioregistry`/`pyobo` are real candidates or overkill.

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

**THE EXTERNAL HALF OF THE RESEARCH STEP DID NOT LAND, and that is the single
biggest gap in this document.** Everything above is the internal corpus, read
directly. No literature, no prior art in other systems, and no reusable-tool survey
informed this draft. Four entries are weaker than they would otherwise be and each
says so in its own `Confidence` line: Q2 (the missing confidence column -- Ensembl's
`info_type` is the direct precedent and went unread), Q2a (the observation model --
OBO's `alt_id`/`replaced_by` is the direct precedent and went unread), Q5 (the
partition key -- no war stories), and Q11.2 (closed-vs-open vocabulary -- the
identifiers.org/Bioregistry experience is exactly the evidence missing). **A reader
should treat those four as internally-reasoned rather than externally-corroborated.**

**Confidence the design should carry: HIGH on everything except Q2, Q2a and Q5.** The
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

One new table, `kbaseincubator.clearinghouse.entity_alias`, plus the accessors,
validators, write boundary, read surface, CLI verbs and health report that make it
usable. It is the seventeenth clearinghouse table and the second one that is not
entity-typed.

```
entity_alias
  entity_type     STRING      -- one of ENTITY_TYPES
  entity_hash     STRING      -- 64 lowercase hex, canonical identity (rule A)
  alias_source    STRING      -- the vocabulary the identifier belongs to; PARTITION KEY
  alias_id        STRING      -- the identifier itself, verbatim
  observation     STRING      -- one of ALIAS_OBSERVATIONS: 'present' | 'withdrawn'
  observed_at     TIMESTAMP
  ingest_batch_id STRING
```

Seven properties define it, and each is there for a stated reason:

**It is generic over entity type, not genome-only.** Proteins, genes, functions and
ontology terms have the same problem -- `ontology_term` has alternate ids,
`function` has tool-local names. A genome-only table is a smaller change today and
four more tables later. Genericity is expressed as a COLUMN (`entity_type`), not in
the table's name, exactly as `parameter_set` is one table and not five.

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
| `ncbi_taxon` | the taxon id, decimal string | `^[1-9][0-9]*$` | deliberately redundant with `genome_content.taxon_id`: one query shape for every identifier |
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
    the seventeenth table in a namespace that already holds sixteen, so that the
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
22. As a **future maintainer**, I want `entity_alias` to be absent from
    `ENTITY_TYPES`, un-nameable by `table_name()`, and absent from all three
    `all_<kind>` union views, so that it cannot leak into the per-entity query
    surface.
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

Add, following the `PARAMETER_SET_TABLE` precedent in the same module line for
line:

- **`ENTITY_ALIAS_TABLE = "entity_alias"`** -- a module constant, NOT built by
  `table_name()`, for the documented reason that `table_name()` composes
  `f"{entity_type}_{kind}"` and validates both halves against `ENTITY_TYPES` and
  `_KINDS`, and the alias table is neither an entity type nor a kind.
- **`_ENTITY_ALIAS_COLUMNS`** -- the seven `(name, sql_type)` pairs in DDL order,
  declared once so the DDL, the accessors and the tests read one definition.
- **`ALIAS_OBSERVATIONS = ("present", "withdrawn")`** -- a closed two-value
  vocabulary, and the DEFAULT for every bulk write is `"present"`. Keep it a tuple
  with a validator for the same reason `ALIAS_SOURCES` is one: an open observation
  vocabulary would let a writer invent a third state that no reader knows how to
  order.
- **`entity_alias_columns()`** and **`entity_alias_column_names()`** -- separate
  accessors mirroring `parameter_set_columns()` / `parameter_set_column_names()`,
  for the same reason those are separate from `table_columns()`: routing the alias
  table through `table_columns()` would mean inventing a pseudo entity type, which
  would then leak into `ENTITY_TYPES`, the union views, and every caller that
  iterates the five types.
- **`ALIAS_SOURCES`** -- a module-level tuple of the ten values, in the order the
  Solution table lists them.
- **`ALIAS_SOURCE_PATTERNS`** -- a mapping from `alias_source` to a compiled
  shape regex, containing ONLY the four sources that have one. A source absent
  from this mapping is opaque by declaration. **The mapping must not be
  defaulted to a permissive pattern**, because "opaque" and "validated by a
  pattern that happens to match everything" are different claims and only the
  first is honest.
- **`validate_alias(entity_type, entity_hash, alias_source, alias_id,
  observation="present") -> tuple`** -- the one validator, returning the
  canonicalized five-tuple or raising. Rules: `entity_type` in `ENTITY_TYPES`;
  `entity_hash` through the existing `encode_entity_hash` 64-hex contract;
  `alias_source` in `ALIAS_SOURCES`; `observation` in `ALIAS_OBSERVATIONS`;
  `alias_id` non-empty after `.strip()` and, where the source has one, matching
  its pattern. **`alias_id` is stored verbatim, not stripped** -- the strip is the
  emptiness TEST, not a transformation, because silently trimming an identifier
  changes it.

`table_configs()` then emits **seventeen** configs: the existing sixteen in their
existing documented order, then the `entity_alias` config LAST, carrying
`"partition_by": "alias_source"`. Its docstring and the module docstring must be
updated from "sixteen" to "seventeen" and `PARAMETER_SET_TABLE`'s own docstring
from "the sixteenth clearinghouse table, and the only one that is not
entity-typed" to "the sixteenth ... and one of two that are not entity-typed".

**`ENTITY_TYPES`, `_KINDS` and `union_view_sql` are NOT touched.** The alias table
appears in no `all_<kind>` view.

### 2. The write boundary (`ClearinghouseCapability.register_aliases`)

A new public verb, because `register()` structurally cannot carry it:

```
register_aliases(rows, *, tenant=TENANT, dataset=NAMESPACE) -> dict
```

`rows` is a sequence of mappings each carrying `entity_type`, `entity_hash`,
`alias_source`, `alias_id`. `observed_at` and `ingest_batch_id` are filled by the
writer exactly as the other write paths fill them.

It is modelled on `_write_parameter_set_registry()` -- a direct
`capability.load()` with `tables=[{"name": ENTITY_ALIAS_TABLE, "mode": "append"}]`
and rows projected through `entity_alias_column_names()` -- and it inherits
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
distinct five-tuples already present for the `(entity_type, alias_source)` slices
the batch touches, and append only the rows not already there. Return a report
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
the `entity_alias` table exists in the write namespace, and refuse with a message
naming the runbook step if it does not -- the same posture `_assert_lake5_schema`
takes for an un-rebuilt result table, for the same reason: an append into a table
that is not there either fails obscurely or creates one with the wrong spec.

### 3. The read surface (`ClearinghouseCapability`)

```
aliases(entity_type=None, entity_hash=None, *, alias_sources=None,
        alias_ids=None, engine=None) -> list[dict]
```

Four filters composed by `AND`. **The semantics are `results()`'s, stated
exactly**: `None` applies no filter; `[]` is a request to match nothing. For the
list-valued filters use the existing `_in_literal()` helper, which already yields
`1 = 0` for an empty list rather than an invalid `IN ()`. `entity_hash` goes
through `_encode_hashes` and is **parameter-bound**, never inlined, exactly as
every other hash lookup in the class. `alias_sources` is the partition column, so
it prunes.

Returns one dict per distinct five-tuple plus `observed_at` and `ingest_batch_id`.
**`SELECT DISTINCT` on the five-tuple**, with `MAX(observed_at)` and a
corresponding `ingest_batch_id`, because the table is fill-only and a duplicate
row is expected and harmless -- a reader that returned duplicates would be
surfacing an implementation detail as data.

`aliases()` returns the OBSERVATION HISTORY, not just current state: both the
`present` and the `withdrawn` rows come back, each with its `observation` value.
That is deliberate -- it is the verb an operator uses to ask "what happened to this
accession", and collapsing it to current state would throw the answer away. A
`current_only=True` keyword applies the latest-observation reduction for callers
that want just the live set.

```
resolve(alias_source, alias_id, *, engine=None) -> str | None
```

The reverse lookup. It resolves against the **LATEST observation** for the pair: if
that observation is `withdrawn`, there is no current mapping and it returns `None`.

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

`resolve` prints the hash, or reports "no entity" and **exits 0** -- "this alias is
not known" is a successful answer to the question asked, not a failure. It exits
non-zero only on the collision `ValueError`.

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
  acceptance read): create `entity_alias` in a namespace that already holds
  sixteen, via `bootstrap()`, which is idempotent and will report the sixteen as
  already present.
- **H3's documentation fix**, which has no other home and is one paragraph: state
  in `agent-io/docs/clearinghouse-schema-operator-runbook.md` that **rule A is the
  clearinghouse identity and rule B is a KBDL-local artifact id**, and that
  `kbaseincubator.fitness`'s `genome.assembly_hash` / `assembly.assembly_hash`
  declares neither today. Record also that **KBDL's `genome_id` is literally
  `fasta_object_id[:16]`**, which is why the vocabulary has no `kbdl_genome_id`
  source.
- **The `obj://` prefix rule**, because Gotcha 12 has no other home: state that
  `alias_id` is stored BARE while `genome_content.fasta_reference` carries
  `obj://<id>[#<member>]` as of `ed40f28`, that a direct join between them returns
  zero rows, and that the archive-member form needs its `#<entry>` fragment split
  off as well as the scheme stripped.
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

1. **`table_configs()` returns seventeen, with `entity_alias` last and partitioned
   on `alias_source`.** The count is the regression guard: a developer adding a
   table and forgetting the config is the failure.
2. **`entity_alias` is in no `all_<kind>` view and `table_name()` refuses it in
   either argument position.** Mirrors the registry test. This is what keeps it out
   of the per-entity query surface.
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
12. **`aliases()` returns one row per distinct five-tuple** when the stub returns a
    duplicated row, so fill-only's duplicates do not leak to a reader.
13. **`resolve()` returns `None` for an unknown alias but RAISES `ValueError` for an
    unknown `alias_source`.** The two must be distinguishable; conflating them is
    the bug this test exists for.
13a. **`aliases()` returns HISTORY by default and current state on request.**
     With a `present` and a later `withdrawn` row for one four-tuple, the default
     call returns BOTH, and `current_only=True` returns only the reduced latest.
     A default that silently reduced would throw away the operator's answer to
     "what happened to this accession".
14. **`resolve()` RAISES on two distinct hashes for one pair**, and the message
    names both. See Q7.
14a. **The contested raise is a DISTINCT exception type.** Assert
     `AliasAmbiguousError` specifically, and assert it is NOT what an unknown
     `alias_source` raises and NOT what a not-found returns. Conflating "unknown"
     with "contested" is the defect; a test on the type is what keeps them apart
     through a later refactor.
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
    against a namespace holding the sixteen old tables and NOT `entity_alias`, and
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

### Q1. Is `entity_alias` ONE generic table carrying `entity_type`, or one alias table per entity type? -- DECIDED: one generic table

**Blast radius:** IRREVERSIBLE
**Why:** Protein, gene, function and ontology_term have the same problem --
`ontology_term` has alternate ids, `function` has tool-local names -- so a
genome-only table is a smaller change today and four more tables later. Genericity
as a COLUMN rather than in the name follows `parameter_set`, which is one table and
not five, and whose docstring already argues the case: making a non-entity-typed
table pretend to be an entity type puts it in the per-type views and in every
caller that iterates the five types.
**If you disagree:** a per-type scheme means five tables named by `table_name()`, a
sixth `_KINDS` member `"alias"`, inclusion in a fourth `all_alias` union view, and
`entity_type` dropped from the column list. In this PRD it changes t1 entirely, the
seventeen-config count in t1's tests, `register_aliases`'s signature in t2 (it
would take `entity_type` and route through `table_name`), both read verbs in t3,
and the manifest's block shape in t6. Once rows exist, converting a generic table
to five means a full rewrite and two read paths during the transition.
**Confidence:** high -- this is the source design's call at high confidence, and
the `parameter_set` precedent makes it cheap.
**A sibling design depends on this answer, so it is stated here explicitly:** the
genome companion namespace (decision (2), session `368c658e`) asked whether its own
`genome_alias` table should be a PROJECTION of `entity_alias` rather than a second
source of truth. **Genericity over `entity_type` is firm and will not be narrowed
to genome-only**, so a projection is the right END STATE. It is deliberately not
the right thing to build yet, for three reasons that are properties of THIS design:
the join key is undeclared (`fitness.assembly_hash` follows neither hash rule -- see
Q11.1), `entity_alias` has no retraction (Gotcha 6), and an alias whose entity does
not exist is legal (Q6), so a projection hands a consumer orphan rows that are
correct by design. The recommendation sent to that session was therefore: build
`genome_alias` separately now, but spell the columns identically
(`alias_source`, `alias_id`, `observed_at`, `ingest_batch_id`) so convergence is
later a VIEW rather than a rewrite.

### Q2. Should `entity_alias` carry a `confidence` or `asserted_by` column? -- DECIDED: no such column

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
**Confidence:** LOW -- and this is the entry the design session itself recorded as
the one it was least sure of. **It is the top thing to look at**, and the cheapest
moment to reverse it is before any row exists. The external prior-art scan was
asked specifically what Ensembl's `info_type`/`info_text` are FOR and what becomes
impossible without them; read
[that scan](research/external-prior-art-scan.md) before accepting this.

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
**Confidence:** medium. The ARGUMENT is strong and I did not originate it; what I am
less sure of is whether a two-value vocabulary is enough, or whether a third state
(`superseded`, distinct from `withdrawn`) earns its place once the obsolete-ID
service is built. I deliberately did NOT add a third value: two writers inventing
states no reader can order is the failure mode, and `ALIAS_OBSERVATIONS` is a code
change away from growing if it needs to.

### Q3. How do alias rows get written, given that `register()` cannot name the table? -- DECIDED: a dedicated public `register_aliases()` verb

**Blast radius:** MEDIUM
**Why:** `register()` selects its table via `table_name(entity_type, kind)`, which
raises `ValueError` on anything outside `ENTITY_TYPES` x `_KINDS`. This is the same
constraint that made `parameter_set` rows go through
`_write_parameter_set_registry()` instead, and that method -- a direct
`capability.load()` with an explicit table name and rows projected through the
table's own column-name accessor -- is the shape to copy. Widening `register()` to
accept a table name would turn its "the table is never caller-supplied" guarantee
into "sometimes it is", which is a safety property worth more than the saved verb.
**If you disagree:** the alternative is a `kind="alias"` sentinel threaded through
`table_name()`, which means `_KINDS` grows, `table_columns()` must special-case it,
and `table_name("genome", "alias")` starts returning `genome_alias` -- a table that
does not exist. That lands in t1 and t2 and changes the test in t1 that asserts
`table_name()` refuses the alias table in either argument position.
**Confidence:** high -- it is forced by a specific `ValueError` in existing code,
and it is a DERIVATION this PRD added, not a call the source design made. Flagged
as such.

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
**Confidence:** medium -- the fourth-kind decision is forced, but the exact TOML
spelling is mine and no preset exists to validate it against. A reviewer who knows
the preset authors' habits should check the shape. This is the second DERIVATION
this PRD added beyond the source design.

### Q5. What is the partition key? -- DECIDED: `alias_source`

**Blast radius:** MEDIUM
**Why:** It is the analogue of `source` on the five `<type>_result` tables and
`query_name` on `council.calls` -- the slice that keeps one vocabulary's files off
another's -- and `alias_sources` is the filter most reads will carry, so it prunes.
**If you disagree:** repartitioning a live Iceberg table is a full rewrite, and
`bootstrap()` will refuse the change rather than attempt it
(`BootstrapPartitionSpecMismatchError`), so reversing this after the table is
created means dropping and recreating it. Today that is free -- the table does not
exist. The alternative worth considering is `entity_type`, which has exactly five
values and therefore a bounded partition count; `alias_source` has ten today and
grows by code change.
**Confidence:** MEDIUM, and the reason is honest: it follows `source` and
`query_name` BY ANALOGY, and **nobody has measured how the ten values will
distribute, because there are no rows.** The external scan was asked for war
stories on partitioning a crosswalk table by its source vocabulary. If
`kbdl_fasta_object` ends up holding 95% of rows, this is a skewed partition that
buys little.

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
**Confidence:** high -- the asymmetry with the result-hash case is well argued in
the source design, and the reversal is cheap.

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

### Q10. Is `ncbi_taxon` redundant with `genome_content.taxon_id`? -- DECIDED: yes, and store it anyway

**Blast radius:** NONE
**Why:** One query shape for every identifier is worth more than the saved rows.
An operator asking "what do we know this genome by" should not have to know that
nine answers come from one table and the tenth from another.
**If you disagree:** drop one vocabulary entry and its row; nothing else moves.
**Confidence:** high.

### Q11. What I could not decide and did not guess

Two items. Neither blocks the build; both are recorded so that nobody later
mistakes them for having been settled.

1. **Whether any `kbaseincubator.fitness` row already carries an `assembly_hash`
   computed under rule B rather than rule A.** The design session read the
   *columns*, not the *values*. If even one row is rule B, H3 is a LIVE DATA PROBLEM
   and not merely a documentation gap, and decision (2) -- the genome companion
   namespace, which builds on exactly that column -- inherits it. This PRD's t7
   documents the distinction, which is the right action either way, but
   documentation does not fix wrong values. **Settled by one `COUNT(*)` against
   `fitness.genome` joined to a known rule-A hash, or more cheaply by whoever wrote
   those rows saying which function they called.** The parallel question for the GAA
   store is genome-identity-v1's own Q12.2 and is open there too.

2. **How the ten-value vocabulary should grow, if it outgrows a code change.**
   `identifiers.org` and Bioregistry exist because closed in-code prefix lists did
   not scale, and this PRD does not know at what scale that becomes true here. The
   external scan was asked for the threshold and for whether there is a migration
   path that does not rewrite rows. Until that comes back, the closed tuple is the
   call and the growth path is "a code change and a review" -- which is the right
   answer for ten values and may be the wrong one for a hundred.

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
seventeenth config named `entity_alias` is skipped with no change. It LOOKS like a
guard that should learn about every new table. Teaching it would make every
namespace that has not yet run OP-C5 fail *result* ingest -- work that has nothing
to do with aliases. Test 18 is the guard against this specific helpfulness.

**3. The table can exist and be unwritable, and the error will not say so.**
`bootstrap()` creates tables that do not exist but **cannot add a column to one
that does**. If anyone hand-creates `entity_alias` with five columns, or with no
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
Anything downstream that joins `entity_alias` directly must do its own reduction.
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
`entity_alias` to itself on `alias_id` across sources will find a 64-hex rule-B
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

**12. Joining `entity_alias.alias_id` to `genome_content.fasta_reference` requires
adding the `obj://` prefix, and neither column says so.** As of `ed40f28`,
`fasta_reference` holds `obj://<id>` for a bare object and
`obj://<archive_id>#<entry>` for an archive member, while `alias_id` under
`alias_source = 'kbdl_fasta_object'` holds the bare `<id>`. Both are correct for
their own column (see Out of Scope), but a consumer joining them directly gets ZERO
ROWS -- not an error, not a type mismatch, just an empty result that looks like
"we have no aliases for these genomes". The archive-member case is worse than a
prefix mismatch: `fasta_reference` may carry a `#<entry>` fragment that has no
counterpart in `alias_id` at all, so even after stripping `obj://` the join needs
the fragment split off. **A silently-empty join is the single most likely way a
downstream reader gets this wrong**, and it is exactly the failure mode the H1
unification was done to prevent, reappearing one level down at the CURIE/URI
boundary. t7's documentation must state the prefix rule explicitly.

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
does not backfill `entity_alias` from them.

**Retrofitting the existing 47,341 `function_entity` / `function_content` rows and
20,036 `function_result` rows.** D3.7: nothing is retrofitted. Those stay as they
are.

**A tombstone / retraction mechanism.** See Gotcha 6. No `retracted_at`, no
`replaced_by`. If it is needed it is a schema change, and nobody has decided to
need it.

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
`action: create` for `entity_alias` ONLY and the sixteen others as already present;
then the real run; then an acceptance read of the live columns AND the partition
spec (not merely that the table exists -- see Gotcha 3); then
`kbu clearinghouse health` exiting ZERO with `alias_integrity` present and an
orphan total of 0. It requires in-pod credentials on kbhub, so its route is
Albert's and his to state. **Until it has run, this PRD is `continue`, not
`finished`.**
