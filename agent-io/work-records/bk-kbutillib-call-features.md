# Work record: bk-kbutillib-call-features

## task_id
bk-kbutillib-call-features

## prd
kbdl-build-genome-bakta-calling-v1

## branch
`maestro/developer/base-kbutillib-main-d19d674-task-bk-kbutillib-call-features`

## commit_shas
- `6edf665` — `feat(bakta): structural-only gene calling via BaktaUtils.call_features`
- `059019c` — `test(bakta): cover the integration test's own skip guard`
- (this work record is committed on top of those)

## base
`main` at `d19d674` — the commit named in the envelope. `main` had not moved, so no
rebase was needed.

## summary
Added **Bakta structural gene calling** (no functional annotation) to KBUtilLib:
`BaktaUtils.call_features(contigs, ...) -> BaktaCallResult`, backed by a new standalone
driver `bakta_call_features.py` that is *run* inside the Bakta container rather than
imported.

The design is forced by Bakta 1.11.4 itself. Read out of the image
(`/opt/conda/lib/python3.11/site-packages/bakta/main.py`, confirmed `bakta.__version__ ==
"1.11.4"`), the CDS branch of `main.py` is one straight-line block: once `--skip-cds` is
off it *always* runs UPS/IPS lookup, the PSC/PSCC Diamond searches, AMRFinderPlus, the
expert Diamond systems, pseudogene detection and Pfam. No CLI flag stops after prediction.
So the driver calls Bakta's own structural predictors directly, in `main.py`'s order, and
nothing else.

## files_touched

### Added — `src/kbutillib/domains/genome/annotation/bakta_call_features.py` (607 lines)
The driver. Imports only `bakta` and the standard library; never `kbutillib`.

- `PINNED_BAKTA_VERSION = "1.11.4"` (`:106`). `main()` (`:414`) checks
  `bakta.__version__` **before importing any other bakta module** and, on a mismatch,
  prints `Bakta version mismatch: driver pinned 1.11.4, image has <v>` to stderr and
  returns `EXIT_VERSION_MISMATCH = 3`.
- `_build_bakta_argv` (`:167`) assembles the argv handed to Bakta's *own*
  `bakta.utils.parse_arguments` (via `sys.argv`), then `bakta.config.setup(args)`:
  `--db --output <tmp> --keep-contig-headers --min-contig-length 1 --translation-table T
  --threads N --skip-ncrna-region --skip-sorf --skip-gap --skip-ori --skip-pseudo
  --skip-plot`, plus `--skip-trna --skip-tmrna --skip-rrna --skip-ncrna --skip-crispr`
  when `--no-non-coding`. `--complete` and `--compliant` are never passed (docstring
  records why: `--complete` flips every contig to a circular replicon, changing
  pyrodigal's `closed` flag and enabling the origin-spanning edge-CDS merge;
  `--compliant` forces `min_sequence_length = 200`, silently dropping short contigs).
- `_run` (`:486`) mirrors `main.py` exactly for `cfg.check_output_path` → `cfg.setup` →
  `cfg.db_info = db.check(cfg.db_path)` → `fasta.import_sequences` →
  `bu.qc_sequences` → `fasta.export_sequences` to `cfg.tmp_path/sequences.fna` → the
  `data` dict and `stats.size` → then, in order and only when enabled,
  `t_rna.predict_t_rnas`, `tm_rna.predict_tm_rnas`, `r_rna.predict_r_rnas`,
  `nc_rna.predict_nc_rnas`, `crispr.predict_crispr` → `feat_cds.predict` →
  `feat_cds.filter_length` → `orf.detect_spurious` → `anno.detect_feature_overlaps`.
  No function from `feat_cds` beyond those two filters, and none from `ups`/`ips`/`psc`/
  `pscc`/`expert.*`, is reached.
