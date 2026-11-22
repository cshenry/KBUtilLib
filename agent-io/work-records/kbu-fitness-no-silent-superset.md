# Work record — kbu-fitness-no-silent-superset (task-6f3cae5f)

Conduct row 1989, "Finding A". Repo KBUtilLib, branch
`maestro/developer/task-kbu-fitness-no-silent-super-task-6f3cae5f`, base commit
`65db6ead6d6401edfa92b61284c6ee47f76e8272` (as the task expected). Machine h100
(poplar), Linux, no human watching.

## The decision

Chris, 2026-11-21 23:55 CST, answering

> "should I fix the KBUtilLib wrapper so a model that can't grow returns
> no_growth, and the row reports which growth target was actually used?"

> **"Yes"**

## The defect, as found in the installed modelseedpy

`modelseedpy` `edafdb1b6ff0b4d694528cd417af32f0581cf607`,
`modelseedpy/core/msfitnessweighted.py`, `analyze_condition` — read out of the
venv at `.../site-packages/modelseedpy/core/msfitnessweighted.py:752-759`:

```python
target = P["frac"] * opt_superset
if (
    P["biomass_reference"] == "model"
    and opt_model is not None
    and opt_model >= P["min_growth"]
):
    target = P["frac"] * opt_model
res["biomass_fixed"] = target
```

`res["growth_model"] = opt_model` is set just above, so a model that cannot
grow is priced against **the superset's optimum** while the caller's row still
says `model`. Observed on real Keio, condition `no-carbon-control`:
`growth_model` 0.0, `biomass_fixed` 10.709949424823044, a 24-reaction gap-fill,
reported as `model` (KBDLJobRunningPrototype `main`,
`agent-io/work-records/fit-live-acceptance.md`, "Finding A").

`MIN_GROWTH` is 0.01 at this commit (`msfitnessweighted.py:68`). The class's
test is `>=`, so the wrapper's test is `<` — a model growing at *exactly*
`min_growth` clears the bar and is not relabelled.

## Files changed

| file | what |
|---|---|
| `src/kbutillib/domains/modeling/ms_fitness_utils.py` | the fix; constants, two helpers, the post-processing loop, three docstrings |
| `tests/modeling/test_ms_fitness_no_growth.py` | new, 20 tests |
| `tests/fixtures/model/fitness_growing_model_reference_base.json` | new, the base commit's own output for the growing-model case |

### `ms_fitness_utils.py` in detail

- `ms_fitness_utils.py:155-172` — `NO_GROWTH_SURVIVING_KEYS`
  (`condition`, `status`, `growth_superset`, `growth_model`) and
  `TIMING_KEY_PREFIX` (`time_`). Both added to `__all__`
  (`ms_fitness_utils.py:120-121`), matching the module's habit of publishing its
  constants.
- `ms_fitness_utils.py:480-513` — `_target_was_the_superset`, which mirrors the
  class's branch rather than guessing at it: no `growth_model` key at all
  (the condition returned before the model solve, so it never had a target),
  `growth_model is None`, or `growth_model < min_growth`.
- `ms_fitness_utils.py:516-536` — `_relabel_no_growth`, which deletes every key
  that is not allow-listed and sets `status = "no_growth"`.
- `ms_fitness_utils.py:539-547` — `_survives_no_growth`, the allow-list test.
- the post-processing loop after `result = analysis.run(specs)` — the relabel
  runs **first in the loop body**, before the `complete_legs` branch that
  `continue`s, and before `n_reactions_discounted` is computed. Then
  `biomass_reference_used` is set on **every** condition.

**Why an allow-list and not a deny-list.** The task required it and it is the
right shape: the eleven keys the task enumerates are exactly what
`analyze_condition` sets today, so a deny-list would be correct today and would
silently pass through the next key the class learns to set. Verified against the
installed source: on the `done` path the method sets `condition`, `status`,
`growth_superset`, `growth_model`, `biomass_fixed`, `time_solve_s`, `loopless`,
`objective`, `gapfilled`, `reversals_used`, `distance_to_growth`,
`annotation_gap`, `fluxes`, `growth_network`, `reaction_calls`, `time_fva_s`,
`edge_evidence`, `time_s` — so the allow-list's complement is precisely the
eleven named keys plus nothing else.

