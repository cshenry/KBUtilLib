# Work record -- `bf-live-test-and-runbook`

- **Repo**: KBUtilLib
- **Branch**: `maestro/developer/shared-context-this-prompt-is-se-bf-live-test-and-runbook`
- **Base commit**: `5cbac64`
- **Date**: 2026-10-09

## Precondition check (lake-5 convergence) -- ALL FOUR PASS

Run in the worktree before any code was written:

| Check | Result |
|---|---|
| (a) every `<type>_result` config's columns include `parameter_set_hash` | **PASS** -- all five result configs carry it |
| (b) `clearinghouse_schema` exports `PARAMETER_SET_TABLE` and `table_configs()` returns sixteen configs including one named `parameter_set` | **PASS** -- `PARAMETER_SET_TABLE == "parameter_set"`, `len(table_configs()) == 16` |
| (c) `kbutillib.domains.identity` exports `parameter_set_hash` and `DEFAULT_PARAMETER_SET_HASH` | **PASS** -- `DEFAULT_PARAMETER_SET_HASH == 44136fa3...aff8a` |
| (d) `examples/clearinghouse_example_seed.toml` exists | **PASS** -- 6169 bytes |

So the build proceeded.

## What I built

### 1. A live test of the PRODUCTION backfill path, nothing injected

`tests/berdl/test_clearinghouse_live.py:751` --
`test_live_backfill_run_file_example_lands_then_adds_nothing`, gated by
`KBUTILLIB_LIVE_BERDL=1` and skipped (not errored) with it unset.

It drives `kbu clearinghouse backfill run file-example --dataset
clh_bf_live_<8 hex>` through click's `CliRunner`, so the capability under test
is the one `cli.clearinghouse._capability()` builds for itself --
`ClearinghouseCapability()` with **nothing injected**. Nothing in the test
supplies a resolver, a probe or a namespace to the write path.

- **Run 1** must land rows: unit `done`, no `incomplete_units`, summed
  `rows_ingested > 0`, and from `backfill status` (all sixteen tables, iterated
  from `table_configs()`) non-zero `protein_entity`, `protein_result` **and**
  `parameter_set` registry rows.
- **Run 2** is a NEW run id into the SAME dataset: every unit's
  `rows_ingested == 0`, and **every one of the sixteen tables shows 0 rows
  ingested, the `parameter_set` registry included** -- asserted both as an
  all-tables sweep and as a named assertion on the registry, because "a run
  that adds no new parameter set ingests zero registry rows" is success.
- The namespace-wide orphan check (`data.parameter_set_integrity.failures`) is
  asserted empty **after each run**.
- Supporting fixtures: `bf_scratch_dataset` (asserts the name is neither
  `chs.NAMESPACE` nor `"clearinghouse"`) and `bf_live_scratch`, which creates
  `kbaseincubator.<scratch>`, bootstraps every table in `table_configs()`,
  redirects `$KBU_CLEARINGHOUSE_STATE_DIR`/`$KBU_CLEARINGHOUSE_LOCK_DIR` under
  `tmp_path`, and tears the dataset down the way the existing live fixture does
  (DROP every table, then DROP NAMESPACE, then `mp.undo()`).
- `_run_backfill_cli` keeps stdout and stderr apart with the same
  `CliRunner(mix_stderr=False)`-probe used in
  `tests/cli/test_clearinghouse_backfill_cli.py:185`: under click < 8.2 a mixed
  runner would hand the JSON parser the stderr warnings.

### 2. `_ScratchAdapter` trimmed

`tests/berdl/test_clearinghouse_live.py:154`. `resolve_write_namespace` and
`resolve_probe_namespace` are **gone**; a `__getattr__` now delegates every
unlisted public attribute to the real `BerdlCapability`, so the dev 1206 guard
is satisfied by the production derivation (`_write_target_namespace`) instead of
by the test. `table_exists`/`table_partition_spec` are **kept**, with a comment
saying why: `clearinghouse_schema.bootstrap()` requires them, the real
`BerdlCapability` implements neither, and both the existing `live_clearinghouse`
fixture and the new `bf_live_scratch` fixture need `bootstrap()` to create the
scratch tables at all. They are setup, not the contract under test.

