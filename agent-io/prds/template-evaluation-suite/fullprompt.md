# PRD: Template Evaluation Suite

## Problem Statement

Energy-loop work (ModelingLOE and related metabolic-reconstruction efforts) requires
curating ModelSEED **templates** — adding, removing, or changing reactions — and knowing
the functional consequences of each edit. Today there is no systematic way to ask "is
this template healthy?" or "what did this template change actually do?" A curator can
build a model and eyeball it, but cannot get a reproducible, quantitative picture of dead
reactions, essential reactions, directionality, growth capability across Biolog nutrient
panels, biosynthetic production potential, or degradation capability — let alone a
before/after diff attributing observed changes to specific template edits.

## Solution

A suite of utility functions that (1) evaluate the metabolic quality of a template by
building a full template model and running a battery of FBA/FVA-based tests, (2) emit a
single structured report covering every quality dimension, and (3) diff two evaluations to
link each template perturbation to the functional changes it caused.

The reusable per-model test functions live in `ms_fba_util` (they are independently useful
on any model, not just full template models). A new `ms_template_util` module provides the
template-centric orchestration: build the full template model, run the suite, render the
report, and run the perturbation diff.

## User Stories

1. As a template curator, I want to build a full template model from any template with both
   gram-positive and gram-negative biomass reactions present, so that I can evaluate a
   template's behavior for both cell-wall types in one model.
2. As a curator, I want to classify every reaction in a model as dead, essential,
   forward-only, reverse-only, or reversible, in both rich and minimal media, so that I can
   see the directional and feasibility structure of the network.
3. As a curator, I want "dead" reactions identified under unconstrained conditions (growth
   not forced), so that a reaction is only called dead when it is genuinely blocked, not
   merely unused at the optimum.
4. As a curator, I want "essential" reactions identified as those forced to carry nonzero
   flux when growth is held at 20% of optimum, so that I get the robustly-required core
   without the over-prediction that 100%-optimum forcing produces.
5. As a curator, I want to set all exchange fluxes to zero and run FVA, so that I can find
   reactions that can still carry flux in a closed system (closed-mode reactions), which
   usually indicates thermodynamically-infeasible loops.
6. As a curator, I want to test growth across all Carbon-, Nitrogen-, Sulfate-, and
   Phosphate- Biolog media formulations from the KBaseMedia workspace, so that I can see
   which nutrient sources the template can support growth on.
7. As a curator, I want the Biolog media stashed locally so that repeated evaluations do
   not require KBase access and are reproducible offline.
8. As a curator, I want to assess production potential by attempting to drain (produce)
   every cytosolic metabolite one at a time in complete and glucose-minimal media, so that
   I know which compounds the network can synthesize.
9. As a curator, I want to assess degradation capability by attempting to source (and have
   the network consume) every cytosolic metabolite one at a time in complete media, so that
   I know which compounds the network can break down.
10. As a curator, I want all of the above assembled into one structured, serializable report
    (with per-media breakdowns and counts), so that I can store it, compare it, and render
    it to markdown.
11. As a curator, I want to perturb a template (add/remove/change a reaction) and re-run the
    evaluation, so that I can measure the functional impact of a candidate edit.
12. As a curator, I want each perturbation linked to the specific changes it caused across
    every report category (reactions newly dead/essential, growth gained/lost on Biolog
    media, metabolites newly producible/consumable, etc.), so that I can decide whether the
    edit is desirable.
13. As a curator with multiple candidate edits, I want to attribute changes either to each
    edit independently (each vs the shared baseline) or cumulatively (each vs the previous
    state), so that I can isolate single-edit effects or study interactions.
14. As a developer of the platform, I want each test function to take a model as input and
    return a structured result, so that the tests compose and can be reused on
    gene-derived models, not only full template models.

## Implementation Decisions

### Module placement
- **`ms_fba_util`** (existing, `MSFBAUtils(KBModelUtils)`): gains the seven reusable
  per-model test functions plus the Biolog stash functions. These take a model (cobra
  Model or MSModelUtil — reuse the existing `_check_and_convert_model` convention) and
  return structured dict/list results.
