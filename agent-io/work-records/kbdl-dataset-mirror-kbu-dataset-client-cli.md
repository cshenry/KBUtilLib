# kbu-dataset-client-cli (PRD kbdl-dataset-mirror-v1, phase 2 of 4)

- Task: `task-a259ed3b`, dev row 2071, role developer, machine h100
- Repo: KBUtilLib, base `main` `e7c04d74035052b4f35308f94086130dcc12f93e`
  (phase 1, `kbu-dataset-publisher-core`)
- Branch: `maestro/developer/dispatch-task-kbu-dataset-client-task-a259ed3b`
- Date: 2026-11-23

## Files changed

| File | Change | What it is |
|---|---|---|
| `src/kbutillib/domains/external/kbdl_service_utils.py` | +331 / −6 | `KBDLGoneError`, `_tombstone_in`, `_gone_body`, the 409/410 mapping, and the seven dataset methods |
| `src/kbutillib/interfaces/cli/dataset.py` | 1048 (new) | the `kbu dataset` group: six verbs, the `--json` envelope, and `pull`'s install discipline |
| `src/kbutillib/interfaces/cli/__init__.py` | +2 | `from .dataset import dataset_cmd` (`:28`) and `main.add_command(dataset_cmd, name="dataset")` (`:80`) |
| `src/kbutillib/domains/kbase/berdl/dataset_publish.py` | +28 / −3 | `_published_snapshot_of` now accepts the route's `{"version", "manifest"}` envelope, and `KBDLPort.get_dataset_version`'s docstring says so. **See "The one change to dataset_publish.py" below.** |
| `tests/external/test_kbdl_dataset_client.py` | 517 (new) | 24 tests: the seven methods on the real client over a faked session, plus the KBDLPort runtime proof |
| `tests/cli/test_dataset_cli.py` | 1231 (new) | 47 tests: `pull` against real zstd parquet, `prune`/`hold`/`list`/locus, the output contract |
| `agent-io/docs/kbu-dataset-cli.md` | 306 (new) | the verbs, the locus table, prune's retire-then-reclaim, pull's safety rules, the publish gates |

No other file was touched. `pyproject.toml` needed nothing (no new runtime or
test dependency: see "Why not `responses`" below).

---

## 1. The seven client methods and their status-code handling

All in `KBDLServiceUtils`, after `delete_object`, under a block comment
stating that all seven need a valid KBase token and the four POSTs
additionally need the caller on the service's `KBDL_DATASET_PUBLISHERS` list.
Every one reuses `_request` (so the 401 token fail-over applies unchanged) and
`_raise_for_status`.

| method | line | route | status handling |
|---|---|---|---|
| `list_datasets()` | `kbdl_service_utils.py:1491` | `GET /datasets` | the shared mapping only |
| `get_dataset_version(name, version="latest")` | `:1501` | `GET /datasets/{name}/latest` when `version` is `"latest"`/`None`, else `GET /datasets/{name}/versions/{version}` (`str()`-coerced, so `3` and `"3"` both work) | **410 → `KBDLGoneError`** carrying the whole body and its tombstone; 404 → `KBDLNotFoundError` |
| `list_dataset_versions(name)` | `:1531` | `GET /datasets/{name}/versions` | returns the service's order **untouched** (newest first, retired included) |
| `publish_dataset_version(name, manifest_object_id)` | `:1540` | `POST /datasets/{name}/versions` with `{"manifest_object_id": ...}` | 201 and **200 both succeed** (200 is the idempotent repeat); **409 → `KBDLConflictError` carrying `body` + `tombstone`** — never success; 403 → `KBDLNotAuthorizedError` |
| `retire_dataset_version(name, version, reason)` | `:1575` | `POST .../retire` with `{"reason": reason}` | 409 for the latest or a held version; a 200 no-op when already retired is returned as success |
| `hold_dataset_version(name, version, reason)` | `:1606` | `POST .../hold` | `reason="text"` → `{"reason": "text"}` holds; **`reason=None` → `{"reason": null}` and RELEASES** — the key is always sent, because the explicit null *is* the release instruction |
| `reclaim_dataset(name, dry_run=True, assume_retired=None)` | `:1630` | `POST /datasets/{name}/reclaim` | `dry_run` defaults **True**; `assume_retired` is sent as strings and **only with `dry_run`** — otherwise `KBDLBadRequestError` is raised locally with **no request made** (the service would 400) |

### New and changed error handling

