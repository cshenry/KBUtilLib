# Work Record: t4-annotate-genes-with-papers

## task_id
t4-annotate-genes-with-papers (PRD kbu-gene-literature-research-v1)

## branch
maestro/developer/base-kbutillib-main-25251e8-task-t4-annotate-genes-with-papers

## base
KBUtilLib `main` @ `25251e8` ("docs(work-record): t3-llm-web-search-optin")

## summary

Layer 1 of the gene-literature stack: `GeneLiteratureUtils` in
`src/kbutillib/domains/literature/annotate.py`. One call,
`annotate_genes_with_papers(genes, organism, ...)`, runs up to three
independent sources over a list of genes and returns a JSON-serialisable
`PaperAnnotation` keyed by canonical publication id, with the inverse gene
view and a per-gene per-source status block beside it.

- **paperblast** — batched `PaperBLASTUtils.search` + `papers_for_hits`.
  Links keep `homolog_id` / `homolog_organism` / `identity` /
  `query_coverage` / `subject_coverage` / `evalue` whatever class they end up
  in, curated included.
- **europepmc** — `alias_query(alias, [name] + synonyms)` then
  `EuropePMCClient.search`, per alias; every hit is `direct` and carries the
  alias as `query_term`.
- **uniprot** — `KBUniProtUtils.get_publications(accession)` per accession,
  minus references whose scope is entirely whole-genome sequencing or
  reannotation.

Evidence-class precedence is implemented exactly as the CONFRONT addendum
binds it: `curated` first, then `direct` only on identity == 1.0 **and**
query_coverage == 1.0 **and** subject_coverage == 1.0 **and** an organism
match, else `homolog`; Europe PMC and UniProt links are `direct`; and the
`mention` override is applied **last**, over every class above, to every link
of any paper touching more than `mention_threshold` genes.

Papers are merged with the Europe PMC **transitive** union (`merge_ids`), and
each link is then re-keyed through `MergedIds.aliases` so that a link whose
own primary id is a DOI lands on the PMID key when some other link proved
they are the same paper. The inverse view is built from the re-keyed id, so
the two views cannot disagree (asserted by an `assert_views_agree` helper run
in every structural test).

Failure is contained per gene per source and never aborts the call: a raising
source sets that gene's `state="error"` with `"<ExceptionType>: <message>"`
and the run continues; links that did come back for that gene are kept and
counted. `skipped` is distinguished from `no_hits` — no sequence, no
searchable alias, or no accession is `skipped`; ran-and-found-nothing is
`no_hits`.

