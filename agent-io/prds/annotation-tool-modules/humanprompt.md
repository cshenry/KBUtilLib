# Annotation Tool Modules (DRAM2, PROKKA, Transyt) — human summary

Add three annotation tools to **KBUtilLib** as reusable APIs, and make **GAA** wrap them
thinly.

## The ask
"Add DRAM2, PROKKA, and Transyt to KBUtilLib. Consistent interface: submit proteins, get
annotations back. Each module checks whether its tool is installed and returns a clear error
if not. Don't install DRAM2 locally — it's on h100, test there. Put the useful APIs in
KBUtilLib; have GAA do a narrow wrap."

## What we decided
- **Separate module per tool** (matches `MMSeqsUtils`/`SKANIUtils`) behind a shared
  `AnnotatorUtils` base. The "consistent interface" is the **return type + availability
  contract**, not a forced-identical input.
- **Input: `{id: seq}` dict** for all three (in memory, not a FASTA path). Value molecule is
  per-tool: **nucleotide CDS for PROKKA**, amino-acid protein for DRAM2/Transyt.
- **PROKKA annotates already-called genes** by emulating the KBase `kb_prokka` trick (write
  each gene's DNA as its own single-gene contig, run plain contigs-in PROKKA, map back by
  contig id). Capture **product, EC, gene, COG** — richer than KBase's product+EC. Handle the
  32-char id limit by internal remap.
- **Transyt runs via Docker** — reuse `merlin-sysbio/kb_transyt` first (entrypoint override),
  derive a slimmed KBUtilLib Dockerfile later. Needs a mandatory NCBI `tax_id`.
- **DRAM2 CLI/output pinned against the h100 install at build time** (module built + live-
  tested on h100; not installed locally).
- **Seam:** KBUtilLib returns structured-native records; GAA's `FunctionResolver` does final
  normalization. One-directional dependency: GAA → KBUtilLib, never the reverse.
- **GAA wraps** take an injectable annotator, map results into the store (seq_hash / source /
  provenance), and **delete** the subprocess logic from GAA's `local_binary_adapter.py`.
- **Tests:** offline unit tests (mock + golden fixture) for all four modules; live
  `skipif`-gated tests for PROKKA (local) / DRAM2 (h100) / Transyt (Docker).

## Scope
Local tools only. KBase-app baselines (`KBASE_PROKKA`, KBase DRAM/Transyt) and the
local-vs-KBase comparison notebooks are separate and unchanged.

## Repos
KBUtilLib (the modules) + GenomeAnnotationAggregator (the narrow wraps).
