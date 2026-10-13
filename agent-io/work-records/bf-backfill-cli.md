# Work record — `bf-backfill-cli`

- **Task:** add the `kbu clearinghouse backfill` command group (list / run / shard / load / status)
- **Repo:** KBUtilLib
- **Branch:** `maestro/developer/shared-context-this-prompt-is-se-bf-backfill-cli`
- **Base commit:** `6d142ad` (*feat(clearinghouse): ingest the shards on the pod's disk, fill-only and provable*)
- **Date:** 2026-10-09

## Precondition check (ran FIRST, all four pass)

The task said to stop if any of four lake-5-convergence checks failed. All four hold at `6d142ad`:

| Check | Result |
|---|---|
| (a) every `<type>_result` config's columns include `parameter_set_hash` | PASS — `clearinghouse_schema.column_names(et, "result")` contains it for all five entity types |
| (b) `clearinghouse_schema` exports `PARAMETER_SET_TABLE` and `table_configs()` has sixteen configs | PASS — `PARAMETER_SET_TABLE == "parameter_set"`; `len(table_configs()) == 16`, one of which is `{"name": "parameter_set", "schema_sql": "parameter_set_hash STRING, canonical_json STRING, observed_at TIMESTAMP, ingest_batch_id STRING"}` |
| (c) `kbutillib.domains.identity` exports `parameter_set_hash` and `DEFAULT_PARAMETER_SET_HASH` | PASS — the default hash is `44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a` |
| (d) `examples/clearinghouse_example_seed.toml` exists | PASS — 6,169 bytes |

I iterate `table_configs()` everywhere a table set is needed (`clearinghouse_backfill.status_report`), never `ENTITY_TYPES × KINDS`, so the registry is never dropped.

## What I built

### 1. Presets as package data

- `src/kbutillib/domains/kbase/berdl/backfills/file-example.toml` — the one shipped preset. Three sources over one parquet fixture: `protein_entity`+`protein_content`, `protein_result` (with `parameter_set = {}`), `function_entity`+`function_content`. Identity is recomputed from `aa` / `product` via `raw_column`, never taken from the fixture's `protein_id`.
- `src/kbutillib/domains/kbase/berdl/backfills/file-example.parquet` — 48 deterministic synthetic protein rows (`protein_id`, `aa`, `product`, `observed_at`), 7,132 bytes.
- `pyproject.toml:216-224` — `backfills/*.toml` and `backfills/*.parquet` added to `[tool.setuptools.package-data]`, so a non-editable install still ships them.
- `clearinghouse_backfill.available_presets()` / `describe_preset()` / `resolve_manifest_path()` — a shipped name wins over a same-named file in the cwd; an explicit path still works.

### 2. Placeholder expansion in manifest locators

`src/kbutillib/domains/kbase/berdl/clearinghouse_manifest.py`:

- `expand_placeholders()` (`:301`) — `${VAR}`, `${VAR:-default}`, and a **leading** `~`. An unset bare `${VAR}` raises `ManifestError` naming the variable, the source and the key.
- Expansion is **innermost-first and iterative** (max 8 passes), so a default may itself be a placeholder. That was needed for the next item.
- `MANIFEST_DIR_VAR` — an **injected, non-overridable** variable holding the manifest file's own directory. This is how a package-data preset points at a fixture shipped beside it (`${KBU_BACKFILL_EXAMPLE:-${MANIFEST_DIR}/file-example.parquet}`) without the loader special-casing one preset's path. Absent (hence an error) for a manifest parsed from a TOML string.
- Expansion covers the **locator half only**, recursively through nested mappings and lists. The mapping half is deliberately untouched — a `@const(${VAR})` would make one manifest write different partition values on different machines.
- `load_manifest(source, *, env=None)` — the `env` kwarg is for tests; production reads `os.environ`.

### 3. The informational `[backfill]` table

`BackfillInfo` dataclass (`clearinghouse_manifest.py:~230`) with `pulls_from` and `needs_env`, reachable as `Manifest.backfill`. Parsed rather than ignored, and an unknown key in it is refused by name, so a typo is visible. Nothing in the shard or ingest path reads it; `backfill list` prints it.

### 4. The `units()` adapter hook and the run context

`src/kbutillib/domains/kbase/berdl/clearinghouse_shard.py`:

- `SourceAdapter.units()` → `(SINGLE_UNIT,)` by default; `restrict_to_unit(unit)` accepts its own label and `None` and **refuses** anything else by name; `unit()` reports what was set.
- `MongoSourceAdapter.units()` → the 256 two-hex prefixes (`HEX_PREFIXES`); `restrict_to_unit()` sets `[prefix, next(prefix))` and `_query_filter()` prefers it over the manifest's own `min_key`/`max_key` (an explicit per-unit instruction is a contradiction of a pinned range, not an intersection).
- `SourceAdapter.bind_run_context(context)` and `_admit_record()` — the `--limit` claim, made in the **adapter's own loop** so a budget stop leaves `exhausted()` at `False`. `FileSourceAdapter.iter_records` and `MongoSourceAdapter.iter_records` both call it.
- `parse_slice` / `slice_prefixes` / `unit_key_range` / `slice_key_range` — `AA` or `AA-BB`, two **lowercase** hex digits, `lo <= hi`; `max_key` omitted for `ff`. Uppercase is rejected rather than folded.
- `get_adapter(source, *, run_context=None, unit=None)` and `shard_manifest(..., run_context=None, adapter_unit=None)` thread both through. Existing callers are unaffected (both default to `None`).
- The lazy `pymongo` import now raises `ManifestSourceError` naming `kbutillib[backfill]`.

### 5. The driver — `src/kbutillib/domains/kbase/berdl/clearinghouse_backfill.py` (NEW)

- `plan_units()` — mongo ⇒ slice units; otherwise the adapters' own `units()`; otherwise one unit. `--slice` against a non-mongo manifest is refused **by adapter name**. For adapter units, `--limit` caps the **unit count**.
- `BackfillRunContext` — durable `get_state`/`put_state` per `(source, unit)`, written atomically and fsynced under the run's state directory, plus `record_limit()` and `retry_failed_archives`.
- `RunRegistration` / `register_run()` — records absolute `--out`, tenant, dataset, the **expanded** manifest's digest, standardizer version, source identities, `--slice`, `--limit`. `--resume` refuses a conflict in any of those (naming the field, the registered value and the supplied one) and accepts a changed `--limit`, keeping a `limit_history`. A scratch run and a production run therefore cannot share a run id or a ledger.
- `run_backfill()` — per unit: **shard → ingest → verify → record**, with the execution id recorded *before* the ingest so a kill cannot strand the ledger. `--resume` reconciles before deleting, reuses published batches, and regenerates only unpublished files.
- `unit_outcome()` — `done` (verified + every source exhausted) / `partial` (`--limit` stopped it) / `incomplete` (a discrepancy, or a non-exhausted unit with no `--limit`). A verification discrepancy beats a limit stop.
- `delete_unpublished()` — deletes only parquet that `run.json` does not name. Never a published batch.
- `status_report()` — merges `backfill-state.json`, each unit's `run.json` and each unit's ledger; reports **all sixteen tables** from `table_configs()`, skips by reason, and the adapter watermark.
- `find_run_for_out()` — the state file first, then a scan of registrations for a matching `out`, so `status --out DIR` works after the state file is gone.

### 6. `--unit` support in the capability

`src/kbutillib/domains/kbase/berdl/clearinghouse_capability.py`:

- `ingest_shards(..., unit=None)` — the inventory stays **whole** and the loop still walks it in order; non-matching entries are skipped at the point of ingest. Filtering the list would shrink the prefix `_assert_registry_committed_through` measures against, letting a result batch load while an earlier unit's registry batch was missing. A unit loaded out of order is therefore refused, which is the correct answer.
- `verify_run(..., unit=None)` — here filtering **is** correct (a unit's batches were published atomically), and a `--unit` naming nothing is reported as a discrepancy.
- `clearinghouse_ingest.assert_unit_in_inventory()` — refuses a `--unit` that matches no entry, naming the available labels, rather than ingesting nothing and exiting zero.

### 7. The CLI — `src/kbutillib/interfaces/cli/clearinghouse.py`

A `backfill` subgroup under `clearinghouse_cmd`, reusing `_envelope`, `_emit`, `_require_in_pod_or_refuse` and `_exit_on_integrity_failure`:

| verb | locus | envelope `verb` |
|---|---|---|
| `list` | anywhere | `backfill-list` |
| `run <preset-or-path>` | **in-pod only** | `backfill-run` |
| `shard <preset-or-path> --out` | anywhere | `backfill-shard` |
| `load SHARD_DIR` | **in-pod only** | `backfill-load` |
| `status` | anywhere | `backfill-status` |

- `run`: `--slice --limit --dataset --resume --run-id --out --retry-failed-archives --json`. `--out` defaults to `~/clearinghouse-shards/<run_id>/`. Locus refusal is first — before the preset is resolved, before a run id is minted, before any directory exists.
- `load`: `--run-id --unit --dataset --dry-run --reconcile --json`. Also runs the **unit verification** after the ingest (unlike `clearinghouse load`), and reports `data.id_mapping` (shard run id → load execution id).
- `run` and `load` both end with `cap.parameter_set_orphans()` and exit non-zero on a finding, **after** emitting the envelope with the findings in full, and **without** un-marking any verified unit.
- `_exit_on_incomplete_units()` — `incomplete` exits non-zero; `partial` does not (it is the outcome the operator asked for).
- `_render` gained the five new verbs so human output is a table, not a JSON dump fallback.
- `pyproject.toml` `[project.optional-dependencies]`: `backfill = ["pymongo >=4.6"]`, deliberately **not** in `all`.

## Verification

### New tests

```
python -m pytest tests/berdl/test_clearinghouse_backfill.py \
                 tests/cli/test_clearinghouse_backfill_cli.py -q -p no:randomly
→ 151 passed            (exit 0)
```

- `tests/berdl/test_clearinghouse_backfill.py` — 101 tests: slice parsing (including that the 256 ranges tile the key space exactly once), the locator-range mapping, the non-mongo refusal, the `units()` hook and its default, `--limit` for both unit kinds, every expansion form and the unresolved-variable error, the `[backfill]` table, unit order, done-only-after-verify, the kill-after-shard resume (exactly once, no published batch deleted), partial/incomplete, run identity and every resume conflict, durable adapter state, `--retry-failed-archives` reaching the adapter, and the status merge over sixteen tables.
- `tests/cli/test_clearinghouse_backfill_cli.py` — 50 tests: the envelope for all five verbs, `list` showing `file-example`, both off-pod refusals (with nothing left behind), the orphan check's non-zero exit without un-marking, incomplete vs partial exit codes, `load --unit`, and the status merge.

### Regression comparison

Both runs used the identical command in the same venv — the 11 modules that cannot be collected in this worker venv (missing `pandas`/`torch`/`rdkit`) are `--ignore`d:

```
python -m pytest -q -p no:randomly \
  --ignore=tests/biochem/test_escher_utils.py \
  --ignore=tests/biochem/test_ms_biochem_deltag.py \
  --ignore=tests/biochem/test_ms_reaction_similarity.py \
  --ignore=tests/external/test_prott5_utils.py \
  --ignore=tests/kbase/test_kb_annotation_utils_parquet.py \
  --ignore=tests/modeling/test_comprehensive_gapfill_wrapper.py \
  --ignore=tests/modeling/test_ms_community_utils.py \
  --ignore=tests/modeling/test_ms_community_utils_live.py \
  --ignore=tests/modeling/test_ms_reconstruction_utils.py \
  --ignore=tests/modeling/test_ms_remote_solver_utils.py \
  --ignore=tests/notebook
```

| | base `6d142ad` | branch |
|---|---|---|
| failing nodeids (`FAILED` + `ERROR`) | **368** | **368** |
| passed | 3375 | 3526 |
| `comm -13 base branch` (new failures) | — | **empty** |

**No test that passed on the base commit fails on the branch.** The 368 pre-existing failures are environmental (`pandas` and the pydantic-backed capability stack are absent from this worker venv); in the clearinghouse area specifically only 3 fail at base, all in `tests/cli/` and all unchanged:

- `tests/cli/test_clearinghouse_operator.py::test_load_dry_run_writes_nothing`
- `tests/cli/test_clearinghouse_operator.py::test_load_json_stdout_is_clean_when_warnings_present`
- `tests/cli/test_clearinghouse_operator.py::test_plan_invalid_manifest_exits_nonzero_naming_key`

The second of those fails because it builds `CliRunner()` with click 8.1.6's default `mix_stderr=True`, so `result.stdout` contains the stderr warnings it then tries to parse as JSON. I used `CliRunner(mix_stderr=False)` in my own tests rather than changing that pre-existing one — fixing it is a one-line change but is outside this task.

**The cached baseline at `build-baselines/kbutillib.json` is stale** exactly as the dispatcher warned: it records 99 `known_failing` nodeids captured at `0f84803` on 2026-09-13, against 368 in this venv today. I captured a fresh baseline on the clean tree at `6d142ad` and compared by name, and did **not** rewrite the cached file (its command and `--ignore` list differ from the one this venv needs, and overwriting it with a venv-specific set would make it wrong for the next worker in a different way).

## Divergences between the task prompt and the tree

Each was reconciled in favour of the tree:

1. **`table_configs()` returns `dict`s with `name` / `schema_sql` / `partition_by`, not objects with a `columns` list.** The precondition as written ("every `<type>_result` config's *columns* include `parameter_set_hash`") cannot be checked that way. I verified the equivalent fact through `clearinghouse_schema.column_names(entity_type, "result")` and `parameter_set_column_names()`. The precondition holds.
2. **`SLOT_KEY_COLUMNS` already exists** as a public alias at `clearinghouse_derivation.py:151`; the prompt said to add it if only `_SLOT_KEY_COLUMNS` existed. Nothing to add, and nothing in this task needed to restate the slot key.
3. **`SourceAdapter.exhausted()` already existed** (`clearinghouse_shard.py:~300`), from `bf-shard-streaming`. The `partial` / `done` distinction is built on it rather than on a new flag.
4. **`shard_manifest` already took `run_id`, `slice_label`, `unit_id`, `attempt_id`**, so the "streaming sharder accepting run_id and slice-label parameters" the prompt attributes to a previous task is present. I added only `run_context` and `adapter_unit`.
5. **`ingest_shards` / `verify_run` had no `unit` parameter.** The prompt specified `load --unit U` as "restrict ingest and verification to one unit" as if it existed; I added it, deliberately *not* by filtering the ingest inventory (see §6 above for why that would break the cross-unit registry gate).
6. **`clearinghouse_ingest.read_run_inventory()` is at line 516 together with the id/state-dir helpers**, not after them as the orientation's "496-516 ... 516" implied. Cosmetic.
7. **`--slice AA-BB` semantics.** The prompt says a slice "maps to the mongo adapter's half-open key range `[AA, next(BB))`" *and* that "with no `--slice`, all 256 prefixes run as units", and separately that "the mongo adapter's unit is a slice (two-hex-digit prefix)". Those reconcile only one way: a unit is **one prefix**, so `--slice 00-0f` is sixteen units each reading `[XX, next(XX))`, and `[AA, next(BB))` is the range the whole selection covers. The envelope reports both (`slice_key_range` for the whole, `unit_key_range` per unit).
8. **`--limit` for a single-unit manifest.** Round 2 says "arc caps the frozen ordered record selection ONCE across all sources". Implemented literally at first — one budget shared across readers — it starves every reader but the first: `--limit 10` gave 10 `protein_entity` rows and 0 `protein_content` rows, which is not a sample of anything. The cap is therefore **per reader** over an ordered, frozen source, so every reader sees the same first N records; cross-reader consistency comes from the adapter's ordering (mongo's sort, the arc adapter's frozen selection), not from the budget arithmetic. Documented at `clearinghouse_shard.SourceAdapter._admit_record`.
9. **Nested placeholder defaults.** Not in the prompt, but required: a package-data preset cannot hardcode where pip put the wheel, and the alternative was special-casing `file-example`'s path inside the preset loader. Hence `${MANIFEST_DIR}` and innermost-first expansion.

## What I did NOT do

- **No live run.** Nothing here touched a BERDL pod, Spark, Mongo, or a real dataset — the task is off-pod development work and `run`/`load` refuse off-pod by design. The ingest half is exercised only against a fake capability, so the real `ingest_shards`/`verify_run` interaction with the new `unit` parameter is **unverified against a live lake**. That is the one claim in this record I cannot make from evidence.
- **Only one preset.** `file-example`, as specified. The `arc`, `genome-archive` and `mongo-protein-bakta` presets are later tasks. Note that `examples/clearinghouse_example_seed.toml`'s header already documents `kbu clearinghouse backfill run mongo-protein-bakta --slice 00 --limit 12000`; that preset does not exist yet, so that command does not work today.
- **`pymongo` is not installed in this venv**, so the mongo adapter's `restrict_to_unit` is tested through `_query_filter()` (its full output, with no connection made) rather than against a live cursor. The `kbutillib[backfill]` error message path is marked `pragma: no cover` for the same reason.
- **Did not rewrite `build-baselines/kbutillib.json`.** See the regression section.
- **Did not fix the 3 pre-existing `tests/cli/test_clearinghouse_operator.py` failures.** Out of scope.
