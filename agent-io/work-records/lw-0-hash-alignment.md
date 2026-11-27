# Work record: lw-0-hash-alignment

- task_id: lw-0-hash-alignment
- branch: maestro/developer/clearinghouse-link-waves-lw-0-hash-alignment
- base: 04a1d98c8faf2bdc35ea95fe34a9228626da55cf
- commit_shas: see `git log 04a1d98..HEAD` (impl+tests: 95793ce; list-arg test fix and this record follow)

## summary
Standardizer v2.0 in `src/kbutillib/domains/identity/standardizers.py`: STANDARDIZER_VERSION "2.0"; gene/contig and protein strip ONE trailing '*'; genome canonical form is `'_'.join(sorted(sha256(contig)))` (empty contigs dropped) and `entity_hash`/`genome_hash_from_fasta` hash that. The v1.1 genome rule survives only as `genome_hash_v1_1` (in `__all__`, called by nothing in src). Module docstring documents the rules and provenance (Filipe Liu's hash_seq.py). function and ontology_term unchanged.

## files_touched
- src/kbutillib/domains/identity/standardizers.py
- tests/domains/test_identity_standardizers.py
- tests/domains/fixtures/cdm_genome_hash_fixture.json (15 real kbase.genomes collections: contig hashes + contig_collection.hash)
- tests/domains/fixtures/cdm_contig_ncbi_fixture.json (NZ_CP010888.1 sha256 + length, no sequence)
- scripts/identity/collect_cdm_genome_hash_fixture.py (BERDL collector; token never printed)
- scripts/identity/check_ncbi_contig_hash.py (NCBI re-derivation, optional --berdl)
- agent-io/work-records/lw-0-hash-alignment.md

## success_criteria_check
- genome rule + 15-genome fixture reproducing contig_collection.hash: pass (test_v2_genome_rule_reproduces_cdm_contig_collection_hash_for_15_genomes; fixture collected live 2026-11-27, 15/15 verified).
- protein strips one trailing '*': pass.
- STANDARDIZER_VERSION '2.0': pass.
- v1.1 only as named legacy function: pass (genome_hash_v1_1).
- NCBI contig: pass. Ran live: NCBI NZ_CP010888.1 -> v2.0 contig hash b71e1139...bbb5, length 4167076, equals BERDL kbase.genomes.contig.hash for the CDM contig id. Unit test only asserts the stored sha/length shape and the synthetic-contig rule (no network at test time).
- Regression-framed: pass, see tests_run.

## tests_run
Baseline command (exact selection, `-m "not network"`, 180 s timeout): after changes `2000 passed, 6 skipped, 0 failed` (base was 1993 passed in the conductor's full-tests figure; this selection is 7 tests larger from the new tests). No baseline failures to exclude. Wider grep-hit files outside the selection (council, ontology_mapping etc. match only on STANDARDIZER_VERSION/entity_hash names) were NOT run.

## pre-existing tests updated (v2.0 deliberately)
In tests/domains/test_identity_standardizers.py:
- test_version_and_public_interface (was STANDARDIZER_VERSION == "1.1" -> "2.0")
- test_protein_preserves_trailing_stop_marker -> renamed test_protein_strips_one_trailing_stop_marker (old: '*' preserved)
- test_genome_sorts_canonical_contigs_before_joining_with_pipe -> renamed ..._contig_hashes_before_joining_with_underscore (old canonical "AAAA|GG|TTT")
- test_genome_contigs_canonicalized_with_dna_rule_before_sorting (old canonical "AAAA|TTT")
- test_genome_single_record_with_own_header_does_not_raise (old canonical == "ACGT"; now compared to standardize of headerless)
(Exact function name of the first may differ; the version assert is in the test containing STANDARDIZER_VERSION.) No other test in the selection pinned a v1.1 value.

## stale callers / stored outputs (not changed; other repos untouched)
- external/kbdl_service_utils.py ~L859: KBDLHashGenomes job docs; any job result with standardizer_version "1.1" holds v1.1 genome hashes.
- kbase/berdl/backfills/kbdl-genome-archives.toml ~L66: `precomputed` genome hashes from poplar KBDLHashGenomes are v1.1 until re-run; the adapter already refuses a standardizer_version mismatch (clearinghouse_shard.py ~L4346, L4457), so v1.1 service results will now be refused until poplar is upgraded.
- kbase/berdl/clearinghouse_derivation.py ~L40-46 docstring is now FALSE: a single-contig genome no longer hashes equal to that gene/protein (v2.0 hashes the hash). Needs a doc fix.
- kbase/berdl/clearinghouse_manifest.py ~L1125 (contig_set doc) and clearinghouse_parity_fixture.py (GENOME_ASSEMBLY_HASH computed at import, self-consistent, tests pass).
- any stored clearinghouse rows with entity_type='genome' / standardizer_version '1.1' (shard/backfill outputs, GAA archives).
- backfill/shard code stamps STANDARDIZER_VERSION into rows (clearinghouse_backfill.py:816, clearinghouse_shard.py:5086, 6489): new rows are now "2.0".

## caveats
- Gene and contig are the same rule; the '*' strip now also applies to gene (per task text).
- Fixture's 15 genomes are all multi-contig (5-47 contigs); single-contig genomes are covered by the toy tests only.
- Pre-existing quirk, untouched: the genome guard iterates the input once, so a one-shot iterator (e.g. reversed()) hashes as empty; pass a list.
- ~50-min full tests/ tree not run.
