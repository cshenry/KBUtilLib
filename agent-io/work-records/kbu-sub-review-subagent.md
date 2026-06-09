# Work Record: kbu-sub-review-subagent

## task_id
kbu-sub-review-subagent

## branch
kbu-conductor/kbu-sub-review-subagent

## commit_shas
- (to be filled after commit)

## summary
Extended `templates/research-project/.claude/agents/kbu-sub-review.md` to add a
structured b-review path that checks the subproject's `buildplan.json` test cases.
The new Step 2b instructs the agent to: (1) load and validate `buildplan.json` via
`kbu buildplan validate`, (2) confirm `test_util.py` exists, (3) check per-helper
test existence and pytest pass/fail for every helper in every notebook entry, (4)
check that notebook-declared outputs exist on disk, and (5) retain the original
structural quality checks from the prior version. The verdict file is written at
`subprojects/<name>/REVIEW_build_<n>.md` — the exact path the state gate globs for
(`_glob_review_files(subproject_dir, "build")` in `subproject.py`). The existing
plan-review and synthesis-review paths are unchanged except for a corrected filename
mapping table (all three stage file-keys are now `plan`, `build`, `synthesis` to
match the gate; previously the table listed `p-review`, `b-review`, `s-review`).

## files_touched
- `templates/research-project/.claude/agents/kbu-sub-review.md` — extended b-review
  path (Step 2b with subsections 2b-1 through 2b-5), corrected filename stage-key
  table, added Build/Test Results section to review template, updated Notes and
  Integration sections

## success_criteria_check
- **b-review checks each helper has a passing test in test_util.py**: PASS — Step
  2b-3 iterates every helper in every notebook entry, checks for a `test_<helper_name>`
  function in `test_util.py`, runs `pytest -k <helper_name>`, and records missing or
  failing tests as critical issues.
- **b-review checks declared outputs are present**: PASS — Step 2b-4 reads each
  notebook's output cells and verifies each declared path exists; absent outputs are
  recorded as important issues (not critical, since notebooks may not yet be run).
- **verdict file uses `<!-- kbu-review:verdict: pass|fail -->` marker**: PASS —
  marker appears verbatim as the required first line; matches the regex in
  `_parse_verdict` in `subproject.py`.
- **verdict file written to path the state gate reads**: PASS — output file for
  b-review is `REVIEW_build_<n>.md`; gate calls `_glob_review_files(subproject_dir,
  "build")` which globs `REVIEW_build_*.md`. Previous table incorrectly used
  `REVIEW_b-review_<n>.md` which the gate would never find.
- **plan-review and synthesis-review behavior preserved**: PASS — Steps 1, 2
  (p-review and s-review branches), 3, 4, 5 are unchanged. Only the b-review branch
  in Step 2 is replaced with a pointer to the new Step 2b section.

## tests_run
No automated tests exist for the agent skill document itself. The only verifiable
check is that the verdict marker string and file-path glob match the gate code in
`src/kbutillib/cli/subproject.py` — confirmed by direct inspection:
- `_VERDICT_PATTERN` at line 83-86 matches `<!-- kbu-review:verdict: pass|fail -->` (case-insensitive)
- `_glob_review_files(subproject_dir, "build")` at line 145 matches `REVIEW_build_*.md`

## caveats
- The original agent's Stage/Output table listed `REVIEW_p-review_<n>.md`,
  `REVIEW_b-review_<n>.md`, `REVIEW_s-review_<n>.md` — none of which the gate
  would ever find (it uses `plan`, `build`, `synthesis` as the stage keys). This is
  a pre-existing bug in the p-review and s-review paths. The task scope was to fix
  b-review only, but the table correction covers all three rows since they appear in
  the same table and fixing only b-review would leave a misleadingly inconsistent
  table. The p-review and s-review text bodies were not modified.
- The "declared outputs" check in Step 2b-4 relies on reading notebook cell content
  to identify output paths. This is a heuristic — it looks for cells writing to
  `../data/` or `../figures/`. If a notebook writes elsewhere, the agent may miss
  it. This is noted as a judgment call; a more robust approach would require an
  explicit `outputs` field in the buildplan schema (out of scope for this task).
- The buildplan schema (from Module A) does not include an explicit per-notebook
  `outputs` field. The review infers outputs from notebook cell content, which is
  the most practical approach given the current schema.