- **`ms_template_util`** (new module, `MSTemplateUtils(KBModelUtils)`): template-centric
  orchestration — full-template-model construction, the `evaluate_template_quality`
  orchestrator, the markdown report renderer, and `diff_template_evaluation`. Register the
  new module following the existing module wiring pattern (`__init__.py`, `toolkit.py`,
  `dependency_manager.py`) — match how `ms_fba_utils` is wired.
- **`ms_reconstruction_util` is NOT touched** — it builds models from genomes; it does not
  evaluate them.

### Build on existing prior art (do not reinvent)
- `MSBuilder.build_full_template_model(template, model_id, index)` already instantiates
  every template reaction into an `MSModel`, adds exchanges, builds all `template.biomasses`
  reactions, sets `bio1` as objective, and adds standard sinks. Wrap it.
- `MSFBAUtils.run_fva(model, media, objective, fraction_of_optimum)` is the working FVA
  (cobra's `flux_variability_analysis` is broken — do NOT use cobra's). It returns
  `{rxn_id: {"MIN": float, "MAX": float}}` via per-reaction `slim_optimize`. All
  classification derives from it.
- `MSGrowthPhenotypes` / `MSGrowthPhenotype` (modelseedpy) drive Biolog simulation. The
  `MSGrowthPhenotype` supports `base_media`, `primary_compounds`, `target_element`, and
  `target_element_limit`, which together drive `ElementUptakePkg` to cap total uptake of
  the target element so the test compound is the sole source. `simulate_phenotypes` runs a
  whole set; `to_dict`/`from_dict` serialize.
- `MSModelUtil.add_exchanges_for_metabolites(metabolites, ...)` adds drain reactions for a
  metabolite list — the basis for production/degradation and closed-mode tests.

### `build_full_template_model(template, auto_add_biomass=True)` — biomass auto-detection
- Generic over any template. Wrap `MSBuilder.build_full_template_model`, which builds every
  biomass present in `template.biomasses`.
- Auto-detect cell-wall coverage: if the template carries both a gram-positive and a
  gram-negative biomass, build both. If only one is present and `auto_add_biomass` is True,
  graft the missing biomass from the corresponding standard ModelSEED template
  (GramPos/GramNeg), so a single model can be tested for both cell-wall types.
- Identify GP vs GN biomass by the biomass id/name convention used in the standard
  templates (resolve at build time from the loaded templates rather than hard-coding).

### Reaction classification (`classify_reactions_by_fva`)
- Two FVA passes per media, using `run_fva`:
  - **Unconstrained pass** (`fraction_of_optimum=0`, growth not forced): yields the
    full feasible flux cone. Classify:
    - `dead`: |MIN| <= tol AND |MAX| <= tol
    - `forward_only`: MIN >= -tol AND MAX > tol
    - `reverse_only`: MAX <= tol AND MIN < -tol
    - `reversible`: MIN < -tol AND MAX > tol
  - **Growth-forced pass** (`fraction_of_optimum=0.2`, biomass objective): yields
    `essential` = reactions where 0 is not within [MIN, MAX] (i.e. forced nonzero).
    20% (not 100%) is deliberate to avoid over-predicting essential reactions.
- Default flux tolerance `tol = 1e-7`. Returns a dict of category → list of reaction ids.
- Run by the orchestrator in BOTH rich and minimal media; the report carries per-media
  results.

### Closed-mode reactions (`find_closed_mode_reactions`)
- Set all exchange/drain reaction bounds to zero (closed system), run `run_fva`
  unconstrained, return reactions whose feasible range exceeds tolerance (can still carry
  flux with no exchange) — these are typically infeasible internal loops. Restore bounds
  afterward (operate on a model copy or save/restore bounds).

### Biolog stash (`get_biolog_phenotypes`, `refresh_biolog_phenotypes`, `simulate_biolog`)
- Stash = four `MSGrowthPhenotypes` sets (C, N, S, P), each `target_element`-constrained
  with `primary_compounds` set to the varying source and a shared `base_media` supplying
  everything else.
