# Work Record — council-cli-backends

## task_id
council-cli-backends

## branch
maestro/developer/background-reference-only-agent--council-cli-backends

## base commit
529ccfa (`docs(work-record): fill in the record's own sha and the real file lengths`)

## commit_shas
- cc97f3d — feat(council): headless Claude and Codex CLI backends
- (this record's own commit, added after it)

## CLI versions found on this machine (success criterion 3)

Read on 2026-10-15 from the binaries at `/home/chenry/.local/bin/`:

| CLI | Version found | Version the task expected |
|---|---|---|
| `claude --version` | **`2.1.269 (Claude Code)`** | 2.1.269 — matches |
| `codex --version` | **`codex-cli 0.160.0`** | 0.160.0 — matches |

**No prompt was sent through either real CLI.** Only `--version` and
`--help` were ever run on the real binaries (`claude --help`,
`codex exec --help`, `codex login --help`). `codex exec` was never invoked,
`codex login` was never invoked, nothing under `~/.codex` was modified, and
no value from `~/.codex/auth.json` was printed, logged or stored — only its
key NAMES and their types. Every test in this task drives fake executables
written into `tmp_path`.

The verification is also recorded in the shipped code, in
`cli_common.py`'s module docstring (`cli_common.py:28-106`), which quotes
the installed help text for each flag the two backends use, lists the config
file paths read, and lists the key names observed.

## files_touched

New (nothing was modified):
- `src/kbutillib/domains/ai/council/backends/cli_common.py` (990 lines) —
  `CHILD_ENV_ALLOWLIST`, `CLAUDE_EXTRA_ENV`, `CODEX_EXTRA_ENV`,
  `DENIED_BILLING_VARS`, `QUOTA_PATTERNS`, `AUTH_PATTERNS`, `_child_env`,
  `dropped_billing_vars`, `billing_preflight`, `run_cli`,
  `classify_failure`, `strip_fences`, and the `CliBackend` base.
- `src/kbutillib/domains/ai/council/backends/cli.py` (457 lines) —
  `ClaudeCliBackend`, `CodexCliBackend`, `CODEX_SCHEMA_NAME`,
  `CODEX_OUTPUT_NAME`.
- `tests/domains/ai/council/test_cli_backends.py` (1534 lines, **105 tests**).
- `agent-io/work-records/council-cli-backends.md` (this file).

Byte-identical to the base commit (`git diff --quiet 529ccfa -- <each>`
passes for all of them): `council/__init__.py`, `council/types.py`,
`council/protocols.py`, `council/config.py`,
`domains/ai/argo_utils.py`, `domains/ai/ai_curation_utils.py`,
`pyproject.toml`. The whole diff against base is three new files and
nothing else.

`pyproject.toml` needed no edit: the CLI backends add no dependency at all
(subprocess, json, tempfile and signal are stdlib), so the `council` extra
is untouched.

## environment

The supervisor had provisioned a `.task-venv/` in the worktree and `python`
resolves to it. Installed into THAT venv only; no shared or service venv was
touched, no `ssh`, no live host, no systemd unit, no database:

```
python -m pip install -e '.[council]'                       # SQLAlchemy 2.1.4, psycopg
python -m pip install -e '.[dev,ai]'                        # httpx 0.28.1, pandas, duckdb
python -m pip install pytest pytest-asyncio pytest-cov ruff # pytest 9.1.1
```

CPython 3.12.3, pydantic 2.14.1. `pytest-asyncio` was installed per the
dispatcher's recipe but is **not used**: this suite drives coroutines
through a local `run()` helper that owns its own loop, for the reason the
earlier council milestones recorded (see "design decisions" below).

## verification

| Command | Exit | Result |
|---|---|---|
| `python -m pytest tests/domains/ai/council/ -q -p no:cacheprovider` (base `529ccfa`, clean tree, BEFORE any file was written) | 0 | 529 passed, 243 skipped |
| `python -m pytest tests/domains/ai/council/test_cli_backends.py -q -p no:cacheprovider` | 0 | **105 passed** |
| `python -m pytest tests/domains/ai/council/ -q -p no:cacheprovider` (branch) | 0 | **634 passed, 243 skipped** (= 529 + 105) |
| `python -m ruff check --output-format=concise src/kbutillib/domains/ai/council tests/domains/ai` | 0 | All checks passed |
| full suite slice A, branch | 1 | 26 failed, 3830 passed, 360 skipped |
| full suite slice A, base behaviour | 1 | 26 failed, 3725 passed, 360 skipped |
| full suite slice B, branch | 0 | 1005 passed, 339 skipped |
| `pytest tests/domains/ai/council/test_cli_backends.py tests/interfaces/test_mcp_server.py` | 0 | 128 passed, 3 skipped (event-loop canary) |

Slice A is `tests/annotators arc_context berdl beril_worktree biochem cli
core domains`; slice B is `tests/external guard harness interfaces
kb_app_runner kbase kb_job_utils koros_arc_store modeling
models_and_analyses verab` plus the three top-level test modules. Per-file
ignores on both sides: `tests/biochem/test_escher_utils.py`,
`tests/modeling/test_comprehensive_gapfill_wrapper.py`,
`tests/modeling/test_ms_reconstruction_utils.py`,
`tests/external/test_prott5_utils.py`, `tests/notebook` (no `rdkit`,
`cobra`/`modelseedpy` or `torch` in a fresh task venv). Each `Bash` call
stayed under 600 s (115 s, 109 s, 78 s); every run was in the foreground and
nothing was backgrounded.

**No regressions, compared BY NAME.** `comm -13` (new on branch) and
`comm -23` (gone on branch) over the two sorted slice-A failure-name sets
are **both empty**: the same 26 nodeids fail on base and on branch, and the
+105 passes are exactly this task's new tests. The 26 are the same
pre-existing failures the previous two council milestones recorded — 17 in
`tests/cli/test_kind.py`, 8 in `tests/cli/test_clearinghouse*.py` and 1 in
`tests/core/test_composition_smoke.py`. None is in `tests/domains/`, and
none is in a file this task created.

**How the base side was established.** This change is purely ADDITIVE —
three new files, nothing modified, and no pre-existing module imports them
— so the base's test outcomes are reproduced exactly by running the same
command with `--ignore=tests/domains/ai/council/test_cli_backends.py`. I
used that rather than a checkout dance, because the worktree's git stash is
shared with other sessions and must not be used. The one way a new test
module can affect unrelated tests is by damaging process-wide state
(the event-loop hazard below), and that is checked directly.

`build-baselines/kbutillib.json` was read and deliberately **not** used to
adjudicate: the dispatcher flagged it stale, and it is — captured
2026-09-13 at `0f84803` in a minimal venv, recording 99 failures whose own
note says they trace to missing optional deps. I did not rewrite it; one
exists, so the "if it does not exist" branch of the instruction does not
apply, and replacing it with numbers from a fifth differently-provisioned
venv would not help the next task any more than it helped this one.

### The event-loop canary

`tests/interfaces/test_mcp_server.py` calls the deprecated
`asyncio.get_event_loop()`, and the first council milestone recorded that
`asyncio.run` in a council test breaks it **from a distance** (it leaves the
thread with no current loop, after which that call raises). My `run()`
helper owns and closes its own loop instead, so the canary still passes with
105 CLI-backend tests ahead of it in the same process (128 passed). The
`DeprecationWarning: There is no current event loop` it emits is
**pre-existing** — I confirmed it appears when that module runs alone with
no council test in the process, so it is that module's own call, not
something this task introduced.

### The tests were checked for vacuity

A security test that cannot fail is worse than no test, so I injected each
bug the criteria name and confirmed the right tests catch it. All five
mutations were reverted and both source files verified byte-identical to the
originals afterwards (`diff -q`), before committing.

| Injected bug | Tests that failed |
|---|---|
| A: `home=Path.home()` frozen at import (the default-argument trap) | both `billing_preflight` default-path tests (criterion 2b) |
| B: `parent_env` frozen as an import-time `dict(os.environ)` snapshot | 4, incl. both criterion-2a tests and `test_child_env_reads_the_live_environment_by_default` |
| C: child inherits the whole parent environment (the credential leak) | 9, incl. both criterion-2a tests |
| D: kill only the parent process, not the process group | `test_a_slow_child_times_out_and_its_process_group_is_killed` |
| E: `structured_output` ignored in favour of `result` | `test_structured_output_is_preferred_over_result` |

Mutation A is worth noting for the reviewer: my FIRST attempt to check it
used `-k "default_path"`, which matched only 2 of the 6 tests in the class
(pytest's `-k` does not substring-match `default_path` against the class
name `TestTheDefaultPathInjectsNothing`) and so reported a false "not
caught". Selecting the class by node id showed the two preflight tests
failing as intended. The criteria are satisfied; the first run was a bad
test selection on my part, not a weak test.

## success_criteria_check

**Criterion 1.** PASS.

`cli.py` defines `ClaudeCliBackend` (`cli.py:76`) and `CodexCliBackend`
(`cli.py:303`); `cli_common.py` defines `_child_env` (`cli_common.py:303`),
`dropped_billing_vars` (`cli_common.py:341`) and `run_cli`
(`cli_common.py:626`). Both backends `isinstance`-check against the
`runtime_checkable` `Backend` protocol, and the lazy loader in
`council/__init__.py` already mapped both names to `backends.cli`, so they
resolve as `from kbutillib.domains.ai.council import ClaudeCliBackend`
with no wiring commit.

Every listed case, with the test that pins it:

| Required case | Test |
|---|---|
| exact argv, both CLIs | `TestClaudeArgv::test_the_exact_argv`, `TestCodexArgv::test_the_exact_argv` (full list equality, not membership) |
| no `--bare`, no `--dangerously-skip-permissions` | `TestClaudeArgv::test_the_two_forbidden_flags_are_absent`, `TestCodexArgv::test_the_forbidden_flags_are_absent` |
| schema flag present | `test_the_schema_travels_and_the_user_prompt_does_not`, `TestCodexArgv::test_the_schema_is_written_to_a_file_the_child_can_read` (the child reads the file back and the test compares its contents) |
| user prompt absent from argv | `test_the_schema_travels_and_the_user_prompt_does_not`, `TestCodexArgv::test_the_prompt_arrives_on_stdin_and_not_in_argv` (asserts system AND user absent) |
| prompt on stdin | `TestClaudeArgv::test_the_user_prompt_arrives_on_stdin`, `TestCodexArgv::test_the_prompt_arrives_on_stdin_and_not_in_argv` |
| fresh cwd, gone afterwards | `TestTheWorkingDirectory::test_the_cwd_is_fresh_and_is_removed_afterwards` (parametrised over both; asserts `!= os.getcwd()`, not inside the test tree, and `not exists()`), `..::test_two_calls_get_two_different_directories` |
| none of the 5 vars reaches the child | `TestTheEnvironmentAllowlist::test_no_billing_variable_reaches_the_child` (both CLIs; also asserts no VALUE arrived under another name), plus the criterion-2 pair below |
| `structured_output` preferred | `TestClaudeEnvelopeParsing::test_structured_output_is_preferred_over_result` |
| `result` fallback sets `schema_enforced` False | `..::test_the_result_string_is_the_fallback_and_is_not_schema_enforced`, `..::test_a_fenced_result_string_is_still_parsed` |
| process-group kill on timeout | `TestTimeout::test_a_slow_child_times_out_and_its_process_group_is_killed` (reads the child's real pgid out of its own record and polls `os.killpg(pgid, 0)` until `ProcessLookupError`) |
| quota mapping | `TestFailureMapping::test_a_usage_limit_on_a_non_zero_exit_is_quota_exhausted` (both CLIs), `..::test_every_quota_pattern_maps_to_quota_exhausted` (6 params) |
| preflight refused: Anthropic base URL in an env block | `TestClaudeBillingPreflight::test_an_anthropic_base_url_in_an_env_block_is_refused` |
| preflight refused: `apiKeyHelper` | `..::test_an_api_key_helper_is_refused` |
| preflight refused: API-key-only Codex `auth.json` | `TestCodexBillingPreflight::test_an_api_key_only_auth_file_is_refused` |
| unknown refuses the first call unless `allow_unverified` | `TestTheBillingGate::test_an_unknown_preflight_refuses_the_first_call`, `..::test_allow_unverified_lets_an_unknown_through` |
| docstring records versions/paths/key names | `cli_common.py:28-106` |

`test_every_declared_pattern_is_covered_by_the_table_above` additionally
asserts that the parametrised quota table covers **every** regex in the
shipped `QUOTA_PATTERNS` constant, so the table is the constant rather than
a subset of it.

Two assertions go beyond the list because the obvious test would have
passed on a broken implementation:

- `TestTheBillingGate::test_a_refused_preflight_halts_the_endpoint_without_running_anything`
  asserts `not fake.invoked` — i.e. the CLI process was never started at
  all. A test that only checked the returned error would also pass a
  backend that spent the tokens and then reported a refusal.
- `TestFailureMapping::test_an_is_error_envelope_never_yields_a_parsed_answer`
  sets `is_error` AND a perfectly parseable `structured_output`, so a
  backend that checked the fields in the wrong order would be caught.

**Criterion 2 (no-injection check on the DEFAULT path).** PASS.

`TestTheDefaultPathInjectsNothing` passes **neither `_parent_env` nor
`_home`/`home`** anywhere:

- (a) `test_no_billing_variable_reaches_the_child_on_the_default_path`,
  parametrised over both backends. It `monkeypatch.setenv`s
  `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`, `OPENAI_API_KEY`,
  `CODEX_API_KEY` and `CLAUDE_CODE_SESSION_ID` into the real `os.environ`,
  constructs `ClaudeCliBackend(str(fake))` / `CodexCliBackend(str(fake))`
  with no other arguments, runs `complete()`, and asserts none of the five
  names is in the child's recorded environment and that no value of theirs
  arrived under any other name. It also asserts `child["HOME"]` IS the
  expected value, so the test cannot pass by the child environment being
  empty for an unrelated reason.
- (b) `test_billing_preflight_honours_a_reassigned_HOME_with_no_home_argument`
  — `monkeypatch.setenv("HOME", tmp_home)` with a `tmp_home` whose
  `.claude/settings.json` sets `ANTHROPIC_BASE_URL`, then
  `billing_preflight("claude_cli")` with **no second or third argument** →
  `"refused"`, and the asserted `checked` path is under `tmp_home`.
  `..._for_codex_too` is the same check for the Codex half.

The defaults are implemented as `parent_env=None` / `home=None` and
resolved at call time by `_resolve_env` (`cli_common.py:282`) and
`_resolve_home` (`cli_common.py:293`), each of which carries a comment
saying why the signature default would have been wrong. Mutations A and B
above confirm the tests fail when that is undone.

Note that criterion 2(a) sets `HOME` to a fake clean home as well. That is
not a way of avoiding the default path — it IS the default path, exercised
through `os.environ` — and it is deliberate: without it the test would read
the reviewer's real `~/.codex/auth.json`, and would fail on any machine
whose Codex login happens to be API-key-based. With it, the test asserts
the allowlist and nothing about the machine it runs on.

**Criterion 3.** PASS — see "CLI versions found on this machine" above:
`claude 2.1.269 (Claude Code)`, `codex-cli 0.160.0`, and the explicit
statement that no prompt was sent through either real CLI.

## design decisions not dictated by the prompt

- **`CliBackend`, a shared base, lives in `cli_common.py`.** The billing
  gate, the never-raise guard, `models()`, `effective_params()`, `aclose()`
  and `version()` are identical for both CLIs, and `http_common.py` already
  establishes the pattern (`HttpBackend` holds the common path, `http.py`
  holds the two concrete backends). Putting it anywhere else would have
  duplicated the billing gate — the one piece of this task where a
  divergence between the two backends costs real money.
- **The billing gate runs once per backend instance, not per call and not at
  construction.** Per call would re-read four files per seat per item for an
  answer that cannot change mid-batch; at construction it would turn a
  billing misconfiguration into an import-time crash instead of the typed,
  endpoint-halting failure the worker already knows how to report. Pinned by
  `test_the_gate_is_evaluated_once_per_backend`, which makes the settings
  hostile *after* the gate has cleared and asserts the second call still runs.
- **`allow_unverified` relaxes `unknown` but NEVER `refused`.** The prompt
  says so for `unknown`; it does not say what `allow_unverified` does to a
  refusal. "I know this bills the wrong way" is a different claim from "I
  could not tell", and the flag's name is about the latter. Pinned by
  `test_allow_unverified_does_NOT_let_a_refusal_through`.
- **`billing_preflight` returns a `detail` key as well as the documented
  `state`/`reasons`/`checked`.** `CouncilWorker._preflight`
  (`worker.py:481`) reads `report.get("detail") or report.get("message")`
  to build its refusal message, and neither key is in the prompt's shape —
  so without this the operator would be told literally "no reason given" on
  every refusal. `detail` is the reasons joined; `reasons` and `checked`
  are exactly as specified. Pinned by
  `test_the_preflight_method_is_what_the_worker_calls`.
- **A refusal wins over an unreadable sibling file.** Scanning the Claude
  settings files can produce both signals at once; one file pinning an API
  key is enough to know how this would bill, so `refused` is returned rather
  than `unknown`. `test_a_refusal_wins_over_an_unreadable_sibling`.
- **No Claude settings file at all is `ready`.** The prompt specifies "clean
  settings -> ready" but not "no settings". Informationally they are the same
  — no API-key override means subscription OAuth — and `unknown` would make
  a fresh machine unable to run a CLI seat without `allow_unverified`.
- **An `auth.json` with neither tokens nor an API key is `unknown`, not
  `ready`.** There is nothing there to bill against, so claiming `ready`
  would assert something not checked.
  `test_neither_tokens_nor_key_is_unknown`.
- **Codex tokens win when an API key is also present**, because Codex prefers
  the ChatGPT session — but the reason string says the API key is inert, so
  an operator who set one is told it does nothing rather than left to assume
  it is in use. `test_tokens_win_when_an_api_key_is_also_present`.
- **Quota patterns are checked before auth patterns.** "usage limit reached;
  run /login to switch accounts" matches both, and reading it as auth would
  send an operator to re-authenticate an account that is merely out of
  quota. `test_quota_is_checked_before_auth`.
- **Both quota and auth halt the endpoint; everything else is a retryable
  transport failure.** The prompt specifies this mapping; what it leaves open
  is a zero exit with `is_error` set, which I route through the SAME
  classifier — a usage limit is a usage limit whichever way the CLI delivers
  it. `test_an_is_error_envelope_on_a_zero_exit_is_mapped_too`.
- **`run_cli` takes `prepare` and `collect` hooks.** Codex names two paths
  (`--output-schema`, `-o`) *inside* a directory that does not exist until
  `run_cli` has made it, and its answer has to be read before that directory
  is removed. Rather than hoist the tempdir into both backends (losing the
  single enforcement point for "fresh cwd, removed afterwards"), `prepare`
  builds the argv from the cwd and `collect` reads the output file in the
  last moment before cleanup. `collect` runs even when the child failed,
  because a partial answer file is evidence.
- **`effective_params` is `{"model": ...}` only.** Neither CLI accepts a
  temperature or a token ceiling, so including the seat's requested values
  would make the prompt hash depend on numbers that never reached the model —
  two seats differing only in `temperature` would then miss each other's
  cached answer for nothing. `test_effective_params_is_the_model_alone`
  builds a seat with a non-default temperature and max_output_tokens to pin it.
- **Claude's `usage` is `None` when the envelope reported none**, and a dict
  with possibly-`None` counts when it did. That is the distinction
  `CouncilWorker._enforce_budget` needs to tell "free" from "unmeasurable"
  (`budget_usage_unknown`); inventing zeros would make a budgeted endpoint
  look free for ever. `test_no_usage_block_means_None_not_zero`.
- **`resolved_model` reports ALL `modelUsage` keys, sorted, when there is
  more than one.** More than one key means a fallback kicked in mid-session;
  picking one would hide that it happened.
- **Prompt text is logged at DEBUG only, and only its length.** The council's
  evidence is the user's data, and a batch at INFO would dump every item into
  the log. No error message in either module quotes the prompt.
- **Tests drive coroutines through a local `run()` helper that owns its
  loop**, not `asyncio.run`, for the cross-module reason recorded above and
  in the two earlier council milestones. No `pytest-asyncio` dependency was
  added.

## reconciliation (prompt vs tree)

The prompt is a claim about the code; the code is the fact. Divergences
found, and what I did about each:

1. **`council/__init__.py` already maps both new names.** The prompt forbids
   editing it. Its `_MODULE_MAP` was written in the first milestone with
   `"ClaudeCliBackend": "backends.cli"` and `"CodexCliBackend":
   "backends.cli"` already present, so the module landed with no edit needed
   anywhere. Recorded so a reviewer does not look for a missing wiring
   commit. Verified live, not just read.
2. **`billing_preflight`'s documented return shape does not carry what the
   worker reads.** The prompt specifies `{"state", "reasons", "checked"}`;
   `CouncilWorker._preflight` reads `detail`/`message`. Added `detail`
   (see "design decisions"); the three specified keys are unchanged, so
   nothing that reads the documented shape is affected.
3. **The default-argument trap the dispatcher warned about is real, and the
   prompt's own signatures contain it.** The prompt writes
   `_child_env(kind, parent_env=os.environ)` and
   `billing_preflight(kind, parent_env=os.environ, home=Path.home())`.
   Implemented as `None` defaults resolved at call time, per the
   dispatcher's note and criterion 2. (Worth recording precisely: of the
   two, only `home=Path.home()` would have been a true freeze —
   `os.environ` is a live mapping, so that default would have tracked
   `setenv` by accident. Both are resolved at call time regardless, and
   mutation B confirms the environment half is pinned by a test too.)
4. **`codex exec` accepts `-` on the installed version, so the prompt's
   fallback clause does not apply.** The prompt says to keep `"-"` even if
   the installed codex rejects it. It does not reject it: `codex exec
   --help` documents `[PROMPT]` as "If not provided as an argument (or if
   `-` is used), instructions are read from stdin". Kept `"-"`, now on
   verified rather than assumed grounds.
