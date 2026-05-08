# Seed prompt — KBUtilLib composition refactor PRD

Use this as the input to a fresh `/ai-design` session focused on producing the PRD for the KBUtilLib composition refactor. The prior `/ai-design` session (`session-20260506-2345-ae2b` and follow-up turns through 2026-05-08) resolved the strategic decisions; this session writes the design document.

---

## Goal

Produce the PRD at `agent-io/prds/kbutillib-composition-refactor/` (this directory) with the standard PRD structure (`humanprompt.md`, `fullprompt.md`, `data.json`) for a **single short-sharp big-bang refactor** of KBUtilLib from multi-inheritance mixins to composition over `SharedEnvUtils`.

## Settled decisions (do NOT re-litigate)

1. **Composition over multi-inheritance** is the going-forward pattern. Reference: KBJobUtils design session 2026-05-07/08 — every utility holds a `SharedEnvUtils` instance instead of inheriting from it; logger delegated via `self.env.logger`; external clients lazy-constructed; higher-level utilities compose siblings by holding instances. `thermo_utils.py` is the existing reference shape.

2. **Short-sharp big-bang refactor**, not phased. The phased option (8–12 weeks) was rejected as prolonging the disruption window. Target: one coordinated PR (or a tight chain of 2–3 AgentForge tasks landing within days, not weeks).

3. **No back-compat constraint.** The legacy `_legacy = NotebookUtil()` shim, `util_legacy.py`, and the multi-inheritance `NotebookUtil` god class are all in scope to retire. Public class/method names can change. Chris explicitly authorized destabilizing the legacy shim.

4. **Top-level public surface = Option C.**
   - `NotebookSession` holds the toolkit, exposed as `session.kbu`. Notebooks call `session.kbu.fba.run_fba(model)`, `session.kbu.biochem.search_compounds(...)`, etc.
   - Non-notebook callers use the `KBUtilLib` (or `Toolkit`) facade directly: `kbu = KBUtilLib(); kbu.fba.run_fba(model)`.
   - Underlying mechanism: lazy-property sub-utilities on the facade. Matches the existing `session.cache` / `session.vectors` ergonomic from Phase 4 of the notebook engine.

