# Annotation Tool Modules (DRAM2, PROKKA, Transyt) — KBUtilLib APIs + GAA narrow wraps

## Problem Statement

I need to run three external annotation tools — **DRAM2**, **PROKKA**, and **Transyt** —
over a genome's genes/proteins and collect each tool's functional annotations into the
GenomeAnnotationAggregator (GAA) store as separate, comparable sources.

Today the tool-invocation logic lives inside GAA (`annotation_runner.py` +
`local_binary_adapter.py`), and it is both trapped and broken:

- The live PROKKA path is **stubbed** — it writes an empty FASTA
  (`fasta_path.write_text("")`) and was never exercised; only the mock path works.
- It invokes PROKKA **incorrectly** as `prokka --proteins <fasta>`. `--proteins` is
  PROKKA's *trusted annotation DB* flag, not the query input.
- It mis-models PROKKA as a per-protein tool fitting GAA's "annotate each unique protein
  once" reuse model, when PROKKA is fundamentally a gene-caller.
- There is **no local DRAM2 capability** and **no local Transyt capability** at all.

Because the tool-running logic lives in GAA, it cannot be reused outside the aggregator,
and the modeling is wrong for at least one tool.

## Solution

Put the reusable tool-running APIs in **KBUtilLib** as three new utility modules behind one
consistent interface. Each module:

- accepts a **`{id: seq}` dict** (in memory, never a FASTA path — the module stages any
  temp files internally),
- **checks whether its tool is installed** and, if not, raises a clear, tool-specific error,
- runs the tool and returns **structured-native annotation records keyed by the caller's
  IDs**, plus run-level provenance (tool/DB version, command, parameters, run_id).

The KBUtilLib modules are **store-agnostic and GAA-agnostic** — they know nothing about
seq_hash, sources, `protein_annotations`, or `FunctionResolver`.

GAA's plugins then become **narrow wraps**: each calls the corresponding KBUtilLib module
and maps the returned records into the GAA store (intern sequences → seq_hash, write
`protein_annotations` rows with the source id + version provenance, storage-level dedup).
The subprocess logic is **deleted** from GAA's `local_binary_adapter.py`.

## User Stories

1. As a platform developer, I want a `ProkkaUtils.annotate(genes)` API in KBUtilLib so I can
   run PROKKA on a set of genes from any project, not just from inside GAA.
2. As a platform developer, I want a `DRAM2Utils.annotate(proteins)` API so I can get DRAM2
   functional annotations for a set of proteins.
3. As a platform developer, I want a `TransytUtils.annotate(proteins, tax_id)` API so I can
   get Transyt transport-reaction annotations for a proteome.
4. As a developer, I want all three behind one consistent interface (`annotate(...) ->
   AnnotationResult`, `is_available()`) so calling code is uniform across tools.
5. As a developer, I want each module to tell me clearly when its tool is **not installed**
   (a tool-specific actionable error), rather than failing obscurely mid-run.
6. As a developer, I want the annotation records **keyed by the IDs I passed in**, so I can
   join the results straight back to my own gene set.
7. As the GAA maintainer, I want GAA's PROKKA/DRAM2/Transyt plugins to be **narrow wraps**
   over the KBUtilLib modules, so the tool logic is owned in one place and GAA only does the
   store-specific mapping.
8. As the GAA maintainer, I want the broken/stubbed local PROKKA path in GAA
   **removed** — the subprocess logic deleted from `local_binary_adapter.py` and replaced by a
   call to `ProkkaUtils`.
9. As a researcher, I want PROKKA to annotate my **already-called genes** (preserving my gene
   IDs), emulating the KBase `kb_prokka` genome re-annotation workaround, so PROKKA fits the
   same "submit my genes, get annotations" model as the other tools.
