## STALL REPORT (the binding part)

1) PHASE / MODULE: Shared ArcIndex dependency (Phase 0 precondition)
- DECISION I CANNOT MAKE: The PRD says this app depends on kbutillib.arc_index and must not create it, and that nothing can be dispatched until it exists on main.
- WHY IT STALLS ME: App and producers import ArcIndex (resolve_runs_root, list_projects, record_analysis). If src/kbutillib/arc_index.py is absent, imports fail. A stub risks divergence from the sibling PRD.
- RECOMMENDED RESOLUTION: Do not dispatch until src/kbutillib/arc_index.py exists on main with the stated interface; otherwise abort the build.

2) PHASE / MODULE: Producer stamping — stable record_id
- DECISION I CANNOT MAKE: Record_id is producer-generated and stable, derived from kind, subject, and significant parameters.
- WHY IT STALLS ME: Without a canonical algorithm, idempotency and deduplication are undefined; producers may collide or drift.
- RECOMMENDED RESOLUTION: Define record_id = sha256 of lowercase(kind), lowercase(subject), and a canonical JSON of significant parameters (sorted keys), hex-encoded; optionally display-truncated to 32 chars.

3) PHASE / MODULE: Artifacts schema per kind
- DECISION I CANNOT MAKE: Artifacts are named refs, never inlined, but exact key names and URI schemes per kind are unspecified.
- WHY IT STALLS ME: The app cannot resolve or generate dashboards without knowing whether refs are file paths or object-store URIs, and the exact key names (for example model_path, flux_path).
- RECOMMENDED RESOLUTION: Specify exact artifacts keys and URI formats per kind. Example: kbutillib.fba uses model_path and flux_path with absolute file paths. kbdl.fitness_analysis uses fitness_sim_reactions and fitness_sim_genes with explicit table names. Allowed schemes: bare path or file:// for files; obj://<object_id> for the KBDL store.

4) PHASE / MODULE: Level 1 Model rows — identity and grouping
- DECISION I CANNOT MAKE: What is the canonical model_id across producers and how analyses group into one row.
- WHY IT STALLS ME: Without a consistent model_id, the app duplicates or mis-merges models, breaking counts and drill-down.
- RECOMMENDED RESOLUTION: Define artifacts.model_id = model:<ns>:<id> with ns in {kbdl, file}. For kbdl.model_build, id is the KBDL object id. For kbutillib verbs, id is the absolute normalized cobra-JSON path. Group strictly by artifacts.model_id.

5) PHASE / MODULE: GET /api/models/{record_id}/dashboard
- DECISION I CANNOT MAKE: Which record {record_id} refers to; create_fitness_dashboard needs both a model reconstruction and a fitness analysis.
- WHY IT STALLS ME: Without a rule, the server may pick the wrong counterpart or fail nondeterministically.
- RECOMMENDED RESOLUTION: {record_id} must reference a kbdl.fitness_analysis record. The paired model is the latest kbdl.model_build with the same artifacts.model_id in the same arc. If absent, return 404 with error model-not-found.

6) PHASE / MODULE: Default Escher map selection
- DECISION I CANNOT MAKE: The default map when multiple exist or none found.
- WHY IT STALLS ME: Non-deterministic defaults harm testability; missing-map behavior unclear.
- RECOMMENDED RESOLUTION: Default order: modelseed_core, then modelseed_global, else first from list_available_maps(model). If none, return 404 with error no-map.

7) PHASE / MODULE: Cache directory for generated HTML
- DECISION I CANNOT MAKE: Cache location is not a concrete path.
- WHY IT STALLS ME: Pod versus laptop writable roots differ; tests need a stable location.
- RECOMMENDED RESOLUTION: Cache under KING_STATE/kind-apps/state/kbu-models-app when KING_STATE is set; otherwise under HOME/kind-apps/state/kbu-models-app; fallback to a per-user state dir such as platformdirs user_state_dir kbutillib/kbu-models-app.

8) PHASE / MODULE: Arc slug uniqueness scope
- DECISION I CANNOT MAKE: Whether arc slugs are globally unique or per project.
- WHY IT STALLS ME: --arc SLUG may ambiguously match multiple projects, risking mis-indexing.
- RECOMMENDED RESOLUTION: Slugs are unique per project. The --arc flag accepts PROJECT/SLUG. If only SLUG is provided and ambiguous, refuse to stamp and print a disambiguation error.

9) PHASE / MODULE: Backfill scan strategy and store reachability
- DECISION I CANNOT MAKE: Exact filesystem patterns to walk and precedence between file artifacts and object-store references; pod store reachability unknown.
- WHY IT STALLS ME: Backfill could miss or over-scan; naive store calls may fail on the pod.
- RECOMMENDED RESOLUTION: Scan <runs_root>/*/arcs/*/ for known artifacts like *.model.json, fba/*.json, fva/*.json. When the KBDL object store is reachable, additionally list by object_type in {model, fba, fva} filtered by owner. Prefer file artifacts; use store refs only when files are absent.

10) PHASE / MODULE: Replacement semantics over append-only JSONL
- DECISION I CANNOT MAKE: Whether replace rewrites the JSONL or is a read-time view over an append-only log.
- WHY IT STALLS ME: Physical rewrites break append-only guarantees and invite conflicts; read-time dedup must be defined for consistent counts.
- RECOMMENDED RESOLUTION: record_analysis is strictly append-only. list_analyses(latest_only=True) returns the last entry per record_id by scanning from the end; no rewrites.

11) PHASE / MODULE: KIND manifest install path(s)
- DECISION I CANNOT MAKE: The exact target on the pod where KING_STATE differs; union symlink farm required.
- WHY IT STALLS ME: Hardcoding HOME risks shadowing shipped plugins or writing to the wrong place.
- RECOMMENDED RESOLUTION: kbu kind install writes the manifest under KING_STATE/kind-apps/plugins when KING_STATE is set, otherwise under HOME/kind-apps/plugins, and refreshes the union symlink farm. Never write to king/plugins/.

## FREE CRITIQUE (non-binding)

- Data reachability risk: If the runs tree is not reachable on the pod, the design must pivot to a central index; verify early.
- Vocabulary drift: fitness_dashboard.py includes aliases like functional and variable. Avoid hard-coding a subset in the app.
- Escher inlining tradeoff: inline_escher=False reduces cache size but adds runtime network dependency; air-gapped pods may blank. Consider a toggle.
- Permissions: Surface explicit errors when artifacts are unreadable to avoid silent empty dashboards.
- Shared index: Mixed kind namespaces in one JSONL require strict prefix filtering; missing filters should be test failures.
- Git ignore mutation: Auto-adding .analyses/ to .gitignore is surprising; emit a one-time notice to users.
- Provider: Revisit a type:provider entry once counts have clear semantics.
- Tests: Use a fake ArcIndex fixture for app tests to decouple from pod or laptop path differences.
- Subject display: Normalize and display genome or model names rather than opaque ids in tables.
