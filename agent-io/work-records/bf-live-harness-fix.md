# bf-live-harness-fix -- repair the clearinghouse live-acceptance harness

**Task:** `bf-live-harness-fix` (developer) · **Repo:** KBUtilLib ·
**Branch:** `maestro/developer/repair-the-clearinghouse-live-ac-bf-live-harness-fix`
**Base:** `55e395e` · **Date:** 2026-10-14

Scope held: **the test module only.** Nothing under `src/` is touched. Neither
defect turned out to need a product change -- both were in the harness, and the
component that fixes Defect 2 (`ClearinghouseBootstrapCapability`) already
ships.

| File | Change |
|---|---|
| `tests/berdl/test_clearinghouse_live.py` | +953 / -53 -- the fix and 34 new ungated tests |
| `agent-io/docs/clearinghouse-schema-operator-runbook.md` | +36 -- the acceptance command and the interpreter |

---

## DEFECT 1 -- ANSI double-quoted Spark identifiers

Spark reads `"x"` as a **string literal** and rejects it where an identifier
belongs. Verbatim from the 2026-10-13 in-pod run:

```
[PARSE_SYNTAX_ERROR] Syntax error at or near '"kbaseincubator"' ...
CREATE NAMESPACE IF NOT EXISTS "kbaseincubator"."clh_bf_live_6ef3b2b1"
```

### The one helper -- `_q()` (`tests/berdl/test_clearinghouse_live.py:152`)

Every identifier this module interpolates into SQL now goes through `_q(*parts)`.
It backtick-quotes **one pair per dot-separated segment** -- the rule already
written three times in `src/` (`clearinghouse_bootstrap_adapter._quote_fqn`,
`capability._quote_fqn`, `clearinghouse_schema._quote_fqn`), because a dotted
namespace quoted as a *single* identifier is a two-part name whose first part
contains a dot and does not resolve
(`agent-io/prds/berdl-smoke-verification/in-pod-results.md`, defect D2). It
raises on a part containing a backtick, and on no usable segment.

That means `_q(TENANT, ns, name)` and `_q(f"{TENANT}.{ns}", name)` are the same
string, which is what lets callers pass a dotted namespace straight through.

### All seven sites, by symbol

| # | Symbol | Was | Now |
|---|---|---|---|
| 1 | `live_clearinghouse` -- `CREATE NAMESPACE` (`:598`) | `f'... "{TENANT}"."{scratch_namespace}"'` | `f"... {_q(TENANT, scratch_namespace)}"` |
| 2 | `bf_live_scratch` -- `CREATE NAMESPACE` (`:985`) | same shape | same fix |
| 3 | `_ScratchAdapter.table_exists` -- probe FQN (`:502`) | `fqn = f'"{TENANT}"."{ns}"."{name}"'` | `_q(ns, name)` (`ns` is already tenant-qualified) |
| 4 | `live_clearinghouse._teardown` -- `DROP TABLE` (`:610`) | `except Exception: pass` + double quotes | `_drop_or_warn(... _q(TENANT, scratch_namespace, cfg['name']))` |
| 5 | `live_clearinghouse._teardown` -- `DROP NAMESPACE` | same | `_drop_or_warn(... _q(TENANT, scratch_namespace))` |
| 6 | `bf_live_scratch._teardown` -- `DROP TABLE` (`:996`) | same | same fix |
| 7 | `bf_live_scratch._teardown` -- `DROP NAMESPACE` | same | same fix |

Sites 1 and 2 were a hard `ParseException` and blocked all five lakehouse
tests. Site 3 was the worst: inside a bare `except Exception: return False`, so
the parse error came back as "table does not exist" for **all sixteen probes**
-- a confident wrong answer, and `bootstrap()` proceeded to create every table
on that basis. Sites 4-7 meant teardown silently did nothing; two scratch
namespaces leaked on 10-13 while the module docstring claimed both were torn
down.

### Why it cannot recur

Three ungated guards, each verified to FAIL against the shipped code
(I reintroduced the defect and ran them -- see Verification):

- `test_module_never_double_quotes_a_sql_identifier` (`:1306`) -- walks this
  module's own AST and asserts no `ast.JoinedStr` whose literal text contains a
  SQL statement keyword carries a `"`. Scanning f-strings and not every string
  is deliberate: the docstrings quote SQL constantly, and they are plain
  `Constant` nodes.
