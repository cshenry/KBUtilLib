# Work record — kbu-fitness-exchanges-not-gapfills (task-9b6a904e)

Conduct row 2075. Repo KBUtilLib, branch
`maestro/developer/kbutillib-in-the-fitness-weighte-task-9b6a904e`, base commit
`e50fdb4ce14e44aff7df4efbbaa1ae7dc1e22670` (as the task expected: `8c0bd1e`
plus `agent-io/` docs only). Machine h100 (poplar), Linux, no human watching.

## The decision

Chris, 2026-11-23 11:57 CST, answering

> "Exclude exchange reactions from distance_to_growth on Complete?"

> **"Yes, exclude them"**

and, 2026-11-21, on what Complete means:

> "Complete generally means ALL exchange fluxes allow uptake. ALWAYS"

## The defect, as found in the installed modelseedpy

`modelseedpy` `edafdb1b6ff0b4d694528cd417af32f0581cf607` (verified at runtime:
`resolve_distribution_provenance("modelseedpy")` returns
`('0.4.3', 'edafdb1b6ff0b4d694528cd417af32f0581cf607')`),
`modelseedpy/core/msfitnessweighted.py`. Read out of
`/home/chenry/venvs/kbdl/lib/python3.11/site-packages/`.

`_classify_superset` (`msfitnessweighted.py:496-523`) — candidates include
boundary reactions, `priced` excludes them:

```python
self.candidates = {
    r.id for r in sup.reactions if r.id not in self.draft_reactions
}
...
self.priced = [
    r.id for r in sup.reactions if not r.boundary and r.id != self.biomass
]
```

`analyze_condition` (`msfitnessweighted.py:775-790`) — every candidate carrying
flux is a gap-fill, and that count is the distance:

```python
gapfilled = {}
for rid in self.candidates:
    v = fluxes.get(rid, 0.0)
    if abs(v) > tol:
        gapfilled[rid] = ">" if v > 0 else "<"
...
res["distance_to_growth"] = len(gapfilled) + (
    len(reversed_used) if P["price_reversals"] else 0
)
res["annotation_gap"] = res["distance_to_growth"] > 0
```

So an exchange the superset added is **free** — the class never prices one, in
any condition — and was still reported as missing biology. Measured on real
Keio (KBDL job 2576dc35, Complete medium): 18 gap-fill entries, several of them
`EX_` reactions, none of which the model has.

`msfitnessweighted.py` is Jose's class and is **not** changed by this task
(Chris, 2026-11-16: conform to the port rather than fork it). The correction is
in the KBUtilLib wrapper.

## Files changed

| file | what |
|---|---|
| `src/kbutillib/domains/modeling/ms_fitness_utils.py` | the fix: two helpers, three lines in the post-processing loop, two docstrings |
| `tests/modeling/test_ms_fitness_exchanges.py` | new, 31 tests |
| `tests/fixtures/model/fitness_no_added_exchange_base.json` | new, the base commit's own output for the no-added-exchange condition |
| `tests/modeling/test_ms_fitness_no_growth.py` | one existing golden test taught to pop the two new keys (see "The one existing test that had to change") |

### `ms_fitness_utils.py` in detail

- `ms_fitness_utils.py:568-595` — `_boundary_reaction_ids(analysis)`: the ids of
  every superset reaction for which `cobra.Reaction.boundary` is true. **The
  same test the class's `priced` list uses**, deliberately not an id-prefix
  test: `EX_`/`DM_`/`SK_` are naming conventions of whatever built the model,
  the class never looks at them, and a model naming its exchanges differently
  would walk straight through a prefix check. A consequence worth stating: this
  also catches the `DM_` demand/sink drains `GapfillingPkg` adds alongside the
  `EX_` exchanges (`gapfillingpkg.py:460-467`), which is right — they are
  unpriced for exactly the same reason.
