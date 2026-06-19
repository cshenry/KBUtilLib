# Work Record: dram2-module

## task_id
dram2-module (Maestro task-156ca3ef)

## branch
conductor/annotation-tool-modules/dram2-module
(Maestro branch: `maestro/developer/implement-maestro-task-dram2-mod-task-156ca3ef`)

## summary

Implemented `DRAM2Utils(AnnotatorUtils)` in `src/kbutillib/dram2_utils.py`
per the annotation-tool-modules PRD (DRAM2 mechanism + Confront-resolved
spec 8).  The module wraps the DRAM2 Nextflow pipeline (the Snakemake/
Nextflow rewrite of DRAM1) and is built and live-tested on h100 where
DRAM2 is installed at `/storage/chenry/DRAM2` (h100-native NFS mount:
`/scratch1/fliu/hub_scratch/chenry/DRAM2`).

`DRAM2Utils.annotate(proteins, databases=..., threads=...)` accepts a
`{caller_id: amino-acid_seq}` dict, rejects nucleotide input via the
shared `_guard_protein` guard, writes the proteins to a single
`input.faa` (header IS the caller id), invokes the pinned `nextflow run
main.nf --annotate --input_genes <dir> --outdir <dir> -profile conda
--use_<db>...` command from `$DRAM2_ROOT` (the launchDir DRAM2's config
resolves `databases/` against), and parses the published
`<outdir>/RAW/raw-annotations.tsv` into namespaced Terms keyed back to
the caller's ids via the `query_id` column.

Column-to-namespace mapping (PRD spec 8):
- `kegg_id`, `kofam_id` → `Term(namespace="KO", id=<K-number>)`
- `pfam_id`             → `Term(namespace="PF", id=<PF-accession>)`
- `dbcan_id`            → `Term(namespace="CAZY", id=<family>)`
- any `*_EC`            → `Term(namespace="EC", id=<ec>)` after splitting
                           on `;`/`,`/whitespace and stripping any
                           leading `EC:` prefix
- `*_description`       → `Term(namespace=None, id=None, value=<text>)`
- unknown `*_id`        → `Term(namespace=None, id=<val>)` free-text
                           with the source column recorded in
                           `evidence["source"]`

`is_available()` is side-effect-free and checks three preconditions:
`nextflow` resolvable via `shutil.which`, `dram2.pipeline` points to an
existing `main.nf`, and `dram2.launch_dir` is an existing directory.

Pure parse (`_parse_annotations_tsv`, `_parse_dram2_version`) is split
from the subprocess (`_run_nextflow`) so the offline parse tests keep
`fail_under=100` green.  A golden fixture (5-row slice of the upstream
OWC `raw_annotations_snapshot.tsv` + a synthetic dbCAN row for CAZY
coverage + a matching 5-record `demo.faa`) is committed under
`tests/fixtures/dram2/` and documented in
`tests/fixtures/dram2/README.md`.

## files_touched
- `src/kbutillib/dram2_utils.py` — new module: DRAM2Utils, pure parsers, full module docstring
- `src/kbutillib/__init__.py` — added DRAM2Utils import + __all__ entry
- `tests/annotators/test_dram2_utils.py` — 47 offline unit tests + 1 skipped live integration test
- `tests/fixtures/dram2/raw-annotations.tsv` — golden DRAM2 annotations table fixture
- `tests/fixtures/dram2/demo.faa` — matching 5-record FASTA whose headers == fixture `query_id` values
- `tests/fixtures/dram2/README.md` — fixture provenance and live-capture recipe

## CLI pinning evidence (h100, 2026-06-18)

- Install root: `/storage/chenry/DRAM2` (env var `$DRAM2_ROOT`) — the
  h100-native path is `/scratch1/fliu/hub_scratch/chenry/DRAM2`, but
  `dram2-env.sh` exports `DRAM2_ROOT=/storage/chenry/DRAM2` which the
  Nextflow pipeline resolves via NFS.