10. As a researcher, I want PROKKA to capture **product, EC, gene, and COG** from its output
    (richer than KBase's genome path, which captures only product + EC).
11. As a researcher, I want Transyt run **locally via Docker** (reusing or deriving from the
    KBase `kb_transyt` image) so I don't have to hand-assemble the Java 11 + Neo4j + BLAST + DB
    stack.
12. As a developer, I want DRAM2 testing to run on **h100** (where DRAM2 is installed); I do
    **not** want DRAM2 installed on the laptop.
13. As a developer, I want every module to have **offline unit tests** (mock + golden fixture)
    that pass with no tool installed, plus **live integration tests** that run only where the
    tool is available.
14. As a maintainer, I want the consistent return type to map cleanly onto the existing
    `kb_annotation_utils` ontology vocabulary (EC, KO, TC, MSRXN, …) so aggregated results are
    comparable.

## Implementation Decisions

### Module layout (one module per tool + shared base)

Mirror the repo's existing external-tool convention (`MMSeqsUtils`, `SKANIUtils` are each
their own module inheriting `SharedEnvUtils`). **Do not lump the three tools into one
module.**

- `src/kbutillib/annotator_utils.py` — `AnnotatorUtils(SharedEnvUtils)` base + return types.
- `src/kbutillib/prokka_utils.py` — `ProkkaUtils(AnnotatorUtils)`.
- `src/kbutillib/dram2_utils.py` — `DRAM2Utils(AnnotatorUtils)`.
- `src/kbutillib/transyt_utils.py` — `TransytUtils(AnnotatorUtils)`.

Export all four from `__init__.py` following the existing export pattern.

### The consistent interface (the deep, stable part)

`AnnotatorUtils` defines:

- `annotate(self, sequences: dict[str, str], **params) -> AnnotationResult` — the single
  stable method every tool implements.
- `is_available(self) -> bool` — **abstract**, each tool implements its own probe (there is
  **no uniform availability check** — see per-tool probes below).
- `_require_available(self)` — calls `is_available()`; on False raises a clear,
  **tool-specific** error naming the tool and how to install it.

The **input is `dict[str, str]` for all three tools** (`{id: seq}`). The molecule type of the
value is **per-tool** and documented on each method:
- PROKKA: value = **nucleotide CDS** (see PROKKA mechanism).
- DRAM2, Transyt: value = **amino-acid protein**.
Each module **guards** its input: PROKKA rejects input that is not nucleotide; the
protein-level tools reject nucleotide input. (Cheap heuristic: alphabet check.)

The **return is keyed by the caller's IDs.** For protein-level tools the module need not echo
sequences back. For PROKKA the gene IDs are preserved via the contig-id trick, so the return
is still keyed by the caller's IDs.

### Return type (structured-native — the GAA `FunctionResolver` owns final normalization)

Lightweight dataclasses (the module is a pure tool-runner; it does **not** resolve to
`function_hash` and does **not** import anything GAA-specific — the dependency is
one-directional, GAA → KBUtilLib):

```python
@dataclass
class Term:
    namespace: str | None   # "EC", "KO", "TC", "MSRXN", ... or None for raw free-text
    id: str | None          # the term id within the namespace, or None
    value: str              # the term value / free-text product string
    evidence: dict          # tool-specific evidence (score, e-value, inference, ...)

@dataclass
class AnnotationRecord:
    gene_id: str            # the caller's id (preserved)
    terms: list[Term]

@dataclass
class AnnotationResult:
    tool: str
    tool_version: str | None
    db_version: str | None
    run_id: str
    command: str            # the exact command line / docker invocation run
    parameters: dict        # the resolved params used
    records: list[AnnotationRecord]
```

`namespace` is set where the tool emits a recognizable namespace; otherwise `namespace=None`
with the free-text in `value`. GAA's `FunctionResolver` consumes `Term`s and maps them into
its `function_hash` space — KBUtilLib does no ontology resolution.

### PROKKA mechanism (emulate KBase `kb_prokka` genome re-annotation — verified against
`kbaseapps/ProkkaAnnotation`)

`ProkkaUtils.annotate(genes: dict[str, str], gcode: int = 11, kingdom: str | None = None)`:

1. **Input value = nucleotide CDS** (not amino-acid). Guard and raise if the input looks like
   protein.
2. **ID safety + remap.** KBase *aborts the whole run* if any feature id exceeds **32 chars**.
   Do better: internally remap each caller id → a short safe id (≤32 chars, FASTA-header-safe),
   keep a `safe_id → caller_id` map, and **map results back to the caller's ids**. Long GAA
   gene ids must not break the run.
