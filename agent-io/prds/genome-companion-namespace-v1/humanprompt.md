# The genome companion namespace — what this is, in one page

**Decision (2), designed, attacked, and folded.** A new lakehouse namespace,
`kbaseincubator.genome_companion`, built on the `kbaseincubator.fitness` scheme you named — with its
fourteen defects decided, 21 tables, three read projections, one in-pod loader, and `KBDLStoreLoad`'s
submission path retired.

**It has been through a cross-family adversary and come out the other side.** The confront returned
18 stall points and opened with *"do not start production loading from this PRD."* All 18 are folded.
Three of them reshaped the design rather than polishing it, and those three are the most important
thing on this page.

## The three things the adversary broke, and what they are now

**1. The snapshot contract was impossible. → Q1, and it re-keyed every table.**
The first draft said every table was append-only, *and* that `genome_id` was a primary key, *and*
that idempotence was keyed on the snapshot. On a second delivery those cannot all hold: appending
duplicates the key, skipping loses enrichment, replacing breaks append-only. Worse — I had *cited*
`council-record-lakehouse-v1`'s snapshot-versioned keys as the precedent and then not adopted the
mechanism. Now: every science row is an immutable snapshot version keyed
`(store_id, snapshot_id, logical_key)`, and a current read picks **one** fully-reconciled snapshot
**before any join**.

**2. `assembly` could not hold what I claimed. → Q3.**
I wrote that the key was `assembly_entity_hash` *and* that one entity hash "may front several content
hashes". One row holds one content hash. Now `assembly` is **one row per FASTA representation**, and
`contig` is scoped to the representation — because contig ids belong to a FASTA form, and a header
change preserves the order-independent hash while changing them.

**3. The sharder would have silently deleted your data. → Q6.**
I said "reuse `clearinghouse_shard.py` unchanged with `genome_id` as the sort key". Its key spec does
*entity* dedup, so that would have **discarded all but one gene per genome**. "Sorting by genome is
not deduplicating by genome." Now there is an explicit per-table key and routing map, and dedup is on
full snapshot-scoped keys only.

## And one thing I measured during the fold that nothing had noticed

**`genome_id` is not a hash.** The draft said it was a truncated content hash — so did the briefing I
was handed. It is `unk` + a **contiguous decimal ordinal**: `unk1` … `unk4865`. That matters because
equal-width *lexicographic* ranges over it are badly skewed (`unk1` captures 1, 10–19, 100–199,
1000–1999 — measured 1111 ids versus 111 for `unk5`), and string sorting puts `unk10` before `unk2`.
Parsing the ordinal and routing on the **integer** is both correct and *better* than hashing: a dense
ordered counter gives near-even shards and near-perfect pruning. Two decisions had the wrong stated
reason; both conclusions survived.

## What else is worth knowing before you read it

**`fitness` is not unpartitioned by oversight — the writer is the defect.** No partition clause on any
of it, and the layout is already broken at 48 genomes: `genome` holds 48 rows in **48 files**,
`function_annotation` 1.1M rows in **2,880 files**. Our write path cannot request an Iceberg transform
or a file-size property at all, so the lever is the writer — and `512 MB` is now treated as an
*uncompressed budget*, with the layout **measured** through the real ingest path rather than asserted.

**Two of your fourteen items dissolved on measurement.** `evidence` *is* linked and is worthless:
2,504,043 rows reconciling exactly to the sum of its parents, zero non-null scores, not even a unique
key. `fitness.annotation` is not merely wide — **all 272,076 rows are orphans.** Both dropped.

**Your assembly-scoped decision met a real counterexample and survived.** `SyringaeB728a` and
`SyringaeB728a_mexBdelta` — a knockout mutant and its wild-type parent — share a byte-identical
assembly. The hash stays assembly-scoped; it just cannot be the genome's key.

**The assembly-hash ambiguity is a documentation gap, not a data problem.** I fetched three
assemblies from your workspace 262939 and recomputed both rules over the real FASTA: rule A matched
all three, rule B none, and two were multi-contig so the sort step was load-bearing. `fitness`
already stores the clearinghouse's canonical hash.

## What to read, and where to push back

**25 judgement calls: 7 IRREVERSIBLE, 14 MEDIUM, 3 LOW, 1 terminal.** Read Q1–Q7 first — those are
the irreversible ones. **Q6 is the single highest-risk entry**: it is IRREVERSIBLE and the only one of
the seven at medium confidence, because its file-size target is a budget rather than a measurement
until the rehearsal runs.

To change anything, edit its `DECIDED:` line and run `--review`.

## Two questions that are yours, and one gap I own

**How many genomes are to be enriched** (blocked continuation 180). No table shape depends on it, but
it decides whether the first load is 4,865 rows or 2,000,000 — i.e. whether the sharding is exercised
on day one.

**Whether anything reads `fitness` or `inner_loop`.** I measured contents, not consumers, so Q20 now
promises *no change at all* to either rather than a compatibility window I could not size.

**My own flaw in how I ran the confront:** the worker's worktree is off `main`, which carries neither
this PRD nor the sibling PRDs it cites, and I did not embed the taskplan — the report says so itself.
Some of its "absent from this checkout" complaints were my dispatch, not its misreading. The valid
core is folded regardless. If a round 2 goes out, the taskplan goes with it.