- `refresh_biolog_phenotypes(workspace="KBaseMedia")`: enumerate the `Carbon-*`,
  `Nitrogen-*`, `Sulfate-*`, and `Phosphate-*` media objects in the KBaseMedia workspace
  (via `kb_ws_utils`), extract each differentiating compound as the primary compound,
  build the four phenotype sets, and serialize all four to a single JSON committed in the
  package data dir. Requires KBase auth; run on demand, not at simulation time.
- `get_biolog_phenotypes(element=None)`: load the committed stash (all four, or one by
  element) via `MSGrowthPhenotypes.from_dict` — no KBase auth required.
- `simulate_biolog(model, elements=("C","N","S","P"), growth_threshold=0.01)`: run
  `simulate_phenotypes` for the requested sets, return functional (growing) media per
  element.

### Production / degradation sweeps
- Scope: cytosolic (`_c`) metabolites only (extracellular compounds are trivially
  producible/consumable via their exchanges; testing them adds noise).
- Pure capability — growth is NOT required during these tests.
- `test_production_potential(model, media, threshold=1e-6)`: for each cytosolic metabolite,
  add a temporary demand reaction (cpd ->), maximize it, record producible if max flux >
  threshold; remove the temporary reaction. Run in complete AND glucose-minimal media (the
  orchestrator calls it once per media).
- `test_degradation_potential(model, media, threshold=1e-6)`: for each cytosolic
  metabolite, add a temporary source reaction (-> cpd), maximize it, record consumable if
  max flux > threshold; remove the temporary reaction. Run in complete media.
- Add then remove temporary reactions per metabolite (mirror the cleanup pattern in
  `unblock_objective_with_exchanges`), or batch-add then batch-remove for speed, but the
  model must be returned to its input state.

### Media resolution
- `rich`/"complete" and `minimal`/"glucose-minimal" media are resolved via the existing
  `get_media` path used by `set_media`. "complete" uses the standard ModelSEED complete
  media; "glucose-minimal" resolves to a minimal media with D-glucose as the carbon source
  (use a named KBaseMedia object if one exists, else construct minimal + D-glucose). Make
  the media identifiers parameters of `evaluate_template_quality` with these defaults.

### `evaluate_template_quality(template, rich_media="complete", minimal_media="glucose-minimal", write_path=None)`
- Orchestrator: build the full template model; run `classify_reactions_by_fva` in rich and
  minimal; run `find_closed_mode_reactions`; run `simulate_biolog`; run
  `test_production_potential` in complete + glucose-minimal; run
  `test_degradation_potential` in complete. Assemble one JSON-serializable report dict.
- Report structure (keys): template metadata (id, biomass ids built, media used,
  timestamp); per-media `dead`/`essential`/`forward_only`/`reverse_only`/`reversible`
  reaction lists for rich and minimal; `closed_mode_reactions`; `functional_biolog_media`
  per element; `producible_metabolites` for complete and glucose-minimal;
  `consumable_metabolites` for complete; counts alongside every list.
- If `write_path` is given, write `report.json`; the renderer can also write a `.md`.

### `render_template_report(report) -> str`
- Pure function: structured report dict in, markdown string out (sectioned, with counts and
  the member lists). No recomputation.

### `diff_template_evaluation(model, perturbations, mode="independent", baseline_report=None, write_path=None)`
- Perturbations are applied at the **model level** (direct edit of the built model — toggle
  bounds, add/remove/modify the cobra reaction), NOT by rebuilding from the template. Build
  the baseline model once from the template, evaluate it (or accept a provided
  `baseline_report`).
- A perturbation spec describes one edit: `{op: "add"|"remove"|"modify", reaction_id,
  ...attributes}` (bounds/stoichiometry for add/modify). Accept a list.
- `mode="independent"` (default): apply each perturbation ALONE to a fresh copy of the
  baseline model, evaluate, diff against the baseline report. Each perturbation gets an
  isolated change report.
- `mode="cumulative"`: apply perturbations in sequence on one evolving model copy, diffing
  each step against the previous.
- Diffing: for every report category, compute added/removed members (set difference) and
  growth gained/lost; attach the per-category deltas to the perturbation that caused them.
- Output: a diff report linking each perturbation to all observed changes; optionally
  written to `write_path`.

### Confront-resolved specifications (round 1, cross-family codex attack, 2026-06-16)

