# KBUtilLib Composition Refactor — Full PRD

**Status**: Design approved; ready for implementation.
**Owner**: Chris Henry
**Repo**: KBUtilLib
**Reference shape**: `src/kbutillib/kb_job_utils/` (composition pilot)
**Design sessions**: 2026-05-06 through 2026-05-08

---

## 1. Goal

Replace KBUtilLib's multi-inheritance mixin hierarchy with composition over `SharedEnvUtils`. Every utility class holds a `SharedEnvUtils` instance instead of inheriting from it. Higher-level utilities compose siblings by holding instances. A `KBUtilLib` facade provides lazy-property access to all sub-utilities.

### Current inheritance graph (simplified)

```
BaseUtils
  └─ SharedEnvUtils
       ├─ KBWSUtils
       │    ├─ KBCallbackUtils
       │    │    └─ KBAnnotationUtils
       │    │         └─ KBModelUtils (+ MSBiochemUtils)  ← diamond
       │    │              ├─ MSFBAUtils
       │    │              ├─ MSReconstructionUtils
       │    │              └─ EscherUtils (+ MSBiochemUtils)  ← diamond
       │    ├─ KBGenomeUtils
       │    │    ├─ KBPLMUtils
       │    │    └─ BVBRCUtils (+ KBAnnotationUtils)  ← diamond
       │    ├─ KBReadsUtils
       │    └─ KBSDKUtils
       ├─ MSBiochemUtils
       │    ├─ ModelStandardizationUtils
       │    └─ (mixed into KBModelUtils, EscherUtils)
       ├─ MMSeqsUtils
       ├─ SKANIUtils
       ├─ ThermoUtils
       ├─ KBBERDLUtils
       ├─ PatricWSUtils
       └─ ArgoUtils
            └─ AICurationUtils
```

### Target architecture

```
SharedEnvUtils  (standalone, no inheritance changes)

MSBiochemUtilsImpl(env)
KBWSUtilsImpl(env)
KBCallbackUtilsImpl(env, ws)
KBAnnotationUtilsImpl(env, ws, callback)
KBModelUtilsImpl(env, ws, annotation, biochem)
MSFBAUtilsImpl(env, model)
MSReconstructionUtilsImpl(env, model)
EscherUtilsImpl(env, model, biochem)
ModelStandardizationUtilsImpl(env, biochem)
KBGenomeUtilsImpl(env, ws)
KBPLMUtilsImpl(env, genome)
BVBRCUtilsImpl(env, genome, annotation)
KBReadsUtilsImpl(env, ws)
KBSDKUtilsImpl(env, ws)
ArgoUtilsImpl(env)
AICurationUtilsImpl(env, argo)
ThermoUtilsImpl(env, biochem)
MMSeqsUtilsImpl(env)
SKANIUtilsImpl(env)
KBBERDLUtilsImpl(env)
PatricWSUtilsImpl(env)
KBUniProtUtilsImpl(env)
RCSBPDBUtilsImpl(env)
KBJobUtils(env)  ← already composition, no change

KBUtilLib facade  ← lazy-property access to all of the above
```

---

## 2. Settled context (do NOT re-litigate)

1. **Composition over multi-inheritance** is the going-forward pattern. Reference: KBJobUtils (`src/kbutillib/kb_job_utils/utils.py`) — holds `SharedEnvUtils`, does not subclass it. Logger via module-level `logging.getLogger(__name__)`. External clients lazy-constructed.

2. **Short-sharp big-bang refactor**, not phased. The phased option (8-12 weeks) was rejected as prolonging the disruption window. Target: one coordinated PR or tight 2-3 AgentForge task chain.

3. **No back-compat constraint.** The legacy `_legacy = NotebookUtil()` shim, `util_legacy.py`, and the multi-inheritance `NotebookUtil` god class are all retired. Public class/method names can change.

4. **Top-level public surface = Option C.**
   - `NotebookSession.kbu` exposes the toolkit. Notebooks call `session.kbu.fba.run_fba(model)`.
   - Non-notebook callers: `kbu = KBUtilLib(); kbu.fba.run_fba(model)`.
   - Mechanism: lazy-property sub-utilities on the facade.

5. **Notebook transition** (archive ALL existing notebooks → rebuild collaboratively) is OUT of scope for this PRD.

6. **AP3 carve-outs in `ms_fba_utils.py`** are deliberate, not regressions:
   - `run_fva` — `cobra.flux_variability_analysis` is broken in production; this is the working FVA implementation.
   - `analyzed_reaction_objective_coupling` — evaluates KO impact on biomass; different semantics from `cobra.single_reaction_deletion`.
   - `fit_flux_to_mutant_growth_rate_data` — specific science code at this abstraction layer; does NOT belong in ModelSEEDpy.MSExpression.

---

## 3. Per-module decomposition

### 3.1 Foundation layer

#### `base_utils.py` (209 LoC) — BaseUtils

**Current public methods**: `__init__`, `reset_attributes`, `initialize_call`, `log_info`, `log_warning`, `log_error`, `log_debug`, `log_critical`, `print_attributes`, `validate_args`, `transfer_outputs`, `save_util_data`, `load_util_data`, `const_util_rxn_prefixes`

**Post-refactor**: Remains as `BaseUtils`. No Impl class needed — this is a standalone base with logging and provenance helpers. `SharedEnvUtils` continues to inherit from it (this single inheritance is intentional and clean).

**Changes**: None structural. Fix the `script_dir+"/../../"` fragile path (line 41). Replace `from genericpath import exists` with `from os.path import exists`.

#### `shared_env_utils.py` (612 LoC) — SharedEnvUtils(BaseUtils)

**Current public methods**: `__init__`, `read_config`, `get_config`, `get_config_value`, `set_environment_variable`, `save_config`, `read_token_file`, `save_token_file`, `set_token`, `get_token`, `load_environment_variables`, `get_env_var`, `get_dependency_path`, `get_data_path`, `export_environment`, `initialize_environment`, `create_local_dependency_config`

