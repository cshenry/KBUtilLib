# Work Record: dram2-id-remap-live-h100

## task_id
dram2-id-remap-live-h100 (Maestro task-1520f857)

## branch
conductor/dram2-id-remap/live-h100

## summary

Live h100 end-to-end validation of the DRAM2Utils id-remap fix
(Acceptance Criterion 16 of
`agent-io/prds/dram2-input-genes-id-remap/fullprompt.md`).  Drove
`DRAM2Utils.annotate(...)` from a Python runner against the real
DRAM2 install at `/scratch1/fliu/hub_scratch/chenry/DRAM2`, with 50
b-prefixed E. coli Keio (BW25113) proteins sourced from the GAA
parquet store on h100 (`/home/chenry/.local/share/gaa_store_h100`,
genome_id `fac9fa4e-530a-4406-8a3b-9349ead11f6b`), and confirmed:

1. `COMBINE_ANNOTATIONS` no longer crashes with
   `ValueError: invalid literal for int() with base 10: 'b0001'`.
2. The returned `AnnotationResult.records` carry the **caller's
   b-prefixed locus_ids** (b0001, b0002, …), translated by the fix's
   `{emitted_id -> caller_id}` reverse map applied during TSV parse.

The exact bug that the Phase-1 fix addresses had been reproduced on
this same install in the prior validation run
(`/scratch1/fliu/hub_scratch/chenry/gaa-dram2-validation/log/smoke.log`)
— that earlier run failed with the literal traceback
`ValueError: invalid literal for int() with base 10:
'951046ba5936ed84b765a0998de640ca90e3d96b38eddc3938981b98f3949b3e'`
inside `combine_annotations.py:70` `int(gene_position)`.  This run, on
the same install with the fix in place, completed cleanly.

## commit_shas
- _the commit that adds this work-record_ (see git log of this branch)

## sync-lag guard

Per the task envelope, before running anything I verified that the
Phase-1 fix landed on `wip` and is the editable kbutillib in the venv
the runner uses:

- `git -C /home/chenry/projects/KBUtilLib pull` brought `wip` up to
  `fe86c70 Merge branch 'main' into wip (dram2 id-remap fix live)`.
- The Maestro worktree's branch
  `conductor/dram2-id-remap/live-h100` already contains
  `e891617 feat(dram2): id-remap + run-hardening (Decisions 1-10, AC
  1-15, 17)` and `84f29bd chore(work-record): add work-record for
  dram2-id-remap-impl`.
