# Work Record: t3-llm-web-search-optin

## task_id
t3-llm-web-search-optin (PRD kbu-gene-literature-research-v1)

## branch
maestro/developer/base-kbutillib-main-a274df2-task-t3-llm-web-search-optin

## base
KBUtilLib `main` @ `a274df2` ("docs(work-record): t2-paperblast-local")

## summary

Opt-in live web search in the generic LLM wrapper (`src/kbutillib/domains/ai/llm/`).
A request opts in with `LLMRequest.params["web_search"] = True`; an API type
declares whether it can serve that with a `supports_web_search` class
attribute, read through `getattr(api, "supports_web_search", False)` so the
member stays OPTIONAL. `claude_cli` and `codex_cli` are `True`, `argo` and
`openai` are `False`.

The two CLI argv lists are a live consumer surface (the council asserts them
flag by flag), so the **default path is byte-identical to `a274df2`**: the
web-search branch is taken only on a truthy opt-in, and both "absent" and
explicit `False` take the old branch. Both golden lists are asserted as
literal lists against the argv a real child process recorded.

HTTP API types refuse a web-search request with `ok=False` /
`kind="invalid_output"` before resolving a credential or opening a socket,
and `web_search` is stripped from the params mapping that reaches a
chat-completions body so a caller that always writes `web_search=False`
cannot have it posted as a model parameter.

## files_touched

- `src/kbutillib/domains/ai/llm/types.py:68` — new `WEB_SEARCH_PARAM = "web_search"`
  constant; new `wants_web_search(request)` helper (truthy-only, accepts
  anything `as_llm_request` reads, so a council `SeatSpec.extra` block opts in
  through configuration with no second code path); `LLMRequest.params`
  docstring records the special key. Both added to `__all__`.
- `src/kbutillib/domains/ai/llm/protocol.py:19` — module docstring now
  documents FOUR optional members instead of three, the fourth being
  `supports_web_search: bool` with default `False`, and records WHY it is
  neither in `REQUIRED_MEMBERS` nor a member of the `ApiType` protocol body
  (either would break every pre-existing third-party type: registration
  refuses a missing required member, and `runtime_checkable` checks that a
  data member exists). No code change — `REQUIRED_MEMBERS` is untouched.
- `src/kbutillib/domains/ai/llm/__init__.py:421` — new public
  `supports_web_search(api_type, endpoint=None) -> bool`, synchronous and
  free, beside the other probes; added to `__all__`.
- `src/kbutillib/domains/ai/llm/apis/cli_common.py:85` — new
  "Web search, re-read off the SAME installed CLIs, 2026-10-20" section in the
  module docstring, quoting the help lines and the probe exit codes (below);
  `CliBackend.supports_web_search: bool = False` as the inherited-safe default.
- `src/kbutillib/domains/ai/llm/apis/cli.py:71` — new
  `CLAUDE_WEB_SEARCH_TOOLS`, `CLAUDE_WEB_SEARCH_ALLOWED_TOOLS`,
  `CODEX_WEB_SEARCH_FLAG` constants (exported, so a test cannot assert a
  spelling the shipped code does not use); `supports_web_search = True` on
  both backends; the web-search branch in `ClaudeCliBackend.argv` and
  `CodexCliBackend.argv`.
- `src/kbutillib/domains/ai/llm/apis/http_common.py:262` —
  `HttpBackend.supports_web_search: bool = False`; the refusal as the FIRST
  thing in `HttpBackend._complete`; `_model_params` now drops
  `WEB_SEARCH_PARAM` as well as `extra_body`.
- `src/kbutillib/domains/ai/llm/apis/http.py:24` — module docstring records
  the refusal and where it lives; explicit `supports_web_search = False` on
  `ArgoBackend` and `OpenAICompatibleBackend`, each with its own reason.
- `tests/domains/ai/council/test_cli_backends.py` — `TestClaudeWebSearchArgv`,
  `TestCodexWebSearchArgv`, `TestCliBackendsDeclareWebSearchSupport` (+ the
  `llm_request` / `_fill` helpers and the two literal golden lists).
- `tests/domains/ai/council/test_http_backends.py` —
  `TestWebSearchIsRefusedWithoutARequest`.