5. **`ClaudeCliBackend.version()` is `async`, not sync.** The prompt writes
   it as `version() -> runs <exe> --version`. It has to await `run_cli`,
   and every other member of the `Backend` surface is already a coroutine,
   so a sync wrapper would be the odd one out and would need its own event
   loop. Shipped as `async def version()` on the shared base, so both
   backends have it (the prompt only names it for Claude; the doctor needs
   both).
6. **The two backends take an optional `config` mapping.** The prompt's
   signature is `(executable=..., allow_unverified=..., _home=...)`. The
   worker builds backends from `CouncilConfig.endpoint(alias)` and the HTTP
   backends take exactly such a mapping, so a CLI endpoint that could not be
   built the same way would be an inconsistency at the one place both are
   constructed. `config` is read for `name` and `executable` only, and every
   positional form the prompt specifies still works.
   `test_an_endpoint_config_can_name_the_executable`.
7. **`_parent_env` was added alongside `_home` as a test seam.** The prompt
   names only `_home`. Both are `None`-defaulted and resolved at call time,
   and criterion 2's tests pass neither — `_parent_env` exists so the
   remaining ~100 tests can assert the allowlist against a controlled
   environment instead of the ambient one, which is what makes them
   machine-independent.
8. **`CouncilError` has no `message` field constraint that the prompt's
   `BillingGuardRefused` shape implies.** Nothing to change — the typed
   exceptions in `types.py` already carry `kind`/`retryable`/`halts_endpoint`
   as `ClassVar`s and `.to_error()` produces the serialisable form, so the
   backends construct the exception and call `.to_error()` rather than
   building a `CouncilError` by hand. Recorded because the prompt describes
   the two as alternatives ("exceptions AND a serialisable CouncilError").

