# Work Record: task-p7-tier2-net-new

## task_id
task-p7-tier2-net-new

## branch
task-p7-tier2-net-new

## commit_shas
- (populated after commit)

## summary
Wrote the three net-new Phase-7 skill files under `templates/student-project/.claude/commands/`: `kbu-start.md` (tier-2 status-aware navigation dashboard), `kbu-run.md` (deliberate notebook runner), and `kbu-update.md` (template update wrapper). All three replace Phase-6 placeholder stubs with full skill prose, carry the canonical `type: net-new` provenance header, stay under 300 lines, and contain no references to AIAssistant runtime, Maestro, or AgentForge concepts.

## files_touched
- `templates/student-project/.claude/commands/kbu-start.md` (replaced placeholder — 157 lines)
- `templates/student-project/.claude/commands/kbu-run.md` (replaced placeholder — 117 lines)
- `templates/student-project/.claude/commands/kbu-update.md` (replaced placeholder — 87 lines)
- `agent-io/work-records/task-p7-tier2-net-new.md` (this file)

## success_criteria_check

- **Three files exist**: PASS — all three files present at the required paths.
- **Each contains `kbu skill provenance` header with `type: net-new`**: PASS — all three have the canonical provenance block with `type: net-new` and `created: 2026-06-05`.
- **`grep -c 'wrong-state\|missing-artifact\|no-subprojects\|notebooks-stale\|review-pending' kbu-start.md` >= 5**: PASS — returns 16 (all five reason strings appear multiple times in prose, table, and inline examples).
- **`grep -c 'kbu subproject list' kbu-start.md` >= 1**: PASS — returns 3.
- **`grep -c 'kbu notebook list' kbu-run.md` >= 1**: PASS — returns 2.
- **`grep -c 'kbu update' kbu-update.md` >= 2**: PASS — returns 9.
- **`grep -lE 'register_prd|set_prd_status|maestro submit|agentforge|assistant.state' ...` returns 0 files**: PASS — grep exits 1 (no matches) on all three files.
- **Each file <= 300 lines**: PASS — kbu-start.md: 157, kbu-run.md: 117, kbu-update.md: 87.

## tests_run

```
grep -c 'wrong-state\|missing-artifact\|no-subprojects\|notebooks-stale\|review-pending' \
  templates/student-project/.claude/commands/kbu-start.md
# → 16  PASS (>= 5 required)

grep -c 'kbu subproject list' templates/student-project/.claude/commands/kbu-start.md
# → 3  PASS

grep -c 'kbu notebook list' templates/student-project/.claude/commands/kbu-run.md
# → 2  PASS

grep -c 'kbu update' templates/student-project/.claude/commands/kbu-update.md
# → 9  PASS

grep -lE 'register_prd|set_prd_status|maestro submit|agentforge|assistant\.state' \
  templates/student-project/.claude/commands/kbu-start.md \
  templates/student-project/.claude/commands/kbu-run.md \
  templates/student-project/.claude/commands/kbu-update.md
# → exit 1 (no files matched)  PASS

wc -l templates/student-project/.claude/commands/{kbu-start,kbu-run,kbu-update}.md
# → 157 / 117 / 87  PASS (all < 300)
```

No unit tests run (these are skill prose files, not code). The relevant tests are the grep checks above.

## caveats

1. **`kbu-start.md` is the tier-2 template copy** — the p7-tier1-kbu-start developer is writing the repo-root `KBUtilLib/.claude/commands/kbu-start.md` (or `agent-io/skills/kbu-start.md`) for use when running the skill from the KBUtilLib repo itself. This file is the per-project copy that students get after `kbu new-project`.
2. **Session save subproject for kbu-update** — `kbu update` is project-wide, not subproject-scoped. The skill uses `project` as a conventional subproject name for the session record. If `kbu session save` requires an existing subproject manifest for `project`, the warning is non-fatal (session is still saved to local YAML). This is consistent with how the session CLI handles missing manifests.
3. **kbu-run notebook exec narration** — the skill instructs Claude to read the `.ipynb` JSON after exec to narrate cell outputs. This relies on Claude reading the notebook file directly; it is not a CLI feature. This is intentional and avoids adding a new CLI subcommand for output narration.
4. **No `kbu notebook exec` output streaming** — `kbu notebook exec` is synchronous and returns only after full execution. For long-running notebooks the student will see no progress until completion. The skill notes this implicitly ("While the notebook is executing, narrate progress") which Claude can satisfy by narrating that execution is in progress.
