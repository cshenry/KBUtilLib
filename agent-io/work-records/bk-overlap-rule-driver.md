# Work record: bk-overlap-rule-driver

## task_id
bk-overlap-rule-driver

## prd
kbdl-build-genome-bakta-calling-v1 (dev 1631)

## branch
`maestro/developer/base-kbutillib-main-fb5dcf8-task-bk-overlap-rule-driver`

## commit_shas
- `8f01192` — `feat(bakta): keep RNA-overlapping CDS below a coverage fraction`
- (this work record is committed on top of it)

## base
`main` at `fb5dcf8c` — the commit named in the envelope. No rebase was needed.

## decision implemented
Chris, 2026-10-22, verbatim: *"Dont' cut small overlapping genes. This causes bugs."*

## summary
Bakta 1.11.4's `anno.detect_feature_overlaps` discards a CDS for an overlap of **any**
size with a tmRNA/tRNA/rRNA/CRISPR feature — one shared base is enough. Read out of the
image to be sure (`docker run kbutillib/bakta:1.11.4 … inspect.getsource(
bakta.features.annotation.detect_feature_overlaps)`), the CDS branch is four independent
loops (tmRNA, tRNA, rRNA, CRISPR) each of which sets `cds['discarded']` on any
intersection, plus a fifth for user-provided CDS.

The driver now re-judges that pass's CDS discards in a new step,
`_reconcile_cds_overlaps`, which runs immediately **after** Bakta's pass:

- A CDS stays discarded for overlap only when the overlapping non-coding features cover
  **≥ `CDS_OVERLAP_DISCARD_FRACTION` (0.5)** of its nucleotide length, measured as the
  **union** of covered positions on the same contig, either strand. Its discard record
  keeps `reason = 'overlap'` and gains `overlap_fraction`.
- Below the threshold the CDS is **kept**, carrying
  `rna_overlap = {feature_type, overlap_start, overlap_stop, overlap_bp, overlap_fraction}`
  for its **largest single** overlap.
- Bakta is neither forked nor monkeypatched. Non-CDS discards (a tRNA dropped for a tmRNA
  overlap, an ncRNA-region dropped for a better-scoring one) are left exactly as Bakta
  left them, and the length and spurious/AntiFam CDS discards — which never reach the
  overlap pass at all, see `_call_cdss` — are untouched.

Threaded through as a driver flag `--cds-overlap-discard-fraction` (default 0.5), a
`call_features(..., cds_overlap_discard_fraction=0.5)` kwarg, and a
`BaktaCallResult.cds_overlap_discard_fraction` echo so provenance records the threshold a
run actually used.

## files_touched

### Modified — `src/kbutillib/domains/genome/annotation/bakta_call_features.py` (+236 lines)
- `CDS_OVERLAP_DISCARD_FRACTION = 0.5` (`:140`) — the module constant the task asked for.
- `_CDS_OVERLAP_FEATURE_TYPES = ("tmRNA", "tRNA", "rRNA", "crispr")` (`:151`) — the
  feature types Bakta's CDS pass *actually* tests against. See
  **divergences_reconciled** for why `ncRNA` is deliberately not in it.
- `--cds-overlap-discard-fraction` in `_parse_driver_args` (`:168`), default
  `CDS_OVERLAP_DISCARD_FRACTION`.
- `_overlap_interval` (`:444`) — intersect two 1-based inclusive intervals.
- `_cds_rna_overlaps` (`:464`) — every non-coding overlap of one CDS, as
  `{feature_type, overlap_start, overlap_stop, overlap_bp}`. **Recomputed from the feature
  list, not parsed back out of Bakta's discard text**: Bakta rewrites `cds['discarded']`
  once per overlapping feature, so the surviving `[start,stop]` in the description names
  only the LAST overlap found — neither the largest nor the union.
- `_covered_bp` (`:508`) — union (not sum) of the overlap intervals. Two non-coding
  features can overlap each other as well as the CDS (a tRNA inside an rRNA operon), and
  summing would double-count shared bases.
