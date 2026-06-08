# Work Record: p4-kbu-migrate-cli

## task_id
p4-kbu-migrate-cli

## branch
task/p4-kbu-migrate-cli

## commit_shas
- 3c24989f4b6e38a84f6be4a31bb9da32e81c1095

## summary
Added `kbu migrate` as a new top-level CLI command for repo-level layout migration per PRD `kbutillib-v2`. The command walks the project root, conditionally adds `[layout.shared_dirs]` to `kbu-project.toml`, creates missing shared-dir scaffolding with `.gitkeep`, then iterates each registered subproject prompting the user (via Click choice prompts) about relocating `data/`, `user_data/`, and `references.md`. Per-subproject `.cache/` and `literature/` dirs are silently ensured, and a gitignore marker block is appended idempotently per subproject. The command is registered in `src/kbutillib/cli/__init__.py` alongside existing commands. Tests cover all three ACs (19 tests, all pass) and all 455 pre-existing tests continue to pass.

## files_touched
- `src/kbutillib/cli/migrate.py` — new file; `migrate_cmd` Click command implementation
- `src/kbutillib/cli/__init__.py` — added `from .migrate import migrate_cmd` import and `main.add_command(migrate_cmd, name="migrate")`
- `tests/cli/test_migrate.py` — new test file (force-added past `.gitignore`); 19 tests covering AC #39, #40, #41

## success_criteria_check

| Criterion | Status | Justification |
|---|---|---|
| `kbu migrate` registered and invocable | PASS | `python -m kbutillib migrate --help` returns usage text; registered in `cli/__init__.py` |
| AC #39: prompts per subproject, no silent ops | PASS | 6 tests in `TestAC39Prompts` verify prompts are shown and skip/choose-4 leaves content intact |
| AC #40: adds `[layout.shared_dirs]` when absent, leaves unchanged when present | PASS | `TestAC40LayoutSharedDirs` — 3 tests; verifies TOML is written correctly and custom value is preserved |
| AC #41: creates root shared dirs with `.gitkeep` when missing | PASS | `TestAC41SharedDirCreation` — 4 tests; verifies dirs created, `.gitkeep` present, pre-existing dirs not clobbered |
| Existing tests continue to pass | PASS | 455 existing CLI + unit tests pass with no regressions |
| No conflicts with p2-subproject-cli or p3 changes | PASS | New file only; registration line added; no edits to subproject.py or bootstrap.py |

## tests_run

```
python -m pytest tests/cli/test_migrate.py -v
19 passed, 0 failed  (0.07s)

python -m pytest tests/cli/ --ignore=tests/cli/test_migrate.py -v
455 passed, 1 warning, 0 failed  (13.98s)
```

## caveats

- The test file is at `tests/cli/test_migrate.py` (not the task-envelope-suggested `tests/test_migrate.py`). Placing it under `tests/cli/` is consistent with all other CLI command tests in the repo. Both locations require `git add -f` due to the root `.gitignore` pattern `test_*.py`.
- `migrate_cmd` imports `_append_subproject_gitignore`, `_find_project_root`, and `_list_subproject_names` directly from `kbutillib.cli.subproject`. These are private helpers (underscore-prefixed) so if the subproject module is significantly refactored in a future slice, the imports in `migrate.py` need updating.
- The "merge flat" option (choice 2) for data relocation checks for collisions before moving anything, and skips with a warning if any collision is found. This is a conservative choice — a more sophisticated implementation could offer per-file resolution.
- `kbu migrate` does not update `kbu-subproject.toml` status after migration (e.g., advancing from `migrate` to `p-review`). The PRD indicates that state advance is a separate `kbu subproject advance` operation the user performs after running `/kbu-migrate` skill.
