# Live acceptance: the five council seat/capture fixes on real CLIs (h100)

Live acceptance for KBUtilLib issues **#76, #77, #78, #79, #92**, run against the
**real `claude` and `codex` CLIs** on h100 (poplar) and a **disposable store on
local disk**. The production council store was opened read-only and never
written.

**Headline: all five PASS.** One finding and three reconciliations are recorded
below; none of them is a defect in the five fixes.

No source file was changed by this task. Its only commit is this document.

## Environment

| | |
|---|---|
| host | `poplar.cels.anl.gov` (h100) |
| branch under test | `maestro/developer/seat-t7-live-acceptance` at `b1d7842e73e599ee20ea11390ec5726409c6e867` — **identical source to `main`**, no commits of this task's own |
| venv | `/scratch/chenry/venvs/seat-t7-20261116` (CPython 3.12.3), `pip install -e '.[council]'` |
| `claude --version` | `2.1.269 (Claude Code)` |
| `codex --version` | `codex-cli 0.160.0` |
| disposable store | `/scratch/chenry/tmp/seat-t7/disposable/council.sqlite` (XFS, local; `/scratch` is not NFS and the council accepted it) |
| config | `/scratch/chenry/tmp/seat-t7/council-t7.yaml` — throwaway, outside the repo. `--config FILE` **replaces** the packaged default (`load_council_config` reads the file instead of merging it), so it is self-contained. |
| production store | `/scratch/chenry/council/council.sqlite`, opened only as `file:...?mode=ro&immutable=1` |

Seats used. Three distinct claude models are seated because
`Roster._check_independence` refuses a critic or meta that repeats a proposer or
each other; `haiku` was confirmed live before the run.

