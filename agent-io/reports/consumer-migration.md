# KBUtilLib reorg — consumer-migration sweep

Sweep root `~/Dropbox/Projects` is **present** on this machine (primary-laptop), so
this report is a live enumeration, not a "skipped" placeholder.

## Method

`grep -rInE --include='*.py' --include='*.ipynb' 'from kbutillib\.[a-zA-Z_]+ import'`
across every repo under `~/Dropbox/Projects`, excluding the `KBUtilLib` repo itself
(its own tests intentionally use flat imports as part of the deprecation-shim test
suite — see `agent-io/prds/kbutillib-reorg-integration/fullprompt.md` §Testing
Decisions #1 — and are covered by the sibling `backcompat-shim-guard` task, out of
scope here). Hits are further filtered to exclude imports of modules that **stayed
top-level** post-reorg per the PRD's old→new mapping table (`toolkit`, `layout`,
`kb_app_runner`, `kb_job_utils`, `cli`, `notebook`, `cheminformatics`, `researchos`,
`thermo_predictors`, `services`, `installed_clients`, `beril`, `beril_worktree`,
`harness`, `king_app`, `data`, and the new `domains`/`core`/`interfaces`/`agents`
packages themselves) — those are not flat-submodule reach-ins and do not depend on
the deprecation shim.

This report is **read-only**: no external consumer file listed below was modified.
All of these imports keep working unchanged once the flat-submodule deprecation
shim (sibling task `backcompat-shim-guard`) lands, since the shim re-exports every
legacy `*_utils` path with a `DeprecationWarning`.

**56 hits across 12 external repos.**

## Hits (path:line:import)

### AISynbioPipeline
- `AISynbioPipeline/aisynbiopipeline/workflows/kbase_io.py:16:from kbutillib.kb_reads_utils import KBReadsUtils, Reads`

