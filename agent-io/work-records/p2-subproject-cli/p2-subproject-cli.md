# Work Record — p2-subproject-cli

## task_id
p2-subproject-cli

## branch
task/p2-subproject-cli

## commit_shas
- af61fda52c5e4f03e8b9b9da0f50fd83c2cbf9b4

(Full SHA resolved from worktree; short: af61fda)

## summary
Refactored `src/kbutillib/cli/subproject.py` to implement the kbutillib-v2
PRD requirements for AC #9–#31. Added the `migrate` state to the state
machine (`_STATES`, `_FORWARD`, `_NEXT_ACTION`, `_check_forward_preconditions`).
Refactored `_scaffold_subproject` to use `kbutillib.layout.subproject_subdirs`
for the directory list, render `notebooks/util.py` from the Jinja template at
`src/kbutillib/cli/templates/util.py.tmpl`, drop the retired `references.md`,
and accept an `adopted: bool = False` parameter. Added the `kbu subproject adopt`
Click command with all six pre-flight refusal rules (bootstrapped project check,
path-exists-and-is-dir, no destination conflict, no path overlap, cross-repo git
guard via `git rev-parse --show-toplevel`, warn-only on zero `.ipynb` files),
then executes `shutil.move` to `archive/`, `_scaffold_subproject(adopted=True)`,
manifest write with `status='migrate'`, `write_adoption_notes`, and idempotent
`.gitignore` append. Also updated `create_cmd` to pass `adopted=False` explicitly.
Added `tests/cli/test_subproject_adopt.py` (35 tests, all passing) and patched
`tests/cli/test_subproject.py` to handle the new `migrate` state in
`_seed_artifacts_for`.

## files_touched
- `src/kbutillib/cli/subproject.py` — state machine additions, scaffold
  refactor, adopt command, gitignore helper, git toplevel helper
- `tests/cli/test_subproject.py` — patched `_seed_artifacts_for` to seed
  `RESEARCH_PLAN.md` for `migrate` state so `test_all_forward_transitions` passes
- `tests/cli/test_subproject_adopt.py` — new file (35 tests, added with `git add -f`
  per repo convention for test files excluded by `.gitignore`)

## success_criteria_check

| AC | Description | Result |
|---|---|---|
| #9 | `_STATES` includes `"migrate"` immediately after `"plan"` | PASS |
| #10 | `_FORWARD["migrate"] == "p-review"`; no `_REVERSE` entry | PASS |
| #11 | `_NEXT_ACTION["migrate"] == "Migrate"` | PASS |
| #12 | `_check_forward_preconditions` for `migrate` returns `None` / `"missing-artifact"` | PASS |
| #13 | `_scaffold_subproject(adopted=False)` creates exactly 6 canonical dirs; `adopted=True` adds `archive/` | PASS |
| #14 | `_scaffold_subproject` renders `notebooks/util.py` from Jinja template | PASS |
| #15 | `_scaffold_subproject` does not create `references.md` | PASS |
| #16 | `kbu subproject create` writes `status="plan"` | PASS |
| #17 | `adopt` exits 1 when not in bootstrapped project | PASS |
| #18 | `adopt` exits 1 when path doesn't exist or isn't a directory | PASS |
| #19 | `adopt` exits 1 when `subprojects/<name>/` already exists | PASS |
| #20 | `adopt` exits 1 when path is inside destination | PASS |
| #21 | `adopt` exits 1 when path is in a different git repo | PASS |
| #22 | `adopt` succeeds when path is in the same git repo | PASS |
| #23 | `adopt` succeeds (with warning) when path is not in any git repo | PASS |
| #24 | `adopt` warns but proceeds when source has zero `.ipynb` files | PASS |
| #25 | `archive/` contains moved content; source no longer exists | PASS |
| #26 | Canonical subdirs created; `notebooks/util.py` rendered from template | PASS |
| #27 | Manifest has `status="migrate"`, empty `notebooks: []` | PASS |
| #28 | `.adoption-notes.md` written with required sections | PASS |
| #29 | Subproject gitignore lines appended to root `.gitignore` (idempotent) | PASS |
| #30 | Manifest `notebooks: []` is empty at adopt time | PASS |
| #31 | `adopt` does NOT write `.gitignore` entries for oversize files | PASS |
| AC #54 | Existing tests continue to pass | PASS — 43 existing tests + 35 new = 78 total |

## tests_run

```
PYTHONPATH=/Users/chenry/.maestro/worktrees/p2-subproject-cli/src \
  python -m pytest tests/cli/test_subproject.py tests/cli/test_subproject_adopt.py -v
```

Result: **78 passed** in 1.06s (0 failed, 0 skipped).

- `tests/cli/test_subproject.py`: 43 tests, all pass (unchanged tests continue to work)
- `tests/cli/test_subproject_adopt.py`: 35 tests, all pass (new AC #9–#31 coverage)

## caveats

1. **Dependency modules not on origin/main**: `kbutillib.layout` and
   `kbutillib.cli.adopt._inventory` are on branches `task/p1-layout-module` and
   `task/p1-inventory-scanner` respectively. Both are present on local `main` in
   the Dropbox repo (merged there via prior Maestro tasks), which is what the
   worktree branches from. The reviewer/merger should ensure these branches are
   merged before merging this task branch.

2. **Gitignore marker strategy**: The `_append_subproject_gitignore` function uses
   a per-subproject marker (`# >>> kbu-subproject:<name> >>>`) rather than the
   global `# >>> kbu-managed >>>` block used by bootstrap. This is intentional:
   each subproject gets its own idempotent block so multiple adopt calls don't
   create duplicate entries.

3. **`shutil.move` behavior**: `shutil.move(str(source_path), str(archive_dir))`
   moves the entire source directory tree to `archive/`. The result is
   `subprojects/<name>/archive/<source_dirname>/...` with internal structure
   preserved. This matches AC #25 — archive contains "moved content with internal
   structure preserved".

4. **Jinja2 dependency**: The scaffold now imports from `jinja2`. This is a declared
   dependency in `pyproject.toml` (`jinja2 >=3.0`) so no new dependency is added.

5. **Test file tracked with `git add -f`**: Per repo convention (`.gitignore` has
   `test_*.py`), `tests/cli/test_subproject_adopt.py` was added with `git add -f`.
