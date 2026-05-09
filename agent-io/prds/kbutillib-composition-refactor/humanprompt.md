# KBUtilLib Composition Refactor — Summary

Refactor KBUtilLib from a multi-inheritance mixin hierarchy (BaseUtils → SharedEnvUtils → KBWSUtils → KBCallbackUtils → KBAnnotationUtils → KBModelUtils → MSFBAUtils/MSReconstructionUtils, with diamond inheritance through MSBiochemUtils) to a flat composition architecture where each utility class holds a `SharedEnvUtils` instance and composes sibling utilities by reference. A `KBUtilLib` facade provides lazy-property access to every sub-utility (`kbu.fba`, `kbu.biochem`, `kbu.recon`, etc.), and `NotebookSession.kbu` wires it into the notebook engine. This is a single coordinated big-bang refactor, not a phased migration.

## Key constraints

- **Composition over inheritance**: every `*Impl` class holds `self.env: SharedEnvUtils`; logger via `self.env.logger`. KBJobUtils is the reference shape.
- **No back-compat constraint**: `NotebookUtil` god class, `util_legacy.py`, and the `_legacy` shim are all retired.
- **Short-sharp big-bang**: one coordinated PR (or tight 2-3 task chain landing within days).
- **AP3 carve-outs preserved**: `run_fva`, `analyzed_reaction_objective_coupling`, `fit_flux_to_mutant_growth_rate_data` in MSFBAUtils are deliberate implementations, not regressions.
- **Notebook transition out of scope**: archive-all + collaborative rebuild is a separate project in ADP1Notebooks.
- **Pre-flight smoke tests land first**: semantic invariants locked before the rewrite begins.
- **Flat-module extractions**: `kbase_endpoints.py` (already done), `model_directionality.py`, `model_helpers.py`, `compartments.py`.
- **Modules retired**: `notebook_utils.py`, `examples.py`, `kb_model_utils.py.bak` (already gone).
