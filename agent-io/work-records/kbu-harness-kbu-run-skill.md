# Work Record: kbu-harness-kbu-run-skill

## task_id
kbu-harness/kbu-run-skill

## branch
kbu-harness/kbu-run-skill

## commit_shas
- f7ce9a8b0a4d1e7c2f5938b4d6a03c12e89f5176

## summary
Created the `kbu-run` Claude Code skill bundle at
`src/kbutillib/harness/skills/kbu-run/SKILL.md` (with the parent directory tree
`src/kbutillib/harness/skills/kbu-run/` created as required by the PRD). The skill
encodes the full harness execution loop: (1) `kbu harness pull` to refresh from
BERIL, (2) classify the run via the PRD-A graduated-execution policy by reading the
fenced YAML block in `.claude/kbu/preferences.md` for `execution.runtime_threshold_seconds`
and `execution.fanout_threshold` — defaulting to the most conservative 🔴 full/consult
tier when the file or thresholds are absent, (3) present local vs h100 to the user with
no encoded default, (4) `kbu harness run`, (5) verify `executed` and `outputs_present`
per notebook, (6) read DEVLOG.md to confirm the entry was appended, and (7) on success
emit the exact single-line prompt `Push results back to BERIL now? (y/N)` and call
`kbu harness push` only on y/Y then remind the user to commit in BERIL; on failure stop
immediately, append the traceback to DEVLOG.md, escalate a BLOCKED report, and edit no
code. The skill contains no Anthropic API calls and no subprocess `claude` invocations.

Also added `tests/harness/__init__.py` and `tests/harness/test_skill_bundle.py` with 21
tests covering frontmatter parsing, all required field values, and body content checks for
each workflow step; all 21 pass.

## files_touched
- `src/kbutillib/harness/skills/kbu-run/SKILL.md` — new file; the kbu-run skill
- `tests/harness/__init__.py` — new file; package marker for the harness test subdirectory
- `tests/harness/test_skill_bundle.py` — new file; 21 smoke tests (force-added past gitignore)
- `agent-io/work-records/kbu-harness-kbu-run-skill.md` — this file

## success_criteria_check

**`src/kbutillib/harness/skills/kbu-run/SKILL.md` exists with valid Claude Code frontmatter**
PASS — file exists at the expected path; frontmatter parses with `name: kbu-run`, a
`description` starting "Use when ...", `allowed-tools: [Read, Bash]`, and `user-invocable: true`.

**description starts with 'Use when ...' and is scoped to COBRA/MSModelUtil/BERIL harness**
PASS — description is: "Use when running a local COBRA/MSModelUtil modeling project that
has been pulled from a BERIL clone into a kbu harness — to execute notebooks
programmatically, verify outputs, keep the DEVLOG, and push results back to BERIL after
user confirmation."

**Body documents pull -> classify -> run -> verify -> devlog -> (success: confirm-push / failure: report-no-edit) loop**
PASS — all seven workflow steps are documented in the SKILL.md body with the exact
`Push results back to BERIL now? (y/N)` prompt (AC #37), `BLOCKED` escalation on failure,
and explicit "Edit no code" instruction on the failure path (AC #26).

**Classification reads preferences.md yaml block and defaults to full/consult when uncertain**
PASS — Step 2 instructs the skill to parse only the fenced YAML block for
`execution.runtime_threshold_seconds` and `execution.fanout_threshold`, and states
"If the file is absent, the YAML block is missing, or either key is absent, default
to 🔴 full / consult (most conservative)."

**`pytest tests/harness/test_skill_bundle.py` passes asserting the frontmatter parses with those fields**
PASS — 21/21 tests pass.

**No Anthropic API / subprocess claude usage anywhere in the added files**
PASS — SKILL.md body explicitly states "Do NOT call the Anthropic API or invoke `claude`
as a subprocess. You ARE the Claude worker." No API calls or subprocess invocations appear
in any added file.

## tests_run
```
cd /tmp/kbu-harness-kbu-run-skill
python -m pytest tests/harness/test_skill_bundle.py -v
# Result: 21 passed in 0.04s
```

## caveats
- The `.gitignore` in this repo contains `test_*.py` at the root level, which git
  interprets as matching any `test_*.py` file anywhere in the tree. The test file was
  force-added with `git add -f`, consistent with how all other test files in this repo
  are tracked (e.g., `tests/test_beril_skill_bundle.py`, `tests/cli/test_bootstrap.py`
  are all tracked despite this rule). The underlying `.gitignore` issue pre-dates this
  task and is the same workaround used by prior tasks.
- The `src/kbutillib/harness/` directory tree contains only the skill bundle at this
  stage. The harness library internals (`scaffold.py`, `sync.py`, `runner.py`,
  `devlog.py`, `config.py`) and the CLI (`src/kbutillib/cli/harness.py`) are Module 1
  of the PRD and will be implemented in a separate task.
- The `tests/harness/` directory has 21 body-content checks in addition to the minimum
  frontmatter checks required by the task. These are smoke checks that verify each
  workflow step (pull, classify, run, verify, devlog, push-prompt, BLOCKED path) is
  documented in the skill body, matching the success criteria in AC #26.
