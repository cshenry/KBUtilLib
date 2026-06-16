# PRD: find_flux_loops — energy/redox/mass loop (EGC) detection in MSFBAUtils

## Problem Statement

When a ModelSEED `MSTemplate` (the universal biochemistry database that every
reconstructed model is built from) contains a reaction whose assigned
directionality/reversibility is wrong, the network can carry flux through a
thermodynamically infeasible internal cycle — an **energy-generating cycle
(EGC)**: a closed loop that regenerates ATP, reducing equivalents, or mass out
of nothing. Because the template is the source of truth, a single such defect
propagates into every model built from it, silently inflating ATP/biomass
yields and corrupting downstream FBA. Today there is no tool that audits a
template for these loops, and finding them by hand is intractable across
~30–40k reactions.

## Solution

A set of utility functions on `MSFBAUtils` (in `ms_fba_utils.py`) that audit a
template for energy/redox/mass loops. The user passes a template (and
optionally a target — a custom reaction object or the name of a predefined
target group). The tool builds a fully-closed metabolic model from the
template, adds a synthetic, mass-and-charge-balanced **probe** reaction that
drains a high-energy currency back to its low-energy form (ATP hydrolysis;
redox-cofactor drain releasing H2; mass sinks for CO2/acetate/formate/NH3/…),
then maximizes flux through that probe in the closed system. *Any* positive
flux is, by construction, a loop. The tool then reduces that loop to a
provably-minimal reaction set, scans the perturbation space around it
(alternatives, coupled reactions, essential reactions), and returns a
structured defect report: which reactions form each loop and in which
direction each was used. The user reads the report and fixes the offending
reaction's directionality in the template.

The mechanism is a direct generalization of ModelSEEDpy's existing ATP-correction
machinery (`MSModelUtil.add_atp_hydrolysis`, `analyze_minimal_reaction_set`,
`ReactionUsePkg`, `binary_check_gapfilling_solution`), lifted from "ATP only" to
an arbitrary catalog of energy/redox/mass probes and exposed as reusable
KBUtilLib utilities.

## User Stories

1. As a model curator, I want to pass a ModelSEED template to `find_flux_loops`
   and get back the list of energy-generating cycles it permits, so that I can
   find and fix bad reaction reversibilities before they propagate into models.
2. As a curator, I want to audit against a predefined group of targets ("atp",
   "redox", "mass", or "all") in one call, so that I do not have to enumerate
   probe reactions by hand.
3. As a curator, I want to pass a single custom reaction object as the target,
   so that I can test a specific currency or a non-standard drain.
4. As a curator, for each loop I want the minimal set of reactions that sustains
   it (not a bloated active set), so that I can see the smallest culprit group.
5. As a curator, for each reaction in a loop I want to know its alternatives
   (other reactions that can substitute for it), the reactions coupled to it,
   and whether it is essential to the loop, so that I can identify the shared
   reaction whose directionality is the real defect.
6. As a curator, I want each loop reaction reported with its direction-of-use,
   equation, reliability score, and core flag, so that I can prioritize which
   reaction to inspect.
7. As a developer, I want a standalone `minimize_active_reactions` utility that
   returns the provably-minimum set of active reactions for any constrained
   objective, so that I can reuse it outside loop analysis (e.g. minimal
   pathway extraction).
8. As a developer, I want a standalone `add_probe_reaction` utility that adds a
   catalog probe (or reuses an existing matching reaction), so that I can build
   custom closed-system tests.
9. As a curator, I want the audit to run in tractable time on a full ~35k-reaction
   template, so that I can run it routinely rather than only on toy subsets.
10. As a curator, I want a clean template to return an empty defect list (no
    false positives), so that I can trust an empty result.
11. As a curator, I want the model left unmodified after the audit (probes and
    use-variables stripped), so that I can reuse the same object.

## Implementation Decisions

### Scope & placement
- All functions are methods on `MSFBAUtils` in
  `src/kbutillib/ms_fba_utils.py`. `MSFBAUtils` already subclasses
  `KBModelUtils` and reaches `MSModelUtil` helpers via
  `_check_and_convert_model`, and inherits `get_template()`. No new module.
- Input is an `MSTemplate` (decided). `find_flux_loops` accepts a template
  object or a template id resolvable via the inherited `get_template()`.

