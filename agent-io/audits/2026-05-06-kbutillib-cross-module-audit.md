# KBUtilLib Cross-Module Audit — 2026-05-06

Audit covers `src/kbutillib/*.py` (excluding `notebook/`, `installed_clients/`, `cli/`).
Total: 30 source modules, ~20.4k lines of code.

## Executive summary

- **30 modules audited** (28 top-level + 2 secondary classes in kb_reads_utils, patric_ws_utils, ai_curation_utils stack).
- **Antipatterns found**: AP3 (algorithm reimplementations) is rampant — ms_fba_utils, kb_genome_utils, ai_curation_utils, ms_biochem_utils, model_standardization_utils all do significant in-class reinvention. AP1 (`GROWTH_DASH_RXN`) is gone from the library (only `bio1` defaults remain). AP2 (missing media) is mostly absent — methods route through `set_media`. AP5 (`.values()` on Series) is absent.
- **Top 5 punch-list items by priority**:
  1. **P0** — Delete `kb_model_utils.py.bak` (1611 LoC, fully obsolete).
  2. **P0** — Fix three `NameError`-grade bugs in `kb_model_utils.py` (`direction_conversion` undefined inside `model_reaction_directionality_analysis`), `ms_fba_utils.py` (`printlp` on cobra model + `EXC_temp_` typo + `MSModelUtil.from_cobrapy(json_string)` API misuse), and `kb_genome_utils.py` (`Path`, `hashlib`, `defaultdict`, `datetime`, `Counter` referenced but never imported in `load_genome_from_local_files`/`create_synthetic_genome`/`aggregate_taxonomies`).
  3. **P0** — Reconcile duplicated `compartment_types` constants in `ms_biochem_utils.py:14-28` vs `model_standardization_utils.py:19-33` (the latter is missing `m`, `mitochondria`, `membrane`, `extracellular`, `environment`, `env`); `kb_model_utils.py:13` imports the *narrower* version, so periplasm/mitochondrial reactions are mis-classified.
  4. **P1** — Resolve the `_parse_id` triplicate (`kb_model_utils._parse_id` lines 126-163, `ms_biochem_utils._parse_id` lines 429-475, `thermo_utils._parse_id` lines 39-64) — three drift-prone copies of the same parser, none identical.
  5. **P1** — `ms_fba_utils.run_fva` (lines 86-97) reimplements FVA via per-reaction MIN/MAX `slim_optimize` instead of calling `cobra.flux_analysis.flux_variability_analysis` (already imported at line 9 but never used). This is a direct AP3 violation.
- **Highest-value redesign candidate**: **`ms_fba_utils.py`** — clearest case of AP3 across the board (FVA reimpl, exchange-unblock with bugs, expression-fitting wrapper that calls into `MSExpression.fit_flux_to_mutant_growth_rate_data` but bookkeeps everything by hand). Deserves its own /ai-design sub-session focused on "what should canonically live in `MSFBAUtils` vs ModelSEEDpy MSFBA/MSModelUtil".
- **Quickest wins**:
  - Remove `kb_model_utils.py.bak`.
  - Remove or fix `examples.py` (calls non-existent methods `parse_genome_object`, `extract_features_by_type`, `kb_environment`, `save_json` — see lines 87, 88, 143, 148; module is already commented out of `__init__.py` import).
  - Update stale `tests/README.md` (claims `test_base_utils.py`, `test_shared_environment.py`, `test_integration.py`, `test_main.py` exist — none do).
  - Delete unused `from unittest import result` (kb_model_utils.py:5, kb_model_utils.py.bak:5).

---

## Per-module findings

### `__init__.py`
- **205 LoC**. Exposes 30+ classes via try/except optional imports.
- Bucket: **C** (healthy).
- Note: `examples` is commented out (lines 162-167) — unused public symbol. Could remove from `__all__`.

### `__main__.py`
- **6 LoC**. CLI entry-point shim. Healthy.
- Bucket: **C**.

### `base_utils.py`
- **209 LoC**, single class `BaseUtils`.
- Provides logging, provenance bookkeeping (`initialize_call`, `reset_attributes`), arg validation, plus a JSON file save/load (`save_util_data`/`load_util_data`).
- Smells:
  - `script_dir+"/../../"` (line 41) — fragile path computation if package is installed editable vs site-packages.
  - `from genericpath import exists` (line 9) — unusual; prefer `os.path.exists`.
  - Constructor mutates `self.version` twice (line 39 set to `"0.0.0"`, then `reset_attributes` resets it to `"Unknown"` line 57).
- Test coverage: **none**.
- Bucket: **B** (tactical cleanup — clean up path/imports, add minimal unit test).

### `shared_env_utils.py`
- **612 LoC**, single class `SharedEnvUtils(BaseUtils)`.
- Three concerns layered: config-file discovery, token loading, env-var loading + dependency manager.
- Smells:
  - `read_token_file` (line 322) logs `f"Loaded {len(token_hash)} tokens from {token_file}"` *before* tokens have been loaded (line 335) — hash is empty when the message prints.
  - `set_environment_variable` is misleading — it sets a *config* value, not an env var (line 248).
  - `get_config(section, key)` is documented deprecated (line 198) but `kb_model_utils.py:57-64` still uses it.
- Test coverage: **none directly**, exercised indirectly through other modules.
- Bucket: **B** (tactical: rename misleading method, fix log timing, deprecate `get_config` use sites, add tests).

### `dependency_manager.py`
- **199 LoC**, `DependencyManager`. Resolves external dependency paths from `~/.kbutillib/dependencies.yaml`.
- Healthy, single responsibility, no obvious antipatterns.
- Test coverage: **none**.
- Bucket: **C**.

