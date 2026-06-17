# Work Record: kbu-beril-worktree-library

## task_id
kbu-beril-worktree-library (PRD: kbu-beril-worktree, task P1 — library core)

## branch
kbu-beril-worktree-library

## commit_shas
- e5df7ae74e1e1a8ffcfb9ed6bfe3ab38fe44a5e0

## summary
Created the `src/kbutillib/beril_worktree/` package with two modules: `config.py` for resolving and persisting BERIL worktree configuration in kbu's own `~/.kbutillib/config.yaml` with documented arg > env > config > default precedence, and `manager.py` implementing `BerilWorktree(beril_root, worktree_root)` with `new/remove/list/open` methods covering worktree creation, branch adoption, `.env`/`.venv-berdl` symlinks, per-worktree `.code-workspace` file generation, idempotent remove with dirty-check, and `list()` across live + reopenable branches. All git operations run with `git -C <beril_root>` and never depend on cwd. A full pytest suite of 69 tests against a scratch temp git repo achieves 100% line+branch coverage, satisfying the `fail_under=100` constraint.

## files_touched
- `src/kbutillib/beril_worktree/__init__.py` — package init (new)
- `src/kbutillib/beril_worktree/config.py` — config resolution + set_root (new)
- `src/kbutillib/beril_worktree/manager.py` — BerilWorktree class (new)
- `tests/beril_worktree/__init__.py` — test package init (new)
- `tests/beril_worktree/test_beril_worktree.py` — 69-test suite (new)

## success_criteria_check

- **src/kbutillib/beril_worktree/{config.py,manager.py} exist** — PASS. Both files created at the expected paths.
- **BerilWorktree exposes new/remove/list/open** — PASS. All four methods implemented with documented signatures.
- **config exposes documented resolution + set_root** — PASS. `resolve_beril_root`, `resolve_worktree_root`, `set_root` all present with correct precedence.
- **new() creates worktree + projects/<id> branch** — PASS. Test `TestNew::test_new_creates_worktree_and_branch`.
- **new() adopts an existing branch (no -b, no error)** — PASS. Test `TestNew::test_new_adopts_existing_branch`.
- **remove() deletes only directory while branch persists** — PASS. Test `TestRemove::test_remove_deletes_dir_branch_survives`.
- **remove() is idempotent no-op when absent** — PASS. Test `TestRemove::test_remove_idempotent`.
- **remove() refuses when dirty and honors force** — PASS. Tests `test_remove_refuses_dirty_worktree` and `test_remove_force_overrides_dirty`.
- **.env/.venv-berdl symlinks created and gitignored** — PASS. Tests `TestSymlinks::test_symlinks_created` and `test_symlinks_are_gitignored`.
- **.code-workspace file valid JSON outside worktree pointing at ./<id>** — PASS. Tests `TestWorkspace::test_workspace_valid_json`, `test_workspace_outside_worktree`, `test_workspace_folder_path`.
- **ID validation rejects slashes** — PASS. Test `TestIDValidation::test_slash_rejected`.
- **Config precedence (arg>env>config>default) and set_root persistence** — PASS. Tests `TestConfigResolution` and `TestSetRoot` classes.
- **Overall coverage is 100% so suite passes under fail_under=100** — PASS. `99 passed, 29 warnings` — coverage report shows 100% for all three new module files.

## tests_run

```
python -m pytest tests/beril_worktree/ --cov=kbutillib.beril_worktree --cov-report=term-missing -q
```

Result:
```
Name                                       Stmts   Miss Branch BrPart  Cover   Missing
src/kbutillib/beril_worktree/__init__.py       0      0      0      0   100%
src/kbutillib/beril_worktree/config.py        62      0     22      0   100%
src/kbutillib/beril_worktree/manager.py      160      0     60      0   100%
TOTAL                                        222      0     82      0   100%
Required test coverage of 100.0% reached. Total coverage: 100.00%
69 passed, 29 warnings in 10.53s
```

Pre-existing failures in `tests/test_ms_biochem_deltag.py`, `tests/test_task_a_venv_doctor.py`, `tests/cli/test_init.py`, and `tests/test_comprehensive_gapfill_wrapper.py` (24 failures + 4 errors) exist on the `main` branch before this change and are unrelated to this task.

## caveats

1. **No CLI wiring**: Per task scope, no CLI commands (`kbu beril worktree new`, etc.) are wired in this task. The `beril_cmd` group in `cli/beril.py` is unchanged. CLI wiring is the next task slice.

2. **No `launch.py`**: The `start` proxy module is out of scope for this task (as specified: "no CLI wiring, no launch proxy").

3. **`open_cursor=True` behavior**: The `open()` method called from `new(open_cursor=True)` returns the worktree path but does not actually launch Cursor (Cursor launch is CLI-layer work). The test covers the code path via monkeypatching `open`.

4. **git porcelain format**: The `_parse_worktree_list` method handles `refs/heads/` prefixes in the `branch` field — this is the actual git output format, verified against a real git repo during development.

5. **Pre-existing test failures**: 24 test failures and 4 errors exist on main before this change. They are not caused by this PR. The new beril_worktree tests all pass at 100% coverage.
