# Work Record: cli-cleanup-task-363

## task_id
task-363 (jane.db) — plus the scope Chris added on 2026-09-06

## branch
wip

## commit_shas
- 91feb75 refactor(cli): delete the duplicate kbutillib.cli tree, repoint tests at interfaces.cli
- 8065839 feat(cli)!: remove the researchos and work-notebook commands
- 24e0932 refactor(cli): rename `kbu king` to `kbu kind`, keeping `king` as a silent alias
- 2f609fd test(cli): update the help-integrity test for the king -> kind rename

## summary

Three pieces of work, in order.

### 1. The duplicate CLI tree (task 363, whose premise was wrong)

Task 363 said `cli/king.py` was the lone divergent duplicate among
auto-generated shims. It was not. WP17 moved the CLI to
`kbutillib.interfaces.cli` and left `kbutillib/cli/` holding a re-export
`__init__.py` — but all 26 implementation modules beside it survived as full
second copies. Sixteen were byte-identical; eleven had diverged. `king.py` was
simply the most diverged (5152 vs 7294 bytes), which is why it got noticed.

The copies were not inert:

- 31 test files made 122 references to `kbutillib.cli.<module>`, importing from
  and monkeypatching the dead copies. `tests/cli/test_doctor.py` patched
  `kbutillib.cli.init._probe_*` while `kbu doctor` runs `interfaces.cli.init` —
  two files that differ. Those tests were green against code that does not ship.
- `harness/scaffold.py` imported `kbutillib.cli._template_ops` at runtime.
- `interfaces/cli/__init__.py` loaded `cli/verab.py` by file path via
  `spec_from_file_location`, because `interfaces/cli/verab.py` was a shim
  pointing *backwards* at the legacy tree. Its docstring said why: "so that
  tests can monkeypatch `kbutillib.cli.verab._get_toolkit` correctly." The
  patching hazard was diagnosed, then solved by moving the authoritative file
  to the deprecated path.

Deleted the tree except `__init__.py`, moved `verab.py` to `interfaces/cli/` as
the real implementation, replaced the file-path hack with a plain import, and
repointed all 122 test references. The 23 package-level
`from kbutillib.cli import ...` imports were left alone — they exercise the
shim, which is the point of keeping it.

### 2. Removed the researchos and work-notebook commands

Chris is transitioning fully to KOROS/KIND on kbhub. Removed `kbu researchos`
(including the whole `agents/researchos/` package and the `kbutillib/researchos/`
shim package), `kbu notebook`, and `kbu notebook-init` (with `worknb_util.py`
and its template).

`kbu init-notebook` was KEPT: it is BERIL machinery, not the work-notebook
surface — `worknb_util.py`'s own docstring drew that line — and Chris asked for
BERIL to stay.

`jupyter_client` and `nbclient` were required runtime dependencies used only by
`interfaces/cli/notebook.py`, so both were dropped from pyproject. `nbformat`
stays (harness, domains/notebook, init_notebook, adopt all use it).

### 3. `kbu king` -> `kbu kind`

Renamed the command. `kbu king` still resolves, via an `_AliasedGroup` whose
`get_command` consults `_COMMAND_ALIASES`; the alias is not registered in
`commands`, so it never appears in `--help`. Two tests pin both halves. The
alias exists because `cw-king` — a sync-managed skill in AIAssistant, not
editable from this repo — invokes `kbu king install`.

Only the command was renamed. `KING_CONTEXT` and `KING_PLUGINS_DIR` are read by
KING's own `scripts/serve.sh`; `KING_STACK_DIR` points at its checkout; and
`KING_APPS_DIR` / `~/king-apps` / `serve-king.sh` name live on-disk state on
both primary-laptop and kbhub. Whether KIND replaces KING as the product name
is filed as jane.db task 688.

## verification

- Full suite: 3 failed, 2751 passed, 372 skipped, 12 errors — identical
  failures and errors to the pre-change baseline, all from optional
  dependencies missing in the venv (cobra, argo, kb_ws, kb_plm).
- `kbu kind status` and `kbu king status` both green against the real
  `~/king-apps` (kbutillib-modeling, persistentai-wake).
- Back-compat shim verified by identity: `kbutillib.cli.main is
  kbutillib.interfaces.cli.main`.

Note on the baseline: `KBUtilLib-py3.13` was missing `tomli-w`, `nbclient` and
`jupyter_client`, which made 281 tests fail environmentally. Installed before
taking the baseline; otherwise a real regression would have been invisible in
the noise.

## follow-ups filed

- 687 (T2) — downstream skills still call the removed commands
  (cw-provision-worknb-repo, kbu-workbench, kbu-run, kbu-build, jupyter-dev,
  synthesize, cw-provision-researchos-project). Sync-managed; fix at source.
- 688 (T3) — decide whether KIND replaces KING as the product name.
- 689 (T4) — `layout.py`'s `worknb_*` helpers are now dead code.
- 604 (comment) — `kbutillib-py3.11` venv pinned to a stale maestro worktree;
  confirmed on primary-laptop, not just h100.
