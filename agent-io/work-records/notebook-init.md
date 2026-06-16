# Work Record: notebook-init

## task_id
notebook-init

## branch
worknb/notebook-init

## commit_shas
- 52f4bf5f1a3a4d95ed54c5fb4ecfa06f6f50da56

## summary
Added the idempotent `kbu notebook-init <repo> [--project <topic>] [--update]` CLI subcommand (Module 1 of the work-notebooks PRD) to KBUtilLib. The command implements three branch cases: (a) repo missing → full bootstrap with git init, Cursor .code-workspace, .claude/ dir, and a notebooks/ tree with shared roots (models/genomes/data) and first PRJ; (b) repo present but notebooks/ missing → scaffold notebooks tree and first PRJ; (c) notebooks/ present → add the named PRJ-<topic>/, refusing (non-zero, no writes) if it already exists. The --update flag re-deploys the work-notebook bundle (jupyter-dev, kbu-run, synthesize) into .claude/commands/ without touching notebooks/PRJs. Topic normalization uses the PRD advisory #1 rule (lowercase ASCII, non-[a-z0-9] → _, collapse, trim). The command is wired into the main CLI group. Registry integration uses assistant.state.registry.find_by_repo_path/add_project when importable, degrades to a name-derived project_id with a notice otherwise. Bundle deployment is a direct-copy from ClaudeCommands/agent-io/skills/ (claude-skills sync-repos cannot target arbitrary paths not in project_registry.yaml), isolated in _deploy_bundle() for Phase 4 adjustment. No BERIL skills are ever deployed.

## files_touched
- `src/kbutillib/cli/notebook_init.py` — new implementation (330 lines)
- `src/kbutillib/cli/__init__.py` — added import + `main.add_command(notebook_init_cmd, name="notebook-init")`
- `tests/cli/test_notebook_init.py` — new tests (44 tests)
- `agent-io/work-records/notebook-init.md` — this file

## success_criteria_check

- **kbu notebook-init implements all three branch cases**: PASS — bootstrap (repo missing), scaffold (repo+notebooks missing), add-PRJ (notebooks present) all implemented and tested.
- **clobber-refusal**: PASS — exits non-zero and writes nothing when PRJ exists; tested by TestClobberRefusal (2 tests: exit code, no writes).
- **--update**: PASS — re-deploys bundle without disturbing notebooks/PRJs; tested by TestUpdate (4 tests).
- **produces the specified tree (models/genomes/data, PRJ-<normalized-topic>/ with util.py/NBCache/NBOutput)**: PASS — verified by TestBootstrap, TestScaffoldNotebooks, TestAddPrj tests asserting each directory and file.
- **.code-workspace with folders entry**: PASS — TestBootstrap::test_creates_code_workspace asserts `"folders"` key with `{"path": "."}`.
- **gitignore block**: PASS — TestBootstrap::test_gitignore_block_written asserts marker-delimited block with three patterns; idempotency tested in TestAddPrj::test_idempotent_gitignore.
- **.kbu-run.json with project_id worknb-<repo_basename>**: PASS — TestBootstrap::test_kbu_run_json_written and TestDegradedAssistant::test_writes_kbu_run_json_without_assistant assert form `worknb-<repo_basename>`.
- **deploys only the work-notebook bundle and never BERIL skills**: PASS — TestBundleSafety asserts no BERIL skills in deployed commands dir and that _WORKNB_BUNDLE contains no BERIL skill names.
- **degrades gracefully without AIAssistant/ClaudeCommands**: PASS — TestDegradedAssistant (2 tests) and TestDegradedClaudeCommands (3 tests) confirm graceful degradation and exit 0.
- **scaffolding tests pass**: PASS — 44/44 tests pass.

## tests_run

```
cd ~/.maestro/worktrees/notebook-init
python -m pytest tests/cli/test_notebook_init.py -v
```
Result: 44 passed in 0.97s

Full suite (excluding pre-existing failures on main):
```
python -m pytest --ignore=tests/kb_app_runner --ignore=tests/harness -q \
  --deselect=tests/cli/test_init.py::TestDoctorCommand::test_doctor_prints_one_line_per_probe
```
Result: 1147 passed (plus 19 failures and 4 errors that are pre-existing on main; none in files I touched).

Pre-existing failures confirmed not introduced by this task:
- `tests/cli/test_init.py::TestDoctorCommand::test_doctor_prints_one_line_per_probe` — probe count assertion mismatch
- `tests/test_ms_biochem_deltag.py::*` — unrelated biochem module
- `tests/test_task_a_venv_doctor.py::*` — unrelated venv doctor
- `tests/test_comprehensive_gapfill_wrapper.py::*` — unrelated gapfill integration tests

## caveats

1. **Bundle deployment uses direct-copy, not claude-skills**: The `claude-skills sync-repos` command requires skills to have `deploys_to_repos` frontmatter and to be resolved through `project_registry.yaml`; it cannot target an arbitrary path. Direct-copy from `ClaudeCommands/agent-io/skills/` is therefore the correct implementation per PRD clarification #3/#6. The copy logic is isolated in `_deploy_bundle()` so Phase 4 (worknb-deploy-integration) can swap to claude-skills if/when the CLI is extended.

2. **kbu-run and synthesize skills don't exist yet in ClaudeCommands**: Only `jupyter-dev.md` is currently present at `ClaudeCommands/agent-io/skills/`. The bundle deployment will print a notice for `kbu-run` and `synthesize` and skip them until those skill files are created (Modules 5 and 7). This is correct behavior per the PRD (skip+notice for missing sources).

3. **Context directory for jupyter-dev**: `jupyter-dev` has no companion context directory, so only the `.md` file is copied. If a context directory is added in the future, `_deploy_bundle()` will pick it up automatically.

4. **AssistantState registry attachment**: `find_by_repo_path` returns a list; the implementation takes the first match (`matches[0]`). If multiple registry entries share the same repo_path, the first is used. This is consistent with the PRD's "attach to an existing entry by repo_path" wording.

5. **Topic normalization is case-insensitive Unicode-safe**: Non-ASCII characters map to `_` via the `[^a-z0-9]+` regex on already-lowercased input, which handles Unicode without crashing.
