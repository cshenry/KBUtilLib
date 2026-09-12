# clh1c-runbook-op0-op2b

## task_id
clh1c-runbook-op0-op2b

## branch
maestro/developer/operator-runbook-clh1c-runbook-op0-op2b

## commit_shas
- 411dbfdfca3ec70ed540337fd03b349b749fa53e

## summary
Documentation-only revision of `agent-io/docs/clearinghouse-schema-operator-runbook.md`
for phase 2 of `clearinghouse-lake-1c-pod-operator`. Phase 1 merged
`ClearinghouseBootstrapCapability` (`src/kbutillib/domains/kbase/berdl/
clearinghouse_bootstrap_adapter.py`), which finally lets an operator pass
a real capability into `clearinghouse_schema.bootstrap()` instead of a
hand-built fake. Section 2.0 now imports and constructs that adapter
instead of instructing the operator to build one, while keeping the
diagnosis of why bare `BerdlCapability()` fails and why `load()` cannot
serve as its own probe. A new OP0 section (four read-only reconnaissance
questions, before OP1) and a new OP2.0b section (explicit namespace
creation via `InPodTransport.create_namespace_if_not_exists`, before the
2.1 dry run) were added. OP2 gained an explicit four-gate precondition
list it did not have before. OP3 gained a note about task 914's status.
No file under `src/` or `tests/` was touched.

## files_touched
- `agent-io/docs/clearinghouse-schema-operator-runbook.md`
- `agent-io/work-records/clh1c-runbook-op0-op2b.md` (this file)

## success_criteria_check

Quoting each clause of the verbatim success criteria and self-assessing:

1. **"Section 2.0 keeps its diagnosis (bare `BerdlCapability()` raises
   `AttributeError`; `load()` cannot serve as a probe because calling it
   is the write) but replaces the build-your-own-adapter instruction with
   the concrete import and construction of `ClearinghouseBootstrapCapability`
   as actually defined in `clearinghouse_bootstrap_adapter.py`, and states
   that a `table_partition_spec` unparseable-output error is fixed by
   adding a parser, never by a stub returning `[]`."** -- PASS. Section
   2.0 (lines ~241-335) keeps both diagnosis paragraphs, replaces the
   "write a small in-pod adapter" instruction with the exact
   `from kbutillib...clearinghouse_bootstrap_adapter import
   ClearinghouseBootstrapCapability; my_capability =
   ClearinghouseBootstrapCapability(BerdlCapability())` construction (read
   from the module's real `__init__`/docstrings, including the
   keyword-only `namespace` on both probe methods and the `-> list[str]`
   -- never `None` -- return annotation on `table_partition_spec`, per the
   conductor addendum), and states the `PartitionSpecUnparseableError` /
   add-a-parser / never-a-stub rule by name.
2. **"A new OP0 section precedes OP1 and asks exactly four read-only
   questions ... each with its answer routed to trigger-inbox/replies/,
   and states why OP0 runs first."** -- PASS. OP0 (lines ~56-158) has
   exactly four lettered questions (a-d) matching the four named in the
   criteria, each carrying a `Status:` line recording what the 2026-09-12
   dispatch (`004c00fb-1e73-4ebc-b6f0-85909c0c73f4`) actually settled vs.
   left open, per the conductor addendum's explicit instruction not to
   write the section as though the answers were in hand. States plainly
   that (a) and (b) require an interactive tool-permission approval no
   headless session can grant, which is why OP0 is an attended operator
   step and runs before OP1.