- `_call_cdss` (`:384`) is the **R5** separate discard accumulator, mirroring
  `main.py:244-256`: `filter_length` runs on the full predicted set and its victims are
  removed *before* AntiFam; AntiFam's victims are removed before `data['features']`. So
  neither ever reaches `detect_feature_overlaps`, but both are still reported.
- `_feature_record` (`:317`) builds each output record **field by field** (**R5**):
  never `json.dump`s a Bakta dict, which would die on `aa_digest` (raw `bytes`) and
  mis-serialise `class` (an `SO` namedtuple). `db_xrefs` is dropped entirely except for
  the Rfam accession, which `_rfam_accession` (`:240`) extracts — so GO xrefs are
  structurally gone, not filtered.
- Module docstring lists every `main.py` step deliberately *not* mirrored, with the
  reason: `bu.test_dependencies`, `nc_rna_region.predict_nc_rna_regions`,
  `revise_translational_exceptions`, `revise_special_cases_annotated`,
  `import_user_cdss`, sORF/gap/ori, locus tags, `select_gene_symbols`, every writer.

### Modified — `src/kbutillib/domains/genome/annotation/bakta_utils.py`
- Module docstring: new "Structural gene calling (`call_features`)" section.
- `BaktaCallError(RuntimeError)` and frozen dataclass `BaktaCallResult`
  (`features`, `discarded`, `versions`, `call_non_coding`).
- `__init__` gains `docker_image: str | None = None` (**R6**). `None` reads
  `bakta.docker_image` from config; an explicit value — *including `""`*, which selects
  native mode — is authoritative and the ambient config cannot override it. Also reads
  the new config key `bakta.python` (default `python3`).
- `call_features(contigs, *, translation_table=11, threads=1, call_non_coding=True)`.
  Per **R6** there is **no per-call `image` kwarg**; the instance's image is used.
- `_driver_path` / `_driver_sha256` / `_docker_image_id` (**R8**).
- `_call_features_available` / `_require_call_features_available`: a separate probe from
  `is_available()`, because structural calling needs the Docker image or the
  `bakta.python` interpreter, not the `bakta_proteins` executable.
- `_build_call_features_cmd`: Docker run with `--network none`, `--user <uid>:<gid>`,
  DB at `/db:ro`, work dir at `/work`, **the driver bind-mounted read-only** at
  `/driver/bakta_call_features.py`, `--entrypoint bash -lc 'PATH=/opt/conda/bin:$PATH
  python …'`. Native: `<bakta.python> <driver> …`.
- `_run_driver(cmd) -> (returncode, stderr)`: the single subprocess seam.
- `_driver_failure_message`: exit 3 names **both** versions; otherwise the stderr tail
  (2000 chars).
- Pure, offline-testable helpers: `_completeness` (**R2**), `_build_kept_feature`,
  `_build_discarded_cds`, `_split_call_features`.
- `_write_one_line_fna`: writes `>id` with **no description** — load-bearing, because
  `bu.qc_sequences` reads the description and flips a contig to complete/circular/
  plasmid on `circular=true`/`complete`/`plasmid` text.

### Modified — `src/kbutillib/domains/genome/annotation/__init__.py`
Re-exports `BaktaCallError` and `BaktaCallResult`.

### Modified — `pyproject.toml`
`bakta_call_features.py` declared under `[tool.setuptools.package-data]` with a comment
explaining that it is a data file, not a module (nothing imports it; it is resolved by
path and bind-mounted to be run, and it imports `bakta`, which KBUtilLib's own
environment does not have).

Verified empirically, not just by grep: built a wheel with
`pip wheel --no-deps --no-build-isolation -w /scratch/chenry/bakta-wheel .`
(`kbutillib-1.0.0-py3-none-any.whl`, 14.9 MB, exit 0) and confirmed
`kbutillib/domains/genome/annotation/bakta_call_features.py` is present inside it, so a
non-editable install can call features. The worktree was left clean afterwards (`git
status --porcelain` shows only the untracked work record).

