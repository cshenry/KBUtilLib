## STALL REPORT (the binding part)

1. PHASE / MODULE: p0a — Runs-root resolver and enumeration
   - DECISION I CANNOT MAKE: The shape of `ProjectRecord` and `ArcRecord` is not defined. PRD text: "class KorosArcStore: list_projects() -> list[ProjectRecord]; list_arcs(project: str) -> list[ArcRecord]; read_arc(project: str, arc: str) -> ArcRecord" and "PROVENANCE.json parses into a typed record carrying ... [fields listed] ... An absent or unparseable PROVENANCE.json yields a record marked invalid with a reason".
   - WHY IT STALLS ME: I cannot implement return types, field names, or invalid-marking without guessing. Guessing wrong breaks both consumers’ compile-time and test contracts and corrupts data interchange.
   - RECOMMENDED RESOLUTION: Add explicit dataclass definitions: `ProjectRecord = {name: str, path: str, arc_count: int}`; `ArcRecord = {project: str, arc: str, path: str, provenance: {...exact field list...}, is_valid: bool, invalid_reason: str | None}`.

2. PHASE / MODULE: p0a — Runs-root resolver construction/config
   - DECISION I CANNOT MAKE: Where the explicit `--runs-root` argument is supplied. PRD text: "1. an explicit --runs-root argument" but the interface does not expose resolver configuration or constructor parameters.
   - WHY IT STALLS ME: I must decide between a module-level function, a `KorosArcStore` constructor param, or environment-only behaviour. The choice affects app wiring and tests; a wrong assumption breaks CLI and pod usage.
   - RECOMMENDED RESOLUTION: State: `KorosArcStore(runs_root: str | None = None)` accepts an explicit path; when `None`, resolution follows the chain: `$KOROS_HOME/runs` then `$KING_KOROS_RUNS` then raise.

3. PHASE / MODULE: p0a — Arc provenance parsing
   - DECISION I CANNOT MAKE: Exact field mapping and optionality for the listed PROVENANCE fields (e.g., `fair_inputs_ref`, `offlimits_list_ref`, `frame_novelty`, `compute_targets`).
   - WHY IT STALLS ME: Without a schema, I cannot decide which keys are required, which are optional, and how to type them; a wrong choice will mark valid arcs invalid or vice versa.
   - RECOMMENDED RESOLUTION: Enumerate the PROVENANCE schema with per-key types and optionality. Example: `created_at: str (ISO8601, required)`, `inputs: list (required, may be empty)`, `tool_versions: dict (required, may be empty)`, others optional; any missing optional key yields `None`.

4. PHASE / MODULE: p0b — Store schema vs. record contract
   - DECISION I CANNOT MAKE: `AnalysisRecord` includes `payload`, but the DDL omits any `payload` column and instead defines a separate `subject_detail` blob table. PRD text: "AnalysisRecord carries ... `payload` (the seam -- opaque here, never interpreted)" and DDL lacks `payload`.
   - WHY IT STALLS ME: I cannot store or retrieve `payload` without guessing whether it belongs in `runs.payload` or exclusively in `subject_detail`. The API and storage boundary are inconsistent; guessing corrupts the blob boundary or drops data.
   - RECOMMENDED RESOLUTION: Specify: `runs` includes a `payload TEXT NULL` column for lightweight summary payloads; heavy subject detail lives in `subject_detail.detail_json`. `record_analysis` accepts an optional `detail: dict | None` argument; when provided, it writes to `subject_detail`.

5. PHASE / MODULE: p0b — Unknown kind detection
   - DECISION I CANNOT MAKE: What list or rule defines "unrecognised kind" to set `unknown_kind = 1`. PRD text: "An unrecognised `kind` sets `unknown_kind = 1`".
   - WHY IT STALLS ME: With no registry or enumeration of known kinds, every kind is either "unknown" or "known" depending on an unstated rule. Guessing gives false badges and miscounts.
   - RECOMMENDED RESOLUTION: Declare: Known kinds are those matching `^kbutillib\.[a-z0-9_.]+$|^kbdl\.[a-z0-9_.]+$` and listed in `KNOWN_KINDS` config; default is an empty set (treat all as unknown) until apps register kinds via `KorosArcStore.register_kind(kind: str)`.

6. PHASE / MODULE: p0b — Replace-not-duplicate semantics
   - DECISION I CANNOT MAKE: When replacing a row by `record_id`, which fields are overwritten and how `created_at` vs `updated_at` are handled.
   - WHY IT STALLS ME: I might inadvertently reset `created_at` or fail to update versioned columns. Wrong write semantics break auditability and tests across apps.
   - RECOMMENDED RESOLUTION: State: "On upsert by `record_id`, overwrite all mutable fields (`status`, `producer*`, `trust_tier`, `artifacts`, `payload`, `provenance`, version-stamp columns), preserve original `created_at`, and set `updated_at` to now (UTC)."

7. PHASE / MODULE: p0b — Detail blob API mismatch
   - DECISION I CANNOT MAKE: How clients pass the subject-level blob to persist in `subject_detail` given `record_analysis(project, arc, record: AnalysisRecord) -> None` has no `detail` parameter.
   - WHY IT STALLS ME: Without a parameter I cannot populate the blob table; inventing a field in `AnalysisRecord` changes the agreed contract.
   - RECOMMENDED RESOLUTION: Amend interface: `record_analysis(project, arc, record: AnalysisRecord, detail: dict | None = None) -> None`.

