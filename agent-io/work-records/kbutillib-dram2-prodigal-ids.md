# Work Record: kbutillib-dram2-prodigal-ids

## task_id
kbutillib-dram2-prodigal-ids

## branch
maestro/developer/kbutillib-dram2-prodigal-ids

## commit_shas
- 226c5fa2 (see `git log` on the branch for the full 40-char SHA)

## summary

Investigated the DRAM2 gene-id defect described in the task envelope
(`DRAM2Utils.annotate` handing DRAM2 bare locus ids like `b0002`, which fail
`combine_annotations.py`'s `int(id.split("_")[-1])` check). Reading
`src/kbutillib/domains/genome/annotation/dram2_utils.py` on the task's own
base commit (`main` @ `2e448d3`) showed the defect had already been fixed by
an earlier, independent task (`dram2-id-remap-impl`, commit `e891617`, an
ancestor of `main`): `_write_faa` assigns synthetic prodigal-safe ids
`g_1, g_2, ...` (final `_`-delimited token always an integer), writes
prodigal-format `# start # end # strand #` headers, and
`_parse_annotations_tsv` translates every `query_id` back to the caller's
original id via an internal `{emitted_id -> caller_id}` reverse map before
constructing `AnnotationRecord`s — so `gene_id` is already byte-identical to
the caller's input with no `_<n>` suffix observable, exactly matching this
task's success criteria.

The one gap against the stated success criteria was test coverage: no
existing test exercised a caller id that *already* ends in `_<digits>`
(e.g. `gene_12`, or the edge case `g_1` — textually identical to what the
scheme would emit as the first synthetic id) to prove the synthetic-id
scheme cannot collide with, or double-apply a suffix onto, such an id. Added
`TestSuffixNoCollideNoDoubleApply` to
`tests/annotators/test_dram2_utils.py` with two tests: one asserting
`_write_faa` still emits the plain `g_1, g_2, g_3` sequence (untouched by
caller-text shape) and returns a reverse map that recovers each original id
exactly, and one full write-then-parse round trip asserting
`AnnotationRecord.gene_id` values are `["g_1", "gene_12", "gene_1"]` —
byte-identical to the inputs, with no synthetic `g_<n>` id and no
`gene_12_2`-style double-suffix ever appearing in the output.

No production code in `dram2_utils.py` needed to change; the fix was
already correct. Only the test file changed.

## files_touched

- `tests/annotators/test_dram2_utils.py` (added `TestSuffixNoCollideNoDoubleApply`, 2 new tests, 68 lines)

## success_criteria_check

1. "DRAM2Utils.annotate writes gene ids whose final underscore-delimited
   token parses as an integer" — **PASS**. `_write_faa` emits `g_1, g_2, ...`;
   every final token is a plain positive integer. Verified pre-existing by
   `TestB0001RoundTrip.test_write_faa_emits_numeric_final_token` and now also
   by the new `test_write_faa_ids_already_suffixed_do_not_collide`.

2. "descriptions in prodigal '# start # end # strand #' form" — **PASS**.
   `_write_faa` writes `>{emitted_id} # {start} # {stop} # {strand} #` for
   every record; verified by pre-existing
   `TestWriteFaaProdigalHeaders.test_header_format_matches_prodigal_pattern`.

3. "every AnnotationRecord.gene_id it returns is byte-identical to a
   caller-supplied input id with no '_<n>' suffix observable" — **PASS**.
   `_parse_annotations_tsv` looks up `emitted_to_caller[query_id]` and sets
   that as `AnnotationRecord.gene_id`; verified pre-existing by
   `TestB0001RoundTrip.test_full_roundtrip_write_then_parse` and now also by
   the new `test_full_roundtrip_preserves_already_suffixed_ids`, which
   additionally proves this holds for ids that themselves already look
   prodigal-safe (`g_1`, `gene_1`, `gene_12`).

4. "A test covers ids that already end in '_<digits>' to prove the suffix
   logic does not collide or double-apply" — **PASS (this was the actual
   gap; now closed)**. `TestSuffixNoCollideNoDoubleApply` uses
   `{"g_1": ..., "gene_12": ..., "gene_1": ...}` as input, including one id
   (`g_1`) that is textually identical to the first synthetic emitted id and
   two (`gene_12`, `gene_1`) that already end in `_<digits>`. Both tests
   assert no collision (emitted sequence is still plain `g_1, g_2, g_3`,
   unrelated to caller text) and no double-apply (`gene_ids ==
   ["g_1", "gene_12", "gene_1"]`, never e.g. `"gene_12_2"`).

5. "No test that passed on the base commit fails on the branch" — **PASS**.
   Full-suite run: `2761 passed, 1 failed, 6 errors, 379 skipped` vs. the
   measured baseline `2759 passed, 1 failed, 6 errors, 379 skipped`. The
   delta is exactly the 2 new tests added (both pass); the 1 failure and 6
   errors are the identical known-failing set from the envelope
   (`test_default_mode_uses_db_fallback`, `test_escher_utils.py` collection,
   `tests/notebook/helpers` (missing `cobra`), and the four
   `test_comprehensive_gapfill_wrapper.py` cases) — none newly broken.

## tests_run

```
cd /Users/chenry/.maestro/worktrees/kbutillib-dram2-prodigal-ids
.venv-dram2task/bin/python -m pytest tests/annotators/test_dram2_utils.py -q
# 93 passed, 1 skipped in 12.97s

.venv-dram2task/bin/python -m pytest tests/ -q --continue-on-collection-errors
# 1 failed, 2761 passed, 379 skipped, 6 errors in 269.81s (0:04:29)
```

Both runs used a fresh venv created per the envelope's instructions
(`python3 -m venv .venv-dram2task && .venv-dram2task/bin/pip install -e
".[dev,all]"`), confirmed to include `tomli_w` before running.

## caveats

- **The task's premise did not hold against this task's own base commit.**
  The envelope describes the defect as still present ("DRAM2Utils currently
  writes bare locus ids like 'b0002'"), but on `main` @ `2e448d3` (the base
  this task was cut from) the fix had already landed via an unrelated prior
  task (`dram2-id-remap-impl`, commit `e891617`, which the task envelope did
  not reference). I verified this by reading the file directly rather than
  trusting the envelope's description, per "read before writing." Judgment
  call: since the substantive fix was already correct and only the specific
  test case named in the success criteria ("ids that already end in
  '_<digits>'") was missing, I added exactly that test rather than
  rewriting already-correct production code, to avoid an unrequested
  refactor of working code.
- The prior fix's synthetic-id scheme (`g_1, g_2, ...`, fully independent of
  caller-id text) differs from the illustrative form in the task prose
  (`<original_locus>_<n>`). Both satisfy the literal success criteria (final
  token is an integer; round-trip is exact); I did not change the scheme,
  since the task's actual acceptance bar is stated in the SUCCESS CRITERIA
  section, not the illustrative "THE FIX" prose, and rewriting a
  well-tested, already-landed scheme was out of scope and would risk
  regressing the `dram2-id-remap-impl`/`dram2-id-remap-live-h100` work
  (including a prior real h100 Nextflow run validated against this exact
  id scheme).
- Did not touch `bakta_utils.py`, `kofamscan_utils.py`, `docker/`, or the
  annotation package `__init__.py`, per the scope fence.
- Left the ad-hoc venv `.venv-dram2task/` untracked in the worktree; it is
  not part of the commit (staged only `tests/annotators/test_dram2_utils.py`
  by name).