- `ms_fitness_utils.py:597-663` — `_split_exchanges_added(...)`: moves boundary
  entries out of `gapfilled` into `exchanges_added`, sets `n_exchanges_added`,
  and recomputes `distance_to_growth` and `annotation_gap` **from the narrowed
  `gapfilled`, by the class's own formula**, reading
  `analysis.params["price_reversals"]` rather than this module's argument so the
  recount follows whatever the object was constructed with. Returns immediately
  on a condition with no `gapfilled` key.
- `ms_fitness_utils.py:1808-1813` — `boundary_reactions` and `priced_reversals`
  read off the analysis **once**, before the loop.
- `ms_fitness_utils.py:1836-1845` — the call, placed after the Finding A relabel
  and after `biomass_reference_used`, and **before** the `complete_legs` branch
  that `continue`s. Order matters twice over: after the relabel, so a
  relabelled condition has no `gapfilled` left for this to touch; before the
  Complete branch, so the Complete leg is split too.
- the module docstring gains item 8, and `run_fitness_weighted_analysis`'
  `Returns:` documents both new keys.

Nothing re-solves. The solve, the penalties, the ATP filter and its key, and
every default are untouched; the exchange is still in `fluxes` with the flux it
carried, so moving its `gapfilled` entry is a relabelling and not a retraction.

### Every condition, not only Complete

Applied to **every** condition. The reason is in the class, not in the medium:
`priced` is `not r.boundary` unconditionally, so the class leaves exchanges
unpriced in every condition and an added exchange is therefore a gap-fill
nowhere. Restricting the rule to Complete would have made
`distance_to_growth` mean two different things in one result.

**Measured on the toy panel** (`tests/modeling/test_ms_fitness_exchanges.py`,
three conditions: `glc` glucose-only, `fru` fructose-only, `complete`
`require_fitness=False`), base `e50fdb4` versus this branch:

| condition | base `gapfilled` | base `d2g` | branch `gapfilled` | branch `exchanges_added` | branch `d2g` |
|---|---|---|---|---|---|
| `glc` | `{'GLCpts_c0': '>', 'EX_glc__D_e0': '<'}` | 2 | `{'GLCpts_c0': '>'}` | `{'EX_glc__D_e0': '<'}` | 1 |
| `fru` | `{}` | 0 | `{}` | `{}` | 0 |
| `complete` | `{'GLCpts_c0': '>', 'EX_glc__D_e0': '<'}` | 2 | `{'GLCpts_c0': '>'}` | `{'EX_glc__D_e0': '<'}` | 1 |

So **1 of the 2 non-Complete conditions changes** — the one where the
superset's added exchange actually carried flux. The rule is not "Complete
only", and it is not "every condition unconditionally" either: it is "wherever
a boundary candidate carried flux", and that is pinned by
`TestEveryConditionAndNotOnlyComplete::test_how_many_non_complete_conditions_this_changes`.

## Every consumer of `gapfilled`, and what changed

Grepped: `gapfilled`, `distance_to_growth` and `annotation_gap` appear nowhere
else in `src/`, `bin/` or `scripts/` than `ms_fitness_utils.py` (the other hits
are unrelated prose about gap-filled *models*). Within the module:

| consumer | reads `gapfilled`? | what changed | why |
|---|---|---|---|
| `distance_to_growth` (per condition) | yes, in the class | **recomputed** from the narrowed map, by the class's own formula | the whole point of the change |
| `annotation_gap` (per condition) | yes, via `distance_to_growth` | **recomputed** as `distance_to_growth > 0` | ditto; it can now go `False` on a condition whose only "gap-fill" was an exchange |
| `n_reactions_discounted` (`_count_discounted`, `ms_fitness_utils.py:448-489`) | **no** | nothing | it counts MODEL reactions (`analysis.draft_reactions`) and explicitly skips candidates, so a superset-added exchange could never be in it. It counts pricing, not flux; `gapfilled` is not an input. Pinned by `TestTheGapfilledConsumers::test_the_discount_count_only_ever_sees_model_reactions` and by the golden comparison |
| `edge_evidence` (per condition) | **no** | nothing | the class builds it from `analysis.edges` ∩ `reaction_calls`, never from `gapfilled` |
| `annotation_evidence` (the rollup, `ms_fitness_utils.py:1863`) | **no** | nothing | `aggregate_evidence` rolls up `edge_evidence` rows |
| `_annotate_significance` (`ms_fitness_utils.py:413-445`) | **no** | nothing | it annotates `edge_evidence` rows in place |
| `reaction_calls` / `growth_network` | **no** | nothing | computed by the class inside `analyze_condition`, from its own in-flight `gapfilled` local, before `run` returns. Out of this layer's reach without re-solving, which this task does not do |
| `fluxes` | **no** | nothing | the exchange's flux is still reported, which is how a reader sees the solve really used it |