**Post-refactor**: Remains as `SharedEnvUtils(BaseUtils)`. This is the composition root — every Impl class holds an instance. No changes to its public surface.

**Changes**: Fix `read_token_file` log timing (logs before tokens are loaded). Rename `set_environment_variable` to `set_config_value` (it sets config, not env vars). Deprecate `get_config(section, key)` — callers should use `get_config_value("section.key")`.

#### `dependency_manager.py` (199 LoC) — DependencyManager

**Post-refactor**: No changes. Standalone class, already used by composition from `SharedEnvUtils`.

### 3.2 KBase workspace layer

#### `kb_ws_utils.py` (654 LoC) — KBWSUtils(SharedEnvUtils)

**Current public methods**: `__init__`, `reset_attributes`, `initialize_call`, `ws_client` (property), `set_ws`, `get_base_url_from_version`, `download_blob_file`, `upload_blob_file`, `save_ws_object`, `set_provenance`, `get_provenance`, `list_ws_objects`, `process_ws_ids`, `get_object_info`, `get_object`, `ws_get_objects`, `wsinfo_to_ref`, `create_ref`, `is_ref`, `list_all_types`, `get_type_specs`, `object_url`, `register_typespec_dryrun`, `request_module_ownership`, `list_module_versions`, `get_module_info`, `release_module`

**Post-refactor class**: `KBWSUtilsImpl(env: SharedEnvUtils)`

**Composed dependencies**: `env`

**Wiring**:
```python
class KBWSUtilsImpl:
    def __init__(self, env: SharedEnvUtils, kb_version: str = "prod"):
        self._env = env
        self._ws_client = None  # lazy
        self._kb_version = kb_version
```

**Changes**: Drop `SharedEnvUtils` inheritance. Workspace client lazy-constructed from `env.get_token("kbase")`. Admin methods (`register_typespec_dryrun`, `request_module_ownership`, `list_module_versions`, `get_module_info`, `release_module`) stay in the same class — they share the ws_client.

#### `kb_callback_utils.py` (260 LoC) — KBCallbackUtils(KBWSUtils)

**Current public methods**: `__init__`, `set_callback_client`, `initialize_callback`, `stop_callback`, `report_client`, `dfu_client`, `gfu_client`, `afu_client`, `rast_client`, `anno_client`, `devutil_client`, `annotate_genome_with_rast`, `save_genome_or_metagenome`, `save_report_to_kbase`

**Post-refactor class**: `KBCallbackUtilsImpl(env, ws)`

**Composed dependencies**: `env: SharedEnvUtils`, `ws: KBWSUtilsImpl`

**Changes**: Fix `Path.parent()` bug (property, not method). Fix `self.set_config()` call (nonexistent method). Drop inheritance from KBWSUtils; hold ws by reference.

### 3.3 Annotation and biochemistry layer

#### `kb_annotation_utils.py` (1016 LoC) — KBAnnotationUtils(KBCallbackUtils)

**Current public methods**: `__init__`, `reset_attributes`, `initialize_call`, `get_alias_hash`, `translate_term_to_modelseed`, `get_annotation_ontology_events`, `add_ontology_events`, `add_annotation_ontology_events`, `build_genome_tsv`, `process_object`, `save_object`, `clean_tag`, `clean_term`, `standardize_event`, `process_feature_aliases`, `upgrade_feature`, `integrate_terms_from_ftr`, `add_feature_ontology_terms`, `check_genome`, `convert_role_to_searchrole`, `translate_rast_function_to_sso`, `get_term_name`

**Post-refactor class**: `KBAnnotationUtilsImpl(env, ws, callback)`

**Composed dependencies**: `env: SharedEnvUtils`, `ws: KBWSUtilsImpl`, `callback: KBCallbackUtilsImpl`

**Changes**: Constructor file reads (`FilteredReactions.csv`) become lazy. Drop 4-level inheritance chain. Internal helpers (`clean_tag`, `clean_term`, `standardize_event`, `process_feature_aliases`, `upgrade_feature`, `integrate_terms_from_ftr`) become private methods.

#### `ms_biochem_utils.py` (1010 LoC) — MSBiochemUtils(SharedEnvUtils)

**Current public methods**: `__init__`, `biochem_db` (property), `identifier_hash` (property), `rxn_identifier_hash` (property), `rxn_stoichiometry_hash` (property), `structure_hash` (property), `element_hashes` (property), `normalize_compound_name`, `normalize_and_search_compound`, `update_database`, `search_compounds`, `search_reactions`, `get_compound_by_id`, `get_reaction_by_id`, `get_database_statistics`, `reaction_to_string`, `reaction_id_to_msid`, `reaction_to_msid`, `reaction_directionality_from_bounds`, `reaction_biochem_directionality`, `build_model_metabolite_index`, `is_water`, `is_proton`, `find_proton_in_compartment`, `parse_formula`, `check_reaction_balance`, `can_fix_with_protons`

**Private methods moving to flat modules**: `_parse_id` → `model_helpers._parse_id`, `_standardize_string` stays private, `_parse_formula` and `parse_formula` consolidated.

**Post-refactor class**: `MSBiochemUtilsImpl(env)`

**Composed dependencies**: `env: SharedEnvUtils`

**Changes**: Drop inheritance. `compartment_types` constant moves to `compartments.py` flat module (canonical home). `_parse_id` moves to `model_helpers.py`. Duplicate `parse_formula`/`_parse_formula` consolidated into one.

### 3.4 Model layer

#### `kb_model_utils.py` (712 LoC) — KBModelUtils(KBAnnotationUtils, MSBiochemUtils)

