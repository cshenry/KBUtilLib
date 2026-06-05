# Work Record: task-p4-cli-init

## task_id
task-p4-cli-init

## branch
task-p4-cli-init

## commit_shas
(filled after commit)

## summary
Implemented the `kbu init` and `kbu doctor` subcommands per PRD `kbu-start-v1` (AC #15, #16, #17, #26 init portion, #39 init portion, #40, #41). Created `src/kbutillib/cli/init.py` with: marker file I/O at `~/.config/kbu/init_done.json` (XDG_CONFIG_HOME respected); `init()` supporting venvman (macOS-only, correct `venvman create --project kbutillib --dir <root> --python 3.11` invocation, activate.sh VIRTUAL_ENV parser, fallback to `python -m venv .venv` on venvman failure with stderr warning); `init_status()` returning 0/1/2; `doctor()` with 5 probes (init-done, cursor-on-path, claude-extension, kbu-version, jupyter-kernel); non-macOS without `KBU_PLATFORM_OVERRIDE=force` prints the v1 message and exits 1. Registered both `init` and `doctor` as top-level subcommands in `src/kbutillib/cli/__init__.py`. Added 34 tests in `tests/cli/test_init.py` covering all required scenarios; all pass.

## files_touched
- `src/kbutillib/cli/init.py` — new file; full init + doctor implementation
- `src/kbutillib/cli/__init__.py` — import and register `init_command` and `doctor_command`
- `tests/cli/test_init.py` — new file; 34 tests across 10 test classes
- `agent-io/work-records/task-p4-cli-init.md` — this file

## success_criteria_check

| Criterion | Status | Notes |
|---|---|---|
| `pytest tests/cli/test_init.py -v` passes | PASS | 34/34 tests pass in 0.07s |
| `kbu init --status` on system without marker exits 1 | PASS | Verified via smoke test and `test_exit_1_when_no_marker` |
| `kbu doctor` prints one line per probe starting with `[PASS\|FAIL\|SKIP]` | PASS | Verified smoke test: 5 lines, each starts with `[PASS]`/`[FAIL]`/`[SKIP]` |
| Test for non-Darwin without override confirms exit 1 with macOS-only message | PASS | `test_non_darwin_without_override_exits_1` + `test_non_darwin_without_override_prints_v1_message` |
| venvman invocation uses `venvman create --project kbutillib --dir <path> --python 3.11` | PASS | `test_venvman_subprocess_uses_correct_args` asserts exact arg list; `test_venvman_present_uses_venvman` checks via mocked `_run_venvman` |

## tests_run
```
pytest tests/cli/test_init.py -v
34 passed in 0.07s

pytest tests/cli/ -v
228 passed, 1 warning in 11.69s   (all prior P1-P3 tests still passing)
```

## caveats
- `tests/cli/test_init.py` required `git add -f` because `.gitignore` contains a `test_*.py` rule added after the P1-P3 test files were already tracked. The rule was clearly intended for scratch test files, not the committed test suite — consistent with the fact that all other `tests/cli/test_*.py` files are tracked. No change to `.gitignore` was made (out of scope for this task).
- The venv python path resolution from `activate.sh` relies on venvman writing a `VIRTUAL_ENV=...` line in that file. If venvman changes its activation format, `_parse_virtual_env_from_activate` would need updating. This is the most fragile piece of the venvman integration.
- `kbu init` on non-macOS with `KBU_PLATFORM_OVERRIDE=force` uses `python -m venv` only (no venvman detection), per PRD AC #39.
- `kbu doctor`'s `kbu-version` probe calls `kbu --version` via subprocess; on a freshly-initialized machine where kbu is installed in a venv that isn't activated, this probe will FAIL even though kbu is installed. This is expected behavior and matches the PRD intent (doctor checks the current shell environment).