3. **Single-gene-contig FASTA.** Write one FASTA record per gene: `>safe_id` + the gene's
   nucleotide sequence. Each gene becomes its own "contig."
4. **Run plain contigs-in PROKKA** — `prokka --outdir <tmp> --prefix mygenome --gcode <gcode>
   [--kingdom <kingdom>] <fasta>`. **No `--proteins`** (verified absent from kb_prokka).
   Prodigal re-calls one ORF inside each single-gene contig.
5. **Parse the `.tsv`** (not just the GFF) and capture **product, EC, gene, COG** per
   locus — richer than KBase's genome path (which captured only product + EC). Map each row
   back to the caller id via the contig id.
6. **Caveats to handle/document:** *last-CDS-wins* if PROKKA splits one input record into
   multiple ORFs (document; for single-gene contigs this is rare); *zero-ORF* → the gene is
   simply absent from the result (no error); a *re-called ORF with different start/length*
   still maps back by id (PROKKA's coordinates are discarded — only the functional terms
   transfer).
7. **Availability probe:** `prokka --version` on PATH. **Provenance:** PROKKA version + the DB
   versions PROKKA reports.

### DRAM2 mechanism (CLI/output schema pinned at build time on h100)

`DRAM2Utils.annotate(proteins: dict[str, str], **params)`:

- Input value = amino-acid protein; write a `.faa` internally; run DRAM2's annotate step on
  called genes; parse DRAM2's annotations table; emit `Term`s tagged by namespace (KO/KEGG,
  Pfam, dbCAN, …).
- **The exact DRAM2 `annotate` invocation and output-table schema are pinned against the
  actual h100 install at build time** (DRAM2, the Snakemake rewrite, differs from DRAM1; we do
  not guess the CLI in this PRD). The h100 install is the authoritative source. The developer
  building this module captures a small golden fixture from a real DRAM2 run on h100 for the
  unit test.
- **Availability probe:** DRAM2 environment/CLI present (pinned at build time).
- **This module is built and live-tested on h100** (maestro lane, machine h100). It is **not
  installed on the laptop**; the laptop runs only the offline unit test (mock + golden fixture).

### Transyt mechanism (Docker; verified against `merlin-sysbio/kb_transyt`)

`TransytUtils.annotate(proteins: dict[str, str], tax_id: str,
reference_database: str = "ModelSEED", metabolites: list[str] | None = None)`:

- **`tax_id` is mandatory** — Transyt aborts (exit code 8) without a resolvable NCBI taxonomy
  id. Raise a clear error if not provided.
- **Docker-invoked.** Stage an input directory (`protein.faa` + `params.txt` carrying
  `taxID`, `reference_database=ModelSEED`, scoring params; optional `metabolites.txt`), then
  `docker run` with a bind mount, **overriding the image entrypoint** to boot Neo4j and run
  the JAR directly: `java … -jar /opt/transyt/transyt.jar 3 /workdir/processingDir/`.
- **Parse** `results/transyt.xml` (SBML transport reactions) + `results/reactions_references.txt`
  (TSV: Transyt rxn id → ModelSEED reaction id). Emit `Term`s linking gene → TC family →
  ModelSEED reaction/compound.
- **Image strategy (two phases):**
  - **Phase 1 — reuse `merlin-sysbio/kb_transyt` as-is**, with an `--entrypoint` override (its
    native entrypoint is the KBase SDK runner, not a plain CLI). Proven stack, lowest risk.
  - **Phase 2 — derive a slimmed KBUtilLib-owned Dockerfile** (`docker/transyt/`): strip the
    SDK layers, clean entrypoint = "start Neo4j → run JAR on `$INPUT_DIR` → emit `results/`".
    **Mirror/vendor** the U Minho downloads (`transyt.jar`, `data.tar.gz`, `workdir.tar.gz`) —
    those URLs are pinned to U Minho hosting and may rot.
  - The image tag is a config value (`transyt.docker_image`).
