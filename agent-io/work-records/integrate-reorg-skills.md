# Work Record: integrate-reorg-skills

## task_id
`integrate-reorg-skills` (branch name; no separate Maestro task_id was
supplied in the envelope)

## branch
`integrate-reorg-skills`

## commit_shas
- `f05a68548bbe0f0654c40dca01c3276bbff93b36` — fix(skills): repoint flat-submodule imports to top-level facade + consumer sweep

Branch base (unmodified): `cc08575f42a0f5d4dfc43b1d4bffa2a518e4c1e6` (tip of
`integrate-reorg` at task start, per the envelope).

## summary

Netted the shipped BERIL/harness/KING-app skill sources against the
`domains/` reorg and produced the consumer-migration sweep, per the
`kbutillib-reorg-integration` PRD's consumer-netting section (read from
`agent-io/prds/kbutillib-reorg-integration/fullprompt.md`, which lives on
the Dropbox parking repo's `wip` branch at commit `f51566b1` and was pulled
into this worktree's context via `git show`, not merged).

1. **Code-import repoint** — `src/kbutillib/beril/skills/kbu-fba/SKILL.md:61`
   imported the deleted flat module `from kbutillib.kb_berdl_utils import
   KBBERDLUtils`; repointed to the top-level facade form `from kbutillib
   import KBBERDLUtils` (bound decision in the PRD: shipped skills
   standardize on the facade form, no domain paths in skill code bodies).
   Swept `beril/skills/{kbu,kbu-fba,kbu-notebook}`, `harness/skills/kbu-run`,
   and `king_app/skill.md` for any other `from kbutillib.<x>_utils import`
   — this was the only hit. `kbu/SKILL.md` and `kbu-notebook/SKILL.md` use
   `from kbutillib.notebook import NotebookSession`, which is a surviving
   package-level shim per the PRD's mapping table (does not need
   repointing) and was left untouched.
2. **Stale file-path prose repoint** — corrected 6 flat file-path citations
   to their new domain paths, re-verifying line numbers against the actual
   reorg tree rather than copying the stale numbers verbatim (the reorg
   moved these files, and the old line numbers no longer matched):
   - `kbu-fba/SKILL.md:66` — `kb_berdl_utils.py:705` →
     `domains/kbase/kb_berdl_utils.py:705` (line number happened to still
     match; verified `get_genometables_from_kbase` is at line 705 in the
     new location).
   - `kbu-fba/SKILL.md:100` — `ms_reconstruction_utils.py:176` (
     `build_metabolic_model`) → `domains/modeling/ms_reconstruction_utils.py:188`
     (re-verified via grep).
   - `kbu-fba/SKILL.md:137` — `ms_reconstruction_utils.py:685`
     (`gapfill_metabolic_model`) → `domains/modeling/ms_reconstruction_utils.py:696`.
   - `kbu-fba/SKILL.md:204` — `ms_fba_utils.py:75` (`run_fba`) →
     `domains/modeling/ms_fba_utils.py:1320`.
   - `kbu-fba/SKILL.md:264` — `ms_fba_utils.py:86` (`run_fva`) →
     `domains/modeling/ms_fba_utils.py:1331`.
   - `king_app/skill.md:175-176` — `~/Dropbox/Projects/KBUtilLib/src/kbutillib/
     ms_reconstruction_utils.py` and `.../ms_fba_utils.py` →
     `.../src/kbutillib/domains/modeling/ms_reconstruction_utils.py` and
     `.../domains/modeling/ms_fba_utils.py`.
   Left `king_app/skill.md:171`'s citation of `src/kbutillib/cli/model.py`
   unchanged — `kbutillib.cli` is a surviving top-level package per the
   PRD's mapping table (not a flat submodule), and `src/kbutillib/cli/model.py`
   still exists verbatim in the reorg tree alongside the new
   `src/kbutillib/interfaces/cli/model.py` (both files are byte-identical,
   777 lines).