`KBUniProtUtils.get_publications` gained an **additive** `scope` key (the
reference's `referencePositions` list). No existing key moved or changed.

## files_touched

- `src/kbutillib/domains/literature/annotate.py` — new, 1169 lines. Models
  `Gene`, `Organism`, `Evidence`, `PaperIds`, `PaperEntry`, `SourceStatus`,
  `GeneEntry`, `Provenance`, `PaperAnnotation` (all pydantic, all
  `Optional[...]` not PEP 604); `GeneLiteratureUtils(SharedEnvUtils)`;
  `GeneLiteratureUtilsImpl` composition wrapper delegating via `__getattr__`
  (`kb_uniprot_utils.py` was the structural template); module constants
  `DEFAULT_SOURCES`, `KNOWN_SOURCES`, `EVIDENCE_CLASSES`, `SOURCE_STATES`,
  `MENTION_THRESHOLD_CONFIG_KEY`, `DEFAULT_MENTION_THRESHOLD = 50`,
  `UNIPROT_ACCESSION_RE`, `SEQUENCING_SCOPE_MARKERS`; helpers
  `uniprot_accessions`, `is_sequencing_scope`.
  - `annotate.py:623` `annotate_genes_with_papers` — duplicate-id and
    unknown-source checks run **before** any search.
  - `annotate.py:736` `_run_paperblast`, `:794` `_paperblast_link`,
    `:855` `_run_europepmc`, `:935` `_run_uniprot`, `:1031` `_assemble`.
  - `annotate.py:244` `_valid_id` — drops a malformed pmid/pmcid/doi at
    record-construction time, because `merge_ids` and `canonical_paper_id`
    both raise `ValueError` on one and a bad `litsearch.db` row must not be
    able to abort an annotation.
  - `annotate.py:308` `Organism.matches` — whole-lowercase-word-token
    contiguous-subsequence match over name, synonyms and `str(taxon_id)`, so
    `"Acinetobacter baylyi"` and `"ADP1"` both match
    `"Acinetobacter baylyi ADP1"` while `"Acinetobacter baumannii AB5075"`
    does not.
- `src/kbutillib/domains/external/kb_uniprot_utils.py:179` —
  `get_publications` now emits `scope` per publication, from
  `referencePositions` (with `scope` accepted as an alias spelling and a bare
  string normalised to a one-element list). Docstring records what the key
  means. Existing keys untouched; the field projection on line 79 is
  unchanged (see *divergences*).
- `src/kbutillib/domains/literature/__init__.py` — the existing lazy export
  map **extended** (not replaced) with 16 `annotate` names.
- `src/kbutillib/toolkit.py:50,130,391` — `TYPE_CHECKING` import, `_…= None`
  slot and lazy `KBUtilLib.gene_literature -> GeneLiteratureUtilsImpl`
  property, in the shape every neighbouring property uses.
- `src/kbutillib/__init__.py:250` — guarded (`try/except ImportError` +
  `_import_error("gene_literature", e)`) export of `Evidence`, `Gene`,
  `GeneLiteratureUtils`, `Organism`, `PaperAnnotation`, plus the five names in
  `__all__`.
- `tests/literature/test_annotate.py` — new, 49 tests.
- `tests/external/test_kb_uniprot_utils.py` — new, 5 tests (the module had no
  test file at all before).

## UniProt scope: confirmed against live UniProt, not assumed

Network was reachable from this worktree, so the addendum's "confirm the
retrieval actually returns reference scope fields" was done against
`rest.uniprot.org` on **2026-10-20** rather than taken on trust.

**The existing field projection already returns the scope — nothing had to be
added.** `get_uniprot_entry`'s `params["fields"]` (line 79) is forwarded as
`lit_pubmed_id,lit_doi_id,cc_interaction`, and that response carries
`referencePositions` on every reference:

```
$ curl -s "https://rest.uniprot.org/uniprotkb/Q6FF68.json?fields=lit_pubmed_id,lit_doi_id,cc_interaction"
  references[0].referencePositions == ["NUCLEOTIDE SEQUENCE [LARGE SCALE GENOMIC DNA]"]
  references[0] keys == ['citation','referenceComments','referenceNumber','referencePositions']
```

Both marker strings are verbatim-correct:

| marker | confirmed on |
|---|---|
| `NUCLEOTIDE SEQUENCE [LARGE SCALE GENOMIC DNA]` | `Q6FF68` ref 1; `P0A7B8` refs 2-4 |
| `GENOME REANNOTATION` | `A0A021WW32` ref 3, found via `?query=scope:"GENOME REANNOTATION"` |

Scope strings that must **not** be filtered were sampled too, from `P0A7B8`,
`P0A6F5`, `P0A9W9`, `P9WNK5`: `NUCLEOTIDE SEQUENCE [GENOMIC DNA]` (no "LARGE
SCALE"), `FUNCTION`, `CATALYTIC ACTIVITY`, `MUTAGENESIS OF …`, `REVIEW`,
`X-RAY CRYSTALLOGRAPHY (…)`, `IDENTIFICATION BY MASS SPECTROMETRY`,
`ACETYLATION [LARGE SCALE ANALYSIS] AT LYS-117`. The last one is why the
marker is the full `LARGE SCALE GENOMIC DNA` and not `LARGE SCALE`.

Filtering is per the addendum: a reference is dropped only when **every** one
of its scopes is a sequencing/reannotation scope. A reference with no scope,
or an empty scope list, is **kept** — unknown is not the same as sequencing.
`["NUCLEOTIDE SEQUENCE [LARGE SCALE GENOMIC DNA]", "FUNCTION"]` is kept.

## NO-INJECTION CHECK: it ran, and it passed

`tests/literature/test_annotate.py::TestNoInjectionPaperBLASTPath::test_real_mmseqs_hits_become_keyed_papers_with_homolog_metrics`

**Status on this host (h100 / poplar): RAN, PASSED.** Not skipped —
`mmseqs` is installed at `/home/chenry/.local/bin/mmseqs`.

```
$ TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src python -m pytest \
    tests/literature/test_annotate.py -q -p no:cacheprovider --no-header -k NoInjection -v
1 passed, 48 deselected in 4.42s          # exit 0
```

Nothing is injected. The test constructs `GeneLiteratureUtils` with **config
only** —

```python
config={"paperblast": {"data_dir": str(data_dir)},
        "gene_literature": {"cache_path": str(tmp_path / "cache.sqlite")}}
```

— so the real `PaperBLASTUtils` and the real `MMSeqsUtils` are both built by
the lazy properties, and a real `mmseqs createdb / search / convertalis` runs
over t2's fixture snapshot (reused via `from .test_paperblast import
make_snapshot, REP_SEQ, CURATED_SEQ, …`, so the two layers cannot drift
apart). It asserts:

- papers keyed by canonical id — `PMID:1001` (direct `GenePaper` row) and
  `PMID:2002` (reachable **only** through `SeqToDuplicate` expansion);
- a `homolog`/`direct` Evidence carrying real `identity`, real
  `query_coverage` and real `subject_coverage` (all from `convertalis`, all
  in `(0, 1]`), plus `evalue`, `homolog_id` and `homolog_organism`;
- a **curated** Evidence for the second gene — `PMID:4004`,
  `curated_db="SwissProt"`, `homolog_id="SwissProt::P12345"` — with its
  alignment metrics still attached;
- the class follows the real numbers rather than an assumption about them:
  `direct` iff identity, query_coverage and subject_coverage are all exactly
  1.0 (which they are, for a self-alignment), else `homolog`;
- the E. coli duplicate link is `homolog` whatever the numbers, because the
  organism does not match;
- provenance picked up the real `snapshot.json` (`stats.nSeq == 3`), the
  result JSON round-trips, and the two views agree.

## success_criteria_check

| criterion | where | status |
|---|---|---|
| JSON-serialisable `PaperAnnotation` keyed by canonical paper id | `annotate.py:1031` `_assemble`; `TestIdMerge`, `TestSerialisation` | met |
| per-gene evidence with source, evidence_class, homolog metrics | `Evidence` (`annotate.py:351`); `TestEvidenceClass` (13 cases) | met |
| per-gene per-source statuses | `SourceStatus`/`GeneEntry`; `TestStatuses` (7 tests) | met |
| sources failing per gene never abort | `_run_*` try/except; `test_a_source_that_raises_for_a_gene_never_aborts_the_call`, `test_a_failing_paperblast_batch_errors_every_gene_in_the_batch_only` | met |
| mention reclassification | `_assemble` post-pass; `TestMentionReclassification` (5 tests, incl. the `== threshold` boundary and "applied last over curated") | met |
| UniProt sequencing-scope filtering | `_is_sequencing_reference`; `TestUniProtScopeFiltering` (4 tests) | met |
| `get_publications` gains an additive `scope` key | `kb_uniprot_utils.py:179`; `tests/external/test_kb_uniprot_utils.py` (5 tests, one asserting the existing key set is unchanged) | met |
| offline tests cover all of this | `tests/literature/test_annotate.py`, 48 of 49 fully offline | met |
| NO-INJECTION real-PaperBLAST/real-MMSeqs test | `TestNoInjectionPaperBLASTPath` | met, and it RAN on this host |
| `for_gene`, `to_json`/`from_json` | `PaperAnnotation`; `TestForGene` (5), `TestSerialisation` (4) | met |
| duplicate gene ids -> ValueError before any search | `annotate_genes_with_papers`; `test_duplicate_gene_ids_raise_before_any_search` asserts all three fakes recorded zero calls | met |
| lazy toolkit property + guarded exports | `toolkit.py:391`, `__init__.py:250`, `domains/literature/__init__.py` | met |
| never import `genome_annotation_aggregator` | `tests/guard/test_dependency_direction.py` — **4 passed** (exit 0); the only matches under `src/` are a docstring mention in `annotator_utils.py:8`, no import | met |

## tests_run

Everything below was run in the FOREGROUND, with
`TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src`, interpreter
`/home/chenry/venvs/kbdl/bin/python` (cpython 3.11.15), pytest 9.1.1.

| command | result | exit |
|---|---|---|
| `pytest tests/literature/test_annotate.py -q` | **49 passed** in 4.52 s | 0 |
| `pytest tests/literature/test_annotate.py -k NoInjection -v` | **1 passed**, 48 deselected | 0 |
| `pytest tests/external/test_kb_uniprot_utils.py -q` | **5 passed** in 1.99 s | 0 |
| `pytest tests/literature tests/external -q` (after the line-wrap pass) | **385 passed, 5 skipped** in 28.1 s | 0 |
| `pytest tests/literature tests/external tests/core -q` | 747 passed, 9 skipped, 1 xfailed, **3 failed, 8 errors** | 1 |
| `pytest tests/interfaces tests/domains tests/cli -q` | 2329 passed, 6 skipped, **28 failed** in 337 s | 1 |

## regression framing

`build-baselines/kbutillib.json` exists, so the full suite was **not** re-run
(`build-baselines/kbutillib-h100-main-6310d61.json` measures it at 3416 s —
5.7x the Bash ceiling). Targeted modules were run instead and compared **by
nodeid**.

The cached baselines are at `0f84803` and `6310d61`; this branch is on
`25251e8`, three literature commits later, so four failing nodeids on my
branch were **absent from the 6310d61 name set** and had to be checked
properly. I did that by cutting a detached worktree at the base commit —

```
$ git worktree add --detach /scratch/chenry/t4-base 25251e8
```

— and running the same selections there. Measured base state at `25251e8`:

| selection | at `25251e8` (base) | on this branch |
|---|---|---|
| `tests/core tests/external` | 3 failed, 554 passed, 7 skipped, 8 errors | same 3 failed, same 8 errors, +193 passed |
| the 4 not-in-6310d61 names | all 4 **fail at base too** | same 4 fail |

The four: `tests/interfaces/test_mcp_server.py::TestMain::test_main_list_json_runs_without_mcp`,
`tests/cli/test_kind_install_containment.py::test_install_records_source_root_in_the_registry`,
`tests/domains/ai/council/test_types_config.py::TestPackagedDefaultConfig::test_codex_seats_are_marked_unverified`,
`…::test_default_profile_is_mixed_subscription`. I checked the MCP one first
and most carefully, because adding a `KBUtilLib` property is exactly the kind
of change that could break a capability-registry JSON dump: it fails at the
base commit identically, and the cause is unrelated — a `KBBERDLUtils` INFO
log line lands on stdout ahead of the JSON, so `json.loads(result.output)`
raises `Extra data: line 1 column 5`. The same stdout-pollution explains the
three `tests/core/test_cli_cap.py::TestCapList` failures (also present at
base). The council/kind failures are the h100 user-config leakage the
`6310d61` baseline already documents under
`MACHINE_LOCAL_FAILURES_NOT_PRODUCT_DEFECTS`.

**No test that passed at `25251e8` fails on this branch.** The temporary
worktree was removed afterwards.

No new `build-baselines/*.json` was written: one already exists for this repo,
and per the role prompt it is only to be created when absent.

## inferred rather than verified

- **`mention` counts genes, not links.** The prompt says "linked to more than
  `mention_threshold` genes", so the comparison is `len(paper.genes) >
  threshold` — distinct genes, strictly greater. A paper on exactly
  `threshold` genes is **not** a mention (asserted).
- **`n_hits` counts retained evidence links, not raw source rows.** A
  reference dropped by the sequencing filter, and a link with no usable
  publication id, do not count. This keeps `n_hits == 0` and `paper_ids == []`
  in agreement, and makes "returned something but nothing survived" report
  `no_hits` rather than `ok`. The prompt did not define it.
- **error outranks hits in `state`.** A gene whose second accession raised
  while its first succeeded reports `state="error"` with the message **and**
  `n_hits=1`, keeping the links that worked. Read from "a source that raises
  for a gene sets that gene's status for that source to error".
- **PaperBLAST failure is attributed to the whole batch.** `search` and
  `papers_for_hits` are batch calls over every sequenced gene, so a raise in
  either marks every gene in that batch `error` — the batch is what failed.
  Sequenceless genes are never in the batch and stay `skipped`.
- **A PaperBLAST Evidence has no `query_term`.** `papers_for_hits` does not
  publish `GenePaper.queryTerm` (`LINK_KEYS` has no such key — it is used
  internally only to pick the snippet), so the field is `None` there rather
  than invented.
- **`taxon_id` organism match** is "`str(taxon_id)` appears as a word token in
  the homolog organism string". PaperBLAST's `Gene.organism` is a free-text
  name and in practice never carries a taxon id, so this branch is covered by
  a unit case (`"taxon 62977"`) but has not been seen in real snapshot data.
- **An unknown source name raises `ValueError`.** The prompt's containment
  rule is about per-gene source failures; a misspelt source name is a caller
  bug, so it is refused up front alongside the duplicate-id check rather than
  silently skipped.
- `Gene.uniprot` is trusted as given (not regex-filtered), so an isoform id
  such as `P12345-2` still reaches UniProt; only *aliases* must match
  `UNIPROT_ACCESSION_RE` to be used as accessions.

## lint

`ruff` is **not installed** in `/home/chenry/venvs/kbdl` or on PATH on this
host, and `.git/hooks/pre-commit` is not installed in this worktree, so the
repo's `.pre-commit-config.yaml` hooks (`ruff check --fix`, `ruff format`)
could **not** be run. As a substitute: `python -m py_compile` is clean on
every changed file, imports were checked by hand for unused names, and no
line in the new files exceeds 88 characters except one 89-char Sphinx
cross-reference in a docstring. A ruff pass on these files is still owed.

## divergences reconciled (prompt vs. tree)

1. **"if `src/kbutillib/domains/literature/cache.py` does not exist, create a
   small SQLite helper `LiteratureCache(path)`"** — it **exists** (t1 landed
   it, 6677 bytes) with exactly the `get(table,key)` / `put(table,key,value_json)`
   API, the `gene_literature.cache_path` default and create-on-first-use
   tables. Nothing created; the shipped one is used through the collaborators,
   which own their own caches.
2. **"Tests go under `tests/literature/` (create it)"** — it **exists**, with
   `__init__.py`, `test_europepmc.py`, `test_paperblast.py`,
   `test_europepmc_live.py` and a `fixtures/` directory. Added
   `test_annotate.py` to it and reused t2's snapshot fixtures from
   `test_paperblast.py`.
3. **"New code goes in a NEW package `src/kbutillib/domains/literature/`
   (create `__init__.py`)"** — the package and its `__init__.py` exist. The
   lazy export map was **extended**, as the re-check note said to do.
4. **"EXTEND `get_publications` … add the needed field"** — no field needed
   adding. The addendum told me to confirm the retrieval actually returns
   reference scope, and it does: the existing
   `lit_pubmed_id,lit_doi_id,cc_interaction` projection already returns
   `referencePositions` (live-verified above). Adding a field would have been
   a change with no effect, so the `fields` list on line 205 is untouched and
   a test pins that projection as the one that works.
5. **`KBUniProtUtils.get_publications` line numbers** — the prompt said
   ~179-228/~179-225 and the field projection ~line 79. Both correct at
   `25251e8`; no reconciliation needed.
6. **`merge_ids`/`MergedIds` "~line 265"** — correct (`MergedIds` at 238,
   `merge_ids` at 265).
7. **"`PaperBLASTUtils.search` + `papers_for_hits`"** — as shipped, `search`
   takes a `{gene_id: sequence}` **dict** and `papers_for_hits` takes
   `{gene_id: [hit]}`, i.e. both are batch calls, not per-gene ones. The
   implementation therefore batches one `search` over every sequenced gene
   rather than calling per gene, which also matches the per-sequence cache
   those methods already keep.
8. **`PaperAnnotation.papers[...].year`** — the prompt lists `year` without a
   type. PaperBLAST's `_year` can yield a `str` and Europe PMC yields an
   `int`, so the model declares `Optional[int]` and a `_as_year` helper
   coerces all three sources the same way `EuropePMCClient._parse_hit`
   already does. A non-numeric year becomes `None`.
9. **No toolkit/`__init__` precedent from t1/t2** — neither `EuropePMCClient`
   nor `PaperBLASTUtils` was registered in `toolkit.py` or in the top-level
   `kbutillib/__init__.py` by t1/t2. The prompt asks for a lazy toolkit
   property and guarded exports for this layer, so `gene_literature` is
   registered following the `uniprot`/`pdb` property pattern and the
   `mmseqs_utils` guarded-import pattern. The t1/t2 classes were left
   unregistered — widening their surface is not this task's scope.
