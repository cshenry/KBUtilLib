# Work Record — council-answer-normalisation

## task_id
council-answer-normalisation (M2.0)

## branch
maestro/developer/background-reference-only-agent--council-answer-normalisation

## base commit
a492760 (`docs(work-record): fill in the record's own commit sha`)

## commit_shas
- 1999bda — feat(council): normalise two measured reply defects before the answer gate
- d27a5b6 — test(council): report normalisation in the GO V3 replay parity report
- 288c627 — docs(council): regenerate the GO V3 parity report at 99/99 end to end
- (this record)

## summary

The worker now repairs exactly two measured reply defects BEFORE the answer
schema sees a reply, records every repair on the call row and on the verdict,
and leaves the raw reply text untouched. **The GO V3 parity replay, rerun on
h100 against the oracle on `/scratch`, reproduces Jose Faria's V3 decisions
99/99 END TO END** — up from 91/99 — with zero tokens spent and no network
call. The eight rows that fell short are each listed in the regenerated
report by input id with the rule that repaired them.

Nothing else is repaired. A reply missing a different required field is still
`invalid_output`, and the rule-only control column (the decision rule with the
schema gate removed, and with no normalisation) is kept in the report and
still agrees 8/8 — so a repair that changed a DECISION rather than merely
admitting one would show up as those two columns disagreeing.

## files_touched

New:
- `src/kbutillib/domains/ai/council/engine/normalise.py` (242 lines) — the
  pure function, both rules, and the reasoning for each.
- `tests/domains/ai/council/test_normalise.py` (25 tests) — the new guard.
- `agent-io/work-records/council-answer-normalisation.md` — this record.

Modified:
- `engine/worker.py` — `normalise()` before `_validate_answer` in `_invoke`;
  `_settle_ok` stores the repaired answer plus the record; `_settle_reused`
  re-applies `normalise()`; module docstring.
- `engine/decide.py` — `_content_flag` promoted to public `content_flag`
  (now dict- or `Ballot`-tolerant); `SettledCall.normalised`; `repairs`
  collected per seat and put on every verdict.
- `engine/__init__.py` — exports `normalise` and `content_flag`.
- `types.py` — `CouncilVerdict.normalised: bool` and
  `CouncilVerdict.repairs: Dict[str, List[Dict]]`.
- `store/schema.py` — `calls.normalised` JSON column; `ADDITIVE_COLUMNS`.
- `store/sqlite.py` — `complete_call(..., normalised=...)`; new
  `add_missing_columns()`, called from `migrate()`.
- `store/queries.py` — `complete_call_update` writes the new column.
- `protocols.py` — the store contract's `complete_call` signature.
- `council_utils.py` — `db_migrate()` reports `columns_added` (see below).
- `scripts/council/replay_go_v3.py` — `normalised_rows()`; `diagnose(...,
  also=)`; the report's new normalisation section, the kept rule-only column,
  the `repaired` column, and the run's command/host/UTC/interpreter/commit.
- `tests/domains/ai/council/test_go_replay.py` — the offline fixture now
  expects 10/10 with exactly three repaired rows, pinned by rule AND value.
- `agent-io/prds/kbu-ai-council-v1/research/go-replay-parity.md`,
  `go-replay-manifest.json` — regenerated / refreshed in place. No other file
  under `agent-io/prds/` was created or edited.

## the two rules, and why each value is what it is

`normalise(role, parsed, schema, context) -> (parsed', repairs)`. Pure, no
I/O, and **typed by the role's own answer schema** — which is why `notes`,
`why`, `reason`, `reasoning` and a query type's renamed id list are all
covered without any of them being named in the module.

**`null_string`.** A property the schema types as `string` whose value is
`None` becomes `""`. Covers the array-of-objects case (`picked[].why`) by
reading the array's `items` schema. A property that is ABSENT is left absent:
a missing optional string is not a defect, and inventing one would turn "said
nothing" into "said the empty string".

**`derived_content_flag`.** A meta reply missing `no_functional_content` gets
it:

