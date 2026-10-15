# Work Record — council-go-replay-parity

## task_id
council-go-replay-parity

## branch
maestro/developer/background-reference-only-agent--council-go-replay-parity

## base commit
362756c (`docs(work-record): fill in the record's own commit sha`)

## commit_shas
- 33d8151 — test(council): replay Jose's V3 GO council through the worker, zero tokens
- (this record's own commit — see the final line)

## summary

Proves, with **zero tokens spent and no network call**, that the council
worker plus the shipped `go_mf@1` builtin reproduces Jose Faria's recorded
CONCORDIA V3 GO council decisions.

**Headline: 91 of 99 recorded `ok` rows reproduce exactly** (selected GO ids
as a set, status, decline_reason, meta-skipped). The 8 that do not are all
one cause, and it is **not** the decision rule — see "The 8 mismatches"
below. With the answer-schema gate as the only variable, the rule reproduces
V3 **99/99**, measured rather than asserted.

The change is **purely additive**: five new files, zero modification to
`src/`, to `pyproject.toml`, or to any pre-existing test.
`git diff --name-only 362756c HEAD -- src/ pyproject.toml` is empty.

## files_touched

All new:
- `scripts/council/replay_go_v3.py` (1029 lines) — the replay.
- `agent-io/prds/kbu-ai-council-v1/research/go-replay-manifest.json` — the
  files read, their SHA-256s, the row-selection outcome, the provenance.
- `agent-io/prds/kbu-ai-council-v1/research/go-replay-parity.md` — the
  report, written by the script (not by hand).
- `tests/domains/ai/council/fixtures/go_v3_replay_10.json` (148 KB) — the
  ten rows the fixed rule selects, inputs + recorded replies + recorded
  decision, verbatim.
- `tests/domains/ai/council/test_go_replay.py` (429 lines, 21 tests) — the
  offline guard.

Only the two files the dispatcher permitted were created under
`agent-io/prds/`; nothing else there was touched.

## PROVENANCE OF THE ORACLE — established cryptographically

The dispatcher asked which generator produced
`results_strat100_jose_v3.jsonl`: the V3 kit, or Jose's 09-18 LIVE pilot
(which has a different proposer system prompt and candidate rendering).

**Answer: the V3 KIT** (`/scratch/chenry/go_council_v3/go_council_kit/go_council.py`,
sha256 `7a567823a1765f7fee93dd29a7a73a5c7f3c9509952cc72f36f43a1f26ca5ea1` —
measured here, and equal to the digest the dispatcher cited).

This was **not** inferred from filenames, mtimes or README prose. The V3 kit
caches every reply in a sidecar SQLite keyed by
`sha256(model \0 max_tokens \0 system \0 user \0)` (`go_council.py:212`), so
the PROMPT TEXT of the run that produced the oracle is recoverable: rebuild
each call's `(model, system, user)` from a candidate generator's constants
plus the recorded input, hash it, and test membership in the sidecar's key
set. A generator differing by one byte matches nothing.

| reconstruction | cache keys found |
|---|---|
| V3 kit, proposer | 299 / 300 |
| V3 kit, critic | 100 / 100 |
| V3 kit, meta | 34 / 34 |
| **V3 kit, total** | **433 / 434 — i.e. every one of the 433 cache rows** |
| 09-18 live pilot, proposer | **0 / 300** |

The single miss is `input_id 5540 / gemini35flash / proposer` — the one call
that raised (`ValueError: no JSON object in: …`). `go_council.py:291-292`
writes the cache only after a successful parse, so it was never cached. The
miss is explained by the record itself, not by a prompt difference.

So the oracle and `go_mf@1` share a prompt lineage, and this is a genuine
parity check rather than a comparison across two different queries.
**No change to `go_mf@1` is warranted and none was made.**

## The fixed row-selection rule

"The first ten `ok` rows by input id, extended in id order until at least one
meta-skipped and one meta-run row are included."

Applied mechanically by `select_fixture_ids()`. The first ten `ok` rows
already hold 5 meta-skipped and 5 meta-run, so no extension was needed:

`[148, 293, 631, 789, 1250, 1402, 1465, 1497, 1711, 2029]`

They also happen to cover all three decline outcomes (`None`,
`contentless`, `no_candidate_matched`), which
`test_the_fixture_covers_both_decline_reasons` pins. The rule is re-applied
to the fixture inside the test, so a hand-edited fixture fails.

## Expected differences — evaluated mechanically, not assigned

| class | predicate | input ids |
|---|---|---|
| (a) recorded status error | `row['status'] == 'error'` | **[5540]** |
| (b) proposer error, then critic/meta | a proposer errored AND critic/meta output exists | **[5540]** |
| (c) off-menu or duplicated pick | a pick absent from the row's menu, or an id twice in one reply | **[]** |

(b) is not a second row: the only recorded proposer error is the one in the
only recorded error row.

(c) is **empty, and structurally so for its off-menu half**:
`go_council.py:314-318` already filters each proposer's picks to the offered
set before recording, so an off-menu id cannot survive into the transcript.
The duplicate half is NOT filtered by the kit (`go_council.py:337`
increments once per pick, where the council dedups first) — it simply did
not occur in these 100 rows. Had it occurred, the vote table would differ by
the repeat count.

## The 8 mismatches — reported, not excused

| rows | cause |
|---|---|
| 1250, 1711, 2029, 3728, 6788, 9871, 10482 | `claudeopus45` omitted the required `no_functional_content` from the meta reply |
| 11938 | `gemini35flash` returned `"notes": null` where the schema says string |

`go_mf@1` declares an answer schema per role (`required` plus
`additionalProperties: false`) and the worker enforces it on every reply
(`engine/worker.py:966` → `_validate_answer`). **Jose's V3 runner enforced
no schema at all** — `go_council.py:162 parse_json` only requires that the
text parse as JSON. The transcript therefore holds replies a real `go_mf@1`
run would have refused on arrival, and the replay refuses them.

These are counted as **mismatches** in the headline, each enumerated by
input id with both answers, because the task permits exactly three
expected-difference predicates and this is not one of them.

A separate, clearly-labelled diagnosis measures the rest:
`rule_only_decision()` calls the real `engine.decide()` on `SettledCall`s
built from the same replies, with the schema gate as the only thing removed.
**The rule agrees on 8/8 of them**, so the rule reproduces V3 99/99 while
the worker end-to-end reproduces it 91/99, and the gap is the gate and
nothing else. The diagnosis excuses nothing and changes no headline number.

I did **not** "fix" this by relaxing the schema, patching the transcript, or
adding a fourth expected-difference class. The schema is a port of what the
V3 prompts specify, the worker is right to enforce it, and a live run
requests structured output against that schema in the first place.

## No-injection (dispatcher criterion 2)

`test_go_replay.py` drives the REAL `CouncilWorker`, the REAL `go_mf@1`
seeded into a temp SQLite store via `QueryRegistry.seed_builtins()` and read
back out of the database, the REAL `SqliteStore` and the REAL `decide()`.
**The backend is the only substitute**, which is the design.

`decide()`, the worker and the store are not monkeypatched, stubbed or
subclassed anywhere in the test or the script. `monkeypatch` appears exactly
once, in `test_the_replay_makes_no_http_call`, where it BREAKS
`socket.socket.connect` and `socket.create_connection` so that any attempt
to reach the network raises — the opposite of substituting a council part.

`TestNothingWasFaked` pins the rest: the query type really came out of the
database, the roster really is the V3 roster on `replay` endpoints, and the
ReplayBackend table keys really are what `worker.py:941` builds as
`trace_id` (so a changed trace format fails loudly instead of erroring every
row silently).

The script and the test share ONE implementation — the test imports
`scripts/council/replay_go_v3.py` by path (the pattern already used by
`tests/berdl/test_clearinghouse_parity_script.py`), so the offline guard
exercises the same code that produced the report.

## Two deliberate withholdings in the replay table

Both documented in `replay_entries()`:

1. **An errored proposer gets no entry.** There is no reply to serve; the
   backend answers `invalid_output`, which is what a failed seat looks like,
   rather than a fabricated success.
2. **A meta reply is withheld on a meta-SKIPPED row.** The V3 runner
   synthesises that dict itself (`go_council.py:423-429`); no meta model was
   called. Serving it would manufacture agreement if this council ever
   reached for meta where V3 skipped. Withholding turns that disagreement
   into a visible failure.

The runner-added `skipped` key is stripped from served meta replies —
`go_mf`'s meta schema is `additionalProperties: false`, so serving it back
would fail a call that never failed live. Pinned by
`test_a_meta_run_row_serves_a_meta_reply_without_the_runners_key`.

## environment

The supervisor provisioned an EMPTY `.task-venv/` in the worktree and
`python` resolves to it. Installed into THAT venv only; no shared or service
venv, no `ssh`, no live host, no systemd unit, no database:

```
python -m pip install --upgrade pip
python -m pip install -e '.[council]'          # SQLAlchemy 2.1.4, psycopg
python -m pip install -e '.[dev,ai]'           # pandas, duckdb, httpx
python -m pip install pytest pytest-asyncio pytest-cov ruff
```

CPython 3.12.3, SQLAlchemy 2.1.4, pydantic 2.14.1, pytest 9.1.1.
`jsonschema` 4.10.3 is present transitively; it is NOT a declared dependency,
and `_validate_answer` falls back to a weaker required-key-and-type check
without it. **The 8 mismatches do not depend on `jsonschema` being
installed**: the fallback loops over `schema["required"]`, which catches the
7 meta replies missing `no_functional_content`, and checks
`expected == "string"`, which catches `notes: null` on row 11938. So all 8
fail either way — though the complaint TEXT differs, and
`test_each_one_fails_on_the_meta_answer_schema` asserts the `jsonschema`
wording. A reviewer must install the same way in its own venv; an
`ImportError` there is an environment gap, not a result.

## verification

| Command | Exit | Result |
|---|---|---|
| `python -m pytest tests/domains/ai/council/ -q -p no:cacheprovider` (base `362756c`, clean tree, BEFORE any file was written) | 0 | **705 passed, 243 skipped** |
| `python -m pytest tests/domains/ai/council/ -q -p no:cacheprovider` (branch) | 0 | **726 passed, 243 skipped** (= 705 + the 21 new) |
| `python -m pytest tests/domains/ai/council/test_go_replay.py -q` (branch) | 0 | **21 passed** |
| `python -m ruff check --output-format=concise scripts/council tests/domains/ai/council/test_go_replay.py` | 0 | All checks passed |
| suite slice A, branch | 1 | 26 failed — `test_kind.py` 17, `test_clearinghouse.py` 6, `test_clearinghouse_operator.py` 2, `test_composition_smoke.py` 1 |
| suite slice B, branch | 0 | **1019 passed, 329 skipped** |
| `python scripts/council/replay_go_v3.py` (full 100 rows) | 1 | 91/99 agreement; exit 1 is the script's accurate signal, see above |

Slices match the `--ignore` list in `build-baselines/kbutillib.json`'s
`command`. Slice A is `annotators arc_context berdl beril_worktree biochem
cli core domains`; slice B is the rest except `tests/notebook`. Every run was
in the FOREGROUND; nothing was backgrounded, and each call stayed well under
600 s (the longest was 46 s).

### Regression comparison — BY NAME

`build-baselines/kbutillib.json` is stale (captured 2026-09-13 at `0f84803`
on a different interpreter), and the dispatcher said so, so it was not used
to adjudicate.

For `tests/domains/ai/council/` — the only directory this change adds tests
to — a base run was taken FIRST, on a clean tree at `362756c`, in this venv,
before any file was written: 705 passed / 243 skipped, exit 0. The branch is
726 / 243, exit 0. The delta is exactly the 21 new tests; no previously
passing test fails.

For the rest of the suite the argument is stronger than a name comparison:
**the change modifies nothing that the rest of the suite executes.** It adds
five files and edits none; `git diff --name-only 362756c HEAD -- src/
pyproject.toml tests/` lists only the two new test artefacts. No
pre-existing test's behaviour can change, because no code it imports
changed. Slice A's 26 failures are the same four files, in the same counts,
recorded as pre-existing by the two prior council work records
(`council-query-types.md`, `council-engine-worker.md`), and none is in
`tests/domains/`.

(Slice A's raw grep also catches 3 `ERROR` LOG lines about a missing
`modelseedpy` — log output, not test errors, which is why 29 lines hold 26
failures. Identically recorded by the earlier work records.)

## success_criteria_check

**Criterion 1.** PASS, with one honest qualification stated plainly rather
than buried.
- `scripts/council/replay_go_v3.py` exists and writes
  `agent-io/prds/kbu-ai-council-v1/research/go-replay-parity.md`.
- The report covers every recorded `ok` row: 91 of 99 show identical
  selected GO ids (as a set), status and decline_reason; the remaining 8 are
  **enumerated individually by input id with both answers** and their cause
  pinned. Parity is not 99/99, and the report says so in its first section
  rather than claiming success.
- `tests/domains/ai/council/test_go_replay.py` replays the checked-in 10-row
  fixture offline: **21 passed**. It asserts the same outcome the full run
  measured (7 agree, 3 schema-gate, rule 10/10), so a regression in either
  the rule or the gate breaks it.
- No network call: no backend but `ReplayBackend` is constructed, and
  `test_the_replay_makes_no_http_call` breaks the socket layer and replays
  anyway.
- No test that passed at `362756c` fails on the branch (above).

**Criterion 2 (no-injection).** PASS — see "No-injection" above. Real
worker, real `go_mf@1` from `seed_builtins()` in a temp SQLite store, real
store, real `decide()`; `ReplayBackend` is the only substitute.

**Criterion 3.** PASS.
- `go-replay-manifest.json` records the SHA-256 of **every** file it names
  (five: input, results, the same results file in its reference-output role,
  the generator, the cache sidecar), and `verify_manifest()` asserts all of
  them before the replay runs.
- It records the fixed rule's outcome: the ten chosen input ids, split into
  meta-skipped and meta-run, with their decline reasons.
- It records the generator as **V3_KIT** with the cryptographic evidence and
  the explicit 0/300 cross-check against the 09-18 pilot.
- `go-replay-parity.md` states the agreement count over **all 99 recorded ok
  rows**, not only the ten fixture rows, and says so in that sentence.

## reconciliation — where the prompt and the tree disagreed

1. **"build a ReplayBackend" — one already ships.**
   `src/kbutillib/domains/ai/council/backends/testing.py` already defines
   `ReplayBackend`, keyed by `(task_id, role, seat_name)` read out of
   `trace_id`, which is exactly what the prompt describes. I used the
   shipped class rather than writing a second one. Writing my own would have
   meant the test exercised my backend instead of the product's.

2. **`endpoint_kind="replay"` already exists.** The prompt says to use a
   roster "whose seats use endpoint_kind `replay`" as though it needed
   adding; `types.SeatSpec.endpoint_kind`'s `Literal` already includes it.
   Nothing to add.

3. **`trace_id` already carries `item_id|role|seat_name`.** `worker.py:941`
   builds exactly that, so no worker change was needed. Pinned by
   `test_the_backend_is_keyed_by_the_workers_own_trace_id` so the contract
   cannot silently drift.

4. **"the matching inputs … find the input file the strat100 run used".**
   Found: `input_strat100_jose_v3.jsonl`, in the same directory. It is the
   only `input_*.jsonl` there, its 100 `input_id`s are set-equal to the
   results file's, and it shares the `strat100_jose_v3` stem. Neither file
   was missing or unreadable, so the prompt's STOP clause did not fire.

5. **Expected-difference class (c) is empty, and (b) is not a distinct
   row.** The prompt presents three classes as though each would have
   members. Evaluated mechanically, (a) and (b) are the same single row
   (5540) and (c) has none. Reported as measured rather than padded.

6. **The prompt did not anticipate the schema-gate divergence**, which is
   the single largest finding. Handled as described above: reported as
   mismatches, diagnosed separately, nothing relaxed.

7. **`agent-io/prds/kbu-ai-council-v1/` did not exist on main**, exactly as
   the dispatcher said. Created `research/` and wrote only the two named
   files there.

## things I inferred rather than verified

- **That a live `go_mf@1` run would not hit the schema-gate failures.** The
  backends request structured output against `answer_schema(role)`, so a
  compliant endpoint would not return a meta reply missing a required key —
  but I did not make a live call to confirm it (the task forbids it), and
  how strictly ARGO enforces a supplied JSON Schema per model is not
  something this replay measures.
- **That the 09-18 pilot would also match 0 keys for critic and meta.** I
  measured the pilot's PROPOSER_SYSTEM (0/300). Its critic and meta prompts
  are byte-identical to the kit's per `council-query-types.md`, so those
  keys would MATCH for either generator and cannot discriminate — which is
  why the proposer prompt is the discriminating test and the one I ran.
- **The fixture's `occurrences` field is unused** by the replay. It is kept
  so each fixture row is byte-verifiable against the source file rather than
  a pruned derivative.
- **Tie order in the vote block.** My provenance reconstruction sorted the
  vote table by descending count where the kit uses
  `Counter.most_common()`. All 134 critic+meta keys matched, so the two
  orderings coincided on this data; a differently-ordered table would have
  shown up as a cache miss.

## notes for the next task

- The script is re-runnable on poplar only (the oracle is on `/scratch` and
  is not checked in). Everywhere else, the offline fixture test is the
  guard, and `verify_manifest()` fails with a clear message pointing at it.
- If `go_mf@1`'s answer schema is ever loosened, `test_go_replay.py` will
  fail on `EXPECTED_SCHEMA_GATE` — that is intentional. Three rows moving
  from "schema gate" to "agrees" is a product decision, not a test bug, and
  should be made deliberately.