- `_reconcile_cds_overlaps` (`:537`) — the rule. Only touches features with
  `type == 'cds'` **and** a `discarded` block of `type == 'overlap'` **and** a
  `feature_type` in `_CDS_OVERLAP_FEATURE_TYPES`.
- Called from `_run` (`:711`) on the line immediately after
  `anno.detect_feature_overlaps(data)`.
- `_feature_record` (`:371`) emits `rna_overlap` when present; `_discard_record` (`:331`)
  carries `overlap_fraction` (`None` for a length/spurious discard).
- Module docstring gained a `CDS OVERLAP RULE (this driver's own, NOT Bakta's)` section
  and the new flag in `CLI`; the `STRUCTURAL CALL ORDER` chain now ends
  `… -> anno.detect_feature_overlaps -> (this driver's own) _reconcile_cds_overlaps`.

### Modified — `src/kbutillib/domains/genome/annotation/bakta_utils.py` (+78 lines)
- `_DRIVER_CDS_OVERLAP_DISCARD_FRACTION = 0.5` (`:199`) — mirrors the driver constant,
  duplicated rather than imported for the same reason the version pin is: importing the
  driver would pull in `bakta`.
- `BaktaCallResult.cds_overlap_discard_fraction` (`:287`), defaulted so an external
  constructor of the dataclass does not break.
- `_build_kept_feature` (`:549`) passes `rna_overlap` through on CDS.
- `_build_discarded_cds` (`:579`) adds `overlap_fraction` to the public discarded-CDS dict.
- `_build_call_features_cmd` (`:1123`) takes `cds_overlap_discard_fraction` and emits
  `--cds-overlap-discard-fraction <f>`.
- `call_features` (`:1218`) takes the kwarg (`:1225`), validates it is in `[0.0, 1.0]`
  (`:1282`, `ValueError` — matching the method's existing input-validation convention),
  passes it to the argv builder and echoes it on the result.
- Module docstring records the one behaviour that is deliberately not Bakta's.

### Modified — `tests/annotators/test_bakta_call_features.py` (+566 lines, 102 → 127 tests)
- `_write_stub(root, appends=…)` and `_run_driver_against_stub(..., appends=…)` gained a
  source-**overlay** mechanism: an entry is *appended* to a base stub file, so a `def` in
  it shadows the base one of the same name without re-declaring the `bakta.forbidden`
  entry points that sit in the same files. Raises `KeyError` on a path the base stub does
  not define, so a typo cannot silently do nothing.
- `_OVERLAP_RULE_STUB_APPENDS` (`:1315`) — the overlap-rule geometry, and a replacement
  `annotation.detect_feature_overlaps` that **reproduces Bakta's real CDS rule** (any
  overlap discards; tmRNA → tRNA → rRNA → CRISPR, last match winning). So each case starts
  from the verdict real Bakta would reach, not from a canned one.
- `TestDriverStubOverlapRule` (`:1817`, 12 tests) — driver in a real subprocess:
  3 bp tail overlap kept with `rna_overlap`; 100%-inside-an-rRNA discarded; 15% kept;
  69% discarded; two 30% overlaps unioned to 60% and discarded; no-overlap CDS untouched;
  CDS inside an ncRNA gene never discarded; spurious and length discards untouched;
  non-CDS (tRNA-for-tmRNA) overlap discard untouched; `--cds-overlap-discard-fraction 0.1`
  flips the 15% case; `0` reproduces Bakta's own behaviour.
- `TestCallFeaturesOverlapRule` (`:2034`, 9 tests) — the same through `call_features`'s
  DEFAULT native runner (`_run_driver` NOT replaced): `rna_overlap` on the kept CDS,
  `overlap_fraction` on the discarded one, the threshold echo, the kwarg flipping the 15%
  case end to end, the flag on the argv, and `ValueError` out of range.
