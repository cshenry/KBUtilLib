# Work Record — gt-adjudicate-live-smoke

## task_id
task-c2ad0f0f (PRD go-mf-council-autoresearch-v1, task 3 of 9)

## branch
maestro/developer/gomf-gt3-adjudicate-live-smoke

## base_commit
fd8c022 — `docs(council): work record for the go_mf_adjudicate@1 build`

## machine
h100 / poplar.cels.anl.gov. Every model call below is a REAL frontier-backend
call made from this host on 2026-11-17 (UTC).

## precondition
MET. `src/kbutillib/domains/ai/llm/apis/schema_compat.py` is present on the
base commit (11,253 bytes), i.e. the `council-seat-schemas-and-capture-v1`
build has landed.

## headline

`go_mf_adjudicate@1` and the `adjudicate` roster work against the live
frontier backends. One job, five real Bench A conflict items, **21/21 live
calls answered, zero seat failures, 5/5 items settled** (success criterion is
≥4/5), and the adjudication's own research fields were read back out of
`calls.reply_parsed` for every proposer, the critic and the one meta call.

No seat produced `schema_incompatible`, `model_unavailable`, `timeout` or
`parse`. The run needed no `--allow-unverified` and no `--allow-unavailable`.

| | |
|---|---|
| job_id | `92e2aa9a4a4d4fd9b3c0a65dc50541c6` |
| label | `gt3-adjudicate-live-smoke` |
| query type | `go_mf_adjudicate@1` |
| profile | `adjudicate` |
| roster_fingerprint | `3946f77f5634263c8e85f1b7effac31dc74ef50cfa412e0724e9f2393617981f` |
| store | `sqlite:////scratch/chenry/tmp/gomf-gt3/adjudicate-smoke.sqlite` (new, local XFS, throwaway) |
| submitted | 2026-11-17 02:09:49.700821 by `chenry` on `poplar.cels.anl.gov` |
| terminal status | `done`, `by_status {"ok": 5}`, `errored_items 0`, `outcome "clean"` |
| wall clock | 02:09:57.292 → 02:13:45.244 = **227.95 s** for 21 calls |
| job token total | prompt 549,872 / completion 25,067 |

## the roster, as it actually resolved on h100

`kbu council --config <temp> doctor --profile adjudicate` reported
`{"ready": 5, "unknown": 0, "unavailable": 0}` BEFORE anything was spent.

| role | seat | endpoint/kind | served_model | canonical_model | verified |
|---|---|---|---|---|---|
| proposer | proposer_1 | claude / `claude_cli` | `opus` | `anthropic/claude-opus-5` | true |
| proposer | proposer_2 | codex / `codex_cli` | `gpt-5.6-sol` | `openai/gpt-5.6-sol` | true |
| proposer | proposer_3 | argo / `argo` | `gemini35flash` | `google/gemini-3.5-flash` | true |
| critic | critic | claude / `claude_cli` | `sonnet` | `anthropic/claude-sonnet-5` | true |
| meta | meta | argo / `argo` | `claudeopus5` | `anthropic/claude-opus-5` | true |

Doctor detail worth keeping: both `claude_cli` seats reported *"no Claude
settings file sets an Anthropic API credential, base URL or apiKeyHelper; the
CLI will use subscription OAuth"*, CLI version `2.1.269 (Claude Code)`; the
codex seat reported *"/home/chenry/.codex/auth.json: holds a ChatGPT 'tokens'
block, so calls bill against the ChatGPT subscription"*, `codex-cli 0.160.0`;
both ARGO seats answered a live `/models` listing (`models_listed: 100`).

### argo-opus5 and opus-cli DO resolve to the same canonical model

**Yes — they are one canonical model on h100, so `allow_meta_overlap: true` is
load-bearing and the seating is refused without it.**

* `argo-opus5` is declared in h100's `~/.kbutillib/config.yaml` with
  `canonical: anthropic/claude-opus-5` outright (served_name `claudeopus5`).
* `opus-cli` carries no `canonical:`, so `default_canonical("opus-cli",
  "opus", "claude_cli")` derives it from the built-in `claude_cli` identity
  table — and returns the same string, `anthropic/claude-opus-5`.