### GenomeAnnotationAggregator
- `GenomeAnnotationAggregator/genome_annotation_aggregator/model_build/fva_runner.py:186:from kbutillib.ms_fba_utils import MSFBAUtils  # type: ignore[import]`
- `GenomeAnnotationAggregator/genome_annotation_aggregator/model_build/fva_runner.py:204:from kbutillib.dependency_manager import (  # type: ignore[import]`
- `GenomeAnnotationAggregator/genome_annotation_aggregator/model_build/fva_runner.py:281:from kbutillib.ms_fba_utils import MSFBAUtils  # type: ignore[import]`
- `GenomeAnnotationAggregator/genome_annotation_aggregator/model_build/recon_driver.py:66:from kbutillib.ms_reconstruction_utils import (  # type: ignore[import]`
- `GenomeAnnotationAggregator/genome_annotation_aggregator/model_build/recon_driver.py:69:from kbutillib.dependency_manager import (  # type: ignore[import]`
- `GenomeAnnotationAggregator/genome_annotation_aggregator/model_build/term_translator.py:67:from kbutillib.kbutils import KBAnnotationUtils  # type: ignore[import]` — **anomaly**: `kbutillib.kbutils` is not a real module in either the old flat layout or the new domain layout (not in the PRD's old→new mapping table); this import predates the reorg and is likely already broken independent of this migration. Flagged for Chris's awareness, not fixed here (external file).
- `GenomeAnnotationAggregator/genome_annotation_aggregator/recon_batch.py:422:from kbutillib.kb_annotation_utils import KBAnnotationUtils  # type: ignore[import]`
- `GenomeAnnotationAggregator/notebooks/PRJ-adp1_pangenome_import/util.py:206:from kbutillib.kb_ws_utils import KBWSUtils`
- `GenomeAnnotationAggregator/notebooks/PRJ-adp1_pangenome_import/util.py:341:from kbutillib.shared_env_utils import SharedEnvUtils`
- `GenomeAnnotationAggregator/notebooks/PRJ-genome_processing_pipeline/util.py:189:from kbutillib.shared_env_utils import SharedEnvUtils`
- `GenomeAnnotationAggregator/notebooks/PRJ-genome_processing_pipeline/util.py:215:from kbutillib.kb_ws_utils import KBWSUtils`
- `GenomeAnnotationAggregator/notebooks/PRJ-genome_processing_pipeline/util.py:251:from kbutillib.kb_ws_utils import KBWSUtils`
- `GenomeAnnotationAggregator/notebooks/PRJ-genome_processing_pipeline/util.py:339:from kbutillib.kb_ws_utils import KBWSUtils`
- `GenomeAnnotationAggregator/notebooks/PRJ-hope_anl_ecoli_phenotype_analysis/util.py:130:from kbutillib.shared_env_utils import SharedEnvUtils`
- `GenomeAnnotationAggregator/notebooks/PRJ-hope_anl_ecoli_phenotype_analysis/util.py:239:from kbutillib.ms_fba_utils import MSFBAUtils`
- `GenomeAnnotationAggregator/notebooks/PRJ-modelingloe_48_annotation_remap/util.py:174:from kbutillib.shared_env_utils import SharedEnvUtils`
- `GenomeAnnotationAggregator/notebooks/PRJ-rast_based_reaction_mapping/util.py:321:from kbutillib.kb_model_utils import KBModelUtils`
- `GenomeAnnotationAggregator/scripts/load_rxnmapping.py:44:from kbutillib.ms_biochem_utils import MSBiochemUtils`
- `GenomeAnnotationAggregator/scripts/phenotype_sweep.py:75:from kbutillib.shared_env_utils import SharedEnvUtils`
- `GenomeAnnotationAggregator/scripts/phenotype_sweep.py:92:from kbutillib.ms_fba_utils import MSFBAUtils`

### KBAnnotationApps
- `KBAnnotationApps/lib/KBAnnotationApps/kbannotationmodule.py:25:from kbutillib.rcsb_pdb_utils import RCSBPDBUtils`

### ModelingLOE
- `ModelingLOE/notebooks/PRJ-annotation_integration/02_translate_to_reactions.ipynb:159:from kbutillib.kb_annotation_utils import KBAnnotationUtils`
- `ModelingLOE/notebooks/PRJ-annotation_integration/04_gold_standard_evaluation.ipynb:172:from kbutillib.kb_annotation_utils import KBAnnotationUtils`
- `ModelingLOE/notebooks/PRJ-kb_genome_annotation/util.py:182:from kbutillib.kb_ws_utils import KBWSUtils`
- `ModelingLOE/notebooks/PRJ-kb_genome_annotation/util.py:431:from kbutillib.kb_annotation_utils import KBAnnotationUtils  # noqa: PLC0415`

### NotebookWorkspaces
- `NotebookWorkspaces/Ecology/WetlandsDataProcessing/notebooks/wetland_pipeline.ipynb:44:from kbutillib.kb_ws_utils import KBWSUtils`
- `NotebookWorkspaces/Ecology/WetlandsDataProcessing/scripts/import_sra_reads.py:35:from kbutillib.kb_ws_utils import KBWSUtils`
- `NotebookWorkspaces/Ecology/WetlandsDataProcessing/scripts/run_fastqc.py:29:from kbutillib.kb_ws_utils import KBWSUtils`
- `NotebookWorkspaces/Ecology/WetlandsDataProcessing/scripts/upload_reads_streaming.py:25:from kbutillib.kb_reads_utils import KBReadsUtils, Reads`
- `NotebookWorkspaces/Ecology/WetlandsDataProcessing/scripts/upload_reads_test.py:21:from kbutillib.kb_reads_utils import KBReadsUtils, Reads`
- `NotebookWorkspaces/Microbial/ANMENotebooks/archive/ANMEThermodynamicAnalysis_old.ipynb:308:from kbutillib.kb_model_standardization_utils import MSModelStandardization` — **anomaly**: `kb_model_standardization_utils` is not in the PRD's old→new mapping table (the real module is `model_standardization_utils`, no `kb_` prefix); this archived notebook's import already predates and is unrelated to the reorg.
- `NotebookWorkspaces/Microbial/ANMENotebooks/archive/ANMEThermodynamicAnalysis_old.ipynb:308:from kbutillib.kb_model_utils import MSModelUtil`
- `NotebookWorkspaces/Microbial/ANMENotebooks/archive/ANMEThermodynamicAnalysis_old.ipynb:308:from kbutillib.ms_biochem_utils import MSBiochem`
- `NotebookWorkspaces/Microbial/WatershedPhenotypeReplication/notebooks/PRJ-watershed_phenotype_replication/util.py:189:from kbutillib.shared_env_utils import SharedEnvUtils`
- `NotebookWorkspaces/Microbial/WatershedPhenotypeReplication/notebooks/PRJ-watershed_phenotype_replication/util.py:202:from kbutillib.ms_fba_utils import MSFBAUtils`
- `NotebookWorkspaces/ModelSEEDNotebooks/notebooks/TemplateManagement.ipynb:2404:from kbutillib.kb_ws_utils import KBWSUtils`
- `NotebookWorkspaces/SalternsNotebooks/agent-io/plans/build_genome_assembly_ref_refactor_nb.py:86:from kbutillib.kb_ws_utils import KBWSUtils`
- `NotebookWorkspaces/SalternsNotebooks/agent-io/plans/build_genome_assembly_ref_refactor_nb.py:133:from kbutillib.kb_ws_utils import KBWSUtils`
- `NotebookWorkspaces/SalternsNotebooks/agent-io/plans/build_genome_assembly_ref_refactor_nb.py:179:from kbutillib.kb_ws_utils import KBWSUtils`
- `NotebookWorkspaces/SalternsNotebooks/agent-io/plans/build_genome_assembly_ref_refactor_nb.py:261:from kbutillib.kb_ws_utils import KBWSUtils`
- `NotebookWorkspaces/SalternsNotebooks/agent-io/plans/build_genome_assembly_ref_refactor_nb.py:317:from kbutillib.kb_ws_utils import KBWSUtils`
- `NotebookWorkspaces/SalternsNotebooks/agent-io/plans/build_genome_assembly_ref_refactor_nb.py:403:from kbutillib.kb_ws_utils import KBWSUtils`
- `NotebookWorkspaces/SalternsNotebooks/agent-io/plans/build_genome_assembly_ref_refactor_nb.py:481:from kbutillib.kb_ws_utils import KBWSUtils`
- `NotebookWorkspaces/SalternsNotebooks/agent-io/plans/build_genome_assembly_ref_refactor_nb.py:548:from kbutillib.kb_ws_utils import KBWSUtils`
- `NotebookWorkspaces/SalternsNotebooks/notebooks/GenomeAssemblyRefRefactor.ipynb:61:from kbutillib.kb_ws_utils import KBWSUtils`
- `NotebookWorkspaces/SalternsNotebooks/notebooks/GenomeAssemblyRefRefactor.ipynb:111:from kbutillib.kb_ws_utils import KBWSUtils`
- `NotebookWorkspaces/SalternsNotebooks/notebooks/GenomeAssemblyRefRefactor.ipynb:160:from kbutillib.kb_ws_utils import KBWSUtils`
- `NotebookWorkspaces/SalternsNotebooks/notebooks/GenomeAssemblyRefRefactor.ipynb:244:from kbutillib.kb_ws_utils import KBWSUtils`
- `NotebookWorkspaces/SalternsNotebooks/notebooks/GenomeAssemblyRefRefactor.ipynb:303:from kbutillib.kb_ws_utils import KBWSUtils`
- `NotebookWorkspaces/SalternsNotebooks/notebooks/GenomeAssemblyRefRefactor.ipynb:391:from kbutillib.kb_ws_utils import KBWSUtils`
- `NotebookWorkspaces/SalternsNotebooks/notebooks/GenomeAssemblyRefRefactor.ipynb:471:from kbutillib.kb_ws_utils import KBWSUtils`
- `NotebookWorkspaces/SalternsNotebooks/notebooks/GenomeAssemblyRefRefactor.ipynb:541:from kbutillib.kb_ws_utils import KBWSUtils`

### ResearchOS-workspaces
- `ResearchOS-workspaces/KBaseInnerLoop/ReactionAnnotationTable/tool/scripts/build_template_reactions.py:31:from kbutillib.kb_ws_utils import KBWSUtils`

### workspace_deluxe
- `workspace_deluxe/agent-io/docs/retrieve_kbase_datatypes.py:16:from kbutillib.kb_ws_utils import KBWSUtils`
- `workspace_deluxe/claude/retrieve_kbase_datatypes.py:16:from kbutillib.kb_ws_utils import KBWSUtils`

## Disposition

None of these external files were modified (out of scope per this task and the
PRD's "Out of Scope" section: "Fixing every external consumer `util.py` reach-in ...
only known in-repo shipped skills are fixed here; external fixes are a follow-up.").
The flat-submodule deprecation shim (sibling task `backcompat-shim-guard`, generated
thin modules per the old→new mapping table) keeps every import above working with a
`DeprecationWarning`, except the two flagged anomalies above which were already
broken pre-reorg and are unrelated to this migration.

`KBDatalakeApps` and `BVBRCHackathon`, named as known reach-ins in the PRD's
Consumer-netting section, both exist under `~/Dropbox/Projects` but produced zero
`from kbutillib.<flat> import` hits at sweep time (no matching lines found in
either repo's `.py`/`.ipynb` files — the reach-in may have been removed/refactored
since the PRD was written, or the PRD's memory of it predates a cleanup).
`EnsembleNotebooks` and `PangenomeAnalysis`, also named in the PRD, do **not**
exist under `~/Dropbox/Projects` on this machine (no matching directory).