8. PHASE / MODULE: p0b — Allowed `artifacts` structure
   - DECISION I CANNOT MAKE: Whether `artifacts` is a list of strings or a list of `{name, uri}` pairs; PRD says "named refs, never inlined data" but does not define the shape.
   - WHY IT STALLS ME: I cannot validate or store artifacts consistently; wrong choice breaks consumers’ expectations and URI validation.
   - RECOMMENDED RESOLUTION: Define: `artifacts: list[{name: str, uri: str}]`, with `uri` restricted to absolute path, `file://` and `obj://<object_id>`.

9. PHASE / MODULE: p0b — Status vocabulary
   - DECISION I CANNOT MAKE: The exact permitted `status` values and case. PRD text: "`status` (`ok|failed|partial`)" but not whether case-insensitive or extendable.
   - WHY IT STALLS ME: Validation must either enforce or permit variants; guessing risks rejecting valid writes or accepting nonsense.
   - RECOMMENDED RESOLUTION: Declare: `status` is one of `{"ok", "failed", "partial"}`; case-insensitive on write, stored lower-case.

10. PHASE / MODULE: p0b — DB path creation
    - DECISION I CANNOT MAKE: Whether the store should create `~/.kbdl/` if absent and what to do on permission errors beyond "log and return".
    - WHY IT STALLS ME: Implementing fail-soft requires a concrete create/check policy; guessing may write to unintended locations or raise.
    - RECOMMENDED RESOLUTION: Specify: Create `~/.kbdl/` if missing; on any write error, log a warning with `logging` and return; reads still raise on misconfiguration per user story 11.

11. PHASE / MODULE: p0c — Contract-version gating
    - DECISION I CANNOT MAKE: The PRD requires major/minor gating but also states "`contract_version` is an INTEGER, currently `1`" (not semver). Contradiction: integers have no minor.
    - WHY IT STALLS ME: I cannot implement compatible-minor warnings with integer-only versions; any choice contradicts the text or breaks interop.
    - RECOMMENDED RESOLUTION: Choose one: (A) "`contract_version` is `MAJOR.MINOR` (string), with hard-fail on differing MAJOR, warn on MINOR > supported"; or (B) "`contract_version` is an integer MAJOR only; equal required, else hard-fail; no minor notion". Pick (B) for simplicity consistent with CAC note.

12. PHASE / MODULE: p0c — Id normalisation scope
    - DECISION I CANNOT MAKE: Which identifiers are normalised with hyphen/underscore (record kinds? app ids? module names?).
    - WHY IT STALLS ME: Applying normalisation to the wrong fields mutates data; applying to too few fails CAC conformance in consumers.
    - RECOMMENDED RESOLUTION: Specify: Normalise `kind` and any `entity_id` fields in records; canonical hyphen form stored; underscore form exposed via helper for Python/SQL identifiers.

13. PHASE / MODULE: p0d — Contract suite parameters and fixtures
    - DECISION I CANNOT MAKE: Test framework, parametrisation mechanism, and exact fixtures content under `tests/fixtures/koros_arc_store/`.
    - WHY IT STALLS ME: I cannot write the suite or fixtures without guessing structure and file names; wrong choices break CI and cross-repo tests.
    - RECOMMENDED RESOLUTION: State: Use `pytest`; place fixtures at `tests/fixtures/koros_arc_store/runs_tree/` and `tests/fixtures/koros_arc_store/subject_blobs/`; provide sample `PROVENANCE.json` with empty `inputs` and `tool_versions` and at least one arcs-less project.

14. PHASE / MODULE: p0d — Fake store behaviour parity
    - DECISION I CANNOT MAKE: Whether the fake should persist to an in-memory SQLite (to exercise DDL) or pure Python dicts while enforcing validations.
    - WHY IT STALLS ME: The choice affects test coverage of the blob boundary and upsert behaviour; diverging from real-store semantics undermines the contract.
    - RECOMMENDED RESOLUTION: Declare: `FakeKorosArcStore` uses pure in-memory dicts but enforces the same validation rules; contract suite separately runs full DDL tests against the real store.

## FREE CRITIQUE (non-binding)

- The DB column set includes summary stats (`subject_feature_count`, `method_count`, `consistency_overall`, `consistency_metric_version`, `ic_corpus_version`) that are not present in `AnalysisRecord`; document how producers populate them or move them to a separate computed view.
- Consider explicitly versioning the `provenance` schema to avoid silent evolution; a `provenance_version` column would help.
- The artifact URI vocabulary will need OS-path clarity (`file:///` vs `file://` semantics), and object IDs likely need a namespace to avoid collisions across stores.
- Runs-root resolution should also permit a `PathLike` and clearly document raising exception type (e.g., `KorosConfigError`) for misconfiguration.
- Enumerating projects/arcs should define slug rules (lowercase, hyphenated) to prevent case drift across filesystems.
- The "fails soft on write" rule is correct but risks silent data loss; recommend an opt-in telemetry hook so operators can detect skipped writes.
- Unknown-kind policy is valuable; consider a registry API in this module to declare known kinds, making the badge meaningful over time.
- The "blob boundary test" deserves a hard performance budget (e.g., O(1) blobs opened per `list_analyses`) so regressions are measurable.
