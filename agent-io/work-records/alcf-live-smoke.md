# Work Record — alcf-live-smoke

## task_id

`task-9b746d71` (phase 0, PRD `kbu-alcf-api-type-v1`) — establish the three
external facts the rest of the plan is written against, against the LIVE ALCF
Inference Endpoints service.

## base commit

`98e9f55` (`docs: work-record for halt-cause-vocabulary`)

## scope

**This task writes no library code.** Nothing under `src/` or `tests/` is
touched. The deliverables are this record and the unmodified reference copy of
the probe at `agent-io/work-records/alcf_apitype_probe_reference.py`.

All observations below are LIVE, made on h100 (poplar) on **2026-11-14**
against `https://inference-api.alcf.anl.gov`. No bench was re-run; the 500-item
Bench A results in
`agent-io/reports/2026-11-12-alcf-inference-endpoints-go-council-evaluation.md`
stand on their own and were not duplicated here.

**No token value appears anywhere in this record, in the committed probe, or in
the diff.** Tokens were held in shell/process memory only; where a token had to
be characterised it is described by length and shape, never by value.

### headline: the premise behind the ceiling worry is FALSIFIED

The task prompt's stated concern was that if ALCF reports reasoning tokens in a
separate field, then `completion_tokens` *understates* what the ceiling bought,
and "nobody sizing a ceiling from it can get the right answer."

Reasoning tokens **are** reported separately — but `completion_tokens` is
**inclusive** of them, so it does **not** understate. See section (d) for the
arithmetic. `read_chat_envelope` reading only `prompt_tokens` and
`completion_tokens` is therefore **sufficient** for sizing a ceiling. The real
hazard is a different one, and section (d) states it.

---

## (a) The token invocation

### Confirmed argv

```
alcf-tokens get-token inference
```

Run from the worker's own shell with no absolute path. Result:

| Check | Result |
|---|---|
| Exit code | `0` |
| stdout non-empty | **yes** (91 characters) |
| stderr | empty (0 bytes) |
| Trailing newline / multi-line | single line, no embedded newline |
| Shape | opaque Globus token, **not** a JWT (no `header.payload.sig` structure) |

`.strip()` is still the right thing to do on the captured stdout — the probe
already does this — but there was no stderr noise to separate out.

### Does a bare `alcf-tokens` resolve on PATH?

**Yes, on this worker.** `which alcf-tokens` →
`/home/chenry/.local/bin/alcf-tokens`, which is a symlink to
`/home/chenry/.local/share/uv/tools/alcf-tokens/bin/alcf-tokens` (the uv tool
venv). The worker's PATH included `/home/chenry/.local/bin`.

**Do not rely on this in shipped code.** The absolute path
`/home/chenry/.local/bin/alcf-tokens` that the existing probe hardcodes is the
safer form, because a daemon or systemd-launched council worker does not
necessarily inherit the interactive PATH that made the bare name resolve here.
Both forms were confirmed working; prefer the absolute one.

### Is `alcf_tokens` importable from the council venv?

**No.** From `/home/chenry/.venvs/kbdl/bin/python`:

```
ModuleNotFoundError: No module named 'alcf_tokens'
```

`alcf_tokens.auth.get_access_token('inference')` is therefore **not callable**
from the venv that serves the council. This confirms the deployment fact the
probe's own docstring flags: `alcf_tokens` lives in its own uv tool venv
(`/home/chenry/.local/share/uv/tools/alcf-tokens`, Python 3.11), isolated from
`/home/chenry/.venvs/kbdl`.

Consequence for the design: the **command route is the only route available
today**. Abandoning the subprocess in favour of an in-process import would
require adding `alcf-tokens` as a dependency of the serving environment first.
That is a deployment change, not a code change, and it is not done.

---

## (b) The roster payload

`GET https://inference-api.alcf.anl.gov/resource_server/list-endpoints`
with the token as `Authorization: Bearer <token>` → **HTTP 200**, 3238 bytes.

### Does the shape match the claim?

The claimed shape was
`{'clusters': {<cluster>: {'frameworks': {<framework>: {'models': [<name>, ...]}}}}}`.

**It matches.** The nesting and every key name in that path
(`clusters` → `<cluster>` → `frameworks` → `<framework>` → `models` → list of
plain strings) is exactly as claimed. The traversal the probe's `models()`
override performs is correct and needs no change.