This is exactly what the commented ADJUDICATE block predicted. Its documented
fallback (move meta to `argo-sonnet5`) does NOT remove the overlap: h100
declares `argo-sonnet5` as `anthropic/claude-sonnet-5`, which is the
`sonnet-cli` critic's canonical, so meta would collide with the critic
instead and the flag would still be required. The block already records that
caveat. **The meta seat was therefore left on `argo-opus5` with the flag set,
not moved.** The three PROPOSERS are mutually independent either way
(`anthropic/claude-opus-5`, `openai/gpt-5.6-sol`, `google/gemini-3.5-flash`),
which is the invariant the vote tally rests on.

## per seat

Latency is the per-attempt `attempts.finished_at - attempts.started_at`.
Tokens are summed from `attempts.usage` across that seat's five (one, for
meta) attempts. There were **zero retries**: `attempt_count` is 0 and every
seat has exactly one attempt per call, `outcome_kind = "ok"`.

| seat | role | calls | answered | failed | error class | latency s (min / median / max) | prompt tok | completion tok | cache created / read |
|---|---|---|---|---|---|---|---|---|---|
| proposer_1 (`opus`, claude_cli) | proposer | 5 | **5** | 0 | — | 8.8 / 20.6 / 35.0 | 249,453 | 7,090 | 73,409 / 176,024 |
| proposer_2 (`gpt-5.6-sol`, codex_cli) | proposer | 5 | **5** | 0 | — | 7.0 / 8.2 / 9.3 | 84,486 | 958 | 0 / 13,568 |
| proposer_3 (`gemini35flash`, argo) | proposer | 5 | **5** | 0 | — | 5.6 / 7.7 / 10.2 | 12,537 | 5,460 | 0 / 0 |
| critic (`sonnet`, claude_cli) | critic | 5 | **5** | 0 | — | 8.4 / 23.1 / 49.2 | 198,697 | 10,398 | 70,053 / 128,628 |
| meta (`claudeopus5`, argo) | meta | **1** | **1** | 0 | — | 15.3 / 15.3 / 15.3 | 4,699 | 1,161 | 0 / 0 |

Per-item latency, same order as the item table below (`-` = not called):

```
proposer_1    16.0   21.8    8.8   20.6   35.0
proposer_2     7.3    8.2    7.0    9.3    8.4
proposer_3     7.4   10.2    7.7    8.9    5.6
critic        23.1   49.2    8.4    9.0   34.2
meta             -   15.3      -      -      -
```

The meta seat was invoked on ONE item only: the engine skips meta when the
proposers are unanimous (`verdict.meta_skipped == true` on the other four), so
21 live calls = 5x3 proposers + 5 critic + 1 meta. That is the engine
behaving as designed, not a seat failure.

**No seat failed, so nothing had to be swapped and nothing is recorded with an
error class.** Had one failed it would appear here with its class; the schedule
below is what was checked for and not found:
`schema_incompatible` 0, `model_unavailable` 0, `timeout` 0, `parse` 0,
`transport` 0, `rate_limited` 0. `calls.error` is NULL on all 21 rows.

Two measured notes against the executor notes:

* The seat-schemas t7 finding that the packaged `gpt54-cli` / `gpt55-cli`
  codex names are `model_not_found` and get retried 3x as `transport` did NOT
  recur, because this roster does not seat them — it seats h100's own
  `codex-gpt56sol` (served name `gpt-5.6-sol`), which answered 5/5 first try.
* h100's own config comment says the codex CLI reports `usage=null` on every
  call. **That is no longer true here.** `codex-gpt56sol` reported real usage
  on all five calls (e.g. `{"prompt_tokens": 17130, "completion_tokens":
  142}`), including `cache_read_prompt_tokens: 13568` on one. Anyone relying
  on "codex is invisible to the budget ceiling" should re-measure.

## per item

Five of the 32 **both-picked-differ** items (both pipelines picked, and the
picks differ) from the Bench A pilot500 job, including the two the task
named. Settled ids are `items.verdict.selected_ids`; `decided_by` is
`items.verdict.decided_by`.

