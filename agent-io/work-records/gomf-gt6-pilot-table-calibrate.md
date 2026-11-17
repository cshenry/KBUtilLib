# go_mf tuning task 6 — the pilot table, the marks reader, the calibration report

Maestro task `task-4f324156`, branch `maestro/developer/gomf-gt6-pilot-table-calibrate`,
base `da23f50` (KBUtilLib `main`, go_mf task 5's ledger + adjudication runner).
Written 2026-11-16 on h100.

## precondition

`src/kbutillib/domains/ai/llm/apis/schema_compat.py` is PRESENT (11,253 bytes).
The `council-seat-schemas-and-capture-v1` build has landed, so the task
proceeded. The council engine (`C/engine/`), the store schema and every
existing query type are untouched — nothing in this task needed them.

## what was added

### `src/kbutillib/domains/ai/council/go_tuning/table.py` (new, 1,029 lines)

The artefact Chris opens in Excel, and the reader that turns his marks back
into gold.

- `COLUMNS` — the eighteen columns in the specified order, each with a reading
  width: item id, annotation, source tool, class (`kind (sub_kind)`), ALCF
  terms, go_mf terms, verdict, correct terms, off-menu flag, off-menu
  description, failure type, rules cited, confidence, why, split, position
  swap, `Chris mark`, `Chris note`.
- `write_pilot(results, manifest, matched, xlsx_path, md_path)` — one row per
  un-swapped pilot entry; both files from one row build. Returns
  `{rows, xlsx, md, scope, manifest_id, answer_sha256, item_ids}`.
- `write_spot_check(results, manifest, matched, item_ids, xlsx, md)` — the same
  table over one tranche's persisted spot-check ids, recorded in the workbook
  as `scope = spot_check`.
- `read_marks(xlsx_path, manifest) -> Gold`.
- `render_terms(menu, ids)` — `GO:id | name` one per line, in MENU order, names
  from the item's own menu labels. An id the menu does not hold is still
  printed (sorted after the menu's), because dropping it would hide exactly
  the off-menu answer the research metadata is about.
- `go_ids(text)` — every `\bGO:\d{7}\b` in a note, de-duplicated, written order
  kept. Seven digits is deliberate: a looser pattern reads a number out of
  prose ("GO:1 or so") and calls it gold.
- `answer_digest(rows)` — sha256 over the DISPLAYED answer columns (everything
  but the two mark columns), with `\x1f`/`\x1e` separators that cannot occur in
  a cell.
- `Gold` / `GoldRecord` — see C12 below.

The workbook: frozen header (`A2`), wrap-text on every cell, per-column widths,
and a `DataValidation` list dropdown `"R,W,?"` over exactly the mark column's
data rows (`Q2:Q<n+1>`). Three sheets — `pilot`, `How to mark`, and a hidden
`_provenance`.

The `.md` copy is the same columns in the same order with the same cell text;
newlines become `<br>` and pipes are escaped, which is the only concession a
pipe table forces. It carries the How-to-mark text too, so the terminal copy
is self-describing.

### `src/kbutillib/domains/ai/council/go_tuning/calibrate.py` (new, 726 lines)

- `wilson(successes, total, z=WILSON_Z)` → `(lower, upper)`, `(None, None)` on
  zero observations. `WILSON_Z = 1.959963984540054`, spelled out rather than
  taken from scipy so this module needs no scientific stack.
- `Agreement(right, wrong)` — `n`, `agreement`, `interval`, `as_dict()`.
- `agreement_of(marks)`, `band_for(agreement, lower, n)`.
- `SwapFlips(pairs, final_ids, verdict, off_menu)` — `rate` and `interval` are
  over the source-normalised `verdict` count, the one that moves a calibration
  figure.
- `population_shares(matched)` → `{"class": {...}, "source": {...}}` marginal
  shares over the full extract.
- `report(results, gold, manifest, *, adjudicate_spec_hash, roster_fingerprint,
  population=None)` → `CalibrationReport`, with `to_markdown()` and
  `as_dict()`.
- `record(ledger, report)` and `record_marks(ledger, gold, *, …)` — both append
  ledger kind `calibration` with the three binding fields
  `calibration_gate` matches on, distinguished by a `source` field
  (`"calibrate"` / `"read-marks"`).

### `scripts/council/go_tuning.py` (+297 / −5 lines)

Four new subparsers, sharing `_add_table_options`:

- `pilot-table --manifest --matched --store (--run-dir | --jobs) --xlsx --md`
- `spot-check` — the same, plus `--tranche N` (omit for the newest tranche that
  recorded spot-check ids)
- `read-marks --manifest --xlsx [--out gold.json] [--run-dir --record]`
- `calibrate --manifest --matched --store --xlsx [--out report.md] [--record]`

`--run-dir` is read three ways: the adjudication job ids come from the ledger's
`tranche` records (not from the store — a store can hold another run's
adjudication of the same manifest, and pooling them would table one item
twice), the spot-check ids come from the named tranche's record, and the
`adjudicate_spec_hash` / `roster_fingerprint` bindings come from the newest
matching tranche record. `--spec-hash` / `--roster-fingerprint` override them;
with neither source the command refuses rather than writing a record no gate
will ever match. `--record` without `--run-dir` is a parser error.

Exit codes extended: `3` now also means `read-marks` found rows still carrying
no mark.

## how each binding contract is met, and where it is tested

**C12 — gold provenance and explicit exclusions.** `GoldRecord` carries
`item_id`, `mark`, `provenance`, `ids`, `off_menu`.

| mark | gold | provenance | counts against agreement |
|---|---|---|---|
| `R` | the adjudicated ids, read off the sheet's `correct terms` | `judge` | yes (R) |
| `W` with valid GO ids in the note | those ids, `off_menu` re-derived from the item's menu | `human` | yes (W) |
| `W` with no ids | none — `excluded["…"] = "wrong_without_ids"` | yes (W) |
| `?` | none — `excluded["…"] = "unsure"` | no |
| blank | none — `excluded["…"] = "unmarked"`, and listed in `Gold.unreviewed` | no |

`Gold.marks` covers EVERY row, including blank ones, so agreement cannot be
computed over a silently shrunken denominator. `report()` and `record_marks()`
both refuse while `Gold.unreviewed` is non-empty.
Tests: `test_go_tuning_table.py::TestRoundTrip` (five tests),
`test_go_tuning_calibrate.py::TestReport::test_a_w_without_ids_still_counts_against_agreement`,
`::test_an_unmarked_row_refuses_the_whole_report`.

**C13 — band order, bindings, weighted agreement, three flip rates.**
`band_for` decides in exactly the stated order; `conditional` is "≥ 0.70 and
not trusted", NOT "0.70 to 0.85", so 0.90 with a weak lower bound is
conditional rather than falling through to `not_trusted`. `report()` requires
both binding fields and refuses without them. Unweighted and
population-weighted agreement are both reported, the weight of a
`class|source` cell being the class share times the source share, renormalised
over the cells that have marks. `SwapFlips` separates final-ids, verdict and
off-menu flips, each with its own Wilson interval on the headline rate.
Tests: `TestBands` (10 cases), `TestWeightedAgreement` (4),
`TestSwapFlips` (4), `TestReport::test_a_missing_binding_is_refused`,
`TestLedgerRecord` (4).

**C16 — the How to mark sheet.** A visible sheet stating R/W/?, that a blank is
not an answer, that `both acceptable` is descriptive only and each row has one
canonical correct set, and that sorting or editing other columns will be
refused. Test: `TestShape::test_a_how_to_mark_sheet_states_what_the_three_marks_mean`.

**C21 — literal strings, hidden provenance sheet, three refusals.** Every cell
is forced to `data_type == "s"` after assignment, because openpyxl infers a
formula from a leading `=` and an annotation can begin with anything — the
fixture carries `"=SUM glucose ABC transporter, ATP-binding protein"` for
exactly this. The hidden `_provenance` sheet holds `manifest_id`, `scope`,
`answer_sha256`, `rows`, the column list, the writing commit, and a per-item
`item id → menu ids` table. `read_marks` refuses a duplicate id, a different
manifest id, a hash mismatch, a changed column order, a row count that is not
the manifest's pilot set (pilot scope) or not its own recorded count, a row the
provenance sheet does not list, and a mark cell holding anything but R/W/?.
Tests: `TestRefusals` (eleven tests, including the two accidents that change
nothing a reader would see — a deleted row and a sorted answer column).

## verification

Interpreter: `/scratch/chenry/venvs/gomf-gt6-20261117/bin/python` (3.11.15), a
scratch venv created for this task under the attached grant, holding
`openpyxl 3.1.5` (the `council-tuning` extra's floor), `pytest 9.2.0`,
`sqlalchemy 2.1.9`, `pydantic`, `pyyaml`, `requests`. Nothing was installed
into a shared venv.

```
PYTHONPATH=$PWD/src python -m pytest \
  tests/domains/ai/council/test_go_tuning_table.py \
  tests/domains/ai/council/test_go_tuning_calibrate.py \
  tests/domains/ai/council/test_go_tuning_script.py \
  -q -p no:cacheprovider --no-header
```
→ **103 passed**, exit 0: 41 table + 42 calibrate + 20 script. 92 of those are
new in this task (41 + 42 + 9 script); the script file's 11 pre-existing tests
still pass.

Pre-existing go_tuning suites, re-run unchanged on the branch:
`test_go_tuning_adjudicate.py` + `test_go_tuning_conflicts.py` +
`test_go_tuning_sample.py` + `test_go_tuning_ledger.py` → **132 passed**,
exit 0.

The same four were measured in this worktree BEFORE any edit, in two commands:
`sample` + `ledger` → 58 passed, and `adjudicate` + `script` + `conflicts` →
85 passed, of which the script file was then 11 tests. 85 − 11 + 58 = **132**.
Identical by count and by file, and no nodeid in those four changed name.

`uvx ruff check` over all seven touched/added files → clean (4 import-order
findings auto-fixed).

Regression comparison for `tests/domains/ai/council/` as a whole is recorded
in the section below.

## regression framing

The cached baseline `build-baselines/kbutillib-h100-main-6310d61.json` was
read rather than re-measured, per its own instruction — the full suite is
~48–57 minutes, far above a sub-agent's Bash ceiling. It records six
machine-local `test_types_config.py::TestPackagedDefaultConfig` failures
caused by the h100 user config leaking past a monkeypatched `HOME`; those are
not this task's and are not regressions.

This task's diff is **purely additive**: two new library modules, three new
test files, new subparsers appended to the CLI script, and docstring-only
edits to `go_tuning/__init__.py`. No existing library function, class or
behaviour was modified, so nothing that passed before can fail because of a
changed code path — the targeted comparison above establishes that for every
suite that imports the package this task touches.

A sweep of the whole `tests/domains/ai/council/` directory was started and
**deliberately abandoned at 740 tests (34%, 0 failures) after 27 minutes**.
Two reasons, and the second is the important one. It was going to take ~80
minutes, past this task's budget. And more to the point it would have produced
a number with nothing to compare it to: no full-council measurement exists at
`da23f50`, so the run could only have re-counted pre-existing failures (the
six machine-local `test_types_config` ones among them) and invited them to be
read as regressions. 740/740 passing is recorded as the partial fact it is,
not as a clean sweep.

## reconciliations between the task prompt and the tree

1. **`openpyxl` has no `Cell.set_explicit_value`.** It was removed in
   openpyxl 3.x. The literal-string guard (C21) is `cell.value = text` followed
   by `cell.data_type = "s"`, verified to survive a save/load round trip.
2. **A control character cannot go in a cell.** The provenance sheet's column
   list was first written `\x1f`-joined (matching the digest's separators) and
   openpyxl raised `IllegalCharacterError`. It is comma-joined; no header holds
   a comma. The digest still uses the control separators — it is hashed, never
   written.
3. **`read_marks(xlsx_path, manifest)` cannot derive off-menu from the
   manifest.** C12 asks that a `W` with GO ids derive `off_menu` from menu
   membership, but the manifest entries carry no menu and the signature admits
   no third input. The menu ids are therefore persisted per item on the hidden
   provenance sheet and read back from there; a row the sheet does not list is
   refused rather than defaulted (defaulting to an empty menu would mark every
   replacement id off-menu).
4. **The manifest records no `class × source` population.**
   `report["population"]` is class counts only. `report()` takes an optional
   `population` marginal pair; the CLI passes `population_shares(matched)` off
   the extract. With neither, the class shares come from the manifest's
   recorded population and the source shares from the marked rows, and the
   report says so in `weights_from` rather than letting a reader assume both
   marginals came from the corpus.
5. **The prompt's Wilson example is right and worth pinning.** 85/100 →
   0.7672–0.9069, matching "about 0.767-0.907" to three places. Two figures I
   had to compute rather than take from the prompt, and both are now pinned:
   17/20 → 0.6396–0.9476, and 10/10 → lower 0.7225, which means a PERFECT ten
   out of ten is `conditional` and not `trusted`. That is the correct answer
   under C13 and it is tested as such.
6. **`read_marks` cannot check a spot-check table against the whole pilot
   set.** The prompt's refusal rule ("the item ids or row count differ from the
   manifest's pilot set") is right for the pilot table and wrong for the
   spot-check table the same prompt also asks for. The workbook records its
   `scope`; a `pilot` scope is checked for being the manifest's whole
   un-swapped pilot set, a `spot_check` scope for being a SUBSET of it plus an
   exact match against its own recorded row count and id list.
7. **The spot-check import path needs a band, and has no results.** The gate
   (`adjudicate.calibration_gate`) rejects a record whose band is not
   `trusted`/`conditional`, so a marks-only import cannot leave the band blank.
   `record_marks` computes the band from the imported marks with `band_for`
   directly, and `record` (the full-pilot path) writes the slices beside it.
   Both are kind `calibration`; `source` distinguishes them.

## what was NOT done, and what is inferred rather than verified

- **Nothing was published or uploaded** (the executor note's instruction). The
  two files are written wherever `--xlsx`/`--md` point.
- **No live store was read.** `read_results` is exercised by
  `test_go_tuning_adjudicate.py` against a real `SqliteStore` with fake
  backends; the three new test files build `ItemResult`s directly, deriving the
  verdict and side through `adjudicate.derived_verdict` /
  `adjudicate.verdict_side` rather than re-deriving them. The CLI tests record
  `read_results` rather than running it, which is the pattern
  `test_go_tuning_script.py` already uses for `run_tranches`.
- **The dropdown, freeze and wrap are asserted through openpyxl, not through
  Excel.** The XML openpyxl writes is what the assertions read; that Excel
  honours a `DataValidation` list and `freeze_panes` is inferred from the
  format, not observed on Chris's laptop. The one thing that WOULD be invisible
  to a round-trip test — a leading `=` becoming a formula — is guarded by
  `data_type`, which openpyxl writes into the cell element itself.
- **`write_pilot` is not resumable and does not merge marks.** Re-running it
  overwrites the workbook, discarding any marks already in it. That is the
  right default for a table generated from a fresh adjudication, but an
  operator who re-runs `pilot-table` over a partly-marked file loses the marks.
  Nothing in the prompt asks for a merge; worth knowing before the human step
  begins.
