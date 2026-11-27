# Work record: lw-3-ontology-loader

- task_id: lw-3-ontology-loader
- branch: maestro/developer/clearinghouse-link-waves-lw-3-ontology-loader
- base: 197f39d3bbbad46142021292f563fe6ee31c7982
- commit_shas: 04fc9f10d2e4f829d595de9253c3a9692c6c0ccf (code, tests, fixtures, script); the work-record commit follows it (see git log)

## summary
New module src/kbutillib/domains/kbase/berdl/clearinghouse_ontology_wave.py. build_ontology_wave() turns store statements (read at one pinned Iceberg snapshot), parsed KO lines and parsed COG lines into: ontology_term and function entity/content rows, function_x_ontology_term, function_x_function (alias) and ontology_term_x_ontology_term links, six method rows, the term -> function-text map (TSV via write_term_function_map) and a dry-run summary (OntologyWaveResult.dry_run_summary(): counts per table and per method plus a run report). It writes nothing to the lake. All links go through lw-1's validate_link_rows and methods through validate_method_rows (no re-implementation). run_ontology_wave() orchestrates: sha256-verifies the KO/COG files before touching the store, resolves ONE snapshot, reads every query AS OF it.

## method_names the loader registers
- ontology-definition@<snapshot>/kbase-ontology-source (label links; Pfam description links)
- ontology-synonym@<snapshot>/kbase-ontology-source (exact_synonym links and the alias function_x_function rows)
- ontology-xref@<snapshot>/kbase-ontology-source (oio:hasDbXref xref, owl:equivalentClass exact_match)
- ontology-definition@2026-11-24/kegg-ko (KO definition links; source_dataset = KO file sha256)
- kegg-ko-ec@2026-11-24/kegg-ko (KO -> EC xref; parameters_json '{}')
- ontology-definition@cog-24/ncbi-cog (COG name links; source_dataset = cog-24.def.tab sha256)
KO/COG methods are emitted only when those files are supplied. parameters_json for the store methods records the ONTOLOGIES prefixes (keys lower-cased, because parameter-set keys must match ^[a-z][a-z0-9_]*$), synonym_predicates and stoplist_version. Descriptions are static text.

## files_touched
- src/kbutillib/domains/kbase/berdl/clearinghouse_ontology_wave.py (new)
- tests/berdl/test_clearinghouse_ontology_wave.py (new, 57 tests)
- tests/berdl/fixtures/ontology_wave/statements.json (real store rows, 37), ko_list_slice.tsv (3 real KO lines), cog_def_slice.tab (3 real COG lines) (new)
- scripts/clearinghouse_ontology_wave_dry_run.py (new; real-data check against the full files and, optionally, the store)
- agent-io/work-records/lw-3-ontology-loader.md (new)
- Outside the repo: staged KBERDL research/cog-24.def.tab (+ cog-24.def.tab.sha256), sha256 f4f0007b356e81dac2afc46d062411bc65dce4de6d3c5d6ce4779687a7193de3, downloaded from ftp.ncbi.nlm.nih.gov/pub/COG/COG2024/data/ (COG2024 release, 5061 COGs). Not committed to any repo by me.

## success_criteria_check
- Fixture -> exactly the hand-written expected rows for the five tables: PASS (test_fixture_yields_exactly_the_hand_written_rows; expected hashes via hashlib, not the loader).
- Refuses a KO file with the wrong sha256: PASS (KO and COG; also refused before any store query).
- Reads one pinned snapshot: PASS for the SQL (every query has VERSION AS OF <id>; resolved once). CAVEAT below: auto-resolution is not possible through the BERDL MCP SQL endpoint.
- Ignores related/narrow/broad synonyms: PASS (12 real ignored statements; output identical with them removed; counted in report).
- Non-Pfam definitions are term content only: PASS.
- Cross-references only when both CURIEs resolve, xref stored directed: PASS (directed reverse-pair test; unresolved reported by predicate and raw prefix).
- Persisted KO -> function-text map: PASS (TSV for every function-bearing term, header + 5 columns; also GO/EC/SEED role/InterPro/Pfam/COG).
- Stop-list applied: PASS (KO K00243 quarantined; family name 'DUF1234 domain-containing protein' kept; a stop-listed label is quarantined).
- Dry-run count summary: PASS.
- Every emitted link passes lw-1 register_links: PASS (registered through ClearinghouseCapability.register_links on the lw-1 in-memory lake; second pass fill-only).
- Regression framing: PASS (baseline selection 2152 passed -> 2209 passed, 6 skipped, 0 failed; +57 = the new tests).

