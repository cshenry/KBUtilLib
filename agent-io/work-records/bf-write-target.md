# bf-write-target — make the clearinghouse write-target guard satisfiable, and fix in-pod Spark params

Branch: `maestro/developer/shared-context-this-prompt-is-se-bf-write-target`
Base commit: `f3e6933`
Role: developer

## Precondition check (clearinghouse-lake-5-convergence) — ALL FOUR PASS

Checked by import, in this worktree, before writing any code:

| Check | Result |
|---|---|
| (a) every `<type>_result` config's columns include `parameter_set_hash` | PASS — all 5 result configs; none of the 10 entity/content configs carry it, as intended |
| (b) `clearinghouse_schema` exports `PARAMETER_SET_TABLE` and `table_configs()` returns a config for it | PASS — `PARAMETER_SET_TABLE == "parameter_set"`, **16** configs, the 16th is `parameter_set` |
| (c) `kbutillib.domains.identity` exports `parameter_set_hash` and `DEFAULT_PARAMETER_SET_HASH` | PASS — `DEFAULT_PARAMETER_SET_HASH == 44136fa3…aff8a` |
| (d) `.../berdl/examples/clearinghouse_example_seed.toml` exists | PASS |

Iteration over tables used `table_configs()` throughout, never `ENTITY_TYPES x KINDS`.

## What I built

### 1. `BerdlCapability` now resolves its own write target (`capability.py`)

- **`BerdlCapability._write_target_namespace(dataset, tenant)`** (`src/kbutillib/domains/kbase/berdl/capability.py:370`) — ONE private, pure staticmethod. No I/O: no transport, no Spark session, no namespace creation, no existence probe. It encodes `data_lakehouse_ingest`'s own derivation and nothing else: `f"{tenant}.{dataset}"` when a tenant is given, `f"my.{dataset}"` (`naming.SPARK_PERSONAL_ALIAS`) when it is not.
- **`resolve_write_namespace(*, dataset, tenant)`** (`capability.py:404`) and **`resolve_probe_namespace(*, dataset, tenant)`** (`capability.py:420`) — both return that function's result and nothing else. Read-only; they work off-pod with no transport ever constructed.
- `from .naming import SPARK_PERSONAL_ALIAS, NormalizedDatabase` (`capability.py:41`).

### 2. `load()` probes the computed namespace and confirms agreement (`capability.py`)

- `load()` now computes `expected_namespace = self._write_target_namespace(dataset, tenant)` (`capability.py:571`) and uses **that** value for the per-table existence probe (`capability.py:626`), so the probe target is provably the value the resolvers publish.
- After `transport.create_namespace_if_not_exists(...)` returns, a new check (`capability.py:575-596`) raises `BerdlLoadRefusedError` when the returned namespace is truthy but differs from `expected_namespace`. The message names BOTH values. It fires before any probe, any staging and any `data_lakehouse_ingest` import, so no ingest is called.
- The pre-existing `if not resolved_namespace:` refusal (empty/falsy resolution, dev 1206 AC 3) is untouched and still fires.
- `load()`'s `Raises:` docstring updated for the new case (`capability.py:504`).

### 3. `_assert_write_target` NOT weakened (`clearinghouse_capability.py`)

No logic change. It still requires both resolvers and still refuses on a falsy or mismatched pair; two tests pin exactly that (see below). Only its docstring changed (`clearinghouse_capability.py:1414-1427`), because the old text claimed "The real `BerdlCapability` does not expose them today" — now false. The replacement states why the agreement is structural rather than coincidental: there is one derivation, and the probe, the write and the guard all read it.

### 4. In-pod Spark promotion no longer passes `params` (`clearinghouse_capability.py`)

- `_bind_offpod` → **`_inline_hex_params`** (`clearinghouse_capability.py:436`). Same algorithm (re-validate each value against `_HEX64_RE`, then substitute `?` left-to-right with a quoted literal, refusing on a non-hex value or a placeholder/param count mismatch); renamed and re-documented because it now serves BOTH no-bind paths. Error-message prefixes and the `_HEX64_RE` reference updated with it. No call site outside the module referenced the old name.
- **`_run`** (`clearinghouse_capability.py:378`) now has three explicit binding regimes. In-pod with `engine == "spark"`: inline via `_inline_hex_params`, then `capability.query(final_sql, params=None, engine="spark")`. In-pod Trino: unchanged server-side binding. Off-pod: unchanged, via the renamed inliner.
- Because every hash-filtered verb routes through `_run`, this fixes `known()`, `content()`, `content_all_types()` and `results()` at/above `SPARK_PROMOTION_THRESHOLD` (4000) in one place, and it fixes any caller that passes `engine='spark'` explicitly.
- Rule 3 (single write path) and Rule 5 (slot key imported, never restated) untouched; the static no-INSERT test in `test_clearinghouse_live.py` still passes.

