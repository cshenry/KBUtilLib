# Work Record: kbu-sub-build-subagent

## task_id
kbu-sub-build-subagent

## branch
kbu-conductor/kbu-sub-build-subagent

## commit_shas
- 0afa944cb24a58fcd02f1ffcba534cdcbabb3c46

## summary
Created `templates/research-project/.claude/agents/kbu-sub-build.md`, the
in-context developer subagent for Module D of the kbu-conductor pipeline. The
subagent accepts a subproject path plus one `buildplan.json` notebook entry,
implements each helper per its `signature`/`contract` into
`subprojects/<name>/notebooks/util.py`, writes a corresponding fast pytest test
per helper into `subprojects/<name>/notebooks/test_util.py`, runs
`pytest test_util.py` (never the full notebook), and iterates until green or hits
a 5-iteration limit. It returns a structured work-record table on success or a
`BLOCKED:` signal with a labelled option list when it encounters a genuine
scientific/algorithmic fork. Frontmatter (`name`, `type`, `description`,
`allowed-tools`) matches the existing `kbu-sub-review.md`, `kbu-sub-diagnose.md`,
and `kbu-sub-literature-review.md` agents exactly.

## files_touched
- `templates/research-project/.claude/agents/kbu-sub-build.md` (created, 211 lines)
- `agent-io/work-records/kbu-sub-build-subagent.md` (this file)

## success_criteria_check

| Criterion | Status | Justification |
|-----------|--------|---------------|
| `templates/research-project/.claude/agents/kbu-sub-build.md` exists | pass | File created at that path in the worktree and committed. |
| Frontmatter matches the other `kbu-sub-*` agents (`name`, `description`, `allowed-tools`, `type` fields) | pass | Frontmatter block uses the identical four-field shape (`name: kbu-sub-build`, `type: agent`, `description: ...`, `allowed-tools: Bash, Read, Write, Edit`) with a provenance comment header, matching all three reference files. |
| Instructs subagent to write helpers into `util.py` and tests into `test_util.py` | pass | Steps 2 and 3 explicitly target `subprojects/<name>/notebooks/util.py` and `subprojects/<name>/notebooks/test_util.py`. Integration section also lists these as the write targets. |
| Runs pytest, not the full notebook | pass | Step 4 specifies `python -m pytest test_util.py -v`; Rule 1 explicitly forbids full notebook execution. |
| Returns a work-record or `BLOCKED:` signal | pass | Step 6 defines the work-record format; BLOCKED Protocol section defines the exact signal format and protocol. |
| BLOCKED uses literal token `BLOCKED:` with a labelled option list | pass | BLOCKED Protocol section specifies the `BLOCKED:` token on its own line followed by `options:\n  A) ...\n  B) ...`. |
| BLOCKED restricted to genuine algorithmic/scientific forks | pass | The protocol section explicitly lists what does NOT qualify (ordinary coding errors, missing imports, edge cases with clear answers, performance tradeoffs) and requires a scientific or algorithmic decision with materially different outcomes. |

## tests_run
None — this task created a documentation/skill file (a Markdown subagent prompt),
not executable code. There is no test suite for `.claude/agents/` files. Correctness
was verified by reading all three reference agents and matching their structure.

## caveats
- The `allowed-tools` list includes `Edit` in addition to the `Bash, Read, Write`
  shared by the other agents. `Edit` is needed so the subagent can patch individual
  functions in `util.py` without overwriting the whole file. This is a deliberate
  extension, not a deviation from the frontmatter shape.
- The 5-iteration limit in Step 5 is a conservative guard against infinite loops;
  the conductor may want to make this configurable via the buildplan entry in a
  future iteration.
- `sampled-real` fixture handling falls back to synthetic if no matching data file
  exists in `subprojects/<name>/data/`; the substitution is documented in the
  work-record caveats section of the subagent's output.