## tests_run
- env PYTHONPATH=src .../KBUtilLib-py3.13/bin/python -m pytest tests/domains/test_identity_standardizers.py tests/domains/test_identity_parameter_sets.py tests/berdl/ tests/cli/test_clearinghouse.py tests/cli/test_clearinghouse_aliases.py tests/cli/test_clearinghouse_backfill_cli.py -q -p no:cacheprovider --no-header -m "not network": 2209 passed, 6 skipped, 0 failed, 21 s. Full tests/ tree NOT run (about 50 min).
- Real-data script on the full files: KO 28,598 lines -> 28,261 function links (337 stop-listed KO texts quarantined), COG 5,061 -> 5,060 links (1 quarantined). With the store read UNPINNED and limited to EC, Pfam, KEGG pathway and SEED subsystem: term counts EC 7,251, Pfam 25,545, KEGG pathway 867, SEED subsystem 2,081 (all equal the 2026-11-24 lake measurements), 9,918 term->term xref rows (mostly KO -> EC), largest alias clique 51 texts (EC:2.4.1.17). GO, SEED role, InterPro, Rhea, SEED reaction and MetaCyc were not read in that check (time).

## pre-existing tests changed
None.

## caveats
- SNAPSHOT RESOLUTION: the BERDL MCP SQL endpoint answers HTTP 400 to every non-plain-SELECT statement (.snapshots, .history, DESCRIBE, VERSION AS OF probes). resolve_snapshot_id() therefore raises SnapshotResolutionError from this machine; callers must pass snapshot_id (resolved in-pod via Spark). The VERSION AS OF SQL itself could not be executed against the lake from here and is untested against a real engine; it is the standard Iceberg/Spark form.
- COG release label: method uses 'cog-24' (file cog-24.def.tab, COG2024 release) as the <release>.
- Term identity: term_id and hashes use the standardized CURIE, so 'seed.role:' becomes 'SEED.ROLE:' (namespace upper-cased by the ontology_term standardizer). The ModelSEED role URL subject form maps to 'seed.role:<tail>'.
- Real store quirks handled: duplicate statement rows are deduplicated; hasDbXref values may sit in object instead of value (Rhea/seed.reaction) and may carry a trailing ';' or lower-case prefix ('rhea:19363;'); owl:equivalentClass objects are blank nodes for the fixture ontologies (reported unresolved); the equivalentClass exact_match path is only exercised by a SYNTHETIC test statement.
- KO terms: name = definition with the EC bracket removed, definition = the full KEGG text. COG terms: name = COG name, namespace 'COG', definition NULL. Pfam function-link field is the selected predicate ('dcterms:description' or 'IAO:0000115').
- Stop-listed or text-less terms: map status 'quarantined' (any ontology, not only KO) or 'no_text' (Pfam without description), function_entity_hash empty. The term itself is still loaded.
- Alias rows use method ontology-synonym@<snapshot>/...; cliques are within one term across label, exact synonyms and the Pfam/KO/COG text (KO/COG/Pfam cliques are size 1 so produce none).
- Self xref pairs are skipped and counted; KO bracket ECs like 1.1.1.- are tried and reported unresolved when no such EC term is loaded.
- Entity/content rows deliberately omit observed_at/ingest_batch_id (write path owns them).
