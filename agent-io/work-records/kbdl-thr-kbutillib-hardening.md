# Work record: kbdl-thr-kbutillib-hardening

## task_id
kbdl-thr-kbutillib-hardening (PRD: kbdl-skani-throughput-v1)

## branch
maestro/developer/skani-throughput-kbdl-thr-kbutillib-hardening

## commit_shas
- a5a3848457dd3b0179b42efd83e5d60322fb4515 — fix(skani): harden cache writes, add advisory locking, loud corrupt-cache failure, and configurable search timeout

## summary
Hardened `src/kbutillib/domains/genome/skani_utils.py` (the canonical module — the
`src/kbutillib/skani_utils.py` flat shim was left untouched as it star-imports from the
canonical module) for KBDL's concurrency and batching work. Four changes, all in the same
file per the task's "one task because they touch the same file" framing: (a) `_save_cache`
now writes to a temp file created in the same directory as the cache file and
`os.replace()`s it into place, so POSIX atomic rename guarantees a concurrent reader always
sees either the complete old file or the complete new file, never a truncated/partial one;
(b) `sketch_genome_directory`, `add_skani_database`, and `remove_database` now wrap their
read-modify-write cache updates in a new `_write_lock()` context manager that takes an
advisory `flock` on a sibling lockfile (`<cache_file>.lock`), never on the cache file
itself, since `os.replace()` swaps the cache file's inode and a lock on that inode would
stop protecting anything the moment a writer completes; (c) `_load_cache` now checks
`self.cache_file.exists()` first and returns `{}` only in that legitimate "no cache yet"
case, while a present-but-unparseable file (bad JSON, unreadable) now raises instead of
being silently swallowed into `{}`, which previously made a corrupted cache
indistinguishable from "no databases registered"; (d) `query_genomes` gained a `timeout:
int = 300` parameter used in its `subprocess.run` call, preserving the exact previous
default for every existing caller while letting a caller batching multiple queries into one
`skani search` invocation raise the budget. The `TimeoutExpired` log message was updated to
report the actual configured timeout instead of a hardcoded "5 minutes" string, matching the
now-parameterized value. Added a new offline test module,
`tests/domains/test_genome_skani_hardening.py` (13 tests), that exercises all four behaviors
without invoking real `skani`.

## files_touched
- `src/kbutillib/domains/genome/skani_utils.py` — the four hardening changes
- `tests/domains/test_genome_skani_hardening.py` — new test module (13 tests)

## success_criteria_check
- **(a) Atomic cache write** — PASS. `_save_cache` uses `tempfile.mkstemp(dir=self.cache_file.parent, ...)` + `os.fdopen`/`json.dump` + `os.replace(tmp_path, self.cache_file)`, with cleanup of the temp file on any failure before the replace. Verified by `test_save_cache_writes_via_tmp_file_in_same_dir_and_replaces` (asserts the temp path is a sibling of the cache file, distinct from it, and gone after replace) and by a threaded stress test, `test_concurrent_reader_never_observes_truncated_cache`, that reads the cache file in a tight loop from a separate thread across 25 writes of a ~500-entry cache and asserts it never observes empty or unparseable content.
- **(b) Advisory lock on sibling lockfile** — PASS. `self.lock_file = self.cache_file.parent / (self.cache_file.name + ".lock")` is set in `__init__`, distinct from `self.cache_file`. `_write_lock()` opens `self.lock_file` (not the cache file) with `os.O_CREAT | os.O_RDWR` and takes `fcntl.flock(fd, fcntl.LOCK_EX)` around the yielded block. `sketch_genome_directory`'s final cache mutation, all of `add_skani_database`, and all of `remove_database` (from the initial cache load through the final `_save_cache`) are wrapped in `with self._write_lock():`. Verified by `test_lock_file_is_sibling_not_cache_file`, `test_write_lock_acquires_sibling_lockfile_and_leaves_cache_file_unlocked` (proves the cache file remains independently, non-blockingly flock-able while the write lock is held — i.e. the lock is not on the cache file's inode), `test_write_lock_serializes_concurrent_writers` (a second acquisition blocks until the first releases, using two threads), and `test_add_and_remove_database_use_the_write_lock` (spies on `_write_lock` to confirm the public methods actually take it).
- **(c) Loud failure on corrupt cache, silent {} on absent** — PASS. `_load_cache` now checks `self.cache_file.exists()` before attempting to open/parse; if absent it returns `{}` immediately. If present, `json.JSONDecodeError`/`IOError` during open/parse are logged and re-raised rather than swallowed. Verified by `test_load_cache_returns_empty_dict_when_file_absent`, `test_load_cache_raises_when_file_present_but_unparseable`, and `test_get_database_info_propagates_corrupt_cache_failure` (confirms the exception isn't caught by a downstream caller either — matches the task's note that the blast radius is the five methods in this file, all of which route through `_load_cache`/`_get_database_info`).
- **(d) Configurable `query_genomes` timeout** — PASS. Signature gained `timeout: int = 300` (last positional-or-keyword param, after `threads`); the `subprocess.run` call now passes `timeout=timeout` instead of the hardcoded `300`; the `TimeoutExpired` handler's log message now interpolates the actual `timeout` value. No other timeout in the file was touched — confirmed by inspection (`timeout=5` at the availability probe, `timeout=600  # 10 minute timeout` in `sketch_genome_directory`, and the standalone `timeout=300` in `compute_pairwise_distances` are all byte-for-byte unchanged) and by the regression test `test_other_module_timeouts_are_unchanged`. Behavior verified by `test_query_genomes_defaults_timeout_to_300`, `test_query_genomes_honors_explicit_timeout`, and `test_query_genomes_timeout_error_message_reports_configured_timeout`, all using a monkeypatched `subprocess.run` (no real `skani` invoked).
- **Canonical module, not shim** — PASS. Only `src/kbutillib/domains/genome/skani_utils.py` was edited; `src/kbutillib/skani_utils.py` (the deprecated flat shim) is an unmodified `from kbutillib.domains.genome.skani_utils import *` re-export, so it picks up all four fixes automatically.
- **Added tests pass; no base-passing test now fails** — PASS. See tests_run below for the exact regression comparison.

