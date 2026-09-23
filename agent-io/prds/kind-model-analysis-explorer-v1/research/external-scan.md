# External Scan: Kind Model Analysis Explorer v1

Recommendation (TL;DR):
- Embed Escher via its JS bundle (`unpkg.com/escher`) or npm import and instantiate `escher.Builder` directly in the web app; avoid iframe unless isolating untrusted content. Treat maps as static JSON assets loaded via app’s asset pipeline with attention to `publicPath`. Use Escher 1.8.x (docs latest) and verify widget → JS parity. Precompute or chunk very large maps and avoid megabyte-scale JSON in a single payload.
- For multi-model navigation, borrow drill-down patterns from BiGG, MetaNetX, and ModelSEED: collection → model → pathways/analyses. Normalize identifiers with cross-ref tables (MNXref/BiGG IDs) and provide per-model synonym resolution instead of forcing a single namespace.
- For provenance/run indexing, adopt RO-Crate as the outer “record envelope” (JSON-LD metadata for a directory) and store tool/run-specific payloads inside. Where workflow engines exist (Snakemake/Nextflow), reference their provenance; otherwise define a minimal profile aligned to RO-Crate and CWLProv.
- Don’t rely on append-only per-dir JSONL indexes beyond small scales: move to SQLite/duckdb per-arc or an embedded write-ahead log with compaction; use file locks and crash-safety techniques if staying with JSONL.
- For gene essentiality agreement, follow MEMOTE and published FBA/TnSeq works: present confusion-matrix metrics (precision/recall/MCC) and per-gene tiles; add an Escher overlay for agreement classes and sortable tables.

## A. Escher embedding in web apps (1.8.x, escher-python)

Supported embedding paths:
- Static JS/CSS files in any HTML (Escher docs 1.8.2 “Using the static JavaScript and CSS files”): `https://unpkg.com/escher/dist/escher.js` or `escher.min.js`; npm `escher`; yarn `add escher`.
  - Source: escher.readthedocs.io/en/latest/development.html (section “Using the static JavaScript and CSS files”).
- JavaScript API (`escher.Builder`) usable outside notebooks; see docs “JavaScript API”.
  - Source: escher.readthedocs.io/en/latest/javascript_api.html (Escher 1.8.2).
- Python notebook widget (anywidget) is for Jupyter; not needed in web apps.
  - Source: GitHub README (latest shows JS artifacts and Python packaging steps).

What breaks / pitfalls:
- Bundlers and `publicPath` (Vite/Webpack): Escher loads assets (fonts/CSS) from its own bundle; when importing via npm, ensure your bundler serves `dist/escher.css` with the correct public base path. If using CDN via `<script src=...>`, do not rely on your app’s `publicPath` for Escher files.
- AMD/RequireJS vs ESM: Docs include RequireJS for notebook embedding; modern apps should import the UMD/ESM bundle from npm or unpkg. Mixing RequireJS with ESM can cause double-loads. Prefer the ESM path your bundler supports.
- Iframe sandboxing: Escher requires pointer/keyboard events; `sandbox`ed iframes with `allow-scripts` but without `allow-same-origin` can block asset loads and localStorage; cross-document drag/select may behave poorly. If isolating, explicitly grant `allow-scripts allow-same-origin` and serve maps from same origin or set CORS.
- Map JSON size: Large maps (multi-MB) will stall main-thread parse and inflate memory; search indexing in Escher can be disabled (`enable_search=false`) for perf. Consider chunking or lazy-loading overlays and avoid embedding giant JSON blobs in HTML; stream over HTTP.
- CSS conflicts: Escher styles assume certain defaults; namespacing your CSS and loading `escher.css` to avoid resets fighting.

Existing web tools embedding Escher (examples, strengths/limits):
- MEMOTE reports (0.11.1): Web reports are static HTML/JS; they do not embed live Escher editors but show model assessment. Essentiality inputs are supported; no Escher map embedding by default. Source: memote.readthedocs.io.
- DD-DeCaF “Caffeine” (circa 2018–2020): A web platform for metabolic engineering that included pathway visualization leveraging Escher-like maps; they handled data overlays and editing restricted in app context. Source: dd-decaf.eu; GitHub caffeine monorepo historically existed; current availability limited.
- Escher demo/test repos: `escher-demo` and `escher-test` show embedding boilerplate and npm setups. Source: escher.readthedocs.io “Using the static JS/CSS files”.
- COBRApy fronts and FluxViz: FluxViz (by Escher authors) demonstrated flux overlays over Escher maps in web contexts; archival now. Source: GitHub `fluxviz` (historical reference, not actively maintained).
- BiGG Models: Historically hosted Escher maps on `escher.github.io`; current BiGG UI exposes maps via static hosting; public endpoints have varied; direct scraping returns 404 today, but Escher map indices are part of their site. Source: bigg.ucsd.edu (maps area; availability fluctuates).