- `test_every_interpolated_sql_identifier_goes_through_the_quoting_helper`
  (`:1342`) -- the stronger half. Every `FormattedValue` in a SQL string passed
  to `query()` or `_drop_or_warn()` (the two sinks through which this module's
  SQL reaches Spark) must be a direct call to `_q`. This catches a reintroduced
  *unquoted* `f"{TENANT}.{ns}"`, which the double-quote check would not.
- `test_the_exact_reintroduced_shapes_are_absent_from_the_source` (`:1385`) --
  the blunt text pin the task asked for, covering prose and commented-out code
  too. The needles are assembled at run time from `chr(34)`; written out, each
  would match this function's own source and the test could never pass.

Plus `TestQuotingHelper` (`:1227`) -- 7 unit tests: backticks each part, joins
with dots, per-segment quoting of a dotted part, never emits a double quote,
drops empty segments, rejects an embedded backtick, rejects no usable segment.

---

## DEFECT 2 -- the adapter wrapped the wrong load

`_ScratchAdapter.__init__` set `self.load = _CountingLoad(real.load)`, wrapping
the **raw** `BerdlCapability.load`. But `clearinghouse_schema.bootstrap()` calls
`load()` in **schema-only** mode -- no `dataframes`, no bronze `paths` -- and
`BerdlCapability.load` always drives `data_lakehouse_ingest.ingest()`, whose
`build_ingest_config` refuses that shape:

```
ValueError: build_ingest_config: bronze mode (no 'dataframes' given)
requires 'paths' with a 'bronze_base'.
```

raised at `src/kbutillib/domains/kbase/berdl/capability.py:228` (line verified
in this tree). `bootstrap()` has no data to offer -- it takes neither parameter
-- so the two sides cannot meet without a translation layer.

### The fix: a dispatcher behind the counter

`_ScratchAdapter._dispatch_load` (`:442`), with
`self.load = _CountingLoad(self._dispatch_load)`:

- **Schema-only** (`_is_schema_only_load`, `:337`) -> `self._bootstrap.load(...)`,
  a `ClearinghouseBootstrapCapability` built in `__init__` (`:371`). That is the
  shipped component that owns the DDL translation, and the path **proven on this
  pod**: it created the production clearinghouse tables on 2026-10-08 (OP-C1
  C1.3) and the `clearinghouse_parity` tables (OP-C2 C2.2). Each table becomes
  one `CREATE TABLE IF NOT EXISTS ... USING iceberg [PARTITIONED BY (...)]` from
  its own `schema_sql`, so types and partition spec are **declared**, not
  inferred.
- **Data-bearing** -> `self._real.load(**kwargs)` **UNCHANGED**.

The second half is a deliberate choice, not an oversight.
`ClearinghouseBootstrapCapability.load` would forward a data-bearing load
correctly, but it also injects its own `spark=` session. This module exists to
prove the **real** write path, so a production ingest must not be routed through
a test-side wrapper. `test_data_bearing_load_still_reaches_the_real_capability`
(`:1482` class) asserts `"spark" not in real.load_calls[0]`.

`_is_schema_only_load` restates `ClearinghouseBootstrapCapability.load`'s own
branch condition rather than reaching into it; its docstring cites the source,
and `test_schema_only_predicate_matches_the_shipped_adapters_own_branch` pins
all five kwarg shapes.

### How the counting contract was preserved

`test_live_mismatched_write_target_raises_and_never_loads` (passing today)
asserts a refused write makes **exactly zero** `load()` calls -- the dev 1206
property. That only means something if the counter sits in front of *every* load
the code under test can make. So `_CountingLoad` wraps the **dispatcher**, not
one branch of it: both modes pass through the single counter.
`test_counting_load_records_both_modes` drives a schema-only load
(`calls == 1`) and then a data-bearing load (`calls == 2`) through one adapter.
`_CountingLoad` itself is unchanged.

`resolve_write_namespace` / `resolve_probe_namespace` were **not**
reintroduced -- the pre-existing
`test_scratch_adapter_does_not_supply_the_namespace_resolvers` still passes,
and every write verb in the module still passes `dataset=<scratch>` explicitly.