- non-empty final id list -> `False`. An item with ids selected is not
  contentless whichever way the flag would have fallen — the same reasoning
  `content_flag`'s docstring already gave for ignoring the vote table.
- empty (or absent) list -> `content_flag(ballots, n, critic)`, i.e. **the
  function `decide()`'s own skip path calls**, imported rather than restated.
  Proposer majority `2t>n -> True`, `2t<n -> False`, exact tie -> the
  critic's `contentless_input`. A council that decided with one content rule
  and repaired with another would be two rules wearing one name, so
  `_content_flag` was promoted to public `content_flag` — the same
  justification `tally()` already carries for being public.

The meta answer's id key is read OFF THE SCHEMA (`_ids_key`): the meta schema
has exactly one array-of-strings property, which is `final_go_ids` for
`go_mf@1` and `final_<id_field>s` in general. It cannot be hard-coded —
normalisation runs before the adapter has renamed anything — and if the key
is ambiguous the rule declines to fire rather than guess which branch to
take, because guessing the branch guesses the answer.

Two more deliberate non-actions: nothing is derived when the schema does not
declare the flag as a boolean, and a meta reply that STATES the flag is never
touched.

## where the record lives — the question the prompt left open

The prompt offered two places and asked which was used. **A new nullable JSON
column, `calls.normalised`**, holding `[{"field", "rule", "original",
"value"}, ...]`.

The alternative — "inside the stored reply envelope beside the raw text" —
would have meant putting non-schema keys into `reply_parsed`, which is the
answer itself: `go_mf@1`'s schemas are `additionalProperties: false`, so a
reply carrying its own repair record would fail the gate it just passed, and
`postprocess` would be handed keys it never declared.

Schema version is **still 1** (schema v2 is a later M2 task and out of scope
here). That is sound rather than convenient, and the mechanism is explicit:
`schema.ADDITIVE_COLUMNS` lists `("calls", "normalised")`, and
`SqliteStore.migrate()` now calls the new `add_missing_columns()`, which
`ALTER TABLE ... ADD COLUMN`s any entry a store lacks. Every column eligible
for that list is nullable, defaultless, and read by code that treats NULL as
"nothing to say", so adding one cannot change the meaning of an existing row.
A store created by the M1 build has the right stamp and the right tables and
is missing exactly this column — a state the version check cannot see and
`metadata.create_all` will not fix — so `kbu council db migrate` is the
remedy, which is where the existing mismatch message already sends people.
`TestTheAdditiveColumn` drops the column from a live store, migrates, and
replays a job through the repaired file.

One small addition beyond the task's named files follows from that, and it is
deliberate: `CouncilUtils.db_migrate()` now asks `add_missing_columns()` for
itself on an already-stamped store and reports `columns_added`, counting it
as `migrated`. Without it `kbu council db migrate` on an M1 store would print
`migrated: false` about a file it had just altered. `columns_added` is a new
key on an existing report dict; both `db_migrate` tests (facade and CLI)
assert named keys and still pass.

What goes where, on a settled call:

| column | holds |
|---|---|
| `reply_text` | the model's UNTOUCHED bytes (`"notes": null` and all) |
| `reply_parsed` | the REPAIRED answer — the one the council read |
| `normalised` | every difference between the two, invertible from `original` |

The verdict carries `normalised: true|false` and `repairs: {seat_name:
[...]}`, with only repaired seats present, so `verdict.repairs` lists exactly
which seats were repaired and how.

## reuse

`_settle_reused` **re-applies `normalise()`** to the reused reply rather than
copying the source's record, and that is not belt-and-braces: a row written
by the M1 build holds the RAW reply and no record, and inheriting it would
give the reusing call an answer its own schema would have refused. If
re-applying finds nothing (because the source has already been through it —
`normalise` is idempotent, pinned by `test_it_is_idempotent`), the source's
own record is carried across, so both calls say the same thing about the same
answer.

