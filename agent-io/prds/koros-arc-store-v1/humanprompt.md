# koros-arc-store-v1

**One line:** build `kbutillib.koros_arc_store` once, as its own deliverable,
instead of building it as phase 1 of one of the two KIND apps that depend on it.

## Where this came from

Two KIND apps were designed in parallel on 2026-09-23 by two sessions that
talked to each other:

- **`kind-annotation-results-explorer-v1`** (GenomeAnnotationAggregator) --
  cross-arc comparison of genome annotation methods.
- **`kind-model-analysis-explorer-v1`** (KBUtilLib) -- models, FBA/FVA and
  fitness analyses organised by project and arc.

They share one layer: navigation over the KOROS runs tree, and a per-user run
database that both apps write analysis records into and read them back out of.
That layer has to be built exactly once.

The annotation PRD was going to build it as its own phase 1, and the model
PRD declared a hard external dependency on it. Both sessions independently
flagged that arrangement as the weakest thing in either plan, and put the
question to Chris. He chose extraction.

## Why extraction rather than leaving it

Four reasons, the first of which is the one a tool can check:

1. **No taskplan can express `depends_on` across PRDs.** `load_taskplan`
   validates within one plan. So the coupling between the two apps lived in
   prose, in two documents, and in nothing any validator reads. Extracting it
   does not fix that -- one PRD still cannot point at another -- but it makes
   the dependency a single, named, separately-dispatchable artifact rather
   than a buried phase, and it reduces the enforcement problem from "did the
   right phase of the right PRD run first" to "is this module on main".

2. **It was the highest-risk work in either plan**, carrying an interface
   negotiated between two sessions, and it sat inside a document about
   genome annotation, reviewed as part of a genome-annotation app.

3. **Two review histories could not be merged.** The alternative Chris floated
   was merging the two app PRDs into one. They have different review and
   confront histories (1/1 and 1/2), so a merged document could not say which
   half had been adversarially attacked.

4. **The apps are otherwise disjoint.** Past this module they share no code:
   different repos, different science, different record kinds.

## What is in scope

The module and nothing else: runs-tree access, the run database and the
`KorosArcStore` interface over it, the CAC conformance helpers, and -- new
here -- the reference test double that both consumer apps build their tests
against, so the fake and the real implementation cannot drift apart.

## What this changes in the two app PRDs

Both are reconciled in the same session that wrote this one. The annotation
PRD loses its three `p0*` tasks and gains the same external-dependency
declaration the model PRD already carries. Neither app's own work changes.

## The build order this creates

    koros-arc-store-v1  ->  on KBUtilLib main
        |
        +--> kind-annotation-results-explorer-v1   (independent of each other
        +--> kind-model-analysis-explorer-v1        from here on)

The two apps are parallel after this module lands, except that both touch
KBDLJobRunningPrototype, which needs separate repo slots under the
one-branch-per-repo rule.