## inferred rather than verified

- **No call was ever made through either real CLI, so the shape of a real
  reply is taken from the prompt, not observed.** Specifically: that the
  Claude JSON envelope spells its fields `is_error`, `structured_output`,
  `result`, `usage.input_tokens`/`usage.output_tokens` and `modelUsage`;
  and that `codex exec` writes its final message to the `-o` file and
  reports no token usage there. The FLAGS are verified against the
  installed `--help`; the reply BODIES are not. If a field name is wrong,
  `ClaudeCliBackend._usage` / `._resolved_model` / `_run` are the three
  places it changes, and the `envelope()` helper in the test module is the
  one place the fixture changes.
- **`schema_enforced=True` for Codex records what was ASKED, not what was
  proved.** Claude's `structured_output` is positive evidence the CLI
  validated the answer, so `True` there is a fact. Codex's `--output-schema`
  has no counterpart field in the reply, so the backend reports `True` when
  a non-empty schema was passed. The engine's `_validate_answer` validates
  either way, so nothing downstream trusts this; it is a report, not a gate.
- **Real quota and auth messages are not captured.** `QUOTA_PATTERNS` and
  `AUTH_PATTERNS` are the prompt's list, exercised against plausible message
  strings I wrote. A real "usage limit" banner from either CLI has not been
  seen, so the patterns are reasoned rather than measured. They are
  deliberately broad and substring-based, and `classify_failure` falls back
  to a retryable transport failure, which is the safe direction for a
  pattern that misses.
