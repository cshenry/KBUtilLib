# Work record — clh2-t1-capability-read

**Task:** Build the READ half of `ClearinghouseCapability`.
**Branch:** `maestro/developer/task-clh2-t1-capability-read`
**Worktree:** `/mnt/homes/chenry/.maestro/worktrees/clh2-t1-capability-read`
**Base commit (worktree HEAD at task start):** `6689ddd` (koros_arc_store reference fake).
**Date:** 2026-09-24

## What was built

### New module — `src/kbutillib/domains/kbase/berdl/clearinghouse_capability.py`
`ClearinghouseCapability`: the one sanctioned, read-only surface over the
fifteen `kbaseincubator.clearinghouse` tables. **Composition, not
inheritance** — it wraps a `BerdlCapability` (accepting one, or constructing
one lazily) and deliberately does NOT subclass it, so the raw
`query()`/`load()` transport surface never appears on the same object as the
clearinghouse verbs. Mirrors the wrap-not-subclass posture of
`ClearinghouseBootstrapCapability`. `issubclass(ClearinghouseCapability,
BerdlCapability)` is `False` (asserted in module import check).

Public read surface (writes are a later task):
`known`, `content`, `content_all_types`, `results`, `current_state`,
`sources`, `stats`, `tables`.

The five hard rules, and where each lives:
1. **`entity_type` mandatory positional** on every read verb except `stats`,
   `tables`, `content_all_types` (the three cross-cutting verbs).
2. **Table names ONLY via `clearinghouse_schema.table_name()`** — routed
   through `_fqn(entity_type, kind)`; no verb accepts a table name from a
   caller.
3. **Per-type batch split** — enforced structurally: the entity type is a
   single positional arg, so a caller physically cannot hand one call a
   mixed-type batch. Chunking happens strictly within a type. Rationale
   documented in the module docstring: `_standardize_protein` and
   `_standardize_gene` are the same function, so identity is
   `(entity_hash, entity_type)`.
4. **Every hash through `encode_entity_hash()`, bound as a parameter** —
   `_encode_hashes()` runs at the boundary before any SQL is built (so a
   malformed digest raises before any query). In-pod the hash travels in
   `params` (server-side bind, never in SQL text). Off-pod there is no bind
   channel, so `_bind_offpod()` inlines the value ONLY after re-validating it
   against `_HEX64_RE` (`^[0-9a-f]{64}$`) — provably injection-free.
5. **`current_state`/`results` wrap `current_state_sql()`** — resolving
   `result_table_fqn` via `_fqn(entity_type, "result")`, not passing
   `entity_types` (redundant on a single-typed table), not hand-rolling the
   window function.

