# PRD: DRAM2Utils --input_genes id-remap + run-hardening

## Problem Statement

`DRAM2Utils.annotate()` cannot annotate any real genome. A single-genome run on
h100 (E. coli Keio, 4607 proteins, kofam) executes every pipeline stage green
(GENE_LOCS, MMSEQS_INDEX, HMM_SEARCH_KOFAM, PARSE_HMM_KOFAM, KOFAM_HMM_FORMATTER
all exit 0) and then dies at the final COMBINE_ANNOTATIONS step:

    ValueError: invalid literal for int() with base 10: 'b0001'
    repo/bin/combine_annotations.py:65  set_gene_data()

DRAM2's `combine_annotations.py` splits each gene id on `_`, takes the last
token, and `int()`s it (the prodigal `contig_geneNumber` convention, e.g.
`k99_42_7`). `DRAM2Utils._write_faa` writes the caller's id as the FASTA header's
first whitespace token, and GAA's `LocalDram2Plugin` passes store **locus_ids**
(`b0001`, `b0002`, …) as those caller ids. Those ids have no `_<int>` final
token, so `int()` crashes and the whole annotation fails. The existing
`_write_faa` already writes prodigal-style *coordinate* fields
(`>b0001 # 1 # 300 # 1 #`), but DRAM2 only reads the first token (`b0001`) as the
id, so the coordinate fields do not satisfy the `int()` requirement — the id
token itself must end in `_<integer>`.

Three further problems surfaced in the same run, each of which makes the failure
harder to operate around:

1. **Stale Nextflow engine pin.** The `nextflow-native` wrapper defaults
   `NXF_VER=26.04.3`, which fails to compile DRAM2 beta17 (8 Groovy errors); the
   pipeline only compiles under `24.10.5`. DRAM2Utils launches the wrapper as a
   subprocess and inherits the caller's environment, so unless the integration
   sets `NXF_VER=24.10.5` (plus java + micromamba on `PATH`), the run never even
   compiles. The repo's `dram2-env.sh` cannot be relied on: it hardcodes a stale
   `DRAM2_ROOT=/storage/chenry/DRAM2` (the actual install is
   `/scratch1/fliu/hub_scratch/chenry/DRAM2`).

2. **Failure evidence is destroyed.** `annotate()` wraps input/work/outdir in a
   `tempfile.TemporaryDirectory`, so a failed run deletes the entire Nextflow
   work tree — the combine traceback above had to be recovered from
   `$ROOT/.nextflow.log` after the fact.

3. **Scratch lands on /tmp.** `TemporaryDirectory` honors `$TMPDIR`; when unset
   it writes to `/tmp`, and a real run's Nextflow work tree filled h100's small
   (8 GB) `/tmp` partition.

## Solution

Make `DRAM2Utils.annotate()` annotate real genomes end-to-end while keeping its
public interface unchanged — input `{caller_id: protein_seq}`, output
`AnnotationResult` whose `records[].gene_id` is the **original caller id**. This
output invariant is load-bearing: GAA's `_kbu_result_to_rows` joins
`record.gene_id` back to `protein_seq_hash` via `gene_to_seq_hash`, so any
mangled id silently drops the row. The fix therefore lives entirely inside
DRAM2Utils; GAA requires no changes.

Internally, DRAM2Utils renames each protein to a prodigal-safe synthetic id
`g_<n>` (1-based integer index over the batch) when writing the input FASTA,
remembers the `{emitted_id -> caller_id}` map, and translates `query_id` back to
the caller id when parsing `raw-annotations.tsv`. `g_<n>` is unique by
construction and has a numeric final token, and the caller's (possibly messy)
locus_id never touches the FASTA header.

Alongside the rename, harden the run: build the subprocess environment
explicitly from config (pin `NXF_VER`, prepend `PATH`), put scratch on a
configured large-disk `work_root` instead of `/tmp`, and preserve the work dir +
`pipeline_info/` + `.nextflow.log` on failure for debugging.

## User Stories

1. As a GAA operator, I want `DRAM2Utils.annotate()` to succeed on a real genome
   whose proteins carry store locus_ids like `b0001`, so that the DRAM2 inner-loop
   stage produces rows instead of crashing at COMBINE_ANNOTATIONS.
