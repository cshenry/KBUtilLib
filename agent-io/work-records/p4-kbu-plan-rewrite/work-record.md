# Work Record: p4-kbu-plan-rewrite

## task_id
p4-kbu-plan-rewrite

## branch
task/p4-kbu-plan-rewrite

## commit_shas
- (populated after commit)

## summary

Rewrote `templates/research-project/.claude/commands/kbu-plan.md` from the original
single-pass grill-then-write flow to the 4-step grilled flow specified in PRD
`kbutillib-v2` (sections "/kbu-plan 4-step flow" and User Stories #1-#4, #16). Step 1
grills goals; Step 2 invokes `kbu-sub-literature-review` via `Agent(subagent_type=...)`
so paper text never pollutes the main thread; Step 3 grills the detailed plan
dependency tree then writes `RESEARCH_PLAN.md`; Step 4 populates manifest
`[[notebooks]]` entries and renders `TASKS.md` as a human-readable flat list. Phase 5
advances state and saves the session. Provenance frontmatter `last_reviewed` updated to
2026-06-08.

## files_touched

- `templates/research-project/.claude/commands/kbu-plan.md` (rewritten)
- `agent-io/work-records/p4-kbu-plan-rewrite/work-record.md` (this file)

## success_criteria_check

- **AC #48** (`/kbu-plan` rewritten to 4-step grilled flow producing `RESEARCH_PLAN.md`,
  `literature/index.md` + `literature/<topic-slug>.md`, populated manifest `[[notebooks]]`,
  and `TASKS.md`): PASS — all four artifacts are documented and produced by their
  respective steps. Manifest `[[notebooks]]` entries are written in Step 4 before
  `TASKS.md` is rendered from them.

- **AC #50** (`/kbu-plan` Step 2 invokes `kbu-sub-literature-review` via the Agent tool,
  not as a slash command): PASS — Step 2 explicitly uses
  `Agent(subagent_type="kbu-sub-literature-review", prompt=...)` with the subproject
  path, topic list, and depth tier passed in the prompt. No slash-command invocation
  of `kbu-sub-literature-review` appears anywhere in the file.

- **User Story #1** (grill goals, literature subagent in own context, grill plan, notebook
  decompose — mechanical plan-to-build handoff): PASS — the 4-step structure implements
  exactly this sequence. Manifest is populated before `TASKS.md` so `/kbu-build` consumes
  it as source of truth.

- **User Story #2** (literature subagent produces `literature/<topic-slug>.md` per topic
  plus `literature/index.md`): PASS — Step 2 Agent invocation prompt explicitly requests
  both file types and states their paths.

- **User Story #3** (plan structure grilled before file is written): PASS — Step 3 grills
  all branches of the dependency tree, presents the proposed structure for user
  confirmation, and only writes `RESEARCH_PLAN.md` after the user confirms.

- **User Story #4** (manifest `notebooks: [...]` populated, parallel `TASKS.md` for human
  reading, manifest is source of truth): PASS — Step 4 writes manifest first, then
  renders `TASKS.md` from it with an explicit note that the manifest wins on divergence.

- **User Story #16** (literature review runs in its own context window): PASS — subagent
  invocation via Agent tool runs in a separate context; paper text and search output never
  appear in the main conversation.

- **Provenance frontmatter preserved**: PASS — `type: lean-fork`, `source_repo`,
  `source_commit`, `source_path` all preserved; `last_reviewed` updated to 2026-06-08.

## tests_run

This task is a pure markdown rewrite. The PRD explicitly notes: "Skills/agents
(markdown) are not auto-tested." A Python smoke check was run instead:

```
python3 -c "smoke check: provenance fields, Step 1-4 headings, Agent invocation,
  four artifact refs, advance + session save, no deprecated slash-command refs"
  → All smoke checks passed. File length: 266 lines.
```

No unit tests apply to this file.

## caveats

1. **kbu-plan does not write the manifest from scratch** — it appends `[[notebooks]]`
   entries to the existing `kbu-subproject.toml`. The skill instructs reading the
   existing file and writing it back, which is the correct pattern since other fields
   (`[subproject]`, `[artifacts]`, etc.) must be preserved. A future improvement could
   use a `kbu subproject notebook add` CLI command, but that doesn't exist yet.

2. **Step 2 depth-tier selection** — the skill defaults to `Standard review` for
   subproject work, consistent with what `kbu-sub-literature-review` defaults to when
   invoked from `/kbu-plan`. The researcher can request `Deep review` explicitly.

3. **TASKS.md "Next Step" section** points to `/kbu-build`. This is appropriate
   since the `p-review` state (after `kbu subproject advance`) is reviewed before
   build starts. The skill correctly notes the state advances to `p-review`, and
   `/kbu-build` is the post-review step.
