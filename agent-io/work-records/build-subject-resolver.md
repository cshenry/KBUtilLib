# build-subject-resolver

- task_id: build-subject-resolver
- branch: maestro/developer/kbdl-arc-indexer-build-subject-resolver
- commit_shas: 927916c3b28bf502e51d13a1cc23443562921868 (feature); work-record commit follows

## summary
Extended src/kbutillib/kbdl_index/subject.py with Subject, SubjectResolver and build_resolver (locus_prefix untouched). resolve() is pure; prefixes are matched by equality against a prefix->[object_ids] table. Ambiguities (prefix collision, multi-genome BuildGenome) return None and are recorded in resolver.ambiguities as (job_id, reason).

## files_touched
- src/kbutillib/kbdl_index/subject.py
- tests/kbdl_index/test_subject.py (new)

## success_criteria_check
- Four names exposed: pass
- No network in resolve(): pass (offline tests)
- Six named fixture tests incl. collision -> None: pass (plus multi-genome test)
- No base-passing test fails: pass for tests/kbdl_index; full suite not run

## tests_run
Baseline command + tests/kbdl_index: 11 passed.

## caveats
- Ambiguity channel is the public attribute resolver.ambiguities (my design choice).
- BuildGenome with no genomes or malformed record returns None.
- Annotation Subject also fills genome_id/taxonomy when a swept BuildGenome result covers the fasta object.