### `kb_ws_utils.py`
- **654 LoC**, `KBWSUtils(SharedEnvUtils)`.
- Solid wrapper around KBase Workspace + Shock + handle service.
- Notable: `ws_get_objects` (line 339) has retry logic — good.
- Smells:
  - `set_provenance`/`get_provenance` (line 241) duplicates the provenance bookkeeping that `BaseUtils.initialize_call` already sets up.
  - `register_typespec_dryrun` and friends (lines 556-654) are admin operations that don't really belong in the same module as object reads.
- Test coverage: **`tests/test_kb_ws_utils.py` (268 LoC)**.
- Bucket: **B** (tactical cleanup — split typespec admin into separate module or accept as-is; reconcile provenance with BaseUtils).

### `kb_callback_utils.py`
- **260 LoC**, `KBCallbackUtils(KBWSUtils)`.
- TODO comment line 13 says "Need to write the callback service and run it on poplar, then write and run tests". This module is essentially un-tested infrastructure.
- Bug: `Path(self._callback_directory).parent()` (line 62) — `Path.parent` is a property, not a method; this will `TypeError`.
- Bug: `self.set_config(...)` (line 69) — `SharedEnvUtils` has no `set_config` method.
- Bucket: **B** (tactical fix; add tests once callback infrastructure exists).

### `kb_genome_utils.py`
- **770 LoC**, `KBGenomeUtils(KBWSUtils)`.
- **AP3 reimplementations**:
  - `reverse_complement` (line 256) and `translate_sequence` (line 273) hand-roll DNA ops; Biopython does this. Fine for vendoring if we want zero deps, but should be documented.
  - `_parse_fasta` (line 526) — naive FASTA parser; Biopython has `SeqIO.parse`.
  - `aggregate_taxonomies` (line 548), `create_synthetic_genome` (line 615) — heavy bespoke logic that probably belongs in BVBRC/synthetic-genome territory, not generic genome utils.
- **NameError-grade bugs (P0)**:
  - `Path` used at line 371, 606 — never imported (only `from os.path import exists` at line 4).
  - `hashlib` used at lines 413, 715, 730, 765 — never imported.
  - `defaultdict` used at line 436 — never imported.
  - `datetime` used at lines 471-472 — never imported.
  - `Counter` used at line 590 — never imported.
  - These code paths will crash with `NameError` the first time they're executed. Either dead code (likely) or never-executed.