### A SECOND instance of the same defect, found and fixed

`ClearinghouseCapability._registered_parameter_set_hashes` (`clearinghouse_capability.py:1805`) hard-codes `engine="spark"` **and** passed `params=list(hashes)`. Against a real `BerdlCapability` that raises `ValueError`, so `register(..., kind='result')` was broken in production for every call that needed a registry append — not just the ≥4000-hash reads the task named. The `_run` fix repairs it with no change at that call site; its docstring, which claimed the lookup was "bound server-side … there is a real bind channel", was wrong and is corrected.

## Tests added

`tests/berdl/test_capability.py` — `TestWriteTargetResolvers` (6 tests):
- resolvers return identical values with tenant set (`kbaseincubator.clearinghouse`) and unset (`my.clearinghouse`);
- resolvers do no I/O — `_get_transport` is monkeypatched to raise, locus forced off-pod, `_transport` asserted still `None`;
- **parametrized over tenant set/unset**: the value `resolve_probe_namespace` publishes is exactly what `load()` passes to `table_exists`;
- `load()` refuses with `BerdlLoadRefusedError` naming both namespaces, with `table_exists_calls == []` and `fake_ingest.configs == []`, when `create_namespace_if_not_exists` returns `"somewhere.else"`.

Reused the existing `_FakeInPodTransport`, `_RecordingIngest` and `fake_ingest` fixture rather than adding new doubles.

`tests/berdl/test_clearinghouse_capability.py` (16 tests, +1 fake hardened):
- `_FakeCapability.query` and `_FakeWriteCapability.query` now **raise on `params` when `engine == "spark"`**, mirroring the real contract. Added `_inlined_hex(sql)` so fakes that used to read `params` read the hashes back out of the SQL text.
- the guard passes for a **real `BerdlCapability`** with no injected resolver, parametrized over tenant set/unset, with the transport replaced by `_BoomTransport` (any attribute access fails) — so the guard is proven to do no I/O;
- the guard passes for `ClearinghouseCapability()` with **no collaborator** — the CLI's exact construction at `src/kbutillib/interfaces/cli/clearinghouse.py:248`, the one that refused on every production call — and `_capability._transport` is still `None` afterwards;
- the guard **still refuses** a capability without the resolvers, and still refuses a mismatched pair (not weakened);
- `known`/`content`/`results` with **5000** hashes: one Spark query, `params is None`, no `?` left, all 5000 hashes inlined in order;
- non-regression: the same three verbs below the threshold still reach Trino with `params` bound and `?` in the SQL (the fix does not spill inlining onto the path that can bind);
- a malformed hash in a 5000-hash batch raises `ValueError` with `fake.calls == []`;
- `_run` with a SQL-injection-shaped param on `engine='spark'` is refused by the inliner, no query issued;
- `_registered_parameter_set_hashes` runs on Spark with the hash inlined and no params.

## Verification

Environment note: the task venv (`.task-venv`) shipped without `pytest`. I installed `pytest` and `ruff` **into this task's own venv** only.

Baseline discipline: `build-baselines/kbutillib.json` exists (base `0f84803`, captured 2026-09-13) and lists **no** berdl or clearinghouse nodeid in its 99 `known_failing`. Because that capture predates this whole clearinghouse line of work and `tests/cli` carries a large unrelated missing-dependency failure set, I did not compare against it by count. I compared **by name against this branch's own base commit**, running the same command with my four files reverted via `git checkout --` and then restored:

```
python -m pytest -q tests/berdl tests/cli \
  --ignore=tests/berdl/test_clearinghouse_derivation.py \
  --ignore=tests/berdl/test_clearinghouse_parity_script.py
```

- base (`f3e6933`, files reverted): 250 failed, 1065 passed, 13 skipped
- branch: 250 failed, 1065 passed, 13 skipped
- `comm` over the sorted failing-nodeid sets: **regressions = 0, newly-passing = 0.** The two sets are identical by name.

Targeted runs on the branch:

