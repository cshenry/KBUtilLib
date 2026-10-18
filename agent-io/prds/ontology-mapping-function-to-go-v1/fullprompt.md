# OntologyMapping: a `function_to_go` capability for KBUtilLib

## Research Report

### Important findings that bear on the design

**1. KBUtilLib already owns most of the substrate. This PRD is an assembly job, not a greenfield library.**
The pieces that already exist on `main` and are directly reusable:

- `OntologyDictionary` (`domains/genome/annotation/ontology_dictionary.py`) already serves
  `describe("GO", accession) -> str | None` over a staged two-column TSV, with lazy
  per-namespace loading, a cached table, a `degraded_namespaces` set for the missing-file case,
  and a hard `RuntimeError` for the file-exists-but-parses-to-zero case. Its missing-file-vs-
  zero-entry distinction is exactly the degradation contract this capability needs, and it is
  already tested (`tests/annotators/test_ontology_dictionary.py`).
- `build_go_description_map.py` already parses `go-basic.obo` `[Term]` stanzas, already skips
  `is_obsolete: true`, already records a `--release-version`, and its output is already
  `.gitignore`d (line 92, `go_description_map*.tsv`) under a deployment data root. The OBO
  parsing problem is solved; it simply extracts too few fields today (`id` and `name` only).
- `EcRoleResolver` (`domains/modeling/ec_role_resolver.py`) is the house precedent for
  identifier mapping done right: a curated table, literal keying, and an explicit refusal to do
  fuzzy or prefix matching. Its module docstring states the principle this PRD adopts wholesale
  — "reuses ModelSEED's own curated role vocabulary ... instead of string similarity."
- `Term` / `AnnotationRecord` / `AnnotationResult` (`annotation/annotator_utils.py`) are the
  established shape for a functional annotation term, and `describe_or_accession()` is the
  established `"<accession>: <description>"` rendering with its documented
  split-on-first-`": "` rule for GO's embedded colon.
- The `@capability` registry (`core/capability.py`, `core/registry.py`) already exposes any
  decorated `*Impl` method over four transports (`lib`, `cli`, `mcp`, `api`), and
  `interfaces/api/app.py` already serves them at `POST /v1/tools/{name}`. "A new API library"
  therefore means *a new capability domain plus a facade property*, not new transport code.

**2. The code we were pointed at cannot, on its own, answer `function_to_go`.**
`/scratch/vsetlur/function-ontology-mapping-program/studies/01_function_to_go/pipelines/type_4/full_specificity_rrf_fusion/source.py`
is a **Type 4 fusion stage**, and fusion is the one stage in that program that computes no
scores. It consumes two already-COMPLETE parent runs (Type 1 BM25, Type 2 PU logistic), verifies
an extensive provenance contract against them, and reciprocal-rank-fuses their *stored rankings*.
It is therefore structurally incapable of scoring a function description that is not already in
the frozen corpus, and its own README states full production is "deliberately blocked until the
external-order streaming join and recovery/checkpoint contract is implemented and validated",
with `run()` raising unless a `--query-manifest` of at most 1,000 pre-selected frozen **test**
queries is supplied. Wrapping Type 4 as `function_to_go` would produce a function that can only
answer questions it was already given the answers to. The usable inference logic lives one level
up, in **Type 1**, which is fit-free by construction.

