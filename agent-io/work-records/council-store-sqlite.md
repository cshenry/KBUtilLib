# Work Record — council-store-sqlite

## task_id
council-store-sqlite

## branch
maestro/developer/background-reference-only-agent--council-store-sqlite

## base commit
56955f0 (`docs(work-record): fill in the second commit sha`)

## commit_shas
- e6102eb — feat(council): store schema, SqliteStore and the CouncilStore contract
- eafb5cb — docs(work-record): record the council store milestone

## summary
Part 1 of 2 of the council store. Adds
`src/kbutillib/domains/ai/council/store/` — `schema.py` (one SQLAlchemy Core
`MetaData` for every table in the schema, including the ones later milestones
use), `sqlite.py` (`SqliteStore`, implementing the non-queue half of the STORE
CONTRACT), `errors.py` (the store's failure taxonomy) and `__init__.py`
(`open_store`) — and replaces the PROVISIONAL `CouncilStore` in
`protocols.py` with the full 43-method contract. Tests:
`tests/domains/ai/council/store_contract.py` (reusable fixtures, parametrised
over SQLite and PostgreSQL) and `tests/domains/ai/council/test_store_contract.py`
(101 tests). No test makes a network call, spends a token, needs a CLI login
or needs a GPU.

`src/kbutillib/domains/ai/argo_utils.py` and
`src/kbutillib/domains/ai/ai_curation_utils.py` are byte-identical to the base
commit (`git diff --stat 56955f0 -- <both>` is empty), as is `pyproject.toml`.

## files_touched
- `src/kbutillib/domains/ai/council/store/schema.py` (new, 402 lines)
- `src/kbutillib/domains/ai/council/store/sqlite.py` (new, 1258 lines)
- `src/kbutillib/domains/ai/council/store/errors.py` (new, 80 lines)
- `src/kbutillib/domains/ai/council/store/__init__.py` (new, 103 lines)
- `src/kbutillib/domains/ai/council/protocols.py` (modified — `CouncilStore`
  replaced; `Backend` and `QueryRenderer` untouched)
- `tests/domains/ai/council/store_contract.py` (new — fixtures and row builders)
- `tests/domains/ai/council/conftest.py` (new — re-exports the fixtures)
- `tests/domains/ai/council/test_store_contract.py` (new — 101 tests)

## environment

SQLAlchemy and psycopg are not in a fresh task venv, and neither were pytest,
pandas, duckdb or httpx. Installed into THIS TASK'S OWN venv
(`.task-venv/` inside the worktree — no shared or service venv was touched):

```
python -m pip install -e '.[council]'      # SQLAlchemy 2.1.4, psycopg 3.3.6
python -m pip install pytest pytest-asyncio pytest-cov   # pytest 9.1.1
python -m pip install -e '.[dev,ai]'       # pandas, duckdb, httpx, ipykernel
python -m pip install ruff
```

Interpreter: CPython 3.12.3. A reviewer must do the same in its own venv; a
test that fails on ImportError here is an environment gap, not a result.

## verification

| Command | Exit | Result |
|---|---|---|
| `python -m pytest tests/domains/ai/council/ -q -p no:cacheprovider` | 0 | 195 passed, 93 skipped |
| `python -m ruff check --output-format=concise src/kbutillib/domains/ai/council tests/domains/ai` | 0 | All checks passed |
| full suite at base `56955f0`, clean tree, same venv (ignore list below) | 1 | **26 failed**, 4249 passed, 429 skipped |
| same command on this branch | 1 | **26 failed**, 4350 passed, 522 skipped |

Ignore list (copied from `build-baselines/kbutillib.json`'s `command`):
`--ignore=tests/berdl/test_clearinghouse_derivation.py
--ignore=tests/biochem/test_escher_utils.py
--ignore=tests/biochem/test_ms_biochem_deltag.py
--ignore=tests/kbase/test_kb_annotation_utils_parquet.py
--ignore=tests/modeling/test_comprehensive_gapfill_wrapper.py
--ignore=tests/modeling/test_ms_reconstruction_utils.py --ignore=tests/notebook`

**Regression comparison was by NAME against a base run in this venv, not
against the cached baseline** — per the dispatcher's note, and for the reason
the previous task recorded: `build-baselines/kbutillib.json` was captured at
`0f84803` on 2026-09-13 in a different, more minimal venv and cannot
adjudicate this branch. The base run was taken first, on a clean tree at
`56955f0`, before any file was written.

**The two failure sets are the same 26 nodeids; the diff is empty in both
directions** (`comm -13` and `comm -23` over the sorted FAILED lists both
produced nothing). The +101 passes are exactly this task's new tests; the +93
skips are the PostgreSQL parametrisations, which skip with the stated reason
`KBU_COUNCIL_TEST_PG_DSN is not set`.

I did NOT rewrite `build-baselines/kbutillib.json`. One exists, the task
instruction only requires writing it when absent, and overwriting a shared
cache with numbers from a venv provisioned differently again would not help
the next task any more than it helped this one.

## success_criteria_check

**Criterion 1.** PASS.
- `store/schema.py` defines all 13 tables: `schema_version`, `query_types`,
  `jobs`, `items`, `calls`, `attempts`, `endpoint_state`, `workers`,
  `daemon_lease`, `local_models`, `model_resources`, `model_loads`,
  `gpu_lease` (`TABLE_NAMES` enumerates them; `TestSchema::
  test_init_creates_every_table_and_stamps_version_1` asserts every one is
  created on disk).
- `SqliteStore` implements the non-queue contract; the ten queue methods
  raise `NotImplementedError("store-queue-ops")`, pinned by a parametrised
  test over all ten.
- `protocols.py` declares `CouncilStore` with 43 methods.
- `pyproject.toml` already carries the `council` extra (see reconciliation 1).
- `store_contract.py` parametrises over SQLite and PostgreSQL;
  `test_store_contract.py` covers every listed case and passes on SQLite.
- No test that passed at `56955f0` fails on this branch.

**Criterion 2 (no-injection check).** PASS.
`TestSqliteEntryPoint::test_open_store_creates_the_file_its_parents_wal_and_the_version`
uses no fixture but `tmp_path`, no monkeypatch and no stub. It asserts the
parent directory does NOT exist, calls
`open_store(f"sqlite:///{tmp_path}/sub/dir/council.sqlite")` and
`init_schema()`, then asserts the file exists on disk, `PRAGMA journal_mode`
returns `"wal"` (and, additionally, `foreign_keys` is 1 and `busy_timeout` is
30000), `schema_version() == 1`, and that a SECOND `open_store()` of the same
URL in a new store object reads version 1.

**Criterion 3.** PASS. `TestCouncilStoreProtocol` holds three tests:
`test_the_protocol_declares_exactly_the_contract` asserts
`{n for n in dir(CouncilStore) if not n.startswith("_")} == CONTRACT_METHODS`,
where `CONTRACT_METHODS` is the prompt's 43 names written out verbatim in
contract order — so a missing, added or misnamed method fails;
`test_an_implementation_satisfies_the_protocol` asserts
`isinstance(store, CouncilStore)`; and
`test_every_contract_method_is_present_on_the_implementation` asserts each of
the 43 is callable on the store.

## reconciliation (prompt vs tree)

1. **The `council` extra already exists in `pyproject.toml`.** The prompt says
   to add it ("Also add the `council` extra to pyproject.toml (sqlalchemy
   >=2.0, psycopg[binary] >=3.1)"); the previous task landed it at lines
   82-100 with exactly those floors and the commentary the prompt asks for.
   The dispatcher's note said the same. I added nothing and left
   `pyproject.toml` byte-identical.
2. **`protocols.py`'s `CouncilStore` was PROVISIONAL and is now replaced.**
   The previous task's prompt omitted the STORE CONTRACT section and it wrote
   a reconstructed 12-method protocol labelled PROVISIONAL, naming this task
   as the one that would refine it. Replaced wholesale with the 43 contract
   methods. This is the permitted `protocols.py` edit; `Backend` and
   `QueryRenderer` are untouched. Two follow-on edits were needed in the same
   file: the typing import gained `ContextManager`, `Set`, `datetime` and
   `timedelta`, and the now-unused `CouncilVerdict` import was dropped (ruff
   F401).
3. **`council/__init__.py` needed no edit.** Its `_MODULE_MAP` already routes
   `open_store` -> `store`, `SqliteStore` -> `store.sqlite` and
   `PostgresStore` -> `store.postgres`, so
   `from kbutillib.domains.ai.council import open_store, SqliteStore` works
   with no change (verified).
4. **`config.py` already refuses Dropbox paths.** `_resolve_store_url` raises
   `ConfigError` for a sqlite URL under a synced directory. The prompt asks
   the STORE to refuse with a `ValueError`. I implemented the refusal
   independently in `_resolve_sqlite_url`, because the store must refuse a
   path handed to it directly (a `--store` flag, a test, `open_store`) and not
   only one that came through the config loader. `StorePathRefused` derives
   from both `CouncilException` and `ValueError`, so the prompt's `ValueError`
   holds and the council's single catch-all still works.
5. **The partial unique index has five key columns, not six.** The prompt
   writes `UNIQUE (job_id, item_id, role, seat_name, upstream_digest, live)`
   and then says to implement it as "a partial unique index WHERE live". Those
   are the same constraint: `live` is the predicate, not a key column
   (including it as a key would make the index a no-op, since every indexed
   row would have `live` true anyway). Implemented as the five-column index
   with `sqlite_where`/`postgresql_where` = `live`, and both halves are tested
   — a second live call is refused, and a superseded call does not block a
   replacement.
6. **Row dicts, not models.** The contract's signatures take and return
   `dict`s (`create_job(job: dict, items: list[dict], ...)`), so the store
   reads and writes plain dicts keyed by column name. It accepts pydantic
   models wherever a JSON column is written (`_jsonify` calls `model_dump
   (mode="json")`), so a caller can hand it a `CouncilVerdict` or a `Roster`
   without converting first.

## design decisions not dictated by the prompt

- **`UtcDateTime`, a `TypeDecorator`, instead of a bare
  `sa.DateTime(timezone=True)`.** The prompt says timestamps are
  "`sa.DateTime(timezone=True)` in UTC", but SQLAlchemy's SQLite dialect
  formats a datetime field by field and silently discards `tzinfo`, returning
  a naive value. On this fleet local time is not UTC, so the naive round trip
  shifts the instant by hours. The decorator converts to UTC on the way in and
  re-tags on the way out, so both dialects behave identically. **A naive
  datetime is refused, not assumed to be UTC** — assuming would turn a
  caller's `datetime.now()` into a wrong instant with no symptom. The refusal
  is raised in `_prepare` (a plain `ValueError` naming the column) as well as
  in the decorator, because SQLAlchemy wraps a type-level raise in
  `StatementError`, which is not a `ValueError`.
- **`BEGIN IMMEDIATE` is the default for `transaction()`, not an opt-in.**
  The prompt names it for `claim_calls`. Every read-then-write in this class
  (`add_tokens`, `reset_budget`, the lease primitives, `put_query_type`) has
  the same lost-update exposure, so the connection is put in autocommit
  (`isolation_level = None`) and this module emits its own `BEGIN IMMEDIATE`.
  `transaction(immediate=False)` is available. Setting `isolation_level = None`
  is also what lets `PRAGMA journal_mode=WAL` run at all, since that pragma
  cannot execute inside a transaction.
- **`transaction()` is re-entrant.** A nested call joins the transaction in
  progress rather than deadlocking against its own write lock, which is what
  lets the next milestone's worker complete a call, record its attempt, add
  tokens and advance the item as one unit while each of those stays usable
  alone. The active connection is held in a `threading.local`.
- **`get_endpoint_state` returns a zeroed row rather than `None`.** Every
  caller asks "is this halted and how much have I spent?"; returning `None`
  would put the same two-line default at every call site.
- **`release_*_lease` expires the row to the epoch, not to "now".** A released
  lease must be free for a successor whose clock the releasing process does
  not share; the tests drive the leases on an injected `now`, and a real-clock
  expiry made them fail for a reason that would also bite in production.
- **Unknown column keys raise instead of being dropped** (`_prepare`), and
  rows are widened to the full column set before `executemany` (`_filled`),
  which is required because one compiled statement serves the whole batch.
- **`get_query_type(name)` sorts versions naturally, not lexicographically.**
  `"10"` must beat `"9"`; plain string ordering would silently return a
  superseded spec the first time a query type reaches version 10.
- **`acquire_*_lease` refuses the CURRENT holder too** while the lease is
  unexpired — the contract says "succeeds only if no row or the row expired",
  and `renew_*` is the holder's method. Tested explicitly so the next
  milestone's daemon loop does not discover it by accident.
- **An extra `close()` (and context-manager protocol) on `SqliteStore`**, not
  on the Protocol. The contract's 43 names are exactly what `CouncilStore`
  declares; `close()` exists so a test or CLI run can release the file handle
  deterministically.
- **`store/errors.py` rather than adding to `types.py`.** The prompt forbids
  editing `types.py`, and these failures are properties of the persistence
  layer: `types.py` must stay importable with no database driver installed.
  `JobCancelled` is the one error not named in the prompt; `add_calls` and
  `settle_item` need something to raise when they "refuse items of a
  cancelled job".
- **`open_store` checks for `psycopg` with `importlib.util.find_spec` before
  importing `.postgres`.** Letting the import fail would blame a missing
  module when the thing the user must install is the driver.
- **Fixtures are re-exported through `tests/domains/ai/council/conftest.py`.**
  Importing a fixture into a test module makes every test that uses it shadow
  the imported name with a parameter — 80 ruff F811s. A conftest has no test
  functions, so there is nothing to shadow, and `store_contract.py` stays
  equally reusable by the next milestone's test module.
- **`foreign_keys=ON` plus a composite FK from `calls` to `items`.** Not
  required by the prompt, but it is what makes the "a call for an unknown
  item is refused" case an IntegrityError instead of an orphan row, and it is
  one of the two atomicity injection points the contract test uses.

## inferred rather than verified

- **The PostgreSQL half of the contract suite has never run.**
  `PostgresStore` is a later milestone, so the parametrisation skips with the
  stated reason `kbutillib.domains.ai.council.store.postgres is not
  implemented yet` and turns itself on the moment that module lands. The
  fresh-schema mechanism (`CREATE SCHEMA`, then a DSN carrying
  `options=-csearch_path=<schema>`, dropped `CASCADE` at teardown) is written
  against libpq's documented `options` parameter and is UNTESTED — no
  PostgreSQL was reachable from this task, and the prompt forbids a test that
  needs one. It also assumes `PostgresStore(url)` accepts a URL as its first
  positional argument, which is the one thing `open_store` guarantees.
- **The schema's portability to PostgreSQL is reasoned, not executed.** The
  partial unique index, `sa.JSON`, `String(64)` and the quoted `"user"` column
  (a reserved word, which SQLAlchemy quotes automatically) are all chosen to
  work on both, but only SQLite ran.
- **`migrate()` has nothing to migrate.** At version 1 it is `init_schema()`
  for an unstamped database and a no-op for a current one; the upgrade path is
  inferred from "v1: no-op beyond init" and will need real work at version 2.
  A database stamped NEWER than this code is refused rather than downgraded.
- **`check_version=False`** is my addition, not the prompt's: without it
  `kbu council db migrate` could never open the databases the version check
  exists to reject, since the check runs in the constructor. The CLI does not
  exist yet, so nothing calls it but the test.