**Current public methods**: `__init__`, `msrecon` (property), `process_media_list`, `create_minimal_medias`, `get_msgenome_from_ontology`, `get_expression_objs`, `get_msgenome_from_dict`, `get_msgenome`, `get_media`, `get_phenotypeset`, `get_model`, `extend_model_with_other_ontologies`, `get_classifier`, `get_gs_template`, `get_template`, `save_model`, `save_phenotypeset`, `save_solution_as_fba`, `model_reaction_directionality_analysis`

**Private methods moving to flat modules**: `_check_and_convert_model` → `model_helpers._check_and_convert_model`, `_parse_id` → `model_helpers._parse_id` (deduplicate with `ms_biochem_utils` version), `_parse_rxn_stoichiometry` stays private.

**Post-refactor class**: `KBModelUtilsImpl(env, ws, annotation, biochem)`

**Composed dependencies**: `env: SharedEnvUtils`, `ws: KBWSUtilsImpl`, `annotation: KBAnnotationUtilsImpl`, `biochem: MSBiochemUtilsImpl`

**Changes**: Diamond inheritance eliminated. `_import_modules` side effects cleaned up. `model_reaction_directionality_analysis` rewritten to use `model_directionality.py` flat module (fixes NameError bug where `direction_conversion` is undefined). `msrecon` lazy property wired via composition.

#### `ms_fba_utils.py` (685 LoC) — MSFBAUtils(KBModelUtils)

**Current public methods**: `__init__`, `set_media`, `set_objective_from_string`, `constrain_objective`, `constrain_objective_to_fraction_of_optimum`, `configure_fba_formulation`, `run_fba`, `run_fva`, `analyzed_reaction_objective_coupling`, `determine_biomass_objective_coupling`, `unblock_objective_with_exchanges`, `fit_flux_to_mutant_growth_rate_data`

**Post-refactor class**: `MSFBAUtilsImpl(env, model)`

**Composed dependencies**: `env: SharedEnvUtils`, `model: KBModelUtilsImpl`

**AP3 carve-outs preserved**:
- `run_fva` (line 86) — kept as-is. The custom per-reaction `slim_optimize` loop is deliberate because `cobra.flux_variability_analysis` produces incorrect results in the production environment.
- `analyzed_reaction_objective_coupling` (line 99) — kept as-is. Evaluates KO impact with reduced/essential/dispensable categorization; different operation than `cobra.single_reaction_deletion`.
- `fit_flux_to_mutant_growth_rate_data` (line 405) — kept as-is. Science code at this abstraction layer.

**Bug fixes during refactor**:
- Line 347: `model.printlp` → `mdlutl.printlp`
- Line 361: `"EXC_temp_"` → `"EX_temp_"` (match prefix used at line 318)
- Line 562: Fix `MSModelUtil.from_cobrapy` API misuse

#### `ms_reconstruction_utils.py` (1154 LoC) — MSReconstructionUtils(KBModelUtils)

**Current public methods**: `__init__`, `module_dir` (property), `modelseedpy_data_dir` (property), `build_metabolic_model`, `compute_ontology_model_changes`, `kb_build_metabolic_models`, `gapfill_metabolic_model`, `kb_gapfill_metabolic_models`

**Post-refactor class**: `MSReconstructionUtilsImpl(env, model)`

**Composed dependencies**: `env: SharedEnvUtils`, `model: KBModelUtilsImpl`

**Changes**: Drop inheritance. Lazy imports in `_reconstruction_imports` preserved. `_build_dataframe_report` becomes private. Common pipeline between `kb_*` and non-`kb_*` variants extracted.

#### `model_standardization_utils.py` (1096 LoC) — ModelStandardizationUtils(MSBiochemUtils)

**Current public methods**: `__init__`, `remove_model_periplasm_compartment`, `model_standardization`, `compare_model_to_msmodel`, `match_model_compounds_to_db`, `match_model_reactions_to_db`, `check_for_perfect_matches`, `analyze_compound_matches`, `translate_model_to_ms_namespace`, `apply_translation_to_model`

**Post-refactor class**: `ModelStandardizationUtilsImpl(env, biochem)`

**Composed dependencies**: `env: SharedEnvUtils`, `biochem: MSBiochemUtilsImpl`

**Changes**: Drop inheritance. Remove local `compartment_types` duplicate (use `compartments.compartment_types` instead). Remove `from email.policy import default` (unused editor autocomplete). `direction_conversion` moves to `model_directionality.py`.

#### `escher_utils.py` (1499 LoC) — EscherUtils(KBModelUtils, MSBiochemUtils)

**Current public methods**: `__init__`, `local_map_index` (property), `kbase_map_index` (property), `list_available_maps`, `map_stats`, `update_map_reaction_names`, `create_map_html2`

**Post-refactor class**: `EscherUtilsImpl(env, model, biochem)`

**Composed dependencies**: `env: SharedEnvUtils`, `model: KBModelUtilsImpl`, `biochem: MSBiochemUtilsImpl`

**Changes**: Drop diamond inheritance. Internal translation helpers (`_translate_map_with_flux`, `_translate_model_with_flux`, `_translate_flux`, `_translate_reaction_classes`, `_translate_reaction_badges`, `_inject_*`) become private methods.

### 3.5 Genome and sequence layer

#### `kb_genome_utils.py` (770 LoC) — KBGenomeUtils(KBWSUtils)

**Current public methods**: `__init__`, `load_kbase_gene_container`, `object_to_features`, `get_ftr`, `ftr_to_aliases`, `alias_to_ftrs`, `object_to_proteins`, `add_annotations_to_object`, `reverse_complement`, `translate_sequence`, `calculate_gc_content`, `load_genome_from_local_files`, `aggregate_taxonomies`, `create_synthetic_genome`

**Post-refactor class**: `KBGenomeUtilsImpl(env, ws)`

