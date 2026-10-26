# Work Record: kbdl-ud-client

**task_id:** kbdl-ud-client
**role:** developer
**dispatched_by:** Jane (e session 71d48f60), 2026-10-26 — item 2 of 2 in a serial bundle
**issue:** kbaseincubator/KBDLJobRunningPrototype#15 (and #11, the other silent loss)

## branch

- KBUtilLib: `maestro/developer/serial-bundle-on-one-kbutillib-b-kbu-prov-typing-ud-client`
- base commit: `0bb8a88` (off `main`); item 1 of this bundle is `5b7342b`, and is
  file-disjoint from this item.

## summary

`KBDLServiceUtils` gains a batch confirmation path, so a bulk load can prove
its uploads were actually registered instead of trusting a `POST /objects` 202.

Both losses this addresses were the same mistake: a 202 means **accepted for
registration, not stored**, and the client recorded it as success.

- **#11**, 2026-10-11, 3,669 archives from 96 concurrent uploaders: `POST
  /objects` returned 202 `{job_id}`; the `KBDLUploadObject` job then failed
  `failure_code=internal_error`, `failure_message="database is locked"`. The
  object was never registered. (`db.connect()` already sets
  `PRAGMA busy_timeout=5000` plus WAL and `synchronous=NORMAL`;
  `atb_ledger.py` and `atb_recompress.py` both document the lock still
  happening with it set, so this is not fixable client-side — only
  *detectable*.)
- **#15**, 2026-10-15, 2,374 groups: job `56e5d4091d6547869bba40b06ec3a7e9`
  failed `worker_lost` with `requeue_count=1`; the lease set at claim was
  never renewed. Lease expiry is never retried —
  `leases.reclaim_expired_leases` fails every expired lease, and
  `requeue_count` counts only graceful SIGTERM requeues
  (`JobService.requeue`, `MAX_REQUEUE_COUNT=3`). kbdl-job-concurrency-v1 D10
  chose "never retry a lost lease" because jobs are non-idempotent in general;
  `KBDLUploadObject` is the exception (content-addressed with
  `transform="none"`, `ObjectStore.put` dedups on `object_id`, and the
  transforms are byte-deterministic per kbdl-upload-transform-v1: gzip
  `mtime=0`, no filename), so a caller that *detects* the failure can safely
  retry it.

Confirmation is now one method call over a batch endpoint rather than N
`GET /jobs/{id}` round trips, which matters at 3,669 jobs against a service
whose SQLite ledger is already the contended resource.

## what changed — `src/kbutillib/domains/external/kbdl_service_utils.py`

### `_STATUS_MANY_MAX_IDS = 1000` (`:235-239`)

The server contract's cap on `POST /jobs/status`. Above it (or for a malformed
id) the service answers 400.

### `KBDLServiceUtils.status_many(job_ids)` (`:836-895`)

Placed after `clear_job`, with the rest of the job-lifecycle methods. Returns
`{"jobs": {job_id: status}, "unknown": [job_id, ...]}`.

- De-duplicates, preserving first-seen order (so the request body is
  deterministic and testable).
- Chunks into `POST /jobs/status` bodies of at most `_STATUS_MANY_MAX_IDS` ids
  and merges the per-chunk `jobs` dicts and `unknown` lists into one answer.
- Goes through the existing `self._request(...)` plumbing, so it picks up
  `_auth_headers`, the client timeout, and the full `_raise_for_status` typed-
  error mapping for free — a malformed id surfaces as `KBDLBadRequestError`
  (or `KBDLInvalidInputError` when the service supplies a `code`), not as a
  bare `HTTPError`.
- An empty `job_ids` makes **no** request and returns empty results; the
  service rejects a zero-id body, and there is nothing to learn from a round
  trip to discover that.
- The docstring records that **another user's job and a nonexistent job are
  indistinguishable** in `unknown`, exactly as `GET /jobs/{id}` renders both
  as 404.

### `KBDLServiceUtils.poll_jobs_until_terminal(...)` (`:953-1071`)

Placed immediately after `poll_until_terminal`, whose signature it mirrors
(`interval`, `timeout`, `sleep_fn`, `time_fn` — all injectable, so the tests
never really sleep). Returns `{job_id: final status dict}`.

- Each round polls **only the ids not yet terminal**: the pending list is
  rebuilt from the round's response, so a load of thousands collapses round by
  round instead of re-asking about every id every time.
- `interval` defaults to **5.0**, not `poll_until_terminal`'s 2.0, because one
  round here covers up to 1,000 jobs. (The task specified 5.0.)
- **Never raises merely because a job FAILED** — the failed status is returned
  like any other and the caller decides. A bulk load wants the whole picture,
  not an exception on the first of 3,669 failures. This matches
  `poll_until_terminal` and deliberately differs from `submit_and_wait`, which
  raises `KBDLJobFailedError`.
- Raises `KBDLNotFoundError` (the module's existing class) if the service
  reports **any** id as unknown, with the message listing those ids. An
  unknown id cannot resolve by waiting, and for a bulk load it most likely
  means the client is confirming ids it never successfully submitted.
- Raises `TimeoutError` naming how many of how many ids are still pending
  (`"2 of 3 job(s) did not reach a terminal state within 50.0s"`).
- Tolerates `requests.exceptions.ConnectionError` / `Timeout` with **the same
  `_POLL_MAX_CONNECTION_ERRORS` bound on CONSECUTIVE failures, the same
  `self.log_warning` call, and the same reset-on-successful-poll** as
  `poll_until_terminal`. A lost connection is not a lost job: the jobs run on
  the service, and an interrupted POST says nothing about them.
- An id in neither `jobs` nor `unknown` should not occur under the server
  contract. It stays pending rather than being invented as terminal, and
  surfaces as a `TimeoutError`. Commented at the branch.

### `upload_object` docstring (`:1126-1209`)

**Behaviour unchanged** — the request construction and the `return
response.json()` are untouched; this is docstring-only. Added:

- A paragraph headed `A 202 MEANS ACCEPTED FOR REGISTRATION, NOT STORED`,
  naming both incidents and their failure codes.
- A paragraph naming `poll_jobs_until_terminal` (or `poll_until_terminal` for
  a single upload) as the required confirmation step for a bulk load, and
  stating that a retry is safe because `KBDLUploadObject` is idempotent.
- A paragraph documenting `expected_object_id`: the sha256 hex of the uploaded
  bytes, i.e. the `object_id` the content *will* have — present **only** for
  `transform="none"`, absent otherwise because the stored bytes are not the
  uploaded bytes and the id cannot be known up front.
- The `Returns:` block updated so the 202 form reads
  `{"job_id": ..., "expected_object_id": ...}` for `transform="none"` and
  `{"job_id": ...}` with no `expected_object_id` otherwise, plus a pointer
  that only the 201 form means the object exists now.

## tests added — `tests/external/test_kbdl_service_utils.py:1113-1551`

All offline, using the module's existing `FakeSession` / `RaisingSession` /
`FakeResponse` / `make_client` stubbed-transport fixtures — no new fixture
machinery. One local helper, `_status(state, **extra)` (`:1129`), builds a
record with the full contract shape (`job_type`, `state`, `submitted_at`,
`started_at`, `finished_at`, `failure`, `auto_retry_count`).

**`status_many`:**

| Test | Asserts |
|---|---|
| `test_status_many_posts_the_ids_and_merges_the_response` | `POST` to `/jobs/status` with body `{"job_ids": [...]}`; `jobs` and `unknown` both surfaced |
| `test_status_many_deduplicates_preserving_first_seen_order` | `["j2","j1","j2","j1","j3"]` → body `["j2","j1","j3"]` |
| `test_status_many_chunks_2500_ids_into_exactly_three_posts` | **exactly three POSTs**, chunk sizes `[1000, 1000, 500]`, every chunk `<= _STATUS_MANY_MAX_IDS`, all 2,500 ids merged into one answer |
| `test_status_many_merges_unknown_across_chunks` | `unknown` concatenated across chunks |
| `test_status_many_makes_no_request_for_an_empty_id_list` | zero calls, empty result |

**`poll_jobs_until_terminal`:**

| Test | Asserts |
|---|---|
| `test_poll_jobs_repolls_only_the_ids_still_pending` | the three request bodies are exactly `[["j1","j2","j3"], ["j2","j3"], ["j3"]]` |
| `test_poll_jobs_returns_every_status_once_all_are_terminal` | one round, both statuses returned |
| `test_poll_jobs_returns_a_failed_job_rather_than_raising` | the 2026-10-11 shape — `failure.message == "database is locked"` is **returned**, not raised |
| `test_poll_jobs_raises_not_found_naming_the_unknown_ids` | `KBDLNotFoundError` whose message names `j2` and `j3` |
| `test_poll_jobs_timeout_message_includes_the_pending_count` | `TimeoutError` containing `"2 of 3"` and `"50.0s"` |
| `test_poll_jobs_tolerates_a_connection_error_streak_below_the_bound` | exactly `_POLL_MAX_CONNECTION_ERRORS` errors then success → returns |
| `test_poll_jobs_re_raises_one_connection_error_above_the_bound` | bound + 1 → `ConnectionError` propagates |
| `test_poll_jobs_tolerates_a_timeout_exception_too` | `requests.exceptions.Timeout` is in the tolerated set |
| `test_poll_jobs_successful_round_resets_the_connection_error_streak` | bound + 2 *non-consecutive* errors still completes |
| `test_poll_jobs_still_honours_its_timeout_while_tolerating_errors` | tolerance does not let a wait outlive its timeout; `"2 of 2"` |
| `test_poll_jobs_deduplicates_before_polling` | `["j1","j1","j1"]` → body `["j1"]` |
| `test_poll_jobs_of_nothing_makes_no_request` | `{}` with zero calls |
| `test_poll_jobs_never_calls_real_time_sleep` | `monkeypatch`es `time.sleep` to raise; the injected `sleep_fn` is honoured |

**docstring:** `test_upload_object_docstring_states_202_is_accepted_not_stored`
pins `"NOT STORED"`, `"expected_object_id"` and `"poll_jobs_until_terminal"` in
`upload_object.__doc__`. That docstring is the only place a caller is told a
202 is not a stored object; if it stops saying so, the next bulk load repeats
the loss.

## verification

```
cd /home/chenry/.maestro/worktrees/kbu-prov-typing-ud-client
TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src \
  /home/chenry/venvs/kbdl/bin/python -m pytest tests/external tests/annotators \
  -q -p no:cacheprovider --no-header