`test_scratch_adapter_does_not_supply_the_namespace_resolvers`
(`:861`, **not** gated) asserts via `vars(_ScratchAdapter)` that neither
resolver is defined and that both bootstrap probes still are. `vars()` rather
than `hasattr()` deliberately: delegation through `__getattr__` is the correct
state and `hasattr` cannot tell it from a local definition.

The static no-`INSERT`/`MERGE` test is untouched and still passes.

**A consequence I had to handle, and it was a latent safety bug.** With the
resolvers removed, the write target is derived from the `dataset` argument --
and `register`/`ingest_shards` bind `dataset: str = NAMESPACE` **at import
time**, so monkeypatching `chs.NAMESPACE` cannot reach that default. Every
write in the module would therefore have resolved to
`kbaseincubator.clearinghouse` -- PRODUCTION. All four existing live writes now
pass `dataset=scratch_namespace` explicitly, and the module docstring records
this as the third never-production guard.

### 3. Runbook -- a new "Backfill" section

`agent-io/docs/clearinghouse-schema-operator-runbook.md:1662`, placed **after**
the parameter-set section (it depends on OP-C1) and **before** "If something
goes wrong". It covers:

- **OP-C1 as the hard precondition of every load step**, enforced by the schema
  preflight: `backfill run` refuses a namespace that predates lake-5 with
  `load()` uncalled, and **a scratch namespace created before the lake-5 deploy
  is refused the same way** (`bootstrap()` cannot add a column -- drop and
  recreate).
- **No gate on anyone outside this PRD**, stated explicitly as withdrawn
  2026-10-07. (There was no Ahmed gate in the document to remove; see
  "Divergences" below.)