- `genetic_code_standard` (lines 10-75) is a 65-line constant; consider moving to a constants file or using Biopython's `Bio.Data.CodonTable`.
- Test coverage: **none directly**.
- Bucket: **A** (real redesign; bugs imply this code never runs; clarify what's keeper vs cruft).

### `kb_annotation_utils.py`
- **1016 LoC**, `KBAnnotationUtils(KBCallbackUtils)`. Largest single mixin.
- Implements KBase Annotation Ontology API (event ingestion, term translation, alias hashing). Heavy domain logic.
- Smells:
  - `__init__` reads multiple data files via `Path.read` (`FilteredReactions.csv` line 78-81) — side-effecting constructor that breaks if `cb_annotation_ontology_api` isn't found. Uses fallback at lines 60-62 of `kb_annotation_utils.py` — if that path doesn't exist either, constructor will throw on `pd.read_csv`.
  - `add_ontology_events` (line 289) and `add_annotation_ontology_events` (line 339) have similar names but different semantics — confusing.
  - `convert_role_to_searchrole` (line 959) and `translate_rast_function_to_sso` (line 968) — small helpers that could move to a `helpers/` submodule or be tested in isolation.
- Test coverage: **none**.
- Bucket: **A** (redesign — large, untested, brittle constructor; needs careful decomposition).

### `kb_model_utils.py`
- **712 LoC**, `KBModelUtils(KBAnnotationUtils, MSBiochemUtils)`. Inherits annotation + biochem.
- **NameError-grade bug (P0)**: `model_reaction_directionality_analysis` (line 689) builds `output[rxn.id]["combined"]` referencing `direction_conversion`, `model_direction`, `ai_direction`, `biochem_direction` — none of which are defined in scope (lines 702-706). The only `direction_conversion` import in this file is via `compartment_types` from `model_standardization_utils` (line 13), and `model_direction`/`ai_direction`/`biochem_direction` are never bound. This method cannot run.
- `_check_and_convert_model` (line 120) duplicates similar logic in `escher_utils.py:1453-1454`.
- `_import_modules` (line 67) sets `self.cobrakbase`, `self.MSModelUtil`, etc. as instance attributes — implicit class-level state shared across mixins. Hidden cross-mixin coupling.
- `_msrecon` lazy property (lines 42-65) instantiates ModelSEEDReconstruction with `clients={"cb_annotation_ontology_api":self}` — passing self as a client. Risky duck-typing.
- `kbase_api = self.cobrakbase.KBaseAPI()` immediately mutates `os.environ["KB_AUTH_TOKEN"]` (line 37) — global side effect on construction.
- Test coverage: **none directly**.
- Bucket: **A** (redesign; bug fix urgent; resolve coupling; remove os.environ mutation).

### `kb_model_utils.py.bak`
- **1611 LoC**. Marked as `.bak`. Contains stale duplicate of `compartment_types` and `direction_conversion`. Predates the split into `model_standardization_utils.py`.
- Bucket: **D** (delete; verified no live references — only own SOURCES.txt and prds reference it).

### `model_standardization_utils.py`
- **1096 LoC**, `ModelStandardizationUtils(MSBiochemUtils)`.
- Defines its own `compartment_types` (lines 19-33) and `direction_conversion` (lines 35-42) — duplicates of the same constants in `ms_biochem_utils.py`. Worse: this `compartment_types` is **missing** `m`/`mitochondria`/`membrane`/`extracellular`/`environment`/`env` keys present in `ms_biochem_utils.compartment_types`. `kb_model_utils.py:13` imports this narrower version.
- `from email.policy import default` (line 10) — unused; almost certainly an editor autocomplete typo.
- Heavy methods: `model_standardization` (line 96), `compare_model_to_msmodel` (line 121), `match_model_compounds_to_db` (line 417), `match_model_reactions_to_db` (line 504), `translate_model_to_ms_namespace` (line 747). All call into `_check_and_convert_model` and `MSBiochemUtils` search/index methods.
- AP3 risk: `match_model_reactions_to_db` reproduces logic that `MSGenomeClassifier`/`MSBuilder` already do for ModelSEED matching. Worth a /ai-design check.
- Test coverage: **none**.
- Bucket: **A** (redesign — already has a PRD at `agent-io/prds/model-standardization-refactor/`).

### `ms_biochem_utils.py`
- **1010 LoC**, `MSBiochemUtils(SharedEnvUtils)`.
- `compartment_types` constant defined here (lines 14-28).
- Has its own `_parse_id` (lines 429-475), distinct from `kb_model_utils._parse_id` (line 126) and `thermo_utils._parse_id` (line 39).
- `rxn_stoichiometry_hash` (line 147) carries the comment `#TODO: This is AI code currently - need to revise this`.
- `normalize_compound_name` (line 227) does extensive regex-based salt/hydrate stripping — niche, hand-rolled, and re-derivable from public chemical-name normalization libs (e.g., chemparse). Document that this is intentional or replace.
- AP3 risk: `search_compounds`/`search_reactions` are heuristic scoring algorithms (lines 504-712). They reinvent the matching `MSBuilder.search_for_reactions` or BiochemPy's tooling does, but specialized for hash-table indices. Probably keepers, but worth confirming they don't duplicate ModelSEEDDatabase native APIs.
- `parse_formula` defined twice — `_parse_formula` (line 368) AND `parse_formula` (line 907) with slightly different behavior. Both used.
- `find_proton_in_compartment` (line 893) does `print(...)` for debugging (line 902) — prints noise to stdout.
- Test coverage: **`tests/test_ms_biochem_deltag.py` (380 LoC)** — only tests deltaG-related parts.
- Bucket: **A** (redesign; large, central, and tightly coupled to the ModelSEEDDatabase data model).

### `ms_fba_utils.py`
- **685 LoC**, `MSFBAUtils(KBModelUtils)`.
- **Highest-priority redesign candidate.**
- AP3 violations:
  - `run_fva` (line 86) reimplements FVA per-reaction with `slim_optimize` instead of calling `cobra.flux_analysis.flux_variability_analysis` (already imported line 9, never used).
  - `analyzed_reaction_objective_coupling` (line 99) is a 130-line custom KO-reaction sweep; this is essentially what `cobra.flux_analysis.single_reaction_deletion` does, with extra "reduced/essential/dispensable" categorization that could sit on top.
  - `unblock_objective_with_exchanges` (line 271) — bespoke alternative-solution search by iterative blocking. Plausible as a unique tool, but contains real bugs.
  - `fit_flux_to_mutant_growth_rate_data` (line 405) is a 280-line wrapper that unpacks an MSExpression call — most of the body is bookkeeping that should live alongside `MSExpression.fit_flux_to_mutant_growth_rate_data`.
- **Bugs (P0)**:
  - Line 347: `model.printlp(print=True,...)` — `model` here is the *user-supplied input*, not `mdlutl`. If user passed a cobra model, this attribute doesn't exist; if `MSModelUtil`, it's been reassigned at line 294 to `mdlutl`. Use `mdlutl.printlp(...)`.
  - Line 361: `met_id = ex_rxn.id.replace("EXC_temp_", "")` — exchanges are prefixed `EX_temp_` (line 318), not `EXC_temp_`. The replace is a no-op.
  - Line 562: `MSModelUtil.from_cobrapy(cobra.io.json.to_json(model.model))` — `from_cobrapy` typically expects a *path* to a JSON file, not a JSON string. Either an API misuse or wrong serializer.
  - Line 654: `expression = MSExpression.from_dataframe(genome_or_model=genome, df=data_source, type="NormalizedRatios")` — keyword `type` shadows builtin and may not match MSExpression's actual signature (this was a known migration footgun).
- `set_media` (line 26), `set_objective_from_string` (line 38), `constrain_objective` (line 45), `constrain_objective_to_fraction_of_optimum` (line 51), `run_fba` (line 75), `configure_fba_formulation` (line 65) — these are reasonable thin wrappers around MSModelUtil package manager. Healthy.
- Test coverage: **none**.
- Bucket: **A** (redesign).

### `ms_reconstruction_utils.py`
- **1154 LoC**, `MSReconstructionUtils(KBModelUtils)`.
- Translation of KB-ModelSEEDReconstruction Perl logic into Python wrappers around `MSBuilder`, `MSGapfill`, `MSATPCorrection`, `MSModelReport`.
- Lazy imports in `_reconstruction_imports` (line 58) — sane.
- Heavy methods: `build_metabolic_model` (line 166, ~180 LoC), `kb_build_metabolic_models` (line 392, ~270 LoC), `gapfill_metabolic_model` (line 663), `kb_gapfill_metabolic_models` (line 819, ~280 LoC), `_build_dataframe_report` (line 1104).
- The two `kb_*` variants share a lot of bookkeeping with the simpler variants — they differ mainly in workspace I/O. Candidates for extracting common pipeline.
- AP3 risk: most of the body is orchestration of MSBuilder/MSGapfill calls — appropriate. Not a reimplementation, just glue.
- Test coverage: **none**.
- Bucket: **B** (tactical: extract common helpers between kb_/non-kb variants; add at least one smoke test).

### `escher_utils.py`
- **1499 LoC**, `EscherUtils(KBModelUtils, MSBiochemUtils)`. Largest module by LOC.
- Multi-inheritance — same issue noted in §1 of the redesign PRD: `EscherUtils` carries the entire kbase_api / annotation / biochem stack just to render maps.
- `create_map_html2` (line 1411) is the canonical Escher rendering method; this is the one BERDL trio's `_legacy.create_map_html2` calls. Solid.
- A lot of this module is HTML/JS injection (`_inject_numerical_badge_overlays` line 1004, `_inject_reaction_class_overlays` line 1228) — this is essentially front-end code in Python. Probably fine, but a lot of LoC for a "render" responsibility.
- Has 5 distinct map-translation helpers (`_translate_map_with_flux`, `_translate_model_with_flux`, `_translate_flux`, `_translate_reaction_classes`, `_translate_reaction_badges`). Some duplication of "look up reaction by id, decide if it should be reversed".
- Test coverage: **`tests/test_escher_utils.py` (390 LoC)** — best-tested module in the audit.
- Bucket: **B** (tactical — split overlay-injection from map-loading; reconcile inheritance so it doesn't pull in KBModelUtils).

### `ai_curation_utils.py`
- **1105 LoC**, `AICurationUtils(ArgoUtils)`.
- Two backends: Argo (parent) + Claude Code subprocess. `_chat_via_claude_code` (line 84) shells out to `claude -p PROMPT --output-format json`.
- **Memory feedback flag**: per Chris's MEMORY.md `feedback_agent_direct_llm_analysis.md`, "**worker prompts must NOT call Anthropic API or subprocess `claude`**". This module is end-user-callable from notebooks (not a worker prompt itself), so the rule doesn't strictly apply, but the backend choice is awkward — when the *caller* is already a Claude Code session, this spawns a nested Claude. Worth confirming intent.
- Methods are thin wrappers that build a long system prompt + cache results to disk via `_load_cached_curation` / `_save_cached_curation` (line 171, 176) — reasonable.
- Smells:
  - `print(self.claude_code_executable)` (line 62) — debug print left in.
  - `_load_cached_curation` uses `load_util_data` from `BaseUtils` — but `BaseUtils.data_directory` is `script_dir+"/../../"+"/data/"` which is the `KBUtilLib/data/` source-tree directory. AI cache files end up *committed to the repo* (e.g., `data/AICurationCacheReactionStoichiometry.json` exists in the repo). Side effect of `save_util_data`.
- Optional cobra import at lines 11-17 (`HAS_COBRA = True/False`) — unused in the file (no references to `HAS_COBRA` or `pfba` after import).
- Test coverage: **none**.
- Bucket: **A** (redesign — clarify backend intent and where caches live).

### `argo_utils.py`
- **421 LoC**, `ArgoUtils(SharedEnvUtils)`.
- Wraps Argo (Anthropic-hosted ANL LLM service) via httpx. `chat`, `_payload`, `_poll_for_result`, `ping`.
- Plus standalone helpers `_parse` (line 380), `llm_label` (line 412) for label extraction.
- AP3: low. The HTTP-mechanics is genuinely the function of this module.
- `O_SERIES_TIMEOUT = 120.0` and friends are reasonable constants.
- Test coverage: **none**.
- Bucket: **C** (healthy — leave alone).

### `kb_plm_utils.py`
- **804 LoC**, `KBPLMUtils(KBGenomeUtils)`. Wraps KBase Protein Language Model API + BLAST.
- Methods: `query_plm_api`, `query_plm_api_batch`, `get_uniprot_sequences`, `create_blast_database`, `run_blastp`, `find_best_hits_for_features`, `get_best_uniprot_ids`.
- Inherits from `KBGenomeUtils` so picks up the same `Path`/`hashlib`/etc. import-bug surface (but probably never triggers since this module's call paths don't exercise those methods).
- Heavy `subprocess` use for BLAST (lines 51-74, 458, etc.) — the right approach.
- Test coverage: **`tests/test_kb_plm_utils.py` (260 LoC)**.
- Bucket: **C** (healthy modulo inheritance concern).

### `kb_uniprot_utils.py`
- **651 LoC**, `KBUniProtUtils(BaseUtils)`. Direct wrapper around UniProt REST.
- Methods are well-scoped: get_uniprot_entry, get_protein_sequence, get_annotations, get_publications, get_rhea_ids, get_pdb_ids, get_uniref_ids, get_uniprot_info, get_batch_uniprot_info.
- AP3: minimal. Genuinely thin REST wrapper.
- Test coverage: **none**.
- Bucket: **C** (healthy; add tests if it sees more use).

### `mmseqs_utils.py`
- **514 LoC**, `MMSeqsUtils(SharedEnvUtils)`. Subprocess wrapper around `mmseqs easy-cluster`.
- Clean separation; methods: cluster_proteins, easy_cluster, get_cluster_representatives, get_cluster_membership, _write_fasta, _parse_cluster_tsv.
- Test coverage: **`tests/test_mmseqs_utils.py` (419 LoC)**.
- Bucket: **C**.

### `skani_utils.py`
- **800 LoC**, `SKANIUtils(SharedEnvUtils)`. Subprocess wrapper around `skani` for genome distance.
- Methods: sketch_genome_directory, add_skani_database, query_genomes, list_databases, get_database_info, remove_database, compute_pairwise_distances.
- Has its own JSON cache at `~/.kbutillib/skani_databases.json` (lines 107-135).
- Cleanly separable from MMSeqs: skani works on nucleotide genomes, mmseqs on protein clusters. Not duplicated, despite the audit prompt's clustering hint.
- Test coverage: **none**.
- Bucket: **C** (add tests).

### `bvbrc_utils.py`
- **462 LoC**, `BVBRCUtils(KBGenomeUtils, KBAnnotationUtils)`.
- Methods: fetch_genome_metadata, fetch_genome_sequences, fetch_genome_features, fetch_feature_sequences, build_kbase_genome_from_api, _convert_bvbrc_feature.
- Multi-inheritance: pulls in both KBGenomeUtils and KBAnnotationUtils — unusual for a fetch-only module. Annotation parent gives access to `add_annotations_to_object` etc., but BVBRC doesn't use those.
- Test coverage: **none**.
- Bucket: **B** (tactical — narrow inheritance, add tests).

### `patric_ws_utils.py`
- **744 LoC**, two classes: `PatricWSClient` (low-level RPC) + `PatricWSUtils(SharedEnvUtils)` (high-level).
- Clean two-layer design. Mirrors KBase Workspace API but for PATRIC/BV-BRC.
- AP3 / overlap risk vs `kb_ws_utils.py`: *some* — both have `save_object`, `get_object`, `list_objects`, `copy_object`, `delete_object`. Different services, so inevitable, but a shared `WorkspaceProtocol` interface might be worthwhile if both are used in the same workflows. Currently no shared abstraction.
- Used by KBUtilLib's own `notebooks/` but not by ADP1Notebooks.
- Test coverage: **none**.
- Bucket: **B** (add tests; consider abstraction with kb_ws_utils).

### `rcsb_pdb_utils.py`
- **598 LoC**, `RCSBPDBUtils(BaseUtils)`. Wraps RCSB PDB REST API for sequence/EC-based hit retrieval.
- Methods: query_rcsb_with_sequence, query_rcsb_metadata_by_id, query_rcsb_with_proteins, _parse_entry_metadata, _parse_polymer_entity, _parse_nonpolymer_entity, _filter_hits_by_ec.
- Inherits from `BaseUtils` directly (not SharedEnvUtils) — minimal dependencies. Good.
- Test coverage: **none**.
- Bucket: **C**.

### `kb_reads_utils.py`
- **1332 LoC**, classes `Reads`, `ReadSet`, `Assembly`, `AssemblySet`, `KBReadsUtils(KBWSUtils)`.
- Largest collection of dataclass-like containers in the library. Each has `to_dict`/`to_json`/`from_dict`/`from_json` — could collapse to dataclasses + a single `JsonSerializable` mixin.
- Heavy upload/download methods: upload_reads (515), download_reads (598), bulk variants (699, 727), upload_assembly (1058), download_assembly (911) — lots of subprocess + Shock plumbing.
- Test coverage: **none**.
- Bucket: **B** (tactical — collapse repeated to_dict/to_json pattern; add a smoke test).

### `kb_sdk_utils.py`
- **66 LoC**, `KBSDKUtils(KBWSUtils)`.
- Single method `build_dataframe_report` (line 28) with embedded HTML.
- **Bug**: line 53 references `json` (not imported), line 62 references `os` (not imported), line 62 references `self.working_dir` (never defined). Method cannot run as written.
- This module is essentially dead — `KBSDKUtils` exists primarily as a class hierarchy node for `examples.KBaseSDKWorkflow`.
- Test coverage: **none**.
- Bucket: **B** (fix imports and `working_dir` plumbing or delete and put `build_dataframe_report` somewhere it's actually exercised).

### `kbase_catalog_client.py`
- **601 LoC**, two classes: `CatalogError`, `CatalogClient` plus `register_module` helper.
- Talks to KBase Catalog (module registration). Standalone — no other module imports it.
- Healthy single-purpose code.
- Test coverage: **none**.
- Bucket: **C**.

### `kb_berdl_utils.py`
- **863 LoC**, `KBBERDLUtils(SharedEnvUtils)`. SQL-over-Spark client for BERDL data lake.
- Methods: query, get_database_list, get_database_tables, get_table_columns, get_database_schema, paginate_query, test_connection, get_genometables_from_kbase.
- Test coverage: **`tests/test_kb_berdl_utils.py` (551 LoC)** — well-tested.
- Bucket: **C**.

### `notebook_utils.py`
- **857 LoC**, classes `NumberType`, `DataType`, `DataObject` (dataclass), `NotebookUtils(BaseUtils)`.
- **Marked deprecated** in module docstring (line 3-7) — superseded by `kbutillib.notebook.NotebookSession`.
- Still used by KBUtilLib's own notebooks (`notebooks/util.py:18`) and ADP1Notebooks `util_legacy.py`. Cannot delete yet.
- Has the legacy `save`/`load` flow + `display_dataframe`/`display_json`/etc. UI helpers + JSON-only serialization (the limitations the redesign called out).
- Test coverage: **none directly**, exercised via notebook smoke tests.
- Bucket: **B** (tactical — add deprecation warnings on call; plan removal once ADP1Notebooks `util_legacy.py` is retired in Phase 5).

### `examples.py`
- **150 LoC**, six demonstrative composite classes.
- Already commented out of `__init__.py` import (lines 162-167). Nothing imports it.
- **Multiple `AttributeError` bugs**: `parse_genome_object` (line 87), `extract_features_by_type` (line 88), `kb_environment` (line 143), `save_json` (line 148) — none of these methods exist on the inheritance chain. Code is broken.
- Bucket: **D** (delete — broken AND unused AND already commented out).

### `thermo_utils.py`
- **356 LoC**, `ThermoUtils(SharedEnvUtils)`.
- Lazy-loads `MSBiochemUtils` via property (line 27) — composition rather than inheritance. **Architecturally cleaner** than most other modules in the library.
- Has its own `_parse_id` (line 39) — third copy.
- Methods: get_compound_deltag, calculate_reaction_deltag, compute_ion_transfer.
- Test coverage: **`tests/test_ms_biochem_deltag.py` (380 LoC)** — covers the deltaG flow including this module.
- Bucket: **C** (a model for what other modules could look like: composition over inheritance).

---

## Cross-cutting findings

### Module overlap clusters

**Cluster 1 — Model/FBA/reconstruction (large overlap)**: `kb_model_utils.py` (KBase API + model loading), `ms_fba_utils.py` (FBA wrappers), `ms_reconstruction_utils.py` (build/gapfill orchestration), `model_standardization_utils.py` (namespace translation). All four share the `_check_and_convert_model` pattern, and the directionality/compartment constants are spread across three of them. Recommendation: a `_model_core.py` with `compartment_types`, `direction_conversion`, `_parse_id`, `_check_and_convert_model` lifted to module-level functions; the four modules then become focused on their distinct concerns.

**Cluster 2 — Workspace clients**: `kb_ws_utils.py` (KBase) and `patric_ws_utils.py` (BV-BRC/PATRIC). Different services, but the public surface (save_object, get_object, list_objects, copy/delete, get_ref) is parallel. Recommendation: define a `WorkspaceClient` Protocol in a shared location; let the two implement it. Low priority since they're called from disjoint workflows today.

**Cluster 3 — Annotation**: `kb_annotation_utils.py` (genome ontology events) vs `ai_curation_utils.py` (LLM-assisted reaction curation). They're complementary, not duplicative — the AI curation tools could *call into* annotation utils to merge results. Currently no overlap. Recommendation: leave the split; document the boundary.

**Cluster 4 — Sequence comparison**: `skani_utils.py` (genome ANI) and `mmseqs_utils.py` (protein clustering). Different problem spaces; not duplicative. Recommendation: leave alone.

**Cluster 5 — Genome-source ingest**: `kb_genome_utils.py`, `kb_plm_utils.py`, `bvbrc_utils.py`. KBPLMUtils inherits from KBGenomeUtils; BVBRCUtils inherits from both KBGenomeUtils AND KBAnnotationUtils. The `kb_genome_utils._parse_fasta`, `_create_cds_features`, `_convert_local_feature` look like they belong in `bvbrc_utils.py` (only BVBRC populates the local-files flow). Recommendation: move the BV-BRC-specific helpers out of KBGenomeUtils.

**Cluster 6 — Notebook (legacy vs redesign)**: `notebook_utils.py` (deprecated, 857 LoC) vs `notebook/` package (Phase 4 just landed). Already on the deprecation path; no action needed beyond eventual removal once `util_legacy.py` consumers move off.

### Shared infrastructure smells

- **Constants duplication**: `compartment_types` defined in 2 modules with different content (`ms_biochem_utils.py:14-28` complete vs `model_standardization_utils.py:19-33` narrow). `direction_conversion` defined in `model_standardization_utils.py:35-42` and `kb_model_utils.py.bak:25-32`. **`_parse_id` defined three times** — `kb_model_utils.py:126`, `ms_biochem_utils.py:429`, `thermo_utils.py:39`. Each is slightly different.
- **Side-effecting constructors**: `KBModelUtils.__init__` mutates `os.environ["KB_AUTH_TOKEN"]` (line 37). `KBAnnotationUtils.__init__` reads CSV from disk in the constructor (line 78-81). `MSBiochemUtils.__init__` calls `_ensure_database_available` which loads the entire ModelSEED biochemistry database (line 82). All three make the classes hard to test in isolation.
- **Hidden cross-mixin coupling**: `KBModelUtils._import_modules` (line 67-92) sets `self.MSModelUtil`, `self.MSFBA`, etc. as instance attrs. `MSFBAUtils` and `EscherUtils` use `self.MSModelUtil` even though it's bound by a sibling class — the redesign PRD §1 calls this out exactly. `self.kbase_api` (line 38) is similarly accessed by sibling mixins.
- **Auth handling**: `SharedEnvUtils.get_token("kbase")` is the canonical accessor. `KBModelUtils` puts the token in `os.environ`. `KBWSUtils` passes it to clients. `KBBERDLUtils._get_headers` (line 126) builds Bearer headers manually. Three patterns — no glaring inconsistency, but worth documenting.
- **Logging**: `BaseUtils._setup_logger` is consistent (line 100). But `model_standardization_utils.py:79` and several others sprinkle `print(...)` calls — uncontrolled output. Counted at least 6 stray `print` calls in code paths (e.g., `kb_model_utils.py:583`, `ms_biochem_utils.py:902`, `ms_fba_utils.py:102, 129, 185, 347`, `model_standardization_utils.py:79, 82, 87, 89, 91`).
- **Error handling**: Most modules `raise` informative exceptions. A few swallow with `try/except: pass` (e.g., `ms_biochem_utils.get_compound_by_id` line 723-726). Inconsistent — some bare `except:`, some specific.
- **MSPackageManager glue**: The pattern `model.pkgmgr.getpkg("XYZPkg").build_package(args)` is used in `ms_fba_utils.py` (lines 35, 43, 49, 122, 241, 243), `ms_reconstruction_utils.py:797-798`. Consistent and correct, but undocumented in any /context skill — newcomers won't know the package names.
- **Token plumbing**: `SharedEnvUtils.get_token(namespace)` is called from at least 8 modules. Healthy.
- **Mutable default arguments**: `kb_genome_utils.py:171` has `def alias_to_ftrs(self, name, alias)` — fine. But `kb_ws_utils.py:241` has `def set_provenance(..., input_objects=[], params={})` — classic Python footgun. Several others use `defaults: Optional[List[X]] = []` instead of `None`. Counted in `bvbrc_utils.py`, `ms_biochem_utils.py`, `ms_reconstruction_utils.py`.
- **Test-coverage map**:
  - `tests/test_escher_utils.py` (390 LoC) — covers escher.
  - `tests/test_kb_berdl_utils.py` (551 LoC) — covers berdl.
  - `tests/test_kb_plm_utils.py` (260 LoC) — covers plm.
  - `tests/test_kb_ws_utils.py` (268 LoC) — covers ws.
  - `tests/test_mmseqs_utils.py` (419 LoC) — covers mmseqs.
  - `tests/test_ms_biochem_deltag.py` (380 LoC) — covers deltaG (parts of ms_biochem + thermo).
  - `tests/cli/` (init-notebook, machine) — covers CLI bootstrap.
  - `tests/notebook/` (cache, catalog, schema, etc.) — covers the new notebook package.
  - **Zero coverage for**: base_utils, shared_env_utils, dependency_manager, kb_callback_utils, kb_genome_utils, kb_annotation_utils, kb_model_utils, ms_fba_utils, ms_reconstruction_utils, model_standardization_utils, ai_curation_utils, argo_utils, kb_uniprot_utils, kb_reads_utils, kb_sdk_utils, kbase_catalog_client, bvbrc_utils, patric_ws_utils, rcsb_pdb_utils, skani_utils, notebook_utils, examples. The largest, most central modules (kb_model_utils, ms_fba_utils, kb_annotation_utils, ms_reconstruction_utils, model_standardization_utils) are entirely untested.
- **`tests/README.md` is stale**: claims `test_base_utils.py`, `test_shared_environment.py`, `test_integration.py`, `test_main.py` exist (they don't).

### Composition vs. inheritance

The library is built on multi-inheritance — `EscherUtils(KBModelUtils, MSBiochemUtils)`, `BVBRCUtils(KBGenomeUtils, KBAnnotationUtils)`, etc. — and projects build their `util.py` on top of stacks like `MSFBAUtils + AICurationUtils + NotebookUtils + KBPLMUtils + EscherUtils`. The orthogonality assumption is leaky:

- `KBModelUtils` mutates `os.environ` and instantiates a `KBaseAPI` in its constructor. `MSFBAUtils` (subclass) needs that.
- `MSFBAUtils.set_media` (line 26) calls `self.MSMediaUtil(media)` — `MSMediaUtil` is bound by `KBModelUtils._import_modules` only if MSMedia is found. Subtle binding.
- `model.printlp` in `MSFBAUtils.unblock_objective_with_exchanges:347` assumes `model` is `MSModelUtil`-like, but the parameter could be cobra.

`thermo_utils.py` is the one example of clean *composition* (lazy `biochem_utils` property). It's cleaner. Pattern worth replicating.

---

## Prioritized punch list

### P0 — Most urgent

1. **Delete `kb_model_utils.py.bak`**
   - Scope: Remove the 1611 LoC `.bak` file from the repo. Git already preserves history. No live imports.
   - Bucket: D (delete).
   - Handler: One-line commit by Chris.
   - Effort: S.

2. **Fix `direction_conversion` undefined in `kb_model_utils.model_reaction_directionality_analysis`**
   - Scope: Lines 702-707 of `kb_model_utils.py` reference `direction_conversion`, `model_direction`, `ai_direction`, `biochem_direction` — none in scope. The method cannot run. Either import `direction_conversion` from `model_standardization_utils` and add proper variable assignments, or delete the broken `combined` field.
   - Bucket: B (tactical cleanup).
   - Handler: AgentForge developer task — small, scoped.
   - Effort: S.
   - Dependencies: None.

3. **Fix `ms_fba_utils.unblock_objective_with_exchanges` bugs**
   - Scope: (a) Line 347 `model.printlp` — change `model` to `mdlutl` (the converted MSModelUtil). (b) Line 361 `EXC_temp_` typo — change to `EX_temp_`. (c) Line 562 `MSModelUtil.from_cobrapy(json_string)` — confirm signature in ModelSEEDpy and use the correct constructor. Add a smoke test that the method runs on a tiny model.
   - Bucket: B.
   - Handler: AgentForge developer task.
   - Effort: S-M.
   - Dependencies: None.

4. **Fix missing imports in `kb_genome_utils.py`**
   - Scope: Add `from pathlib import Path`, `import hashlib`, `from collections import defaultdict, Counter`, `from datetime import datetime` at top of file. Run a smoke test on `load_genome_from_local_files`, `aggregate_taxonomies`, `create_synthetic_genome`. If the methods are dead (never called), delete them instead.
   - Bucket: B.
   - Handler: AgentForge developer task.
   - Effort: S.
   - Dependencies: Verify whether these methods have any callers (likely BV-BRC ingest workflows in `notebooks/BVBRCGenomeConversion.ipynb`).

5. **Reconcile `compartment_types` constants**
   - Scope: The narrow `compartment_types` in `model_standardization_utils.py:19-33` lacks `m`/`mitochondria`/`membrane`/`extracellular`/`environment`/`env`. `kb_model_utils.py:13` imports this narrow version. Mitochondrial reactions parsed via `KBModelUtils._parse_id` will be silently mis-classified as `c`. Move the canonical version to `ms_biochem_utils.py` (already there at line 14-28), delete the narrow copy, update `kb_model_utils.py:13` to import from MSBiochem.
   - Bucket: B.
   - Handler: AgentForge developer task.
   - Effort: S.
   - Dependencies: None.

### P1 — High priority

6. **Resolve triplicated `_parse_id` and split out `model_core`**
   - Scope: `kb_model_utils._parse_id`, `ms_biochem_utils._parse_id`, `thermo_utils._parse_id` are three slightly different copies of the same parser. Pick the most permissive (probably `ms_biochem_utils._parse_id`) and move to a `_model_core.py` module-level function. Re-export from each module if needed for backward compat.
   - Bucket: B.
   - Handler: AgentForge developer task.
   - Effort: M.
   - Dependencies: P0 #5 (compartment_types).

7. **Replace `ms_fba_utils.run_fva` with `cobra.flux_analysis.flux_variability_analysis`**
   - Scope: Lines 86-97 reimplement FVA per-reaction. Replace body with a single FVA call returning the same structure. Keep wrapper signature stable so callers don't break.
   - Bucket: B.
   - Handler: AgentForge developer task.
   - Effort: S.
   - Dependencies: None.

8. **Redesign `ms_fba_utils.py`** ⭐ **highest-value design candidate**
   - Scope: This module is the locus of AP3 in the library. Decide: which methods stay (set_media, run_fba, configure_fba_formulation), which get pushed into ModelSEEDpy as `MSFBA` methods (`fit_flux_to_mutant_growth_rate_data`, `analyzed_reaction_objective_coupling`, `unblock_objective_with_exchanges`), and which get deleted (`run_fva` → FVA call). Document the boundary in a `agent-io/prds/ms-fba-utils-redesign/` PRD. Likely a /ai-design sub-session.
   - Bucket: A (real redesign).
   - Handler: /ai-design.
   - Effort: L.
   - Dependencies: P0 #3 (need bugs fixed first to know what current behavior actually is).

9. **Redesign `ai_curation_utils.py` cache + backend**
   - Scope: AI cache files land in `KBUtilLib/data/` and get committed to git. Two backend options (Argo vs claude subprocess) with no stated rationale. Decide: cache location (probably `~/.kbutillib/ai_curation_cache/`), backend default (probably Argo since it's the team's LLM service), debug-print cleanup.
   - Bucket: A.
   - Handler: /ai-design or AgentForge developer task with clear spec.
   - Effort: M.
   - Dependencies: None.

10. **Delete `examples.py`**
    - Scope: Already commented out of `__init__.py`. Calls non-existent methods; broken on every code path. Either fix and re-export OR delete.
    - Bucket: D.
    - Handler: One-line PR.
    - Effort: S.

### P2 — Medium priority

11. **Update `tests/README.md`** to match reality (drop the stale references to test_base_utils, test_shared_environment, test_integration, test_main).
    - Bucket: B. Effort: S.

12. **Add tests for the highest-value untested modules**: `kb_model_utils`, `ms_fba_utils`, `kb_annotation_utils`, `ms_reconstruction_utils`, `model_standardization_utils`. At least one smoke test per module against a tiny fixture model.
    - Bucket: B. Handler: AgentForge developer task. Effort: L (per module).

13. **Fix `kb_callback_utils.py:62` `Path.parent()` call** (parent is a property) and `line 69 self.set_config` (method doesn't exist on SharedEnvUtils).
    - Bucket: B. Effort: S.

14. **Fix `kb_sdk_utils.py:53,62` missing imports + `working_dir`**, or delete the method.
    - Bucket: B. Effort: S.

15. **Move BV-BRC-specific helpers out of `kb_genome_utils.py`** (`_parse_fasta`, `_create_cds_features`, `_convert_local_feature`, the `load_genome_from_local_files` sister) into `bvbrc_utils.py`. Keep generic genomics in KBGenomeUtils.
    - Bucket: B. Effort: M.

16. **Redesign `kb_annotation_utils.py`** — large untested module with brittle constructor.
    - Bucket: A. Handler: /ai-design. Effort: L.

17. **Redesign `model_standardization_utils.py`** — already has a PRD at `agent-io/prds/model-standardization-refactor/`. Verify status; resume if stalled.
    - Bucket: A. Effort: L.

18. **Decompose `escher_utils.py`** — split rendering/overlay-injection from map-loading; consider whether it really needs to inherit from KBModelUtils + MSBiochemUtils (could be composition).
    - Bucket: B. Effort: M-L.

### P3 — Nice-to-have

19. Define `WorkspaceClient` Protocol shared by `kb_ws_utils` and `patric_ws_utils`.
    - Bucket: B. Effort: M. Optional.

20. Stop side-effecting constructors: lazy-load ModelSEEDDatabase in `MSBiochemUtils`, lazy-load FilteredReactions.csv in `KBAnnotationUtils`, drop `os.environ["KB_AUTH_TOKEN"]=...` mutation in `KBModelUtils`.
    - Bucket: A. Touches every base mixin. Effort: L.

21. Replace mutable default args (`[]`, `{}`) with `None` patterns across the codebase. Multiple files. Could be a single ruff/lint sweep.
    - Bucket: B. Effort: S-M.

22. Unify token-header construction: a single `_auth_headers(namespace="kbase")` helper rather than three patterns (env var, OAuth, Bearer).
    - Bucket: B. Effort: S.

23. Remove stray `print(...)` calls (counted ~12 across `kb_model_utils.py`, `ms_fba_utils.py`, `model_standardization_utils.py`, `ms_biochem_utils.py`).
    - Bucket: B. Effort: S.

24. Consider `notebook_utils.py` deprecation timeline — emit `DeprecationWarning` when used; remove once Phase 5 of notebook redesign is done.
    - Bucket: B. Effort: S.

---

## Open questions for Chris

1. **`kb_genome_utils.py` BV-BRC ingest path**: Is `load_genome_from_local_files`/`aggregate_taxonomies`/`create_synthetic_genome` actually called in any real workflow? The missing imports (`Path`, `hashlib`, `defaultdict`, `datetime`, `Counter`) imply it has *never* been executed since those imports were lost. If dead, delete. If live, fix.
2. **`ai_curation_utils._chat_via_claude_code` intent**: Is shelling out to Claude CLI from inside a Claude Code session actually the desired UX? Or is Argo always the right backend and this should be deprecated?
3. **Whether to rip `examples.py` cold** vs. fix it as living documentation. Currently broken (calls 4 non-existent methods). If we keep it, it should compile and run. If we don't, delete.
4. **`MSFBAUtils.fit_flux_to_mutant_growth_rate_data` future**: Push the bookkeeping (~280 LoC) into ModelSEEDpy `MSExpression.fit_flux_to_mutant_growth_rate_data` upstream, or keep here as an integration shim?
5. **Periplasm-removal in `model_standardization_utils.remove_model_periplasm_compartment`** — is mid-stream `cpd.id` mutation (lines 80-81 setting `cpd.id = base_id + "_e" + index`) safe with cobra's internal indices? This is an aggressive mutation pattern; worth confirming.
6. **`cb_annotation_ontology_api` data location**: `kb_annotation_utils.py:78-81` reads `FilteredReactions.csv` at construction time. If the data dir isn't found, the constructor crashes. Should this fail-soft (log a warning, set empty `filtered_rxn`) or fail-hard (current behavior)?
7. **`_parse_id` canonical behavior**: There are three implementations. The most permissive accepts both `cpd00001[c]` and `cpd00001_c0`. Confirm we want both notations forever, then I (or AgentForge) can collapse safely.
