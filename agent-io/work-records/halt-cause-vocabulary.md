# Work record: implement-cshenry-kbutillib-118 (halt_cause)

- task_id: task-2c62edac
- branch: maestro/developer/implement-cshenry-kbutillib-118--task-2c62edac
- base: 480a61d (`main`; did not move during the task)
- issue: cshenry/KBUtilLib#118 -- give the council's endpoint halt a closed-vocabulary `halt_cause`

## Summary

An endpoint halt now carries a third field beside `halt_kind` and
`halt_message`: a closed cause code saying WHY, so PersistentAI's campaign
watchdog can decide whether an automatic resume is allowed without parsing
either of the other two. The eight codes and the four resumable ones are
module constants; the cause is classified from STRUCTURED signals at the one
place that holds the HTTP response, rides on the `CouncilError`, is written by
the worker's existing halt path, and appears in `foreign_halts()` -- the
record that reaches `RUN.json`.

Both real halts (2026-10-30 ARGO, 2026-10-31 `mag`) were `halt_kind='auth'`;
they now classify `unclassified_auth_denial` (resumable -- it cleared itself
in 4h10m) and `administratively_disabled` (terminal) respectively. That
difference, driven through the shipped backend and worker into a real store,
is the test the field exists for.

## files_touched

New:
- `src/kbutillib/domains/ai/llm/halt_causes.py` -- the vocabulary, the signal
  tables, `classify_http_halt_cause()`, `with_halt_cause()`.
- `src/kbutillib/domains/ai/council/halt_causes.py` -- the council-side
  module the task asked for; re-exports the above (see Divergences).
- `tests/domains/ai/council/test_halt_causes.py` -- 44 tests.
- `tests/domains/ai/council/fixtures/real_halt_cases.json` -- the two real
  provider responses, copied from PersistentAI's fixtures (read-only there).

Changed:
- `src/kbutillib/domains/ai/llm/types.py:111` -- `LLMError.halt_cause`.
- `src/kbutillib/domains/ai/llm/apis/http_common.py` -- `_status_error()`
  classifies (401/403/5xx/structured quota/scope/revoked/admin signals);
  `_connect_error()`, the timeout and the generic `httpx.HTTPError` handlers
  carry `network_error`; new `_parsed_body()` reads the body for signal only.
- `src/kbutillib/domains/ai/llm/apis/http.py` -- `argo_refusal_error()` passes
  the existing signature table's own verdict: auth notice ->
  `unclassified_auth_denial`, usage notice -> `quota_exhausted`.
- `src/kbutillib/domains/ai/llm/apis/anthropic.py` -- `anthropic_status_error()`
  classifies; its two existing spend-limit detectors pass `quota_exhausted`;
  SDK timeout/connection errors carry `network_error`.
- `src/kbutillib/domains/ai/council/store/schema.py` -- `endpoint_state.halt_cause`
  (nullable `String(32)`) and `("endpoint_state", "halt_cause")` in
  `ADDITIVE_COLUMNS`. `SCHEMA_VERSION` stays 2.
- `src/kbutillib/domains/ai/council/store/sqlite.py` -- `set_halt(cause=...)`,
  `reset_halt`/`reset_budget` clear it, and `_backfill_additive_columns()`
  called from `__init__` so an existing store gains the column on OPEN.
- `src/kbutillib/domains/ai/council/protocols.py:376` -- the protocol signature.
- `src/kbutillib/domains/ai/council/engine/worker.py:3073` -- `_park()` passes
  `cause=getattr(error, "halt_cause", None)`.
- `src/kbutillib/domains/ai/council/__init__.py` -- `HALT_CAUSES`,
  `RESUMABLE_HALT_CAUSES` in the lazy export map.
- `src/kbutillib/domains/genome/ontology_mapping/council_run.py:602` --
  `foreign_halts()` entries carry `halt_cause`, key always present.
- `src/kbutillib/domains/kbase/berdl/council_schema.py` +
  `tests/berdl/test_council_schema.py` -- the clearinghouse archive drops
  THREE halt columns now, not two (see below).
- `tests/domains/test_ontomap_council_run.py` -- the `_FakeStore` keeps the
  cause, so the fake still implements the protocol it stands in for.

## Classification rules as implemented

Order is most-specific-signal first, then status:

| signal / status | cause |
|---|---|
| `type`/`code`/`provider_specific_fields.amsc_reason` in `{amsc_project_disabled, project_disabled}` | `administratively_disabled` |
| `invalid_grant`, `revoked`, `token_revoked`, ... | `credentials_revoked` |
| `insufficient_scope` | `insufficient_scope` |
| `insufficient_quota`, `quota_exceeded`, `billing_hard_limit_reached`, `credit_balance_too_low`, `enforced_spend_limit_reached` | `quota_exhausted` |
| 401 + `invalid_token` / an `expired` type or code | `token_expired` |
| any other 401/403, and the ARGO HTTP-200 auth notice | `unclassified_auth_denial` |
| >= 500 | `upstream_5xx` |
| `httpx`/SDK timeout, connect and generic HTTP error classes | `network_error` |
| anything else, incl. a windowed 429 and every budget halt | `None` |

Deliberate `None`s: budget halts (worker.py:3097/3106, daemon.py:1647), the
supervisor's own halts (council_run.py:711), a SUSTAINED-429 halt
(`_note_rate_limit`, worker.py:2598 and 1945), and a missing `$ARGO_USER`
(no response, no signal). A sustained windowed rate limit is neither
resumable on the next tick nor permanently terminal, so neither set is honest
about it and `None` is the only non-guess available.

## success_criteria_check

- closed vocabulary exported as module constants, eight + the exact four: pass
- classify where the HTTP response is in hand, carried on the error into
  `set_halt(..., cause=...)`: pass