- **Availability probe:** `docker image inspect <tag>` (image present) **and** a Neo4j-up
  check — the JAR alone is not runnable without the populated Neo4j 4.0.2 DB. **Provenance:**
  image tag/digest + Transyt version.

### GAA narrow wraps (the consuming layer)

Refit GAA's plugins (`annotation_runner.py`) to be narrow wraps:

- `CLI_PROKKA` plugin → calls `ProkkaUtils.annotate(genes)`.
- local `DRAM2` plugin → calls `DRAM2Utils.annotate(proteins)`.
- local `TRANSYT` plugin → calls `TransytUtils.annotate(proteins, tax_id=…)`.

Each wrap: takes the annotator as an **injectable constructor dependency** (so GAA's offline
tests inject a fake annotator — this replaces today's `LocalBinaryAdapter` mock), calls
`.annotate(...)`, then maps `AnnotationResult.records` → intern sequences via `SequenceStore`
→ `protein_seq_hash`, route `Term`s through `FunctionResolver`, write `protein_annotations`
rows with the source id + version provenance, and respect storage-level dedup.

**Delete** the PROKKA/PaperBLAST subprocess logic and command templates from GAA's
`local_binary_adapter.py` (the live PROKKA path moves wholesale into `ProkkaUtils`). Keep the
KBase-app adapter (`kbase_adapter.py`) and the KBase-app plugins unchanged.

**Source ids** (registry amendment, mirrors the existing scheme): local PROKKA = `CLI_PROKKA`,
local DRAM2 = `DRAM2`, local Transyt = `TRANSYT`. The KBase-app baselines (`KBASE_PROKKA`,
`DRAM`, and the existing KBase Transyt plugin) are **out of scope and unchanged**.

### Confront-resolved specifications (round 1 — folded wholesale)

1. **Dataclass placement/exports.** `Term`, `AnnotationRecord`, `AnnotationResult` live in
   `src/kbutillib/annotator_utils.py`; export `AnnotatorUtils`, `Term`, `AnnotationRecord`,
   `AnnotationResult` from `src/kbutillib/__init__.py` under those exact names. Non-frozen,
   value-comparable (dataclass default), in-memory only (no JSON-schema promised).
2. **Availability exception.** Define `ToolUnavailableError(tool: str, detail: str)` in
   `annotator_utils.py`. `is_available()` returns `bool` and is **side-effect-free** (no
   logging). `_require_available()` raises `ToolUnavailableError` with message
   `"{tool} not available: {detail}. Install: {hint}"` (per-tool `hint`).
3. **Alphabet guards.** DNA guard accepts IUPAC nucleotide `{A,C,G,T,U,R,Y,S,W,K,M,B,D,H,V,N}`
   plus `-` and `*`; protein guard accepts the 20 AA plus `{B,Z,X}`, `-`, `*`. Case-insensitive,
   whitespace ignored. Raise `ValueError` if >10% of characters fall outside the allowed set.
4. **PROKKA safe-id remap.** Deterministic ordinal: `g{index}` (0-based input order) — ≤32
   chars and collision-free by construction. Keep an in-memory `safe_id → caller_id` map; map
   all results back to caller ids. Record the remapped-id count in `parameters`.
5. **PROKKA flags.** `prokka --outdir <tmp> --prefix prokka --gcode <gcode> [--kingdom <k>]
   --cpus <threads> --force --quiet <fasta>`. `gcode` default 11; `threads` default 1
   (configurable). `kingdom ∈ {None, "Bacteria", "Archaea", "Viruses"}` — reject others with
   `ValueError`. **Prefix is the neutral `prokka`** (the module is GAA-agnostic — not
   `gaa_*`).
6. **PROKKA `.tsv` schema.** Columns `locus_tag, ftype, length_bp, gene, EC_number, COG,
   product`. Parse only `ftype == "CDS"` rows. Missing fields → empty/None. Split multi-valued
   `EC_number`/`COG` (on `;`/`,`/whitespace) into one `Term` each: `product` →
   `Term(None, None, product, …)`; `EC_number` → `Term("EC", ec, ec, …)`; `gene` →
   `Term("GENE", gene, gene, …)`; `COG` → `Term("COG", cog, cog, …)`.
7. **PROKKA multi-ORF tie-break.** If a single-gene contig yields multiple CDS rows, select
   the **longest CDS by `length_bp`, ties broken by smallest start coordinate**; document in
   the module docstring. (Chosen over last-in-file: longest ORF is the most informative call.)
8. **DRAM2 — discover-and-pin on h100 (NOT fabricated here).** The DRAM2 module task runs on
   h100 where DRAM2 is installed. The developer must: discover the real `annotate` subcommand
   + flags from the live `dram2 --help`; run it on a tiny demo `.faa`; pin the exact invocation
   and the output annotations-table column schema in the module; and **commit the captured
   real output as a golden fixture** under `tests/fixtures/dram2/` (`demo.faa` +
   `annotations.tsv`). Do not guess the CLI from memory. Namespace tagging: KO/KEGG → `"KO"`,
   Pfam → `"PF"`, EC → `"EC"`, CAZy/dbCAN → `"CAZY"`; unknown columns → `namespace=None`
   free-text. Map back via the `.faa` header == caller id.
9. **Transyt Docker invocation.** Determine the **actual** `merlin-sysbio/kb_transyt` image
   reference at build time (build from its Dockerfile or pull the published tag); record the
   exact tag/digest in config `transyt.docker_image` and in provenance. Run:
   `docker run --rm -v {indir}:/workdir/processingDir --entrypoint bash <image> -lc 'neo4j
   start && <neo4j-readiness-poll> && java --add-exports java.base/jdk.internal.misc=ALL-UNNAMED
   -Dio.netty.tryReflectionSetAccessible=true -Dworkdir=/workdir -Xmx4096m -jar
   /opt/transyt/transyt.jar 3 /workdir/processingDir/'`. **Replace any fixed `sleep` with a
   Neo4j readiness poll** — loop `curl -fsS http://localhost:7474/` (or `neo4j status`) up to a
   max timeout (default 120s) before launching the JAR. Results land in
   `{indir}/results/`. Availability probe: `docker image inspect <tag>` succeeds. Treat exit
   code 8 / empty results as "no resolvable taxonomy → no annotations" (not an error).
10. **Transyt parse mapping.** From `results/transyt.xml` (SBML) extract per-gene GPR →
    `Term("TC", <TC family>, "transporter", …)`. From `results/reactions_references.txt` map
    each Transyt reaction → ModelSEED reaction → `Term("MSRXN", <MSrxn>, "reaction", …)`;
    metabolites → `Term("MSCPD", <MScpd>, "compound", …)`. Multiple transporters per gene →
    multiple `Term`s on the same `AnnotationRecord`. Map back to the caller id via the FASTA
    header / GPR gene id.
11. **GAA wrap shape.** Mirror the existing `DramPlugin` / `Glm4ecPlugin` / `TransytPlugin`
    shape in `annotation_runner.py` (the in_context developer reads it in the worktree).
    Constructor injects the annotator plus the same store/resolver dependencies the sibling
    plugins already take; call `annotator.annotate(...)` and route records through the existing
    `SequenceStore` / `FunctionResolver` / `protein_annotations` path. Do not invent new store
    APIs.
12. **Test layout + gates.** `tests/annotators/test_annotator_utils.py`,
    `test_prokka_utils.py`, `test_dram2_utils.py`, `test_transyt_utils.py`; fixtures in
    `tests/fixtures/{prokka,dram2,transyt}/`. Live tests `@pytest.mark.integration` +
    `@pytest.mark.skipif(not <Utils>().is_available(), …)`; the DRAM2 live test additionally
    env-gated by `KBU_DRAM2_LIVE=1` so it only runs on h100. Keep `_run_*`/docker paths thin
    and delegate logic to pure `_parse_*` functions so the offline parse unit tests keep
    `fail_under=100` green.
13. **Provenance formats.** `tool_version` = string from the tool's `--version` (image
    tag/digest for Transyt); `db_version` = best-effort string from tool logs/output (None if
    unavailable); `run_id` = generated unique id (uuid4 hex); `command` = exact argv joined
    with shlex-quoting; `parameters` = dict of resolved values incl. defaults (threads, gcode,
    kingdom, tax_id, remap count, image tag).
