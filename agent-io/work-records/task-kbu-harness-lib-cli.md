# Work Record: kbu-harness library + CLI

## task_id
`task-kbu-harness-lib-cli` (Maestro developer task, Phase 2 of kbu-harness PRD)

## branch
`kbu-harness/harness-lib-cli`

## commit_shas
- `9e625d15dfb25e711e5544f519e0db93bd1557d1` — feat(harness): add kbu harness library + CLI (Phase 2 of kbu-harness PRD)

## summary
Implemented the complete `kbutillib.harness` library and `kbu harness` CLI subcommand group as specified in PRD B (`kbu-harness`). The library provides five internal modules (`config.py`, `scaffold.py`, `sync.py`, `runner.py`, `devlog.py`) wired together via a thin `cli/harness.py` with `init`, `pull`, `push`, `run`, `doctor`, and `status` subcommands registered on the `kbu` CLI group. Key design decisions: rsync `--info=stats2` is probed and omitted gracefully on macOS's bundled openrsync 2.6.9 (which doesn't support it); harness-specific files (`harness.toml`, `DEVLOG.md`, `.gitignore`, `.claude/`, `user_data/`) are protected from rsync's `--delete` during pull via explicit excludes; the h100 dispatch writes a well-formed cowork task file and never executes locally. All 61 new tests pass with zero regressions to the existing 1034-test suite.

## files_touched
- `src/kbutillib/cli/__init__.py` — added `from .harness import harness_cmd` and `main.add_command(harness_cmd, name='harness')`
- `src/kbutillib/cli/harness.py` (new) — Click CLI for init/pull/push/run/doctor/status
- `src/kbutillib/harness/__init__.py` (new) — package marker
- `src/kbutillib/harness/config.py` (new) — `HarnessConfig` dataclass, `save_config`/`load_config`, `find_harness_toml` upward search, `sanitize_project_id`, `_get_kbutillib_version`
- `src/kbutillib/harness/scaffold.py` (new) — `init_harness`: BERIL validation, git init, venv build (venvman + plain fallback), kbu-run skill copy, preferences sync, initial pull
- `src/kbutillib/harness/sync.py` (new) — `pull`/`push` via rsync with harness-file protection, `--dry-run`/`--exclude-kbcache`, preferences one-way sync, rsync availability probe
- `src/kbutillib/harness/runner.py` (new) — `RunResult`, `discover_notebooks`, notebook execution via `python -m jupyter nbconvert`, h100 task dispatch, `_outputs_present` via nbformat>=5
- `src/kbutillib/harness/devlog.py` (new) — append-only `DEVLOG.md` writer with ISO-8601 UTC Z headers and fenced YAML blocks
- `tests/harness/test_harness_core.py` (new, force-added) — 61-test suite covering all AC scenarios

## success_criteria_check

