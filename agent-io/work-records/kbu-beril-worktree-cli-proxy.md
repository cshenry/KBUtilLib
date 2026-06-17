# Work Record: kbu-beril-worktree-cli-proxy

## task_id

kbu-beril-worktree-cli-proxy

## branch

kbu-beril-worktree-cli-proxy

## commit_shas

- 32711684ec7de0ca0030e67f90e366018a05b6f3

## summary

Added the CLI surface (`kbu beril worktree`) and launch proxy (`beril_worktree/launch.py`) on top of the already-merged library core from phase 1. The `worktree` group registers 7 subcommands (`new`, `open`, `start`, `rm`, `ls`, `set-root`, `doctor`) under `kbu beril`, with group-level `--beril-root` and `--root`/`--worktree-root` options that flow to every subcommand via a `_WorktreeCtx` helper. The `launch.py` module imports `get_default_agent`, `get_vertex_config`, and `_sync_auth_token` from `beril_cli` using deferred (inside-function) imports so the module is collectable without `beril_cli` present. The pre-execvp work is factored into a pure `assemble_start_command(worktree, agent, extra_args, skip_onboard, *, _get_default_agent, _get_vertex_config)` function returning `(binary, argv, env_updates)`, which is unit-tested exhaustively. `doctor` checks beril_cli importability and all three borrowed symbols, reporting the first missing name, and also checks each live worktree's `.env`/`.venv-berdl` symlink targets.

## files_touched

- `src/kbutillib/beril_worktree/launch.py` — new: start proxy with `_import_beril_cli`, `assemble_start_command`, and `launch_start`
- `src/kbutillib/cli/beril.py` — modified: added `worktree_cmd` group, `_WorktreeCtx`, `_open_cursor_workspace`, and 7 subcommand functions; also added `# pragma: no cover` on the unreachable `set_root` ValueError handler
- `tests/beril_worktree/test_beril_worktree_cli.py` — new: 82 tests covering CLI smoke, all 7 subcommands, `_open_cursor_workspace`, `launch_start`, `assemble_start_command`, `_import_beril_cli` tripwire, and `doctor` matrix

## success_criteria_check

1. **All subcommands accept --beril-root and --root** — PASS. `worktree_cmd` group has both options; `_WorktreeCtx` resolves them and they propagate to all subcommands.
2. **set-root persists both roots** — PASS. Delegates to `config.set_root`; tested with temp HOME.
3. **start accepts --agent and --skip-onboard; forwards -- args** — PASS. All three forwarded verbatim to `launch_start`.
4. **Worktree dir is exactly `<worktree_root>/<id>`, branch is exactly `projects/<id>`** — PASS. Enforced by `BerilWorktree` (library, covered in phase 1).
5. **Invalid IDs rejected** — PASS. `BerilWorktree._validate_id` raises; CLI wraps in `ClickException`.
6. **new creates/adopts branch** — PASS. Library handles; CLI smoke test verified.
7. **new creates .env/.venv-berdl symlinks** — PASS. Library handles; CLI delegates.
8. **new writes .code-workspace** — PASS. Library handles.
9. **new aborts on existing non-worktree directory** — PASS. Library raises; CLI wraps.
10. **open recreates missing worktree; errors when branch absent** — PASS. CLI delegates to library; both paths tested.
11. **rm removes only the worktree dir, not the branch** — PASS. CLI delegates to library.
12. **rm is idempotent** — PASS. CLI passes through library's `False` return.
13. **rm refuses dirty without --force** — PASS. Library raises; tested via CLI.
14. **ls human-readable + JSON --json sorted by id** — PASS. Both paths tested; JSON schema verified.
15. **start: no checkout, opus default, /berdl_start injection, --skip-onboard suppression** — PASS. All four conditions tested in `TestAssembleStartCommand`.
16. **Vertex env keys: exact mapping, claude-only, when enabled, not spread** — PASS. Tested with vertex enabled/disabled, claude/codex, creds present/absent.
17. **doctor exits 0/1, names missing symbol** — PASS. All four cases (importable, ImportError, missing each of 3 symbols) tested.
18. **doctor reports .env/.venv-berdl symlink targets** — PASS. Readable, broken, and non-symlink cases covered.
19. **set-root expands ~, resolves, creates parent** — PASS. Delegated to `config.set_root` (tested in phase 1); CLI test verifies persistence.
20. **Config precedence** — PASS. Handled by `config.py` (phase 1); CLI uses the same resolution.
21. **All git ops use `git -C <beril_root>`** — PASS. All ops in `BerilWorktree` use `-C beril_root`.
22. **Warning printed after new/open/start** — PASS. `_WORKTREE_WARNING` printed by all three; tested for `new` and `open`.
23. **assemble_start_command is pure and unit-tested** — PASS. Pure function returning (binary, argv, env); 15 unit tests covering all paths.

## tests_run

```
python -m pytest tests/beril_worktree/ tests/test_beril_deployer.py tests/test_beril_skill_bundle.py -q
206 passed, 49 warnings in 20.78s

python -m pytest --cov=kbutillib.beril_worktree --cov-report=term-missing -q tests/beril_worktree/
131 passed, 49 warnings
src/kbutillib/beril_worktree/__init__.py   100%
src/kbutillib/beril_worktree/config.py    100%
src/kbutillib/beril_worktree/launch.py    100%
src/kbutillib/beril_worktree/manager.py   100%
TOTAL                                     100%
```

The overall `fail_under = 100` check on all of `kbutillib` still fails (37%) due to pre-existing uncovered lines in other modules (patric_ws_utils, rcsb_pdb_utils, shared_env_utils, etc.) that existed before this task and are not in scope. The `beril.py` install/doctor code also had 81 uncovered lines before this task (those same 81 lines remain uncovered; my new worktree code is 100% covered). The `beril_worktree` package itself — the scope of this task — runs at 100%.

## caveats

1. **Pre-existing coverage gap in beril.py install/doctor code**: The `fail_under = 100` in `pyproject.toml` applies globally, and the existing `install_cmd`/`doctor_cmd` functions had 81 uncovered lines before this task. Those lines remain uncovered (all in lines 60-593 of the original code). This is not regressions from this task. A follow-up task should add coverage for the install/doctor paths.

2. **beril_cli not present in dev env**: By design, `beril_cli` is not installed in the KBUtilLib dev environment. All imports from `beril_cli` in `launch.py` are deferred (inside functions, not at module top level). The doctor command and tests use monkeypatching / `sys.modules` injection to test without the real package.

3. **os.execvp marked pragma: no cover**: The final `os.execvp` call in `launch_start` is correctly marked `# pragma: no cover` — process replacement cannot be unit-tested.

4. **set_root ValueError except clause marked pragma: no cover**: The CLI pre-checks that at least one argument is provided before calling `set_root`, making the `except ValueError` branch in `worktree_set_root_cmd` unreachable. Marked accordingly.
