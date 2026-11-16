# Real CLI output shapes for the two headless council seats

These fixtures are **captured output from the installed CLIs on h100 (poplar)**,
not transcriptions from documentation. They exist so that the usage/result
parsers for issues #77 and #79 can be written against the bytes the CLIs
actually emit, and so that a later CLI upgrade that changes those bytes breaks
a test instead of silently zeroing the council's cost reporting.

Captured 2026-11-16 on poplar (h100), task `task-7e647702`.

## CLI versions

| CLI | `--version` |
|---|---|
| `claude` | `2.1.269 (Claude Code)` |
| `codex` | `codex-cli 0.160.0` |

Both resolve to `~/.local/bin/`. The codex runs authenticate against a ChatGPT
subscription account (`provider: openai`), not an API key.

## Exact argv used

The prompt is never in argv for either CLI -- it goes in on stdin. The trivial
prompt for every capture except the reasoning runs was the single line
`Reply with {"ok": true}`.

### claude

Matches `ClaudeCliBackend.argv` in
`src/kbutillib/domains/ai/llm/apis/cli.py:152` (no web search, so the
`--tools ""` tail is the one that applies):

```
claude -p --output-format json --json-schema '<schema JSON>' \
  --model sonnet --system-prompt 'You reply only with JSON.' \
  --no-session-persistence --tools ''
```

### codex

Matches `CodexCliBackend.argv` in
`src/kbutillib/domains/ai/llm/apis/cli.py:408`, **plus `--json`**, which is
what this task was capturing:

```
codex exec --json --model gpt-6.1-sol \
  --output-schema <file> -o <file> \
  --ephemeral --ignore-user-config --sandbox read-only \
  --skip-git-repo-check -
```

`-o` is `--output-last-message`. `CODEX_SCHEMA_NAME`/`CODEX_OUTPUT_NAME`
(`cli.py:68-69`) are `schema.json` / `last.txt`; the captures here use the
fixture names instead, which changes nothing about the output.

`--model gpt-6.1-sol` is **not** what the packaged config seats -- see
"Codex served names are wrong in the packaged config" below.

## Where token usage lives

### codex: the `turn.completed` event

Usage is on exactly one event per run, emitted last on stdout:

```json
{"type": "turn.completed",
 "usage": {"input_tokens": 14201, "cached_input_tokens": 0,
           "cache_write_input_tokens": 0, "output_tokens": 15,
           "reasoning_output_tokens": 0}}
```

Event type name: `turn.completed`. Field names: `input_tokens`,
`cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`,
`reasoning_output_tokens`. All five are always present, all snake_case.

**A failed turn emits no usage at all.** `codex_nonstrict_output.txt` ends in
`turn.failed`, with no `turn.completed` anywhere in the stream. A parser must
treat "no `turn.completed`" as *unknown*, which is the #79 resolution -- not as
zero.

### claude: the `usage` block of the result envelope

`claude_ok.json` is the whole single-object envelope (`"type": "result"`). The
`usage` block confirms the expected field names:

```
usage.input_tokens                  2
usage.cache_creation_input_tokens   19962
usage.cache_read_input_tokens       0
usage.output_tokens                 52
```

plus `output_tokens_details.thinking_tokens`, `server_tool_use`,
`service_tier`, `cache_creation.{ephemeral_1h,ephemeral_5m}_input_tokens`,
`inference_geo`, `speed`, and an `iterations` array carrying the same four
counters per turn.

**This capture explains the implausible `in=20` in issue #79.** It is not a bug
in the CLI or in the council's arithmetic: `usage.input_tokens` counts only the
input that was neither cache-read nor cache-written. Here a ~1200-char prompt
reports `input_tokens: 2` against `cache_creation_input_tokens: 19962`. Billable
input for a claude seat is therefore
`input_tokens + cache_creation_input_tokens + cache_read_input_tokens`;
reading `input_tokens` alone will under-report by orders of magnitude.

