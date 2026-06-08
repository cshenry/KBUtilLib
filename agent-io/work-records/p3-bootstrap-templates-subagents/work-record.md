# Work Record: p3-bootstrap-templates-subagents

## task_id
p3-bootstrap-templates-subagents

## branch
task/p3-bootstrap-templates-subagents

## commit_shas
(populated after commit)

## summary

This task updated the KBUtilLib templates, bootstrap CLI, new-project CLI, and tests to implement the v2 PRD's shared-dir and subagent layout changes. Three slash-commands (`kbu-literature-review`, `kbu-review`, `kbu-diagnose`) were moved from `.claude/commands/` to `.claude/agents/` as `kbu-sub-*` subagents with `type: agent` frontmatter added. The `kbu-sub-literature-review` subagent had its output paths updated to `subprojects/<name>/literature/<topic-slug>.md` + `literature/index.md` per the PRD §"/kbu-plan 4-step flow". Root shared dirs (`data/`, `models/`, `genomes/`) with `.gitkeep` were added to the template. `kbu-project.toml.template` gained a `[layout]\nshared_dirs` block. `bootstrap.py` and `new_project.py` were updated to create shared dirs, emit `[layout]` in the manifest, and deploy/track `.claude/agents/` files alongside `.claude/commands/`. `kbu-migrate.md` was added to the bootstrap command list but uses the existing "skip missing source" behaviour (Option 1).

## files_touched

- `templates/research-project/.claude/agents/kbu-sub-literature-review.md` (new)
- `templates/research-project/.claude/agents/kbu-sub-review.md` (new)
- `templates/research-project/.claude/agents/kbu-sub-diagnose.md` (new)
- `templates/research-project/.claude/commands/kbu-literature-review.md` (deleted)
- `templates/research-project/.claude/commands/kbu-review.md` (deleted)
- `templates/research-project/.claude/commands/kbu-diagnose.md` (deleted)
- `templates/research-project/.gitignore` (updated: added 9 root_gitignore_lines patterns)
- `templates/research-project/kbu-project.toml.template` (updated: added [layout] block)
- `templates/research-project/data/.gitkeep` (new)
- `templates/research-project/models/.gitkeep` (new)
- `templates/research-project/genomes/.gitkeep` (new)
- `src/kbutillib/cli/bootstrap.py` (updated: _CLAUDE_COMMAND_FILES, _CLAUDE_AGENT_FILES, agents loop, shared-dir creation, [layout] in manifest)
- `src/kbutillib/cli/new_project.py` (updated: _TRACKED_DIRS, DEFAULT_SHARED_DIRS import, [layout] in manifest)
- `tests/cli/test_bootstrap.py` (updated: counts, _make_stub_template, AC 42-47 tests)
- `tests/cli/test_new_project.py` (updated: AC 42-43 tests, agents tracking test)
- `agent-io/work-records/p3-bootstrap-templates-subagents/work-record.md` (this file)

## success_criteria_check

- **AC #42** (`kbu new-project` and `kbu bootstrap` scaffold root `data/`, `models/`, `genomes/` with `.gitkeep`): PASS — templates have the dirs; bootstrap creates them programmatically; new-project copies them via copy_template_tree. Tests verify both.
- **AC #43** (`kbu-project.toml` has `[layout.shared_dirs] = ["data","models","genomes"]`): PASS — both bootstrap and new-project write `layout.shared_dirs` into the manifest. Tests verify TOML output.
- **AC #44** (subagent sources have `type: agent` in frontmatter): PASS — all three agent files have `type: agent` in YAML frontmatter. Test verifies post-bootstrap presence.
- **AC #45** (subagent sources at `templates/research-project/.claude/agents/<name>.md`): PASS — files created at correct path.
- **AC #46** (no claude-skills code change required): PASS (out of scope; not modified).
- **AC #47** (`kbu-literature-review.md` → `kbu-sub-literature-review.md` with `type: agent`; same for review and diagnose; old files removed): PASS — old command files deleted, new agent files present with correct names and frontmatter.
- **Existing bootstrap tests (107)**: PASS — all continue to pass after AC9 count updates.
- **Existing new-project tests (15)**: PASS — unchanged behaviour, new tests added.

## tests_run

```
python3 -m pytest tests/cli/test_bootstrap.py -x -q
  → 107 passed

python3 -m pytest tests/cli/test_new_project.py -x -q
  → 15 passed (pre-update)
  → 25 passed (post-update with new AC42-45 tests)

python3 -m pytest tests/cli/test_bootstrap.py tests/cli/test_new_project.py -x -q
  → 133 passed

python3 -m pytest tests/test_layout.py tests/test_adopt_inventory.py -x -q
  → 79 passed

python3 -m pytest tests/cli/test_update.py tests/cli/test_update_bootstrap_aware.py -x -q
  → 47 passed
```

Total: 133 + 79 + 47 = 259 tests run, 0 failures.

## caveats

1. **kbu-migrate.md — Option 1 chosen.** Bootstrap already had `if not src.exists(): continue` for command files (line 431-432 in original). The new code uses the same pattern for both commands and agents. When p4 lands `kbu-migrate.md` in the template, bootstrap will automatically copy it without any code change needed.

2. **kbu-diagnose.md had no YAML frontmatter** in the original. The converted `kbu-sub-diagnose.md` gains standard frontmatter (`name:`, `type: agent`, `description:`, `allowed-tools:`) consistent with the other two agent files. The h1 title is preserved as `kbu-sub-diagnose` to match the new slug.

3. **bootstrap manifest now includes `[layout]` table.** The `write_project_manifest` function (TOML writer) must handle nested dict keys — confirmed it does (existing tests cover this pattern with `[project.authors]` etc.).

4. **_TRACKED_DIRS in new_project.py** now includes `.claude/agents` so agents are hashed in `[update.file_hashes]` for the `kbu update` flow — consistent with the treatment of `.claude/commands`.

5. **Template .gitignore patterns** are baked in as literal strings matching `root_gitignore_lines(["data","models","genomes"])` output, with a comment noting the source function. This matches the "bake them in" approach (option i from the task prompt) since template files are static.