The reused reply is NOT re-validated, and that is safe by construction
rather than by hope: `types.prompt_hash` includes `json_schema`
(`types.py:269`), so a reuse source was validated against the IDENTICAL
schema when it was stored.

## verification

Every run was in the FOREGROUND, nothing was backgrounded, and the longest
single call was 82 s.

| Command | Exit | Result |
|---|---|---|
| `python -m pytest tests/domains/ai/council/ -q -p no:cacheprovider` (base `a492760`, clean tree, BEFORE any file was written) | 0 | **726 passed, 243 skipped** |
| same, on the branch | 0 | **752 passed, 243 skipped** (= 726 + the 25 new `test_normalise.py` tests + 1 net in `test_go_replay.py`, where two tests became three) |
| `python -m pytest tests/domains/ai/council/test_normalise.py -q` | 0 | **25 passed** |
| `python -m pytest tests/domains/ai/council/test_go_replay.py -q` | 0 | **22 passed** (was 21; one test became three) |
| `python -m pytest tests/domains/ai/council/test_council_utils.py tests/cli/test_council_cli.py -q` | 0 | 57 passed (after the `db_migrate` change) |
| `python -m ruff check src/kbutillib/domains/ai/council tests/domains/ai/council scripts/council` | 0 | All checks passed |
| suite slice A, branch | 1 | 26 failed — `test_kind.py` 17, `test_clearinghouse.py` 6, `test_clearinghouse_operator.py` 2, `test_composition_smoke.py` 1 |
| suite slice B, branch | 0 | **1019 passed, 329 skipped** |
| `python scripts/council/replay_go_v3.py` (full 100 rows, h100) | **0** | **99/99 end to end**; 8 rows repaired |

Slices match the `--ignore` list in `build-baselines/kbutillib.json`'s
`command`. Slice A is `annotators arc_context berdl beril_worktree biochem
cli core domains`; slice B is the rest except `tests/notebook`.

### Regression comparison — BY NAME

`build-baselines/kbutillib.json` exists and was READ rather than re-derived
(no full-suite baseline run was taken). It is stale — captured 2026-09-13 at
`0f84803` on a different interpreter — and the dispatcher said committed
baselines may be, so it was not used to adjudicate.

For `tests/domains/ai/council/` a base run was taken FIRST, on a clean tree
at `a492760`, in this task's own venv, before any file was written: 726 / 243,
exit 0. The branch is 752 / 243, exit 0, and no previously passing test
fails.

For the rest of the suite the argument is stronger than a name comparison.
`git diff --name-only a492760 HEAD` touches only `src/kbutillib/domains/ai/
council/**`, `scripts/council/`, `tests/domains/ai/council/` and two files
under `agent-io/`. The only importers of council code outside the council are
`domains/ai/__init__.py`, `toolkit.py` and `tests/cli/test_council_cli.py`
(24 passed). Slice A's 26 failures are in four modules, **none of which
contains the string "council" at all** (`grep -ril council` over the four
returns nothing), in the same files and the same counts the two prior council
work records recorded as pre-existing.

(Slice A's raw grep also catches 3 `ERROR` LOG lines about a missing
`modelseedpy` — log output, not test errors. Identically recorded by the
earlier council work records.)

## the replay rerun (dispatcher criterion 3)

Rerun on **h100 / `poplar.cels.anl.gov`**, `2026-10-16T03:32:01+00:00`,
command `python scripts/council/replay_go_v3.py`, interpreter CPython 3.12.3,
repository commit `d27a5b6`, exit **0**. All five of those facts are recorded
in the report itself (`## Headline` and `## How this run was made`) and in the
manifest's `latest_rerun`.

- Headline: **Agreement 99/99 (100.0%)** over ALL scored recorded `ok` rows,
  measured END TO END through the shipped worker INCLUDING its answer-schema
  gate. One row (5540) remains excused by predicate (a)/(b), unchanged.
- Every repaired row is listed by input id with seat, field, rule, original
  and repaired value, plus a per-rule roll-up:
  `derived_content_flag` 7 rows (1250, 1711, 2029, 3728, 6788, 9871, 10482),
  `null_string` 1 row (11938).
