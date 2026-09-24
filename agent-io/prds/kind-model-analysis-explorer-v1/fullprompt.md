# PRD: kind-model-analysis-explorer-v1 — Models and Analyses, by Project and Arc

## Research Report

### Important findings

**1. The deepest two levels of the requested app already exist, fully built.**
`EscherUtils.create_fitness_dashboard`
(`kbutillib/domains/notebook/escher_utils.py:1498`, builder at
`domains/notebook/fitness_dashboard.py`, 458 lines)
renders a single self-contained HTML dashboard that consumes the *native JSON
outputs of the KBDL pipeline* and produces exactly what Chris described as the
bottom of the drill-down:
an Escher map recoloured by fitness **class** with a condition dropdown and an
FVA-solution dropdown,
toggleable experimental and propagated RB-TnSeq badge layers,
and five tabular tabs —
`Genes (annotation-oriented)`, `Reactions (model-oriented)`, `Fitness detail`,
`Conditions`, `Concordance`.
Its siblings `create_map_html2` (flux, reaction-class overlays, numerical
fold-change badges) and `map_stats`/`list_available_maps` cover the flux
rendering Chris asked for separately.
A design that rebuilt these would be the failure mode the research step exists
to prevent.
**This PRD therefore builds navigation and a join key, and delegates rendering.**

**2. There is no join key between an analysis and an arc. This is the gap.**
Grepping both producer repos —

```
grep -rl 'KOROS_RUNS|koros_runs|Science/runs|arcs/' \
  KBUtilLib/src/kbutillib KBDLJobRunningPrototype/src
```

— returns **nothing**.
Neither KBDL nor KBUtilLib records project or arc on anything it produces.
`ObjectMetadata` (`kbdl_service/object_store/store.py:57`) carries
`object_id, name, object_type, visibility, owner, size_bytes, added_at,
storage_kind` and its docstring states outright that fields are deliberately
withheld and that adding one would be "a deliberate act rather than an
accident", asserted by `tests/test_objects_skani_version.py`.
So the join cannot be added there, and must be added beside the arc.

**3. KIND already supplies the arc to every session it starts.**
`king_backend/session.py:779` and `:865` inject
`KOROS_ARC = s.arc` into the spawned session's environment, and
`koros_start.py:454` reads it as the active write-target.
The value is an arc **slug**, not a path (`test_start.py:134` sets
`KOROS_ARC=proj-alice`).
This is what makes "whenever someone runs an analysis, it shows up in the
dashboard" achievable without asking the scientist to label anything —
for work run *inside a KIND session*.
It does **not** cover KBDL service-side jobs, which is why they take the arc
as an explicit parameter (Q4).

**4. Arcs are directories, not records, and their metadata is thinner than it looks.**
Layout is `<runs_root>/<project>/arcs/<slug>/` with a `PROVENANCE.json` per
arc; `king_backend/p0.py:93` also accepts a project dir that is itself an arc.
**The runs root is resolved, never assumed** — see the correction in finding 12.
A live example (`runs/genome-clearinghouse/arcs/mag-integration-arc`) carries
`run_id, run_name, created_at, created_by, init_provenance, project, role,
leg_of, parent, history`
— and `inputs: []`, `tool_versions: {}`, `compute_targets: []`, all **empty in
practice**. Do not design against them being populated.
The arc dir also contains a **`.git` directory and a `.gitignore`** — arcs are
themselves git repos, which matters for where we write (Q6, and Gotcha G4).

**5. The KIND extension contract is fixed and documented, and one of its
gotchas silently blanks the page.**
`king/docs/APP_INTEGRATION.md` gives the manifest schema
(`launch.cmd` with `{port}` `{proxy_path}` `{home}` `{koros_dir}` tokens,
`ready_probe`, `embed`, `port_strategy`, `singleton`),
the port range 8800–8899, and `_resolve_exe`'s console-entry-point resolution.
Two operational constraints are stated as defects others already hit:
**do not set `FORWARDED_ALLOW_IPS`/`UVICORN_PROXY_HEADERS`** (the hub's double
proxy sends `x-forwarded-proto: "https,http"`; trusting it rewrites asset URLs
and the iframe renders blank), and **the trailing slash on the proxy URL is
load-bearing**.
`_resolve_plugins()` in `king_backend/config.py` **replaces** rather than
unions the plugin directory, which is why the union symlink farm at
`~/kind-apps/plugins/` exists.

**6. The clearinghouse lake is not where models live.**
`clearinghouse-lake-1d-per-entity-type-tables` defines fifteen tables over five
entity types — genome, protein, gene, function, ontology_term.
There is no model or reaction entity type.
Model reconstructions and FBA/FVA results are job results and object-store
objects, not lake rows.
An app that queried Trino for this would find nothing.

**7. The two producers have different shapes and both need stamping.**
KBDL job types are
`build_genome, genome_annotation, model_reconstruction, fitness_model_analysis,
fitness_prop, skani, build_skani_db, checkm2, store_load, upload_object`.
`fitness_model_analysis` emits `gaa_data` tables
`v2_fitness_simulations`, `v2_fitness_simulation_reaction`,
`v2_fitness_simulation_genes`;
model reconstruction supplies `gaa_data.v2_reaction_fva`.
KBUtilLib's side is the `kbu model` CLI —
`reconstruct | gapfill | fba | fva | exec` — which writes plain cobra JSON
files and prints `--json` results, persisting nothing and indexing nothing.

**8. Reaction and concordance vocabularies are already fixed by the existing dashboard.**
Fitness classes are `essential / active / unused / blocked`, with
`essential-forward` and `essential-reverse` variants
(`fitness_dashboard.py:34-40`);
concordance is computed per gene as predicted-essential versus measured
(`_concordance`, `:165`), summarised into `concordance_summary`.
This PRD adopts that vocabulary rather than defining a second one.

**9. "An official app in KOROS" is a specified conformance target, not a
manifest drop — and it is a much larger target than a plugin.**
Relayed from the parallel annotation session, which was told it by Chris about
*its* app; **verified independently here against the live
`king/docs/CROSS_APP_COMMUNICATION.md` (the CAC, 353 lines) rather than taken
on report.**
An official app is a CAC-conformant peer of Function Junction, genepool and
genKnown, across three planes —
plane 1 launch/embed, plane 2 filesystem context handoff, plane 3
research-seeding to and from KOROS, which the doc calls "the load-bearing
plane; = the interop contract".

**Two corrections to the relay, both verified in the source:**

- There are **six** invariants, not four. The relay listed I1–I5 and omitted
  **I6 — isolation of authority** (§D): an embedded app's evidence access is
  its **own direct REST** and it carries **no inherited authority**; a
  third-party iframe cannot reach KING's authorized MCP servers or their
  credentials.
- **Plane 3 is opt-in, not mandatory.** §5 states the two adoption profiles
  outright: *"plane-1 is universal; plane-3 is opt-in (evidence-producing
  apps)."* The relay presented full three-plane conformance as the bar. It is
  not, and that materially reduces what this PRD must build (Q12).

The invariants that do bind this app:
**I1** — it must run with KING absent from the runtime path; the app
self-registers its own manifest on `serve`, and standalone `<app> serve` is the
I1 proof.
**I4** — one canonical **hyphen** app-id (= dist name = CLI command = manifest
basename = koros `--project` token), with the underscore form derived
mechanically as `id.replace("-","_")` for Python modules and SQL identifiers —
computed, never hand-chosen.
**I5** — `contract_version` is required in the manifest from v1. The CAC states
the gate as semver (hard-gate major, soft-warn minor) while ruling the wire
field an integer, and marks the mechanics unfilled; **we implement the half
that is implementable — integer comparison, equal proceeds, any difference
hard-fails.** See `koros-arc-store-v1` S11.
**I3** — app-to-app is **never** a direct import; it travels as a plane-3
bundle through a derived lakehouse rendezvous table
`arkinlab.<app_us>.interop_bundles`.

**10. Everything a metabolic model produces is `hypothesis` tier, and the
contract forbids presenting it as anything stronger.**
CAC §A ratifies four trust tiers — `verified` (an ID-join or crosswalk, must
carry crosswalk id and coverage), `homology` (via a *sequence or structure*
homolog, must carry the bridge metric and the accession reached through),
`hypothesis` (**"KOROS/model-generated (argued, not proven)"**, must carry the
reasoning or arc provenance), and `opinion`.
The anti-laundering test is stated explicitly: *"a lower-tier claim may never be
restated as a higher tier … a KOROS 'confirmed' is not a lakehouse
'verified'."*
A gapfilled reaction, an FBA-predicted flux and a reconstruction-inferred
pathway are all `hypothesis`.
**Experimental RB-TnSeq fitness is not** — and this app puts the two side by
side in a concordance view. Keeping them structurally distinct, with arc
provenance on every model-derived value, is therefore a **contract
requirement**, not a nicety (Q13).
Note also that `homology` is reserved for a real sequence or structure homolog;
`kbdl.fitness_prop` propagates fitness via protein co-clustering with a
percent-identity metric, which is exactly the `homology` case and must carry
its bridge metric.

**11. The external scan says build it, and corrects two of my choices.**
`task-f379e8ac` returned 128 lines (linked below). Its verdict on build-versus-buy:
*"No existing open-source tool provides the exact PROJECT → ARC → multi-model
drill-down with Escher overlays and fitness agreement as described … Building a
focused app is justified."*
MEMOTE is per-model QA, KBase Narrative is notebooks, MetaNetX is ID
harmonization — pieces, not the thing.
Two findings that changed decisions in this PRD:

- **On the record store (question D):** append-only per-directory JSONL has
  named failure modes — interleaved lines from concurrent writers, a truncated
  final object after a crash, unbounded growth, and O(n) startup — and the
  scan's threshold is *"beyond a few MB or thousands of records, switch to
  SQLite/duckdb"*, with `O_APPEND` plus file locks and periodic compaction if
  JSONL is kept. This is folded into Q1 as explicit mitigations rather than
  left to be discovered.
- **On the envelope (question C):** the scan recommends **RO-Crate** as the
  directory-level envelope and says outright *"avoid inventing a schema"*.
  Q1 weighs and **declines** it, with reasons, rather than silently ignoring
  the recommendation.
- **On Escher (question A):** embed via the JS bundle and `escher.Builder`;
  disable Escher's search (`enable_search=false`) on large maps; a sandboxed
  iframe needs **both** `allow-scripts` and `allow-same-origin` or asset loads
  and localStorage break; and avoid multi-megabyte map JSON in a single
  payload. The last two bear directly on serving generated dashboards inside
  KIND's iframe.
- **On cross-model identifiers (question B):** every prior tool harmonizes via
  MNXref or BiGG. This app does not need to — `create_fitness_dashboard`
  already *"keys purely on ModelSEED reaction ids"* and is documented
  cross-genome, so the problem is solved upstream of us.
- **On the agreement metric (question E):** published practice is a confusion
  matrix with MCC, precision and recall. The existing dashboard gives per-gene
  concordance categories and a summary but no MCC; the arc-level view adds it
  (Q14).

**12. THE RUNS ROOT IS AN ENV-RESOLVED PATH AND THE SKILL DOCUMENTING IT IS
WRONG.** Chris, 2026-09-23: *"KOROS_HOME should be used for the path to
projects.... don't assume it's Dropbox/Science/runs."* Checked against source
rather than taken on the instruction, and he is right — with a wrinkle worth
recording, because **two different variables exist and they belong to different
tools**:

- **`KOROS_HOME` — koros's own, and the one that matters here.** koros resolves
  its runs root as `--runs-root` → **`$KOROS_HOME/runs`** → the repo's `runs/`
  (`koros/skills/koros-start/koros_start.py:24-27`,
  `research_init.py:98-102`, `koros_project.py:496,509`, and
  `koros/INSTALL.md:88-92`: *"An installed koros looks for your runs under
  `$KOROS_HOME`"*). Note the shape: `KOROS_HOME` is the **workspace root**, and
  runs live at `$KOROS_HOME/runs` — it is not itself the runs directory.
- **`KING_KOROS_RUNS` — KING's override**, at `king_backend/config.py:25`:
  `KOROS_RUNS = Path(os.environ.get("KING_KOROS_RUNS", KOROS_DIR / "runs"))`.

**The `cw-kind` skill names neither correctly.** It says to "resolve via
`config.KOROS_RUNS` / `$KIND_KOROS_RUNS`", and **`KIND_KOROS_RUNS` does not
exist anywhere in the stack** — the real variable is `KING_KOROS_RUNS` (KING,
not KIND; the rename left this behind). It also gives
`~/Dropbox/Science/runs` as the pod path, which is a local fact about this
laptop, not a constant. This PRD's first draft inherited both errors from the
skill. Filed against the skill separately; **read the source, not the skill**,
for this.

### Links to full research output

- [External prior-art scan](research/external-scan.md) — run on h100 as Maestro
  `task-f379e8ac` (backend codex, cross-family). **Returned; folded.**
  Covers Escher web embedding, multi-model navigation prior art, run-index
  standards including RO-Crate, append-only JSONL failure modes, and how
  published tools present predicted-versus-measured essentiality agreement.
  Its build-versus-buy verdict and its corrections to Q1 and the Escher
  embedding path are in findings 11 above.
- [Confront round 1 stall report](research/confront-round-1-stall-report.md) —
  the cross-family adversary (codex on h100, Maestro `task-e294135c`).
  Eleven binding stall points, all folded as S1–S11 in Implementation
  Decisions and as the Acceptance Criteria list; its free critique folded as
  gotchas G16–G19 and the `inline_escher` toggle in G5.
- [Confront round 2 stall report](research/confront-round-2-stall-report.md) —
  a second round (`task-263b2f5c`) against the post-CAC document.
  Fourteen stalls, **four folded** as S12–S15 and G20. The other ten were of
  the form "I cannot change KBDL or find the CLI from this repo" — the
  adversary reading PRD text in a single checkout, not a design gap, since the
  taskplan names each task's owning repo and every prompt is self-contained.
  Recorded here rather than quietly dropped so the ratio is visible: round 1
  engaged with the design and round 2 largely engaged with its own environment.

### Quality report

**Searched, first-hand, on this machine:** the KIND/KOROS checkout
(`~/king-stack`) including `APP_INTEGRATION.md`, the shipped plugin manifests,
`session.py`, `p0.py`, `config.py`, `koros_start.py`;
KBUtilLib source including `escher_utils.py`, `fitness_dashboard.py`,
`kind_app/bundle.json` and its `skill.md`;
KBDL source including all ten job types, `object_store/store.py`,
`object_store/references.py`, and the clearinghouse module;
the live KOROS runs tree on THIS laptop (at `~/Dropbox/Science/runs`, which is this machine's `$KOROS_HOME/runs` and not a constant — finding 12) including a real
`PROVENANCE.json` and a real arc directory listing;
the KBDL PRD corpus (30 PRDs) with `kbdl-fitness-prop-v1` and
`clearinghouse-lake-1d-per-entity-type-tables` read in substance;
the AIAssistant project registry.

**Not searched, and why it matters:**

- **The pod.** Everything here was read on primary-laptop. KIND's *primary*
  environment is the kbhub BERDL pod, where the five repos sit flat in `~`,
  `KING_STATE` is `~/kind-apps`, and kbhub runs **selective** Dropbox sync that
  excludes several `Projects/*` repos. Whether the runs tree and the KBDL
  object store are reachable from the pod was the single largest hole in this
  research when it was written. **Chris confirmed on 2026-09-23 that the runs
  tree is reachable**, which closes the half that mattered; the object store
  remains unchecked and keeps its degrade path. See Q10.
- **A running KIND server.** No live `GET /api/apps` or
  `/api/projects/<p>/overview` call was made; the endpoint shapes come from
  source and docs. The manifest is written from the live
  `APP_INTEGRATION.md`, not from memory, but it has not been loaded by a real
  loader.
- **The external scan returned mid-draft and IS folded** (finding 11). Its
  build-versus-buy verdict supports building; its RO-Crate recommendation is
  weighed and declined in Q1 with reasons; its JSONL failure modes are folded
  into Q1 as mitigations; its Escher iframe-sandbox and map-size findings are
  folded into Q3 and the gotchas. Two of its recommendations were **rejected
  on local evidence** — cross-model ID harmonization (already solved upstream
  by `create_fitness_dashboard` keying on ModelSEED ids) and instantiating
  `escher.Builder` ourselves (we delegate rendering entirely).
- **The confront adversary had not returned when this text was written.**
  `task-e294135c` was dispatched against the PRE-CAC draft. Its findings will
  be folded before commit, and anything it says about the app's extension
  surface may be stale with respect to finding 9.
- **`create_fitness_dashboard` was read, not run.** Its docstring and builder
  were read closely; no dashboard was generated from live inputs during this
  session, so the claim that its five tabs match Chris's ask rests on source
  reading.

- **The CAC's own unfilled seams.** The doc carries explicit
  `⟨fill: …⟩` markers on the things this app would most like specified: how a
  launched app obtains the user's ORCID (§C), the id-normalization helper (§E),
  and the `contract_version` gate mechanics (§F). Those are open on KING's
  side, not answerable by reading, and are recorded as cross-team dependencies
  rather than invented here.

**Confidence the design should carry:** high on the *shape* — four levels,
delegate the bottom two, stamp records on arcs — because every load-bearing
claim above is from source read this session, and the external scan
independently found no tool that already does it.
Medium on the *record schema* (Q1) and on *how much CAC conformance is owed*
(Q12), the latter because the instruction reached this session by relay about a
sibling app rather than directly.
Low on **pod data reachability**, which is unverified and could force the whole
data-access layer to change.

## Revision Log

- **Round 2 — 2026-09-24 (reconciliation, not a review round).** Chris chose
  EXTRACTION over merging, which is the recommendation this PRD made in round 1.
  `kbutillib.koros_arc_store` is now built by its own PRD,
  **`koros-arc-store-v1`** (owning repo KBUtilLib), rather than by
  `kind-annotation-results-explorer-v1`. Nothing this PRD builds changed; what
  changed is who the dependency points at, and that the app tests now import a
  `FakeKorosArcStore` **shipped from that module** instead of writing their own.
  Q2 rewritten. `review_rounds` deliberately NOT incremented: this propagated a
  structural decision, it did not fold Chris's in-document edits.

- **Round 1 — 2026-09-23 (review).** Chris's only review instruction was to
  reconcile this PRD against `kind-annotation-results-explorer-v1` and consider
  merging them. Added the **Conformance contract** section: adopted four things
  from that PRD (the trust-tier floor rule, write-time `provenance`
  enforcement, the stored-and-flagged unknown-kind policy, and their full-tree
  census in place of this PRD's single-arc sample), and recorded three defects
  in their shared scaffold that land on code this PRD's tasks call. Q13 and Q14
  amended so the arc-level MCC carries a floor tier. **Recommendation on
  merging is in Further Notes: do not merge the two app PRDs; extract the
  shared scaffold into its own.**

- **Round 0 — 2026-09-23 (draft).** Single-pass draft.
  Written alongside `kind-annotation-results-explorer-v1`, which at the time
  owned and built the shared project/arc layer this PRD consumes. That layer
  moved to `koros-arc-store-v1` on 2026-09-24; see round 2.
  The central finding is that levels 3 and 4 already exist in `EscherUtils`,
  which moved the PRD's centre of gravity from rendering to the missing
  analysis-to-arc join.
  Mid-draft, two things arrived and were folded before commit: the external
  prior-art scan (build-versus-buy confirmed, RO-Crate weighed and declined,
  JSONL mitigations and Escher iframe constraints adopted), and the
  instruction — relayed, then verified against the live CAC — that this class
  of app is an *official KOROS app*. The latter added CAC conformance at the
  plane-1/plane-2 profile, the canonical-id derivation, `contract_version`
  gating, and trust tiers on every record.
  Then, before commit, **Chris replaced the storage backend**: a per-user local
  database in the pod home directory written by KBUtilLib's KBDL client
  utilities, with a two-tier schema, in place of the per-arc sidecar. Q1 was
  rewritten; Q6, S8, S14, G4 and G18 were withdrawn; `read_detail` joined the
  interface; and G21–G23 were added for the consequences the two-tier split
  carries. Two confront rounds ran and were folded (S1–S15).

## Problem Statement

Chris builds genome-scale metabolic models with KBDL and analyses them both
with KBDL's own analysis job types and with KBUtilLib's `kbu model` verbs.
The work happens inside KOROS research arcs, which are grouped into projects,
and it happens across many arcs concurrently.

Once a run finishes, its output is unreachable except by remembering where it
went.
A model is a cobra JSON file on disk or an object id in the KBDL object store.
An FBA result is JSON that was printed to a terminal.
A fitness analysis is a `gaa_data` block inside a job result.
None of these records the arc or the project it belongs to — confirmed by
grep across both producer repos, which returns zero arc-aware references.

The consequence is that the question a scientist actually asks —
*what modelling have I done in this project, what did it show, and where does
it disagree with the experimental data* —
cannot be answered by any tool.
It can only be answered by recalling which directories to open.

The arcs already carry identity, lineage and history.
The analyses already carry rich results.
The two have never been connected, and that missing connection, not a missing
renderer, is the actual problem.

## Solution

A KIND app, **Models and Analyses**, presenting four levels.

It is built as a **CAC-conformant app at the plane-1 + plane-2 profile**, which
the contract calls universal, and **not** at the plane-3 profile, which §5
states is opt-in for evidence-producing apps (Q12).
Concretely that means: it runs standalone with KING absent (I1) and
self-registers its own manifest on `serve`; its id is canonical hyphen form
with the underscore form derived mechanically (I4); it declares
`contract_version` (I5); it reaches its evidence over its own REST and inherits
no authority from KING (I6); and every model-derived value it displays carries
`hypothesis` tier with arc provenance, never presented as level with
experimental data (I2, Q13).

**Level 0 — Portfolio.**
Every project visible to the user, every arc within it, each row carrying
counts of the modelling work recorded there —
models built, gapfills, FBA and FVA runs, fitness analyses —
the arc's `role` and lineage (a leg is shown under its parent), and the
timestamp of the most recent analysis.
Arcs with no modelling work are shown greyed rather than hidden, because their
absence is itself information.

**Level 1 — Arc.**
A table of the models analysed in that arc, one row per model:
model id, the genome or subject it was built from, template, reaction and gene
counts, the gapfill media if any, the set of analyses run against it, and when
each last ran.
Selecting a row opens level 2.

**Level 2 — Model.**
Genes and reactions with annotations, reaction classifications and fitness
agreement.
**Delegated to `EscherUtils.create_fitness_dashboard`**, which already produces
precisely this as a self-contained HTML page with five tabs and an Escher map
recoloured by fitness class.
The app generates it on demand from the recorded artifact references and serves
it in a nested frame.

**Level 3 — Escher / flux.**
The model drawn on a metabolic map with flux.
**Delegated to `EscherUtils.create_map_html2`**, with the map chosen from
`list_available_maps(model)` and flux taken from the selected FBA or FVA
record.

The new engineering is therefore not the views. It is the join.

**The join: analysis records stamped onto arcs.**

A producer, when it completes, writes one `AnalysisRecord` into the arc it ran
in.
The record is an envelope — identity, kind, time, producer, subject, status,
and named references to artifacts — plus an opaque `payload` the domain app
interprets.
It never inlines results.

The arc is resolved, in order: an explicit argument; the `KOROS_ARC`
environment variable that KIND sets on every session it starts; inference from
the working directory when it lies under the runs root.
If none resolves, the producer records nothing and says so, rather than
guessing an arc.

Records are read by a small shared library, `KorosArcStore`, which also resolves the
runs root across the pod and laptop layouts and enumerates projects and arcs
from `PROVENANCE.json`.
**That library is built by `koros-arc-store-v1`, not by this PRD and not by
the annotation app either.** Both apps declare a hard dependency on it and
build against its interface; neither creates it. The two apps share the whole left-hand navigation and the record
store, and share no science code at all.

## User Stories

1. As a modeller, I want to open one KIND app and see every project I have
   modelling work in, so that I do not have to remember which arcs contain
   models.
2. As a modeller, I want each project row to show how many models and analyses
   it contains, so that I can tell an active project from a dormant one at a
   glance.
3. As a modeller, I want arcs that are legs of another arc to appear nested
   under their parent, so that the view matches the lineage KOROS already
   records.
4. As a modeller, I want to see arcs that contain no modelling work, so that I
   notice when an arc I expected to have results does not.
5. As a modeller, I want to drill from an arc into a table of the models
   analysed in it, so that I can compare several models from the same study.
6. As a modeller, I want each model row to show the genome it came from, its
   template, and its reaction and gene counts, so that I can spot an
   anomalously small or large reconstruction immediately.
7. As a modeller, I want each model row to list which analyses were run against
   it and when, so that I can see what is missing without opening the model.
8. As a modeller, I want to drill from a model into its genes with their
   annotations, so that I can check what function was called for a gene that
   behaves oddly.
9. As a modeller, I want to see reactions with their classifications, so that I
   can tell essential from active from blocked reactions.
10. As a modeller, I want to see fitness agreement per gene — predicted
    essentiality against measured RB-TnSeq fitness — so that I can find where
    the model and the experiment disagree.
11. As a modeller, I want to switch conditions and FVA solutions within the
    model view, so that I can see how classification changes with media.
12. As a modeller, I want the model and its flux drawn on an Escher map, so
    that I can read the result as pathways rather than as a table.
13. As a modeller, I want to choose which Escher map a model is drawn on, so
    that I can use a core map for a quick look and a global map for detail.
14. As a modeller, I want a model I build with KBDL inside a KIND session to
    appear in the app without my labelling it, so that the index stays true
    with no discipline required of me.
15. As a modeller, I want an analysis I run with `kbu model fba` in a KIND
    session to appear against the model it was run on, so that the model row
    accumulates its analyses.
16. As a modeller running KBDL jobs from a script outside KIND, I want to pass
    the arc explicitly, so that scripted work is indexed the same as
    interactive work.
17. As a modeller, I want work I did before this app existed to be findable,
    so that the app is useful on day one rather than only for future runs.
18. As a modeller, I want backfilled records to be visibly marked as inferred,
    so that I do not mistake a guess for a recorded fact.
19. As a modeller, I want an analysis that failed to appear with its failure,
    so that the index tells me what I tried, not only what worked.
20. As a modeller, I want to open the app from the KIND nav like any other
    capability, so that it is part of the environment rather than a separate
    tool I must launch.
21. As a scientist sharing a project, I want to see only arcs and objects I am
    permitted to read, so that the app does not expose a colleague's private
    work.
22. As a modeller, I want the app to work identically on the pod and on my
    laptop, so that I do not learn two tools.
23. As a maintainer, I want the app to write nothing into the five KIND repos,
    so that the consume-only interlock holds by construction.
24. As a maintainer, I want the record store to be a plain file I can read with
    `cat`, so that diagnosing a missing analysis needs no service.
25. As a maintainer, I want re-recording the same analysis to replace rather
    than duplicate its record, so that a re-run does not inflate the counts.
26. As the annotation explorer, I want my records and the modelling records to
    live in the same per-arc index under distinct namespaced kinds, so that one
    arc shows both kinds of work and neither app has to know about the other.
27. As a scientist, I want model predictions marked as predictions wherever
    they sit beside experimental data, so that I never mistake an FBA result
    for a measurement.
28. As a scientist, I want propagated fitness distinguished from measured
    fitness, with the percent identity it was propagated through, so that I can
    judge how far the evidence travelled.
29. As a scientist, I want a single agreement number per model — a confusion
    matrix with MCC — so that I can rank several models in an arc by how well
    they match the experiment.
30. As a scientist away from KIND, I want to run the app on its own and still
    browse my arcs, so that the tool is mine rather than the environment's.
31. As a maintainer, I want the app to refuse to start against an incompatible
    contract version rather than render data it may be misreading, so that a
    breaking change fails loudly instead of quietly.
32. As a maintainer, I want the app's id to exist in exactly one place with
    every other spelling derived from it, so that renaming it is one edit
    rather than a hunt.

## Implementation Decisions

### The consumed shared layer (built elsewhere — do not build it here)

`kbutillib.koros_arc_store`, owned and built by `koros-arc-store-v1`
(KBUtilLib), together with `kbutillib.koros_arc_store_testing`, which ships the
`FakeKorosArcStore` this PRD's app tests import rather than writing their own.
This PRD's tasks import both and must create neither.
The interface this PRD is written against:

```python
def resolve_runs_root() -> Path: ...
    # explicit --runs-root, then $KOROS_HOME/runs, then $KING_KOROS_RUNS.
    # RAISES rather than guessing. NO hardcoded path, ever.

class KorosArcStore:
    def list_projects(self) -> list[ProjectRecord]: ...
    def list_arcs(self, project: str) -> list[ArcRecord]: ...
    def read_arc(self, project: str, arc: str) -> ArcRecord: ...
        # PROVENANCE.json parsed; lineage, leg_of, parent, role included.
    def record_analysis(self, project: str, arc: str, rec: AnalysisRecord) -> None: ...
        # append; idempotent on record_id (re-record replaces, never duplicates)
    def list_analyses(self, project: str, arc: str,
                      kind: str | None = None) -> list[AnalysisRecord]: ...
    def read_detail(self, record_id: str) -> dict: ...
        # THE ONLY call that opens a JSON blob. Levels 0 and 1 never call it.

@dataclass
class AnalysisRecord:
    record_id: str          # stable, producer-generated
    kind: str               # NAMESPACED: 'kbdl.model_build', 'kbutillib.fba', ...
    created_at: str         # ISO8601 UTC
    producer: str           # tool name + version
    subject: str            # what it ran on: genome id, model id
    status: str             # ok | failed | partial -- THE JOB, not the evidence
    trust_tier: str         # verified | homology | hypothesis | opinion (CAC §A)
    provenance: dict        # the bridge kind + its metric; REQUIRED for any
                            # tier other than `verified` (CAC D125)
    contract_version: int   # CAC I5
    artifacts: dict[str, str]   # named refs; never inlined data
    payload: dict           # opaque to the shared layer
```

**`status` and `trust_tier` are different questions and the record needs
both.** `status` says whether the *job* succeeded; `trust_tier` says how strong
the resulting *evidence* is. A gapfilled reaction from a cleanly-completed
reconstruction is `status: "ok"` and `trust_tier: "hypothesis"` — argued, not
proven. Collapsing the two is exactly the laundering I2 forbids, and `status`
is where that collapse happens if the record has nowhere else to put it.

**Storage is a PER-USER LOCAL DATABASE in the pod home directory**, written
automatically by KBUtilLib's KBDL client utilities — on by default on pods,
configurable elsewhere. It is **not** a sidecar in the arc directory, and this
app never writes into the runs tree at all.

Chris, 2026-09-23: *"I like the idea of kbdl client creating a local database
(optionally configurable with PODs having it on by default) and keeping enough
data in the database to make the dashboard for genome annotations **and
models** easy to load BUT does NOT have TOO many rows (meaning genome gene
level and **reaction level** data should be JSON blobs rather than SQL
tables)."*

**ONE database serves both this app and the annotation explorer.** The
instruction names both dashboards and names reaction-level data, which is this
PRD's domain rather than the annotation PRD's.

**THE TWO-TIER SCHEMA IS THE LOAD-BEARING PART.**

- **SQL columns carry only NAVIGABLE DIMENSIONS** — run, project, arc, subject,
  kind, producer, status, `trust_tier`, timestamps, counts, and **precomputed
  summary statistics**. Everything levels 0 and 1 render comes from columns, so
  the portfolio and the arc table never open a blob.
- **JSON blobs carry gene-level and reaction-level detail, ONE BLOB PER
  SUBJECT.** A genome-scale model is thousands of reactions, and flux across
  conditions multiplies that; as rows it would swamp the row budget the
  instruction sets. `read_detail(record_id)` is the **only** call that opens a
  blob, and only level 2 and level 3 call it.

`read_detail(record_id) -> dict` is therefore added to the interface.

**Records with no arc are a FIRST-CLASS STATE, not an error.**
`record_analysis` accepts a null project and arc; `list_analyses` exposes those
rows behind an explicit selector. This matters because until every producer
adopts attribution, *most* records have no arc — the grep in finding 2 is the
measure of that — and an implementation that drops them makes the dashboards
look correct while being empty for the wrong reason.

**Per-writer sharding is WITHDRAWN.** It existed only because the runs tree is
Dropbox-synced and Dropbox resolves concurrent appends with a silent conflicted
copy. The pod home directory is not Dropbox-synced, so the hazard is gone and
so is the fix. Ordinary database write semantics replace it. This is recorded
rather than deleted because the reasoning is still correct for anything that
does live in the runs tree.

**This interface is the annotation PRD's, committed at `8a2d8ab`, and this PRD
conforms to it.** It was agreed across two exchanges: this session supplied the
write side and the namespaced-kind convention; that session supplied
`trust_tier`/`provenance`/`contract_version`, the per-writer sharding, and the
unattributed state. The seam — an opaque `payload` under a namespaced `kind` —
is the part neither side may move.

### Kinds this PRD owns

This app reads and writes exactly these `kind` values and ignores every other
prefix it finds:

| kind | producer | subject | payload carries |
|---|---|---|---|
| `kbdl.model_build` | KBDL `model_reconstruction` | genome id | template, reaction/gene counts, atp-safe flag, object ref to the cobra JSON |
| `kbdl.fitness_analysis` | KBDL `fitness_model_analysis` | model id | condition list, object refs to the `gaa_data` tables |
| `kbdl.fitness_prop` | KBDL `fitness_prop` | genome id | condition list, ref to the propagated fitness table |
| `kbutillib.reconstruct` | `kbu model reconstruct` | genome path/id | template, model file path, counts |
| `kbutillib.gapfill` | `kbu model gapfill` | model id | media, reactions added |
| `kbutillib.fba` | `kbu model fba` | model id | media, objective, objective value, flux artifact ref |
| `kbutillib.fva` | `kbu model fva` | model id | media, fraction-of-optimum, FVA artifact ref |

`kbdl.annotation` and `kbdl.skani` are the annotation explorer's and are
**read but not rendered** by this app — they contribute to the level-0
"other work in this arc" count only.

### Arc resolution (the producer side)

One helper, used by every producer:

```python
def resolve_current_arc(explicit: str | None = None) -> tuple[str, str] | None:
    """Return (project, arc) or None. Never guesses."""
```

Order: `explicit` → `KOROS_ARC` env var (KIND sets it; the value is a **slug**,
so the project is read from the matching arc's `PROVENANCE.json` `project`
field) → the working directory if it lies under `resolve_runs_root()` →
`None`.

On `None`, the producer completes normally and emits a single line on stderr
saying the result was not indexed and why.
It never fails the analysis, and it never picks an arc.

### Producer stamping — KBUtilLib

`kbu model reconstruct|gapfill|fba|fva` each gain an optional `--arc SLUG` and
call `record_analysis` after a successful or failed run.
This is additive: no existing flag changes meaning, and stdout `--json` output
is unchanged so existing scripted callers are unaffected.
`kbu model exec` does **not** stamp — it is an escape hatch whose output shape
is unknown by definition.

### Producer stamping — KBDL

KBDL jobs run **server-side, where `KOROS_ARC` does not exist**.
The arc therefore travels as an explicit job parameter.
`JobContext` gains optional `project` and `arc` fields; the client sets them
from `resolve_current_arc()` at submit time; the job stamps its record on
completion.
A job submitted without them runs exactly as it does today and is not indexed.

### The app

A standalone web app with its own console entry point,
`models-and-analyses`, declared in KBUtilLib's `pyproject.toml` so it resolves via
`_resolve_exe` whether KBUtilLib is a checkout, an editable install or a pipx
install.
It takes a port and a root path:

```
models-and-analyses serve {port} --root-path {proxy_path}
```

FastAPI serving a small JSON API plus generated HTML.
It **must not** set `FORWARDED_ALLOW_IPS` or `UVICORN_PROXY_HEADERS`
(see Gotcha G1).

API surface:

```
GET /api/portfolio                      -> projects[] with arcs[] and counts
GET /api/arcs/{project}/{arc}/models    -> model rows for the arc table
GET /api/models/{record_id}/dashboard   -> generates and returns level-2 HTML
GET /api/models/{record_id}/escher?map= -> generates and returns level-3 HTML
GET /api/maps?model={record_id}         -> available Escher maps for the model
```

The two generating endpoints call `create_fitness_dashboard` and
`create_map_html2` respectively, cache the result on disk under the app's own
state directory keyed by `record_id` plus the input artifact mtimes, and serve
it.
Generation is synchronous and can take seconds; the endpoints stream a
placeholder with a poll rather than holding an open request.

### CAC conformance — identity, versioning and registration

**The canonical id is `models-and-analyses`**, hyphen form, and it is
simultaneously the distribution name, the console command, the manifest
basename and the koros `--project` token (I4).
The underscore form is **derived, never written down**:
`app_us = APP_ID.replace("-", "_")`, used for the Python module and any SQL or
lakehouse identifier.
A test asserts the derivation rather than a hardcoded literal, because a
hand-chosen second spelling is exactly what I4 exists to prevent.

The console entry point is therefore named for the id itself, so that
`launch.cmd[0]` is just the id — not a tool-prefixed name like `kbu-models-app`,
which would make the command and the manifest basename two different strings
and break I4.

**The app self-registers (I1).** On `serve`, the app writes its own manifest
into the plugin directory via a `king_register()` step, and
`models-and-analyses serve --no-king` runs it with registration skipped.
Standalone `serve` with no KING present is the I1 proof and is a test, not a
claim.

The manifest:

```json
{
  "type": "app",
  "id": "models-and-analyses",
  "title": "Models and Analyses",
  "description": "Metabolic models and their analyses across your projects and arcs.",
  "contract_version": 1,
  "launch": {
    "cmd": ["models-and-analyses", "serve", "{port}", "--root-path", "{proxy_path}"],
    "ready_probe": {"path": "/", "timeout_s": 45}
  },
  "embed": "iframe",
  "port_strategy": "allocate",
  "singleton": true
}
```

It is written as a **real file** to `~/kind-apps/plugins/models-and-analyses.json`
— never into `king/plugins/`.
`contract_version` is compared against KING's on startup. §F states the gate as
semver — hard-gate major, soft-warn minor — but §4 rules the wire field an
**integer** (`:141`, and the DDL at `:173`/`:252`) and §F itself leaves the gate
mechanics as an unfilled placeholder (`:294`). **With an integer on the wire
there is nothing to compare a minor against, so we implement integer comparison
only: equal proceeds, any difference hard-fails with an error naming both
versions, and no warn branch is written.** Corrected 2026-09-24; see
`koros-arc-store-v1` S11. Failing loud is the required behaviour either way —
§F's rationale is that silently misreading a peer's evidence across a breaking
gap is the laundering hazard I2 forbids.

Registration also runs the plugin-union symlink farm so KIND's own shipped
plugins are not hidden (`_resolve_plugins` replaces rather than unions).

**I6 — no inherited authority.** The app reaches everything it displays through
its own filesystem and REST access. It must never attempt to use KING's
authorized MCP servers or their credentials, and it must not assume it can.

### Trust tiers on every record (I2)

`AnalysisRecord.payload` carries two fields on every record this app writes or
reads:

- `trust_tier` — one of `verified | homology | hypothesis | opinion`.
- `provenance` — the evidence the tier requires.

The assignment is fixed, not per-record judgement:

| record kind | tier | `provenance` must carry |
|---|---|---|
| `kbdl.model_build` | `hypothesis` | template, the arc, the reconstruction's own provenance |
| `kbutillib.reconstruct` | `hypothesis` | same |
| `kbutillib.gapfill` | `hypothesis` | media and the gapfill objective — a gapfilled reaction is argued, not observed |
| `kbutillib.fba` / `kbutillib.fva` | `hypothesis` | media, objective, the model record it derives from |
| `kbdl.fitness_analysis` | `hypothesis` | conditions, the model record it derives from |
| `kbdl.fitness_prop` | `homology` | the bridge metric (percent identity) and the accession reached through — propagation via protein co-clustering is a *sequence* homolog, which is precisely what `homology` is reserved for |
| measured RB-TnSeq fitness, where present | not this app's to tier | carried through unchanged with its own provenance |

**The anti-laundering rule in the UI:** the concordance view puts predicted
essentiality beside measured fitness. Predicted values are model-generated and
are rendered as `hypothesis` with arc provenance reachable from the row;
measured values are not restated at a higher tier and are never merged into a
single undifferentiated "agreement" number without both tiers visible.
A `homology`-tier propagated fitness value is distinguished from a measured
one — they are *not* the same evidence, and showing them identically is the I2
category error.

### Backfill

`kbu arc backfill [--project P] [--arc A] [--dry-run]`
walks the runs tree and the KBDL object store and writes records for work that
predates stamping.
Every backfilled record carries `payload.provenance = "inferred"` and the app
marks those rows visibly.
Backfill is idempotent on `record_id` and is never run automatically.

### Conformance contract with kind-annotation-results-explorer-v1

Both PRDs build on ONE shared module and ONE shared database. This section is
the reconciliation, performed against that PRD as committed at `2f35541`
(review round 1) on 2026-09-23. **Where the two disagree, this section says
which side is right and why** — a disagreement about a shared store is the
expensive kind, and leaving it implicit is how two apps corrupt one database.

**AGREED, and identical in both documents** — the class `KorosArcStore` in
`kbutillib.koros_arc_store`; the methods `list_projects`, `list_arcs`,
`read_arc`, `record_analysis`, `list_analyses`, `read_detail`; the
`AnalysisRecord` field set including `trust_tier`, `provenance` and
`contract_version`; namespaced `kind`; `record_analysis` idempotent on
`record_id`; the per-user local database; and the two-tier schema with
`read_detail` as the only blob reader.

**ADOPTED FROM THEIRS — their spec is better than mine was and this PRD now
defers to it:**

- **The FLOOR rule.** A record summarizing several findings carries the
  **least-trusted** contributing tier. Adding lower-tier findings lowers the
  floor; adding higher-tier findings **never** raises it. The shared layer
  provides the floor helper and **exposes no API that raises a tier**. This
  matters here specifically: the arc-level MCC (Q14/S13) summarizes many
  records, so **it carries a floor tier**, and a model-versus-experiment
  statistic computed over `hypothesis`-tier predictions cannot be presented at
  a higher tier than its inputs.
- **Write-time enforcement of `provenance`.** Any tier other than `verified`
  requires a non-empty `provenance` dict; the store **rejects** a write that
  omits it. Stronger than this PRD's original "must carry", which was a
  convention rather than a gate.
- **The unknown-kind policy** (imitated from
  `kbdl_service.clearinghouse.result_types`): an unrecognized `kind` is
  **stored and flagged, never refused**; only structural validation rejects.
  S11 already required foreign kinds to be *counted* on read; this adds the
  write side, and together they mean neither app can stall the other.
- **Their census beats my sample.** Finding 4 above rests on one arc. They
  measured the whole tree: **9 project directories, 8 with an `arcs/`
  subdirectory, 26 arcs, and `inputs` empty and `tool_versions` empty on ALL
  26.** The empty case is the common case, not an anomaly. Also: **a project
  directory with no `arcs/` subdirectory must enumerate as a project with zero
  arcs and never raise** — one of the nine live projects is exactly that.

**THREE DEFECTS IN THEIR `p0-shared-scaffold`, each of which lands on code this
PRD's tasks call.** Raised with that session; recorded here so the build does
not inherit them silently if the fix does not land first.

1. **The runs-root resolution is wrong, and it is the error Chris personally
   corrected in this PRD.** Their p0 says *"honor `KIND_KOROS_RUNS` first, then
   a configured setting, then default to `$HOME/Dropbox/Science/runs`"*.
   `KIND_KOROS_RUNS` **does not exist anywhere in the stack** (finding 12), the
   real names are `KOROS_HOME` (as `$KOROS_HOME/runs`) and `KING_KOROS_RUNS`,
   and the Dropbox path is one machine's layout rather than a constant. Both
   documents inherited this from the `cw-kind` skill; this one is fixed and
   theirs is not yet. **Blocking for the shared module** — a scaffold that
   resolves the runs root wrongly makes every level-0 view empty on any machine
   whose environment differs.
2. **No canonical `record_id` algorithm.** Theirs says only "producer-generated".
   This PRD pins it (S1: sha256 over lowercased kind, lowercased subject and a
   canonical sorted-key JSON of the significant parameters). **Two producers
   writing one database with different derivations produce duplicate rows for
   the same logical analysis**, and the idempotency both documents promise
   silently stops holding. The algorithm must live in the shared layer, not in
   each producer.
3. **No artifact URI scheme.** Theirs says "named refs to files/objects"; this
   PRD pins bare absolute path, `file://`, or `obj://<object_id>` (S2). Without
   one vocabulary, each app writes refs the other cannot resolve — which
   matters the first time one arc carries both kinds of work.

**WHAT THIS PRD DOES ABOUT THEM.** It does not work around them. Its tasks
declare the shared module a hard precondition and stop if it is absent; if it
is present but resolves the runs root from a non-existent environment variable,
that is a defect to fix in the shared module, not to paper over here. The
`p1-arc-resolver` prompt carries the correct precedence and an explicit warning
not to reintroduce `KIND_KOROS_RUNS`.

### Specifications forced by confront round 1

The cross-family adversary stalled on eleven underspecifications. Each is
resolved here; these are binding, not advisory.

**S1. `record_id` algorithm.**
`record_id = sha256( lower(kind) || "\0" || lower(subject) || "\0" ||
canonical_json(significant_params) ).hexdigest()`, where `canonical_json` sorts
keys and uses compact separators. Significant params per kind: template
(reconstruct/model_build); media (gapfill); media + objective (fba); media +
fraction-of-optimum (fva); the condition list (fitness analyses). Truncate to
32 hex chars for display only, never for identity. Without one algorithm,
idempotency is undefined and two producers collide.

**S2. `artifacts` keys and URI schemes, per kind.**
Allowed schemes: a bare absolute path or `file://` for files; `obj://<object_id>`
for the KBDL object store. Exact keys:

| kind | keys |
|---|---|
| `kbdl.model_build` | `model_id`, `model` (cobra JSON), `reaction_fva` if present |
| `kbutillib.reconstruct` | `model_id`, `model_path` |
| `kbutillib.gapfill` | `model_id`, `model_path` |
| `kbutillib.fba` | `model_id`, `model_path`, `flux_path` |
| `kbutillib.fva` | `model_id`, `model_path`, `fva_path` |
| `kbdl.fitness_analysis` | `model_id`, `fitness_sim_reactions`, `fitness_sim_genes`, `fitness_sims` |
| `kbdl.fitness_prop` | `propagated_fitness` |

**S3. Canonical model identity and grouping.**
`artifacts.model_id = "model:<ns>:<id>"` with `ns ∈ {kbdl, file}`.
For `kbdl.model_build` the id is the KBDL object id; for the `kbu model` verbs
it is the absolute, normalized path of the cobra JSON. **The arc table groups
strictly by `artifacts.model_id`** — never by subject string, which drifts
between producers.

**S4. What `{record_id}` means on the dashboard endpoint.**
`GET /api/models/{record_id}/dashboard` takes a **`kbdl.fitness_analysis`**
record. The paired model is the most recent `kbdl.model_build` with the same
`artifacts.model_id` in the same arc. If none exists, return **404
`model-not-found`** — `create_fitness_dashboard` needs both halves and must not
be called with a guess.

**S5. Default Escher map.**
`modelseed_core`, else `modelseed_global`, else the first entry from
`list_available_maps(model)`. If the list is empty, return **404 `no-map`**.
A non-deterministic default is untestable.

**S6. Cache location.**
`$KING_STATE/kind-apps/state/models-and-analyses` when `KING_STATE` is set,
else `$HOME/kind-apps/state/models-and-analyses`, else a per-user state dir.
Never inside the five KIND repos and never inside the runs tree.

**S7. Arc slug scope.**
Slugs are unique **per project**, not globally. `--arc` accepts
`PROJECT/SLUG`; given a bare `SLUG` that matches in more than one project, the
producer **refuses to stamp** and prints a disambiguation error. Guessing here
would mis-file records, which Q5's reasoning already rules out.

**S8. Replacement semantics — WITHDRAWN and replaced by Q1.**
This specified strictly-append-only shards with read-time dedup. With the store
now a per-user database, `record_analysis` is an upsert keyed on `record_id`
and `latest_only` is no longer a scan-from-the-end view. The *requirement* is
unchanged and still tested: re-recording the same logical analysis leaves
exactly one row, never two.

**S9. Backfill scan strategy.**
Walk `<runs_root>/*/arcs/*/` for `*.model.json`, `fba/*.json`, `fva/*.json`.
Where the KBDL object store is reachable, additionally list by `object_type ∈
{model, fba, fva}` filtered by owner. **Prefer file artifacts; use store refs
only when no file is present.**

**S10. Manifest install path.**
`$KING_STATE/kind-apps/plugins` when `KING_STATE` is set, else
`$HOME/kind-apps/plugins`, then refresh the union symlink farm. Never
`king/plugins/`.

**S11. Foreign records are COUNTED, never silently dropped.**
Every read of an arc returns records this app cannot interpret — the annotation
app's `kbdl.annotation` and `kbdl.skani` — and that is normal, not exceptional.
The prefix filter therefore **counts** what it excludes and surfaces the count.
A dropped foreign record makes a filter bug look like missing data; a counted
one is debuggable. A missing prefix filter must be a **test failure**, not a
display quirk.

### Specifications forced by confront round 2

Round 2 attacked the post-CAC document. Most of its fourteen stalls were of the
form "I cannot change KBDL / find the CLI / call `EscherUtils` from this repo",
which is the adversary running against the PRD text in a single checkout rather
than a genuine underspecification — the taskplan already names each task's
owning repo and every prompt is self-contained. Four points were real and are
resolved here.

**S12. Normative `payload` schema per kind.**
S2 pinned `artifacts` keys and left `payload` loose, which is a drift risk
across two producers. Payload keys, exactly:

| kind | payload keys |
|---|---|
| `kbdl.model_build`, `kbutillib.reconstruct` | `template`, `n_reactions`, `n_genes`, `atp_safe` |
| `kbutillib.gapfill` | `media`, `objective`, `reactions_added` |
| `kbutillib.fba` | `media`, `objective`, `objective_value` |
| `kbutillib.fva` | `media`, `fraction_of_optimum` |
| `kbdl.fitness_analysis` | `conditions` (list), `n_simulations` |
| `kbdl.fitness_prop` | `conditions` (list), `n_propagated` |

Plus, on every kind: `provenance` (as the record field, not duplicated here)
and, on backfilled records only, `provenance: "inferred"` — see G20.
A writer emitting a key outside its kind's set, or omitting one, fails
validation rather than writing a record the reader cannot interpret.

**S13. The concordance-to-confusion-matrix mapping.**
Q14 asked for MCC without saying what goes into it, which would have produced
two incompatible metrics. The mapping, stated once:
a gene is **predicted essential** when its reference reaction class from
`fitness_dashboard.py` starts with `essential` (which covers
`essential-forward` and `essential-reverse` — see `ess_rxns`, `:163`);
it is **observed essential** when its measured RB-TnSeq fitness is below the
essentiality threshold.
TP = predicted and observed; FP = predicted, not observed; FN = observed, not
predicted; TN = neither.
`MCC = (TP·TN − FP·FN) / sqrt((TP+FP)(TP+FN)(TN+FP)(TN+FN))`, and **when the
denominator is zero MCC is reported as `null`, never as 0** — a zero would read
as "no correlation" when the truth is "not computable".
**Genes with no measured fitness are excluded from the matrix entirely**, and
the excluded count is reported beside it; folding them in as true negatives
would inflate every model's score with genes nobody measured.
Propagated (`homology`-tier) fitness is **not** substituted for measured
fitness in this matrix — that substitution is the laundering G15 names.

**S14. The `.gitignore` line — WITHDRAWN with Q6.**
Nothing is written into the arc directory, so no `.gitignore` is touched.

**S15. API response schemas are part of the contract, not an implementation
detail.** Each endpoint's JSON shape is fixed in the task prompt and a schema
test asserts it, because the app's own frontend and any later client both bind
to it. The generating endpoints return `202` with a poll token while
generation runs and `200` with the HTML when ready; a poll of an unknown token
is `404`, and a generation that failed is `500` carrying the failing artifact's
name (G17), never an empty `200`.

### Deep module boundary

`KorosArcStore` is the deep module: a large amount of filesystem, layout and
lineage behaviour behind five methods and one dataclass.
The app above it holds no filesystem knowledge, and the producers below it hold
no layout knowledge.
The `payload` field is the seam that keeps two domain apps out of each other's
schemas.

## Testing Decisions

**What a good test is here:** one that drives the external behaviour —
a producer runs, a record appears; the API returns rows for an arc that has
records and an empty list for one that does not; the manifest loads in KIND's
own loader.
Tests that assert on the shape of generated HTML are testing
`create_fitness_dashboard`, which is not this PRD's code, and are not written.

**Modules tested:**

- `resolve_current_arc` — the full precedence chain, including the
  `None` case, with `KOROS_ARC` set and unset, and with a cwd inside and
  outside the runs root. This is the function whose silent misbehaviour would
  put records on the wrong arc, so it gets the densest tests.
- The producer stamping in `kbu model` — that a run with a resolvable arc
  writes exactly one record, that a re-run replaces rather than duplicates it,
  that a failed run writes a record with `status: "failed"`, and that an
  unresolvable arc writes nothing and leaves stdout `--json` byte-identical to
  today's.
- The KBDL `JobContext` extension — that a job submitted without
  `project`/`arc` behaves exactly as before.
- The app's JSON endpoints against a synthetic runs tree — portfolio counts,
  arc model rows, leg nesting, and the empty-arc case.
- The manifest — loaded through KIND's own `plugins.py` loader rather than
  compared against a hand-written expectation, so a schema drift in KIND is
  caught rather than mirrored.
- **I1, as a test and not a claim** — start the app with KING absent and no
  KING environment, and get a working `/api/portfolio`. The contract's whole
  decoupling invariant is unfalsifiable if it lives in a docstring.
- **I4, as a derivation** — assert `app_us() == APP_ID.replace("-","_")`, not
  the literal `models_and_analyses`. A test asserting the literal would pass
  while permitting exactly the hand-chosen second spelling I4 forbids.
- **I5, on both sides of the gate** — an EQUAL `contract_version` proceeds and a
  DIFFERING one refuses startup. Testing only the refusal would let a gate that
  refuses *everything* pass, which is why both sides are asserted. (The
  minor/warn side is not tested because it is not implemented; see
  `koros-arc-store-v1` S11.)
- **The tiers** — every record carries the tier its kind mandates, and
  specifically that `kbdl.fitness_prop` writes `homology` rather than
  `hypothesis`. That one is the likeliest to be got wrong, because
  `hypothesis` is the right answer for every other record this PRD writes.

**Prior art in this codebase:** KBDL's `tests/test_layering.py` enforces
import-boundary invariants and is the model for asserting that the app layer
does not reach past `KorosArcStore` into the filesystem.
`tests/test_objects_skani_version.py` is the model for asserting an unchanged
published payload, which is the shape of the "stdout is byte-identical" test.

**Regression framing:** every criterion in the taskplan is phrased as "the
tests this task adds pass, and no test that passed on the base commit fails",
because both repos carry pre-existing failures.

## Open Questions and Judgement Calls

Every entry below is **already decided and reflected in the PRD body and the
taskplan**. The PRD is buildable as it stands. Changing a decision means
editing its `DECIDED:` line and following the `If you disagree` consequences.

**Entries are ORDERED by blast radius descending; their NUMBERS are stable
ids, not positions.** Q-numbers were assigned in the order the decisions were
made and never change, so `### Q3` remains findable across review rounds even
as the ordering shifts. Q11 is the terminal could-not-decide entry and always
sorts last regardless of number.

### Q1. Where does the analysis-to-arc join live? -- DECIDED: a per-user local database in the pod home directory, written by KBUtilLib's KBDL client utilities, with a two-tier schema -- navigable dimensions as SQL columns, gene- and reaction-level detail as one JSON blob per subject.

**Blast radius:** IRREVERSIBLE
**Why:** Chris specified it directly on 2026-09-23, and the instruction names
both dashboards and names reaction-level data, so it binds this PRD and not
only the annotation twin. It replaces an earlier decision in this same document
for a per-arc `.analyses/*.jsonl` sidecar, and it is better on three counts the
sidecar could not meet. It puts the store where KIND actually runs rather than
in a Dropbox-synced tree. It gives levels 0 and 1 a queryable surface, where a
sidecar forced a walk over every arc to render the portfolio. And the two-tier
split answers a problem the sidecar had no answer for at all: a genome-scale
model is thousands of reactions and flux across conditions multiplies it, so
detail as rows would swamp the row budget the instruction sets.
**If you disagree:** the sidecar version is recoverable from this document's
history and its one real advantage was that an arc was self-describing — copy
an arc to a colleague and its records travelled with it. A per-user database
does not travel. If that property is wanted back, the fix is an export, not a
return to the sidecar.
**What moved with it:** per-writer sharding is withdrawn (the Dropbox
conflicted-copy hazard does not exist in a pod home directory); Q6 is withdrawn
entirely (nothing is written into the arc directory, so no `.gitignore` is
touched); S8's append-only read-time-dedup rule is replaced by ordinary
database write semantics; and `read_detail` joins the interface.
**Confidence:** high on the backend, **medium on the schema boundary** — which
statistics are precomputed into columns is the part most likely to need a
second pass, and G21 says why.

### Q2. Who builds the shared project/arc layer? -- DECIDED: its own PRD, `koros-arc-store-v1` in KBUtilLib. Neither app builds it; both declare a hard dependency. REVISED 2026-09-24 from "the annotation PRD builds it".

**Blast radius:** IRREVERSIBLE
**Why:** Two PRDs are being designed in parallel against the same layer. If
both taskplans contain a task that creates `kbutillib.koros_arc_store`, they either
collide on the same file in two branches or silently produce two divergent
versions, and the dev-1032 one-branch-per-repo rule makes the collision the
likelier outcome. The original answer made whichever app dispatched first the
owner, which worked but left the highest-risk work in either plan buried inside
a document about genome annotation. Extraction removes the ownership question
rather than answering it: the module is one deliverable, dispatched on its own,
and the ordering is the same for both consumers.
**If you disagree:** reversing it means folding
`koros-arc-store-v1`'s four tasks back into one of the two app plans and
re-pointing the other's preconditions. The seam itself does not move either
way, which is why this reversal is cheap in code and expensive only in
documents.
**Confidence:** high — Chris decided it on 2026-09-24, and both design sessions
had independently recommended it.

**One consequence that is easy to miss:** KBUtilLib is both the module's repo
AND this app's repo, so under one-branch-per-repo this PRD cannot start while
`koros-arc-store-v1` is building. The annotation app has no such collision —
its own tasks are in KBDLJobRunningPrototype and GenomeAnnotationAggregator.
The two consumers are therefore *not* symmetric in the build order even though
the dependency graph says they are.

### Q10. Is the data reachable from the kbhub pod, where KIND primarily runs? -- DECIDED: the KOROS runs tree IS reachable (confirmed by Chris); the KBDL object store may not be, so object-store access stays optional and degrades to file-path artifacts.

**Blast radius:** IRREVERSIBLE
**Why:** KIND's primary environment is the pod, and kbhub runs **selective**
Dropbox sync that deliberately excludes several `Projects/*` repos — so whether
the runs tree is visible there was the load-bearing unknown in this design.
**Chris confirmed it on 2026-09-23: it is reachable.** That settles the half
that mattered, because both apps enumerate projects and arcs from
`PROVENANCE.json` to render level 0, and an unreachable runs tree would have
left the app empty on the machine it is meant to run on.
Q1's storage change settles the other half from the opposite direction: the
per-user database is created in the pod home directory, so it is reachable by
construction. What remains genuinely uncertain is only the **KBDL object
store**, which is why artifact resolution keeps its degrade path.
**If you disagree:** if the object store turns out to be fully reachable from
the pod, the optional path is dead weight and `p3-artifact-resolver` simplifies
to a single code path. If the *runs tree* turns out **not** to be reachable,
the entire design changes — there is no arc to stamp, and the join would have
to move to a central index on the pod, taking Q1 with it.
**Confidence:** HIGH on the runs tree, on Chris's direct confirmation rather
than a probe — worth stating as his assertion rather than a measurement, but he
is the authority on his own pod. Medium on the object store, which nobody has
checked and which the degrade path makes survivable either way. This entry was
the PRD's weakest irreversible call when it was written; it is now among its
strongest.

### Q12. How much CAC conformance does "an official app in KOROS" actually require? -- DECIDED: the plane-1 + plane-2 profile, which the contract calls universal. NOT plane 3, which it calls opt-in.

**Blast radius:** IRREVERSIBLE
**Why:** CAC §5 states the two adoption profiles outright — *"plane-1 is
universal; plane-3 is opt-in (evidence-producing apps)."* Plane 1 (launch,
embed, self-registration, `contract_version`, the canonical id) and plane 2
(the watched-directory handoff) are what every official app owes. Plane 3 is
the research-seeding round trip to KOROS plus the lakehouse rendezvous table,
and it is what an app owes when it *emits evidence for peers to consume*. This
app is a navigator over evidence other tools produced; it publishes nothing a
peer app would consume that the peer could not read from the same records. So
the universal profile is the honest reading of "official".
**If you disagree** — and the disagreement worth having is whether this app
*is* evidence-producing — plane 3 adds: the Schema-#1 outbound bundle, the
Schema-#2 result consumer, the derived rendezvous table
`arkinlab.models_and_analyses.interop_bundles` with the seven-column RULED row
schema, `arkinlab.models_and_analyses.koros_result`, TTL-based GC on `ts`, and
a poll-first return leg. Concretely that is at least two more taskplan phases
and a lakehouse dependency this PRD does not currently have. It also becomes
the right call the moment the annotation app wants to consume model output,
because I3 forbids the two apps importing each other — they must meet in the
lakehouse.
**Confidence:** HIGH — **confirmed by Chris directly on 2026-09-23**, asked
whether "official app in KOROS" applied to this app or only the annotation
twin: *"Yes - it applies to both."* The premise is no longer an inference from
the two apps being twins, and the entry no longer carries a relay caveat. The
contract reading (plane-1 + plane-2 universal, plane 3 opt-in) was already high
confidence from §5; both halves are now settled.

### Q15. What happens to an analysis that belongs to no arc? -- DECIDED: it is recorded to a root-level unattributed index and surfaced behind an explicit selector. Unattributed is a first-class state, not an error.

**Blast radius:** MEDIUM
**Why:** Q5 decides that an unresolvable arc means "record nothing and warn".
That is right for the *producer's* behaviour and wrong as the *system's* end
state, and the distinction took a second pass to see. Today **almost every**
analysis has no arc — that is precisely what the finding-2 grep measures — so
until every producer adopts attribution, an implementation that simply drops
null-arc records drops the majority of them. The dashboards then render
correctly and are empty, and the emptiness reads as "no work was done" rather
than "attribution is not adopted yet". Writing them to a root-level
unattributed index makes the gap visible and gives backfill somewhere to put
what it finds but cannot attribute.
**If you disagree:** dropping the unattributed index means `record_analysis`
rejects a null arc, backfill can only emit records it can attribute, and the
app has no way to show a user work it knows about but cannot place. It also
removes the most honest signal that attribution rollout is incomplete.
**Confidence:** high — this was the annotation session's catch against my
design, and the argument is measurement-based rather than aesthetic.

### Q3. Does this app render genes, reactions, classes and fitness itself, or delegate to `EscherUtils`? -- DECIDED: delegate to `create_fitness_dashboard` and `create_map_html2`; build no new rendering.

**Blast radius:** MEDIUM
**Why:** `create_fitness_dashboard` already produces the five tabs Chris asked
for plus a fitness-class-recoloured Escher map with condition and FVA
dropdowns, and it already consumes the native KBDL JSON outputs. Rebuilding it
would be a second implementation of a vocabulary
(`essential/active/unused/blocked`, the concordance categories) that is already
fixed in `fitness_dashboard.py`, and the two would drift.
**If you disagree:** the cost of delegating is that level 2 is a **generated
page**, not a live-filtering view — no cross-model filtering, no sorting that
survives a reload, and a generation step measured in seconds per model. If
Chris wants a single interactive table spanning many models, that is a
different level-2 and it needs its own data path reading the `gaa_data` tables
directly; `create_fitness_dashboard` cannot be bent into it. Concretely that
would replace tasks `p3-dashboard-endpoint` and `p3-escher-endpoint` with a
model-data API and a frontend table, roughly doubling the app work.
**Confidence:** high on the reuse being correct, medium on it being
*sufficient* — the existing dashboard was read, not run, this session.

### Q4. How does a KBDL service-side job learn its arc? -- DECIDED: as an explicit optional `project`/`arc` on `JobContext`, set by the client at submit time.

**Blast radius:** MEDIUM
**Why:** `KOROS_ARC` is injected into KIND *session* processes
(`session.py:779`). A KBDL job executes in the service, in a different process
on possibly a different machine, and that environment variable is simply not
there. Reading it server-side would silently index every job to whatever arc
the service happened to inherit — which is worse than not indexing at all,
because a wrong arc is invisible while a missing record is obvious.
**If you disagree:** the alternative is that KBDL jobs are never indexed and
only `kbu model` work appears, which removes stories 14 and 16 and makes the
model table empty for anyone who builds models through KBDL — i.e. most of the
intended use. Concretely it deletes tasks `p2-kbdl-jobcontext` and
`p2-kbdl-stamping` and removes `kbdl.*` from the kinds table.
**Confidence:** high.

### Q6. Arcs are git repos; does the sidecar get committed? -- WITHDRAWN by Q1. No longer applicable.

**Blast radius:** NONE
**Why:** This asked whether the per-arc `.analyses/` sidecar should be
gitignored, and answered that `KorosArcStore` would add the line itself. Q1 has
moved the store to a per-user database in the pod home directory. **Nothing is
written into the arc directory at all**, so there is no sidecar to ignore and
no `.gitignore` to edit.
**Kept rather than deleted** so a reader of a later revision can see that the
question was asked and why it stopped mattering. Gotchas G4 and G18, and
specification S14, are withdrawn with it.
**Confidence:** high.

### Q7. What is the record identity, so a re-run replaces rather than duplicates? -- DECIDED: `record_id` is producer-generated and stable across re-runs of the same logical analysis, not content-addressed.

**Blast radius:** MEDIUM
**Why:** Content-addressing would make every re-run a new record, so an arc
re-analysed five times shows five rows where the scientist did one thing five
times. A stable id — derived from the kind, the subject, and the significant
parameters such as media and objective — collapses them to one row showing the
latest, which is what a table of "models analysed in this arc" should show.
**If you disagree:** keeping every run means the app needs a history axis on
every row and the counts at level 0 stop meaning "how much is here" and start
meaning "how many times did anything run". Concretely `list_analyses` would
gain a `latest_only` flag and both the arc table and the portfolio counts would
need a de-duplication pass.
**Confidence:** medium — an argument exists that re-runs *are* interesting
(a model re-analysed after a gapfill change), and this decision loses that.
Mitigated because the failed and superseded records still exist in the JSONL;
only the view collapses them.

### Q13. How are model-derived values kept distinct from experimental data? -- DECIDED: every record carries a CAC `trust_tier` and `provenance`; model output is `hypothesis`, propagated fitness is `homology`, and the concordance view never merges tiers into one undifferentiated number.

**Blast radius:** MEDIUM
**Why:** CAC §A ratifies the tiers and names model-generated output as
`hypothesis` — "argued, not proven" — with an explicit anti-laundering test.
This app's whole purpose at level 2 is to show predicted essentiality beside
measured RB-TnSeq fitness, which is the exact adjacency the invariant is about.
An "agreement score" that averages the two without carrying tier is laundering:
it restates a model prediction as though it had the standing of a measurement.
Putting the tier on the record from the first commit is far cheaper than adding
a confidence column to a schema and a UI that already shipped without one.
**If you disagree:** dropping tiers removes `trust_tier` and `provenance`
from `AnalysisRecord`, removes the tier column from the arc and concordance
views, and removes the `homology` distinction on `kbdl.fitness_prop` — which
would present propagated fitness and measured fitness identically, the case
§A calls out by name as the category error. It also forecloses plane 3 later,
since the rendezvous row schema requires a `trust_tier` floor column.
**Amended, review round 1:** the annotation PRD's **floor rule** is adopted —
a record summarizing several findings carries the **least-trusted** contributing
tier, adding higher-tier findings never raises it, and the shared layer exposes
no API that raises a tier at all. The consequence for this PRD is concrete and
was not previously stated: **the arc-level MCC (Q14) carries a floor tier**, so
a statistic computed over `hypothesis`-tier predictions is reported at
`hypothesis`, never promoted by the presence of measured data on the other side
of the comparison. Write-time enforcement also moves into the store, which
rejects a non-`verified` record with an empty `provenance` rather than trusting
the producer to populate it.
**Confidence:** high on the requirement, medium on the exact `provenance`
payload for `kbdl.fitness_prop` — the percent-identity bridge metric exists in
that job's design (it is the stated advantage of search over clustering), but I
did not verify the field name in its result shape.

### Q5. What happens when the arc cannot be resolved? -- DECIDED: record nothing, warn on stderr, and never fail the analysis.

**Blast radius:** LOW
**Why:** The three failure shapes are: guess an arc, fail the run, or skip
indexing. Guessing puts results on the wrong arc, and a wrong arc is a lie the
UI presents as fact. Failing the run makes an indexing feature able to break
science that has nothing to do with it. Skipping is visible on stderr and
recoverable by backfill.
**If you disagree:** making it fail loudly would catch mis-configured
environments sooner, at the cost of `kbu model fba` exiting non-zero outside
KIND — which would break existing scripted callers immediately.
**Confidence:** high.

### Q8. Which web framework does the app use? -- DECIDED: FastAPI with server-generated HTML, matching the launch contract rather than lakehouse-explorer's Solara.

**Blast radius:** LOW
**Why:** The launch contract is a console entry point that binds a port and
honours `--root-path`; anything satisfying that works. FastAPI is already a
transitive presence in this stack, the app's job is mostly to serve JSON and
two generated HTML pages, and `create_fitness_dashboard` already returns a
complete self-contained page — a reactive frontend framework would be carrying
weight for a page it does not render.
**If you disagree:** matching lakehouse-explorer's Solara would make the two
apps look and behave alike and let KIND's existing handoff adapter be reused
verbatim. It costs a heavier dependency in KBUtilLib and a rewrite of
`p3-app-skeleton`.
**Confidence:** high.

### Q9. Does the app also register a KIND `type: "provider"` for the Data nav? -- DECIDED: no, not in v1.

**Blast radius:** LOW
**Why:** A provider entry surfaces a resource count in KIND's Data section from
a glob. Under Q1 the store is a per-user database, not files in the runs tree,
so there is no meaningful glob to point a provider at — a glob over the
database file counts one file, not analyses. A provider that reports a
misleading count is worse than no provider. If KIND later accepts a
query-backed provider rather than a glob-backed one, this becomes cheap and
worth revisiting.
**If you disagree:** adding it is a single JSON file with a `present`/`count`
glob and no code, so this is cheap to revisit once the record store has proved
out.
**Confidence:** high.

### Q14. Does the arc view add an essentiality agreement metric the existing dashboard lacks? -- DECIDED: yes -- a confusion matrix with MCC, precision and recall, computed at the arc level only.

**Blast radius:** LOW
**Why:** The external scan (question E) reports that published practice —
MEMOTE and the RB-TnSeq literature — is a confusion matrix with MCC,
precision and recall. `fitness_dashboard.py` computes per-gene concordance
categories and a `concordance_summary` count, which is the per-model view, but
no single comparable number. A per-model scalar is what makes an arc table of
several models readable at a glance. Computing it at the arc level keeps it out
of `fitness_dashboard.py`, which this PRD does not modify.
**If you disagree:** dropping it leaves the arc table showing raw concordance
counts per model, which is still usable and is strictly less work — one column
and its computation come out of `p3-app-skeleton`.
**Amended, review round 1:** the metric **carries a floor trust tier** under
the rule adopted into Q13. It compares `hypothesis`-tier predictions against
measured fitness, so it is reported at the floor — `hypothesis` — and labelled
as a model-versus-experiment statistic. It is never presented as a `verified`
measure of model quality because one of its two inputs is measured.
**Confidence:** high — this is a small, additive, well-evidenced choice.

### Q11. What I could not decide and did not guess

Five external facts are unreconciled, and none was guessed at:

1. **RESOLVED 2026-09-23.** Pod reachability of the runs tree: Chris confirmed
   it is reachable, so no envelope to Albert was needed. Only the KBDL object
   store remains unchecked, and the artifact resolver degrades without it.
2. **Whether Chris wants arc analysis records in git history.** Q6 decides
   "no" on the reasoning that index churn in `git status` is a cost he has not
   agreed to pay. Nothing in any artifact states how he uses arc git history,
   so this is one of two decisions resting on an assumption about a person
   rather than about a system.
3. **RESOLVED 2026-09-23.** Whether "an official app in KOROS" was said about
   THIS app: asked directly, Chris answered *"Yes - it applies to both."* Q12's
   premise is no longer an inference.
4. **Whether "official" means our code is eventually contributed UPSTREAM into
   the `koros`/`king` repos, or stays in KBUtilLib as a registered conformant
   capability.** The consume-only interlock forbids writing into those five
   repos, so this PRD designs for the second reading. The first reading would
   change the owning repo and the whole deploy route, and it is a cross-team
   question no artifact on this machine answers. The parallel annotation
   session reached the same reading independently and recorded the same
   caveat.
5. **The CAC's own unfilled seams** (`⟨fill: …⟩` in the live doc): how a
   launched app obtains the user's ORCID (§C), the canonical id-normalization
   helper (§E), and the `contract_version` gate mechanics (§F). This PRD
   implements its own derivation and its own gate to the stated policy; if
   KING later ships helpers, ours should be replaced by them rather than
   competing.

## Gotchas and Unintuitive Consequences

**G1. Setting the obvious proxy environment variables blanks the page.**
`APP_INTEGRATION.md` records that the hub's double proxy sends
`x-forwarded-proto: "https,http"`, and that setting `FORWARDED_ALLOW_IPS` or
`UVICORN_PROXY_HEADERS` — which is exactly what a developer reaches for when an
app behind a proxy renders wrong — rewrites asset URLs under `/hub/…` and the
iframe comes up blank with no error. The fix looks like the cause.

**G2. The trailing slash on the proxy URL is load-bearing.**
Also from `APP_INTEGRATION.md`. A returned URL without it fails in a way that
looks like the app never started.

**G3. `_resolve_plugins` replaces; it does not union.**
Pointing `KING_PLUGINS_DIR` at our directory **hides every plugin KIND ships**
unless the symlink farm has been run. So installing this app can silently
remove other capabilities from the user's KIND — an effect with no plausible
connection to its cause.

**G4. WITHDRAWN with Q1** — stamping no longer touches the arc directory, so it
cannot dirty the scientist's git repo.

**G5. Inlined Escher makes every generated dashboard multi-megabyte — and
turning it off can blank the page on an air-gapped pod.**
`create_fitness_dashboard` defaults `inline_escher=True` so the page is
self-contained offline. Generating one per model and caching them means the
app's state directory grows by the size of the Escher bundle per model, not per
map. For an arc with thirty models that is a surprising amount of disk for what
presents as a browsing tool. So the app passes `inline_escher=False` — **but
that trades a disk problem for a network one**: the served dashboard then needs
Escher fetchable at view time, and the pod's egress is not something this PRD
verified. Confront's free critique caught this, and the resolution is a
**toggle, defaulting to non-inlined, with the inlined path available** — not a
single hardcoded choice. Whichever is wrong is wrong in a way that looks like
"the map does not render" either way.

**G6. `PROVENANCE.json`'s richest-looking fields are empty.**
`inputs`, `tool_versions` and `compute_targets` are present and empty on the
live arc inspected. A level-0 view that showed "tools used in this arc" from
`tool_versions` would show nothing forever and look broken rather than empty.

**G7. A leg re-forked becomes a leg again.**
The `cw-kind` skill records that `koros fork` re-stamps `leg_of`/`role`, and
there is no `koros` command to detach. So an arc the user detached can silently
re-nest under a parent in this app's level-0 view after an unrelated fork.

**G8. The annotation explorer's records appear in the same index.**
Both apps write to the same per-user database under different `kind`
namespaces. A reader that forgets to filter shows annotation runs in a model
table. The upside is the reason for the choice — one arc shows all its work —
but it means every query in this app must filter by prefix, and an unfiltered
`list_analyses` is a bug that looks like data corruption.

**G9. Stamping changes what a failed analysis costs.**
Today a failed `kbu model fba` leaves nothing behind. After this, it leaves a
record with `status: "failed"` (story 19, deliberate) — so the arc table will
show analyses that produced no result, and a user reading the count as "work
completed" will over-count. The app shows status per row for this reason.

**G16. The fitness class vocabulary is wider than the four names suggest.**
`fitness_dashboard.py` carries aliases beyond `essential / active / unused /
blocked` — including forward and reverse essential variants and further labels.
Any view that hardcodes a four-value subset will silently mis-colour or drop
classes the dashboard itself handles. Read the class table from the module
rather than restating it.

**G17. An unreadable artifact renders an empty dashboard, not an error.**
If the model JSON or a `gaa_data` table is present but unreadable — a
permission, a half-synced Dropbox file — the generators can produce a page with
nothing in it. That is indistinguishable from a model with no results. The
endpoints must probe readability and return an explicit error naming the
unreadable artifact, rather than handing a silent blank to the user.

**G18. WITHDRAWN with Q1** — no `.gitignore` is edited, so there is nothing to
notify about.

**G19. Opaque ids make every table unreadable at the exact moment it matters.**
`artifacts.model_id` is `model:kbdl:<object id>` or `model:file:<absolute
path>`, and grouping is strictly by it (S3). Displayed raw, the arc table
becomes a column of hashes and long paths. Identity and display are different
concerns: group by the id, show the genome or model *name*, and keep the id
available for copying.

**G20. A backfill that reports only what it wrote hides what it could not.**
Following from G10: the honest output is not a count of records written but a
**coverage report** — arcs scanned, records written, and an explicit list of
artifacts found-but-unattributable and analyses referenced-but-missing. Without
that list the operator sees a successful run and a partial index, and has no
way to tell the difference.

**G21. A precomputed summary statistic with no version stamp silently mixes two
populations.**
The two-tier schema puts summary statistics in SQL columns precisely so the arc
view never opens a blob — and the moment the definition of one changes, the
database holds rows scored the old way and rows scored the new way with nothing
on the row saying which. The MCC in Q14/S13 is exactly such a statistic. Every
precomputed statistic therefore carries the version of the code that computed
it, and a view that mixes versions says so rather than ranking across them.

**G22. The two-tier split forecloses every cross-subject detail question.**
"Show me every reaction across all my models where X" cannot be answered by SQL
once reaction detail is a JSON blob. Every query a drill-down issues is scoped
to one subject, so v1 genuinely never needs it — which is exactly why the
limitation is invisible until the first corpus-wide question is asked, and then
it looks like a bug rather than a design boundary. The fix when that day comes
is a derived index built from the blobs, **not** a schema change promoting
detail back into rows.

**G23. Recording is on by default, so it is a side effect the user never asked
for — and it must never be able to break their pipeline.**
On pods the KBDL client records automatically. A database that is locked,
full, read-only, or on a filesystem that has gone away must degrade to a
warning and let the analysis complete. This is the same posture as Q5's
unresolvable arc, now applying to the store itself, and it matters more here
because the failure is silent from the user's side: they did not ask for
recording and will not be looking for it to fail.

**G10. Backfill can only infer what the filesystem still holds.**
Records written by backfill are marked `inferred`, but the deeper consequence
is that backfill coverage is silently partial: an analysis whose output was
deleted, or printed to a terminal and never saved, is unrecoverable and will
never appear. The app will look like it has full history when it has only the
history that left files.

**G11. Conforming to I1 means the app must be useful with KIND switched off —
which changes what "an app in KIND" is.**
`models-and-analyses serve --no-king` has to work and has to be worth running.
That is not a checkbox: it forbids depending on anything KIND injects at launch,
including the proxy path, the allocated port, and any identity KING would have
supplied. The consequence nobody predicts is on **identity** — CAC §C says KING
owns ORCID and hands it to the app, and the seam by which it does so is an
unfilled `⟨fill:⟩` in the contract. So standalone, the app has no user identity
at all, and anything keyed on "the current user" must degrade rather than fail.

**G12. The canonical id is the distribution name, so choosing it is a packaging
decision, not a label.**
I4 makes `models-and-analyses` simultaneously the manifest basename, the console
command, the Python dist name and the koros `--project` token. Renaming the app
later is therefore a package rename plus a manifest rename plus a koros project
rename, and any rendezvous table already created under
`arkinlab.models_and_analyses.*` is orphaned by it. The name is far more
load-bearing than it looks at the point where someone picks it.

**G13. A `contract_version` major mismatch takes the app down deliberately.**
§F mandates a hard gate on a major mismatch. So a KING stack cut that bumps the
contract makes this app refuse to start rather than render slightly-wrong data
— correct under I2, and it will nonetheless present as "the app broke after an
unrelated KIND update". The failure message must name the version gap, or the
first person to hit it will debug the wrong thing.

**G14. Tiering makes the concordance view say something less satisfying than a
single number.**
Q13 forbids merging `hypothesis`-tier predictions and measured fitness into one
undifferentiated agreement score. The MCC in Q14 is therefore explicitly a
*model-versus-experiment* statistic with both tiers named, not a quality score
for the arc. A reader who wants one number for "how good is this model" will not
get it, and that is the contract working as intended rather than a gap.

**G15. Propagated fitness looks like measured fitness and is not.**
`kbdl.fitness_prop` output is `homology` tier — it reached the gene through a
protein homolog with a percent-identity bridge — while RB-TnSeq fitness is
measured. The existing dashboard already keeps them as separate badge layers,
which is lucky rather than designed for this; any new view this app adds must
preserve that separation, and §A names collapsing them as the category error
by which `homology` gets laundered.

## Sources Consulted

**KIND / KOROS (`~/king-stack`, read this session):**
- `king/docs/APP_INTEGRATION.md` — full read. Manifest schema, launch tokens,
  port range 8800–8899, `_resolve_exe`, the `FORWARDED_ALLOW_IPS` and
  trailing-slash gotchas, `type:"app"` and `type:"provider"` JSON plugins.
- `king/backend/king_backend/session.py` — `KOROS_ARC` injection at `:779` and
  `:865`; `Session.arc` at `:240`; `session_for_arc` at `:507`.
- `king/backend/king_backend/p0.py:93-96` — arc directory enumeration, and the
  project-dir-is-an-arc special case.
- `king/backend/king_backend/config.py` — `_resolve_plugins` (replaces, does
  not union).
- `koros/skills/koros-start/koros_start.py:451-462` and `test_start.py:124-138`
  — `KOROS_ARC` is a slug and is the write-target.
- `king/plugins/*.json` — the 24 shipped manifests, for manifest shape.
- `king/docs/` index — `CAPABILITY_CONTRACT.md`, `CROSS_APP_COMMUNICATION.md`
  noted as the governing contracts; **cited, not read in full.**

**KBUtilLib (`~/Dropbox/Projects/KBUtilLib`, on `wip` at `9b4ffe4`):**
- `src/kbutillib/domains/notebook/escher_utils.py` — `EscherUtils` class;
  `create_fitness_dashboard:1498` and `create_map_html2:1408` read in full
  including docstrings; `list_available_maps:214`, `map_stats:345`,
  `_load_map:398`, `_translate_map_with_flux:621`,
  `_inject_reaction_class_overlays:1225`.
- `src/kbutillib/domains/notebook/fitness_dashboard.py` — `build_dashboard_html`,
  the five-tab list at `:440`, fitness class vocabulary at `:34-40`,
  `_concordance` at `:165`.
- `src/kbutillib/kind_app/bundle.json` and `skill.md` — the existing
  `kbutillib-modeling` context-injection app and the `kbu model` verb surface.
- `src/kbutillib/` module listing — confirmed `escher_utils.py` at top level is
  a deprecation shim for `domains/notebook/escher_utils.py`.

**KBDL (`~/Dropbox/Projects/KBDLJobRunningPrototype`, on `wip` at `7209e73`):**
- `src/kbdl_service/object_store/store.py:57-97,350-374` — `ObjectMetadata`
  fields and the docstring stating fields are deliberately withheld;
  `ObjectStore.list(username, object_type)` ACL filtering.
- `src/kbdl_service/object_store/references.py` — `register_reference`,
  `resolve`, the reference-versus-content storage split.
- `src/kbdl_service/job_types/` — all ten job types listed;
  `fitness_model_analysis.py` `gaa_data` table keys at `:198-203` and
  `_shape_gaa_data` at `:630`; `JobContext` fields at `:53`.
- `src/kbdl_service/clearinghouse/` — module listing.
- `agent-io/prds/kbdl-fitness-prop-v1/humanprompt.md` — the
  `BuildGenome → GenomeAnnotation → ModelReconstruction → FitnessModelAnalysis`
  chain and the `KBDLFitnessProp` addition.
- `agent-io/prds/clearinghouse-lake-1d-per-entity-type-tables/fullprompt.md`
  Solution section — the fifteen tables over five entity types; **read to
  establish that no model or reaction entity type exists**, which is a
  negative finding.

**Live data:**
- This laptop's runs tree — project listing. Path recorded as an observation about this machine only; the resolved location is `$KOROS_HOME/runs`.
- `<runs>/genome-clearinghouse/arcs/mag-integration-arc/` —
  full directory listing and `PROVENANCE.json` read verbatim.

**The CAC and its neighbours (`~/king-stack/king/docs`, read this session):**
- `CROSS_APP_COMMUNICATION.md`, 353 lines — read in substance, not summarised
  from the relay. §0 the acceptance test; §1 the **six** invariants I1–I6; §2
  plane 1 including the manifest field table and **1.2 self-registration +
  `--no-king`**; §3 plane 2; §4 plane 3 with schemas #1–#3; **§A trust tiers,
  ratified [D125], including the anti-laundering test and the reservation of
  `homology` for sequence/structure homologs**; §B the rendezvous DDL and the
  derived table-naming rule; §C identity; §D I6 isolation of authority; §E
  id-normalization; §F versioning and the hard-gate/soft-warn policy; **§5's
  statement that plane-1 is universal and plane-3 is opt-in**, which is the
  finding that bounded this PRD's scope.
- Its unfilled `⟨fill: …⟩` markers in §C, §E and §F, noted as open on KING's
  side rather than invented here.

**External research (`research/external-scan.md`, Maestro `task-f379e8ac`,
codex on h100):** read in full and folded — §A Escher embedding and its
iframe-sandbox and map-size constraints; §B multi-model navigation prior art
(MetaNetX, BiGG, ModelSEED, MEMOTE, Caffeine, KBase) and identifier
harmonization; §C run-index envelopes (MLflow, W&B, DVC, Snakemake, Nextflow
Tower, Galaxy, **RO-Crate**, CWLProv); §D append-only JSONL failure modes and
the scale threshold; §E essentiality-agreement metrics and the MCC
recommendation; and its build-versus-buy verdict.

**Platform state:**
- AIAssistant project registry — the `kind-integration` project exists
  (active, last activity 2026-07-28) and is where this work belongs.
- `cw-kind` skill (`~/.claude/commands/cw-kind.md`, 821 lines) — the
  consume-only interlock, the two extension mechanisms, the plugin-union
  runbook, the two-environment table, and the detach-arcs runbook establishing
  that arc **data** is writable while the five repos are not.

**Read and found uninformative:**
- `mem search` on "KIND app dashboard metabolic model analysis arc project"
  returned no results; on "KIND" returned only rename-and-install episodes with
  no bearing on app design. There is no memory node about KIND app
  architecture.
- `src/kbutillib/agents/kind_install.py` and `king_install.py` — install
  plumbing only, no app-authoring guidance.

**Sibling session:**
- Session `1f49b595`, designing `kind-annotation-results-explorer-v1` in
  `GenomeAnnotationAggregator`. Its mail established points 1–4 of its own
  research independently; this PRD's reply corrected its seam to add the write
  side. Ownership of the shared layer is settled in that exchange (Q2).

## Out of Scope

- **Building `kbutillib.koros_arc_store`.** Owned by
  `kind-annotation-results-explorer-v1` (Q2).
- **Anything the annotation explorer renders** — SKANI results, per-method gene
  annotations, ontology standardization, annotation consistency scoring.
- **Modifying `create_fitness_dashboard` or `create_map_html2`.** If they are
  wrong, that is a separate PRD.
- **Community models.** `kbu-community-analysis-v1` is in flight in KBUtilLib
  and owns how a community model is displayed; that question is explicitly its
  Q4 and is not answered here.
- **Adding model or reaction entity types to the clearinghouse lake.**
- **Writing to any of the five KIND repos**, and any change to KIND core.
- **Cross-arc comparison** — comparing models across arcs in one table. The
  level-1 table is per-arc by construction.
- **Authentication and multi-user ACL beyond what the object store and the
  filesystem already enforce.**
- **`kbu model exec` stamping.**
- **A derived cross-subject index over the JSON blobs** (G22). v1's drill-downs
  are all subject-scoped; the corpus-wide question is real but later.
- **Migrating detail out of blobs into SQL rows.** The row budget is the
  instruction's own constraint, not an implementation convenience.
- **A KIND `type:"provider"` entry** (Q9).
- **CAC plane 3** — the outbound bundle (schema #1), the KOROS result consumer
  (schema #2), the feedback schema (#3), and the lakehouse rendezvous tables
  `arkinlab.models_and_analyses.interop_bundles` and
  `arkinlab.models_and_analyses.koros_result`. §5 makes plane 3 opt-in for
  evidence-producing apps; Q12 declines it for v1. **Q12 also names what turns
  that decision around**: the moment the annotation app wants to consume model
  output, I3 forbids the two importing each other and plane 3 becomes the
  required path.
- **Contributing this app upstream into the `king` or `koros` repos.** Q11
  item 4 records that "official" has two readings and this PRD designs for the
  one the consume-only interlock permits.

## Further Notes

The app is deliberately thin. Counting lines, the great majority of what a user
sees at levels 2 and 3 is `fitness_dashboard.py`, which already exists and is
not touched. If a reviewer finds this PRD small for what it delivers, that is
the intended shape and the research report explains why.

The ordering constraint worth stating plainly: **nothing in this PRD can be
dispatched until `kbutillib.koros_arc_store` exists on `main`.** Its phase 1 is
empty by design. The annotation PRD's phase 1 is this PRD's precondition, and
a build that starts before that lands will fail at import in every task.

**On merging the two PRDs — recommendation: DO NOT merge the apps; DO extract
the shared scaffold into a third PRD.**

Chris raised merging on 2026-09-23: *"It may make sense even to merge them even
if this creates a huge prd."* The instinct is right about the problem and, I
think, wrong about the remedy. Four reasons, in descending weight.

1. **The dependency that actually hurts is not expressible in either
   taskplan.** `load_taskplan` validates `depends_on` within one plan only;
   there is no way to say "this phase depends on a task in another PRD". Today
   the coupling lives in prose in both documents and in nothing a tool checks.
   Merging fixes that — and so does extraction, at a fraction of the size.
2. **The shared scaffold is currently ONE task carrying three responsibilities
   and roughly fifteen distinct requirements** — runs-tree access, the database
   and its two-tier schema, the trust-tier enum with the floor rule and the
   anti-laundering constraint, the unknown-kind policy, and the CAC helpers. By
   the standard this PRD was held to, that is under-decomposed, and it is the
   single highest-risk task in either plan because **both apps block on it**.
   Extraction is the natural moment to split it into three.
3. **Merging would collapse two different review histories.** This PRD is at
   `review_rounds` 1 / `confront_rounds` 2; theirs is at 1 / 0. A merged
   document could not honestly say which half had been adversarially attacked,
   and the fields that record it would become meaningless. That is a
   provenance loss, and provenance is the thing these records exist for.
4. **The apps are otherwise disjoint and ship independently.** Different owning
   repos, different payload schemas, different renderers — theirs a per-gene
   multi-method matrix, this one Escher maps. Of a merged plan's eleven tasks,
   eight would never interact. Merging couples two release schedules that have
   no reason to be coupled.

**The proposed shape is three PRDs:** `koros-arc-store-v1` in KBUtilLib owning
the scaffold, decomposed into its three responsibilities; and the two app PRDs,
each declaring a PRD-level dependency on it. Build order becomes a fact about
the dev board rather than a paragraph two documents have to keep in sync.

**What this PRD does if the answer is "merge anyway":** its tasks are already
self-contained prompts and its phases already assume the scaffold exists, so
they graft onto a merged plan as phases 2–6 with no rewriting — only the
`depends_on` edges change. Extraction and merging cost the same here; the
difference is entirely in what the other document has to absorb.

**On the shared surface, revised.** The parallel session's second message
argued that CAC conformance is now the larger shared surface — the id
derivation, `contract_version` handling, the envelope shapes, the rendezvous
DDL and the tier vocabulary are identical for both apps and are boilerplate
neither should write twice. That is right, and this PRD has deliberately kept
its CAC work small and separable for that reason: `APP_ID` / `app_us()` sit in
`arc_context` beside the arc resolution, and the tier fields are two keys on a
record rather than a class hierarchy. If a shared CAC scaffold is later
extracted, this PRD's tasks should collapse into it rather than fight it.
One caution the sibling raised and this session endorses after reading the
source: `king_backend.cac` is KING's canonical code for tier derivation, and
the consume-only interlock means **we bind to the contract, never fork or
vendor that module.**

## Acceptance Criteria

1. `record_id` is the sha256 of lowercased kind, lowercased subject and a canonical sorted-key JSON of the kind's significant parameters, and a test asserts two independent producers computing it for the same logical analysis agree.
2. Every `AnalysisRecord` written carries `trust_tier`, `provenance` and `contract_version`, and a record missing any of them is rejected by the writer.
3. `trust_tier` is `hypothesis` for `kbdl.model_build`, `kbutillib.reconstruct`, `kbutillib.gapfill`, `kbutillib.fba`, `kbutillib.fva` and `kbdl.fitness_analysis`, and `homology` for `kbdl.fitness_prop`.
4. A `kbdl.fitness_prop` record carries its percent-identity bridge metric and the accession reached through in `provenance`, or records an explicit null for the metric with the reason stated in the task report.
5. `status` and `trust_tier` are independent: a test asserts a `status: "ok"` record can and does carry `trust_tier: "hypothesis"`.
6. Records are written to the per-user local database in the pod home directory; a test asserts nothing is written into the KOROS runs tree.
7. `record_analysis` upserts on `record_id`: re-recording the same logical analysis leaves exactly one row, never two.
8. A record with a null project and arc is written and is retrievable through the explicit unattributed selector, not dropped.
9. `artifacts` keys match the per-kind table exactly, and every reference is a bare absolute path, a `file://` URI, or an `obj://<object_id>` URI.
10. Model rows in the arc view group strictly by `artifacts.model_id` in `model:<ns>:<id>` form, never by subject string.
11. `GET /api/models/{record_id}/dashboard` accepts a `kbdl.fitness_analysis` record, pairs it with the most recent `kbdl.model_build` sharing its `artifacts.model_id` in the same arc, and returns 404 `model-not-found` when there is none.
12. Escher map selection defaults to `modelseed_core`, then `modelseed_global`, then the first available map, and returns 404 `no-map` when the list is empty.
13. Generated HTML is cached under `$KING_STATE/kind-apps/state/models-and-analyses` or the documented fallbacks, never inside the five KIND repos and never inside the runs tree.
14. A bare `--arc SLUG` that matches arcs in more than one project refuses to stamp and prints a disambiguation error; `PROJECT/SLUG` resolves unambiguously.
15. The prefix filter counts the foreign records it excludes and exposes that count, and a test fails if a foreign-prefix record is silently dropped from a model count.
16. The app starts and serves `/api/portfolio` with KING absent from the environment, and `--no-king` suppresses self-registration.
17. `app_us()` is asserted equal to `APP_ID.replace("-","_")` rather than to a literal.
18. A differing `contract_version` refuses startup with an error naming both versions, an equal one proceeds, and no minor/warn branch exists.
19. The manifest is written under `$KING_STATE/kind-apps/plugins` or `$HOME/kind-apps/plugins` and a test asserts `king/plugins/` is untouched.
20. `--json` stdout of `kbu model reconstruct|gapfill|fba|fva` is byte-identical with and without a resolvable arc.
21. Backfill prefers file artifacts over object-store references, marks every record `payload.provenance == "inferred"`, is idempotent across two runs, and writes nothing under `--dry-run`.
22. No file in `king`, `koros`, `semcat`, `lakehouse-explorer` or `narrative-connector` is modified by any task in this PRD.
23. `escher_utils.py` and `fitness_dashboard.py` are unmodified; the app calls them and does not reimplement their rendering.
24. No test that passed on each task's base commit fails on its branch.
25. Payload keys match the normative per-kind schema exactly; a record carrying an unlisted key, or missing a listed one, fails validation rather than being written.
26. The MCC mapping follows the specified predicted/observed definitions, reports `null` rather than `0` when the denominator is zero, excludes genes with no measured fitness and reports that excluded count, and never substitutes propagated fitness for measured fitness.
27. Each endpoint's JSON response shape is asserted by a schema test.
28. A generating endpoint returns 202-with-poll-token, 200-with-HTML, 404 on an unknown token, or 500 naming the failing artifact — never an empty 200.
29. Nothing in this PRD writes into the KOROS runs tree or edits any `.gitignore`.
30. Backfill emits a coverage report listing arcs scanned, records written, artifacts found-but-unattributable, and analyses referenced-but-missing.
31. Gene-level and reaction-level detail is stored as one JSON blob per subject; `read_detail(record_id)` is the only call that opens a blob.
32. `GET /api/portfolio` and `GET /api/arcs/{project}/{arc}/models` are answerable from SQL columns alone, and a test asserts neither calls `read_detail`.
33. Every precomputed summary statistic carries the version of the code that computed it, and a view mixing versions says so rather than ranking across them.
34. A database that is locked, full, read-only, or absent degrades to a warning and the analysis completes; a write failure never fails a pipeline run.