**Composed dependencies**: `env: SharedEnvUtils`, `ws: KBWSUtilsImpl`

**Bug fixes during refactor**: Add missing imports (`Path`, `hashlib`, `defaultdict`, `datetime`, `Counter`) that cause NameErrors in `load_genome_from_local_files`, `create_synthetic_genome`, `aggregate_taxonomies`. Move `genetic_code_standard` constant to a constants file or module-level.

#### `kb_plm_utils.py` (804 LoC) — KBPLMUtils(KBGenomeUtils)

**Current public methods**: `__init__`, `query_plm_api`, `query_plm_api_batch`, `get_uniprot_sequences`, `create_blast_database`, `run_blastp`, `find_best_hits_for_features`, `get_best_uniprot_ids`

**Post-refactor class**: `KBPLMUtilsImpl(env, genome)`

**Composed dependencies**: `env: SharedEnvUtils`, `genome: KBGenomeUtilsImpl`

#### `bvbrc_utils.py` — BVBRCUtils(KBGenomeUtils, KBAnnotationUtils)

**Current public methods**: `__init__`, `fetch_genome_metadata`, `fetch_genome_sequences`, `fetch_genome_features`, `fetch_feature_sequences`, `build_kbase_genome_from_api`

**Post-refactor class**: `BVBRCUtilsImpl(env, genome, annotation)`

**Composed dependencies**: `env: SharedEnvUtils`, `genome: KBGenomeUtilsImpl`, `annotation: KBAnnotationUtilsImpl`

**Changes**: Drop diamond inheritance through KBWSUtils.

#### `kb_reads_utils.py` (1300+ LoC) — KBReadsUtils(KBWSUtils) + data classes

**Current public methods (KBReadsUtils)**: `__init__`, `upload_reads`, `download_reads`, `bulk_upload_reads`, `bulk_download_reads`, `download_assembly`, `upload_assembly`

**Data classes** (`Reads`, `ReadSet`, `Assembly`, `AssemblySet`): standalone, no inheritance — no change needed.

**Post-refactor class**: `KBReadsUtilsImpl(env, ws)`

**Composed dependencies**: `env: SharedEnvUtils`, `ws: KBWSUtilsImpl`

### 3.6 External service layer

#### `kb_uniprot_utils.py` — KBUniProtUtils(BaseUtils)

**Current public methods**: `__init__`, `get_uniprot_entry`, `get_protein_sequence`, `get_annotations`, `get_publications`, `get_rhea_ids`, `get_pdb_ids`, `get_uniref_ids`, `get_uniprot_info`, `get_batch_uniprot_info`

**Post-refactor class**: `KBUniProtUtilsImpl(env)`

**Composed dependencies**: `env: SharedEnvUtils`

**Note**: Currently inherits from `BaseUtils` (not `SharedEnvUtils`). Post-refactor, holds `SharedEnvUtils` for consistency. Logger via `self._env.logger` or module-level.

#### `rcsb_pdb_utils.py` — RCSBPDBUtils(BaseUtils)

**Current public methods**: `__init__`, `query_rcsb_with_sequence`, `query_rcsb_metadata_by_id`, `query_rcsb_with_proteins`

**Post-refactor class**: `RCSBPDBUtilsImpl(env)`

**Composed dependencies**: `env: SharedEnvUtils`

#### `kb_berdl_utils.py` — KBBERDLUtils(SharedEnvUtils)

**Current public methods**: `__init__`, `print_docs`, `query`, `get_database_list`, `get_database_tables`, `get_table_columns`, `get_database_schema`, `paginate_query`, `test_connection`, `get_genometables_from_kbase`

**Post-refactor class**: `KBBERDLUtilsImpl(env)`

**Composed dependencies**: `env: SharedEnvUtils`

#### `kbase_catalog_client.py` — CatalogClient (standalone)

**Current public methods**: `__init__`, `register_repo`, `push_dev_to_beta`, `request_release`, `get_module_info`, `get_module_state`, `get_module_version`, `list_basic_module_info`, `get_build_log`, `list_builds`, `is_registered`, `wait_for_build`, `register_and_wait`

**Post-refactor**: No changes. Already standalone (no KBUtilLib inheritance). Plus standalone function `register_module`. Exposed as `kbu.catalog` on the facade.

#### `patric_ws_utils.py` — PatricWSClient + PatricWSUtils(SharedEnvUtils)

**Current public methods (PatricWSUtils)**: `__init__`, `ws_client` (property), `build_ref`, `save_object`, `get_object`, `get_ref`, `list_objects`, `delete_object`, `copy_object`, `save_model_object`, `get_model_object`, `save_fba_object`, `get_fba_object`, `save_media_object`, `get_media_object`, `parse_media_data`, `get_media`, `list_models`, `list_fbas`, `list_media`

**Post-refactor class**: `PatricWSUtilsImpl(env)`

**Composed dependencies**: `env: SharedEnvUtils`

**Note**: `PatricWSClient` is already a standalone class (no inheritance). Stays as-is.

### 3.7 AI and LLM layer

#### `argo_utils.py` (421 LoC) — ArgoUtils(SharedEnvUtils)

**Current public methods**: `__init__`, `chat`, `ping`

**Post-refactor class**: `ArgoUtilsImpl(env)`

**Composed dependencies**: `env: SharedEnvUtils`

**Note**: Standalone functions `_parse`, `llm_label` stay as module-level functions.

#### `ai_curation_utils.py` (1105 LoC) — AICurationUtils(ArgoUtils)

**Current public methods**: `__init__`, `chat`, `analyze_reaction_directionality`, `evaluate_reaction_equivalence`, `evaluate_reaction_gene_association`, `analyze_reaction_stoichiometry`, `build_reaction_from_functional_roles`, `find_compound_aliases`, `curate_biochemical_compound`

**Post-refactor class**: `AICurationUtilsImpl(env, argo)`

