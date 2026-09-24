# koros-arc-store-v1 -- the shared KOROS project/arc and run-record layer

**Owning repo:** KBUtilLib.
**Module:** `kbutillib.koros_arc_store` (plus `kbutillib.koros_arc_store_testing`).
**Consumers:** `kind-annotation-results-explorer-v1` and
`kind-model-analysis-explorer-v1`, both of which are reconciled against this
document.

---

## Research Report

### Important findings

**This PRD is an EXTRACTION, not a new design.** Its technical content was
researched, written, adversarially attacked and reviewed inside
`kind-annotation-results-explorer-v1` over 2026-09-23, and separately reviewed
by the session that wrote `kind-model-analysis-explorer-v1`. The research
finding that matters most is therefore about provenance: **almost nothing here
is new, and the parts that ARE new are named explicitly in the Revision Log so
a reader can tell them apart.**

The inherited findings that bear on the design:

1. **The join key does not exist anywhere today.** `grep` over
   `KBUtilLib/src/kbutillib` and `KBDLJobRunningPrototype/src` for `koros`,
   `arc_id`, `KOROS_RUNS` or an `arcs/` path returns **zero hits**. Neither
   producer records a project or an arc on anything. This module is the first
   thing in our stack that knows what an arc is. (Re-verified for this PRD on
   2026-09-24: still zero hits, and `kbutillib.koros_arc_store` does not
   exist -- so neither app has been dispatched and the extraction is clean.)

2. **The live runs tree, counted programmatically over every
   `PROVENANCE.json`:** 9 project directories, 8 of which have an `arcs/`
   subdirectory, 26 arcs in total. **`inputs` is an empty list and
   `tool_versions` an empty dict on ALL 26.** One project has no `arcs/`
   directory at all. The empty case is the common case; the parser must treat
   it as valid, and enumeration must not raise on the arcs-less project.

3. **`KIND_KOROS_RUNS` does not exist.** Both app PRDs originally took that
   name from the `cw-kind` skill, which is stale after the KING->KIND rename.
   `grep -rn KIND_KOROS_RUNS ~/king-stack` returns zero files. The real chain
   is `--runs-root` -> `$KOROS_HOME/runs` -> `$KING_KOROS_RUNS` -> raise, with
   `KOROS_HOME` the **workspace root** and runs in its `runs/` subdirectory.
   Verified in `koros_start.py:27`, `research_init.py:98-102`,
   `koros/INSTALL.md:88-92`, `king_backend/config.py:24-25`. The skill defect
   is filed as dev 1262.

4. **Blob arithmetic is what forces the two-tier schema.** A 4,617-gene genome
   across up to 15 annotation methods is roughly 70,000 gene-level facts **for
   one genome**. As rows that is tens of millions across a corpus; as a blob it
   is one row. Every query the drill-down issues is scoped to a single subject,
   so the blob costs nothing at read time.

5. **`trace.jsonl` is not a record store.** Across all arcs the event mix is
   `magnitude_guard` 1824, `shell` 799, `file_write` 605, `gate` 50,
   `artifact` 31, `decision` 28, `milestone` 26, `tool_call` 17. Sampled rows
   carry empty `tool` and `run_id` fields. It is a trace, and reading analyses
   out of it would be inference dressed as provenance.

6. **The arcs are git repositories** (`runs/genome-clearinghouse/arcs/
   mag-integration-arc` contains `.git`). This is why nothing is written into
   the arc directory: an earlier design put a sidecar index there and it would
   have dirtied the scientist's working tree.

7. **Arc directories are Dropbox-synced on this fleet**, which is why the
   earlier sidecar design also needed per-writer sharding: Dropbox writes a
   conflicted copy rather than merging concurrent appends, reports nothing, and
   an advisory lock does not cross a sync boundary. Moving the store to a
   per-user database in the home directory **removes that hazard entirely**,
   which is the single biggest reason the storage decision landed where it did.

8. **The external prior-art scan was run, and it was run against this
   question.** `kind-model-analysis-explorer-v1`'s Maestro scan
   (`task-f379e8ac`, codex, h100) asked directly whether RO-Crate or an
   MLflow-style index is the standard envelope for a run-record log, and
   whether an append-only JSONL store is sound. Its accepted recommendations
   (last-writer-wins on `record_id`; move to SQLite past a few thousand
   records) are in this design. RO-Crate was declined with a stated reason: it
   describes a crate, not an append-many log of runs.

### Links to full research output

- [Model-analysis external scan](../kind-model-analysis-explorer-v1/research/external-scan.md)
  -- the run-record envelope and JSONL-failure-mode material.
- [Annotation external scan](../../../../GenomeAnnotationAggregator/agent-io/prds/kind-annotation-results-explorer-v1/research/external-scan.md)
  -- annotation-comparison metrics; bears on this module only through the
  `trust_tier` and consistency-column decisions.

### Quality report

**What was searched for THIS document:** the two parent PRD bundles in full
(`fullprompt.md`, `data.json`, `taskplan.json` for both); the live git state of
KBUtilLib, GenomeAnnotationAggregator and KBDLJobRunningPrototype; the
KBUtilLib source tree, to confirm the module does not already exist and to
match its layout conventions; the PRD registry, to establish that both parents
are at `ready` with `auto_conduct` unset.

**What was NOT searched, and why:**

- **No new external literature or prior-art scan was dispatched.** The two
  parent PRDs already ran one each (`task-f379e8ac` and `task-a8bc0867`), and
  the model-analysis scan covered exactly this module's external question -- the
  run-record envelope. A third scan over extracted text would restate them. This
  is a deliberate decision with named prior work, not a skipped step; if it is
  wrong, the cost is one Maestro round.
- **The live arkinlab lakehouse catalog was never queried.** It is not this
  module's concern -- nothing here touches the lakehouse -- but it is inherited
  as an open item by both consumers and is restated in their documents.
- **The CAC's own `<fill:>` placeholders** in the id-normalization helper and
  the contract-version gate mechanics are unresolved upstream. `p0c` implements
  the documented behaviour and will be revisited when FJ's or genKnown's
  `interop.py` can be read.

**Confidence.** High on everything inherited, because it has been through a
confront round and a cross-PRD conformance review that found three real defects
in this exact material and fixed all three. Medium on the packaging decisions
this document adds -- the PRD boundary, the ordering contract, and the reference
test double -- because those are new and have not been attacked.

---

## Revision Log

**Round 1 -- 2026-09-24 -- Chris's review. Three edits, one of which changed the
identity model.** He answered all three items round 0 left in the
could-not-decide list. Two closed cleanly: the KBDL object store IS reachable
from the pod and always will be, and "official app in KOROS" means the apps
show up **in** KIND rather than being contributed upstream. The third did not
close -- it reversed a design property. **Re-runs must NOT collapse to one row;
they must be dated, groupable, and deletable.** That made the single derived
`record_id` untenable, so identity is now SPLIT: `analysis_id` (round 0's
derivation, unchanged, now the grouping key) and `record_id` (per run, from
`analysis_id` + a required `run_uid`). `delete_record` is added, the DDL gains
two columns and an index, and the retry guarantee survives intact. See Q10.

Also repaired two pieces of markdown-formatter corruption introduced on save,
one of which had deleted the quotation marks from a quoted CAC line and thereby
inverted its meaning. The document was rebuilt from `draft_commit` and the three
edits reapplied, which is why this round's diff is readable.

**Round 0 -- 2026-09-24 -- extraction.** Created by lifting the `p0a` / `p0b` /
`p0c` tasks and their supporting design text out of
`kind-annotation-results-explorer-v1`, at Chris's decision to extract rather
than merge the two app PRDs. The three task prompts are carried **verbatim**
apart from the changes listed below, because they were already self-contained,
already confronted, and already conformance-reviewed.

Everything this extraction CHANGED or ADDED, so it can be told apart from the
inherited material:

1. **ADDED `p0d-contract-fake`** -- a reference test double and a contract test
   suite, shipped from the owning module. Previously
   `kind-model-analysis-explorer-v1`'s `p3-app-skeleton` instructed its builder
   to write its own fake `KorosArcStore`. Two independently-written fakes over
   one interface is the drift this module exists to prevent. See Q4.
2. **CHANGED the test-fixture path** from
   `tests/fixtures/kind_annotation_explorer/` to
   `tests/fixtures/koros_arc_store/`. The old path named a consumer app inside a
   module whose own prompt says in capitals that the schema names are
   deliberately domain-neutral. It was an artifact of the module having lived
   inside that app's PRD. See Q5.
