# Work record: king-kbu-model-verbs

## task_id
king-kbu-model-verbs (manually-dispatched developer task for PRD
`king-integration-apps`, Module B — no Maestro envelope task_id issued)

## branch
king-kbu-model-verbs

## commit_shas
- f49f598c5cb3f8b7beb2940234aebd5e1993da54 (`feat(cli): add kbu model verb group (reconstruct/gapfill/fba/fva/exec)`)

## summary

Added the `kbu model` Click subcommand group (`src/kbutillib/cli/model.py`,
registered in `src/kbutillib/cli/__init__.py`) as a thin facade over
KBUtilLib's existing modeling modules (`ms_reconstruction_utils`,
`ms_fba_utils`) per `agent-io/prds/king-integration-apps/fullprompt.md`
Module B. Four verified verbs (`reconstruct`, `gapfill`, `fba`, `fva`) call
straight through to `MSReconstructionUtils.build_metabolic_model`/
`gapfill_metabolic_model` and `MSFBAUtils.run_fba`/`run_fva` (never
`cobra.flux_variability_analysis`, which is broken) and emit the exact
`--json` schemas specified in Acceptance Criterion #11. A fifth verb,
`exec`, runs an arbitrary Python script in the kbu interpreter with a
durable, provenance-preserving run directory (script copy, stdout.txt,
stderr.txt, run.json with package versions) instead of a throwaway temp
dir, per Acceptance Criterion #12, and records a `kbu session save`
entry when a real project context exists.