### Added — `tests/annotators/test_bakta_call_features.py` (1,989 lines, 102 tests)
Four groups, plus `TestIntegrationSkipGuard` and `TestDriverPackaging`:

1. **Fake driver** (`TestCallFeaturesParsing`, `TestCallFeaturesErrors`,
   `TestSplitCallFeatures`, `TestCompleteness`, `TestDockerCommandShape`) — a fake that
   reads the `--out` path out of the real argv it was handed and writes canned driver
   JSON there, so only Bakta is faked, not the interface. Covers parsing, the
   kept/discarded split, discard counts, the version block, and every error mapping.
2. **Stub-driver subprocess** (`TestDriverStub`) — the real driver run with a stub
   `bakta` package on `PYTHONPATH` that records every permitted call to a trace file and
   whose `ups.lookup`, `ips.lookup`, `psc.search/lookup`, `pscc.search/lookup`,
   `exp_amr.search`, `exp_aa_seq.search`, `exp_aa_hmms.search`, `cds.predict_pfam`,
   `cds.predict_pseudo_candidates`, `cds.detect_pseudogenes`,
   `annotation.combine_annotation`, `nc_rna_region.predict_nc_rna_regions` and
   `s_orf.extract` all raise. Asserts the exact 16-call order, that no `FORBIDDEN:`
   marker appears, the Bakta argv (including that `--complete`/`--compliant` are absent),
   and that a stub reporting `1.11.3` exits 3 having run nothing.
3. **Native end-to-end, no fakes** (`TestNativeDriverEndToEnd`) — `call_features`
   through its DEFAULT native runner path: it builds its own argv, really spawns the
   driver, really parses the result. Only Bakta is substituted, via the child's
   `PYTHONPATH`.
4. **Real image** (`TestRealImageIntegration`) — see `no_injection_check` below.

## confront_resolutions
- **R1 — crispr may carry `?`, nothing else may.** `_STRAND_UNKNOWN_TYPES = {"crispr"}`;
  `_build_kept_feature` raises `BaktaCallError` for `?` on any other type. Tested:
  `test_crispr_strand_question_mark_is_accepted`,
  `test_unknown_strand_on_a_non_crispr_feature_raises`, `test_strands_respect_r1`.
- **R2 — every CDS carries `start_complete`/`stop_complete`.** Derived by
  `_completeness` from Bakta's `truncated` marker, whose values are
  `5-prime`/`3-prime`/`both` (`bakta/constants.py:167-170`). Bakta already resolves the
  strand when it *sets* the marker (`bakta/features/cds.py:118-124`: pyrodigal's
  `partial_begin` → `5-prime` on `+`, `3-prime` on `-`), so the marker names the
  biological end and start/stop completeness read straight off it on either strand —
  which is what makes this strand-aware without a second flip. Tested:
  `TestCompleteness` (incl. `test_marker_is_already_strand_resolved`),
  `test_cds_carries_completeness_flags`,
  `test_every_cds_carries_completeness_flags` (real image).
- **R3 — `call_non_coding` echoed.** Field on `BaktaCallResult`. Tested:
  `test_call_non_coding_is_echoed_true`,
  `test_call_non_coding_is_echoed_false_and_passed_to_the_driver`,
  `test_call_non_coding_false_returns_cds_only` (real image).
- **R5 — separate discard accumulator; field-by-field JSON.** `_call_cdss` +
  `_feature_record`. Tested: `test_discarded_cds_never_reach_the_overlap_pass`,
  `test_filter_length_runs_before_antifam_on_the_full_set`,
  `test_output_is_built_field_by_field_not_dumped`. The last one is load-bearing: the
  stub's CDS carry a real `aa_digest` of `bytes`, so a driver that `json.dump`-ed Bakta's
  dicts would raise `TypeError` rather than merely emit an extra key.
