# Work Record: transyt-module rebase onto main

## task_id
transyt-module-rebase (integration/rebase task, no Maestro task ID)

## branch
conductor/annotation-tool-modules/transyt-module

## commit_shas
- df50f6a8b5e67c56f4cf432fa26d72ab8c72d9c8  feat(transyt_utils): implement TransytUtils with Docker invocation and offline parse tests
- d9fc73acec06d344d1082ca03f53345991898d49  chore(work-record): add transyt-module work record

## summary
Rebased conductor/annotation-tool-modules/transyt-module onto current main (tip 0fa8660). There was a single conflict in src/kbutillib/__init__.py: both the prokka-module branch (now on main) and the transyt branch had independently inserted a `try: from .X import Y` import block immediately after the annotator_utils block. The conflict was resolved by keeping both — prokka's ProkkaUtils import block first, then transyt's TransytUtils import block — matching the pattern of sibling annotation-tool module exports. The `__all__` section was already merged cleanly by git (both "ProkkaUtils" and "TransytUtils" present). No other files conflicted.

## files_touched
- src/kbutillib/__init__.py (conflict resolution: added ProkkaUtils import block alongside TransytUtils import block)

## success_criteria_check
- Rebase onto current main: PASS — branch rebased cleanly onto main (0fa8660), tip SHA d9fc73a
- __init__.py conflict resolved keeping both exports: PASS — ProkkaUtils at line 222-225, TransytUtils at line 227-231; both in __all__ at lines 442 and 458
- Offline transyt + annotator + guard tests pass: PASS — 209 tests passed, 2 skipped in tests/annotators/
- TransytUtils and ProkkaUtils both importable from kbutillib: PASS — verified with PYTHONPATH=src
- git diff main...HEAD no longer touches prokka-related items except additive transyt exports: PASS — diff only adds TransytUtils import block (+6 lines) and "TransytUtils" in __all__ (+1 line)

## tests_run
- `pytest tests/annotators/ -x -q`: 209 passed, 2 skipped — PASS
- `pytest tests/ -x -q --ignore=tests/annotators -k "not docker and not live"`: 321 passed, 1 failed (pre-existing), 3 deselected, 47 warnings
  - FAILED tests/cli/test_init.py::TestDoctorCommand::test_doctor_prints_one_line_per_probe — pre-existing failure on main (test expects 6 doctor output lines, doctor now emits 8 after new probes were added on main post-test-write). Confirmed fails identically from main's working tree.
- Import smoke: `PYTHONPATH=src python -c "from kbutillib import TransytUtils, ProkkaUtils, AnnotatorUtils; ..."` — all three resolve correctly

## caveats
- The CLI doctor test failure (test_doctor_prints_one_line_per_probe) is pre-existing on main and unrelated to this branch. It should be addressed in a separate task.
- The editable install in the active Python environment points to ~/Dropbox/Projects/KBUtilLib (wip HEAD), not the worktree. Import verification was done with explicit PYTHONPATH=src from the worktree.