---

## The exception condition `table_exists` discriminates on, and where I found it

`_ScratchAdapter.table_exists` (`:502`) returns `False` **only** for a genuine
table-or-namespace-not-found and **re-raises** everything else. `bootstrap()`
then wraps the re-raise into `BootstrapIndeterminateStateError` with nothing
written (`clearinghouse_schema.py`, the `except Exception as exc` around the
probe) -- the only honest outcome for a lookup that did not answer.

**Discrimination is on the Spark error CONDITION, not the exception class.**
`_spark_error_condition` (`:261`) reads, in order:

1. `exc.getCondition()` -- pyspark 4.x
2. `exc.getErrorClass()` -- pyspark 3.4/3.5
3. the `[NAME]` prefix pyspark renders onto `str(exc)` -- the only thing
   available from a Spark Connect client exposing neither accessor

`_is_missing_relation_error` (`:289`) then tests that condition against
`_MISSING_RELATION_CONDITIONS` (`:227`):

| Condition | Verdict |
|---|---|
| `TABLE_OR_VIEW_NOT_FOUND` | absent -> `False` |
| `TABLE_OR_VIEW_NOT_FOUND_WITH_CANDIDATES` | absent -> `False` |
| `SCHEMA_NOT_FOUND` / `NAMESPACE_NOT_FOUND` / `DATABASE_NOT_FOUND` | absent -> `False` |
| `PARSE_SYNTAX_ERROR` | **re-raise** |
| anything else (auth, connection, timeout) | **re-raise** |

**Where these came from, since pyspark is not importable off-pod and I could not
introspect it:**

- `TABLE_OR_VIEW_NOT_FOUND` -- **measured in this pod**, recorded in
  `agent-io/prds/berdl-smoke-verification/in-pod-results.md` (defect D2:
  `` `kbaseincubator.smoke_scratch_20260803`.`t1` -> TABLE_OR_VIEW_NOT_FOUND``),
  and cited again in `src/kbutillib/domains/kbase/berdl/capability.py:68` and
  `clearinghouse_bootstrap_adapter.py:183`. Not a guess.
- `PARSE_SYNTAX_ERROR` -- verbatim from Albert's 2026-10-13 report, quoted in
  the task prompt and reproduced in the test fixture `_parse_error()`.
- The namespace-level spellings are listed beside `SCHEMA_NOT_FOUND` because
  Spark has used several across versions and an absent namespace is equally
  "not found" for this probe.

Two fallbacks, for a client that publishes no condition: exception **class
name** (`_MISSING_RELATION_TYPES` -- the `NoSuchTable*`/`NoSuchNamespace*`
spellings) and a deliberately narrow **message** regex that requires the
relation KIND beside the phrase, so an unrelated failure quoting "does not
exist" in a stack trace is not read as absence. **A published condition always
wins** -- `test_a_published_condition_wins_over_the_message_text` pins that a
`PARSE_SYNTAX_ERROR` whose message quotes "Table or view not found" still
re-raises.

**This never imports pyspark and never matches on a pyspark class** -- an import
would make the ungated tests uncollectable off-pod.

### Teardown now leaves a trace

`_drop_or_warn` (`:311`) replaces both `except Exception: pass` blocks. It
tolerates only a genuine absence (same predicate) and otherwise emits a
`UserWarning` naming the namespace and the exact statement to run by hand. It
**warns rather than raises** because it runs in a finalizer, where raising would
replace a real test failure with a teardown error.

---

## A THIRD harness defect, found while reconciling (fixed)

Both fixtures passed the **bare** scratch namespace to `bootstrap()`
(`namespace=scratch_namespace`). Only the **dotted, tenant-prefixed** form
addresses a namespace on this cluster -- measured in-pod 2026-09-12, runbook
question (c):

```
table_exists('genome_quality', namespace='kbaseincubator.genome_clearhouse')  -> True
table_exists('genome_quality', namespace='genome_clearhouse')                 -> False
```

The old hand-rolled `table_exists` masked it by prepending `TENANT` itself, so
the probes looked right while `bootstrap()`'s `load()` would have emitted DDL
under a two-part name. `kbaseincubator.<dataset>` is also exactly
`BerdlCapability._write_target_namespace(dataset, tenant)` -- `f"{tenant}.{dataset}"`
-- so the bootstrapped namespace and the namespace the writes land in are now
the same value, not two that happen to agree.