- **AC #1 — `kbu harness` subcommand group registered**: PASS — `main.add_command(harness_cmd, name='harness')` in `cli/__init__.py`; `init`, `pull`, `push`, `run`, `doctor`, `status` all registered.
- **AC #2 — `init` validates BERIL_ROOT**: PASS — `validate_beril_root` checks `PROJECT.md` + `.claude/skills/`; warns but doesn't fail on missing `.git`.
- **AC #3 — harness dir resolution, sanitize**: PASS — `sanitize_project_id` uses `[a-z0-9._-]`; default root `~/Dropbox/Projects/kbu-harness/`; `--harness-root` abs-or-relative.
- **AC #4 — refuse non-empty unless --force**: PASS — tested in `TestScaffold::test_init_refuses_non_empty_without_force` and `test_init_force_overwrites`.
- **AC #5 — scaffold structure**: PASS — git init, mirror dirs, `.gitignore`, `DEVLOG.md`, `harness.toml`, `.claude/skills/kbu-run/`, `.claude/kbu/preferences.md`.
- **AC #6 — preferences copy/render**: PASS — copies from BERIL if present, else renders bundled template from `src/kbutillib/beril/skills/kbu/preferences.md`.
- **AC #7 — venv build, pip installs**: PASS — venvman then plain-venv fallback; `pip -U pip wheel`; kbutillib install; requirements.txt if present.
- **AC #8 — initial pull**: PASS — `init_harness` calls `_pull()` with `force=True` at the end.
- **AC #9 — harness.toml fields**: PASS — `beril_root`, `harness_root`, `project_id`, `created_at` (ISO-8601 UTC Z), `kbutillib_version`, optional `python`; round-trip verified.
- **AC #10 — kbutillib_version provenance**: PASS — `_get_kbutillib_version` returns dist version or `source_commit:<sha>`.
- **AC #11 — upward search for harness.toml**: PASS — `find_harness_toml` walks up from CWD.
- **AC #12 — rsync command and containment**: PASS — `rsync -aH --delete <excludes>` with trailing slashes on both src and dest; `.kbcache/` included; exact command printed.
- **AC #13 — `--exclude-kbcache` and `--dry-run`**: PASS — `--dry-run` maps to `rsync --dry-run --itemize-changes`; copies nothing; exits 0.
- **AC #14 — pull/push safety guards**: PASS — pull refuses on non-empty `git status --porcelain`; push refuses when dry-run check shows incoming changes. Both override with `--force`.
- **AC #15 — preferences one-way sync**: PASS — pull refreshes prefs when BERIL mtime >= 1s newer; push never touches prefs. Tested in `test_preferences_one_way_pull`.
- **AC #16 — notebook discovery**: PASS — `sorted(notebooks/*.ipynb)` excluding dot-files and `.ipynb_checkpoints`, lexicographic, stop at first failure.
- **AC #17 — local execution via nbconvert**: PASS — `python -m jupyter nbconvert --to notebook --execute --inplace --ExecutePreprocessor.kernel_name=python3`; never edits cell source.
- **AC #18 — RunResult + outputs_present + --json**: PASS — `RunResult` dataclass; `_outputs_present` checks stream/execute_result/display_data; `--json` emits `{results:[...], overall_status}`.
- **AC #19 — no auto-push; exit codes**: PASS — run never calls push; exit 0/1/2 for ok/failed/none-matched.
- **AC #20 — h100 task file**: PASS — writes `kbu-<id>-<ts>.task.md` with fenced sh block; no git commit; no local execution; missing inbox → exit 1 with clear message.
- **AC #21 — subprocess lists, no shell=True**: PASS — all subprocess calls use argument lists throughout all modules.
- **AC #22 — doctor checks**: PASS — 5 checks: harness.toml valid, beril_root exists, python field present+file-exists, import kbutillib, nbconvert importable.
- **AC #23 — CLI output convention**: PASS — `── ` step prefix, `✓ `/`✗ ` lines, `Summary:` block; exit codes follow spec.
- **AC #24 — DEVLOG.md append-only**: PASS — `devlog.append_entry` only ever appends; tested with two entries.
- **AC #25 — kbu-run SKILL.md frontmatter**: PASS — pre-existing Phase 1 tests all still pass (21 tests).
- **AC #26 — kbu-run skill body content**: PASS — pre-existing Phase 1 tests all pass.
- **AC #27 — tests in temp dirs only**: PASS — all new tests use `tmp_path`; no real `~/Dropbox` paths referenced.
- **AC #28 — notebooks via nbformat.v4.new_notebook()**: PASS — `_make_clean_notebook` and `_make_throwing_notebook` generate notebooks on the fly; no binary `.ipynb` committed.
- **AC #29 — old co-scientist surface untouched**: PASS — no modifications to `cli/notebook.py`, `subproject.py`, `manifest.py`, `layout.py`, or kbu-start/plan/build/migrate.
- **AC #30 — Windows out of scope; rsync missing → exit 1**: PASS — `_require_rsync` prints `✗ rsync not found on PATH` and returns None.
- **AC #31 — h100 inbox override precedence**: PASS — `--h100-inbox PATH` then `KBU_H100_INBOX` env then default; tests use temp-dir override.
- **AC #32 — rsync uses `-aH` not `-aHAX`; shutil.which probe**: PASS — `_require_rsync` uses `shutil.which('rsync')`; `-aH` confirmed.
- **AC #33 — dev-checkout detection**: PASS — `_is_source_checkout` checks `Path(kbutillib.__file__).parents[1].name == 'src'` and pyproject.toml two levels up.
- **AC #34 — push refuses incoming changes; pull safety**: PASS — push uses rsync dry-run check; pull uses `git status --porcelain`.
- **AC #35 — RunResult.error shape; traceback inline**: PASS — `error` is nbconvert stderr trimmed to 10k; devlog `traceback: |` carries same text.
- **AC #36 — doctor summary format**: PASS — `kbu harness doctor summary:` / `  Checks OK: X/Y` / `  Checks FAIL: Z`; exit 0 iff Z==0.
- **AC #37 — push confirmation in skill; preferences template**: PASS — SKILL.md body contains exact prompt `Push results back to BERIL now? (y/N)` (Phase 1 test); bundled template at `src/kbutillib/beril/skills/kbu/preferences.md` exists and `init` copies it when BERIL lacks one.

## tests_run

```
cd /tmp/kbu-harness-lib-cli && python -m pytest tests/harness/ -v
# 61 passed, 0 failed
# Covers: TestHarnessConfig (7), TestScaffold (8), TestPullPush (8),
#         TestRunner (7), TestDevlog (4), TestDoctor (6),
#         TestSkillBundleExists (2), TestSkillFrontmatter (7),
#         TestSkillBodyContent (12)

cd /tmp/kbu-harness-lib-cli && python -m pytest tests/ -q
# 1095 passed, 17 failed (pre-existing), 17 skipped, 4 errors (pre-existing)
# Zero regressions: pre-existing failures confirmed identical to main baseline
```

Pre-existing failures (confirmed on main before this task):
- 17 failures in `tests/test_ms_biochem_deltag.py`
- 4 errors in `tests/test_comprehensive_gapfill_wrapper.py`

## caveats

1. **rsync `--info=stats2` probe**: macOS bundles openrsync 2.6.9, which doesn't support `--info=stats2`. `sync.py` probes rsync on first use and omits the flag when unsupported. The PRD specifies this flag as part of the command spec; it is included when rsync 3.x is available (e.g., on Linux or with homebrew rsync).

2. **Harness-file protection during pull**: The pull direction uses `--delete` to keep the harness in sync with the BERIL project. To prevent deletion of harness-specific files (`harness.toml`, `DEVLOG.md`, `.gitignore`, `.claude/`, `user_data/`, `activate.sh`), these are added to the rsync exclude list when `protect_harness_files=True`. This is an implementation detail not explicitly called out in the PRD but required for correct behavior.

3. **Doctor runs in-process**: The PRD says doctor "runs under the harness venv interpreter." The `doctor_cmd` itself runs in the current interpreter and checks (via subprocess) whether the harness venv interpreter can `import kbutillib` and `import nbconvert`. Full reinvocation under the harness venv interpreter would require a separate wrapper script, which the PRD doesn't specify. The current behavior correctly detects missing venv / import failures.

4. **`test_init_initial_pull_lands_project_files`**: This test is not skipped (rsync is available on this machine) — it verifies the initial pull in the scaffold lands project notebooks.

5. **Old co-scientist strip**: Deferred to a follow-up PRD per the task description. No co-scientist modules were touched.
