# Work Record: kbu-build-skill

**task_id**: kbu-build-skill
**branch**: kbu-conductor/kbu-build-skill

## commit_shas

- (to be filled after commit)

## summary

Rewrote `templates/research-project/.claude/commands/kbu-build.md` from a
stub-scaffolding conductor into a proper parallel-dispatch conductor mirroring
`/ai-conductor`. The old skill wrote `NotImplementedError` stubs and "scientific
logic belongs to the researcher" language; the new skill fans out work to
`kbu-sub-build` developer subagents per notebook, gates on `kbu-sub-review`
verdict files, runs a `kbu-sub-diagnose` + retry loop (bounded to 2 retries),
assembles notebooks as thin orchestration layers calling verified helpers, and
advances the state machine only after `grep` confirms a `REVIEW_build_*.md`
pass verdict on disk. Applied the same mandatory-subagent-delegation rule and
STOP tell-sign to `kbu-migrate.md`, which already used an explicit `Agent()`
for literature review and now carries the rule block prominently and a first
rule explicitly covering future review/diagnose delegation.

## files_touched

- `templates/research-project/.claude/commands/kbu-build.md` — complete rewrite
- `templates/research-project/.claude/commands/kbu-migrate.md` — added MANDATORY SUBAGENT DELEGATION RULE header block, STOP tell-sign, updated provenance date, added Rule 1 (delegated steps run in explicit subagents), renumbered existing rules

## success_criteria_check

| Criterion | Status | Justification |
|---|---|---|
| kbu-build.md loads buildplan.json and runs `kbu buildplan validate` | PASS | Phase 1 runs `kbu buildplan validate <path>` and refuses to proceed on failure |
| Fans each notebook out to kbu-sub-build via explicit Agent(subagent_type="kbu-sub-build") calls in depends_on order | PASS | Phase 2 dispatches each notebook via `Agent(subagent_type="kbu-sub-build", prompt=...)` in topological order, with independent notebooks eligible for parallel dispatch |
| Escalates `BLOCKED:` forks to the user and re-dispatches with DECISION | PASS | Phase 2 handles `BLOCKED:` by presenting the decision to the researcher verbatim and re-dispatching with `DECISION: <chosen option>` appended |
| Spawns kbu-sub-review and kbu-sub-diagnose via explicit Agent() calls | PASS | Phase 3 uses `Agent(subagent_type="kbu-sub-review", ...)` and `Agent(subagent_type="kbu-sub-diagnose", ...)` explicitly at the exact call sites |
| 2-retry bound on diagnose+rebuild loops | PASS | Phase 3 documents max 2 retries before escalating to researcher |
| Assembles notebooks without executing the full notebook | PASS | Phase 4 writes `.ipynb` files calling `util.<helper>` — never calls `kbu notebook run` |
| No "stubs only" / NotImplementedError-scaffold language | PASS | Grep confirms zero occurrences of `NotImplementedError` or "stubs only" in the new kbu-build.md |
| Confirms pass verdict file (REVIEW_build_*.md) on disk before `kbu subproject advance` | PASS | Phase 5 + Phase 6: `grep` must confirm `<!-- kbu-review:verdict: pass -->` in a `REVIEW_build_*.md` file before `kbu subproject advance` runs |
| kbu-migrate.md carries explicit-subagent-delegation rule and STOP tell-sign | PASS | Added MANDATORY SUBAGENT DELEGATION RULE header block with STOP tell-sign; added Rule 1 covering explicit Agent() delegation for review/diagnose steps |

## tests_run

- No automated tests for skill `.md` files exist in this repo. The skill files
  are documentation/prompts consumed by the Claude runtime.
- Verified by inspection: searched for banned strings (`NotImplementedError`,
  "stubs only", "scientific logic belongs"), confirmed zero hits in new
  kbu-build.md.
- Verified `REVIEW_build_*.md` glob pattern matches what `_glob_review_files`
  uses in `src/kbutillib/cli/subproject.py` line 109
  (`subproject_dir.glob(f"REVIEW_{stage}_*.md")` where `stage="build"`).
- Verified `kbu buildplan validate` CLI exists and the command path is correct
  per `src/kbutillib/cli/buildplan.py`.
- Verified subagent `name` fields match: `kbu-sub-build`, `kbu-sub-review`,
  `kbu-sub-diagnose` per the frontmatter in each `.claude/agents/*.md` file.

## caveats

- The `kbu-sub-diagnose` prompt in Phase 3 passes `review_file` as context to
  help the diagnose subagent focus on failing helpers. The diagnose subagent's
  current spec does not explicitly declare `review_file` as an input parameter,
  but the subagent reads the subproject freely and the extra context is
  non-harmful.
- The closing build review in Phase 5 spawns `kbu-sub-review` with
  `prompt="<name>"`. The reviewer auto-detects stage from `kbu subproject
  status`, which at this point will be `build` (the per-notebook reviews in
  Phase 3 happen while the subproject is still in `build` state). This is
  consistent with the reviewer's step 1 logic.
- The verify-and-extend mode (adopted branches) preserves the structure from
  the old skill. It skips Phase 4 notebook assembly for existing `.ipynb` files
  but still runs Phases 2–3 for helpers that need building.
- `kbu-migrate.md` does not add a plan-review subagent call at Phase 8e because
  the `migrate`→`p-review` state advance does not require a review file (the
  state machine checks for `RESEARCH_PLAN.md` existence only). The plan review
  happens in the subsequent `/kbu-plan` or `/kbu-build` invocation.
