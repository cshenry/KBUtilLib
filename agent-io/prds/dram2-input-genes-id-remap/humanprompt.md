# DRAM2Utils --input_genes integration fix

Fix the DRAM2 `--input_genes` integration in KBUtilLib `DRAM2Utils` so that
real-genome annotation works end-to-end. Complements the in-progress GAA
native_batch DRAM2 executor work already on `main` (`LocalDram2Plugin` /
`NativeBatchExecutor`).

## PRIMARY BUG (blocks every real genome)

A single-genome test on h100 (E. coli Keio, 4607 proteins, kofam) ran the whole
DRAM2 pipeline green — GENE_LOCS, MMSEQS_INDEX, HMM_SEARCH_KOFAM, PARSE_HMM_KOFAM,
KOFAM_HMM_FORMATTER all exit 0 — then died at the final COMBINE_ANNOTATIONS step:

    ValueError: invalid literal for int() with base 10: 'b0001'
    at repo/bin/combine_annotations.py:65  set_gene_data()

Root cause: combine_annotations does

    split_label = seq.metadata["id"].split("_")
    gene_position = split_label[-1]
    ... genes_faa_dict[id]["gene_number"] = int(gene_position)

i.e. it requires every gene id to END IN `_<integer>` (prodigal's
contig_geneNumber convention, e.g. `k99_42_7`). DRAM2Utils._write_faa passes the
GAA store's locus_ids straight through (`b0001`, `b0002`, …), which have no
`_<int>` suffix → int() crashes. This is an input-contract mismatch, not a DRAM2
bug. NOTE: the existing `_write_faa` already emits prodigal-style *coordinate*
headers (`>b0001 # 1 # 300 # 1 #`), but DRAM2 takes only the first whitespace
token (`b0001`) as the id, so the coords do not help — the id token itself needs
a numeric final element.

## DECISION — input strategy (RESOLVED: A)

(A) Rename on the way in: DRAM2Utils emits prodigal-safe ids in the input FASTA
    (`g_<n>`, per-genome/batch integer n), keeps a `{emitted_id -> store_locus_id}`
    map, and translates `query_id` back to the store locus_id when parsing
    raw-annotations.tsv. Keeps `--input_genes` (no gene recalling). Guarantees
    uniqueness and a numeric final token.

(B) Switch to `--input_fasta` (nucleotide contigs + prodigal gene-calling, remap
    by protein_seq_hash) — REJECTED. Incompatible with GAA's seq-hash
    cross-genome batching: a `NativeBatchExecutor` bucket is the set of unique
    proteins (deduped by seq_hash) drawn from many genomes, so there is no single
    assembly to feed prodigal. B would force per-genome re-annotation and defeat
    the dedup that is GAA's core scaling mechanism.

Resolved: A, emitted-id scheme `g_<n>` (1-based batch index; the store locus_id
never touches the FASTA header, so messy locus characters can never corrupt it).

## SECONDARY FIXES (found in the same run)

1. NXF_VER pinning. The `nextflow-native` wrapper defaults NXF_VER=26.04.3, which
   FAILS TO COMPILE beta17 (8 Groovy errors). The pipeline only compiles on
   24.10.5. DRAM2Utils invokes the wrapper directly, inheriting the caller's env —
   so the integration MUST export NXF_VER=24.10.5 (and put env_nf/bin java +
   micromamba on PATH). Don't rely on dram2-env.sh: it still hardcodes the stale
   DRAM2_ROOT=/storage/chenry/DRAM2 (actual: /scratch1/fliu/hub_scratch/chenry/DRAM2).
   RESOLVED: env owned via new config keys `dram2.nxf_ver` (default 24.10.5) +
   `dram2.env_path` (PATH prepend); DRAM2Utils builds the subprocess env explicitly.

2. Work-dir destroyed on failure. DRAM2Utils wraps input/work/outdir in
   tempfile.TemporaryDirectory, so a failed run deletes ALL evidence (had to
   recover the combine traceback from $ROOT/.nextflow.log). RESOLVED: keep-on-
   failure — preserve the work dir and copy pipeline_info/ + .nextflow.log out.

3. TMPDIR discipline. DRAM2Utils' TemporaryDirectory honors $TMPDIR; if unset it
   lands on /tmp, and a real run's Nextflow work tree fills small /tmp partitions
   (filled h100's 8 GB /tmp earlier). RESOLVED: force scratch onto a configured
   large-disk path `dram2.work_root` (default = launch_dir, on /scratch1), never
   inherit /tmp.

## ACCEPTANCE

- `DRAM2Utils.annotate(proteins, gene_coords, databases=['kofam'])` on a real
  genome with store locus_ids (b0001-style) completes COMBINE_ANNOTATIONS exit 0
  and returns records whose `query_id` maps back to the original store locus_ids.
- Unit test: a protein dict with non-prodigal ids (e.g. 'b0001') round-trips
  (emitted id has numeric final token; output remaps to 'b0001').
- Env: a documented, non-stale way to get NXF_VER=24.10.5 + PATH without sourcing
  the broken dram2-env.sh.
- Failure path preserves the work dir / pipeline_info for debugging.

## REPO / INSTALL

KBUtilLib (DRAM2Utils, branch wip) + GAA (LocalDram2Plugin / NativeBatchExecutor
already wired on main, NO GAA changes required). Install:
/scratch1/fliu/hub_scratch/chenry/DRAM2 (beta17, Nextflow 24.10.5, 81 GB DBs).
REF: GenomeAnnotationAggregator/agent-io/work-records/dram2-docker-profile-validation.md.
