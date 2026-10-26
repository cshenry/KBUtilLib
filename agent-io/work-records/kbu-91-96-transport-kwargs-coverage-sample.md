# Work Record — kbu-91-96-transport-kwargs-coverage-sample

## task_id
kbu-91-96-transport-kwargs-coverage-sample

## branch
maestro/developer/kbu-91-96-transport-kwargs-coverage-sample

## base
`5d83908` (main at submit time)

## commit_shas
- `b56001c` — fix(berdl): InPodTransport passes only kwargs the installed BERDL helper accepts (refs #91)
- (this record's commit) — fix(clearinghouse): name the unresolved keys in the coverage-floor refusal (refs #96)

## summary

Two independent fixes, serial, one per commit.

**#91.** `InPodTransport` passed kwargs the BERDL image dated 2026-10-19 no
longer accepts, so every `BerdlCapability.query` (both engines) and every
clearinghouse verb touching Spark raised `TypeError` before doing anything. The
transport now reads the *installed* helper's signature with `inspect.signature`
at call time and passes only what it accepts, through one private helper shared
by both methods. Dropping is per-kwarg policy, not blanket: `delta_lake` and
`use_hive` go at any value, `tenant_name` only when `None`, `connector` only
when `'iceberg'` — anything else is refused with a `ValueError` naming the
kwarg, its value and KBUtilLib#91, raised before the helper is called. The same
method also raises `spark.sql.session.localRelationCacheThreshold` to 64 MiB on
the returned session, which is the verified workaround for the AddArtifacts RPC
failure that killed every non-trivial backfill.

**#96.** `join_assertions`' coverage-floor refusal counted the unresolved keys
but never named them, so an operator could only see *which* ones by lowering
the floor — bypassing the gate to inspect what it caught. The report now
carries `unresolved_sample` (sorted, ≤20) and `unresolved_sample_truncated`,
and the below-floor message lists the sample. `unresolved_keys` is still the
integer count it was.

## files_touched

Commit 1:
- `src/kbutillib/domains/kbase/berdl/transports.py` — `_DROPPABLE_WHEN_UNACCEPTED`
  (:83), `_accepted_kwargs` (:100), `InPodTransport.spark_session` (:239),
  `InPodTransport.trino_connection` (:393)
- `tests/berdl/test_transports.py` — new, 16 tests

Commit 2:
- `src/kbutillib/domains/kbase/berdl/clearinghouse_ingest.py` —
  `UNRESOLVED_SAMPLE_LIMIT` (:414), `_unresolved_clause` (:437),
  `join_assertions` coverage computation (:559-575), below-floor message
  (:613-624), report dict (:638-639)
- `src/kbutillib/domains/kbase/berdl/clearinghouse_capability.py` — the
  `_join_assertions` ValueError fallback report (:2781), which hand-writes the
  report's full key set; extended with the two new keys at `None`
- `tests/berdl/test_clearinghouse_ingest.py` — 5 new tests

Nothing else was touched. In particular `capability.py`'s own docstring at :724
still says `get_trino_connection` "defaults to the legacy connector", which is
now image-dependent — left alone as out of the task's file list. Likewise
`council_rehearsal.py:1672-1680`, whose t11 finding text states that the
refusal "only counts them" and that "its report carries no list of the
[unresolved keys]"; that is now stale, but the council modules are explicitly
out of scope. Both are worth a follow-up.

## success_criteria_check

**1. Kwarg filtering through one helper driven by `inspect.signature`.** PASS.
`_accepted_kwargs(helper, kwargs, where=...)` at `transports.py:100` is the
only filter; `spark_session` (`:239`) and `trino_connection` (`:393`) both call
it with `self._bnu.get_spark_session` / `self._bnu.get_trino_connection`, read
at call time, never at import. `delta_lake`/`use_hive` are dropped when
unaccepted whatever their value; a non-`None` `tenant_name` and a
non-`'iceberg'` `connector` raise `ValueError` naming the kwarg, its value and
`KBUtilLib#91` *before* the helper is called (asserted by
`test_new_image_spark_session_refuses_a_non_none_tenant_name` and
`test_new_image_trino_connection_refuses_any_other_connector`, both of which
assert the recorded call list is empty). Old-image behaviour is asserted by
value, not call count, in `test_old_image_spark_session_passes_every_kwarg_unchanged`
and `test_old_image_spark_session_defaults_are_unchanged_too`. The threshold is
asserted as `"67108864"` in two tests.

**2. NO-INJECTION / NO-PERMISSIVE-FAKE.** PASS, and checked by reverting.
Both fake images in `tests/berdl/test_transports.py` are plain `def` functions
with the exact signatures given in the task and **no** `**kwargs`. I replaced
`transports.py` with its `HEAD` content (`git show HEAD:<path>`, file swapped
aside and restored — deliberately not `git stash`, which is shared across
worktrees in this environment) and re-ran the new file:

```
11 failed, 5 passed in 1.53s
E  TypeError: _new_image.<locals>.get_spark_session() got an unexpected
   keyword argument 'delta_lake'
```

All 11 new-image tests fail with `TypeError`; the 5 that still pass are the
old-image ones, which is correct — the old image is what the original code was
written for. The fixed file was restored and the suite returns `16 passed`.

**3. The new-image path through `BerdlCapability`.** PASS. Four tests drive the
real `BerdlCapability` over a real `InPodTransport` (only `._bnu` faked):
`test_capability_query_trino_works_against_the_new_image` covers the
`transport.trino_connection(connector="iceberg")` call site at
`capability.py:784`, plus the Spark engine branch at `:783`,
`BerdlCapability.spark_session()` at `:350`, and the old image through the same
caller. The transport is pre-seeded onto `capability._transport`, which
`_get_transport` returns untouched and which satisfies `query`'s
`isinstance(transport, InPodTransport)` test. `BerdlCapability.spark_session()`
additionally gates on `locus()`, so that one test forces locus in-pod the way
`tests/berdl/test_capability.py`'s `force_in_pod` fixture does.

**4. The join report names the unresolved keys.** PASS. `unresolved_sample` is
`sorted(distinct_keys - held, key=str)[:20]`, `unresolved_sample_truncated` is
`len(unresolved) > 20`, both `None` when coverage was not checked;
`unresolved_keys` is unchanged and asserted `isinstance(..., int)`. The
below-floor message appends `"Unresolved (first 20 of 25): ..."` (or
`"Unresolved: ..."` when the sample is complete) via `_unresolved_clause`; the
zero-resolve WRONG-JOIN-KEY message is untouched and a test asserts it carries
no `Unresolved` clause. All five new tests call the real `ci.join_assertions`
on a real `pyarrow` table built by the file's own `_arrow` helper — nothing is
monkeypatched.

**Grep for exact-equality consumers (commit 2's instruction).** Done, and
reported here as asked. No test anywhere compares a whole join report dict for
equality — every consumer reads individual keys
(`tests/berdl/test_council_publish_lakehouse.py:894`,
`tests/domains/test_ontomap_pipeline_rehearsal.py:1352/1416/1418`,
`src/kbutillib/interfaces/cli/clearinghouse.py:209/465`,
`council_publish.py:576`). The one place with a *fixed key set* is
`clearinghouse_capability.py:2767-2790`: `_join_assertions` catches a
`ValueError` from `join_assertions` and hand-builds a report with every key
spelled out, so a reader of that report would have seen the new keys present on
the success path and absent on the refusal path. Extended with both at `None` (`:2782-2783`)
(consistent with `coverage_checked: False` there).

**5. `tests/berdl` suite.** PASS, and it is clean — no pre-existing failures to
separate out.

```
$ TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src ~/venvs/kbdl/bin/python \
    -m pytest tests/berdl -q -p no:cacheprovider --no-header
1568 passed, 6 skipped in 32.56s        # exit 0
```

Also run, because they consume the join report:

```
tests/domains/test_ontomap_pipeline_rehearsal.py    55 passed in 7.31s   # exit 0
tests/cli/test_clearinghouse*.py                   129 passed in 1.57s   # exit 0
```

No full-suite run: `build-baselines/kbutillib-h100-main-6310d61.json` records
the full `tests/` tree at 2665–3416 s, 4.8× the 600 s tool ceiling, and its own
`WHY_DURATION_MATTERS` field says a sub-agent must run a targeted selection
instead. That baseline's `failures_by_area` lists biochem, cli, modeling,
domains/ai and core — **no berdl entry**, consistent with the clean 1568-test
run above. The `cli` area does carry 19 known failures there, which is why I
ran the three clearinghouse CLI modules specifically rather than `tests/cli`
wholesale; all 129 pass.

**6. Two commits, subjects as given, "refs" not "fixes".** PASS — see
`commit_shas` above.

**7. Work record.** This file.

## reconciliation against the tree

The prompt was accurate almost throughout. Divergences, all minor:

- **`transports.py` line numbers.** `spark_session` is at `:142` (prompt said
  `:142-167` — correct), `create_namespace_if_not_exists` at `:169` (correct),
  but `trino_connection` is at `:262` with the body at `:271-280`, not
  `:278-280`. Found by name; no behavioural consequence.
- **`trino_connection`'s `connector` is a required positional parameter**, not
  a keyword with a default — the prompt describes it only as "passes
  `connector=`". Signature kept exactly as it was, per the prompt's own
  instruction to leave the public signatures alone.
- **`clearinghouse_ingest.py` line numbers were all correct** against the base
  commit: `join_assertions` at `:428`, the coverage computation at `:519-530`,
  the below-floor message ending at `:574` (prompt said `:566-575`), the report
  dict at `:577-594`. Nothing needed reconciling here.
- **`kbu_compat.py`** at `~/Dropbox/Jobs/function-to-go/albert-load-artifacts/`
  was not read — the prompt fully restated the rule, and I implemented that
  statement rather than porting a file. Nothing was imported or vendored.
- **`build-baselines/kbutillib.json` exists** (2026-09-13, `0f84803`) alongside
  the newer `kbutillib-h100-main-6310d61.json` (2026-10-19). The newer one has
  no `known_failing` array at all — it points at
  `/scratch/chenry/build-baselines/kbutillib-6310d61-failed.txt` instead, which
  is outside the worktree. Moot here: `tests/berdl` is fully green on this
  branch, so there is no failure to classify.

## inferred rather than verified

- The new BERDL image's exact signatures are taken from the task prompt. I did
  not read the installed package — it is pod-only and not present on h100 — so
  the fakes encode the prompt's claim, not a harvest. If the real
  `get_spark_session` differs from the seven parameters listed, the filter
  still behaves correctly (it reads whatever is installed); only the *tests*
  would be describing the wrong image.
- That 64 MiB is sufficient for a real backfill batch, and that
  `conf.set` is permitted on a Spark Connect session at that point, come from
  the prompt's report of the 19,268-row load. Unverified here.
- `ruff` is not installed in `~/venvs/kbdl`, so no lint run. `E501` is in the
  repo's ruff ignore list (`pyproject.toml:360`) so long lines are not a
  concern; style was matched to the surrounding modules by hand.

Pod leg: awaiting operator acceptance on kbhub (deploy via Albert).