**Composed dependencies**: `env: SharedEnvUtils`, `argo: ArgoUtilsImpl`

**Changes**: Remove debug `print` (line 62). Move AI cache storage from `BaseUtils.data_directory` (source tree) to `~/.kbutillib/ai_cache/`. Remove unused cobra import.

### 3.8 Tool/algorithm layer

#### `mmseqs_utils.py` — MMSeqsUtils(SharedEnvUtils)

**Current public methods**: `__init__`, `cluster_proteins`, `easy_cluster`, `get_cluster_representatives`, `get_cluster_membership`

**Post-refactor class**: `MMSeqsUtilsImpl(env)`

**Composed dependencies**: `env: SharedEnvUtils`

#### `skani_utils.py` — SKANIUtils(SharedEnvUtils)

**Current public methods**: `__init__`, `sketch_genome_directory`, `add_skani_database`, `query_genomes`, `list_databases`, `get_database_info`, `remove_database`, `compute_pairwise_distances`

**Post-refactor class**: `SKANIUtilsImpl(env)`

**Composed dependencies**: `env: SharedEnvUtils`

#### `thermo_utils.py` — ThermoUtils(SharedEnvUtils)

**Current public methods**: `__init__`, `get_compound_deltag`, `calculate_reaction_deltag`, `compute_ion_transfer`

**Post-refactor class**: `ThermoUtilsImpl(env, biochem)`

**Composed dependencies**: `env: SharedEnvUtils`, `biochem: MSBiochemUtilsImpl`

**Changes**: Already near-composition (lazy-loads `biochem_utils`). Drop `SharedEnvUtils` inheritance. Accept `biochem` as constructor arg instead of lazy-constructing it internally. Move `_parse_id` to `model_helpers._parse_id`.

### 3.9 KBase SDK/callback layer

#### `kb_sdk_utils.py` (30 LoC) — KBSDKUtils(KBWSUtils)

**Current public methods**: `__init__`, `build_dataframe_report`

**Post-refactor class**: `KBSDKUtilsImpl(env, ws)`

**Composed dependencies**: `env: SharedEnvUtils`, `ws: KBWSUtilsImpl`

#### `kb_callback_utils.py` — covered in 3.2 above.

### 3.10 Already-composition modules (no structural change)

#### `kb_job_utils/` — KBJobUtils

Already composition-based. Holds `SharedEnvUtils`, no inheritance. The canonical reference shape. No changes needed.

---

## 4. Modules to retire

| Module | LoC | Reason | Action |
|--------|-----|--------|--------|
| `notebook_utils.py` | 857 | Legacy 840-line file superseded by `notebook/` subpackage | Delete |
| `examples.py` | 150 | Broken; calls non-existent methods; commented out of `__init__.py` | Delete |
| `kb_model_utils.py.bak` | — | Already removed by task-40d03085 | Confirmed gone |

**In consuming repos (out of scope for this PR, noted in migration plan)**:
- `util_legacy.py` in ADP1Notebooks and similar — retired when notebooks are rebuilt.
- `NotebookUtil` god class patterns in per-project `util.py` — retired during notebook transition.

---

## 5. Flat-module extractions

### 5.1 `kbase_endpoints.py` — ALREADY DONE

Already extracted. Contains `base_url`, `service_url`, `narrative_url`, `env_from_url`. No further action.

### 5.2 `model_directionality.py` — NEW

Direction-analysis stitching extracted from `kb_model_utils.model_reaction_directionality_analysis` (currently broken — `direction_conversion` undefined) and `ms_biochem_utils.reaction_directionality_from_bounds`/`reaction_biochem_directionality`.

```python
# model_directionality.py

direction_conversion = {
    "=": "reversible",
    ">": "forward",
    "<": "reverse",
    "?": "unknown",
}

def directionality_from_bounds(reaction, tol=1e-9) -> str: ...
def biochem_directionality(reaction, biochem) -> str: ...
def combine_directionality_signals(model_dir, biochem_dir, ai_dir=None) -> dict: ...
```

`MSBiochemUtilsImpl` methods `reaction_directionality_from_bounds` and `reaction_biochem_directionality` become thin wrappers delegating to this module. `KBModelUtilsImpl.model_reaction_directionality_analysis` is rewritten to call these functions.

### 5.3 `model_helpers.py` — NEW

Canonical versions of duplicated helpers:

```python
# model_helpers.py

def _check_and_convert_model(model) -> "MSModelUtil": ...
def _parse_id(object_or_id) -> tuple[str, str | None, str | None]: ...
```

Replaces the triplicate `_parse_id` in `kb_model_utils` (lines 126-163), `ms_biochem_utils` (lines 429-475), and `thermo_utils` (lines 39-64). The canonical implementation covers all three variants' behavior.

### 5.4 `compartments.py` — NEW

```python
# compartments.py

compartment_types = {
    "c": "cytosol", "c0": "cytosol",
    "e": "extracellular", "e0": "extracellular",
    "p": "periplasm", "p0": "periplasm",
    "m": "mitochondria", "m0": "mitochondria",
    # ... full mapping from ms_biochem_utils.py (the complete version)
}

def normalize_compartment(compartment: str) -> str:
    """Normalize a compartment identifier to its canonical name."""
    return compartment_types.get(compartment.lower(), compartment)
```

This is the **complete** `compartment_types` dict from `ms_biochem_utils.py` (lines 14-28), which includes `m`, `mitochondria`, `membrane`, `extracellular`, `environment`, `env` — keys missing from `model_standardization_utils.py`'s narrower duplicate. The narrower version in `model_standardization_utils` is deleted and all imports point to `compartments.py`.

---

## 6. KBUtilLib facade (Toolkit)

### 6.1 Class name decision

