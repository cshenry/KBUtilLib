# kbutillib-bakta-kofamscan-annotators

## task_id
kbutillib-bakta-kofamscan-annotators

## branch
maestro/developer/kbutillib-bakta-kofamscan-annotators

## commit_shas
- `25c5b67afc7338f8bc551eb0eaae54908622f974` — feat(annotation): add BaktaUtils and KofamscanUtils annotator modules

## summary

Added two new annotator modules to `kbutillib/domains/genome/annotation/`,
siblings of `prokka_utils.py` / `dram2_utils.py` / `transyt_utils.py`,
implementing the `AnnotatorUtils` contract for two local, credential-free
tools per PRD `kbdl-local-bakta-kofamscan-v1`:

- `bakta_utils.py` → `BaktaUtils`, wrapping Bakta's protein entry point
  (`bakta_proteins`). Emits exactly one `Term(namespace="FUNCTION", id=None,
  value=<product>)` per gene (D4); `ec_ids`, `kegg_orthology_id`, `cog_id`
  and `go_ids` (nested under the payload's `psc` object) are carried in
  `Term.evidence` only, never emitted as Terms. The database directory is an
  explicit constructor argument (`db_path=`), and every run reads
  `<db_path>/version.json` for the database's `major` version, failing fast
  with `ToolUnavailableError` (fixed wording: `"Bakta DB at <resolved_path>
  is missing db/version.json or it is not parseable"`) when that file is
  absent or unparseable — before ever invoking the tool.

- `kofamscan_utils.py` → `KofamscanUtils`, wrapping KofamScan's
  `exec_annotation` with `-f detail-tsv` (not `mapper`). Filters output to
  rows carrying the `*` significance flag — mandatory per the PRD's
  measurement that unfiltered `detail-tsv` output is ~5x inflated with
  below-threshold hits. Emits every significant hit for a gene (regression
  test against the upstream parser's last-hit-wins multimap bug), and, per
  hit, a stable-ordered `Term` pair: the raw KO id first, then — when
  bridgeable — the composed `"symbol; definition"` text via `_bridge_ko`, a
  pure dict-lookup function over an in-memory/loaded table (no I/O, no
  runtime string normalisation — the composition already happened when the
  table was built by a sibling task's generator script). When
  `kofamscan.ko_function_map` is unset or unreadable, every pair degrades to
  its KO-id-only first element, `bridged_fraction` is recorded as exactly
  `0.0`, and `AnnotationResult.parameters["ko_function_map"] = "absent"` is
  set — this is a reported degradation, never a raised error.

Both classes follow the existing Docker/native dispatch convention exactly
(`<tool>.docker_image` empty ⇒ native binary on `PATH`, `<tool>.docker_workdir`
for the per-run tempdir base, availability probed via `docker image
inspect`). Docker invocations run with `--user <uid>:<gid>`, `--network
none`, and the database/profile-set root bind-mounted `:ro`. `BaktaUtils`'s
Docker path additionally overrides the entrypoint to `bash -lc` so `PATH`
can be prefixed with `/opt/conda/bin` — without it `bakta_proteins` fails
with "AMRFinderPlus not found or not executable" per the task brief.

Also updated `kbutillib/domains/genome/annotation/__init__.py` to re-export
`BaktaUtils` and `KofamscanUtils` alongside the existing annotator classes.
Per the task's explicit scope boundary, nothing outside the annotation
package (`domains/genome/__init__.py`, top-level `kbutillib/__init__.py`,
Dockerfiles, generator scripts, KBDL wiring) was touched — those belong to
sibling tasks.

**Environment note (worth recording for the reviewer/coordinator):** the two
new files and the `__init__.py` edit were initially written to the wrong
path — the Dropbox parking repo's working tree
(`~/Dropbox/Projects/KBUtilLib`) instead of this task's worktree
(`~/.maestro/worktrees/kbutillib-bakta-kofamscan-annotators`). This was
caught before any commit; the files were copied into the correct worktree
and the Dropbox repo's working tree was reverted with `git checkout --`
(confirmed clean via `git status --porcelain`) before proceeding. No commit
ever touched the Dropbox repo directly.

## files_touched

- `src/kbutillib/domains/genome/annotation/bakta_utils.py` (new)
- `src/kbutillib/domains/genome/annotation/kofamscan_utils.py` (new)
- `src/kbutillib/domains/genome/annotation/__init__.py` (modified — re-exports)
- `tests/annotators/test_bakta_utils.py` (new)
- `tests/annotators/test_kofamscan_utils.py` (new)
- `agent-io/work-records/kbutillib-bakta-kofamscan-annotators.md` (this file)

## success_criteria_check

Restated from the task's "SUCCESS CRITERIA" section:

1. **`kbutillib/domains/genome/annotation/` contains `bakta_utils.py` and
   `kofamscan_utils.py`, each exposing `annotate()` returning
   `AnnotationResult` and raising `ToolUnavailableError` when the tool or
   database is absent.** PASS — both classes implement `annotate(proteins,
   threads=1, **params) -> AnnotationResult`; both call
   `self._require_available()` first (raises `ToolUnavailableError` when
   `is_available()` is False); `BaktaUtils` additionally raises
   `ToolUnavailableError` when the database's `version.json` is missing or
   unparseable. Covered by `TestAnnotateValidation` and
   `TestReadDbVersion` in both test files.

2. **BAKTA emits only the product string as a FUNCTION Term.** PASS —
   `_parse_bakta_features` emits exactly one
   `Term(namespace="FUNCTION", id=None, value=<product>)` per gene;
   `ec_ids`/`kegg_orthology_id`/`cog_id`/`go_ids` are read from
   `feature["psc"]` and placed only in `Term.evidence`. Verified by
   `TestParseBaktaFeatures::test_ec_kegg_cog_go_never_emitted_as_terms`,
   which asserts the emitted namespace set is exactly `{"FUNCTION"}` while
   the four channels are still present (uncollapsed) in `evidence`.

3. **KOFAMSCAN emits, per significant hit, the KO id followed by the
   bridged text, and filters detail-tsv to the significance flag.** PASS —
   `_build_kofam_records` emits `Term(namespace="KO", id=ko_id,
   value=ko_id)` then, when bridgeable, `Term(namespace=None, id=None,
   value=<bridged text>)`, in that order (though ordering carries no
   semantics per the task's own instruction and tests compare as a
   multiset). `_parse_kofam_detail_tsv` drops every row whose first column
   is not exactly `"*"`. Verified by
   `TestBuildKofamRecords::test_bridgeable_hit_emits_id_then_bridged_text_pair`
   and `TestParseKofamDetailTsv::test_below_threshold_row_is_excluded`.

4. **The bridge is a pure function unit-tested over an injected in-memory
   table requiring no generated artifact.** PASS — `_bridge_ko(ko_id,
   table) -> str | None` is a one-line dict lookup with zero I/O; the table
   is a caller-supplied argument. `TestBridgeKo` injects a small inline
   dict covering a multi-symbol prefix (`"e1.1.1.1, adh; alcohol
   dehydrogenase"`), an `[EC:...]` block (`"hom; homoserine dehydrogenase
   [EC:1.1.1.3]"`), and a non-bridging KO (returns `None`) — none of these
   touch a file or any sibling-task-generated artifact.

5. **A multimap test proves a gene with several significant hits yields a
   term per hit.** PASS —
   `TestParseKofamDetailTsv::test_multimap_gene_with_several_significant_hits`
   and `TestBuildKofamRecords::test_multimap_gene_yields_term_per_hit`
   assert all N KO ids for a single gene survive parsing/record-building,
   not just the last (the explicit regression against the upstream
   `bioseed_tools` parser's `res[id] = KO` last-hit-wins bug named in the
   PRD).

6. **A filtering test proves below-threshold rows are excluded.** PASS —
   `TestParseKofamDetailTsv::test_below_threshold_row_is_excluded` feeds a
   fixture with one `*`-flagged and one blank-flagged row and asserts only
   the flagged one survives.

7. **An absent `ko_function_map` yields KO ids only with
   `bridged_fraction` 0.0 and an absent marker rather than an error.**
   PASS — `TestBuildKofamRecords::test_table_none_forces_bridged_fraction_zero`
   / `test_table_none_emits_id_only` cover the pure function; end-to-end,
   `TestAnnotateMocked::test_absent_ko_function_map_yields_ko_ids_only`
   asserts `annotate()` succeeds (no exception), every emitted Term has
   `namespace == "KO"` (no bridged-text Term), `parameters["bridged_fraction"]
   == 0.0`, and `parameters["ko_function_map"] == "absent"`.
   `test_configured_but_missing_file_also_degrades_not_fails` covers the
   "configured but the file is gone" variant of the same degradation.

8. **No test that passed on the base commit fails on the branch.** PASS
   (regression-checked, not merely asserted) — see `tests_run` below: the
   full suite went from the stated baseline (2759 passed / 1 failed / 6
   errors / 379 skipped) to 2834 passed / 1 failed / 6 errors / 379
   skipped on this branch. 2834 − 2759 = 75, exactly the number of new
   tests added. The single failure and all six errors are byte-identical
   to the task envelope's `failed_node_ids` list (GAA/cobra-absence
   failures unrelated to this change) — confirmed by grepping the full
   run's `FAILED`/`ERROR` lines against that list.

## tests_run

- `.venv-dev/bin/python -m pytest tests/annotators/test_bakta_utils.py
  tests/annotators/test_kofamscan_utils.py -q` → **75 passed** (the new
  tests, run first in isolation while iterating).
- `.venv-dev/bin/python -m pytest tests/annotators/ -q` → **360 passed, 2
  skipped** (full annotators directory, confirming no interaction with the
  existing Prokka/DRAM2/Transyt/RAST test modules).
- `ruff check` on all five touched/added files → **All checks passed**
  (one unused `pathlib.Path` import in `test_bakta_utils.py` was caught
  this way and removed before the commit).
- Full baseline command, exactly as specified in the task envelope:
  `python -m pytest tests/ -q --continue-on-collection-errors` (via
  `.venv-dev/bin/python`, timeout 600000 ms, actual wall time ~270s) →
  **2834 passed, 1 failed, 379 skipped, 6 errors**. The failed test
  (`tests/modeling/test_ms_reconstruction_utils.py::test_default_mode_uses_db_fallback`)
  and all 6 errors (`tests/biochem/test_escher_utils.py`,
  `tests/notebook/helpers`, and the four
  `tests/modeling/test_comprehensive_gapfill_wrapper.py::*` cases) exactly
  match the envelope's stated `failed_node_ids` — confirmed by explicit
  string comparison, not just count. No environment noise from the
  `tomli-w` trap the task warned about (verified `import tomli_w` and
  `import kbutillib` both succeed in `.venv-dev` before running anything).

Environment used: a fresh venv at
`~/.maestro/worktrees/kbutillib-bakta-kofamscan-annotators/.venv-dev`,
created via `python3 -m venv .venv-dev` then `pip install -e ".[dev,all]"` —
not reused from any pre-existing `~/VirtualEnvironments/*` venv, per the
task's explicit warning about stale editable installs. `.venv-dev/` is
untracked and was never staged.

## caveats

- **Bakta `input.json` feature schema is inferred, not measured from a
  real payload.** The task brief describes the schema only at the level of
  field names (`id`, `product`, `gene`, `psc.ec_ids`, `psc.kegg_orthology_id`,
  `psc.cog_id`, `psc.go_ids`, `aa_hexdigest`, and the `version` stamp
  `{"bakta": ..., "db": {"version": ..., "type": ...}}`); I could not reach
  a live Bakta install from this environment to confirm the exact nesting.
  `_parse_bakta_features` is written defensively (`.get()` with fallbacks,
  skips non-dict entries, skips features without an `id` or `product`) so
  it degrades gracefully rather than raising if the real payload's shape
  differs in some minor way, but a reviewer with access to a real
  `input.json` sample should sanity-check the field names against it,
  ideally as part of the PRD's separate host-gated live-verification task
  (criteria 28-31, explicitly out of my task's scope).

- **No real-dependency guard tests were added.** The task's inline
  instructions scope this task strictly to the two annotator modules plus
  the four listed unit-test categories (bridge, multimap, filtering,
  absent-table) — it does not ask for a Docker-backed guard test, and the
  Dockerfiles that such a guard would depend on belong to the explicitly
  named sibling task. The PRD background document does describe
  mandatory real-dependency guards (criterion 21-24) but the task's own
  inline "SUCCESS CRITERIA" section — which the dispatch contract
  instructed me to treat as authoritative over the PRD — does not include
  them for this task. I judged this the conservative, in-scope
  interpretation rather than reaching into a sibling task's territory.

- **KOfamScan `detail-tsv` column layout is inferred from the PRD's prose
  description** (`sig_flag, gene name, KO, thrshld, score, E-value, KO
  definition`, tab-separated), not from a captured real sample. Same
  defensive-parsing posture as above (`_parse_kofam_detail_tsv` skips rows
  with fewer than 6 tab-separated fields rather than raising).

- **`ko_list_version` is set equal to `profile_set`** (the bare profile-set
  directory name), per D9's statement that "the version *is* the
  profile-set directory name, paired with its `ko_list` at `<name>.txt`" —
  I did not implement a separate read of a version string from inside the
  `.txt` file itself, since the task brief does not specify one and the
  directory-name-as-version convention is explicit in the PRD.

- **`kegg_release` in `AnnotationResult.parameters` is sourced from the
  loaded table's own `# kegg_release: ...` header comment**, via
  `_parse_ko_function_map_text`'s generic `# key: value` comment parser.
  The exact header format of the real generated `ko_function_map.tsv` is
  owned by the sibling generator-script task and was not available to
  verify against; the parser accepts any `# key: value` line so it should
  tolerate reasonable header variations, but the specific key name
  `kegg_release` is my choice and should be cross-checked against
  whatever key name the generator script actually emits.

- Did not touch `domains/genome/__init__.py` or the top-level
  `kbutillib/__init__.py` lazy-import/re-export tables (where
  `ProkkaUtils`/`DRAM2Utils`/`TransytUtils` are additionally exposed as
  `from kbutillib import X`), even though doing so would be a natural
  consistency follow-up. The task's explicit instruction — "do NOT edit
  anything outside the annotation package" — is why; `BaktaUtils` and
  `KofamscanUtils` are currently reachable only via
  `from kbutillib.domains.genome.annotation import BaktaUtils` /
  `from kbutillib.domains.genome.annotation.bakta_utils import BaktaUtils`
  (and the `kofamscan_utils` equivalent), not via the top-level `kbutillib`
  package. If broader reachability is wanted, it is a small, mechanical
  follow-up someone with authority over those two files should make.
