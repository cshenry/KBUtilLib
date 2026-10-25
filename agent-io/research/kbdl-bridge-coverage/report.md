# KBDL bridge coverage report

Generated 2026-10-25T06:01:09Z on kbhub by `measure_bridge_coverage.py` (read-only).
kbutillib imported from: `/home/chenry/.maestro/worktrees/measure-bridge-coverage/src/kbutillib/__init__.py`

## Populations

Ledger: 185126 lines, 175943 distinct job ids.

| job type | ledger entries (distinct ids) | result files on disk | union of ids |
|---|---|---|---|
| KBDLBuildGenome | 4654 | 4889 | 4889 |
| KBDLBuildSKANIDB | 14 | 12 | 14 |
| KBDLCheckM2 | 2 | 0 | 2 |
| KBDLFitnessModelAnalysis | 1 | 2 | 3 |
| KBDLFitnessProp | 2 | 0 | 2 |
| KBDLGenomeAnnotation | 835 | 3302 | 3317 |
| KBDLHorizyn | 123 | 123 | 123 |
| KBDLModelReconstruction | 9 | 8 | 17 |
| KBDLSKANI | 114123 | 226 | 114123 |
| KBDLUploadObject | 56180 | 17145 | 56180 |

## Q1 COVERAGE (annotation locus prefix -> object store)

Annotation result files on disk: 3302; examined (parsed, non-empty): 3302; unreadable/empty: 0; files whose keys carry >1 distinct 16-char prefix: 1; distinct locus prefixes among examined files: 2203.

**BLOCKED: `KBDLServiceUtils().list_objects()` failed after 44.0s: `KBDLAuthenticationError: {'error': 'invalid_token', 'message': 'KBase auth service rejected the token (HTTP 401)'}`.**
The KBase token at `~/.kbase/token` (mtime 2026-10-23) is rejected by the KBase auth service. Q1 and Q2 therefore have NO object-store numbers; rerun the script with a valid token (no other change needed).

## Q2 COLLISION

**BLOCKED** for the same reason (needs the full object listing).

## Q3 OVERLAP (existing arc-store rows keyed on an indexable KBDL job id)

Runs root: `/home/chenry/Dropbox/Science/runs`; run database: `/home/chenry/.kbdl/runs.sqlite`.
Rows in `runs` table (read-only count): 528; rows enumerated via `KorosArcStore.list_analyses` over 34 (project, arc) pairs incl. unattributed: 527.

**Overlap (run_uid in ledger with job_type BuildGenome/GenomeAnnotation): 51**
Overlap against the wider ledger-union-result-file population: 517.
Rows whose run_uid is ANY ledger job id: 51.

Wider-population overlap by producer: {"kbdl-job-api-backfill": 419, "koros.fitness-genome-analysis": 98}
Wider-population overlap by kind: {"kbdl.genome_annotation": 232, "kbdl.build_genome": 285}
Overlap by kind: {"kbdl.genome_annotation": 1, "kbdl.build_genome": 50}
Overlap by producer: {"kbdl-job-api-backfill": 1, "koros.fitness-genome-analysis": 50}
Overlap by (project, arc): {"None/None": 1, "inner-loop/fitness-genome-analysis": 50}

### Example rows (up to 5): fields a mechanical indexer could not reconstruct

- run_uid `87886f5e8e674e30bb59c844b9544f57` (KBDLGenomeAnnotation); project/arc `None/None`; kind `kbdl.genome_annotation`; producer `kbdl-job-api-backfill` 1; trust_tier `hypothesis`; status `ok`; subject `ACIAD`
  - provenance: `{"bridge_kind": "id_join", "job_id": "87886f5e8e674e30bb59c844b9544f57", "metric": {"name": "locus_prefix_coverage", "value": 0.9963}, "recovered_at": "2026-10-02T03:54:00.580212+00:00", "source": "kbdl-job-api"}`
  - payload keys: `['gene_count', 'kbdl_job_id', 'kbdl_job_type', 'methods', 'provenance', 'subject_is']`; artifacts: `{}`
