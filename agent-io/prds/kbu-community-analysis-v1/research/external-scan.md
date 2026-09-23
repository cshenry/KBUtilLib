# External scan: community metabolic modeling and visualization prior art

## Findings that bear on the design
- Separate concerns: expose a clear  façade and a pure merged-model builder. MSCommunity already supports both; KBUtilLib should present two entry points:  and  so users aren’t forced into one abstraction.
- Make abundance semantics explicit and orthogonal: require callers to specify  with allowed values like , ,  and a  policy like , , . Default to safe, documented behavior; log warnings when modes imply different outcomes.
- Require an explicit “objective policy” rather than overloaded defaults: e.g.,  or  to avoid MICOM/SteadyCom confusion about tradeoff vs equal growth.
- Return typed results with provenance: e.g.,  and . Avoid raw DataFrames only; include units and conventions.
- Embrace optional backends while isolating them: expose a stable KBUtilLib API that dispatches to MSCommunity backends (LP/QP/JAX/CuPy/PDLP) via lazy, optional imports. Don’t hard-require GPU/solver packages.
- Visualization should be pluggable and export-first: provide adapters that yield NetworkX graphs, Graphviz dot strings, Escher overlays, and Plotly Sankey frames; renderers should support both notebook display and saved SVG/HTML/PNG. Provide default styles that scale to 10–30 members and degrade gracefully beyond that.
- Avoid rebuilding general-purpose utilities: depend on , ,  Sankey, , and  for SBML I/O; only add thin adapters and opinionated defaults.
- Package hygiene: treat MSCommunity as an optional VCS dependency pinned by commit in  (e.g., ). Guard imports and surface helpful install hints. Provide a fallback stub that raises a clear error if missing.

## 1) Prior art in community metabolic modeling toolkits (API shape and usability)

- MICOM (Python)
  - Abstractions: community object over a merged COBRA model; cooperative trade-off objective (community growth vs member growth) and abundance fitting utilities.
  - Public API shape: construct  from individual COBRA models and an abundance table; call methods like , , , . Results return DataFrames for growth rates and exchanges. Docs emphasize the  parameter controlling community vs individual growth optimization.
  - Usability notes: clear high-level  class; can confuse users about whether provided abundances are constraints vs targets (depends on using  vs ); requires care to set media and the tradeoff alpha; strong pandas-first outputs ease analysis in notebooks.
  - References: micom docs and repo homepages.

- SteadyCom (MATLAB) and SteadyComPy (Python wrappers/ports)
  - Abstractions: joint model with enforced equal growth rate across members (µ shared); “steady-state” community composition at common growth. Typically users assemble a community model and call a steady-state solver that enforces equal µ while adjusting fluxes and member biomasses.
  - Public API shape: MATLAB functions within the COBRA Toolbox; Python ports/wrappers expose  style functions that set equal growth constraints and solve. Inputs often include optional target growth or minimal community growth; outputs include member fluxes and equal µ.
  - Usability notes: the equal-growth constraint is a strong assumption that differs from MICOM; users often hit infeasibility when media or minimal growth targets are not consistent. Requires explicit documentation of “equal µ” and how to relax it. Python support exists but is less standardized than MICOM.
  - References: COBRA Toolbox SteadyCom docs and community Python ports/wrappers.

- SMETANA (Python CLI/library)
  - Abstractions: not an FBA-based joint optimization of abundances; rather, computes metabolic interaction scores (e.g., metabolic support score) between species on media. Emphasis on predicting cross-feeding potential, not steady-state abundances.
  - Public API shape: primarily a command-line tool () with Python library utilities; inputs are individual models and media; outputs are CSV/TSV tables of interaction scores and required exchanges.
  - Usability notes: clear inputs/outputs; avoids the abundance semantics trap by not modeling abundances directly. Users sometimes conflate SMETANA outputs with dynamic predictions; docs could better emphasize its scope.
  - References: ReadTheDocs and GitHub.

