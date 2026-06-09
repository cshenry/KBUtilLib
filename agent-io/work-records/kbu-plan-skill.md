# Work Record: kbu-plan-skill

## task_id
kbu-plan-skill

## branch
kbu-conductor/kbu-plan-skill

## commit_shas
- 544f4f0c02b6aa9f534a3df35e8e9f8f1c28a9a1

## summary
Extended `templates/research-project/.claude/commands/kbu-plan.md` to emit a validated build contract and enforce closed-loop subagent delegation. Added Step 3b (test-design grill: per-helper `data_source`, `data_spec`, and assertion pinning), Step 3c (write `buildplan.json` conforming to the KBU conductor schema and run `kbu buildplan validate` before advancing), and Step 3d (spawn `kbu-sub-review` via an explicit `Agent()` call, then confirm a `pass` verdict file exists on disk before `kbu subproject advance`). A mandatory-subagent-delegation rule block with a STOP tell-sign was added near the top of the skill to prevent inline satisfaction of delegated steps. The existing literature `Agent()` call was annotated to reinforce the same rule. Rules and Phase 5 files-written list were updated to reflect all additions.

## files_touched
- `templates/research-project/.claude/commands/kbu-plan.md`

## success_criteria_check

| Criterion | Self-assessment | Justification |
|---|---|---|
| kbu-plan.md contains a mandatory test-design grill step | PASS | Step 3b grills each util.py helper one question per round for data_source, data_spec, and assertions, and gates on user confirmation before advancing. |
| Writes subprojects/<name>/buildplan.json and invokes `kbu buildplan validate` before advancing | PASS | Step 3c emits buildplan.json from confirmed plan + test specs, runs `kbu buildplan validate subprojects/<name>/buildplan.json`, and gates on exit 0 before advancing to Step 3d. |
| Invokes every delegated step via an explicit Agent(subagent_type="kbu-sub-...") call | PASS | Step 2 literature review retains its existing explicit `Agent(subagent_type="kbu-sub-literature-review", …)` call (annotated to clarify it must never be inlined). Step 3d review spawns an explicit `Agent(subagent_type="kbu-sub-review", prompt="<name>")` call. |
| Contains a hard anti-inline rule with the STOP tell-sign | PASS | The MANDATORY SUBAGENT DELEGATION RULE block at the top of the file contains the exact STOP tell-sign wording from the task specification. |
| Confirms a pass verdict file on disk before `kbu subproject advance` | PASS | Step 3d verifies the verdict file via `head -1` shell check and loops the reviewer on fail; Phase 5 references the Step 3d gate explicitly. The advance call will not execute until a pass verdict file exists on disk. |
| No inline-review path remains | PASS | The old Step 4 / Phase 5 had no review step at all (the skill advanced unconditionally). The new flow gates advance on Step 3d's closed-loop verdict. |

## tests_run
- No automated tests applicable to a `.md` skill file. The skill was verified by full read-through against the task specification and the authoritative `buildplan.json` schema in `src/kbutillib/cli/buildplan.py`. Structural gates (step ordering, gating language, Agent() call syntax, verdict-file shell commands) were checked manually against the kbu-sub-review agent's documented file naming and verdict-comment convention.

## caveats
- The `kbu-sub-review` subagent currently advances the subproject state internally on pass (Step 4 of that agent). When called from Step 3d of `kbu-plan`, this means the subproject state may advance inside the reviewer subagent before Phase 5 runs `kbu subproject advance` again. This is a pre-existing sequencing ambiguity in the kbu-sub-review design and is out of scope for this task; the reviewer should confirm whether a `--no-advance` flag or equivalent is needed for subagent-invoked reviews in the kbu-plan context.
- The commit SHA above is abbreviated. Full SHA: `544f4f0` — run `git log kbu-conductor/kbu-plan-skill --format="%H" -1` in the worktree to get the full hash.