- `tests/domains/ai/llm/test_llm_wrapper.py` — `TestWebSearchCapability`
  (+ `SearchingApi`, `_builtin_endpoint`).

## the argv lists

Claude, default path — **byte-identical to `a274df2`**:

```
[exe, '-p', '--output-format', 'json', '--json-schema', <schema>,
 '--model', m, '--system-prompt', s, '--no-session-persistence',
 '--tools', '']
```

Claude, `params={"web_search": True}`:

```
[exe, '-p', '--output-format', 'json', '--json-schema', <schema>,
 '--model', m, '--system-prompt', s, '--no-session-persistence',
 '--tools', 'WebSearch,WebFetch',
 '--allowedTools', 'WebSearch', 'WebFetch']
```

The `--tools ''` pair is REPLACED, not supplemented (asserting
`argv.count('--tools') == 1`), and the variadic `--allowedTools` is appended
LAST because it swallows every following non-flag token — which is safe
because the prompt is on stdin.

Codex, default path — **byte-identical to `a274df2`**:

```
[exe, 'exec', '--model', m, '--output-schema', <file>, '-o', <file>,
 '--ephemeral', '--ignore-user-config', '--sandbox', 'read-only',
 '--skip-git-repo-check', '-']
```

Codex, `params={"web_search": True}` — one token, BEFORE the subcommand:

```
[exe, '--search', 'exec', '--model', m, '--output-schema', <file>, '-o', <file>,
 '--ephemeral', '--ignore-user-config', '--sandbox', 'read-only',
 '--skip-git-repo-check', '-']
```

`--sandbox read-only` and `--ignore-user-config` are unchanged. Search is the
provider's own tool reading the web, not the agent writing to disk or loading
a `config.toml` that could re-route the billing, so neither reason to be
strict has gone away.

## installed-CLI verification (the CONFRONT addendum)

Versions on h100 (`poplar`), both exactly as `cli_common.py` already recorded:

```
$ claude --version
2.1.269 (Claude Code)
$ codex --version
codex-cli 0.160.0
```

### Codex: the help lines used

`--search` is a **TOP-LEVEL** option. It appears in `codex --help` and is
**absent** from `codex exec --help`:

```
$ codex --help
      --search
          Enable live web search. When enabled, the native Responses `web_search` tool is available
          to the model (no per‑call approval)
```

```
$ codex exec --help | grep -i search
      (no --search entry; only --output-schema, --sandbox, --ephemeral,
       --ignore-user-config, --skip-git-repo-check, --strict-config, ...)
```

Placement was MEASURED on the installed version, not assumed:

```
$ codex --search exec --help ; echo "exit=$?"
Run Codex non-interactively
Usage: codex exec [OPTIONS] [PROMPT]
exit=0

$ codex exec --search --help ; echo "exit=$?"
error: unexpected argument '--search' found
  tip: to pass '--search' as a value, use '-- --search'
Usage: codex exec [OPTIONS] [PROMPT]
exit=2
```

So the flag MUST precede `exec`; after it the CLI exits 2. The complete
web-search command line was parse-checked too:

```
$ codex --search exec --model gpt-5 --output-schema /tmp/s.json -o /tmp/o.txt \
    --ephemeral --ignore-user-config --sandbox read-only \
    --skip-git-repo-check --help ; echo "exit=$?"
Run Codex non-interactively
exit=0
```

**No `-c` config-override equivalent is documented** in either help text
(`codex --help | grep -i 'web_search\|tools\.'` matches only the `--search`
description line itself), so the top-level flag is used rather than a guessed
`-c tools.web_search=true`.

### Claude: the help lines used

```
$ claude --help
  --tools <tools...>                    Specify the list of available tools from
                                        the built-in set. Use "" to disable all
                                        tools, "default" to use all tools, or
                                        specify tool names (e.g.
                                        "Bash,Edit,Read").
  --allowedTools, --allowed-tools <tools...>
      Comma or space-separated list of tool names to allow (e.g. "Bash(git *)
      Edit")
```

