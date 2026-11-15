# kbu-fva-panel — work record

**Task:** `task-924f89ea` · **Role:** developer · **Repo:** KBUtilLib
**Branch:** `maestro/developer/kbdl-recon-kbu-fva-panel`
**Base:** `b5823f0` (`docs: work record for kbu-recon-gapfill`)
**Date:** 2026-11-15
**PRD:** kbdl-recon-thin-layer-v1, review round 1 — part of the 8-task programme

Port the FVA panel KBDL reaches through GAA into KBUtilLib, so the recon job
needs no `genome_annotation_aggregator` for FVA or essentiality.

---

## What landed

| Commit | Contents |
|---|---|
| `6178135` | `feat(fva)`: the layer + 43 unit tests on fakes |
| `ba24458` | `test(fva)`: real-dependency parity, fixtures, the upstream `ObjectivePkg` fix, +7 unit tests |

### Code

`src/kbutillib/domains/modeling/fva_panel.py` — a NEW module, as the task
directed, rather than growing `ms_fba_utils.py` (which is now 2600+ lines).

* `MediaPanel` (`fva_panel.py:109`) — `complete` / `gapfill_media` / `fva_media`,
  field-for-field GAA's `model_build.dataclasses.MediaPanel`, so KBDL's
  `gaa_infra.make_media_panel` swaps one import and changes nothing else.
* `run_fva_panel(mdlutl, panel, *, fraction_of_optimum, fba, growth_out)`
  (`fva_panel.py:198`) — per-medium growth gate, then FVA on the growing media.
* `classify_fva_essentiality(fva_min, fva_max)` (`fva_panel.py:139`) and
  `compute_fva_essentiality(...)` (`fva_panel.py:172`) — GAA's
  `schema.compute_model_fva_essentiality` and the `_classify_essentiality` rule
  behind it.
* `_ensure_objective_package` (`fva_panel.py:382`) — the upstream compatibility
  fix described under **The real finding** below.
* `src/kbutillib/domains/modeling/__init__.py` — four additive lazy exports.

No `genome_annotation_aggregator` import; asserted on the AST, at any depth.
Every call into `MSFBAUtils` uses keyword arguments, and the test fakes'
`run_fba`/`run_fva` are keyword-ONLY so a positional call fails loudly rather
than binding to the wrong parameter against a different modelseedpy build.

### Tests

| File | Count | Kind |
|---|---|---|
| `tests/modeling/test_fva_panel.py` | 50 | fakes, no solver |
| `tests/modeling/test_fva_panel_real_dependency.py` | 17 | real solves, parity vs GAA |

### Fixtures

| Path | Size | What |
|---|---|---|
| `tests/fixtures/model/haemophilus_influenzae_71421_8_rast_gapfilled_model.json.gz` | 70 KB | the shared parity model |
| `tests/fixtures/model/fva_panel_gaa_expected.json.gz` | 29 KB | GAA's captured output + provenance |
| `tests/fixtures/model/fva_panel_media_spec.json` | 530 B | the two media, so both sides build them identically |

---

## Which parity route, and why

**The fixture route.** GAA pins `modelseedpy==0.4.2` (the cshenry fork) while
this layer targets upstream dev; both report the same version string with
different `MSBuilder.build` signatures, so they cannot share an environment.
MEASURED: `import genome_annotation_aggregator` raises `ImportError` in
`/scratch/chenry/venvs/kbdl-recon-scratch-env`, and the parity module asserts
that rather than assuming it — if GAA ever does become importable alongside
upstream, that test fails and the honest response is to compare live instead of
trusting a capture.

GAA's output was captured ONCE in the production venv `/scratch/chenry/venvs/kbdl`.
**Read-only use, as the task required:** a script was run with that venv's
interpreter; nothing was installed, nothing was modified.

### What makes it parity and not two unrelated measurements

Two things were verified BEFORE the capture, and they are what let any
difference in output be attributed to the orchestration:

1. **The model is identical by construction.** Built ONCE through part B's
   native role-based path (`build_metabolic_model`, `atp_safe=True`) and
   gap-filled on both media, then serialised to cobra JSON. Both sides load
   that exact file. It is CAPTURED rather than rebuilt per run because ATP
   correction and gap-fill both choose among tied LP optima — a rebuilt model
   would not be bit-identical, and then a numeric disagreement could never be
   attributed to the FVA layer rather than to the build. Recording which, as
   asked: **a model built from the H. influenzae genome fixture by part B's
   build path**, not the genome fixture used directly (that fixture is a RAST
   genome, not a model).
