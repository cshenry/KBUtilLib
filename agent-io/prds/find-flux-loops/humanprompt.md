# find_flux_loops — human summary

## What & why
Audit a ModelSEED `MSTemplate` for **energy-generating cycles (EGCs)** —
thermodynamically infeasible internal loops that regenerate ATP / reducing
equivalents / mass for free, caused by wrong reaction reversibilities. Because
the template seeds every reconstructed model, one such defect corrupts every
downstream model. Driving project: EnergyLoopAnalysis / FindingCurrentLoops.

## How it works
1. Build a fully-**closed** cobra model from the template (every reaction, its
   template directionality preserved, no exchanges/biomass/maintenance).
2. Add a balanced **probe** that drains a high-energy currency to its low-energy
   form (ATP hydrolysis; redox-cofactor drain releasing H2; mass sinks for
   CO2/acetate/formate/NH3/…), from a module-level catalog keyed by group
   (`atp` / `redox` / `mass` / `all`), or a custom reaction object.
3. LP-maximize the probe in the closed system. **Any** positive flux = a loop.
4. Shrink to the provably-**minimum reaction set** (LP-max → LP-min flux →
   ReactionUse binaries on active reactions only → minimize Σ binaries).
5. Scan the **perturbation space** (per-reaction knockout → alternatives /
   coupled / essential). Strip binaries between loops.
6. Return a structured **defect list**: per loop, the reactions + the direction
   each was used in, with reliability score and core flag. No fix prescribed.

## Four functions on `MSFBAUtils` (`ms_fba_utils.py`)
- `find_flux_loops(template, objective="all", ...)` — orchestrator / EGC report.
- `add_probe_reaction(model, probe)` — catalog probe builder (reuse-or-add).
- `minimize_active_reactions(model, ...)` — provably-minimum active set (MILP on
  active reactions only). Reusable beyond loops.
- `enumerate_alternative_reaction_sets(model, solution)` — per-reaction
  perturbation scan.

## Built by generalizing existing ModelSEEDpy code
- `add_probe_reaction` ⟵ `MSModelUtil.add_atp_hydrolysis`
- `minimize_active_reactions` ⟵ `binary_check_gapfilling_solution` + `ReactionUsePkg`
- `enumerate_alternative_reaction_sets` ⟵ `analyze_minimal_reaction_set`

## Key decisions
- Purpose: **QC EGC audit** (not biological enumeration).
- Input: **MSTemplate only**.
- Targets: **synthetic balanced probe reactions**, added (redox drains release H2).
- Scaling: **LP everywhere; binaries only on active reactions**, stripped between
  loops. No global MILP.
- Output: **loop + directions only**; no fix diagnosis.
- Tests: all four functions (toy models with known EGC / minimal set /
  alternatives), plus "clean template → empty" and "model unmodified after run".

## Open empirical item
The H2-releasing redox drain only fires if the network can recycle H2. Run the
audit on a real template early and inspect redox results before finalizing the
redox probe convention (catalog is a tunable module constant).
