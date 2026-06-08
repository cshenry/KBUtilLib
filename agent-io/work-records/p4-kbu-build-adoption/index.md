# Work Record: p4-kbu-build-adoption

## task_id
p4-kbu-build-adoption

## branch
task/p4-kbu-build-adoption

## commit_shas
- 3981cce3000fc9f55e30eacb57eda2890ac21bb4

## summary

Added adopted-branch detection (verify-and-extend mode) to `templates/research-project/.claude/commands/kbu-build.md` per PRD kbutillib-v2 User Story #14 and Acceptance Criterion #51. A new Phase 2 checks for existing `.ipynb` files in `notebooks/` immediately after loading the plan. If notebooks are present, the skill emits the exact warning strings required by AC #51 — `"Manifest lists missing notebook: <slug>"` and `"Notebook present but not in manifest: <filename>"` — then skips to Phase 7 without auto-creating anything. Virgin subprojects with no notebooks proceed through the full scaffold flow (now Phase 3 onward, renumbered from the original Phase 2 onward). The `last_reviewed` frontmatter date was updated to 2026-06-08.

## files_touched

- `templates/research-project/.claude/commands/kbu-build.md` — added Phase 2 (adopted-branch detection), renumbered Phases 2-6 to 3-7, added Rule 7, updated `last_reviewed`
- `agent-io/work-records/p4-kbu-build-adoption/index.md` — this file

## success_criteria_check

- **AC #51 / User Story #14**: `pass` — Phase 2 emits `"Manifest lists missing notebook: <slug>"` and `"Notebook present but not in manifest: <filename>"` with the exact strings from the PRD; does NOT auto-create notebooks; skips scaffold phases.
- **Original scaffold-from-plan branch preserved**: `pass` — when no `.ipynb` files are present, execution falls through to Phase 3 (renamed from old Phase 2) and the full scaffold flow runs unchanged.
- **Provenance frontmatter preserved**: `pass` — all original frontmatter fields kept; `last_reviewed` updated to 2026-06-08.
- **Pure markdown change**: `pass` — no code files modified.

## tests_run

- Skipped: skill markdown has no automated test framework (documented as "What is NOT tested" in the PRD).
- Manual review: final file read to verify phase numbering, warning strings, and branch logic are internally consistent.

## caveats

- Phase 3 still references `../data/` in the scaffold steps — this reflects the old layout and will be updated in a separate path-convention sweep task (noted as out of scope for the current task per the PRD's "Out of Scope" section).
- The `--scaffold-missing` flag referenced in Phase 2 and Rule 7 is explicitly out of scope for this PRD (noted in the PRD's "Further Notes").
- The manifest slug matching logic in Phase 2 assumes slugs map to `<slug>.ipynb` filenames, which is consistent with the manifest schema defined in the PRD (`[[notebooks]]` entries have a `slug` field).