Named constants (with measured rationale in their docstrings):
- `SPARK_PROMOTION_THRESHOLD = 4000` — records the off-pod-vs-in-pod
  inversion (in-pod Trino beats Spark 8× at K=1000; Spark leads only
  marginally at K=4000) and both caveats (fixed-cost-in-K downgraded to
  unproven; measured on a 203M-row table vs the clearinghouse's ~1B–4B).
- `OFFPOD_PAGE_CAP = 5000` — documents that the true server page cap is
  encoded by no code we own (grep confirms no such constant in the berdl
  package), adopted from an earlier spike's assertion; paging keeps going on
  an exactly-full page and stops only on a short page.

Locus-dependent chunking: `_chunk_size()` returns `OFFPOD_PAGE_CAP` off-pod
and `None` (no chunking) in-pod, picked from the RESOLVED locus.
`_run_hash_lookup()` chunks within a type, pages each chunk to completion
off-pod, and deduplicates the reassembled result (`_dedupe_rows`), so a
caller passing 50000 hashes gets one deduplicated answer.

`stats()` derives row counts from an explicit `SELECT COUNT(*)` per table —
never from a transport `row_count` field (off-pod `row_count` is the
materialized page size). `include_files` is in-pod only; off-pod it returns
without file fields and with a non-empty `warnings` list.

### Edited — `src/kbutillib/domains/kbase/berdl/capability.py`
`BerdlCapability.query` gained a keyword-only `params: Sequence[Any] | None`.
It is honoured ONLY on the in-pod Trino path
(`cursor.execute(sql, params)` — true server-side binding); passing `params`
on the in-pod Spark path or off-pod raises `ValueError` (neither can bind
server-side, and the method never interpolates a bind value into SQL text).
The in-pod Trino path now returns **dict rows keyed by column name** (from
`cursor.description`) instead of bare positional tuples, matching the shape
off-pod (`{'data': [dict]}`) and Spark (`Row.asDict()`) already use — the
only in-pod Trino consumer of `query()` today is the new reader, and the
Spark path (used by the bootstrap adapter) is untouched.

### New tests — `tests/berdl/test_clearinghouse_capability.py`
48 tests against a fake capability (no pod, no network, no Spark) that
records every `query()` call. Coverage of the seven success criteria:
- (a) each verb hits the `table_name()`-named table, all five types
- (b) two-type batch → two queries against two tables (structural: no verb
  takes a set of types); unknown type raises before any query
- (c) uppercase and lowercase hex bind to the identical parameter (lowercase)
- (d) malformed digest (5 shapes) raises before any query
- (e) in-pod SQL text carries no 64-char hex run (`_HEX_RUN_RE` asserted
  absent; hashes in `params`, `?` placeholders in SQL); off-pod inlines only
  validated hex
- (f) off-pod 12000-hash lookup chunked to ≤ page cap and reassembled
  without duplicates (incl. cross-page-boundary dedup); in-pod 12000 not
  chunked (one query, whole batch bound)
- (g) `stats()` uses COUNT(*): a fake whose envelope `row_count` is the
  sentinel `5000` (the page-size trap) never leaks into any reported count

Plus engine-default/promotion, `tables()`, `sources()`, derivation-wrap, and
empty-batch short-circuit tests.

## Verification

Command and exit code (quoted):

```
PYTHONPATH=$PWD/src /home/chenry/venvs/kbdl/bin/python -m pytest tests/berdl/ -q
→ 275 passed in 5.66s ; EXIT: 0
```

(227 pre-existing berdl tests + 48 new.)

```
PYTHONPATH=$PWD/src /home/chenry/venvs/kbdl/bin/python -m pytest tests/berdl/test_clearinghouse_capability.py -q
→ 48 passed ; EXIT: 0
```

### Full-suite regression analysis (compare by NAME)
Full suite: `36 failed, 3349 passed, 282 skipped, 1 xfailed, 31 errors`.
The 36 failed + 31 errored are all in `tests/biochem/`, `tests/modeling/`,
`tests/core/` (escher, composition smoke, cli, gapfill, flux-loops, lp
solver) — optional-dependency / environment failures (missing
`python-graphql-client`, ModelSEED database path mismatch, duplicate
capability registration). **None import the berdl reader or
`capability.query`.**

The cached baseline `build-baselines/kbutillib.json` was captured at a
DIFFERENT commit (`0f8480…`) in a richer environment where those optional
deps were present, so a naive name-diff shows 49 "new" failures — an
environment artifact, not a regression. I verified this directly: I set my
changes aside (temporary WIP commit, then reverted the working tree to the
base file content), ran the flagged tests at base state, and they FAIL
IDENTICALLY without any of my changes:

```
# at base state (my changes reverted):
pytest tests/modeling/test_find_flux_loops.py::...::test_returns_short_path \
       tests/core/test_cli_cap.py::TestCapRun::test_unavailable_cap_exits_nonzero \
       tests/modeling/test_comprehensive_gapfill_wrapper.py::...model_grows \
       tests/biochem/test_escher_utils.py::TestGetFluxDirection::test_forward_flux
→ 3 failed, 1 error   (same failures as with my changes)
```

Then restored my work and re-confirmed `tests/berdl/` is 275 passed.

**Conclusion:** no test that passed at the base commit fails because of this
change. Success = the tests this task adds pass AND no previously-passing
test regresses — both hold.

## Off-pod live-read verification — NOT PERFORMED (assumption unmarked)
The task asked for one real off-pod read if a token is available.
- Current locus in this worktree: `off_pod` (`berdl_notebook_utils` not
  importable here).
- No BERDL/KBase/Polaris/Trino token is present in the environment (checked
  `env` for `berdl|kbase|polaris|trino|spark|token|KB_` — none set).
- Therefore no live off-pod clearinghouse read could be attempted.

**The off-pod claim remains UNPROVEN end to end.** This matches
`OffPodTransport`'s own docstring ("currently unproven end to end"). The
off-pod degradation path (validated inlining via `_bind_offpod`, self-paged
chunking) is implemented and unit-tested against a fake, but has never run
against the live REST surface. Anyone relying on off-pod reads must perform
that live verification before trusting them.

## Deferred / out of graded scope
- **CLI** (`src/kbutillib/interfaces/cli/clearinghouse.py`) is absent on the
  base commit and is NOT built here. The task's seven success criteria cover
  the capability module only ("capability-read"); the CLI is a separate
  surface and is deferred to a later task. Noted so a reviewer expecting a
  CLI knows it was a deliberate scope decision, not an omission.
- **Write methods** are explicitly a later task and are not built.

## Files changed
- `src/kbutillib/domains/kbase/berdl/clearinghouse_capability.py` (new)
- `tests/berdl/test_clearinghouse_capability.py` (new)
- `src/kbutillib/domains/kbase/berdl/capability.py` (edited — `query`
  `params=` seam + dict-row Trino return)
- `agent-io/work-records/clh2-t1-capability-read.md` (this record)