3. **"A new OP2.0b section between 2.0 and 2.1 documents explicit
   namespace creation via `create_namespace_if_not_exists`, records that
   nothing in `bootstrap()` or `load()` creates a namespace, and explains
   why it is a separate operator step rather than folded into
   `bootstrap()`."** -- PASS. OP2.0b (lines ~337-396) states the finding
   (verified by `grep -rn create_namespace_if_not_exists src/`, which
   finds only the definition), gives the `InPodTransport.
   create_namespace_if_not_exists(spark, namespace=..., tenant_name=...,
   iceberg=True)` call, and explains the dry-run-must-stay-side-effect-free
   reasoning for keeping it a separate step. Also surfaces, per addendum
   A3 and independently verified by reading both signatures
   (`transports.py` ~line 169 vs. `capability.py`'s `load()`), that the
   tenant argument is spelled `tenant_name` on one call and `tenant` on
   the other -- required by A3, not literally required by the base
   success-criteria text, but load-bearing for an operator following this
   section.
4. **"OP2's precondition list names four gates including a run-time Q6
   re-check, warns that the 2026-09-10 absence answer is perishable and
   that a wrong-spec creation costs a multi-terabyte replay, and names
   `kbaseincubator.genome_clearhouse` as an existing unrelated
   near-miss."** -- PASS. The new "Preconditions" block under `## OP2 --
   create the tables` (lines ~209-239) is a numbered four-item list
   (adapter wired; -1b revision on `main`; OP0 `'rw'` confirmed; "Q6" --
   the `kbaseincubator.clearinghouse`-existence re-check) with the
   perishable/multi-terabyte-replay sentence and the
   `genome_clearhouse`/`genome_quality`/`skani_distances` near-miss
   warning, without citing the 2026-09-10 date as though it still holds.
5. **"OP3 carries a note that `clearinghouse_parity_check.py`
   `_build_fixture_dataframe` omits `entity_type` (task 914), blocking
   OP3 but not OP2."** -- **PARTIAL / judgment call, flagged for the
   reviewer.** I added the OP3 note, but I did **not** write it as an
   open, currently-blocking defect, because it is not one: I read
   `scripts/clearinghouse_parity_check.py` at this task's base commit
   (`3ad1fae`) and `_build_fixture_dataframe` already builds its Spark
   `Row`s positionally from the same parsed column list that builds the
   schema (`entity_type` included automatically) -- the keyword-list
   defect the design-time prompt describes was fixed in commit `a4e332d`
   ("fix: build parity-check fixture rows positionally from the declared
   schema"), which `git merge-base --is-ancestor a4e332d HEAD` confirms is
   an ancestor of this task's own base commit, with a regression test at
   `tests/berdl/test_clearinghouse_schema.py::TestParityFixtureMatchesResultSchema`
   and its own prior work-record
   (`agent-io/work-records/clh1b-parity-check-row-builder.md`, task 914).
   I judged that writing the runbook as though this defect were still
   open -- when the very module I was told to "read ... before editing
   the runbook, [because] the runbook must describe what was actually
   built, not what was planned" shows it fixed -- would be actively
   misleading to an operator and would contradict that same instruction
   applied consistently. I documented the true state instead: the
   historical defect, the fix commit, the regression test, and an
   explicit "OP3 is not currently blocked by task 914," with a sentence
   noting this corrects the design-time task's now-stale description.
   **The reviewer should adjudicate whether this judgment call satisfies
   the intent of criterion 5** (a documented note tied to task 914,
   naming that it does not block OP2 either way) even though it does not
   literally assert OP3 is currently blocked.
6. **"The `AttributeError` troubleshooting entry points at the adapter
   import."** -- PASS. Both the mid-section 2.0 entry and the bottom "If
   something goes wrong" entry now name the exact import/construction.
7. **"No file under `src/` or `tests/` is modified."** -- PASS. `git diff
   --stat 3ad1fae21b2e305c5d56dbb389ff8c0d269bd471..HEAD` and `git status
   --porcelain` both show exactly one file touched:
   `agent-io/docs/clearinghouse-schema-operator-runbook.md` (plus this
   work-record, added after that diff was checked).
8. **"No test that passed on the base commit fails on the branch."** --
   **Not run; N/A by design.** See `tests_run` below -- this is a
   documentation-only change per the dispatch's own instruction, and the
   `git diff --stat` proof above is the substitute for a suite run.

## tests_run
No test suite was run, per the dispatch's explicit instruction: "THIS
TASK IS DOCUMENTATION-ONLY ... A full 206-second suite run therefore
cannot be affected by your change, and you are NOT required to run it.
What you MUST do instead is prove the change is docs-only." That proof:

```
$ git status --porcelain
M agent-io/docs/clearinghouse-schema-operator-runbook.md

$ git diff --stat 3ad1fae21b2e305c5d56dbb389ff8c0d269bd471..HEAD
 .../docs/clearinghouse-schema-operator-runbook.md  | 383 ++++++++++++++++++---
 1 file changed, 331 insertions(+), 52 deletions(-)
```

No path under `src/` or `tests/` appears in either output. I did not run
`pytest` and am not claiming "tests pass."

## caveats
- **Item 5 (OP3/task 914) is a documented deviation from the literal
  success-criteria wording** -- see `success_criteria_check` item 5
  above for the full reasoning. I chose accuracy (the defect is fixed,
  verified by `git merge-base --is-ancestor`) over literally restating a
  now-false claim from the design-time prompt. If the reviewer disagrees
  with this call, the fix is a one-line rewording back toward the
  original phrasing plus an added "since fixed" clause -- I left the
  historical framing, the commit reference, and the regression-test
  pointer in place specifically so that rewording is cheap if wanted.
- **"Q6" is used as a label without an independently verifiable source.**
  The design-time task prompt and conductor addendum both use "Q6" for
  the run-time re-check of `kbaseincubator.clearinghouse`'s existence,
  presumably from PRD-level numbering I could not reach (the PRD lives on
  `wip` in a different repo and the dispatch says not to rely on reading
  it). I used "Q6" as this document's own label for that specific check,
  matching the success-criteria wording, rather than inventing a
  different name.
- **2.3's namespace literal was also converted to the same
  unresolved-placeholder pattern**, beyond what the addendum explicitly
  named (it named only 2.1 and 2.2). 2.3's acceptance-check code block
  had the identical hardcoded, unconfirmed `` `clearinghouse` `` literal
  in a runnable snippet -- the same failure class A3 describes -- so I
  fixed it for consistency. This is a judgment call, not something either
  the design-time prompt or the addendum named by section number.
- **OP0's four questions are unresolved by design, not by omission.**
  Per the addendum, I did not attempt to answer them and did not
  re-dispatch the trigger envelope; the section records their status as
  of the 2026-09-12 attempt and defers the actual answers to whoever runs
  OP0 next, from an attended pod session.
- No source or test file was read as "to be changed" -- only inspected
  for accuracy (`clearinghouse_bootstrap_adapter.py`, `transports.py`,
  `capability.py`, `clearinghouse_schema.py`,
  `scripts/clearinghouse_parity_check.py`, and
  `tests/berdl/test_clearinghouse_schema.py`). None were modified.