- The rule-only column is KEPT, now computed over the repaired rows, with the
  end-to-end column beside it: 8/8 agree.
- The report still prints what the schema says about the RAW recorded reply,
  so the transcript's defect is stated rather than hidden by the repair.
- Manifest: every file entry's SHA-256 was re-measured by `verify_manifest`
  before the replay ran and each entry now carries a `sha256_verified` stamp.
  All five are UNCHANGED (independently re-measured with `sha256sum` too).

**All seven `derived_content_flag` rows name a non-empty `final_go_ids`**, so
all seven take the first branch and the skip-path branch is never reached by
this transcript. That is recorded in the manifest rather than glossed: the
empty-list branch, including the tie, is covered only by
`test_normalise.py`.

## no-injection (dispatcher criterion 2)

`test_normalise.py` drives every reply through the REAL `CouncilWorker` into a
REAL store opened with `open_store("sqlite:///<tmp_path>/.../council.sqlite")`
and `init_schema()`. The BACKEND is the only substitute (the shipped
`FakeBackend`, scripted through `engine_support.scripted`).

`normalise()`, `_validate_answer`, `decide()` and the store are not
monkeypatched, stubbed or subclassed anywhere in the module — the string
`monkeypatch` does not appear in it.
`TestTheRepairIsInTheStore::test_the_repair_is_readable_from_a_second_store_on_the_same_file`
closes the store, reopens the FILE with a second `open_store`, and reads the
repair record, the repaired answer, the untouched raw text and the verdict's
`normalised` flag back out of the database.

Two tests write to the store directly through its own engine — the
`predates normalisation` reuse test, which ages a source row to the M1 shape,
and the additive-column test, which drops the column. Both use the store's
public `transaction()` and SQLAlchemy table objects, which is the pattern
`test_engine.py:1874` already uses; neither substitutes any part of the
council.

## test coverage against the prompt's list

| asked for | test |
|---|---|
| null notes -> `""`, call settles ok, repair recorded | `TestNullString::test_a_null_notes_settles_the_call_ok`, `::test_the_repair_is_recorded_on_the_call`, `::test_the_stored_answer_is_repaired_and_the_raw_text_is_not` |
| meta missing the flag, non-empty ids -> `False` | `TestDerivedContentFlag::test_non_empty_final_ids_derive_false` |
| empty ids -> the proposer-majority value | `::test_empty_final_ids_take_the_proposer_majority_true` / `_false` |
| ...and the critic's on a tie | `::test_an_exact_tie_takes_the_critics_answer` (parametrised both ways, 2 proposers) |
| a DIFFERENT missing required field is still invalid_output | `TestEveryOtherSchemaFailureStillFails` (4 tests: proposer, meta, a null in a BOOLEAN field, a non-object reply) |
| reuse re-applies the repair | `TestReuseReappliesTheRepair` (3 tests, incl. an M1-shaped source row and a reused meta reply) |
| the verdict carries `normalised: true` with the list | `TestTheVerdictCarriesTheRecord` (both ways round) |

Extra, because the code would otherwise be untested: `picked[].why`, an
absent optional string left absent, a meta reply that states the flag,
non-mutation of the caller's dict, idempotence, and the additive column.

## reconciliation — where the prompt and the tree disagreed

1. **`normalise(role, parsed, context)` cannot work as specified.** The
   signature in the prompt has no schema, and BOTH rules are typed by the
   schema — `null_string` needs to know which properties are strings, and
   `derived_content_flag` needs the flag's declared type and the id list's
   key. The prompt wrote it as "e.g.", so the shipped signature is
   `normalise(role, parsed, schema, context=None)` with `schema =
   query.answer_schema(role)`. Hard-coding `notes`/`why`/`reason`/
   `reasoning`/`final_go_ids` instead would have put a domain vocabulary in
   a domain-neutral engine.