- `KBDLGoneError(KBDLServiceError)` — `:384`. Carries `body` (the whole
  parsed 410 body, which is the same `{"version", "manifest"}` shape a 200
  has) and `tombstone`, plus `status_code = 410`. **Deliberately not a
  subclass of `KBDLNotFoundError`:** "withdrawn" and "never existed" lead to
  different operator actions and only the first has a tombstone. Asserted by
  `test_KBDLGoneError_is_not_a_not_found_error`.
- `_raise_for_status` gained a `410` arm that uses the new `_gone_body`
  (`:618`) rather than `_error_detail` — `_error_detail` projects `detail` out
  of the body, which would throw away the retired version's record.
- `KBDLConflictError` gained an **optional** `body=` argument (`:359`), so the
  existing single-argument construction keeps working, and sets `body`,
  `tombstone` and `status_code = 409`. The 409 arm of `_raise_for_status` now
  passes the parsed body through. This is what makes "a 409 whose body carries
  a tombstone surfaces as an error, never as success" checkable by a caller
  rather than by regexing a message.
- `_tombstone_in(value)` (`:341`) finds a `tombstone` at any nesting depth
  (`detail`/`error`/`body`/`version`). Written locally rather than imported
  from `dataset_publish._tombstone_of`: this module is wire-contract-only and
  the publisher deliberately does not import this client either.
- The module docstring's endpoint list and error-mapping list were extended
  with the nine dataset routes and the 410/409-tombstone rules. That docstring
  is the module's documented contract, so leaving it stale would have been a
  defect of its own.

### KBDLPort satisfaction

`KBDLServiceUtils` now has all four methods `KBDLPort` declares
(`upload_object`, `poll_jobs_until_terminal`, `get_dataset_version`,
`publish_dataset_version`) with compatible signatures.
`test_the_client_has_every_method_KBDLPort_declares` reads the names **off the
Protocol itself** rather than hard-coding them, so adding a method to the port
without a client method is caught.

---

## 2. The CLI verbs

`src/kbutillib/interfaces/cli/dataset.py`, registered as
`main.add_command(dataset_cmd, name="dataset")`.

| verb | signature | locus | notes |
|---|---|---|---|
| `list` | `[--json]` | anywhere | warns in the envelope about a dataset with no registered version |
| `status` | `<name> [--json]` | **IN-POD ONLY** | `DatasetPublisher.status`; the publisher's `warnings` are lifted into the envelope's `warnings` and removed from `data` |
| `publish` | `<name> [--dry-run] [--force] [--keep-workdir] [--json]` | **IN-POD ONLY** | all three flags pass straight through to `DatasetPublisher.publish` |
| `pull` | `<name> [--version N] --dest DIR [--json]` | anywhere | see below |
| `hold` | `<name> <version> (--reason TEXT \| --release) [--json]` | anywhere | exactly one required; neither or both is a `click.UsageError` (exit 2) with **no HTTP call** |
| `prune` | `<name> --keep N [--apply] [--json]` | anywhere | `--keep < 1` is a `click.UsageError` (exit 2) with no HTTP call |

**The output contract** is `kbu clearinghouse`'s, copied structurally:
`_envelope` (`:106`) builds exactly
`{"schema_version": 1, "verb", "generated_at", "locus", "data", "warnings"}`;
`_emit` (`:124`) either serialises it (one object, stdout only, warnings
mirrored to stderr) or hands the **same dict** to `_render` (`:147`). There is
no second computation path, which is what
`test_list_human_output_renders_the_same_dict` and
`test_prune_human_output_renders_the_same_dict` assert by patching `_render`.

**A refusal is not an envelope.** Every refusal is a `click.ClickException`
(or `UsageError`): non-zero exit, reason on stderr, **nothing on stdout**, in
both human and `--json` mode. That is the line `kbu clearinghouse load` draws
and it is what keeps `--json` stdout parseable.

**The locus gate.** `_locus()` (`:269`) delegates to
`berdl_notebook_utils_importable()` — the same probe `BerdlCapability.locus()`
uses, never an environment variable.
`_require_in_pod_or_refuse(verb, locus)` (`:284`) builds a
`BerdlLoadRefusedError` with the message shape the clearinghouse load verbs
use and raises a `click.ClickException` **from** it, so the type survives as
`__cause__` (a clean non-zero exit with no traceback, and the refusal type
still inspectable). It is called before `_publisher()`, which would otherwise
reach for a Spark session and a MinIO client;
`test_status_and_publish_refuse_off_pod_before_any_call` proves the ordering
by making `_publisher` raise if it is ever called.

