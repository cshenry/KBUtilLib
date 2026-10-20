# t12-cli-temp-root-wiring

- **task_id**: t12-cli-temp-root-wiring
- **branch**: maestro/developer/t12-cli-temp-root-wiring
- **commit_shas**:
  - e882bbc — fix: thread configured cli_temp_root through to run_cli for both CLI backends

## Summary

`ClaudeCliBackend._run` and `CodexCliBackend._run` in
`src/kbutillib/domains/ai/llm/apis/cli.py` both called `run_cli(...)` without
passing `temp_root`, so a `cli_temp_root` configured on `CouncilConfig` —
already threaded into each backend's `config` mapping as `"temp_root"` by
`CouncilWorker.endpoint_mapping()` (`engine/worker.py:560-571`) — could never
reach `tempfile.TemporaryDirectory(dir=...)` inside `run_cli`. Every CLI
child kept building its per-call working directory under the platform
default (`/tmp`), which is the specific failure mode the PRD exists to fix on
a shared host whose `/tmp` has filled before and taken out unrelated work.

The fix reads `self.config.get("temp_root")` in both `_run` methods and
passes it through as the `temp_root=` keyword `run_cli` already accepts.
`self.config` is the same mapping the base `CliBackend.__init__` already
stores from its `config` argument, so no new plumbing was needed — only the
missing read-and-forward. Absent or `None` is unchanged: `run_cli` treats
`temp_root=None` as "use the platform default," which is exactly what both
backends did before this change.

The argv built by `ClaudeCliBackend.argv()` and `CodexCliBackend.argv()` is
untouched — neither method nor any call into it was modified. The diff is
two one-line additions, each adding only the `temp_root=` keyword argument to
an existing `run_cli(...)` call.

## Files touched

- `src/kbutillib/domains/ai/llm/apis/cli.py` — pass `temp_root=self.config.get("temp_root")` through in both `ClaudeCliBackend._run` and `CodexCliBackend._run`.
- `tests/domains/ai/council/test_cli_backends.py` — new `TestConfiguredTempRoot` class with four tests (see below).

No other files were touched, per the task's file-ownership constraint.

## Success criteria self-assessment

- **A configured temp root places the CLI child's working directory under that root for both the claude and codex backends** — PASS. `test_claude_backend_threads_a_configured_temp_root_to_the_cwd` and `test_codex_backend_threads_a_configured_temp_root_to_the_cwd` both construct the backend with `config={"temp_root": str(root)}`, run a real `complete()` call against a fake executable, and assert `root in child_cwd.parents`. Both pass.
- **An absent temp root reproduces base-commit behaviour** — PASS. `test_claude_backend_without_a_temp_root_reproduces_base_behaviour` and `test_codex_backend_without_a_temp_root_reproduces_base_behaviour` construct the backend with no `config` at all (the exact call shape every pre-existing test in this file already uses) and assert the child's cwd is outside both an unrelated candidate root and the test's own tree — the same shape `TestStdinViaFile.test_temp_root_none_preserves_todays_default_location` already uses for `run_cli` directly, now exercised through each backend's `_run`.
- **The argv is explicitly asserted unchanged in both cases, with every pre-existing argv assertion intact and passing** — PASS. All four new tests assert the full argv list: the Claude tests repeat the exact list from `TestClaudeArgv.test_the_exact_argv`; the Codex tests repeat the exact list (via `_argv_realpaths`) from `TestCodexArgv.test_the_exact_argv`. The pre-existing `TestClaudeArgv` and `TestCodexArgv` classes were not modified and still pass (see test run below). `argv()` itself was not touched in either backend.
- **No test that passed on the base commit fails on the branch** — PASS, within the scoped selection measured below (227 of 228 passed on both base and branch, with the 1 failure being the documented pre-existing machine-local failure, unchanged). The full `tests/` tree was not run, per the machine instructions (it yields spurious setup errors in `tests/core/test_composition_smoke.py` inside a `.maestro` worktree, unrelated to this change), and the wider council subtree (976 tests) was not run either since the task scoped the selection explicitly to `tests/domains/ai/council/test_cli_backends.py tests/domains/ai/llm`.

