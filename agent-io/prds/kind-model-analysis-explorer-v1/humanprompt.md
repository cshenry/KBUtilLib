# PRD: kind-model-analysis-explorer-v1 — Models and Analyses, by Project and Arc

## Problem Statement

Chris builds metabolic models with KBDL and analyses them with KBUtilLib and
with KBDL's own analysis job types.
The work happens inside KOROS research arcs, grouped into projects, and he
runs it across many arcs at once.

After the run, none of it is browsable.
A model is a cobra JSON file or an object-store object id.
An FBA result is whatever `kbu model fba --json` printed into a terminal.
A fitness analysis is a `gaa_data` block inside a job result.
Nothing anywhere records which arc, or which project, any of it belongs to —
verified, not assumed: grepping both producer repos for `KOROS_RUNS`,
`Science/runs` or `arcs/` returns zero hits.

So the question "what modelling have I done in this project, and what did it
show" has no answer short of remembering which directories to look in.
The arcs know their own identity and lineage; the analyses know their own
results; the two have never been connected.

## Solution

A KIND app, **Models and Analyses**, that drills down through four levels:

1. **Portfolio** — every project the user has, every arc in it, with a count
   of the modelling work done in each and when it last moved.
2. **Arc** — a table of the models analysed in that arc: what genome each came
   from, how big it is, what was run on it, and when.
3. **Model** — genes and reactions with annotations, reaction classifications,
   and fitness agreement.
4. **Escher** — the model and its flux drawn on a metabolic map.

Levels 3 and 4 are **not built by this PRD**, because they already exist.
`EscherUtils.create_fitness_dashboard` in KBUtilLib already renders an Escher
map recoloured by fitness class with a condition dropdown and an FVA-solution
dropdown, plus five tabular views — genes with annotations, reactions with
classes, fitness detail, conditions, and concordance.
`EscherUtils.create_map_html2` already renders a model with flux, reaction-class
overlays and numerical badges.
This PRD wires navigation on top of them and generates them on demand.

What the PRD actually builds is the thing that does not exist: **the join
between an analysis and the arc it was run in.**

Producers stamp a small record onto the arc when they run.
KIND already sets `KOROS_ARC` in the environment of every session it starts
(`king_backend/session.py`), so a `kbu model fba` run inside a KIND session
knows its own arc without the scientist doing anything.
KBDL jobs, which execute server-side where that variable does not exist, carry
the arc as an explicit job parameter set by the client.

The records live in a sidecar beside the arc, read by a small shared library
that also resolves the runs root and enumerates projects and arcs.
That library — `KorosArcStore` — is **shared with the annotation explorer**
being designed in parallel, and is **built by that PRD, not this one**.

Two details of it are worth stating here because they are not obvious.

**The records are sharded per writer, not one file per arc.** The runs tree is
Dropbox-synced, and Dropbox does not merge concurrent appends — it writes a
conflicted copy and says nothing. A single shared index would therefore lose
records silently, and a provenance store that quietly holds less than it should
is worse than one that refuses the write, because the gap looks exactly like
work that never happened.

**Every record carries a trust tier, separately from whether the job
succeeded.** Those are different questions. A gapfilled reaction from a
cleanly-completed reconstruction is a successful job and is still an argument
rather than an observation. This matters here more than in most places, because
the whole point of the deepest view is to show model predictions next to
measured RB-TnSeq fitness — and the contract KIND's apps work to forbids
presenting the first as though it had the standing of the second.

## What this is not

It is not a new database, and it does not add a column to the KBDL object
store — that store's published metadata shape is asserted by its own tests
and is deliberately closed.

It is not a re-implementation of the fitness dashboard. If the existing
dashboard is wrong about something, that is a defect to fix in
`EscherUtils`, not a reason for this app to draw its own.

It is not a general provenance system. It records enough to navigate to an
analysis and hand it to a renderer, and nothing more.

It does not touch the five KIND repos. KIND is consume-only; the app
registers itself as a JSON plugin manifest in the union plugin directory.