- **Where it runs**: in-pod only, the pod PULLS (Mongo on poplar:27017, KBDL on
  poplar:8791, the arc store read in place on the pod's own filesystem), and
  **there is no staging step** -- plus why there cannot be one (Spark executors
  cannot read the pod's local disk).
- **The do-not-delete warning** for `~/.kbdl/job-results/` and the
  `~/arc-relocate-backup-*` tarballs: the only surviving copies.
- **Deploy, two trees two routes**: the pod's `/global_share/KBaseUtilities/KBUtilLib`
  on `deploy/main-20260926` (merge `main` into that branch, there, with the
  uncommitted-paths stop policy), the **bare-venv trap** (always launch through
  `kbu`; `~/venvs/kbu-modeling/bin/python` imports the Dropbox dev tree), the
  `kbutillib[backfill]` extra for the Mongo presets, and the KBDL **content
  route** which must be deployed on poplar before the arc preset can fetch
  genome objects (and which, unlike a KBUtilLib clearinghouse deploy, does need
  KBDL restarted).
- **poplar's measured route** (Miles, 2026-10-08): `~/venvs/kbdl` imports
  `~/projects/KBUtilLib` via an editable `.pth`, the dist-info
  `direct_url.json` is stale, `fetch` → assert `origin/main` equals the Dropbox
  `main` → `merge --ff-only` → smoke-test with `~/venvs/kbdl/bin/python` and
  trust `kbutillib.__file__`. "Merge main into wip" is called out as never a
  poplar deploy.
- **The order with exact commands**: arc (`arc-kbdl`, scratch → `status` →
  production; OP-BF4 tool versions must be folded in before the first
  production arc load, no values named) → genome archives
  (`kbdl-genome-archives --limit 1` first, `max_in_flight` 1, the frozen unit
  list, `--retry-failed-archives`) → the seed (identified as **OP-C3**, with
  its six-item promotion gate) → the full Mongo preload, one preset at a time,
  all slices **including `00`**, with the per-preset order table
  (`mongo-protein-seq` first).
- **Four easy-to-get-wrong rules**: scratch and production are separate runs
  (never `--resume` one into the other -- enforced by the run's immutable
  identity); fill-only never replaces a row so re-running is safe and a
  zero-row second run is success; fill-only is idempotent and lock-protected
  but **not atomic against other writers** (run production backfills when
  lake-4's writer is idle; worst case a duplicate slot row); a namespace-wide
  orphan exits non-zero **although every unit is done** -- read the report, a
  re-run cannot fix it.
- **Resume / status / reading a run**: reconcile-before-delete with the exact
  count rule, unit states done/partial/incomplete/pending, and **load
  executions versus shard runs** (promotion rewrites `ingest_batch_id`, so
  production's batch ids are not scratch's).
- **Incremental arc runs**: the persisted watermark, promoted only on a `done`
  unit, keyed by tenant+dataset+store+endpoint+principal so **a scratch run
  never moves production's**; an explicit `since` wins over the checkpoint;
  `since` narrows what is emitted, never the dependency index.
- **Skip reasons**, grouped into expected-absence / not-backfillable /
  retryable (`object-unavailable` leaves the unit incomplete) / advisory, with
  the adapters' real constant values.

Also in the runbook:

- The **deploy table's poplar row was wrong** (it said `~/venvs/kbdl` imports
  the Dropbox tree, parked on `wip`) and is corrected in place, with a note
  saying what it said and why the correction matters.
- **OP-C3's "the backfill command is not deployed" text was stale** -- it told
  an operator that `No such command 'backfill'` is the expected answer. It now
  says `backfill` is on `main` and that the error means *this tree is not
  deployed*, pointing at the deploy route.
- **OP-C3's "uniform 1/256 sample"** is replaced with a **capped hash-prefix
  sample**: `--slice 00` is 1/256 *only if* the `_id` space is uniform (which
  is unmeasured), and `--limit` takes a **sorted prefix within** the slice --
  explicitly not an unbiased or representative sample.

### 4. CLI doc -- backfill verb reference

`agent-io/docs/kbu-clearinghouse-cli.md`: the operator-half intro now points at
`backfill` as the thing you want when filling from a source, the locus table
gains five `backfill` rows, and a new `# kbu clearinghouse backfill` section
documents the shard→ingest→verify→record order, the unit-state table with exit
codes, every verb and flag, fill-only (safe to re-run, not atomic, the shared
lock), load-executions-vs-shard-runs, reconcile-before-delete, frozen unit
lists, the arc watermark, and the skip reasons. It defers the operator
procedure to the runbook rather than duplicating it.

## Verification

Cached baseline `build-baselines/kbutillib.json` exists (base commit
`0f84803`, `compare_by: "name"`), so per the role prompt I did **not** re-run
the full suite. I ran the targeted set for what I touched, in the same venv, at
`5cbac64` (clean worktree, before any edit) and again on the branch:

```
.task-venv/bin/python -m pytest tests/berdl tests/cli/test_clearinghouse_backfill_cli.py \
    tests/cli/test_clearinghouse_operator.py tests/cli/test_clearinghouse.py \
    tests/cli/test_arc_backfill.py -q \
    --ignore=tests/berdl/test_clearinghouse_derivation.py \
    --ignore=tests/berdl/test_clearinghouse_parity_script.py
```

| | base `5cbac64` | branch |
|---|---|---|
| exit code | 1 | 1 |
| result | 8 failed, 1091 passed, 5 skipped | 8 failed, **1092** passed, **6** skipped |

**The failure NAME sets are identical** (8 names, `comm -13` empty): two in
`test_clearinghouse_operator.py` (`test_load_dry_run_writes_nothing`,
`test_load_json_stdout_is_clean_when_warnings_present`) and six in
`test_clearinghouse.py` (three `[health]` parametrisations,
`test_offpod_stats_include_files_degrades`,
`test_content_all_types_uses_lossy_view_and_warns`,
`test_health_offpod_cannot_evaluate_warns`). All eight are pre-existing and
fail at the base commit too. The +1 passed is the new ungated `_ScratchAdapter`
check; the +1 skipped is the new live test.

Live-gating, with `KBUTILLIB_LIVE_BERDL` unset (exit 0):

```
.task-venv/bin/python -m pytest tests/berdl/test_clearinghouse_live.py -q
->  2 passed, 6 skipped
```

Six live tests SKIP (five existing + the new one), not error. Two static tests
pass.

Lint (`ruff check tests/berdl/test_clearinghouse_live.py`): **1 error, the same
1 the file has at the base commit** (a pre-existing `I001` on the local import
block inside `test_live_ingest_shards_tiny_directory`, which I did not touch).
No new lint error.

The two ignored modules (`test_clearinghouse_derivation.py`,
`test_clearinghouse_parity_script.py`) fail to COLLECT in this venv for lack of
`duckdb` -- at the base commit as well -- which is why they are ignored in both
runs. `pytest` and `ruff` were `pip install`ed into the worktree's own
`.task-venv`, which had neither.

**Off-pod proof of the new test's envelope assumptions.** The live test itself
cannot run here, so I verified every key and shape it asserts on by driving the
real CLI against the existing `_FakeBackfillCapability` from
`tests/cli/test_clearinghouse_backfill_cli.py` in a throwaway script:
`data.dataset`, `data.run_id`, `data.incomplete_units`,
`data.units[].state`/`.rows_ingested`,
`data.parameter_set_integrity.failures`, and `backfill status`'s
`data.tables` -- which came back with **16** entries, `parameter_set: 1`,
`protein_entity: 48`, `protein_result: 48`, unit `('all', 'done', 165)`. So the
assertions are against the real envelope contract, not a guess.

## What I could NOT verify

- **Nothing in-pod.** The new live test, like every test in that module, has
  never executed: this worktree is on h100, not the kbhub BERDL pod, and
  `BerdlCapability.locus()` is `off_pod` here. Everything it asserts about the
  live write path is unproven until an operator runs it in the pod with
  `KBUTILLIB_LIVE_BERDL=1`. In particular the scratch-dataset `bootstrap()`
  call, the `CREATE`/`DROP NAMESPACE` statements and the second-run zero-row
  claim are code-review-correct and live-unverified.
- **The runbook's measured facts are reported, not re-measured**: poplar's
  `.pth`/`direct_url.json` state (Miles, 2026-10-08), the pod tree's branch
  name, the `kbu` shim's `PYTHONPATH` behaviour, the KBDL port and the state of
  the KBDL content route. I checked the one thing I could from here --
  `kbutillib[backfill]` really is a declared extra and really is just `pymongo`
  (`pyproject.toml:60`).
- Whether a pre-lake-5 `clearinghouse_bf_scratch` namespace currently exists on
  the pod. The runbook tells the operator to check; I could not.

## Divergences between the task prompt and the tree (fixed, carried on)

1. **"Correct any runbook sentence that still calls
   `kbaseincubator.genome_clearhouse` 'unrelated'" -- nothing to correct.**
   "unrelated" appears exactly twice and **neither occurrence calls it
   unrelated**: line 217 says it is "**not** an unrelated namespace" and line
   347 says the adjacency is "more dangerous now than when the source **was**
   unrelated". Both are already the corrected form. I changed neither.
2. **No Ahmed gate exists to remove.** `grep -rn Ahmed agent-io/docs/` returns
   nothing. OP-C3 does carry an acknowledgment precondition, but what it
   requires is "**no** gate on an outside party", which agrees with the
   amendment rather than conflicting with it, so I left it alone and instead
   stated the no-outside-gate rule positively in the new section.
3. **`_ScratchAdapter`'s line numbers had moved** (prompt: adapter at 136,
   resolvers at 168/171, probes at 176/185; actual: class at 136 with the
   members a few lines lower). Found by name.
4. **The prompt describes `kbdl-genome-archives` `--limit 1` as "each archive
   is a KBDL job"** -- correct, and worth noting it is the adapter-unit case
   where `--limit` caps the **number of units**, not records. Documented that
   way, from `_LIMIT_HELP` in the CLI.
5. **`_ScratchAdapter`'s trim is not inert for the rest of the module.** The
   prompt treats items 1 and 2 as independent; they are not. See the safety
   consequence under item 2 above -- removing the resolvers without adding
   `dataset=` to the four existing live writes would have pointed them at
   production.

## Two in-scope fixes I made that the prompt did not ask for

Both are consequences of lake-5 that make the live module unable to pass in the
pod, so leaving them would have shipped a test suite that cannot do its job:

1. **The existing fixture's teardown dropped fifteen tables, not sixteen**
   (`ENTITY_TYPES × KINDS`), leaving `parameter_set` behind -- which makes the
   following `DROP NAMESPACE` fail and the scratch namespace survive the run.
   It now iterates `table_configs()`, per the task's own standing rule.
2. **`_result_row` carried no `parameter_set`.** Since lake-5, `register(...,
   kind="result")` refuses a result row without one rather than defaulting it,
   so `test_live_current_state_latest_per_slot` would error in the pod. It now
   declares `parameter_set: {}`, with a comment saying why.

I did **not** touch `mongo-protein-seq.toml`'s comment calling its slice "a
UNIFORM 1/256 sample, not a biased first-N", which carries the same
imprecision the round-2 amendment corrects. The amendment is scoped to the
runbook, and widening a doc fix into package data was not mine to decide --
flagging it here instead.
