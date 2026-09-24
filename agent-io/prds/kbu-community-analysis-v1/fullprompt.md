# kbu-community-analysis-v1 — Community metabolic analysis utilities for KBUtilLib

## Research Report

### Important findings

**Upstream moved on 2026-09-23, and three of the five new commits fix defects this design
would have hit.** The pin advances from `b5f37c4b` to `2dcff16` (tip 2026-09-23 08:52).
What changed, and why each matters here:

- **`b14f50f` made plain float abundances work.** Before it,
  `MSCommunity.__init__` evaluated `"abundance" in <float>` and raised `TypeError`.
  This PRD passes `abundances={model_id: float}`, so the draft as written was specified
  against a path that did not run. The pin is therefore a **minimum**, not a preference.
- **`b14f50f` also fixed `gapfill`'s solver argument**, which had been passed as
  `MSGapfill`'s seventh positional — now `atp_gapfilling` — so a truthy solver string
  *silently switched every community gapfill into ATP-gapfilling mode*.
  `gapfill_community(solver="glpk")` would have done exactly that.
- **`b14f50f` added `build_solver` / `final_solver`** to `build_from_species_models`:
  populate the merged model under GLPK, then one clean rebuild under the configured solver.
  Incremental constraint addition into optlang's Gurobi interface is superlinear; the
  authors report equivalence verified at 32, 64 and 128 members.
- **`57e1504` added `close_member_drains`**, and it is the one that changes results rather
  than merely unbreaking them. See Q5 — it is a new decision this PRD now has to make.

**The community Escher map exists, in a separate repository, and it is what this PRD's
visualization half should be built on.** `ModelSEED/editEscher` (package `escher_edit`, tip
`6474698`, 2026-09-23 09:20) *generates* an Escher map from per-member exchange fluxes rather
than editing a hand-drawn one. Its `build_map.py` is explicit that it "closes the gap the
notebook left open".

The layout it produces is the community figure: member reactions stacked in one central
column with their midmarkers on a single axis; compounds only ever consumed in a left input
column; compounds only ever produced in a right output column; and compounds consumed by one
member and produced by another in two exchange lanes between them, placed level with the
members that trade them. Members are ordered by connectivity — busiest in the middle of the
column, quietest at the ends — every compound is collapsed to one node per block, and the
canvas is aspect-capped so the figure keeps a usable shape however large the community gets.

**The input format matches MSCommunity's cross-feeding output exactly, and this was checked
rather than hoped.** `build_member_reactions` takes
`{member: {compound: flux}}` documented as "negative = consumed, positive = excreted".
MSCommunity's `interactions()` fills each member column of `cross_feeding_df` from that
member's net flux on the metabolite and branches on `< Zero` for "metabolic consumption of a
species from the environment" and `> Zero` for "metabolic donations" (`mscommviz.py:130-215`).
Same convention, no sign flip. The adapter is a dict comprehension over the DataFrame's
member columns.

**`render_map_svg(..., html=True)` is a single call that writes both the finished SVG and an
interactive HTML figure**, and it needs neither graphviz, nor a browser, nor the network.
`escher_edit` depends on `beautifulsoup4` and `lxml`, with `shapely` and `pandas` as extras.
That is a strictly lighter dependency footprint than the graphviz path this PRD had
specified, which needed a Python package *and* a `dot` system binary *and* the ModelSEED
biochemistry database — see Q7, where the graphviz renderer is now dropped.

**THE PIPELINE WAS RUN, NOT JUST READ, AND IT FOUND A DEFECT IN THIS PRD'S OWN CLAIM.**
A three-member synthetic community on ModelSEED `cpd#####` ids was pushed through
`build_member_reactions` → `build_escher_map` → `render_map_svg(html=True)` in the scratchpad
on 2026-09-24. It works: 3 member reactions, 16 nodes, 16 segments, one block, a
2292×1734 canvas at 1.32:1; the two compounds constructed as cross-fed were the two detected
and dashed (4 of 4 segments); `member_box` present on 3 of 3 reactions; three distinct member
colours applied across 16 segments; SVG and interactive HTML both written, the HTML fetching
nothing external. That closes two of this PRD's own recorded unknowns — no map had been
generated, and `escher_edit` had no evidence of being run against ModelSEED ids.

It also disproved a claim this PRD had made. **`write_escher_svg` labels metabolite nodes from
`bigg_id`, not from `name`** (`render.py:185`), so passing `compound_names` does *not* put
display names on the static figure — the generated SVG's ten text elements were seven
`cpd#####` ids and three member names. The names do reach the JSON's `name` field, and
`interactive.py:491-493` composes tooltips as `"{name} ({bigg_id})"`, so the interactive page
*does* read `D-Glucose (cpd00027)`. Static figure: ids. Interactive page and Escher's own
viewer: names.

**And a naive label swap would break the layout, which is why this needed measuring.**
`MapStyle.fitted_column_dx` sizes the input/output columns from `_label_width` over the
compound **ids**, with an 8-character default — and a ModelSEED id is exactly 8 characters.
Measured on the same sample, the widest name is **2.88× the widest id** (270px vs 94px). So
the room reserved is sized for ids. The fix is upstream's own documented override:
`fitted_column_dx(names.values())` then `MapStyle(input_column_dx=-w, output_column_dx=w)`,
verified to be accepted and to widen the canvas from 2292 to 2644 on that sample. This is
what the `label_compounds` parameter does, and why it is opt-in.

**There is no Escher code in MSCommunity itself** — checked on 2026-09-23 and worth recording
so nobody looks there again: `mscommviz.py` is byte-identical between `b5f37c4b` and
`2dcff16`, and `grep -ri escher` returns nothing on `main`, nothing on `gpu` (2026-06-11,
eleven commits behind, no commits of its own), and nothing on `freiburgermsu/MSCommunity`.
The two packages are complementary, not overlapping: MSCommunity computes the fluxes,
`escher_edit` draws them.

**MSCommunity has been extracted out of ModelSEEDpy, and the two copies have diverged.**
`modelseedpy/community/mscommunity.py` (718 lines) still ships a class called `MSCommunity`
with `build_from_species_models` as a *method*, plus `compute_interactions`, `run` and
`steady_com`.
The standalone repo's `mscommunity/mscommsim.py` (881 lines, tip `b5f37c4`, 2026-08-27,
Andrew Freiburger) is the newer line: `build_from_species_models` is a module function in
`commhelper`, the runner is `run_fba`, and it adds `micom`, `regularization`, a
determinizing QP split, and a batched-LP layer with cpu/jax/cupy/pdlp backends.
Both packages export the name `MSCommunity` through `import *`.
KBUtilLib already depends on ModelSEEDpy, so **both classes will be importable in the same
process**, and the design has to name which one it means everywhere.

**The dependency mechanism this needs already exists and is not pyproject.**
`dependencies.yaml` plus `kbutillib/core/dependency_manager.py` is how KBUtilLib already
resolves `modelseedpy`, `ModelSEEDDatabase`, `cobrakbase` and
`cb_annotation_ontology_api` — GitHub-only checkouts in sibling directories, with
`get_dependency_path(name)` returning `None` rather than raising when absent
(`domains/thermo/thermo_predictors/base.py` is the worked example).
MSCommunity is a GitHub-only `setup.py` package at version 0.0.1 with no PyPI release, so
it fits that mechanism exactly and must not go into `pyproject.toml`.

**KBUtilLib's visualization surface is Escher, and Escher cannot read a community model
directly.** `EscherUtilsImpl.create_map_html2(model, map, output_path, flux=...)` renders a
self-contained HTML map with flux overlays, keyed on reaction ID.
`build_from_species_models` rewrites every member's intracellular compartment to `c{i}`
with `i` starting at 1, shares one extracellular compartment `e0` across all members, and
renumbers member biomass reactions to `bio{n}` (verified in
`mscommunity/commhelper.py:44-140`).
A single-organism Escher map is drawn against `_c0`/`_e0`.
So a community flux vector overlays onto an existing map only after a **per-member
projection** that rewrites `_c{i}` → `_c0`; there is no community-wide Escher map to draw
onto, and inventing one is out of scope.

**Abundance is not a constraint in MSCommunity — it is baked into the model.**
`set_abundance` rewrites the stoichiometry of the *primary biomass reaction* in place
(`self.primary_biomass.add_metabolites(..., combine=False)`), so calling it mutates the
model rather than adding a constraint, and `predict_abundances(update_abundances=True)`
silently rewrites it again mid-analysis.
Any wrapper that hides this will produce results that depend on call order.

**Four module-level** `@staticmethod` **decorators in** `mscommviz.py` **are a Python-version
landmine.** Lines 37, 84, 99 and 252 decorate *module-level* functions (`run_fba`,
`abundance_variability_analysis`, `interactions`, `visual_interactions`).
`staticmethod` objects only became directly callable in Python 3.10.
KBUtilLib declares `requires-python = ">=3.9"`, so on 3.9 every call into `mscommviz`
raises `TypeError: 'staticmethod' object is not callable` — including MSCommunity's own
`MSCommunity.interactions`, which imports that function.
Verified by reading the file and by testing `staticmethod` call semantics locally.

**There is existing KBase-side prior art that is NOT reusable here.**
`KB-ModelSEEDCommunity/lib/ModelSEEDCommunity/mscommunitymodule.py` is an SDK app wrapping
the *old* ModelSEEDpy community code behind a KBase report. It shows the intended user
workflow, but it is app-shaped (workspace refs in, HTML report out) rather than
library-shaped, and it is pinned to the superseded implementation.

**The external scan confirms the two decisions it was most likely to overturn, and widens
a third.** It recommends "return typed results with provenance … avoid raw DataFrames only",
which is Q2 as decided. On visualization it reports that layering multiple members onto one
Escher map "becomes confusing beyond two or three members" and that community models need
"per-member color channels or separate overlays" — separate overlays being exactly the
projection Q4 chose. What it adds is that a visualization layer should emit a **graph
object** alongside its renderers, not only rendered files, and that cross-feeding layouts
become unreadable past roughly 30–50 nodes. Both are folded in.

**Two findings changed the design.** First, every surveyed toolkit that lets a caller both
fix abundances and constrain member growth is reported to produce infeasibility that users
misread as a modelling failure — so the scan's advice is to *validate incompatible
combinations with clear errors*. `run_community_fba` accordingly refuses a positive
`min_member_growth` combined with caller-supplied fixed abundances rather than handing back
an infeasible LP. Second, the scan's packaging recommendation is to pin a GitHub-only
dependency **to a tested commit SHA, not a branch**; `dependencies.yaml` as it stands
records only a path and a git URL, so this design adds a `commit` key.

### Links

- [Prototype community map](research/prototype-community-map.svg) — generated 2026-09-24 from a
  three-member synthetic community on ModelSEED ids, with its
  [map JSON](research/prototype-community-map.json) and
  [interactive page](research/prototype-community-map.html). This is the evidence behind Q4 and
  behind the `label_compounds` decision; open the SVG to see what the figure actually looks
  like.
- [Confront round 1](research/confront-round-1.md) and
  [confront round 2](research/confront-round-2.md) — the cross-family stall reports and, in
  `data.json`, the adjudication of each with reasons for every decline.
- [External scan: community modeling and visualization prior art](research/external-scan.md)
— literature, competing toolkits (MICOM, SteadyCom, SMETANA, COMETS, PyCoMo, MMinte,
BacArena, gapseq), the abundance-semantics trap, visualization prior art, and
GitHub-only-dependency practice.
Produced by Maestro task `task-c1a370b4` on h100 (codex backend) and verified from the
task branch rather than from its reported status.



### Quality report

**What was searched.** The whole of the MSCommunity repository at tip `b5f37c4`
(`mscommsim.py`, `mscommviz.py`, `commhelper.py`, `mskineticsfba.py`, `batched_lp.py`,
`__init__.py`, `setup.py`); the diverged copy under `ModelSEEDpy/modelseedpy/community/`;
KBUtilLib's `toolkit.py` facade, `core/capability.py`, `core/dependency_manager.py`,
`compartments.py`, `domains/modeling/*` (notably `ms_fba_utils.py` and `kb_model_utils.py`),
`domains/notebook/escher_utils.py`, both domain READMEs, `pyproject.toml`,
`dependencies.yaml`, `tests/conftest.py` and the `tests/modeling/` layout; the
`KB-ModelSEEDCommunity` SDK app; and the KBUtilLib git log and working-tree state.

**What was NOT searched, and why.** The external half was dispatched to h100
(`task-c1a370b4`) and returned before commit; it covers nine competing toolkits, the
abundance-semantics failure modes, visualization libraries and scaling limits, and
GitHub-only-dependency practice, with eighteen cited URLs.
It is, however, **degraded in a specific and visible way**: every inline code span in the
returned markdown is empty, so class and method names were stripped — the file says
"expose a clear  façade" where it meant a named class. The prose survives and the
argument is followable, but no API name it cites can be quoted from it, and none has been
quoted into this PRD. Anything below that names an upstream symbol comes from reading
source, not from the scan.
The scan also states plainly that its failure-mode claims derive from issue trackers
without linking specific issues, so the MICOM/SteadyCom infeasibility reports are
second-hand.

No MSCommunity code was *executed*. The claims about compartment renaming, abundance
mutation and the `@staticmethod` hazard come from reading source, not from running it;
the first two are structural enough to be safe, the third was independently verified
against Python's own `staticmethod` semantics.
No profiling was done; the confront round removed the batched-LP passthrough claim
entirely (Q10), so nothing in the design now rests on an unmeasured performance argument.

**Confidence.** High on the module's placement, construction and dependency handling —
those follow patterns already in the repo.
Medium on the result-object shape and on the Escher projection, which are the two places
this design invents rather than follows.

## Revision Log