2. **The solver core is identical.** `domains/modeling/ms_fba_utils.py` is
   **byte-identical** (`diff`, exit 0) between this worktree and the kbutillib
   installed in the production venv (`/scratch/fliu/hub_home/chenry/H100Projects/KBUtilLib`,
   `9b154da`). Both panels therefore call the same `run_fva`, and the test
   isolates the panel orchestration — gate, per-medium isolation,
   fraction-of-optimum default, result shaping — which is exactly what was ported.

Also verified: the production GAA checkout is at **`58f20fa`**, the SHA the task
cited, and its `fva_runner.py` and `_classify_essentiality` are byte-identical
to the copies I read.

---

## Declared tolerances

| Quantity | Rule | Worst observed |
|---|---|---|
| FVA endpoints (MIN, MAX) | `abs(a-b) <= max(1e-6, 1e-6 * max(abs(a),abs(b)))` | **5.09e-10** |
| Growth (biomass flux) | `abs(a-b) <= max(1e-9, 1e-6 * max(abs(a),abs(b)))` | ~1e-14 |
| Essentiality class | **exact**, no tolerance | 0 mismatches |
| `is_essential` / `is_blocked` | **exact** | 0 mismatches |
| `n_essential` / `n_blocked` per medium | **exact** | 0 mismatches |

Two-term tolerances because fluxes on this model span zero to several hundred: a
single absolute tolerance would be meaningless at the top of the range or
unsatisfiable at the bottom. The class is discrete and the eps rule was ported
verbatim, so it gets no tolerance at all.

---

## Measured results