## tests_run
1. Targeted new-test run (fast iteration):
   `python -m pytest tests/domains/test_genome_skani_hardening.py -q`
   → `13 passed in 2.59s`
2. Regression spot-check for the new tests alongside cwd-mutating tests (the first full-suite
   run caught one of my own tests, `test_other_module_timeouts_are_unchanged`, failing only
   when run after `tests/beril_worktree/` because that suite leaves the process cwd changed —
   fixed by resolving the source path via the imported module's `__file__` instead of a
   cwd-relative path; re-verified):
   `python -m pytest tests/beril_worktree/ tests/domains/test_genome_skani_hardening.py -q`
   → `221 passed in 26.94s`
3. Full baseline CI command (exact string from the envelope), final verification run:
   `python -m pytest --ignore=tests/notebook/helpers --ignore=tests/modeling/test_comprehensive_gapfill_wrapper.py --ignore=tests/biochem/test_escher_utils.py --ignore=tests/kbase/test_kb_narrative_provenance.py --ignore=tests/kbase/test_kb_plm_utils.py --ignore=tests/kbase/test_kb_ws_utils.py --ignore=tests/modeling/test_ms_reconstruction_utils.py --ignore=tests/kbase/test_upload_blob_file_streaming.py -q`
   → `14 failed, 2872 passed, 339 skipped, 266 warnings in 265.38s (0:04:25)`

   **Regression comparison against the measured base (2859 passed, 14 failed, 0 errors, 339
   skipped):**
   - Failed count: 14 → 14, and the failing node IDs are byte-for-byte identical to the 14
     pre-existing failures listed in the envelope (`tests/external/test_kbdl_service_utils.py`,
     none touching skani). Confirmed by diffing the FAILED lines from this run against the
     envelope's `failed_node_ids` list — exact match, no additions, no removals.
   - Passed count: 2859 → 2872 (net +13), which is exactly the 13 tests added in
     `tests/domains/test_genome_skani_hardening.py`; no other file's pass count moved.
   - Skipped count: 339 → 339, unchanged.
   - Errors: 0 → 0, unchanged.
   - Conclusion: **zero net-new failures**; the change is a clean regression-free pass per the
     task's evaluation criterion.
4. `ruff check src/kbutillib/domains/genome/skani_utils.py tests/domains/test_genome_skani_hardening.py`
   → `All checks passed!`

## caveats
- The advisory lock (`fcntl.flock`) is POSIX-only (no Windows support), consistent with the
  rest of this module's reliance on POSIX-specific behavior (`os.replace()` atomicity is
  also POSIX-guaranteed; this repo already assumes a POSIX runtime elsewhere).
- The lock is process-local/advisory: it only protects cooperating writers that go through
  `_write_lock()` (i.e. calls into `sketch_genome_directory`, `add_skani_database`, and
  `remove_database` on this class). It does not prevent a process that opens/writes the
  cache file directly, bypassing this API — that was already true of the pre-existing
  behavior and is out of scope for this task.
- `sketch_genome_directory`'s early "already exists and not force_rebuild" cache read (near
  the top of the function, used only to decide whether to skip the expensive `skani sketch`
  subprocess call) is intentionally left unlocked; only the final cache mutation
  (load-under-lock, mutate, save) is guarded. Holding the write lock across the entire
  multi-minute sketch build would serialize independent database builds without any
  correctness benefit — the task's stated hazard is "lose an update" on the read-modify-write,
  which is fully closed by locking that step. This means two concurrent
  `force_rebuild=False` calls for the same not-yet-cached `database_name` could both decide
  to build (a performance/redundant-work concern), not that either could corrupt or drop a
  write — the lock still fully protects the cache file's data integrity.
- `_get_database_info` / `list_databases` / `get_database_info` are read-only call sites and
  were not wrapped in the write lock, per the task's scope naming only the three
  read-modify-write methods; they do now propagate a raised corruption error from
  `_load_cache` rather than masking it, which is the intended (c) behavior.
- New venv created at `~/VirtualEnvironments/kbutillib-thr-hardening-envA` per the
  environment instructions; not reused from any shared/other-task venv. Confirmed
  `python -c "import kbutillib; print(kbutillib.__file__)"` resolved to
  `/Users/chenry/.maestro/worktrees/kbdl-thr-kbutillib-hardening/src/kbutillib/__init__.py`
  before trusting any test result.