**`KBUtilLib`** — matches the package name; clear and unambiguous. The `Toolkit` or `ModelingToolkit` alternatives were considered but `KBUtilLib` wins on discoverability.

### 6.2 Implementation

```python
class KBUtilLib:
    """Lazy-loading facade for all KBUtilLib sub-utilities.

    Usage:
        kbu = KBUtilLib()
        kbu.fba.run_fba(model)
        kbu.biochem.search_compounds("glucose")
        kbu.ws.get_object("12345/6/7")
    """

    def __init__(self, env: SharedEnvUtils | None = None, **env_kwargs):
        self.env = env or SharedEnvUtils(**env_kwargs)
        # Private backing fields for lazy properties
        self._ws = None
        self._callback = None
        self._annotation = None
        self._biochem = None
        self._model = None
        self._fba = None
        self._recon = None
        self._escher = None
        self._standardize = None
        self._genome = None
        self._plm = None
        self._bvbrc = None
        self._reads = None
        self._sdk = None
        self._argo = None
        self._curation = None
        self._thermo = None
        self._mmseqs = None
        self._skani = None
        self._berdl = None
        self._patric = None
        self._uniprot = None
        self._pdb = None
        self._catalog = None
        self._jobs = None

    # ── sub-utility lazy properties ──────────────────────────────

    @property
    def ws(self) -> KBWSUtilsImpl:
        if self._ws is None:
            self._ws = KBWSUtilsImpl(self.env)
        return self._ws

    @property
    def callback(self) -> KBCallbackUtilsImpl:
        if self._callback is None:
            self._callback = KBCallbackUtilsImpl(self.env, self.ws)
        return self._callback

    @property
    def annotation(self) -> KBAnnotationUtilsImpl:
        if self._annotation is None:
            self._annotation = KBAnnotationUtilsImpl(self.env, self.ws, self.callback)
        return self._annotation

    @property
    def biochem(self) -> MSBiochemUtilsImpl:
        if self._biochem is None:
            self._biochem = MSBiochemUtilsImpl(self.env)
        return self._biochem

    @property
    def model(self) -> KBModelUtilsImpl:
        if self._model is None:
            self._model = KBModelUtilsImpl(self.env, self.ws, self.annotation, self.biochem)
        return self._model

    @property
    def fba(self) -> MSFBAUtilsImpl:
        if self._fba is None:
            self._fba = MSFBAUtilsImpl(self.env, self.model)
        return self._fba

    @property
    def recon(self) -> MSReconstructionUtilsImpl:
        if self._recon is None:
            self._recon = MSReconstructionUtilsImpl(self.env, self.model)
        return self._recon

    @property
    def escher(self) -> EscherUtilsImpl:
        if self._escher is None:
            self._escher = EscherUtilsImpl(self.env, self.model, self.biochem)
        return self._escher

    @property
    def standardize(self) -> ModelStandardizationUtilsImpl:
        if self._standardize is None:
            self._standardize = ModelStandardizationUtilsImpl(self.env, self.biochem)
        return self._standardize

    @property
    def genome(self) -> KBGenomeUtilsImpl:
        if self._genome is None:
            self._genome = KBGenomeUtilsImpl(self.env, self.ws)
        return self._genome

    @property
    def plm(self) -> KBPLMUtilsImpl:
        if self._plm is None:
            self._plm = KBPLMUtilsImpl(self.env, self.genome)
        return self._plm

    @property
    def bvbrc(self) -> BVBRCUtilsImpl:
        if self._bvbrc is None:
            self._bvbrc = BVBRCUtilsImpl(self.env, self.genome, self.annotation)
        return self._bvbrc

    @property
    def reads(self) -> KBReadsUtilsImpl:
        if self._reads is None:
            self._reads = KBReadsUtilsImpl(self.env, self.ws)
        return self._reads

    @property
    def sdk(self) -> KBSDKUtilsImpl:
        if self._sdk is None:
            self._sdk = KBSDKUtilsImpl(self.env, self.ws)
        return self._sdk

    @property
    def argo(self) -> ArgoUtilsImpl:
        if self._argo is None:
            self._argo = ArgoUtilsImpl(self.env)
        return self._argo

    @property
    def curation(self) -> AICurationUtilsImpl:
        if self._curation is None:
            self._curation = AICurationUtilsImpl(self.env, self.argo)
        return self._curation

    @property
    def thermo(self) -> ThermoUtilsImpl:
        if self._thermo is None:
            self._thermo = ThermoUtilsImpl(self.env, self.biochem)
        return self._thermo

    @property
    def mmseqs(self) -> MMSeqsUtilsImpl:
        if self._mmseqs is None:
            self._mmseqs = MMSeqsUtilsImpl(self.env)
        return self._mmseqs

    @property
    def skani(self) -> SKANIUtilsImpl:
        if self._skani is None:
            self._skani = SKANIUtilsImpl(self.env)
        return self._skani

    @property
    def berdl(self) -> KBBERDLUtilsImpl:
        if self._berdl is None:
            self._berdl = KBBERDLUtilsImpl(self.env)
        return self._berdl

    @property
    def patric(self) -> PatricWSUtilsImpl:
        if self._patric is None:
            self._patric = PatricWSUtilsImpl(self.env)
        return self._patric

    @property
    def uniprot(self) -> KBUniProtUtilsImpl:
        if self._uniprot is None:
            self._uniprot = KBUniProtUtilsImpl(self.env)
        return self._uniprot

    @property
    def pdb(self) -> RCSBPDBUtilsImpl:
        if self._pdb is None:
            self._pdb = RCSBPDBUtilsImpl(self.env)
        return self._pdb

    @property
    def catalog(self) -> CatalogClient:
        if self._catalog is None:
            from .kbase_catalog_client import CatalogClient
            self._catalog = CatalogClient(url=service_url("catalog"))
        return self._catalog

    @property
    def jobs(self) -> KBJobUtils:
        if self._jobs is None:
            self._jobs = KBJobUtils(self.env)
        return self._jobs
```