- COMETS (engine with Python interface)
  - Abstractions: dynamic spatiotemporal simulation of multiple models on a lattice; explicit initial biomass (abundances), media, diffusion; integrates growth over time with exchange reactions and spatial layout. Uses external solvers per time step.
  - Public API shape: model and environment builders plus a simulation runner; Python tools wrap configuration files and results parsing. Outputs time series for biomass and metabolites.
  - Usability notes: powerful for dynamics and spatial effects; setup overhead is higher (config files, lattice); abundance is clearly an initial state, not a constraint. Good for time-course plots; not a drop-in “steady-state FBA” tool.
  - References: COMETS website and GitHub.

- COBRApy community tooling
  - Abstractions: no canonical  class; users typically merge models (distinct compartments or ID prefixes) using  utilities and then solve with standard FBA. Some third-party examples implement community wrappers atop COBRApy.
  - Public API shape: functions to merge models and manipulate compartments; users manually define objectives and constraints.
  - Usability notes: maximal flexibility but low guidance; easy to produce unintentionally coupled reactions across members if IDs/compartments aren’t handled carefully. Visualization and abundance semantics left to user code.
  - References: COBRApy docs and repo.

- PyCoMo (Python)
  - Abstractions: community modeling wrapper over COBRApy; emphasis on simple community construction and basic cross-feeding outputs.
  - Public API shape: -like class with methods to add member models and run combined FBA; limited maintenance.
  - Usability notes: narrow scope; suitable for small demos but not actively developed.
  - References: project README.

- MMinte (Python)
  - Abstractions: pipeline from 16S sequences through model reconstruction to pairwise interaction inference; focuses on pairwise interaction types rather than full joint model optimization.
  - Public API shape: scripts and library functions to run pipeline steps; outputs include inferred interaction networks.
  - Usability notes: strong focus on integration pipeline; less a general-purpose community FBA toolkit.
  - References: project GitHub.

- BacArena (R)
  - Abstractions: agent-based simulation;  as a spatial grid with organisms (agents) added via . Growth and interactions simulated stepwise; abundance as agent counts with birth/death.
  - Public API shape: S4 classes , , functions like , , . Outputs include time-resolved agent distributions and metabolite fields.
  - Usability notes: approachable for R users; explicit initial abundances; emphasizes spatial dynamics; not a steady-state LP/QP optimizer but integrates per-step FBA.
  - References: CRAN manual.

- gapseq community workflows
  - Abstractions: reconstruction/gapfilling of individual models from genomes; “community” typically achieved by merging reconstructed models and optionally applying community-specific constraints.
  - Public API shape: command-line  and scripts; no dedicated, standardized community API beyond merging.
  - Usability notes: strong single-species reconstruction; community analysis is ad hoc and reliant on downstream tools.
  - References: gapseq GitHub.

## 2) The abundance/growth-rate semantics trap (API exposure and user confusion)

- MICOM
  - Exposure:  optimizes a cooperative trade-off between community and individual growth; abundances can be fitted via  using regularization to match observed abundances. Thus, abundances are either inputs to fit against or parameters for objective weighting, not hard constraints unless explicitly modeled.
  - Confusions/failure modes: users often assume providing abundances turns them into fixed constraints; instead, they need  to reconcile growth with observed abundances. Misinterpreting  causes unexpected distributions (α near 1 favors community growth, α near 0 favors individual growth). Reported issues include infeasibility when media insufficient under high tradeoff and confusion over media scaling when fitting.

- SteadyCom / SteadyComPy
  - Exposure: enforces equal growth rate across members (µ shared). Users can specify minimum community growth or member constraints; abundances emerge as steady-state compositions consistent with equal µ. Abundances are outputs at steady state, not arbitrary fixed inputs.
  - Confusions/failure modes: frequent infeasibility when equal µ is incompatible with media or maintenance; users surprise when trying to fix abundances and µ simultaneously. A common pitfall is forgetting to scale maintenance ATP for all members, leading to “no feasible solution”.

- SMETANA
  - Exposure: does not model abundances or growth optimization; computes metabolic interdependence scores given models and media. No abundance semantics to misinterpret.
  - Confusions/failure modes: users sometimes read MSS as predicting actual growth or time dynamics; not the case by design.

