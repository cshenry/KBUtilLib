# Work Record: kbu-tmfa-p3p4p6-cli-polish

## task_id
kbu-tmfa-p3p4p6-cli-polish

## branch
kbu-tmfa-p3p4p6-cli-polish

## commit_shas
- b239409c9a13d21f4e05b1df476714226b7fcf3c

## summary
Three independent CLI/template/install polish items landed in a single commit. P3 adds a guarded `from modelseedpy.core.msmodelutl import MSModelUtil` import to `util.py.tmpl`, matching the existing try/except + `# type: ignore` style used for numpy, pandas, and cobra. P4 removes the "Copy this file ... and edit the values" instructional header from `preferences.md` so the file is a truthful ready-to-use config snapshot when installed; three keys without code-level defaults (`solver.name`, `organism.focus`, `media.default`) now carry `# unset` inline comments per the task spec; all other keys retain their documented effective defaults. P6 normalizes the two pinned notebook deps in `machine_configs/_default.yaml` from `requests_toolbelt >=0.10.0` / `tomli-w >=1.0` (space before version operator) to `requests_toolbelt>=0.10.0` / `tomli-w>=1.0` (canonical PEP 440, no space) — verified that the prior form was already one YAML list element = one argv token, so the version constraint was binding, but the space was non-canonical; this fix removes any ambiguity across pip versions.

## files_touched
- `src/kbutillib/cli/templates/util.py.tmpl` — added MSModelUtil guarded import block after cobra block
- `src/kbutillib/beril/skills/kbu/preferences.md` — removed 4-line "Copy this file" header; added `# unset` comments on solver.name, organism.focus, media.default
- `machine_configs/_default.yaml` — removed space before `>=` in requests_toolbelt and tomli-w pins

## success_criteria_check

- **Rendered util.py contains a guarded `from modelseedpy.core.msmodelutl import MSModelUtil` import** — PASS. Template renders with the import at line 64; fallback `MSModelUtil = None  # type: ignore` at line 66. Verified via Python `_render_util_template("test-project")`.

- **Installed preferences.md has the 'copy this file' header removed and shows effective code-sourced defaults (blank+marked where no default exists)** — PASS. Header stripped. All numeric defaults (60, 5, 1, 1, 1, 10) retained; boolean defaults (configured: false, gapfill.comprehensive: false) retained; three project-specific/hardware-specific keys (solver.name, organism.focus, media.default) left blank with `# unset` inline comments. YAML parsed cleanly with `yaml.safe_load`.

- **init-notebook dependency pins are confirmed (or fixed to be) single argv tokens so version constraints bind** — PASS (with canonical fix applied). Prior form `requests_toolbelt >=0.10.0` was ONE YAML list element = ONE argv token to pip, so the constraint was already binding. However, the space-before-operator form is non-canonical PEP 440; normalized to `requests_toolbelt>=0.10.0` and `tomli-w>=1.0` for clarity and cross-pip-version safety.

## tests_run

```
pytest tests/cli/test_worknb_util_template.py tests/cli/test_init_notebook.py tests/cli/test_machine.py -v
```

Result: 61 passed, 1 pre-existing failure (`TestRenderUtilTemplate::test_contains_session_for` checks for `def session_for` which does not exist in the template and was failing on `main` before this branch was cut — confirmed via `git stash` + rerun).

Python validation scripts also run inline:
- Template renders with MSModelUtil import: PASS
- preferences.md YAML parses cleanly, blank keys return None: PASS
- machine_configs/_default.yaml deps have no internal spaces: PASS

## caveats

- The pre-existing `test_contains_session_for` failure is on main; this branch does not introduce it and does not fix it (out of scope). The reviewer should note it separately if it needs fixing.
- The `configured: false` sentinel is intentional — the installed default is `false` because the user hasn't yet reviewed the file. The task asked to populate with "effective code-sourced defaults"; the effective behavior of the `/kbu` skill when `configured: false` is to warn and apply the numeric defaults anyway, so leaving it `false` is the truthful starting state.
- P4 only modifies the template source at `src/kbutillib/beril/skills/kbu/preferences.md`. Already-installed copies at `<project>/.claude/kbu/preferences.md` are not updated by this change (the CLI preserves existing files, per `beril.py` lines 373-378).