### 6.3 Sub-utility attribute namespace (finalized)

| Attribute | Impl class | Dependencies |
|-----------|-----------|--------------|
| `ws` | KBWSUtilsImpl | env |
| `callback` | KBCallbackUtilsImpl | env, ws |
| `annotation` | KBAnnotationUtilsImpl | env, ws, callback |
| `biochem` | MSBiochemUtilsImpl | env |
| `model` | KBModelUtilsImpl | env, ws, annotation, biochem |
| `fba` | MSFBAUtilsImpl | env, model |
| `recon` | MSReconstructionUtilsImpl | env, model |
| `escher` | EscherUtilsImpl | env, model, biochem |
| `standardize` | ModelStandardizationUtilsImpl | env, biochem |
| `genome` | KBGenomeUtilsImpl | env, ws |
| `plm` | KBPLMUtilsImpl | env, genome |
| `bvbrc` | BVBRCUtilsImpl | env, genome, annotation |
| `reads` | KBReadsUtilsImpl | env, ws |
| `sdk` | KBSDKUtilsImpl | env, ws |
| `argo` | ArgoUtilsImpl | env |
| `curation` | AICurationUtilsImpl | env, argo |
| `thermo` | ThermoUtilsImpl | env, biochem |
| `mmseqs` | MMSeqsUtilsImpl | env |
| `skani` | SKANIUtilsImpl | env |
| `berdl` | KBBERDLUtilsImpl | env |
| `patric` | PatricWSUtilsImpl | env |
| `uniprot` | KBUniProtUtilsImpl | env |
| `pdb` | RCSBPDBUtilsImpl | env |
| `catalog` | CatalogClient | (standalone) |
| `jobs` | KBJobUtils | env |

### 6.4 NotebookSession integration

```python
# In notebook/session.py
class NotebookSession:
    @property
    def kbu(self) -> KBUtilLib:
        if self._kbu is None:
            self._kbu = KBUtilLib(env=self._env)
        return self._kbu
```

`NotebookSession` constructs `KBUtilLib` with its own `SharedEnvUtils` instance. Notebooks access utilities via `session.kbu.fba.run_fba(model)`, `session.kbu.biochem.search_compounds(...)`, etc. This matches the existing `session.cache` / `session.vectors` ergonomic from the notebook engine.

---

## 7. Test gate — pre-flight semantic smoke tests

### 7.1 Fixture

Reuse the `mini_model` fixture pattern from `tests/notebook/helpers/conftest.py`. All tests use a common `SharedEnvUtils` configured with `config_file=False` (no file discovery) and a test KBase token when available.

### 7.2 Per-module invariants

| Module | Test invariant | Marker |
|--------|----------------|--------|
| `MSBiochemUtilsImpl` | `get_compound_by_id("cpd00001")` returns compound with name containing "H2O" or "Water" | — |
| `MSBiochemUtilsImpl` | `search_compounds("glucose")` returns results including "cpd00027" | — |
| `MSBiochemUtilsImpl` | `reaction_directionality_from_bounds(reversible_rxn)` returns `"="` | — |
| `MSFBAUtilsImpl` | FBA on `mini_model` with `bio1` objective produces flux > 0 | — |
| `MSFBAUtilsImpl` | `run_fva` on `mini_model` produces N reactions with nonzero ranges | — |
| `MSFBAUtilsImpl` | `analyzed_reaction_objective_coupling` on `mini_model` categorizes reactions as essential/reduced/dispensable | — |
| `KBModelUtilsImpl` | `_check_and_convert_model(cobra_model)` returns `MSModelUtil` | — |
| `ModelStandardizationUtilsImpl` | `model_standardization(mini_model)` runs without error | — |
| `ThermoUtilsImpl` | `get_compound_deltag("cpd00001")` returns a float or None | — |
| `EscherUtilsImpl` | `list_available_maps()` returns a non-empty list | — |
| `KBWSUtilsImpl` | `ws_client` constructs without error given valid token | `@pytest.mark.kbase` |
| `KBWSUtilsImpl` | `is_ref("12345/6/7")` returns True, `is_ref("foo")` returns False | — |
| `KBGenomeUtilsImpl` | `reverse_complement("ATCG")` returns `"CGAT"` | — |
| `KBGenomeUtilsImpl` | `translate_sequence("ATGATGATG")` returns expected amino acid string | — |
| `KBAnnotationUtilsImpl` | `translate_term_to_modelseed(term)` returns ModelSEED ID for known term | — |
| `MMSeqsUtilsImpl` | Constructor succeeds when mmseqs2 is available | — |
| `SKANIUtilsImpl` | Constructor succeeds when skani is available | — |
| `KBUtilLib` | Facade `kbu.fba` returns `MSFBAUtilsImpl` instance | — |
| `KBUtilLib` | `kbu.biochem` is same object on second access (lazy singleton) | — |

### 7.3 KBase-dependent tests

Tests requiring a real KBase token are marked `@pytest.mark.kbase` and skipped in CI. These cover: `ws.get_object`, `genome.get_msgenome`, `model.get_model`, `annotation.get_annotation_ontology_events`.

---

## 8. Implementation sequence — AgentForge task chain

### Task 1: Pre-flight smoke tests

**Scope**: Write the semantic smoke tests from section 7 against the *current* public surface. Tests must pass on the current multi-inheritance codebase. This locks the behavioral contract before refactoring.

**Role**: developer
**Flags**: `--auto-merge --auto-review`
**Timeout**: 1800s
**Files touched**: `tests/test_composition_smoke.py`, `tests/conftest.py` (shared fixtures)
**Gate**: All tests pass on current code.

### Task 2: The rewrite

