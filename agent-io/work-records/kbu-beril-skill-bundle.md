# Work Record — kbu-beril-skill-bundle

## task_id
kbu-beril-skill-bundle

## branch
kbu-beril/skill-bundle

## commit_shas
- 5806f2f156c0755de59d5129d65d8de951b8b750

## summary
Created the KBUtilLib BERIL skill bundle (PRD kbu-beril-augmentation,
Module 2).  Three skill units were added under
`src/kbutillib/beril/skills/`: `kbu/` (user-invocable primer that loads
preferences and activates modeling guidelines), `kbu-notebook/` (notebook
construction discipline: one util.py per dir, `%run util.py` cells,
cache-as-you-go pattern, `.kbcache/` layout, BERIL integration), and
`kbu-fba/` (full modeling arc: build → gapfill → FBA → FVA, with
mandated `MSFBAUtils.run_fva` and explicit prohibition of
`cobra.flux_variability_analysis` which is broken in this environment).
Both kbu-notebook and kbu-fba encode the graduated execution policy
(🟢/🟡/🔴 tiers, <5s / 5–60s / >60s runtime rubric).  A preferences.md
template and util.py.tmpl skeleton were also added.  43 smoke tests were
written and all pass on the task branch.

## files_touched
- `src/kbutillib/beril/skills/kbu/SKILL.md` — /kbu primer; user-invocable,
  loads preferences.md, points to kbu-notebook + kbu-fba, does NOT patch
  /berdl_start
- `src/kbutillib/beril/skills/kbu/preferences.md` — editable preferences
  template with all required YAML keys; defaults: runtime_threshold=60,
  fva_reaction_n=10, reconstruction_n/gapfill_media_n/gapfill_max_solutions=1
- `src/kbutillib/beril/skills/kbu-notebook/SKILL.md` — notebook discipline
  (auto-discoverable; no user-invocable); encodes graduated execution policy;
  supersedes jupyter-dev; documents .kbcache/ rules and BERIL layout
- `src/kbutillib/beril/skills/kbu-notebook/util.py.tmpl` — canonical util.py
  skeleton: sys-path bootstrap, NotebookSession.for_notebook, path constants,
  guarded imports
- `src/kbutillib/beril/skills/kbu-fba/SKILL.md` — modeling arc
  (auto-discoverable); mandates run_fva; forbids cobra.flux_variability_analysis;
  encodes graduated policy; documents FBA/FVA/gapfill signatures and sampling
  defaults
- `tests/test_beril_skill_bundle.py` — 43 smoke tests across 5 test classes

## success_criteria_check

1. `src/kbutillib/beril/skills/` contains `kbu/`, `kbu-notebook/`, `kbu-fba/`
   each with a valid-frontmatter `SKILL.md` — **PASS** (all three dirs present,
   frontmatter parsed and validated by tests)

2. `name`/`description` with "Use when"/`allowed-tools` in each SKILL.md —
   **PASS** (all three; 9 parametrized test cases pass)

3. `kbu` has `user-invocable: true` — **PASS** (test_kbu_user_invocable_true
   passes; notebook+fba correctly do NOT have user-invocable: true)

4. A preferences.md template with required YAML keys — **PASS** (all 11
   dotted keys validated by parametrized tests; runtime_threshold=60,
   fva_reaction_n=10 defaults verified)

5. A util.py template — **PASS** (util.py.tmpl present, parses as valid
   Python via ast.parse, contains _bootstrap_sys_paths and NotebookSession)

6. kbu-fba mandates `run_fva` and forbids `cobra.flux_variability_analysis` —
   **PASS** (test_fba_skill_mandates_run_fva and test_fba_skill_forbids_cobra_fva
   both pass; the word "broken" appears in the prohibition text)

7. Both notebook+fba skills encode graduated execution policy with <5/5-60/>60
   rubric — **PASS** (both SKILL.md files contain 🟢🟡🔴 markers and the
   5s/60s boundary values; 4 content tests pass)

8. `tests/test_beril_skill_bundle.py` passes — **PASS** (43/43 tests pass)

## tests_run

```
PYTHONPATH=src python3 -m pytest tests/test_beril_skill_bundle.py -v
```
Result: **43 passed** in 0.06s

```
PYTHONPATH=src python3 -m pytest tests/ -q --ignore=tests/test_beril_skill_bundle.py
```
Result: 17 failed, 961 passed, 17 skipped, 4 errors — all failures confirmed
pre-existing on `main` before this task (verified via `git stash` round-trip).
The failures are in `test_ms_biochem_deltag.py` and
`test_comprehensive_gapfill_wrapper.py`, both unrelated to the skill bundle.

## caveats

- The repo's `.gitignore` contains `test_*.py` under "Test files created
  during development", which would block `tests/test_beril_skill_bundle.py`.
  All other test files in `tests/` are already tracked (committed before that
  rule), so the rule appears to target root-level scratch files rather than
  the canonical test suite.  The new test file was committed with `git add -f`
  to match the existing convention.  A reviewer may want to clarify or remove
  that `.gitignore` rule to avoid the same friction on future test additions.

- The skill files do not include a `__init__.py` under
  `src/kbutillib/beril/` because `beril/skills/` holds static markdown/text
  assets (not Python modules) — no package init is needed.  A deployer task
  will copy these dirs into a BERIL root.

- `kbu-notebook/util.py.tmpl` uses `<project_id>` and `<notebook_name>` as
  literal placeholder strings (rather than Python template syntax) to keep it
  parseable by `ast.parse` and readable without any template engine.  Deployers
  should substitute these before use.