- **R6 — `docker_image` constructor override, tested against ambient config.**
  `TestDockerImageOverride` constructs with `config={"bakta": {"docker_image":
  "ambient/from-config:9"}}` and asserts the explicit image wins, that an explicit `""`
  forces native, that config is used when nothing is explicit, that the argv and the
  recorded `versions["image"]` carry the explicit image and never the ambient one, and
  (`test_no_per_call_image_kwarg`) that `call_features`'s parameter set is exactly
  `{self, contigs, translation_table, threads, call_non_coding}`.
- **R8 — `image_id` and `driver_sha256`.** `_docker_image_id` uses
  `docker image inspect --format '{{.Id}}'` as R8 specifies and returns `"native"` in
  native mode; `_driver_sha256` is the sha256 of the driver file. Tested:
  `test_versions_carry_driver_block_plus_provenance`,
  `test_docker_image_id_is_native_in_native_mode`,
  `test_call_features_uses_the_instance_image_not_a_kwarg`,
  `test_versions_record_real_provenance` (real image, asserts
  `image_id.startswith("sha256:")`).

## success_criteria_check
The envelope gates on exactly these.

1. **`BaktaUtils.call_features` and `BaktaCallResult`/`BaktaCallError` exist with the
   documented signature.** PASS —
   `call_features(self, contigs: Sequence[tuple[str, str]], *, translation_table: int = 11,
   threads: int = 1, call_non_coding: bool = True) -> BaktaCallResult`, with the `image`
   kwarg dropped per R6. Both new types are also re-exported from
   `kbutillib.domains.genome.annotation` (`test_exported_from_annotation_package`).
2. **`bakta_call_features.py` is shipped as package data and imports only `bakta` and
   stdlib.** PASS — declared in `[tool.setuptools.package-data]`
   (`test_driver_is_declared_as_package_data`) and verified present inside a built wheel
   (see `tests_run` §5). `test_driver_imports_only_bakta_and_stdlib` AST-walks every
   `Import`/`ImportFrom` in the driver and asserts the module roots are a subset of
   `sys.stdlib_module_names | {"bakta", "__future__"}` and that `kbutillib` is not among
   them.
3. **The stub-driver test proves the structural call order and that no annotation
   function is called, and that a non-1.11.4 version exits 3.** PASS —
   `test_structural_call_order_is_exactly_main_pys` compares the recorded trace to the
   literal 16-call sequence; `test_no_annotation_function_is_called` asserts no
   `FORBIDDEN:` marker for any of 16 annotation entry points;
   `test_version_mismatch_exits_three_before_doing_anything` asserts returncode 3, both
   versions on stderr, an EMPTY trace and no output file. Both order and abstinence were
   falsification-checked (see `tests_run` §3).
4. **Fake-runner tests cover parse, discarded counts, contig-id mismatch and error
   mapping.** PASS — `TestCallFeaturesParsing` (18 tests) and `TestCallFeaturesErrors`
   (18 tests), covering parse of all six feature types, the three discard counts,
   `contig_ids` mismatch and subset, exit 3 (with and without a parseable version line),
   other non-zero exits with the stderr tail, unparseable JSON, a missing output file, a
   non-object payload, a missing `features` array, an unknown feature type, an unknown
   strand, and the four input `ValueError`s plus both `ToolUnavailableError` paths.
5. **The real-image integration test exists and is skipped when the image or
   `KBUTILLIB_TEST_BAKTA_DB` is absent.** PASS — `TestRealImageIntegration` carries two
   `skipif` marks. The DB-absent half is demonstrated (91 passed / 11 skipped); the
   image-absent half is covered by `TestIntegrationSkipGuard`, since the image IS present
   on this host and so that branch cannot be demonstrated here directly.
6. **The work record states whether the integration test ran on this host and its
   result.** PASS — see `no_injection_check` below: it ran, 11/11 passed.
7. **No test that passed on the base commit fails.** PASS — the 62 non-passing nodeids
   are node-ID-identical between base and branch source, measured in the same worktree
   with the same command (see `tests_run` §4).
8. **R1, R2, R3, R5, R6, R8 implemented and tested.** PASS — itemised above.