Both fixtures now build `scratch_ns_fqn = f"{TENANT}.{scratch_namespace}"` and
pass it to both `bootstrap(namespace=...)` and `_ScratchAdapter(...)`.
`test_schema_only_ddl_is_backtick_quoted_and_names_the_dotted_namespace` asserts
the emitted DDL contains `` `kbaseincubator`.`clh_live_unit`. `` and no `"`.

**Had I fixed only the two named defects, the acceptance would still have failed
in the pod** -- on a namespace the DDL could not address.

---

## The exact in-pod command that runs the acceptance

```bash
cd ~/projects/KBUtilLib
KBUTILLIB_LIVE_BERDL=1 ~/king/backend/.venv/bin/python -m pytest \
    tests/berdl/test_clearinghouse_live.py -v
```

Expect **8 collected, 8 passed, 0 skipped**. Any `skipped` means the gate or the
locus did not take -- read the skip reason before reporting a result, and read
the **warnings summary**: a `UserWarning` from `_drop_or_warn` is the only signal
that a scratch namespace leaked.

`~/king/backend/.venv/bin/python` is the interpreter that worked on 2026-10-13.
This is added to the runbook in two places: the new
**"Acceptance: the live in-pod test"** subsection at the end of the Backfill
section, and **OP1**, where "which interpreter imports all three packages" is
the open question it answers.

**dev 1534's second half is an OPERATOR concern and I did not touch code for
it.** `~/venvs/kbu-modeling` reports `locus() == "off_pod"` from inside the pod,
so every write refuses there. That is an environment fact, not a code defect:
`BerdlCapability.locus()` tests importability of `berdl_notebook_utils`, and
that venv lacks it. The runbook now names the working interpreter and warns that
a run under `kbu-modeling` skips for the wrong reason, which reads as a pass.

---

## Verification

**Every new test was verified to FAIL against the defective code**, by
reintroducing each defect and re-running. This is the part that matters -- a
static check that passes trivially is worthless:

| Reintroduced defect | Tests that caught it |
|---|---|
| `f'"{TENANT}"."{ns}"'` at the `CREATE NAMESPACE` site | all 3 static guards failed |
| *unquoted* `f"{TENANT}.{ns}"` (passes the double-quote check) | `test_every_interpolated_...` failed, naming `line 598: TENANT` |
| `_CountingLoad(real.load)` | 3 routing tests failed with `ValueError: build_ingest_config: bronze mode ...` at `capability.py:228` -- the exact in-pod error |
| `except Exception: return False` in `table_exists` | 4 tests failed (parse, auth, connection, bootstrap-refuses) |
| `except Exception: return` in teardown | 2 tests failed |

### Runs (all foreground)

```
.task-venv/bin/python -m pytest tests/berdl/test_clearinghouse_live.py -q
->  36 passed, 6 skipped                                            exit 0
```

The 6 skips are the `@_need_live` tests. **h100 is off-pod, so they skip here --
expected, and I did not try to make them run.** The live write/read path is
still UNPROVEN until the command above is run in the pod; that is the whole
point of this task and it is the one thing I could not verify.

```
.task-venv/bin/python -m pytest tests/berdl/ -q \
  --ignore=tests/berdl/test_clearinghouse_derivation.py \
  --ignore=tests/berdl/test_clearinghouse_parity_script.py
->  base 55e395e:  967 passed, 6 skipped                            exit 0
->  branch:       1001 passed, 6 skipped                            exit 0
```

`+34` = exactly the new tests. The two `--ignore`s are collection errors from
`duckdb` missing in this task venv, present at the base commit too.

**Full ungated suite, base vs branch, compared BY NAME** (13 modules excluded
for missing optional deps -- `pandas`, `duckdb`, `rdkit`, etc. -- all
uncollectable at the base commit too; base run done in a detached worktree at
`55e395e`, since removed):

