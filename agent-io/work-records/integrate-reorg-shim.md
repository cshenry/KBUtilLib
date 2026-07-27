# Work Record — integrate-reorg-shim

## task_id
integrate-reorg-shim (Maestro developer sub-task on top of KBUtilLib reorg-integration branch `integrate-reorg`)

## branch
`integrate-reorg-shim` (worktree: `~/.maestro/worktrees/integrate-reorg-shim`, cut from `integrate-reorg` @ `cc08575f42a0f5d4dfc43b1d4bffa2a518e4c1e6`)

## commit_shas
- `fa13496d307c8c3e62f870fa65b59dfacd177713` — feat(shim): flat-submodule deprecation shims + silent-None guard

(single commit on top of base `cc08575f42a0f5d4dfc43b1d4bffa2a518e4c1e6`)

## summary

Implemented the flat-submodule deprecation shim and the silent-None guard called for
by `agent-io/prds/kbutillib-reorg-integration/fullprompt.md`. The shim is a table-driven
generator (`scripts/generate_deprecation_shims.py`, single source-of-truth
`LEGACY_MODULE_MAP`) that writes 38 real, thin `.py` modules directly under
`src/kbutillib/` — one per legacy flat `kbutillib.<x>_utils` (etc.) path the `domains/`
reorg deleted, covering the *entire* old→new mapping table from the PRD's Implementation
Decisions section (not just the 11-module minimum called out in the task prompt). Each
generated module executes `warnings.warn(..., DeprecationWarning, stacklevel=2)` once
(module bodies only run on first import) and then `from kbutillib.<new> import *`, which
binds — not copies — the new module's public names, so old and new import paths resolve
to identical objects. No `__getattr__`/meta-path mechanism was used, per the PRD's bound
decision. Also added `tests/guard/test_silent_none_reexports.py`, which walks the
class-shaped entries of `kbutillib.__init__.py`'s `__all__` (selected via the PascalCase
vs. snake_case naming convention that cleanly separates classes from the plain
functions/constants also in `__all__`) and asserts each resolves non-`None`, closing the
gap where a missed re-export in the `try/except ImportError → None` pattern would
otherwise degrade silently to a confusing `NoneType` error downstream instead of failing
the build. `tests/core/test_deprecation_shims.py` is the load-bearing safety-net test for
the shim itself: for every one of the 38 legacy paths it asserts the import succeeds, emits
a `DeprecationWarning`, and every re-exported public name is object-identical (`is`) to the
new `domains.*` module's — plus an explicit `MSFBAUtils` identity spot-check and a
`-W error::DeprecationWarning` promotion check, matching the task's success criteria
verbatim.

## files_touched

- `scripts/generate_deprecation_shims.py` (new) — the single old→new mapping table
  (`LEGACY_MODULE_MAP`) plus the codegen that writes the 38 shim files. Re-run this script
  to regenerate the shims if the mapping ever changes.
- `src/kbutillib/{ai_curation_utils,annotator_utils,argo_utils,base_utils,bvbrc_utils,
  dependency_manager,dram2_utils,escher_utils,kb_annotation_utils,kb_berdl_utils,
  kb_callback_utils,kb_genome_utils,kb_model_utils,kb_narrative_audit,kb_plm_utils,
  kb_reads_utils,kb_sdk_utils,kb_uniprot_utils,kb_ws_utils,kbase_catalog_client,
  kbase_endpoints,king_install,mmseqs_utils,model_directionality,model_helpers,
  model_standardization_utils,ms_biochem_utils,ms_fba_utils,ms_reconstruction_utils,
  ms_template_utils,ontomap_utils,patric_ws_utils,prokka_utils,rcsb_pdb_utils,
  shared_env_utils,skani_utils,thermo_utils,transyt_utils}.py` (new, 38 files) — the
  generated deprecation-shim modules.
- `tests/core/test_deprecation_shims.py` (new) — table-driven shim tests (parametrized
  over all 38 legacy paths: import succeeds + warns + identity), plus a representative
  `MSFBAUtils` identity test and a `-W error` promotion test.
- `tests/guard/test_silent_none_reexports.py` (new) — the silent-None guard test over
  `kbutillib.__all__`'s class-shaped entries.

No files outside these paths were touched — the sibling task's scope
(`src/kbutillib/beril/skills`, `src/kbutillib/harness/skills`, `src/kbutillib/king_app`,
`agent-io/reports/consumer-migration.md`) was left untouched, confirmed by `git status`
before committing.

## success_criteria_check

1. `PYTHONPATH=src python3 -W error::DeprecationWarning -c 'from kbutillib.ms_fba_utils
   import MSFBAUtils'` exits non-zero — **PASS**. Verified: raises `DeprecationWarning`
   traceback, `echo $?` → `1`.
2. Without `-W error`, `from kbutillib.ms_fba_utils import MSFBAUtils` returns the SAME
   object (`is`) as `kbutillib.domains.modeling.ms_fba_utils.MSFBAUtils` — **PASS**.
   Verified directly (`is: True`), and additionally verified programmatically for all 38
   shims (import + warn + identity on every re-exported public name), not just
   `ms_fba_utils`.
3. The `__all__`-driven guard test passes — **PASS**. `tests/guard/test_silent_none_reexports.py`
   (3 tests) passes; separately confirmed all 75 class-shaped `kbutillib.__all__` entries
   resolve non-`None` in this environment.