## no_injection_check
**Satisfied, and it actually ran on this host.**

`TestRealImageIntegration` (11 tests) calls `BaktaUtils.call_features` through its **default runner
path** — no fake runner, no monkeypatched invocation helper, no patched `subprocess`.
`call_features` builds its own argv, runs real Docker, runs real Bakta 1.11.4 against a
real database, and the assertions are on the returned `BaktaCallResult`.

- Image: `kbutillib/bakta:1.11.4`, id
  `sha256:6718c6d0a9412eb4bbf345c7512141722464f7d6acdb9639de18c19188837dff`
  (`docker image inspect kbutillib/bakta:1.11.4` exit 0).
- DB: `/scratch/kbdl/refdata/bakta-db-6.0-full` (`version.json`: major 6, minor 0,
  type `full`), supplied via `KBUTILLIB_TEST_BAKTA_DB`.
- Command:
  `TMPDIR=/scratch/chenry/tmp KBUTILLIB_TEST_BAKTA_DB=/scratch/kbdl/refdata/bakta-db-6.0-full PYTHONPATH=$PWD/src /home/chenry/venvs/kbdl/bin/python -m pytest tests/annotators/test_bakta_call_features.py -q -p no:cacheprovider --no-header`
- Result: **102 passed in ~10 s**, exit 0 — the 11 integration tests included. Without
  `KBUTILLIB_TEST_BAKTA_DB` the same command reports **91 passed, 11 skipped**, which is
  the behaviour the success criteria require when the image or the DB is absent.

Test input: a 4,801 bp slice of *E. coli* K-12 MG1655 `NC_000913.3` 3,422,400-3,427,200,
embedded in the test file with its NCBI RefSeq coordinates in a comment. It spans the
`yhdZ` CDS and the whole `rrnD` rRNA operon (minus strand): a three-tRNA cluster
(`thrV`, `alaU`, `ileU`) plus two complete 5S rRNA genes (`rrfF`, `rrfD`) and the 23S
(`rrlD`).

What the real run found, through `call_features`:

| type | kept | note |
|---|---|---|
| tRNA | 3 | `thrV` 1181-1256, `alaU` 4559-4634, `ileU` 4677-4753, all `-` |
| rRNA | 3 | two 5S (`RF00001`) + 23S (`RF02541`) |
| ncRNA | 1 | `RF01830` |
| cds | 1 | `yhdZ` **37-795 `+`, exactly the RefSeq coordinates** |

plus 1 CDS discarded `spurious` (AntiFam ANF00012). `versions` came back
`{"bakta": "1.11.4", "db": {"major": 6, "minor": 0, "type": "full"}, "image":
"kbutillib/bakta:1.11.4", "image_id": "sha256:6718c6d0…", "driver_sha256": "<64 hex>"}`.
Every kept feature's `nt` equals the input slice (reverse-complemented for `-` strand,
per `bu.extract_feature_sequence`), every CDS `product` is `None`, and no `psc`/`ips`/
`gene` key appears anywhere — i.e. no functional annotation leaked. `call_non_coding=False`
on the same input returns CDS only.

## tests_run

All runs used the repo's own pytest, the `kbdl` venv
(`/home/chenry/venvs/kbdl/bin/python`, CPython 3.11.15), `PYTHONPATH=$PWD/src` and
`TMPDIR=/scratch/chenry/tmp`, from inside this worktree.

### 1. The new file (with the real image and database)
```
TMPDIR=/scratch/chenry/tmp KBUTILLIB_TEST_BAKTA_DB=/scratch/kbdl/refdata/bakta-db-6.0-full \
PYTHONPATH=$PWD/src /home/chenry/venvs/kbdl/bin/python -m pytest \
  tests/annotators/test_bakta_call_features.py -q -p no:cacheprovider --no-header
```
**102 passed in ~10 s — exit 0.** Without `KBUTILLIB_TEST_BAKTA_DB`: **91 passed,
11 skipped — exit 0.**

