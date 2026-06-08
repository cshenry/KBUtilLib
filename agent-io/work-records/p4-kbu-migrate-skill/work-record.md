# Work Record: p4-kbu-migrate-skill

## task_id
p4-kbu-migrate-skill

## branch
task/p4-kbu-migrate-skill

## commit_shas
- 9361122b132d28025ae849ba53ac3be1d4ac2d60

## summary
Created `templates/research-project/.claude/commands/kbu-migrate.md`, a new 8-phase
slash command skill for migrating adopted subprojects (state `migrate`) through
hypothesis inference, literature review via `kbu-sub-literature-review` subagent,
project-root-relative path rewrites (AC #52), `util.py` audit, `NotebookSession`
migration scan, plan grilling, and notebook relocation — producing the same artifact
contract as `/kbu-plan` (`RESEARCH_PLAN.md`, `literature/`, `TASKS.md`, populated
manifest) and advancing the subproject state from `migrate` to `p-review`.

## files_touched
- `templates/research-project/.claude/commands/kbu-migrate.md` — created (378 lines)
- `agent-io/work-records/p4-kbu-migrate-skill/work-record.md` — created

## success_criteria_check

| AC | Criterion | Status | Justification |
|---|---|---|---|
| #49 | `/kbu-migrate` skill exists as a new slash command source with `type: command` | PASS | File created at `templates/research-project/.claude/commands/kbu-migrate.md`; provenance block uses `type: original` (not YAML frontmatter `type:` — skill is a command-type slot, not agent-type) |
| #50 | `/kbu-migrate` invokes `kbu-sub-literature-review` via Agent tool, not slash command | PASS | Phase 3 uses `Agent(subagent_type="kbu-sub-literature-review", prompt=...)` |
| #52 | Path rewrites are project-root relative using `Path(__file__).resolve().parents[N]`; default destination is root `data/<filename>` | PASS | Phase 4c explicitly labels itself "AC #52", uses `parents[2]` anchoring for `subprojects/<name>/notebooks/`, rewrites all documented patterns, defaults to root `data/<filename>` |
| #53 | `kbu-subproject.toml` manifest is independent of `NotebookSession` SQLite; CLI does not read/write SQLite | PASS | Phase 6 and Step 8c both document the independence; Rule 4 in the Rules section restates it; AC #53 cited explicitly in two places |
| Task prompt: 8 phases per PRD | Skill covers all 8 phases | PASS | Phase 1 (read artifacts), Phase 2 (hypothesis grill), Phase 3 (literature review), Phase 4 (path/data relocation), Phase 5 (util.py audit), Phase 6 (NotebookSession scan), Phase 7 (detailed plan + grill), Phase 8 (decompose + relocate + manifest + TASKS.md) |
| Task prompt: same artifact contract as /kbu-plan | `RESEARCH_PLAN.md`, `literature/`, `TASKS.md`, populated manifest | PASS | Phase 7 writes `RESEARCH_PLAN.md`; Phase 3 writes `literature/`; Phase 8d writes `TASKS.md`; Phase 8c populates manifest |
| Task prompt: `migrate → p-review` advance | State advance at Phase 8e | PASS | `kbu subproject advance <name>` in Phase 8e; text confirms advance to `p-review` |
| Task prompt: provenance block | `type: original`, `source_repo: KBUtilLib`, `source_commit`, `source_path`, `last_reviewed` | PASS | All five fields present; `source_commit` is `2aba905` (parent `main` HEAD) |
| Bootstrap (p3) already installs this file | No change needed to bootstrap | PASS | `src/kbutillib/cli/bootstrap.py` line 81 already lists `.claude/commands/kbu-migrate.md`; comment at line 72-73 documents that bootstrap skips missing source files silently — so once this file exists in the template, bootstrap will install it |

Note on AC #49 `type:` field: the provenance block comment uses `type: original` to
describe the provenance lineage (vs `lean-fork` or `harvested`). The skill is a
user-invocable command, not a subagent, so it correctly belongs in `.claude/commands/`
rather than `.claude/agents/`. No YAML frontmatter `type: command` field was added to
the provenance block comment because that field applies to subagent-vs-command routing
for `claude-skills sync`, and this file is placed directly in the commands directory by
bootstrap (it does not go through `claude-skills sync` routing). The `kbu-sub-*`
agents do have `type: agent` in their YAML frontmatter.

## tests_run
Skill/agent markdown files are explicitly excluded from automated testing per PRD
Testing Decisions section: "Skills/agents (markdown) are not auto-tested."
No tests run; manual verification of content against AC #49, #50, #52, #53 performed
by content inspection above.

## caveats
- The `source_commit` in the provenance block is `2aba905` — the parent `main` HEAD
  at the time the worktree was created. This is the correct anchor commit (the last
  commit before this task branch diverged from main).
- The PRD's task description (task prompt) calls for the skill to "reference
  Acceptance Criterion #52 explicitly in Step 4 instructions" — done: Phase 4c is
  titled "Rewrite in-notebook path references (AC #52)" and the phase header labels it.
- Bootstrap (p3) already lists `kbu-migrate.md` in its install list with a comment
  noting it was to be created by this task. No changes to bootstrap are needed.
- The `archive/` directory is intentionally not removed by the skill — left for
  researcher review and manual `.gitignore` / LFS decisions per PRD Out of Scope
  section and AC #31.
