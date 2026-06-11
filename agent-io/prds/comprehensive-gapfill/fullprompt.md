# PRD: Comprehensive Gapfilling

## Problem Statement

Standard ModelSEED gapfilling adds the *minimum* set of reactions needed to make
a single target (biomass) flux feasible on a given medium. As a side effect,
large portions of a reconstructed model remain **blocked** — reactions that can
never carry flux because the surrounding network lacks the connections to
produce their substrates or consume their products. A modeler who wants the most
*functionally complete* model — one in which as many of the model's own reactions
as possible can actually carry flux — has no single command to produce it. They
would have to hand-assemble a two-stage optimization against ModelSEEDpy
internals (`MSGapfill.gfmodel`, `GapfillingPkg`, `ReactionActivationPkg`,
`RevBinPkg`), which is fragile and undocumented.

## Solution

Add a **comprehensive gapfilling** procedure that maximizes the number of the
model's own reactions that can be simultaneously active in complete media, then
finds the minimum-cost set of database reactions that keeps that activated set
viable while still producing biomass. The model returned is the
"comprehensively gapfilled" model: biomass-positive and with the largest
feasible fraction of its native reactions unblocked.

From the user's perspective:

- They call `MSReconstructionUtils.run_comprehensive_gapfill_on_model(mdlutl, templates, ...)`
  on an existing reconstructed model and get back a gapfilled, growing model in
  which substantially more of the model's native reactions carry flux than after
  standard gapfilling.
- Power users working directly in ModelSEEDpy can pass
  `gapfilling_mode="Comprehensive"` to `MSGapfill.run_multi_gapfill(...)` and get
  the same behavior, integrated with the existing solution-integration and
  sensitivity-analysis plumbing.

## User Stories

1. As a modeler, I want a one-call comprehensive gapfill on an existing model so
   that I get a functionally complete, growing model without hand-wiring an
   optimization.
2. As a modeler, I want comprehensive gapfilling to recruit database reactions
   *only* to unblock my model's own reactions, so the result is my model made
   functional — not my model plus thousands of unrelated database reactions.
3. As a modeler, I want the comprehensively gapfilled model to still produce
   biomass on the target objective, so it remains a usable growth model.
4. As a modeler, I want comprehensive gapfilling to run in complete media so the
   activation maximization is not artificially limited by nutrient availability.
5. As a ModelSEEDpy power user, I want `gapfilling_mode="Comprehensive"` in
   `run_multi_gapfill` so the new mode reuses the existing prefilter, integrate,
   and sensitivity-analysis machinery rather than a parallel code path.
6. As a ModelSEEDpy power user, I want the comprehensive routine to return a
   solution dict in the same shape as `run_gapfilling`, so
   `integrate_gapfill_solution` and downstream reporting consume it unchanged.
7. As a maintainer, I want the two-stage optimization to live as a dedicated
   `MSGapfill.run_comprehensive_gapfilling` method (a deep module behind a small
   interface), so the KBUtilLib wrapper and the `run_multi_gapfill` dispatcher
   are both thin.
8. As a maintainer, I want the gapfilling model (`gfmodel`) restored to a clean,
   reusable state after each comprehensive run (objective restored, activation
   locks released), so multi-media runs and subsequent gapfills are not
   corrupted by leftover constraints.
9. As a modeler running multiple media, I want comprehensive gapfilling to
   integrate cumulatively across media (like Sequential mode), so the activated
   sets from each medium accumulate into one model.

## Implementation Decisions

### Module A — ModelSEEDpy `MSGapfill.run_comprehensive_gapfilling` (the deep module)

New method on `modelseedpy/core/msgapfill.py`:

```
def run_comprehensive_gapfilling(
    self, media, target=None, minimum_obj=None,
    binary_check=False, prefilter=True, activation_cap=0.001
) -> dict | None
```

Algorithm (operates on `self.gfmodel` / `self.gfpkgmgr`, exactly the surface
`run_gapfilling` uses):

1. **Setup.** `target = target or self.default_target`;
   `minimum_obj = minimum_obj or self.default_minimum_objective`.
   `self.gfpkgmgr.getpkg("GapfillingPkg").set_base_objective(target, minimum_obj)`
   (this installs the `biomass >= minimum_obj` constraint *and* the standard
   gapfilling minimization objective). `set_media(media)` (complete media is
   supplied by the caller).