### 2. The whole annotators suite
```
… -m pytest tests/annotators/ -q -p no:cacheprovider --no-header
```
**550 passed, 3 skipped — exit 0.** (Was 448 passed / 3 skipped before this task; the
+102 are the new file.)

### 3. Falsification checks (the tests are load-bearing, not vacuous)
Two deliberate probes were inserted into the driver, run, and reverted (md5 of the
reverted file verified identical to the pre-probe copy):

- Added `ups.lookup(cdss)` and `feat_cds.predict_pfam(cdss)` →
  `TestDriverStub::test_no_annotation_function_is_called` **FAILED** with
  `assert ('FORBIDDEN:' + 'ups.lookup') not in {...}`. Reverted.
- Added `data["features"].extend(discarded_cdss)` before
  `anno.detect_feature_overlaps` →
  `TestDriverStub::test_discarded_cds_never_reach_the_overlap_pass` **FAILED**
  (`Left contains 2 more items, first extra item: 2400`). Reverted.

### 4. Regression check — full suite, A/B in the SAME worktree
The committed baselines could not be used as-is. `build-baselines/kbutillib.json` is from
`0f84803` (2026-09-13) with a different command in a minimal venv;
`build-baselines/kbutillib-h100-main-6310d61.json` is from `6310d61` measured in
`/home/chenry/projects/KBUtilLib` and **explicitly documents** that a worktree run is not
comparable to a main-clone run (its `WORKTREE_MEASUREMENT_TRAP` note: a worktree yields
phantom errors in `tests/core/test_composition_smoke.py` because a fixture resolves a
data file relative to a non-existent sibling worktree). This run confirms that trap — the
worktree run takes **6-8 minutes**, not the main clone's 47, precisely because ~31
fixtures error at setup and short-circuit the slow tests.

So the A/B was taken in the same place, with the same command and interpreter, varying
only `PYTHONPATH` between the base-commit source and the branch source. The base source
was exported off-tree with `git archive d19d674 src | tar -x -C /scratch/chenry/bakta-base-src`,
so neither run was perturbed by the other.

| | command | result | wall |
|---|---|---|---|
| **base** `d19d674` | `PYTHONPATH=/scratch/chenry/bakta-base-src/src … -m pytest tests/ -q -p no:cacheprovider --no-header --ignore=tests/annotators/test_bakta_call_features.py` | 31 failed, 5843 passed, 283 skipped, 1 xfailed, 31 errors | 484 s |
| **branch** | `PYTHONPATH=$PWD/src … -m pytest tests/ -q -p no:cacheprovider --no-header` | 31 failed, 5924 passed, 294 skipped, 1 xfailed, 31 errors | 365 s |

**ZERO REGRESSIONS.** The 62 non-passing nodeids (31 failed + 31 errors) are
**node-ID-identical** between the two runs — `comm -13` over the sorted sets is empty in
both directions. Verified by name, not by count. The set is archived at
`/scratch/chenry/bakta-call-nonpassing-identical.txt` and breaks down as:

```
 26 ERROR  tests/biochem/test_escher_utils.py
 17 FAILED tests/cli/test_kind.py
  6 FAILED tests/domains/ai/council/test_types_config.py
  5 ERROR  tests/core/test_composition_smoke.py
  2 FAILED tests/cli/test_model_recording.py
  1 FAILED tests/modeling/test_lp_solver_solver_backends.py
  1 FAILED tests/domains/ai/llm/test_static_headers.py
  1 FAILED tests/core/test_composition_smoke.py
  1 FAILED tests/core/test_cli_cap.py
  1 FAILED tests/cli/test_kind_install_containment.py
  1 FAILED tests/biochem/test_registry_biochem.py
```

The count deltas are fully accounted for and are not regressions: passed 5843 → 5924
(**+81**) and skipped 283 → 294 (**+11**) are exactly the 92 tests the new file carried at
the time of that run (81 passing + 11 integration tests skipped, since the full-suite runs
did not set `KBUTILLIB_TEST_BAKTA_DB`). The base run had to `--ignore` the new file, which
imports `BaktaCallError` and cannot be collected against base source.