### `build_full_model_from_template(template, index="0")`
- Build a fresh `cobra.Model` containing **every** template reaction, via
  `MSTemplateReaction.to_reaction(model, index)` — which carries each reaction's
  template directionality through its `lower_bound`/`upper_bound`
  (`get_reaction_constraints_from_direction`). Directionality is the defect
  surface, so it must be preserved exactly.
- Add no biomass, no exchanges, no demand/sink reactions. The result is a fully
  closed system. Wrap as `MSModelUtil` and return.
- If the template build produces any boundary reactions, zero their bounds
  (`lb=ub=0`). Likewise force any maintenance/biomass reaction bounds to 0.

### Probe catalog & `add_probe_reaction(model, probe, compartment="c0")`
- Generalize `MSModelUtil.add_atp_hydrolysis`: given a probe spec (ModelSEED
  compound-id stoichiometry + name + seed.reaction annotation), use
  `find_reaction(stoichiometry)` to reuse an existing matching reaction in the
  proper direction if present (`{"new": False}`), else build a new
  `cobra.Reaction` (lb=0, ub=1000) with the `seed.reaction` annotation and add
  it. Return `{"reaction", "direction", "new"}` exactly like `add_atp_hydrolysis`.
- The catalog is a module-level constant keyed by group:
  - **atp**: ATP hydrolysis — `cpd00002 + cpd00001 -> cpd00008 + cpd00009 + cpd00067`
    (ATP + H2O -> ADP + Pi + H), reusing `add_atp_hydrolysis`'s exact stoichiometry.
  - **redox**: one **balanced drain per cofactor couple**, releasing the
    reducing equivalents as H2 (decided). Convention: `<reduced> -> <oxidized> +
    H2 (+/- H+)`, balanced elementally and by charge using the couple's
    ModelSEED formulas/charges (e.g. NADH cpd00004 / NAD cpd00003; NADPH
    cpd00005 / NADP cpd00006; plus FAD/FADH2, ferredoxin ox/red, quinone/quinol,
    glutathione, thioredoxin). H2 is released into the same compartment pool and
    must be internally recycled by the loop for the probe to carry flux (no H2
    sink is opened for redox probes). The implementer resolves exact ModelSEED
    ids and charge-balances each probe against the biochemistry DB
    (`MSFBAUtils` has biochem access); couples whose ids cannot be resolved are
    skipped with a logged warning rather than guessed.
  - **mass**: irreversible sink reactions `<metabolite> ->` for CO2 (cpd00011),
    acetate (cpd00029), formate (cpd00047), NH3 (cpd00013), and similar freely
    consumable end-products. Resolve ids via the biochem DB; skip+warn on
    unresolved.
  - **all**: union of atp + redox + mass.
- The `objective` parameter of `find_flux_loops` accepts: `None`/`"all"` (all
  groups), a group name string (`"atp"`/`"redox"`/`"mass"`), or a single
  `cobra.Reaction` object (custom probe, added as-is). A group string iterates
  over every probe in the group; results are keyed by probe.

### Per-probe loop-finding pipeline (the core algorithm)
For each probe target, on the closed model (LP-only until the support is tiny;
binaries introduced last and stripped between loops):

```
add_probe_reaction(model, probe)            # reuse-or-add; get probe rxn R, direction
set objective = MAX{R}; vmax = LP optimize  # plain LP, fast at template scale
if vmax <= tol:                             # no loop for this probe
    record clean; strip probe; continue
loop_count = 0
while loop_count < max_loops_per_probe:
    constrain R >= vmax            # pin the drain at (a fraction of) its max
    LP-minimize  sum |v|           # pFBA-style; shrinks active support to a few rxns
    active = {rxn: dir for rxn with |flux| > tol}
    # rigorous minimum reaction SET (count, not flux):
    minimal = minimize_active_reactions(model, active_filter=active)
    # scan the perturbation space around this single loop:
    perturb = enumerate_alternative_reaction_sets(model, minimal.solution)
    strip ReactionUse variables/constraints      # between every loop
    record loop(probe, minimal, perturb)
    loop_count += 1
    block minimal set (integer cut: zero its reactions' used direction) ; re-LP-max R
    if new vmax <= tol: break       # no more distinct loops
remove probe ; restore any blocked bounds
```

### `minimize_active_reactions(model, objective=None, active_filter=None, ...)`
- LP-maximize the objective if not already pinned; LP-minimize total flux to get
  the candidate active set (or accept a precomputed `active_filter`).