Two **additive** keys exist that the claim did not mention. Neither breaks the
traversal, and both are worth knowing:

1. **`clusters[<cluster>].base_url`** — a sibling of `frameworks`, e.g.
   `"/resource_server/tara"`. A **relative** path, not an absolute URL, and it
   stops at the cluster: it does **not** include the framework or the `/v1`
   suffix. Building a call URL from it means appending `/<framework>/v1`
   and prefixing the host.
2. **`clusters[<cluster>].frameworks[<framework>].endpoints`** — a sibling of
   `models`, listing the paths that framework serves. This is a real
   capability signal: `/v1/chat/completions` is present on both clusters below,
   but the rest of the list **differs per cluster** (see metis vs tara).

The top level has exactly one key: `clusters`. Clusters present:
`minerva`, `tara`, `sophia`, `metis`.

### Trimmed excerpt — `tara/api` and `metis/api` only

```json
{
  "clusters": {
    "tara": {
      "base_url": "/resource_server/tara",
      "frameworks": {
        "api": {
          "models": [
            "beam-1.0-fp8",
            "google/gemma-4-31B-it-262k",
            "inkling-bf16",
            "mistral-large-4",
            "muse-spark-bf16",
            "nemotron-3-ultra",
            "nemotron-35-super-vl",
            "openai/gpt-oss-120b",
            "openai/gpt-oss-20b"
          ],
          "endpoints": [
            "/v1/chat/completions",
            "/v1/responses",
            "/v1/messages",
            "/v1/completions",
            "/v1/embeddings",
            "/v1/health",
            "/v1/metrics"
          ]
        }
      }
    },
    "metis": {
      "base_url": "/resource_server/metis",
      "frameworks": {
        "api": {
          "models": [
            "Mistral-Large-3-675B-Instruct-2512",
            "gemma-4-31B-it",
            "gpt-oss-120b"
          ],
          "endpoints": [
            "/v1/chat/completions",
            "/v1/responses"
          ]
        }
      }
    }
  }
}
```

The remaining clusters are trimmed out as instructed. One fact from outside the
excerpt is needed by section (d): `minerva/api` carries
`["mistral-large-4-nvfp4", "muse-spark-bf16"]`.

### Model-name traps this exposes

Names are **not** portable across clusters and are **case-sensitive**:

- `gemma-4-31B-it` exists on **metis**; tara's is `google/gemma-4-31B-it-262k`.
- `gpt-oss-120b` on metis is `openai/gpt-oss-120b` on tara (prefixed).
- `mistral-large-4` (tara), `mistral-large-4-nvfp4` (minerva) and
  `Mistral-Large-3-675B-Instruct-2512` (metis) are three different strings for
  three different seats, one of them capitalised.

Both model names this task was told to use — `gemma-4-31B-it` on `metis/api`
and `mistral-large-4-nvfp4` on `minerva/api` — are present in the live roster
exactly as the prompt spelled them.

---

## (c) Two chat-completions calls, with and without `response_format`

`POST https://inference-api.alcf.anl.gov/resource_server/metis/api/v1/chat/completions`,
model `gemma-4-31B-it`, `max_tokens: 4000`, `temperature: 0`. The prompt asked
for a JSON object with one key `answer`.

### Call A — WITHOUT `response_format`

- **HTTP 200**
- `finish_reason`: `stop`
- `content` was **not** null
- Raw content, verbatim:

  ````text
  ```json\n{\n  "answer": "ok"\n}\n```
  ````

  i.e. the JSON is wrapped in a **markdown fence**. `json.loads` on this raw
  string fails — it needs the fence stripped first. This is exactly the
  "unparseable without it" behaviour the 2026-11-12 report recorded for
  `gemma-4-31B-it`.

- **COMPLETE `usage` object, verbatim:**

  ```json
  {
    "completion_tokens": 16,
    "completion_tokens_after_first_per_sec": 154.13888398894574,
    "completion_tokens_after_first_per_sec_first_ten": 155.2031852460351,
    "completion_tokens_after_first_per_sec_graph": 155.2031852460351,
    "completion_tokens_details": {
      "reasoning_tokens": 0
    },
    "completion_tokens_per_sec": 23.123573143536042,
    "end_time": 1794680394.2923317,
    "is_last_response": true,
    "prompt_tokens": 38,
    "stop_reason": "stop",
    "time_to_first_token": 0.5946197509765625,
    "time_to_first_token_graph": 0.566047191619873,
    "total_latency": 0.6919345855712891,
    "total_tokens": 54,
    "total_tokens_per_sec": 78.04205935943413
  }
  ```