### `pull`

`_pull` (`:577`) does the six steps in this order, and the order is the safety
property:

1. `--dest` must not exist — `os.path.lexists`, so an empty directory, a
   file, a live symlink **and a broken symlink** are all refused. A broken
   symlink is the one case `Path.exists()` reports absent, and renaming onto
   it would follow it elsewhere.
2. Fetch the version (`latest` by default). `KBDLGoneError` → non-zero exit
   printing the tombstone.
3. `_resolve_workdir` (`:535`): `W = <dest>.kbu-partial`, created with
   `mkdir(parents=True, exist_ok=False)` and holding
   `pull.json = {"manifest_object_id": ...}`. An existing W resumes only when
   its `pull.json` names the same id; a different id or no `pull.json` is
   refused **by name** and never deleted.
4. The manifest OBJECT's bytes → `W/MANIFEST.json`, verbatim, never
   re-serialised; `W/README.md` from `manifest["readme"]`.
5. Per file: the name must match `^part-[0-9]{5}\.parquet$`; the bytes must
   hash to the object id; `pq.ParquetFile(...).metadata.num_rows` must equal
   the manifest's `row_count` for that file. The footers must also total the
   manifest's own `row_count`.
6. Remove `W/pull.json`, re-check `os.path.lexists(dest)`, then **one**
   `os.rename(W, dest)`. If `dest` appeared meanwhile the install is refused
   and W is left intact.

`_fetch_verified` (`:500`) is the resume: a file already at the path is
accepted **only if its sha256 is the object id**, otherwise
`client.download_object` is called, and the digest is re-checked afterwards.
Steps 4–5 run inside a `try` in `_pull` that converts any non-`ClickException`
failure (a transport error, an `OSError`) into a refusal whose message says W
is intact and that re-running resumes it — a bare traceback exits non-zero but
does not say that.

### `prune`

`_prune_plan` (`:900`) takes the service's version list and produces the plan:
only `state == "PUBLISHED"` records are candidates, sorted **oldest first**
(`_version_key` sorts numerically when the versions parse as ints, else by
string); the newest `keep` are kept and **every held version is filtered out**
of the retire list rather than left to 409 at the service — `--apply` stops at
the first error, so one predictable 409 would abandon the rest of a correct
plan.

`_build_prune` (`:943`):

- **without `--apply`**: the only call is
  `reclaim_dataset(name, dry_run=True, assume_retired=<exactly the planned
  versions>)`. Nothing is retired. `assume_retired` is what makes the preview
  honest — without it the dry run reports the reclaim of an empty retire list.
- **with `--apply`**: retire each planned version oldest first, each its own
  request, **stopping at the first error** (recorded in `data.error`, which
  makes `prune_cmd` exit 1 after emitting the envelope), then
  `reclaim_dataset(name, dry_run=False)` — once, with no `assume_retired`.
  Retire-then-reclaim, never the reverse: reclamation only deletes objects of
  already-retired versions, so the retires are the authorisation.

The `retire_reason` recorded is `"retired by kbu dataset prune --keep N"`.

---

## 3. The one change to `dataset_publish.py`

The dispatch rules allow a change "if strictly needed to let
`KBDLServiceUtils` satisfy `KBDLPort`". One was needed, and this is it.

`GET /datasets/{name}/latest` answers `{"version": <record>, "manifest":
<manifest>}`, and `get_dataset_version` returns that body. Phase 1 was written
against "the record": `DatasetPublisher._published_snapshot_id` passes
whatever `get_dataset_version` returns straight to `_published_snapshot_of`,
which read `source_snapshot_id` off the top level and therefore raised
`DatasetPublishError: the registered version record carries no
source_snapshot_id` against the real client. That is a hard failure of
`status()` and `publish()` with the real client, and the success criteria
require exactly that path to work.

The fix is 20 lines in `_published_snapshot_of` (`dataset_publish.py:1152`):
when the mapping has neither `source_snapshot_id` nor `source` but does have a
mapping under `version`, unwrap it. The function already tolerated two shapes
(`source_snapshot_id`, or `source.snapshot_id`), so this is a third in the same
place. `KBDLPort.get_dataset_version`'s one-line docstring (`:340`) was
extended to say the real client returns the envelope.

