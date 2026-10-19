# t4-cli-stdin-from-file

## task_id
t4-cli-stdin-from-file

## branch
maestro/developer/t4-cli-stdin-from-file

## commit_shas
- 471ab416f8b6e0e00d32d74ebdefbf8fca98b80c

## summary
`run_cli` in `src/kbutillib/domains/ai/llm/apis/cli_common.py` used to feed a
CLI child's stdin via `await proc.communicate(stdin_text.encode("utf-8"))`,
pumping the pipe from the same asyncio event loop that also performs the
council's synchronous store I/O. When that loop stalled, the child gave up
waiting on its prompt and exited with "Input must be provided through stdin"
(measured destroying 23 of 24 Claude attempts on a 19,276-item production
run). Fixed by writing the prompt to a file (`STDIN_FILENAME`,
`prompt.stdin`) inside the existing per-call `TemporaryDirectory` and handing
the child that file opened for reading (`stdin=stdin_file`) instead of a
`PIPE`. This removes the parent from the data path: the child reads the file
at its own pace, nothing hangs or raises if the child never reads it (there
is no pipe to write into), and the file is deleted along with the rest of
the per-call working directory, so the prompt still never persists. Also
added an optional `temp_root` keyword parameter to `run_cli`, threaded
straight through to `tempfile.TemporaryDirectory(dir=...)`; `None` (the
default) preserves today's behaviour exactly. The argv construction in
`llm/apis/cli.py` (`--tools ""`, `--system-prompt`, `--no-session-persistence`,
absence of `--bare`/`--dangerously-skip-permissions`) was not touched, and the
process-group timeout/kill/reap behaviour and stdout/stderr capture are
unchanged.

## files_touched
- `src/kbutillib/domains/ai/llm/apis/cli_common.py` -- `run_cli` rewritten to
  write the prompt to a file and pass it as `stdin=`; added `STDIN_FILENAME`
  constant and `temp_root` parameter; updated the function's docstring to
  describe the new mechanism and the bug it fixes.
- `tests/domains/ai/council/test_cli_backends.py` -- added a new
  `TestStdinViaFile` class (7 tests); extended the shared fake-executable
  helper (`make_fake` / `_FAKE_SOURCE`) with an optional `read_stdin` flag so
  a test can produce a fake that never touches stdin at all; added a direct
  import of `STDIN_FILENAME` from `kbutillib.domains.ai.llm.apis.cli_common`
  (not from the `council.backends.cli_common` re-export shim, which this task
  does not own and whose `__all__` was left untouched).

## success_criteria_check
- "A fake executable that echoes stdin receives the complete prompt while the
  prompt is absent from argv" -- PASS. `test_the_child_receives_the_complete_prompt`
  and `test_the_prompt_never_appears_in_argv` cover this directly against
  `run_cli`, independent of either backend's argv construction. Also,
  previously existing `TestClaudeArgv.test_the_user_prompt_arrives_on_stdin`
  and `test_the_schema_travels_and_the_user_prompt_does_not` still pass
  unchanged (verified they exercise the real backend path).
- "the prompt file is gone after run_cli returns" -- PASS.
  `test_the_prompt_file_is_gone_after_run_cli_returns` uses the `collect`
  hook to prove the file exists (with the exact prompt text) while the
  working directory is still present, then asserts both the file and its
  parent directory are gone once `run_cli` has returned.
- "a temp_root argument relocates the working directory" -- PASS.
  `test_temp_root_places_the_working_directory_under_it` asserts the child's
  reported cwd is nested under the given root;
  `test_temp_root_none_preserves_todays_default_location` asserts the
  default (`temp_root=None`) path is NOT under an arbitrary caller-supplied
  directory, i.e. unchanged.
- "a child that never reads stdin neither hangs nor raises out of run_cli" --
  PASS. `test_a_child_that_never_reads_stdin_does_not_hang_or_raise` uses a
  fake built with `read_stdin=False` (added to the shared fake-executable
  harness) and asserts `run_cli` returns within 5s with return code 0 and the
  expected stdout, with no exception raised.
- "the pre-existing argv assertions in test_cli_backends.py are unchanged and
  passing" -- PASS. `TestClaudeArgv` and `TestCodexArgv` (including the
  `--bare`/`--dangerously-skip-permissions` absence checks) were not edited
  and pass in the full targeted run.
- "no test that passed on the base commit fails on the branch" -- PASS, by
  comparison of pass/fail counts (see `tests_run` below): the only failure on
  the branch is the same pre-existing, pre-documented, machine-local failure
  that was already failing on the base commit.

## tests_run
Command (exact, as specified in the envelope):
```
TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src /home/chenry/venvs/kbdl/bin/python -m pytest tests/domains/ai/council/test_cli_backends.py tests/domains/ai/llm -q -p no:cacheprovider --no-header
```
Result: **227 passed, 1 failed** in 11.53s.

The 1 failure is
`tests/domains/ai/llm/test_static_headers.py::TestPackagedExample::test_the_default_config_declares_no_mag_endpoint`,
the same pre-existing, machine-local failure documented in the task envelope
(dev row 53, caused by this box's `~/.kbutillib/config.yaml` declaring a
`mag` endpoint that leaks past the test's isolation). It is not mine to fix
and is not a regression.

Arithmetic check against the measured base (220 passed, 1 failed = 221
total): this task added 7 new tests in `TestStdinViaFile`. 221 + 7 = 228
total, matching 227 passed + 1 failed = 228 observed on the branch. No test
that passed on the base commit fails on the branch.

Did not run the full suite (measured at 3416s against a 600s Bash ceiling);
that is the conductor's regression diff to own, not this task's.

## caveats
- `STDIN_FILENAME` is a new module-level constant in
  `llm/apis/cli_common.py`. It is not re-exported through
  `council/backends/cli_common.py` (the shim module), since that file is
  outside this task's ownership; the test imports it directly from the real
  module. If a later task wants it available via the shim, that is a small,
  separate addition for whoever owns that file.
- Passing an already-open file object as `stdin=` to
  `asyncio.create_subprocess_exec` relies on the standard `subprocess`
  behaviour of duplicating the fd for the child at exec time; the parent's
  handle is closed immediately after the subprocess is created (in a
  `finally`), which is safe and is exercised by every test in the targeted
  run including the Claude/Codex envelope-parsing and billing-preflight
  tests that go through the full backend path.
- `--version` calls (`CliBackend.version()`) pass `stdin_text=""` through the
  same code path and now get an empty prompt file rather than a closed empty
  pipe; `TestTheProtocolSurface.test_version_runs_version_and_nothing_else`
  (pre-existing, unmodified) still asserts `record["stdin"] == ""` and
  passes, confirming no behavioural change there.
- Did not touch `llm/apis/cli.py` (argv construction) per the task's explicit
  instruction, and did not add a config field or read any config in
  `run_cli` -- `temp_root` is a bare parameter, wiring is left to a later
  task as specified.