### Call B — WITH `response_format` (`json_schema`, `strict: true`)

- **HTTP 200** — the parameter was **ACCEPTED**, not rejected and not silently
  ignored.
- `finish_reason`: `stop`
- `content` was **not** null
- Raw content, verbatim:

  ```
  {\n  "answer": "ok"\n}
  ```

  **No markdown fence.** This is the observable difference: the same model, the
  same prompt, and the fence is gone. `json.loads` succeeds directly.

- **COMPLETE `usage` object, verbatim:**

  ```json
  {
    "completion_tokens": 11,
    "completion_tokens_after_first_per_sec": 116.86227745117161,
    "completion_tokens_after_first_per_sec_first_ten": 118.00580701793874,
    "completion_tokens_after_first_per_sec_graph": 118.00580701793874,
    "completion_tokens_details": {
      "reasoning_tokens": 0
    },
    "completion_tokens_per_sec": 16.026677930681842,
    "end_time": 1794680395.2411783,
    "is_last_response": true,
    "prompt_tokens": 192,
    "stop_reason": "stop",
    "time_to_first_token": 0.6007847785949707,
    "time_to_first_token_graph": 0.5666069984436035,
    "total_latency": 0.6863555908203125,
    "total_tokens": 203,
    "total_tokens_per_sec": 295.76505635712857
  }
  ```

### What this settles for Q4

**ALCF honours `response_format` with `json_schema` / `strict: true` on
`metis/api`, and it is the difference between parseable and unparseable output
for `gemma-4-31B-it`.** Confirmed, consistent with the 2026-11-12 report.

Two side observations that matter for sizing:

1. **The schema is billed as prompt tokens.** `prompt_tokens` rose from **38**
   to **192** — ~154 tokens for a three-line schema. A real GO-MF schema is far
   larger, so the schema is a per-call input cost on every call, not a free
   parameter.
2. Constrained decoding was *cheaper* on output here (16 → 11 completion
   tokens) because the fence itself costs tokens. Do not generalise from one
   trivial call.

---

## (d) The low-ceiling reasoning call, and the ceiling question

Two calls were needed. The one the prompt specified did **not** reach its
ceiling, so it could not answer the capping question on its own; a second,
saturating call was added to settle it.

### Call C — as specified: `minerva/api`, `mistral-large-4-nvfp4`, `max_tokens: 1500`, trivial prompt

- **HTTP 200**
- `finish_reason`: **`stop`**
- `content` was **NOT** null — it was `{"answer": "ok"}`, bare and directly
  parseable, with **no** `response_format` sent.
- The `message` object carried extra keys absent on metis:
  `role`, `refusal`, `audio`, `function_call`, **`reasoning`** — with
  `reasoning` holding 262 characters of visible reasoning text.
- **COMPLETE `usage` object, verbatim:**

  ```json
  {
    "prompt_tokens": 40,
    "total_tokens": 115,
    "completion_tokens": 75,
    "completion_tokens_details": {
      "reasoning_tokens": 66
    }
  }
  ```

Note the usage object is **much leaner than metis's** — no throughput or
latency telemetry at all. Any code reading the per-second / `end_time` /
`stop_reason` fields seen in section (c) must treat them as **optional**; they
are a metis-only extra, absent on minerva.

The trivial prompt used only 75 of 1500 tokens and stopped naturally, so this
call proves reasoning is *reported* but says nothing about *capping*.

### Call D — added to saturate the ceiling: same seat, same `max_tokens: 1500`, hard combinatorics prompt

- **HTTP 200**
- `finish_reason`: **`length`**
- `content` was **`null`** — while `message.reasoning` held **4262 characters**
  of reasoning text.
- **COMPLETE `usage` object, verbatim:**

  ```json
  {
    "prompt_tokens": 78,
    "total_tokens": 1578,
    "completion_tokens": 1500,
    "completion_tokens_details": {
      "reasoning_tokens": 1499
    }
  }
  ```

### Are reasoning tokens reported in a SEPARATE field?

