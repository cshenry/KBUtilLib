## STALL REPORT (the binding part)

1. PHASE / MODULE: Shared ArcIndex dependency and interface
- DECISION I CANNOT MAKE: External ArcIndex module promised but not present here
- WHY IT STALLS ME: Cannot import or test required API without guessing
- RECOMMENDED RESOLUTION: Publish module path and exact signatures with fixtures

2. PHASE / MODULE: KBUtilLib CLI producer stamping
- DECISION I CANNOT MAKE: Add --arc and stamping to kbu model without source
- WHY IT STALLS ME: This repo lacks the CLI code to modify safely
- RECOMMENDED RESOLUTION: Name owning repo and integration point, define record_id derivations

3. PHASE / MODULE: KBDL JobContext and stamping
- DECISION I CANNOT MAKE: Add project and arc to JobContext and hook stamping
- WHY IT STALLS ME: KBDL is external and not available to change here
- RECOMMENDED RESOLUTION: Provide exact file paths and parameter names in KBDL

4. PHASE / MODULE: CAC manifest registration and version gate
- DECISION I CANNOT MAKE: Implement gate against KING version without local loader
- WHY IT STALLS ME: No KING present to validate manifest and gating logic
- RECOMMENDED RESOLUTION: Include manifest example and local validation harness

5. PHASE / MODULE: Delegation to EscherUtils builders
- DECISION I CANNOT MAKE: Call create_fitness_dashboard or create_map_html2 here
- WHY IT STALLS ME: Functions not present, inputs and outputs unspecified locally
- RECOMMENDED RESOLUTION: Document exact signatures and required artifacts

6. PHASE / MODULE: Trust tiers payload schema
- DECISION I CANNOT MAKE: Exact payload key names per kind are unspecified
- WHY IT STALLS ME: Risk of schema drift across producers
- RECOMMENDED RESOLUTION: Publish normative payload schemas and a validator

7. PHASE / MODULE: Arc resolution behavior
- DECISION I CANNOT MAKE: Implement resolve_current_arc without ArcIndex behavior
- WHY IT STALLS ME: Runs root precedence and layouts are external
- RECOMMENDED RESOLUTION: Document precedence and examples or ship ArcIndex

8. PHASE / MODULE: Backfill command
- DECISION I CANNOT MAKE: Discovery rules and record_id derivations unspecified
- WHY IT STALLS ME: Cannot write a correct walker without external repos
- RECOMMENDED RESOLUTION: Define globs and provide sample fixtures

9. PHASE / MODULE: Pod data reachability
- DECISION I CANNOT MAKE: Confirm runs and object store access on pod
- WHY IT STALLS ME: Architecture choices depend on this assumption
- RECOMMENDED RESOLUTION: Document confirmed access and fallbacks

10. PHASE / MODULE: Git ignore policy for arcs
- DECISION I CANNOT MAKE: Auto edit .gitignore without explicit policy
- WHY IT STALLS ME: Risk of surprising users and conflicts
- RECOMMENDED RESOLUTION: State exact .gitignore line and messaging policy

11. PHASE / MODULE: App identity and packaging
- DECISION I CANNOT MAKE: Register models-and-analyses entry point here
- WHY IT STALLS ME: No app scaffold or pyproject entry present
- RECOMMENDED RESOLUTION: Provide owning repo or add minimal scaffold

12. PHASE / MODULE: API response schemas and polling protocol
- DECISION I CANNOT MAKE: JSON shapes and polling details unspecified
- WHY IT STALLS ME: Cannot implement or test clients without schemas
- RECOMMENDED RESOLUTION: Publish response schemas and poll protocol

13. PHASE / MODULE: Tests for I1 I4 I5
- DECISION I CANNOT MAKE: KING absent serve test and gates without harness
- WHY IT STALLS ME: No stubs to assert behavior
- RECOMMENDED RESOLUTION: Include test scaffolding and version stub

14. PHASE / MODULE: Arc level MCC metric
- DECISION I CANNOT MAKE: Mapping from concordance to confusion matrix unspecified
- WHY IT STALLS ME: Risk of inconsistent metric computation
- RECOMMENDED RESOLUTION: Publish mapping and formula with a worked example

## FREE CRITIQUE (non-binding)

- Pin minimal versions for KING KBDL and KBUtilLib to avoid drift
- Ship JSON Schemas and a validator to govern payloads per kind
- Plan migration from JSONL to SQLite or duckdb beyond small scale
- Generated HTML limits cross model interactions add future live table
- Auto editing .gitignore should be user configurable and well logged
- Surface unresolved arc warnings beyond stderr in central logs and UI
- Provide a local manifest loader stub to test CAC without KING
- Add tier legend and tooltips and keep propagated vs measured distinct
- Include a backfill coverage report and missing artifact list
- Persist map selection per model and make it shareable
- Avoid per user filters until identity handoff is specified
