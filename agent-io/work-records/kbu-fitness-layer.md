# Work record — kbu-fitness-layer (task-fc5eced5)

Task 4 of PRD `kbdl-fitness-off-gaa-v1`. Repo KBUtilLib, branch
`maestro/developer/kbu-fitness-layer-task-fc5eced5`, base commit `4528fe9`
(`main` at dispatch was `da23f50`; this worktree's base is the same line).
Machine h100 (poplar), Linux, no human watching.

Scope delivered: the keyword-only entry point and its five KBDL-specific
behaviours, the flat re-export shim, the `[model]` extra's SHA bump, 18
real-dependency tests, and the committed ModelSEEDpy test-data fixtures the
tests need. Nothing in ModelSEEDpy was touched.

---

## Preconditions — both verified, neither assumed

| precondition | how checked | result |
|---|---|---|
| `KBDL_TARGET_FITNESS_SHA` recorded in ModelSEEDpy's work record | `git -C ~/Dropbox/Projects/ModelSEEDpy show kbdl-target:agent-io/work-records/msp-fitness-port.md` line 16 | `KBDL_TARGET_FITNESS_SHA = d55034cfb1eac8c6bd0f86720f7e8076ff9be2f7` |
| that SHA is published on the cshenry fork | `git ls-remote https://github.com/cshenry/ModelSEEDpy.git kbdl-target` | `d55034cfb1eac8c6bd0f86720f7e8076ff9be2f7  refs/heads/kbdl-target` |
| it contains `modelseedpy/core/msfitnessweighted.py` | `git ls-tree origin/kbdl-target modelseedpy/core/msfitnessweighted.py` | blob `b92b369`, 957 lines; `fitnessweightedfluxpkg.py` blob `98afb10`, 88 lines |
| KBUtilLib `main` carries `initialize_reconstruction_environment` | read in this worktree | `src/kbutillib/domains/modeling/ms_reconstruction_utils.py:528` |

**The env helper's signature, as found** (unchanged from what the PRD quoted):

```python
initialize_reconstruction_environment(
    *,
    dependency_config: Optional[Union[str, Path]] = None,
    dependencies: Iterable[str] = RECONSTRUCTION_DEPENDENCIES,
    solver: Optional[str] = None,
    solver_preference: Sequence[str] = DEFAULT_SOLVER_PREFERENCE,
    check_alias_matching: bool = True,
) -> Dict[str, Any]
```

Its work record is `agent-io/work-records/kbu-recon-env.md`. Measured here:
called with `check_alias_matching=False` it returns in **4.7 s** and reports
`solver='glpk'`, `solver_probes={'glpk': True}`, dependencies
`ModelSEEDDatabase` and `cb_annotation_ontology_api` both resolved from
`~/.kbutillib/dependencies.yaml`.

A second helper in the same module is reused rather than re-implemented:
`resolve_distribution_provenance(name) -> (version, revision)`
(`ms_reconstruction_utils.py:627`), which reads `direct_url.json`'s
`vcs_info.commit_id` and returns `UNKNOWN_REVISION = "unknown"` when the install
records none. That is exactly what `modelseedpy_sha` is specified to be, so the
entry point calls it instead of parsing metadata a second way.

The other named recon prerequisite — the KBDL template handler from
`kbdl-template-handler` — is **not needed by this task and was not waited on**:
this layer builds no model from a KBDL template, which is also why it passes
`check_alias_matching=False`.

---

## What changed

| file | change |
|---|---|
| `src/kbutillib/domains/modeling/ms_fitness_utils.py` (new, 827 lines) | the module; public `run_fitness_weighted_analysis` (:567) and `normalize_gene_reactions` (:187), plus `_policy_adjusted_fitness` (:262), `_annotate_significance` (:307), `_count_discounted` (:342), `_atp_hydrolysis_hint` (:386), `_atp_drain_id` (:412), `_atp_leak` (:459), `_neutral_gene` (:531), `_fitness_module` (:119), `_release` (:145), `_cobra_model` (:167) |
| `src/kbutillib/ms_fitness_utils.py` (new) | the flat re-export shim, GENERATED (see below), not hand-written |
| `scripts/generate_deprecation_shims.py:94` | `"ms_fitness_utils": "domains.modeling.ms_fitness_utils"` added to `LEGACY_MODULE_MAP` |
| `src/kbutillib/domains/modeling/__init__.py` | the two public names added to the lazy map, to `__all__` and to the submodule list in the docstring |
| `pyproject.toml:250-252` | `[model]` extra's modelseedpy pin bumped `e1155a41…` -> `d55034cfb1eac8c6bd0f86720f7e8076ff9be2f7`, with the comment extended to say what the new commit adds and why this module needs it |
| `tests/modeling/test_ms_fitness_utils.py` (new, 607 lines) | 18 tests, marker `kbu_model` |
| `tests/fixtures/model/e_coli_core.json.gz` (new, 29 KB) | ModelSEEDpy `tests/test_data/e_coli_core.json` at d55034c, gzipped |
| `tests/fixtures/model/template_core_bigg.json.gz` (new, 4.5 KB) | ModelSEEDpy `tests/test_data/template_core_bigg.json` at d55034c, gzipped |

### The signature, as delivered

```python
run_fitness_weighted_analysis(
    *, model, template, conditions, gene_reactions=None, biomass="bio1",
    atp_safe=False, core_template=None, blacklist=(),
    biomass_reference="model", missing_t_policy="fitness_only",
    fit_only_threshold=-2.0, nonpositive_penalty="one", price_reversals=True,
    frac=1.0, loopless="auto", **method_params,
) -> Dict[str, Any]
```

Exactly the specified signature, keyword-only, with no parameter added or
dropped. `**method_params` reaches `MSFitnessWeightedAnalysis` verbatim.

### The shim is generated, not written

`src/kbutillib/ms_fitness_utils.py` carries an `AUTO-GENERATED by
scripts/generate_deprecation_shims.py` banner, and
`tests/core/test_deprecation_shims.py` reads `LEGACY_MODULE_MAP` out of that
script and asserts every entry imports, warns, and resolves to the SAME objects
as the `domains.*` path. So the shim was produced by adding the table entry and
running the generator (`39 deprecation shim module(s) generated`), which
rewrote all 39 and left the other 38 byte-identical. Hand-writing it would have
left the table and the file able to disagree.

---

## (2b) Where the class uses the t-score — read at d55034c, line by line

Required verification before the substitution could be considered safe. Line
numbers are in `modelseedpy/core/msfitnessweighted.py` **at
`d55034cfb1eac8c6bd0f86720f7e8076ff9be2f7`**:

| line(s) | symbol | what `t` does there |
|---|---|---|
| 103, 105, 107, 110 | `normalize_fitness` | extracted from each accepted input shape; no arithmetic |
| 115-116 | `normalize_fitness` | a NaN `t` becomes `None` |
| 119-122 | `normalize_fitness` | multi-row-per-gene preference, decided by `_significant_negative(fit, t, T_SIG)` — i.e. the gate again, with the MODULE constant `T_SIG = 4.0`, not `self.params["t_sig"]` |
| 128-129 | `_significant_negative` | **the gate**: `fit is not None and t is not None and fit < 0 and abs(t) >= t_sig` |
| 159 | `classify_impact` | **the gate**: `if fit is None or t is None or abs(t) < t_sig: return False, 0.0, None` |
| 161-165 | `classify_impact` | tier and weight — computed from `fit` ALONE (`fit < fit_important` -> tier1 weight 1.0; `fit < 0` -> tier2 weight `|fit|/|fit_important|`). `t` appears nowhere |
| 526-533 | `gene_fit_best` | `{gene: abs(fit)}` for rows passing `_significant_negative`; the VALUE is `abs(fit)`, never `t` |
| 819, 853-855 | `analyze_condition`, `score_condition_edges` | `f, t = fit.get(gene, ...)` then straight into `classify_impact` |
| 879 | `score_condition_edges` | `"t_score": t` — reported, nothing more |

**Conclusion: `t` is used as a boolean gate and as a reported field, and for
nothing else.** No penalty, tier, weight, score, verdict or class is computed
from its magnitude. That is what makes `t_score = -t_sig` a way of opening the
class's own gate rather than a second, divergent scoring rule — and it is why
the wrapper could be built without touching `msfitnessweighted.py`. Had `t`
entered any arithmetic, this approach would have changed results and the task
said to stop; it does not, so it did not.

Two consequences recorded rather than papered over:

* The substitution uses the CONSTRUCTED OBJECT's `t_sig`
  (`analysis.params["t_sig"]`), so it is correct for a caller that overrides
  `t_sig` through `method_params`. `normalize_fitness`'s own dedup at :119-120
  still uses the module constant 4.0 in that case. That is Jose's behaviour,
  unchanged; the wrapper normalises first and adjusts afterwards, so it never
  feeds the synthetic value into that preference.
* `normalize_fitness` is idempotent on the `{gene: (fit, t)}` shape, so the
  class re-normalising the already-adjusted dict inside `analyze_condition`
  (:727) is a no-op.

---

## MEASURED

Everything below was executed in this worktree on 2026-11-16, h100.

### The scratch venv

Created new (never reusing or installing into an existing one):

```
uv venv --seed --python 3.11 /scratch/chenry/venvs/kbdl-fitness-layer-fc5eced5
uv pip install -e ".[model,dev]"     # with the bumped pin
```

`python -m venv` is unusable on this host (no `python3-venv`, no sudo), so uv
with `--seed` was used, as the dispatch contract requires. The install records:

```
{"url":"https://github.com/cshenry/modelseedpy",
 "vcs_info":{"vcs":"git","commit_id":"d55034cfb1eac8c6bd0f86720f7e8076ff9be2f7",
             "requested_revision":"d55034cfb1eac8c6bd0f86720f7e8076ff9be2f7"}}
resolve_distribution_provenance('modelseedpy') -> ('0.4.3', 'd55034cfb1…')
```

So the bumped pin resolves, installs, and is what the tests ran against.

### The tests this task adds

```
TMPDIR=/scratch/chenry/tmp KBU_REQUIRE_MODEL_STACK=1 \
/scratch/chenry/venvs/kbdl-fitness-layer-fc5eced5/bin/python -m pytest \
    tests/modeling/test_ms_fitness_utils.py -q -p no:cacheprovider --no-header
-> 18 passed, 54 warnings in 14.00s          exit 0
```

| class | tests | what is pinned |
|---|---|---|
| `TestDefectWithTScore` | 3 | `PGI_c0` priced at `1 - 0.9*tanh(1.5)` = 0.18537 and `PGI2_c0` at 1.0; `n_reactions_discounted == 1`; flux on PGI_c0 and none on PGI2_c0; swapping the gene swaps both the prices and the flux; `significance == "t"` with `t_score == -10.0`; every row carries `significance` |
| `TestMissingTScorePolicy` | 4 | no t-score, fitness -3.0: `ignore` -> n=0, all `significance is None`; `fitness_only` -> n=1, `significance == "fitness_only"`, `t_score is None`; fitness -1.5 under `fitness_only` -> n=0; an unknown policy raises `ValueError` |
| `TestGeneReactionNormalisation` | 4 | `PGI` + `PGI_c0` union genes AND b4025's sources, identical in both input orders; duplicate sources de-duplicated; a bare id reaches `PGI_c0` and `EX_glc__D_e0`; two unknown keys -> `n_unmapped_edge_reactions == 2` with the sorted sample, no raise, and the bare key's gene still reaches the price |
| `TestCompleteLeg` | 1 | `require_fitness=False` -> `status == "done"`, n=0, `edge_evidence == []`, biomass at `biomass_fixed`; the default (no flag) still reports `no_fitness`; the recomputed rollup is empty |
| `TestAtpSafe` | 2 | `atp_leak_flux` / `atp_leak_status` present and typed; the superset's bounds are byte-identical before and after the diagnostic; `atp_safe=True` without `core_template` raises `ValueError` |
| `TestCandidatePricing` | 1 | the candidate tier is open on the `ko=['GLCpts_c0']` toy, `candidate_factor` is 2.0 by default and 1.0 when passed, and the prices are 20x / 10x the template gap-fill penalty |
| `TestEntryPointContract` | 3 | `MSFBAUtils.__init__` patched to raise -> the run still completes; positional arguments raise `TypeError`; every condition reports `time_s` and `n_reactions_discounted`, and a no-growth medium reports `no_growth` with n=0 |

The ATP-safe test is the slow one (it runs `MSATPCorrection` end to end to build
the full test set); the whole module is still 14 s.

### Candidate pricing — the conductor's open question, measured

The predecessor task found that the complete `e_coli_core` toy leaves the
template with **0 candidates** (reproduced here: `n_candidates == 0`), so the
reported GAA parity never touched the candidate tier, and that on a
`ko=['GLCpts_c0']` leg the objective differed from GAA by 0.4995 until
`candidate_factor=1.0` was passed. Measured here on that same leg:

| `candidate_factor` | `GLCpts_c0` price `>` | template gap-fill penalty | weighted objective |
|---|---|---|---|
| 2.0 (Jose's default, what this wrapper uses) | 40000.0 | `{'forward': 2000}` | 400394.29211125744 |
| 1.0 (GAA-identical) | 20000.0 | same | 200394.29211125773 |

So `price = candidate_scale(10) * candidate_factor * penalty`, and **with the
wrapper's defaults candidate prices are exactly 2x GAA's**, which moves the
objective by ~0.4995 relative on a leg where a candidate carries flux — the same
number the predecessor measured. This is **not** silently absorbed:

* the entry point's docstring says it, names `candidate_factor=1.0` as the way
  to get GAA-identical candidate prices, and says why the default was left
  alone (conforming to Jose's port means taking his default, not quietly
  substituting another);
* `result["superset"]["params"]` carries `candidate_factor`, so every run
  reports which scale it used;
* `TestCandidatePricing` asserts both scales, so a future change to either is a
  test failure rather than a quiet drift.

**It is not fixed here, deliberately.** Overriding `candidate_factor` in the
wrapper would be exactly the divergence Chris's 2026-11-16 instruction forbids,
and the PRD's own default list does not include it. If KBDL wants GAA-identical
candidate pricing it is a one-keyword decision at the call site, and it should
be taken with a parity measurement in front of it.

### Regression comparison — a TARGETED selection, against a STALE base

The conductor's instruction stands: the full `tests/` tree takes ~3416 s on this
host against a 600 s sub-agent tool ceiling, so no full-suite run was attempted
and none should be read into this record. `build-baselines/kbutillib.json`
already exists (base `0f84803`, captured 2026-09-13 in a minimal venv, 99
`known_failing` nodeids) and was READ, not re-measured; no new baseline file was
written.

All runs below used
`/scratch/chenry/venvs/kbdl-fitness-layer-fc5eced5/bin/python -m pytest -q
-p no:cacheprovider --no-header` with `TMPDIR=/scratch/chenry/tmp`.

| selection | result | duration | exit |
|---|---|---|---|
| `tests/modeling/` minus the 4 `*_real_dependency.py` + `test_ms_community_utils_live.py` | **4 failed, 427 passed, 21 skipped** | 40 s | 1 |
| `tests/modeling/test_recon_env_real_dependency.py` + `test_fva_panel_real_dependency.py` | 31 passed | 60 s | 0 |
| `tests/modeling/test_recon_build_real_dependency.py` | 25 passed | 72 s | 0 |
| `tests/modeling/test_recon_gapfill_real_dependency.py` | 32 passed | 145 s | 0 |
| `tests/core/test_deprecation_shims.py`, `tests/domains/test_modeling_smoke.py`, `tests/test_python_floor_syntax.py`, `tests/core/test_composition_smoke.py` | 111 passed, 10 skipped, 1 xfailed, **3 errors** | 81 s | 1 |
| `tests/modeling/test_ms_fitness_utils.py` (this task's own) | 18 passed | 14 s | 0 |

Nodeid-level comparison, **against the STALE reference base `6310d61`**
(2026-10-19 capture, `/scratch/chenry/build-baselines/kbutillib-6310d61-failed.txt`,
69 failures) — stale because no baseline was captured at this task's own base
`4528fe9`, and not a pass/fail gate:

* The 4 failures are
  `tests/modeling/test_comprehensive_gapfill_wrapper.py::test_run_comprehensive_gapfill_on_model_{returns_correct_shape,solutions_nonempty,model_grows,reaction_count_increases}`
  — **all four present verbatim in the stale list**, and excluded by
  `--ignore` in the cached `build-baselines/kbutillib.json` command too. Cause:
  `ms_reconstruction_utils.py:2846` passes `base_media=` to
  `MSGapfill.__init__`, which the installed modelseedpy does not accept. **Not
  caused by the SHA bump**: `git diff --name-only e1155a4 d55034c --
  modelseedpy/core/msgapfill.py` is EMPTY — msgapfill.py is byte-identical at
  both pins, and `base_media` appears in neither.
* The 3 errors are `tests/core/test_composition_smoke.py::TestMSBiochemUtils::*`
  resolving ModelSEEDDatabase to
  `/mnt/homes/chenry/.maestro/worktrees/ModelSEEDDatabase` — the documented
  worktree measurement trap (a fixture resolving a data file relative to the
  package directory and landing on a sibling worktree path). Pre-existing and
  environment-only; they pass in the main clone.
* Several stale-list failures (`test_find_flux_loops.py`, 8 nodeids;
  `test_ms_reconstruction_utils.py::test_default_mode_uses_db_fallback`) PASSED
  here. A cached failure absent on this branch is not this task's concern.
* **No nodeid failed here that is absent from the stale list.** That is the bar,
  and it is met.

Lint, matching the repo's `.pre-commit-config.yaml` hooks (`ruff check`,
`ruff format`):

```
ruff check src/kbutillib/domains/modeling/ms_fitness_utils.py \
           src/kbutillib/ms_fitness_utils.py \
           tests/modeling/test_ms_fitness_utils.py \
           src/kbutillib/domains/modeling/__init__.py \
           scripts/generate_deprecation_shims.py
-> All checks passed!          exit 0
ruff format  (the two new files)     -> 2 files reformatted, then re-tested green
```

`src/kbutillib/domains/modeling/__init__.py` would still be reformatted by
`ruff format`, at PRE-EXISTING long lines (`MSReconstructionUtilsImpl`,
`directionality_from_bounds`, …), not at anything added here. Reformatting it
would bundle an unrelated change, so it was left alone.

---

## Packaging outcome

`pyproject.toml`'s `[model]` extra now reads

```
model = [
  "modelseedpy @ git+https://github.com/cshenry/ModelSEEDpy.git@d55034cfb1eac8c6bd0f86720f7e8076ff9be2f7",
]
```

and the comment above it now records what d55034c adds over e1155a41 and why
this module requires it (`MSFitnessWeightedAnalysis` does not exist at
e1155a41, so with the old pin `run_fitness_weighted_analysis` would raise
`ImportError` at call time).

The `[dev]` / `[dependency-groups].dev` duplication rule was respected: `model`
is an extra with no matching dependency group (nothing syncs a `model` group, as
its own comment says), so there was no second list to keep in step. No other
dependency was added, moved or removed.

**One consequence callers must know.** `tests/modeling/test_recon_build_real_dependency.py:396
::test_that_revision_is_the_one_pyproject_pins` asserts the INSTALLED
modelseedpy revision equals the pin. It passes here because this venv is
installed at d55034c. In a venv still at e1155a41 — for instance
`/scratch/chenry/venvs/kbdl-recon-scratch-env`, which earlier recon tasks used —
that test now FAILS until the venv is reinstalled. That is the test doing its
job ("guards against measuring one install and shipping another"), not a
regression, and the fix is `uv pip install -e ".[model]"` in that venv. The
module docstrings of the three recon `*_real_dependency.py` files still quote the
old SHA when describing that venv; they are prose, not assertions, and were left
untouched as out of scope.

---

## Divergences between the task prompt and the tree

Reconciled and carried on in every case; nothing here stopped the work.

1. **`biomass='BIOMASS_Ecoli_core_w_GAM'` for the e_coli_core fixture.** The
   prompt says to pass that id. The tree says otherwise: the toy the prompt
   itself specifies (`PGI_c0`, `PGI2_c0`) is e_coli_core with ModelSEED
   compartment suffixes, and under that suffixing — ModelSEEDpy's own
   `test_msfitnessweighted.py` recipe, which the fixture's gap-fill candidates
   and ATP scaffold require — the biomass reaction is
   **`BIOMASS_Ecoli_core_w_GAM_c0`**. Upstream's own fitness test file sets
   `BIOMASS = "BIOMASS_Ecoli_core_w_GAM_c0"` at line 18. Used that; recorded in
   a comment on the constant.
2. **`modelseedpy_sha` "from the installed distribution's direct_url.json
   commit id, else 'unknown'".** That reader already exists on `main` as
   `resolve_distribution_provenance` (`ms_reconstruction_utils.py:627`) and
   returns exactly that. Reused it rather than writing a second metadata parser.
3. **ATP tests on the e_coli_core fixture need the ATP hydrolysis reaction
   named.** `atp_tests_from_correction(model, core_template)` with no
   `atp_hydrolysis_id` fails on this fixture with
   `TypeError: 'NoneType' object is not subscriptable` inside
   `MSATPCorrection.__init__:109`, because `add_atp_hydrolysis` builds from
   ModelSEED compound ids (`cpd00001_c0`) and e_coli_core names water `h2o_c0`,
   so it returns `None`. Upstream's own test works around it by passing
   `atp_hydrolysis_id="ATPM_c0"`. The wrapper's specified signature has no such
   parameter, so instead of adding one it resolves the hint itself
   (`_atp_hydrolysis_hint`, :386) using the class's OWN rule — the
   `rxn00062_*` / `ATPM*` test from `_empty_atp_test` (:476), same order — and
   passes it. On a real ModelSEED model this picks `rxn00062_c0`, which is what
   `MSATPCorrection` would have found anyway, so behaviour there is unchanged.
4. **ModelSEEDpy's `tests/test_data` is not reachable from an installed
   package.** A `pip install` from git carries no `tests/` directory, and
   depending on a ModelSEEDpy source checkout would make these tests pass or
   fail according to another repo's working tree. Both files were therefore
   committed as gzipped fixtures (29 KB and 4.5 KB) under `tests/fixtures/model/`
   — the pattern this repo already uses — with their provenance recorded in the
   test module docstring.
5. **`agent-io/work-records/msp-fitness-port.md` is not on `origin/kbdl-target`.**
   It is on the LOCAL `kbdl-target` (08d239d = d55034c + the baseline commit +
   the work-record commit); only the code was pushed. The precondition is about
   the record existing and naming the SHA, which it does, and the SHA it names
   was independently confirmed published by `git ls-remote`. Not a blocker.
6. **The result's `edges` key when `gene_reactions is None`.** The prompt
   defines it as "the normalised merged mapping actually passed to the class",
   which is `None` in that case. The key is still populated, from
   `analysis.edges` (the model's own gene rules as the class read them), because
   a key that vanishes for one input shape is worse than one that always answers
   "the edges in force". Documented in the docstring.

---

## What was NOT done, and why

* **`msfitnessweighted.py` was not modified, extended or subclassed in the
  library.** Every KBDL behaviour is in the wrapper, on the public API. The one
  subclass in this diff is a test-only recorder inside the test module's
  `recorder` fixture.
* **No full-suite baseline was captured or written.** Forbidden by the dispatch
  contract (3416 s vs a 600 s ceiling) and a cached baseline already exists.
* **`candidate_factor` was not overridden** — see the measurement above.
* **No retry logic.** 2f is satisfied by reporting: `time_s` comes from the
  class, a non-optimal solve keeps the class's own status (`solver_failed` /
  `no_growth`), and the wrapper never loops. A solver that RAISES still
  propagates and ends the run; that is stated in the module docstring rather
  than hidden behind a bare `except`, because a partial panel reported as a
  complete one is the failure mode this PRD exists to stop.
* **The three recon `*_real_dependency.py` docstrings still name e1155a41** when
  describing a scratch venv. Prose, not an assertion; out of scope.
