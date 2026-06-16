# Work Record: tes-p1-fba-tests-biolog-stash

## task_id
tes-p1-fba-tests-biolog-stash

## branch
tes-p1-fba-tests-biolog-stash

## commit_shas
- 6b74aaae70dc13c7c6bbbadfde20d2b450521a3d

## summary
Implements Phase 1 of the Template Evaluation Suite PRD. Seven new methods were added to `MSFBAUtils` in `src/kbutillib/ms_fba_utils.py`: `classify_reactions_by_fva`, `find_closed_mode_reactions`, `get_biolog_phenotypes`, `refresh_biolog_phenotypes`, `simulate_biolog`, `test_production_potential`, and `test_degradation_potential`. A real Biolog phenotype stash (`src/kbutillib/data/biolog_phenotypes.json`) was generated from the live KBase KBaseMedia workspace and committed — it contains 468 phenotypes across 4 element panels (C: 168, N: 85, S: 169, P: 46), each with `target_element`, `primary_compounds`, and `base_media` set. A `data/__init__.py` was added to make `kbutillib.data` importable via `importlib.resources`. 33 offline pytest tests cover all 7 functions and the stash round-trip; they run without KBase credentials and pass in 1.65s.

## files_touched
- `src/kbutillib/ms_fba_utils.py` — 7 new methods appended to `MSFBAUtils` class (~360 new lines)
- `src/kbutillib/data/__init__.py` — new file, makes data directory a Python package for importlib.resources
- `src/kbutillib/data/biolog_phenotypes.json` — new 1.8MB file, Biolog phenotype stash from live KBase
- `tests/test_ms_fba_utils_eval.py` — new file, 33 offline pytest tests

## success_criteria_check

- **All 7 functions present in MSFBAUtils**: PASS — `classify_reactions_by_fva`, `find_closed_mode_reactions`, `get_biolog_phenotypes`, `refresh_biolog_phenotypes`, `simulate_biolog`, `test_production_potential`, `test_degradation_potential` all added to the class.
- **Uses `run_fva` (not cobra.flux_variability_analysis)**: PASS — all FVA calls use `self.run_fva(mdlutl, ...)` exclusively; `cobra.flux_variability_analysis` is not called anywhere.
- **`test_production_potential` and `test_degradation_potential` leave the model unchanged**: PASS — both methods use `with cobra_model:` context manager which guarantees reaction cleanup on exit; verified by `test_bounds_unchanged_after_sweep` and `test_model_unchanged_after_classify` tests.
- **`biolog_phenotypes.json` committed with 4 C/N/S/P sets from live KBase**: PASS — file generated from live KBaseMedia workspace (kbaseprivate workspace) via `kbase_api.list_objects` + `kbase_api.get_object`; contains C:168, N:85, S:169, P:46 phenotypes; MSGrowthPhenotypes serialization confirmed by round-trip test.
- **Offline pytest passes**: PASS — 33/33 tests pass in 1.65s with no KBase credentials; existing 16 tests in `test_find_flux_loops.py` also unaffected (49 total pass).

## tests_run

```
PYTHONPATH=<worktree>/src:/Dropbox/Projects/ModelSEEDpy:/Dropbox/Projects/cobrakbase:/Dropbox/Projects/cb_annotation_ontology_api:/Dropbox/Projects/KBUtilLib/src \
  /Users/chenry/VirtualEnvironments/kbu.nb-modelingloe-py3.11/bin/python3 \
  -m pytest tests/test_find_flux_loops.py tests/test_ms_fba_utils_eval.py -v --tb=short
```

Result: **49 passed, 8 warnings in 1.63s** (8 warnings are pre-existing solver infeasibility warnings from `test_find_flux_loops.py`).

## caveats

- `refresh_biolog_phenotypes` requires `self.kbase_api` (a live cobrakbase `KBaseAPI` instance with a valid token). It cannot be exercised offline; there is no offline test for it. This is by design — the stash it writes is committed as a static artifact for offline use.
- `simulate_biolog` calls `self.set_media()` and `self.set_objective_from_string()` — both are inherited KBModelUtils methods. The offline test for it is limited to verifying the result structure (growth/no-growth dict with bio1/bio2 keys) using a patched `slim_optimize`. A full end-to-end integration test requires a real KBase-loaded model and was excluded from the offline suite.
- The `classify_reactions_by_fva` essential-reaction detection relies on `objective=f"MAX{{{bio_id}}}"` syntax for `run_fva`. This was validated against the existing `run_fva` implementation (which parses MAX/MIN prefix plus reaction id). If `run_fva` objective parsing changes, this will silently fall back to default objective.
- The Biolog stash was generated against the `kbaseprivate` workspace on KBase using the production Dropbox KBUtilLib environment (not the worktree), because the worktree can't resolve relative paths for `cb_annotation_ontology_api` and `ModelSEEDDatabase` without the full config. The stash content is deterministic for the current KBase state.