2. **Feasibility / prefilter.** Reuse `run_gapfilling`'s guard:
   `test_gapfill_database(media, target, before_filtering=prefilter)`; if
   `prefilter`, run `self.prefilter(...)` then re-test. Bail to `None` on
   infeasibility (matches `run_gapfilling`).
3. **Build activation constraints, filtered to the model's own reactions.**
   `orig_ids = set(r.id for r in self.model.reactions)` (the *original* model,
   not the candidate-laden `gfmodel`). Intersect with `gfmodel` reaction ids for
   safety. `rapkg = self.gfpkgmgr.getpkg("ReactionActivation")` then
   `rapkg.build_package(rxn_filter=orig_ids, max_value=activation_cap)`. This also
   builds `RevBinPkg` over the same filter — **required** so a reaction cannot
   activate both directions via a net-zero futile assignment.
4. **Stage 1 — maximize coverage.** Save the current objective. Build a max
   objective over all activation variables:
   `obj = gfmodel.problem.Objective(Zero, direction="max")`,
   `obj.set_linear_coefficients({v: 1 for v in rapkg.variables["fra"].values()} | {v: 1 for v in rapkg.variables["rra"].values()})`.
   `gfmodel.objective = obj`; `sol = gfmodel.optimize()`. If `sol.status != "optimal"`, return `None`.
5. **Lock.** For each `fra`/`rra` variable whose `primal >= activation_cap - 1e-9`,
   set `var.lb = activation_cap` (direction-preserving). Variables that did not
   reach the cap keep `lb = 0` (left free). Track the locked variables so they can
   be released in step 8.
6. **Stage 2 — minimize cost under locks.** Restore the gapfilling objective via
   `set_base_objective(target, minimum_obj)` (re-installs biomass constraint +
   gapfilling minimization). `sol2 = gfmodel.optimize()`; if not optimal, release
   locks and return `None`. `self.last_solution =
   self.gfpkgmgr.getpkg("GapfillingPkg").compute_gapfilled_solution()`. Run
   `binary_check_gapfilling_solution()` iff `binary_check`. Attach
   `media`/`target`/`minobjective`/`binary_check` keys exactly as `run_gapfilling`
   does.
7. **Test-condition reconciliation.** If `self.test_conditions`, run them through
   `run_test_conditions(...)` exactly as `run_gapfilling` (so ATP safety is
   preserved); return `None` if no compliant solution.
8. **Cleanup (mandatory).** Release all activation locks (`var.lb = 0` for the
   tracked variables) so `gfmodel` is reusable. The gapfilling objective is
   already restored from step 6. Return `self.last_solution`.

Return shape is identical to `run_gapfilling`'s `last_solution` dict, so callers
and `integrate_gapfill_solution` need no special-casing.

### Module B — ModelSEEDpy `MSGapfill.run_multi_gapfill` dispatch

Add a branch in the per-media loop, parallel to the existing
`"Independent"/"Sequential"` branch:

- When `gapfilling_mode == "Comprehensive"`: call
  `solution = self.run_comprehensive_gapfilling(media, target_i, threshold_i, binary_check, prefilter=False)`
  (prefilter already handled by `test_and_adjust_gapfilling_conditions` at the
  top of `run_multi_gapfill`, same as Sequential). If `solution`, integrate with
  `integrate_gapfill_solution(..., gapfilling_mode="Sequential")` semantics
  (cumulative integration), and apply the same Sequential penalty-recompute step
  between media so already-added reactions are not re-penalized.
- After the loop, restore the gapfilling objective exactly as the existing
  `elif gapfilling_mode == "Sequential":` post-loop block does
  (`compute_gapfilling_penalties(...)` + `build_gapfilling_objective_function()`).

The existing top-of-function `test_and_adjust_gapfilling_conditions` and the
end-of-function sensitivity-analysis block are reused unchanged (Comprehensive
solutions have the same dict shape).

### Module C — KBUtilLib `MSReconstructionUtils.run_comprehensive_gapfill_on_model` (thin wrapper)

New method on `src/kbutillib/ms_reconstruction_utils.py`, mirroring
`gapfill_metabolic_model`'s construction:

```
def run_comprehensive_gapfill_on_model(
    self, mdlutl, templates, media=None, core_template=None,
    source_models=None, additional_tests=None, atp_safe=True,
    reaction_exclusion_list=None, objective="bio1", minimum_objective=0.01,
    base_media=None, base_media_target_element="C", reaction_scores={},
) -> tuple
```

