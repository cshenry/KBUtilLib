# Work Record: KB_AUTH_TOKEN injection (Task E)

## task_id
kbu-friction-token-injection

## branch
kbu-friction/token-injection

## commit_shas
- 182d3b09b5d01a7e2c5e7f5c9c6d3f4a8b2e1c7d (check with `git log --oneline -1` in the worktree for the exact full SHA)

## summary
In `SharedEnvUtils.__init__` (`src/kbutillib/shared_env_utils.py`), added a
five-line block immediately after `load_environment_variables()` is called:
if `KB_AUTH_TOKEN` is present in `os.environ`, its value is written directly
into `self._token_hash['kbase']` without calling `save_token_file`, giving env-var
tokens precedence over file-sourced tokens while leaving the existing `token=`
constructor param free to override both (it is applied in the block that follows,
unchanged). Added a focused unit-test module
`tests/test_kb_auth_token_injection.py` with four test cases covering: env var
wins over file token, file token returned when env var absent, token file is not
modified on disk when env var is set, and explicit `token=` param still beats env.
The test file was committed with `git add -f` because `.gitignore` ignores
`test_*.py`; `.gitignore` was not modified.

## files_touched
- `src/kbutillib/shared_env_utils.py` — added 8 lines in `__init__` after `load_environment_variables()` call
- `tests/test_kb_auth_token_injection.py` — new file, 4 test cases in `TestKBAuthTokenInjection` class

## success_criteria_check

**With KB_AUTH_TOKEN exported, SharedEnvUtils().get_token('kbase') returns the env value (overriding ~/.kbase/token)**
PASS — `test_env_var_overrides_file_token` constructs with a real kbase token file in tmp_path while KB_AUTH_TOKEN is patched in; asserts the env value is returned.

**With KB_AUTH_TOKEN set, does NOT write to disk**
PASS — `test_env_var_token_not_written_to_disk` reads the file before and after construction and asserts byte-for-byte equality.

**With KB_AUTH_TOKEN unset, returns the file value**
PASS — `test_file_token_used_when_env_var_unset` patches the full environment without KB_AUTH_TOKEN and asserts the file-sourced token is returned.

**Committed unit test asserts both directions and passes**
PASS — all 4 tests pass locally (`python -m pytest tests/test_kb_auth_token_injection.py -v`).

**Composition smoke tests pass**
PASS — `python -m pytest tests/test_composition_smoke.py -v` yields 9 passed, 15 skipped (skips are expected: modelseedpy/cobrakbase/KBase-live not in this venv), 0 failed.

**Acceptance Criterion 11 (env injection + no disk write + file fallback)**
PASS

**Acceptance Criterion 12 (test committed with git add -f; .gitignore not modified)**
PASS — `.gitignore` is unchanged; `tests/test_kb_auth_token_injection.py` is tracked via `git add -f`.

## tests_run
```
python -m pytest tests/test_kb_auth_token_injection.py -v
# 4 passed in 1.21s

python -m pytest tests/test_composition_smoke.py -v
# 9 passed, 15 skipped in 1.34s
```

## caveats
- The `_make_env` helper in the test uses a non-existent dummy path for
  `token_file` (rather than `None`) to avoid a pre-existing bug in
  `read_token_file`: when `token_file` is `None` but `kbase_token_file` exists,
  `read_token_file` is called and then tries `Path(None)` because `self._token_file`
  is also `None`. This is not Task E's concern and is not fixed here; the test
  works around it cleanly. The reviewer may want to note this pre-existing bug for
  a future cleanup task.
- Skipped smoke tests (modelseedpy, cobrakbase, annotation API, mmseqs2, KBase live)
  are environment-driven skips that existed before this task and are unrelated to
  this change.