- `TestDriverPackaging.test_driver_overlap_fraction_matches_the_adapters_default` — the
  two constants cannot drift, mirroring the existing version-pin test.
- `_SCO_TRNA_OVERLAP_REGION` (`:2306`) — a 1,001 bp slice of *S. coelicolor* A3(2)
  `NC_003888.3:4,498,100-4,499,100`, fetched over HTTPS from NCBI efetch and embedded as a
  literal with its coordinates in the comment, exactly as the existing E. coli slice is.
  Local coordinates (local = absolute − 4,498,099), verified against the RefSeq annotation
  of `NC_003888.3:4,497,000-4,500,200`: `SC_RS22650` tRNA-Lys 50-122 (+), `SC_RS22655`
  CDS 120-809 (−), overlapping by exactly 3 bp (120, 121, 122) at the CDS's 3' end.
- Three tests in `TestRealImageIntegration` on that slice (`:2493`): the CDS is kept with
  the right `rna_overlap`, the tRNA is still called too, and the threshold is echoed.
- Three pre-existing call sites updated for the new required argument /
  parameter-set assertion: `TestDockerImageOverride.test_no_per_call_image_kwarg` (the
  expected `call_features` parameter set now includes `cds_overlap_discard_fraction`) and
  the two `_build_call_features_cmd` callers in `TestDockerCommandShape`. No assertion was
  weakened.

## no_injection_check
**The real-image S. coelicolor case runs through `call_features`'s DEFAULT runner — real
Docker, real Bakta 1.11.4, the real database — on this host, and SC_RS22655 is kept.**

```
TMPDIR=/scratch/chenry/tmp KBUTILLIB_TEST_BAKTA_DB=/scratch/kbdl/refdata/bakta-db-6.0-full \
PYTHONPATH=$PWD/src /home/chenry/venvs/kbdl/bin/python -m pytest \
  "tests/annotators/test_bakta_call_features.py::TestRealImageIntegration" -v
=> 14 passed in 10.04s   (exit 0)
```

Nothing is faked in that class: no fake `_run_driver`, no monkeypatched helper.
`call_features` builds its own Docker argv and runs
`kbutillib/bakta:1.11.4` (image id `sha256:6718c6d0a941…`) with `--network none` and the
database mounted read-only at `/db`. The same run, instrumented directly, reports:

```
image: kbutillib/bakta:1.11.4 sha256:6718c6d0a941
bakta: 1.11.4  db: {'major': 6, 'minor': 0, 'type': 'full'}
threshold echo: 0.5
 KEPT tRNA 50 122 + Lys
 KEPT cds  120 809 -  rna_overlap={'feature_type': 'tRNA', 'overlap_start': 120,
                                   'overlap_stop': 122, 'overlap_bp': 3,
                                   'overlap_fraction': 0.004347826086956522}
 discarded: {"counts": {"overlap": 0, "spurious": 0, "length": 0}, "cds": []}
```

**Before/after on the same live path.** The identical probe run against the BASE code
(`fb5dcf8c`, before the rule) on the same slice, same image, same database:

```
KEPT: tRNA 50 122 +
DISCARDED: {"counts": {"overlap": 1, …},
            "cds": [{"start": 120, "stop": 809, "strand": "-", "reason": "overlap",
                     "overlap_type": "tRNA", "overlap_start": 120,
                     "overlap_stop": 122}]}
```

So Bakta really does discard this exact RefSeq gene over 3 bp, and the rule really does
recover it. Bakta's own call is otherwise unchanged: the CDS comes back at local
120..809 (−), i.e. `NC_003888.3:4,498,219..4,498,908(−)` = SC_RS22655's RefSeq
coordinates, and the tRNA-Lys it overlaps is still called at 50..122.

## success_criteria_check