That documents the comma-separated `--tools` value and the variadic
`--allowedTools`. Acceptance was then confirmed by a **parse-only probe** — a
command line ending in a deliberately invalid sentinel flag, so the CLI
reports the first option it does not recognise and exits without sending a
prompt:

```
$ claude -p --output-format json --json-schema '{"type":"object"}' \
    --model sonnet --system-prompt s --no-session-persistence \
    --tools 'WebSearch,WebFetch' --allowedTools WebSearch WebFetch \
    --zzz-not-a-real-flag ; echo "exit=$?"
error: unknown option '--zzz-not-a-real-flag'
exit=1
```

Only the sentinel is reported, i.e. every option before it was recognised.
The control proves the probe is meaningful and free:

```
$ claude -p --definitely-not-a-flag 'say OK' ; echo "exit=$?"
error: unknown option '--definitely-not-a-flag'
exit=1
```

`--help` is NOT usable as a parse check for this CLI — commander
short-circuits on it and prints help even alongside an unknown option (both
the real flags and the sentinel exited 0 that way), which is why the sentinel
form was used instead.

**No prompt was sent through either real CLI and no token was spent**, so the
claim at the top of `cli_common.py`'s docstring still holds.

## success_criteria_check

- **Every built-in API type exposes `supports_web_search`, True only for
  `claude_cli` and `codex_cli`** — PASS.
  `TestWebSearchCapability::test_each_builtin_reports_its_own_answer`
  (parametrised over all four) and
  `::test_only_the_two_cli_types_can_search`, which states it as a SET so a
  new built-in cannot quietly join the true side.
- **`llm.supports_web_search()` exists** — PASS. Published in `llm.__all__`
  (`::test_it_is_part_of_the_published_surface`), raises `KeyError` for an
  unknown type like every other verb, and asking sends nothing
  (`::test_asking_costs_nothing_and_sends_nothing`).
- **With `params['web_search']` absent or false both CLI argv lists are
  byte-identical to the base commit's** — PASS. Both
  `test_without_the_opt_in_the_argv_is_the_base_commit_s_byte_for_byte` tests
  are parametrised over `absent` / `{}` / `False` / `None` and compare against
  a LITERAL golden list (`CLAUDE_BASE_GOLDEN`, `CODEX_BASE_GOLDEN`), not
  against anything derived from the code under test.
- **With it true they carry the flags documented by the installed CLIs, and
  Codex stays `--sandbox read-only`** — PASS.
  `TestClaudeWebSearchArgv::test_the_opt_in_carries_the_documented_flags`,
  `TestCodexWebSearchArgv::test_the_opt_in_adds_search_before_the_exec_subcommand`
  (which also asserts `argv.index('--search') < argv.index('exec')`) and
  `::test_the_sandbox_stays_read_only_and_the_user_config_stays_ignored`.
- **HTTP types return `ok=False` without a request** — PASS.
  `TestWebSearchIsRefusedWithoutARequest::test_a_web_search_request_is_refused_and_nothing_is_sent`
  asserts `recorder.requests == []` for both `argo` and `openai`, with the
  ARGO credential PRESENT so the refusal cannot be confused with the
  missing-username `auth` failure; a sibling test deletes `ARGO_USER` and
  proves the web-search refusal is still what is reported, i.e. the guard runs
  before `_bearer`.
- **`web_search` is never forwarded as a model parameter** — PASS.
  `::test_web_search_is_never_posted_as_a_model_parameter` (parametrised over
  `False` / `None` / `0`) reads the POSTed body and asserts the key is absent
  while a sibling `top_p` IS still forwarded, so this is not "params dropped
  wholesale"; `::test_web_search_is_absent_from_effective_params_too` covers
  the cache-key surface.
- **A third-party API type without the attribute still registers and reports
  False** — PASS.
  `::test_a_third_party_type_without_the_attribute_registers_and_reports_false`
  first asserts `not hasattr(ScriptedApi, "supports_web_search")`, then that it
  registers, that `isinstance(api, ApiType)` still holds, and that the answer
  is `False`. `::test_it_is_not_a_required_member` pins
  `REQUIRED_MEMBERS == ("complete", "effective_params")` and that the
  attribute is not in `ApiType.__protocol_attrs__`.