**The discarded solve is called out in the code** (`_relabel_no_growth`'s
docstring): the class has already solved the leg by the time `run` returns, so
the relabel throws that work away. Avoiding the solve would need a change in
`modelseedpy`, which this task does not make.

`atp_filter` and its key are untouched. No default changed. `biomass_reference`
still defaults to `"model"`.

## Fail-before evidence

`tests/conftest.py:19` does `sys.path.insert(0, <repo>/src)`, so `PYTHONPATH`
cannot be used to point a pytest run at a different kbutillib. The base run was
therefore done inside a detached worktree of base `65db6ea` under
`$MAESTRO_TASK_SCRATCH`, with the new test file and fixture copied in.

`17 failed, 3 passed` on base; `20 passed` on the branch. The four headline
failures, verbatim:

```
assert dead_model_result["conditions"]["glc"]["status"] == "no_growth"
E  AssertionError: assert 'done' == 'no_growth'

assert leaked == [], f"superset-referenced fields leaked: {leaked}"
E  AssertionError: superset-referenced fields leaked: ['biomass_fixed',
   'loopless', 'objective', 'gapfilled', 'reversals_used',
   'distance_to_growth', 'annotation_gap', 'fluxes', 'growth_network',
   'reaction_calls', 'edge_evidence']

assert dead_model_result["annotation_evidence"] == []
E  AssertionError: Left contains 31 more items, first extra item:
   {'gene': 'b0720', 'reaction': 'CS_c0', 'source': 'model', 'net_score': -1.0, ...}

assert dead_model_result["conditions"]["glc"]["biomass_reference_used"] is None
E  KeyError: 'biomass_reference_used'
```