- COMETS
  - Exposure: abundances are initial biomass values; growth rates are dynamic outputs over time. No enforced equal µ; members can diverge. Exchange constraints and diffusion drive dynamics.
  - Confusions/failure modes: treating initial biomass as fixed abundance or expecting steady-state outcomes; confusion about time-step stability and solver settings can lead to numerical instability or zero-growth artifacts with tight timesteps.

- COBRApy community workflows
  - Exposure: abundance semantics are user-defined; equal µ and tradeoff must be enforced manually if desired.
  - Confusions/failure modes: unintended coupling by shared metabolites without compartment separation; accidentally “sharing” internal metabolites; mis-specified objectives that bias one member; interpreting fluxes as abundances without a defined mapping.

- MMinte and PyCoMo
  - Exposure: pipeline-level or simplified wrappers; abundances typically not directly optimized; focus on interaction sign or basic combined models.
  - Confusions/failure modes: extrapolating to abundance predictions beyond scope.

- BacArena
  - Exposure: initial agent counts define initial abundances; growth emerges via agent rules; no equal µ constraint.
  - Confusions/failure modes: users misinterpret agent counts as deterministic trajectories; stochasticity and spatial effects dominate; sensitive to step sizes and local media.

- gapseq community workflows
  - Exposure: none formal; merging models leaves abundance/growth semantics to downstream tools.
  - Confusions/failure modes: assuming gapseq delivers community abundance predictions by itself.

Design implication: MSCommunity’s API should force callers to choose what “abundance” means in their run, rather than infer it, and should validate incompatible combinations (e.g., both fixed abundances and equal-µ simultaneously) with clear errors.

## 3) Visualization prior art (cross-feeding, maps, Sankey, interactive widgets)

- Cross-feeding / exchange networks
  - Libraries: NetworkX for graph construction; Graphviz (dot) for high-quality static layout; PyVis (vis.js) or Plotly for interactive notebooks; Cytoscape/ipycytoscape for large networks and rich interaction.
  - Notebook vs static: Graphviz renders crisp SVGs inline and exports well; NetworkX+Matplotlib is quick but aesthetically limited; Plotly renders interactively in notebooks and exports to HTML and static images via kaleido; PyVis emits HTML; ipycytoscape integrates Cytoscape.js in notebooks with export options.
  - Scaling limits: layouts become unreadable beyond ~30–50 nodes/edges without grouping; Graphviz  can handle hundreds but still visually dense; interactive filtering and grouping are required for >20 members or >200 exchanges. Cytoscape scales better with UI filtering; static diagrams hit readability limits quickly.

- Escher maps for community models
  - Libraries: Escher overlays fluxes on pathway maps; supports JSON maps and overlaying reaction fluxes. Community models require either per-member color channels or separate overlays; escher works well in notebooks and exports SVG/PNG.
  - Scaling limits: maps look good for tens to hundreds of reactions; overlaying multiple members on a single map needs careful legend design; beyond two or three members, layered colors become confusing.

- Sankey, chord, alluvial diagrams
  - Libraries: Plotly Sankey, Holoviews/Plotly, Altair/Vega-Lite chord-like diagrams; these are effective for aggregate exchange flows between members and media.
  - Notebook vs static: Plotly supports interactive filtering and hover; static exports via kaleido produce publication-quality figures. Chords with many links become hairballs; practical cap ~20–40 links unless grouped.

- Interactive widgets
  - Libraries: ipywidgets controls for filtering, ipycytoscape for network exploration, PyVis for quick interactive views. For export, prefer generating a saved HTML bundle alongside a static SVG/PNG fallback.

Design implication: KBUtilLib should output both a graph object (NetworkX) and ready-made exporters to SVG/HTML; provide defaults that group by metabolite class and hide low-flux edges to keep figures readable by default.