| Criterion | Status |
|---|---|
| Rule implemented as specified, `CDS_OVERLAP_DISCARD_FRACTION = 0.5` | PASS — `bakta_call_features.py:140`, `_reconcile_cds_overlaps:537` |
| Driver flag `--cds-overlap-discard-fraction` (default 0.5) | PASS — `_parse_driver_args:168` |
| `call_features(..., cds_overlap_discard_fraction=0.5)` kwarg | PASS — `bakta_utils.py:1225` |
| `BaktaCallResult` echo | PASS — `bakta_utils.py:287`, set at `:1352` |
| `rna_overlap` on kept CDS | PASS — driver `_feature_record:371`, adapter `_build_kept_feature:549` |
| Implemented AFTER `anno.detect_feature_overlaps`, no fork/monkeypatch, non-CDS features unchanged | PASS — single call site in `_run` on the line after Bakta's pass; the loop skips every non-CDS feature |
| Still-discarded CDS keep reason `'overlap'` and gain `overlap_fraction` | PASS — `_reconcile_cds_overlaps` mutates the existing block in place |
| Stub tests: 3 bp keep, 100% discard, 15% keep, 69% discard, spurious untouched, threshold kwarg | PASS — `TestDriverStubOverlapRule` (12 tests) + `TestCallFeaturesOverlapRule` (9) |
| NO-INJECTION real-image S. coelicolor case, SC_RS22655 kept, stated in this record | PASS — see **no_injection_check** |
| No test that passed on the base commit fails | PASS — see **tests_run** §3 |

## tests_run
All commands were run in the FOREGROUND; the interpreter is the kbdl venv
(`/home/chenry/venvs/kbdl/bin/python`, cpython-3.11.15, pytest 9.1.1).

### 1. The changed module, offline (no DB env var → the real-image class skips)
```
TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src python -m pytest \
  tests/annotators/test_bakta_call_features.py -q -p no:cacheprovider --no-header
=> 113 passed, 14 skipped in 4.18s   (exit 0)
```

### 2. The real-image class (DB env var set) — the no-injection check
```
=> 14 passed in 10.04s   (exit 0)      # command quoted under no_injection_check
```

### 3. Regression A/B, by NODEID, same selection on both sides
`build-baselines/kbutillib.json` exists and was read; per
`build-baselines/kbutillib-h100-main-6310d61.json` the full suite takes **45–57 min** on
this host (5–6× the 600 s tool ceiling), so the full suite was NOT re-run — a targeted
selection was A/B'd instead. The selection is every test file that touches Bakta or the
genome-annotation package (`grep -rln "bakta\|genome.annotation\|annotator_utils" tests/`):

```
SEL="tests/annotators tests/domains/test_genome_smoke.py tests/guard \
     tests/cli/test_registry_reader.py tests/core/test_deprecation_shims.py \
     tests/external/test_kbdl_service_utils.py"
```

| Side | Where | Result |
|---|---|---|
| **A — base `fb5dcf8c`** | a detached worktree at `/scratch/chenry/base-ab-bk-overlap` (created with `git worktree add --detach`, removed afterwards) | **715 passed, 14 skipped, 0 failed** (6.89 s, exit 0) |
| **B — this branch** | this worktree | **737 passed, 17 skipped, 0 failed** (7.62 s, exit 0) |

**Zero failures on either side, so zero regressions by nodeid.** The delta is additive:
+22 passed (the new offline tests) and +3 skipped (the three new real-image tests, which
skip without `KBUTILLIB_TEST_BAKTA_DB`). No test was deleted or renamed. KBUtilLib
issue 83's `test_static_headers` is not in this selection.

### 4. Falsification — the new tests are load-bearing
With the single line `_reconcile_cds_overlaps(data, args.cds_overlap_discard_fraction)`
replaced by `pass`, the module run gives **9 failed, 104 passed, 14 skipped**: all six
threshold cases in `TestDriverStubOverlapRule` and all three behavioural ones in
`TestCallFeaturesOverlapRule`. The driver was restored from the pre-edit copy and
re-verified green before committing. The union test is specifically load-bearing: with a
largest-single-overlap test instead of a union, the 30% + 30% CDS would be kept and
`test_two_small_overlaps_are_unioned_not_taken_singly` would fail.