2. As the GAA `LocalDram2Plugin`, I want `AnnotationResult.records[].gene_id` to
   equal the exact caller id I passed in (`b0001`), so that my
   `gene_to_seq_hash[gene_id]` join writes the right `protein_seq_hash` rows.
3. As a caller, I want the public `annotate(proteins, databases, gene_coords,
   run_config, threads)` signature to stay the same, so that no caller (notably
   GAA) needs to change.
4. As a developer, I want a protein dict with non-prodigal ids (e.g. `b0001`) to
   round-trip through an offline unit test (emitted id has a numeric final token;
   parsed output remaps to `b0001`), so that the contract is locked without a live
   DRAM2 install.
5. As an h100 operator, I want DRAM2Utils to pin `NXF_VER=24.10.5` and prepend the
   needed `PATH` entries from config, so that beta17 compiles without me sourcing
   the broken `dram2-env.sh`.
6. As an h100 operator, I want a failed run to leave the work dir and copy
   `pipeline_info/` + `.nextflow.log` to a stable location, so that I can read the
   real traceback instead of recovering it from a transient temp dir.
7. As an h100 operator, I want DRAM2Utils' scratch to land on a configured
   large-disk `work_root` (default the launch dir on `/scratch1`), never `/tmp`,
   so that a real run does not fill a small `/tmp` partition.
8. As a developer, I want the existing offline golden-fixture parse tests to keep
   passing (adapted to the new id-translation signature), so that the parser's DB
   namespace tagging behavior is not silently regressed.
9. As a caller who passes `gene_coords`, I want the real coordinates to still be
   written into the prodigal header (looked up by my caller id), so that
   coordinate provenance is preserved even though the header id token is now
   synthetic.
10. As a maintainer, I want a live h100 validation (gated behind
    `KBU_DRAM2_LIVE=1`) that runs the real Keio genome through COMBINE_ANNOTATIONS
    and asserts exit 0 + b0001 remap, so that the end-to-end fix is proven against
    the real 81 GB install, not just mocks.

## Implementation Decisions

All changes are confined to `src/kbutillib/dram2_utils.py` and its test module.
The module is the deep unit here: a small public interface
(`annotate(proteins, databases, gene_coords, run_config, threads)` +
`is_available()`) hiding all rename/remap/env/scratch complexity.

**Decision 1 — Input strategy = A (rename + reverse-map).** Reject B
(`--input_fasta`). B requires a nucleotide assembly per run and prodigal
gene-calling, but GAA's `NativeBatchExecutor` buckets unique proteins by
`seq_hash` prefix *across many genomes* — a batch has no single assembly to feed
prodigal. B is therefore incompatible with the seq-hash dedup that is GAA's core
scaling mechanism, not merely heavier. A keeps `--input_genes` (no gene
recalling) and is fully local to DRAM2Utils.

**Decision 2 — Emitted-id scheme = `g_<n>`.** When writing the input FASTA, emit
ids `g_1, g_2, …` where `n` is the 1-based index over the proteins in the batch
(iteration order of the `proteins` dict, which is insertion-ordered). Properties
relied on: (a) unique within a batch; (b) `split("_")[-1]` is a base-10 integer,
satisfying combine_annotations; (c) the caller's locus_id never appears in the
header, so a locus_id containing whitespace / `#` / `|` cannot corrupt the FASTA
header or the prodigal `#`-delimited coord format. Do NOT use `<locus>_<n>` (it
reintroduces the header-corruption risk).

**Decision 3 — `_write_faa` returns the reverse map.** Change the signature to
`_write_faa(path, proteins, gene_coords=None) -> dict[str, str]` returning
`{emitted_id: caller_id}`. The header is written as
`>{emitted_id} # {start} # {stop} # {strand} #`, where coords are looked up by
**caller id** in `gene_coords` (unchanged synthetic fallback:
`start=1, stop=3*len(seq), strand=1`). Strand normalization (`>0 -> 1`, else
`-1`) is unchanged.