**3. The GO Consortium publishes curated deterministic identifier→GO mappings, and for
annotation-derived input they should run before any text ranking.**
`external2go` (notably `ec2go` and `interpro2go`) are manually curated transitive mapping files
([ec2go](http://www.geneontology.org/external2go/ec2go),
[interpro2go](http://current.geneontology.org/ontology/external2go/interpro2go),
[GO_REF:0000002](https://geneontology.org/GO_REF/0000002.html)). This matters because the input
this capability will actually receive is overwhelmingly *not* arbitrary English — it is
annotation-pipeline output from BAKTA, KOfamScan, Prokka, DRAM2 and RAST, which frequently
carries an EC number inline (`"Arylsulfatase B precursor (EC 3.1.6.12)"`). For those, a curated
EC→GO lookup is both more accurate and more defensible than character-n-gram similarity, and it
reuses the EC-parsing regex discipline already in `EcRoleResolver`. Text ranking earns its place
on the residue: descriptions with no parseable identifier.

**4. The previous attempt at this exact surface was retired for a dependency reason that still
holds today, and it is measurable on this machine.**
PRD `gaa-ontomap-council-mapping` (GenomeAnnotationAggregator + KBUtilLib) was archived
2026-07-12 with the note that its remaining tasks were "blocked on Vibhav non-public assets
(SSO LoRA + target_sapbert.npz) + h100 env". The artifact that survived is `OntomapUtils`, which
wraps the external `ontomap` package. Measured on poplar 2026-10-18: `ontomap` imports in **none**
of the 20 venvs on this host, including the three that do import `kbutillib`
(`~/venvs/{gaa,gaa-model,kbdl}`). `OntomapUtils.map_functions` has therefore never been
executable here, and its test suite never loads the Pipeline — `tests/external/test_ontomap_utils.py`
says so in its own docstring ("The ontomap Pipeline is never actually loaded"). Building
`function_to_go` on `ontomap` would re-acquire the dependency that already killed one PRD.

**5. The most expensive lesson available is from the sibling PRD that shipped and was inert.**
`kbdl-ontology-descriptions-v1` (done, 2026-08-27) recorded: "the PRD was INERT IN PRODUCTION.
The KBDL adapter constructed BaktaUtils/KofamscanUtils passing neither `ontology_dictionary=`
nor `ec_role_resolver=` ... Six tasks shipped and passed review without catching it because the
real-dependency guards INJECT an OntologyDictionary at the `ontology_dictionary=` constructor
seam — proving the annotators work WHEN WIRED and nothing about whether production wires them."
The reaction-yield delta of that entire build was **zero**. This PRD's acceptance criteria are
written against that failure specifically: the gate is a call through the public facade with
**nothing injected**, not a unit test with a fixture dictionary.

**6. A frozen, richly-structured GO corpus already exists and is readable, but it is not ours.**
`/scratch/vsetlur/.../data/candidate/go-candidates.jsonl` (51 MB, world-readable) carries exactly
the fields a text ranker needs — `go_id`, `name`, `synonyms`, `definition`, `namespace`, `depth`,
`height`, `leaf`, `obsolete`, `alt_ids`, `replaced_by`, `consider`, `typed_parents` (with
`relation`), plus `source_release: "2026-07-26"` and a `source_sha256`. It is a good schema to
copy and a bad artifact to depend on: it lives in another user's `/scratch`, which this fleet
treats as ephemeral, and its sibling `results/` subdirectories are already permission-denied to
us. The design borrows the **schema** and regenerates the **data** from `go-basic.obo` through
KBUtilLib's own builder.

**7. The external scan changed one default and added three decisions.** Run cross-family (codex,
Maestro `task-ebbb3fd6`) with live web access, it contributed four things this design did not
have:

- **A concrete reason not to fuse by default.** RRF has no monotonic improvement guarantee, and
  the scan supplied the arithmetic for our exact shape: a relevant term at ranks 1 and 100 scores
  `1/61 + 1/160 ≈ 0.02264` while an irrelevant term at ranks 2 and 1 scores
  `1/62 + 1/61 ≈ 0.03252` — fusion reverses the better system's correct lead. Cormack et al. fixed
  `k=60` in pilot work (MAP 0.2145 at k=60 vs 0.2147 at k=80 — local robustness, not a universal
  optimum) and reported exceptions; Bruch et al. (2023) found tuned convex combination beating
  RRF. Q4 flipped from "equal-weight RRF by default" to "route precedence by default, RRF opt-in".
- **GO synonyms carry a scope** (`exact`/`broad`/`narrow`/`related`), and collapsing them "can
  create false specificity". The research program's corpus flattens them; ours does not (Q11).
- **Abstention is the correct answer for placeholder products** — "'Hypothetical protein' is a
  legitimate abstention, not a request to find the least bad GO term" (Q12).
- **The structured-record interface**, which the scan recommended directly on the grounds that
  "a product string alone discards useful evidence" (Q13). It also resolved KO→GO as a
  *licensing* question rather than a technical one: `kegg_reaction2go` maps KEGG reactions, not
  KOs, and the real KO crosswalk is KEGG-hosted under terms separate from GO's CC BY 4.0 (Q14).

It also corrected a URL this design had wrong — the current `ec2go` lives at
`https://current.geneontology.org/ontology/external2go/ec2go`, and its verified header referenced
GO `releases/2026-07-26`, the same release the research program's frozen corpus was built from.
And it named the closest reusable prior art, `text2term` (MIT, maintained, TF-IDF/cosine label
matching), which we are not adopting — it is generic ontology matching with no GO-specific
accuracy guarantee and would add a dependency — but which should be reconsidered if the BM25
route measures badly.

### Links to full research output

- [External prior-art and literature scan](research/external-prior-art-scan.md) — cross-family
  (codex) scan with live web access, covering prior art in function-text→GO mapping, CAFA
  benchmarking conventions, the RRF literature, reusable Python libraries
  (`goatools`/`pronto`/`obonet`, `rank_bm25`/`bm25s`, `ranx`, `text2term`), embedding approaches,
  and GO obsolescence handling. Its library section is the input to any future decision to
  replace hand-written code here; it is deliberately **not** acted on in v1, because every
  candidate adds a dependency and Q1 makes dependency-freedom load-bearing.

### Research quality report

**What was read directly, in full:** the entire Type 4 package (`source.py` 450 lines,
`config.yaml`, `environment.yaml`, `README.md`, `manifests/component-contract.json`); the Type 1
and Type 2 package READMEs; the Function→GO data-foundation README and a full candidate record;
`ontomap_utils.py` (320 lines) and its test; `ontology_dictionary.py` (292 lines);
`build_go_description_map.py`; the `Term`/`AnnotationRecord`/`AnnotationResult` dataclasses and
`describe_or_accession()`; `ec_role_resolver.py`'s contract; `core/README.md` and
`interfaces/api/README.md`; the `KBUtilLib` facade's `ontomap` property; and the registry
entries for both prior PRDs.

**What was measured rather than assumed:** `ontomap` import availability across all 20 h100 venvs
with a scrubbed environment (`env -u PYTHONPATH -u VIRTUAL_ENV -u PYTHONHOME`); the identity of
the KBUtilLib install tree by `stat` device:inode (`64517:5167606134` — `~/projects/KBUtilLib`
and `/scratch/fliu/hub_home/chenry/H100Projects/KBUtilLib` are the same clone, not two trees);
the Maestro pool's `accepted_backends: [claude, codex]`; and the readability of the vsetlur data
tree (candidate/ readable, results/ permission-denied).

**What was NOT searched, and why it matters:**

- **No Type 1 `source.py` was read.** The design ports Type 1's *algorithm* from its README
  (BM25 over character 3–5 grams; `name` 3.0 / `synonyms` 2.0 / `definition` 1.0) rather than
  from its implementation. The weights and n-gram range are therefore second-hand, and the
  tokenisation details — case folding, punctuation, IDF variant, the BM25 `k1`/`b` constants —
  are **unknown**. Task 3 is written to read that file first. If it has been deleted or is
  unreadable by then, the implementer must choose defaults and say so.
- **No accuracy number for any route is known to this design.** The program's own reports
  directory was not read, and the Type 4 benchmark it describes is explicitly "an operational
  benchmark, not a final production claim". **This PRD therefore makes no accuracy claim at all**,
  and deliberately ships an evaluation harness (Task 6) instead of a quality threshold.
- **The `ec2go` file was confirmed to exist but was not downloaded or parsed.** Its exact line
  format and release cadence are taken from the GO Consortium's documented conventions, not from
  a file on disk. Task 2 must verify the format before relying on it.
- **No license review was done** on `go-basic.obo` or `ec2go` beyond noting that GO content is
  CC-BY and that the repo already treats bulk ontology output as un-committable.

**On the external scan specifically:** it ran with live web access and verified package metadata
against PyPI's JSON API and upstream repositories on 2026-10-17. It labels its own deductions as
deductions, and states that it did not install or performance-test any package and did not
reproduce the in-house Type 2 timings. Its RRF counterexample is arithmetic rather than a quoted
experiment — which is why Q4 relies on it: it is checkable by hand.

**Confidence the design should carry:** high on *architecture* (the reuse targets were read in
full and the integration seams are verified), high on *the dependency and inertness risks* (both
measured, both with named prior failures), high on *not defaulting to fusion* (arithmetic, and
it matches our route asymmetry), **low on anything quantitative** — no accuracy, no latency, and
no corpus-size figure in this document has been measured by its author. The synonym-scope
multipliers (Q11) and the choice of precedence over a tuned combination (Q4) are explicitly
judgement calls awaiting Task 6's measurement. Where a number appears it is sourced and labelled.

## Revision Log

**Round 0 — 2026-10-18 (Draft Mode, single pass).**
Initial complete bundle. Scope was reshaped twice during local research: first from "wrap the
type_4 code" to "port Type 1 and reuse the house substrate", once Type 4 was found to be a fusion
stage that cannot score novel input; then from "new standalone library" to "new capability domain
over existing modules", on Chris's in-session steer ("if there are already capabilities, build on
those"). Type 2 and the `ontomap` dependency are both out of scope, each for a measured reason.

The cross-family external scan then landed before commit and changed the bundle again, within
the same round: the default combine mode flipped from equal-weight RRF to route precedence (Q4,
on a concrete k=60 counterexample), and three decisions were added — synonym scope in the corpus
and the ranker (Q11), explicit abstention on placeholder products (Q12), and a structured-record
input alongside text (Q13), plus KO→GO deferred on licensing grounds (Q14). Those edits are
folded into the Solution, Implementation Decisions, Testing Decisions, Gotchas and taskplan, not
merely appended to the research section.

## Problem Statement

A user holding free-text functional descriptions — typically the `product` / `function` strings
that come out of BAKTA, KOfamScan, Prokka, DRAM2 or RAST for a newly annotated genome — has no
way in KBUtilLib to get Gene Ontology terms for them.

Today they have three unsatisfying options. They can read GO terms out of whichever annotator
happened to emit them, which gives patchy coverage and ties the answer to one tool's database
version. They can call `OntomapUtils.map_functions()`, which maps to ModelSEED **reactions**
rather than GO terms, and which cannot run anywhere on this fleet because the `ontomap` package
is installed in none of the host's venvs. Or they can go to the Function→GO research program in
`/scratch/vsetlur/`, which has done the real scientific work but exposes it only as a
runner-driven benchmark harness: it requires a frozen 500-query manifest, two COMPLETE parent
runs with matching provenance hashes, and a study data root, and it refuses by design to score a
query that is not already in its corpus.

So the capability exists as research and does not exist as software. The gap is not an algorithm
— it is the absence of a plain, callable, dependency-light function that takes strings and
returns ranked GO terms with enough provenance to be trusted and reproduced.

## Solution

Add an **OntologyMapping** capability domain to KBUtilLib, surfaced as `KBUtilLib().ontology_mapping`
and registered on the capability registry so it is reachable from the library, the CLI, the MCP
server and the HTTP API with no additional transport work.

Its headline capability is:

```
ontology_mapping.function_to_go(descriptions, ids=None, top_k=10, routes=None)
    -> list of per-description results, each carrying ranked GO candidates
```

Each candidate carries its GO id, name, namespace, a score, a rank, and — the part that makes the
result trustworthy — **the route that produced it**. Two routes ship:

1. **The identifier route (`ec2go`), tried first and deterministic.** If a description carries a
   parseable EC number, the curated GO Consortium `ec2go` mapping is consulted and the resulting
   GO terms are returned as exact, curated mappings. There is no similarity scoring anywhere in
   this path; an EC either maps or it does not. This reuses the discipline (and the EC-parsing
   regex shape) already established by `EcRoleResolver`.

2. **The lexical route (BM25), for everything the first route does not resolve.** A fit-free
   BM25 ranker over character 3–5 grams scores the description against each GO term's `name`
   (weight 3.0), `synonyms` (2.0) and `definition` (1.0) — the Type 1 method, ported into
   KBUtilLib as ordinary library code with no runner, no stage directory, and no provenance
   contract.

When both routes return candidates, **route precedence** applies by default: curated `ec2go`
terms occupy the top ranks and the lexical route fills the remainder of `top_k`. Reciprocal rank
fusion (the Type 4 algorithm, `k=60`) is implemented and available as an opt-in
`combine="rrf"` mode for experimentation, but it is **not** the default — see Q4, where the
external scan supplied a concrete counterexample in which equal-weight RRF at `k=60` demotes a
correct leading result below an incorrect one.

The GO corpus that the lexical route ranks against is built by extending KBUtilLib's existing
`build_go_description_map.py` into a sibling `build_go_corpus.py`, which emits a JSONL corpus
carrying name, synonyms, definition, namespace, obsolescence and `is_a` parents, pinned to a
named GO release with a SHA-256 of the source OBO. Like its sibling, its output is deployment
data and is never committed. Every result the capability returns names the GO release it was
produced against.

Everything degrades the way the rest of this package degrades: a missing corpus disables the
lexical route and says so, rather than silently returning nothing.

## User Stories

1. As a **microbial genome annotator**, I want to pass a list of gene product descriptions and
   get ranked GO terms for each, so that I can attach GO annotations to a genome whose annotator
   did not emit them.
2. As an **annotator**, I want a description carrying an EC number to resolve through the curated
   `ec2go` mapping rather than by text similarity, so that the answer is a curated biological
   assertion and not a string coincidence.
3. As a **curator reviewing output**, I want each returned candidate to say which route produced
   it, so that I can trust an `ec2go` hit differently from a BM25 hit.
4. As a **curator**, I want to see the score and the rank-1-to-rank-2 margin, so that I can tell
   a confident call from a coin-flip.
5. As a **reproducibility-minded scientist**, I want every result to name the GO release version
   and the corpus SHA-256 it was produced against, so that a result can be regenerated or
   explained a year from now.
6. As a **scientist**, I want obsolete GO terms excluded from results by default and merged terms
   (`alt_id`) resolved to their current accession, so that I never attach a dead term to a genome.
7. As a **pipeline author**, I want `function_to_go` available over the HTTP API and MCP without
   writing transport code, so that a notebook on a remote kernel or an agent can call it.
8. As a **pipeline author**, I want to call it on thousands of descriptions in one call and have
   the corpus loaded once, not once per description.
9. As an **operator deploying to a new host**, I want one documented command that builds the GO
   corpus from a `go-basic.obo` the host already holds, consistent with how the other staged
   ontology dictionaries are built.
10. As an **operator**, I want a missing corpus to be a loud, named, reportable degradation with
    the expected path in the message, not a silent empty result.
11. As an **operator**, I want a corpus file that exists but parses to zero entries to be a hard
    error, because a silently empty corpus is indistinguishable from "nothing matched".
12. As a **caller who only trusts curated mappings**, I want to request the identifier route
    alone and get nothing rather than a guess when the EC is absent.
13. As a **KBUtilLib developer**, I want this to be a capability on the existing registry and
    facade, not a parallel subsystem with its own conventions.
14. As a **KBUtilLib developer**, I want the GO terms returned in the existing `Term` shape so
    that they compose with the annotation records the rest of the genome domain already produces.
15. As a **maintainer**, I want the capability to carry no new mandatory third-party dependency,
    so that it cannot become unrunnable the way `OntomapUtils` did.
16. As a **scientist assessing whether this is good enough**, I want an evaluation entry point
    that scores the mapper against a held-out gold set and reports standard metrics, so that
    quality is measured rather than asserted.
17. As an **operator**, I want to point the evaluation at the existing frozen gold set in the
    research program without copying it into this repo.
18. As a **caller**, I want deterministic output — the same inputs and the same corpus produce
    byte-identical results, with ties broken by ascending GO ID.
19. As an **annotator**, I want a curated `ec2go` term to rank above a text-derived guess by
    construction, not by luck of a fusion score.
20. As an **annotator**, I want "hypothetical protein" and its relatives to return an explicit
    abstention rather than the least-bad GO term, so that noise is not presented as an answer.
21. As a **pipeline author whose annotator already emits structured identifiers**, I want to pass
    EC / InterPro / Pfam / GO identifiers alongside the text, so that the better evidence is used
    rather than re-derived from a lossy product string.
22. As a **pipeline author**, I want a GO term my annotator already asserted to be preserved at
    the top of the result rather than re-derived.
23. As a **scientist**, I want a match on a `broad` synonym to count for less than a match on an
    `exact` one, so that the ranker does not manufacture false specificity.
24. As a **scientist reading an evaluation**, I want coverage and abstention rates reported
    alongside accuracy, so that a high score over a small answered fraction is visible as such.

## Implementation Decisions

### Module layout

A new subpackage `kbutillib/domains/genome/ontology_mapping/`:

| Module | Contents |
|---|---|
| `__init__.py` | Re-exports `OntologyMappingUtils`, `OntologyMappingUtilsImpl`, `GoCandidate`, `FunctionToGoResult` |
| `corpus.py` | `GoCorpus` — loads and caches the staged GO corpus JSONL; owns release/SHA provenance |
| `ec2go.py` | `Ec2GoResolver` — parses the curated `ec2go` file; literal EC keying only |
| `lexical.py` | `BM25CharNgramRanker` — the fit-free Type 1 port |
| `fusion.py` | `reciprocal_rank_fusion()` — the Type 4 algorithm, standalone |
| `ontology_mapping_utils.py` | `OntologyMappingUtils` (inherits `SharedEnvUtils`) + `OntologyMappingUtilsImpl` (composition) |

It sits under `domains/genome/` rather than at top level because GO mapping of functional
descriptions is genome-annotation work and the substrate it reuses all lives there. The
`*Utils` + `*UtilsImpl` pair and the lazy `__getattr__` export in `domains/genome/__init__.py`
follow the pattern every sibling module already uses.

### Facade and capability registration

`KBUtilLib.ontology_mapping` is added to `toolkit.py` as a lazy property constructing
`OntologyMappingUtilsImpl(self.env)`, matching the existing `ontomap` property exactly. The
public methods on the `*Impl` class are decorated:

```
@capability(
    domain="ontology_mapping",
    summary="Map free-text functional descriptions to ranked Gene Ontology terms.",
    tags=("genome", "ontology", "annotation", "readonly"),
    visibility="public",
)
```

`domain="ontology_mapping"` yields the registered name `ontology_mapping.function_to_go`, which
is what the CLI, MCP and `POST /v1/tools/{name}` will expose with no further work.

Three capabilities are registered: `function_to_go` (the headline), `go_corpus_status`
(returns release, SHA-256, term count, and which routes are available — the operator's
degradation report), and `evaluate_function_to_go` (the harness).

### The GO corpus artifact

`build_go_corpus.py` is added beside `build_go_description_map.py` in
`domains/genome/annotation/`, and deliberately does **not** modify it — that builder has a live
consumer (`OntologyDictionary`) and a shipped two-column contract, and widening it would be a
breaking change to a done PRD's output.

The new builder parses the same `go-basic.obo` and emits one JSON object per line:

```
{"go_id", "name", "synonyms", "definition", "namespace",
 "obsolete", "alt_ids", "replaced_by", "consider",
 "is_a_parents", "depth", "height", "leaf"}
```

This schema is copied from the research program's `go-candidates.jsonl`, which is a proven shape
for exactly this task, **with one deliberate departure: `synonyms` carries scope.** Each entry is
`{"text": str, "scope": "exact"|"broad"|"narrow"|"related"}` rather than a bare string. GO's OBO
`synonym:` lines declare a scope, and the external scan flagged that collapsing them loses real
information: a `broad` synonym matching a query is weak evidence for a *specific* term, and
treating it as equivalent to an `exact` synonym manufactures false specificity. The research
program's corpus flattens this; we do not. The file header carries `# release_version`, `# source_sha256` and
`# term_count` as `#`-prefixed comment lines, matching the convention the existing builders use
and which `_parse_two_column_tsv` already skips.

**Obsolete terms are retained in the corpus but flagged**, which differs from
`build_go_description_map.py`'s choice to skip them. The reason: that builder serves
`describe()`, where a stale description is worse than `None`; this corpus serves `alt_id`
resolution, which needs the obsolete records in order to redirect a merged accession to its
replacement. Obsolete terms are excluded from *ranking* by default.

Output path is read from config key `ontology_mapping.go_corpus_path`, mirroring
`ontology_dictionary.*_path`. The output is added to `.gitignore` as `go_corpus*.jsonl`
alongside the existing `go_description_map*.tsv` entry, and the module docstring carries the same
"DO NOT COMMIT THE OUTPUT" warning the sibling builder carries.

### Degradation contract (inherited deliberately, not invented)

`GoCorpus` reproduces `OntologyDictionary`'s two-failure-mode split exactly, because it is the
right contract and because a second, different contract in the same package would be a defect:

- **Corpus path unset or unreadable** → not an error. The lexical route is disabled, a warning
  naming the expected path is logged once, `"lexical"` is recorded in a `degraded_routes` set,
  and `go_corpus_status()` reports it. `function_to_go` still serves the `ec2go` route.
- **Corpus file readable but parses to zero terms** → `RuntimeError` naming the path and the
  expected layout. A silently empty corpus would make every description return no candidates,
  which is indistinguishable from a genuine no-match.

The same split applies to the `ec2go` file independently, so a host can have either route, both,
or neither, and `go_corpus_status()` always says which.

### Route 1 — `Ec2GoResolver`

Parses the GO Consortium `ec2go` file, whose documented line form is
`EC:1.1.1.1 > GO:alcohol dehydrogenase (NAD+) activity ; GO:0004022`. The resolver keys on the
**literal** normalised EC string and performs no prefix expansion: a query for `1.1.1.-` matches
only rows carrying that exact wildcard, never `1.1.1.1`. This is stated as a hard rule because
`EcRoleResolver`'s docstring already had to state it, for the same reason — prefix expansion
silently turns a specific enzyme into a whole subclass.

EC numbers are extracted from a description with a regex of the same shape as
`_EC_IN_ROLE_RE`/`_BARE_EC_RE` in `ec_role_resolver.py`, covering both the parenthetical form
(`"... (EC 3.1.6.12)"`) and a bare trailing EC. A description may carry several; all are
resolved and their GO terms unioned.

Candidates from this route get `route="ec2go"`, `score=1.0`, and ranks assigned by ascending GO
ID. The score is a constant and is **not** comparable to a BM25 score — the fusion step below is
rank-based precisely so that this never has to be reconciled.

### Route 2 — `BM25CharNgramRanker`

The Type 1 port: character 3–5 grams, BM25 term saturation, fields weighted `name` 3.0,
`synonyms` 2.0, `definition` 1.0. Implemented against the loaded `GoCorpus` with no third-party
dependency.

**Synonym scope modulates the synonym weight**, which is this design's one deliberate deviation
from Type 1. The base synonym weight of 2.0 is multiplied by scope: `exact` 1.0, `narrow` 0.75,
`related` 0.5, `broad` 0.25. A `broad` synonym is by definition a *less* specific name than the
term it hangs off, so a match on one is weaker evidence for that specific term than a match on
its exact synonym. The multipliers are a judgement, not a measurement — they are recorded as
module constants so the evaluation harness can sweep them, and Q12 says so plainly.

**The constants are now known** — the confront round read the reference implementation this
design had not, and they were verified directly at
`/scratch/vsetlur/.../type_1/field_aware_bm25_char/source.py`:

```
FIELD_WEIGHTS = {"name": 3.0, "synonyms": 2.0, "definition": 1.0}   # source.py:31
NGRAM_SIZES   = (3, 4, 5)                                            # source.py:32
K1, B         = 1.2, 0.75                                            # source.py:33
```

Normalisation is `" ".join(TOKEN.findall(text.lower()))`, then the string is padded with one
leading and one trailing space before n-grams are taken (`_text_ngrams`, source.py:47–50) — the
padding is load-bearing, because it is what lets a 3-gram capture a word boundary. The
implementer must still read that file to match `TOKEN`'s exact pattern and the IDF variant;
these constants remove the guesswork, not the reading.

Obsolete terms are excluded from the ranked universe by default. Ties are broken by ascending
GO ID, which makes the output deterministic.

The corpus index is built once per `GoCorpus` instance and cached, so a 5,000-description call
pays the index cost once.

### Combining routes

`function_to_go` takes `combine` with two values.

**`combine="precedence"` (the default).** Curated `ec2go` terms take the top ranks, ordered by
ascending GO ID; the lexical route fills the remainder of `top_k`, with any GO ID already
contributed by `ec2go` suppressed from the lexical block rather than repeated. This encodes the
same judgement as the route ordering itself: a curated GO Consortium assertion outranks a
character-n-gram similarity, always, and not merely usually.

**`combine="rrf"` (opt-in).** The Type 4 arithmetic — `sum over routes of weight / (60 + rank)`,
equal weights of 1.0, GO-ID-ascending ties. Retained because it is the research program's method
and the evaluation harness must be able to measure it, but not the default; Q4 records why.

RRF is the right *fusion* algorithm if one fuses at all, because the routes' scores are not on a
common scale and never will be — one is a constant and the other is a BM25 sum. The objection is
to fusing by default, not to the algorithm.

**Under either mode, with only one route returning candidates the result is that route's own
ranking.** For `rrf` this is because RRF is a monotone transform of a single ranking; the code
must not imply that fusion added information.

Each returned candidate retains `per_route_rank` and `per_route_score` so a curator can see what
each route thought — the same transparency Type 4's `fusion_evidence` provides, carried in the
return value rather than written to a file.

### Abstention

**Abstention suppresses the LEXICAL route only — it never discards identifier evidence.**
Confront round 1 caught the contradiction: a gene whose product is `"hypothetical protein"` but
whose record carries `{"go": ["GO:0004022"]}` cannot both abstain and pass the provided term
through, and the common real case is a placeholder product beside a perfectly good EC. The rule
is therefore: placeholder text disables lexical scoring; if provided GO or `ec2go` yields
anything, return it with `abstained: false`. `abstained: true` with
`abstention_reason: "uninformative_description"` is returned only when placeholder text has
**no** resolved identifier evidence behind it.

A description that is uninformative *and* unsupported is answered with **no candidates and an
explicit `abstained: true` plus a reason**, not with the least-bad GO term. The default list
covers the standard placeholder products — `hypothetical protein`, `putative uncharacterized
protein`, `uncharacterized protein`, `unknown function`, `conserved hypothetical protein`,
`DUF####`-only products — matched case-insensitively after whitespace normalisation, and
extensible by the caller.

This exists because a lexical ranker will always return *something*: `"hypothetical protein"`
has character 3–5 grams that overlap plenty of GO definitions, and the top hit would be noise
wearing a rank. The external scan states the principle directly — "'Hypothetical protein' is a
legitimate abstention, not a request to find the least bad GO term."

### Structured identifiers alongside text

`function_to_go` accepts an optional `records` argument parallel to `descriptions`, each entry a
dict that may carry `ec`, `interpro`, `pfam` and `go` identifiers already known for that gene.
When present these feed the identifier route directly rather than being re-extracted from text,
which is strictly better evidence: the upstream annotator's structured output is the thing the
product string was lossily derived from.

Pre-existing `go` identifiers are passed through as candidates with `route="provided"` and
`score=1.0`, ahead of everything else — if BAKTA already asserted a GO term, this capability's
job is to preserve it, not to re-derive it.

`interpro` and `pfam` are **accepted and recorded but not resolved in v1** (see Out of Scope);
they are in the signature now so that adding `interpro2go` later is not a breaking change.

### Return shape

```
{
  "query_id": str,
  "description": str,
  "go_release": str,          # e.g. "2026-07-26"
  "corpus_sha256": str,
  "routes_used": ["ec2go", "lexical"],
  "candidates": [
    {"rank": int, "go_id": str, "name": str, "namespace": str,
     "score": float, "route": str, "per_route_rank": {...},
     "per_route_score": {...}, "top1_margin": float | None}
  ]
}
```

`top1_margin` is rank-1 score minus rank-2 score, and **is `None` whenever the top two scores do
not share a scale** — which is the common case under `precedence`, where an `ec2go` constant of
1.0 can sit above a BM25 score of 20.0 and a naive subtraction yields `-19.0` for the answer the
design deliberately preferred. Confront round 1 caught this: the draft called scores
incomparable in one section and `top1_margin` the confidence signal in another. The margin is
computed only when both of the top two candidates come from the **same** route under
`precedence`, or are both non-prefix candidates under `rrf`. `OntomapUtils` established the
field; it did not have two scales to reconcile.

A `to_terms()` helper converts candidates to the existing `Term` dataclass
(`namespace="GO"`, `id=go_id`, `value=name`, `evidence={score, route, rank}`) so results compose
with `AnnotationRecord`.

**No qualitative confidence band is emitted.** `OntomapUtils._build_confidence_band` hardcodes
0.9/0.7 thresholds with a docstring conceding they are "intentionally conservative; adjust as
ontomap benchmarks are published" — thresholds that were never calibrated. Repeating that
pattern on an unmeasured ranker would invent authority this design does not have.

### Evaluation harness

`evaluate_function_to_go(gold_path, queries_path, limit=None)` scores the mapper against a gold
set in the research program's JSONL shape and reports, per route and fused: top-1 exact-ID
accuracy, MRR, recall@k for k in (1, 5, 10, 20), and an `is_a`-closure hierarchy precision/recall
computed from the corpus's `is_a_parents`. It reads gold read-only and writes nothing outside a
caller-named output path.

It exists because this PRD makes no accuracy claim and the only honest way to close that gap is
to ship the instrument that measures it. Hierarchy metrics use **asserted `is_a` only**, never
`part_of`, matching the research program's stated relation policy.

### Canonical GO resolution, shared by every route

All routes canonicalise a GO ID **before** de-duplication and before it reaches a result:

- resolve an `alt_id` to its owning term;
- follow a **unique** `replaced_by` chain to an active term;
- **never** treat `consider` as an automatic replacement — it is a curator's suggestion, and
  promoting it would fabricate a biological assertion;
- omit any ID that is unresolved, ambiguous (several `replaced_by`), or unknown to the corpus,
  recording why in a per-result `identifier_diagnostics` list rather than dropping it silently;
- a cycle, or two active terms claiming the same `alt_id`, is a **corpus error** and raises.

**No route emits an obsolete term.** Q6's "excluded from ranking" was too narrow — a curated
`ec2go` row or a caller-provided GO ID can name a dead term, and passing it through would attach
one to a genome. The obsolete records exist in the corpus to power this redirection, not to be
returned.

**The one exception is the degraded-corpus case**, below: without a corpus there is nothing to
canonicalise against, and the result says so rather than pretending.

### Provenance fields, and what they say when a route is degraded

The routes degrade independently, so a result must be able to describe a partial world honestly.
`go_release` and `corpus_sha256` are **nullable**, and `corpus_sha256` means the SHA-256 of the
**source OBO**; a separate `corpus_artifact_sha256` identifies the built JSONL. `ec2go_release`
and `ec2go_sha256` are carried alongside, because an identifier result is not reproducible from
the OBO hash — the mapping file can change independently.

An `ec2go`-only candidate (no corpus staged) uses the label from the mapping file, sets
`namespace: null`, and carries `canonicalization_status: "unverified"`. A provided GO ID with no
corpus uses `name = go_id`, `namespace: null`, and the same status. With a corpus present, corpus
metadata is authoritative and any disagreement between the two files' release strings is reported
rather than silently resolved.

### Ordering, de-duplication and route attribution

Under **both** combine modes, canonical **provided** GO IDs form an ascending-ID **prefix**: they
count toward `top_k`, and they do **not** participate in RRF. Including them in a fused sum could
demote a term the annotator already asserted, which is exactly what User Story 22 forbids.
`ec2go` and `lexical` then fill the remaining slots under the chosen mode, with prefix duplicates
suppressed.

`route` is `"provided"` for prefix terms, `"ec2go"` or `"lexical"` for single-source terms, and
**`"combined"`** for a term more than one route produced — the draft's singular `route` field
could not describe a mixed-evidence candidate without lying about it. Full `per_route_rank` and
`per_route_score` evidence is retained for every contributing route either way. Where `ec2go` and
`lexical` overlap under `precedence`, the candidate takes the `ec2go` score.

**`top_k` applies only at the end** — after canonicalisation, full-route ranking, combination and
de-duplication. RRF consumes the **complete** lexical ranking and all resolved `ec2go` IDs, not a
pre-truncated top-`k` slice; truncating first would discard the lower-rank corroboration that
fusion exists to use, and would under-fill the response whenever curated terms overlap the
lexical prefix. An implementation may compute an equivalent result without materialising every
intermediate row.

### Dependencies

**None added.** Everything is standard library over staged data files, which is a hard
constraint of this design rather than an incidental property — it is the specific thing that
makes this capability unable to fail the way `OntomapUtils` failed.

## Testing Decisions

**What makes a good test here.** The behaviour that matters is externally observable: given a
description and a corpus, which GO terms come back, in what order, with what route attribution,
and what happens when the data is missing or malformed. Tests assert on returned values and on
raised errors — never on how the index is built or how n-grams are counted.

**The acceptance gate is a production-wiring check, and this is the single most important
testing decision in the PRD.** `kbdl-ontology-descriptions-v1` shipped seven reviewed tasks that
were inert in production because every test injected the dictionary at the constructor seam. So
the final task's criterion is: construct `KBUtilLib()` with **no arguments**, set only the config
keys an operator would set, call `k.ontology_mapping.function_to_go([...])` through the facade,
and assert real GO terms come back. Any test that passes a corpus object into a constructor is
explicitly *not* sufficient to satisfy that gate.

**Modules tested, and against what:**

- `GoCorpus` — a small hand-written fixture corpus (~20 real GO terms, committed, a few hundred
  bytes). Both degradation modes get a test: unset path → lexical route disabled, `degraded_routes`
  populated, `ec2go` still works; file present but zero parseable terms → `RuntimeError` naming
  the path. This mirrors `tests/annotators/test_ontology_dictionary.py`, which is the prior art
  to follow.
- `Ec2GoResolver` — a fixture `ec2go` file. The load-bearing test is **negative**: a query for
  `1.1.1.-` must not match `1.1.1.1`, and vice versa. Also: multiple ECs in one description,
  the parenthetical and bare forms, and a malformed EC returning `[]` rather than raising.
- `BM25CharNgramRanker` — determinism (same input twice, identical output, including tie order),
  field weighting (a term matching in `name` outranks an otherwise-equal term matching only in
  `definition`), and obsolete-term exclusion.
- `reciprocal_rank_fusion` — a pure function with hand-computable expectations: known ranks in,
  known fused order out; the single-route case returns the input order unchanged; ties break by
  ascending GO ID. **One test encodes the external scan's counterexample directly** — ranks
  (1, 100) versus (2, 1) at k=60 — asserting that RRF *does* put the second term first. It is a
  characterisation test: it pins the behaviour that justifies not defaulting to this mode, so
  that anyone who later makes `rrf` the default has to confront it.
- Combine modes — under `precedence` an `ec2go` candidate outranks every lexical candidate
  regardless of BM25 score, a GO id produced by both routes appears once, and switching to
  `rrf` on the same inputs can produce a different order (asserted, not incidental).
- Abstention — a description on the default placeholder list returns zero candidates with
  `abstained=True` and a reason; a normal description returns `abstained=False`; the list is
  caller-extensible; matching is case- and whitespace-insensitive.
- Structured records — a `records` entry carrying `ec` feeds the identifier route without the EC
  appearing in the text; an entry carrying `go` is passed through at the top with
  `route="provided"`; `interpro`/`pfam` are accepted and recorded without raising.
- Synonym scope — a term matching on an `exact` synonym outranks an otherwise-comparable term
  matching only on a `broad` synonym.
- `build_go_corpus.py` — a fixture `.obo` snippet covering a normal term, an obsolete term with
  `replaced_by`, a term with `alt_id`, and a `[Typedef]` stanza that must be skipped. Follows
  `tests/annotators/test_build_go_description_map.py`.
- Capability registration — `get_registry()` after `register_all()` contains
  `ontology_mapping.function_to_go` with the expected domain and visibility.

**Prior art to follow in this repo:** `tests/annotators/test_ontology_dictionary.py` (degradation
modes), `tests/annotators/test_build_go_description_map.py` (OBO fixture parsing), and
`tests/external/test_ontomap_utils.py` for the translation-layer shape — while explicitly **not**
following its all-mocked approach for the acceptance gate.

**Criteria are regression-framed throughout.** KBUtilLib carries pre-existing test failures, so
no task may require a green suite; each requires that the tests it adds pass and that nothing
passing on the base commit fails on the branch.

## Open Questions and Judgement Calls

Every item below is **already decided and reflected in the PRD body and taskplan**. The PRD is
buildable as it stands. To change a decision, edit its `DECIDED:` line and the Review Mode pass
will propagate it.

Entries are ordered by **blast radius descending** — the cost of reversing a decision after the
work ships, not the size of the change now. Q-numbers are stable identifiers assigned when each
entry was written, so they are **not sequential in this ordering**; cite them by number, and
read them in the order given.

### Q1. Should `function_to_go` be built on the `ontomap` package, reusing `OntomapUtils`? -- DECIDED: No. Neither depend on it nor extend it; build a route-based mapper with zero new dependencies.
**Blast radius:** IRREVERSIBLE
**Why:** `ontomap` imports in none of the 20 venvs on this host, including all three that import
`kbutillib` — measured 2026-10-18 with a scrubbed environment. PRD `gaa-ontomap-council-mapping`
was archived 2026-07-12 precisely because its remaining work was "blocked on Vibhav non-public
assets (SSO LoRA + target_sapbert.npz) + h100 env", and nothing has changed. `OntomapUtils` also
maps to ModelSEED *reactions*, not GO terms, so even if it ran it would answer a different
question. Choosing it would make the capability unrunnable on the only machine that would run it.
**If you disagree:** the entire taskplan changes shape. Tasks 2–5 (`ec2go.py`, `lexical.py`,
`fusion.py`, `corpus.py`) collapse into a wrapper around `ontomap.Pipeline`, Task 1's
"no new dependency" criterion is struck, and the build acquires a blocking prerequisite — getting
`ontomap` plus its SSO LoRA and `target_sapbert.npz` assets installed into `~/venvs/gaa` on
poplar, which is Vibhav's to grant and is not in this repo's control.
**Confidence:** high — this is a measurement, not a judgement. What would raise it further:
nothing. What would *change* it: `ontomap` becoming pip-installable with public weights.

### Q2. Should the lexical route port Type 1's BM25, or call the Type 4 fusion code we were pointed at? -- DECIDED: Port Type 1's BM25 and the ~20-line RRF kernel from Type 4; use neither package as a runtime dependency.
**Blast radius:** IRREVERSIBLE
**Why:** Type 4 computes no scores. It fuses two already-COMPLETE parent runs after verifying a
provenance contract (frozen input hashes, `support_sha256` evaluator identity, matching profile,
test-split membership, exact GO-ID membership per ranking), and `run()` raises unless given a
manifest of ≤1,000 pre-selected *frozen test* queries. Its own README says full production is
"deliberately blocked until the external-order streaming join and recovery/checkpoint contract is
implemented". A `function_to_go` built on it could not answer any query outside the frozen
corpus — which is every query a real caller has. The scoring logic lives in Type 1, which is
fit-free and therefore portable.
**If you disagree:** `function_to_go` becomes a benchmark-replay function. It would need
`parent_runs`, a `query_manifest`, a `stage_dir` and a study data root as arguments, could only
accept query IDs rather than descriptions, and User Stories 1, 2, 7 and 8 all become
unsatisfiable. The capability would also inherit a hard dependency on `/scratch/vsetlur/`, which
this fleet treats as ephemeral.
**Confidence:** high — read from `source.py` (450 lines) and `manifests/component-contract.json`
in full.

### Q3. Should the identifier (`ec2go`) route exist at all, or should this be purely a text ranker? -- DECIDED: Ship it, and try it FIRST.
**Blast radius:** MEDIUM
**Why:** The input this capability will actually receive is annotation-pipeline output, and
BAKTA/KOfamScan/RAST descriptions routinely embed an EC number. For those, the GO Consortium's
curated `ec2go` is a manually maintained biological assertion; character-n-gram BM25 is a string
coincidence. KBUtilLib already made this exact call once — `EcRoleResolver` exists specifically
to avoid "a fuzzy text join against an external enzyme-description vocabulary". Text ranking is
the right tool for the residue, not the first resort.
**If you disagree:** drop Task 2 and the `routes` parameter, and `function_to_go` becomes
single-route. Fusion (Task 4) then has nothing to fuse and should be dropped too, reducing the
plan from seven tasks to five. Accuracy on EC-bearing descriptions would be expected to fall,
though by how much is unmeasured.
**Confidence:** medium — the reasoning is sound and the mapping file's existence is confirmed,
but its **coverage over our actual description corpus has not been measured**. The evaluation
harness in Task 6 reports per-route metrics specifically so this can be checked rather than
believed.

### Q4. Should equal-weight RRF be the default way of combining the two routes? -- DECIDED: No. Default to ROUTE PRECEDENCE (curated `ec2go` above lexical); keep RRF as an opt-in `combine="rrf"` mode.
**Blast radius:** MEDIUM
**Why:** This reverses the draft's first answer, and the external scan is why. RRF carries **no
monotonic improvement guarantee**, and the scan supplied a concrete counterexample at `k=60`:
with a relevant term ranked 1 and 100 by two systems (`1/61 + 1/160 ≈ 0.02264`) and an irrelevant
term ranked 2 and 1 (`1/62 + 1/61 ≈ 0.03252`), fusion *reverses* the correct leading result of
the better system. That is exactly our shape — `ec2go` returns a short, high-precision list and
BM25 returns a long, noisy one — so the failure is not hypothetical here, it is the expected
case. Cormack et al. fixed `k=60` during pilot work and reported exceptions where fusion did not
win; Bruch et al. (2023) found tuned convex score combination beating RRF. Against that, Q3's
whole argument for having an identifier route is that a curated GO Consortium assertion is
*better evidence* than a string match — and defaulting to a fusion that can demote it would
discard the reason the route exists.
**If you disagree:** flip the `combine` default to `"rrf"`. Nothing else in the build changes —
both modes ship, both are tested, and the evaluation harness reports both — which is precisely
why this is MEDIUM rather than IRREVERSIBLE. The decision is a default, not an architecture.
**Confidence:** high on *not defaulting to fusion* (the counterexample is arithmetic, and it
matches our route asymmetry). Low on whether precedence is the best non-fusion rule — a tuned
weighted combination, or precedence only above a BM25 score floor, may well beat it. Task 6
measures all of this; what would raise confidence is that measurement.

### Q5. Extend `build_go_description_map.py`, or add a new builder? -- DECIDED: Add a sibling `build_go_corpus.py`; leave the existing builder untouched.
**Blast radius:** MEDIUM
**Why:** The existing builder has a live consumer (`OntologyDictionary`) and a shipped
two-column `accession<TAB>description` contract with its own tests and a `.gitignore` entry.
Widening it to carry synonyms, definitions and parents would change that contract and break a
done PRD's output format. Two builders over the same OBO is a small, legible duplication; one
builder with two output modes is a breaking change to a shipped interface.
**If you disagree:** Task 1 becomes a modification of `build_go_description_map.py` with a
`--format` flag, `tests/annotators/test_build_go_description_map.py` must be extended rather than
paralleled, and `OntologyDictionary._parse_two_column_tsv`'s zero-entry guard must be checked
against the new format.
**Confidence:** high.

### Q7. A new top-level library, or a capability domain inside `domains/genome/`? -- DECIDED: A capability domain inside `domains/genome/ontology_mapping/`, exposed as `KBUtilLib().ontology_mapping`.
**Blast radius:** MEDIUM
**Why:** Chris, in session 2026-10-18: "if there are already capabilities, build on those." The
`@capability` registry plus `register_all()` plus the four transports already *are* the API-library
machinery; `interfaces/api/app.py` serves any registered capability at `POST /v1/tools/{name}`
with no new code. Placing it under `domains/genome/` puts it beside every piece of substrate it
reuses — `OntologyDictionary`, `build_go_description_map.py`, `Term`, the annotators that produce
its input.
**If you disagree:** the subpackage moves to `kbutillib/ontology_mapping/` as a peer of
`domains/`, which breaks the invariant that domain code lives under `domains/` and would be the
only such exception. The facade property, capability names and transport exposure are unaffected
either way.
**Confidence:** high — this is a direct instruction, and the alternative was named and rejected
in-session.

### Q13. Should the input be text only, or also accept structured identifiers from the annotator? -- DECIDED: Accept both -- an optional `records` argument carrying `ec`/`interpro`/`pfam`/`go` per description. Resolve `ec` and pass through `go`; accept but do not resolve `interpro`/`pfam` in v1.
**Blast radius:** MEDIUM
**Why:** The external scan's central recommendation: "retain the simple text-only callable, but
offer an adjacent structured-record interface accepting source GO/EC/KO/Pfam/InterPro IDs",
because "a product string alone discards useful evidence" — the structured output is what the
product string was lossily derived from. A BAKTA record that already carries `GO:0004022` should
have that preserved, not re-derived by BM25. Accepting the fields now, even unresolved, means
adding `interpro2go` later is additive rather than a signature change.
**If you disagree:** drop `records` and the capability is text-only; callers with structured
output must flatten it into a string and let us parse it back out, which is strictly lossier.
**Confidence:** high on accepting the argument. Medium on deferring `interpro2go`/`pfam2go`
resolution — note the scan's warning that `pfam2go` is *generated from* `interpro2go`, so the two
are not independent evidence and must not be counted twice when they are implemented.

### Q6. Keep obsolete GO terms in the corpus, when the existing builder drops them? -- DECIDED: Keep them, flagged `obsolete: true`, and exclude them from ranking by default.
**Blast radius:** LOW
**Why:** The two artifacts serve different jobs. `describe()` wants a stale description gone
(`None` beats wrong). This corpus must resolve a merged `alt_id` to its current accession, and
that redirect is only possible if the obsolete record is present to carry `replaced_by`. Keeping
and flagging serves both; dropping serves only one.
**If you disagree:** `alt_id` resolution (User Story 6) cannot be implemented from the corpus
alone and would need a second lookup table, adding a file to build, stage and keep in sync.
**Confidence:** high.

### Q8. Emit a qualitative confidence band, as `OntomapUtils` does? -- DECIDED: No. Emit raw score, rank, and `top1_margin` only.
**Blast radius:** LOW
**Why:** `OntomapUtils._build_confidence_band` hardcodes 0.9/0.7 with a docstring admitting the
thresholds are provisional pending benchmarks that never arrived. Attaching "high confidence" to
an output from a ranker whose accuracy has never been measured would manufacture authority. The
margin is a real, computable signal; a band is an unearned interpretation of it.
**If you disagree:** add `_build_confidence_band` to the result builder — about ten lines —
*after* Task 6 produces calibration data that sets the thresholds on evidence.
**Confidence:** high.

### Q9. Should Type 2 (the PU pairwise sparse-logistic ranker) be a third route? -- DECIDED: No. Out of scope for v1.
**Blast radius:** LOW
**Why:** It needs a trained model artifact that does not exist in a KBUtilLib-reachable location,
and its own README records a 500-query gate spending ~577 s in fit trials plus ~246 s inference
at ~4.14 GiB peak RSS, with full-corpus extrapolation "many hours to more than a day" and the
full run explicitly "not launched". It would also pull in the SciPy/CuPy stack this design
deliberately avoids. The route interface is designed so a third route can be added later without
reshaping the API.
**If you disagree:** v1 grows a heavyweight optional dependency group, a model-artifact staging
step, and a latency profile that rules out interactive use.
**Confidence:** high.

### Q10. Where does the GO corpus live on a deployed host? -- DECIDED: Outside git, at a config-keyed path (`ontology_mapping.go_corpus_path`), built per deployment; `.gitignore` gains `go_corpus*.jsonl`.
**Blast radius:** LOW
**Why:** Exactly the pattern `kbdl-ontology-descriptions-v1` established for the four staged
dictionaries, for the same reason stated in `build_go_description_map.py`'s docstring: the output
is derived third-party bulk ontology data, not this repo's content. Committing a ~50 MB
regenerable derivative would also be the kind of repo bloat the fleet guidelines forbid.
**If you disagree:** the artifact must be versioned, hosted and fetched, which is a distribution
problem this PRD does not solve.
**Confidence:** high.

### Q11. Should GO synonym scope (exact/broad/narrow/related) be retained and weighted? -- DECIDED: Yes. Carry scope in the corpus and multiply the synonym weight by exact 1.0 / narrow 0.75 / related 0.5 / broad 0.25.
**Blast radius:** LOW
**Why:** GO's OBO format declares a scope on every `synonym:` line, and the external scan flagged
that treating them as equivalent "can create false specificity" — a `broad` synonym is a *less*
specific name than the term carrying it, so matching it is weaker evidence for that term than
matching its exact synonym. The research program's `go-candidates.jsonl` flattens synonyms to
bare strings and so cannot express this; since we are regenerating the corpus anyway, carrying
scope costs one field.
**If you disagree:** set all four multipliers to 1.0. **Note, corrected after confront round 1:
that does NOT reproduce Type 1 exactly, and the draft's claim that it would was wrong.** Type 1's
`_candidate_fields` concatenates every synonym into a single `"synonyms"` field
(`" ".join(...)`, source.py:53) and saturates it once; four scope-separated fields saturate
independently, so a term with many synonyms of one scope scores differently even at equal
weights. A strict Type 1 port requires a single concatenated synonym field, which is a separate
switch from the multipliers. The corpus field stays either way.
**Confidence:** high that scope should be *retained*; **low on the specific multipliers**, which
are a judgement with no measurement behind them. They are module constants precisely so Task 6
can sweep them.

### Q12. What should the capability do with an uninformative description like "hypothetical protein"? -- DECIDED: Abstain explicitly -- return zero candidates with `abstained: true` and a reason.
**Blast radius:** LOW
**Why:** A lexical ranker always returns something. "Hypothetical protein" shares character 3–5
grams with a great many GO definitions, so the top hit would be noise with a rank number attached
— and in a genome annotation context these placeholder products are *common*, so the failure
would be high-volume. The external scan puts it plainly: this "is a legitimate abstention, not a
request to find the least bad GO term."
**If you disagree:** drop the abstention list and these descriptions return ranked noise that a
caller must learn to filter. The `abstained` field would then always be false and could be
removed from the return shape.
**Confidence:** high on abstaining; medium on the default list's coverage, which is drawn from
the common Prokka/BAKTA placeholder products and is caller-extensible rather than exhaustive.

### Q14. Should KO→GO be a route, given KO-bearing KOfamScan output is common here? -- DECIDED: No, not in v1 -- on licensing grounds, not technical ones.
**Blast radius:** LOW
**Why:** The GO Consortium's `external2go` directory carries `kegg_reaction2go`, which maps KEGG
**reaction** IDs and is *not* a KO→GO service. The actual KO→GO crosswalk is KEGG-hosted
(`ko2go.xl`), and the scan was explicit that KEGG's terms are separate from GO's CC BY 4.0, that
its API restricts access to academic users for academic use, and that "accessibility ... alone
does not establish redistribution permission". Shipping a capability that depends on staging a
KEGG file is a licensing question, and licensing questions are not mine to settle.
**If you disagree:** it is a small addition — a third resolver with the same shape as
`Ec2GoResolver` — but it needs a licensing answer first, and that answer is Chris's.
**Confidence:** high on deferring; the technical work is trivial and the blocker is not technical.

### Q15. What I could not decide and did not guess
Three external facts remain unreconciled. None blocks the build; each is named where it bites.

- **Type 1's exact BM25 constants and tokenisation.** `type_1/field_aware_bm25_char/source.py`
  was not read — only its README. The n-gram range (3–5) and field weights (3.0/2.0/1.0) come
  from that README; `k1`, `b`, the IDF variant, and case/punctuation handling are unknown. Task 3
  instructs the implementer to read the file first and gives a labelled fallback. **I did not
  invent constants and present them as ported.**
- **`ec2go` coverage over our real description corpus.** The file exists and is curated; what
  fraction of BAKTA/KOfamScan/Prokka descriptions carry a parseable EC, and what fraction of
  those ECs appear in `ec2go`, is unmeasured. Q3's blast radius rests on this being non-trivial.
  Task 6's per-route reporting is what will settle it.
- **Whether any of this is accurate enough to be useful.** No accuracy figure for any route is
  known to this design, and the research program's own Type 4 benchmark is self-described as "an
  operational benchmark, not a final production claim". The external scan likewise reports no
  measured head-to-head for text ranking versus identifier crosswalk on data like ours, and
  states its own recommendation is "an engineering recommendation supported by GOA practice, not
  a measured head-to-head accuracy result on this project's data". This PRD ships a measuring
  instrument rather than a quality claim. If a go/no-go threshold is wanted before merge, it has
  to be set by Chris, because nothing I can read supplies one.
- **Whether route precedence beats a tuned combination.** Q4 establishes that equal-weight RRF
  should not be the default and gives the arithmetic. It does **not** establish that precedence
  is the best alternative — a weighted score combination, or precedence gated above a BM25 floor,
  may do better, and the scan notes tuned convex combination outperforming RRF in the retrieval
  literature. I picked the option that is defensible without measurement; I did not pick the
  measured best, because there is no measurement.
- **The synonym-scope multipliers.** `exact` 1.0 / `narrow` 0.75 / `related` 0.5 / `broad` 0.25
  are invented. That scope *should* modulate weight is well supported; these four numbers are
  not. They are module constants so the harness can sweep them, and Q11 says so.
- **A KEGG licensing answer.** Q14 defers KO→GO because I cannot settle whether we may stage and
  use `ko2go.xl`. That is a question for Chris, not a technical gap.

## Gotchas and Unintuitive Consequences

**A passing test suite will not tell you this capability works in production, and there is a
dated precedent.** `kbdl-ontology-descriptions-v1` merged seven reviewed tasks whose measured
production effect was a reaction-yield delta of exactly zero, because every guard injected its
dependency at the constructor seam while the real adapter passed nothing. The same shape is
available here: a `GoCorpus` handed to a constructor in a fixture proves the ranker ranks, and
nothing about whether `KBUtilLib()` finds a corpus on a real host. This is why Task 7's criterion
forbids injection.

**The two routes' scores are not comparable, and the fused score is not a probability.** `ec2go`
returns a constant 1.0; BM25 returns an unbounded sum. After RRF both become `1/(60+rank)` sums
in roughly 0.008–0.033 — numbers that look like low confidence and mean nothing of the kind. A
caller who thresholds on the fused score will be thresholding on rank position. `top1_margin` is
the signal to use; the absolute score is not.

**Anyone who switches `combine` to `"rrf"` re-acquires the bug precedence was chosen to avoid.**
The arithmetic, from the external scan: a relevant term ranked 1 and 100 across two routes scores
`1/61 + 1/160 ≈ 0.02264`; an irrelevant term ranked 2 and 1 scores `1/62 + 1/61 ≈ 0.03252`. The
wrong term wins. With `ec2go` returning a short high-precision list and BM25 a long noisy one,
this is the expected shape rather than a corner case. The mode exists so the harness can measure
it — it should not be switched on because it sounds more sophisticated.

**Scores mean different things under the two combine modes, and the field name does not change.**
Under `precedence`, a candidate's `score` is its own route's score (1.0 for `ec2go`, a BM25 sum
for lexical) — so the list is not monotone in `score`, because an `ec2go` 1.0 sits above a BM25
value that may be numerically larger. Under `rrf` every score is an RRF sum in roughly
0.008–0.033. A caller that sorts by `score` rather than trusting `rank` will get a different
order under `precedence`, and a caller that thresholds on `score` will be comparing
incomparable quantities across modes. **Trust `rank`; treat `score` as per-route evidence.**

**Single-route output under `rrf` passes through fusion and is unchanged by it.** With one route
RRF is monotone in that route's rank, so `routes_used` will say `["lexical"]` and the scores will
be RRF-shaped even though no fusion happened.

**Abstention is silent coverage loss unless someone counts it.** Placeholder products are common
in real annotation output, so a run over a genome can legitimately abstain on a large fraction of
its input and return a tidy, entirely correct, mostly empty result. `abstained` is on every
record and the evaluation harness reports coverage for exactly this reason — a quality number
computed only over non-abstained queries looks better than the system is.

**`pfam2go` and `interpro2go` are not independent evidence.** The scan verified that `pfam2go`'s
own header says it is generated from InterPro-supplied InterPro2GO data. Whoever implements those
routes later must not treat agreement between them as corroboration; it is one source counted
twice.

**The corpus is pinned to a GO release; the annotator that produced the input is pinned to its
own.** A BAKTA run from six months ago and a corpus built today can disagree about whether a term
exists. Results carry `go_release` so the mismatch is visible, but nothing reconciles it, and
nothing warns when the input's vintage differs from the corpus's.

**Excluding obsolete terms from ranking silently shrinks the candidate universe between corpus
builds.** A term obsoleted in a new GO release stops being rankable the moment the corpus is
rebuilt, so the same description can return a different top-1 after a corpus refresh with no code
change. `go_corpus_status()` reporting release and SHA-256 is what makes that diagnosable.

**`describe_or_accession()`'s colon rule applies to anything this capability renders.** GO
accessions embed a colon (`GO:0006260`), so the existing convention is to split on the first
`": "` (colon-space), not the first colon. Any new rendering code that splits on `":"` will
truncate every GO accession to `"GO"`.

**The vsetlur data tree is partly unreadable to us already.** `candidate/` and the pipeline
sources are world-readable, but every `results/` subdirectory returns permission denied. Task 6's
evaluation reads `gold/` and `queries/`, which are readable today — but this is another user's
`/scratch`, outside our control, and the task must fail loudly rather than silently skipping
evaluation if that changes.

**Building the corpus is an operator step that nothing performs automatically.** On a host with
no corpus the capability comes up degraded-but-working (`ec2go` only), which is a legible state
that is also easy to leave in place indefinitely without noticing the lexical route never ran.

## Sources Consulted

**Source pipeline (`/scratch/vsetlur/function-ontology-mapping-program/`)**
- `studies/01_function_to_go/pipelines/type_4/full_specificity_rrf_fusion/source.py` — read in
  full (450 lines). Key symbols: `check()`, `run()`, `smoke()`, `_fuse()`, `_parents()`,
  `_load_parent()`, `_spool_selected_rankings()`, `_benchmark_query_ids()`; constants `RRF_K=60`,
  `DEFAULT_WEIGHTS`, `MAX_BENCHMARK_QUERIES=1000`.
- Same package: `README.md`, `config.yaml`, `environment.yaml`, `manifests/component-contract.json`
  — the parent-compatibility contract and the production-blocked statement.
- `pipelines/type_4/README.md`, `pipelines/type_1/README.md`,
  `pipelines/type_1/field_aware_bm25_char/README.md` (BM25 method and field weights),
  `pipelines/type_2/README.md`, `pipelines/type_2/pu_pairwise_sparse_logistic/README.md`
  (the latency and RSS figures quoted in Q9).
- `studies/01_function_to_go/data/README.md` and one full record from
  `data/candidate/go-candidates.jsonl` — the corpus schema this PRD copies.
- `pipelines/support/` — file listing only (`go_graph.py`, `go_metrics.py`, `go_profiles.py`,
  `go_validate.py`, …); **not read**, noted as the shared-support layer the pipelines import.
- **Not read:** `type_1/field_aware_bm25_char/source.py` (the gap named in Q11),
  `data/reports/`, `data/gold/`, and all `results/` trees (permission denied).

**KBUtilLib (`~/projects/KBUtilLib`, `main` at 6de52e9)**
- `src/kbutillib/domains/genome/annotation/ontology_dictionary.py` — read in full; `describe()`,
  `_parse_two_column_tsv()`, `degraded_namespaces`, `_CONFIG_KEYS`, and the
  missing-file-vs-zero-entry contract this PRD copies.
- `src/kbutillib/domains/genome/annotation/build_go_description_map.py` — read; `_iter_go_entries()`,
  `GoEntry`, the obsolete-skipping choice, and the DO-NOT-COMMIT docstring.
- `src/kbutillib/domains/genome/annotation/annotator_utils.py` — `Term`, `AnnotationRecord`,
  `AnnotationResult`, `describe_or_accession()` and its colon rule.
- `src/kbutillib/domains/modeling/ec_role_resolver.py` — the no-fuzzy-matching principle,
  `_EC_IN_ROLE_RE`, `_BARE_EC_RE`.
- `src/kbutillib/domains/genome/ontomap_utils.py` — read in full; `map_functions()`,
  `_translate_map_result()`, `_build_confidence_band()` (rejected in Q8), `OntomapUtilsImpl`.
- `src/kbutillib/core/README.md` — `CapabilityRegistry`, `@capability`, `register_all()`,
  `BaseUtils`, `SharedEnvUtils`.
- `src/kbutillib/interfaces/api/README.md` — `POST /v1/tools/{name}`, bearer auth.
- `src/kbutillib/toolkit.py` — the `ontomap` lazy property, copied as the pattern for
  `ontology_mapping`.
- `src/kbutillib/domains/genome/__init__.py`, `domains/genome/annotation/__init__.py`,
  `domains/genome/README.md` — export conventions.
- `tests/external/test_ontomap_utils.py` — read; the all-mocked pattern this PRD declines for its
  acceptance gate.
- `tests/annotators/` listing — `test_ontology_dictionary.py` and
  `test_build_go_description_map.py` named as the prior art to follow.
- `.gitignore` line 80–92 — the staged-dictionary exclusion block.

**Prior PRDs (AIAssistant `state/prd_registry.json`)**
- `gaa-ontomap-council-mapping` — status `archived`; the retirement note quoted in Q1.
- `kbdl-ontology-descriptions-v1` — status `done`; the "INERT IN PRODUCTION" note quoted in the
  Testing Decisions and the Gotchas.
- Registry scanned for all 40 KBUtilLib-touching PRDs; the rest are unrelated (modeling,
  notebooks, council, clearinghouse) and were not opened.

**Measured on poplar, 2026-10-18**
- `ontomap` import across all 20 `~/venvs/*`, scrubbed env — absent everywhere; `kbutillib`
  present in `gaa`, `gaa-model`, `kbdl`, `lp-solver`.
- `stat` device:inode on `~/projects/KBUtilLib` vs `/scratch/fliu/hub_home/chenry/H100Projects/KBUtilLib`
  — `64517:5167606134` both; one clone, not two.
- `~/.maestro/config.yaml` — `accepted_backends: [claude, codex]`, `researcher: codex`.

**External**
- [ec2go](https://current.geneontology.org/ontology/external2go/ec2go),
  [interpro2go](https://current.geneontology.org/ontology/external2go/interpro2go),
  [the external2go directory](https://current.geneontology.org/ontology/external2go/),
  [GO_REF:0000002](https://geneontology.org/GO_REF/0000002.html) — curated transitive mappings;
  the scan verified `ec2go`'s header references GO `releases/2026-07-26`. **Format not parsed by
  this design, coverage over our descriptions not measured.**
- [GO use-and-license](https://www.geneontology.org/page/use-and-license) (CC BY 4.0 for data),
  [GO synonym documentation](https://www.geneontology.org/docs/ontology-documentation/) (the
  exact/broad/narrow/related scopes behind Q11),
  [KEGG licensing FAQ](https://kegg.net/en/licensing_faq.html) and
  [KEGG API restrictions](https://www.genome.jp/kegg/rest/) (behind Q14).
- [Cormack, Clarke & Büttcher, SIGIR 2009](https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf)
  — the original RRF result, the pilot origin of k=60, and the reported exceptions;
  [Bruch, Gai & Ingber, 2023](https://arxiv.org/abs/2210.11934) — tuned convex combination
  beating RRF. Both behind Q4.
- [text2term](https://github.com/rsgoncalves/text2term) (MIT) — the closest reusable prior art,
  assessed and not adopted; [goatools](https://github.com/tanghaibao/goatools),
  [pronto](https://github.com/althonos/pronto), [bm25s](https://github.com/xhluca/bm25s),
  [ranx](https://github.com/AmenRa/ranx) — assessed in the scan's library section, none adopted
  in v1 per Q1.
- The cross-family scan itself:
  [research/external-prior-art-scan.md](research/external-prior-art-scan.md), Maestro
  `task-ebbb3fd6`, codex backend, h100, committed at `5287a8b7`.

## Out of Scope

- **Type 2 (PU pairwise sparse logistic) as a route.** See Q9. The route interface accommodates a
  third route later.
- **Any dependency on, or revival of, the `ontomap` package.** See Q1. `OntomapUtils` is left
  exactly as it is — not deleted, not deprecated, not modified.
- **Sequence-based GO prediction.** This maps text to GO. Anything taking an amino-acid sequence
  (BLAST, HMM, PLM embeddings) is a different capability.
- **Writing GO annotations back to a KBase genome object or the clearinghouse lake.** This
  returns data; persisting it belongs to the caller.
- **Reproducing the research program's provenance machinery** — COMPLETE markers, manifests,
  `parent_set_hash`, frozen-input hash verification, stage publication. That exists to make a
  benchmark auditable; a library call does not need it.
- **Tuning RRF weights, or calibrating confidence thresholds.** Both need the measurements Task 6
  produces. See Q4 and Q8.
- **`interpro2go` and `pfam2go` resolution.** The identifiers are accepted and recorded in v1 but
  not resolved (Q13). When implemented they must not be treated as independent evidence —
  `pfam2go` is generated from `interpro2go`.
- **KO→GO.** Deferred on licensing, not difficulty: the GO Consortium's `kegg_reaction2go` maps
  KEGG *reactions*, and the real KO crosswalk is KEGG-hosted under separate terms. See Q14; the
  licensing call is Chris's.
- **Adopting `bm25s`, `ranx`, `goatools`, `pronto` or `text2term`.** All are live options the
  external scan assessed, and all add a dependency, which Q1 makes load-bearing to avoid. Revisit
  if Task 6 shows the hand-written route measuring badly — the scan's library section is written
  to be the input to that decision.
- **Embedding or cross-encoder routes.** A biomedical bi-encoder is a plausible improvement the
  scan could not show beats character-BM25 on this task. It would also pull in a model dependency
  of exactly the kind that made `OntomapUtils` unrunnable.
- **Hosting or distributing the built GO corpus.** Per-deployment build only. See Q10.
- **`part_of` closure.** Asserted `is_a` only, matching the research program's relation policy.
- **A go/no-go accuracy threshold.** Not settable from anything readable; Chris's to set if wanted.

## Further Notes

**On the brief as given.** The topic asked for "a new API library called OntologyMapping ...
integrate the code here: `.../pipelines/type_4` to create a `function_to_go` function". Two
things moved during research, both flagged rather than silently absorbed. The `type_4` path
contains a fusion stage that computes no scores and cannot handle a query outside its frozen
corpus, so "integrate type_4" became "port Type 1's scorer plus Type 4's RRF kernel" (Q2). And
"new API library" became "new capability domain on the existing registry" on Chris's in-session
steer (Q7). The name `OntologyMapping` is preserved as the capability domain
(`ontology_mapping.*`) and the facade property.

**On deployment.** KBUtilLib on poplar installs from `~/projects/KBUtilLib`, which is the same
inode as `/scratch/fliu/hub_home/chenry/H100Projects/KBUtilLib` — one clone reached by two path
aliases, verified by `stat`. Four venvs import it (`gaa`, `gaa-model`, `kbdl`, `lp-solver`), and
it is an editable install, so a `git pull` refreshes the code those venvs see. Any long-lived
service among them holds the old code in memory until restarted — the deploy signal is process
age against commit date, not tree state.

**On the operator step.** The corpus build is a per-deployment command, and nothing runs it
automatically. A host without it serves `ec2go` only and reports the degradation through
`go_corpus_status()` — correct behaviour that is also easy to leave in place unnoticed. Whoever
deploys this should run the build and check `go_corpus_status()` in the same sitting.

## Acceptance Criteria

Folded from confront round 1 (Maestro `task-16d54bf7`, codex backend; stall report at
[research/confront-round-1-stall-report.md](research/confront-round-1-stall-report.md)). All 12
binding stalls were accepted — each named a real gap, and three caught outright contradictions
in the draft. These are the externally checkable conditions the build must satisfy.

1. `fullprompt.md` is authoritative wherever it and `humanprompt.md` disagree; `precedence` is
   the default combine mode and `rrf` is opt-in, in both documents.
2. Corpus graph attributes are computed over **active terms and asserted `is_a` edges only**:
   `depth` is the minimum distance from any parentless active root (root = 0), `height` the
   maximum distance to any active leaf (leaf = 0), `leaf` means no active children. Obsolete
   terms carry `depth: null`, `height: null`, `leaf: false`.
3. The corpus builder rejects cycles and dangling active-parent references rather than emitting
   them.
4. Every route canonicalises GO IDs before de-duplication: `alt_id` resolved to its owner, a
   **unique** `replaced_by` chain followed to an active term, `consider` never used as an
   automatic replacement, and unresolved / ambiguous / unknown IDs omitted with a
   `identifier_diagnostics` entry. A cycle or conflicting alias ownership raises a corpus error.
5. **No route emits an obsolete GO term** — not the lexical ranker, not `ec2go`, not a
   caller-provided ID.
6. `ec2go` is read from `ontology_mapping.ec2go_path`, read-only, never fetched at inference
   time, and its release and SHA-256 are reported separately from the corpus's.
7. `go_release` and `corpus_sha256` are null when no corpus is staged; `corpus_sha256` is the
   source OBO's hash and `corpus_artifact_sha256` the built JSONL's. An `ec2go`-only candidate
   has `namespace: null` and `canonicalization_status: "unverified"`. A release-string
   disagreement between the two staged files is reported, not silently resolved.
8. The lexical ranker preserves Type 1's `name`, `definition`, document-frequency and
   total-length calculations; synonyms are split into four scope-specific fields, each saturated
   independently under the shared document-length normalisation, then multiplied by 2.0 × its
   scope multiplier. A gram counts once per document for IDF across all fields. This differs
   from flat Type 1 **even when every multiplier is 1.0**, and the docstring says so.
9. The signature is
   `function_to_go(descriptions, ids=None, top_k=10, routes=None, *, combine='precedence', records=None, abstention_terms=None)`.
   `records` is length-aligned; its four supported values are `list[str]`; absent keys mean empty
   lists; unknown keys or wrong types raise `ValueError`. A non-empty structured `ec` list
   replaces inline extraction, otherwise inline ECs are extracted. `routes=None` enables `ec2go`
   and `lexical`, `routes=[]` enables neither, an unknown route name raises `ValueError`, and
   **provided GO is processed independently of `routes`**. Mismatched lengths and a
   non-positive or non-integer `top_k` are rejected before any data is loaded.
10. Placeholder detection disables **lexical scoring only**. `abstained: true` with
    `abstention_reason: "uninformative_description"` is returned only when placeholder text
    yields no identifier evidence; if provided GO or `ec2go` resolves, candidates are returned
    with `abstained: false`. Matching is on the normalised whole string; DUF-only means the
    entire normalised text matches `duf[0-9]+`. Empty and whitespace-only descriptions follow the
    same policy.
11. Canonical provided GO IDs form an ascending-ID prefix under both combine modes, count toward
    `top_k`, and do not participate in RRF. `route` is `"provided"`, `"ec2go"`, `"lexical"`, or
    `"combined"` for a multi-source term; per-route evidence is retained for every contributor;
    an `ec2go`/`lexical` overlap takes the `ec2go` score under `precedence`.
12. `top_k` is applied only after canonicalisation, full-route ranking, combination and
    de-duplication. RRF consumes the complete active lexical ranking and all resolved `ec2go`
    IDs; `precedence` scans the lexical ranking until filled or exhausted.
13. The result carries `abstained: bool`, `abstention_reason: str|null`,
    `input_identifiers: {ec, interpro, pfam, go: list[str]}` and
    `identifier_diagnostics: list[dict]` in addition to the documented fields.
14. `top1_margin` is **null unless the top two candidates share a scale** — the same sole route
    under `precedence`, or both non-prefix candidates under `rrf`. No document claims a
    cross-route margin is a confidence signal.
15. `evaluate_function_to_go` gains `split_path` and `evaluation_split='test'`. Queries join gold
    on **`entity_id`**, not `query_id`; query text is `raw_text`; the cohort is selected from
    `split-membership.tsv` rather than from `source_split_metadata`.
16. Gold positives are the de-duplicated `canonical_go_id` values whose resolution status is
    active and `in_full_active` is true, excluding unknown-or-unresolved rows. Queries with no
    eligible gold are excluded and counted. `limit` is applied in query-file order **after**
    filtering and split selection. Duplicate `query_id`s or missing split membership raise
    `ValueError`.
17. The harness reports `ec2go`-only, `lexical`-only, `precedence` and `rrf` over the **identical
    eligible cohort**, as macro means of per-query top-1 membership accuracy, full-ranking
    reciprocal rank, recall@1/5/10/20, and self-inclusive asserted-`is_a` closure precision and
    recall of top-1 against all eligible gold IDs. Recall@20 is computed from the full ranking,
    not from a 10-candidate default. Empty predictions contribute zero.
18. Coverage is non-empty predictions over eligible queries and `abstention_rate` is explicit
    abstentions over eligible queries; every accuracy figure is reported both over answered
    queries and over all eligible queries. A degraded route is reported as null with a reason,
    never as 0.0, and a *requested* but unavailable route fails the evaluation.
19. `evaluate_function_to_go` takes `output_path=None`, returns a JSON-compatible report, and
    writes that same report only when `output_path` is supplied — never into the research
    program's tree. The report records cohort counts, input hashes, mapper provenance and its own
    metric definitions.
20. The no-injection acceptance test builds its corpus by invoking `build_go_corpus.py` as a
    module and configures paths through an operator-style config, not constructor arguments; a
    temporary config location is acceptable and a real operator's config is never edited.