| # | item_id | annotation | A ← | B ← | settled ids | decided_by | conf | critic verdict | matches |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `006c5d1bf1ab` | `hrta; hemin transport system atp-binding protein` | live v4 `GO:0015439` | Bench A `GO:0016887` | `GO:0015439` | unanimous | high | strongly_consistent | **live v4** |
| 2 | `01d31c80efe1` | `Putative glutamine ABC transporter, ATP-binding protein` | live v4 `GO:0015426, GO:0005524` | Bench A `GO:0016887, GO:0005524` | `GO:0015426` | **meta** | high | **inconsistent** | **NEITHER** |
| 3 | `009948a3814c` | `Cytidylate kinase` | live v4 `GO:0036430` | Bench A `GO:0036430, GO:0036431` | `GO:0036430` | unanimous | high | strongly_consistent | **live v4** |
| 4 | `00ad90589555` | `Sulfite reductase [NADPH] flavoprotein alpha-component` | Bench A `GO:0003958` | live v4 `GO:0004783` | `GO:0004783` | unanimous | high | strongly_consistent | **live v4** |
| 5 | `00da9e369fcf` | `Helicase IV` | Bench A `GO:0017116` | live v4 `GO:0043138` | `GO:0043138` | unanimous | high | consistent | **live v4** |

All five items are `items.status = "ok"` with a non-null `decided_at`;
`decline_reason` is null on all five.

Item 2 is the one that matters most for the tuning pass: the council settled
on ids matching **neither** answer. The meta seat adopted the majority's
`GO:0015426` and then **stripped** `GO:0005524` (ATP binding) as an
unsupported extra, citing P1's own worked example — so the adjudicator can
reject a term both pipelines agreed on, which is the behaviour the research
fields exist to capture.

### extra fields read back from `calls.reply_parsed`

Read from `calls.reply_parsed` (NOT `attempts.reply_text`), as required.
Every proposer row carried all nine spec fields —
`correct_go_ids`, `prefers`, `correct_term_off_menu`,
`off_menu_description`, `failure_type`, `rules_cited`, `confidence`, `why`,
`notes` — and every critic row carried `verdict`, `reason`, `overpicked`,
`missed`, `misapplied_rules`, `off_menu_missed`, `contentless_input`, `notes`.
The one meta row carried `final_go_ids`, `final_confidence`, `prefers`,
`failure_type`, `rules_cited`, `correct_term_off_menu`,
`off_menu_description`, `no_functional_content`, `reasoning`, `notes`.

**`prefers`, translated back through the per-item A/B assignment** (the
adjudicators never saw which side was which):

| # | proposer_1 | proposer_2 | proposer_3 | union of `rules_cited` |
|---|---|---|---|---|
| 1 | live v4 | live v4 | live v4 | P1, P2, P3 |
| 2 | live v4 | live v4 | **neither** | P1, P2, P3, P4, P6 |
| 3 | live v4 | live v4 | live v4 | P1, P2, P6 |
| 4 | live v4 | live v4 | live v4 | P1, P2 |
| 5 | live v4 | live v4 | live v4 | P1, P2 |

14 of 15 proposer ballots preferred the live `go_mf@4` answer; the fifteenth
(`gemini35flash` on item 2) returned `prefers: "neither"` with
`correct_go_ids: []`.

**`failure_type`** assigned to the losing answer:

| # | proposer_1 | proposer_2 | proposer_3 |
|---|---|---|---|
| 1 | too_generic | too_generic | too_generic |
| 2 | too_generic | too_generic | too_generic |
| 3 | unsupported_extra | unsupported_extra | unsupported_extra |
| 4 | wrong_substrate | wrong_substrate | too_generic |
| 5 | wrong_parent_level | wrong_parent_level | too_generic |

`correct_term_off_menu` was `false` with `off_menu_description: "none"` on 14
of 15 ballots. The exception is `gemini35flash` on item 2, which reported
`true` and named the gap: *"ATPase-coupled glutamine transmembrane transporter
activity"*. The critic contradicted it (`off_menu_missed: false`) on the
grounds that `GO:0015426` was on the menu and is the policy-mandated term,
and the meta seat agreed with the critic. A menu-gap claim and its rebuttal
both survived into the store, which is the point of keeping these fields.

Nothing was lost through the adapter: `items.verdict.ballots[*]` carries the
engine's generic shape (`picks: [{id, confidence, why}]`,
`overall_confidence`, `no_functional_content`, `notes`), and the
adjudication's own vocabulary is intact in `calls.reply_parsed` beside it.

## how the items were built