| Command | Exit | Result |
|---|---|---|
| `python -m pytest -q tests/berdl --ignore=…derivation.py --ignore=…parity_script.py` | 0 | 579 passed, 5 skipped |
| `python -m pytest -q tests/berdl/test_capability.py` | 0 | 40 passed |
| `python -m pytest -q tests/berdl/test_clearinghouse_capability.py` | 0 | 190 passed |
| `ruff check` on the 3 files I changed that were clean at base | 0 | All checks passed |

## What I could NOT verify

- **`duckdb` is not installed in this venv**, so `tests/berdl/test_clearinghouse_derivation.py` and `tests/berdl/test_clearinghouse_parity_script.py` fail at COLLECTION with `ModuleNotFoundError: No module named 'duckdb'` — identically before and after my change, so they are excluded from both legs of the comparison. Neither module exercises `_run`'s binding regimes or `BerdlCapability`'s resolvers (they drive SQL text through DuckDB directly), so I do not expect them to be affected; I did not prove it.
- **Three pre-existing failures** in `tests/cli/test_clearinghouse_operator.py` (`test_load_dry_run_writes_nothing`, `test_plan_invalid_manifest_exits_nonzero_naming_key`, `test_load_json_stdout_is_clean_when_warnings_present`) are environmental: the `[KBUtilLib] 14 optional modules unavailable: …` banner pollutes stdout, so `json.loads` on the captured envelope raises `JSONDecodeError`. Present identically at base. Not touched.
- **`data_lakehouse_ingest` is not importable off-pod**, so I could not read `orchestrator/init_utils.py` directly to confirm the derivation rule. I took it from the two in-repo records of a live in-pod verification: `src/kbutillib/domains/kbase/berdl/capability.py:481` and `agent-io/prds/berdl-smoke-verification/in-pod-results.md:114-115` (`orchestrator/init_utils.py:102–117` — `{tenant}.{dataset}` with a tenant, `my.{dataset}` without). The existing `_FakeInPodTransport` already mirrors that contract. **This rule is inferred from those records, not re-measured in the pod.** The new agreement check in `load()` is precisely the guard for the case where it is wrong: if the platform ever resolves something else, the load refuses and names both values instead of writing blind.
- No in-pod run. Every test here is a fake/double; nothing touched live BERDL, Spark, Trino or any namespace.

## Reconciliations (prompt claim → what the tree actually holds → what I did)

1. **"`_assert_write_target` (clearinghouse_capability.py ~958-1021)"** → it is at **`clearinghouse_capability.py:1395`**. Located by name; edited there.
2. **"read helpers (~336-387)" / "the off-pod path already does, ~355-433"** → the shared seam is `_run` at **`:378`** and the inliner at **`:436`**. Fixed in `_run`, which every hash-filtered verb routes through, so one edit covers `known`/`content`/`content_all_types`/`results`.
3. **"`BerdlCapability.query` raises ValueError for params on Spark (~646-653)"** → the raise is at **`capability.py:753`** after my insertions (it was ~660 at base). Behaviour as described; not changed.
4. **"the CLI builds a bare `ClearinghouseCapability()` (~226-236)"** → it is at **`src/kbutillib/interfaces/cli/clearinghouse.py:248`**. No CLI change was needed: the lazily-constructed `BerdlCapability` now carries the resolvers, and a test pins that exact construction.
5. **"the test fake accepts params on any engine (test_clearinghouse_capability.py ~93-110)"** → `_FakeCapability.query` is at **`:118`** at base and already rejected params off-pod; it accepted them on in-pod Spark. Hardened as instructed.
6. **Precondition (a) was stated as "every `<type>_result` config's **columns** include `parameter_set_hash`"** → a `table_configs()` entry is `{'name', 'schema_sql'}`; there is no `columns` key. Checked the column's presence in each result config's `schema_sql` instead. The substance of the check holds.
7. **The task named ONE Spark/params site (the read helpers).** The tree holds a **second**: `_registered_parameter_set_hashes` (`:1805`) also pairs `engine="spark"` with `params`, breaking `register(kind='result')` in production. Fixing `_run` fixes both; I added a test and corrected that method's incorrect "bound server-side" docstring. Flagging it because the taskplan's defect statement was narrower than the defect.
8. **"the slot-key column tuple, currently `_SLOT_KEY_COLUMNS`"** → not needed. Nothing in this task restates or imports the slot key, so no `SLOT_KEY_COLUMNS` public alias was added.
9. **Standing rule "DuckDB as the stand-in engine as in tests/berdl/test_clearinghouse_derivation.py"** → `duckdb` is not installed here and that module does not even collect. These tests need no SQL engine (they assert on SQL text and call shape), so they use the existing in-repo fakes instead.
