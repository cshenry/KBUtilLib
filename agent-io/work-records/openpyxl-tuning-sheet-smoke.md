# Work Record — openpyxl-tuning-sheet-smoke

## task_id
`task-936c11d3` (gt1, task 1 of 9, PRD `go-mf-council-autoresearch-v1`, dev 1988)

## base commit
`1ac7958` (`docs(council): live acceptance of the five seat/capture fixes on real CLIs`)

## branch
`maestro/developer/gomf-gt1-openpyxl-smoke`

## precondition: MET

The prerequisite build `council-seat-schemas-and-capture-v1` is on main:
`src/kbutillib/domains/ai/llm/apis/schema_compat.py` exists (11253 bytes).
Verified before any edit. No council engine, store schema or existing query
type was touched by this task.

## summary

Declares a new optional extra `council-tuning` (openpyxl only) and adds a
REAL-dependency round-trip smoke test for the four workbook features the
council's human-in-the-loop tuning sheet is built from. Nothing in KBUtilLib
had ever used openpyxl, so nothing established that those features survive
openpyxl's own save/load cycle.

## openpyxl version actually used

| | |
|---|---|
| **openpyxl version** | **3.1.5** |
| installed into | `/scratch/chenry/venvs/gomf-gt1-20261116` (the task's granted scratch venv) |
| import resolved from | `/scratch/chenry/venvs/gomf-gt1-20261116/lib/python3.11/site-packages/openpyxl/__init__.py` (confirmed by `openpyxl.__file__` in the same interpreter that ran the test) |
| interpreter | `/home/chenry/venvs/kbdl/bin/python`, CPython 3.11.15 |
| how openpyxl reached that interpreter | `PYTHONPATH=$PWD/src:/scratch/chenry/venvs/gomf-gt1-20261116/lib/python3.11/site-packages` — the scratch venv's site-packages placed FIRST, so the grant-venv copy is the one imported. Nothing was installed into `~/venvs/kbdl`. |

**Note worth recording, because it contradicts the task prompt's premise in a
way that does not change the work:** openpyxl 3.1.5 is *already* importable
from the `kbdl` environment on h100, via a `.pth`-added path
(`/scratch/chenry/venvs/kbdl-science-py311/.../openpyxl`) — presumably pulled
in transitively by the scientific stack. So "the test would skip on h100
without this install" is false *on this machine*. KBUtilLib still **declares**
no openpyxl dependency anywhere, which is the thing the new extra fixes; and
the measured run above imported the grant-venv copy, not that one. Both are
3.1.5.

## what changed

### `pyproject.toml:125-145` — new `council-tuning` extra

```toml
council-tuning = [
  "openpyxl >=3.1.5",
]
```

Placed immediately after the existing `council` extra, with a comment block in
the style of its neighbours explaining what it is for, why it is optional, why
the floor is `3.1.5`, and why it is deliberately **not** folded into `all`
(same rationale the file already records for `backfill` and `council`: `all`
is "every transport plus the docs build", and a spreadsheet writer is neither).

`3.1.5` is the current 3.1.x release on PyPI and the exact version the smoke
was run against — a measured floor, not an invented one. No upper bound; the
3.1 series has held this API stable.

### `tests/domains/ai/council/test_openpyxl_roundtrip.py` (new, 125 lines)

One test, `test_tuning_sheet_survives_a_full_openpyxl_round_trip`, with no
mock anywhere: openpyxl writes every file and openpyxl reads every file.

1. Builds a sheet named `tuning` with header row `("term","rationale","mark")`.
2. `ws.freeze_panes = "A2"`.
3. A long GO-term rationale in `B2` with `Alignment(wrap_text=True, vertical="top")`.
4. A list data-validation dropdown, `formula1='"R,W,?"'`, over `C2:C200`.
5. Saves to `tmp_path/tuning.xlsx`.
6. Reloads with `openpyxl.load_workbook`; asserts header, freeze pane, wrap,
   validation formula, and that the mark cell is still empty.
7. Makes the edit **a human would make** — `ws["C2"] = "R"`, i.e. typing a
   value from the dropdown into the cell — and saves again.
8. Reloads a second time and asserts the mark round-tripped (`"R"`) **and**
   that the freeze pane, the wrapped alignment, the long text and the
   validation formula + sqref all still hold.

Step 8 is the point of the test. openpyxl re-serialises the *whole* workbook on
every save rather than patching the file, and the four features live in four
different parts of the format (`sheetViews`, `cellXfs`, `dataValidations`,
`sharedStrings`). "Wrote it once, read it back" would not catch a feature
dropped by the second write; this does.

Module-level `pytest.importorskip("openpyxl", reason=...)` names the extra in
the skip reason.

## verification

All runs: `cwd` = the worktree, `TMPDIR=/scratch/chenry/maestro-task-scratch/task-936c11d3`,
interpreter `/home/chenry/venvs/kbdl/bin/python`.

### 1. the new test, run FOR REAL (not skipped) — exit 0

```
PYTHONPATH=$PWD/src:/scratch/chenry/venvs/gomf-gt1-20261116/lib/python3.11/site-packages \
  python -m pytest tests/domains/ai/council/test_openpyxl_roundtrip.py -v -p no:cacheprovider --no-header -rs
```
```
tests/.../test_openpyxl_roundtrip.py::test_tuning_sheet_survives_a_full_openpyxl_round_trip PASSED
1 passed in 0.42s
```
**exit 0.** `PASSED`, not `SKIPPED` — this is the success criterion.

### 2. the skip path, verified empirically — exit 0

Verified by hiding openpyxl with a `sys.meta_path` finder that raises
`ModuleNotFoundError` (in `$MAESTRO_TASK_SCRATCH`, nothing installed or
uninstalled):
```
SKIPPED [1] tests/domains/ai/council/test_openpyxl_roundtrip.py:26: the council
tuning sheet needs the `council-tuning` extra: pip install 'kbutillib[council-tuning]'
1 skipped in 0.04s
```
**exit 0.**

> Method note for whoever repeats this: a stub module that raises a bare
> `ImportError` does **not** work under pytest 9.1.1 — `importorskip` lets it
> through as a collection error. Only `ModuleNotFoundError` (what a genuinely
> absent package raises) is treated as "not installed". Cost one wrong
> measurement here.

### 3. regression check (targeted) — exit 1, ZERO regressions

```
python -m pytest tests/test_python_floor_syntax.py tests/domains/ai/council/ \
  -q -p no:cacheprovider --no-header
```
```
6 failed, 1535 passed, 7 skipped in 419.10s (0:06:59)
```
**exit 1**, and the 6 failures are **node-ID-identical** to the
`MACHINE_LOCAL_FAILURES_NOT_PRODUCT_DEFECTS` set in
`build-baselines/kbutillib-h100-main-6310d61.json`:

```
test_types_config.py::TestPackagedDefaultConfig::test_cli_endpoints_default_to_one_call_at_a_time
test_types_config.py::TestPackagedDefaultConfig::test_codex_seats_are_marked_unverified
test_types_config.py::TestPackagedDefaultConfig::test_default_profile_is_mixed_subscription
test_types_config.py::TestPackagedDefaultConfig::test_every_packaged_profile_builds_a_roster
test_types_config.py::TestPackagedDefaultConfig::test_on_a_fresh_machine_with_no_kbutillib_config
test_types_config.py::TestPackagedDefaultConfig::test_the_packaged_config_declares_no_poplar_endpoint
```

All six are the known h100 config leak (`/home/chenry/.kbutillib/config.yaml`
reaching tests that monkeypatched `HOME`; the captured stderr in this run shows
exactly that load). **No new failing node id. No previously-passing test
fails.**

The full `tests/` suite was deliberately NOT re-run: the cached baseline
records 2665–3416 s for it, far above the 600 s Bash ceiling, and the baseline
file itself instructs sub-agents to run a targeted selection. The selection
above is the set that can see this diff — `test_python_floor_syntax.py` is the
only test in the repo that reads the real `pyproject.toml`, and
`tests/domains/ai/council/` is where the new file lives.

### 4. `pyproject.toml` still parses; extra reads back correctly

```
tomllib → project.optional-dependencies['council-tuning'] == ['openpyxl >=3.1.5']
'council-tuning' in all → False   (as intended)
```

## reconciliation against the tree

Nothing material diverged. Checked and found exactly as the prompt described:
`schema_compat.py` at the stated path; the council package at
`src/kbutillib/domains/ai/council/`; the extras block and the `council` extra
in `pyproject.toml`; the h100 baseline file and its six-name machine-local set
(the *corrected* count of six, not the superseded four). Two notes:

- The prompt's framing "openpyxl, which KBUtilLib does not use today" is true
  of the **declarations** but not of the h100 **environment** — see the version
  table above. Did not change the approach; recorded because a reader could
  otherwise conclude the test's real-dependency run was accidental.
- The prompt's "strict-clean schemas only" clause describes JSON schemas for
  council roles. This task adds no schema, so the clause has nothing to bind
  to here; it is carried by the later gt tasks.

## not run

`ruff` is not installed in this environment (`no module named ruff`; no `ruff`
on PATH), so the repo's lint gate was not exercised on the new file. It was
written to the same conventions as its neighbours (`from __future__ import
annotations`, `# noqa: E402` on the post-`importorskip` imports, isort-ordered
stdlib/third-party groups).

## inferred rather than verified

- That the tuning sheet's eventual writer will use exactly these four features
  in this combination. The task specified them; no sheet-writing code exists
  yet, so the test pins the *capability*, not a caller.
- That `>=3.1.5` is the newest 3.1.x. It is what a plain `pip install openpyxl`
  resolved to in this environment on 2026-11-16; PyPI was not enumerated for a
  higher 3.1.x.
- That openpyxl's API is stable across all of 3.1.x (the justification for the
  open upper bound) — from the series' track record, not measured here.