- Resolve complete media: if `media is None`, load `KBaseMedia/Complete`
  (via the same media-resolution path the module already uses, e.g.
  `self.get_media("KBaseMedia/Complete", None)` / `process_media_list`).
- Compute ATP tests when `atp_safe` (reuse the `get_atp_tests(...)` block from
  `gapfill_metabolic_model`).
- Construct `MSGapfill(mdlutl, templates, source_models, all_tests,
  blacklist=reaction_exclusion_list, default_target=objective,
  minimum_obj=minimum_objective, base_media=base_media,
  base_media_target_element=base_media_target_element)`; set `reaction_scores`.
- Call `msgapfill.run_multi_gapfill([complete_media], target=objective,
  default_minimum_objective=minimum_objective, gapfilling_mode="Comprehensive",
  binary_check=False, prefilter=True, run_sensitivity_analysis=True,
  integrate_solutions=True)`.
- Return a tuple consistent with `gapfill_metabolic_model`:
  `(current_output, solutions, output_solution, output_solution_media)` where
  `current_output` records Growth / GS GF / Reactions / Model genes. The
  integrated `mdlutl` is mutated in place (caller already holds it).

### Cross-cutting decisions

- **Single source of the activated set:** the original-model reaction id set is
  captured from `self.model.reactions` inside `run_comprehensive_gapfilling`, not
  passed in — there is exactly one correct filter and it should not be a caller
  footgun.
- **`activation_cap` is a parameter** defaulting to `0.001` (the
  `ReactionActivationPkg` default). Lock bound == cap.
- **Package-name strings** are exact: `getpkg("ReactionActivation")`,
  `getpkg("GapfillingPkg")`, `getpkg("KBaseMediaPkg")`. Variable dicts are
  `variables["fra"]` and `variables["rra"]`, keyed by reaction id.
- **No new gapfilling_mode constants elsewhere** — `"Comprehensive"` is a plain
  string checked alongside the existing modes.

## Testing Decisions

Test **external behavior**, not the internal LP construction.

- **ModelSEEDpy integration test (primary):** on a small toy model + template
  where some native reactions are blocked under standard gapfilling, assert:
  (a) the comprehensively gapfilled model grows (`biomass slim_optimize > 0` on
  complete media); (b) the count of *original-model* reactions carrying nonzero
  flux (pFBA or FVA) is **strictly greater** than after a standard
  `run_multi_gapfill(gapfilling_mode="Sequential")` on the same inputs; (c) no
  reaction that exists only as a gapfill candidate (absent from the original
  model) is force-locked into the result beyond what minimization requires —
  operationalized as: re-running standard gapfilling on the comprehensive model
  removes no native reactions. Prior art: existing ModelSEEDpy gapfill tests
  under `tests/` that build a small model and assert growth.
- **`gfmodel` reusability test:** run comprehensive gapfilling, then assert a
  subsequent standard `run_gapfilling` on the same `MSGapfill` instance still
  behaves normally (objective restored, no residual activation locks) — guards
  Module A step 8.
- **KBUtilLib wrapper test:** `run_comprehensive_gapfill_on_model` on a small
  saved model + template returns a biomass-positive model and a non-empty
  solution dict; reaction count increases vs input. Mirror existing KBUtilLib
  reconstruction/gapfill pytest patterns.

Which modules get tests: Module A (the deep routine) and Module C (the wrapper)
are both tested. Module B is exercised transitively by the Module A test through
`run_multi_gapfill(gapfilling_mode="Comprehensive")`.

## Out of Scope

- Multi-objective / non-biomass activation targets (biomass is the only enforced
  growth target).
- Performance optimization of the MILP beyond filtering activation to model
  reactions (RevBin binaries over native reactions are accepted as inherent cost).
- KBase app wiring / narrative UI for comprehensive gapfilling.
- Changing the default behavior of any existing gapfilling mode.
- Exposing `activation_cap` through the KBUtilLib wrapper (defaulted in the deep
  method; can be threaded later if needed).

## Further Notes

- The `MSReconstructionUtils.gapfill_metabolic_model` method is the structural
  template for Module C (ATP tests, MSGapfill construction, return tuple).
- `RevBinPkg` is pulled in automatically by `ReactionActivationPkg.build_package`;
  no separate call is needed, but the correctness rationale (net-zero futile
  activation) must be respected — do not "optimize" it away.
- Both repos build and test locally on primary-laptop; no GPU or cross-machine
  execution required.