3. **CHANGED the BACKGROUND block** of all three inherited prompts to cite this
   PRD rather than the annotation PRD, and to say that BOTH apps are downstream
   consumers rather than one being the owner.
4. **ADDED an explicit non-goal:** this PRD ships no app, no route, no manifest
   and no KIND registration. Those stay with the two consumers.

---

## Problem Statement

A scientist runs work inside KOROS arcs: a genome build here, three annotation
methods there, a model reconstruction and an FBA in the arc next door. The work
happens; the record of it does not survive in any form a second tool can read.
`PROVENANCE.json` describes the arc, `trace.jsonl` describes keystrokes, and
neither describes "this analysis, of this subject, by this tool, at this trust
level, producing these artifacts."

Two apps are being built to show that record back. Both need the same three
things -- a way to enumerate projects and arcs, a place to write and read
analysis records, and the CAC conformance helpers that let them be registered
KIND apps at all. Neither app's science is shared with the other's; this layer
is the whole of what they have in common.

Built inside either app, it is a phase of a document about something else,
reviewed as part of something else, and invisible to the other app's taskplan.

## Solution

One domain-agnostic module in KBUtilLib, built once, dispatched on its own,
with both apps depending on it and neither creating it.

### Runs-tree access (read-only)

Resolve the KOROS runs root through a fixed chain, enumerate projects and arcs,
and parse `PROVENANCE.json` into a typed record that preserves lineage
(`role`, `leg_of`, `parent`). Nothing here writes to the runs tree, and nothing
here imports `king_backend` or any of the five KING/KOROS repos -- the
consume-only interlock is structural, not a convention.

### The run database

A per-user local SQLite database in the user's home directory, on by default on
pods and configurable elsewhere, holding analysis records from every producer.
**Two tiers:** navigable dimensions as SQL columns, so the dashboards sort,
filter and aggregate without opening anything; gene-level and reaction-level
detail as a JSON blob, one per subject, opened by exactly one call.

### The `KorosArcStore` interface

The single API both apps use. Its shape was negotiated between the two design
sessions and is adopted here -- **the names are an agreement with a live PRD,
not a preference.** Chris's round-1 review added to it, additively: re-runs of
one analysis are distinct dated rows grouped by a shared `analysis_id`, and a
run can be deleted. See Q10.

### The CAC helpers

Id normalisation (hyphen canonical, underscore derived mechanically) and
contract-version gating (hard-fail on a major mismatch, warn on a compatible
minor difference), with `contract_version` an integer.

### The contract test double -- the new piece

A reference fake, shipped from this module, that both consumer apps import for
their own tests, plus a contract suite that the real implementation and the
fake must both pass. This is what makes extraction pay for itself: the
interface stops being a promise in two documents and becomes an executable one.

### The honesty spine

Three rules run through the whole module and each of them is a rule about what
the code may NOT do:

- **No API raises a trust tier.** A summarising record carries the **floor** --
  the least-trusted contributing tier. Adding higher-tier findings never raises
  it. There is no method that produces a higher tier than its inputs.
- **Any tier other than `verified` requires provenance.** A write that omits the
  bridge kind and its metric is rejected, not defaulted.
- **An unknown record kind is stored, flagged and counted -- never refused, and
  never silently dropped.** Two apps write into one database; one app's new kind
  must not be able to stall the other's reader.
- **A re-run never overwrites its predecessor.** History is kept by default and
  removed only when somebody asks for it by `record_id`. The store forgets on
  instruction, never as a side effect of somebody doing their work twice.

## User Stories

1. As the annotation explorer, I want to list every project and arc the user has
   worked in, so that I can render the top level of my drill-down without
   knowing anything about the filesystem.
2. As the model-analysis explorer, I want the same list from the same call, so
   that the two apps cannot disagree about what a project is.
3. As a KBDL client writing a job result, I want to record an analysis against a
   project and arc, so that the work becomes findable later.
4. As that same client running outside any arc, I want to record the analysis
   anyway with a null project and arc, so that unattributed work is still
   captured rather than lost.
5. As a producer RETRYING a failed run, I want the second write to replace the
   first rather than duplicate it, so that one run is one row however many
   attempts it took.
5a. As a scientist re-running an analysis a month later, I want the new run kept
   ALONGSIDE the old one and dated, so that I can see what changed instead of
   losing the earlier result.
5b. As an app, I want every run of one logical analysis to share a stable
   `analysis_id`, so that I can group re-runs in the interface without
   re-deriving what "the same analysis" means.
5c. As a scientist who has decided an old run is not useful, I want to delete it
   from the database and have its detail blob go with it, so that curation is a
   supported operation rather than something I do with sqlite3.
6. As two different producers computing the same logical analysis, I want to
   derive the same `record_id` from a shared helper, so that "replace, never
   duplicate" is a real guarantee rather than a hope.
7. As a dashboard, I want to sort and filter thousands of runs without opening a
   single detail blob, so that the list view stays fast as the corpus grows.
8. As a gene view, I want to open exactly one blob -- the one for the subject I
   am showing -- so that the cost of detail is bounded by what is on screen.
9. As a reader, I want to ask for "homology or better" and get an answer, so that
   I can filter on trust without parsing payloads.
10. As a reviewer of a summary figure, I want its tier to be the floor of its
    inputs, so that a summary can never look better-evidenced than its weakest
    contributing call.
11. As an operator on a machine with no KOROS workspace, I want the runs-root
    resolution to raise a clear error, so that I see a misconfiguration rather
    than an empty dashboard that reads as "no work has been done".
12. As a scientist whose pipeline run is finishing, I want a database that cannot
    be written to be logged and skipped, so that a dashboard gap never fails my
    science run.
13. As an app author writing tests, I want a fake store shipped with the module,
    so that my tests bind to the real contract and cannot drift from it.
14. As an app author, I want the module never to interpret my `payload`, so that
    I can add a record kind without touching this module or the other app.
15. As a KIND app, I want id normalisation and contract-version gating from one
    place, so that two apps cannot conform to the CAC in two different ways.
16. As a maintainer, I want this module to import nothing from the five
    KING/KOROS repos, so that the consume-only interlock is checkable by grep.

## Implementation Decisions

### Module layout

`kbutillib/koros_arc_store.py` holds the runs-tree access, the store and the
records. `kbutillib/koros_arc_store_testing.py` holds the reference fake, kept
in a separate module so that importing the store never drags a test double into
production. The CAC helpers live alongside as `kbutillib/cac_conformance.py`.
All names are **domain-neutral at every level** -- nothing in this module, its
schema, its fixtures or its tests may be named `annotation`, `genome`, `model`
or `flux`. Two apps share it and either name would be wrong half the time.

### Runs-root resolution -- the single most likely thing to get wrong

```
1. an explicit --runs-root argument
2. $KOROS_HOME/runs
3. $KING_KOROS_RUNS
4. RAISE
```

`$KOROS_HOME` is the **workspace root**, not the runs directory; runs live in
its `runs/` subdirectory. `KIND_KOROS_RUNS` does not exist and must appear
nowhere in the code. **There is no hardcoded fallback.**
`~/Dropbox/Science/runs` is merely one laptop's `$KOROS_HOME/runs`; hardcoding
it would render the top level of both apps empty on any other machine, and an
empty dashboard reads as "no work has been done" rather than as a
misconfiguration. Raising is more honest than guessing at another setup's
directory.

### Enumeration

Projects are the immediate subdirectories of the runs root; arcs are the
subdirectories of `<project>/arcs/`. **A project directory with no `arcs/`
subdirectory enumerates as a project with zero arcs and must never raise** --
one of the nine live projects is exactly that. `PROVENANCE.json` parses into a
typed record carrying the verified live fields: `run_id`, `run_name`,
`created_at`, `created_by`, `init_provenance`, `fair_inputs_ref`,
`offlimits_list_ref`, `non_overlap_statement`, `frame_novelty`, `inputs`,
`tool_versions`, `compute_targets`, `trace_file`, `trace_format`, `project`,
`role`, `leg_of`, `parent`. **Empty `inputs` and empty `tool_versions` are
VALID and are the common case.** An absent or unparseable `PROVENANCE.json`
yields a record marked invalid with a reason; one bad arc must not break the
listing.

### The `KorosArcStore` interface -- agreed, not preferred

