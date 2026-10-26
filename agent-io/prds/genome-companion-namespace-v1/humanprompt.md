# The genome companion namespace — what this is, in one page

**Decision (2), designed.** A new lakehouse namespace, `kbaseincubator.genome_companion`, built on
the `kbaseincubator.fitness` scheme you named, with its fourteen defects decided before any row is
written. Seventeen tables, one in-pod loader, and `KBDLStoreLoad` retired unbuilt.

## The four things worth knowing before you read the PRD

**1. `fitness` is not unpartitioned-by-oversight — the writer is the defect, and we already own the fix.**
`SHOW CREATE TABLE` shows no partition clause on any of it, and the file layout is already broken at
48 genomes: `genome` holds 48 rows in **48 files**, `function_annotation` 1.1M rows in **2,880
files**. At ATB scale `gene` is ~10⁹ rows. Our write path *cannot* request an Iceberg transform or a
target file size — `table_configs()` emits only a name, a schema and one bare partition column — so
the lever is the writer, and `clearinghouse_shard.py` already implements exactly the right
range-cutting sharder. The design reuses it rather than inventing a second answer.

**2. Two of your fourteen items dissolved on measurement, and one was worse than we thought.**
`evidence` *is* linked — the three annotation tables point at it — and it is worthless: 2,504,043
rows reconciling exactly to the sum of its parents, **zero** non-null scores, an `evidence_type`
that just repeats the parent's `tool`, and not even a unique key. Dropped. `fitness.annotation` is
not merely "wide and tool-shaped": **all 272,076 of its rows are orphans** against `gene`. Dropped.

**3. Your assembly-scoped decision meets a real counterexample in live data — and survives it.**
`fitness` holds 48 genomes over 47 distinct assembly hashes. The colliding pair is
**`SyringaeB728a` and `SyringaeB728a_mexBdelta` — a knockout mutant and its wild-type parent,
byte-identical assemblies.** The hash stays assembly-scoped; what follows is that it **cannot be the
genome's primary key**. `genome_id` is the key, `assembly_entity_hash` is a many-to-one foreign key.
A loader that keyed on the hash would have merged a mutant into its parent on row 48 of the smallest
table.

**4. The assembly-hash ambiguity is a documentation gap, not a data problem. Measured.**
This was flagged as the one unmeasured item decision (2) inherits, with the note that one rule-B row
would make it a live data problem. It is settled, and the suggested row-count test could never have
settled it — the `fitness` and KBDL corpora are disjoint, so the intersection is empty under *either*
rule. I fetched three assemblies from your workspace 262939 and recomputed both rules over the real
FASTA bytes: **rule A matched all three, rule B none**, and two of the three are multi-contig so the
sort step is load-bearing. `fitness.assembly_hash` already *is* the clearinghouse's canonical genome
entity hash, so the companion namespace joins to `clearinghouse.genome_entity` with no translation.

## What it costs you to disagree

Three decisions are IRREVERSIBLE after the first load and are the ones to read first:
**Q1** (genome keyed on `genome_id`, not the assembly hash), **Q2** (two separately-named hash
columns, no column called `assembly_hash`), **Q3** (partition only on low-cardinality columns; size
files in the writer). **Q3 is the one with medium confidence** — its ~512 MB target is transferred
from another corpus rather than measured on this one.

## One gap in this document's provenance, stated plainly

**It has not been adversarially attacked.** The cross-family confront step did not run: `codex` on
h100 has a dead credential (refresh token already used, then HTTP 401, then no `OPENAI_API_KEY`),
confirmed by two independent dispatches. The invariant forbids substituting a Claude adversary for a
Claude-written PRD, so it was skipped rather than faked, and the external research scan was
re-dispatched same-family with that disclosed. Every decision here is checked against *measurement*;
none is checked against a hostile reader. Fixing the pool needs an interactive codex login.

## What it does not touch

Not the four-element architecture (that is the sibling vision session's). Not
`clearinghouse.entity_alias` (decision (3), its own PRD, being written in parallel). Not
`inner_loop`'s ~20 modelling tables, which have no `fitness` equivalent and would be dropped on the
floor by a blanket "retire `inner_loop`". Not rewriting `fitness` in place — I could not find what
wrote it, and say so.

## Two things I could not decide, both yours

**How many genomes are to be enriched** (blocked continuation 180). It changes no table shape, but
it decides whether the loader's first run is 4,865 rows or 2,000,000 — i.e. whether the sharding is
exercised at all on day one.

**Whether anything reads `fitness` today.** I measured its contents, not its consumers, so the
retirement window in Q12 is asserted rather than evidenced.
