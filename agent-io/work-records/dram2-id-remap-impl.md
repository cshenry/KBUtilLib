# Work Record: dram2-id-remap-impl

## task_id
dram2-id-remap-impl

## branch
conductor/dram2-id-remap/impl

## commit_shas
- e891617d6502e93a42a60a32ed201b6148967e0d

## summary

Implemented DRAM2Utils id-remap + run-hardening per PRD
`agent-io/prds/dram2-input-genes-id-remap/fullprompt.md`.  The core
problem was that `_write_faa` emitted caller ids directly as FASTA header
tokens, and real locus ids (`b0001`) fail DRAM2's
`combine_annotations.py` `int(id.split("_")[-1])` check.  The fix
assigns synthetic prodigal-safe ids `g_1, g_2, ...` when writing the
input FASTA, records the `{emitted_id -> caller_id}` reverse map, and
translates each `query_id` back to the caller id when parsing
`raw-annotations.tsv`.  Public interface and `AnnotationResult.records[].gene_id`
semantics are unchanged.  Additionally hardened the run: explicit
subprocess env (`NXF_VER=24.10.5`, optional `PATH` prefix from config),
`mkdtemp`-based scratch under `dram2.work_root` (default `<launch_dir>/scratch`,
never `/tmp`), keep-on-failure preservation of scratch dir +
`pipeline_info/` + `.nextflow.log`, and opt-in keep-on-success via
`dram2.keep_work` config / `annotate(..., keep_work)` kwarg.
`is_available()` now warns when pipeline is not under launch_dir.

## files_touched

- `src/kbutillib/dram2_utils.py`
- `tests/annotators/test_dram2_utils.py`
- `agent-io/work-records/dram2-id-remap-impl.md` (this file)

## success_criteria_check

1. `pytest tests/annotators/test_dram2_utils.py` passes including b0001 round-trip — **PASS**: 86 passed, 1 skipped (live h100 gate)
2. `_write_faa` writes `^g_\d+$` headers and returns `{emitted_id: caller_id}` dict — **PASS**: `TestB0001RoundTrip` + `TestWriteFaa` + `TestWriteFaaProdigalHeaders`
3. Emitted ids in insertion order (g_1 first, g_2 second); parsed result follows same order — **PASS**: `test_full_roundtrip_write_then_parse` asserts `gene_ids == ["b0001", "b0002"]`
4. `_parse_annotations_tsv(tsv_text, emitted_to_caller)` translates and drops unknown query_ids — **PASS**: `TestParseAnnotationsTsv.test_unknown_query_id_dropped` + `test_translation_uses_caller_id_not_emitted_id`
5. `gene_coords` coords looked up by caller_id; synthetic fallback; strand normalized — **PASS**: `TestWriteFaaProdigalHeaders`
6. Subprocess env has `NXF_VER=dram2.nxf_ver`; non-empty `env_path` prepended to `PATH` — **PASS**: `TestBuildSubprocessEnv`
7. `env_path` consumed as a single colon-separated string — **PASS**: implemented as `str` config key; test validates PATH prefix
8. Scratch under `dram2.work_root` (default `<launch_dir>/scratch`), never `/tmp` — **PASS**: `TestKeepOnFailure.test_scratch_under_work_root_not_tmp`
9. Scratch deleted on success unless `keep_work=True` — **PASS**: `TestAnnotateWithMockedNextflow.test_keep_work_kwarg_false_deletes_scratch` + `test_keep_work_kwarg_true_preserves_scratch`
10. Failed run preserves scratch + creates `failed-<run_id>/`; copies pipeline_info/ and .nextflow.log when present; skips missing; re-raises — **PASS**: full `TestKeepOnFailure` suite
11. `AnnotationResult.parameters` includes `work_dir` and `kept` on every run — **PASS**: `TestAnnotateWithMockedNextflow.test_parameters_include_work_dir_and_kept`
12. Public `annotate(proteins, databases, gene_coords, run_config, threads)` unchanged for existing callers; only `keep_work` added — **PASS**: signature adds only optional `keep_work: bool | None = None`
13. `is_available()` warns (does not return False) when pipeline not under launch_dir — **PASS**: `TestIsAvailable.test_warns_when_pipeline_not_under_launch_dir`
14. Existing golden-fixture parse tests pass after adaptation to identity `{id: id}` maps — **PASS**: `TestParseAnnotationsTsv` all green; all `_parse_annotations_tsv` calls updated to pass `dict[str, str]` identity maps
15. Offline unit tests cover b0001 round-trip, `_write_faa` map+coords, parser translation + unknown drop, env builder, keep-on-failure — **PASS**: `TestB0001RoundTrip`, `TestWriteFaa`, `TestWriteFaaProdigalHeaders`, `TestBuildSubprocessEnv`, `TestKeepOnFailure`
16. Phase 2 live h100 validation — **NOT ATTEMPTED** (Criterion 16 is the separate Phase-2 task; explicitly out of scope per task envelope)
17. No file under `GenomeAnnotationAggregator/` modified — **PASS**: `git diff --name-only HEAD` shows only `src/kbutillib/dram2_utils.py` and `tests/annotators/test_dram2_utils.py`

## tests_run

```
cd /Users/chenry/.maestro/worktrees/dram2-id-remap-impl
python -m pytest tests/annotators/test_dram2_utils.py -q
```

Result: **86 passed, 1 skipped** in ~1.2s.

The skipped test is `TestDram2LiveIntegration::test_annotate_real_proteins`, gated by `KBU_DRAM2_LIVE=1` which is not set in the offline environment.

## caveats

- The `_run_nextflow` signature gained two new keyword parameters (`run_id`, `work_root`) for API symmetry with `annotate()`'s failure-preservation logic, but those parameters are not used inside `_run_nextflow` itself — the failure handling is in `annotate()`'s `except` block. This is intentional: `_run_nextflow` raises `CalledProcessError` on non-zero exit, and `annotate()` catches it, preserves evidence, and re-raises.
- `TestAnnotateWithMockedNextflow` tests that previously used the golden TSV verbatim (which had `OWC_...` query_ids) needed to be updated. Tests that check record content now pass a remapped TSV via `_remap_tsv_query_ids()`. Tests that only check parameters/metadata use an empty TSV mock.
- The `test_query_id_is_first_whitespace_token` test in `TestWriteFaaProdigalHeaders` was renamed from its original form to `test_emitted_id_is_first_whitespace_token` and now asserts `first_token == "g_1"` (the synthetic id) instead of the caller id.
- `strand_raw == 0` normalizes to `-1` (per the original `else -1` branch), matching the existing behavior.