A separate, narrower A/B over the areas this diff can actually reach
(`tests/annotators` minus the new file, `tests/domains/test_genome_smoke.py`,
`tests/test_python_floor_syntax.py`, `tests/guard`) gave **470 passed, 3 skipped on both**
base and branch — identical.

One sub-case worth naming because it looks like a regression and is not: run
`tests/core/test_cli_cap.py` in a small selection and **3** tests fail
(`TestCapList::test_json_output_is_valid`, `…has_required_keys`, `…filter_by_tag`) because
an INFO log line (`KBBERDLUtils initialized …`) is emitted on stdout ahead of the JSON.
Confirmed pre-existing: the same 3 fail against the pristine base-commit source
(`PYTHONPATH=/scratch/chenry/bakta-base-src/src … -m pytest tests/core/test_cli_cap.py` →
3 failed, 34 passed). In the full run only 1 of the 3 fails, which is test-ordering
dependent and likewise identical on both sides.

### 5. Packaging
```
pip wheel --no-deps --no-build-isolation --no-cache-dir -w /scratch/chenry/bakta-wheel .
```
exit 0; `kbutillib/domains/genome/annotation/bakta_call_features.py` is present in the
resulting `kbutillib-1.0.0-py3-none-any.whl`. Worktree left clean.

## divergences_reconciled
The prompt was accurate about the hard facts (the `main.py` CDS block, the image
contents, `/opt/conda/bin` on PATH, the discard types). These are the places where the
tree or the shipped Bakta source differed from the prompt's wording:

1. **PRD fullprompt not reachable, as warned.**
   `agent-io/prds/kbdl-build-genome-bakta-calling-v1/fullprompt.md` does not exist on
   `wip` in this worktree (`git show wip:… → fatal: path … does not exist`). Worked from
   the envelope alone, which was sufficient.
2. **`main.py` line numbers.** The prompt cites "lines 228-360" for the CDS branch; in
   the shipped 1.11.4 `main.py` (633 lines) the branch runs **238-351** (`if(cfg.skip_cds)`
   at 238, `data['features'].extend(cdss)` at 351). R5's cited "247-263" for the
   length/spurious filters is likewise one or two lines off: `feat_cds.filter_length` is at
   248 and `orf.detect_spurious` at 254, so the filter region is 244-256. The claims the
   numbers carry are both correct — the branch really is unconditional once `--skip-cds` is
   off, and the two filters really do run before any lookup. Cited the real numbers.
3. **`anticodon` vs `anti_codon`.** Bakta's tRNA dict key is `anti_codon`
   (`bakta/features/t_rna.py:97`), not `anticodon`. The driver reads `anti_codon` and
   emits `anticodon` as the prompt's API spec requires. Asserted in
   `test_bakta_keys_are_renamed_for_output`.
4. **`overlap_start`/`overlap_stop` are not fields in Bakta.**
   `annotation.detect_feature_overlaps` emits `{type, feature_type, description}` and
   embeds the interval in the *description text* as `[start,stop]` (e.g.
   `annotation.py:236`). `_discard_record` recovers the two integers by pattern and
   emits `None` for discard types that have no interval (length/spurious). Documented at
   the regex and asserted in `test_overlap_interval_is_recovered_from_the_description`.
5. **`ncrna_class` is not a string in Bakta.**
   `nc_rna.determine_class` returns an `SO` namedtuple (`bakta/so.py`) or `None`, so it
   needs rendering before JSON. `_ncrna_class_name` emits `.name` (e.g. `"ribozyme"`).
   Asserted in `test_so_namedtuple_class_is_rendered_as_a_string`.
6. **`"cds"` is lowercase.** The prompt's type set `{"cds","tRNA",…}` matches
   `bakta.constants` exactly (`FEATURE_CDS = 'cds'`, `FEATURE_T_RNA = 'tRNA'`) — noting
   it because the asymmetry looks like a typo and is not.
