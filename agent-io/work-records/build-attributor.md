# Work record: build-attributor

- task_id: build-attributor
- branch: maestro/developer/kbdl-arc-indexer-build-attributor
- commit_shas: see `git log d2eb2cc..HEAD` (code commit, then this record)

## summary
Added src/kbutillib/kbdl_index/attribution.py: build_attributor, Attributor.attribute, Attribution, scan_arc_tree_mentions, load_existing_rows. Precedence submission > existing_row > arc_tree_mention; multi-arc tree mentions contribute nothing and are counted (multi_arc_discarded); jobs whose sources name different arcs are counted (disagreeing_jobs). The scan streams 1 MiB chunks with overlap, skips unreadable files, and only reads.

## files_touched
- src/kbutillib/kbdl_index/attribution.py
- tests/kbdl_index/test_attribution.py
- agent-io/work-records/build-attributor.md

## success_criteria_check
- Interface exposed: pass.
- Precedence enforced by tests: pass.
- Two-arc job attributes to neither: pass.
- Writes nothing: pass (read-only code paths).
- Six named offline tests: pass.
- No base-passing test fails: pass for scoped run (new module only; no existing file edited).

## tests_run
- /home/chenry/venvs/kbu-modeling/bin/python -m pytest -q -p no:cacheprovider tests/kbdl_index : 20 passed. Full baseline suite not run (new module + tests only).

## caveats
- load_existing_rows misses run-DB rows under arcs absent from the runs tree (documented in docstring).
- Tests use a stub store for load_existing_rows rather than FakeKorosArcStore.
- Scan only considers files under <project>/arcs/<slug>/; other files are ignored. Disagreement count excludes multi-arc jobs.
- Unreadable-file test is lenient when run as root.