- **NO-INJECTION CHECK** — PASS. Every test in `TestClaudeWebSearchArgv` and
  `TestCodexWebSearchArgv` goes through the backend's REAL execution path:
  the pre-existing `make_fake` writes an executable Python script into
  `tmp_path`, the backend's own `run_cli` / `asyncio.create_subprocess_exec`
  runs it, the child dumps the argv it received to a JSON file, and the
  assertions read that file. Neither `argv` nor the runner nor anything else
  is monkeypatched; the executable is supplied as the backend's configured CLI
  path. Both the web-search-true argv AND the byte-identical default argv are
  asserted from that recorded file.
- **The work record quotes the codex help lines used** — PASS, above.

## tests_run

Targeted, per the cached-baseline rule. All runs:
`TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src /home/chenry/venvs/kbdl/bin/python -m pytest <sel> -q -p no:cacheprovider --no-header`

| selection | result | exit |
|---|---|---|
| `tests/domains/ai/council/test_cli_backends.py` | 137 passed | 0 |
| `tests/domains/ai/council/test_http_backends.py` | 102 passed | 0 |
| `tests/domains/ai/llm/` | 127 passed, 1 failed | 1 |
| `tests/domains/ai/` (whole tree) | 1150 passed, 7 failed | 1 |
| `tests/cli/test_council_cli.py` | 32 passed | 0 |
| `tests/core/test_composition_smoke.py` | 44 passed, 4 skipped, 1 xfailed, 8 errors | 1 |

The same three test files, measured on the base export at `a274df2` and on
this branch, so the 44 added tests are an exact count rather than an estimate:

| file | base `a274df2` | branch | added |
|---|---|---|---|
| `test_cli_backends.py` | 116 passed | 137 passed | +21 |
| `test_http_backends.py` | 91 passed | 102 passed | +11 |
| `test_llm_wrapper.py` | 76 passed | 88 passed | +12 |
| the three together | 322 passed, 1 failed | 366 passed, 1 failed | +44 |

The one failure is the same pre-existing nodeid on both sides (see below).

## regression framing

`build-baselines/kbutillib-h100-main-6310d61.json` is the cached h100 baseline
(69 failing nodeids at `6310d61`, listed in
`/scratch/chenry/build-baselines/kbutillib-6310d61-failed.txt`). Comparison is
BY NODEID.

The 7 failures in `tests/domains/ai/` are **all pre-existing**:

- 5 are in the cached set verbatim —
  `test_types_config.py::TestPackagedDefaultConfig::{test_every_packaged_profile_builds_a_roster,
  test_the_packaged_config_declares_no_poplar_endpoint,
  test_on_a_fresh_machine_with_no_kbutillib_config,
  test_cli_endpoints_default_to_one_call_at_a_time}` and
  `test_static_headers.py::TestPackagedExample::test_the_default_config_declares_no_mag_endpoint`.
- 2 are NOT in the cached set (it was captured at the older `6310d61`):
  `test_types_config.py::TestPackagedDefaultConfig::{test_default_profile_is_mixed_subscription,
  test_codex_seats_are_marked_unverified}`.
  Rather than assume, the base state was MEASURED: `git archive a274df2` was
  exported to `/scratch/chenry/t3base` and the two files run there against
  pristine base source — the result is the **same 7 failures, same nodeids**.
  So they are pre-existing at my base, not regressions.

All 7 are the documented h100 config leak: `/home/chenry/.kbutillib/config.yaml`
reaches these tests despite their monkeypatched `HOME`, so the resolved
`CouncilConfig` carries the user's `mag` endpoint and `smoke` profile. The
baseline file records this as `MACHINE_LOCAL_FAILURES_NOT_PRODUCT_DEFECTS`
with "these would PASS on a clean machine ... do not fix them inside a
council task". Not touched.

The 8 errors in `tests/core/test_composition_smoke.py` are optional-dependency
collection errors (rdkit/cobra/escher absent from this venv) and reproduce
identically on the base export.

**No test that passed on `a274df2` fails on this branch.**

## inferred rather than verified