```
base 55e395e:  358 failed, 3651 passed, 425 skipped, 9 errors      (131s)
branch:        358 failed, 3685 passed, 425 skipped, 9 errors      (133s)

comm -13 base_fail branch_fail  ->  EMPTY   (no regressions)
comm -23 base_fail branch_fail  ->  EMPTY   (nothing newly passing)
diff base_fail branch_fail      ->  IDENTICAL, 367 nodeids
```

**The failure name sets are byte-identical.** Zero of the 367 pre-existing
failures are in `tests/berdl/`, the only directory this change touches; they are
all in `cli` (249), `harness` (31), `domains` (28), `biochem` (24), `core` (23),
`interfaces` (10) and are missing-optional-dep failures of this minimal task
venv, consistent with `build-baselines/kbutillib.json`'s own note.

```
.task-venv/bin/python -m ruff check tests/berdl/test_clearinghouse_live.py
->  All checks passed!                                              exit 0
```

(Better than the base commit, which carried one pre-existing `I001`.)
`ruff format --check` reports a diff, but it does so on the **base** version of
this file too, so format is not enforced here and I did not reformat the module
-- that would have buried the change in noise. Every line I added is under 89
columns except one Sphinx cross-reference.

All 6 in-document anchor links in the runbook (5 unique) were checked against
the heading slugs programmatically: **0 broken**, including the new
`#op1-blocking----verify-the-three-package-import` (four hyphens for `" -- "`,
the rule `17e9922` established).

### Baseline cache

`build-baselines/kbutillib.json` exists (base `0f84803`, 2026-09-13, 99
`known_failing`) so I did **not** re-run the full suite to establish a
baseline from scratch. It is stale relative to this tree and was captured in a
differently-provisioned venv, so I used it as the task instructs -- as a NAME
set and a signal that broad optional-dep failures are expected -- and
established the authoritative comparison from a base-commit run of the same
command in the same interpreter. I did not rewrite the cache: it is keyed to a
different commit and env, and overwriting it with this venv's much larger
failure set would make it less useful to the next task, not more.

---

## Divergences between the task prompt and the tree

The prompt was written against a design branch; the tree is `main` at `55e395e`.
Each was fixed and the work carried on.

| Prompt claimed | Tree actually held | What I did |
|---|---|---|
| The second fixture is `bf_live_backfill` | It is **`bf_live_scratch`** (`:938`); `bf_live_backfill` does not exist | Edited `bf_live_scratch` |
| Seven sites at `~221-227`, `~294`, `~316`, `~324`, `~684`, `~698`, `~706` | Seven sites, all present, at `223`, `294`, `317`, `324`, `684`, `699`, `706` | Count and identity exact; edited by symbol as instructed |
| `ClearinghouseBootstrapCapability` "class at ~627" | At **`627`** -- exact | Read it before designing the fix, as instructed |
| `build_ingest_config` raises at `capability.py:228` | **`228`** -- exact | Cited and reproduced in a test |
| "Add one line to the operator runbook's **acceptance section**" | The runbook has **no acceptance section for the live test** -- only `2.3` and `C1.5`, both about `DESCRIBE` checks | Created the minimal home for it: a new `### Acceptance: the live in-pod test` at the end of the Backfill section, plus the interpreter line in OP1 where the question it answers lives |
| Two defects, "both in `tests/berdl/test_clearinghouse_live.py`" | **Three.** Both fixtures also passed the BARE namespace to `bootstrap()` | Fixed it -- see above. Still test-module-only, so no scope widening |
| "The product code is not believed to be at fault" | Correct. Nothing under `src/` needed to change | `src/` untouched |

### One thing I had to add that the prompt did not mention

`_ScratchAdapter.__init__` gained an optional keyword `spark=None`, forwarded to
`ClearinghouseBootstrapCapability(real, spark=spark)`. Without it the schema-only
DDL path cannot be exercised off-pod at all: the bootstrap adapter resolves a
session lazily via `InPodTransport()`, which raises off-pod, so requirement (a)
of the task's test list would be untestable. On the pod the fixtures pass
nothing and it resolves its own in-pod session, exactly as the production
bootstrap does. This fakes the **remote collaborator** (the Spark session), which
the task sanctions; the adapter itself is never faked.

## Environment note

The task venv `.task-venv` had no `pytest`. I installed `pytest` and `ruff`
into **that venv only** -- this task's own environment. No service venv, no
system interpreter, nothing outside the worktree was touched.
