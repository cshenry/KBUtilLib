# KBDatalakeApps Pipeline Reference

## Pipeline Stage 1: Genome Pipeline

**Script:** `scripts/run_genome_pipeline.sh` -> `berdl/berdl/pipeline.py`
**Environment:** berdl_genomes venv
**Called from:** `KBDatalakeApps.run_genome_pipeline(input_params_file)`

### What It Does

1. Creates `KBaseAPI` from context token
2. Fetches all genomes from workspace using `kbase.get_from_ws()`
3. Initializes `GenomePaths` with scratch root
4. Runs `BERDLPreGenome.run(genomes)` which:
   - Exports genomes to FASTA (.faa, .fna) files
   - Runs skani ANI against BERDL reference genomes
   - Assigns each user genome to a BERDL pangenome clade
   - Returns: user_genome_files, user_to_clade mapping, ANI results

### Key Output Files

```
$scratch/
├── genome/
│   ├── user_<name>.faa         # Protein sequences
│   ├── user_<name>.fna         # DNA sequences
│   └── user_<name>_genome.tsv  # Full genome features table
├── pangenome/
│   ├── user_to_clade.json      # {"genome_name": "clade_id", ...}
│   └── <clade_id>/             # Created per assigned clade
└── ani/                        # ANI comparison results
```

### After Genome Pipeline (in Impl)

The Impl also runs `util.run_user_genome_to_tsv()` for each genome to create the full genome TSV using KBUtilLib's `build_genome_tsv()` method. This produces TSV with columns: gene_id, contig, start, end, strand, type, dna_sequence, protein_translation, functions, aliases, ontology_terms, Annotation:SSO.

---

## Pipeline Stage 2: Annotation Pipeline

**Runs in SDK Python env (via TaskExecutor) AND berdl_genomes venv**

### User Genome Annotations (SDK env, parallel threads)

For each `.faa` file in `$scratch/genome/`, these tasks run in parallel via `TaskExecutor(max_workers=4)`:

| Task | Function | Client | Output File |
|------|----------|--------|-------------|
| RAST | `task_rast` -> `run_rast()` | `RAST_SDKClient` | `user_<name>_rast.tsv` |
| KOfam | `task_kofam` -> `run_kofam()` | `kb_kofamClient` | `user_<name>_annotation_kofam.tsv` |
| Bakta | `task_bakta` -> `run_bakta()` | `kb_baktaClient` | `user_<name>_annotation_bakta.tsv` |
| PSORTb | `task_psortb` -> `run_psortb()` | `kb_psortbClient` | `user_<name>_annotation_psortb.tsv` |

### Annotation Output Formats

**RAST TSV:** `feature_id\tRAST` (semicolon-delimited function descriptions)

**KOfam TSV:** `feature_id\tKEGG` (semicolon-delimited KO IDs)

**Bakta TSV:** `feature_id\t<dynamic columns>` (columns vary: EC, GO, KEGG, COG, PFAM, SO, UniRef, bakta_product)

**PSORTb TSV:** `feature_id\tprimary_localization_psortb\tsecondary_localization_psortb`

### Pangenome Member Annotations (berdl_genomes venv)

For each pangenome clade's `genome/` directory, additional annotation runs via:
- RAST (SDK env, via TaskExecutor)
- Full annotation pipeline (berdl_genomes env, via `run_annotation_pipeline()` subprocess)

### Task Barrier

The Impl uses explicit task barriers:
1. **RAST barrier** - Wait for all RAST tasks (needed before model pipeline)
2. **Full annotation barrier** - Wait for all annotation tasks
3. **Pangenome annotation barrier** - Wait for pangenome member annotations

---

## Pipeline Stage 3: Pangenome Pipeline

**Script:** `scripts/run_pangenome_pipeline.sh` -> `berdl/berdl/bin/pangenome.py`
**Environment:** berdl_genomes venv
**Called from:** `KBDatalakeApps.run_pangenome_pipeline(input_params, clade_id)`

### What It Does

For each clade directory under `$scratch/pangenome/<clade_id>/`:
1. Collects all protein sequences from user genomes assigned to this clade
2. Retrieves pangenome reference members from BERDL
3. Runs MMseqs2 protein clustering
4. Classifies clusters as core (present in all members) or accessory
5. Assigns user genome features to existing pangenome clusters