- run_uid `412c494350d1464bb42d3f7619f7f5c2` (KBDLBuildGenome); project/arc `inner-loop/fitness-genome-analysis`; kind `kbdl.build_genome`; producer `koros.fitness-genome-analysis` 0.1; trust_tier `verified`; status `ok`; subject `SyringaeB728a_mexBdelta`
  - provenance: `{"bridge_kind": "user_assertion", "build_job_id": "412c494350d1464bb42d3f7619f7f5c2", "gene_id_space": "fitness_browser", "metric": {"name": "n_gene_calls_in", "value": 5216}, "recalling": false, "source": "berdl_inner_loop.V2_Genes (source='fitnessDB')"}`
  - payload keys: `['build_job_id', 'genome_id', 'genome_object_id', 'n_features_built', 'n_gene_calls_in', 'no_recalling', 'sref']`; artifacts: `{"genome_object": "obj://b3b66173c92a0f54571e5a09d8cac7eeaa756228b90cd1076e13569985057734"}`
- run_uid `709418433dfb4194940449f9f155ee9b` (KBDLBuildGenome); project/arc `inner-loop/fitness-genome-analysis`; kind `kbdl.build_genome`; producer `koros.fitness-genome-analysis` 0.1; trust_tier `verified`; status `ok`; subject `SyringaeB728a`
  - provenance: `{"bridge_kind": "user_assertion", "build_job_id": "709418433dfb4194940449f9f155ee9b", "gene_id_space": "fitness_browser", "metric": {"name": "n_gene_calls_in", "value": 5216}, "recalling": false, "source": "berdl_inner_loop.V2_Genes (source='fitnessDB')"}`
  - payload keys: `['build_job_id', 'genome_id', 'genome_object_id', 'n_features_built', 'n_gene_calls_in', 'no_recalling', 'sref']`; artifacts: `{"genome_object": "obj://b3b66173c92a0f54571e5a09d8cac7eeaa756228b90cd1076e13569985057734"}`
- run_uid `23031582b911463b8077c84522e5769b` (KBDLBuildGenome); project/arc `inner-loop/fitness-genome-analysis`; kind `kbdl.build_genome`; producer `koros.fitness-genome-analysis` 0.1; trust_tier `verified`; status `ok`; subject `WCS417`
  - provenance: `{"bridge_kind": "user_assertion", "build_job_id": "23031582b911463b8077c84522e5769b", "gene_id_space": "fitness_browser", "metric": {"name": "n_gene_calls_in", "value": 5580}, "recalling": false, "source": "berdl_inner_loop.V2_Genes (source='fitnessDB')"}`
  - payload keys: `['build_job_id', 'genome_id', 'genome_object_id', 'n_features_built', 'n_gene_calls_in', 'no_recalling', 'sref']`; artifacts: `{"genome_object": "obj://63ab865d7b52d4ce733441d1c1bd261ef693b4aab4b3f60079d2640c8cbf8005"}`
- run_uid `dc4e00b8d0724bafa1ffdfae423e8221` (KBDLBuildGenome); project/arc `inner-loop/fitness-genome-analysis`; kind `kbdl.build_genome`; producer `koros.fitness-genome-analysis` 0.1; trust_tier `verified`; status `partial`; subject `SyringaeB728a`
  - provenance: `{"bridge_kind": "user_assertion", "build_job_id": "dc4e00b8d0724bafa1ffdfae423e8221", "gene_id_space": "fitness_browser", "metric": {"name": "n_gene_calls_in", "value": 5216}, "recalling": false, "source": "berdl_inner_loop.V2_Genes (source='fitnessDB')"}`
  - payload keys: `['build_job_id', 'genome_id', 'genome_object_id', 'n_features_built', 'n_gene_calls_in', 'no_recalling', 'sref']`; artifacts: `{"genome_object": "obj://f1e3762ddd8ff160974991e42730fb4abbe0ceb2e78eb708b3146bcce5e2bce1"}`

## Q4 ATTRIBUTION COVERAGE (job ids mentioned in arcs of the runs tree)

Scanned 5239 regular files (2.56 GB) in 45s; 52 of them lie outside `<project>/arcs/<slug>/` and are not attributed to any arc; 0 unreadable.

### Population A: ledger job ids only (distinct ids per ledger job_type)