14. **Dependency-direction guard.** Add `tests/guard/test_dependency_direction.py` scanning
    `src/kbutillib/**/*.py` for any import of `genome_annotation_aggregator` and failing if
    found.

### Config keys (consistency with mmseqs/skani)

New modules wire their own config keys following the existing pattern: `prokka.executable`
(default `prokka`), `dram2.executable` (default pinned on h100), `transyt.docker_image`,
`transyt.neo4j_timeout` (default 120). Read via `get_config_value(...)` as `MMSeqsUtils` does.

## Testing Decisions

Test external behavior (given an input dict and a canned tool output, the right records come
back keyed by the right ids), not implementation details.

- **Split pure parse from subprocess.** Each module separates a pure `_parse_*` (fully
  unit-testable on a golden fixture, offline) from `_run_*` (subprocess/docker). This keeps
  offline coverage high under the repo's `fail_under=100` gate without invoking real tools.
- **All four modules get offline unit tests** (mock + golden fixture). For PROKKA, the golden
  fixture is a real `.tsv` + `.gff`; the test covers id-remap (incl. a >32-char id), the
  contig-id → caller-id mapping, product/EC/gene/COG capture, and the zero-ORF /
  last-CDS-wins caveats. For Transyt, a golden `transyt.xml` + `reactions_references.txt`. For
  DRAM2, a golden annotations table captured from the h100 install.