**Why here rather than in the client:** the alternative was to make
`get_dataset_version` project `["version"]` out and throw the manifest away.
The manifest is the other thing every caller of that route wants — `pull`
needs the record *and* the manifest — so a client that discarded it would make
every puller fetch the version twice.

`tests/berdl/test_dataset_publish.py` (97 tests) still passes unchanged, and
`test_a_real_client_over_a_fake_session_satisfies_KBDLPort` is the regression
guard for the shape.

---

## 4. New tests, by nodeid

**Two files, not one.** The dispatch said "new test file"; the client tests
belong under `tests/external/` beside `test_kbdl_service_utils.py` and the CLI
tests under `tests/cli/` beside `test_clearinghouse.py`, which is where this
repo puts each. 71 tests, all passing.

**Why not `responses`/`requests-mock`:** neither is a test dependency of this
repo and neither is installed, so per the dispatch a monkeypatched session was
used. `KBDLServiceUtils.__init__` already takes `session=`, and
`tests/external/test_kbdl_service_utils.py` already establishes the
`FakeSession`/`FakeResponse` pattern; both new files follow it. **No dependency
was added.**

**The two things the success criteria single out:**

- The `pull` tests drive the **real** `KBDLServiceUtils` with only its
  `requests.Session` faked — not a fake client object — so `download_object`'s
  streaming, its resume-by-digest and its content-addressing check are the
  code under test. The parquet served is real zstd parquet written by pyarrow
  and every object id is the **measured** sha256 of the bytes served, so the
  digest and footer assertions are about actual bytes.
  `test_the_fixture_manifest_is_a_valid_schema_1_manifest` runs the fixture
  through the publisher's own `validate_manifest` so it cannot drift into
  something no publish could produce.
- `test_a_real_client_over_a_fake_session_satisfies_KBDLPort` constructs
  `DatasetPublisher(FakeLakehouse(), FakeStaging(), <real KBDLServiceUtils>)`
  and runs `status()` through it, asserting both the verdict and that the real
  `GET /datasets/{name}/latest` request was made.

### `tests/external/test_kbdl_dataset_client.py` (24)

- `test_list_datasets_gets_the_collection_route`
- `test_the_token_is_sent_on_every_dataset_call`
- `test_get_dataset_version_defaults_to_the_latest_route`
- `test_get_dataset_version_numbered_uses_the_versions_route`
- `test_get_dataset_version_410_raises_KBDLGoneError_with_the_tombstone`
- `test_KBDLGoneError_is_not_a_not_found_error`
- `test_get_dataset_version_410_without_json_keeps_the_text`
- `test_get_dataset_version_404_is_not_found`
- `test_list_dataset_versions_returns_the_service_order_untouched`
- `test_publish_dataset_version_posts_the_manifest_object_id`
- `test_publish_dataset_version_200_is_the_idempotent_repeat`
- `test_409_carrying_a_tombstone_is_an_error_and_keeps_the_tombstone`
- `test_a_plain_409_has_no_tombstone`
- `test_403_on_a_post_is_a_permission_error_not_an_auth_error`
- `test_retire_dataset_version_posts_the_reason`
- `test_retire_409_for_the_latest_or_a_held_version`
- `test_hold_dataset_version_sends_the_reason`
- `test_hold_with_reason_None_sends_an_explicit_json_null_and_releases`
- `test_reclaim_dry_run_is_the_default`
- `test_reclaim_assume_retired_is_sent_as_strings_with_dry_run`
- `test_reclaim_assume_retired_without_dry_run_is_refused_locally`
- `test_reclaim_real_run_sends_dry_run_false_and_nothing_else`
- `test_the_client_has_every_method_KBDLPort_declares`
- `test_a_real_client_over_a_fake_session_satisfies_KBDLPort`

### `tests/cli/test_dataset_cli.py` (47)