**Decision 4 — Parser translates emitted ids back to caller ids.** Change
`_parse_annotations_tsv(tsv_text, emitted_to_caller: dict[str, str]) ->
list[AnnotationRecord]`. The TSV's `query_id` is now an emitted id (`g_<n>`); look
it up in `emitted_to_caller` and set `AnnotationRecord.gene_id` to the resolved
caller id. Rows whose `query_id` is not a key in the map are dropped (replacing
the old `caller_set` membership guard). Record emission order follows the
caller-id order derived from the map's values (preserve a stable order — e.g.
iterate the original `proteins` insertion order; the caller passes that order in).
All DB-column namespace tagging (KO/PF/EC/CAZY, free-text descriptions,
multi-valued EC split, `EC:` prefix strip) is unchanged.

**Decision 5 — `annotate()` threads the map.** Build the `{emitted: caller}` map
via `_write_faa`, run Nextflow, then call
`_parse_annotations_tsv(tsv_text, emitted_to_caller)`. Public signature and
`AnnotationResult` fields are unchanged. `parameters` may additionally record
`work_dir` (the resolved scratch path) and `kept` (bool) for provenance.

**Decision 6 — Explicit subprocess env from config.** Add config keys (read in
`__init__` via `get_config_value`):
- `dram2.nxf_ver` (default `"24.10.5"`) → set `NXF_VER` in the subprocess env.
- `dram2.env_path` (default `""`) → a `:`-joined list of dirs prepended to
  `PATH` in the subprocess env (java + micromamba live here on h100). Empty →
  inherit `PATH` unchanged.
`_run_nextflow` constructs `env = {**os.environ, "NXF_VER": nxf_ver}` and, when
`env_path` is non-empty, `env["PATH"] = env_path + os.pathsep + os.environ.get(
"PATH", "")`, and passes `env=env` to `subprocess.run`. Do not source
`dram2-env.sh`. `dram2.nextflow` continues to select the launcher binary (set it
to the `nextflow-native` wrapper on the native h100 host).

**Decision 7 — Configured scratch root, never /tmp.** Add `dram2.work_root`
(default = the resolved `launch_dir`). Replace `tempfile.TemporaryDirectory`
with `tempfile.mkdtemp(prefix="dram2_", dir=work_root)` and a manual
`try/finally`. `launch_dir` is `$DRAM2_ROOT` on `/scratch1` (the 81 GB-DB
volume), so it has room for a real work tree; `/tmp` is never used unless a
caller explicitly sets `work_root` to it.

**Decision 8 — Keep-on-failure + opt-in keep-on-success.** Add `dram2.keep_work`
(default `False`) and an `annotate(..., keep_work: bool | None = None)` override
(param wins over config). Lifecycle of the `mkdtemp` scratch dir:
- On success: delete the scratch dir unless `keep_work` is true.
- On failure (Nextflow non-zero OR any exception before parse): **always**
  preserve the scratch dir, and copy `pipeline_info/` (from `outdir`) and
  `.nextflow.log` (from `launch_dir`, if present) into
  `<work_root>/failed-<run_id>/`. Log the preserved path at ERROR level. Re-raise
  the original error after preserving. Use the existing `run_id` (uuid4 hex) for
  the failed-dir name.

**Decision 9 — GAA is untouched.** No file under
`GenomeAnnotationAggregator/` changes. The fix is validated against the existing
`LocalDram2Plugin` contract (`record.gene_id == caller locus_id`).

**Decision 10 — Confront round 1 resolutions (folded; authoritative where they
supersede earlier defaults).**