7. **`cfg.setup` does not set `cfg.output_path`.** `main.py` calls
   `cfg.check_output_path(args.output, args.force)` separately, *before* `cfg.setup`, and
   sets `cfg.prefix` before that. The driver does the same. `check_output_path` also
   refuses a directory that already exists without `--force`, so the driver points
   `--output` at a not-yet-created child of a fresh `mkdtemp`.
8. **Per-call `image` kwarg dropped.** Section 2 of the prompt specifies
   `call_features(..., image: str | None = None)`; **R6** explicitly retracts it ("drop
   the per-call image kwarg"). R6 is binding and later, so `call_features` has no `image`
   parameter and `test_no_per_call_image_kwarg` locks that in.
9. **`docker_image_identity` already exists but is not what R8 asks for.**
   `annotator_utils.docker_image_identity` prefers a `RepoDigest` and returns a
   tag-qualified `<image>@<id>`. R8 names `docker image inspect --format '{{.Id}}'`
   specifically, so `_docker_image_id` is a separate method; its docstring records why it
   does not reuse the sibling helper.
10. **`bu.test_dependencies()` deliberately not mirrored.** `main.py` calls it right
    after `db.check`; it probes every binary Bakta might shell out to, including the
    AMRFinderPlus/Diamond stack this driver never uses. It is not in the prompt's
    mirror list and would be a new failure mode for no benefit.
11. **"Several kb from NC_000913.3 containing a tRNA cluster and a 5S rRNA."** The
    prompt's natural first choice, the `rrnB` operon, has only *one* tRNA (`gltT`).
    Used `rrnD` instead (3,422,400-3,427,200), which has a genuine three-tRNA cluster
    plus two 5S genes, and extended the window upstream to include a *complete* CDS
    (`yhdZ`) so there is a kept CDS to assert on — in the narrower window the only CDS
    Bakta predicts is discarded as spurious.

## caveats / not verified
- **`partial` on edge features.** The spec says `partial` comes "from Bakta
  truncated/edge markers". Implemented as `partial = truncated is not None`, because
  Bakta *pops* `truncated` and sets `edge = True` when it merges two partial CDS across a
  circular origin into one complete gene (`cds.py:162-163`) — an edge feature is
  complete, not partial. `edge` is carried through the driver's output for a consumer that
  wants it, but does not contribute to `partial`. This path is never exercised by
  `call_features` as written: it passes no `--complete` and writes no FASTA description, so
  every contig is `topology = linear` and the edge merge cannot fire. Not covered by a
  live test, only by reading `cds.py`.
- **`start <= stop` is asserted, not enforced.** An origin-spanning edge CDS on a circular
  replicon would legitimately have `start > stop`. `call_features` cannot produce one (see
  above), so the integration test asserts the invariant rather than the code raising on it
  — raising would be wrong for a caller who later enables circular input.
- **Only the `full` database type was exercised.** `db.check` on a `light` DB would
  change nothing in this path (the `db_info['type']` branches in `main.py` are all inside
  the annotation block the driver never enters), but it was not run.
- **tmRNA and CRISPR were not observed live.** The test region contains neither, so those
  two branches of the driver are exercised only by the stub-driver test (which does drive
  `tm_rna.predict_tm_rnas` and `crispr.predict_crispr`, including R1's `?` strand).
- **No ruff/black run.** Neither is installed in any interpreter on this host
  (`python -m ruff → No module named ruff`). Checked by hand instead: zero unused imports,
  zero lines over 88 characters in all three touched/added files, import grouping matching
  the repo's existing pattern.
- Nothing was deployed, no service venv was touched, no host was reached over ssh. The
  only external resource used was `eutils.ncbi.nlm.nih.gov` to fetch the reference
  sequence and its feature table, which is now embedded in the test file and is not
  fetched at test time.