**Yes — but nested, and NOT under either name the prompt guessed.** The field is:

```
usage.completion_tokens_details.reasoning_tokens
```

There is **no** top-level `reasoning_tokens` and **no**
`completion_reasoning_tokens`. It is present on **both** clusters: metis
reports it as `0` for the non-reasoning `gemma-4-31B-it`, minerva reports it
non-zero. So its presence does not imply a reasoning model; its *value* does.

> **This invalidates a line in the committed reference probe.**
> `alcf_apitype_probe_reference.py` reads
> `u.get("reasoning_tokens") or u.get("completion_reasoning_tokens") or 0`,
> which looks only at the top level. Both keys are absent there, so that
> expression returns **0 for every call, including reasoning models**. The
> `rsn` column in that probe's output is therefore meaningless and must not be
> cited as evidence that reasoning was unused. Phase 4 must read the nested
> path. The probe is committed unmodified as instructed; this note is the
> correction.

### Does `max_tokens` cap the TOTAL generated, or only the visible content?

**It caps the TOTAL generated — reasoning plus visible — and
`completion_tokens` is the inclusive count.** Call D is unambiguous:

```
max_tokens        = 1500
completion_tokens = 1500   <- exactly the ceiling, not more
reasoning_tokens  = 1499
visible  = completion_tokens - reasoning_tokens = 1
prompt_tokens + completion_tokens = 78 + 1500 = 1578 = total_tokens   ✓
```

`completion_tokens` landed **exactly** on `max_tokens` while visible content
was 1 token. Had the cap applied only to visible output, generation would have
continued far past 1500. It did not. Call C corroborates on the other side:
`75 = 66 reasoning + 9 visible`, and `40 + 75 = 115 = total_tokens`.

So, explicitly:

| Question | Answer |
|---|---|
| Reasoning reported separately? | **Yes**, at `usage.completion_tokens_details.reasoning_tokens` |
| Is `completion_tokens` inclusive of reasoning? | **YES** |
| Does `max_tokens` cap total generated? | **YES** — total, not visible |
| `prompt + completion == total`? | **Yes**, on both clusters |

### Consequence for the ceiling rule — the worry inverts

`read_chat_envelope`
(`src/kbutillib/domains/ai/llm/apis/http_common.py:250-254`, confirmed in this
worktree: it builds `usage` from `prompt_tokens` and `completion_tokens` only)
reads **the right number**. Because `completion_tokens` is inclusive, it does
**not** understate what the ceiling bought, and a ceiling sized from it is
correct. **No change to `read_chat_envelope` is required for the ceiling rule
to be sound.** The premise in the task prompt — and any plan text resting on
it — should be corrected.

The genuine hazard sits elsewhere, and Call D is the proof case:

**A reasoning model can spend essentially the entire ceiling on reasoning and
return `content: null` with `finish_reason: "length"`.** 1499 of 1500 tokens
went to reasoning; the caller got no content at all. Therefore:

1. A ceiling must cover **reasoning + visible**, so it must be sized well above
   the visible-output estimate. The report's 14,095-completion-token maximum at
   a 16,000 ceiling (critic `beam-1.0-fp8`) is an **inclusive** figure under
   this finding — it does not decompose, and how much of it was reasoning is
   still unknown for that seat.
2. `content: null` with `finish_reason: "length"` must be classified as
   **truncation**, not as a parse failure or a refusal. Mapping it to a parse
   error would blame the model for a ceiling that was simply too low, and would
   send a retry down the wrong path.
