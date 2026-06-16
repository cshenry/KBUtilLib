# Work Record: kbu-tmfa-p5-kbufba-doc

## task_id
kbu-tmfa-p5-kbufba-doc

## branch
kbu-tmfa-p5-kbufba-doc

## commit_shas
- b27b6e7c0e2ed70097d27dbf09289c5edba18ff4

## summary
Added a new subsection "Applying a constraint package to an existing model" to
`src/kbutillib/beril/skills/kbu-fba/SKILL.md`. The subsection documents the
alternative path of loading a finished JSON model and wrapping it via
`MSModelUtil.get`, then applying `FullThermoPkg` via
`pkgmgr.getpkg("FullThermoPkg").build_package(params)` before calling
`run_fba` or `run_fva`. It explicitly states the in-place contract: both
`MSFBAUtils.run_fba` and `MSFBAUtils.run_fva` operate on the `MSModelUtil`
object in place (calling `configure_fba_formulation` then solving in the
current LP state without copying or resetting the model), so constraint
packages applied externally before the call persist and constrain the solve.
Methods are referenced by name, not line number. No code files were changed.

## files_touched
- `src/kbutillib/beril/skills/kbu-fba/SKILL.md` — 48 lines added (new subsection)
- `agent-io/work-records/kbu-tmfa-p5-kbufba-doc.md` — this work record

## success_criteria_check

- **kbu-fba SKILL.md contains a new 'applying a constraint package to an
  existing model' subsection** — PASS. Section added at line 325, between
  "Complete build → gapfill → FBA handoff" and "Graduated execution policy".

- **Documents MSModelUtil.get -> pkgmgr.getpkg(FullThermoPkg).build_package ->
  run_fba/run_fva** — PASS. All three steps appear in the interaction-pattern
  code block with explanatory prose.

- **Explicitly states run_fba/run_fva operate on the model in place** — PASS.
  "In-place contract" subsection states: "MSFBAUtils.run_fba and
  MSFBAUtils.run_fva operate on the MSModelUtil object in place" and explains
  that constraint packages survive into the solve.

- **Referenced by method name, not line number** — PASS. The prose references
  `MSFBAUtils.run_fba`, `MSFBAUtils.run_fva`, and `configure_fba_formulation`
  by name only; no line numbers used.

- **No code files changed** — PASS. `git diff --stat` showed only SKILL.md
  modified; `ms_fba_utils.py` and all other source files untouched.

## tests_run
No automated tests apply (docs-only change). Verified visually that:
- The new section appears between the correct anchors in SKILL.md.
- All three interaction steps (MSModelUtil.get, build_package, run_fba/run_fva)
  are present in the code block.
- The in-place contract paragraph references methods by name only.
- git status is clean after commit.

## caveats
- `FullThermoPkg.build_package` requires a `modelseed_path` parameter pointing
  to a local ModelSEED database checkout. The code example shows a placeholder
  string; users will need to supply their actual path. This matches modelseedpy's
  own documentation style.
- The `FullThermoPkg` class is not yet wired into the `session.kbu` toolkit
  (it lives in modelseedpy, not KBUtilLib). The example therefore uses
  `mdlutl.pkgmgr.getpkg(...)` directly rather than a `session.kbu.*` method,
  which is the appropriate escape-hatch pattern described in the existing SKILL.md
  overview section.