The main engineering discovery during implementation: the documented `kbu`
interpreter contract ("kbutillib + cobra + modelseedpy already
importable") does not include `cobrakbase` (confirmed absent via `pip
show cobrakbase` — not installed) or a checked-out `cb_annotation_ontology_api`
sibling repo, both of which `KBModelUtils.__init__` unconditionally
requires for its normal KBase-SDK construction path. `tests/
test_ms_fba_utils_eval.py` had already established the sanctioned
workaround (bypass `KBModelUtils.__init__`, construct only the
`MSBiochemUtils` base + reconstruction/FBA-specific setup) for exactly
this reason; `model.py::_construct_offline` reuses that same pattern
rather than inventing a new one, so `kbu model` works fully offline with
no KBase token, no cobrakbase, and no network. modelseedpy 0.4.2 ships
two local templates (`core`, `gram_neg`) and a local `atp_medias.tsv`,
which is what makes offline `reconstruct`/`gapfill` possible at all;
`--atp-safe` (default off) additionally needs a local ModelSEED
biochemistry database checkout, resolved via `KBU_MODELSEED_DB_PATH` env
var or common local paths (`~/Dropbox/Projects/ModelSEEDDatabase` on this
machine).

Media is accepted as either a local JSON file (`MSMedia.from_dict`-
compatible dict — minimal/bounds/complete forms) or a KBase workspace
reference; `--objective` follows modelseedpy's `MAX{}`/`MIN{}` DSL for
`fba`/`fva` but is a bare reaction id for `gapfill` (matching
`gapfill_metabolic_model`'s own `default_target` contract) — both forms
are documented in `agent-io/docs/kbu-model-cli.md` and in the `model.py`
module docstring (Acceptance Criterion #10).

## files_touched

- `src/kbutillib/cli/model.py` (new) — the `kbu model` command group
- `src/kbutillib/cli/__init__.py` — registers `model_cmd`
- `pyproject.toml` — adds the `kbu_model` pytest marker
- `tests/cli/test_model.py` (new) — 10 tests covering the full verb chain
  and exec envelope/provenance
- `tests/fixtures/model/demo_genome.faa` (new) — 5-protein FASTA fixture
  (copy of the existing `tests/fixtures/dram2/demo.faa` content, given a
  feature-local name so this test suite isn't coupled to dram2's fixture)
- `tests/fixtures/model/glucose_minimal.json` (new) — minimal-format
  aerobic glucose media fixture
- `agent-io/docs/kbu-model-cli.md` (new) — user-facing CLI reference,
  including the `--media`/`--objective`/`--template` accepted-forms
  enumeration required by Acceptance Criterion #10

## success_criteria_check

- **`kbu model reconstruct|gapfill|fba|fva` run against the committed
  fixture genome, write model files, and emit `--json` matching AC #11
  with a plausible non-zero FBA objective and fva routed through
  `ms_fba_utils.run_fva`** — PASS. `tests/cli/test_model.py::
  TestVerifiedVerbChain::test_full_chain_reconstruct_gapfill_fba_fva`
  runs the complete chain against `tests/fixtures/model/demo_genome.faa`
  + `glucose_minimal.json`; observed FBA `objective_value≈1.397`
  (`solver_status="optimal"`), FVA returns 442 `{id,min,max}` entries
  whose `bio1` range brackets the FBA objective. A dedicated monkeypatch
  test (`test_fva_routes_through_run_fva_not_cobra_native`) asserts
  `MSFBAUtils.run_fva` is called exactly once and fails the test outright
  if `cobra.flux_analysis.flux_variability_analysis` is ever invoked.
- **`kbu model exec` returns the #12 JSON envelope for both a passing and
  a failing script AND leaves a durable run_dir (script copy +
  stdout/stderr + run.json with versions, relative outputs captured, no
  temp-cwd loss) plus a `kbu session list` entry** — PASS. `TestExec`
  covers: success envelope + run_dir contents + relative-output capture
  (`test_exec_success_envelope_and_run_dir`), failure envelope with
  nonzero `exit_code` and no crash
  (`test_exec_failure_yields_nonzero_exit_code_not_a_crash`), `--`
  passthrough args (`test_exec_passthrough_args`), session-list
  visibility when a project context exists
  (`test_exec_records_kbu_session_when_project_context_exists`), and the
  `~/.kbcache` fallback + no-session-entry behavior when no project
  context exists (`test_exec_falls_back_to_kbcache_without_project_context`).
- **The new tests pass (skip-marker respected when kbu absent)** — PASS.
  `pytest tests/cli/test_model.py -v` → 10 passed in ~77s. The module is
  gated behind `pytest.mark.kbu_model` plus a module-level
  `pytest.skip(..., allow_module_level=True)` when `cobra`/`modelseedpy`
  fail to import (mirrors the existing `_require_cobra`/
  `_require_modelseedpy` convention in `tests/test_ms_fba_utils_eval.py`).
  The `kbu_model` marker is registered in `pyproject.toml`.

## tests_run

- `pytest tests/cli/test_model.py -v` — **10 passed** in 76.5s (0:01:16).
- `pytest tests/cli/ -q --deselect tests/cli/test_jobs.py --deselect
  tests/cli/test_jobs_chain.py --deselect tests/cli/test_jobdaemon.py`
  (full CLI suite, deselecting the three job-daemon files that were
  already excluded by prior developer tasks per their work-records) —
  **600 passed, 2 failed** (`test_init.py::TestDoctorCommand::
  test_doctor_prints_one_line_per_probe`,
  `test_init_notebook.py::TestRenderUtilTemplate::
  test_contains_session_for`). Both failures reproduce identically on
  `main` with my changes `git stash`ed (verified directly), i.e. they are
  pre-existing and unrelated to this task.
- `ruff check src/kbutillib/cli/model.py tests/cli/test_model.py` —
  clean (one import-order and one unused-import issue were auto-fixed
  via `ruff check --fix` before the final run).
- Manual end-to-end smoke via `click.testing.CliRunner` in `/tmp`
  scratch directories (not committed) for all five verbs, including the
  `run_dir` path-doubling bug caught and fixed before writing the
  automated tests (see caveats).

## caveats

- **`--atp-safe` requires a local ModelSEED biochemistry database
  checkout** (not just cobra/modelseedpy). It resolves via
  `KBU_MODELSEED_DB_PATH` env var, then `~/Dropbox/Projects/
  ModelSEEDDatabase`, then `~/code/ModelSEEDDatabase`, then KBUtilLib's
  own default (sibling-directory) resolution. The default (`--no-atp-
  safe`) verbs never need this and are what the test suite exercises;
  `--atp-safe` itself is implemented and callable but not covered by an
  automated test (would need the real ModelSEED biochemistry DB present
  in CI, which is a large — several-GB — external dependency).
- **`gapfill` always gapfills against both local templates (`core` +
  `gn`)**, regardless of which template `reconstruct` originally used —
  documented as a deliberate simplification in `agent-io/docs/
  kbu-model-cli.md` ("Accepted --template forms"), since those are the
  only two templates modelseedpy 0.4.2 ships offline. A KBase-ref
  `--template` override exists on `reconstruct` for the online/KBase-auth
  case but was not exercised by tests (no live KBase token available in
  this environment).
- **`--media` KBase-workspace-reference resolution
  (`KBModelUtils.get_media`) is implemented but untested** — requires a
  live KBase token and network access, neither available in this
  environment. Only the local-JSON-file media path is exercised by
  automated tests, which is also the form the PRD's "local-only" KING
  use case actually needs.
- **`kbu model exec`'s `kbu session save`-equivalent step uses a fixed
  pseudo-subproject name `"kbu-model-exec"`** rather than trying to infer
  "the current subproject" from cwd (no such inference helper exists
  elsewhere in the `kbu` CLI to reuse, and inventing one felt out of
  scope for a thin facade). This subproject directory is created
  on-demand by the existing `_route_save_local` helper even though it was
  never explicitly initialized via `kbu subproject new` — confirmed this
  is exactly how `kbu session save` already behaves for arbitrary
  subproject names, not a new behavior I introduced.
- **The `<runs_root>/kbu-model-exec/...` PRD wording is ambiguous**
  about whether the `~/.kbcache/kbu-model-exec/` fallback string already
  includes the `kbu-model-exec` path segment that gets joined on top of
  it (which would literally double it: `~/.kbcache/kbu-model-exec/
  kbu-model-exec/...`). I resolved this by treating the PRD's fallback
  phrase as describing the *destination directory*, not the literal
  `<runs_root>` variable — so `<runs_root>` = `~/.kbcache` in the
  no-project-context case, giving the same final layout
  (`~/.kbcache/kbu-model-exec/<timestamp>-<hash>/`) as the project-context
  case's `<project>/runs/kbu-model-exec/<timestamp>-<hash>/`. Caught via a
  manual smoke test before writing the automated test suite; both cases
  are now covered by dedicated tests.
- **Did not modify AIAssistant or KING**, per the task's explicit
  instruction — Module A (`assistant` CLI) and Module D (`skill.md`
  content for both KING apps) are out of scope for this KBUtilLib-only
  task and remain to be picked up as separate work.
