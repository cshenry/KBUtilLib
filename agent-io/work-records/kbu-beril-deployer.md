# Work Record: kbu-beril-deployer

## task_id
kbu-beril-deployer

## branch
kbu-beril/deployer

## commit_shas
- 42924637da0f21ae3c7650fc791a5eb136af0a67

## summary
Implemented PRD kbu-beril-augmentation Module 1 — the `kbu beril` deployer command group. Added `src/kbutillib/cli/beril.py` with two Click subcommands (`install` and `doctor`, no `configure`) registered under the existing `kbu` Click group. `install` validates the BERIL root, copies the three skill dirs idempotently, renders preferences.md only if absent (never clobbers), discovers a Python interpreter via .venv → sys.executable → python3, records it in install.json, and pip-installs KBUtilLib with a version-match skip guard and PEP-668 retry; `--dry-run` prints all planned actions without writing any files. `doctor` is a pure-read four-check health probe (skill dirs, import success, version match, preferences.md present) returning 0 only if all pass. Output shape follows CRAFT CLI conventions: per-step `── name`, ✓/✗ lines, summary block, return codes 0/1/2. 19 passing tests in `tests/test_beril_deployer.py` (force-added past gitignore) cover all required scenarios.

## files_touched
- `src/kbutillib/cli/beril.py` — new file; full implementation of `beril_cmd` group with `install_cmd` and `doctor_cmd`
- `src/kbutillib/cli/__init__.py` — added `from .beril import beril_cmd` import and `main.add_command(beril_cmd, name="beril")`
- `tests/test_beril_deployer.py` — new file; 19 tests (force-added with `git add -f`)
- `agent-io/work-records/kbu-beril-deployer.md` — this file

## success_criteria_check

**kbu beril install and doctor are implemented (no configure)**
PASS — `install` and `doctor` subcommands exist under `kbu beril`; no `configure` subcommand was added.

**install does untracked skill-dir copy**
PASS — `shutil.copytree` into `<BERIL_ROOT>/.claude/skills/`; files are never staged or committed. Verified by `test_skill_dirs_untracked`.

**render-if-absent-never-clobber preferences**
PASS — `prefs_dest.exists()` guard before rendering; existing content left byte-for-byte unchanged. Verified by `test_preferences_rendered_if_absent` and `test_preferences_not_clobbered`.

**interpreter discovery + install.json**
PASS — `.venv/bin/python` → `sys.executable` → `python3` chain; writes `.claude/kbu/install.json` with `interpreter` and `kbutillib_version`. Verified implicitly by install tests and `test_doctor_green_after_clean_install` (which reads install.json).

**guarded idempotent pip with PEP-668 retry**
PASS — version probe (`_installed_version_under`) gates the pip call; PEP-668 retry present in `_run_pip_install`. Pip skip path verified by `test_pip_skipped_when_version_matches`.

**--dry-run no-op**
PASS — all write operations guarded by `if dry_run:` branch; `test_dry_run_no_files_written` verifies root is byte-identical before/after.

**doctor reports four checks with correct exit codes**
PASS — checks: skill dirs (3), import succeeds, version matches, preferences.md present. Exit 0 on all pass, exit 1 on any fail. Verified by `test_doctor_green_after_clean_install`, `test_doctor_fail_missing_skill_dir`, `test_doctor_fail_import_fails`, `test_doctor_fail_version_mismatch`.

**tests/test_beril_deployer.py passes against a temp fake BERIL root**
PASS — 19/19 tests pass (`PYTHONPATH=src python3 -m pytest tests/test_beril_deployer.py -q`).

**git checkout v0 survival**
PASS — `test_skill_dirs_survive_git_checkout` confirms all skill dirs and `.claude/kbu/` survive `git checkout v0` because they are untracked.

**--dry-run no-op assertions**
PASS — `test_dry_run_no_files_written`, `test_dry_run_mentions_skill_dirs`, `test_dry_run_mentions_interpreter` all pass.

## tests_run
```
PYTHONPATH=src python3 -m pytest tests/test_beril_deployer.py -q
# Result: 19 passed in 4.50s

PYTHONPATH=src python3 -m pytest tests/test_beril_skill_bundle.py -q
# Result: 43 passed in 0.06s  (pre-existing tests; no regressions)
```

## caveats
- `mix_stderr` was removed from Click's `CliRunner` before 8.x; tests use the bare `CliRunner()` and `result.output` (which mixes stdout+stderr in the test harness). This is consistent with Click 8.3.1 installed in this environment.
- The pip step in production will actually try to install from PyPI via `pip install --upgrade KBUtilLib`. In a BERIL project that has no PyPI connection the pip step will fail; the operator should use the returned exit code (1 = partial) and resolve manually. The PRD does not specify an offline mode.
- `_installed_version_under` runs a subprocess against the target interpreter to probe the installed version; this is intentionally a subprocess call (not `importlib.metadata` in the deployer process) so it works across interpreter boundaries.
- The beril module locates skill dirs via `Path(kbutillib.__file__).parent / "beril" / "skills"`. This works for both editable and wheel installs as long as package data is included (it is, per the existing repo layout).
