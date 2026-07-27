# task_id

reorg-livability-fixes (Maestro developer task, run locally on primary-laptop)

# branch

reorg-livability-fixes

# commit_shas

1. bb71ad2fb1930dbccebd980560968f93d4338d69 — fix(reorg): resolve 4 pre-existing reorg-base livability bugs

HEAD = `bb71ad2fb1930dbccebd980560968f93d4338d69`

# summary

Fixed 4 pre-existing bugs in the KBUtilLib reorg base
(`full-suite-verification-local` @ `4c2a1023`) that blocked the notebook,
researchos, and cheminformatics/verab domains from actually working despite
importing cleanly: (1) the nested `kbutillib/notebook/{schema,serialization,
storage,helpers}/` subpackages (26 files) were byte-identical duplicates of
their `kbutillib/domains/notebook/` counterparts rather than shims, so
pydantic models built via the flat path were class-distinct from what
`domains/notebook/experiment_store.py` expects, causing
`pydantic.ValidationError` — converted all 26 files into one-line
re-export shims; (2) `domains/notebook/session.py`'s `NotebookSession.kbu`
property used a broken relative import (`from ..toolkit import KBUtilLib`,
resolving to the nonexistent `kbutillib.domains.toolkit`) — fixed to the
absolute `from kbutillib.toolkit import KBUtilLib`; (3)
`domains/cheminformatics/verab/facade.py` had 4 deferred imports that
duplicated the package's own path as a prefix (e.g.
`.cheminformatics.verab.rule_discovery` instead of `.rule_discovery`),
invisible without rdkit installed — fixed all 4 to plain sibling-relative
imports; (4) `researchos/config.py`'s shim was missing re-exports of 5
underscore-prefixed module constants (`_KBUTILLIB_DIR` etc.) that
`tests/researchos/test_researchos.py`'s `tmp_home` fixture
`monkeypatch.setattr()`s directly, causing `AttributeError` at fixture
setup for every test using it — added the explicit re-export. Ran the full
suite before and after in a fresh throwaway venv (rdkit + cobra installed
ad hoc to exercise the rdkit-gated verab tests and the cobra-dependent
notebook/helpers tests) and confirmed via `comm`-diff of the FAILED/ERROR
id sets that this branch introduces **zero regressions** and resolves
exactly 49 previously-failing/erroring tests (2658→2707 passed,
50→32 failed, 61→30 errors, 325 skipped unchanged). The remaining 32
failed + 30 errors are all pre-existing, explicitly out-of-scope
modelseedpy/ModelSEEDDatabase environment gaps (plus two small adjacent
researchos shim/behavior gaps — a missing `_slug` re-export in
`registry.py` and a `set_root persists` CLI test — that are structurally
similar to bug 4 but were not in the 4-bug scope, so left untouched and
documented).

# files_touched

- `src/kbutillib/notebook/schema/{__init__,entity,experiment,manifest,media,strain,validation,vector}.py` (8 files, converted to shims)
- `src/kbutillib/notebook/serialization/{__init__,serialize_cobra_model,serialize_dataframe,serialize_dict,serialize_json,serialize_msexpression,serialize_msgenome,serialize_msmedia,serialize_msmodelutil,serialize_text}.py` (10 files, converted to shims)
- `src/kbutillib/notebook/storage/{__init__,blobs,catalog,vectors}.py` (4 files, converted to shims)
- `src/kbutillib/notebook/helpers/{__init__,compartment,fva,reaction}.py` (4 files, converted to shims)
- `src/kbutillib/domains/notebook/session.py` (fixed `..toolkit` relative import)
- `src/kbutillib/domains/cheminformatics/verab/facade.py` (fixed 4 relative imports)
- `src/kbutillib/researchos/config.py` (added underscore-constant re-exports)
- `agent-io/reports/reorg-livability-fixes.md` (new — full report with per-bug detail and before/after counts)

# success_criteria_check

- **Bug 1 (notebook duplicate-subpackage class-identity bug) fully fixed, class identity verified** — PASS. All 26 duplicate files converted to shims; verified `kbutillib.notebook.schema.experiment.Sample is kbutillib.domains.notebook.schema.experiment.Sample` and 5 similar checks (Strain, NotebookEntry, Vector, `register_serializer`, `normalize_compartment`) all `True`. `tests/notebook/test_experiment_store.py`, `test_vector_store.py`, `test_manifest.py`, `test_schema.py`, `test_validate_entities.py` all now fully pass (was 9/16/1/1/3 respective failures+errors, now 0).
- **Bug 2 (session.py `..toolkit` relative-import bug) fixed** — PASS. Changed to absolute import; verified `NotebookSession(...).kbu` returns a real `kbutillib.toolkit.KBUtilLib` instance; `tests/core/test_composition_smoke.py::TestNotebookSessionKbu::test_notebook_session_kbu_returns_facade` went FAILED→PASSED.
- **Bug 3 (verab/facade.py relative-import bug, rdkit-gated) fixed** — PASS. Found and fixed all 4 occurrences of the duplicated-path bug (task description implied at least one; I found 4 identical instances across `discover_rules`, `emit_king_workflow`, `screen`, `enumerate_methoxy_aromatics`). Verified with rdkit installed: `tests/verab/` went from 190 passed/3 failed/13 skipped to 193 passed/13 skipped.
- **Bug 4 (researchos/config.py shim gap) fixed** — PASS. Added `_KBUTILLIB_DIR` plus the 4 sibling underscore constants tests touch. `tests/researchos/test_researchos.py` went from 59 passed/7 failed/15 errors to 74 passed/7 failed (the 7 are a pre-existing, structurally-adjacent-but-out-of-scope gap — see caveats).
- **Do not expand beyond the 4 listed bugs / do not touch the modelseedpy env gap** — PASS. Verified via full-suite `comm`-diff: 0 new failures/errors introduced; the pre-existing 32 failed + 30 errors after my changes are byte-for-byte the same test ids as a subset of the 50+61 before, confirming nothing outside the 4 bugs was touched or affected.
- **Verify class identity, run affected test subsets, regression guard, deprecation-shim + guard tests** — PASS. All commands from the task's VERIFY section run and reported below with actual output.
- **Commit fixes on branch reorg-livability-fixes; write report + work-record** — PASS.