- **Live `skipif`-gated integration tests** for all three tools, each skipped unless the tool
  is available: PROKKA live test runs where `prokka` is on PATH (local); **DRAM2 live test
  runs on h100**; Transyt live test runs where the Docker image is present.
- **Prior art:** GAA's existing `MockLocalBinaryAdapter` / `MockKBaseAdapter` golden-fixture
  tests and the `tests/fixtures/ecoli_mini` set; ModelSEEDpy/KBUtilLib token-gated tests that
  skip when deps/credentials are absent.
- **GAA wrap tests** inject a fake `AnnotatorUtils` returning a canned `AnnotationResult` and
  assert the correct `protein_annotations` rows (source id + provenance) are written, that
  re-runs are idempotent (storage dedup), and that a local-PROKKA row is distinguishable from
  a KBase-PROKKA row for the same protein.

## Out of Scope

- The KBase-app baselines: `KBASE_PROKKA`, the existing KBase `DRAM`/Transyt plugins, and
  `kbase_adapter.py` — unchanged. (The local-vs-KBase comparison is separate GAA analytics.)
- The PROKKA / DRAM / Transyt **local-vs-KBase comparison notebooks** — separate analytics
  work, built once both sources are in the store.
- Installing DRAM2 on the laptop — explicitly not done; DRAM2 is h100-only here.
- GAA capability Y (KBase genome create/import/reuse) and any genome-ingest work.
- PaperBLAST — its adapter logic in GAA is left as-is unless trivially affected by the
  `local_binary_adapter.py` cleanup; no PaperBLAST KBUtilLib module in this PRD.

## Further Notes

- The Transyt KBase app is maintained by Davide Lagoa (dlagoa@anl.gov) — an in-house contact
  if image/DB questions arise.
- One-directional dependency is a hard invariant: **GAA depends on KBUtilLib, never the
  reverse.** No `kbutillib` module may import from `genome_annotation_aggregator`.
- The return type intentionally maps onto the `kb_annotation_utils` vocabulary (EC, KO, TC,
  MSRXN, MSCPD, GO, PF, TIGR already present) so downstream aggregation is comparable.

## Acceptance Criteria