These concrete values were added after a GPT-5/codex confront pass that found 15 points
where an autonomous build would have to guess. Each is now pinned:

1. **Module/class names.** Create `src/kbutillib/ms_template_utils.py` with
   `MSTemplateUtils(KBModelUtils)` and a `MSTemplateUtilsImpl` mirroring the existing
   `MSFBAUtils` / `MSFBAUtilsImpl` pair. Expose as `KBUtilLib.template` in `toolkit.py`
   and add the optional import in `__init__.py` under the name `MSTemplateUtils`.
2. **`build_full_template_model`.** Use `modelseedpy.MSBuilder.build_full_template_model(
   template, model_id, index)` — this method exists in modelseedpy's `MSBuilder` (the
   confront agent only saw KBUtilLib's local `MSBuilder` reference and missed it). It builds
   every biomass in `template.biomasses` and sets `bio1` as objective. Detect gram-positive
   vs gram-negative biomass by the id/name convention in the loaded standard templates
   (resolve at build time, do not hard-code); if only one cell-wall type is present and
   `auto_add_biomass=True`, graft the missing biomass from the corresponding standard
   ModelSEED template (GramPos/GramNeg, V6).
3. **Media defaults.** `rich_media="KBaseMedia/Complete"`, `minimal_media="KBaseMedia/Carbon-D-Glucose"`,
   both as parameters of `evaluate_template_quality`, resolved via the existing `get_media`
   path. (These are real KBaseMedia workspace objects — author-confirmed.)
4. **Essentiality + Biolog objective when both biomasses are built.** Run the growth-forced
   FVA pass (fraction 0.2) and the Biolog simulation **once per biomass separately**
   (`MAX{bio1}`, then `MAX{bio2}` if present). Report essential reactions and functional
   Biolog media **per biomass (per cell-wall type) AND as the union**. Do NOT sum the
   biomasses into one objective — a GP-only or GN-only essential reaction must not be masked
   by slack from the other biomass. The unconstrained pass (dead + directionality),
   closed-mode, production, and degradation tests force no growth and run once (biomass-
   independent).
5. **Closed-mode reaction set.** Zero-bound all reactions whose ids start with `EX_`, `DM_`,
   or `SK_` (and any further drains found via `MSModelUtil.exchange_hash()`); leave `ATPM`
   and biomass reactions unconstrained. Restore bounds afterward.
6. **Stash location.** Write the committed stash to `src/kbutillib/data/biolog_phenotypes.json`;
   load via `importlib.resources`. `refresh_biolog_phenotypes` matches workspace `KBaseMedia`,
   media names starting `Carbon-`, `Nitrogen-`, `Sulfate-`, `Phosphate-` (case-sensitive).
7. **Cytosolic scope + cleanup.** Treat metabolite ids ending `_c` or `_c0` as cytosolic
   (normalize via `KBModelUtils._parse_id`). Add temporary reactions prefixed `DM_tmp_`
   (production) and `SK_tmp_` (degradation) and remove them after each metabolite using a
   cobra context manager (`with model.model:`) to guarantee no leaked reactions.
8. **Perturbation attribute schema.** `{op, reaction_id, ...}` where for `add`/`modify`:
   `lower_bound` (float), `upper_bound` (float), `stoichiometry` ({met_id: coeff} dict); for
   `modify`, omitted keys mean "no change"; `remove` needs only `reaction_id`. Normalize
   metabolite ids to model ids on add/modify.
9. **Canonical report keys.** Top level: `template_metadata`; `reaction_classes` (per media
   `rich`/`minimal`, each with `dead`, `forward_only`, `reverse_only`, `reversible`, and
   `essential` keyed per biomass plus `union`); `closed_mode_reactions`;
   `functional_biolog_media` (per element, per biomass plus `union`); `producible_metabolites`
   (per media `complete`/`glucose_minimal`); `consumable_metabolites` (complete only). Every
   list is accompanied by a count. These keys are the stable contract the diff consumes.
10. **Test fixtures.** Bundle a small toy COBRA model JSON and a tiny template JSON under
    `tests/fixtures/`; tests must run offline with NO KBase auth (mock `get_media` /
    `MSGrowthPhenotypes` where a network object would otherwise be needed).