3. **Consumer sweep** — `~/Dropbox/Projects` is present on this machine, so
   the sweep ran live (not "skipped"). Grepped every `.py`/`.ipynb` file
   under `~/Dropbox/Projects` for `from kbutillib.<x> import`, excluded the
   `KBUtilLib` repo itself (its own tests intentionally use flat imports as
   part of the deprecation-shim test suite, owned by the sibling
   `backcompat-shim-guard` task — out of scope here), and excluded modules
   that stayed top-level post-reorg (`toolkit`, `layout`, `kb_app_runner`,
   `kb_job_utils`, `cli`, `notebook`, `cheminformatics`, `researchos`,
   `thermo_predictors`, `services`, `installed_clients`, `beril`,
   `beril_worktree`, `harness`, `king_app`, `data`, and the new
   `domains`/`core`/`interfaces`/`agents` packages). Result: **56 hits
   across 12 external repos** (AISynbioPipeline, GenomeAnnotationAggregator,
   KBAnnotationApps, ModelingLOE, NotebookWorkspaces, ResearchOS-workspaces,
   workspace_deluxe), written to `agent-io/reports/consumer-migration.md`
   as `path:line:import` entries grouped by repo. Flagged two anomalies
   that are pre-existing bugs unrelated to the reorg (`kbutillib.kbutils` —
   not a real module in either layout; `kbutillib.kb_model_standardization_utils`
   — not in the PRD's mapping table, real module has no `kb_` prefix). No
   external consumer file was modified. `KBDatalakeApps`/`BVBRCHackathon`
   exist but produced zero hits at sweep time; `EnsembleNotebooks`/
   `PangenomeAnalysis` (also named in the PRD) do not exist on this
   machine.

## files_touched
- `src/kbutillib/beril/skills/kbu-fba/SKILL.md` (6 edits: 1 import + 5 prose citations)
- `src/kbutillib/king_app/skill.md` (1 edit: 2-line prose citation)
- `agent-io/reports/consumer-migration.md` (new file)

## success_criteria_check

1. `grep -rEn 'from kbutillib\.[a-z_]+_utils import' src/kbutillib/beril/skills
   src/kbutillib/harness/skills src/kbutillib/king_app` returns no matches —
   **pass**. Ran it post-commit: exit code 1 (no matches).
2. `agent-io/reports/consumer-migration.md` exists and either lists external
   flat-import hits with path:line or is explicitly marked "skipped" —
   **pass**. File exists, `~/Dropbox/Projects` was present so it is a live
   enumeration (56 `path:line:import` entries across 12 repos), not marked
   "skipped".

## tests_run

No code was changed (only Markdown skill bodies and a new report), so there
is no pytest/lint surface introduced by this task. Verified by inspection
that the only non-Markdown/non-report file touched is none — `git diff
--cached --stat` confirms exactly 3 files: 2 `.md` skill sources + 1 new
report `.md`. Ran the two success-criteria grep commands directly (see
above) as the verification for this task.

## caveats

- The authoritative old→new mapping PRD (`agent-io/prds/kbutillib-reorg-integration/`)
  is not present in this worktree's history (it lives on `wip` at
  `f51566b1`, one commit ahead of what `integrate-reorg`'s branch point
  carries) — I read it via `git show f51566b1:agent-io/prds/kbutillib-reorg-integration/fullprompt.md`
  rather than a normal file read. It was not merged or copied into this
  branch; only its content was consulted for the mapping table.
- Re-verified and corrected the stale line numbers in the 4 `ms_reconstruction_utils.py`/
  `ms_fba_utils.py` prose citations against the actual reorg tree rather
  than mechanically substituting the old numbers into the new path — the
  task prompt only required correcting the *path*, but leaving badly stale
  line numbers next to a corrected path seemed more likely to mislead an
  agent reading the skill than to help, so I fixed both. This is a
  judgment call the reviewer should confirm is in scope.
- Left `king_app/skill.md:171` (`cli/model.py`) and the `kbutillib.notebook`
  imports in `kbu/SKILL.md`/`kbu-notebook/SKILL.md` untouched — both are
  surviving top-level package paths per the PRD's mapping table, not flat
  submodules, so they do not need repointing. Confirmed `src/kbutillib/cli/model.py`
  still exists (777 lines, byte-identical to the new
  `src/kbutillib/interfaces/cli/model.py`) rather than assuming this from
  the mapping table alone.
- Two anomalous external reach-ins (`kbutillib.kbutils` in
  `GenomeAnnotationAggregator/.../term_translator.py:67`, and
  `kbutillib.kb_model_standardization_utils` in the archived
  `ANMEThermodynamicAnalysis_old.ipynb`) do not correspond to any module in
  either the old flat layout or the new domain layout — they read as
  pre-existing typos/bugs unrelated to this migration. Flagged in the
  report rather than silently omitted or silently "fixed" (no external
  file was touched, per scope).
- Did not touch any file under `tests/`, `tests/core`, `tests/guard`, or any
  shim `.py` module, per the explicit scope boundary with the concurrent
  `backcompat-shim-guard` sibling task.
- Did not merge, rebase, or push anywhere; this branch sits on top of
  `integrate-reorg` tip `cc08575` only.