### Key Output Files

```
$scratch/pangenome/<clade_id>/
├── genome/                     # Reference genome FAA files + annotations
├── master_mmseqs2/             # MMseqs2 clustering intermediate files
├── pangenome_cluster_with_mmseqs.parquet  # Cluster assignments
└── user_<name>_pangenome_profile.tsv      # Per-user-genome cluster mapping
```

---

## Pipeline Stage 4: Modeling Pipeline

**Script:** `scripts/run_model_pipeline.sh` -> `berdl/berdl/bin/model_pipeline.py`
**Environment:** SDK Python (not berdl_genomes, because it needs ModelSEEDpy/cobra/KBUtilLib)
**Called from:** `KBDatalakeApps.run_model_pipeline(params_file)`

### Step 4a: Model Reconstruction

**Function:** `run_model_reconstruction(input_tsv, output_base, classifier_dir, kbversion)`
**Runs in:** ProcessPoolExecutor with 10 workers

For each genome TSV:
1. Parse features from TSV into `MSGenome` with `MSFeature` objects
2. Add RAST ontology terms from `functions` column
3. Add SSO terms from `Annotation:SSO` column
4. Load genome classifier (KNN from pickle files)
5. Call `worker_util.build_metabolic_model()`:
   - Template selection based on genome class (GN/GP)
   - Reaction mapping from RAST annotations
   - ATP correction
   - Core gapfilling
   - Growth media gapfilling (Carbon-Pyruvic-Acid)
6. Run pFBA in minimal and rich media
7. Run FVA in minimal and rich media (classify reactions as essential/variable/blocked)
8. Categorize gapfilled reactions (core vs minimal vs rich-essential)
9. Save COBRA JSON model and _data.json with:
   - model_info (num_reactions, metabolites, genes, genome_class, growth)
   - Full reaction list with bounds, gene rules, equations
   - Full metabolite list
   - gapfilled_reactions by category
   - flux_analysis (pfba_fluxes, fva_classes for minimal and rich)

### Step 4b: Phenotype Simulation

**Function:** `run_phenotype_simulation(model_file, output_file, data_path, max_phenotypes, kbversion)`
**Runs in:** ProcessPoolExecutor with 10 workers

For each COBRA model:
1. Load model with `cobra.io.load_json_model()`
2. Load phenotype set from `full_phenotype_set.json`
3. Create `MSGapfill` with ATP test conditions
4. Filter mass-imbalanced (MI) reactions from gapfill database
5. Run `phenoset.simulate_phenotypes()`:
   - For each phenotype (carbon source condition):
   - Test growth/no-growth
   - Gapfill if no growth detected
   - Record objective values and gapfilled reactions
6. Save results as `<genome>_phenosim.json`:
   - `details`: Parallel arrays (Phenotype, Class, Simulated/Observed objective, Transports)
   - `data`: Per-compound dict with objective_value, class, gfreactions, fluxes
   - `data.summary`: Accuracy metrics (CP, CN, FP, FN)

### Step 4c: Table Building (within model_pipeline.py)

After reconstruction and simulation:
1. `kbdl.build_phenotype_tables()` -> 3 TSV files (model_performance, genome_phenotypes, gene_phenotypes)
2. `kbdl.build_model_tables()` -> genome_reactions.tsv, gene_reaction_data.tsv, media_compositions.tsv

---

## Pipeline Stage 5: Table Generation

**Script:** `scripts/run_generate_table.sh` -> `berdl/berdl/bin/table.py`
**Environment:** berdl_genomes venv
**Called from:** `KBDatalakeApps.run_build_table(input_params, clade_id)`

### What DatalakeTableBuilder.build() Does

1. **build_ani_table()**: Creates `ani` table from skani JSON results
2. **build_user_genome_feature_parquet()**: Builds user_feature DataFrame:
   - Read genome TSV (contig, gene_id, start, end, strand, type, sequences)
   - Collect annotations from all TSV files (*_rast.tsv, *_annotation_*.tsv)
   - Add pangenome cluster assignments
   - Hash protein sequences for deduplication