- **The `CLAUDE_CODE_OAUTH_TOKEN` pass-through is untested against the real
  CLI.** That it is a subscription credential rather than an API key is the
  prompt's claim, consistent with `--bare`'s help text (which lists
  `ANTHROPIC_API_KEY` and `apiKeyHelper` as the API-billing routes and says
  OAuth is "never read" under `--bare`). No call was made to confirm the CLI
  honours it.
- **`os.killpg(proc.pid, ...)` assumes the child is its own group leader.**
  That follows from `start_new_session=True` and is exercised for real by
  the timeout test (which reads the child's own `os.getpgid(0)` and confirms
  the group is gone), but only for a child that does not itself call
  `setsid`. A CLI that re-parented its helpers into a third group would
  escape the kill; neither appears to, and nothing in this task could check
  it without running them.
- **PostgreSQL is untouched by this task** and the 243 skipped council tests
  skip for the same reasons as on base (`KBU_COUNCIL_TEST_PG_DSN` unset /
  `store.postgres` not implemented). This task added no store code.
- **The real `~/.claude/settings.json` on this machine is `ready`**, which I
  confirmed by calling `backend.preflight()` once (offline, no spend). No
  test asserts against the real `$HOME`, precisely so this record's claim
  about one machine does not become a test that fails on another.
