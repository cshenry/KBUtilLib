# Work Record: layout-profile

## task_id
layout-profile

## branch
worknb/layout-profile

## commit_shas
- 39fd8f1f4430d7706110be10fc8b1c1c010c6c46

## summary
Added the work-notebook layout descriptor to `src/kbutillib/layout.py` as a
clean parallel section alongside the existing BERIL functions, which are
untouched. The descriptor exposes four constants (`WORKNB_SHARED_ROOTS`,
`WORKNB_PRJ_SUBDIRS`, `WORKNB_GITIGNORE_MARKER_START`,
`WORKNB_GITIGNORE_MARKER_END`) and two functions (`worknb_gitignore_lines()`
returning the three canonical gitignore patterns, and
`apply_worknb_gitignore_block()` which appends or replaces the marker-delimited
block idempotently in any `.gitignore` file). Tests cover descriptor values,
gitignore pattern content and order, all three behavioral branches of the helper
(append-to-absent-file, append-to-existing-file, replace-stale-block), the
idempotency invariant (called 2 and 3 times), and a BERIL regression guard. All
71 tests in `test_layout.py` pass.

## files_touched
- `src/kbutillib/layout.py` — added work-notebook constants and two public functions; BERIL section unchanged
- `tests/test_layout.py` — added 36 new tests across 8 new test classes; updated import block

## success_criteria_check

- **`layout.py` exposes a work-notebook layout descriptor (shared roots models/genomes/data, per-PRJ NBCache/NBOutput)** — PASS. `WORKNB_SHARED_ROOTS = ("models", "genomes", "data")` and `WORKNB_PRJ_SUBDIRS = ("NBCache", "NBOutput")` are present and pinned by tests.
- **Idempotent gitignore-block helper using the specified markers** — PASS. `apply_worknb_gitignore_block()` uses `# >>> kbu work-notebook gitignore >>>` / `# <<< kbu work-notebook gitignore <<<` delimiters; the block contains exactly `notebooks/PRJ-*/NBCache/`, `notebooks/PRJ-*/NBOutput/`, `.ipynb_checkpoints/`; calling twice produces identical output (verified by test).
- **BERIL layout functions are unchanged** — PASS. All original BERIL tests (35 tests) continue to pass; a `TestBerilFunctionsUnchanged` class was added as an explicit regression guard.

## tests_run
```
pytest tests/test_layout.py -v
71 passed, 4 warnings in 1.24s
```
The 4 warnings are pre-existing `DeprecationWarning: invalid escape sequence` in
`ms_biochem_utils.py` and `kb_model_utils.py` — unrelated to this task, not
introduced here.

## caveats
- The PRD (Module 2 / AC 14) scopes this task to the layout descriptor and the
  gitignore-block helper only. Module 1 (`kbu notebook-init` CLI), Module 3
  (`NotebookSession` cache-dir parametrization), and the skill/command modules
  are separate tasks.
- `WORKNB_SHARED_ROOTS` lists roots in `("models", "genomes", "data")` order,
  matching the PRD's directory-convention listing. `DEFAULT_SHARED_DIRS` (BERIL)
  uses `("data", "models", "genomes")` order; both exist as separate constants.
- The module docstring was extended to advertise the new work-notebook public
  API alongside the existing BERIL API, with a clear section heading so the
  two namespaces stay legible as the module grows.
