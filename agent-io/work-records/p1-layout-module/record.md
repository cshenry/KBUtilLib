# Work Record — p1-layout-module

## task_id
p1-layout-module

## branch
task/p1-layout-module

## commit_shas
- 9f95ced0213e6dfda8a4fb1d7fcbe82f573d270c

## summary
Created `src/kbutillib/layout.py`, a new module that owns all canonical-layout
knowledge for KBUtilLib research projects: the `DEFAULT_SHARED_DIRS` constant,
`read_shared_dirs` (reads `[layout.shared_dirs]` from `kbu-project.toml` via
`tomllib`, falls back to defaults), `subproject_subdirs` (returns ordered
subdir lists with/without `archive`), `subproject_gitignore_lines` (per-subproject
gitignore patterns), and `root_gitignore_lines` (large-file glob patterns for
shared dirs). Added `tests/test_layout.py` with 35 test cases organised by
Acceptance Criteria #1–#8; all 35 pass. No existing tests regress (the 17 pre-
existing failures in `test_ms_biochem_deltag.py` were already failing on `main`).

## files_touched
- `src/kbutillib/layout.py` — new module (public API: `DEFAULT_SHARED_DIRS`,
  `read_shared_dirs`, `subproject_subdirs`, `subproject_gitignore_lines`,
  `root_gitignore_lines`)
- `tests/test_layout.py` — new test file (35 tests, AC #1–#8 each with at
  least one test case)
- `agent-io/work-records/p1-layout-module/record.md` — this file

## success_criteria_check

| Criterion | Status | Justification |
|---|---|---|
| `tests/test_layout.py` exists | PASS | Created at `tests/test_layout.py` |
| Covers AC #1–#8 | PASS | 35 tests across 8 test classes, each class maps to one AC |
| Passes via `pytest tests/test_layout.py -v` | PASS | 35/35 passed |
| `kbutillib.layout` is importable | PASS | `from kbutillib.layout import ...` works cleanly |
| Exposes documented public API | PASS | `DEFAULT_SHARED_DIRS`, `read_shared_dirs`, `subproject_subdirs`, `subproject_gitignore_lines`, `root_gitignore_lines` all present |
| No other tests regress | PASS | 770 passed + 17 skipped; 17 pre-existing failures in `test_ms_biochem_deltag.py` unchanged |

## tests_run
```
PYTHONPATH=src pytest tests/test_layout.py -v
  35 passed in 1.07s

PYTHONPATH=src pytest tests/ --ignore=tests/test_layout.py -v
  770 passed, 17 failed (pre-existing), 17 skipped
  Pre-existing failures: test_ms_biochem_deltag.py — MSBiochemUtils.calculate_reaction_deltag method missing; confirmed identical failure count on unmodified main.
```

## caveats
- The task scope is AC #1–#8 (layout module only). AC #9–#54 (state machine
  updates, adopt CLI, migrate command, template changes, skill rewrites) are
  explicitly out of scope for this slice.
- The 17 `test_ms_biochem_deltag.py` failures are pre-existing on `main`;
  none were introduced by this change.
- `kbutillib.layout` is not yet imported by any other module in the repo. Wiring
  it into `_scaffold_subproject` and other consumers is deferred to later PRD
  slices (AC #13–#14).
