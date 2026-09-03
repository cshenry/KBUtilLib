# Work record: kbutillib-mmseqs-search

- **task_id**: kbutillib-mmseqs-search
- **branch**: maestro/developer/fitness-prop-kbutillib-mmseqs-search
- **commit_shas** (chronological):
  - ea2d2bfd19a1998d000a602cb7f5d92509899b84 (`feat(mmseqs): add build_search_db and search_proteins for reference-DB search`)

## summary

Added an mmseqs SEARCH surface to `MMSeqsUtils`
(`src/kbutillib/domains/genome/mmseqs_utils.py`), which previously only
exposed easy-cluster methods (`cluster_proteins`, `easy_cluster`,
`get_cluster_representatives`, `get_cluster_membership`). The new
`build_search_db(proteins, out_dir, *, threads=1) -> Path` writes a
FASTA, runs `mmseqs createdb` then `mmseqs createindex` into `out_dir`,
and returns the database path; the resulting database is left on disk
(not in a temp dir) so it can be searched repeatedly without rebuilding.
The new `search_proteins(proteins, db_path, *, min_seq_id, coverage,
threads=1, sensitivity=None) -> List[Dict]` runs `mmseqs search` of the
query proteins against that database, then `mmseqs convertalis` to a
tabular report, and parses it into one dict per accepted hit with
exactly six keys: `query_id`, `target_id`, `identity`, `coverage`,
`evalue`, `bits`.

The highest-risk detail — mmseqs2's identity-unit ambiguity — is handled
by explicitly requesting the `pident` column (a 0-100 percentage, as
opposed to the `fident` column which is already a 0.0-1.0 fraction) via
`--format-output query,target,pident,qcov,evalue,bits`, then dividing by
100.0 in the new `_parse_search_tsv` helper before returning, with an
assertion that the raw value is in `[0, 100]` before conversion. The
docstrings on both `search_proteins` and `_parse_search_tsv` name
`pident` as the source column and state the division explicitly, so no
caller can receive a raw percentage. Coverage is computed as coverage of
the query sequence (`--cov-mode 2`, `qcov` column) since queries are
being placed against reference representatives that may differ in
length; this choice is documented in the `coverage` parameter's
docstring. Every mmseqs parameter the method exposes (`--min-seq-id`,
`-c`, `--cov-mode`, `--threads`, `-s` when given) is passed explicitly on
the command line, matching `cluster_proteins`' discipline — `min_seq_id`
and `coverage` are required keyword-only arguments with no defaults, so
callers cannot silently fall back to mmseqs' own (very different)
defaults. Both methods reuse the same `_check_mmseqs_availability` gate,
`_write_fasta` helper, logging calls (`log_info`/`log_error`/
`log_warning`), `initialize_call` provenance hook, and
`RuntimeError`/`ValueError` exception conventions already used by
`cluster_proteins`. Filtering to hits meeting both `min_seq_id` and
`coverage` is done explicitly in Python (`_parse_search_tsv`) rather than
relying solely on mmseqs' own internal `--min-seq-id`/`-c` thresholding
(which is still passed to the `search` subprocess call for performance,
but the Python-side filter is authoritative).

Added unit tests to `tests/external/test_mmseqs_utils.py` following the
existing conventions in that file: `TestBuildSearchDb` and
`TestSearchProteins` (mocked `subprocess.run`, mirroring the
`TestClusterProteins` patterns for not-available/empty-list/missing-field
validation, success paths, per-step failure handling, and explicit-
parameter-passing checks), `TestParseSearchTsv` (helper-level unit tests
for the percent-to-fraction conversion and threshold filtering, mirroring
`TestParseClusterTsv`), and a new `@pytest.mark.integration`
`TestMMSeqsSearchIntegration` class that builds a real database and
searches real proteins against it end-to-end with the actual `mmseqs`
binary, skipping cleanly if mmseqs is unavailable (mirroring
`TestMMSeqsIntegration`). The real binary happens to be installed in
this environment (`/opt/homebrew/bin/mmseqs`, version `18-8cc5c`), so the
integration test ran for real (not skipped) during verification and
confirmed both the database-reuse property and that the returned
`identity` values are genuine fractions (e.g. 0.973, 0.966 for
near-identical sequences), not percentages.

## files_touched

- `src/kbutillib/domains/genome/mmseqs_utils.py`
- `tests/external/test_mmseqs_utils.py`

## success_criteria_check

- `MMSeqsUtils` exposes `build_search_db` and `search_proteins` with the
  documented signatures: **pass** — `build_search_db(self, proteins,
  out_dir, *, threads=1) -> Path` and `search_proteins(self, proteins,
  db_path, *, min_seq_id, coverage, threads=1, sensitivity=None) ->
  List[Dict[str, Any]]` are defined on the `MMSeqsUtils` class exactly as
  specified.
- `search_proteins` returns dicts carrying exactly the six named keys
  with identity as a fraction in (0,1]: **pass** — `_parse_search_tsv`
  builds each hit dict with only `query_id`, `target_id`, `identity`,
  `coverage`, `evalue`, `bits` (verified by
  `test_search_proteins_success_returns_exact_keys_and_fraction_identity`
  and `test_parse_search_tsv_exact_keys` asserting `set(hit.keys()) ==
  {...}` exactly); `identity` is always `pident / 100.0` with an
  assertion that `pident` is in `[0, 100]` first, and the manual
  real-mmseqs smoke test returned `0.973`/`0.966`, not `97.3`/`96.6`.