11. **Objective notation in sweeps.** Use `MAX{<temp_rxn_id>}` for each temporary drain/source
    during production/degradation; reset to the biomass objective after each iteration.
12. **Logging.** Use `self.log_info` in the Impl classes; gate verbose stage logging behind a
    `verbose: bool=False` parameter on the orchestrators.
13. **Growth threshold units.** Interpret growth as biomass-reaction flux in 1/h;
    `growth_threshold=0.01` (1/h).
14. **`write_path` semantics.** If `write_path` is a directory, write `<write_path>/report.json`
    and `<write_path>/report.md`; if a file stem, append `.json`/`.md`.

## Testing Decisions

Test external behavior against a small, fast, known model — prefer the core template model
(small) as the fixture so FVA/sweeps run quickly.

- **The 7 `ms_fba_util` tests** (highest value — deterministic primitives): on a small
  fixture model with hand-verified expectations, assert `classify_reactions_by_fva`
  partitions reactions correctly (a known blocked reaction lands in `dead`; a known
  growth-required reaction lands in `essential` at 0.2; directionality classes are
  mutually exclusive and exhaustive); `find_closed_mode_reactions` returns [] for a
  loop-free model and flags a deliberately-added futile cycle; `test_production_potential`
  finds a known producible compound and excludes a known non-producible one;
  `test_degradation_potential` likewise; assert the model is returned to its input bound
  state after each sweep (no leaked temporary reactions).
- **Biolog stash round-trip**: `get_biolog_phenotypes` loads the committed stash and
  `to_dict`/`from_dict` round-trips with no KBase auth; the four element sets are present
  and `target_element` is set correctly. Do NOT hit KBase in the test.
- **`evaluate_template_quality`** (integration): run on a small template; assert the report
  has all expected keys, per-media breakdowns, and that counts equal the lengths of their
  member lists.
- **`diff_template_evaluation`**: remove a known-essential reaction and assert the diff
  reports the expected growth loss / newly-dead reactions; verify both `independent` and
  `cumulative` modes; verify `independent` mode leaves the baseline model unmodified.

Prior art for test style: existing KBUtilLib tests under `tests/` (follow the conftest /
fixture conventions already present).

## Out of Scope

- True single-reaction-knockout essentiality (FVA forced-flux at 20% is the agreed
  definition).
- Template-level perturbation with full rebuild (perturbations are applied at model level).
- Gapfilling or template repair — this suite only *evaluates*; it does not fix.
- Any new template-building logic in `ms_reconstruction_util`.
- Multi-compartment production/degradation beyond cytosol (cytosol-only by decision).
- UI / dashboard surfacing of reports.

## Further Notes

- Phased delivery: Phase 1 = the seven `ms_fba_util` per-model tests + the Biolog stash
  (independently useful and fully testable). Phase 2 = `ms_template_util`
  (`build_full_template_model` wrapper, `evaluate_template_quality`, `render_template_report`,
  `diff_template_evaluation`), which depends on Phase 1.
- Performance: a full template model is thousands of reactions; FVA is O(reactions) LPs per
  pass and the metabolite sweeps are O(cytosolic metabolites) LPs. This is acceptable but
  the orchestrator should log progress per stage. Keep using `slim_optimize` (as `run_fva`
  does), not full `optimize`, in the sweeps.
- Reuse `_check_and_convert_model` so every per-model function accepts either a cobra Model
  or an MSModelUtil.
- The committed Biolog stash is reference data, version-controlled with the package; the
  `refresh_*` function is the only path that touches KBase.

## Acceptance Criteria

