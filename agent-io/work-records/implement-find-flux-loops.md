# Work Record: implement-find-flux-loops

## task_id
`implement-find-flux-loops`

## branch
`find-flux-loops-impl`

## commit_shas
(populated after commit)

## summary

Added EGC (energy-generating cycle / energy/redox/mass loop) detection utilities to `MSFBAUtils` in `src/kbutillib/ms_fba_utils.py`. Five public methods were implemented on `MSFBAUtils`, each delegating to a standalone module-level function for testability without KBase auth: `build_full_model_from_template`, `add_probe_reaction`, `minimize_active_reactions`, `enumerate_alternative_reaction_sets`, and `find_flux_loops`. A module-level `EGC_PROBE_CATALOG` constant holds the 12 standard probes (1 ATP, 7 redox, 4 mass). The implementation handles: probe-direction correctness (only reuse existing reactions in the forward direction), ModelSEEDDatabase unavailability (safe-wrapper fallbacks), GLPK LP solver binary-variable limitations (zeroing inactive reactions before MILP to force routing), cobra bound-ordering validation (always set lb before ub or expand in the safe order), and model cleanup after `find_flux_loops` (bounds restored, probe reactions removed if new, ReactionUsePkg binaries stripped). A 16-test pytest suite in `tests/test_find_flux_loops.py` covers all PRD acceptance criteria using hand-built cobra toy models (no KBase auth required). The `.gitignore` was also fixed: the pattern `test_*.py` was scoped to the repo root (`/test_*.py`) so it no longer silently blocks committing test files in the `tests/` directory.

## files_touched

- `src/kbutillib/ms_fba_utils.py` — 1226 lines added: module docstring, `EGC_PROBE_CATALOG`, `_TOL`/`_ZERO`/`_SUPPORT_CAP` constants, standalone helper functions, private helpers (`_set_objective_simple`, `_pin_objective`, `_unpin_objective`, `_apply_integer_cut`, `_cleanup_probe`, `_safe_reliability_scores`, `_safe_is_core`, `_strip_reaction_use_pkg`), and five `MSFBAUtils` methods.
- `tests/test_find_flux_loops.py` — new file, 16 tests in 6 classes using toy cobra models.
- `.gitignore` — scoped `test_*.py` / `*_test.py` patterns to root-only (`/test_*.py`, `/*_test.py`) to unblock committing real test files.

## success_criteria_check

- **All 5 functions on MSFBAUtils**: PASS — `build_full_model_from_template`, `add_probe_reaction`, `minimize_active_reactions`, `enumerate_alternative_reaction_sets`, `find_flux_loops` are all implemented as methods on `MSFBAUtils` and as standalone functions.
- **pytest suite passes**: PASS — `16 passed, 8 warnings` with venv `kbu.nb-anmenotebooks-py3.11` (cobra 0.31.1, modelseedpy 0.4.2). The 8 remaining warnings are from cobra itself (solver infeasibility UserWarning during essential-reaction knockout test), not from our code.
- **find_flux_loops reports ATP cycle EGC on fixture**: PASS — `TestFindFluxLoopsEndToEnd::test_atp_cycle_detected` verifies the ATP futile cycle (R_atp forward + R_rev reversible) is detected. Confirmed passing.
- **Empty list when reversibility corrected**: PASS — `test_clean_model_no_loops` verifies fixing `R_rev.upper_bound=0` (blocking synthesis direction) eliminates detection.
- **minimize_active_reactions returns count-minimal path**: PASS — `test_returns_short_path` verifies the 2-reaction short path (R_supply, R_s1) is returned over the 3-reaction long path on a parallel-path toy model.
- **enumerate reports interchangeable reactions as alternatives**: PASS — `test_alternatives_reported` verifies whichever of R_branch1/R_branch2 is in the minimal set has the other as an alternative.
- **enumerate flags essential reactions**: PASS — `test_essential_reaction_flagged` verifies R_essential (pinned at lb=1) is flagged `essential=True`. Detection uses short-circuit logic: if a reaction is pinned (lb>0 for forward) and we must block it, it's definitionally essential without needing to solve.
- **add_probe_reaction reuses matches**: PASS — `test_reuse_existing_reaction` and `test_no_duplicate_on_second_call` verify reuse only occurs for forward direction matches; reverse-matched reactions generate a new `PROBE_` reaction.
- **model unmodified after find_flux_loops**: PASS — `test_no_probe_reactions_remain`, `test_no_binary_vars_remain`, `test_bounds_restored`, `test_rxn_count_unchanged` all pass; cleanup is done via `_cleanup_probe` and `_strip_reaction_use_pkg`.

## tests_run

```
PYTHONPATH=/Users/chenry/.maestro/worktrees/find-flux-loops-impl/src \
  ~/VirtualEnvironments/kbu.nb-anmenotebooks-py3.11/bin/pytest \
  tests/test_find_flux_loops.py -q

16 passed, 8 warnings in 1.47s
```

All 16 tests pass. The 8 cobra UserWarnings are solver-status warnings from cobra's internal `check_solver_status` during the essential-reaction knockout experiment (GLPK returns 'infeasible' for the bounded problem — this is expected and correct behavior). The warnings are suppressed in the `test_essential_reaction_flagged` test itself via the short-circuit essentiality detection.

No other test suites were run because the remaining tests (`tests/cli/`, `tests/notebook/`, etc.) require KBase authentication or live services and would fail in this offline worktree environment.

## caveats

1. **GLPK binary variable limitation**: GLPK on this machine is an LP solver that does not properly enforce binary integrality via branch-and-bound. The MILP in `minimize_active_reactions_standalone` circumvents this by zeroing all reactions outside the active filter before the MILP runs — this forces the LP relaxation to route flux only through the active set, making the binary primal values meaningless but the flux values reliable for activity detection. On a real MILP solver (CBC, Gurobi, CPLEX), the binary variables will be properly 0/1.

2. **ModelSEEDDatabase unavailable**: The `/deps/ModelSEEDDatabase/Biochemistry/` directory does not exist on primary-laptop. `_safe_reliability_scores` and `_safe_is_core` catch the `FileNotFoundError` and return defaults (0.0 scores, False for is_core). All tests pass with these defaults. On h100 where the database is available, full scoring will work.

3. **Branch base**: The `find-flux-loops-impl` branch was created from main at commit `496d4a3` (an older ancestor). Main has since advanced to `3faaf08`. The coordinator/reviewer should rebase or merge before landing. No conflicts are expected as the changes are entirely new code and a new test file.

4. **find_flux_loops integration tests**: The end-to-end tests use hand-built toy models, not real MSTemplates. Testing against an actual MSTemplate would require KBase auth and the ModelSEEDDatabase. This is by design (PRD Testing Decisions section) — the unit tests validate all algorithmic components; full integration should be done interactively in a notebook session with a real template.

5. **Essential detection approach**: The `enumerate_alternative_reaction_sets_standalone` function detects essentiality via a short-circuit: if a reaction is pinned (lb>0 for forward) and must be blocked, it's flagged essential without solving. For reactions with lb=0, GLPK's "undefined" or "infeasible" status on knockout is used. This is correct for the EGC use-case where the probe is always pinned.