# tests_run

- Class-identity script (`PYTHONPATH=src python3 -c ...`) — 6/6 identity checks `True`.
- `pytest tests/notebook/test_experiment_store.py tests/notebook/test_vector_store.py tests/notebook/test_manifest.py tests/notebook/test_schema.py tests/notebook/test_validate_entities.py tests/researchos/test_researchos.py -q` — `154 passed` (notebook subset) `+ 74 passed, 7 failed` (researchos, pre-existing out-of-scope).
- `pytest tests/verab/ -q` (rdkit installed ad hoc) — before: `190 passed, 3 failed, 13 skipped`; after: `193 passed, 13 skipped`.
- `pytest tests/notebook/helpers -q` (cobra installed ad hoc, sanity check beyond task's required list) — `26 passed` both before and after (confirms no regression in this rdkit/cobra-adjacent subset).
- `PYTHONPATH=src python3 -c 'import kbutillib; kbutillib.KBUtilLib'` — exit 0.
- `PYTHONPATH=src python3 -m kbutillib model --help` — exit 0.
- `pytest tests/core/test_deprecation_shims.py tests/guard -q` — `77 passed, 8 failed`, identical before and after (pre-existing `escher_utils`/`ms_fba_utils`/`ms_template_utils` modelseedpy-env gap, explicitly out of scope).
- `pytest tests/ -q --tb=no` (full suite) — before (base `4c2a1023`): `50 failed, 2658 passed, 325 skipped, 61 errors`; after (`bb71ad2`): `32 failed, 2707 passed, 325 skipped, 30 errors`. `comm`-diff of sorted FAILED/ERROR test-id sets: 0 new ids in after-not-in-before; 49 ids in before-not-in-after (all resolved by the 4 fixes).
- `ruff check` on all 30 touched files — `All checks passed!`.

# caveats

- Bug 4's fix closes the `AttributeError` shim gap exactly as scoped, but I noted (and documented in the report) that patching the shim's re-exported copy of e.g. `_DEFAULT_ROOT` does not actually change the behavior of `resolve_researchos_root()` etc., because those functions read their own module's globals in `kbutillib.agents.researchos.config`, not the shim's namespace. None of the currently-passing assertions depend on that distinction (they're written loosely enough to pass either way), so this is a latent, separate design question about the monkeypatch-target contract — flagged for whoever next touches this shim, not fixed here (out of scope for "close the re-export gap").
- Discovered two more test failures in `tests/researchos/test_researchos.py` while fixing bug 4 that are structurally similar (both are "shim/registry doesn't expose something a test needs") but were not in the 4-bug list: `TestSlug::*` (6 tests) fail because `kbutillib/researchos/registry.py`'s shim doesn't re-export `_slug`, and `TestCLISetRoot::test_set_root_persists_with_root_option` fails for an unrelated reason. Both are confirmed pre-existing on the base branch (same failure count/ids before and after my change) and left untouched per the task's explicit scope boundary ("scoped list — do NOT expand beyond these").
- Found 4 occurrences of the verab/facade.py duplicated-path relative-import bug, not just the 1 implied by the task description — all 4 share the identical root cause and fix pattern, so I fixed all 4 rather than just enough to pass the specific test named in the task's VERIFY section, since leaving 3 of 4 broken would have left the bug only partially resolved.
- Full suite run took ~110s per pass; ran it twice (before/after) plus targeted subset reruns, all within the same throwaway venv (`/private/tmp/claude-501/.../scratchpad/kbu-reorg-venv`, Python 3.11.14). This venv does not persist past this session; a future task needing the same coverage should reprovision with `pip install -e '.[all]' --group dev --group lint --group notebook`, then `pip install rdkit cobra` for maximal coverage, matching this task and the prior `full-suite-verification-local` task's approach.
- Did not attempt `minedatabase`/pickaxe or dev-`modelseedpy` installs (per explicit task instruction not to touch the modelseedpy environment gap); the ambient PyPI `modelseedpy`/`cobra`/`rdkit` combination used here is sufficient to reach and verify all 4 target bugs.