Notes on versions:
- Docs site shows “Escher 1.8.2 documentation” and JavaScript API page labeled 1.8.2. GitHub latest release tag reported 1.7.3 for releases API, but README indicates modern toolchain (Vite, yarn). Pin 1.8.x documentation features.

Implementation sketch:
- Include `escher.js` and `escher.css`; instantiate:
  - `const builder = escher.Builder(null, null, null, d3.select(#container), { enable_search: false });`
  - Load map JSON via fetch and call `builder.load_map(map_json)`; load reaction_data separately to avoid giant payloads.

## B. Multi-model navigation prior art

Compare organization and drill-down:
- MetaNetX portal: Collection-level browsing by organism/taxonomy; normalized cross-references (MNXref) mapping metabolites/reactions across models; drill-down model → reactions/genes; cross-model IDs resolved via MNX IDs. Source: metanetx.org; MNXref papers.
- BiGG Models: Catalog of curated models with consistent BiGG identifiers; model page lists reactions/genes/metabolites; provides Escher pathway maps per model; drill-down is model → subsystems/pathways → reaction details. Source: bigg.ucsd.edu.
- ModelSEED web: Workspace of models per project; lists and compares models; pathway maps via KBase/Escher; drill-down: project → model → pathways/reactions/genes; IDs often standardized to ModelSEED IDs with alias tables. Source: modelseed.org, KBase Narrative.
- MEMOTE: Produces per-model HTML reports; not a multi-model navigator but supports diff reports and history; organization is per-report with links to metrics; no cross-model ID harmonization. Source: memote docs.
- Caffeine (DD-DeCaF): A multi-model design/analysis platform allowing users to manage several models and perform simulations; used consistent ID backbones and likely BiGG alignment; drill-down similar to project → model → pathway/reaction. Source: dd-decaf.eu.
- KBase Narrative: Project notebooks organize multiple models and analyses in a narrative; drill-down via app cells; identifiers resolved via service layers and alias mapping; not a dedicated browser but demonstrates multi-model management within a project. Source: narrative.kbase.us.

Identifier differences handling:
- MetaNetX: MNXref mappings across databases; translate per-model IDs to MNX IDs for cross-model comparison, while preserving original IDs.
- BiGG: Enforces BiGG namespace for curated models; cross-links synonyms; external DB links.
- ModelSEED: Maintains ModelSEED IDs plus alias mapping for external IDs; provides tools to map.
- Practice recommendation: Maintain per-model synonym tables attached to analyses; normalize to MNXref or BiGG for aggregation, but display original IDs with aliases. Provide per-reaction gene rules and crosswalks.

## C. Provenance/run indexes across research arcs

Tools and their “record envelope” vs payload:
- MLflow: Envelope is an “experiment → run” with run UUID, params, metrics, tags, artifacts; stored in backend (file store/SQL); payloads are arbitrary artifacts in directories. Source: mlflow.org docs.
- Weights & Biases: Envelope includes project, run id, config (params), metrics, tags; payloads are logged artifacts; centralized service backend. Source: docs.wandb.ai.
- DVC: Envelope is DVC YAML + git commits; pipelines tracked via `dvc.yaml`, `dvc.lock`; payloads are data artifacts; provenance anchored to Git; experiments as branches. Source: dvc.org.
- Snakemake reports: Generates a static HTML report with metadata on rules, inputs, outputs; the “envelope” is the report JSON and HTML; payloads are files referenced. Source: snakemake docs “report”.
- Nextflow Tower: Centralized run metadata; envelope includes pipeline, run id, params, trace; payloads are artifacts/logs produced by Nextflow; web service backend. Source: tower.nf.
- Galaxy histories: Envelope is a history with dataset metadata and provenance; payloads are datasets; captured via Galaxy’s object store and provenance tracking. Source: usegalaxy.org.
- RO-Crate: A directory-level metadata envelope using JSON-LD (`ro-crate-metadata.json`) describing the dataset (the directory) and contained files, workflows, and provenance; profiles for workflows exist; lightweight and designed for packaging research data and metadata. Source: researchobject.org/ro-crate.
- CWLProv: A profile for capturing provenance of CWL workflows; records activities, entities, and agents; can be embedded or referenced. Source: w3id.org/cwl/prov.

Fit for “analysis record stamped onto a research arc directory”:
- RO-Crate is a good fit: a crate per arc directory describing runs, inputs, outputs, and context; supports extensions (profiles) for workflows; interoperable; not overly heavy if using the minimal JSON-LD with references to files. Recommendation: adopt RO-Crate as the envelope; include run payloads as files; optionally include CWLProv when workflows are CWL-based.
- Avoid inventing a schema: define a minimal profile (project, arc id, run id, timestamps, tool, params, inputs, outputs, links) using RO-Crate terms and schema.org, with JSON-LD context.

