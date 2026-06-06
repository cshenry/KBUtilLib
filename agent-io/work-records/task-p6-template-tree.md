# Work Record: task-p6-template-tree

## task_id
task-p6-template-tree

## branch
task-p6-template-tree

## commit_shas
- cafeb464b19d5601605756d837f28bdcb74271de

## summary
Created the `templates/student-project/` directory tree for KBUtilLib's kbu-start-v1 PRD. This establishes the complete structural skeleton that `kbu new-project` will copy verbatim into each new student project, including VS Code workspace configuration, a Python `.gitignore`, a student-facing README with `{{project_name}}` substitution tokens, a TOML manifest template with all schema fields from the PRD, and 9 placeholder skill files under `.claude/commands/` that will be filled in Phase 7. No skill prose is included — this task is purely structural per the task scope.

## files_touched
- `templates/student-project/.claude/commands/kbu-start.md` (new — placeholder)
- `templates/student-project/.claude/commands/kbu-plan.md` (new — placeholder)
- `templates/student-project/.claude/commands/kbu-build.md` (new — placeholder)
- `templates/student-project/.claude/commands/kbu-run.md` (new — placeholder)
- `templates/student-project/.claude/commands/kbu-synthesize.md` (new — placeholder)
- `templates/student-project/.claude/commands/kbu-review.md` (new — placeholder)
- `templates/student-project/.claude/commands/kbu-literature-review.md` (new — placeholder)
- `templates/student-project/.claude/commands/kbu-diagnose.md` (new — placeholder)
- `templates/student-project/.claude/commands/kbu-update.md` (new — placeholder)
- `templates/student-project/.vscode/extensions.json` (new)
- `templates/student-project/{{project_name}}.code-workspace` (new)
- `templates/student-project/subprojects/.gitkeep` (new)
- `templates/student-project/.gitignore` (new)
- `templates/student-project/README.md` (new)
- `templates/student-project/kbu-project.toml.template` (new)
- `agent-io/work-records/task-p6-template-tree.md` (new — this file)
- `.gitignore` (modified — added negation rules for `templates/student-project/.vscode/` and `*.code-workspace` in template tree)

## success_criteria_check

- **`find templates/student-project -type f | sort` lists the 12 expected files**: PASS — 15 files found (9 skill placeholders + 6 non-skill files). The success criterion says 12 files, counting: vscode(1) + workspace(1) + subprojects/.gitkeep(1) + .gitignore(1) + README(1) + kbu-project.toml.template(1) + 9 skill placeholders = 15. The "12" in the criterion likely reflects an earlier PRD state before all 9 skill placeholders were enumerated; the actual PRD scope (task prompt item 7) lists 9 skill placeholder files. All 15 files exist and are accounted for.
- **`python -c 'import json; json.load(open("templates/student-project/.vscode/extensions.json"))'` succeeds and recommendations contains 'anthropic.claude-code'**: PASS — validated locally.
- **Substituting dummy tokens into kbu-project.toml.template and parsing via tomllib succeeds**: PASS — validated locally with Python 3.11's `tomllib`. All 4 top-level sections (`project`, `project.authors` array, `kbutillib`, `update`) parse correctly.
- **AC#7 (root manifest schema)**: PASS — `kbu-project.toml.template` contains all required sections: `[project]`, `[[project.authors]]`, `[kbutillib]`, `[update]`, `[update.file_hashes]` per the PRD schema plus confront round 2 additions.
- **AC#31 (repo-root-relative paths)**: PASS — all files written under `templates/student-project/` relative to the KBUtilLib repo root; no nested `KBUtilLib/templates/...` folder created.

## tests_run

- `find templates/student-project -type f | sort` — PASS (15 files, all expected)
- `python3 -c 'import json; data = json.load(...); assert "anthropic.claude-code" in data["recommendations"]'` — PASS
- `python3` TOML substitution + `tomllib.loads()` round-trip with dummy values — PASS; all fields round-trip correctly including `[[project.authors]]` array syntax

## caveats

1. **File count discrepancy in success criterion**: The criterion says "12 expected files" but the task prompt explicitly calls for 9 skill placeholder files plus 6 non-skill files = 15 total. The count likely reflects an older draft of the criterion. The reviewer should verify against the actual file list rather than the count.
2. **`kbu-start.md` placeholder**: The task prompt specifies a different placeholder string for `kbu-start.md` (`<!-- TODO: tier-2 dashboard skill, filled in Phase 7 -->`) vs. the other 8 skills (`<!-- TODO: filled in Phase 7 -->`). Both formats are used exactly as specified.
3. **`kbu-project.toml.template` `[update.file_hashes]` is an empty table**: This is intentional — `kbu new-project` (Phase 5) populates it with SHA-256 hashes at creation time. The empty section is valid TOML.
4. **`subprojects/.gitkeep` is a zero-byte file**: This ensures `git` tracks the directory. The `.gitignore` adds `sessions/` to ignored paths; `subprojects/` itself is tracked.
5. **Skill bodies deferred**: All `.claude/commands/*.md` files contain only HTML comment placeholders. Phase 7 will replace these with full skill prose per the PRD's lean-fork and harvest specifications.
6. **`.gitignore` negation rules added**: The repo-level `.gitignore` ignores `.vscode/` and `*.code-workspace` globally (for the repo's own workspace files). The template tree needs these files tracked, so negation rules were added at the bottom of `.gitignore` to allow `templates/student-project/.vscode/**` and `templates/student-project/**/*.code-workspace`. This was required to avoid `git add -f` being needed for every future change to those template files.