| alias | endpoint kind | served name | canonical |
|---|---|---|---|
| `sonnet-cli` | `claude_cli` | `sonnet` | `anthropic/claude-sonnet-5` |
| `opus-cli` | `claude_cli` | `opus` | `anthropic/claude-opus-5` |
| `haiku-cli` | `claude_cli` | `haiku` | `anthropic/claude-haiku-4.5` |
| `sol-cli` | `codex_cli` | **`gpt-6.1-sol`** | `openai/gpt-6.1-sol` |
| `gpt55-cli` | `codex_cli` | `gpt-5.5` (the packaged name; seated deliberately to fail — see #92) | `openai/gpt-5.5` |

## Step 1 — install, and a fresh store on local disk

```
uv venv --python /usr/bin/python3 --seed /scratch/chenry/venvs/seat-t7-20261116
VIRTUAL_ENV=/scratch/chenry/venvs/seat-t7-20261116 uv pip install -e .            # exit 0
VIRTUAL_ENV=/scratch/chenry/venvs/seat-t7-20261116 uv pip install -e '.[council]' # exit 0
```

`python3 -m venv` cannot be used on this machine (`ensurepip is not available`;
no `python3.12-venv` package and no sudo), so `uv venv --seed` was used instead.
The first `kbu council db init` failed `ImportError: the council store needs
SQLAlchemy 2.x: pip install 'kbutillib[council]'` — expected, the `council`
extra is not part of the base install; installing it fixed it.

## Step 2 — the production store's `go_mf@4` spec digest (READ-ONLY)

Opened as `file:/scratch/chenry/council/council.sqlite?mode=ro&immutable=1`;
digest is `sha256(canonical_json(spec))` using
`kbutillib.domains.ai.council.types.canonical_json`, the same function
`put_query_type` compares with.

```
go_mf rows in the production store:
  go_mf@1 source=builtin   created_by=builtin
  go_mf@2 source=directory created_by=chenry
  go_mf@3 source=directory created_by=chenry
  go_mf@4 source=user      created_by=chenry
stored spec column bytes : 9289
canonical-JSON SHA-256   : 77dab25a779f30d98f89ace4b38b6ea2c0fe6dc04434bf1f83c045d22dff2349
expected                 : 77dab25a779f30d98f89ace4b38b6ea2c0fe6dc04434bf1f83c045d22dff2349
MATCH                                                                    (exit 0)
```

**RESULT: MATCH.** The shipped `builtin/go_mf_v4.yaml`, normalised through
`validate_spec(...).to_spec()`, digests to the same value, so the pin in
`tests/domains/ai/council/test_go_mf_v4_snapshot_pin.py` holds against the live
store and not merely against its own fixture.

All four builtins agree with the production rows, digest for digest:

| builtin | shipped digest | production row |
|---|---|---|
| `go_mf@1` | `d2d6f98acbe4ac84…` | same |
| `go_mf@3` | `401f992cb2a11aaf…` | same (row `source=directory`) |
| `go_mf@4` | `77dab25a779f30d9…` | same (row `source=user`) |
| `reaction_pickup@1` | `501b94fd56149f18…` | same |

Nothing was written. Afterwards the production file is byte-for-byte where it
was — size `1570947072`, mtime `2026-11-02 23:58:22.026269179 -0600` — and
`find /scratch/chenry/council -maxdepth 1 -newermt '2026-11-16 18:25'` returns
**nothing**: no new file, and no `-wal`/`-shm` sidecar was created (which is
what `immutable=1` buys).

## Step 3 — re-seeding builtins

### Into the fresh disposable store

```
kbu council --config $CFG db init --json     # pass 1, exit 0
kbu council --config $CFG db init --json     # pass 2, exit 0
```

Both passes print the identical envelope; neither raises:

```json
{"store": "sqlite:////scratch/chenry/tmp/seat-t7/disposable/council.sqlite",
 "schema_version": 2,
 "query_types": ["go_mf@1", "go_mf@3", "go_mf@4", "reaction_pickup@1"],
 "query_dirs": [], "query_dir_types": []}
```

`go_mf` with no version resolves to **version 4**
(`QueryRegistry.get("go_mf").version == "4"`).

### Into a COPY of the production store

`cp /scratch/chenry/council/council.sqlite
/scratch/chenry/tmp/seat-t7/prodcopy-council.sqlite` (1.57 GB, 38 jobs, 331,613
calls), then:

```
kbu council --config $CFG --store sqlite:////scratch/chenry/tmp/seat-t7/prodcopy-council.sqlite db init --json  # pass 1, exit 0
kbu council --config $CFG --store sqlite:////scratch/chenry/tmp/seat-t7/prodcopy-council.sqlite db init --json  # pass 2, exit 0
```

**No `QueryTypeExists`** on either pass. The `go_mf@4` row still reads
`source=user created_by=chenry` with digest `77dab25a…`: the identical re-put was
a no-op in the store and did not restamp the row's provenance, exactly as
`TestSeedingOverPoplarsExistingRow` predicts. The copy's job and call counts are
unchanged (38 / 331,613).

**Operator note, not a defect.** `db init` on the copy ran `migrate()` and so
`ALTER`-ed in the four `ADDITIVE_COLUMNS` of #92 — `attempts.reply_text`,
`calls.effective_params`, `calls.reuse_namespace`, `jobs.code_version` —
in place, with `schema_version` staying at 2 (that is the documented point of
`ADDITIVE_COLUMNS`). Read against the production store itself, those four
columns are **absent**. So the first process on this branch that opens the real
store read-write will add them. The change is additive and nullable, but it is a
write, and an operator should take it deliberately rather than discover it.

## Step 4 — a mixed claude/codex job, run to idle

Items: three real annotation strings with 40-candidate menus each, taken from the
repo's own fixture `tests/domains/ai/council/fixtures/go_v3_replay_10.json` (rows
2, 3, 8 → `item_id` `631`, `789`, `1711`), converted with the same mapping
`scripts/council/replay_go_v3.py:task_from_input` uses. No prompt text is
recorded in this document or in the throwaway config.

Roster (`profile: t7-mixed`): proposers `sol-cli` (codex) + `sonnet-cli`,
critic `opus-cli`, meta `haiku-cli`.

```
kbu council --config $CFG doctor --profile t7-mixed                               # exit 0
#   seats: ready=2, unknown=2, unavailable=0
#   (the two `unknown` are verified:false, hence --allow-unverified below)
kbu council --config $CFG submit --query go_mf@4 --input .../items3.jsonl \
    --profile t7-mixed --allow-unverified --label t7-step4-mixed --json           # exit 0
#   job_id 92595da1b121463aa5f222373a66aefc, n_items 3, blockers []
kbu council --config $CFG run --job 92595da1b121463aa5f222373a66aefc --json       # exit 0
```