- Introduce binaries **only** for active reactions:
  `model.pkgmgr.getpkg("ReactionUsePkg").build_package(active_filter)` builds
  `fu`/`ru` per reaction in the filter (exactly the
  `binary_check_gapfilling_solution` pattern, gapfillingpkg.py:634–655).
- Set objective to **minimize Σ(fu + ru)** over the filter (min reaction count,
  not min flux). Solve the MILP.
- Return `{reactions: [{id, direction, flux, reliability_score, is_core}], size,
  solution}` using `assign_reliability_scores_to_reactions` and `is_core`.
- Caller is responsible for stripping the ReactionUsePkg variables/constraints
  afterward; provide a helper that does so cleanly so the model is reusable.

### `enumerate_alternative_reaction_sets(model, solution, ...)`
- Generalize `MSModelUtil.analyze_minimal_reaction_set` (msmodelutl.py:1813):
  identify the active reaction set + the "initially zero" reactions, set a
  **minimal-deviation objective** (minimize flux through currently-zero
  reactions), then knock out each active reaction one at a time (zero the used
  direction's bound), re-optimize, and record per reaction:
  - `alternatives`: previously-zero reactions that now carry flux to replace it,
  - `coupled`: other active reactions that drop to zero with it,
  - `essential`: True if knockout makes the probe-constrained problem infeasible.
- Restore each bound after its knockout; restore the original objective at the
  end. Returns the per-reaction perturbation map plus the print-style record set
  (id, direction, flux, score, core, equation, alternatives, coupled, failed).

### Return schema (`find_flux_loops`)
A dict keyed by probe name; each value is a list of loop records (a clean probe
yields an empty list):
```
{
  "<probe_name>": [
    {
      "target_flux": <vmax>,
      "size": <int>,                      # minimal reaction count
      "reactions": [
        {
          "id", "direction_used", "flux", "equation",
          "reliability_score", "is_core",
          "alternatives": [[rxn_id, dir], ...],
          "coupled":      [[rxn_id, dir], ...],
          "essential":    <bool>
        }, ...
      ]
    }, ...
  ]
}
```
Framed as a defect list: a non-empty list for any probe is an EGC report. No
fix is prescribed (decided) — reporting loop + directions only.

### Parameters / defaults
- `compartment="c0"`, `tol=1e-6`, `flux_min_threshold` (probe lower bound when
  pinning), `fraction_of_optimum` for the pin (default pin at full `vmax`),
  `max_loops_per_probe` (default small, e.g. 5), `max_alternatives`.
- All thresholds match existing `ms_fba_utils` conventions (`1e-9`/`1e-6` are
  used in the ModelSEEDpy prior art; pick consistently and document).

### Confront resolutions (round 1) — concrete pins folded in

These resolve the binding stall points from the cross-family confront and are
authoritative over any looser wording above.

- **External APIs (stall 1, 7):** import from `modelseedpy.core.mstemplate`
  (`MSTemplate`, `MSTemplateReaction`); build cobra reactions via
  `MSTemplateReaction.to_reaction(cobra_model, index)`. Min-count binaries via
  `model.pkgmgr.getpkg("ReactionUsePkg").build_package(filter_dict)` exposing
  `fu`/`ru` binary variables; reliability scores via
  `MSModelUtil.assign_reliability_scores_to_reactions(rxn_list)`; core flag via
  `MSModelUtil.is_core(rxn)`. `find_reaction(stoichiometry)` is an existing
  `MSModelUtil` method (reachable via `_check_and_convert_model`) — not new.
  Document the relied-on ModelSEEDpy API names in a module docstring; do NOT
  hard-pin a version SHA in pyproject (KBUtilLib manages deps via
  `dependencies.yaml`).
- **Boundary/maintenance identification (stall 2):** after building the full
  model, treat any reaction whose id starts with `EX_`, `DM_`, `SK_`, or `bio`
  as boundary/maintenance and set `lb=ub=0`. All other reactions keep their
  template directionality. (A correctly built full template model should have
  none of these; this is a guard.)
- **Probe matching + ids (stall 3, 15):** match an existing reaction by exact
  metabolite ids in compartment `{compartment}` (default `c0`) with matching
  stoichiometric coefficients; direction is forward if signs match, reverse if
  inverted. If no match, create `cobra.Reaction` id `PROBE_<name>_<compartment>`
  (lb=0, ub=1000) with the `seed.reaction` annotation. A probe id must be unique
  in the model; if a custom-object probe's id already exists, reuse the existing
  reaction iff stoichiometry matches, else raise `ValueError`. Reference probes
  by id in objective strings (`MAX{<probe_id>}`).
- **Redox couples + balancing (stall 4):** v1 couples (reduced, oxidized) by
  base id — NADH cpd00004 / NAD cpd00003; NADPH cpd00005 / NADP cpd00006; plus
  FADH2/FAD, ferredoxin reduced/oxidized, ubiquinol/ubiquinone, GSH/GSSG,
  thioredoxin reduced/oxidized (resolve these remaining ids against the biochem
  DB; skip+warn any that do not resolve). Each probe drains
  `reduced -> oxidized + H2` in `{compartment}`, adding `+H+ (cpd00067)` on the
  product side as needed to satisfy charge per ModelSEED charges. No H2 sink is
  opened.
- **Mass sinks (stall 5):** exactly CO2 cpd00011, acetate cpd00029, formate
  cpd00047, NH3 cpd00013 in `{compartment}`; irreversible `<met> ->`. No "and
  similar" — no additional metabolites in v1.
- **Thresholds (stall 6):** `tol=1e-6` for activity (`|flux|>tol` ⇒ active);
  treat `|flux|<=1e-9` as zero; pin `fraction_of_optimum=1.0` unless overridden;
  `flux_min_threshold` defaults to `tol`. A probe `vmax<=tol` ⇒ no loop.
- **Minimal-deviation objective (stall 8):** in
  `enumerate_alternative_reaction_sets`, objective = minimize
  `Σ (forward_var_i + reverse_var_i)` over reactions currently at `|v|<=tol`.
  Report each reaction's equation via
  `rxn.build_reaction_string(use_metabolite_names=True)`; populate score/core via
  `assign_reliability_scores_to_reactions` / `is_core` over the union set.
- **Integer-cut blocking (stall 9):** to block a found minimal set, for each of
  its reactions set `lb=0` if used forward (`v>tol`) or `ub=0` if used reverse
  (`v<-tol`), leaving the opposite bound unchanged; record every bound change and
  restore it when the probe is removed.
- **Return schema is frozen (stall 10):** the keys listed in the Return schema
  are exhaustive — no additional keys in v1. Types: `flux` float,
  `reliability_score` float, `is_core`/`essential` bool, ids/directions strings.
- **Support cap (stall 11):** if the pFBA-minimized support for a probe exceeds
  **500** reactions, build the ReactionUse filter from the 500 reactions with the
  largest `|flux|` and `log_info` the cap (never silently truncate).
- **Cleanup contract (stall 14):** snapshot every per-reaction bound before any
  blocking. After each loop and at the end of each probe: strip ReactionUse via
  `model.pkgmgr.getpkg("ReactionUsePkg").clear()` if available, else remove
  `fu_`/`ru_` variables+constraints by name prefix through the optlang API;
  remove the probe reaction; restore all snapshotted bounds. Post-run the model
  must equal its pre-run state (no probe reactions, no leftover binaries).
- **Redox is provisional (stall 13):** the redox catalog ships in v1 behind a
  `enable_redox_probes=True` flag; tests and CI do not depend on a real template
  (they use cobra-model stand-ins, see Testing). The empirical-verification note
  (inspect redox results on a real template before finalizing the convention) is
  retained as guidance, not a build blocker.

## Testing Decisions

Test **external behavior**, not solver internals. Prior art for the toy-model
style: ModelSEEDpy `tests/core/test_msgapfill.py` and the existing KBUtilLib
FBA tests. **Fixtures use small hand-built `cobra.Model` stand-ins wrapped via
`MSModelUtil.get(model)`** rather than synthetic `MSTemplate` objects (stall
12) — the model carries the same per-reaction directionality the template path
would produce, so the loop-finding pipeline can be driven and asserted without a
template-fixture factory. `find_flux_loops` is structured so its core operates on
the built model, with `build_full_model_from_template` as the thin template→model
adapter tested separately/lightly. All four functions get tests (decided):

1. **`find_flux_loops` end-to-end** — a small closed `cobra.Model` stand-in
   containing exactly one known futile ATP cycle (a reversible reaction pair that
   regenerates ATP from ADP+Pi) plus one clean unrelated reaction. Assert
   `find_flux_loops` (driven on the built model) reports exactly that EGC for the
   ATP probe and an empty list once the bad reversibility is corrected. This is
   the headline behavioral test (no false positives on clean input).
2. **`minimize_active_reactions`** — a toy model whose provably-minimum active
   set for a pinned objective is known by hand (e.g. two parallel paths of
   different length); assert the returned set is exactly the shorter path
   (count-minimal), distinguishing it from a pFBA flux-minimal answer.
3. **`enumerate_alternative_reaction_sets`** — a toy model with two
   interchangeable reactions on a loop plus one essential reaction; assert the
   knockout scan reports each interchangeable reaction as the other's
   `alternative` and flags the essential reaction `essential=True`.
4. **`add_probe_reaction`** — assert a probe is added with correct stoichiometry
   and `seed.reaction` annotation; assert that when a matching reaction already
   exists in the model, it is reused (`new=False`) rather than duplicated.

After the audit, assert the model is unmodified (no leftover probe reactions,
no leftover ReactionUse variables/constraints).

## Out of Scope

- Diagnosing/prescribing the fix (which reaction's reversibility to change, dG-based
  culprit detection). v1 reports loop + directions only.
- Loopless/thermodynamic (ll-FBA, CycleFreeFlux) constraints — unnecessary here
  because the fully-closed system guarantees every solution is an internal cycle.
- Biological "real loop" enumeration with open substrates — v1 is the closed-system
  EGC audit only.
- Automatic repair of the template.
- A global min-count MILP over all template reactions (rejected for
  intractability; replaced by LP-support restriction + ReactionUse binaries on
  active reactions only).
- gene_reaction_rule / GPR handling, compartmentalization beyond the single
  cytosolic `c0` probe compartment.

## Further Notes

- **Prior-art generalization map** (the implementer should read these first):
  - `add_probe_reaction` ⟵ `MSModelUtil.add_atp_hydrolysis` (msmodelutl.py:2011)
  - `minimize_active_reactions` ⟵ `binary_check_gapfilling_solution` +
    `ReactionUsePkg` (gapfillingpkg.py:634–655, reactionusepkg.py)
  - `enumerate_alternative_reaction_sets` ⟵ `analyze_minimal_reaction_set`
    (msmodelutl.py:1813)
  - iterative block/re-solve enumeration ⟵ `unblock_objective_with_exchanges`
    (ms_fba_utils.py:271)
- **Empirical caveat on redox probes** (consistent with the "measure then
  design" principle): the H2-releasing redox drain only carries flux if the
  closed network can recycle H2 (e.g. via a hydrogenase-like reaction). On a
  real template this couples redox-loop detection to H2-interconverting
  reactions. Run the audit on a real ModelSEED template early and inspect the
  redox results before finalizing the redox probe convention; the catalog is a
  module constant precisely so it can be tuned after this measurement.
- The whole pipeline is LP-bound except the final small min-count MILP, so it
  must remain tractable on a full ~35k-reaction template. If even the
  LP-minimized support is large for some probe, cap the ReactionUse filter and
  log the cap rather than silently truncating.
- Driving consumer: the EnergyLoopAnalysis project
  (`subprojects/FindingCurrentLoops`). These utilities are the substrate that
  project's notebooks will call.

## Acceptance Criteria

1. All public functions (`find_flux_loops`, `build_full_model_from_template`, `add_probe_reaction`, `minimize_active_reactions`, `enumerate_alternative_reaction_sets`) are methods on `MSFBAUtils` in `src/kbutillib/ms_fba_utils.py`.
2. `find_flux_loops` accepts an `MSTemplate` object or a template id resolvable via the inherited `get_template()`, and an `objective` that is one of: `None`/`"all"`, a group name (`"atp"`/`"redox"`/`"mass"`), or a `cobra.Reaction` object.
3. External APIs are called exactly as: `MSTemplateReaction.to_reaction(cobra_model, index)`; `model.pkgmgr.getpkg("ReactionUsePkg").build_package(filter_dict)` with `fu`/`ru` binaries; `MSModelUtil.assign_reliability_scores_to_reactions`; `MSModelUtil.is_core`; `MSModelUtil.find_reaction(stoichiometry)`. No ModelSEEDpy version SHA is hard-pinned in pyproject.
4. `build_full_model_from_template` produces a `cobra.Model` containing every template reaction with template directionality preserved, and any reaction whose id starts with `EX_`, `DM_`, `SK_`, or `bio` has `lb=ub=0`.
5. `add_probe_reaction` reuses an existing reaction when metabolite ids in the target compartment and stoichiometric coefficients match (returning `new=False`), and otherwise creates a reaction with id `PROBE_<name>_<compartment>` (lb=0, ub=1000) and the `seed.reaction` annotation; it never duplicates a matching reaction.
6. A custom-object probe whose id collides with an existing reaction is reused iff stoichiometry matches, else raises `ValueError`.
7. The `atp` probe is `cpd00002 + cpd00001 -> cpd00008 + cpd00009 + cpd00067`.
8. The `redox` group contains one balanced `reduced -> oxidized + H2 (+ cpd00067 as needed for charge)` drain per resolvable couple from the v1 list (NADH/NAD cpd00004/cpd00003, NADPH/NADP cpd00005/cpd00006, FADH2/FAD, ferredoxin red/ox, ubiquinol/ubiquinone, GSH/GSSG, thioredoxin red/ox); couples with unresolvable ids are skipped with a logged warning; no H2 sink is opened.
9. The `mass` group contains exactly irreversible sinks for cpd00011, cpd00029, cpd00047, cpd00013 in the target compartment and no others.
10. Redox probes are gated behind an `enable_redox_probes` flag (default True) and no test or CI path depends on a real ModelSEED template.
11. Thresholds are: active iff `|flux|>1e-6`; zero iff `|flux|<=1e-9`; probe `vmax<=1e-6` yields no loop; pin `fraction_of_optimum` default `1.0`; `flux_min_threshold` default `1e-6`.
12. `minimize_active_reactions` introduces `fu`/`ru` binaries only for reactions in the active filter, minimizes `Σ(fu+ru)`, and returns the count-minimal active set with per-reaction `reliability_score` and `is_core`; on a toy with a short and a long parallel path it returns the short path (distinguishable from a pFBA flux-minimal answer).
13. If a probe's pFBA-minimized support exceeds 500 reactions, the ReactionUse filter is built from the 500 largest-`|flux|` reactions and the cap is logged (no silent truncation).
14. `enumerate_alternative_reaction_sets` uses the objective `minimize Σ(forward_var_i + reverse_var_i)` over reactions at `|v|<=1e-6`, and for each active reaction reports `alternatives`, `coupled`, and `essential` (knockout → infeasible ⇒ `essential=True`); on a toy with two interchangeable reactions each is reported as the other's alternative and the essential reaction is flagged essential.
15. Integer-cut blocking of a minimal set sets `lb=0` for forward-used and `ub=0` for reverse-used reactions (opposite bound unchanged), and these are restored when the probe is removed.
16. `find_flux_loops` returns a dict keyed by probe name; each value is a list of loop records with exactly the documented keys and types and no additional keys; a clean input yields an empty list per probe.
17. After `find_flux_loops` returns, the input model is byte-for-byte equivalent to its pre-call state: no probe reactions remain, no `fu_`/`ru_` variables or constraints remain, and all temporarily blocked bounds are restored from the pre-run snapshot.
18. `find_flux_loops` reports exactly the planted EGC on the ATP-cycle stand-in fixture and an empty list once the bad reversibility is corrected.
19. `add_probe_reaction`, `minimize_active_reactions`, and `enumerate_alternative_reaction_sets` each have a passing behavioral test as described in Testing Decisions, and the full new test set passes under the repo's standard test command.

### Non-binding refinements (from confront free critique — adopt if cheap)

- Dedup near-duplicate enumerated loops by hashing the (reaction-id, direction)
  set before recording, so tiny variations of the same loop don't inflate the
  report.
- Emit performance logging (per-probe LP/MILP timings and ReactionUse filter
  size) to help tune the 500-reaction cap on real ~35k-reaction templates.
- Add a specificity test: an ATP probe must yield zero on a model whose only
  cycles are balanced futile cycles that do NOT regenerate ATP (guards against
  the probe firing on energy-neutral loops).
- If the H2-releasing redox drain proves too dependent on hydrogenase presence
  on real templates, a proton/electron pseudo-metabolite drain is the documented
  fallback convention (revisit under the redox empirical caveat).