The separate camelCase `modelUsage` block disagrees with `usage`, as previously
observed, and in a newly specific way: it attributes work to **two** models
(`claude-haiku-4-5` at 899 input tokens and `claude-sonnet-5` at 2) while
`usage` reports only the primary model. The council uses `usage`; this fixture
is the evidence for not switching to `modelUsage`.

## Does codex `input_tokens` include `cached_input_tokens`?

**Yes -- `cached_input_tokens` is a component of `input_tokens`, not additional
to it.** This is an inference across two observations rather than a single-run
proof, because the cache never engaged during this capture (see open fact (ii)):

- The external scan of 2026-11-12, same `codex-cli 0.160.0`, recorded
  `input_tokens: 14176` with `cached_input_tokens: 12288`.
- Every run captured here, on the same trivial prompt shape, recorded
  `input_tokens: 14201` with `cached_input_tokens: 0`.

The two totals agree to within 25 tokens. Were `cached_input_tokens` additional,
the 2026-11-12 run's true input would have been 14176 + 12288 = 26464 tokens for
a prompt of the same shape that measures 14201 uncached -- an 86% inflation that
nothing in the prompt accounts for. Component is the only reading consistent
with both numbers.

## Open fact (i): is `reasoning_output_tokens` a component of `output_tokens`?

**Component. Settled directly, by measurement.** Two reasoning runs, differing
only in `model_reasoning_effort`, both returning a final message of near
identical length:

| fixture | final message | `output_tokens` | `reasoning_output_tokens` | difference |
|---|---|---|---|---|
| `codex_ok.jsonl` (no reasoning) | `{"ok":true}` | 15 | 0 | 15 |
| `codex_reasoning.jsonl` | `{"answer":35}` | 47 | 30 | 17 |
| `codex_reasoning_high.jsonl` | `{"answer":2}` | 101 | 84 | 17 |

Between the two reasoning runs, `output_tokens` rose by 54 and
`reasoning_output_tokens` rose by exactly 54, leaving the non-reasoning
remainder pinned at 17 both times. If reasoning tokens were additional, the
13-character message in the high-effort run would have had to cost 101 output
tokens on its own, against 17 for the same message shape at low effort.

So billable output for a codex seat is `output_tokens` as reported. **Adding
`reasoning_output_tokens` to it would double-count.**

`codex_reasoning_high.jsonl` is the only fixture here whose argv carries an
extra flag, `-c model_reasoning_effort="high"`; it is included because it is
what makes the component question decisive rather than merely probable.

## Open fact (ii): is `cache_write_input_tokens` a component of `input_tokens`?

**Unresolved. It stayed 0 in every run, so this capture cannot answer it.**
Stating that plainly rather than guessing, as the task requires.

Fourteen codex calls were made, including six with a byte-identical prompt
prefix back to back, and `cached_input_tokens` and `cache_write_input_tokens`
were **both 0 on every single one**. Two things were ruled out:

- It is not `--ephemeral`. Two runs with `--ephemeral` dropped reported 0 as
  well.
- It is not prompt length. The prefix is ~14.2k tokens, well past any plausible
  minimum-cacheable size, and the 2026-11-12 scan did see a 12288-token cache
  hit at the same CLI version.

So prompt caching was simply not being credited to this account/model on
2026-11-16, and `cache_write_input_tokens` was never exercised. What a parser
should do in the meantime: treat `input_tokens` as the authoritative total and
**do not** add `cache_write_input_tokens` to it. That is the safe default --
it matches the confirmed semantics of its sibling `cached_input_tokens`, and if
it turns out to be additional the error is an under-count on cache-write turns
only, rather than a standing inflation of every turn.

## Confirmed: `-o` still writes the final message under `--json`

`codex_ok_last_message.txt` is the `-o` file from the same run that produced
`codex_ok.jsonl`. Both were written. The file holds the bare final message
`{"ok":true}` with **no trailing newline** (verified with `xxd`), so a parser
must not assume one.

On a failed turn the `-o` file is **not created at all** -- the non-strict run
left no file, so the council must handle a missing output file rather than an
empty one.

## Schema-compatibility findings