That is Finding A itself: `done` instead of `no_growth`, all eleven
superset-solve fields readable, and 31 evidence rows derived from a solve
against a target the model cannot reach. The 3 that passed on base are the ones
that assert unchanged behaviour (`growth_superset > 0`, the superset leg still
gap-filling, the Complete leg's zero count) — they must pass on both.

### The toy

`test_ms_fitness_utils`' own toy with `GLCpts_c0` knocked out — `e_coli_core`'s
only glucose uptake route, which the core BiGG template can put back. The same
knockout `TestCandidatePricing` already uses. Measured on base:

| field | base `65db6ea` | branch |
|---|---|---|
| `status` | `done` | `no_growth` |
| `growth_superset` | 0.8739215069684295 | 0.8739215069684295 |
| `growth_model` | `None` | `None` |
| `biomass_fixed` | 0.8739215069684295 | *absent* |
| `gapfilled` | `{'GLCpts_c0': '>'}` | *absent* |
| `n_reactions_discounted` | 1 | 0 |
| `len(annotation_evidence)` | 31 | 0 |
| `biomass_reference_used` | *absent* | `None` |

The real Keio case is the same shape with `growth_model` 0.0 rather than
`None`; both are covered, `0.0` by a stubbed unit test
(`TestTheMinGrowthBoundary::test_zero_is_relabelled`).

## Test outcomes

All runs in a **fresh** `python3.11 -m venv` at
`$MAESTRO_TASK_SCRATCH/venv` (`cpython-3.11.15`, pytest 9.2.0), never a
pre-existing one, `pip install -e ".[model]"`. modelseedpy pin verified from
`site-packages/modelseedpy-0.4.3.dist-info/direct_url.json`:

```json
{"url": "https://github.com/cshenry/ModelSEEDpy.git", "vcs_info":
 {"commit_id": "edafdb1b6ff0b4d694528cd417af32f0581cf607", ...}}
```

| run | command | result | exit |
|---|---|---|---|
| base, the two named files | `REAL_SOLVER_TEST=1 pytest -q -p no:cacheprovider tests/modeling/test_ms_fitness_utils.py tests/modeling/test_ms_fitness_atp_filter.py` | **44 passed** (as the task stated) | 0 |
| branch, those two files | same | **44 passed** | 0 |
| branch, plus the new file | same + `tests/modeling/test_ms_fitness_no_growth.py` | **64 passed** | 0 |
| new file on base | run inside the base worktree | **17 failed, 3 passed** | 1 |

**No existing test was weakened, skipped or deleted, and none needed
`biomass_reference="superset"` added.** All 44 pass unchanged. The one existing
test that touches a non-growing condition,
`TestEntryPointContract::test_every_condition_reports_its_wall_clock`, uses an
empty medium, where the **superset** cannot grow either — so the class returns
`no_growth` before the model solve, no `growth_model` key is set, and
`_target_was_the_superset` returns `False`. It is untouched by the relabel and
its assertions (`time_s` is a float, `n_reactions_discounted == 0`) still hold.
`TestCandidatePricing` uses the same `GLCpts_c0` knockout as the new tests but
asserts only on the recorded `analysis` object's prices, never on the condition,
so it is unaffected.

### Wider regression check, by nodeid

`REAL_SOLVER_TEST=1 pytest -q -p no:cacheprovider tests/modeling/`, run on both
trees in the same venv:

- base: `6 failed, 539 passed, 31 skipped`
- branch: `6 failed, 559 passed, 31 skipped` (+20 = the new file)

The six are **identical by nodeid** and pre-existing:

```
tests/modeling/test_comprehensive_gapfill_wrapper.py::test_run_comprehensive_gapfill_on_model_model_grows
tests/modeling/test_comprehensive_gapfill_wrapper.py::test_run_comprehensive_gapfill_on_model_reaction_count_increases
tests/modeling/test_comprehensive_gapfill_wrapper.py::test_run_comprehensive_gapfill_on_model_returns_correct_shape
tests/modeling/test_comprehensive_gapfill_wrapper.py::test_run_comprehensive_gapfill_on_model_solutions_nonempty
tests/modeling/test_recon_build_real_dependency.py::TestProvenanceComesFromDistributionMetadata::test_that_revision_is_the_one_pyproject_pins
tests/modeling/test_recon_build_real_dependency.py::TestProvenanceComesFromDistributionMetadata::test_the_modelseedpy_revision_is_a_real_commit
```

`build-baselines/kbutillib.json` was NOT used as the baseline and NOT rewritten:
it was captured at `0f84803` in a minimal venv missing pandas/torch/rdkit, with
7 modules `--ignore`d, and records none of these nodeids. A same-venv base-vs-
branch comparison is the stronger evidence, so that is what was run.

### Lint and types

- `ruff check` on both changed files: **All checks passed!**
- `ruff format`: the new test file is formatted. `ms_fitness_utils.py` has 5
  format hunks, **identical on base and branch** (in `_serialise_atp_tests`,
  `_check_atp_filter_shape`, `_check_atp_filter_key`, `_replay_atp_filter`) —
  all pre-existing, none in the changed code, so they were left alone rather
  than swept into this diff.
- `mypy --no-incremental --cache-dir=/dev/null --follow-imports=skip` on the
  changed file: **25 errors on base, 25 on branch**. The change adds none.

## Downstream: KBDLJobRunningPrototype `main` at `72b81d7`

Confirmed `main` is at `72b81d7` ("feat(kbdl): ATP filter computed once per
model and stored as an ATPFilter object"). A **`git archive`** was used rather
than a worktree, so nothing was written to the live repo at all — two real jobs
are running against that tree. Extracted to `$MAESTRO_TASK_SCRATCH/kbdl`,
dependencies installed, then `pip install --no-deps` of the archive, with
kbutillib left resolving to this worktree via the editable `.pth`. Verified
before running:

```
kbutillib -> /mnt/homes/chenry/.maestro/worktrees/task-6f3cae5f/src/kbutillib/...
has fix: True
kbdl -> .../site-packages/kbdl_service/__init__.py
```

`REAL_SOLVER_TEST=1 pytest -q -p no:cacheprovider
tests/test_job_types_fitness_model_analysis.py tests/test_atp_filter_store.py`:

| kbutillib | result |
|---|---|
| base `65db6ea` | **175 passed** |
| this branch | **175 passed** |

**No failures by nodeid in either run, so nothing is new.** KBDL was not
edited.

### What that result does and does not prove — read this before trusting it

KBDL's fitness-job tests **monkeypatch `_call_fitness_analysis`**
(`tests/test_job_types_fitness_model_analysis.py:500, 525, 535`), so they never
invoke the real `run_fitness_weighted_analysis`. The 175 passes therefore prove
that the job layer's own contract still holds; they do **not** exercise the
relabel.

So the consumer was read directly instead, and it handles a relabelled
condition correctly — every removed key is read defensively and lands on the
"never solved" encoding this table already defines (`None` for numerics, `[]`
for lists), in `src/kbdl_service/job_types/fitness_model_analysis.py`:

- `:1463` `_numeric(condition.get("biomass_fixed"))` → `None`
- `:1465` `_integer(condition.get("distance_to_growth"))` → `None`
- `:1483` `sorted(condition.get("reaction_calls") or {})` → loop not entered,
  so the `condition["reaction_calls"][...]` at `:1484` is unreachable
- `:1513` `condition.get("edge_evidence") or []` → `[]`
- `:1362` `(condition.get(key) or {}).items()` for `gapfilled` /
  `reversals_used` → `{}`
- `:1318` `_loopless_label` guards with `if "loopless" not in condition`
- `:942` `{k: v for k, v in condition.items() if k != "fluxes"}` → unaffected

The only direct subscripts of any removed key in KBDL's `src` are the two above
and both are guarded.

**One thing for KBDL, not fixed here:** nothing in KBDL reads
`biomass_reference_used` yet, so the new key is currently dropped on the floor
rather than stored. Recording it on `v2_fitness_conditions` is a KBDL change
and is out of this task's scope.

## Notes, and what was inferred rather than measured

- The golden `tests/fixtures/model/fitness_growing_model_reference_base.json`
  is the **base commit's own output** for the growing-model,
  `biomass_reference="model"` call, timing stripped with the existing
  `_without_timing` helper. Diffing base against branch gave exactly one line —
  `+ "biomass_reference_used": "model"` — which is the measurement behind "the
  growing-model case is unchanged". The test compares with `rel=1e-6` rather
  than `==`, because the golden carries solver output that is reproducible to
  far better than that here but is not bit-stable across glpk builds; the defect
  this guards is a factor of 10 on real Keio and could not hide inside 1e-6.
- `biomass_reference_used` is normalised to `"model"` / `"superset"` rather than
  echoing the argument, because the class treats **any** value other than
  `"model"` as the superset — so echoing a typo would misreport what ran. The
  wrapper does not validate `biomass_reference`, and this change does not start
  validating it.
- The two boundary cases the task flagged for stubbing are stubbed
  (`TestTheMinGrowthBoundary`, `TestTheRelabelItself`): a model growing at
  *exactly* `min_growth` cannot be built to order, and the question is purely
  which side of the class's `>=` the wrapper lands on.
- `solver_failed` is covered by a stub rather than a real solve: provoking a
  genuine solver failure on this toy was not attempted. The code path is the
  same `_relabel_no_growth` call, and the assertion is that a `solver_failed`
  condition carrying `biomass_fixed` is relabelled and loses it.
- The Complete leg keeps `edge_evidence = []` after the relabel, because the
  Complete-leg branch sets it unconditionally and item 4 of the task asks that
  leg to "still end with ... a consistent edge_evidence". An empty list leaks
  nothing from the superset solve.