## 4) Reusable tools we should not rebuild
- Network construction and layout:  for graph building;  (via  or ) for dot layout; avoid re-implementing layouts.
- Interactive network viewing:  and  for notebook interactivity; use Plotly for Sankey.
- Pathway visualization:  for flux overlays on maps; leverage existing maps and styles.
- SBML I/O and model plumbing: COBRApy (, , ); avoid custom SBML code.
- Optimization modeling: rely on Optlang via COBRApy; for batch LP/QP, use MSCommunity’s batched solver wrappers when available instead of rolling our own.
- Data handling:  for tabular outputs; optional  for speed if desired, but not required.

## 5) Packaging and dependency risk for MSCommunity (GitHub-only dependency)
- Current status: MSCommunity is not on PyPI (setup.py indicates 0.0.1; depends on , , , , , , optional , , ). Downstream libs must treat it as an optional VCS dependency.
- Recommended strategy for KBUtilLib:
  - Optional extra: define  in /. Pin to a tested commit SHA, not branch or tag, to ensure reproducibility.
  - Lazy imports: wrap imports in a small adapter module, e.g.,  that raises a clear ImportError with install instructions if unavailable.
  - Capability flags: expose  and gate features accordingly; surface helpful error messages rather than ImportErrors deep in code.
  - Solver optionality: MSCommunity’s optional GPU/JAX/PDLP backends should be gated behind feature flags and try/except imports; document tested versions and fallbacks.
  - CI approach: run two CI matrices: one with the extra installed (full tests) and one without (ensuring graceful degradation). Cache the VCS dependency to avoid rate limits.
  - Vendor of last resort: if stability is critical, consider vendoring a minimal, fixed snapshot under a separate namespace with clear provenance; avoid unless necessary to meet release timelines.
- How others handle this:
  - Many bioinformatics packages pin to VCS URLs for upstreams not on PyPI, e.g.,  with optional  when needed; optional extras and lazy imports prevent hard failures for users not needing the feature.
  - Documented examples include scientific Python packages using optional GPU backends (e.g., CuPy) guarded by optional extras and runtime checks; documentation prominently lists optional dependencies and features.

Design implication: Treat MSCommunity as an optional capability in KBUtilLib with strong guardrails and reproducible pinning; don’t make it a hard dependency until it’s published on PyPI.

## Sources
- MICOM docs: https://micom-dev.github.io/micom/ — user guide and API reference.
- MICOM GitHub: https://github.com/micom-dev/micom — source and examples.
- SMETANA docs: https://smetana.readthedocs.io/ — methods and CLI reference.
- SMETANA GitHub: https://github.com/cdanielmachado/smetana — code and issues.
- COMETS site: https://comets.bu.edu — overview and downloads.
- COMETS GitHub: https://github.com/segrelab/comets — source and interfaces.
- COBRApy docs: https://opencobra.github.io/cobrapy/ — merging models, I/O.
- COBRApy GitHub: https://github.com/opencobra/cobrapy — code and issues.
- BacArena CRAN: https://cran.r-project.org/package=BacArena — manual (PDF) and examples.
- BacArena GitHub: https://github.com/euba/BacArena — source and issues.
- PyCoMo GitHub: https://github.com/Eladoren/pycomo — lightweight Python community modeling.
- MMinte GitHub: https://github.com/segrelab/mminte — pipeline for microbial interactions.
- gapseq GitHub: https://github.com/jotech/gapseq — reconstruction; community workflows discussed in issues/wiki.
- Escher: https://escher.github.io — pathway visualization in notebooks.
- NetworkX: https://networkx.org — graph analysis and construction.
- Graphviz: https://graphviz.org — dot layout and static rendering.
- Plotly Sankey docs: https://plotly.com/python/sankey-diagram/ — interactive flow diagrams.
- ipycytoscape: https://github.com/QuantStack/ipycytoscape — Cytoscape.js in Jupyter.

Notes: For some tools (SteadyComPy, PyCoMo) Python wrappers/ports vary and may be less standardized; verify the specific package used in your environment. Where issue-level citations of failure modes are not linked, they derive from user reports in project issue trackers and documentation; exact issue IDs should be added during implementation if needed.
