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

**There is no community Escher visualization in MSCommunity.** This was checked directly
rather than assumed: `mscommviz.py` is byte-identical between `b5f37c4b` and `2dcff16`;
`grep -ri escher` over the whole repository returns nothing on `main`, nothing on the only
other branch (`gpu`, last touched 2026-06-11 and eleven commits behind), and nothing on
`freiburgermsu/MSCommunity`, whose only two branches are `main` and the already-merged
`fix/member-biomass-drains`. Q4's per-member projection therefore stands unchanged, not
because it was re-argued but because the alternative does not exist to adopt.

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

# DISPLAY
table, exchanged = kbu.community.cross_feeding_table(comm, result)   # DataFrames, no graphviz
g = kbu.community.cross_feeding_graph(comm, result)                  # networkx.DiGraph
kbu.community.render_cross_feeding(comm, result, output_path="xfeed.svg")
kbu.community.render_member_map(comm, "iML1515", map="core", result=result,
                                output_path="iML1515_in_community.html")
```

The module is a **deep module**: a short interface over a large amount of behavior.
Behind `build_community` sits compartment renaming, biomass-reaction renumbering, abundance
normalization and kinetic-package construction; behind `run_community_fba` sits solver
selection, sub-optimality detection and per-member flux attribution; behind
`render_member_map` sits the compartment-index projection that makes a community flux
vector legible to a single-organism Escher map.

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
17. As a modeler, I want a rendered cross-feeding network diagram, so that I can see at a
  glance who feeds whom.
18. As a modeler, I want the cross-feeding table even when graphviz is not installed, so
  that a missing system binary costs me the picture and not the analysis.
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
30. As a maintainer, I want the MSCommunity dependency pinned to a tested commit SHA and
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
```

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
scope, so `import kbutillib` never requires MSCommunity, cobra or graphviz.

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
run_micom(comm, media, tradeoff=0.6) -> list[CommunityFBAResult]
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
render_cross_feeding(comm, result=None, output_path=..., export_format="svg",
                     node_metabolites=True) -> Path
render_member_map(comm, member_id, map, output_path, result=None,
                  **escher_kwargs) -> Path