3. **build_user_genome_features_table()**: Writes user_feature to SQLite
4. **build_pangenome_member_feature_parquet()**: Same for pangenome member genomes
5. **build_pangenome_genome_features_table()**: Writes pangenome_feature to SQLite

### After Table Building (in Impl)

The Impl also calls:
- `generate_ontology_tables(db_path, reference_data_path, source_tables)` - Extracts unique ontology terms from feature tables and enriches them via OntologyEnrichment

---

## Pipeline Stage 6: Report Generation

**In:** `KBDatalakeAppsImpl.build_genome_datalake_tables()` (end of method)

1. Build file_links from export flags:
   - `export_genome_data` -> zip genome/ directory
   - `export_folder_models` -> zip models/ directory
   - `export_folder_phenotypes` -> zip phenotypes/ directory
   - `export_databases` -> zip each clade's db.sqlite

2. Build HTML report:
   - Copy `/kb/module/data/html/` template to output directory
   - Write `app-config.json` with UPA reference
   - Upload to Shock

3. Create KBase report:
   - `KBaseReport.create_extended_report()` with html_links + file_links
   - 800px height HTML window

---

## Standalone Functions in KBDatalakeUtils.py

These run in separate processes (ProcessPoolExecutor):

### run_model_reconstruction()
- Creates `MSReconstructionUtils` worker
- Parses genome TSV -> `MSGenome` -> `MSFeature` objects
- Detects simple vs full TSV format
- Calls `build_metabolic_model()` then `gapfill_metabolic_model()`
- Runs pFBA + FVA in minimal and rich media
- Returns complete model data dict

### run_phenotype_simulation()
- Creates `PhenotypeWorkerUtil(MSReconstructionUtils, MSFBAUtils, MSBiochemUtils)`
- Loads phenotype set and model
- Creates `MSGapfill` with ATP test conditions
- Filters MI reactions
- Runs `phenoset.simulate_phenotypes()` with gapfilling
- Returns phenosim JSON

### generate_ontology_tables()
- Reads user_feature and pangenome_feature tables from SQLite
- Extracts all ontology term IDs from annotation columns
- Uses OntologyEnrichment to get labels/definitions
- Writes ontology_terms table to SQLite

---

## KBDataLakeUtils.build_phenotype_tables() Details

Creates three TSV files from phenosim JSON results:

### 1. model_performance.tsv
Columns: genome_id, taxonomy, false_positives, false_negatives, true_positives, true_negatives, accuracy, positive_growth, negative_growth, avg_positive_growth_gaps, avg_negative_growth_gaps, closest_user_genomes, source

### 2. genome_phenotypes.tsv
Columns: genome_id, phenotype_id, phenotype_name, class, simulated_objective, observed_objective, gap_count, gapfilled_reactions, reaction_count, transports_added, closest_experimental_data, source

### 3. gene_phenotypes.tsv
Columns: genome_id, gene_id, phenotype_id, phenotype_name, association_sources, model_pred_reactions, model_pred_max_flux, fitness_match, fitness_max, fitness_min, fitness_avg, fitness_count, essentiality_fraction

Gene-phenotype associations come from three sources:
1. **Gapfill**: Genes in gapfilled reactions for a phenotype
2. **Model prediction**: Genes in reactions with flux for a phenotype
3. **Fitness**: Experimental fitness data mapped via ortholog clustering

---

## KBDataLakeUtils.build_model_tables() Details

Creates reaction and gene-reaction tables:

### genome_reactions.tsv / genome_reactions SQLite table
Columns: genome_id, reaction_id, genes, equation_names, equation_ids, directionality, upper_bound, lower_bound, gapfilling_status, rich_media_flux, rich_media_class, minimal_media_flux, minimal_media_class

### gene_reaction_data.tsv / feature table updates
Per-gene aggregated data: reaction (semicolon-joined), rich_media_flux (max), rich_media_class (most constrained), minimal_media_flux, minimal_media_class

### media_compositions.tsv
Columns: media_id, compound_id, max_uptake, compound_name
Sources: KBase workspace media (Carbon-Pyruvic-Acid, AuxoMedia) + phenotype set formulations