- **`claude`'s web-search flags were parse-checked, not exercised.** The
  sentinel probe proves `--tools WebSearch,WebFetch` and
  `--allowedTools WebSearch WebFetch` are RECOGNISED options on 2.1.269. It
  does not prove a real `claude -p` run with them actually performs a search —
  that needs a live model call, which this suite is forbidden from making and
  which would spend subscription tokens. Same for `codex --search`: parse
  verified, behaviour not.
- **Both tools are pre-allowed** (`--allowedTools WebSearch WebFetch`) rather
  than `WebSearch` alone, which is what the task prompt specified. The
  reasoning recorded in the constant's docstring — under `-p` there is nobody
  to approve a tool that is available but not allowed, so omitting `WebFetch`
  would let the model find a URL and then be unable to read it — is my
  inference from the headless mode's semantics, not something the help text
  states.
- **`CliBackend.effective_params` still returns `{"model": ...}` only**, so a
  web-search call and a plain one fingerprint identically for the CLI types.
  For a caller that content-addresses answers in a cache that is a real
  collision, but changing it would move the council's existing cache keys and
  is outside this task. Flagged, not fixed. The HTTP types do not have the
  problem (they refuse the request outright).
- **`ruff` is not installed in `/home/chenry/venvs/kbdl`** and no `ruff`
  binary is on `PATH` on this machine, so the repo's configured lint was NOT
  run. Style was matched by hand against the surrounding modules.

## divergences reconciled (prompt vs. tree)

1. **"if `src/kbutillib/domains/literature/cache.py` does not exist, create
   it"** — it EXISTS (landed by t1), together with `europepmc.py` and
   `paperblast.py`, and `tests/literature/` exists too. That whole paragraph
   is PRD-wide boilerplate repeated in every task of this PRD and has nothing
   to do with t3, which is confined to `domains/ai/llm/`. Nothing created, no
   literature file touched.
2. **"New code goes in a NEW package `src/kbutillib/domains/literature/`"** —
   same boilerplate; contradicted by the task's own `TASK:` line, which names
   `src/kbutillib/domains/ai/llm/`. The specific, task-level instruction wins.
3. **"find the argv tests under `tests/` by grepping for
   'no-session-persistence' and 'ignore-user-config'"** — accurate. They are
   in `tests/domains/ai/council/test_cli_backends.py` (not under
   `tests/domains/ai/llm/`), which imports the backends through the council
   re-export shim `council.backends.cli`. Extended there, importing the three
   new flag constants directly from `llm.apis.cli` because the shim
   re-exports only the M1 surface.
4. **"HTTP API types (argo, openai in `apis/http.py`): return
   `LLMResult(ok=False, ...)`"** — the single HTTP request path is
   `HttpBackend._complete` in `apis/http_common.py`, not in `apis/http.py`;
   neither concrete class has a `complete` of its own. The guard therefore
   went in the shared base (one copy, and a future HTTP backend cannot miss
   it), with `supports_web_search = False` declared explicitly on both classes
   in `apis/http.py` and the arrangement recorded in that module's docstring.
   Observable behaviour is exactly as specified for both `argo` and `openai`.
5. **"`ClaudeCliBackend.argv` ... replace the `'--tools', ''` pair"** — the
   base `argv` was a single literal `return [...]`, so it was restructured
   into a common prefix plus one of two tails. The default tail reproduces the
   base list byte for byte, which the golden tests assert.
6. **"add the web-search enablement documented by the installed
   `codex exec --help` / `codex --help`"** — `codex exec --help` documents
   none; only `codex --help` does, as the CONFRONT addendum anticipated. Flag
   placed before `exec`, measured as described above.
7. **`protocol.py`: "add a class attribute `supports_web_search: bool` to
   every API type ... document it in `llm/protocol.py`"** — documented in the
   module docstring but deliberately NOT added to the `ApiType` protocol
   class body. `ApiType` is `runtime_checkable` and `isinstance` checks that a
   data member EXISTS, so adding it there would make
   `isinstance(third_party_api, ApiType)` false — breaking, among others, the
   pre-existing
   `TestBuiltins::test_each_builtin_resolves_by_name_to_a_conforming_api_type`
   contract for any plug-in. This is the same reason the prompt gives for
   keeping it out of `REQUIRED_MEMBERS`, applied to the protocol body as well.