1. `src/kbutillib/ms_template_utils.py` exists defining `MSTemplateUtils(KBModelUtils)` (and a `MSTemplateUtilsImpl` mirroring `MSFBAUtils`/`MSFBAUtilsImpl`), wired into `toolkit.py` as `KBUtilLib.template` and importable from the package top level as `MSTemplateUtils`.
2. The seven per-model test functions (`classify_reactions_by_fva`, `find_closed_mode_reactions`, `get_biolog_phenotypes`, `refresh_biolog_phenotypes`, `simulate_biolog`, `test_production_potential`, `test_degradation_potential`) exist in `ms_fba_util`, each accepting a cobra Model or MSModelUtil via `_check_and_convert_model`.
3. `classify_reactions_by_fva` runs two passes via `run_fva`: an unconstrained pass (fraction_of_optimum=0) producing `dead` (|MIN|<=1e-7 and |MAX|<=1e-7), `forward_only`, `reverse_only`, `reversible`; and a growth-forced pass (fraction_of_optimum=0.2) producing `essential` (0 not within [MIN,MAX]). It never calls cobra's `flux_variability_analysis`.
4. When both biomasses are present, the growth-forced pass and Biolog simulation run once per biomass (`MAX{bio1}`, `MAX{bio2}`); essential reactions and functional Biolog media are reported per biomass and as a union. The biomasses are never summed into one objective.
5. `find_closed_mode_reactions` zero-bounds all `EX_`/`DM_`/`SK_` reactions (leaving `ATPM` and biomass unconstrained), runs unconstrained FVA, returns reactions still able to carry flux, and restores all bounds before returning.
6. `build_full_template_model` wraps `modelseedpy.MSBuilder.build_full_template_model`; if only one cell-wall biomass is present and `auto_add_biomass=True`, it grafts the missing GP/GN biomass from the corresponding standard ModelSEED V6 template.
7. The Biolog stash is committed at `src/kbutillib/data/biolog_phenotypes.json` as four `MSGrowthPhenotypes` sets (C/N/S/P), each `target_element`-constrained; `get_biolog_phenotypes(element=None)` loads it via `importlib.resources` with no KBase auth, and `to_dict`/`from_dict` round-trips it.
8. `refresh_biolog_phenotypes(workspace="KBaseMedia")` enumerates `Carbon-*`/`Nitrogen-*`/`Sulfate-*`/`Phosphate-*` media via `kb_ws_utils`, rebuilds the four sets, and rewrites the committed JSON.
9. `test_production_potential` and `test_degradation_potential` iterate cytosolic (`_c`/`_c0`) metabolites only, add temporary `DM_tmp_`/`SK_tmp_` reactions inside a cobra context manager, maximize with `MAX{<temp_rxn_id>}`, threshold flux at 1e-6, and leave the model's reactions and bounds unchanged after returning (no leaked temporary reactions).
10. Production runs in `KBaseMedia/Complete` and `KBaseMedia/Carbon-D-Glucose`; degradation runs in `KBaseMedia/Complete`.
11. `evaluate_template_quality(template, rich_media="KBaseMedia/Complete", minimal_media="KBaseMedia/Carbon-D-Glucose", write_path=None)` returns a JSON-serializable report dict with exactly the canonical keys: `template_metadata`, `reaction_classes` (per media `rich`/`minimal`, each with `dead`/`forward_only`/`reverse_only`/`reversible` and `essential` per-biomass+`union`), `closed_mode_reactions`, `functional_biolog_media` (per element, per-biomass+`union`), `producible_metabolites` (`complete`/`glucose_minimal`), `consumable_metabolites` (complete only); every list has an accompanying count.
12. When `write_path` is given: a directory yields `<write_path>/report.json` and `<write_path>/report.md`; a file stem yields `<stem>.json` and `<stem>.md`.
13. `render_template_report(report)` is a pure function returning markdown from the report dict with no recomputation.
14. `diff_template_evaluation(model, perturbations, mode="independent", baseline_report=None, write_path=None)` applies perturbations at the model level (per the schema in Implementation Decisions), supports `independent` (each vs shared baseline) and `cumulative` (each vs previous) modes, and returns a report linking each perturbation to per-category added/removed members and growth gained/lost. In `independent` mode the baseline model is left unmodified.
15. Tests cover: the seven `ms_fba_util` functions against bundled offline fixtures with hand-verified expectations (including no-leaked-reaction assertions); the Biolog stash round-trip with no KBase auth; `evaluate_template_quality` report structure and count==len(list) invariants; and `diff_template_evaluation` for a known-essential-reaction removal in both modes. No test requires KBase credentials.
16. The orchestrators log per-stage progress via `self.log_info`, gated behind a `verbose: bool=False` parameter.