- **Round 0.4 — 2026-09-24 (Chris's decision folded; advanced to `ready`).**
  Q7 settled by Chris in his own words — the graphviz renderer is dropped outright — which was
  the last open judgement call in the bundle. The taskplan was already written and validated
  (5 tasks, 4 phases, all `in_context`); no change was needed for this round.
  Advanced to `ready` **on Chris's direct instruction** ("write the task plan and set this as
  ready for dev"), NOT after a review round. `review_rounds` is still **0** and is deliberately
  left that way: the field records whether a review round ran, and one did not. See Further
  Notes for what that means for whoever dispatches this.

- **Round 0.3 — 2026-09-24 (prototype + confront round 2, NOT a review round).**
  Ran the `escher_edit` pipeline rather than only reading it, which closed two recorded unknowns
  and **found a defect in this PRD's own claim**: `compound_names` does not put names on the
  static figure, and the layout reserves label room sized from the ids, so names need a
  column-widening pass. That is now the `label_compounds` parameter, defaulting to `"id"`.
  Confront round 2 (`task-80a75ae8`) returned 29 stall points: 21 folded, 5 declined, 3 already
  satisfied. Its best catch was that `cross_feeding_graph` as a `DiGraph` would silently
  overwrite parallel exchanges — now a `MultiDiGraph`. Four of the five declines were the
  adversary inventing upstream APIs its worktree cannot see. Twelve exact literals are now bound
  in one place. `review_rounds` still **0**.

- **Round 0.2 — 2026-09-24 (editEscher folded, NOT a review round).**
  Chris supplied `ModelSEED/editEscher`, which is where the community Escher viz actually
  lives. This rewrites the visualization half:
  Q4 now generates a **community exchange map** through `escher_edit` as the primary view,
  keeping the per-member pathway projection as the complementary second view; the
  flag-not-question from round 0 is answered — he wanted the whole community at once.
  Q7 changes from "what happens when graphviz is missing" to **dropping the graphviz
  renderer entirely**, because `escher_edit` draws the same relationships better with no
  system binary. Q6 now carries two GitHub-only dependencies.
  `review_rounds` still **0**.

- **Round 0.1 — 2026-09-23 (upstream refresh, NOT a review round).**
  Re-pinned MSCommunity from `b5f37c4b` to `2dcff16` after five commits landed the same day.
  Three fixed defects this design would have hit (float abundances raised `TypeError`;
  `gapfill`'s solver string silently forced ATP-gapfilling; build-time solver scaling), so
  the pin is now a floor. One added `close_member_drains`, which is a genuine new decision
  and is recorded as Q5 together with the trap it sets for `test_member_growth`.
  Checked for the community Escher viz that prompted this refresh: it does not exist
  anywhere in the repository or its fork, so Q4 is unchanged.
  `review_rounds` stays **0** — nothing here came from Chris, and counting an upstream
  refresh as his review round would open the ready gate on a document he has not read.

- **Round 0 — 2026-09-23 (draft).** Initial single-pass draft.
Wraps the standalone MSCommunity package (not `modelseedpy.community`) as a new
`domains/modeling/ms_community_utils.py` reached through `kbu.community`.
Scope covers construction, simulation, and visualization (cross-feeding table, graph and
rendered network, plus a per-member Escher projection); dynamic/kinetic FBA is deferred.
The h100 external scan returned during authoring and was folded before commit: it
confirmed the typed-result and per-member-overlay decisions, added `cross_feeding_graph`,
added a refusal on fixed-abundances-plus-growth-floor, and added a pinned commit SHA to
the dependency entry.
Confront round 1 (cross-family codex, `task-63133ccb`) also ran before commit: 18 stall
points, 14 folded, 2 answered by changing the design (FVA and the batched-LP backends are
no longer claimed as exposed), 2 declined with reasons. Its sharpest catch was that the
new exceptions should derive from the existing `kbutillib.core.errors` hierarchy rather
than bare `Exception`; its most consequential was that nothing persisted the community
provenance across a save/load round trip.



## Problem Statement

A modeler working in a KBUtilLib notebook can reconstruct, gapfill, simulate and visualize
a *single-organism* model without leaving the toolkit:
`kbu.recon` builds it, `kbu.fba` runs it, `kbu.escher` draws it, `kbu.model` loads and
saves it against the KBase workspace.

The moment the question becomes a *community* question — do these three organisms grow
together, what do they trade, which member is limiting — the toolkit stops helping.
The capability exists, in MSCommunity, but reaching it means leaving KBUtilLib: cloning a
GitHub-only package, working out which of two same-named `MSCommunity` classes is the
current one, assembling member models by hand, and then discovering that the community
model cannot be drawn on any of the Escher maps the toolkit already knows how to render.

The practical consequences are concrete:

1. Community work is done in one-off scripts that are not reproducible and not shared.
2. The wrong `MSCommunity` gets imported, and the failure is silent — the older class has
  most of the same method names, so the script runs and returns different numbers.
3. Abundance semantics get confused. MSCommunity treats abundance as something that is
  *written into the biomass reaction*, so a script that predicts abundances and then runs
   another simulation has silently changed the model underneath itself.
4. Community results cannot be visualized with the tools the rest of the toolkit uses, so
  they are inspected as raw flux dictionaries.



## Solution

A new KBUtilLib module, `kbutillib.domains.modeling.ms_community_utils`, reached through a
lazy facade property `kbu.community`, that wraps the standalone MSCommunity package behind
a small, stable interface covering three verbs: **build**, **run**, **display**.

```python
kbu = KBUtilLib()

# BUILD — merge single-species models into a community
comm = kbu.community.build_community(
    member_models=[mdl_a, mdl_b, mdl_c],
    abundances={"iML1515": 0.5, "Bth": 0.3, "Ec": 0.2},
    model_id="three_member_gut",
)

# RUN — simulate it
result = kbu.community.run_community_fba(comm, media=media, pfba=True)
result.community_growth          # float
result.member_growth             # {member_id: float}
result.exchange_fluxes           # {exchange_rxn_id: float}
result.trustworthy               # False when the LP was sub-optimal

pred = kbu.community.predict_abundances(comm, media=media)   # does NOT mutate comm
solo = kbu.community.test_member_growth(comm, media=media)   # solo vs interacting

# DISPLAY -- data, then the two figures
table, exchanged = kbu.community.cross_feeding_table(comm, result)   # DataFrames
g = kbu.community.cross_feeding_graph(comm, result)                  # networkx.DiGraph

# the community exchange map: who trades what with whom, whole community at once
kbu.community.render_community_map(comm, result, output_path="community.json")
#   -> community.json  (portable Escher map)
#      community.svg   (finished figure: member boxes, member colours, dashed cross-feeding)
#      community.html  (interactive: hover an edge/compound/member to follow its exchanges)

# several media as captioned blocks in ONE map, members keeping their colours across blocks
kbu.community.render_community_map(comm, {"glucose": r1, "acetate": r2},
                                   output_path="both_media.json")

# the per-member pathway view: what one member's internal metabolism is doing in context
kbu.community.render_member_map(comm, "iML1515", map="core", result=result,
                                output_path="iML1515_in_community.html")
```

**Two figures, answering two different questions.** This is the shape of the display half
and it is worth stating plainly, because the two are easy to confuse:

- `render_community_map` draws the **exchange** level — every member as a box, every
  metabolite it consumes or excretes as a node, cross-feeding edges dashed, the whole
  community in one picture. This is what you reach for first, and what you put in a paper.
  It is *generated*: there is no map to find or draw by hand.
- `render_member_map` draws the **pathway** level for one member — its internal fluxes, as
  realized inside the community, projected onto an ordinary single-organism Escher map you
  already have. This answers "what is member X actually doing", which the exchange map
  cannot show because it carries no intracellular reactions at all.

The module is a **deep module**: a short interface over a large amount of behavior.
Behind `build_community` sits compartment renaming, biomass-reaction renumbering, abundance
normalization and kinetic-package construction; behind `run_community_fba` sits solver
selection, sub-optimality detection and per-member flux attribution; behind
`render_community_map` sits the DataFrame-to-`fluxes_by_member` adapter, compound-name
resolution, member-colour stability across blocks, and a three-stage
build/assemble/render pipeline; behind `render_member_map` sits the compartment-index
projection that makes a community flux vector legible to a single-organism map.

Three properties hold throughout:

- **Abundance is an input unless you ask for a prediction.** Nothing in this module
rewrites a community's abundances as a side effect of another call.
- **Every simulation result says whether it can be trusted.** MSCommunity logs sub-optimal
solutions rather than raising; the wrapper surfaces that as a field.
- **Absent optional dependencies degrade, they do not crash.** `kbu.community.available`
is `False` with a `unavailable_reason` naming what is missing, exactly as
`MSFBAUtilsImpl` already does.



## User Stories

1. As a modeler, I want to merge a list of single-species COBRA models into one community
  model with a single call, so that I do not have to reproduce MSCommunity's compartment
   and biomass renaming by hand.
2. As a modeler, I want to supply relative abundances when I build the community, so that
  the community biomass reaction reflects the composition I measured.
3. As a modeler, I want to build a community without supplying abundances and have them
  default to uniform, so that I can get a first answer quickly.
4. As a modeler, I want to load member models from the KBase workspace by reference and
  build a community from them, so that community work uses the same object sources as the
   rest of my notebook.
5. As a modeler, I want to save a community model back to the workspace, so that a
  collaborator can load exactly what I simulated.
6. As a modeler, I want to export a community model to SBML, so that I can hand it to a
  tool outside the ModelSEED ecosystem.
7. As a modeler, I want to run community FBA on a named medium and get community growth,
  per-member growth, and exchange fluxes in one result object, so that I do not have to
   pick those apart from a raw solution.
8. As a modeler, I want the result to tell me when the LP was sub-optimal, so that I do not
  publish fluxes MSCommunity itself considers untrustworthy.
9. As a modeler, I want to run a pFBA community simulation, so that the flux distribution
  is parsimonious rather than arbitrary among equivalent optima.
10. As a modeler, I want to predict member abundances from a medium, so that I can compare
  predicted composition against 16S or metagenomic data.
11. As a modeler, I want abundance prediction to leave my community object unchanged unless
  I explicitly ask it to update, so that a sequence of analyses is order-independent.
12. As a modeler, I want to run a MICOM-style tradeoff simulation, so that I can compare a
  cooperative optimum against a more realistic self-interested one.
13. As a modeler, I want a clear error when MICOM is requested without a QP-capable solver,
  naming which solvers would work, rather than an optlang exception.
14. As a modeler, I want to test each member's growth alone and in the community, so that I
  can tell cross-feeding dependence from independence.
15. As a modeler, I want to gapfill a community model on a medium, so that a community that
  cannot grow can be made to grow and I can see what was added.
16. As a modeler, I want the cross-feeding exchange table as a DataFrame, so that I can
  filter, join and export it like any other result.
17. As a modeler, I want a generated community exchange map — every member as a box, every
  compound it consumes or excretes as a node, cross-feeding edges dashed — so that I can see
  the whole community's trade at a glance without drawing a map by hand.
18. As a modeler, I want that map as an interactive page as well as a static figure, so that
  I can hover a compound and follow which members produce and consume it.
19. As a modeler, I want to view one member's fluxes *as realized inside the community* on
  a standard Escher map, so that I can use the maps I already have.
20. As a modeler, I want that member view to be visibly labelled as a projection, so that I
  do not mistake it for a single-organism simulation.
21. As a modeler, I want `kbu.community.available` to tell me whether the module can run and
  why not, so that I can diagnose a broken environment without reading a traceback.
22. As a modeler, I want the module to fail loudly and specifically if it picks up
  `modelseedpy.community.MSCommunity` instead of the standalone package, so that the
    divergence never silently changes my results.
23. As a maintainer, I want MSCommunity declared in `dependencies.yaml` like the other
  GitHub-only dependencies, so that a fresh checkout resolves it the same way.
24. As a maintainer, I want the module's methods registered as capabilities, so that they
  appear in the CLI/MCP/API transports without extra wiring.
25. As a maintainer, I want tests that run without MSCommunity installed, so that CI stays
  green on a machine that has only the core dependencies.
26. As a modeler, I want the cross-feeding network as a `networkx` graph object, so that I
  can filter it, count it, or render it with a library of my own choosing rather than
    being confined to the one picture the module draws.
27. As a modeler, I want to be told when I have over-specified the problem — fixed
  abundances together with a per-member growth floor — so that I get a named refusal
    instead of an infeasible LP I have to diagnose.
28. As a modeler, I want the abundances I supply to actually bind, so that the member growth
    rates and kinetic coefficient I read off the solution reflect the composition I declared
    rather than a member synthesising biomass and discarding it.
29. As a modeler, I want solo member growth to be measured correctly even on an
    abundance-bound community, so that "grows alone" and "grows in the community" remain
    comparable instead of one of them silently reading zero.
30. As a modeler, I want to draw several media as captioned blocks in one map, so that I can
  compare conditions side by side in a single figure.
31. As a modeler, I want each member to keep the same colour across every block and every
  map in a series, so that I can read one member across conditions.
32. As a modeler, I want to colour members by taxonomic group with a legend, so that a
  forty-member community reads as phyla rather than as forty indistinguishable boxes.
33. As a modeler, I want compound names available on the figure — in the interactive
  tooltips by default, and on the static figure when I ask for it — so that a reader who does
  not know the ModelSEED database can follow it.
34. As a modeler, I want asking for names on the static figure to widen the layout to fit
  them, so that I get readable labels rather than labels overlapping the nodes.
35. As a modeler, I want the adapter between the community fluxes and the map builder to be
  a public method I can inspect, so that when a figure looks wrong I can check the numbers
  going into it rather than guessing.
36. As a modeler, I want to be warned rather than silently given an unreadable figure when
  the community is too large for the layout to stay legible.
37. As a maintainer, I want the MSCommunity dependency pinned to a tested commit SHA and
  drift reported as a warning, so that a divergent local checkout is visible without
    blocking anyone who is deliberately working ahead of the pin.



## Implementation Decisions



### Module placement and shape

The module is `src/kbutillib/domains/modeling/ms_community_utils.py`.
It follows the two-class convention already used by every module in that domain:

- `MSCommunityUtils(KBModelUtils)` — the implementation, inheriting the model/media/
workspace surface it needs (`get_model`, `get_media`, `save_model`, `_parse_id`).
- `MSCommunityUtilsImpl` — the composition wrapper: holds `env` and sibling `Impl`s,
constructs the delegate inside `try/except`, exposes `available` /
`unavailable_reason` / `__dir__` / `__getattr__`, exactly as `MSFBAUtilsImpl` does.

`__all__ = ["MSCommunityUtils", "MSCommunityUtilsImpl"]`.

The facade gains a lazy property in `toolkit.py`:

```python
@property
def community(self) -> MSCommunityUtilsImpl:
    if self._community is None:
        from .domains.modeling.ms_community_utils import MSCommunityUtilsImpl
        self._community = MSCommunityUtilsImpl(self.env, self.model, self.fba, self.escher, self.biochem)
    return self._community
```

with `self._community = None` added to `__init__` alongside the other backing fields, and a
`TYPE_CHECKING` import next to the other `.domains.modeling` imports.
`kbutillib/__init__.py` gains a guarded re-export using the existing `_import_error`
pattern, exporting the symbol `MSCommunityUtils` (the legacy class, matching how
`MSFBAUtils` and `MSReconstructionUtils` are re-exported there — `*Impl` classes are reached
through the facade and are *not* re-exported at package top level).
`domains/modeling/README.md` gains a row in its module table.

### Resolving MSCommunity

MSCommunity is declared in `dependencies.yaml`:

```yaml
  mscommunity:
    path: "../MSCommunity"
    git: "https://github.com/ModelSEED/MSCommunity.git"
    commit: "2dcff16f8e20a5b2a2acbb28b60a8ad38ec924fc"
  escher_edit:
    path: "../editEscher"
    git: "https://github.com/ModelSEED/editEscher.git"
    commit: "6474698c67e8b9a1e8ab44c38686b27821d20389"
```

Note the **directory and import names differ**: the repository is `editEscher`, the
distribution is `escher-edit`, and the import is `escher_edit` under a `src/` layout. So
`_import_escher_edit()` must add `<path>/src` to `sys.path`, not `<path>` — the one detail
most likely to produce a confusing `ModuleNotFoundError` on a checkout that is present and
correct. `_import_mscommunity()` adds the repo root, because MSCommunity is a flat layout.
Both follow the same shape otherwise: try a plain import, fall back to the declared path,
return `None` rather than raising, and warn on a commit mismatch without refusing.
`escher_edit` needs **no provenance gate** — nothing else in the ecosystem exports that name,
so the hazard Q3 guards against does not exist here.

**The pin is a FLOOR, not a preference.** Three defects fixed on 2026-09-23 sit directly
under this design: before `b14f50f`, `abundances={model_id: float}` raised `TypeError` in
`MSCommunity.__init__`, and `gapfill(solver=...)` landed on `MSGapfill`'s `atp_gapfilling`
positional so any truthy solver string silently ran an ATP gapfill instead of the one
asked for. A checkout older than `2dcff16` does not merely lack improvements; it fails or
lies on two of this module's own code paths. The mismatch warning below therefore matters
more than a normal pin would.

Import resolution is a single private helper, `_import_mscommunity()`, which:

1. attempts `import mscommunity` normally (covers the case where it is pip-installed);
2. on `ImportError`, calls `get_dependency_path("mscommunity")` and, when that returns a
  path containing a `mscommunity/` package directory, prepends it to `sys.path` and
   retries;
3. returns the module or `None` — never raises.

It then **asserts provenance**, with both sides of the rule bound so the allowlist is
neither too strict nor too lax:

- **ACCEPT** when the resolved `MSCommunity` class has `__module__` starting with
`mscommunity.` — which admits `mscommunity.mscommsim.MSCommunity` and any future
re-export inside that package.
- **REJECT** otherwise, and specifically when it starts with `modelseedpy`.

On rejection the module reports itself unavailable with a stable message, quoted here
because the tests assert on it:
`"resolved MSCommunity is modelseedpy.community.MSCommunity (the superseded copy), not the standalone mscommunity package"`, with the actually-resolved `__module__` appended.
This check is the whole defense against the divergence, so it is a hard gate, not a warning.

**Commit pinning.** The `dependencies.yaml` entry carries
`commit: "2dcff16f8e20a5b2a2acbb28b60a8ad38ec924fc"`, and the SHA is also a module constant,
`PINNED_MSCOMMUNITY_COMMIT`. `_import_mscommunity()` compares it best-effort against the
resolved checkout's HEAD and **logs a warning on mismatch — it never raises and never
refuses the import.** The check lives here and not in `DependencyManager`, which stays
untouched: pinning is for visibility, not enforcement, so a developer deliberately working
ahead of the pin is never blocked.

`_import_mscommunity` is importable at
`kbutillib.domains.modeling.ms_community_utils._import_mscommunity` and tests are expected
to patch `sys.modules` to simulate both the missing and the wrong-package cases.

All MSCommunity symbols are imported **inside** `_import_mscommunity()`, never at module
scope, so `import kbutillib` never requires MSCommunity, cobra, networkx or `escher_edit`.

### The community handle

`build_community` returns a `CommunityModel` dataclass owned by KBUtilLib, not a bare
`MSCommunity` object:

```python
@dataclass
class CommunityModel:
    mscomm: Any                        # the underlying mscommunity.MSCommunity
    member_ids: list[str]              # in community index order (index i -> compartment c{i+1})
    abundances: dict[str, float]       # normalized, as built
    source_model_ids: dict[str, str]   # member_id -> id of the model it was merged from
    kinetic_coeff: float
```

The underlying object is exposed rather than hidden: `comm.mscomm` is the escape hatch for
anything this module does not wrap, and no method is added purely to proxy one that already
exists upstream.
What the dataclass adds is the *provenance* MSCommunity discards — `build_from_species_models`
mutates copies of the member models and renames their compartments, so after the merge there
is no way to recover which input model became which community member.
`member_ids` and `source_model_ids` are captured at build time because they cannot be
reconstructed afterwards.

### Construction

```
build_community(member_models, abundances=None, model_id=None, name=None,
                kinetic_coeff=750, element_limits=None, printing=False,
                build_solver="glpk", final_solver=None) -> CommunityModel
load_community(id_or_ref, ws=None, member_ids=None) -> CommunityModel
save_community(comm, workspace=None, objid=None, suffix=None)
export_community_sbml(comm, path)
```

`member_models` accepts cobra models, `MSModelUtil`s, or workspace references — resolved
through `self._check_and_convert_model` and `self.get_model`, matching how the rest of the
modeling domain accepts models.

**`close_member_drains` is derived, not exposed as a free parameter.** It is set to
`abundances is not None` — the same fact as `abundances_were_supplied` — per Q5, and
recorded on the handle. The caller does not get a third thing to reason about: saying
"these are the abundances" is the same statement as "make them bind."

`build_solver` and `final_solver` are forwarded to `build_from_species_models` with
upstream's defaults (`"glpk"` and `None`), which preserve behavior. They are reachable
rather than hard-coded because incremental constraint addition into optlang's Gurobi
interface is superlinear: upstream populates the merged model under GLPK and does one clean
rebuild at the end, reporting equivalence verified at 32, 64 and 128 members. A caller
assembling a large community is who needs them.
`abundances` is a `{model_id: float}` mapping; it is normalized to sum to 1 and stored.
When omitted, members are uniform.

`save_community` **writes our own provenance into the model, and** `load_community` **reads it
back.** This is the round-trip the rest of the design depends on and it must be bound
explicitly: `build_community` captures `member_ids` and `source_model_ids` because upstream
destroys them, and without persisting that capture a save/load cycle throws it away again —
after which `render_member_map` cannot resolve a member's source model and raises. So
`save_community` writes

CSH: Can we add a JSON export that exports the model using a custom JSON format that puts community model metadata (e.g. member names, abundances, element limits etc) and nests the cobrapy json of the community model.

```
model.notes["kbutil.community"] = json.dumps({
    "schema_version": 1,
    "member_ids": [...],
    "source_model_ids": {...},
    "abundances": {...},
    "kinetic_coeff": 750,
    "abundances_were_supplied": bool,
})
```

as a JSON **string** (workspace `notes` values are not reliably structured), and the object
is saved through the existing `self.save_model` path as an ordinary `KBaseFBA.FBAModel` — a
community model *is* an FBA model, and inventing a new workspace type would make it invisible
to every tool that already reads models.

`load_community` reads `model.notes["kbutil.community"]` first and reconstructs the handle
from it. Falling back, it accepts an explicit `member_ids` argument, then upstream's own
`model.notes["member_biomass_cpds"]`. When none of the three is available it raises
`ValueError` naming all three rather than guessing — and a handle rebuilt from the second or
third route has an empty `source_model_ids`, which `render_member_map` reports as a named
`ValueError` rather than a traceback.

### Simulation

```
run_community_fba(comm, media=None, pfba=False, min_member_growth=0.0)
                  -> CommunityFBAResult
predict_abundances(comm, media=None, pfba=True, regularization=True,
                   update=False, determinize=False) -> dict[str, float] | None
run_micom(comm, media, tradeoff=0.6) -> CommunityFBAResult
test_member_growth(comm, media=None, interacting=True) -> pandas.DataFrame
gapfill_community(comm, media=None, target=None, templates=None, models=None,
                  solver="glpk") -> GapfillResult
```

`CommunityFBAResult` is a dataclass carrying `status`, `trustworthy`,
`community_growth`, `member_growth`, `exchange_fluxes`, `fluxes` (the full
`pandas.Series`), `media_id`, `kinetics_relaxed` and `notes`.
`trustworthy` is `not comm.mscomm.suboptimal_solution` — MSCommunity sets that flag and logs
an error rather than raising, so a wrapper that does not surface it hands the caller numbers
its own author marked untrustworthy.

`media_id` **is bound, not incidental.** When `media` is a name or reference it is resolved
through `self.get_media` and `media_id` is the resolved object's id; when `media` is already
a media object, its id; when `media` is `None`, the literal string `"<model-default>"`.
Provenance is the point — two results that cannot say what medium they came from cannot be
compared.

`kinetics_relaxed` **and** `notes` **exist to recover what upstream only prints.** MSCommunity
announces its most consequential silent behaviors on stdout rather than raising or recording
them — the kinetic-constraint removal described under Gotchas is a `print`, not a flag. So
every delegate call is wrapped in `contextlib.redirect_stdout`, the captured text is scanned
for upstream's known markers (`"Kinetic constraints disabled"`, `"doesn't grow"`), the
matches are re-emitted through `logging` and recorded in `notes`, and `kinetics_relaxed` is
set when the kinetics marker appears. This does two jobs at once: it makes a
constraint-relaxed result distinguishable from a constrained one, and it stops MSCommunity's
print noise from landing uninvited in a notebook. Captured output that matches nothing is
logged at DEBUG and discarded.

**FVA is not exposed in v1.** Upstream's `run_fba` accepts an `fva_reactions` argument whose
result shape is unspecified and which `CommunityFBAResult` has no field for. Rather than
invent a contract for it, the parameter is omitted; `comm.mscomm.run_fba(..., fva_reactions=...)`
remains reachable through the escape hatch for anyone who needs it.

`min_member_growth` maps to MSCommunity's `minMemGrwoth` SteadyCom-style floor.
It defaults to `0.0`, matching upstream's deliberate choice: the historical default of 1 was
applied in a way that had no effect on the LP, so enforcing a floor by default would silently
change every existing result.

**A positive** `min_member_growth` **combined with caller-supplied fixed abundances raises**
`CommunitySolverError` **before the solve.** This is the scan's most actionable finding: every
surveyed toolkit that permits both reports users hitting infeasibility and reading it as a
modelling failure rather than as an over-specified problem. Fixing composition *and*
imposing a per-member growth floor over-determines the system in the common case, and an
infeasible LP is a far worse message than a named refusal. The error says which two inputs
conflict and that dropping either one resolves it.

`predict_abundances` **copies the community's abundance state and restores it** unless
`update=True`. Upstream's `update_abundances` parameter rewrites the primary biomass
reaction in place; that mutation is the single most confusing behavior in the package, so
the default here is non-mutating and the mutating path is opt-in and named.
What is snapshotted is bound exactly, because restoring too little leaves hidden mutation
and restoring too much fights upstream's own bookkeeping: the `comm.mscomm.abundances`
mapping, and the `{metabolite: coefficient}` stoichiometry of **the single community
primary biomass reaction** (`comm.mscomm.primary_biomass`) — which is the only reaction
`set_abundance` touches. Members' own `primary_biomass` reactions are not modified by
`set_abundance` and are not snapshotted. Restore happens in a `finally` block via
`add_metabolites(..., combine=False)`.

**`test_member_growth` must reopen the drains it asks about, and this is not optional.**
With `close_member_drains=True`, `test_individual_species(interacting=False)` reads **zero
growth for every member** — silently, with no error. The mechanism is exact: it disables the
other members, which zeroes `bio1`, and `bio1` is then the only outlet for the member under
test's biomass because its drain is shut. Upstream fixed this for `_solo_max_batch`, which
restores the target's drain from the `biomass_drain_bounds` snapshot taken at construction
(`mscommsim.py:886-888`), and did **not** fix it for `test_individual_species`, which has no
drain handling at all (`mscommsim.py:387-400`). Verified by reading both.

So `test_member_growth` reopens every member's drain from `member.biomass_drain_bounds`
inside the model's context manager before delegating, and the restoration is automatic on
exit. It does this only when `comm.mscomm.close_member_drains` is True and
`interacting=False` — the two conditions that together produce the false zeros — and it
records in the returned DataFrame's `.attrs` that drains were reopened for the measurement,
so a solo growth rate is never silently comparable with a coupled one.

`run_micom` takes **one medium and returns one result**, not a list. Upstream's `micom()`
accepts either a single medium or a sequence and always returns a list; wrapping that shape
through would make every caller unwrap a one-element list for the overwhelmingly common case.
A caller sweeping media loops, which is also what `render_community_map` wants as its
`{label: result}` input.

`run_micom` checks for a QP-capable solver *before* dispatching and raises a
`CommunitySolverError` naming the acceptable solvers (`gurobi`, `cplex`, `osqp`, or the
`hybrid` HiGHS+OSQP path) when none is available, rather than letting optlang raise from
inside the tradeoff loop.
**Detection reuses upstream's own table rather than reimplementing it**: `_QP_CAPABLE` and
`_pick_qp_backend` are MODULE-level names in `mscommunity.mscommsim` — not attributes on the
`MSCommunity` instance — so the check reads them off the imported module, and falls back to
testing `optlang.available_solvers` against the same four names only if upstream ever removes
them. Reimplementing the table locally would let it drift from the one upstream actually
uses to pick a backend.

### Visualization

```
cross_feeding_table(comm, result=None, media=None, flux_threshold=1.0,
                    ignore_mets=None) -> tuple[DataFrame, DataFrame]
cross_feeding_graph(comm, result=None, min_abs_flux=1e-4) -> networkx.DiGraph
fluxes_by_member(comm, result=None, min_abs_flux=0.0) -> dict[str, dict[str, float]]
render_community_map(comm, result_or_results, output_path, *, min_abs_flux=0.0,
                     skip_amino_acids=False, label_compounds="id",
                     member_groups=None, member_legend=None, style=None,
                     svg=True, html=True, map_name=None) -> CommunityMapArtifacts
render_member_map(comm, member_id, map, output_path, result=None,
                  **escher_kwargs) -> Path
```

#### The data methods

`cross_feeding_table` returns MSCommunity's `(cross_feeding_df, exchanged_mets_df)` pair
with `visualize=False`. `msdb` is passed to upstream as the keyword `msdb_path=` (or `msdb=`
for an already-loaded object), never positionally — a positional call breaks the moment
upstream's signature moves. It is **not needed for the table**: upstream touches `msdb` only
inside its `if visualize:` branch (`mscommviz.py:99-252`). The confront round proposed an
`MSCOMMUNITY_MSDB_PATH` environment-variable fallback; **declined**, as untestable dead code
that would hide a real signature break behind a silent fallback.

`cross_feeding_graph` builds a **`networkx.MultiDiGraph`** from the cross-feeding DataFrame:
one node per member plus an `Environment` node, and one edge per
donor→recipient→**metabolite** triple, keyed on the metabolite id and carrying `metabolite`,
`flux` and `abs_flux`. Edges below `min_abs_flux` are dropped.

**It must be a MultiDiGraph, not a DiGraph**, and the confront round caught this: two members
commonly trade several metabolites, and a `DiGraph` holds at most one edge per ordered pair, so
every exchange after the first would be **silently overwritten**. A caller counting exchanges
would get the number of trading *pairs* and have no way to tell. Using the metabolite id as the
edge key also makes a specific transfer addressable.

It exists because the external scan's clearest visualization finding is that a layer should
emit a graph object and not only rendered files: a caller who wants Plotly, Cytoscape, their own
layout, or simply an edge count should not have to go through a renderer to get one.

`fluxes_by_member` is the **adapter**, and it is public because it is the seam between the
two packages and the thing most likely to need inspecting when a figure looks wrong. It
converts the cross-feeding DataFrame into `escher_edit`'s input shape,
`{member: {compound_id: flux}}`. No sign flip is applied, and that is a checked fact rather
than an assumption: `escher_edit.build_member_reactions` documents "negative = consumed,
positive = excreted", and MSCommunity fills each member column from that member's net flux,
branching on `< Zero` for consumption and `> Zero` for donation. The `Environment` column is
dropped — `escher_edit` derives its own input and output columns from which members consume
and produce each compound, so passing `Environment` as a member would draw the medium as an
organism.

#### `render_community_map` — the community exchange map

This is the primary community figure and the one new integration in the PRD. It is a
three-stage pipeline over `escher_edit`:

1. **Per-condition member reactions.** For each result, `fluxes_by_member` then
   `escher_edit.build_member_reactions(fbm, model_id=<condition label>,
   compound_names=..., min_abs_flux=..., skip_names=..., skip_ids=...)`. Each member becomes
   one net organism reaction: consumed compounds as reactants, excreted as products.
2. **Assemble.** `escher_edit.build_escher_map(blocks, compound_names=..., map_name=...,
   style=...)` returns the `[header, body]` Escher map. Passing `{label: result}` instead of
   a single result produces **one captioned block per condition in one map** — which is how
   a media comparison is drawn, and compounds are collapsed within a block and never across
   blocks, so each condition keeps its own node set.
3. **Render.** `escher_edit.render_map_svg(escher_map, out_path, dashed=True, layout=style,
   html=html, member_colors=..., member_groups=..., member_legend=...)` writes the finished
   SVG — member boxes, member colours, cross-feeding edges dashed — and, with `html=True`,
   the interactive page beside it in the same call.

**Compound display names come from `self.biochem`, not from the DataFrame.** This is a real
gap rather than a preference: `interactions()` builds a `"Metabolites/Donor"` name column and
then **drops it** before returning, so `cross_feeding_df` carries bare ModelSEED ids only.
`escher_edit` wants `{compound_id: display_name}` for node labels and for matching
`skip_names`. The module resolves them through the biochemistry sibling already on the
`Impl`, and falls back to using the ids as their own names when biochem is unavailable — a
map with `cpd00027` on it is worse than one saying `D-Glucose` and far better than no map.

**Member colours are built once, across every condition, and reused.** `escher_edit`'s own
documentation is explicit that a per-map default assigns palette slots from that map's own
members, so a member missing from one condition would change every other member's colour.
`render_community_map` therefore builds `escher_edit.palette.member_colors(all_member_ids,
groups=member_groups)` once from `comm.member_ids` and passes the mapping to every render.

`member_groups` accepts `{member: group}` or a callable, colours members by group with a
legend, and is how a community is coloured by phylum. `escher_edit.palette.taxon_groups`
reads groups off a taxonomy mapping or GTDB strings; this module forwards rather than
wrapping it.

`skip_amino_acids=True` forwards `escher_edit.filter_map.DEFAULT_SKIP_NAMES` to
`build_member_reactions`, reproducing the reference figure's compound selection. It is off by
default because silently hiding metabolites from a figure someone will interpret is not a
default anyone should get without asking.

**`label_compounds` controls what the STATIC figure calls each compound, and its default is
the ModelSEED id.** This is the one place where the obvious thing is wrong, so it is specified
rather than left to the builder. `escher_edit` draws node labels from `bigg_id`
(`render.py:185`), so `compound_names` alone puts names in the JSON and in the interactive
tooltips (`"D-Glucose (cpd00027)"`) while the static SVG still reads `cpd00027`.

- `label_compounds="id"` (default) — upstream behaviour, and the only setting where the
  reserved label room certainly matches the text drawn.
- `label_compounds="name"` — names on the static figure too. Because
  `MapStyle.fitted_column_dx` reserves horizontal room from the **ids** (an 8-character
  default, and a ModelSEED id is exactly 8 characters) while measured names run about 2.9×
  wider, this MUST widen the columns *before* layout using upstream's documented override:
  compute `w = style.fitted_column_dx(names.values())`, derive a new style as
  `MapStyle(input_column_dx=-w, output_column_dx=w, ...)`, lay out with that, and swap the
  `<text class="node-label label">` contents after render in a small BeautifulSoup pass — bs4
  is already an `escher_edit` dependency. Verified in the scratchpad: the override is accepted
  and widened the canvas from 2292 to 2644 on the sample.
- `label_compounds="name_id"` — `"<name> (<id>)"`, sized the same way.

The default is `"id"` rather than `"name"` because a figure whose labels overflow the room
reserved for them is worse than one whose labels are terse, and only the id case is guaranteed
safe without the widening pass. Never mutate a caller-supplied `MapStyle`; derive a new one.

Returns a `CommunityMapArtifacts` dataclass carrying `map_json`, `svg` and `html` paths (the
last two `None` when not requested) plus `n_members`, `n_compounds` and `n_blocks` — counts
a caller needs to judge whether the figure is readable, and which the acceptance criteria
assert on.

#### `render_member_map` — the per-member pathway view

Unchanged from round 0, and retained deliberately: the community map carries no intracellular
reactions at all, so it cannot answer "what is this member's metabolism doing". The
projection:

1. Take `member_id`, look up its community index `i` (1-based, from `member_ids`).
2. From the result's flux series, select reactions whose compartment suffix is `c{i}` or the
   shared `e0`, plus the member's `bio{n}` reaction.
3. Rewrite `_c{i}` → `_c0` in those reaction IDs, and map `bio{n}` → `bio1`.
4. Hand the rewritten flux dict and the *member's source model* to
   `self.escher.create_map_html2(model, map, output_path, flux=...)`.

Step 4 uses the **source single-species model**, not the community model, because the Escher
map was drawn against single-organism IDs. The community supplies the fluxes; the source
model supplies the geometry. The generated HTML carries a banner naming the member, the
community, the medium and the community growth rate, so a projection is never mistaken for a
standalone simulation.

`EX_` reactions are a deliberate edge case: they live in the shared `e0` compartment and are
**community-level**, not member-level. They are included with an explicit note in the banner,
because a member's uptake is not separable from the community's on a shared extracellular
compartment.

### Bound strings, shapes and defaults

Confront round 2 asked for twelve separate exact literals, on the ground that a test grepping
free-form text is a test that breaks on a reword. Rather than scatter them, they are bound
here, and this is the single place to change any of them.

**Exact messages.** Tests assert these; the module builds them from f-strings with exactly
this wording.

- Provenance rejection: `"resolved MSCommunity is modelseedpy.community.MSCommunity (the
  superseded copy), not the standalone mscommunity package: {resolved_module}"`
- Commit mismatch (one per package, logged at WARNING, never raised):
  `"{package} commit mismatch: pinned {pinned_sha}, found {found_sha}"`
- `load_community` with no recoverable provenance: `"Cannot reconstruct community provenance:
  no kbutil.community notes, no member_ids argument, and no member_biomass_cpds in
  model.notes."`
- `render_member_map` with no source model: `"No source model id recorded for member
  {member_id}; cannot render member projection."`
- Unknown notes schema: `"Unsupported kbutil.community schema_version: {v}"` — `load_community`
  accepts `{1}` and raises on anything else. Declaring the policy now is cheaper than
  discovering later that an old reader silently mis-parsed a new writer.
- The member-projection banner, verbatim: `"Member {member_id} projected from community
  {community_id} on medium {media_id} (community growth {growth:.4f}/hr). EX_ exchange
  reactions are community-level on the shared e0 compartment and cannot be attributed to a
  single member; EX_ fluxes are shown as community totals."`

**Upstream stdout markers**, as module-level constants so a wording change is a one-line fix
and the test that asserts them against `mscommsim.py` fails loudly:
`KINETICS_RELAXED_MARKER = "Kinetic constraints disabled"` and
`NO_GROWTH_MARKER = "doesn't grow"`.

**Exact shapes.**

- `CommunityModel.source_model_ids` is `{member_id: source_model_id}` — member first. Example:
  `{"iML1515": "iML1515", "Bth": "Bth_draft_v2"}`. Getting the direction wrong breaks
  `render_member_map`, which looks up by member.
- `CommunityFBAResult.fluxes` is a `pandas.Series` indexed by **string reaction id** with float
  values and no NaNs.
- `model.notes["member_biomass_cpds"]` is upstream's and is a **dict**, `{model_id:
  [biomass_compound, ...]}` — verified from `mscommsim.py:239`, which iterates it with
  `.items()`, and `commhelper.py`, which builds it with `setdefault(org_model.id,
  []).append(met)`. It is **not** an ordered list; confront round 2 proposed binding it as one
  and that is declined as factually wrong.
- `build_community`'s `model_id` becomes the underlying cobra model's `id` and `name` its
  `name`; both are echoed into the `kbutil.community` notes.
- `predict_abundances` records `{"regularization": bool, "determinize": bool}` in the result's
  `notes`, so a returned abundance vector says which subroutine produced it.

**Defaults and determinism.**

- `render_community_map`'s `style` defaults to `escher_edit.MapStyle()` and the **same
  instance** goes to `build_escher_map(style=)` and `render_map_svg(layout=)`.
- Multi-condition blocks are assembled in **sorted label order**, and labels must be unique
  non-empty strings. Dict insertion order would make a figure depend on how the caller happened
  to build its mapping, which is exactly the kind of irreproducibility a figure in a paper
  cannot have.
- `n_compounds` is the count of **distinct compound ids across all blocks** (the union), not a
  per-block maximum.
- `export_community_sbml` creates parent directories and overwrites by default, with
  `overwrite=True` in the signature so a caller can opt out. It delegates to
  `comm.mscomm.to_sbml`, which already does the `makedirs`. Confront round 2 proposed routing
  through a `KBModelUtils.CobraModelConverter.to_cobra` helper; **declined — no such API exists
  in this repo**, and MSCommunity already exposes the exporter.
- `gapfill_community` forwards by keyword to upstream's **real** parameter names,
  `default_gapfill_templates` and `default_gapfill_models` — not `templates` / `models`, which
  confront round 2 proposed and which would be silently swallowed by `**kwargs`-free positional
  binding. This matters more than usual here: the historical `MSGapfill` positional bug this
  PRD already documents was exactly a mis-bound gapfill argument.
- Compounds appearing **only** in the `Environment` column — no member carries a non-zero flux
  for them — are excluded from `fluxes_by_member`, from compound-name resolution, and from
  `n_compounds`.
- `_import_escher_edit` and `_import_mscommunity` both **prefer an already-importable package**
  on `sys.path` and only fall back to the declared dependency path, so a developer's editable
  install wins over the checkout.

**Two bindings confront round 2 asked for and did not get, both because it does not have
MSCommunity in its worktree and guessed:**

- It proposed reading drain bounds from `comm.mscomm.member_biomass_drains[member_id]`. **No
  such attribute exists.** The verified path is `member.biomass_drain_bounds`, a
  `(lower, upper)` tuple snapshotted per member at construction (`mscommsim.py:153`, with the
  comment saying it is kept precisely so the drain can be restored where a member is measured
  alone). Using the invented path would leave solo growth reading zero — the exact failure the
  guard exists to prevent.
- It proposed stripping `_c\d+` and `_e0` suffixes from compound ids used as fallback labels.
  **Unnecessary:** `interactions()` already strips `_e0` when it builds the DataFrame index, and
  community cross-feeding ids are therefore bare `cpd#####`. A strip pass would be dead code
  that looks load-bearing.

One further decline, on proportionality rather than fact: confront round 2 proposed normalizing
abundances through `Decimal` at 1e-12 and rounding to 8 decimal places for note stability.
Declined — abundances are caller-supplied input, float normalization is what upstream does, and
a `Decimal` path buys reproducibility in a field nothing compares exactly.

### Calling into `mscommviz` safely

The module never calls `mscommviz.interactions` (or its three siblings) directly.
A private `_unwrap(fn)` helper returns `fn.__func__` when `isinstance(fn, staticmethod)` and
`fn` otherwise, and every call into that module goes through it.
This makes the module work identically on Python 3.9 and 3.10+, and costs one line.

**Support policy, stated so tests and docs can be written against it.** The module supports
Python >= 3.9, matching `pyproject.toml`. On 3.9, every call routed through this module
works, and a *direct* call to `comm.mscomm.interactions()` through the escape hatch fails
inside upstream — that is upstream's bug and the module does not monkeypatch its way into
fixing it. The module README notes this, and any test that exercises the escape hatch
directly carries a version skip marker. Raising KBUtilLib's floor to 3.10 would make the
problem disappear, but that is a library-wide decision this PRD does not get to make.

### Capability registration

Public methods carry `@capability(domain="community", ...)` with summaries and tags,
following `ms_fba_utils.py`. Registration is inert to direct calls; it is what makes the
methods reachable from the CLI, MCP and HTTP transports without further wiring.

**Capability names are public API and are bound here**, because a later rename breaks every
CLI and HTTP caller. There are **fourteen**: `community.build_community`,
`community.load_community`, `community.save_community`, `community.export_community_sbml`,
`community.run_community_fba`, `community.predict_abundances`, `community.run_micom`,
`community.test_member_growth`, `community.gapfill_community`,
`community.cross_feeding_table`, `community.fluxes_by_member`,
`community.cross_feeding_graph`, `community.render_community_map`,
`community.render_member_map`.
These are `@capability`'s default `"<domain>.<fn.__name__>"`, so they follow from the method
names rather than being a second thing to keep in sync.

### Error types

Three exceptions, defined in the module and exported through `__all__`.
They derive from **KBUtilLib's existing hierarchy in** `kbutillib/core/errors.py`, not from
bare `Exception`, so CLI/API error mapping keeps working:

- `CommunityDependencyError(BackendUnavailableError)` — MSCommunity absent, or the wrong one
resolved. `BackendUnavailableError` already subclasses `KBUtilLibError` and is the type the
cheminformatics and thermo backends raise for exactly this condition.
- `CommunitySolverError(KBUtilLibError)` — no QP solver for MICOM; the over-specification
refusal; infeasibility where a solution was required.
- `CommunityVisualizationError(KBUtilLibError)` — a rendering dependency is missing
  (`escher_edit` itself, or its `beautifulsoup4` / `lxml` requirements), or a map was built
  with no drawable members.



## Testing Decisions

**What makes a good test here.** The module's externally-visible behavior is: does it
resolve the right dependency, does it refuse the wrong one, does it preserve provenance the
upstream package discards, does it avoid mutating state the caller did not ask to mutate,
and does the projection produce the reaction IDs an Escher map expects.
None of those require a solver, and the ones that do are the least interesting.
So the test suite is two-tier:

**Tier 1 — no MSCommunity, no solver (runs in CI).** These are the majority.

- `_import_mscommunity()` returns `None` cleanly when neither a pip install nor a
`dependencies.yaml` path is available, and `available` is then `False` with a reason
naming MSCommunity.
- The provenance gate: a fake module whose `MSCommunity.__module__` is
`modelseedpy.community.mscommunity` is **rejected**, with the reason naming the
superseded copy. This is the divergence defense and it is the single most important test
in the file.
- `_unwrap` returns the underlying function for a `staticmethod` and is identity for a
plain function.
- The member-map projection is tested as a **pure function** on a synthetic flux dict:
`{"rxn00001_c2": 5.0, "rxn00002_c1": 1.0, "EX_cpd00027_e0": -3.0, "bio3": 0.4}` with
`member_ids=["A","B"]` and `member_id="B"` yields `{"rxn00001_c0": 5.0, "EX_cpd00027_e0": -3.0, "bio1": 0.4}` and drops `rxn00002_c1`.
Extracting the rewrite into a module-level function is what makes this testable without
cobra, and is required.
- Abundance normalization: `{"a": 3, "b": 1}` becomes `{"a": 0.75, "b": 0.25}`.
- `MSCommunityUtilsImpl` constructs successfully with a missing delegate and raises a clear
`RuntimeError` on attribute access, matching `MSFBAUtilsImpl`'s contract.
- The facade property exists, is lazy, and is idempotent — extend the existing
`tests/core/test_composition_smoke.py` rather than writing a new smoke test.

**The adapter and the map assembly are the new testable surface, and both are testable
without either package installed.** `fluxes_by_member` is a pure transformation of a
DataFrame, so it is tested against a hand-written cross-feeding frame: member columns become
`{compound: flux}` dicts with signs unchanged, the `Environment` column is dropped, zero
entries are omitted, and a frame whose only non-zero column is `Environment` yields `{}`
rather than a spurious member. That last case is the one that would otherwise draw the medium
as an organism.

`render_community_map` is tested by **faking `escher_edit`** rather than by rendering: a stub
module records the calls, and the assertions are that `build_member_reactions` was called once
per condition with the right `model_id`, that `build_escher_map` received blocks in
`[(label, members), ...]` form when several results were passed and a bare member list when
one was, that `member_colors` was built **once** from `comm.member_ids` and the same mapping
was handed to every render, and that `render_map_svg` received `dashed=True` and the `html`
flag as given. The colour-stability assertion is the one that catches a real, silent defect
(see Gotchas) and cannot be caught by looking at a single figure.

**Tier 2 — MSCommunity present (marked, skipped by default).** Guarded by a
`pytest.mark.skipif` on `_import_mscommunity() is None`, following the existing
`tests/modeling/test_predictive_thermo.py` pattern for dependency-gated tests.

- Build a two-member community from `tests/conftest.py`'s `mini_model` fixture duplicated
and re-identified; assert `member_ids`, `source_model_ids`, and that compartments `c1`
and `c2` both appear.
- `predict_abundances(update=False)` leaves `comm.mscomm.abundances` and the primary
biomass stoichiometry **byte-identical**; `update=True` changes them.
This is the non-mutation guarantee and it is worth a real model to test.
- `run_community_fba` on an infeasible medium returns a result with `trustworthy=False`
rather than raising.
- `run_micom` on a GLPK-only environment raises `CommunitySolverError` naming the QP
solvers.
- **With `escher_edit` also present:** `render_community_map` on the two-member fixture writes
a `map_json` that parses as a two-element `[header, body]` list whose `reactions` hold one entry
per member, and — when `svg` / `html` are requested — produces files that exist and are
non-empty. Assert on structure and on the returned `n_members` / `n_compounds` / `n_blocks`
counts, **not on pixels**: a rendering test that asserts on SVG text breaks every time the
upstream layout is tuned, and the layout is upstream's to tune.
- Passing two results produces `n_blocks == 2` and a `text_labels` entry per condition label.

**Prior art in the codebase.** `tests/modeling/test_ms_remote_solver_utils.py` for
dependency-gated modeling tests, `tests/core/test_composition_smoke.py` for facade
properties, and `tests/conftest.py`'s `mini_model` fixture for a solver-free cobra model.

**Regression framing.** Every criterion is phrased as "the tests this task adds pass, and
no test that passed on the base commit fails" — KBUtilLib carries pre-existing failures, so
a green-suite criterion is unmeetable.

## Open Questions and Judgement Calls

Every item below is **already decided and reflected in the PRD body and taskplan**.
The PRD is buildable as it stands.
Changing a decision means editing its `DECIDED:` line — and then the propagation named in
`If you disagree` has to follow.
Entries are ordered by blast radius descending.

### Q1. Does `predict_abundances` mutate the community by default? — DECIDED: No. Non-mutating by default; `update=True` is opt-in.

**Blast radius:** IRREVERSIBLE
**Why:** This is the module's central semantic promise and it is baked into every caller's
mental model from the first use. MSCommunity's `set_abundance` rewrites the primary biomass
reaction's stoichiometry in place, so a mutating default makes every multi-step analysis
order-dependent, and the dependence is invisible — the second simulation just returns
different numbers. Reversing this later silently changes the results of every notebook
written against the old behavior, with no error to warn anyone.
**If you disagree:** `predict_abundances` in `ms_community_utils.py` drops its save/restore
of `comm.mscomm.abundances` and the primary biomass stoichiometry; user story 11 is wrong
and must be rewritten; the Tier-2 non-mutation test is inverted; and the `CommunityModel`
dataclass's `abundances` field stops being meaningful as a record of what was built.
**Confidence:** high — MSCommunity's own `PATCH 3` comments and the `update_abundances`
flag show upstream treats the mutation as a special case too.

### Q2. Do the wrappers return MSCommunity objects or KBUtilLib-owned result types? — DECIDED: A `CommunityModel` handle plus dataclass results, with `comm.mscomm` exposed as an escape hatch.

**Blast radius:** MEDIUM
**Why:** Returning raw MSCommunity objects would make the module a pass-through with no
value beyond import resolution, and would lose the provenance `build_from_species_models`
destroys — after the merge, member models have been copied and renamed and there is no way
to recover which input became which member. Wrapping *completely* would be worse: it would
mean re-exporting a 881-line surface and going stale every time upstream moves. The handle
carries only what upstream discards; everything else is reached through `.mscomm`.
**If you disagree:** the four dataclasses (`CommunityModel`, `CommunityFBAResult`,
`GapfillResult`, and the exceptions) collapse; all 25 user stories that name a result field
need rewriting; `render_member_map` loses `member_ids` and cannot compute the compartment
index, so the projection would need a different source of member ordering.
**Confidence:** medium, and the external scan raised rather than settled it: the scan
independently recommends "return typed results with provenance … avoid raw DataFrames only",
which is this call, but it argues from other tools' experience rather than from anything
about MSCommunity, so it corroborates the shape without testing it here.

### Q3. Which MSCommunity do we wrap? — DECIDED: The standalone `mscommunity` package, with a hard provenance gate rejecting `modelseedpy.community`.

**Blast radius:** MEDIUM
**Why:** The standalone repo is the maintained line (tip 2026-08-27; adds `micom`,
regularization, the determinizing QP split, and batched LP). The copy inside ModelSEEDpy
lacks all of that. Because both export the name `MSCommunity` and share most method names,
picking the wrong one produces *plausible wrong numbers* rather than an error — which is why
the check is a hard gate rather than a warning.
**If you disagree:** the whole Implementation Decisions "Resolving MSCommunity" subsection
changes; `run_micom` and the regularization/determinize parameters have no upstream to call
and must be dropped; `commhelper.build_from_species_models` becomes
`MSCommunity.build_from_species_models` (a method, not a function); and the Tier-1
provenance test is inverted.
**Confidence:** high on which is current; medium on whether ModelSEEDpy will eventually
re-absorb it, which would make the gate wrong rather than merely unnecessary.

### Q4. How is a community model displayed on an Escher map? — DECIDED: Generate a community exchange map with `escher_edit`, and keep the per-member pathway projection as a second, complementary view.
**Blast radius:** MEDIUM
**Why:** Round 0 decided per-member projection only, on the stated premise that no
community-wide map existed to build from. **That premise was wrong** — it was true of
MSCommunity, which was the only place checked, and false of the ModelSEED ecosystem:
`ModelSEED/editEscher` generates exactly such a map from per-member exchange fluxes. Round 0
flagged this as its one flag-not-question ("no artifact states what a modeler wants to SEE");
Chris answered it by naming the repository, and the answer was the whole community at once.

The two views are kept because they answer different questions and neither substitutes for
the other. The community map is an **exchange** map: it has one net organism reaction per
member and carries no intracellular reactions at all, so it cannot show what a member's
metabolism is doing. The per-member projection is a **pathway** view on an ordinary
single-organism map, and cannot show the community. Dropping either would leave a real
question unanswerable.

**One sub-decision rides inside this entry rather than taking its own number**, because it is
the same question — how the community is displayed — and renumbering the section a third time
risks the cross-references more than the separation is worth: **compound labels on the static
figure default to ModelSEED ids, not names** (`label_compounds="id"`). `escher_edit` draws
node labels from `bigg_id`, and the layout reserves horizontal room sized from the ids, so
names-on-the-static-figure requires a column-widening pass and is therefore opt-in. Names are
always present in the map JSON and in the interactive tooltips regardless. If you would rather
the static figure read `D-Glucose` by default, that is a one-word change to the default and the
widening pass runs on every map.

Ordering matters and is part of the decision: `render_community_map` is the primary view —
the one a notebook reaches for first and the one that goes in a paper — and
`render_member_map` is the follow-up once a member looks interesting.
**If you disagree** and want only the community map: `render_member_map`, the
`project_member_fluxes` pure function and its Tier-1 test go, `kbu.escher` stops being a
constructor dependency of this module, and acceptance criteria 23-25 are removed — that is
the cheaper half to cut, because the projection is the part nobody has yet looked at.
If you want only the projection: `escher_edit` leaves `dependencies.yaml`, `render_community_map`
and `fluxes_by_member` go with it, taskplan task `t3` shrinks to its round-0 form, and the
PRD returns to having no answer for "show me the community".
**Confidence:** high on the community map, which is now a concrete artifact with a documented
API and a verified input match rather than a design guess. Medium on retaining the
projection — it is cheap and complementary, but it remains the piece nobody has seen output
from, and if it turns out to duplicate what the community map already tells people it is the
first thing to cut.

### Q5. Do we close the member biomass drains? — DECIDED: Yes, exactly when the caller supplied abundances; `close_member_drains = comm.abundances_were_supplied`.
**Blast radius:** MEDIUM
**Why:** Upstream added `close_member_drains` in `57e1504` (2026-09-23) and states the rule
plainly: set it True "whenever the declared abundance vector is meant to BIND -- any run
where you read member growth rates or a kinetic coefficient off the solution."
That is precisely the case this module creates whenever a caller passes `abundances`, which
Q1 defines as an input that binds. With the drain open a member can synthesise biomass and
immediately discard it, so its biomass flux exceeds `abundance * bio1` and the CommKinetics
right-hand side inflates with it — each drained unit buys `kinetic_coef - 1` units of flux
budget. Upstream measured 83% of one member's biomass leaving through the drain on a
two-member denitrifying SynCom, and closing the drains moved the lowest feasible kinetic
coefficient from 1285 to 1481.
Tying it to `abundances_were_supplied` rather than exposing a third state keeps the rule
derivable from something the caller already said, instead of asking them to understand a
flux-budget argument in order to pick a boolean.
**If you disagree:** `build_community` stops forwarding the flag and upstream's `False`
default applies; every member growth rate and every kinetic coefficient this module reports
changes; the `test_member_growth` drain-reopening guard below becomes dead code; and the
acceptance criteria covering both are removed. Note the reversal is **silent** — numbers
move, nothing errors.
**Confidence:** high on the rule, because upstream states the condition and this design
satisfies it exactly. Medium on the *default* for a community built without abundances,
where `False` is chosen only because there is no declared vector to bind and upstream's own
default agrees.

### Q6. Where do MSCommunity and editEscher get declared as dependencies? — DECIDED: `dependencies.yaml` for both, not `pyproject.toml`.

**Blast radius:** LOW
**Why:** Neither has a PyPI release — MSCommunity is `setup.py` at 0.0.1, `escher_edit` is
hatchling at 0.1.0 — so a `pyproject.toml` entry would have to be a git URL, which breaks
`pip install KBUtilLib` for anyone without git credentials and pins a moving target.
`escher_edit` is the easier of the two: it is a `src/`-layout package whose only required
dependencies, `beautifulsoup4` and `lxml`, ARE on PyPI, so those two go in a normal
`pyproject.toml` extra (`community`) while the package itself resolves through
`dependencies.yaml`. Its `shapely` (labels) and `pandas` (mapping) extras are not needed by
anything this module calls and are left out. `dependencies.yaml` is the mechanism KBUtilLib
already uses for exactly this class of dependency — `modelseedpy`, `ModelSEEDDatabase`,
`cobrakbase`, `cb_annotation_ontology_api` — and `get_dependency_path` already returns
`None` rather than raising when the checkout is absent.
The scan's one objection to a bare VCS dependency is reproducibility: it recommends pinning
a **tested commit SHA rather than a branch**. `dependencies.yaml` as used today records only
`path` and `git`, so the entry adds a third key, `commit: 2dcff16`, and
`_import_mscommunity()` logs a warning (never raises) when the resolved checkout's HEAD does
not match it. Pinning without enforcing keeps a developer working on a newer MSCommunity from
being blocked, while still making the drift visible.
**If you disagree:** `pyproject.toml` gains git-URL entries; `_import_mscommunity()` and
`_import_escher_edit()` lose their `get_dependency_path` fallback branches and their Tier-1
tests; and CI must install from git to exercise Tier 2.
**Confidence:** high — this follows an established repo pattern with a worked example in
`domains/thermo/thermo_predictors/base.py`. The `commit` key is new to `dependencies.yaml`
and no other entry carries one, so it must be additive: `DependencyManager._load_config`
ignores unknown keys, which was checked, but nothing else in the repo reads it yet.

### Q7. Do we keep MSCommunity's graphviz cross-feeding renderer? — DECIDED: No. `escher_edit` replaces it; `render_cross_feeding` is removed from the design.
**Blast radius:** LOW
**Why:** This entry previously asked what should happen when graphviz was missing. With
`escher_edit` in the design that is the wrong question, because the graphviz renderer no
longer earns its place: it draws *the same relationships* — who feeds whom, through which
metabolites — in a worse layout, and it is the heaviest dependency in the PRD. It needs the
`graphviz` Python package, the `dot` system binary, **and** a ModelSEED biochemistry checkout
(`visual_interactions` asserts on `msdb or msdb_path`). `escher_edit` needs `beautifulsoup4`
and `lxml`, no binary and no network, and produces a member-box figure plus an interactive
page. Keeping both would mean maintaining two renderers of one question and specifying a
fallback between them.
**CONFIRMED BY CHRIS, 2026-09-24:** "Yes, drop the graphviz stuff entirely in favor of the
escher viz." This was raised to him as a removal he had not asked for, with the specific risk
named — continuity with existing MSCommunity-drawn figures — and he decided against keeping it.
The entry is settled, not merely proposed.
The data is unaffected: `cross_feeding_table` and `cross_feeding_graph` still return everything
the graphviz figure was drawn from, and `comm.mscomm.interactions(visualize=True,
msdb_path=...)` remains available through the escape hatch.
**If you disagree:** `render_cross_feeding` comes back with its two-branch dependency probe
(`graphviz` package vs `dot` binary) and its 40-node warning; `CommunityVisualizationError`
regains its graphviz meaning alongside the `escher_edit` one; user stories 17 and 18 return;
and the acceptance criteria covering both renderers are restored. One line in `taskplan.json`
task `t3` and roughly thirty lines of module code.
**Confidence:** high, on both halves. `escher_edit` is the better figure and the lighter
dependency, and the removal is wanted — the one thing this entry could not know was a fact
about Chris's own work, and he has now supplied it.

### Q8. Which module does the community code live in? — DECIDED: `domains/modeling/ms_community_utils.py`, not a new `domains/community/`.

**Blast radius:** LOW
**Why:** It is modeling work, it inherits `KBModelUtils`, and it sits beside
`ms_fba_utils.py` and `ms_reconstruction_utils.py` which it most resembles. A new domain
would mean a new `__init__.py`, a new README, and a new entry in every domain-walking
mechanism, to hold one module.
**If you disagree:** a `domains/community/` package is created with `__init__.py` and
`README.md`; the facade import path changes; and `domains/modeling/README.md` does not get
its new row.
**Confidence:** high — a second community module would justify revisiting this, and there is
exactly one.

### Q9. Do we wrap `MSKineticsFBA` (dynamic/kinetic community FBA over time)? — DECIDED: No, not in v1.

**Blast radius:** LOW
**Why:** It is a different analysis — time-series concentration simulation with its own
matplotlib plotting, its own kinetics data format, and its own failure modes — and the ask
names creation, running and display of *community models*, not dynamic simulation. Adding it
would roughly double the surface for an orthogonal capability.
**If you disagree:** a fifth group of methods is added
(`run_kinetic_fba`, `plot_concentrations`), `MSKineticsFBA` joins the import gate, the
kinetics-data file format needs its own decisions, and the taskplan gains a phase.
**Confidence:** high — deferring is reversible; the API it would need is independent of
everything decided here.

### Q10. Do we expose the batched-LP backends (jax / cupy / pdlp)? — DECIDED: Not in v1. No method takes a `backend` argument; `comm.mscomm.extract_problem()` / `solve_batch()` stay reachable through the escape hatch.

**Blast radius:** LOW
**Why:** This entry originally said "passthrough only, `backend=` is forwarded", and the
confront round found the hole: **no method in the design takes a** `backend` **argument**, so
there was nothing to forward it through and the entry described a parameter that did not
exist. The honest version is that v1 exposes no batched-LP surface at all. That costs
nothing real — `comm.mscomm.solve_batch(instances, backend="jax")` works today through the
escape hatch, which is exactly what the escape hatch is for — and it avoids specifying a
batch API (what is an `LPInstance`? who builds the media sweep?) that nobody has asked for.
Adding jax or cupy to KBUtilLib's dependency surface for a GPU path primary-laptop cannot
exercise remains not worth it either way.
**If you disagree:** a `run_community_fba_batch(comm, medias, backend="cpu")` method is
added along with a `CommunityBatchResult` type, the `LPInstance` construction has to be
specified, `pyproject.toml` gains a `gpu` extra, and the performance claim needs an actual
benchmark, which this design has not run.
**Confidence:** high on the narrowed decision — "we do not expose it" needs no measurement.
The earlier passthrough claim was medium-confidence and wrong in a way the confront caught.

### Q11. What is the default `kinetic_coeff`? — DECIDED: 750, upstream's default, surfaced as an explicit parameter and recorded on `CommunityModel`.

**Blast radius:** LOW
**Why:** Changing upstream's default would make KBUtilLib results differ from MSCommunity
results for no stated reason. But it must be *visible*, because it has a real failure mode:
MSCommunity's own `PATCH 3` exists because at `kinCoef=750` the kinetic constraint makes
growth infeasible on low-yield carbon sources like acetate, and the package silently drops
the constraint and re-solves. Recording it on the handle means a surprising result can be
traced to it.
**If you disagree:** the default changes in `build_community`; every Tier-2 growth assertion
shifts; and the Gotchas entry on silent kinetic-constraint removal needs rewriting.
**Confidence:** high on matching upstream; low on 750 being a *good* value, which is a
science question this design does not answer.

### Q12. What I could not decide and did not guess

- **Round 0's flag-not-question is ANSWERED and is recorded here so the answer is not lost.**
  It read: no artifact I can read says what a modeler wants to SEE when they ask to display a
  community model, and I assumed one member at a time. Chris answered on 2026-09-24 by naming
  `ModelSEED/editEscher`, and the answer was the whole community at once. The assumption was
  wrong, Q4 changed, and the visualization half was rewritten. Worth keeping visible: the
  premise that made it wrong was searching only MSCommunity and concluding about the
  ecosystem.
- **Whether `render_member_map` still earns its place.** Now that the community map exists,
  the per-member projection is retained on the argument that exchange-level and pathway-level
  are different questions. That argument is mine, not anyone's stated need, and nobody has
  looked at a projected member view yet. If it turns out to tell people nothing the community
  map does not, it is the first thing to cut.
- **What the figures actually look like.** No map has been generated. `escher_edit` claims it
  reproduces its reference figure's 115 reactions with identical ids, metabolite sets and
  coefficients on a generated layout, and the input-shape match to MSCommunity was verified by
  reading both sides — but I ran nothing. Whether a KBase community's compound set produces a
  legible figure at its own scale is unmeasured.
- **Whether `escher_edit` has been used on a ModelSEED-id community.** Its reference data is an
  ASV/metagenomic study keyed by its own compound ids with a names CSV beside it. Nothing says
  it has been run against `cpd#####` ids with names from the ModelSEED biochemistry database.
  The adapter makes that a naming question rather than a structural one, but it is untested.
- **Whether `modelseedpy.community` will be retired.** If ModelSEEDpy drops its copy, Q3's
  provenance gate becomes dead code; if ModelSEEDpy re-absorbs the standalone package, the gate
  becomes actively wrong. No artifact states either intention and the two repos' histories do
  not reference each other.
- **Whether the external scan's stripped API names hid a contradiction.** Every inline code
  span in `research/external-scan.md` is empty, so wherever it named a class or method the name
  is gone. Its arguments are followable and were folded; its citations of specific APIs could
  not be checked, and I did not re-run it.
- **Whether `load_community` can recover `member_ids` in general.** The design now persists its
  own `kbutil.community` notes key, which removes the dependence on upstream's
  `member_biomass_cpds` — but whether either survives a round trip through the KBase workspace
  serializer is unverified, because I did not run one. The design raises rather than guessing
  when all three routes are absent, which makes the failure visible instead of wrong.


## Gotchas and Unintuitive Consequences

`build_from_species_models` **renames your models' metabolites and reactions.** It copies
the inputs, but the copies get compartment suffixes rewritten (`c` → `c{i}`), biomass
reactions renumbered (`bio2`, `bio3`, …), and non-ModelSEED IDs mangled by
`correct_nonMSID`. A member model *inside* a community is not the model you passed in, and
its reaction IDs will not match anything you recorded beforehand. This is why
`CommunityModel` captures `source_model_ids` at build time.

**Abundance is stoichiometry, not a constraint.** `set_abundance` writes the abundances into
the coefficients of the primary biomass reaction. Two consequences a reader would not
predict from the Solution section: changing abundances changes the *model*, so an SBML
export carries the abundances invisibly; and two communities built from the same members
with different abundances are different models, not the same model under different
constraints.

**One shared extracellular compartment means no spatial structure.** Every member exchanges
through `e0`. A metabolite secreted by member 1 is immediately available to member 3 at no
cost and with no gradient. Cross-feeding predictions are therefore upper bounds on what
physical proximity would permit, and the cross-feeding graph shows *possible* exchange, not
demonstrated exchange.

**Kinetic constraints can be silently removed mid-simulation.** `predict_abundances` detects
a zero-growth community, removes the `_commKin` constraints inside a context manager,
re-solves, and if that grows, **proceeds with the constraint-free result** — printing a
message to stdout, not raising and not recording it on the solution. A caller comparing two
media would otherwise get one answer with kinetics and one without and have no field to
distinguish them. This is why `CommunityFBAResult` carries `kinetics_relaxed` and `notes`
and why the wrapper captures upstream's stdout to populate them — the signal exists only as
a printed line, so anything that does not read stdout cannot see it at all. The capture is
marker-based, so **an upstream wording change silently stops setting the flag**; the markers
are asserted in a test against the literal strings in `mscommsim.py` so that drift fails
loudly rather than quietly.

**Closing the biomass drains silently zeroes solo growth.** This is the sharpest
consequence of Q5 and it follows from nothing in the Solution section. `close_member_drains`
makes the declared abundances bind, which is what we want — and it simultaneously breaks
every measurement that switches the other members off, because `bio1` consumes every
member's biomass compound and the drain was the only other outlet. Upstream compensated in
`_solo_max_batch` and not in `test_individual_species`. A caller reaching past this module
to `comm.mscomm.test_individual_species(interacting=False)` on an abundance-bound community
gets a table of zeros that looks like a scientific result.
The related asymmetry is worth holding too: `predict_abundances` and `regularization` go
through `_solo_max_batch`, so they are safe; the non-batched solo paths are not.

**A sub-optimal LP does not raise.** `_set_solution` logs an error and sets
`suboptimal_solution = True`, then returns the solution anyway. `trustworthy` exists
entirely because of this, and any code path that bypasses `CommunityFBAResult` loses the
signal.

`micom()` **swaps your solver and swaps it back — but not your objective order.** It
restores the caller's objective *before* restoring the solver, deliberately, because a
quadratic objective cannot be cloned into a non-QP interface. If an exception escapes
between those two steps the model is left on the QP backend. Rare, but the model object is
shared state.

**Nothing in this module calls MSCommunity's own rendering path, and that is deliberate.**
`visual_interactions` asserts on `msdb or msdb_path` and calls `msdb.compounds.get_by_id` for
every cross-fed metabolite, so a missing biochemistry checkout would fail *after* the
simulation had been paid for and *after* the table was already in hand.
`cross_feeding_table` passes `visualize=False`; the figures come from `escher_edit` instead
(Q7). The biochemistry database is still used — for compound display names — but only where
its absence degrades a label rather than losing a figure.

**A re-save from Escher's own editor silently destroys the member boxes.** The builder records
the fitted box geometry on each member reaction as `reaction["member_box"]`, which is not part
of Escher's JSON schema. Escher ignores the extra key on load and **drops it when the map is
saved back out of its editor**. A round-tripped map still renders, but every member box is
re-sized to its label on the point where its two marker segments meet — so a figure
regenerated after an editing session does not match the one before it, with nothing indicating
why. Escher's schema has no per-segment style and no box node at all, which is why boxes,
member colours and cross-feeding dashes live on the rendered SVG rather than in the map.

**The map JSON alone is not the figure.** Following from the above: `map_json` is portable and
loadable in Escher and carries none of the member identity. Anyone handed only the JSON sees
an unstyled exchange network. The SVG and the HTML are the deliverables; the JSON is their
input.

**Past eight members the colour palette stops being categorical.** `escher_edit`'s first eight
slots are a validated categorical palette; beyond that each further member takes the in-band
colour furthest from those already used, and the package warns that "the labels have to carry
identity". A twelve-member community therefore produces a figure where colour is suggestive
rather than definitive. Grouping by taxon is the real answer at that size, because it
collapses forty members onto a handful of slots — which is why `member_groups` is forwarded
rather than hidden.

**A member missing from one condition changes every other member's colour, unless the mapping
is built once.** `escher_edit` assigns palette slots from the members present in the map it is
drawing, so two per-condition maps drawn independently disagree about which member is blue.
`render_community_map` builds the mapping once from `comm.member_ids` and reuses it — invisible
when it works, and a subtly wrong figure series when it is forgotten.

**Passing `compound_names` does not put names on the static figure.** `escher_edit` draws
metabolite node labels from `bigg_id` (`render.py:185`); `compound_names` sets the JSON's
`name` field, which drives the interactive tooltips and Escher's own viewer. So the same map
reads `cpd00027` as a static SVG and `D-Glucose (cpd00027)` on hover in the HTML. Verified by
generating one: its ten text elements were seven ids and three member names.

**And the reserved label room is sized from the ids, so swapping labels naively overflows.**
`MapStyle.fitted_column_dx` computes its column offsets from `_label_width` over the compound
**ids**, defaulting to eight characters — which is exactly a ModelSEED id. Measured on a
three-compound sample the widest name was **2.88×** the widest id (270px against 94px). A
post-render text swap with no widening pass therefore pushes labels into the column nodes.
This is why `label_compounds` defaults to `"id"` and why `"name"` recomputes the column
offsets through upstream's documented override before laying out.

**`build_member_reactions` returns members in ALPHABETICAL order, not the order you passed
them.** It sorts by member name. The figure's own member column is then ordered by
connectivity, not alphabetically either, so neither matches community index order. This is
harmless for colours — the palette mapping is keyed by name — but a reader expecting the
member column to follow the abundance vector or the community index will misread it.

**The cross-feeding DataFrame has no compound names in it.** `interactions()` assembles a
`"Metabolites/Donor"` display-name column and then drops it before returning, so anything
drawing from `cross_feeding_df` gets bare `cpd#####` ids. A reader would reasonably assume the
names travel with the table. They do not, which is why names are resolved separately through
the biochemistry sibling.

**MSCommunity prints. A lot.** It uses `icecream` with `includeContext=True` configured at
import, plus bare `print()` calls in `build_from_species_models`, `set_abundance`,
`add_commkinetics` and `predict_abundances`. Importing MSCommunity reconfigures `icecream`
*globally* for the whole process, which affects any other library in the notebook that uses
it. Wrapping does not suppress this, and this PRD does not try to — but a notebook user will
see output they did not ask for.

**The Python 3.9 hazard is upstream's, and it reaches us through MSCommunity's own code.**
`mscommsim.py` imports `mscommviz.interactions` and calls it, so on Python 3.9
`MSCommunity.interactions()` fails inside the package, not just at our call site. `_unwrap`
protects the calls we make; it cannot protect upstream's. On 3.9, `comm.mscomm.interactions()`
is broken and `kbu.community.cross_feeding_table()` works.

`EX_` **reactions are community-level and cannot be attributed to a member.** In the
projection, a member's apparent uptake is really the community's. Two members consuming the
same substrate are indistinguishable in the exchange fluxes. The banner says so; the numbers
cannot.

**The first call to the import gate can print warnings about OTHER dependencies.**
`get_dependency_path` goes through a module-level `DependencyManager` singleton constructed
with `auto_init=True`, and `initialize_dependencies()` prints
`Warning: Dependency <name> not found at <path>` — with `print`, not `logging` — for *every*
entry in `dependencies.yaml` that is missing, not only the one being asked about.
`SharedEnvUtils` avoids this by constructing its own manager with `auto_init=False`, so
today nothing triggers it during normal facade construction; adding `mscommunity` to
`dependencies.yaml` does not by itself change that, but the first
`kbu.community.<anything>` call on a machine without the sibling checkouts will emit a small
burst of warnings about `modelseedpy`, `cobrakbase` and `ModelSEEDDatabase` as well.
Verified by reading `core/dependency_manager.py:111-141` and by constructing the facade
locally, which printed only the existing optional-import summary line.

**A `render_member_map` projection is not a simulation of that member.** It is the member's
slice of a community solution, which includes fluxes that only balance because *another*
member is consuming or producing something. Running the source model alone on the same
medium will generally give a different answer, and that difference is the interesting
result, not an error.

## Sources Consulted

**MSCommunity** (github.com/ModelSEED/MSCommunity, tip `2dcff16`, 2026-09-23; re-read in
full after the five commits that landed that morning, and diffed against `b5f37c4b`):

- The five commits `b5f37c4b..2dcff16` and their messages: `57e1504` (member biomass drain
  detection, the `member_kinetic_reactions` membership rule, and the new
  `close_member_drains`), `b14f50f` (build-time solver scaling via `build_solver` /
  `final_solver`, float abundances, and the `MSGapfill` solver positional), plus the three
  merge commits. `tests/test_f_biomass_drains.py` read as the upstream contract for the
  drain behavior.
- `mscommunity/commkineticpkg.py` — `member_kinetic_reactions`, which now defines member
  kinetic membership once for both `add_commkinetics` and `CommKineticPkg.build_constraint`;
  the two previously disagreed on the community biomass reaction.
- `mscommunity/mscommsim.py` — `MSCommunity.__init__` (compartment/biomass discovery,
`CommKineticPkg` construction), `set_abundance` (in-place biomass rewrite), `run_fba`,
`_set_solution` (sub-optimality logging), `_comm_growth`, `predict_abundances`
(PATCH 3 kinetic-constraint removal), `micom` (QP backend swap), `gapfill`,
`test_individual_species`, `extract_problem`/`solve_batch`, `_QP_CAPABLE`.
- `mscommunity/commhelper.py` — `build_from_species_models` lines 44–140: the compartment
index assignment (`index = 0 if compartment == "e" else model_index`, `model_index`
starting at 1), biomass renumbering, and `correct_nonMSID`.
- `mscommunity/mscommviz.py` — module-level `@staticmethod` at lines 37, 84, 99, 252;
`interactions` return shape `(cross_feeding_df, exMets_df)`; `visual_interactions` msdb
requirement; `abundance_variability_analysis`; `run_fba`'s `minMemGrwoth` history.
- `mscommunity/__init__.py`, `setup.py` — export surface; no PyPI release; dependency list.
- `mscommunity/mskineticsfba.py` — read to scope Q9; not wrapped.
- `mscommunity/batched_lp.py`, `mscommunity/backends/` — read for the `backend=` passthrough
in Q10; not otherwise used.

**ModelSEEDpy** (`~/Dropbox/Projects/ModelSEEDpy`):

- `modelseedpy/community/mscommunity.py` — the diverged 718-line copy; `MSCommunity` with
`build_from_species_models` as a method, `compute_interactions`, `run`, `steady_com`.
- `modelseedpy/community/__init__.py` — confirms `MSCommunity` is exported by `import *`,
which is what makes the name collision live.

**KBUtilLib** (`~/Dropbox/Projects/KBUtilLib`, branch `wip` at `76d0f65`):

- `src/kbutillib/toolkit.py` — the facade: `__init__` backing fields, the lazy-property
pattern, sibling-injection (`MSFBAUtilsImpl(self.env, self.model)`,
`EscherUtilsImpl(self.env, self.model, self.biochem)`).
- `src/kbutillib/__init__.py` — `_import_error` / `_flush_import_errors` optional-import
pattern for legacy class re-exports.
- `src/kbutillib/domains/modeling/ms_fba_utils.py` — the exemplar: module docstring pinning
upstream APIs, `@capability` usage, `MSFBAUtils(KBModelUtils)` plus `MSFBAUtilsImpl` with
`available` / `unavailable_reason` / `__dir__` / `__getattr__`.
- `src/kbutillib/domains/modeling/kb_model_utils.py` — `get_model`, `get_media`,
`save_model`, `_check_and_convert_model`, `_parse_id`.
- `src/kbutillib/domains/notebook/escher_utils.py` — `create_map_html2`,
`_translate_model_with_flux`, `_get_short_reaction_name`, `list_available_maps`;
establishes that maps are keyed on reaction ID with `_c0`/`_e0` compartments.
- `src/kbutillib/core/dependency_manager.py` — `get_dependency_path`, `get_data_path`,
`initialize_dependencies`.
- `src/kbutillib/core/capability.py` — the `@capability` decorator contract (inert to calls;
availability degrades gracefully).
- `src/kbutillib/compartments.py` — `normalize_compartment`; read and found **insufficient**
for community compartments: its table stops at `c0`/`e0`/`p0`/`m0` and has no notion of an
arbitrary index, which is why the projection carries its own rewrite.
- `dependencies.yaml`, `pyproject.toml` — the two dependency surfaces and which is which.
- `tests/conftest.py` (`mini_model`, `kbutillib_app` fixtures), `tests/modeling/`,
`tests/core/test_composition_smoke.py` — test conventions.
- `src/kbutillib/domains/modeling/README.md`, `src/kbutillib/domains/notebook/README.md` —
the module-table convention the new module must join.
- `src/kbutillib/domains/thermo/thermo_predictors/base.py` — `dependency_repo_path`, the
worked example of best-effort `get_dependency_path` resolution.

**editEscher / `escher_edit`** (github.com/ModelSEED/editEscher, tip `6474698`,
2026-09-23 09:20; sole branch `main`):

- `src/escher_edit/__init__.py` — the declared public surface, read as the contract.
- `src/escher_edit/build_map.py` — `build_member_reactions` (the `{member: {compound: flux}}`
  input shape and its "negative = consumed, positive = excreted" convention, the `model_id`
  suffix, `min_abs_flux` / `skip_names` / `skip_ids` filtering, the returned
  `{"name","bigg_id","fluxes"}` records); `build_escher_map` (blocks as
  `[(label, members), ...]` or a bare member list, per-block compound collapsing, the
  `[header, body]` return); `MapStyle` (the input/output column and two-lane geometry,
  connectivity ordering, aspect capping, label and radius sizing);
  `classify_compounds`, `cross_feeding_segments`, `member_box_rects`;
  `parse_interaction_matrix` and `build_map_from_interactions` — read and found study-specific
  (a wide `ASVMetaboliteInteractions.csv` with `<diet>-ABX_<day>` ids), hence the decision to
  build on the lower-level pair instead.
- `src/escher_edit/render.py` — `render_map_svg` (the one call that produces the finished SVG
  and, with `html=True`, the interactive page) and `write_escher_svg`.
- `src/escher_edit/interactive.py` — `write_interactive_html`; what the page does (hover
  highlighting, tooltips, zoom/pan, pinned links) and that it fetches no scripts or fonts.
- `src/escher_edit/palette.py` — `member_colors`, `group_palette`, `taxon_groups`, `UNGROUPED`;
  the eight validated categorical slots and the documented degradation past them, and the
  instruction to build one mapping for a whole series.
- `src/escher_edit/svg_editor.py` — `EscherSVG_processing`, `EscherStyle`, `draw_member_boxes`,
  `color_member_edges`, `dash_segments`, `draw_member_legend`.
- `pyproject.toml` — hatchling, 0.1.0, no PyPI release, `src/` layout (hence the `sys.path`
  detail in Q6); required `beautifulsoup4` + `lxml`; `shapely` / `pandas` extras not needed
  here.
- `EXTRACTION_NOTES.md` — "Map generation" and "What only the SVG can carry" read in full;
  the source of the `member_box` round-trip hazard, the SVG-only styling constraint, and the
  reproduction claim against the reference figure.

**External scan** (`research/external-scan.md`, Maestro `task-c1a370b4`, h100/codex,
verified from the task branch):

- Section 1 — API shape of MICOM, SteadyCom/SteadyComPy, SMETANA, COMETS, cobrapy community
tooling, PyCoMo, MMinte, BacArena, gapseq.
- Section 2 — the abundance/growth-rate semantics trap and the infeasibility failure mode
that motivated the `run_community_fba` refusal.
- Section 3 — visualization libraries and scaling limits (~30–50 nodes; multi-member Escher
overlays confusing beyond two or three members).
- Section 5 — GitHub-only dependency practice; the commit-SHA pinning recommendation.
- Eighteen cited URLs. Read and found **degraded**: all inline code spans are empty, so no
API name in it is quotable.

**Other:**

- `KB-ModelSEEDCommunity/lib/ModelSEEDCommunity/ModelSEEDCommunityImpl.py` and
`mscommunitymodule.py` — existing KBase SDK app; read and found not reusable (app-shaped,
pinned to the superseded ModelSEEDpy copy).
- Local verification of `staticmethod` call semantics under Python 3.11.14.
- **Negative result, recorded because an absence Chris can see is one he can correct:**
  `grep -ri escher` over `ModelSEED/MSCommunity` returns nothing on `main` (`2dcff16`),
  nothing on `gpu` (`bb26f99`, 2026-06-11, eleven commits behind and carrying no commits of
  its own), and nothing on `freiburgermsu/MSCommunity`, whose only branches are `main`
  (`bdc5316`, already merged) and `fix/member-biomass-drains` (`57e1504`, already merged).
  `mscommviz.py` is byte-identical across `b5f37c4b..2dcff16`.
- `agent-io/prds/kbu-prott5-and-horizyn-client-v1/` — read for current PRD bundle shape in
this repo.



## Out of Scope

- **Dynamic / kinetic community FBA** (`MSKineticsFBA`) — deferred per Q9.
- **Drawing Escher map LAYOUTS by hand, or writing a map builder of our own.** Generating the
community exchange map is now IN scope (Q4) and is done by calling `escher_edit`; what stays
out is reimplementing any part of it. `escher_edit`'s layout engine — connectivity ordering,
arc geometry, aspect fitting, the SVG post-processing — is ~5,000 lines and is not ours to
duplicate or fork. If it needs to change, it changes there.
- **Editing existing hand-drawn Escher maps.** `escher_edit`'s other modules
(`filter_map`, `clean_json`, `layout`, `reverse_reactions`, `svg_editor` used directly) exist
for that and are not wrapped here; this PRD uses only the build + render path.
- **Community model reconstruction from metagenomes.** Members come in as models; producing
them is `kbu.recon`'s job.
- **A KBase SDK app or narrative report.** This is a library module.
`KB-ModelSEEDCommunity` already occupies the app slot and is not touched.
- **Fixing MSCommunity upstream.** The `@staticmethod` bug, the stdout printing, the silent
kinetic-constraint removal and the unguarded `test_individual_species` under closed drains
are all upstream defects. This PRD works around the first, second and fourth, and surfaces
the third into `CommunityFBAResult`; it does not patch the dependency. Where upstream has
since fixed something itself — as with the biomass drains — the pin is raised rather than
the workaround kept.
- **CommPhitting, MSSteadyCom, MSCompatibility, commscores** — the other ModelSEEDpy
community modules. Not in the standalone package and not part of the ask.
- **Adding jax / cupy / ortools to KBUtilLib's dependencies**, and the batched-LP surface
itself — per Q10, v1 exposes no batch API at all; `comm.mscomm.solve_batch` stays reachable
through the escape hatch.
- **Benchmarking.** No performance criterion appears in the taskplan because none was
measured.



## Further Notes

**On deep modules.** The interface is eleven public methods over roughly 1,300 lines of
upstream behavior, and the escape hatch (`comm.mscomm`) means the interface does not have to
grow when upstream does. The one place the module owns real logic rather than delegating is
the compartment projection, and that is deliberately extracted as a pure function so it can
be tested without cobra, MSCommunity, or a solver.

**On the provenance gate.** It is worth being explicit about why this is a gate and not a
warning. Two classes share a name, most of their method names, and their general behavior.
A warning would be emitted once, into a notebook that scrolls, and the analysis would
continue and produce numbers. The failure mode this prevents is not a crash — it is a
correct-looking result from the wrong code.

**On reaching `ready` with `review_rounds` at 0.** The standing gate is that a PRD completes
one review round — Chris editing the document and those edits being folded — before it becomes
dispatchable. That did not happen here. Chris advanced it directly on 2026-09-24, and the
`review_rounds` field is left at 0 rather than incremented, because the field's job is to record
whether a review round ran and incrementing it to satisfy a gate would make it lie.

What stands in place of a review round is worth naming, so a dispatcher can judge it: four
author-side rounds (an upstream re-pin, the editEscher fold, a run prototype, and two
cross-family confront rounds totalling 47 stall points, 35 folded), plus two design questions
answered by Chris in the session — which repository the viz lives in, and whether to drop
graphviz. What is NOT covered is the one thing a review round is actually for: **nobody has read
this document end to end except its author.** The highest-value early check is therefore the
build itself — dispatch `t1` first and alone, because it carries the dependency gate, the pure
helpers and their tests, and a misunderstanding there will surface as a failing test rather than
as a wrong figure four phases later.

**On the external scan.** `task-c1a370b4` was dispatched to h100 before local research
began and had not returned at commit time. Its output lands at
`research/external-scan.md` on branch
`maestro/researcher/research-task-researcher-role-pr-task-c1a370b4`. The first review round
should fold it and revisit Q2 and Q4 specifically.

## Acceptance Criteria

1. `dependencies.yaml` declares `mscommunity` with `path`, `git` and `commit: "2dcff16f8e20a5b2a2acbb28b60a8ad38ec924fc"`, and `DependencyManager` is unchanged.
2. `_import_mscommunity()` resolves the package by normal import first, then via `get_dependency_path("mscommunity")`, and returns `None` rather than raising in every failure case.
3. `_import_mscommunity()` ACCEPTS a `MSCommunity` class whose `__module__` starts with `mscommunity.` and REJECTS one whose `__module__` starts with `modelseedpy`, and the rejection reason contains the literal phrase "the superseded copy" followed by the module path actually resolved.
4. A commit mismatch between `PINNED_MSCOMMUNITY_COMMIT` and the resolved checkout's HEAD logs a warning naming both SHAs and never raises, never refuses the import.
5. `CommunityDependencyError` derives from `kbutillib.core.errors.BackendUnavailableError`; `CommunitySolverError` and `CommunityVisualizationError` derive from `kbutillib.core.errors.KBUtilLibError`; all three are in `__all__`.
6. `build_community` records `member_ids` in community index order, `source_model_ids` mapping each member back to the id of the model passed in, `abundances`, `kinetic_coeff`, and `abundances_were_supplied`.
7. `save_community` writes `model.notes["kbutil.community"]` as a JSON string carrying `schema_version`, `member_ids`, `source_model_ids`, `abundances`, `kinetic_coeff` and `abundances_were_supplied`, and saves through the existing `save_model` path as a `KBaseFBA.FBAModel`.
8. `load_community` reconstructs the handle from `model.notes["kbutil.community"]` when present, falls back to an explicit `member_ids` argument, then to `model.notes["member_biomass_cpds"]`, and raises `ValueError` naming all three routes when none is available.
9. A `save_community` then `load_community` round trip preserves `member_ids` and `source_model_ids` exactly.
10. `predict_abundances(update=False)` leaves `comm.mscomm.abundances` and the `{metabolite: coefficient}` stoichiometry of `comm.mscomm.primary_biomass` identical, restoring in a `finally` block; `update=True` changes at least one of them.
11. `run_community_fba` returns a result whose `trustworthy` is False when the LP was sub-optimal, and does not raise on a sub-optimal or non-growing solve.
12. `run_community_fba` raises `CommunitySolverError` BEFORE solving when `min_member_growth > 0` and `comm.abundances_were_supplied` is True, with a message naming both inputs and stating that dropping either resolves it.
13. `run_community_fba` accepts no `fva_reactions` argument.
14. `media_id` on a result is the resolved media object's id, or the literal `"<model-default>"` when `media` was None.
15. Every delegate call is wrapped in `contextlib.redirect_stdout`; captured text matching upstream's markers is re-emitted through `logging` and recorded in `result.notes`, and `result.kinetics_relaxed` is True when the "Kinetic constraints disabled" marker appears.
16. A test asserts the stdout marker strings against the literal text present in `mscommunity/mscommsim.py`, so an upstream wording change fails loudly.
17. `run_micom` reads `_QP_CAPABLE` / `_pick_qp_backend` as MODULE-level names on `mscommunity.mscommsim`, not as attributes of the `MSCommunity` instance, and raises `CommunitySolverError` naming gurobi, cplex, osqp and hybrid when no QP backend is available.
18. `cross_feeding_table` calls `mscommviz.interactions` through `_unwrap` with `visualize=False`, passes the biochemistry database as the `msdb_path=` / `msdb=` keyword, and succeeds with no graphviz package and no `dot` binary present.
19. No `MSCOMMUNITY_MSDB_PATH` environment-variable fallback exists.
20. `cross_feeding_graph` returns a `networkx.DiGraph` with one node per member plus an `Environment` node, edges carrying `metabolite`, `flux` and `abs_flux`, a default `min_abs_flux` of 1e-4, and no dependency on graphviz.
21. No `render_cross_feeding` method exists and nothing in the module imports `graphviz` or calls `shutil.which("dot")`.
22. `fluxes_by_member` converts a hand-written cross-feeding DataFrame to `{member: {compound: flux}}` with signs unchanged, drops the `Environment` column, omits zero entries, and returns `{}` for a member whose only non-zero value is in `Environment`.
23. `project_member_fluxes(fluxes, member_index, member_biomass_id)` is a module-level pure function requiring no cobra import, and maps `{"rxn00001_c2": 5.0, "rxn00002_c1": 1.0, "EX_cpd00027_e0": -3.0, "bio3": 0.4}` with `member_index=2` and `member_biomass_id="bio3"` to exactly `{"rxn00001_c0": 5.0, "EX_cpd00027_e0": -3.0, "bio1": 0.4}`.
24. `render_member_map` renders against the member's SOURCE single-species model, not the community model, and raises a named `ValueError` when `source_model_ids` has no entry for the member.
25. The injected banner names the member id, the community id, the media id and the community growth rate, and contains the sentence stating that `EX_` exchange reactions are community-level and cannot be attributed to one member; `EX_` fluxes are included unmodified at community totals with no per-member attribution attempted.
26. Capability names are exactly the thirteen `community.<method_name>` strings listed in Implementation Decisions.
27. `kbu.community` is a lazy, idempotent facade property backed by `self._community`, and `kbutillib/__init__.py` re-exports `MSCommunityUtils` (not `MSCommunityUtilsImpl`) under the existing `_import_error` guard.
28. `python -c "import kbutillib; kbutillib.KBUtilLib().community.available"` runs without error on a machine with no MSCommunity checkout.
29. No `mscommunity`, `cobra`, `networkx` or `graphviz` symbol is imported at module scope in `ms_community_utils.py`.
30. `domains/modeling/README.md` lists `ms_community_utils.py` in its module table and canonical imports.
31. All Tier-1 tests pass with MSCommunity, cobra, graphviz and every solver absent; the Tier-2 file collects and skips cleanly in the same environment.
32. `build_community` passes `close_member_drains=True` to `MSCommunity` exactly when `abundances` is not None, records it on the handle, and forwards `build_solver` / `final_solver` with upstream's defaults.
33. `render_community_map` calls `escher_edit.build_member_reactions` once per condition with that condition's label as `model_id`, passes blocks to `build_escher_map` as `[(label, members), ...]` for several results and as a bare member list for one, and forwards `dashed=True` plus the requested `html` flag to `render_map_svg`.
34. `render_community_map` builds `escher_edit.palette.member_colors` EXACTLY ONCE from `comm.member_ids` and passes the identical mapping to every render call, verified against a fake `escher_edit` that records its calls.
35. `render_community_map` resolves compound display names through the biochemistry sibling and falls back to using compound ids as their own names when biochem is unavailable, without raising.
36. `render_community_map` returns a `CommunityMapArtifacts` carrying `map_json`, `svg`, `html`, `n_members`, `n_compounds` and `n_blocks`, with `svg` / `html` set to None when not requested.
37. `label_compounds` defaults to `"id"` and leaves upstream's labels untouched; `"name"` and `"name_id"` first derive a NEW `MapStyle` whose `input_column_dx` / `output_column_dx` come from `fitted_column_dx` over the display NAMES, lay out with that style, and only then rewrite the `<text class="node-label label">` contents. A caller-supplied `MapStyle` is never mutated.
38. A test asserts that `label_compounds="name"` produces a wider canvas than `label_compounds="id"` for the same community when the names are longer than the ids.
39. `_import_escher_edit()` adds `<declared path>/src` to `sys.path` — not the repo root — and returns None rather than raising when the package is absent; it applies NO provenance gate.
40. `dependencies.yaml` declares `escher_edit` with `path: "../editEscher"`, its git URL, and `commit: "6474698c67e8b9a1e8ab44c38686b27821d20389"`; `pyproject.toml` gains a `community` extra carrying `beautifulsoup4` and `lxml` only.
41. `test_member_growth(interacting=False)` on a community built WITH abundances reopens every member's drain from `member.biomass_drain_bounds` inside the model context manager before delegating, and records in the returned DataFrame's `.attrs` that it did so.
42. A test proves that solo growth measured through `test_member_growth` on an abundance-bound community is non-zero where the same call against `comm.mscomm.test_individual_species(interacting=False)` returns zero — the false-zero this guard exists to prevent.
43. No test that passed on the base commit fails on the branch.
44. `cross_feeding_graph` returns a `networkx.MultiDiGraph` (not a `DiGraph`) with one edge per donor->recipient->metabolite triple keyed on the metabolite id, and a test proves two metabolites traded between the same ordered member pair both survive.
45. `run_micom` takes one medium and returns a single `CommunityFBAResult`, not a list.
46. Exactly fourteen capabilities are registered, matching the `community.<method_name>` list in Implementation Decisions.
47. The provenance-rejection message, the commit-mismatch warning, the `load_community` no-provenance error, the `render_member_map` no-source-model error, the unknown-schema error and the member-projection banner all match the literals bound in Implementation Decisions, and a test asserts each.
48. `KINETICS_RELAXED_MARKER` and `NO_GROWTH_MARKER` exist as module-level constants and are asserted against the literal text in `mscommsim.py`.
49. `test_member_growth` reads drain bounds from `member.biomass_drain_bounds` and NOT from any `comm.mscomm.member_biomass_drains` attribute, which does not exist.
50. `load_community` treats `model.notes["member_biomass_cpds"]` as a dict `{model_id: [compound, ...]}`, not as an ordered list.
51. `gapfill_community` forwards to upstream as `default_gapfill_templates` / `default_gapfill_models`, never as `templates` / `models`.
52. Multi-condition blocks are assembled in sorted label order, labels are required to be unique non-empty strings, and `n_compounds` is the union of distinct compound ids across blocks.
53. `export_community_sbml` delegates to `comm.mscomm.to_sbml`, creates parent directories, and takes `overwrite=True` in its signature.
54. Compounds appearing only in the `Environment` column are excluded from `fluxes_by_member`, from compound-name resolution and from `n_compounds`.
55. `_import_escher_edit` and `_import_mscommunity` prefer an already-importable package over the `dependencies.yaml` path.
56. `load_community` accepts `kbutil.community` `schema_version` 1 and raises on any other value.
57. `predict_abundances` records `{"regularization": bool, "determinize": bool}` in the result notes.

