# Work Record: transyt-module

## task_id
transyt-module

## branch
conductor/annotation-tool-modules/transyt-module

## commit_shas
- 4ed507c feat(transyt_utils): implement TransytUtils with Docker invocation and offline parse tests

## summary
Implemented `TransytUtils(AnnotatorUtils)` in `src/kbutillib/transyt_utils.py` per the annotation-tool-modules PRD (Transyt mechanism, Confront-resolved specs 9–10).  The module stages a protein FASTA + params.txt (and optional metabolites.txt) into a temp directory, invokes Transyt via Docker with a Neo4j readiness poll loop (no fixed sleep), parses `results/transyt.xml` (SBML) and `results/reactions_references.txt` into TC/MSRXN/MSCPD Terms keyed to caller gene IDs, and returns empty records on exit-8 (no resolvable taxonomy).  `is_available()` returns False without raising when the Docker image is absent.  Pure parse functions (`_parse_transyt_xml`, `_parse_reaction_tc`, `_parse_reactions_references`, `_build_annotation_records`) are split from the Docker path and covered 100% offline.  Golden fixtures (transyt.xml + reactions_references.txt) are committed under `tests/fixtures/transyt/`.  A KBUtilLib-owned `docker/transyt/Dockerfile` (derived from merlin-sysbio/kb_transyt, stripped of SDK layers) is included.  TransytUtils and the annotator base types are exported from `__init__.py`.

## files_touched
- `src/kbutillib/transyt_utils.py` — new module: TransytUtils, four pure parse helpers, module docstring
- `src/kbutillib/__init__.py` — added TransytUtils import + __all__ entry
- `config.yaml` — added `transyt.docker_image` and `transyt.neo4j_timeout` config keys
- `docker/transyt/Dockerfile` — KBUtilLib-owned Dockerfile derived from merlin-sysbio/kb_transyt
- `tests/annotators/test_transyt_utils.py` — 89 offline unit tests + 1 skipped live integration test
- `tests/fixtures/transyt/transyt.xml` — golden SBML fixture (3 reactions, 3 gene products, GPR associations)
- `tests/fixtures/transyt/reactions_references.txt` — golden reactions-to-ModelSEED mapping fixture

## success_criteria_check

- **TransytUtils.annotate raises on missing tax_id** — PASS.  `ValueError` raised before any Docker call when `tax_id=""` or omitted.
- **Stages protein.faa + params.txt** — PASS.  `_stage_inputs` writes both files; `metabolites.txt` written only when provided.  Covered by TestStageInputs (5 tests).
- **Invokes Transyt via the configured Docker image with Neo4j readiness poll (no fixed sleep)** — PASS.  `_build_docker_command` emits a `while [ $i -lt <timeout> ]` poll loop with `curl -fsS http://localhost:7474/`.  TestBuildDockerCommand confirms `"while"` in the inner script.
- **Parses transyt.xml + reactions_references.txt into TC/MSRXN/MSCPD Terms keyed to caller ids** — PASS.  TestBuildAnnotationRecords exercises all three term namespaces from golden fixtures, verifies caller-id keying and dedup.
- **Returns empty records on exit-8** — PASS.  TestAnnotateMocked::test_annotate_exit8_returns_empty_records.
- **is_available() returns False (no raise) when image absent** — PASS.  TestIsAvailable covers FileNotFoundError, non-zero exit, TimeoutExpired, OSError, and empty image tag.
- **Offline golden-fixture unit test passes with no Docker image** — PASS.  89 tests pass with Docker absent (1 live integration test skipped).
- **docker/transyt/ Dockerfile is present** — PASS.  `docker/transyt/Dockerfile` committed.
- **TransytUtils exported from __init__.py** — PASS.  TestExports verifies both attribute presence and identity.
- **fail_under=100 coverage gate for new code** — PASS.  `transyt_utils.py` at 100%, `annotator_utils.py` at 100%.

## tests_run

```
pytest tests/annotators/test_transyt_utils.py --cov=kbutillib.transyt_utils --cov-report=term-missing
89 passed, 1 skipped — Coverage: 100.00% on transyt_utils.py

pytest tests/annotators/ tests/guard/test_dependency_direction.py
142 passed, 1 skipped — Coverage: 100.00% on both transyt_utils.py and annotator_utils.py
```

Pre-existing test failures in `test_ms_biochem_deltag.py`, `tests/cli/`, `test_task_a_venv_doctor.py`, and `test_comprehensive_gapfill_wrapper.py` were already present on `main` and are unrelated to this task.

## caveats

1. **Transyt JAR/Neo4j DB not vendored.** The `docker/transyt/Dockerfile` references `vendor/transyt.jar`, `vendor/data.tar.gz`, and `vendor/workdir.tar.gz` which must be placed under `docker/transyt/vendor/` (or provided via `ARG *_URL`) before the image can be built.  The U Minho download URLs may rot; these should be mirrored to internal storage and the ARGs updated.

2. **Live integration test.** `TestTransytLiveIntegration::test_annotate_returns_result` is skipped everywhere the image is absent.  It can be run on any machine where `merlin-sysbio/kb_transyt:latest` (or the configured `transyt.docker_image`) is locally present.

3. **SBML parse assumes GPR in listOfGeneProductAssociations.** The fixture and the parse logic assume the SBML uses `<listOfGeneProductAssociations>` with `<geneProductAssociation reaction="...">` elements.  If a real Transyt version encodes GPR differently (e.g., directly in reaction notes or as MathML annotation), the parser would need updating.

4. **Phase 1 image strategy.** The default `transyt.docker_image` value is `merlin-sysbio/kb_transyt:latest`.  This uses the KBase SDK entrypoint override approach (Phase 1 from the PRD).  Phase 2 (build the slim KBUtilLib-owned image from `docker/transyt/Dockerfile`) requires pulling/vendoring the Transyt artifacts.

5. **DRAM2 and ProkkaUtils modules not in scope for this task.** This worktree implements Transyt only; the `annotator-base` branch (already merged to main) and other parallel tasks handle the remaining modules.