2. **"the call site is CouncilWorker's validation step, which calls
   `_validate_answer` in `engine/worker.py`"** — correct, but the line is not
   where the prompt's sibling documents say: it is `worker.py:966` in the M1
   record and now `worker.py:977` after the insert. Found by name, as the
   role prompt requires.
3. **"add a nullable JSON column ... via the existing migrate path"** — the
   existing `migrate()` had no path to add anything: it was `init_schema()`
   for version 0 and a no-op for version 1. The column is therefore added
   through a NEW, explicitly-scoped additive step (`ADDITIVE_COLUMNS` +
   `add_missing_columns()`) rather than by a version bump, which is out of
   scope. See "where the record lives" above.
4. **`decide()`'s skip-path content rule was private.** The prompt says to
   use "the value `decide()`'s skip-path content rule gives" as though it
   were callable. `_content_flag` was module-private and typed to
   `Sequence[Ballot]`. Promoted to `content_flag`, widened to accept the
   mappings a rendering context carries, and added to `decide.__all__` and
   the engine package's exports. The one existing call site is unchanged in
   behaviour.
5. **The report's "Mismatches" section had to be restructured, not just
   re-run.** Every explanation — the schema complaints and the rule-only
   diagnosis — lived inside the `else` branch of `if not mismatched`, so a
   clean run would have printed "**None.**" and dropped the rule-only column
   the dispatcher requires. The explanations now live in their own
   `## Normalisation` section, and `diagnose()` takes an `also=` argument so
   the control is computed over the repaired rows.
6. **"7 claudeopus45 META replies ... and 1 gemini35flash PROPOSER reply"** —
   confirmed exactly, by input id and by seat, against the oracle before
   writing any code, and reproduced by the rerun.
7. **The task's "expected 99/99" was reached**, so the fallback clause
   ("report the remaining rows by input id with their exact schema error")
   did not fire. The report prints those errors anyway, because the
   transcript's defect is a fact about the recording that outlives the
   repair.

## things I inferred rather than verified

- **That no OTHER row in the 100 would be changed by normalisation.** The
  argument is sound and the measurement agrees (exactly the 8 known rows were
  repaired), but it rests on a property rather than an enumeration: a
  schema-typed string that is null, or a required flag that is absent, fails
  validation, so a reply the rules touch is a reply that was already being
  refused. A reply that is BOTH repairable and defective in some third way
  would still be refused, and none occurs here.
- **That a live `go_mf@1` run would rarely need these repairs.** The backends
  request structured output against `answer_schema(role)`, so a compliant
  endpoint should not return either defect — but no live call was made (the
  task forbids it), and how strictly each gateway enforces a supplied JSON
  Schema is not something this replay measures. Normalisation is insurance on
  a transcript-shaped failure, not a substitute for asking properly.
- **That `PostgresStore` needs the same column.** `store/__init__.py` still
  dispatches `postgresql:` URLs to `.postgres`, but **`store/postgres.py`
  does not exist in this tree**, so there was nothing to update. Removing the
  PostgreSQL code is a later M2 task and was left alone; the shared
  `schema.py` the prompt's design note still describes as two-dialect now
  carries the column, so a future PostgreSQL store would get it from
  `create_all` for free.

## notes for the next task

- `schema.ADDITIVE_COLUMNS` is the only sanctioned way to add a column
  without a version bump, and its docstring says so. The schema-v2 task
  should fold `calls.normalised` into the v2 table definition and can then
  empty the list — but should NOT remove the mechanism, since a v1 store on
  somebody's disk still needs it.
- `verdict.repairs` is keyed by seat name, so the facade and the `kbu council`
  CLI can surface "this verdict was repaired" without re-reading the call
  rows. Nothing displays it yet; no task asked for that.
- The replay script's exit code is now `0`, so a future regression in either
  the rule or the gate turns it non-zero again. `test_go_replay.py` is the
  offline guard on every machine; `EXPECTED_NORMALISED` there pins the three
  repaired fixture rows by rule AND value, so a widened rule fails it rather
  than passing more quietly.