## divergences_reconciled

1. **The prompt says Bakta's pass discards a CDS for any overlap with a
   "tRNA/tmRNA/rRNA/ncRNA/CRISPR" feature. `ncRNA` is wrong — and including it would have
   been a behaviour change.** Read out of the image, Bakta 1.11.4's
   `detect_feature_overlaps` tests a CDS against tmRNA, tRNA, rRNA, CRISPR and
   user-provided CDS only; its only ncRNA clause is ncRNA-**region** vs ncRNA-region
   (and `--skip-ncrna-region` means this driver never predicts those at all). So no CDS
   is ever discarded for an ncRNA-**gene** overlap. Folding ncRNA coverage into the
   fraction would therefore have let a feature type Bakta ignores tip a CDS into a
   *discard* — the exact opposite of the decision being implemented. **Resolution:**
   `_CDS_OVERLAP_FEATURE_TYPES` is `("tmRNA", "tRNA", "rRNA", "crispr")`, documented at
   the constant, and `TestDriverStubOverlapRule.test_a_cds_inside_an_ncrna_gene_is_never_discarded`
   pins the behaviour (a CDS wholly inside an ncRNA gene is kept, with no `rna_overlap`,
   because it never reached the rule).

2. **`BaktaUtils.call_features` is at `bakta_utils.py:1218`, not ~1178.** Located by
   symbol, as instructed. `_build_call_features_cmd` is at `:1123`.

3. **`overlap_fraction`'s meaning inside `rna_overlap` was under-specified.** The task
   says the rule decides on the *union* of covered positions but that `rna_overlap`
   describes *the largest* overlap. Those differ when a CDS has two or more overlaps.
   **Resolution:** `feature_type`/`overlap_start`/`overlap_stop`/`overlap_bp` describe the
   largest single overlap; `overlap_fraction` is the union fraction — i.e. the one number
   the rule actually compared against the threshold — so it means the same thing on a kept
   CDS and on a discarded one. With a single overlapping feature, which is every case seen
   in acceptance, the two readings coincide (`overlap_bp / length`). Documented in
   `_reconcile_cds_overlaps`'s docstring.

4. **The stub `detect_feature_overlaps` only discarded a tRNA, never a CDS**, so there was
   nothing for the rule to re-judge. Rather than mutate the base stub (which five existing
   tests assert exact counts and coordinates against), `_write_stub` gained an
   append-overlay and the overlap-rule classes supply their own geometry plus a faithful
   re-implementation of Bakta's CDS rule. No existing stub expectation changed.

5. **Not a divergence, but worth recording: a 1,001 bp slice was enough.** The prompt
   suggested building the fixture "around 4498100..4499100"; that window was tried first
   and Bakta calls SC_RS22655 at exactly its RefSeq coordinates on it, so it was kept as
   is and no widening was needed.

## caveats / not verified
- **SC_RS16190 (the 10 bp case) was not reproduced live.** Only SC_RS22655 is covered by a
  real-image test, which is what the task asked for. The 10 bp case is the same shape at
  0.016 of a 615 bp CDS, far below any plausible threshold, and the stub covers the
  geometry.
- **`ruff`/`mypy` were not run — neither is installed in the kbdl venv** (`No module named
  ruff`). Lines were kept within 88 characters by hand and `ast.parse` was run on all
  three files; `E501` is ignored in `pyproject.toml` anyway.
- **`cds_overlap_discard_fraction` is echoed from the kwarg, not read back out of the
  driver's JSON**, exactly as `call_non_coding` is. The driver's payload does not carry the
  threshold. If provenance ever needs to be defensible against a driver that ignored the
  flag, that echo is the weak link — but the flag is on the argv (asserted) and the live
  run above shows it took effect.
- **The full test suite was not run** (45–57 min on this host, per
  `build-baselines/kbutillib-h100-main-6310d61.json`). The regression claim is scoped to
  the selection in §3, which is every file that imports or mentions the changed package.