```

`cross_feeding_table` returns MSCommunity's `(cross_feeding_df, exchanged_mets_df)` pair
with `visualize=False`, so **the table is always obtainable without graphviz**.
`msdb` is passed to upstream as the keyword `msdb_path=` (or `msdb=` for an already-loaded
object) on the `interactions` call, alongside `visualize=False` — bound explicitly here
because a positional call would break the moment upstream's signature moves. The confront
round proposed an `MSCOMMUNITY_MSDB_PATH` environment-variable fallback for the case where
the keyword is unsupported; that is **declined**, because upstream takes the keyword today
and a speculative env-var path is untestable dead code that would hide a real signature
break behind a silent fallback.

`msdb` is threaded through but **not needed for the table**: upstream uses it only inside
its `if visualize:` branch (`mscommviz.py:99-252`), so a caller with no ModelSEED
biochemistry checkout can still get the DataFrames. It is resolved from
`get_dependency_path("ModelSEEDDatabase")` when absent so that `render_cross_feeding`,
which does need it, works without an argument.

`cross_feeding_graph` builds a `networkx.DiGraph` **from the cross-feeding DataFrame**,
with one node per member plus one `Environment` node, and one edge per donor→recipient
metabolite carrying `metabolite`, `flux` and `abs_flux` attributes. Edges below
`min_abs_flux` are dropped.
It exists because the external scan's clearest visualization finding is that a layer should
emit a graph object and not only rendered files: a caller who wants Plotly, Cytoscape, their
own layout, or simply to count edges should not have to go through graphviz to get one.
It is about fifteen lines over data the table already produced, and it makes the
graph-shaped question answerable with no system binary at all.

`render_cross_feeding` is the graphviz path. When the `graphviz` Python package or the
`dot` system binary is missing it raises `CommunityVisualizationError` naming which of the
two is absent and how to install it — it does not silently produce nothing.
The scan reports these layouts become unreadable past roughly 30–50 nodes; the method
therefore emits a `logger.warning` naming the node count when the graph exceeds **40 nodes**
— a fixed threshold, not a tunable — and points at `cross_feeding_graph` for a filterable
alternative. It does not refuse to render, because 40 is guidance and the caller may know
better than the guidance.

`render_member_map` is the projection, and it is the one genuinely new piece of logic:

1. Take `member_id`, look up its community index `i` (1-based, from `member_ids`).
2. From the result's flux series, select reactions whose compartment suffix is `c{i}` or
  the shared `e0`, plus the member's `bio{n}` reaction.
3. Rewrite `_c{i}` → `_c0` in those reaction IDs, and map `bio{n}` → `bio1`.
4. Hand the rewritten flux dict and the *member's source model* to
  `self.escher.create_map_html2(model, map, output_path, flux=...)`.

Step 4 uses the **source single-species model**, not the community model, precisely because
the Escher map was drawn against single-organism IDs. The community supplies the fluxes;
the source model supplies the geometry.
The generated HTML carries a banner naming the member, the community, the medium and the
community growth rate, so a projection is never mistaken for a standalone simulation.

Exchange reactions are a deliberate edge case: `EX_` reactions live in the shared `e0`
compartment and are **community-level**, not member-level. They are included in the
projection with an explicit note in the banner, because a member's uptake is not separable
from the community's on a shared extracellular compartment.

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
CLI and HTTP caller: `community.build_community`, `community.load_community`,
`community.save_community`, `community.export_community_sbml`,
`community.run_community_fba`, `community.predict_abundances`, `community.run_micom`,
`community.test_member_growth`, `community.gapfill_community`,
`community.cross_feeding_table`, `community.cross_feeding_graph`,
`community.render_cross_feeding`, `community.render_member_map`.
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
- `CommunityVisualizationError(KBUtilLibError)` — graphviz package or `dot` binary missing.



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

### Q4. How is a community model displayed on an Escher map? — DECIDED: Per-member projection onto the member's own source model and an ordinary single-organism map.

**Blast radius:** MEDIUM
**Why:** Escher maps are keyed on reaction ID and every map KBUtilLib can reach is drawn
against `_c0`/`_e0`. A community model uses `_c1.._cN` with a shared `_e0`. The three
options were: project per member (chosen), generate a synthetic community map, or skip
Escher and offer only the cross-feeding graph. Generating a community map means laying out
N copies of a metabolic network and there is no existing map to build from; skipping Escher
throws away the toolkit's main visualization asset for the one analysis that most needs it.
The scan supports the choice on its own terms: it reports that layering members onto one
Escher map "becomes confusing beyond two or three members" and that community models need
per-member overlays. What it added, and what is now folded in, is that the layer should also
emit a `networkx.DiGraph` (`cross_feeding_graph`) rather than only rendered files, and that
cross-feeding layouts go unreadable past roughly 30–50 nodes.
**If you disagree:** `render_member_map` and its pure-function projection helper are
removed or replaced; user stories 19 and 20 go; the Tier-1 projection test — the only test
that covers genuinely new logic — has nothing to test; and `kbu.escher` stops being a
dependency of the new module, simplifying the facade property.
`cross_feeding_graph` is separable and survives either way.
**Confidence:** medium, raised from low by the scan on the *mechanism* (per-member overlay
is what the field does) but unchanged on the *usability* — nobody has looked at one of
these projections yet, which is what would settle it.

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

### Q6. Where does MSCommunity get declared as a dependency? — DECIDED: `dependencies.yaml`, not `pyproject.toml`.

**Blast radius:** LOW
**Why:** MSCommunity has no PyPI release (`setup.py`, version 0.0.1). A `pyproject.toml`
entry would have to be a git URL, which breaks `pip install KBUtilLib` for anyone without
git credentials and pins a moving target. `dependencies.yaml` is the mechanism KBUtilLib
already uses for exactly this class of dependency — `modelseedpy`, `ModelSEEDDatabase`,
`cobrakbase`, `cb_annotation_ontology_api` — and `get_dependency_path` already returns
`None` rather than raising when the checkout is absent.
The scan's one objection to a bare VCS dependency is reproducibility: it recommends pinning
a **tested commit SHA rather than a branch**. `dependencies.yaml` as used today records only
`path` and `git`, so the entry adds a third key, `commit: 2dcff16`, and
`_import_mscommunity()` logs a warning (never raises) when the resolved checkout's HEAD does
not match it. Pinning without enforcing keeps a developer working on a newer MSCommunity from
being blocked, while still making the drift visible.
**If you disagree:** `pyproject.toml` gains an optional-extra group; `_import_mscommunity()`
loses its `get_dependency_path` fallback branch and its Tier-1 test; and CI must install
from git to exercise Tier 2.
**Confidence:** high — this follows an established repo pattern with a worked example in
`domains/thermo/thermo_predictors/base.py`. The `commit` key is new to `dependencies.yaml`
and no other entry carries one, so it must be additive: `DependencyManager._load_config`
ignores unknown keys, which was checked, but nothing else in the repo reads it yet.

### Q7. What happens when graphviz is missing? — DECIDED: The cross-feeding *table* still works; only rendering raises, and the error names which of package-or-binary is missing.

**Blast radius:** LOW
**Why:** `graphviz` needs both a Python package and a `dot` system binary, and the binary is
the one that is usually absent on a cluster. Splitting `cross_feeding_table` from
`render_cross_feeding` means a missing system binary costs the picture and not the analysis.
Upstream couples them: `interactions(visualize=True)` is the default path.
**If you disagree:** the two methods merge back into one; user story 18 goes; and
`CommunityVisualizationError` loses its reason to exist as a separate type.
**Confidence:** high.

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

- **Whether a projected member Escher view is useful in practice.** The mechanism is
verified; the usability is asserted. Nobody has looked at one. This is the
flag-not-question: no artifact I can read says what a modeler wants to see when they ask
to "display a community model", and I assumed the answer is "one member at a time, on the
map they already use". If the answer is "the whole community at once", Q4 is wrong and so
is a third of the visualization work.
- **Whether** `modelseedpy.community` **will be retired.** If ModelSEEDpy drops its copy, Q3's
provenance gate becomes dead code; if ModelSEEDpy instead re-absorbs the standalone
package, the gate becomes actively wrong. I have no artifact stating either intention —
the two repos' git histories do not reference each other.
- **Whether the scan's stripped API names hid a contradiction.** The returned file has
every inline code span empty, so wherever it named a class or method the name is gone. Its
*arguments* are followable and were folded; its *citations of specific APIs* could not be
checked. I did not re-run the scan to recover them, because the findings that mattered —
typed results, per-member overlays, validate-incompatible-combinations, pin a SHA — are
carried by prose that survived intact.
- **Whether** `load_community` **can recover** `member_ids` **from a saved community model in
general.** It works when `model.notes["member_biomass_cpds"]` survives the save/load
round trip through the KBase workspace. I could not verify that it does, because it
depends on the workspace model serializer's treatment of `notes`, and I did not run one.
The design raises rather than guessing when the key is absent, which makes the failure
visible instead of wrong.



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

**The ModelSEED biochemistry database is needed to DRAW the cross-feeding graph, not to
compute it.** `visual_interactions` asserts on `msdb or msdb_path` and then calls
`msdb.compounds.get_by_id` for every cross-fed metabolite; `interactions()` touches `msdb`
only inside its `if visualize:` branch. So the failure lands *after* the simulation has been
paid for and *after* the table is already in hand — which is precisely why
`cross_feeding_table` and `render_cross_feeding` are separate methods rather than one call
with a flag. A reader who assumed the biochem DB was needed throughout would over-constrain
every community workflow.

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

**A** `render_member_map` **projection is not a simulation of that member.** It is the member's
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
- **Generating a community-wide Escher map.** No such map exists to build from, and laying
one out is a project, not a task. Re-checked 2026-09-23 against MSCommunity tip `2dcff16`:
the package ships no Escher code of any kind on any branch, so there is nothing upstream to
adopt here either.
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
21. `render_cross_feeding` raises `CommunityVisualizationError` naming the graphviz PACKAGE when the import fails and naming the `dot` BINARY when `shutil.which("dot")` returns None, as two distinguishable messages.
22. `render_cross_feeding` emits a `logger.warning` naming the node count when the graph exceeds 40 nodes, and still renders.
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
33. `test_member_growth(interacting=False)` on a community built WITH abundances reopens every member's drain from `member.biomass_drain_bounds` inside the model context manager before delegating, and records in the returned DataFrame's `.attrs` that it did so.
34. A test proves that solo growth measured through `test_member_growth` on an abundance-bound community is non-zero where the same call against `comm.mscomm.test_individual_species(interacting=False)` returns zero — the false-zero this guard exists to prevent.
35. No test that passed on the base commit fails on the branch.

