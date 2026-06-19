# DRAM2 golden fixture

## Files

- `raw-annotations.tsv` — small slice (5 rows) of the upstream OWC
  `raw_annotations_snapshot.tsv`, with one synthetic dbCAN row appended
  on `OWC_0000_k121_3157_2` so the parser exercises the `dbcan_id`
  (→ `CAZY` namespace) and `dbcan_EC` (→ `EC` namespace) columns.  The
  rest of every retained row is verbatim DRAM2 output.
- `demo.faa` — 5 protein records whose headers exactly match the
  `query_id` values in the TSV slice.  Sequences are short protein
  excerpts chosen to satisfy the `_guard_protein` alphabet check; they
  are NOT the proteins DRAM2 actually annotated (the fixture is for the
  pure parse test, which never invokes DRAM2).

## Provenance

The base of `raw-annotations.tsv` rows 1–5 was captured upstream by the
DRAM2 maintainers from a real DRAM2 run on the OWC test contigs and
shipped with the pinned h100 install at
`/storage/chenry/DRAM2/repo/tests/data/owc/annotation/raw_annotations_snapshot.tsv`
(commit pinned at `v2.0.0-beta17`).  The header tuple
(`query_id, input_fasta, start_position, stop_position, strandedness,
rank, gene_number,
kegg_id, kegg_EC, kegg_bitScore, kegg_description, kegg_gene_name,
kofam_id, kofam_EC, kofam_bitScore, kofam_description, kofam_score_rank,
heme_regulatory_motif_count,
dbcan_id, dbcan_EC, dbcan_bitScore, dbcan_description, dbcan_score_rank`)
is the schema DRAM2 emits when `--use_kofam --use_kegg --use_dbcan` are
the enabled databases — this is what the parser pins against.

The single appended dbCAN row (`OWC_0000_k121_3157_2`, family `GT4`,
EC `2.4.1.-`) is synthetic and clearly marked here; it lets the offline
parse test verify the `dbcan_id` → `CAZY` namespace mapping without
needing to wait several minutes for a full live DRAM2 run.

## Live-run capture

A fresh live capture from the h100 install can be obtained with::

    source /storage/chenry/DRAM2/dram2-env.sh
    cd "$DRAM2_ROOT"
    nextflow run "$DRAM2_PIPELINE" -profile conda \\
        --annotate \\
        --input_genes <dir-with-input.faa> \\
        --outdir <abs-out> \\
        --use_kofam --use_dbcan --use_pfam --use_merops --use_vog

The result file lands at `<abs-out>/RAW/raw-annotations.tsv` and can be
trimmed and committed in place of this fixture without any parser
change, provided the schema columns documented above remain present.
