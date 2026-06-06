# Work Record: task-p7-tier2-lean-forks

## task_id
task-p7-tier2-lean-forks

## branch
task-p7-tier2-lean-forks

## commit_shas
- 12a1259a6b62ab9634e55a66f426bfd2c770fdf2

## summary
Replaced three Phase-6 placeholder files under `templates/student-project/.claude/commands/` with full lean-fork skill implementations. `kbu-plan.md` forks `ai-design.md`'s grill methodology, stripping all AIAssistant state machinery and directing output to `RESEARCH_PLAN.md`. `kbu-build.md` forks `ai-conductor.md`'s in-context decompose-and-implement pattern, replacing the sub-agent fan-out with direct scaffold of notebooks and `util.py`. `kbu-diagnose.md` forks `diagnose.md` unchanged in methodology (reproduce → minimise → hypothesise → instrument → fix), with platform refs removed and `kbu session save` as the only end-of-skill call.

## files_touched
- `templates/student-project/.claude/commands/kbu-plan.md` (replaced placeholder, 143 lines)
- `templates/student-project/.claude/commands/kbu-build.md` (replaced placeholder, 158 lines)
- `templates/student-project/.claude/commands/kbu-diagnose.md` (replaced placeholder, 141 lines)
- `agent-io/work-records/task-p7-tier2-lean-forks.md` (this file)

## success_criteria_check

1. **Three files exist** — PASS. All three files written at the required paths.
2. **Each contains a `kbu skill provenance` header with `type: lean-fork` and the 40-char source_commit** — PASS. Verified with `grep type:` and `grep source_commit:`.
3. **`grep -lE 'register_prd|set_prd_status|maestro submit|agentforge'` returns 0 files** — PASS. Verified by individual `grep -l` checks; all returned exit 1 (no matches). One false-positive was caught and corrected (the word "agentforge" appeared in a "do not use" rule in kbu-diagnose.md and was rephrased).
4. **Each file ≤ 300 lines** — PASS. kbu-plan: 143, kbu-build: 158, kbu-diagnose: 141.

## tests_run
- Line count check: `wc -l` on all three files — pass (all ≤ 300)
- Forbidden string check: `grep -l` for each of `register_prd`, `set_prd_status`, `maestro submit`, `agentforge` — pass (all exit 1)
- Provenance header check: `grep source_commit`, `grep "type:"` — pass
- No automated test suite applicable for markdown skill files.

## caveats
- The `kbu-diagnose.md` Rules section originally contained the word "agentforge" in a "do not reference" prohibition. This would have caused the success-criterion grep to match. Reworded to say "external orchestration systems" instead.
- `kbu session save` and `kbu subproject advance` are referenced as CLI calls throughout. These are assumed to be implemented by parallel Phase-7 tasks (p7-tier1-kbu-start and p7-tier2-net-new); the skill files are inert stubs if those CLI commands do not yet exist.
- The `kbu-build.md` notebook scaffold writes valid Jupyter JSON directly. This is complex enough that students may want a follow-up task that replaces the manual JSON construction with `nbformat` library calls — not a blocker for this task.