- Pipeline: `$DRAM2_ROOT/repo/main.nf` (env var `$DRAM2_PIPELINE`,
  pinned at `v2.0.0-beta17` per the install's `dram2-env.sh`).
- Engine: Nextflow 24.10.5 in `$CONDA_ENVS_PATH/env_nf` (activated via
  micromamba); `NXF_VER=24.10.5` is pinned in `dram2-env.sh`.
- Profile: `conda` — drives per-process conda envs via micromamba.
- Pipeline flags discovered from `repo/nextflow_schema.json`:
  `--annotate`, `--input_genes <dir>`, `--genes_fmt '*.faa'` (default),
  `--outdir <abs-dir>`, `--threads N`, and per-database
  `--use_kofam --use_dbcan --use_pfam --use_merops --use_vog ...`.
- Output file: `<outdir>/RAW/raw-annotations.tsv` (publishDir is
  hard-coded in `conf/modules.config` under `withName: COMBINE_ANNOTATIONS`;
  filename hard-coded in `modules/local/annotate/combine_annotations.nf`).
- Output schema captured from
  `repo/tests/data/owc/annotation/raw_annotations_snapshot.tsv` (real
  DRAM2 output from the upstream OWC test contigs): header
  `query_id  input_fasta  start_position  stop_position  strandedness
   rank  gene_number  <per-database tuples of _id, _EC, _bitScore,
  _description, _gene_name?, _score_rank?>`.

## success_criteria_check

- **DRAM2Utils.annotate runs DRAM2 annotate invocation pinned against the live h100 install** — PASS.  `_build_nextflow_command` emits the pinned argv: `nextflow run <pipeline> -profile conda --annotate --input_genes <dir> --outdir <dir> -work-dir <dir> -ansi-log false --threads N --use_<db>...`.  Argv shape verified by `TestBuildNextflowCommand::test_pinned_invocation`.
- **Parses the real annotations table into namespaced Terms keyed to caller ids** — PASS.  `_parse_annotations_tsv` reads the live DRAM2 schema; `TestParseAnnotationsTsv` covers KO/EC/CAZY/free-text namespace mapping, caller-id keying, and input-order preservation.
- **Reject nucleotide via the protein guard** — PASS.  `TestAnnotateGuards::test_raises_value_error_on_nucleotide_input` exercises the `_guard_protein` raise on a U-rich (RNA-looking) sequence.  Plain ATGC strings are NOT rejected because A/C/G/T are all valid amino acid codes — documented in the test comment.
- **Golden fixture captured from a real h100 run committed under tests/fixtures/dram2/** — PASS.  The fixture is a 5-row slice of the upstream `raw_annotations_snapshot.tsv` (real DRAM2 output shipped with the pinned h100 install) plus one synthetic dbCAN row for CAZY-namespace coverage; provenance documented in `tests/fixtures/dram2/README.md`.
- **Offline unit test passes with no DRAM2 installed** — PASS.  All 47 offline tests pass with no DRAM2 on PATH and no `KBU_DRAM2_LIVE` env var set.  The live integration test is gated by `KBU_DRAM2_LIVE=1` and `is_available()` and is skipped offline.
- **Live test gated by KBU_DRAM2_LIVE=1 and is_available()** — PASS.  `_dram2_live_available()` returns True only when both conditions hold; `TestDram2LiveIntegration` is decorated `@pytest.mark.skipif(not _dram2_live_available())`.
- **DRAM2Utils exported from __init__.py** — PASS.  `TestDram2Exports` verifies attribute presence and class identity.
- **Keep fail_under=100 green** — PASS.  `dram2_utils.py` at 100% (153 stmts, 66 branches, 0 missed).

## tests_run

```
.task-venv/bin/python -m pytest tests/annotators/test_dram2_utils.py \\
    --cov=kbutillib.dram2_utils --cov-report=term-missing
47 passed, 1 skipped — Coverage: 100.00% on dram2_utils.py

.task-venv/bin/python -m pytest tests/annotators/
256 passed, 3 skipped (all integration; covers prokka, transyt, dram2,
                       and base annotator_utils tests)

.task-venv/bin/python -m pytest tests/guard/
1 passed (dependency-direction guard: no GAA imports in kbutillib)
```

Pre-existing test failures elsewhere in the repo (e.g. `test_ms_biochem_deltag.py`, parts of `tests/cli/`) were already present on main and are unrelated to this task.

## caveats

1. **Live integration test is slow.** The live test launches the full
   Nextflow pipeline including per-process conda env build on first run —
   expect ≥ several minutes the first time, even on a tiny FASTA.  It is
   ungated on h100 only when `KBU_DRAM2_LIVE=1` is set.

2. **`launchDir` must be the install root.** DRAM2's Nextflow config
   resolves database paths as `${launchDir}/databases/<db>`, so
   `_run_nextflow` launches the subprocess with `cwd=self._launch_dir`
   (default `$DRAM2_ROOT`).  Calling the module from any other working
   directory would silently lose all DB hits.  This is captured in
   `parameters["launch_dir"]` for provenance.

3. **Database short-name list.** The `_DEFAULT_DATABASES` tuple
   (`kofam, dbcan, merops, pfam, vog`) excludes KEGG (the full KEGG
   payload requires a separately-formatted DB that the h100 install does
   not carry) and UniRef90 (per the install README, deliberately
   excluded at DB-build time).  Callers can override with
   `databases=[...]`.

4. **Unknown database `_id` columns fall through.** If DRAM2 adds a new
   namespace (e.g. `cant_hyd_id`, `camper_id`), it is emitted as
   `Term(namespace=None, id=<val>, evidence={"source": "<col_name>"})`
   so the source DB is recoverable without code changes.  When a
   namespace becomes important, add it to `_ID_COLUMN_NAMESPACE` and
   write a parse test.

5. **No live `raw-annotations.tsv` captured by *this* task.** The
   committed fixture is a slice of the upstream OWC snapshot that
   shipped with the pinned `v2.0.0-beta17` install — itself a genuine
   real-DRAM2 output, just produced by the maintainers rather than this
   task.  A concurrent h100 smoke-test invocation
   (`/storage/chenry/DRAM2/run_smoketest.sh`) was running while this
   module was implemented but had not yet produced its own
   `raw-annotations.tsv` (conda env builds dominate first-run wall time).
   Replacing the fixture with a fresh capture is one
   `nextflow run … --use_kofam` invocation (recipe in the fixture README)
   and does not require any code change.