- Direct introspection of the installed `kbutillib.dram2_utils` (via
  the task's `.task-venv` editable install pointing at this worktree)
  shows:
  - `module path: src/kbutillib/dram2_utils.py`
  - `Has _build_subprocess_env: True`
  - `_write_faa return annotation: dict[str, str]`
  - `'emitted_to_caller' in _write_faa source: True`

## files_touched

- `agent-io/work-records/dram2-id-remap-live-h100.md` (this file)
- `agent-io/work-records/dram2-id-remap-live-h100/run_summary.json`
  — runner-produced summary (counts, parameters, command, kbutillib
  path, error=null, b-prefixed verification flags)
- `agent-io/work-records/dram2-id-remap-live-h100/sample_records.json`
  — first 20 returned `AnnotationRecord`s with terms (b0001 first)
- `agent-io/work-records/dram2-id-remap-live-h100/all_records.tsv`
  — all 46 returned records (one row per gene), tab-separated
- `agent-io/work-records/dram2-id-remap-live-h100/raw-annotations.tsv`
  — DRAM2's published `<outdir>/RAW/raw-annotations.tsv` from the
  live run, with `g_1..g_50` query_ids (proof that the synthetic
  prodigal-safe headers are what reached `combine_annotations.py`)
- `agent-io/work-records/dram2-id-remap-live-h100/input.faa`
  — the FASTA DRAM2 actually consumed (50 records, prodigal-style
  `>g_N # start # stop # strand #` headers, b0001's sequence under g_1)
- `agent-io/work-records/dram2-id-remap-live-h100/command.txt`
  — the exact `nextflow run …` command emitted by DRAM2Utils
- `agent-io/work-records/dram2-id-remap-live-h100/nextflow.log.tail.txt`
  — last 200 lines of `.nextflow.log` (process events, COMBINE_ANNOTATIONS
  exit 0, "Pipeline completed successfully")
- `agent-io/work-records/dram2-id-remap-live-h100/run_validation.py`
  — the runner script used (committed verbatim so the run is
  reproducible)

No source files under `src/` or tests were modified.

## DRAM2Utils configuration (Step 2)

Passed to `DRAM2Utils(config={...}, config_file=False)`:

```
dram2.launch_dir = /scratch1/fliu/hub_scratch/chenry/DRAM2
dram2.pipeline   = /scratch1/fliu/hub_scratch/chenry/DRAM2/repo/main.nf
dram2.nextflow   = /scratch1/fliu/hub_scratch/chenry/DRAM2/bin/nextflow-native
dram2.nxf_ver    = 24.10.5
dram2.env_path   = /scratch1/fliu/hub_scratch/chenry/DRAM2/env/env_nf/bin:
                   /scratch1/fliu/hub_scratch/chenry/DRAM2/bin
dram2.profile    = conda
dram2.config     = /scratch1/fliu/hub_scratch/chenry/DRAM2/dram2.config
dram2.work_root  = /scratch1/fliu/hub_scratch/chenry/dram2-id-remap-live-validation/
                   keio-validation/scratch
dram2.keep_work  = True
```

`env_path` choice: the first dir (`env/env_nf/bin`) puts the JDK 17
`java` and the conda-installed `nextflow` on PATH; the second
(`DRAM2/bin`) puts `micromamba` on PATH so `conda.useMicromamba =
true` in `dram2.config` resolves.  `nextflow-native` itself is invoked
via absolute path from `dram2.nextflow`.

The runner additionally seeds the *process* environment with the
non-PATH bits from `dram2-env.sh` (`TMPDIR`, `NXF_HOME`,
`MAMBA_ROOT_PREFIX`, `CONDA_ENVS_PATH`, `CONDA_PKGS_DIRS`,
`NXF_CONDA_CACHEDIR`, `DRAM2_DB_DIR`) before calling
`DRAM2Utils(...)`, because `_build_subprocess_env` inherits
`os.environ` and these are required for the pipeline to find its
databases / conda caches / Nextflow home on the NFS scratch volume.
`/tmp` is only 8 GB on this host and fills before MMseqs2 finishes
indexing, so `TMPDIR` MUST be redirected to NFS — confirmed by an
earlier smoke run that failed mid-way with `No space left on device`
on `/tmp` before this was added.

## Keio proteins (Step 3)

The h100 GAA store already contained the Keio genome (created
2026-06-26, before this run was attempted), so re-seeding was
unnecessary.  The runner queries it directly via DuckDB:

```sql
SELECT g.locus_id, s.sequence, g."start", g."end", g.strand
FROM read_parquet('/home/chenry/.local/share/gaa_store_h100/genes/*.parquet') g
JOIN read_parquet('/home/chenry/.local/share/gaa_store_h100/sequences/*.parquet') s
  ON g.protein_seq_hash = s.seq_hash
WHERE g.genome_id = 'fac9fa4e-530a-4406-8a3b-9349ead11f6b'
  AND s.seq_type = 'protein'
  AND g.locus_id LIKE 'b%'
  AND LENGTH(s.sequence) >= 5
ORDER BY g.locus_id
LIMIT 50
```

The b-prefixed filter is what makes this a faithful test of the
PRD bug:  ModelingLOE-style real-world locus_ids (`b0001` …) are
exactly the ids that triggered DRAM2's
`combine_annotations.py:70 int(id.split("_")[-1])` crash.  No FASTA
files are hardcoded.  50 proteins were sent (the full ~4493 was
unnecessary to validate the fix and would have made the run several
hours; 50 is enough to exercise b0001, the literal example from the
PRD).

The full {locus_id: protein_seq} dict that the runner sent is in
`/scratch1/fliu/hub_scratch/chenry/dram2-id-remap-live-validation/keio-validation/protein_subset.json`
(not committed: 1991 bytes, reproducible from the SQL query above).

## Run (Step 4)

`KBU_DRAM2_LIVE=1 python run_validation.py --limit 50 --threads 4
--databases kofam --out <NFS>/keio-validation`

`DRAM2Utils.annotate(proteins, gene_coords=coords, databases=['kofam'],
threads=4)` was called.  Pipeline timeline from
`.nextflow.log`:

```
17:10:29  Submitted MMSEQS_INDEX, HMM_SEARCH_KOFAM, GENE_LOCS
17:10:30  Submitted MULTIQC
17:10:31  COMPLETED  GENE_LOCS         exit: 0
17:10:37  COMPLETED  MMSEQS_INDEX      exit: 0
17:10:43  COMPLETED  MULTIQC           exit: 0
17:11:18  Submitted PARSE_HMM_KOFAM   (after HMM_SEARCH_KOFAM)
17:11:21  Submitted KOFAM_HMM_FORMATTER
17:11:23  Submitted COMBINE_ANNOTATIONS
17:11:29  COMPLETED  COMBINE_ANNOTATIONS  exit: 0   <-- THE CRITICAL ONE
17:11:29  Pipeline completed successfully
```

`COMBINE_ANNOTATIONS` exit 0 with `b0001`-style ids in the input is
the direct refutation of the PRD bug.

## Sample of returned records (Step 5)

`result.records` had 46 entries (the 50 inputs minus 4 with no kofam
hit; these are dropped from the result, which matches DRAM2's
expected behavior).  All 46 had b-prefixed `gene_id` values.  First
ten rows (full table in `all_records.tsv`):

```
gene_id  n_terms  term_namespaces  first_term
b0001    2        KO               KO:K08278=K08278         (thr operon leader peptide)
b0002    4        EC,KO            KO:K12524=K12524
b0003    3        EC,KO            KO:K00872=K00872
b0004    3        EC,KO            KO:K06037=K06037
b0006    2        KO               KO:K09861=K09861
b0007    2        KO               KO:K03310=K03310
b0008    3        EC,KO            KO:K00616=K00616
b0009    3        EC,KO            KO:K03831=K03831
b0010    2        KO               KO:K07034=K07034
b0011    2        KO               KO:K00697=K00697
```

`b0001` came back annotated with `KO:K08278` ("thr operon leader
peptide"), matching its description in the upstream Keio bundle
(`MKRISTTITTTITITTGNGAG`, 21 aa).  The fact that b0001 is **present
in the records at all** is the round-trip proof: the FASTA was
written with `>g_1 # 190 # 255 # 1 #` (`input.faa` line 1), DRAM2
saw `g_1` as the query_id all the way through
`combine_annotations.py`, and the parser translated `g_1` back to
`b0001` via the in-memory `{emitted_id -> caller_id}` map returned
by `_write_faa`.

## Execution trace (Step 5)

DRAM2Utils does not surface an `execution_trace` attribute on
`AnnotationResult`; the equivalent evidence is captured as:

- `run_summary.json::command` — the exact `nextflow run …` argv
  (also in `command.txt`).
- `run_summary.json::parameters` — `{databases, threads, pipeline,
  launch_dir, profile, extra_config, input_protein_count=50,
  work_dir, kept=true, gene_coords=true}`.
- `run_summary.json::run_id` = `98428bef10d645d7a877a6283fd76dd9`.
- `run_summary.json::tool_version` = `24.10.5` (Nextflow engine
  version; DRAM2 itself ships as `v2.0.0-beta17` per the install's
  `dram2-env.sh`).
- `nextflow.log.tail.txt` — last 200 lines of `.nextflow.log`
  including the full process event sequence above and the
  `Pipeline completed successfully` line.
- `raw-annotations.tsv` — the unmodified DRAM2 output table that
  `_parse_annotations_tsv` consumed; the first column shows
  `g_1..g_50` (i.e. exactly what `_write_faa` emitted), confirming
  the synthetic-id round-trip on the live pipeline.

## success_criteria_check

- **Phase-1 fix is the installed/editable kbutillib in the run
  environment** — PASS.  `run_summary.json::kbutillib_path` is the
  worktree's `src/kbutillib/dram2_utils.py`; the worktree's git log
  contains commits `e891617` and `84f29bd`; `.task-venv` is an
  editable install of the worktree.
- **Pipeline completes COMBINE_ANNOTATIONS without the
  `ValueError: invalid literal for int() with base 10: 'b0001'`
  crash** — PASS.  `.nextflow.log`:
  `name: WRIGHTONLABCSU_DRAM:DRAM:ANNOTATE:COMBINE_ANNOTATIONS;
  status: COMPLETED; exit: 0; error: -` followed by
  `Pipeline completed successfully`.  No `ValueError` /
  `invalid literal` anywhere in the log.
- **Returned `AnnotationRecords` carry b0001-style gene_ids** — PASS.
  `run_summary.json::all_gene_ids_are_b_prefixed: true`,
  `n_records_with_b_prefixed_gene_id: 46`, total `n_records: 46`,
  `first_5_b_prefixed_gene_ids_in_records: ["b0001", "b0002",
  "b0003", "b0004", "b0006"]`.

## caveats

- `dram2.config` (`conda.enabled = true; conda.useMicromamba =
  true`) is REQUIRED for this install — without it, the standard
  `mamba 2.x env create` hangs on this host per the
  `dram2-env.sh` header comment.  Passed via `dram2.config`
  config key (which `_run_nextflow` maps to `-c <file>`).
- DRAM2 only emits annotation rows for genes that had at least one
  hit, so the 4 input genes with no kofam hit are absent from
  `result.records`.  This is unchanged behavior from before the
  Phase-1 fix.
- The runner script reads from the GAA store via DuckDB rather than
  via the GAA Python API, because the store-on-disk format
  (parquet under `~/.local/share/gaa_store_h100/`) is stable and
  the GAA installation on h100 is in a separate venv whose
  invocation path would have added cross-venv friction not
  warranted for a one-shot validator.
- `TMPDIR` redirection from `/tmp` (only 8 GB on this host) to NFS
  scratch is set in the runner BEFORE constructing
  `DRAM2Utils`, since `_build_subprocess_env` inherits
  `os.environ`.  Without this, a smoke run failed mid-pipeline
  with `No space left on device` during MMseqs2 indexing.  This
  is a host-config concern, not a Phase-1 fix concern.

## tests_run

End-to-end live run:

```
KBU_DRAM2_LIVE=1 \
  /mnt/homes/chenry/.maestro/worktrees/task-1520f857/.task-venv/bin/python \
  /tmp/dram2_validation_run/run_validation.py \
    --limit 50 --threads 4 --databases kofam \
    --out /scratch1/fliu/hub_scratch/chenry/dram2-id-remap-live-validation/keio-validation
```

Result: exit 0; `Pipeline completed successfully`;
`run_summary.json::error: null`; `all_gene_ids_are_b_prefixed: true`.

A 5-protein smoke run preceded the final run and also completed
successfully end-to-end (its `raw-annotations.tsv` had query_ids
`g_1..g_5` and `COMBINE_ANNOTATIONS` exited 0).  The smoke run dir
was overwritten by the final run; only the final-run artifacts are
committed.

A literal-bug repro snapshot from the PRE-fix install for context:
`/scratch1/fliu/hub_scratch/chenry/gaa-dram2-validation/log/smoke.log`
shows `combine_annotations.py:70` raising
`ValueError: invalid literal for int() with base 10:
'951046...3949b3e'` on the SAME `combine_annotations.py` and the
SAME nextflow engine.  That run used raw hash strings as query_ids
(the pre-fix behavior); this run uses synthetic `g_N` ids (the
post-fix behavior) and succeeds.