The 32-item both-picked-differ group was derived from the two stores, not from
the inlined report table, and then cross-checked against it (all 32 counted,
and all five chosen rows' live-v4 / Bench A picks matched the table exactly).

* **Bench A side:** store `sqlite:////scratch/chenry/council-alcf-test/benchA.sqlite`,
  job `8ec9bc6f1102491e94ec6e2b6cfe114a` (label `benchA-pilot500`, `go_mf@4`,
  status `done`, 495 `ok` / 5 `error`). All five chosen items are `ok`.
* **The join.** Bench A `item_id` is the first 12 hex chars of the
  `entity_hash`, and Bench A stores the FULL 64-hex `entity_hash` alongside
  it — asserted for all five (`entity_hash[:12] == item_id`). The join to
  production was therefore on the full `entity_hash`, and `data_hash` was
  confirmed equal on all five. Belt and braces: the two stores' `menu` JSON
  was asserted byte-equal per item, which it was.
* **Production baseline side:** store
  `sqlite:////scratch/chenry/council/council.sqlite`. Each of the five
  resolved to exactly one `ok` item in a `done` `go_mf@4` job:

  | item_id | production job | label | menu size | source |
  |---|---|---|---|---|
  | `006c5d1bf1ab` | `d7a7d696…` | pmi-20261031-remainder chunk 4 | 27 | `""` |
  | `01d31c80efe1` | `501e9468…` | pmi-20261031-remainder chunk 0 | 29 | `""` |
  | `009948a3814c` | `a4d2ac39…` | ADP1+PMI chunk02 (option b, go_mf@4) | 31 | `clearinghouse.function_content` |
  | `00ad90589555` | `a6064fb2…` | pmi-20261031-remainder chunk 1 | 26 | `""` |
  | `00da9e369fcf` | `df6146a7…` | pmi-20261030 chunk 3 | 27 | `""` |

  `009948a3814c` also has two `cancelled` rows (a `go_mf@3` job and the
  cancelled option-b `go_mf@4` job); the `ok` row in the `done` job was taken,
  as instructed.
* **Both stores were opened READ-ONLY** with
  `sqlite3.connect("file:<path>?mode=ro&immutable=1", uri=True)`. Neither was
  written to, and the production store was never submitted to.
* **Item shape.** `go_mf_adjudicate@1` declares
  `evidence_fields: [text, source, answer_a, answer_b]` and `menu_fields: []`,
  so each item is
  `{"item_id": <benchA item_id>, "evidence": {"text", "source", "answer_a",
  "answer_b"}, "menu": <the baseline menu, unchanged>}`.
  `text` and `source` were copied from the production baseline item's
  `evidence`; the menu was copied unchanged.
* **answer rendering.** `"GO:id | label"` one per line, labels taken from the
  menu (every picked id was asserted present on the menu).
* **A/B order.** One `random.Random(20261116)` instance, one `rng.random()`
  draw per item, taken in the order items 1→5 above; live v4 became answer A
  when the draw was `< 0.5`. Result: A ← live v4 on items 1, 2, 3; A ← Bench A
  on items 4, 5. Neither side's reasoning was rendered, and the source labels
  appear nowhere in the prompts.

## the temporary config — `~/.kbutillib/config.yaml` was NOT edited

`~/.kbutillib/config.yaml` was opened read-only and is byte-identical to
before this task. The roster was made resolvable through the documented
override: the `--config FILE` option on the `kbu council` group (env var
`KBU_COUNCIL_CONFIG`), which reaches `load_council_config(path)` via
`CouncilUtils.council_config`.

**That override reads the file STANDALONE** — `load_council_config(path)`
does not overlay the packaged default, unlike the `None` route. A copy of
`~/.kbutillib/config.yaml` is therefore not a usable input on its own: its
council settings live under an `ai_council:` key that
`load_council_config` would discard as an unknown top-level key, and it
declares no `claude` endpoint at all. The temp file was built as the already
merged config the default route would have produced, which is the faithful
"copy of the user config" in the shape this option takes:

```python
merged = _merge(_packaged_default(),
                yaml.safe_load(open("~/.kbutillib/config.yaml"))["ai_council"])
merged = json.loads(json.dumps(merged))   # resolve the YAML anchors h100's
                                          # profiles: section shares
```

written to `/scratch/chenry/tmp/gomf-gt3/config-adjudicate.yaml`. Exactly four
changes were then made to it, and nothing else:

**1. The two `claude_cli` model entries from the commented ADJUDICATE block**

```yaml
models:
  opus-cli:
    endpoint: claude
    served_name: opus
  sonnet-cli:
    endpoint: claude
    served_name: sonnet
```

**2. The `adjudicate` profile from the commented ADJUDICATE block**

```yaml
profiles:
  adjudicate:
    proposers: [opus-cli, codex-gpt56sol, gemini35flash]
    critic: sonnet-cli
    meta: argo-opus5
    allow_meta_overlap: true
```

**3. The store pointed at a NEW throwaway SQLite file on local disk**

```yaml
store: sqlite:////scratch/chenry/tmp/gomf-gt3/adjudicate-smoke.sqlite
```

Created by `kbu council --config … db init` (schema_version 2). The
production store was never opened for writing and never submitted to. No
Dropbox or NFS path was involved.

**4. The `queues:` section was DROPPED.**

Not cosmetic — the config is otherwise unloadable. h100's `queues:` assigns
`proposer_1` to the ARGO-account queue `argo-chair`, and under `adjudicate`
that seat is `opus-cli` on the `claude-max` account while under `gomf` it is
`gemini35flash` on ARGO. `_validate_queues` resolves EVERY declared profile
and raises `ConfigError: queue 'argo-chair' resolves to more than one account
across the configured profiles` for exactly that reason. Queue mode is
additive and opt-in, and this run used the foreground worker
(`kbu council run`) rather than the daemon, so dropping the section is the
correct narrowing. **An operator who wants `adjudicate` under the daemon on
h100 needs a second set of queues**, keyed so no queue spans two accounts —
that is a real follow-on item, not something this smoke resolved.

The remaining ARGO/codex/mag endpoints, the ARGO and codex model entries, the
other three profiles, `query_dirs` and `default_profile` are h100's own,
unchanged. `ARGO_USER=chenry` was exported for the run (ARGO authenticates by
ANL username; it is not a credential and is not in any config file — the value
is already recorded in `/scratch/chenry/council/kbu-env.sh`).

### the one deviation from the commented block's roster

The block spells the codex proposer `gpt-5.6-sol` and says it is "already
present on h100". **It is not.** h100's user config declares no such alias; it
calls the codex GPT-5.6 Sol chair `codex-gpt56sol` (and the retired MAG one
`mag-gpt56sol`). The block's *entry* is right — `endpoint: codex`,
`served_name: gpt-5.6-sol`, `canonical: openai/gpt-5.6-sol` all match what
`codex-gpt56sol` actually carries — only the alias name differs, so
`codex-gpt56sol` was seated and it answered 5/5.

## the change to `default_config.yaml`

**Only the commented ADJUDICATE block was touched.** Four comment-text
corrections, all inside `# >>> BEGIN ADJUDICATE BLOCK` …
`# <<< END ADJUDICATE BLOCK <<<`, no live YAML anywhere in the repo changed,
and no test file changed:

1. **The MODELS preamble.** It claimed the two `claude_cli` seats "MUST ADD to
   ~/.kbutillib/config.yaml on h100 … a user `models:` section is what h100
   actually seats from". That is wrong: `_merge` overlays the user's
   `ai_council` section **per entry** inside `models:`, not whole-section, so
   the packaged `opus-cli` / `sonnet-cli` survive h100's own `models:`
   untouched and `load_council_config()` already seats them. They only have to
   be written out for a STANDALONE `--config` file — which is why this task
   wrote them, and the corrected text now says so.
2. **The `gpt-5.6-sol` entry's trailing comment**, `# already present on
   h100` → `# h100 calls this one \`codex-gpt56sol\``. The entry's
   `served_name` and `canonical` were correct and are unchanged.
3. **The PROFILE block** gained a note that the bench reads
   `[opus-cli, codex-gpt56sol, gemini35flash]` on h100, with the live result.
4. **The `allow_meta_overlap` rationale** now records the h100 measurement
   (argo-opus5 and opus-cli ARE one canonical model there) instead of leaving
   it conditional.

`tests/domains/ai/council/test_go_mf_adjudicate_builtin.py::TestTheCommentedAdjudicateBlock`
and `test_default_config_alcf_block.py` both still pass — every string they
pin (`opus-cli`, `gpt-5.6-sol`, `gemini35flash`, `sonnet-cli`, `argo-opus5`,
`served_name: opus/sonnet/claudeopus5`, `canonical: openai/gpt-5.6-sol`,
`go_mf CONFLICTS`, `A CHANGED ROSTER INVALIDATES CALIBRATION`,
`THE META SEAT MOVES TO`, the gpt54-cli/gpt55-cli OP-2 sentence, and the
"entirely commented" property) is still present, and the byte-identity check
still cuts both commented blocks before hashing against the unmoved
`BASE_SHA256`.

**The alias in the block was deliberately NOT renamed to `codex-gpt56sol`.**
Six assertions across `test_go_mf_adjudicate_builtin.py` pin the literal
`gpt-5.6-sol`, so renaming it would require editing a test file — outside
this task's permitted scope. The entry as written is also a working one for
any machine that lacks h100's alias. The divergence is recorded in the
comment instead.

## commands run, with exit codes

Interpreter: `/scratch/chenry/venvs/gomf-gt3-20261116/bin/python`
(CPython 3.11.15, created with `uv venv`, `uv pip install -e '.[dev,council]'`
plus `anthropic`). `python3 -m venv` cannot be used on h100 — system
python3.12 has no `ensurepip` ("you need to install the python3-venv
package", and there is no sudo here). The `council` extra is required
separately from `dev`: it carries `SQLAlchemy >=2.0` and the store will not
import without it.

All four `kbu` invocations ran with
`ARGO_USER=chenry TMPDIR=/scratch/chenry/tmp/gomf-gt3
PATH=/home/chenry/.local/bin:$PATH`,
`CFG=/scratch/chenry/tmp/gomf-gt3/config-adjudicate.yaml`, and
`KBU=/scratch/chenry/venvs/gomf-gt3-20261116/bin/kbu`:

| command | exit |
|---|---|
| `$KBU council --config $CFG db init` | **0** |
| `$KBU council --config $CFG doctor --profile adjudicate --json` | **0** (`ready 5`, `exit_code 0`) |
| `$KBU council --config $CFG submit --query go_mf_adjudicate@1 --input …/items.jsonl --label gt3-adjudicate-live-smoke --profile adjudicate --json` | **0** (`n_items 5`, `blockers []`, `warnings []`) |
| `$KBU council --config $CFG run --job 92e2aa9a4a4d4fd9b3c0a65dc50541c6 --json` | **0** (`status done`, `outcome clean`) |

### regression check

`build-baselines/kbutillib-h100-main-6310d61.json` was used as the baseline
rather than re-running the suite (it records 56m56s for the full run, well
past the tool ceiling). Compared **by nodeid, not count.**

```
TMPDIR=/scratch/chenry/tmp/gomf-gt3 \
/scratch/chenry/venvs/gomf-gt3-20261116/bin/python -m pytest \
  tests/domains/ai/council/ tests/cli/test_council_cli.py \
  tests/domains/ai/llm/test_schema_compat.py \
  tests/domains/ai/llm/test_static_headers.py \
  -q -p no:cacheprovider --no-header
```

→ **`7 failed, 1737 passed, 1 skipped in 230.40s`, exit code 1.**

The seven are the SAME seven nodeids task 2 recorded at the base commit, and
are machine-local, not product defects — h100's `~/.kbutillib/config.yaml`
leaks past the tests' monkeypatched `HOME`:

* the six `test_types_config.py::TestPackagedDefaultConfig::*` listed in the
  baseline's `MACHINE_LOCAL_FAILURES_NOT_PRODUCT_DEFECTS` block
  (`test_cli_endpoints_default_to_one_call_at_a_time`,
  `test_codex_seats_are_marked_unverified`,
  `test_default_profile_is_mixed_subscription`,
  `test_every_packaged_profile_builds_a_roster`,
  `test_on_a_fresh_machine_with_no_kbutillib_config`,
  `test_the_packaged_config_declares_no_poplar_endpoint`), and
* `test_static_headers.py::TestPackagedExample::test_the_default_config_declares_no_mag_endpoint`,
  the same leak reaching a `mag` endpoint that only the user config declares.

A narrower selection
(`test_go_mf_adjudicate_builtin.py test_go_mf_adjudicate_council.py
test_default_config_alcf_block.py test_types_config.py`) gives
`6 failed, 203 passed in 7.23s`, exit 1 — the same six, with every
adjudicate-specific and config-block test passing. **No regression.** No new
baseline file was written: two already exist for this repo.

## reconciliation — prompt vs. tree

Six places where the task prompt or the executor notes disagreed with the
code or the machine. All fixed in place; none was a reason to stop.

1. **"add the adjudicate profile and the two claude_cli model entries … to a
   copy of the user config"** — the `--config` override reads the file
   standalone, so a copy of `~/.kbutillib/config.yaml` is not loadable as a
   council config (its settings are under `ai_council:`, which
   `load_council_config` discards, and it declares no `claude` endpoint). The
   temp file was built as `_merge(packaged, user["ai_council"])` instead,
   which is the same configuration the default route produces. Recorded above.
2. **The prompt and the block name the codex seat `gpt-5.6-sol`** and the
   block calls it "already present on h100". No such alias exists in h100's
   config; the real one is `codex-gpt56sol`, with the same served_name and
   canonical. Seated `codex-gpt56sol`.
3. **Nothing warned that `queues:` makes the merged config unloadable** once
   `adjudicate` is declared (`argo-chair` would span two accounts). Dropped
   the section; the consequence for the daemon is flagged above as a follow-on.
4. **The executor note says `claude_cli` is "usable but NOT seated"** on h100
   and implies `opus-cli` / `sonnet-cli` are absent. They are **packaged** in
   `default_config.yaml` and survive the per-entry merge, so the default route
   already seats them; only a standalone `--config` file needs them written
   out. Corrected in the block as well.
5. **`ARGO_USER` is not set in this environment** and no config file carries
   it, so both ARGO seats would have failed. Exported `ARGO_USER=chenry`,
   the value already recorded in `/scratch/chenry/council/kbu-env.sh`.
6. **The executor note anticipated the servable.py display-name bug** making
   every ARGO seat read `unavailable`. It did not occur: with
   `served_name: gemini35flash` the probe listed 100 models and reported
   "gemini35flash is served by argo", `state: ready`. The `verified: false` /
   `allow_unverified` path was never needed either — all five seats resolved
   `verified: true`.

Also worth recording because the user config's own comment asserts the
opposite: the codex CLI **did** report token usage on every call (see "per
seat").

## scratch artefacts (ephemeral — /scratch is not synced)

Under `/scratch/chenry/tmp/gomf-gt3/`:
`config-adjudicate.yaml` (the temp config), `items.jsonl` (the five built
items), `items_report.json` (the A/B assignment and provenance per item),
`benchA_picked.json`, `prod_candidates.json`, `adjudicate-smoke.sqlite` (the
throwaway store holding the job), `run.json`, `analysis.txt` (the full
per-item `reply_parsed` dump, including every `why` and `reasoning` string).
Venv: `/scratch/chenry/venvs/gomf-gt3-20261116`.

## files_touched

Modified:
- `src/kbutillib/domains/ai/council/default_config.yaml` — four comment-text
  corrections inside the commented ADJUDICATE block only (lines ~432-500).

New:
- `agent-io/work-records/gt-adjudicate-live-smoke.md` — this record.

No source module, no test, no query spec, no store schema and no engine file
was changed.

## success_criteria_check

- **The work record exists on the branch and reports five real Bench A
  conflict items run through `go_mf_adjudicate@1` with the `adjudicate`
  profile on h100** — PASS. Job `92e2aa9a4a4d4fd9b3c0a65dc50541c6`.
- **A terminal outcome per item and per seat, with error classes for any
  failure** — PASS. Five items all `ok` with settled ids and `decided_by`;
  five seats all `answered` with latency and tokens; no failure, and the
  classes checked for and not found are listed explicitly.
- **At least 4 of 5 items settled** — PASS, **5 of 5**.
- **Extra fields were readable from `calls.reply_parsed`** — PASS. All nine
  proposer fields on 15/15 proposer rows, the critic's eight on 5/5, the
  meta's ten on 1/1, read from `calls.reply_parsed` and not from
  `attempts.reply_text`.
- **No code outside `default_config.yaml`'s commented adjudicate block
  changed** — PASS. The only other new file is this work record.
- **`~/.kbutillib/config.yaml` was not edited** — PASS. Opened read-only and
  copied; unchanged on disk.