## My own measured baseline

Command (run against base commit `cdec484`, before any change):

```
TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src /home/chenry/venvs/kbdl/bin/python -m pytest \
  tests/domains/ai/council/test_cli_backends.py tests/domains/ai/llm \
  -q -p no:cacheprovider --no-header
```

Result: **227 passed, 1 failed** in 11.77s.

The 1 failure is the documented pre-existing machine-local failure:
`tests/domains/ai/llm/test_static_headers.py::TestPackagedExample::test_the_default_config_declares_no_mag_endpoint`
(caused by this box's `/home/chenry/.kbutillib/config.yaml` declaring a
`mag` endpoint, per the PRD envelope's known-failures list / dev row 53).
Not mine; did not touch it.

## Tests run (after the change, same command)

```
TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src /home/chenry/venvs/kbdl/bin/python -m pytest \
  tests/domains/ai/council/test_cli_backends.py tests/domains/ai/llm \
  -q -p no:cacheprovider --no-header
```

Result: **231 passed, 1 failed** in 11.65s — the same +4 (the new
`TestConfiguredTempRoot` tests) over the 227 baseline passes, and the same
one pre-existing `test_static_headers.py::...no_mag_endpoint` failure,
unchanged. No regression.

Also ran the new tests alone in isolation first:

```
TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src /home/chenry/venvs/kbdl/bin/python -m pytest \
  tests/domains/ai/council/test_cli_backends.py -q -p no:cacheprovider --no-header -k TempRoot
```

Result: **4 passed** in 2.31s.

## Confirmation: argv unchanged for both backends

- `ClaudeCliBackend.argv()` and `CodexCliBackend.argv()` were not edited at
  all — `git diff` on `cli.py` shows exactly two one-line additions, each
  adding only `temp_root=self.config.get("temp_root"),` to an existing
  `run_cli(...)` call inside `_run`. Nothing in the argv-construction path
  changed.
- The pre-existing `TestClaudeArgv` (`test_the_exact_argv`,
  `test_the_two_forbidden_flags_are_absent`, etc.) and `TestCodexArgv`
  classes were not touched and still pass in full (confirmed by the full
  run above), so every assertion standing between a CLI seat and metered
  billing (`--bare` and `--dangerously-skip-permissions`/
  `--dangerously-bypass-approvals-and-sandbox` absent; `--tools ""`,
  `--system-prompt`, `--no-session-persistence`, `--ignore-user-config`,
  `--sandbox read-only`, `--ephemeral`, `--skip-git-repo-check` all present)
  is intact.
- New `TestConfiguredTempRoot` tests additionally assert the full,
  byte-for-byte argv list (not just a subset of flags) for both backends in
  both the configured-temp-root case and the absent-temp-root case, so a
  future regression in this exact code path is caught at the same place
  the risk was introduced.

## Caveats

- `CliBackend.__init__`'s docstring in `src/kbutillib/domains/ai/llm/apis/cli_common.py`
  (not an owned file) says of the `config` parameter: "Only `name` and
  `executable` are read." That is now slightly stale — `temp_root` is read
  by the two subclasses' `_run` methods, not by `__init__` itself, so the
  claim is still technically true of `__init__` but may read as misleading
  in context. Left untouched per the task's file-ownership constraint
  (`cli_common.py` is the file that was "deliberately fenced off" and is not
  in the owned-files list); flagging it here rather than silently leaving a
  stale-sounding doc.
- Did not run the full `tests/` tree or the full council subtree (976
  tests), per the machine instructions and the task's own scoped-selection
  guidance. The scoped selection (`test_cli_backends.py` + `tests/domains/ai/llm`)
  is the one place `cli.py`'s behaviour is exercised, so it is the correct
  scope for this change; a wider run was not attempted within the time
  budget.