Model: 1093 reactions, 588 genes. Solver **glpk**, confirmed by real solve
(issue #35 — Gurobi's expired licence is not needed and was not used).
ModelSEEDDatabase resolved to `/home/chenry/Dropbox/Projects/ModelSEEDDatabase`
through `~/.kbutillib/dependencies.yaml`, never modelseedpy's
`/deps/ModelSEEDDatabase` default (issue #41).

| | glucose minimal | succinate minimal |
|---|---|---|
| Growth (mine) | 0.6805372885225851 | 0.4384625430571134 |
| Growth (GAA) | 0.6805372885225724 | 0.4384625430571128 |
| FVA rows | 1093 | 1093 |
| `n_essential` mine / GAA | 418 / **418** | 415 / **415** |
| `n_blocked` mine / GAA | 570 / **570** | 577 / **577** |

* **4372** endpoints compared, **1951** of them non-zero — pinned in the test so
  an all-zero panel cannot satisfy the tolerance while measuring nothing.
* **2186** essentiality classifications, **0** mismatches.
* All **four** classes observed (`blocked`, `essential-forward`,
  `essential-reverse`, `functional`), so every branch of the eps rule is
  exercised rather than just the common one.
* **Both media grow**, so no per-reaction assertion passes vacuously on an
  empty medium.

### Real runs used (against the task's cap of three)

| # | What | Wall time | Outcome |
|---|---|---|---|
| — | model build + gap-fill on 2 media (scratch venv) | 59.0 s + 62.0 s | model captured; one earlier attempt was lost to my own script bug (see below) |
| — | GAA capture (production venv, read-only) | 38.9 s | fixture captured |
| 1 | parity test | 28.6 s | **FAILED** — 0 FVA rows; found the `ObjectivePkg` divergence |
| 2 | parity test | 59.8 s | **17 passed** |

Two parity runs of the three allowed; one spare unused. Panel itself: 33.6 s for
two media over 1093 reactions (≈4372 LP solves).

Two self-inflicted costs, recorded honestly rather than quietly: the first build
attempt reached the end of gap-fill and then died unpacking the return tuple —
I omitted `return_gapfill_records=True`, so the 5th element was never returned
and `res[-1]` was an `MSMedia`. ~2 minutes of build lost. The rebuild writes the
model to disk *before* anything else can fail. The first parity run was spent on
the real finding below, which is a cost I would spend again.

---

## The real finding: `ObjectivePkg` is not resolvable on upstream

Parity run 1 returned **zero rows for both media** while growth gating worked
perfectly. That was not a test bug.

`run_fva` sets a per-reaction objective through `set_objective_from_string`,
which calls `model.pkgmgr.getpkg("ObjectivePkg")`. `MSPackageManager.__init__`
builds its `available_packages` registry from
`inspect.getmembers(sys.modules["modelseedpy"])` — the **top-level** namespace.
MEASURED on both interpreters:

| | `modelseedpy.fbapkg.ObjectivePkg` | top-level `modelseedpy.ObjectivePkg` |
|---|---|---|
| fork 0.4.2 (production) | present | **present** |
| upstream dev 0.4.3 `e1155a4` (scratch) | present | **ABSENT** |

So on upstream the registry lacks the entry and `getpkg` raises
`PackageError("Package ObjectivePkg does not exist!")` for *every* reaction,
failing an entire medium's FVA — while the identical code works on the fork.
This is a defect in the **pre-existing** `run_fva` path against the kbdl-target
modelseedpy, not in the ported panel; the panel's per-medium `except` merely
turned it into an empty result.

**Fixed in `_ensure_objective_package`, the narrowest option available:** it
registers the class on that model's own per-model `available_packages` dict.
`MSPackageManager` keeps one manager per model, so this patches no global state,
does not monkeypatch modelseedpy, and — importantly — does **not** modify
`ms_fba_utils.py`, whose byte-identity with the production venv's copy is what
the whole parity argument rests on. It is a no-op on the fork.

**Flagged for the programme:** any other caller of
`MSFBAUtils.set_objective_from_string` or `run_fva` hits the same wall on
upstream dev. A general fix belongs in `ms_fba_utils.py` or in the upstream
`kbdl-target` branch's `__init__.py` re-exports, and is outside this task's
scope — I deliberately did not widen into it.

---

## Deliberate behaviour divergences from GAA

Both are inside what the task authorised dropping ("drop GAA batch-pipeline
concerns … process pools for 21 media are optional — keep a sequential path").

1. **Sequential, so no preemptive per-medium timeout.** GAA ran media in a
   `ProcessPoolExecutor` with a 120 s budget each, recording `{"timeout": True}`
   for a medium that blew it so the batch continued. A solve already inside the
   solver's C code cannot be interrupted from the calling thread, and a
   thread-based "timeout" would record a result while the solve kept running —
   worse than waiting. **The cost is explicit: a pathological medium blocks the
   panel here where GAA abandoned it.** `TIMEOUT_SENTINEL` is still exported and
   documented because consumers (KBDL's `_shape_fva_result`) already branch on
   it, but this module never emits it. If the 21-medium batch case ever needs
   the timeout back, it needs the process pool back with it.
2. **No `SharedEnvUtils.__init__` monkeypatch in `_make_fba`.** GAA patched it to
   mirror the global dependency map into each instance because it had no
   supported way to make the ModelSEED biochem path visible to a freshly
   constructed utility. In this repo that is part A's
   `initialize_reconstruction_environment`'s job, so the patch would duplicate a
   supported path with a monkeypatch. `_make_fba` instead constructs
   `MSFBAUtils(config_file=False, token_file=None, kbase_token_file=None)` —
   offline, no KBase call, which is what a CTS job needs.

Also dropped, as instructed: store/parquet writes, the fixed 21-medium panel,
and the batch-pipeline `BuildReport` plumbing.

---

## Verification

```
# unit, on fakes
PYTHONPATH=$PWD/src /scratch/chenry/venvs/kbdl-recon-scratch-env/bin/python \
    -m pytest tests/modeling/test_fva_panel.py -q
→ 50 passed                                                        exit 0

# real-dependency parity
KBU_REQUIRE_MODEL_STACK=1 PYTHONPATH=$PWD/src \
/scratch/chenry/venvs/kbdl-recon-scratch-env/bin/python \
    -m pytest tests/modeling/test_fva_panel_real_dependency.py -q
→ 17 passed in 59.84s                                              exit 0
```

### Regression check — by NAME, against the base commit

The cached `build-baselines/kbutillib.json` (base `0f84803`, captured
2026-09-13 in a minimal venv) `--ignore`d both files that fail here, so it
carries **no name** for them and could not settle whether they were mine. I
therefore established the base state directly: a **detached** worktree at
`b5823f0` (no branch created, removed afterwards), same venv, same targeted set.

```
pytest tests/modeling/ tests/domains/test_modeling_smoke.py \
       tests/core/test_composition_smoke.py tests/core/test_deprecation_shims.py \
       (real-dependency modules ignored)

base b5823f0 : 12 failing nodeids
this branch  : 12 failing nodeids
regressions  : 0        newly fixed : 0        (set difference, both directions)
```

The 12 pre-existing failures, unchanged by this work:

* 4 × `tests/modeling/test_comprehensive_gapfill_wrapper.py` —
  `MSGapfill.__init__() got an unexpected keyword argument 'base_media'`, a
  fork/upstream signature divergence.
* 8 × `tests/core/test_composition_smoke.py` — ModelSEEDDatabase not loadable
  from `/tmp/ModelSEEDDatabase`, plus escher; environment, not product.

Branch total on the targeted set: **516 passed, 4 failed, 8 errors, 35 skipped,
1 xfailed**. Per the standing instruction, this is NOT phrased as "green" — the
bar met is that no nodeid passing on `b5823f0` fails on this branch.

I did **not** run the full suite, per the task's budget rule.

---

## Measured vs inferred

**Measured** (I ran it and read the number):

* Every number in the results and tolerance tables above.
* The `ObjectivePkg` top-level-export asymmetry, on both interpreters.
* `ms_fba_utils.py` byte-identity between worktree and production venv (`diff`).
* Production GAA at `58f20fa`; its `fva_runner.py` and `_classify_essentiality`
  byte-identical to the copies I read.
* GAA not importable in the scratch venv.
* The 12-nodeid base-vs-branch failure sets.
* `fva_panel` imports, and `MediaPanel` constructs, with **no** cobra or
  modelseedpy installed (run in the task venv, which has neither).
* glpk works by real solve; Gurobi never involved.

**Inferred** (reasoned, not executed):

* That KBDL's `_shape_fva_result` will consume this unchanged. The shapes it
  reads are reproduced exactly — `{media_id: {rxn_id: {"MIN","MAX"}}}`, `{}` for
  a non-growing medium, the timeout sentinel recognised, and
  `compute_fva_essentiality`'s three keys — and the essentiality numbers it
  derives (`n_essential`, `n_blocked`, `class`) match GAA exactly on this model.
  But I did not run KBDL itself: KBDL is a separate repo and switching its
  import is a different task in this programme.
* That the sequential path's lack of a timeout is acceptable for the CTS
  single-model case. Reasoned from the task's explicit authorisation, not
  measured against a pathological medium.
* That a medium that fails for a non-growth reason is adequately served by `{}`
  plus an ERROR log. Ported from GAA, which does the same; I did not survey
  consumers for one that needs to distinguish the two.
* Parity on media with **no** growth is untested by real solves — both media on
  this model grow. That branch is covered on fakes only.
* Parity was established on ONE model and TWO media, per the budget rule. It is
  not evidence about GAA's full 21-medium panel or about models whose FVA is
  degenerate enough for tied optima to diverge beyond the declared tolerance.

---

## Reconciliation — prompt claims vs the tree

Every position the prompt cited was checked. Divergences found:

| Prompt claimed | The tree held | What I did |
|---|---|---|
| `ms_fba_utils.py` `run_fva` at `:1331`, `classify_reactions_by_fva` at `:1940` | **exactly** those lines | nothing; verified by `grep -n` as instructed |
| KBDL consumer at `main ce9eb60`, `_shape_fva_result` at `:1087-1160`, essentiality import `:1113`, call `:1137`, `run_fva_panel` call `:2440` / import `:2433`, docstring `:92-125` | **all exact** — but the Dropbox checkout is parked on `wip`, where they do not match | read from `main` via `git show main:<path>`, per the parking-branch model |
| GAA at `~/Dropbox/Projects/GenomeAnnotationAggregator` "at its main, `58f20fa`" | that checkout is parked on **`wip`** and its HEAD commit is `0e614eb`; `58f20fa` is the **production venv's** GAA checkout at `/scratch/fliu/.../H100Projects/GenomeAnnotationAggregator` | read both; confirmed `fva_runner.py`, `outputs.py` `_classify_essentiality` and `schema.py` are byte-identical across them, and `MediaPanel` identical between `main` and `wip` (the only `main..wip` delta in `dataclasses.py` is in `BuildReport.gapfill_results`, which FVA does not touch). Parity ran against `58f20fa`, the cited SHA. |
| `fva_runner.py` 356 lines, `media_panel.py` 468, `dataclasses.py` `MediaPanel` at `:46`, `schema.py` `compute_model_fva_essentiality` at `:1232` | 356 / 468 / `:46` / `:1232` — all exact | nothing |
| scratch venv has modelseedpy from `e1155a4` | present, reporting **0.4.3** (not 0.4.2 as the programme context says both report) — and `kbutillib` is **not installed** there | used `PYTHONPATH=$PWD/src`, the pattern parts A–C established |
| use `tests/fixtures/model/haemophilus_influenzae_71421_8_rast.json.gz` "or a model built from it by part B's build path" | that fixture is a RAST **genome**, not a model | built a model from it via part B's path and committed the result, so the parity test needs no rebuild; recorded as asked |

Nothing in the prompt was contradicted by the tree in a way that changed the
design. No stop-and-report clause was triggered.

---

## Not done, and why

* **KBDL's own switch** from `genome_annotation_aggregator.model_build.fva_runner`
  to this module. KBDL is a different repo; this task was scoped to "add to
  KBUtilLib". The import swap is one line plus `make_media_panel` pointing at
  `kbutillib.domains.modeling.MediaPanel`.
* **A general `run_fva` fix for the `ObjectivePkg` divergence.** Scoped to the
  FVA panel here on purpose; see the flag above.
* **The process-pool / timeout path.** Explicitly optional per the task.
* **`classify_reactions_by_fva` (`ms_fba_utils.py:1940`)** — read, and left
  alone. It is a different analysis (unconstrained + growth-forced passes,
  per-biomass essential sets) and is not what KBDL's panel consumes.
