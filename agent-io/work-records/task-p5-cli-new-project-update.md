# Work Record: task-p5-cli-new-project-update

## task_id
`task-p5-cli-new-project-update`

## branch
`task-p5-cli-new-project-update`

## commit_shas
- `29562ea8e8e0ae58adff15e70b15d0c5a14d8f3d`

## summary
Implemented `kbu new-project` and `kbu update` per PRD `kbu-start-v1` phases 5/P5. `new_project.py` scaffolds a student project from `templates/student-project/` (with `{{project_name}}` substitution in both filenames and content), creates a per-project venv via venvman or `.venv` fallback, pip-installs KBUtilLib editable, registers a Jupyter kernel, writes `kbu-project.toml` (including `[update.file_hashes]` SHA-256 entries for all tracked `.claude/commands/` and `.vscode/` files), runs `git init` + initial commit, and prints Cursor instructions. `update.py` reads `kbu-project.toml`, optionally pulls the source repo, diffs tracked template files between `last_pulled_commit` and current HEAD (using `TemplateDiff` dataclass), detects locally-modified files via hash comparison, prompts before clobbering, applies the diff, and rewrites `[update.file_hashes]` + `last_pulled_commit`. Both commands are registered in `cli/__init__.py`.

## files_touched
- `src/kbutillib/cli/__init__.py` — added `new-project` and `update` subcommands
- `src/kbutillib/cli/new_project.py` — new file: `new_project()` function + `new_project_command` Click command
- `src/kbutillib/cli/update.py` — new file: `update()` function + `update_command` Click command, `TemplateDiff` dataclass
- `tests/cli/test_new_project.py` — new file: 15 tests
- `tests/cli/test_update.py` — new file: 25 tests

## success_criteria_check

- **AC #18 — `kbu new-project` creates venv, pip-installs KBUtilLib, registers Jupyter kernel, writes kbu-project.toml with source_path/source_commit, runs git init+commit, prints Cursor instructions**: PASS — all steps implemented in `new_project.py`; tested in `test_creates_project_toml`, `test_template_*`, `test_first_subproject_invoked`.
- **AC #19 — `kbu update` reads source_path, pulls source if git repo, diffs .claude/ and .vscode/ between last_pulled_commit and HEAD, presents diff summary, applies on confirmation**: PASS — implemented in `update.py`; tested in `TestCheckDryRun`, `TestClobberWithWarn`, `TestPostApplyHashes`.
- **AC #20 — `kbu update` records SHA-256 hashes under `[update.file_hashes]`; locally-modified files trigger clobber-with-warn prompt**: PASS — `_compute_file_hashes` + `_detect_locally_modified` + prompt logic; tested in `TestLocallyModifiedDetection`, `TestClobberWithWarn`.
- **AC #21 — `kbu update --set-source <path>` relocates source_path and clears last_pulled_commit**: PASS — tested in `TestSetSource` (3 tests).
- **AC #22 — `kbu update --check` is dry-run; prints diff without writing**: PASS — tested in `TestCheckDryRun` (2 tests).
- **AC #23 — `kbu new-project` substitutes `{{project_name}}` in filenames and content**: PASS — tested in `TestCopyTemplateTree` and `TestNewProjectCore`.
- **AC #26 (new-project portion) — v1 macOS-only; non-macOS exits 1 without KBU_PLATFORM_OVERRIDE=force; with override uses python -m venv only**: PASS — tested in `test_non_darwin_exits_without_override` and `test_non_darwin_with_override_proceeds`.
- **AC #39 (new-project portion) — KBU_PLATFORM_OVERRIDE=force bypasses macOS check**: PASS — same as AC #26 test.
- **AC #45 — `kbu update` defaults to interactive; `--yes` bypasses prompts; `--check` and `--yes` are mutually exclusive**: PASS — tested in `TestCheckYesMutuallyExclusive` (4 tests) and `TestClobberWithWarn`.

## tests_run

```
pytest tests/cli/test_new_project.py tests/cli/test_update.py -v
40 passed in 0.12s

pytest tests/cli/ -v --ignore=tests/cli/test_new_project.py --ignore=tests/cli/test_update.py
228 passed in 11.86s (existing tests unbroken)
```

## caveats

- The `_build_diff` algorithm uses `git diff --name-status` to detect deleted files, and independently walks current template files for added/modified detection. This is sound but slightly indirect — P6 (when the real template tree ships) should exercise the diff path end-to-end against a real git history.
- Tests synthesize a stub `templates/student-project/` directory in a temp dir per the task instructions, as P6 ships the real template tree. No test depends on real template content.
- `test_missing_source_message_contains_path` patches `Path.cwd` to point the CLI at a synthetic project root; this is the standard approach given the CLI reads `kbu-project.toml` from `Path.cwd()`.
- Test files were added with `git add -f` because the repo's `.gitignore` contains `test_*.py`, but all prior P1-P4 test files were also force-added via the same pattern.
- The `first_subproject` flow calls `venv_python -m kbutillib subproject create <name>` which is best-effort (non-zero exit from subproject is not fatal). This matches the PRD's pattern.