## D. Append-only JSONL indexes (per-directory)

Failure modes:
- Concurrent writers: Without locking, lines interleave or overwrite; NP-hard to resolve; readers may see partial states.
- Partial line writes / crashes: Truncated line corrupts last JSON object; whole-file reads fail unless tolerant parser; requires fsync discipline and write-ahead temp file rename.
- Duplicate record ids: Append-only means updates add new versions; queries need last-writer-wins; storage bloat without compaction.
- Growth: Whole-file read scales poorly; O(n) start-up, GC pressure; tens of MB become slow; hundreds of MB unacceptable in browsers.

What real tools do instead:
- MLflow/W&B: Centralized backends (SQL/hosted) with transactional writes.
- DVC: Git-based logs and YAML lockfiles; dedup via hashes; no single JSONL per dir; use DAGs and git history.
- Many CLIs (e.g., Pip’s log, cargo) rotate logs, or use SQLite/LevelDB with WAL; some use journaling (append-only) plus compaction phases and file locks (`flock`, `fcntl`).
- At scale threshold: Beyond a few MB or thousands of records, switch to SQLite/duckdb or an indexed store. In browsers, use IndexedDB for client-side indexes.

Practical guidance:
- If keeping JSONL: use per-arc file locks, write to temp and append atomically (`O_APPEND`), include sequence numbers, and implement periodic compaction into a canonical JSON or SQLite snapshot.

## E. Fitness/essentiality agreement visualization

Published tools and metrics:
- MEMOTE: Supports essentiality experiment inputs and compares model predictions against experimental gene essentiality; reports scores; visuals are static bar/summary with per-test results; metrics include accuracy-like scores. Source: memote experimental_data and understanding_reports pages (v0.11.1 docs).
- KBase/ModelSEED pipelines: Some narratives include gene essentiality comparisons visualized as tables with pass/fail and potentially ROC-like summaries; not standardized; rely on app widgets.
- Tn-Seq/RB-TnSeq literature: Common metrics are confusion matrix counts (TP/FP/TN/FN), Matthews correlation coefficient (MCC), precision/recall, F1; visuals include heatmaps of pathways, per-gene volcano/MA-like plots, and pathway maps colored by agreement.
  - E.g., Price et al., RB-TnSeq datasets and tools show per-gene fitness with thresholds; agreement assessed via thresholds and MCC.
- FluxViz/Escher overlays: Flux overlays can categorize reactions/genes; extending to agreement classes enables coloring genes/reactions by agreement status on Escher maps.

Recommendation:
- Present a confusion matrix + summary metrics (MCC, precision, recall) per arc/model and an interactive per-gene table with filters.
- Overlay agreement classes on Escher maps (e.g., green = predicted essential and observed essential; red = disagreement) with tooltips showing fitness values and knockout prediction.

References (URLs and versions):
- Escher docs (1.8.2): https://escher.readthedocs.io/en/latest/ (JavaScript API, Development → using static JS/CSS)
- GitHub README (escher): https://github.com/zakandrewking/escher (README shows Vite/yarn toolchain and Python packaging)
- MEMOTE docs (0.11.1): https://memote.readthedocs.io/en/latest/
- DD-DeCaF Caffeine: https://www.dd-decaf.eu/
- BiGG Models main site: https://bigg.ucsd.edu/ (maps availability varies)
- ModelSEED: https://modelseed.org/
- KBase Narrative: https://narrative.kbase.us/
- MetaNetX: https://metanetx.org/ (MNXref concept for cross-model ID mapping)
- MLflow docs: https://mlflow.org/docs/latest/
- Weights & Biases docs: https://docs.wandb.ai/
- DVC docs: https://dvc.org/doc
- Snakemake reports: https://snakemake.readthedocs.io/en/stable/snakefiles/report.html
- Nextflow Tower: https://tower.nf/
- Galaxy project: https://usegalaxy.org/
- RO-Crate: https://www.researchobject.org/ro-crate/
- CWL provenance profile: https://w3id.org/cwl/prov

Gaps / “nothing found” notes:
- Concrete, modern examples of third-party web apps embedding Escher 1.8.x outside notebooks are sparse; most references are legacy demos or internal to projects like BiGG and KBase.
- Public APIs serving Escher maps (BiGG) are unstable or 404 at time of scan; plan for local hosting of map JSONs.

Build vs buy warning:
- No existing open-source tool provides the exact PROJECT → ARC → multi-model drill-down with Escher overlays and fitness agreement as described. Pieces exist (MEMOTE for per-model QA, KBase for narratives, MetaNetX for ID harmonization). Building a focused app is justified; adopt RO-Crate and reuse Escher, MNXref/BiGG IDs, and standard metrics to avoid bespoke reinvention.
"EOF