- `test_the_fixture_manifest_is_a_valid_schema_1_manifest`
- `test_the_cli_part_name_pattern_is_the_publishers_pattern`
- `test_pull_installs_the_verified_version_at_dest`
- `test_pull_writes_the_manifest_objects_exact_bytes`
- `test_pull_writes_the_readme_from_the_manifest`
- `test_pull_installed_parquet_is_readable_with_the_declared_rows`
- `test_pull_a_numbered_version_uses_the_versions_route`
- `test_pull_refuses_any_existing_dest[empty_dir]`
- `test_pull_refuses_any_existing_dest[file]`
- `test_pull_refuses_any_existing_dest[symlink]`
- `test_pull_refuses_any_existing_dest[broken_symlink]`
- `test_pull_refuses_a_retired_version_and_prints_the_tombstone`
- `test_pull_fails_the_digest_check_on_one_corrupted_byte`
- `test_pull_fails_the_footer_check_on_a_row_count_mismatch`
- `test_pull_refuses_manifest_bytes_that_do_not_hash_to_the_id`
- `test_pull_refuses_a_part_name_the_manifest_should_not_be_able_to_choose`
- `test_pull_refuses_a_leftover_workdir_for_a_different_manifest`
- `test_pull_refuses_a_leftover_workdir_with_no_pull_json`
- `test_pull_refuses_when_dest_appears_between_verification_and_the_rename`
- `test_an_interrupted_pull_leaves_nothing_at_dest_and_resumes_on_rerun`
- `test_a_resume_re_downloads_a_file_that_no_longer_hashes_correctly`
- `test_list_emits_exactly_one_envelope`
- `test_list_warns_about_a_dataset_with_no_registered_version`
- `test_list_human_output_renders_the_same_dict`
- `test_hold_with_a_reason_places_the_hold`
- `test_hold_release_sends_reason_none`
- `test_hold_with_both_reason_and_release_is_a_usage_error`
- `test_hold_with_neither_reason_nor_release_is_a_usage_error`
- `test_prune_keep_zero_is_a_usage_error`
- `test_prune_keep_negative_is_a_usage_error`
- `test_prune_without_apply_changes_nothing_and_previews_the_reclaim`
- `test_prune_without_apply_previews_an_empty_plan_as_an_empty_plan`
- `test_prune_with_apply_retires_exactly_the_plan_then_reclaims_once`
- `test_prune_with_apply_retires_oldest_first_and_keeps_the_newest_n`
- `test_prune_with_apply_never_retires_a_held_version`
- `test_prune_with_apply_stops_at_the_first_retire_error`
- `test_prune_human_output_renders_the_same_dict`
- `test_status_and_publish_refuse_off_pod_before_any_call[status]`
- `test_status_and_publish_refuse_off_pod_before_any_call[publish]`
- `test_the_off_pod_refusal_carries_a_BerdlLoadRefusedError[status]`
- `test_the_off_pod_refusal_carries_a_BerdlLoadRefusedError[publish]`
- `test_in_pod_lets_the_gate_through`
- `test_status_emits_exactly_one_envelope_in_pod`
- `test_publish_passes_every_flag_through_and_emits_one_envelope`
- `test_publish_defaults_are_all_off`
- `test_kbu_dataset_is_registered_on_the_main_cli`
- `test_every_verb_emits_one_object_with_its_own_verb_name`

### One testing note worth carrying forward

`CliRunner` on this repo's click (8.1.6) **merges stderr into stdout by
default**, which would make every "exactly one object on stdout" assertion
vacuous. The tests use a `_runner()` helper that passes `mix_stderr=False`
and falls back to a bare `CliRunner()` on `TypeError` (click >= 8.2 removed
the argument and always separates the streams).

---

## 5. Suite comparison, by nodeid

The full KBUtilLib suite takes ~57 minutes on h100
(`build-baselines/kbutillib-h100-main-6310d61.json`), far above the Bash-tool
ceiling, so per the dispatch a **targeted selection** was run **twice in this
same worktree** — once at the base commit with a clean tree, once on the
branch. Comparing in the same location matters: that baseline file records a
worktree measurement trap (fixtures in `tests/core/test_composition_smoke.py`
resolve a data file relative to the package dir and land on a sibling
worktree), and running base and branch in the same place cancels it.

### Paths run (both runs, identical selection)

```
tests/external tests/cli tests/berdl tests/core tests/beril_worktree
tests/kbdl_index tests/verab
tests/harness/test_harness_core.py
tests/domains/test_cli_smoke.py
tests/domains/test_ontomap_council_publish.py
tests/domains/test_ontomap_pipeline_rehearsal.py
tests/domains/ai/council/test_quote_per_model_pricing.py
tests/models_and_analyses/test_backfill.py
```

The four directories the dispatch named, plus every other test file that
mentions `kbdl_service_utils` or `interfaces.cli`
(`grep -rln "kbdl_service_utils\|interfaces\.cli" tests/`).

### Command and results