**"An exchange is not annotation evidence" is structurally guaranteed, and that
is why no evidence filter was added.** An added exchange cannot reach
`edge_evidence` or `annotation_evidence` by any route, with or without a
caller-supplied gene mapping, because `normalize_gene_reactions`
(`ms_fitness_utils.py:293-360`) resolves a mapping's keys **only** onto model
reaction ids — a key naming a superset-added reaction is counted as unmapped and
dropped. Measured, not assumed: passing
`gene_reactions={"EX_glc__D_e0": ["gX"], "PGI_c0": ["b4025"]}` through the entry
point on the toy logs

```
1 of 2 gene-mapping reaction keys matched no model reaction and were dropped
(first 1: ['EX_glc__D_e0'])
```

and the resulting `edges` in force is `{'PGI_c0': {'b4025': ['model']}}`. The
`gene_reactions=None` path takes edges from the model's own gene rules, which
are likewise model reactions only. Both panel-wide assertions
(`test_annotation_evidence_holds_no_exchange`,
`test_no_condition_s_edge_evidence_holds_an_exchange`) **pass on the base commit
as well as on this branch**, which is the evidence that nothing here moved. A
filter over evidence rows would have been dead code.

## The exact new result keys a caller (KBDL) must read

Per condition, present on **every solved condition** — one carrying a
`gapfilled` key — and absent on `no_growth`/`no_fitness`, which carry no
`gapfilled` either:

| key | type | meaning |
|---|---|---|
| `exchanges_added` | `{reaction_id: ">" \| "<"}` | the boundary (exchange/demand) superset reactions that carried flux. Same shape and same direction strings as `gapfilled`. `{}` when none |
| `n_exchanges_added` | `int` | `len(exchanges_added)`. `0` when none |

Changed meaning of keys that already existed:

| key | before | now |
|---|---|---|
| `gapfilled` | every candidate carrying flux, exchanges included | every **non-boundary** candidate carrying flux |
| `distance_to_growth` | `len(gapfilled) + len(reversals_used) if price_reversals` over the old `gapfilled` | the same formula over the new, narrower `gapfilled` |
| `annotation_gap` | `distance_to_growth > 0` | unchanged formula, new input — so it can now be `False` where it was `True` |

Unchanged: `reversals_used` (a reversal is a direction of a reaction the model
already has, so it is never a boundary candidate), `fluxes`, `objective`,
`loopless`, `reaction_calls`, `growth_network`, `edge_evidence`,
`n_reactions_discounted`, `biomass_reference_used`, `atp_filter` and its key,
and the whole `superset` summary.

