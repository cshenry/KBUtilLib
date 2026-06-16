# Work Record: kbu-friction/template-unify

## task_id
kbu-friction-template-unify (Task B of kbu-skills-friction-fix PRD)

## branch
kbu-friction/template-unify

## commit_shas
- 843a9f3f (full SHA to be confirmed via `git rev-parse HEAD`)

## summary

Collapsed the two divergent `util.py.tmpl` files into a single canonical template at
`src/kbutillib/cli/templates/util.py.tmpl` (the file the CLI actually renders via
`kbu init-notebook`). The beril/skills copy at
`src/kbutillib/beril/skills/kbu-notebook/util.py.tmpl` was deleted. The unified
template now has all heavy imports (numpy, pandas, cobra) guarded in individual
`try/except ImportError` blocks, uses `__file__` anchoring for the session (cwd-
independent), uses FLAT path math (`PROJECT_ROOT = NOTEBOOK_DIR.parent`, one level
up), includes the smart-merge marker so `kbu init-notebook --force` round-trips
cleanly, and drops the `kbutillib.notebook.helpers`/`.schema` import block and the
`session_for()` back-compat shim. The `kbu-notebook` SKILL.md was updated to document
the FLAT layout, point its template reference at `cli/templates/util.py.tmpl`, show
the correct `PROJECT_ROOT = NOTEBOOK_DIR.parent` math, and add a BERIL lifecycle
section instructing users to run `/berdl_start` after `kbu init-notebook`. Tests in
`test_beril_skill_bundle.py` were updated to point to the unified template path and
13 new AC4/5/6 tests were added (including a smart-merge round-trip test).

## files_touched

- `src/kbutillib/cli/templates/util.py.tmpl` — rewritten: guarded imports, __file__ anchoring, FLAT path math, no helpers/schema block, no session_for() shim
- `src/kbutillib/beril/skills/kbu-notebook/util.py.tmpl` — **deleted** (was the divergent skills-side copy)
- `src/kbutillib/beril/skills/kbu-notebook/SKILL.md` — updated FLAT layout description, template ref, PROJECT_ROOT math, BERIL lifecycle section
- `tests/test_beril_skill_bundle.py` — `_UTIL_TMPL` path updated to cli/templates; 13 new tests added; committed with `git add -f`
- `agent-io/work-records/kbu-friction-template-unify.md` — this file

## success_criteria_check

- **AC 4 (unified template body):** PASS. `cli/templates/util.py.tmpl` has the bootstrap block, guarded numpy/pandas/cobra imports, `from kbutillib.notebook import NotebookSession`, `__file__`-anchored session, flat path constants, smart-merge marker, no helpers/schema block, no `session_for()` shim. `beril/skills/kbu-notebook/util.py.tmpl` is deleted. `test_beril_skill_bundle.py` updated.
- **AC 5 (PROJECT_ROOT = NOTEBOOK_DIR.parent, no parent.parent):** PASS. Template has `PROJECT_ROOT: Path = NOTEBOOK_DIR.parent`. SKILL.md documents the same. No `parent.parent` anywhere in either file.
- **AC 6 (FLAT shape + smart-merge round-trip):** PASS. `init_notebook.py` already wrote the FLAT shape (`notebooks/util.py`). `_smart_merge_util` finds the marker in the new template (verified by test). Smart-merge round-trip test preserves user helpers below the marker.
- **AC 1, 2, 3, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17:** Out of scope for Task B (Tasks A, C, D, E). Not addressed in this commit.

## tests_run

- `pytest tests/test_beril_skill_bundle.py` — **56 passed** (all green, including 13 new Task B tests)
- `pytest tests/test_composition_smoke.py` — **2 failed, 7 passed, 14 skipped, 1 error** — same result as on `main` before this branch; failures are pre-existing (missing `pandas`/`modelseedpy` in the `kbutillib-py3.11` venv, unrelated to Task B changes)

## caveats

- The `_assert_import_is_guarded` helper in the test class is an instance method (so `self` is available) but pytest collects it by walking `test_*` methods; it is named without the `test_` prefix intentionally to avoid collection as a test. If a reviewer finds this pattern odd, it can be extracted to a module-level function.
- The two pre-existing failures in `test_composition_smoke.py` (`test_facade_argo_deferred`, `test_notebook_session_kbu_returns_facade`) are caused by `pandas` not being installed in the `kbutillib-py3.11` test venv, which triggers a `ModuleNotFoundError` in `kbutillib/notebook/storage/vectors.py`. This is the same venv used to run tests on `main`; the failures exist before and after Task B.
- `init_notebook.py` already wrote the FLAT shape before this task — no code change was needed there. The task confirmed the existing behavior matches the new template.
- Tasks A, C, D, E of the PRD are not addressed here and remain to be done in separate branches.
