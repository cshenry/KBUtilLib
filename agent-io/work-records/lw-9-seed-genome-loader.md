# Work record: lw-9-seed-genome-loader

- task_id: lw-9-seed-genome-loader
- branch: maestro/developer/clearinghouse-link-waves-lw-9-seed-genome-loader (base ae6c9f602525d707cb4cb843f39862ddcd07b24f)
- commit_shas (chronological; the work-record commit follows these and is the branch tip):
  - c37081bef37be8ec96e99810fc077d0a0f0a2e02 fix: method_name pattern check uses fullmatch (the conductor's separate commit)
  - 9c3e374e2655bcf494db5e6ecd46c4d2b77fdc6c feat: protein_x_function accepts term-id rows (validator extension, see caveat 1)
  - 20800d8661c0975b5e7d9c851df6f01472ee4b4f feat: load_kbdl_genomes + inputs module + fixtures + tests
  - b7e23885e5381b7b0431b7840fe50a63a316d4d8 feat: kbu clearinghouse load-kbdl-genomes
  - 06e70462fd4de369c042fc0f2a00d936351077cf docs

## summary

Adds the public KBDL genome loader. `load_kbdl_genomes(genome_object_ids, *, namespace, external_ids, dry_run, ...)` turns CanonicalGenome objects plus their `KBDLGenomeAnnotation` results into genome/gene/protein/function entities, genome aliases, a `method` row per cited tool, and `gene_x_genome` / `gene_x_protein` / `protein_x_function` links, and writes them genome-by-genome under the namespace writer lock (methods, then entities+content, then aliases, then links; all fill-only, so a re-run writes zero rows). `build_genome_rows` is the pure stage; every link and method row is validated by lw-1's `validate_link_rows` / `validate_method_rows`. The lw-12 registry is checked in as `KBDL_ANNOTATOR_METHODS`. Inputs (archive prefix index, term-map reader, external-id builder for the three ledgers, fetch helper) are in `clearinghouse_genome_inputs.py`. CLI verb `kbu clearinghouse load-kbdl-genomes`.

## files_touched

- src/kbutillib/domains/kbase/berdl/clearinghouse_genome_loader.py (new)
- src/kbutillib/domains/kbase/berdl/clearinghouse_genome_inputs.py (new)
- src/kbutillib/domains/kbase/berdl/__init__.py (exports `load_kbdl_genomes`, `KBDL_ANNOTATOR_METHODS`)
- src/kbutillib/domains/kbase/berdl/clearinghouse_links.py (fullmatch fix; term-id rule)
- src/kbutillib/domains/kbase/berdl/clearinghouse_capability.py (`schema_method_ok`: same `.match` -> `.fullmatch`)
- src/kbutillib/interfaces/cli/clearinghouse.py (verb + render branch)
- tests/berdl/test_clearinghouse_genome_loader.py (new), tests/cli/test_clearinghouse_load_kbdl_genomes_cli.py (new)
- tests/berdl/test_clearinghouse_links.py (one extra refusal case, one new test, three term-id tests)
- tests/berdl/fixtures/kbdl_genome_loader/{adp1_genome_slice.json, adp1_annotation_slice.json, term_function_map.tsv, PROVENANCE.md}
- agent-io/docs/kbu-clearinghouse-cli.md (new section)

## method-name mapping (registry raw -> loaded name)

Rule: `<tool lower-cased, underscores kept>@<version>/kbdl-default`; TRANSYT's name version is `sha256-<digest>`. `algorithm`, `algorithm_version` (TRANSYT: the full image reference; SOURCE_GFF: `unknown`) and `parameters_json` are the measured raw values. Every loaded name is tested with `re.fullmatch` against the lw-1 pattern; every raw name is tested to FAIL it.

| tool | registry method_name (raw) | loaded method_name |
|---|---|---|
| PROKKA | PROKKA@1.14.6/kbdl-default | prokka@1.14.6/kbdl-default |
| BAKTA | BAKTA@1.11.4/kbdl-default | bakta@1.11.4/kbdl-default |
| KOFAMSCAN | KOFAMSCAN@1.3.0/kbdl-default | kofamscan@1.3.0/kbdl-default |
| DRAM2 | DRAM2@2.0.0-beta17/kbdl-default | dram2@2.0.0-beta17/kbdl-default |
| TRANSYT | TRANSYT@kbutillib/transyt:latest@sha256:92f6...4fe7/kbdl-default | transyt@sha256-92f6e78fbdb8dc1952ad0c4130776f4fe6f5cf524b3f4f3cbd30b3ddbe8c4fe7/kbdl-default |
| RAST_API | RAST_API@2018-0531/kbdl-default | rast_api@2018-0531/kbdl-default |
| BAKTA_CALL | BAKTA_CALL@1.11.4/kbdl-default | bakta_call@1.11.4/kbdl-default |
| PYRODIGAL | PYRODIGAL@3.7.1/kbdl-default | pyrodigal@3.7.1/kbdl-default |
| PYARAGORN | PYARAGORN@0.3.0/kbdl-default | pyaragorn@0.3.0/kbdl-default |
| PYBARRNAP | PYBARRNAP@0.5.2/kbdl-default | pybarrnap@0.5.2/kbdl-default |
| SOURCE_GFF | SOURCE_GFF@unknown/kbdl-default | source_gff@unknown/kbdl-default |
| LOADED_FROM_INPUT (12th; NOT a poplar measurement, see Chris decisions) | (none) | loaded_from_input@unknown/kbdl-default |

The result files call RAST's tool key `RAST`; the loader maps it to `RAST_API`. `source_dataset` of every method row is the sha256 of the lw-12 registry file (e937e88f...b779). TRANSYT is never cited, so its method row is never registered.

## success_criteria_check

- Emits hand-written expected genome/gene/protein/alias/method/gene_x_genome/gene_x_protein/protein_x_function rows for three genes incl. a minus-strand gene: PASS on the fixture (hand-written expected rows in tests; minus gene begin 6712, end 6948). CAVEAT: the genome object is reconstructed, see caveat 2.
- Classifies the real PROKKA and DRAM2 FUNCTION lists by the explicit patterns: PASS (full 3,256-gene real result when on disk, plus the three-gene fixture).
- Links term ids through W1's term map with the CURIE as link_key: PASS (needed a validator extension, caveat 1). The map in the tests was produced by W1's own `build_ontology_wave`/`write_term_function_map`.
- Ignores KOFAMSCAN FUNCTION and TRANSYT with counts: PASS (`kofamscan_function_ignored`, `transyt_not_loaded:TRANSYT/<ns>`).
- Reports dna_sequence/contig-slice mismatches and multi-location features: PASS (`dna_sequence_mismatch`, `multi_location_feature`, with feature ids; plus a minus-strand-as-plus mismatch test).
- Builds the external-id map from the three ledgers: PASS on the real ledgers (66 genome objects / 67 external ids; see caveat 4); field names read from the files.
- Finds annotation results in the sweeper archive by gene-id prefix: PASS, also on the real Dropbox mirror (ADP1 prefix 82eef07a1eadf57a -> f072e65c...json.gz, 3,256 genes).
- Cites only KBDL_ANNOTATOR_METHODS methods: PASS (test: cited names are a subset of the registry names; a tool absent from it is refused and counted, e.g. PSORTB).
- Exposed as load_kbdl_genomes plus the load-kbdl-genomes verb: PASS (package export test, CLI tests).
- Writes zero rows on a re-run: PASS (library and CLI, `_MiniLake` stores what load() is handed; the re-run makes no load() call).
- Every emitted link passes register_links validation: PASS (the real `register_links` path ran against `_MiniLake`, including the orphan/method checks).
- Regression framing: PASS, see tests_run.

## tests_run

Baseline command (conductor's, plus the new CLI test file), `env PYTHONPATH=src .../KBUtilLib-py3.13/bin/python -m pytest tests/domains/test_identity_standardizers.py tests/domains/test_identity_parameter_sets.py tests/berdl/ tests/cli/test_clearinghouse.py tests/cli/test_clearinghouse_aliases.py tests/cli/test_clearinghouse_backfill_cli.py tests/cli/test_clearinghouse_load_kbdl_genomes_cli.py -q -p no:cacheprovider --no-header -m "not network"`:
**2291 passed, 0 failed, 6 skipped** (baseline at ae6c9f6: 2209 passed, 0 failed, 6 skipped; +82 new tests, 0 regressions). The full tests/ tree was not run.

## ADP1 dry run

The KBDL genome object `aa50dced...` could NOT be read: poplar.cels.anl.gov:8791 does not resolve from this laptop and no copy of the CanonicalGenome JSON is on disk (searched Science, ADP1Notebooks, runs, ~/.kbdl). I did not fabricate it. What I ran against REAL inputs:

`load_kbdl_genomes([aa50dced...], external_ids=build_external_ids(), dry_run=True, archive_dirs=[Dropbox mirror])` on the three-gene reconstructed object slice (so the numbers below are for 3 genes, not 3,256): external ids from the real ledgers (ADP1: ncbi_assembly GCF_000046845.1, taxon 62977, fasta 82eef07a...), the real prefix index over the 3,327-file mirror found `f072e65c6f784629852a922b3bad4d75.json.gz`, 0.5 s (cached index).

- rows per table: genome_entity 1, genome_content 1, genome_alias 2, gene_entity 3, gene_content 3, protein_entity 3, protein_content 3, function_entity 21, function_content 21, method 6, gene_x_genome 3, gene_x_protein 3, protein_x_function 33
- rows per method: bakta 19 pxf; dram2 5; kofamscan 2; prokka 5; rast_api 2; source_gff 3 gene_x_genome + 3 gene_x_protein
- skips: gene_symbol_not_loaded PROKKA/FUNCTION 2; kofamscan_function_ignored 2; stoplisted_function_text PROKKA/FUNCTION 1 and RAST_API/role 1; vog_not_loaded DRAM2/FUNCTION 2

Census of the FULL real result (3,256 genes; distinct values per gene) by (tool, namespace, class), no genome object needed: BAKTA FUNCTION text 3,100 + 156 stop-listed, GO 3,944, EC 952, KO 939, COG 344; PROKKA FUNCTION text 1,925, stop-listed 1,102, symbol 1,902, EC 1,151, COG 1,382; DRAM2 FUNCTION text 2,767, stop-listed 96, KO 2,656, EC 1,841, VOG 713, symbol 258; KOFAMSCAN KO 2,003, FUNCTION ignored 1,992; RAST role text 2,935, stop-listed 469; TRANSYT not loaded: MSCPD 6,712, TRANSYT_RXN 6,051, MSRXN 1,135, TC 269. Whether each GO/EC/KO/COG resolves depends on the W1 term map, which does not exist on this machine (needs the lakehouse); the real full dry run needs the genome object plus that map.

## pre-existing tests changed

None changed or deleted. Additions to tests/berdl/test_clearinghouse_links.py only: one more parametrized bad name (`a@1/b\n`), one fullmatch test, three term-id tests.

## caveats

1. **Contract conflict, resolved by a validator extension (reviewer: please look).** The task asks for protein_x_function rows with link_key = term CURIE and metadata.term_id for GO/EC/KO/COG, but lw-1's validator allowed a non-empty link_key only for KofamScan rows (metadata.ko_id). Emitting the spec'd rows would have failed register_links, so commit 9c3e374 adds: a row with `metadata.term_id` is valid iff `term_id == link_key` and it is a CURIE (ko_id rows unchanged; both together must agree). Rows with a non-empty link_key and neither key are still refused. KofamScan rows carry both `ko_id` and `term_id`. The alternative (link_key '' and no term_id for non-KO terms) would drop the CURIE and merge two terms sharing a function text.
2. **The ADP1 genome object is reconstructed, not real.** See PROVENANCE.md. Real: the annotation slice (verbatim), the NCBI contig sequence, NCBI CDS coordinates and sequences (every dna_sequence verified equal to the contig slice, minus gene reverse-complemented). Reconstructed: the KBDL object wrapper, the mapping of KBDL feature `_N` to the N-th NCBI CDS (checked: BAKTA FUNCTION text equals the NCBI product for CDS 1-5 but NOT for CDS 6-8, so the ordinal mapping is only trusted for the three chosen genes), `source_id` = NCBI locus tag, and the contig is a 7,000 bp window. GO/EC labels in the term map are hand-typed (ontology store unreachable); KO/COG texts are real (pinned files). The loader is exercised on the real object only after `fetch_genome_objects` runs on kbhub/h100.
3. **Gene id form.** The ROWS text says `'KBDL:' + feature id` but the Gene-ids section and lw-1's validator require `KBDL:<genome_hash[:16]>.<contig ordinal>.<cds ordinal>`; I followed the validator. The KBDL feature id and source id are kept in gene_x_genome metadata (`feature_id`, `source_id`). Ordinals are positional (contig order; rank by begin, end, id among single-location features), assigned before any skip so skips never renumber.
4. **External ids.** 66 genome objects carry 67 external ids: Fitness organisms SyringaeB728a and SyringaeB728a_mexBdelta share one genome object, so it gets two `fitness_org` aliases (list value; supported). Only ADP1 states its NCBI assembly; the ledgers' `taxonomy.reference` is the ANI-nearest reference and is deliberately NOT used as ncbi_assembly, so Fitness/PMI genomes get no ncbi_assembly alias. `gene_call_tool` = SOURCE_GFF for all three ledgers on the evidence that each was built from supplied FASTA+GFF (ADP1 `source`; fitness `gff` files; PMI `gff_path`); a genome with no gene_call_tool cites loaded_from_input (superseded the earlier PYRODIGAL default, see Chris decisions) and says so in its report.
5. Function content for a term-linked function uses W1's STANDARDIZED text (the map holds no display text); content is fill-only, so W1's display text wins when W1 loaded first.
6. `fasta_reference` is NULL (and reported) when `fasta_object_id` is not in external_ids; `taxon_id`/`assembly_accession` only when known; `is_closed` is NULL.
7. `load_kbdl_genomes` requires the term map (path or `$KBU_CLEARINGHOUSE_TERM_FUNCTION_MAP`) and raises otherwise, rather than silently dropping every GO/EC/KO/COG link.
8. Uses `ClearinghouseCapability` internals (`_writer_lock`, `_register_methods`, `_register_links`, `_held_values`, `_alias_clock`) so one batch holds the writer lock once (the public register_methods/register_links each take it themselves and the lock is not re-entrant). Entity/content existence is checked with `_held_values` (chunked IN), not by the ingest path's range-window anti-join; fine at 67 genomes, worth revisiting for very large batches.
9. The live-KBDL fallback and `fetch_genome_objects` are read-only but untested against a real service (unreachable here); they are tested with fake clients. The sweep-ledger `locus_prefix` field they rely on was read from sweep.py, not from a real ledger (none on this machine).
10. An unreadable/malformed genome is reported in `LoadReport.failed` (CLI exits 1) and the batch continues with the other genomes.

## Chris decisions 2026-11-27 (relayed by the conductor, ~15:58 CST)

1. The protein_x_function term_id/link_key validator extension 9c3e374 is APPROVED as built.
2. PMI keeps source_gff -- no change.
3. CHANGE REQUIRED: when a genome has no gene_call_tool, do NOT default to PYRODIGAL. Chris's words: "cite loaded_from_input". Implemented in one new commit: a 12th `KBDL_ANNOTATOR_METHODS` entry `LOADED_FROM_INPUT` (method_name `loaded_from_input@unknown/kbdl-default`, algorithm `loaded_from_input`, algorithm_version `unknown`, version_kind null, parameters_json `{"parameter_status":"unknown"}`, source_dataset the same registry-file sha256 as the other entries). It is NOT a poplar measurement: it records Chris's decision that gene calls are taken as they arrived in the genome object, caller unknown. A genome with no `gene_call_tool` cites it for gene_x_genome and gene_x_protein and its report carries skip/note `gene_caller_unknown_loaded_from_input`. PYRODIGAL is cited only when the ledger says so. Baseline plus the CLI test file: 2292 passed, 0 failed, 6 skipped.