3. What `read_chat_envelope` drops is the **decomposition**, not the total.
   Keeping `reasoning_tokens` would let a ceiling be *diagnosed* ("the ceiling
   was consumed by reasoning") rather than merely *sized*. That is a
   worthwhile but optional enhancement, not a correctness fix.

---

## (e) 30-day Globus re-auth expiry: is the due date readable?

**No. The 30-day re-auth deadline is NOT discoverable without performing a
login.** Checked three places, no login performed:

1. **`alcf-tokens test-token inference`** — exit code `0`, and the entire
   output is:

   ```json
   {"ready": true, "error": null}
   ```

   A boolean liveness answer. No timestamp, no remaining lifetime, no deadline.

2. **The CLI surface** — `alcf-tokens --help` offers `list-services`, `login`,
   `get-token`, `test-token`, `clear-tokens`. **No command reports expiry.**

3. **The on-disk token store** —
   `~/.globus/app/7f3e61f5-e0de-4e8f-9150-0a62c65dda63/alcf_tokens/tokens.json`
   (mode `0600`, `format_version` `2.0`, `globus-sdk` `4.10.0`). It holds, per
   resource server, `expires_at_seconds` — but **only for the ACCESS token**:

   | Resource server | `expires_at_seconds` | Decoded |
   |---|---|---|
   | `auth.globus.org` (scope `openid`) | 1794546801 | 2026-11-12 23:13:21 |
   | `681c10cc-…` (inference, `action_all`) | 1794853150 | 2026-11-16 12:19:10 |

   The inference access token sat **48.0 h** from the `get-token` call made for
   this task, confirming the 48-hour access-token lifetime the probe docstring
   asserts. A `refresh_token` is present for both resource servers, but **the
   refresh token carries no stored expiry field**, and the 30-day limit is a
   property of the Globus session / refresh-token validity, not of anything
   written to this file.

   (Inspected with every one of `access_token`, `refresh_token`, `id_token` and
   `other_tokens` redacted; no secret was printed or recorded.)

### Operational consequence

The 30-day re-auth is **a silent cliff**. Nothing on disk or in any
`alcf-tokens` output counts down to it. It is detectable only *after* it
bites, as a failing `get-token` or a rejected token, which in the council
**HALTS the endpoint** rather than retrying.

Two things follow for the plan:

- The only cheap proxy available is the **mtime of `tokens.json`**
  (`2026-11-14 12:19:10` at the time of writing, bumped by every successful
  refresh). A refresh keeps the session alive, so mtime tracks last-use rather
  than the deadline — it bounds the cliff from below but does not locate it.
- Anything depending on ALCF across a month-long gap in use needs an
  **external** reminder of the re-auth date. It cannot be derived from the
  system, and h100 is the only machine holding the Globus login.

---

## Reconciliation against the tree and against the prompt

The prompt was accurate in its mechanics and wrong in one premise.

| Prompt claimed | Actually found | Action |
|---|---|---|
| `alcf-tokens get-token inference` prints a raw token | Confirmed: exit 0, 91-char single-line opaque token, clean stderr | Confirmed as-is |
| Roster shape `clusters→frameworks→models` | Matches exactly; **plus** additive `base_url` (per cluster) and `endpoints` (per framework) | Recorded the additions in (b) |
| Models `gemma-4-31B-it` @ metis, `mistral-large-4-nvfp4` @ minerva | Both present verbatim in the live roster | Used as specified |
| Reasoning may be in `reasoning_tokens` / `completion_reasoning_tokens` | Neither exists. It is nested at `completion_tokens_details.reasoning_tokens` | Recorded real path; flagged the reference probe's always-0 read |
| "If reasoning is separate, `completion_tokens` understates the ceiling" | **FALSIFIED.** `completion_tokens` is inclusive; `max_tokens` caps the total | Stated the inversion and the real hazard in (d) |
| One low-`max_tokens` reasoning call answers the capping question | It did not — the trivial prompt stopped at 75/1500 with `finish_reason: stop` | Added saturating Call D, which settles it |
| `read_chat_envelope` reads only `prompt_tokens`/`completion_tokens` | Confirmed at `llm/apis/http_common.py:250-254` | No change needed for the ceiling rule |
| `alcf_tokens` importable from kbdl venv (open question) | **Not** importable — `ModuleNotFoundError` | Command route is the only route today |

## Verification

No library code changed, so the suite is not the relevant check; the checks
that matter are that the deliverable is complete, that no source file moved,
and that no secret leaked.

- `git diff --stat` against the base confirms only two files, both under
  `agent-io/work-records/`. **No file under `src/` or `tests/` is modified.**
- `agent-io/work-records/alcf_apitype_probe_reference.py` is byte-identical to
  `~/Dropbox/AIPlatform/artifacts/alcf-api-type-findings-2026-11-11/alcf_apitype_probe.py`
  (`diff` exit `0`, 151 lines). Unmodified, as instructed.
- The diff was scanned for the live token (by length, prefix and the opaque
  token pattern) and for any `access_token` / `refresh_token` value. **Clean.**
- Probe scripts were written to `/tmp` only and are not part of the commit.
