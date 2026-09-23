# kbu-community-analysis-v1 — human prompt

Add utilities to KBUtilLib for microbial community metabolic analysis, built on
[MSCommunity](https://github.com/ModelSEED/MSCommunity).

The module should let a notebook user:

- **create** a community model by merging single-species ModelSEED/KBase models,
  with optional member abundances;
- **run** the community — plain community FBA, MICOM-style tradeoff simulation,
  abundance prediction, member-by-member growth testing, and gapfilling;
- **display** the result with KBUtilLib's existing visualization tools — the
  cross-feeding exchange network, and Escher maps of individual members carrying
  their community fluxes.

It should follow KBUtilLib's current composition architecture (`domains/modeling/`,
a legacy class plus an `*Impl` wrapper, reached through a lazy facade property),
degrade gracefully when its optional dependencies are absent, and load MSCommunity
through the same `dependencies.yaml` mechanism already used for `modelseedpy`,
`ModelSEEDDatabase` and `cobrakbase`.