**Scope**: Implement all `*Impl` classes, flat modules (`model_directionality.py`, `model_helpers.py`, `compartments.py`), and the `KBUtilLib` facade. Convert each module from inheritance to composition. Update `__init__.py` exports. Delete `notebook_utils.py` and `examples.py`. Fix all P0 bugs identified in the audit (NameErrors in `kb_genome_utils`, `kb_model_utils.model_reaction_directionality_analysis`, `ms_fba_utils` bugs). All pre-flight smoke tests pass on the new code.

**Role**: developer
**Flags**: `--auto-merge --auto-review`
**Timeout**: 1800s
**Depends on**: Task 1 merged
**Files touched**: Every module listed in section 3, plus new flat modules and the facade class
**Gate**: `pytest tests/test_composition_smoke.py` passes. No import errors. `KBUtilLib()` constructs without error.

### Task 3: Cleanup and integration (optional)

**Scope**: Update `NotebookSession.kbu` integration. Clean up `__init__.py` to export both legacy names (for transition) and new `KBUtilLib` facade. Update CLI entry points if any reference old class names. Verify downstream repos can import `from kbutillib import KBUtilLib`.

**Role**: developer
**Flags**: `--auto-merge --auto-review`
**Timeout**: 1200s
**Depends on**: Task 2 merged
**Gate**: `python -c "from kbutillib import KBUtilLib; kbu = KBUtilLib()"` succeeds.

---

## 9. Migration plan for downstream consumers

### 9.1 Identified consumers

| Consumer | Usage pattern | Breaking changes | Migration hint |
|----------|--------------|------------------|----------------|
| **ADP1Notebooks** | `util.py` god class inherits from `MSFBAUtils + AICurationUtils + NotebookUtils + KBPLMUtils + EscherUtils` | All — class hierarchy gone | Archive all notebooks → collaborative rebuild against `session.kbu.*`. OUT OF SCOPE for this PRD. |
| **KBModelAgent** | `from kbutillib import MSFBAUtils, KBModelUtils` + direct instantiation | Class names change to `*Impl`; constructor signature changes | `from kbutillib import KBUtilLib; kbu = KBUtilLib(); kbu.fba.run_fba(...)` |
| **KBDatalakeApps** | `from kbutillib import KBWSUtils, KBGenomeUtils` | Constructor changes | `kbu = KBUtilLib(); kbu.ws.get_object(...)` |
| **MeetingAIAssistant** | `from kbutillib import ArgoUtils, AICurationUtils` | Constructor changes | `kbu = KBUtilLib(); kbu.curation.chat(...)` |
| **cb_annotation_ontology_api** | `from kbutillib import KBAnnotationUtils` used as SDK callback client | Constructor + callback wiring changes | `KBAnnotationUtilsImpl(env, ws, callback)` — the callback client is now an explicit dep |

### 9.2 Transition strategy

1. **KBUtilLib `__init__.py`** re-exports legacy class names as aliases during transition: `MSFBAUtils = MSFBAUtilsImpl` etc. This provides import compatibility but NOT constructor compatibility (signatures change).
2. **Downstream repos** are updated in separate PRs after the refactor lands. Each is a small change: replace inheritance-based instantiation with facade-based instantiation.
3. **ADP1Notebooks** follows the notebook transition recipe (out of scope for this PRD): archive all → rebuild against `session.kbu.*`.

---

## 10. Rollback plan

If Task 2 lands and downstream breaks are worse than expected:

1. **KBUtilLib itself is internally consistent** — the pre-flight smoke tests (Task 1) pass on the new code, confirming behavioral equivalence.
2. **Revert plan**: `git revert <merge-commit>`. The smoke tests revert with it. All consumers go back to working state.
3. **Risk mitigation**: Task 1 lands separately and is reviewable before the refactor. The smoke tests serve as the regression safety net. If they pass before and after, the refactor is semantically correct.
4. **Downstream consumer breakage** is expected and planned — each consumer has a noted migration path. The refactor is authorized to break consumers; their updates are tracked as follow-up tasks.

---

## 11. Resolved decisions (audit trail)

1. **Class name**: `KBUtilLib` (matches package name).
2. **Naming convention**: `*Impl` suffix for composed classes (e.g., `MSFBAUtilsImpl`). The facade uses short attribute names (`fba`, `biochem`, `recon`).
3. **Logger pattern**: module-level `logging.getLogger(__name__)` (matching KBJobUtils pattern), not `self.env.logger`.
4. **`_parse_id` canonical home**: `model_helpers.py`.
5. **`compartment_types` canonical home**: `compartments.py` (the complete version from `ms_biochem_utils`).
6. **AP3 carve-outs**: `run_fva`, `analyzed_reaction_objective_coupling`, `fit_flux_to_mutant_growth_rate_data` preserved in `MSFBAUtilsImpl`.
7. **Refactor style**: big-bang, not phased.
8. **Back-compat**: none required. Legacy names exported as aliases for import compatibility only.
9. **Notebook transition**: out of scope. Tracked separately in ADP1Notebooks.
10. **`kbase_endpoints.py`**: already extracted, no further action.
11. **`kb_model_utils.py.bak`**: already deleted by task-40d03085.

---

## 12. Open considerations (not blocking, noted for follow-up)

- **Post-refactor boundary refactor**: Once composition lands, method placement questions (`save_solution_as_fba` in model vs fba, etc.) become trivial method moves between composed building blocks. Track as follow-up.
- **`kb_callback_utils.py` infrastructure**: Callback service needs to be built and tested on poplar before this module can be fully validated.
- **AI curation cache location**: Moving from `KBUtilLib/data/` (committed to repo) to `~/.kbutillib/ai_cache/` (user-local) is a behavior change. Confirm with Chris.
- **`model_standardization_utils` parallel PRD**: A separate PRD exists at `agent-io/prds/model-standardization-refactor/`. Coordinate to avoid conflicts.