5. **Notebook transition recipe** (applies after the refactor lands, not part of this PRD's implementation scope):
   - Step 1: archive ALL existing notebooks in each consuming repo (ADP1Notebooks first) to create a clean slate.
   - Step 2: build new notebooks collaboratively with Chris using archived code as reference, against the new toolkit API.
   - No bulk migration.

6. **AP3 carve-outs in `ms_fba_utils.py`** (the audit over-flagged these — they are deliberate, not regressions):
   - `run_fva` — `cobra.flux_variability_analysis` is broken; this is the working FVA implementation.
   - `analyzed_reaction_objective_coupling` — evaluates KO impact on biomass; different operation than `cobra.single_reaction_deletion`.
   - `fit_flux_to_mutant_growth_rate_data` — specific science code that belongs at this abstraction layer; do NOT move into ModelSEEDpy.MSExpression.

   The PRD's per-module decomposition must preserve these methods and their semantics.

## What the PRD must specify

### Per-module decomposition

For each module in scope (list below), specify:

- Current public methods (read the source).
- Which `*Impl` class hosts each method post-refactor (e.g., `MSFBAUtilsImpl(env, biochem)`), or which **flat-module** function it becomes.
- Which composed dependencies the impl class holds (`env`, `biochem`, `kbio`, `annotation`, etc.) and how they're wired.

**Modules in scope:**
- `base_utils.py`, `shared_env_utils.py`, `dependency_manager.py`
- `ms_biochem_utils.py`, `kb_ws_utils.py`, `kb_annotation_utils.py`
- `kb_model_utils.py`, `ms_fba_utils.py`, `ms_reconstruction_utils.py`
- `model_standardization_utils.py`, `escher_utils.py`
- `argo_utils.py`, `ai_curation_utils.py`
- `kb_genome_utils.py`, `kb_plm_utils.py`, `kb_uniprot_utils.py`, `kb_reads_utils.py`, `kb_callback_utils.py`, `kb_sdk_utils.py`, `kb_berdl_utils.py`, `kbase_catalog_client.py`
- `bvbrc_utils.py`, `patric_ws_utils.py`, `rcsb_pdb_utils.py`
- `mmseqs_utils.py`, `skani_utils.py`, `thermo_utils.py` (reference shape — already composition)

**Modules to retire:**
- `notebook_utils.py` (legacy 840-line file — superseded by the `notebook/` subpackage)
- `kb_model_utils.py.bak` (already removed by task-40d03085, confirm gone)
- `examples.py` (broken; commented out of `__init__.py`)
- `util_legacy.py` in any consuming notebook repo (out of this repo, but call out in migration plan)

### Flat-module extractions

Specify the flat modules and their contents:
- `kbase_endpoints.py` — `ws_url_for_version`, `ee2_url_for_version`, `infer_version_from_endpoint`. Pure URL helpers extracted from KBWSUtils.
- `model_directionality.py` — direction-analysis stitching: `directionality_from_bounds`, `biochem_directionality`, `combine_directionality_signals`, `direction_conversion` constant. Replaces the broken `kb_model_utils.model_reaction_directionality_analysis` method (already nominally fixed by P0 task-40d03085, but the canonical home is here).
- `model_helpers.py` — `_check_and_convert_model`, `_parse_id` (the canonical version, replacing 3 duplicates).
- `compartments.py` — `compartment_types` constant + `normalize_compartment` function. (Note: P0 task already deduplicated `compartment_types` between `ms_biochem_utils` and `model_standardization_utils`. Confirm the current state and decide whether to move it again or leave in `ms_biochem_utils`.)

### Toolkit facade

Specify the `KBUtilLib` (or chosen name) class:

```python
class KBUtilLib:
    def __init__(self, env: SharedEnvUtils | None = None, **env_kwargs):
        self.env = env or SharedEnvUtils(**env_kwargs)
        # lazy sub-utilities below
        self._fba = None
        self._biochem = None
        # ...

    @property
    def fba(self) -> MSFBAUtilsImpl: ...
    @property
    def biochem(self) -> MSBiochemUtilsImpl: ...
    # etc.
```

Decide:
- Class name: `KBUtilLib`, `Toolkit`, `ModelingToolkit`, something else?
- Sub-utility attribute names: `fba`, `biochem`, `kbio`, `annotation`, `escher`, `recon`, `standardize`, `genome`, `plm`, `uniprot`, `mmseqs`, `skani`, `thermo`, `bvbrc`, `argo`, `curation`, `reads`, `callback`, `sdk`, `berdl`, `catalog`, `patric`, `pdb` — finalize the namespace.
- `NotebookSession.kbu` integration: how does it construct? Does it pass its own env in, or inherit one from a config?

### Test gate

Specify the **pre-flight semantic smoke tests** that must pass identically before and after the refactor:

- Reuse the `mini_model` fixture pattern from `tests/notebook/helpers/conftest.py`.
- For each module, lock at least one semantic invariant (e.g., "FBA on `mini_model` with `bio1` objective produces flux X within tolerance Y"; "FVA on `mini_model` produces N nonzero ranges"; "compound `H2O` matches ModelSEED `cpd00001`").
- Methods that need a real KBase token: `@pytest.mark.kbase`, skipped in CI.
- The PRD lists the specific invariants per module. The first AgentForge task writes those tests.

### Implementation sequence

Specify the AgentForge task chain. Suggested decomposition:

- **Task 1 (pre-flight):** Add semantic smoke tests against the current public surface. Tests pass on current code. Land separately so the contract is reviewable before the refactor.
- **Task 2 (the rewrite):** Implement all `*Impl` classes, flat modules, and the `KBUtilLib` facade. Update internal call sites. Tests pass. ADP1Notebooks intentionally not preserved.
- **Task 3 (cleanup, optional):** Remove `notebook_utils.py`, `examples.py`, retire `NotebookUtil` god class. Update `kbutillib/__init__.py` exports.

Each task: developer role, `--auto-merge --auto-review`, `--timeout 1800` (substantial work).

### Migration plan for downstream consumers

Identify and brief the consumers:

- **ADP1Notebooks** — the bulk of legacy callers. Plan: archive all existing notebooks (transition Step 1), then collaborative rebuild (transition Step 2). NOT in this PRD's implementation scope.
- **KBModelAgent**, **KBDatalakeApps**, **MeetingAIAssistant**, others — grep for `from kbutillib` imports and `NotebookUtil()` usage. The PRD lists each consumer and notes whether breaking changes are expected, plus a one-line migration hint.

### Rollback plan

If Task 2 lands and downstream breaks worse than expected:
- The pre-flight smoke tests stay green (they test KBUtilLib internals, not consumers), so KBUtilLib itself is internally consistent.
- Revert plan: revert the merge commit; the smoke tests revert with it. Consumers go back to working state.

## Inputs to read before drafting

1. **Audit** — `agent-io/audits/2026-05-06-kbutillib-cross-module-audit.md`. Per-module findings inform the decomposition. Note: the audit overreached on AP3 in several places (see decision #6 above) and had two false positives on `kb_genome_utils.py`. Verify before relying on specific claims.

2. **Reference PRD** — `agent-io/prds/notebook-engine-redesign/fullprompt.md`. Use its structure and tone as the template for this PRD.

3. **Notebook engine playbook** — `agent-io/docs/notebook-migration-playbook.md`. §13 describes the legacy migration approach being superseded. Note: there is currently an unresolved merge conflict (`UU`) on this file from before the audit session — Chris will resolve manually.

4. **Project state** — read kbutillib's project state via `assistant.state.load_project_state('kbutillib')`. The notes section captures the full design discussion across this session and the prior one.

5. **Existing source** — read each module's source under `src/kbutillib/` to populate the per-module decomposition tables. Don't trust the audit's method enumeration without verification.

6. **KBJobUtils** — read its design notes / source if available; it's the canonical composition example. The user mentioned it's the pilot; it may not be fully landed yet.

## Out of scope for this PRD

- The notebook transition itself (archive ALL → rebuild collaboratively). That belongs in the ADP1Notebooks repo's own design session.
- The post-refactor four-module boundary refactor (`save_solution_as_fba` placement, etc.). Once composition lands, those are trivial method moves between composed building blocks. Note as follow-up; don't design here.
- KBJobUtils itself — if its PRD isn't done yet, that's a parallel track.

## Deliverables expected from the session

- `agent-io/prds/kbutillib-composition-refactor/humanprompt.md` (one-paragraph summary + key constraints)
- `agent-io/prds/kbutillib-composition-refactor/fullprompt.md` (full PRD per the structure above)
- `agent-io/prds/kbutillib-composition-refactor/data.json` (machine-readable: module→Impl class mapping, flat-module contents, toolkit attribute names, AgentForge task chain spec)
- A clean session record saved at the end via the `/ai-design` Phase 6 protocol.