```
class KorosArcStore:
    list_projects() -> list[ProjectRecord]
    list_arcs(project: str) -> list[ArcRecord]
    read_arc(project: str, arc: str) -> ArcRecord
    record_analysis(project, arc, record: AnalysisRecord,
                    detail: dict | None = None) -> None
    list_analyses(project, arc, kind: str | None = None,
                  analysis_id: str | None = None,
                  latest_only: bool = False) -> list[AnalysisRecord]
    read_detail(record_id: str) -> dict
    delete_record(record_id: str) -> bool
```

`AnalysisRecord` carries `record_id` (this RUN), `analysis_id` (this ANALYSIS,
shared by every re-run of it), `run_uid` (required, identifies one execution),
`kind` (namespaced, e.g. `kbdl.annotation`, `kbutillib.fba`), `created_at`
(ISO8601 UTC -- the run's date), `producer` (tool + version), `subject`,
`status` (`ok|failed|partial`), `artifacts` (named refs, never inlined data),
`payload` (**the seam -- opaque here, never interpreted**), `trust_tier`,
`provenance`, `contract_version`, and the optional producer-supplied summary
fields.

The original six names were settled between the two design sessions on
2026-09-23. **A builder may not rename or restructure them**: a live PRD is
written against them, and renaming breaks an agreement rather than a preference.
`delete_record`, the `detail` argument, and the two `list_analyses` filters are
**additive** -- every call site written against the agreed interface stays
correct -- and they exist because Chris's round-1 review required re-runs to
survive and to be deletable (Q10).

### The two-tier schema

`TABLE runs`: `record_id TEXT PRIMARY KEY` (one row per RUN),
`analysis_id TEXT NOT NULL` (shared by every re-run of one analysis),
`run_uid TEXT NOT NULL`, `project TEXT NULL`,
`arc_slug TEXT NULL` (both null means unattributed), `subject TEXT NOT NULL`,
`kind TEXT NOT NULL`, `producer TEXT NOT NULL`, `producer_version TEXT NOT
NULL`, `status TEXT NOT NULL`, `trust_tier TEXT NOT NULL`, `provenance TEXT NOT
NULL` (JSON), `contract_version INTEGER NOT NULL`, `unknown_kind INTEGER NOT
NULL DEFAULT 0`, `created_at TEXT NOT NULL`, `updated_at TEXT NOT NULL`,
`subject_feature_count INTEGER`, `method_count INTEGER`,
`consistency_overall REAL NULL`, `consistency_metric_version TEXT NULL`,
`ic_corpus_version TEXT NULL`.

`INDEX` on `(analysis_id, created_at DESC)` -- "the runs of this analysis,
newest first" is the query the grouped interface issues, and without the index
it is a scan of the whole table.

`TABLE subject_detail`: `record_id TEXT PRIMARY KEY`, `detail_json TEXT NOT
NULL`. One row per RUN's subject detail, keyed by `record_id`, so two runs of
one analysis hold two blobs and deleting one leaves the other. **This is the
only place gene-level or reaction-level data lives**, and `read_detail` is the
only call that opens it.
`list_analyses` and every summary query must open none -- that boundary is
visible in the API deliberately, rather than hidden inside an implementation.

Engine: SQLite, path resolved from `KBDL_RUN_DB` if set, else
`~/.kbdl/runs.sqlite`.

### Identity: `analysis_id` groups, `record_id` is the run -- both from one helper here

```
analysis_id = sha256( lower(kind) + NUL + lower(subject) + NUL
                      + canonical_json(significant_params) ).hexdigest()

record_id   = sha256( analysis_id + NUL + run_uid ).hexdigest()
```

`canonical_json` sorts keys and uses compact separators. Truncation to 32
characters is **for display only, never for identity**. `significant_params` is
supplied by the caller per kind, and each consumer PRD specifies its own.

**`analysis_id` is what round 0 called `record_id`** -- the derivation is
unchanged. What changed is its job: it now **groups** every run of one logical
analysis instead of collapsing them into a single row (Q10). `record_id` is the
per-run primary key, and `run_uid` is required rather than defaulted, so that a
producer has to decide what one of its runs is instead of discovering the answer
from a row count.

The consequence for idempotency is worth stating exactly, because it is easy to
read the change as weakening the guarantee and it does not:

- **A retry** of one run reuses its `run_uid`, derives the same `record_id`, and
  **replaces** -- exactly as before.
- **A re-run** is a new `run_uid`, a new `record_id`, and a new dated row that
  shares an `analysis_id` with its predecessors.

Both helpers live here and nowhere else. Putting either in each producer
reproduces the problem one level down: "re-recording the same id replaces" is a
guarantee about nothing if two producers derive different ids for one logical
analysis, and "group the re-runs" is impossible if two producers group by
different keys.

### Deleting a run

```
delete_record(record_id) -> bool      # True when a row was removed
```

A **hard** delete of the `runs` row, cascading to its `subject_detail` blob. No
tombstone. Chris's case is curation -- *"if you decide for example that an older
run isn't useful"* -- not falsification, this is a per-user store, and a
tombstone nobody reads is complexity in a store whose discipline is that it
holds only what somebody will look at. The independent evidence that the work
happened is the runs tree and `trace.jsonl`, neither of which this module can
touch.

Deleting a `record_id` that does not exist returns `False` and is not an error;
a delete is a thing a person does twice by accident.

### `artifacts` URI vocabulary

Exactly three forms, and anything else is rejected: a bare absolute path,
`file://`, or `obj://<object_id>` for the KBDL object store. Without a fixed
vocabulary, the first arc carrying both apps' kinds leaves each holding refs it
cannot resolve.

**The KBDL object store IS reachable from the pod.** Chris, 2026-09-24:
*"YES - it is and it always will be, so if that helps the design use it."* Round
0 carried this as an open item and it is now closed. It changes nothing in THIS
module -- the store validates the SHAPE of a ref and never resolves one, so
`obj://` was always storable -- but it removes the reason both consumer apps
were treating object-store artifacts as a degrade path. They can rely on it,
and the "always will be" is what makes that safe to build on rather than a
condition to re-check.

### `provenance` schema

```
{ bridge_kind, metric: {name, value, units}, source: {db, accession} }
```

`bridge_kind` names the **bridge**, not the tier: one of `id_join`,
`sequence_homolog`, `profile_hmm`, `model_prediction`, `user_assertion`. Tier
names must not appear here. An adversarial review proposed
`bridge_kind: "homology|hypothesis|opinion"` and it was rejected on that
ground -- a bridge and a trust level are different facts, and collapsing them
loses the one that can be checked.

### Trust tiers

Four values, decreasing trust: `verified`, `homology`, `hypothesis`, `opinion`.
The module provides a comparison so a caller can ask for "homology or better",
and a **floor helper** that returns the least-trusted of its inputs. It provides
**no API that raises a tier**, and the floor helper is the only way a summary
tier is produced. Any tier other than `verified` requires a non-empty
`provenance`; a write omitting it is rejected.

Tier is recorded **per call, not per tool** -- several tools emit function calls
or bare terms depending on configuration, so a per-tool table would be wrong for
half its rows.

### Unknown kinds

An unrecognised `kind` sets `unknown_kind = 1`, is stored, counted and badged,
and is never refused. Only a record failing **structural** validation is
rejected. This is imitated deliberately from
`kbdl_service.clearinghouse.result_types`. It matters more here than it did
there: two apps write into one database, so one app adding a kind must not
require the other to ship first.

### The write side fails soft

Recording is on by default on pods, so it is a side effect the user did not
request. If the database cannot be written, log and return -- **never raise into
a caller running a pipeline.** A dashboard gap is far cheaper than a failed
scientific run.

### CAC conformance helpers

Id normalisation: the canonical id is the **hyphen** form; the underscore form
is derived mechanically as `id.replace('-','_')` for Python modules and SQL
identifiers, computed and never looked up. Contract-version gating: hard-fail on
a differing **major**, warn and proceed on a compatible minor difference.

**`contract_version` is an INTEGER, currently `1`** -- not a semver string. The
CAC rules this explicitly ("FJ emits int 0; genKnown emits \"0\" -> must change
to int to interoperate"). An adversarial review recommended `"1.0.0"` and was
rejected on that evidence; a string breaks peer interop.

### The reference test double and the contract suite -- new in this PRD

`kbutillib.koros_arc_store_testing` ships `FakeKorosArcStore`: an in-memory
implementation of the same interface, enforcing **the same validation rules as
the real store** -- the artifact URI vocabulary, the provenance requirement on
non-`verified` tiers, replace-never-duplicate on `record_id`, the unknown-kind
policy, and the blob boundary.

A **contract suite** is written once and parameterised over both
implementations, so the real store and the fake are executed against the same
assertions in the same run. A fake that merely accepts everything is worse than
no fake at all: it lets a consumer's tests pass against behaviour the real store
rejects, and the failure surfaces at integration time in someone else's repo.

Both consumer apps import this fake rather than writing their own.

### Specifications forced by confront round 1 (S1-S12)

A GPT-5 codex adversary (`task-adedb702`) tried to execute this PRD phase by
phase and reported fourteen points at which it would have had to guess. Twelve
were real. They are decided here; **do not re-derive them.** Two were rejected
on evidence and the rejections are recorded with the rest, because a builder
who reaches the same conclusion needs to know it was considered.

**S1 -- `ProjectRecord` and `ArcRecord` are pinned, because both consumers
compile against them.**

```
ProjectRecord:  name (str, the directory name)
                path (Path)
                arc_count (int)

ArcRecord:      project (str)
                slug (str, the directory name, VERBATIM)
                path (Path)
                provenance (ArcProvenance | None)
                valid (bool)
                invalid_reason (str | None)
```

**S2 -- the runs root is supplied by argument, not only by environment.**
`resolve_runs_root(runs_root: str | Path | None = None)` is a module-level
function; `KorosArcStore(runs_root=None, db_path=None, known_kinds=None)`
accepts the same value and delegates to it. When `runs_root` is `None`,
resolution follows the chain. Misconfiguration raises a named error type rather
than a bare `ValueError`, so an app can catch it and render "misconfigured"
distinctly from "empty".

**S3 -- `PROVENANCE.json` field optionality.** `run_id` and `created_at` are
REQUIRED; a record missing either is `valid=False` with a reason. `inputs`,
`tool_versions` and `compute_targets` are required keys that **may be empty,
and empty is the common case** -- all 26 live arcs are like this. Every other
listed field is optional and yields `None` when absent. **Keys not in the
listed set are preserved verbatim in a `raw` dict** rather than dropped:
upstream adds fields and losing them silently is how a parser becomes the
reason a future feature is impossible.

**S4 / S7 -- `payload`, the detail blob, and the summary columns. THIS WAS A
REAL CONTRADICTION in the interface as inherited, and it is the most important
fix in this round.** `AnalysisRecord` carried a `payload`, the DDL had no
`payload` column, `read_detail` read a `subject_detail` table, and
`record_analysis` had no way to write one. The blob tier was added after the
interface was agreed and its write path was never specified. Resolved:

- `runs` gains **`payload TEXT NULL`** -- the record's own opaque payload, which
  is small (an objective value, a count, a status detail). It is the seam, and
  this module still never interprets it.
- `record_analysis(project, arc, record: AnalysisRecord, detail: dict | None = None) -> None`.
  When `detail` is given it is written to `subject_detail.detail_json` for this
  `record_id`. This is an **additive** change to the agreed interface -- every
  existing call site remains correct -- and it is propagated to
  `kind-model-analysis-explorer-v1`'s recorded interface in the same commit.
- The summary columns (`subject_feature_count`, `method_count`,
  `consistency_overall`, `consistency_metric_version`, `ic_corpus_version`) are
  **optional fields on `AnalysisRecord`, supplied by the producer**. This module
  computes none of them; it is domain-agnostic and has no idea what a feature or
  a consistency figure is. A record omitting them stores NULL.

**S5 -- what makes a kind "unknown". The module holds NO list of kinds.** A
kind is well-formed when it matches `^[a-z0-9_]+(\.[a-z0-9_]+)+$` -- a
namespace and at least one segment. A malformed kind sets `unknown_kind = 1`.
If the caller passes `known_kinds`, a well-formed kind outside that set ALSO
sets `unknown_kind = 1`. Either way the record is **stored and counted, never
refused.**

*Rejected:* the adversary proposed a `register_kind()` API with `KNOWN_KINDS`
defaulting to empty. That defaults to badging every record unknown, and a
registry of kinds inside this module is exactly the domain knowledge the
module is forbidden to hold -- the same reason `significant_params` is
caller-supplied.

**S6 -- upsert semantics on `record_id`.** Note what an upsert now MEANS: after
Q10 a matching `record_id` is a **retry of one run**, not a second run of the
same analysis, because a re-run carries a different `run_uid` and lands as its
own row. Overwrite every mutable field
(`status`, `producer`, `producer_version`, `trust_tier`, `provenance`,
`contract_version`, `artifacts`, `payload`, the summary columns, and the detail
blob when one is supplied); **preserve the original `created_at`**; set
`updated_at` to now, UTC. A re-record that supplies no `detail` leaves the
existing blob in place rather than deleting it. `analysis_id` and `run_uid` are
**immutable** -- they are what the id was derived from, so a write that changes
either is a different record and must be rejected rather than silently applied.

**S8 -- `artifacts` is `dict[str, str]`**, mapping a caller-chosen name to a
URI. *The adversary's list-of-`{name, uri}`-pairs proposal is rejected:* the
inherited spec already said "dict of named refs", and changing the shape would
break an agreed interface to fix an ambiguity that one sentence closes. That
sentence is this one. `file://` and `file:///` are both accepted on read and
normalised to `file://` + an absolute path on write.

**S9 -- `status` is exactly one of `ok`, `failed`, `partial`**, lower-case. Any
other value, including a case variant, is **rejected**. *The adversary proposed
accepting case-insensitively and coercing;* rejected for consistency with the
rest of the module, which rejects a bad artifact URI and a missing provenance
rather than repairing them.

**S10 -- database path creation, and the read/write asymmetry.** The store
creates the parent directory of `db_path` if it is missing. **On any WRITE
error it logs a warning with a stable, greppable message and returns** -- the
fail-soft rule. **A READ against a database that cannot be opened RAISES**, so
an app shows an error rather than an empty list. Those two are deliberately
different: a failed write must not break a pipeline; a failed read
masquerading as "no data" is the thing story 11 and G10 exist to prevent.

**S11 -- `contract_version` gating. THE INHERITED SPEC CONTRADICTED ITSELF,
neither parent PRD caught it, AND THE CONTRADICTION IS UPSTREAM IN THE CAC
ITSELF -- verified by reading it rather than inferred.** Our spec said
"hard-fail on an incompatible MAJOR mismatch, warn and proceed on a compatible
minor difference" AND "`contract_version` is an INTEGER". An integer has no
minor. The CAC is where both halves come from and it does not reconcile them:

- The **wire field is an integer**, ruled repeatedly and for a stated interop
  reason -- `CROSS_APP_COMMUNICATION.md:141` ("INTEGER -- RULED (FJ emits int 0;
  genKnown emits \"0\" -> must change to int to interoperate)"), and again in the
  DDL at `:173` and `:252`.
- **Section F describes semver gating** -- `:290` "RESOLVED (semver): hard-gate
  on incompatible (major) mismatch, soft-warn on compatible (minor/additive)" --
  and then marks the mechanics as an explicit unfilled placeholder at `:294`:
  "fill: the gate/warn mechanics + how an app declares the version it targets".

**Resolved: implement integer comparison ONLY. Equal proceeds; any difference
hard-fails with an error naming both versions. Implement NO minor branch** --
not because soft-warning is wrong in principle, but because with an integer
wire field there is nothing to compare a minor against, and writing one today
means inventing a representation upstream has explicitly not chosen. This is
the conservative half of the CAC's own rule: the hard gate is implementable and
the soft warn is not yet.

Revisit when `:294` is filled. This is not a new open item -- the PRD already
lists the contract-version gate mechanics among the CAC's unfilled seams
(Q9), and S11 is what that seam looks like when a builder hits it.

This propagates into both consumer apps, whose tasks required the
warn-on-minor behaviour: `kind-model-analysis-explorer-v1`'s `p3-app-skeleton`
and `kind-annotation-results-explorer-v1`'s `p4-kind-registration`. Both are
corrected in the same commit.

**S12 -- what id normalisation applies to.** **APP IDS ONLY** -- the canonical
hyphen form that is simultaneously the distribution name, the console command,
the manifest basename and the `koros --project` token (CAC I4). The underscore
form is derived for Python modules and SQL identifiers.

*Rejected, and the rejection matters:* the adversary proposed normalising
`kind` and "any `entity_id` fields". **Never apply it to `kind`, `subject` or
`record_id`.** Kinds are dotted namespaced strings; hyphen/underscore
normalisation would mutate them, and `record_id` is a hash whose identity
depends on the exact `kind` and `subject` that went into it. Normalising
either would silently fork the identity space.

**S13 -- test framework and fixtures.** `pytest`, matching KBUtilLib's existing
style. Fixtures live under `tests/fixtures/koros_arc_store/` in three
subdirectories: `runs_tree/`, `runs_db/` and `subject_blobs/`. `runs_tree/`
MUST contain at least a `PROVENANCE.json` with empty `inputs` and
`tool_versions`, a project directory with no `arcs/` subdirectory, an arc with
`role: leg` and a populated `leg_of`, and an unparseable `PROVENANCE.json`.
No network in tests.

**S14 -- `FakeKorosArcStore` uses plain in-memory dictionaries**, not an
in-memory SQLite. It enforces the same validation rules; the DDL itself is
exercised by the contract suite's real-store parameterisation only, and those
assertions are marked not-applicable for the fake **explicitly** rather than
skipped silently.

**Adopted from the free critique:**

- An arc `slug` is the directory name **verbatim**. No case normalisation and
  no slug rewriting -- the adversary suggested lower-casing, which would break
  lookup on a case-sensitive filesystem where two arcs differ only in case.
  Comparisons are exact.
- A soft-failed write emits a warning on a **named logger with a stable message
  prefix** and increments an in-process counter the caller can read. Fail-soft
  without a signal is silent data loss; see G12.

**Declined from the free critique:** a `provenance_version` column (the record
already carries `contract_version`; a second version axis with no consumer is
speculative), and a hard performance budget on blob-opening (the contract suite
asserts the count directly, which is stronger than a budget and does not need
a threshold chosen without measurement).

## Testing Decisions

A good test here tests **externally visible behaviour of the contract**, not the
SQLite schema. The schema is an implementation choice the interface exists to
hide; the things worth asserting are the ones a consumer can observe.

**The contract suite is the centre of the testing story**, and its defining
property is that it runs unchanged against both the real store and the fake. If
adding an assertion to it breaks the fake, the fake was lying. Every rule in
"The honesty spine" gets an assertion there.

The behaviours that must be covered, in both implementations where applicable:

- The runs root resolving through each step of the chain **and raising when none
  resolve**; no hardcoded path anywhere; `KIND_KOROS_RUNS` appearing nowhere
  (both checkable by grep, and asserted as such).
- `$KOROS_HOME` treated as the workspace root, with runs read from its `runs/`
  subdirectory.
- A `PROVENANCE.json` with empty `inputs` and `tool_versions` parsing as
  **valid** -- this is the common case, not an edge case.
- A project directory with no `arcs/` subdirectory enumerating as zero arcs.
- Lineage (`role`, `leg_of`, `parent`) surviving parsing.
- An unparseable `PROVENANCE.json` yielding an invalid-marked record without
  breaking enumeration.
- The floor rule in **both** directions: adding a lower-tier input lowers the
  floor; adding a higher-tier input never raises it.
- A non-`verified` tier with empty provenance rejected.
- An unknown kind stored, flagged and **counted** -- not dropped, not rejected.
- A null project/arc record stored and read back as unattributed.
- Re-recording a `record_id` replacing rather than duplicating.
- Two producers deriving the same `record_id` for the same logical analysis.
- `artifacts` values outside the three permitted forms rejected.
- **The blob boundary: `list_analyses` and the summary queries open NO blob,
  while `read_detail` opens exactly one.** This is the assertion most worth
  writing well, because violating it is invisible until the corpus is large.
- A database that cannot be written not raising into the caller.
- **Re-runs, grouping and delete (Q10), which is where a regression would be
  quietest:** a second run of the same analysis with a new `run_uid` producing a
  SECOND row rather than replacing the first; both rows sharing one
  `analysis_id`; a retry with the SAME `run_uid` still replacing; `latest_only`
  returning exactly one row per `analysis_id` and it being the newest by
  `created_at`; `delete_record` removing the row AND its blob; `delete_record`
  on an absent id returning `False` without raising; deleting one run of two
  leaving the other and its blob intact; and a write attempting to change
  `analysis_id` or `run_uid` on an existing `record_id` being rejected.
- The underscore id form derived mechanically, never looked up.
- A differing major `contract_version` hard-failing; a compatible difference
  warning and proceeding.

Fixtures live under `tests/fixtures/koros_arc_store/` with `runs_db/` and
`subject_blobs/`. **No network in tests.** Prior art for the import-boundary
assertion is `KBDLJobRunningPrototype`'s `tests/test_layering.py`, which is the
model for asserting that this module imports nothing from the five KING/KOROS
repos.

## Open Questions and Judgement Calls

**Every entry below is already decided and reflected in the body and the
taskplan.** This PRD is buildable as it stands. The section is a manifest of
the choices made, not a queue of holes; changing a decision means editing its
`DECIDED:` line and letting a review round propagate the consequences.

Entries are ordered by **blast radius descending**. Q-numbers are stable ids,
not positions.

### Q1. Where exactly does the line fall between this shared module and the two apps? -- DECIDED: navigation, the record store, identity and CAC conformance are shared; all science, all rendering, all routes and all manifests stay in the apps.

**Blast radius:** IRREVERSIBLE
**Why:** The seam is the `payload`: opaque here, interpreted only by whichever
app owns the record kind. That is what lets two apps share navigation while
sharing no science code at all. Moving a responsibility across this line after
both apps are built means changing three repos at once, and the two apps have
different owners' attention.
**If you disagree:** the concrete things that move are `record_id` derivation
and the `trust_tier` floor helper -- the two pieces most tempting to push into
the apps. Pushing either one out means each producer derives its own ids or its
own floors, which is precisely the defect the twin session found in the
pre-extraction plan and which `p0b` was corrected to fix.
**Confidence:** high -- the line was drawn by two sessions independently
reaching the same place, then attacked in a confront round.

### Q2. Should the interface names be revisited now that the module has its own PRD? -- DECIDED: no. `KorosArcStore`, `AnalysisRecord`, `resolve_runs_root` and the six method names are frozen as agreed.

**Blast radius:** IRREVERSIBLE
**Why:** `kind-model-analysis-explorer-v1` is a `ready` PRD whose six task
prompts import these names and carry STOP-and-report preconditions on them.
Renaming during extraction would break a live agreement for cosmetic gain, and
extraction is exactly the moment where a fresh document tempts a fresh naming
pass.
**If you disagree:** every rename has to land in the same commit as edits to
`kind-model-analysis-explorer-v1`'s `p1-arc-resolver`, `p2-kbu-model-stamping`
and `p3-app-skeleton` prompts, and to `kind-annotation-results-explorer-v1`'s
`p1` and `p2`. Five task prompts across two repos.
**Confidence:** high.

### Q3. Is a per-user SQLite database in the home directory the right store? -- DECIDED: yes, two-tier, at `KBDL_RUN_DB` or `~/.kbdl/runs.sqlite`.

**Blast radius:** IRREVERSIBLE
**Why:** Chris's instruction is explicit and names both domains: "kbdl client
creating a local database (optionally configurable with PODs having it on by
default) and keeping enough data in the database to make the dashboard for
genome annotations and models easy to load BUT does NOT have TOO many rows
(meaning genome gene level and reaction level data should be JSON blobs rather
than SQL tables)." It also solves a real hazard the earlier arc-sidecar design
had: the runs tree is Dropbox-synced, and Dropbox writes a conflicted copy
rather than merging concurrent appends, silently.
**If you disagree:** the interface does not move -- storage was put behind
`KorosArcStore` for exactly this reason -- but `p0b`'s DDL, the blob boundary
tests, and the "fails soft" contract are all written against a local file store.
A networked store would also make the fail-soft rule load-bearing rather than
defensive.
**Confidence:** high on the decision, medium on the specific column set: which
summary statistics are precomputed is the part most likely to need a second
pass, and it is why the version-stamp columns exist.

### Q10. Do re-runs of the same analysis collapse to one row? -- DECIDED: NO. Re-runs are distinct, dated rows, grouped by a stable `analysis_id`, and a run can be DELETED. Chris, 2026-09-24.

**Blast radius:** IRREVERSIBLE
**Why:** Chris, reviewing round 0, verbatim: *"No - it should not, so runs should
be dated and ideally grouped in the interace, and critically you should be able
to 'delete' a run from the database if you decide for example that an older run
isn't useful. The apps should offer this kind of delete interface and the
underlying data API should support it."*

Round 0 carried this as an unresolved item and it is now the design's identity
model, so it is an Open Question rather than a note. **The single derived
`record_id` could not satisfy it:** it hashed kind + subject +
`significant_params`, so a second run of an identical analysis produced the same
id and replaced its predecessor. That is precisely the collapse Chris rejects.

**The identity is therefore SPLIT, and the split is the whole of this entry:**

- **`analysis_id`** -- `sha256(lower(kind) + NUL + lower(subject) + NUL +
  canonical_json(significant_params))`. This is round 0's derivation, unchanged
  and still produced by ONE shared helper. It is now the **grouping** key: every
  run of the same logical analysis shares it, which is what lets an app group
  re-runs in the interface.
- **`record_id`** -- `sha256(analysis_id + NUL + run_uid)`, the primary key, one
  row per RUN. `run_uid` is **required** and identifies one execution.

This preserves every guarantee that was negotiated with the twin session while
delivering what Chris asked for. Re-recording the same `record_id` still
replaces rather than duplicates -- that is now a **retry** of one run, which is
what the guarantee was always for. A producer that mints `run_uid` once at run
start and reuses it on retry gets idempotency; one that mints a fresh id per
attempt gets a row per attempt, visibly and by its own choice.

Runs are dated by the `created_at` column that already existed, and an index on
`(analysis_id, created_at DESC)` makes "the runs of this analysis, newest first"
one query.

**Delete is a first-class operation, not a cleanup script.** `delete_record`
removes the row and cascades to its detail blob. It is a **hard** delete with no
tombstone: this is a per-user curation store, Chris's case is "an older run
isn't useful", and a tombstone nobody reads would be complexity added to a store
whose whole discipline is that it holds only what somebody will look at. The
independent evidence that work happened is the runs tree and `trace.jsonl`,
neither of which this module can touch.

**If you disagree:** reverting to collapse-on-rerun means dropping `run_uid` and
`analysis_id` from the DDL and the record, removing `delete_record` from the
interface, the fake and the contract suite, and deleting acceptance criteria
43-49. It also means telling both consumer apps that the delete affordance Chris
asked them for has no API under it. The grouping half could be kept without the
delete half; the reverse is not true, because deleting one of two identical rows
is meaningless when there is only ever one.
**Confidence:** high on the decision, which is Chris's own words. Medium on
making `run_uid` **required** rather than defaulting to a fresh UUID: required
forces every producer to decide what a run is, and a default would quietly make
every retry a new row. Required is the noisier and more honest choice, and it is
cheap to relax.

### Q4. Should this PRD ship a reference test double, or should each app write its own fake? -- DECIDED: ship one, from this module, with a contract suite both implementations must pass. NEW IN THIS EXTRACTION.

**Blast radius:** MEDIUM
**Why:** `kind-model-analysis-explorer-v1`'s `p3-app-skeleton` currently tells
its builder "TESTS USE A FAKE STORE. Build the app tests against a fake
`KorosArcStore` fixture." Nothing binds that fake to the real module. A fake
written by a consumer drifts toward whatever makes the consumer's tests pass,
and the divergence surfaces at integration time in a third repo. Shipping the
fake from the owning module is the thing extraction makes cheap: the interface
stops being prose in two documents and becomes an executable contract.
This is not added work so much as **moved** work -- the model-analysis app was
going to build a fake regardless.
**If you disagree:** drop `p0d` from the taskplan, and restore the fake-store
instruction in `kind-model-analysis-explorer-v1`'s `p3-app-skeleton`. The
annotation app is unaffected either way, since its plan never mentioned a fake.
**Confidence:** medium -- the value is real but unmeasured here, and a
permissive fake would be worse than none, which is why the contract suite runs
against both implementations rather than the fake merely existing.

### Q5. Does extracting this module leave `kind-annotation-results-explorer-v1` dispatchable? -- DECIDED: no, and that is stated loudly in both documents rather than discovered at dispatch.

**Blast radius:** MEDIUM
**Why:** That PRD is registered `ready`, and an open continuation (session
`1f49b595`, id 257) instructs an executor session to "dispatch `p0a` first,
alone". After this extraction its first task is `p1-kbdl-arc-attribution`,
which imports a module that does not exist. The task prompts now carry
STOP-and-report preconditions, so the failure is loud rather than silent -- but
a loud failure still burns an envelope and a slot.
**If you disagree:** the alternative is leaving `p0a`/`p0b`/`p0c` in both
places, which means a dispatcher can build the module twice, on two branches,
in the same repo.
**Confidence:** high on the fact; the mitigation (superseding the stale
continuation and saying so in both PRDs) is the part that depends on somebody
reading it.

### Q6. Should this PRD be registered `ready` on the strength of the review its content already carries? -- DECIDED: no; held at `draft` until Chris pressed. RESOLVED 2026-09-24: he reviewed, edited, and pressed.

**Blast radius:** MEDIUM
**Why:** The material is unusually well-reviewed for a new document -- it has
been through a confront round that folded sixteen binding stalls into exactly
this text, a cross-PRD conformance review that found three defects in it and
fixed all three, and one Chris review round in its parent. But `review_rounds`
is a property of a document, and this document is new. The gate exists so that
nobody dispatches a PRD Chris has not read, and self-advancing on inherited
provenance is precisely the reasoning that makes a gate stop meaning anything.
**If you disagree:** the consequence of holding at `draft` was real and is worth
keeping on the record -- work that was dispatchable on the 23rd was not
dispatchable on the 24th, and stayed that way until Chris read the document.
The wait cost about eleven hours and bought a review that reversed the identity
model (Q10), which is a defect that would otherwise have been found by a builder
or, worse, by a scientist who lost a re-run.
**Confidence:** high, and now evidenced rather than argued.

### Q7. Do the CAC conformance helpers belong in this module at all, or in each app? -- DECIDED: here.

**Blast radius:** LOW
**Why:** Id normalisation and contract-version gating are the two places where
two apps could conform to the CAC in two different ways, and `contract_version`
is an integer precisely so peers can interoperate -- a per-app implementation
reintroduces the exact string-versus-integer divergence the CAC calls out by
name. They are small, but they are shared-by-nature.
**If you disagree:** `p0c` is a 3-file task and folds into either app cheaply;
what does not fold cheaply is the guarantee that both apps gate versions the
same way.
**Confidence:** high.

### Q8. Should the test fixtures keep the path `tests/fixtures/kind_annotation_explorer/`? -- DECIDED: no, moved to `tests/fixtures/koros_arc_store/`.

**Blast radius:** LOW
**Why:** It named a consumer app inside a module whose own task prompt insists
in capitals that the schema names are deliberately domain-neutral because a
second app shares them. It was an artifact of the module having been drafted
inside that app's PRD, and the extraction is the moment it becomes visible.
**If you disagree:** nothing else moves; it is a directory name in one task
prompt.
**Confidence:** high.

### Q9. What I could not decide and did not guess.

- **The CAC's own unfilled `<fill:>` seams** in the id-normalisation helper and
  the contract-version gate mechanics. `p0c` implements the documented
  behaviour; the remaining detail will be settled by reading FJ's or genKnown's
  `interop.py`, not by reading the CAC. Tracked as dev 1275 -- see S11, where
  this stopped being abstract.
- **Nothing else.** The two items round 0 listed here were both answered by
  Chris on 2026-09-24 and have moved into the body: the KBDL object store IS
  reachable from the pod and always will be (his words), and "official app in
  KOROS" means it shows up as an app **in** KIND, not contributed upstream --
  see Q3 and the note below it.

## Gotchas and Unintuitive Consequences

**G1. The extraction temporarily makes a `ready` PRD un-dispatchable.**
`kind-annotation-results-explorer-v1` was dispatchable an hour before this
document existed. It is not now, and will not be until this module is on
KBUtilLib `main`. Nothing about that is visible on the dev board, which shows
three `ready` PRDs with no edge between them.

**G2. KBUtilLib is both the owning repo of this module AND the repo the
model-analysis app is built in.** Under the one-branch-per-repo rule (dev 1032)
that means the model-analysis app cannot begin while this module is building --
not because of its declared dependency, but because of the repo slot. The
annotation app has no such collision: its own tasks are in
KBDLJobRunningPrototype and GenomeAnnotationAggregator. So the two consumers
are *not* symmetric in the build order even though the dependency graph says
they are.

**G3. Shipping the fake from this module means a KBUtilLib commit can break a
GenomeAnnotationAggregator test run.** That is the price of an executable
contract and it is the right price -- a break here is the contract doing its
job -- but it is a new cross-repo coupling that did not exist when each app
owned its own fake, and the first time it fires it will look like an unrelated
repo breaking someone's build.

**G4. "On by default on pods" means the first pod run after deploy starts
writing `~/.kbdl/runs.sqlite` without anyone having asked for it.** The pod home
directory persists across re-provisioning, which is what makes the store useful
and also means the file outlives the session that created it. Nobody will be
told this is happening.

**G5. Changing the `significant_params` list for a kind silently re-ids every
future run of it** -- and after Q10 it does something worse than fork identity:
it forks the GROUPING. Old runs keep the old `analysis_id`, new ones get
another, and the interface shows what is really one analysis as two unrelated
groups with no error anywhere. Old rows keep their old ids, new rows get new ones, and the
two populations coexist as different analyses with no error anywhere. Adding
`bakta_db` to the annotation params was correct; doing it *after* records exist
would fork the identity space.

**G6. Precomputed summary statistics need their version stamped or the database
silently holds two populations scored differently.** `consistency_metric_version`
and `ic_corpus_version` exist for this and are not optional decoration -- an
unstamped column is indistinguishable from a stamped one until someone compares
two arcs scored months apart.

**G7. The two-tier split forecloses cross-subject detail queries.** "Which genes
disagreed across every genome in this arc" cannot be answered from SQL, because
the gene level is inside blobs. The fix, when someone wants it, is a derived
index built from the blobs -- **not** a schema change, and not expanding the
blob into rows, which is the thing the arithmetic forbids.

**G8. The floor rule means adding a well-evidenced method to a summary can never
improve its tier.** This is correct and it will surprise people: the intuition
is that more evidence is better evidence. Expect the question, and expect the
answer ("a summary is only as trustworthy as its weakest contributing call") to
be unpopular the first time a `verified` method gets averaged into a
`hypothesis` floor.

**G9. Unattributed will be most records for a long time.** Arc attribution is
optional at every producer, recorded forward and never inferred. Until every
producer adopts it, the unattributed lane is where the data is -- so a dashboard
that treats unattributed as an edge case will look broken while being correct.

**G10. Neither app can show historical work.** The 26 existing arcs cannot be
backfilled: `inputs` and `tool_versions` are empty on all of them and no
analysis record exists to reconstruct. Historical coverage requires re-running
under attribution. Both apps therefore launch empty, and "empty" must be
distinguishable in the UI from "misconfigured" -- which is also why runs-root
resolution raises rather than guessing.

**G12. Fail-soft on write is silent data loss unless somebody looks.** A pod
whose home directory is full, or read-only, records nothing and breaks nothing.
The dashboard is simply empty, which is indistinguishable from "no work has been
done" -- the same confusion the runs-root rule refuses to create. The warning log
and the in-process counter (S-free) are the only signal, and nothing reads them
today.

**G13. Deleting a run can leave a precomputed summary stale, and nothing
recomputes it.** The summary columns and any stored trust floor were computed
when the record was written, from a population that included the deleted run. An
arc-level figure that summarised three runs still says so after one is deleted.
This is the cost of precomputing (G6) meeting the cost of deleting, and neither
feature can see the other: the store computes no summaries, so it cannot
recompute them either. A consumer that shows an aggregate after a delete must
either recompute it itself or say when it was computed.

**G14. A delete is unrecoverable and leaves no trace in this store.** No
tombstone, by decision (Q10). The runs tree and `trace.jsonl` remain independent
evidence that the work happened, but nothing in the run database will say a row
was ever there -- so "the dashboard used to show four runs and now shows three"
has no explanation inside the system. That is the right trade for a per-user
curation store and the wrong one to inherit silently if this database ever
becomes shared.

**G11. `list_analyses` opening a blob would be invisible until the corpus is
large.** Nothing fails; the query simply gets slower in proportion to data
nobody is looking at. This is why the blob boundary is asserted by a test rather
than left to review.

## Sources Consulted

**Parent PRD bundles, read in full:**

- `GenomeAnnotationAggregator/agent-io/prds/kind-annotation-results-explorer-v1/`
  -- `fullprompt.md` (the Solution "shared scaffold", Implementation Decisions
  "The run database", "The shared scaffold" and "Concrete specifications" blocks),
  `data.json` (26 decisions, the confront round-1 record, the
  `post_confront_conformance_review` block naming three defects, the
  `key_measurements_2026_09_23` census), `taskplan.json` (the `p0a`/`p0b`/`p0c`
  prompts carried into this plan).
- `KBUtilLib/agent-io/prds/kind-model-analysis-explorer-v1/` -- `data.json`
  (`hard_external_dependency`, `shared_interface`, `storage_backend`,
  `chris_answers_2026_09_23`, both confront rounds), `taskplan.json` (the
  STOP-preconditions in `p1-arc-resolver`, `p2-kbu-model-stamping` and
  `p3-app-skeleton`, and the fake-store instruction at `p3` line 63).

**Repository state, verified 2026-09-24:**

- `KBUtilLib` on `wip` at `52068b3`; `GenomeAnnotationAggregator` on `wip` at
  `4723830`. KBUtilLib carries uncommitted work from a live sibling session, so
  staging here is by explicit path only.
- `grep -rl 'koros_arc_store|KorosArcStore|arc_context'` over
  `KBUtilLib/src` and `KBDLJobRunningPrototype/src`: **zero hits.** The module
  does not exist and neither app has been dispatched.
- `KBUtilLib/src/kbutillib/` layout: flat modules (`escher_utils.py`,
  `kb_model_utils.py`) alongside packages (`core/`, `interfaces/`, `domains/`).
  `kind_app/` holds KIND skill bundles (`bundle.json`, `skill.md`), not a Python
  app -- so it is **not** a home for this module.

**Registry:** `AIAssistant/state/prd_registry.json` --
`kind-annotation-results-explorer-v1` and `kind-model-analysis-explorer-v1` both
at `status: ready`, `auto_conduct` unset on both. Nothing builds either
autonomously, which is why this extraction does not race a dispatcher.

**Open continuations read for their content:** ids 256 (session `7abb83e1`) and
257 (session `1f49b595`) -- both name this extraction as the decision they were
waiting on, and 257 carries the stale dispatch instruction that G1/Q5 address.

**Read and found uninformative:** `KBUtilLib/src/kbutillib/kind_app/` (skill
bundles, not app code); `KBUtilLib/src/kbutillib/core/` (no store or registry
primitive this module could reuse -- `core/registry.py` is a capability
registry, unrelated).

**Not re-derived, taken from the parent bundles:** the live runs-tree census,
the `trace.jsonl` event mix, the 70,000-facts arithmetic, the CAC six-invariant
reading, the `koros_start.py` / `research_init.py` / `king_backend/config.py`
line references for runs-root resolution, and the external scan findings. Each
is attributed above rather than presented as fresh work.

## Out of Scope

- **Any app, route, template, manifest or KIND registration.** This PRD ships a
  library. Both consumer PRDs keep their own app, their own console entry point
  and their own manifest.
- **Any science.** No annotation comparison, no consistency metric
  implementation, no flux or fitness interpretation. The module stores a
  `consistency_overall` column; it does not compute one.
- **Arc attribution at the producers.** Teaching KBDL and the KBUtilLib CLI to
  stamp an arc onto their work stays in the consumer PRDs
  (`p1-kbdl-arc-attribution` and `p2-kbu-model-stamping` respectively). This
  module provides the place to write; it does not make anyone write.
- **Crosswalk and reference data.** Loading InterPro2GO, EC2GO or UniProt
  crosswalks into the datalake is owned elsewhere and is not a task in any of
  these three plans.
- **Backfilling the 26 existing arcs.** Not deferred -- not possible. See G10.
- **Contributing anything upstream into the five KING/KOROS repos.** The
  consume-only interlock holds by construction: this module imports nothing from
  them and writes nothing to them. **Chris settled the reading on 2026-09-24:**
  *"I want it to show up as an app in KIND."* That is registration, not
  contribution -- the apps conform to the CAC and declare themselves; no code of
  ours lands in `king`, `koros`, `semcat`, `lakehouse-explorer` or
  `narrative-connector`. This closes the question for all three PRDs.
- **Registering anything with KIND.** He asked, in the same edit, whether this
  involves a deploy step. It does, and it is one file: KING loads `*.json` from
  its plugins directory **at request time**, so a manifest with `"type":"app"`
  dropped there registers an app with no KING restart and no KING code change
  (`king_backend/plugins.py`; `docs/APP_INTEGRATION.md:91`; the directory
  resolves `$KING_PLUGINS_DIR` -> `<king checkout>/plugins/` -> the bundled
  `_plugins`, at `king_backend/config.py:117-127`). **That step belongs to the
  two consumer apps, not to this module** -- a library registers nothing. Two
  things found while checking it are recorded as **dev 1294** rather than fixed
  here, because they are defects in the app PRDs: both of them write the
  manifest to `~/kind-apps/plugins/`, a path that appears NOWHERE in the stack,
  and `plugins.py::_load()` swallows every error, so a misplaced or malformed
  manifest means the app simply never appears, with no message anywhere.

## Further Notes

### Why not merge the two app PRDs

Chris floated merging as the alternative. Four reasons it was not done, recorded
here because the question will come back:

1. No taskplan can express `depends_on` across PRDs -- but merging solves that
   by making a document that is two documents, and the coupling it removes is
   replaced by an eleven-task plan of which eight tasks never interact.
2. The two PRDs have different review and confront histories (1/1 and 1/2). A
   merged document could not say which half had been adversarially attacked, and
   that is precisely the claim a reviewer needs to trust.
3. The apps are otherwise disjoint: different repos, different science,
   different record kinds, different frontends.
4. Extraction gets most of the benefit of merging -- one authoritative
   specification of the shared layer -- without any of the cost.

### The ordering contract, stated once

```
koros-arc-store-v1  (KBUtilLib)   -- p0a -> p0b -> p0c -> p0d, strictly sequential
      |
      +-- kind-annotation-results-explorer-v1  (KBDLJobRunningPrototype, GAA)
      +-- kind-model-analysis-explorer-v1      (KBUtilLib, KBDLJobRunningPrototype)
```

The four tasks here are sequential because they are all in one repo
(one-branch-per-repo, dev 1032) and because each composes the previous. After
this lands, the two apps are independent of each other, with the two caveats in
G2 (the KBUtilLib repo slot) and the shared KBDLJobRunningPrototype slot.

**Nothing enforces this ordering except the STOP-preconditions in the consumer
task prompts and whoever reads the dev board.** That is the honest state of the
tooling and it is why this PRD says so in three places rather than one.

### Related task rows

- **dev 1262** -- the `cw-kind` skill instructs resolving the runs root via
  `KIND_KOROS_RUNS`, which does not exist. Both app PRDs inherited the error from
  it. Fixing the skill does not fix anything here (the corrected chain is already
  in `p0a`), but leaving it stale means the next person to read the skill
  reintroduces it.
- **dev 1273** -- the three conformance defects the model-analysis session found
  in the pre-split `p0-shared-scaffold`. All three are fixed in the prompts
  carried into this plan: the runs-root chain (`p0a`), the canonical `record_id`
  helper and the artifact URI vocabulary (`p0b`).

## Acceptance Criteria

1. `resolve_runs_root` resolves via an explicit argument, then `$KOROS_HOME/runs`, then `$KING_KOROS_RUNS`, and raises a named error when none resolve.
2. No hardcoded runs path exists anywhere in the module, and `KIND_KOROS_RUNS` appears nowhere -- both verifiable by grep.
3. `$KOROS_HOME` is treated as the workspace root, with runs read from its `runs/` subdirectory.
4. `ProjectRecord` exposes `name`, `path` and `arc_count`; `ArcRecord` exposes `project`, `slug`, `path`, `provenance`, `valid` and `invalid_reason`.
5. An arc `slug` is the directory name verbatim, with no case normalisation, and lookup by slug is an exact match.
6. A `PROVENANCE.json` with empty `inputs` and empty `tool_versions` parses as valid.
7. A `PROVENANCE.json` missing `run_id` or `created_at` yields `valid=False` with a reason and does not raise.
8. Keys in `PROVENANCE.json` outside the documented set are preserved in a `raw` mapping rather than dropped.
9. A project directory with no `arcs/` subdirectory enumerates as a project with zero arcs and does not raise.
10. Lineage fields `role`, `leg_of` and `parent` survive parsing unnormalised.
11. An unparseable `PROVENANCE.json` yields an invalid-marked record and does not break enumeration of the other arcs.
12. The module imports nothing from `king_backend` or the five KING/KOROS repos, asserted by an import-boundary test.
13. `KorosArcStore` exposes `list_projects`, `list_arcs`, `read_arc`, `record_analysis`, `list_analyses` and `read_detail` under exactly those names.
14. `record_analysis` accepts an optional `detail` mapping and writes it to `subject_detail.detail_json` for that `record_id`.
15. `runs` carries a nullable `payload` column holding the record's own opaque payload; the module never interprets it.
16. The summary columns are populated only from optional fields supplied on `AnalysisRecord`; the module computes none of them, and a record omitting them stores NULL.
17. `list_analyses` and every summary query open NO detail blob; `read_detail` opens exactly one. Asserted directly, against both implementations.
18. `analysis_id` is derived by one shared helper such that two producers recording the same logical analysis produce the same `analysis_id`.
19. Truncation of either id is available for display and is never used for identity.
20. `artifacts` is a mapping of name to URI; a value outside {absolute path, `file://`, `obj://<object_id>`} is rejected; `file:///` is accepted on read and normalised on write.
21. `status` outside {`ok`, `failed`, `partial`} is rejected, including case variants.
22. A record at a tier other than `verified` with an empty `provenance` is rejected.
23. `provenance.bridge_kind` is one of {`id_join`, `sequence_homolog`, `profile_hmm`, `model_prediction`, `user_assertion`}; a tier name in that field is rejected.
24. The trust-tier floor helper returns the least-trusted of its inputs, is never raised by adding a higher-tier input, and no other API produces a tier.
25. A "homology-or-better" comparison returns exactly `verified` and `homology`.
26. A malformed `kind` sets `unknown_kind = 1`; the record is stored and counted, never refused.
27. When `known_kinds` is supplied, a well-formed kind outside it also sets `unknown_kind = 1` and is still stored and counted.
28. The module contains no hardcoded list of record kinds -- verifiable by grep for `kbdl.` and `kbutillib.` literals outside tests and docstrings.
29. A record with null `project` and `arc_slug` is stored and reads back as unattributed.
30. Re-recording an existing `record_id` (a RETRY -- same `run_uid`) replaces rather than duplicates, preserves the original `created_at`, and advances `updated_at`.
31. A re-record supplying no `detail` leaves the existing blob in place.
32. `contract_version` is handled as an integer everywhere; no semver string appears in the code or its tests.
33. A differing `contract_version` hard-fails with an error naming both versions, and no minor/warn-and-proceed branch is implemented.
34. The id-normalisation helper derives the underscore form mechanically from the hyphen form and is applied to app ids only -- never to `kind`, `subject` or `record_id`.
35. A database that cannot be written logs a warning on a named logger with a stable message prefix, increments a readable counter, and does not raise into the caller.
36. A database that cannot be read RAISES rather than returning an empty result.
37. The store creates the parent directory of its database path when it is missing.
38. `FakeKorosArcStore` is importable from `kbutillib.koros_arc_store_testing`, a module separate from `koros_arc_store`.
39. The fake enforces the same validation rules as the real store and derives `record_id` by importing the shared helper, not by reimplementing it.
40. The contract suite runs parameterised over both the real store and the fake in one run; assertions that apply only to the real store are marked not-applicable for the fake explicitly rather than skipped silently.
41. Fixtures live under `tests/fixtures/koros_arc_store/` in `runs_tree/`, `runs_db/` and `subject_blobs/`, and no test makes a network call.
42. A write that changes `analysis_id` or `run_uid` on an existing `record_id` is rejected rather than applied.
43. A second run of the same analysis with a new `run_uid` produces a SECOND row; the first is not replaced.
44. Both rows share one `analysis_id`, and `list_analyses(analysis_id=...)` returns both.
45. `list_analyses(latest_only=True)` returns exactly one row per `analysis_id`, and it is the newest by `created_at`.
46. An index on `(analysis_id, created_at)` exists, verifiable in the schema.
47. `delete_record` removes the `runs` row AND its `subject_detail` blob, and returns `True`.
48. `delete_record` on an id that does not exist returns `False` and does not raise.
49. Deleting one run of an analysis leaves the other run and its blob intact.
50. `run_uid` is required: constructing or recording an `AnalysisRecord` without one is rejected rather than defaulted.
51. The tests this plan adds pass, and no test that passed on the base commit fails on the branch.