`kbu council jobs status 92595da1b121463aa5f222373a66aefc --json` (identical to
the `run` envelope's `data`):

```json
{"job_id": "92595da1b121463aa5f222373a66aefc", "status": "done", "n_items": 3,
 "by_status": {"ok": 3}, "errored_items": 0, "outcome": "clean",
 "percent_complete": 100.0, "pending_calls_by_role_endpoint": {},
 "blockers": [], "throughput_last_10min": 10}
```

Ten calls, every one `ok`, every one on its first attempt. Per-call usage as
stored in `calls.fresh_usage`:

| item | role | seat | served model | status | prompt_tokens | completion_tokens | cache_creation | cache_read |
|---|---|---|---|---|---|---|---|---|
| 631 | proposer | proposer_1 | `gpt-6.1-sol` | ok | **17,267** | 77 | 0 | 0 |
| 631 | proposer | proposer_2 | `sonnet` | ok | **24,663** | 320 | 24,661 | 0 |
| 631 | critic | critic | `opus` | ok | **21,985** | 410 | 21,983 | 0 |
| 789 | proposer | proposer_1 | `gpt-6.1-sol` | ok | **17,072** | 87 | 0 | 0 |
| 789 | proposer | proposer_2 | `sonnet` | ok | **24,370** | 767 | 11,652 | 12,716 |
| 789 | critic | critic | `opus` | ok | **45,009** | 1,482 | 10,693 | 34,312 |
| 789 | meta | meta | `haiku` | ok | **39,390** | 2,647 | 20,841 | 18,529 |
| 1711 | proposer | proposer_1 | `gpt-6.1-sol` | ok | **17,563** | 127 | 0 | 0 |
| 1711 | proposer | proposer_2 | `sonnet` | ok | **25,261** | 479 | 12,543 | 12,716 |
| 1711 | critic | critic | `opus` | ok | **22,113** | 432 | 9,696 | 12,415 |

Expectations, each checked:

- **No call failed on `$schema` or `invalid_json_schema`.** Zero of the ten
  failed at all, and a store-wide search for `'%$schema%'`,
  `'%invalid_json_schema%'` and `'%no schema with key or ref%'` across every call
  row of every job in this store returns **0 matches**.
- **Claude `prompt_tokens` is in the thousands, not single digits.** Lowest
  claude seat reading is 21,985; the implausible `in=20` of issue #79 is gone.
  The arithmetic is checkable on the row: item 631's critic reads
  `2 (input) + 21,983 (cache_creation) + 0 (cache_read) = 21,985`, i.e. the
  cache fields #92 item 3 said were being dropped are now both kept and summed
  into `prompt_tokens`.
- **Codex usage is non-null with `prompt_tokens` in the thousands** — 17,072 /
  17,267 / 17,563, parsed from the `turn.completed` event of the `--json` stream.

Meta ran on **1 of 3 items** (789), not 3. That is `decide()` working as
specified, not a gap: step 6 of `engine/decide.py` skips meta when the critic
approves and every voted id was picked by every proposer, and items 631 and 1711
were unanimous.

## Step 5 — codex on META

### 5a, first attempt: the meta seat was never reached

Roster (`t7-codex-meta`): proposers `sonnet-cli` + `opus-cli`, critic
`haiku-cli`, meta `sol-cli` (codex).

```
kbu council --config $CFG submit --query go_mf@4 --input .../items3.jsonl \
    --profile t7-codex-meta --allow-unverified --json      # exit 0, job 7cf5e497b3da4ba38fd17ee123d14fcf
kbu council --config $CFG run --job 7cf5e497b3da4ba38fd17ee123d14fcf --json   # exit 0
```

```json
{"job_id": "7cf5e497b3da4ba38fd17ee123d14fcf", "status": "done", "n_items": 3,
 "by_status": {"ok": 3}, "errored_items": 0, "outcome": "clean",
 "percent_complete": 100.0, "blockers": [], "throughput_last_10min": 9}
```

Nine calls, no meta call at all: sonnet and opus agreed unanimously on all three
items and the critic approved, so meta was skipped every time and the codex meta
seat was never exercised. Recorded rather than dropped, because it is the reason
step 5 needed a second attempt — **a roster alone does not guarantee the seat is
reached**.

### 5b, forced meta: `schema_incompatible`, `with_errors`, exit 2

Roster (`t7-codex-meta2`): **step 4's exact bench** — the one observed to need
meta on item 789 — with codex on meta instead of haiku. `allow_meta_overlap:
true` is required because `sol-cli` is then seated twice. Because the proposer
and critic prompts are unchanged, their calls are **reused** from the step-4 job,
so the path to meta is reproduced rather than hoped for.

```
kbu council --config $CFG submit --query go_mf@4 --input .../items3.jsonl \
    --profile t7-codex-meta2 --allow-unverified --json     # exit 0, job 3b552d86a28b4b6a827fd956ac682e24
kbu council --config $CFG run --job 3b552d86a28b4b6a827fd956ac682e24 --json   # EXIT 2
```

```json
{"job_id": "3b552d86a28b4b6a827fd956ac682e24", "status": "done", "n_items": 3,
 "by_status": {"error": 1, "ok": 2}, "errored_items": 1,
 "outcome": "with_errors", "percent_complete": 100.0,
 "pending_calls_by_role_endpoint": {}, "blockers": [],
 "throughput_last_10min": 10}
```

The meta call on item 789:

```
item 789  meta  gpt-6.1-sol  status=error  attempt_count=0  reused=False
  error.kind    = schema_incompatible
  error.message = codex: the answer schema is not valid for OpenAI strict
                  structured outputs, so codex was not run:
                  $: an object node must set additionalProperties to exactly false (True)
```

All three expectations hold:

- meta failed **`schema_incompatible`**, naming the offending keyword, and
  `attempt_count` is 0 — the refusal is local, before the CLI is spawned, so it
  cost no tokens and no thread;
- the job reports **`outcome: with_errors`** (one of the two permitted outcomes;
  `all_errored` would require every item to reach meta, and two were unanimous);
- **`kbu council run` exited 2.**

## The #92 capture probe — a failed-then-retried attempt keeps its reply

`#92` item 1 needs an attempt that **failed, was retried, and whose reply body
survives only on the attempt row**. That shape cannot be produced by either of
the two refusals above: `schema_incompatible` is raised before the CLI runs (no
body), and a `$schema` refusal makes `claude` exit 1 with an empty stdout (also
no body). Two levers were tried and rejected on measurement before this one:
`go_mf@1` on a codex seat is now caught by the local strict lint, and a schema
carrying `minItems` was **accepted** by this provider (exit 0 — so `minItems` is
not a strict-mode refusal on `gpt-6.1-sol` today).

What does work, and is a genuine CLI failure: seat the **packaged** served name
`gpt-5.5`. The account refuses it at 400, `codex` exits 1 with the error on
**stdout**, and `CliBackend` keeps that stdout as the result text
(`cli.py:681` — `text=last or out or None`), which `classify_failure` maps to a
retryable `TransportError`.

```
kbu council --config .../council-t7-92probe.yaml run --query go_mf@4 \
    --input .../items1.jsonl --profile t7-92probe --allow-unverified --json   # EXIT 2
```

```json
{"job_id": "a35ba9359ec04a21b29229e3413c6e8a", "status": "done", "n_items": 1,
 "by_status": {"error": 1}, "errored_items": 1, "outcome": "all_errored",
 "percent_complete": 100.0, "blockers": [], "throughput_last_10min": 2}
```

```
call a7380987  proposer  gpt-5.5  status=error  attempt_count=2
  calls.reply_text   : 783 chars        (written by the FINAL _fail_call only)
  effective_params   : {"model": "gpt-5.5"}
  reuse_namespace    : codex_cli:gpt-5.5
  error.kind         : transport
  attempt 1  outcome=transport  usage=null  reply_text=783 chars   <-- RETRIED
  attempt 2  outcome=transport  usage=null  reply_text=783 chars   <-- RETRIED
  attempt 3  outcome=transport  usage=null  reply_text=783 chars
```

Each of the three attempts carries its own 783-character body. Attempts 1 and 2
are the retried ones — under the old schema their replies would be
unrecoverable, which is precisely the loss #92 item 1 describes. The body is the
provider's own event stream, ending:

```
{"type":"turn.failed","error":{"message":"{\"type\":\"error\",\"status\":400,
 \"error\":{\"type\":\"invalid_request_error\",\"message\":\"The 'gpt-5.5' model
 is not supported when using Codex with a ChatGPT account.\",
 \"code\":\"model_not_found\"}}"}}
```

Store-wide coverage over all four jobs (31 calls, 33 attempts):

| attempt outcome | n | with `reply_text` | `usage` |
|---|---|---|---|
| `ok` | 16 | 0 (the reply is on the call row) | populated |
| `reused` | 13 | 0 | `{"prompt_tokens": 0, "completion_tokens": 0}` |
| `schema_incompatible` | 1 | 0 (no CLI was run) | `null` |
| `transport` | 3 | **3** | `null` |

`calls`: 31 of 31 rows carry `effective_params`, 31 of 31 carry
`reuse_namespace`. `jobs`: all four rows carry
`code_version = {"package_version": "1.0.0", "git_commit":
"b1d7842e73e599ee20ea11390ec5726409c6e867", "git_dirty": false}`.

A failed CLI attempt records `usage` as JSON **`null`** — unknown — rather than
`0`. That is the #79 resolution for a turn that emitted no `turn.completed`, and
it is also the gap t5 left open (a failed call reports no token cost at all);
expected, not a regression.

`endpoint_state` after the four jobs: `claude` `epoch_tokens=451502`, `codex`
`epoch_tokens=52193`. Those figures include the claude cache fields, which is
the budget-guard consequence of #92 item 3 — before that fix the claude figure
would have been short by roughly three orders of magnitude.

## PASS / FAIL

| Issue | Verdict | Evidence |
|---|---|---|
| **#76** — `go_mf@1` proposer `required` omits `notes`, 400 `invalid_json_schema` on every codex seat | **PASS** | 3 of 3 codex proposer calls on `go_mf@4` returned `ok` (17,072–17,563 prompt tokens, 77–127 completion). Zero `invalid_json_schema` across all 31 call rows. `go_mf@4`'s proposer schema is strict-clean, and the local lint refuses a non-strict one for free instead of paying for a 400. |
| **#77** — `$schema` makes the claude CLI exit 1, failing every claude critic/meta call | **PASS** | 7 claude calls (3 sonnet proposer, 3 opus critic, 1 haiku meta) all `ok`. Zero matches for `$schema` / `no schema with key or ref` in any call row's error. |
| **#78** — `status: done` with every item errored and `blockers: []` read as success | **PASS** | Three distinguishable outcomes measured on the same CLI: `outcome=clean, errored_items=0`, exit **0** (step 4); `outcome=with_errors, errored_items=1`, exit **2** (step 5b); `outcome=all_errored, errored_items=1`, exit **2** (the #92 probe). A shell script can no longer read an errored job as success. |
| **#79** — CLI token usage reported as 0 / implausible | **PASS** | Claude `prompt_tokens` 21,985–45,009 against the old `in=20`, and reconstructible from the stored cache fields. Codex `prompt_tokens` 17,072–17,563, non-null, parsed from `turn.completed`. A failed turn records `null` (unknown), not 0. |
| **#92** — three capture gaps | **PASS** on all three | (1) a failed-then-retried attempt keeps its reply: attempts 1 and 2 of call `a7380987` each hold 783 chars while `calls.reply_text` holds only the final one. (2) 31/31 call rows carry `effective_params` and `reuse_namespace`; all 4 job rows carry `code_version` with the git commit. (3) `cache_creation_prompt_tokens` / `cache_read_prompt_tokens` are stored on every claude call and are included in `prompt_tokens` and in `endpoint_state.epoch_tokens`. |

## FINDING — the packaged config's codex served names are wrong, and now measured twice

`src/kbutillib/domains/ai/council/default_config.yaml` seats `gpt54-cli`
(`served_name: gpt-5.4`) and `gpt55-cli` (`served_name: gpt-5.5`) in the default
`mixed-subscription` profile. **This ChatGPT account serves neither.** Measured
directly, and then again through the whole council:

```
{"type":"error","status":400,"error":{"type":"invalid_request_error",
 "message":"The 'gpt-5.5' model is not supported when using Codex with a ChatGPT account.",
 "code":"model_not_found"}}
```

Through the council, as the #92 probe above, the same refusal arrives as a
`transport` error and is **retried three times per call** before the item fails.
So the packaged default profile does not merely fail on this machine: it fails
expensively and looks like a flaky endpoint rather than a wrong name.

The name this account does serve, and the CLI's own default under
`--ignore-user-config`, is **`gpt-6.1-sol`**, which is what every passing codex
call above used. Both seats are correctly marked `verified: false` pending
operator step OP-2; OP-2 should set them to `gpt-6.1-sol`. `default_config.yaml`
was **not** edited by this task (no source changes), and the fix belongs with the
operator step rather than in an acceptance run.

This confirms, live and end to end, the same defect
`tests/domains/ai/council/fixtures/cli_envelopes/README.md` records from the
fixture capture. `codex doctor` will not catch it: it reports the route in
`~/.codex/config.toml`, which the council's argv deliberately bypasses.

## Reconciliations (prompt vs. tree)

1. **"Submit the same items with a roster putting `codex_cli` on META" is not
   sufficient to exercise the meta seat.** `decide()` skips meta on unanimity,
   and the first step-5 roster (sonnet + opus proposers) was unanimous on all
   three items, so the job came back `outcome: clean` with exit 0 and no meta
   call. Fixed by re-running with step 4's bench plus `allow_meta_overlap: true`
   (step 5b), which does reach meta. Both runs are reported.
2. **"codex 0.160.0 emits `turn.completed` usage" is true but not unconditional.**
   A failed turn emits no `turn.completed` at all, so its usage is recorded as
   `null`. The prompt's expectation of non-null codex usage holds for the
   successful calls, which is where it was checked.
3. **The step-2 instruction to use the `sqlite3` URI form was followed, but not
   via the `sqlite3` binary** — there is no `sqlite3` CLI on this host. Python's
   `sqlite3` module with `uri=True` and the same
   `?mode=ro&immutable=1` query string was used instead, which is the same
   SQLite open flags.

## Verification

Live acceptance is the verification for this task, and the command/exit-code
record is above. As a check that the branch itself is sound in the throwaway
venv:

```
TMPDIR=/scratch/chenry/tmp/seat-t7 python -m pytest \
    tests/domains/ai/council tests/domains/ai/llm tests/cli/test_council_cli.py \
    -q -p no:cacheprovider --no-header
# 7 failed, 1847 passed, 77 skipped in 235.01s     exit 1
```

**All 7 are machine-local, and none can be a regression from this task, which
changed no source file** — the worktree's tree is identical to `main` at
`b1d7842`. Six are the set
`build-baselines/kbutillib-h100-main-6310d61.json :: MACHINE_LOCAL_FAILURES_NOT_PRODUCT_DEFECTS`
records by nodeid (`test_types_config.py::TestPackagedDefaultConfig::*`). The
seventh,
`tests/domains/ai/llm/test_static_headers.py::TestPackagedExample::test_the_default_config_declares_no_mag_endpoint`,
is **not** in that recorded list but has the identical documented cause: the h100
user config `~/.kbutillib/config.yaml` leaks past the monkeypatched `HOME`, and
its `ai_council` section declares a `mag` endpoint plus
`store: sqlite:////scratch/chenry/council/council.sqlite` and
`query_dirs: ['/scratch/chenry/council/queries']` — which is exactly what each
failure asserts is absent. That baseline's machine-local list is worth extending
by this one nodeid.

## Artefacts (outside the repo, on ephemeral scratch)

| path | what |
|---|---|
| `/scratch/chenry/tmp/seat-t7/council-t7.yaml` | throwaway config for steps 4, 5a, 5b |
| `/scratch/chenry/tmp/seat-t7/council-t7-92probe.yaml` | same plus the `gpt-5.5` seat |
| `/scratch/chenry/tmp/seat-t7/items3.jsonl`, `items1.jsonl` | the 3 (and 1) items, derived from the repo fixture |
| `/scratch/chenry/tmp/seat-t7/disposable/council.sqlite` | the disposable store holding all four jobs |
| `/scratch/chenry/tmp/seat-t7/prodcopy-council.sqlite` | the production-store copy used for the re-seed check |
| `/scratch/chenry/tmp/seat-t7/step{4,5,5b}_run.json`, `probe92_run.json` | the `run` envelopes quoted above |
| `/scratch/chenry/venvs/seat-t7-20261116` | the venv |

Treat all of them as ephemeral: `/scratch` is shared, unbacked and 93% full on
this host. Everything load-bearing is quoted in this document.