**Both keys are always present on a solved condition** rather than added only
when non-empty. The task's test list asks for "a condition with no added
exchange is byte-identical to base output", which a condition carrying two new
keys is not; the reconciliation taken is the one the sibling change already set
(`biomass_reference_used` is set on every condition "so a caller never has to
ask which run produced one"), and it is what items 1 and 2 of the task ask for
literally ("For every condition that has a gapfilled map", "Add
`n_exchanges_added`"). The byte-identity is therefore asserted **modulo those
two keys, with both asserted empty** — `gapfilled`, `distance_to_growth`,
`annotation_gap`, `fluxes`, `reaction_calls`, `edge_evidence` and every other
field compared against the base commit's own output. Stated here because a
caller reading only the task text would expect the keys to be conditional.

## The one existing test that had to change

`tests/modeling/test_ms_fitness_no_growth.py::TestGrowingModelIsUntouched::test_every_other_field_matches_the_base_commit`
compares a condition against a committed golden with an exact key-set check
(`_assert_close` asserts `set(actual) == set(expected)`), popping
`biomass_reference_used` as the one key its own change added. It now pops
`exchanges_added` and `n_exchanges_added` too, **asserting both empty** —
that toy's superset adds no boundary reaction at all, so a non-empty value
there would mean the split had invented one. Nothing was weakened: every other
field is still compared against base.

## Fail-before evidence

`tests/conftest.py:19` does `sys.path.insert(0, <repo>/src)`, so `PYTHONPATH`
cannot point a pytest run at a different kbutillib. The base run was therefore
done in a detached worktree of base `e50fdb4` under `$MAESTRO_TASK_SCRATCH`,
with the new test file and the new fixture copied in.

```
cd $MAESTRO_TASK_SCRATCH/base
TMPDIR=... KBU_REQUIRE_MODEL_STACK=1 python -m pytest \
    tests/modeling/test_ms_fitness_exchanges.py -q -p no:cacheprovider --no-header
18 failed, 13 passed
```

versus `31 passed` on the branch. The headline failures, verbatim:

```
assert ADDED_EXCHANGE not in condition["gapfilled"]
E  AssertionError: assert 'EX_glc__D_e0' not in {'EX_glc__D_e0': '<', 'GLCpts_c0': '>'}

assert condition["distance_to_growth"] == 1
E  assert 2 == 1

assert changed == {"glc"}
E  AssertionError: assert set() == {'glc'}

assert condition["exchanges_added"] == {}
E  KeyError: 'exchanges_added'
```

That is the defect itself: the exchange counted as a gap-fill, the distance one
too high, no condition split, and neither new key in existence.

**The 13 that pass on base are deliberate and are the second half of the
evidence.** They are the premise checks (the superset really does offer
`EX_glc__D_e0` as an unpriced boundary candidate the model does not have, and
`GLCpts_c0` alongside it as a priced one), the unchanged-behaviour checks (the
evidence rollup holds no exchange; the discount count sees model reactions only;
a mapping key naming an added exchange is dropped at normalisation) and
`TestANoGrowthConditionIsUntouched`. The `boundary` fixture computes
`cobra.Reaction.boundary` over the superset itself rather than calling the new
`_boundary_reaction_ids`, precisely so those tests are runnable on base — a
fixture calling the new helper would have turned the no-change evidence into
three collection errors.

### The toy, and why the existing one could not be reused

`test_ms_fitness_utils`' toy **cannot** reproduce this defect, for a reason
worth recording: the committed core BiGG template fixture
(`tests/fixtures/model/template_core_bigg.json.gz`) declares all 72 of its
`compcompounds` with `templatecompartment_ref` `"~/compartments/id/"` — an empty
compartment id — and ships `"compartments": []`. `MSTemplateSpecies.from_dict`
therefore reads every compartment as the empty string, so
`GapfillingPkg.extend_model_with_template_metabolites`' `if compartment == "e"`
never fires and **no exchange is ever created** for a new extracellular
metabolite. Measured: that toy's superset has 0 boundary candidates, with or
without reaction knockouts.

The new toy therefore does two things to the existing fixtures, both minimal:

1. `_template_with_extracellular_compartment()` points the `_e` compcompounds at
   compartment `e`, which is what a real ModelSEED template does. No metabolite
   or reaction id in the superset changes (`to_metabolite` builds `glc__D_e` +
   index `0` either way); the only difference is that extracellular metabolites
   now reach `new_exchange`.
2. The model drops `EX_glc__D_e0`, `GLCpts_c0` **and the orphan metabolite
   `glc__D_e0`**. The third is required: `_model` keeps every metabolite in the
   fixture JSON whether a surviving reaction uses it or not, and an
   extracellular metabolite the model already has is not in `new_exchange`.

Result: the superset re-adds `GLCpts_c0` (a priced candidate) and creates
`EX_glc__D_e0` for it (`bounds (-100, 100)`, boundary, unpriced) — the real Keio
shape, an `EX_` in the gap-fill that the model does not have.

The panel runs with `biomass_reference="superset"`. It has to: this toy's model
cannot grow on glucose at all — that is what makes the gap-fill add the exchange
— and under the `model` default the Finding A relabel discards the whole solve
before the split could see it. That case is covered separately and asserted
untouched (`TestANoGrowthConditionIsUntouched`).

The medium is keyed by the toy's own cobra metabolite ids (`glc__D_e0`) rather
than bare BiGG ids (`glc__D`). Not cosmetic: `KBaseMediaPkg` resolves a media
compound through `MSModelUtil.find_met`, which indexes `met.id`, `met.name` and
`met.annotation` — and the metabolite the **template** contributes carries no
annotation, so a bare `glc__D` key finds the model's own metabolite but not a
template-contributed one, and the added exchange stays shut (measured:
`EX_glc bounds after media: (0, 100)`, `superset growth: nan`).

## Test outcomes

Environment: `/home/chenry/venvs/kbdl/bin/python` (cpython-3.11.15, pytest
9.1.1, cobra 0.32.1), modelseedpy `edafdb1`, `TMPDIR` under
`$MAESTRO_TASK_SCRATCH`. No install was performed into that venv or any other;
`tests/conftest.py` puts the worktree's `src` on `sys.path` itself.

### The three fitness modules, base versus branch

```
python -m pytest tests/modeling/test_ms_fitness_utils.py \
    tests/modeling/test_ms_fitness_no_growth.py \
    tests/modeling/test_ms_fitness_atp_filter.py -q -p no:cacheprovider --no-header
```

| | base `e50fdb4` | branch |
|---|---|---|
| result | `64 passed` (exit 0) | `64 passed` (exit 0) |

The one test that failed in between — `test_every_other_field_matches_the_base_commit`,
on the new keys — is the one fixed as described above.

### The new module

| | base `e50fdb4` | branch |
|---|---|---|
| `tests/modeling/test_ms_fitness_exchanges.py` | `18 failed, 13 passed` | `31 passed` (exit 0) |

### `tests/modeling`, base versus branch, compared by nodeid

```
python -m pytest tests/modeling -q -p no:cacheprovider --no-header
```

| | base `e50fdb4` | branch |
|---|---|---|
| result | `18 failed, 553 passed, 25 skipped` in 291 s | `18 failed, 584 passed, 25 skipped` in 274 s |

The +31 passes are this change's new module. **The two failed-nodeid sets are
identical — `comm -13` and `comm -23` over the sorted nodeid lists are both
empty, so there is no regression and nothing that only base failed.** The 18
pre-existing failures, none of them in `test_ms_fitness_*`:

```
tests/modeling/test_comprehensive_gapfill_wrapper.py::test_run_comprehensive_gapfill_on_model_model_grows
tests/modeling/test_comprehensive_gapfill_wrapper.py::test_run_comprehensive_gapfill_on_model_reaction_count_increases
tests/modeling/test_comprehensive_gapfill_wrapper.py::test_run_comprehensive_gapfill_on_model_returns_correct_shape
tests/modeling/test_comprehensive_gapfill_wrapper.py::test_run_comprehensive_gapfill_on_model_solutions_nonempty
tests/modeling/test_find_flux_loops.py::TestEnumerateAlternativeReactionSets::test_alternatives_reported
tests/modeling/test_find_flux_loops.py::TestEnumerateAlternativeReactionSets::test_essential_reaction_flagged
tests/modeling/test_find_flux_loops.py::TestEnumerateAlternativeReactionSets::test_schema
tests/modeling/test_find_flux_loops.py::TestFindFluxLoopsEndToEnd::test_atp_cycle_detected
tests/modeling/test_find_flux_loops.py::TestMinimizeActiveReactions::test_result_schema
tests/modeling/test_find_flux_loops.py::TestMinimizeActiveReactions::test_returns_short_path
tests/modeling/test_find_flux_loops.py::TestModelUnmodifiedAfterFindFluxLoops::test_bounds_restored
tests/modeling/test_find_flux_loops.py::TestModelUnmodifiedAfterFindFluxLoops::test_no_binary_vars_remain
tests/modeling/test_find_flux_loops.py::TestModelUnmodifiedAfterFindFluxLoops::test_no_probe_reactions_remain
tests/modeling/test_find_flux_loops.py::TestModelUnmodifiedAfterFindFluxLoops::test_rxn_count_unchanged
tests/modeling/test_fva_panel_real_dependency.py::TestFixtureProvenance::test_gaa_is_not_importable_here_which_is_why_the_fixture_exists
tests/modeling/test_lp_solver_solver_backends.py::test_gurobi_inf_or_unbd_disambiguates_to_unbounded
tests/modeling/test_recon_build_real_dependency.py::TestProvenanceComesFromDistributionMetadata::test_that_revision_is_the_one_pyproject_pins
tests/modeling/test_recon_build_real_dependency.py::TestProvenanceComesFromDistributionMetadata::test_the_modelseedpy_revision_is_a_real_commit
```

The base run was done in the detached base worktree with the new test module and
the new fixture **removed**, so it measures the base tree and nothing else.

`build-baselines/` was NOT re-captured, per the role's instruction to use the
cached baseline rather than re-run the full suite: `build-baselines/kbutillib.json`
and `build-baselines/kbutillib-h100-main-6310d61.json` both already exist, and the
latter records the full-suite duration as 3416 s (5.7x the 600 s tool ceiling) and
says in terms that only a conductor may run it. The targeted base-versus-branch
run above is the comparison, and it is strictly stronger for this change than the
cached set because both halves are at this task's own commit pair.

Cross-referenced against that cached baseline's `modeling` failures anyway
(16 nodeids, captured at `6310d61` on 2026-10-19). Three names fail on this base
that are not in it —
`test_fva_panel_real_dependency.py::TestFixtureProvenance::test_gaa_is_not_importable_here_which_is_why_the_fixture_exists`
and the two
`test_recon_build_real_dependency.py::TestProvenanceComesFromDistributionMetadata`
tests — and one in it does not fail here
(`test_ms_reconstruction_utils.py::test_default_mode_uses_db_fallback`). All four
differences are present identically on base and branch, so none is this change's;
they are 5 weeks of drift between `6310d61` and `e50fdb4`.

#### The new module, by nodeid

All 31 pass on the branch.  The 13 that also pass on base `e50fdb4` are marked
`base-pass` -- they assert the premise or assert that nothing moved; the 18
marked `base-FAIL` are the fail-before evidence.

```
base-pass  TestTheSupersetOffersAnUnpricedExchange::test_the_exchange_is_a_candidate_the_model_does_not_have
base-pass  TestTheSupersetOffersAnUnpricedExchange::test_the_exchange_is_a_boundary_reaction
base-pass  TestTheSupersetOffersAnUnpricedExchange::test_the_class_never_prices_it
base-FAIL  TestTheSupersetOffersAnUnpricedExchange::test_the_boundary_set_is_the_supersets_own_boundary_reactions
base-pass  TestTheSupersetOffersAnUnpricedExchange::test_the_transporter_is_a_candidate_and_is_priced
base-FAIL  TestAnAddedExchangeIsNotAGapFill::test_the_exchange_is_reported_as_an_exchange
base-FAIL  TestAnAddedExchangeIsNotAGapFill::test_the_exchange_is_not_a_gap_fill
base-FAIL  TestAnAddedExchangeIsNotAGapFill::test_n_exchanges_added_counts_them
base-FAIL  TestAnAddedExchangeIsNotAGapFill::test_distance_to_growth_is_recomputed
base-pass  TestAnAddedExchangeIsNotAGapFill::test_annotation_gap_is_recomputed
base-FAIL  TestAnAddedExchangeIsNotAGapFill::test_the_exchange_really_did_carry_flux
base-pass  TestAnAddedExchangeIsNotAGapFill::test_annotation_evidence_holds_no_exchange
base-pass  TestAnAddedExchangeIsNotAGapFill::test_no_condition_s_edge_evidence_holds_an_exchange
base-FAIL  TestEveryConditionAndNotOnlyComplete::test_the_complete_leg_is_split_too
base-FAIL  TestEveryConditionAndNotOnlyComplete::test_a_non_complete_condition_is_split_too
base-FAIL  TestEveryConditionAndNotOnlyComplete::test_how_many_non_complete_conditions_this_changes
base-FAIL  TestAConditionWithNoAddedExchange::test_nothing_was_added_and_nothing_was_gap_filled
base-FAIL  TestAConditionWithNoAddedExchange::test_every_other_field_matches_the_base_commit
base-pass  TestANoGrowthConditionIsUntouched::test_the_condition_is_still_relabelled
base-pass  TestANoGrowthConditionIsUntouched::test_it_gained_neither_new_key
base-pass  TestANoGrowthConditionIsUntouched::test_the_allow_list_still_describes_it_exactly
base-pass  TestTheGapfilledConsumers::test_a_mapping_key_naming_an_added_exchange_is_dropped
base-pass  TestTheGapfilledConsumers::test_the_discount_count_only_ever_sees_model_reactions
base-pass  TestTheGapfilledConsumers::test_the_discount_count_is_what_it_was
base-FAIL  TestTheSplitItself::test_it_splits_and_recounts_with_reversals_priced
base-FAIL  TestTheSplitItself::test_reversals_are_left_out_when_the_class_does_not_price_them
base-FAIL  TestTheSplitItself::test_annotation_gap_goes_false_when_only_exchanges_were_added
base-FAIL  TestTheSplitItself::test_the_test_is_boundary_membership_and_not_the_id_prefix
base-FAIL  TestTheSplitItself::test_an_empty_gapfilled_still_gets_both_keys
base-FAIL  TestTheSplitItself::test_a_condition_with_no_gapfilled_key_is_left_exactly_alone
base-FAIL  TestTheSplitItself::test_a_missing_reversals_used_counts_as_none
```

`test_annotation_gap_is_recomputed` passes on base because it asserts the
INVARIANT (`annotation_gap is (distance_to_growth > 0)`), which the class also
satisfies -- base simply satisfies it with the wrong distance, which
`test_distance_to_growth_is_recomputed` is what catches.

#### The four fitness modules together

```
python -m pytest tests/modeling/test_ms_fitness_exchanges.py \
    tests/modeling/test_ms_fitness_no_growth.py \
    tests/modeling/test_ms_fitness_utils.py \
    tests/modeling/test_ms_fitness_atp_filter.py -q -p no:cacheprovider --no-header
95 passed in 42.78s          exit 0
```

#### Lint

```
ruff check src/kbutillib/domains/modeling/ms_fitness_utils.py \
    tests/modeling/test_ms_fitness_exchanges.py \
    tests/modeling/test_ms_fitness_no_growth.py
All checks passed!           exit 0   (ruff 0.17.5)
```

`ruff format --check` reports `ms_fitness_utils.py` as needing reformatting at
five places -- `:1166`, `:1295`, `:1319`, `:1328`, `:1407`, all in the ATP-filter
region and none of them this change's. **Verified pre-existing**: the same file
at base `e50fdb4` reports the same, so the repo was last formatted with a
different ruff. Left alone rather than reformatted, to keep the diff to this
change. The new test module was formatted and is clean.

## What was inferred rather than verified

- The real-Keio numbers (KBDL job 2576dc35, 18 gap-fill entries several of them
  `EX_`) are the task's, carried over; nothing in this task re-ran that job.
- `reaction_calls` and `growth_network` are computed by the class from its own
  in-flight `gapfilled` before `run` returns, so the restricted FVA in the
  `glc` condition still ran with the added exchange counted as part of the
  solution network. That is correct — the exchange really did carry flux — but
  it means those two fields are not "exchange-free" in the sense `gapfilled`
  now is. Changing them would need a re-solve or a change in Jose's class, and
  this task does neither.
- `cobra.Reaction.boundary` is read as the authority on what an exchange is,
  because it is what the class's own `priced` list reads. A superset whose
  exchanges were somehow not boundary reactions would defeat both equally.