| Case | Fixture | Result |
|---|---|---|
| claude, schema carries `"$schema": "https://json-schema.org/draft/2020-12/schema"` | `claude_schema_key_stderr.txt` | **exit 1**, reproduces issue #77 verbatim |
| claude, same schema with `$schema` removed | `claude_ok.json` | exit 0, structured output returned |
| codex, strict schema (`additionalProperties: false`, `required` == `properties`) | `codex_ok.jsonl` | exit 0 |
| codex, `required` omits one property | `codex_nonstrict_output.txt` | **exit 1**, 400 `invalid_json_schema` |
| codex, strict schema **plus** `$schema` and `$id` at root | `codex_schema_key_ok.jsonl` | exit 0 -- codex ignores the key claude rejects |

Issue #77's stderr, reproduced exactly:

```
Error: --json-schema is not a valid JSON Schema: no schema with key or ref "https://json-schema.org/draft/2020-12/schema"
```

The 400 from the non-strict schema names the response-format slot, which is
useful for matching: `Invalid schema for response_format
'codex_output_schema': 'schema.required' is required to include every key in
properties. Missing 'notes'.`

Two further details worth having, both visible in the fixtures:

- The `$schema`/`$id` keys are **billed**. `codex_schema_key_ok.jsonl` reports
  `input_tokens: 14221` against `codex_ok.jsonl`'s `14201` -- the 20-token
  delta is the two extra schema keys. The schema is part of the model input, so
  stripping `$schema` for claude's sake is also marginally cheaper for codex.
- Strictness is enforced **server-side at 400**, not locally by the CLI. The
  CLI accepts the schema file and opens a thread (`thread.started`,
  `turn.started`) before the API rejects it, so the failure costs a round trip.

## Codex served names are wrong in the packaged config

Not what this task set out to capture, but it blocks any live codex seat and was
measured here, so it is recorded rather than dropped.

`default_config.yaml:376-385` seats `gpt54-cli` (`served_name: gpt-5.4`) and
`gpt55-cli` (`served_name: gpt-5.5`), both correctly marked `verified: false`
pending operator step OP-2. **Both are rejected by this account:**

```
{"type":"error","status":400,"error":{"type":"invalid_request_error",
 "message":"The 'gpt-5.5' model is not supported when using Codex with a ChatGPT account.",
 "code":"model_not_found"}}
```

`gpt-5.4`, `gpt-5.1-codex` and `gpt-5-codex` fail the same way. The model this
account does serve, and the built-in default under `--ignore-user-config`, is
**`gpt-6.1-sol`** -- which is why every codex fixture here pins it. OP-2 should
set the codex served names to `gpt-6.1-sol`.

Note that `codex doctor` is **not** a usable check for this: it reports
`default model provider argo` / `model GPT-4.1 · argo` from the user config at
`~/.codex/config.toml`, which the council's argv deliberately bypasses with
`--ignore-user-config`. Doctor describes a route the council never takes.

## Redaction

A success criterion. Applied before commit, then audited by grep:

- `session_id`, `uuid` (claude) and `thread_id` (codex) are replaced with
  same-shape placeholder UUIDs (`00000000-0000-4000-8000-00000000000N`), so a
  parser still sees a UUID-shaped string where the CLI puts one.
- Audited for `/home/`, `/mnt/homes`, `chenry`, `/Users/`, `/scratch/`, email
  addresses, unredacted UUIDs, raw codex thread ids, and
  `sk-`/`api_key`/`bearer`/`account_id`/`org-` patterns. All clean; no account
  id or email appeared in any CLI output in the first place.

`claude_ok.json` is the only fixture reformatted: it was pretty-printed from the
CLI's single line, preserving key order and every value. The `.jsonl` fixtures
keep one event per line, because that framing is itself the thing under test.

## Prompts

Per the repo rule, no prompt text is stored anywhere in this directory beyond
the trivial `Reply with {"ok": true}` quoted above, which is test scaffolding
rather than a council prompt. The two reasoning prompts were throwaway
arithmetic/logic puzzles written for this capture; they are not recorded here
because nothing downstream depends on their text, only on the token counts in
the fixtures.