829 passed, 20 skipped in 21.46s     # exit 0
```

`tests/external/test_kbdl_service_utils.py` alone: **85 passed** (was 66 before
this item; 19 added).

Every other suite in the repo that imports `KBDLServiceUtils`:

```
TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src \
  /home/chenry/venvs/kbdl/bin/python -m pytest tests/kbdl_index \
  tests/models_and_analyses/test_backfill.py \
  tests/berdl/test_clearinghouse_arc_adapter.py \
  tests/berdl/test_clearinghouse_kbdl_hash_adapter.py \
  tests/cli/test_clearinghouse_backfill_cli.py \
  -q -p no:cacheprovider --no-header

244 passed in 3.15s                  # exit 0
```

(Consumer list established with
`grep -rln "KBDLServiceUtils\|kbdl_service_utils" src/ tests/`.)

**Baseline comparison (by nodeid, never by count).** As for item 1, neither
cached baseline lists a failing nodeid under `tests/external/` or
`tests/kbdl_index/`:

- `build-baselines/kbutillib-h100-main-6310d61.json` (6310d61, 2026-10-19):
  `failures_by_area` = biochem 27 / cli 19 / modeling 16 / domains/ai 5 /
  core 2. Zero in external or kbdl_index.
- `build-baselines/kbutillib.json` (0f84803, 2026-09-13): none of its 99
  `known_failing` ids are in these directories.

Per the h100 baseline's own instruction, the full suite was **not** re-run: it
records 3416 s (56m56s), 5.7× the 600 s Bash-tool ceiling, with an explicit
`WHY_DURATION_MATTERS` note that a sub-agent told to run it is killed mid-run
and reads as a failure. **This is not a claim that the suite is green** — both
baselines record a substantial pre-existing failing set in biochem, cli,
modeling, domains/ai and core.

## what this prompt asserted that I found to be false

Nothing material. Everything the prompt asserted about the tree checked out:

- `src/kbutillib/domains/external/kbdl_service_utils.py`, class
  `KBDLServiceUtils` — present.
- `KBDLNotFoundError` already exists in this module (`:303`) — used as
  instructed rather than a new class.
- `_POLL_MAX_CONNECTION_ERRORS` (`:233`) and `_TERMINAL_STATES`
  (`frozenset({"completed", "failed"})`, `:224`) — present, and
  `_TERMINAL_STATES` already matches the contract's terminal states, so no new
  constant was needed.
- `poll_until_terminal` (`:830` pre-change) tolerates connection errors
  exactly as described: bounded CONSECUTIVE failures, `log_warning`, streak
  reset on success. Mirrored.
- `tests/external/test_kbdl_service_utils.py` builds stubbed sessions
  (`FakeSession`, and `RaisingSession` for exception sequences) — used as-is.
- `_request` plumbing (`:483`) — used as instructed.

One thing I had to add that the prompt did not mention: the 202 `transform ==
"none"` body gaining `expected_object_id` makes the *existing* documented
return shape in `upload_object`'s `Returns:` block incomplete, so I updated
that block too rather than only adding the new paragraphs. That is still
docstring-only; `upload_object`'s request and return behaviour are unchanged.

## caveats and things inferred rather than verified

- **The server side does not exist yet.** `POST /jobs/status` is being
  implemented in parallel in KBDLJobRunningPrototype; this client is built
  against the contract in the task prompt and tested against a stubbed
  session, exactly as instructed. Nothing here has spoken to a live KBDL
  service. In particular the following are **assumed from the contract, not
  observed**: that the 200 body's key names are `jobs` and `unknown`; that
  `unknown` is a list of bare id strings; that the cap is exactly 1,000; and
  that `expected_object_id` is the key name on the 202. If the server lands
  with different spellings, `status_many` silently returns empty results
  (`body.get("jobs") or {}`) rather than raising — an integration check
  against the deployed service is still required before this is trusted in a
  bulk load.
- **`auto_retry_count` is carried through, not acted on.** The contract
  includes it, and `poll_jobs_until_terminal` returns it in each status dict,
  but nothing in this client retries a lost lease. That is the server-side
  "bounded per-type retry" extension named by kbdl-job-concurrency-v1 D10 and
  is not in this task's scope.
- **No automatic retry of failed uploads.** `poll_jobs_until_terminal`
  *reports* failures; it does not re-upload. The docstring records that
  `KBDLUploadObject` is idempotent so a caller's retry is safe, but the retry
  policy is the caller's. Adding one here would be scope the task did not ask
  for.
- **Ruff was not run.** `pyproject.toml:194` declares `ruff >=0.5.5` as a dev
  dependency but no `ruff` binary exists on this machine's PATH or in
  `/home/chenry/venvs/kbdl/bin/`, and installing into a shared service venv is
  outside a developer task's bounds. One lint-adjacent defect was caught and
  fixed by the test run instead: an RST `\ ` escape in the new `upload_object`
  paragraph produced a `DeprecationWarning: invalid escape sequence '\ '`
  (surfaced by this module's own `test_module_source_never_imports_kbdl_service`,
  which `ast.parse`s the source). Rewritten to plain prose; the suite now runs
  warning-free.
- `status_many`'s return type is a plain `Dict[str, Any]` rather than a
  dataclass or `TypedDict`, matching every other method on this class
  (`check_job`, `list_jobs`, `get_job_result` all return raw decoded JSON).