```
TMPDIR=/scratch/chenry/tmp python -m pytest <the paths above> \
  -q -p no:cacheprovider --no-header --tb=no -rfE
```

- interpreter: the worktree's own `.task-venv`, cpython 3.12.3, pytest 9.2.0
  (`pytest`, `numpy`, `sqlalchemy`, `duckdb`, `pandas` were installed into
  that venv — this task's own — because six test modules could not be
  collected without them)

| run | commit | result | exit |
|---|---|---|---|
| base | `e7c04d7` (clean tree) | **278 failed, 3618 passed, 87 skipped, 8 errors** in 330 s | 1 |
| branch | this branch | **278 failed, 3689 passed, 87 skipped, 8 errors** in 323 s | 1 |

**Comparison by nodeid: the 286 non-passing node ids are IDENTICAL.**
`comm -13` (new on the branch) is empty and `comm -23` (gone from the branch)
is empty; `diff` of the two sorted sets reports no difference.

- **New breakages: 0.**
- **Accidentally fixed: 0.**
- `+71 passed` is exactly the 71 tests this task adds.

The 286 pre-existing non-passing node ids are environmental: this venv lacks
`tomli_w`, `rsync`-dependent fixtures, `torch`, `rdkit` and the full capability
stack (`tests/harness`, `tests/cli/test_update*`, `tests/cli/test_kind*` and
the biochem registry tests dominate them). The count differs from both
existing baseline files because both were captured in different environments
(`kbutillib.json`: 99 failures, a 2026-09-13 minimal venv at `0f84803` with
seven `--ignore` flags; `kbutillib-h100-main-6310d61.json`: 69 failures, the
full `~/venvs/kbdl` interpreter at `6310d61`). Neither is comparable by count
to this run, which is why the base was re-measured here rather than read off a
file — and why the comparison is by **name**.

No new baseline file was written: `build-baselines/kbutillib.json` already
exists, so the "if it does not exist, capture one" clause did not apply, and
this run was a targeted selection rather than the full-suite capture those
files record.

### The new tests on their own

```
python -m pytest tests/external/test_kbdl_dataset_client.py \
                 tests/cli/test_dataset_cli.py -q
→ 71 passed, exit 0

python -m pytest tests/berdl/test_dataset_publish.py -q
→ 97 passed, exit 0        (phase 1's suite, unchanged by the _published_snapshot_of edit)

python -m ruff check <every file changed above>
→ All checks passed!, exit 0
```

`src/kbutillib/interfaces/cli/__init__.py` carries one **pre-existing** ruff
`I001` (import-block ordering), confirmed present at `e7c04d7` by running
ruff against `git show HEAD:...` output. It was left alone rather than
reformatted, to keep this diff to the two added lines.

---

## 6. Prompt-vs-tree reconciliation

The dispatch prompt was accurate about the tree with three exceptions, all
handled in code rather than by stopping:

1. **"download_object (resume + digest)"** — `download_object`
   (`kbdl_service_utils.py:1271`) has the digest check but **no resume**: it
   streams the whole object to a temp file beside the destination and sends no
   `Range` header. Adding HTTP range-resume to a shipped method with its own
   retry semantics is well outside this task, so resume is implemented at the
   **pull** level instead: `_fetch_verified` skips any file already on disk
   whose sha256 is its object id. That satisfies the stated behaviour — "an
   interrupted pull ... resumes on rerun", asserted by
   `test_an_interrupted_pull_leaves_nothing_at_dest_and_resumes_on_rerun`,
   which also proves the already-verified objects are **not** re-requested.
2. **`get_dataset_version` returning the record vs the envelope** — the
   prompt's route spec and phase 1's `KBDLPort` disagree. The route spec wins
   (it is the service's actual contract) and `dataset_publish.py` was adapted;
   see section 3.
3. **"raise the same `BerdlLoadRefusedError` shape the load verbs use
   off-pod"** — the clearinghouse load verbs raise a `click.ClickException`
   naming the locus; `BerdlLoadRefusedError` is what the *capability* raises.
   Both are honoured: `_require_in_pod_or_refuse` builds the
   `BerdlLoadRefusedError` (so the message shape and the type are both
   present, the latter as `__cause__`) and raises the `ClickException` that
   gives a clean non-zero exit.

Nothing in the prompt was unbuildable and no boundary was reached. Everything
was written inside the worktree; the only writes outside it were pip installs
into the worktree's own `.task-venv` and scratch files under
`$MAESTRO_TASK_SCRATCH`.