4. `pytest tests/core tests/guard` is green — **UNCERTAIN / PARTIAL PASS with documented
   pre-existing failures**. The run is `5 failed, 392 passed, 9 skipped, 1 xfailed, 3 errors`.
   All 84 of my new tests (`test_deprecation_shims.py` + `test_silent_none_reexports.py`)
   pass. The 5 failures / 3 errors are **pre-existing** on the unmodified base branch
   `integrate-reorg` @ `cc08575` — I ran the identical `pytest tests/core tests/guard -q`
   in the sibling worktree `~/.maestro/worktrees/integrate-reorg` (base commit, no shim
   changes) and got the exact same 5 failed / 3 errors / 308 passed. My worktree's 392
   passed = 308 (base) + 84 (new), i.e. purely additive — nothing that passed on the base
   now fails. Root causes of the pre-existing failures, none related to this task's scope:
   - `tests/core/test_composition_smoke.py::TestMSBiochemUtils::*` (3 errors) and
     `TestThermoUtils::test_get_compound_deltag_returns_float_or_none` (1 failure):
     `ModelSEEDBiochem.get()` tries to load `.../src/ModelSEEDDatabase/Biochemistry/`,
     which does not exist in this worktree/environment — a missing local data
     dependency, not a code defect.
   - `TestNotebookSessionKbu::test_notebook_session_kbu_returns_facade` (1 failure):
     `src/kbutillib/domains/notebook/session.py:157` does `from ..toolkit import
     KBUtilLib`, which resolves to the nonexistent `kbutillib.domains.toolkit` (toolkit.py
     stayed top-level per the PRD's mapping) — a pre-existing relative-import bug in the
     reorg base itself, outside this task's shim/guard scope.
   - `tests/core/test_cli_cap.py::TestCapList::{test_json_output_is_valid,
     test_json_output_has_required_keys,test_json_filter_by_tag}` (3 failures): CLI `kbu
     cap list --json` output issue, pre-existing on the base, unrelated to shims.
   I did not fix these — they are outside the assigned scope (shim modules + tests only)
   and fixing `notebook/session.py` or CLI code would risk touching files the coordinator
   may want reserved for a separate follow-up task. Flagging for the reviewer/coordinator
   to decide whether a follow-up task is warranted, or whether "green" should be
   interpreted as "no new failures" given these are pre-existing/environment-only.

## tests_run

- `PYTHONPATH=src python3 -W error::DeprecationWarning -c 'from kbutillib.ms_fba_utils
  import MSFBAUtils'` — exits 1 (DeprecationWarning promoted to error), as required.
- `PYTHONPATH=src python3 -c "..."` ad hoc identity check for `MSFBAUtils` — `is: True`.
- Ad hoc Python script (not committed) iterating all 38 `LEGACY_MODULE_MAP` entries,
  asserting import succeeds, warns, and every re-exported name is `is`-identical to the
  new module's — `ALL 38 SHIMS OK (import + warn + identity)`.
- `PYTHONPATH=src python3 -m pytest tests/core/test_deprecation_shims.py
  tests/guard/test_silent_none_reexports.py -q` — `84 passed`.
- `PYTHONPATH=src python3 -m pytest tests/core tests/guard -q` — `5 failed, 392 passed,
  9 skipped, 1 xfailed, 3 errors` (see success_criteria_check #4 for the pre-existing-failure
  analysis and the base-branch reproduction).
- Comparison run in sibling worktree `~/.maestro/worktrees/integrate-reorg` (base commit
  `cc08575`, unmodified): `PYTHONPATH=src python3 -m pytest tests/core tests/guard -q` —
  `5 failed, 308 passed, 9 skipped, 1 xfailed, 3 errors` — same failure set, confirming
  they predate this task's changes.

## caveats

- `pytest tests/core tests/guard` is not fully green due to 5 pre-existing
  failures/environment gaps unrelated to the shim/guard work (see above). These should be
  triaged by whoever owns the broader reorg-integration landing (missing
  `ModelSEEDDatabase` local checkout, the `notebook/session.py` `..toolkit` relative-import
  bug, and CLI `kbu cap list --json` output). I deliberately did not fix them to stay
  inside the assigned scope boundary (shim modules + tests only) and to avoid
  colliding with the concurrent skill-netting-and-sweep task.
- The task prompt's "cover at minimum" list of 11 modules is a strict subset of the 38
  generated — I generated the shim for every entry in the PRD's old→new mapping table, as
  instructed ("generate for the full mapping so no legacy path 404s").
- `ms_remote_solver_utils` / `ms_remote_solve_utils` (the LP-solver client modules) are
  intentionally NOT shimmed: they are not in the PRD's old→new mapping table (that table
  only covers modules that existed *flat* pre-reorg; the LP-solver client was relocated
  into `domains/modeling/` as part of *this same* integration, not renamed by the reorg
  itself), so there is no historical flat `kbutillib.ms_remote_solver_utils` import path to
  preserve.
- The generator script (`scripts/generate_deprecation_shims.py`) is a maintenance tool, not
  a build step — it was run once to produce the 38 committed files. If the mapping changes
  later, re-run it and re-commit the regenerated files; nothing at runtime depends on the
  script being present.