1. `Term`, `AnnotationRecord`, `AnnotationResult`, and `AnnotatorUtils` are defined in `src/kbutillib/annotator_utils.py` and exported from `src/kbutillib/__init__.py` under those exact names.
2. `AnnotatorUtils.annotate(sequences: dict[str, str], **params) -> AnnotationResult` is the single public method; `is_available()` is abstract and side-effect-free; `_require_available()` raises `ToolUnavailableError(tool, detail)` with the message format `"{tool} not available: {detail}. Install: {hint}"`.
3. Each module returns an `AnnotationResult` whose `records` are keyed by the caller's input ids (preserved exactly).
4. The DNA guard accepts the IUPAC nucleotide set + `-`/`*` and the protein guard accepts the AA set (incl. B/Z/X) + `-`/`*`, case-insensitive, raising `ValueError` when >10% of characters are out-of-alphabet. PROKKA rejects protein input; DRAM2/Transyt reject nucleotide input.
5. `ProkkaUtils.annotate` writes one single-gene-contig FASTA record per input gene using deterministic safe ids `g{index}`, runs `prokka … --prefix prokka --gcode <gcode> [--kingdom <k>] --cpus <threads> --force --quiet <fasta>` with **no `--proteins` flag**, and maps every result back to the caller id via the safe-id map.
6. `ProkkaUtils` rejects `kingdom` values outside `{None, "Bacteria", "Archaea", "Viruses"}` with `ValueError`.
7. `ProkkaUtils` parses the `.tsv`, processes only `CDS` rows, and emits Terms for product (free-text), EC (`EC`), gene (`GENE`), and COG (`COG`), splitting multi-valued EC/COG into separate Terms.
8. When a PROKKA single-gene contig yields multiple CDS, the longest-by-`length_bp` (ties → smallest start) is selected; an input gene with zero called ORFs is absent from `records` without error.
9. A PROKKA input id exceeding 32 characters does not abort the run (it is handled by the safe-id remap), unlike the KBase app.
10. `DRAM2Utils` writes the input proteins to a `.faa`, runs the DRAM2 `annotate` invocation pinned against the live h100 install, parses the real output annotations table, and maps results back via the `.faa` header == caller id.
11. A golden DRAM2 fixture captured from a real h100 run is committed under `tests/fixtures/dram2/`, and the DRAM2 live integration test runs only when `KBU_DRAM2_LIVE=1` and DRAM2 is available.
12. `TransytUtils.annotate` raises a clear error when `tax_id` is not supplied.
13. `TransytUtils` stages `protein.faa` + `params.txt` (`taxID`, `reference_database=ModelSEED`), invokes Transyt in the Docker image recorded at `transyt.docker_image`, waits for Neo4j via a readiness poll (not a fixed sleep) bounded by `transyt.neo4j_timeout`, and parses `results/transyt.xml` + `results/reactions_references.txt`.
14. `TransytUtils` emits Terms tagged `TC` (transporter family), `MSRXN` (ModelSEED reaction), and `MSCPD` (ModelSEED compound), keyed back to the caller's gene ids; exit code 8 / empty results yields an empty `records` list, not an exception.
15. `TransytUtils.is_available()` returns False (no raise) when the Docker image is absent (`docker image inspect` fails).
16. Each `AnnotationResult` populates `tool`, `tool_version`, `db_version` (best-effort, nullable), `run_id` (uuid4 hex), `command` (shlex-quoted argv), and `parameters` (resolved values incl. defaults).
17. The GAA plugins (`CLI_PROKKA`, local `DRAM2`, local `TRANSYT`) take the annotator as an injectable constructor dependency, mirror the existing `DramPlugin`/`Glm4ecPlugin` shape, and write `protein_annotations` rows with the correct source id + version provenance.
18. The PROKKA/PaperBLAST subprocess logic and command templates are removed from GAA's `local_binary_adapter.py`; GAA's offline tests pass by injecting a fake annotator (no real binaries invoked).
19. GAA offline tests prove a local-PROKKA (`CLI_PROKKA`) row is distinguishable from a KBase-PROKKA (`KBASE_PROKKA`) row for the same protein, and that re-runs are idempotent (storage dedup).
20. `tests/guard/test_dependency_direction.py` fails if any module under `src/kbutillib/` imports `genome_annotation_aggregator`.
21. Offline unit tests exist for all four KBUtilLib modules (mock + golden fixture), pass with no tool installed, and keep the repo's `fail_under=100` coverage gate green for the new modules.
22. No module under `src/kbutillib/` imports from `genome_annotation_aggregator` (the dependency is one-directional: GAA → KBUtilLib).
