# Fixture provenance (lw-9, KBDL genome loader)

- `adp1_annotation_slice.json` -- the REAL ADP1 annotation result
  (`annot_result_f072e65c6f784629852a922b3bad4d75.json`, 3,256 genes; KBDL job
  f072e65c...) cut to three genes, entries copied verbatim: `82eef07a1eadf57a_1`
  (dnaA), `_4` (gyrB), `_5` (DUF2171 domain-containing protein, minus strand).
- `adp1_genome_slice.json` -- a CanonicalGenome-shaped object RECONSTRUCTED, not
  downloaded: KBDL genome object `aa50dced...` was unreachable from the laptop
  (poplar:8791 does not resolve; no copy on disk). Built from the real NCBI
  GCF_000046845.1 sequence (contig NC_005966.1, first 7,000 bases; coordinates are
  unchanged because the window starts at base 1) and the real NCBI CDS
  coordinates/sequences of the first five CDS of the KBase ADP1 genome
  (`ADP1Notebooks/notebooks/datacache/ADP1Genome.json`). KBDL feature `_N` is
  taken to be the N-th CDS; that mapping was checked against the annotation
  (BAKTA FUNCTION text equals the NCBI product for CDS 1-5 except where noted in
  the work record). Each `dna_sequence` was verified equal to the contig slice its
  location names (reverse-complemented on `-`). `source_id` is the NCBI locus tag.
  The contig is a 7,000 bp window, so the genome hash differs from the real one.
- `term_function_map.tsv` -- written by W1's `build_ontology_wave` +
  `write_term_function_map` over the real pinned KO list and COG definitions
  (KBERDL research/) and hand-typed GO / EC labels (the ontology store is not
  reachable from the laptop); only the shape and the KO / COG texts are real.