- structured signals only; no new prose regex (the ARGO and Anthropic
  detectors pass their own verdicts): pass
- additive nullable column through the existing path, no `SCHEMA_VERSION`
  bump, existing store gains it without `kbu council db migrate`: pass
- `halt_cause` in every `foreign_halts()` entry, key present, `None` when
  absent: pass
- both real cases, driven through a mocked HTTP response into the store and
  into `foreign_halts()`: pass
- one test per vocabulary member with a structured trigger: pass
- `reset_halt` clears the cause: pass
- `RESUMABLE_HALT_CAUSES` subset with the exact four: pass
- out of scope and untouched: the resume adapter, PersistentAI, `halt_kind`
  values and semantics

## tests_run

Interpreter `~/venvs/kbdl/bin/python` (CPython 3.11.15, pytest 9.1.1), all
runs `TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src python -m pytest ... -q
-p no:cacheprovider --no-header` from this worktree.

BEFORE, at 480a61d, selection `tests/domains/ai/council/
tests/domains/test_ontomap_council_run.py
tests/domains/test_ontomap_council_status.py tests/cli/test_ontomap_cli.py`:
**6 failed, 1488 passed, 10 skipped** (264 s), exit 1.

AFTER, identical selection: **6 failed, 1532 passed, 10 skipped** (273 s),
exit 1. The six are node-ID-identical to the before run and are the
machine-local `test_types_config.py::TestPackagedDefaultConfig` set already
recorded in `build-baselines/kbutillib-h100-main-6310d61.json` (the h100 user
config leaks past the monkeypatched `HOME`). +44 passed = the new module.
ZERO regressions.

Wider affected set (adds `tests/domains/ai/llm`,
`test_ontomap_pipeline_rehearsal.py`, `test_council_cli.py`,
`tests/berdl/test_council_schema.py`, `tests/berdl/test_council_archive.py`):
**7 failed, 1919 passed, 77 skipped** (354 s). The seventh is
`tests/domains/ai/llm/test_static_headers.py::TestPackagedExample::test_the_default_config_declares_no_mag_endpoint`,
the same HOME leak: that module is **40 passed, 0 failed** when run with
`HOME` pointed at an empty directory, and this diff touches no config file.

`tests/berdl/test_council_schema.py` failed once mid-task and is FIXED, not
excused: it asserts the clearinghouse archive drops exactly the halt columns,
and `halt_cause` is one by the archive's own stated reasoning (live
operational control, not provenance). The allow-list already excluded it; the
test's and the docstring's enumeration of "the two halt columns" is what was
stale. Both now say three. 154 passed.

No ruff available in any venv on this machine (`python -m ruff` absent from
`~/venvs/*`), so the lint gate was NOT run. Unused imports were checked with
an AST pass over the new files instead.

## Divergences between the task prompt and the tree

1. **"Export `HALT_CAUSES` and `RESUMABLE_HALT_CAUSES` from one module in
   `src/kbutillib/domains/ai/council/`."** The classification point is
   `llm/apis/http_common.py`, and `llm/types.py` states twice that that layer
   "is domain-neutral and must not import the council". Defining the
   vocabulary in the council and importing it from the LLM layer would break
   that rule. Resolved by defining it in `llm/halt_causes.py` and having
   `council/halt_causes.py` re-export both names (and the eight constants),
   so the council-side module the prompt asks for exists and exports both
   names from one place, with the layering intact. Both are also on the
   `council` package itself via the lazy map.
2. **"`store/sqlite.py` `set_halt` is at line 1448 ... the protocol signature
   at `protocols.py:376` ... column definitions at `store/schema.py:341`."**
   All three were accurate at 480a61d.
3. **"`sqlite.py` lines 167-184" as part of the additive-column path.** Those
   lines are the store-identity commentary; the additive path is
   `add_missing_columns()` at ~1043 (cited correctly) and `migrate()` at
   ~905. Used those.
4. **"An existing store must open and gain the column without `kbu council db
   migrate`."** At 480a61d, `add_missing_columns()` was reachable ONLY from
   `migrate()`; opening a store did nothing. So the requirement needed a new
   call site, which is `_backfill_additive_columns()` in `SqliteStore.__init__`
   (guarded by `check_version`, failures swallowed at debug level so a
   read-only or concurrently-written store still opens). This makes
   `calls.normalised` and `attempts.queued_at` self-heal on open too.
5. **`error` at `_park()` is a `CouncilError`** = `llm.types.LLMError`, a
   pydantic model -- so the new field is a model field and the existing
   `rate_limited` flag was the precedent for `model_copy(update=...)`.
6. **The 403 `mag` case reaches `http_common._status_error` through
   `OpenAICompatibleBackend._status_error`'s `super()` call**, which is why
   one classifier serves both backends. Note that a 503 on that backend
   returns `model_unavailable` BEFORE `super()`, so it parks and never
   becomes `upstream_5xx` -- correct, because it is not a halt.

## caveats

- `kbu council endpoints status` (interfaces/cli/council.py:255) still shows
  `halt_kind` only. Adding a cause column there would help the operator who
  has to clear a halt, but the task enumerated its touch points and the CLI
  was not among them, so it was left alone.
- The sustained-rate-limit halt in the batched path (worker.py:1945) calls
  `set_halt` directly and passes no cause, same as the single-call path.
- `classify_http_halt_cause` matches signal values by EQUALITY, lowercased.
  A provider that states its reason only in prose gets `None`.
- The ARGO body in the fixture is the one captured in the 2026-10-30
  `RUN.json` halt message, i.e. ARGO's real 200 text; the `mag` body is the
  verbatim JSON from the task prompt, which is the same response the
  truncated 500-character excerpt in `case1-*.RUN.json` came from.