- (S1/S5 — ordering) `proteins` MUST be a standard insertion-ordered `dict` (or
  `OrderedDict`); behavior is undefined for unordered mappings. `_write_faa`
  enumerates `proteins` in insertion order to assign `g_<n>` and builds
  `emitted_to_caller` in that same order. `_parse_annotations_tsv` defines record
  emission order by iterating `emitted_to_caller` in insertion order (equivalently
  the caller's `proteins` order); no separate `caller_order` parameter is added.
- (S2 — keep_work vs "unchanged interface") "Public interface unchanged" means
  existing calls keep working; adding the optional keyword `annotate(...,
  keep_work: bool | None = None)` is an explicitly permitted, backward-compatible
  extension. The `keep_work` param overrides the `dram2.keep_work` config.
- (S3 — launcher default) `dram2.nextflow` default stays `"nextflow"`. On the
  native h100 host set
  `dram2.nextflow = /scratch1/fliu/hub_scratch/chenry/DRAM2/bin/nextflow-native`;
  add a code comment pointing at that path.
- (S4 — failure copy robustness) On failure: always create
  `<work_root>/failed-<run_id>/`; if `outdir/pipeline_info` exists, copy it
  recursively, else skip; if `<launch_dir>/.nextflow.log` exists, copy it as
  `nextflow.log`, else skip; always leave the full scratch dir in place and record
  its path in `parameters["work_dir"]`. Error handling MUST NOT raise a secondary
  error when a source is missing.
- (S6 — live input) Phase-2 live validation derives the Keio proteins from the
  already-seeded GAA store (b0001-style locus_ids) rather than a hardcoded FASTA
  path; the Phase-2 task prompt names the store/genome to pull and the minimal
  fetch. No new committed fixture is required.
- (S7 — env_path type) `dram2.env_path` is consumed as a single colon-separated
  string inserted verbatim as the `PATH` prefix; YAML list values are NOT
  accepted. Document an example (e.g.
  `"/scratch1/.../DRAM2/env/env_nf/bin:/scratch1/.../micromamba/bin"`).
- (S8 — provenance keys) `AnnotationResult.parameters` MUST include `work_dir`
  (absolute scratch path) on every run and `kept` (bool — whether the scratch dir
  was preserved after return).
- (FC4 — scratch separation) **Supersedes Decision 7's default:** `dram2.work_root`
  default = `<launch_dir>/scratch` (a dedicated subdir under the install root), NOT
  `launch_dir` itself, so transient Nextflow work trees never mix with the
  immutable install tree and are easy to purge. Create it if absent. `/tmp` is
  still never used unless a caller explicitly sets `work_root` to it.
- (FC6 — misconfig guard) `is_available()` additionally emits a WARNING (NOT a
  hard failure / not a `False` return) when `Path(pipeline)` does not resolve under
  `launch_dir`, to catch the stale-`DRAM2_ROOT` class of misconfiguration early.

## Testing Decisions

Test external behavior (the id round-trip and the run contract), not internals.
Prior art: the existing `tests/annotators/test_dram2_utils.py` already exercises
`_parse_annotations_tsv` against a committed golden `raw-annotations.tsv` slice
and `is_available()` against fake PATH/pipeline layouts — follow those patterns.

Modules to test (offline unless noted):
1. **b0001 round-trip (acceptance unit test).** `_write_faa` on
   `{"b0001": "MKT…", "b0002": "MAA…"}` produces emitted ids whose
   `split("_")[-1]` is an int and returns a map back to the b-ids; feeding a
   synthetic `raw-annotations.tsv` keyed by those emitted ids through
   `_parse_annotations_tsv(..., map)` yields records with
   `gene_id in {"b0001","b0002"}`.
2. **`_write_faa` map + coords.** Returned map is `{emitted: caller}`; header
   coords come from `gene_coords[caller_id]` when supplied, synthetic otherwise;
   strand normalized.
3. **Parser translation + unknown drop.** `query_id` present in the map →
   translated; `query_id` absent from the map → row dropped.
4. **Existing golden-fixture parse tests adapted.** Update each existing
   `_parse_annotations_tsv(tsv, [ids])` call to pass an identity map
   `{id: id}`; all namespace/EC/description assertions must stay green.
5. **Env builder.** With `dram2.nxf_ver` + `dram2.env_path` configured, the env
   passed to the (mocked) `subprocess.run` contains `NXF_VER=24.10.5` and a
   `PATH` with the configured prefix first. Use monkeypatch on `subprocess.run`.
6. **Scratch + keep-on-failure.** With a configured `work_root`, scratch is
   created under it (not `/tmp`); a forced Nextflow failure preserves the scratch
   dir and creates `<work_root>/failed-<run_id>/` (mock `subprocess.run` to return
   non-zero and stage a fake `pipeline_info/`).
7. **Live h100 validation (`KBU_DRAM2_LIVE=1` + `is_available()`), Phase 2.** Run
   the real Keio genome (b0001-style locus_ids, kofam) end-to-end on
   `/scratch1/fliu/hub_scratch/chenry/DRAM2`; assert the run completes (no
   COMBINE_ANNOTATIONS crash) and the returned records carry b0001-style
   `gene_id`s. This is the gated end-to-end acceptance, not part of the offline
   suite.

## Out of Scope

- Any change to `GenomeAnnotationAggregator/` source (the plugin/executor are
  already correctly wired and must keep working unchanged).
- The `conda` → `docker` profile flip (tracked separately in
  `dram2-docker-profile-validation.md`); this PRD is profile-agnostic.
- Repairing or deleting `dram2-env.sh` (intentionally bypassed, not fixed).
- Gene-calling / `--input_fasta` support (Option B, rejected).
- Memory-cap tuning, Wave-container pinning, and other pipeline-internal concerns.
- Changing the DRAM2 default database set or namespace-tagging rules.

## Further Notes

- The output invariant `records[].gene_id == caller_id` is the single most
  important contract to preserve; a reviewer should verify it explicitly against
  the GAA `_kbu_result_to_rows` join.
- Keep the module docstring honest: update the "the `.faa` header IS the caller
  id" claim, since the header id is now a synthetic `g_<n>` and the caller id is
  recovered via the reverse map.
- Reference run + recovery context:
  `GenomeAnnotationAggregator/agent-io/work-records/dram2-docker-profile-validation.md`
  and `dram2-e2e-validation.md`.
- Install for the live test: `/scratch1/fliu/hub_scratch/chenry/DRAM2` (beta17,
  Nextflow 24.10.5, 81 GB DBs), launcher `repo` + `bin/nextflow-native`.

## Acceptance Criteria

1. `DRAM2Utils.annotate({"b0001": <aa>, "b0002": <aa>}, databases=["kofam"])` returns an `AnnotationResult` whose `records[].gene_id` values are drawn from the original caller ids (`b0001`/`b0002`), never the synthetic `g_<n>` ids.
2. `_write_faa` writes FASTA headers whose id token matches `^g_\d+$` (a base-10 integer after splitting the id on `_` and taking the last token) and returns a `{emitted_id: caller_id}` dict covering every input protein.
3. Emitted ids are assigned by enumerating `proteins` in insertion order (`g_1` for the first key, `g_2` for the second, …); the parsed result's record emission order follows that same caller order.
4. `_parse_annotations_tsv(tsv_text, emitted_to_caller)` translates each row's `query_id` via `emitted_to_caller` and drops rows whose `query_id` is absent from the map.
5. When `gene_coords` is supplied, each prodigal header's coordinates are taken from `gene_coords[caller_id]`; otherwise the synthetic fallback (`start=1, stop=3*len(seq), strand=1`) is used; strand is normalized to `1`/`-1`.
6. The subprocess environment passed to `nextflow` contains `NXF_VER` equal to `dram2.nxf_ver` (default `24.10.5`), and when `dram2.env_path` is non-empty, `PATH` begins with that colon-separated prefix.
7. `dram2.env_path` is consumed as a single colon-separated string; list values are not required to be supported.
8. Scratch directories are created under `dram2.work_root` (default `<launch_dir>/scratch`), never under `/tmp`, via `mkdtemp`.
9. On a successful run the scratch dir is deleted unless `keep_work` is true (the `keep_work` annotate() param overrides the `dram2.keep_work` config).
10. On a failed run the scratch dir is preserved and `<work_root>/failed-<run_id>/` is created; `pipeline_info/` and `.nextflow.log` are copied into it when present, missing sources are skipped without raising, and the original error is re-raised.
11. `AnnotationResult.parameters` includes `work_dir` (absolute path) and `kept` (bool) on every run.
12. The public `annotate(proteins, databases, gene_coords, run_config, threads)` calls used by GAA's `LocalDram2Plugin` continue to work unchanged; the only signature addition is the optional `keep_work` keyword.
13. `is_available()` warns (does not return False) when the configured `pipeline` path does not resolve under `launch_dir`.
14. The existing offline golden-fixture parse tests pass after being adapted to pass an identity `{id: id}` map; no namespace/EC/description assertion regresses.
15. Offline unit tests cover the b0001 round-trip, `_write_faa` map+coords, parser translation + unknown-query_id drop, the env builder (`NXF_VER` + `PATH` prefix), and keep-on-failure preservation.
16. Phase 2 (h100, `KBU_DRAM2_LIVE=1`): the real Keio genome (b0001-style locus_ids, kofam) runs end-to-end through `COMBINE_ANNOTATIONS` without the `int('b0001')` crash, and the returned records carry b0001-style `gene_id`s.
17. No file under `GenomeAnnotationAggregator/` is modified.