| job type | jobs in population | in exactly 1 arc | in >1 arc | in no arc |
|---|---|---|---|---|
| KBDLBuildGenome | 4654 | 51 | 0 | 4603 |
| KBDLBuildSKANIDB | 14 | 2 | 0 | 12 |
| KBDLCheckM2 | 2 | 0 | 0 | 2 |
| KBDLFitnessModelAnalysis | 1 | 0 | 0 | 1 |
| KBDLFitnessProp | 2 | 1 | 0 | 1 |
| KBDLGenomeAnnotation | 835 | 70 | 0 | 765 |
| KBDLHorizyn | 123 | 109 | 0 | 14 |
| KBDLModelReconstruction | 9 | 4 | 0 | 5 |
| KBDLSKANI | 114123 | 210 | 0 | 113913 |
| KBDLUploadObject | 56180 | 8 | 0 | 56172 |

### Population B: union of ledger ids and result-file stems

| job type | jobs in population | in exactly 1 arc | in >1 arc | in no arc |
|---|---|---|---|---|
| KBDLBuildGenome | 4889 | 277 | 0 | 4612 |
| KBDLBuildSKANIDB | 14 | 2 | 0 | 12 |
| KBDLCheckM2 | 2 | 0 | 0 | 2 |
| KBDLFitnessModelAnalysis | 3 | 1 | 0 | 2 |
| KBDLFitnessProp | 2 | 1 | 0 | 1 |
| KBDLGenomeAnnotation | 3317 | 387 | 0 | 2930 |
| KBDLHorizyn | 123 | 109 | 0 | 14 |
| KBDLModelReconstruction | 17 | 6 | 0 | 11 |
| KBDLSKANI | 114123 | 210 | 0 | 113913 |
| KBDLUploadObject | 56180 | 8 | 0 | 56172 |

Hand-measurement to confirm or correct: 70/835 GenomeAnnotation and 49/4654 BuildGenome ledger jobs mentioned, zero multi-arc. Compare against Population A rows above (mentioned = exactly 1 + more than 1).

## Summary and notes (hand-written after the run; a rerun of the script overwrites this section)

1. Q1 (coverage): NOT MEASURED. The KBase token in `~/.kbase/token` is rejected (HTTP 401), so `list_objects()` could not run. What was measured: 3302 annotation result files on disk (not 2997; the sweeper has kept adding), all parsed, carrying 2203 distinct locus prefixes; one file has keys with more than one 16-char prefix. The exactly-one / multiple / none split needs a valid token; rerun the script.
2. Q2 (collision): NOT MEASURED, same cause.
3. Q3 (overlap): 51 existing rows have a run_uid that is a ledger job id of type BuildGenome/GenomeAnnotation (50 BuildGenome rows by producer `koros.fitness-genome-analysis` in arc `inner-loop/fitness-genome-analysis`, plus 1 GenomeAnnotation row by `kbdl-job-api-backfill`, unattributed). This is not the 92 stated in the brief. Against the wider population (ledger ids plus result-file stems) the overlap is 517: 419 rows by `kbdl-job-api-backfill` and 98 by the hand script, so most existing rows key on jobs absent from the ledger. The run database holds 528 rows; 527 were reachable through `list_analyses` over the 34 known (project, arc) pairs, so one row sits under a project/arc pair not present in the tree.
   Provenance carried by the hand-script rows that a mechanical indexer cannot derive from the job result and ledger: `bridge_kind: user_assertion`, `gene_id_space: fitness_browser`, `source: berdl_inner_loop.V2_Genes (source='fitnessDB')`, `recalling: false`, `metric n_gene_calls_in` (the input gene-call count from the external fitness database), `trust_tier: verified`, and the project/arc attribution itself. A BuildGenome result supplies genome_object_id and feature counts, but not these.
4. Q4 (attribution): Population A (ledger ids) confirms the round-1 numbers for annotation: 70/835 GenomeAnnotation mentioned, none in more than one arc. BuildGenome is corrected from 49 to 51 of 4654. Zero multi-arc mentions for every job type. Population B (ledger union result files) gives 387/3317 GenomeAnnotation and 277/4889 BuildGenome. The difference against A is mostly the 98+ rows the hand script wrote for jobs absent from the ledger.