- The identity-unit conversion is documented in the docstring naming its
  source column: **pass** — both `search_proteins`'s docstring
  ("Identity-unit conversion" section) and `_parse_search_tsv`'s
  docstring explicitly name `pident` as the source column, contrast it
  with `fident`, and state the `/100.0` conversion.
- New tests pass and no test that passed on the base commit fails:
  **pass** — see `tests_run` below; the failing/erroring set after the
  change is byte-for-byte the same 7 pre-existing baseline node ids
  listed in the envelope, and the passed count went from 2939 (baseline)
  to 2963 (24 new tests added: 7 in `TestBuildSearchDb`, 11 in
  `TestSearchProteins`, 5 in `TestParseSearchTsv`, 1 integration test),
  with skipped (399) and failed/error (1/6) counts unchanged.

## tests_run

1. Manual real-mmseqs smoke test (informal, before writing pytest cases)
   to sanity-check unit conversion and DB reuse end-to-end — confirmed
   `identity` values of `0.973`/`0.966` (not `97.3`/`96.6`) and that the
   same `db_path` returned by `build_search_db` could be searched twice.

2. Targeted file (fast iteration):
   ```
   cd /Users/chenry/.maestro/worktrees/kbutillib-mmseqs-search && PYTHONPATH=src /Users/chenry/VirtualEnvironments/skanidb-base-kbu/bin/python -m pytest tests/external/test_mmseqs_utils.py -q
   ```
   Result: `47 passed in 7.84s` (includes the real-mmseqs integration
   test, which ran for real since `mmseqs` is installed on this machine).

3. Full baseline suite (exact command from the envelope), explicit
   600000 ms timeout:
   ```
   cd /Users/chenry/.maestro/worktrees/kbutillib-mmseqs-search && PYTHONPATH=/Users/chenry/.maestro/worktrees/kbutillib-mmseqs-search/src /Users/chenry/VirtualEnvironments/skanidb-base-kbu/bin/python -m pytest tests/ -q --continue-on-collection-errors
   ```
   Result: `1 failed, 2963 passed, 399 skipped, 266 warnings, 6 errors in
   236.97s (0:03:56)`. The 1 failed + 6 errors are exactly the 7 baseline
   node ids called out in the envelope
   (`tests/modeling/test_ms_reconstruction_utils.py::test_default_mode_uses_db_fallback`,
   `tests/biochem/test_escher_utils.py` collection error,
   `tests/notebook/helpers` collection error, and the four
   `test_comprehensive_gapfill_wrapper.py` errors). No regression; passed
   count is baseline 2939 + 24 new tests = 2963.

4. `ruff check` on both changed files: one pre-existing finding (`F401
   subprocess imported but unused` at `tests/external/test_mmseqs_utils.py:3`)
   that predates this change (confirmed via `git diff main` — that import
   line is outside my diff hunk) and was left untouched, matching the
   instruction not to fix unrelated pre-existing issues. No lint findings
   in the code this task added.

## caveats

- `coverage` in the returned hit dicts is query coverage (`qcov`,
  mmseqs `--cov-mode 2`), not target coverage or the query-and-target
  mode (`--cov-mode 0`) that `cluster_proteins` defaults to. This was a
  judgment call: the task's signature for `search_proteins` has a single
  `coverage` parameter (no `coverage_mode`), and query coverage is the
  natural choice when placing a query genome's proteins against
  potentially differently-sized reference representatives. Documented
  explicitly in the `coverage` parameter's docstring so callers are not
  surprised.
- `min_seq_id`/`coverage` are passed to the `mmseqs search` subprocess
  call itself (so mmseqs also pre-filters internally for performance),
  but the authoritative filter enforced by this code is the explicit
  Python-side check in `_parse_search_tsv`, per the task's instruction to
  "filter to hits meeting BOTH min_seq_id and coverage before returning."
- `build_search_db` raises `RuntimeError` (not a `{"success": False,
  ...}` dict like `cluster_proteins` returns) on `createdb`/`createindex`
  subprocess failures, and `search_proteins` raises `RuntimeError` on
  `createdb`/`search`/`convertalis` failures. This differs from
  `cluster_proteins`'s dict-based error reporting, but is required by the
  task's specified return types (`-> Path` and `-> List[Dict]`, neither
  of which has room for a `success`/`error` field); the exception
  *types* used (`RuntimeError` for mmseqs-unavailable and subprocess
  failures, `ValueError` for malformed input) match `cluster_proteins`'s
  conventions exactly.
- The `mmseqs` binary happens to be installed on this development
  machine (Homebrew, `/opt/homebrew/bin/mmseqs`, version `18-8cc5c`), so
  the new `@pytest.mark.integration` test exercised the real binary
  during verification rather than skipping. The test still contains an
  explicit `pytest.skip("MMseqs2 not available")` / `pytest.skip("MMseqs2
  not installed")` guard, mirroring the existing
  `TestMMSeqsIntegration.test_cluster_real_proteins`, so it will skip
  cleanly on machines without mmseqs installed.
