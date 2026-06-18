# Work Record: prokka-module

## task_id
prokka-module

## branch
conductor/annotation-tool-modules/prokka-module

## commit_shas
- 8ebecbafd86a8d1b1d2684f5967fe238dd2db5d0

## summary
Implemented `ProkkaUtils(AnnotatorUtils)` in `src/kbutillib/prokka_utils.py` per the annotation-tool-modules PRD (PROKKA mechanism + Confront-resolved specs 4–9). The module emulates the KBase `kb_prokka` genome re-annotation workaround: each caller gene is remapped to a deterministic safe id `g{index}` (eliminating the >32-char abort that breaks the KBase app), written as a single-gene-contig FASTA record, annotated by PROKKA without `--proteins`, and the resulting product/EC/gene/COG terms are mapped back to the caller's original ids via the GFF seqname→locus_tag linkage. Pure parse functions (`_parse_gff_locus_map`, `_parse_tsv`, `_row_to_terms`) are split from the subprocess runner (`_run_prokka`) for offline testability. A golden TSV+GFF fixture set is committed under `tests/fixtures/prokka/` covering all specified scenarios; 121 offline unit tests pass with no PROKKA installed; the annotator modules reach 100% coverage.

## files_touched
- `src/kbutillib/prokka_utils.py` — new: ProkkaUtils class + pure parse helpers
- `src/kbutillib/__init__.py` — added ProkkaUtils import/export
- `tests/annotators/test_prokka_utils.py` — new: 68 test functions (67 offline + 1 live skipif)
- `tests/fixtures/prokka/prokka.tsv` — new: golden TSV fixture (6 rows: 5 CDS + 1 rRNA)
- `tests/fixtures/prokka/prokka.gff` — new: golden GFF fixture (5 CDS entries for g0–g4, 1 rRNA for g3)

## success_criteria_check

1. **ProkkaUtils.annotate writes single-gene-contig FASTA with g{index} safe ids** — PASS. The `annotate()` method builds `caller_to_safe` map of `g{index}` ids and writes `>{safe_id}\n{seq}` per gene. Verified by `test_annotate_long_id_does_not_abort` and `test_annotate_records_keyed_by_caller_ids`.

2. **Runs prokka without --proteins** — PASS. The `_run_prokka` command list never includes `--proteins`. Verified by `test_run_prokka_kingdom_in_command` (inspects assembled command).

3. **Parses .tsv CDS rows into product/EC/gene/COG Terms keyed back to caller ids** — PASS. `_parse_tsv` + `_row_to_terms` build Terms with correct namespaces (None/product, "EC", "GENE", "COG"). Records keyed by caller ids via safe→caller reverse map. Verified by `TestParseTsv::test_golden_*` suite.

4. **Handles >32-char ids without aborting** — PASS. The caller id `"gene_with_id_longer_than_32_chars_abcdefghijklmnopqrstuvwxyz"` (60 chars) is remapped to `g2` internally; the result contains the original long id. Verified by `test_golden_long_id_remapped` and `test_annotate_long_id_does_not_abort`.

5. **Applies longest-CDS tie-break** — PASS. Golden fixture has two CDS rows for `g2` (450 bp vs 750 bp); the 750 bp row ("Serine hydroxymethyltransferase") is selected. Tie-break by start coordinate also tested via `test_golden_multi_orf_tiebreak_by_start`.

6. **Offline golden-fixture unit test passes with no prokka installed** — PASS. All 67 offline tests pass without any subprocess calls to prokka. The live integration test is marked `@pytest.mark.skipif(not _prokka_available(), ...)` and skipped (1 skipped).

7. **Live skipif-gated test present** — PASS. `TestProkkaLiveIntegration::test_annotate_small_gene` is decorated with `@pytest.mark.integration` and `@pytest.mark.skipif(not _prokka_available(), reason="prokka not installed")`.

8. **ProkkaUtils exported from __init__.py** — PASS. Added try/except import block and added "ProkkaUtils" to `__all__`. Verified by `TestProkkaExports::test_prokka_utils_exported` and `test_prokka_utils_is_correct_class`.

9. **fail_under=100 coverage gate green for new code** — PASS. When run as `pytest tests/annotators/ tests/guard/ --cov=kbutillib.prokka_utils --cov=kbutillib.annotator_utils`, both modules reach 100% coverage (121 passed, 1 skipped). Full-suite coverage is lower due to pre-existing pandas/cobra-dependent tests that cannot run in this environment; those gaps are not caused by this task.

## tests_run

```
pytest tests/annotators/ tests/guard/ \
  --cov=kbutillib.prokka_utils \
  --cov=kbutillib.annotator_utils \
  --cov-report=term-missing
```

Result:
```
Name                               Stmts   Miss Branch BrPart  Cover
----------------------------------------------------------------------
src/kbutillib/annotator_utils.py      58      0     10      0   100%
src/kbutillib/prokka_utils.py        161      0     58      0   100%
----------------------------------------------------------------------
TOTAL                                219      0     68      0   100%
Required test coverage of 100.0% reached. Total coverage: 100.00%
121 passed, 1 skipped
```

Live integration test: skipped (prokka not on PATH on primary-laptop).

## caveats

1. **GFF-based contig→locus_tag mapping**: The implementation parses both the GFF (for `seqname→locus_tag` mapping) and the TSV (for functional annotations). The TSV alone does not contain contig/seqname info, so the GFF parse is required. This is documented in the module docstring and is consistent with the PRD's "parse the .tsv (not just the GFF)" intent — we use both.

2. **Multi-ORF tie-break choice**: The PRD spec (item 7) calls for "longest by length_bp, ties by smallest start." This was implemented as `sort(key=lambda t: (-t[0], t[1]))`. The `start` coordinate comes from the GFF; if a locus_tag appears in the TSV but not the GFF (shouldn't happen with real PROKKA output), it defaults to `start=0` (from `locus_to_safe_start.get(locus_tag, (safe_id, 0))`).

3. **Coverage gate scope**: The `fail_under=100` gate in pyproject.toml applies globally to `source=["kbutillib", "tests"]`. The full suite has pre-existing coverage gaps in pandas-dependent modules (ms_biochem_utils, ms_reconstruction_utils, etc.) and integration tests that need credentials. The gate passes when scoped to the new annotator modules, consistent with how the annotator-base task was validated.

4. **`_parse_prokka_version` fallback**: The version probe falls back to running `prokka --version` if the run-time stderr doesn't contain the version string. This adds a second subprocess call post-run. If this is undesirable, it can be removed and `tool_version` will return None for those environments — both are valid per the PRD ("tool_version: string from tool --version or None").

5. **DRAM2 and Transyt modules**: Only PROKKA is implemented in this task. DRAM2 and Transyt are separate tasks per the PRD task plan.
