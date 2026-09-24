
## STALL REPORT (the binding part)

1. PHASE / MODULE: Runs-root resolution (p0a: resolve_runs_root)
   - DECISION I CANNOT MAKE: "1. an explicit --runs-root argument; 2. $KOROS_HOME/runs; 3. $KING_KOROS_RUNS; 4. RAISE" plus "Misconfiguration raises a named error type rather than a bare ValueError".
   - WHY IT STALLS ME: The PRD does not specify the exact error class name. Without a concrete exception type, downstream apps cannot reliably catch/configure error handling; guessing risks mismatched error handling in consumers.
   - RECOMMENDED RESOLUTION: "Define a concrete exception class name: kbutillib.koros_arc_store.RunsRootResolutionError raised on unresolved runs-root."

2. PHASE / MODULE: Runs-tree enumeration (p0a: list_projects/list_arcs/read_arc)
   - DECISION I CANNOT MAKE: "PROVENANCE.json parses into a typed record carrying the verified live fields... Keys not in the listed set are preserved verbatim in a raw dict" and "An absent or unparseable PROVENANCE.json yields a record marked invalid with a reason".
   - WHY IT STALLS ME: The exact schema of ArcProvenance (field names, types) and the explicit set of allowed keys is implied but not precisely enumerated in a machine-implementable way; also no exact invalid_reason values or codes are specified.
   - RECOMMENDED RESOLUTION: "Specify ArcProvenance dataclass with exact fields and types: run_id:str, run_name:Optional[str], created_at:str(ISO8601 UTC), created_by:Optional[str], init_provenance:Optional[dict], fair_inputs_ref:Optional[str], offlimits_list_ref:Optional[str], non_overlap_statement:Optional[str], frame_novelty:Optional[str], inputs:list, tool_versions:dict, compute_targets:list, trace_file:Optional[str], trace_format:Optional[str], project:Optional[str], role:Optional[str], leg_of:Optional[str], parent:Optional[str], raw:dict; when invalid set valid=False and invalid_reason to a stable code string, e.g. 'missing_run_id' or 'missing_created_at' or 'json_parse_error'."

3. PHASE / MODULE: KorosArcStore interface types (p0b/p0c)
   - DECISION I CANNOT MAKE: "AnalysisRecord carries ... plus optional producer-supplied summary fields." The PRD enumerates many fields but does not freeze the exact structure of 'artifacts' and 'provenance' beyond prose.
   - WHY IT STALLS ME: Implementing serialization and validation needs exact types for each field. Ambiguity (e.g., producer_version is separate from producer? artifacts mapping vs list) risks incompatible implementations and broken interop.
   - RECOMMENDED RESOLUTION: "Freeze AnalysisRecord as a dataclass with exact types: record_id:str, analysis_id:str, run_uid:str, kind:str, created_at:str(ISO8601 UTC), producer:str, producer_version:str, subject:str, status:Literal['ok','failed','partial'], artifacts:dict[str,str], payload:Optional[str], trust_tier:Literal['verified','homology','hypothesis','opinion'], provenance:dict{bridge_kind:str, metric:{name:str,value:float|int|str,units:str}, source:{db:str, accession:str}}, contract_version:int, subject_feature_count:Optional[int], method_count:Optional[int], consistency_overall:Optional[float], consistency_metric_version:Optional[str], ic_corpus_version:Optional[str]."

4. PHASE / MODULE: Identity helpers (p0b)
   - DECISION I CANNOT MAKE: "canonical_json(significant_params)" is caller-supplied per kind, but no canonicalization specifics beyond 'sort keys and compact separators' are given; encoding, allowed value types, and NUL character choice are given but not exact.
   - WHY IT STALLS ME: To ensure cross-producer identity stability, the canonicalization function must be specified exactly (e.g., float formatting, Unicode normalization). Guessing risks diverging hashes and broken idempotency.
   - RECOMMENDED RESOLUTION: "Define canonical_json precisely: JSON RFC 8259, UTF-8 encoding, ensure dict keys sorted lexicographically, separators=(',',':'), no whitespace, numeric formatting per Python json module default, strings normalized to NFC, booleans and null per standard; NUL is '\x00'."

5. PHASE / MODULE: Artifacts URI validation (p0b)
   - DECISION I CANNOT MAKE: "Exactly three forms" but normalization details for 'file://', absolute path handling across OS, and 'obj://' schema specifics aren't fully specified (e.g., must object_id be hex? length?).
   - WHY IT STALLS ME: Implementing validation without precise regex risks rejecting valid URIs or accepting malformed ones.
   - RECOMMENDED RESOLUTION: "Specify regexes: absolute path: ^/[^\n]*$; file://: ^file:///[^\n]*$ (normalize 'file:///' and 'file://'); obj://: ^obj://[A-Za-z0-9._-]+$; normalize file URIs to 'file://' + absolute path."

6. PHASE / MODULE: Contract-version gating (p0c CAC helpers)
   - DECISION I CANNOT MAKE: The PRD decides integer-only gating with hard-fail on any difference, but does not specify the exact exception class, message format, or where gating is applied (write vs read vs both).
   - WHY IT STALLS ME: Consumers need predictable failure semantics; ambiguity could lead to gating at inconsistent points (e.g., during record_analysis vs list_analyses).
   - RECOMMENDED RESOLUTION: "Apply gating on record_analysis only; raise ContractVersionMismatch(contract_expected:int, contract_found:int) with message 'contract_version mismatch: expected X, found Y'."

7. PHASE / MODULE: Fail-soft write logging and counters (p0b store write path)
   - DECISION I CANNOT MAKE: "named logger with a stable message prefix" and "increments an in-process counter the caller can read" are not concretely named.
   - WHY IT STALLS ME: Without names, tests and consumers cannot assert or observe behaviour; guessing risks divergent instrumentation.
   - RECOMMENDED RESOLUTION: "Use logger name 'kbutillib.koros_arc_store'; prefix 'koros_arc_store_write_soft_fail:'; expose counter via KorosArcStore.failed_write_count property."

8. PHASE / MODULE: Database path resolution and creation (p0b)
   - DECISION I CANNOT MAKE: "Engine: SQLite, path resolved from KBDL_RUN_DB else ~/.kbdl/runs.sqlite. The store creates the parent directory." No exact environment variable precedence across constructor vs module-level resolution, nor permissions errors handling details.
   - WHY IT STALLS ME: Implementing predictable behavior requires an exact resolution order and behavior when env var points to a non-writable path.
   - RECOMMENDED RESOLUTION: "KorosArcStore(db_path=None) resolves as: if db_path passed, use it; else if env KBDL_RUN_DB set and non-empty, use it; else ~/.kbdl/runs.sqlite. On directory creation failure, log the same soft-fail and set failed_write_count; reads still raise if DB open fails."

9. PHASE / MODULE: Known kinds set (p0b unknown kind policy)
   - DECISION I CANNOT MAKE: "If caller passes known_kinds" but not how it's supplied (constructor vs per-call) and whether updates at runtime are supported.
   - WHY IT STALLS ME: Inconsistent application could lead to different unknown_kind flags.
   - RECOMMENDED RESOLUTION: "known_kinds: Optional[set[str]] passed in constructor, immutable for store lifetime; validation checks membership at record_analysis time only."

10. PHASE / MODULE: Delete semantics (p0b delete_record)
    - DECISION I CANNOT MAKE: "cascading to its subject_detail blob" but DDL does not specify foreign key constraints or PRAGMA; exact transaction behavior and isolation level unspecified.
    - WHY IT STALLS ME: Implementing deletion consistency without FK may require manual deletes; guessing isolation risks partial deletes under concurrent writes.
    - RECOMMENDED RESOLUTION: "Perform delete in a transaction: BEGIN IMMEDIATE; DELETE FROM subject_detail WHERE record_id=?; DELETE FROM runs WHERE record_id=?; COMMIT; return True if runs row count >0. No SQLite FK pragmas required."

11. PHASE / MODULE: latest_only behavior (p0b list_analyses)
    - DECISION I CANNOT MAKE: "latest_only returns exactly one row per analysis_id newest by created_at" but tie-breaker on equal created_at is unspecified and timezone assumptions implicit.
    - WHY IT STALLS ME: Two runs with identical created_at could lead to nondeterminism.
    - RECOMMENDED RESOLUTION: "Tie-break by updated_at DESC then record_id DESC; created_at stored as ISO8601 UTC strings."

12. PHASE / MODULE: Provenance metric types (p0b AnalysisRecord)
    - DECISION I CANNOT MAKE: Metric value type can be float, int, or string; units unspecified; source fields optionality unclear.
    - WHY IT STALLS ME: Validation requires exact typing; ambiguity causes cross-app incompatibility.
    - RECOMMENDED RESOLUTION: "Metric: name:str required; value:Union[int,float,str]; units:Optional[str]; source: Optional[dict{db:str, accession:str}]. Require provenance non-empty for non-verified tier."

13. PHASE / MODULE: Test suite parameterization (p0d contract suite)
    - DECISION I CANNOT MAKE: "assertions not-applicable for the fake marked explicitly" but not the marker string or mechanism.
    - WHY IT STALLS ME: Tests need a concrete way to mark not-applicable vs skipped.
    - RECOMMENDED RESOLUTION: "Use pytest marks: @pytest.mark.not_applicable_for_fake on tests that apply only to real store; in parametrization, skip when implementation=='fake' with reason 'not_applicable_for_fake'."

14. PHASE / MODULE: Arc slug lookup (p0a read_arc)
    - DECISION I CANNOT MAKE: "lookup is exact" but whether slug matching is case-sensitive on all platforms; behavior when multiple arcs differ only by case.
    - WHY IT STALLS ME: On case-insensitive filesystems (macOS default), ambiguity exists.
    - RECOMMENDED RESOLUTION: "Define behavior independent of filesystem: treat slug as case-sensitive string; if underlying FS collapses case, enumeration returns whatever actual directory names exist; no synthetic normalization applied."

15. PHASE / MODULE: Record payload size limits (p0b runs.payload)
    - DECISION I CANNOT MAKE: "payload is small" but no explicit size limit provided.
    - WHY IT STALLS ME: Without limits, producers may store large blobs in runs table, harming performance.
    - RECOMMENDED RESOLUTION: "Enforce payload length <= 64KB; reject larger with validation error 'payload_too_large'."

16. PHASE / MODULE: Subject detail blob constraints (p0b subject_detail)
    - DECISION I CANNOT MAKE: No maximum size or compression guidance.
    - WHY IT STALLS ME: Unbounded blobs can grow DB excessively; migration guidance needed.
    - RECOMMENDED RESOLUTION: "Accept detail blobs up to 50MB; consider gzip-compressing JSON before storage; producers exceeding limit must store externally and reference via artifacts instead."

17. PHASE / MODULE: Status transition semantics (p0b upsert)
    - DECISION I CANNOT MAKE: Whether replacing a record_id can change status arbitrarily, and if partial->ok is allowed without provenance changes.
    - WHY IT STALLS ME: Validation may need to prevent inconsistent transitions.
    - RECOMMENDED RESOLUTION: "Allow any status transition on retry upsert; validation applies per-write (tier/provenance rules)."

18. PHASE / MODULE: Timestamp generation (p0b created_at/updated_at)
    - DECISION I CANNOT MAKE: "created_at preserved; updated_at set to now" but exact format and timezone not pinned beyond 'ISO8601 UTC'.
    - WHY IT STALLS ME: Implementations may diverge on fractional seconds or 'Z' suffix.
    - RECOMMENDED RESOLUTION: "Use RFC 3339 format 'YYYY-MM-DDTHH:MM:SSZ' without fractional seconds; UTC only; store as text."

19. PHASE / MODULE: Enumeration of projects with zero arcs (p0a)
    - DECISION I CANNOT MAKE: No explicit return shape for ProjectRecord.arc_count when arcs directory missing vs present but empty.
    - WHY IT STALLS ME: Edge-case handling may differ.
    - RECOMMENDED RESOLUTION: "arc_count is 0 in both cases; ProjectRecord.path points to project directory; absence of arcs subdirectory is not an error."

20. PHASE / MODULE: list_analyses filters precedence (p0b)
    - DECISION I CANNOT MAKE: When both kind and analysis_id filters are supplied with latest_only, exact SQL WHERE/ORDER semantics not pinned.
    - WHY IT STALLS ME: Ambiguity can lead to inconsistent results across implementations.
    - RECOMMENDED RESOLUTION: "Apply WHERE kind= and analysis_id= conjunctively; latest_only applies after filtering; ORDER BY created_at DESC, updated_at DESC, record_id DESC; return one per analysis_id."

21. PHASE / MODULE: Subject identifier normalization (p0b identity)
    - DECISION I CANNOT MAKE: 'subject' lowercased in hash derivation is specified, but whether the stored subject should preserve original case is unclear.
    - WHY IT STALLS ME: Consumers may expect original subject formatting.
    - RECOMMENDED RESOLUTION: "Store subject verbatim; use lower(subject) only inside hash derivation; do not mutate the stored subject."

22. PHASE / MODULE: SQLite pragmas and performance (p0b)
    - DECISION I CANNOT MAKE: No guidance on WAL mode, synchronous, and index creation timing.
    - WHY IT STALLS ME: Default SQLite settings may harm concurrent write-read behavior.
    - RECOMMENDED RESOLUTION: "Enable WAL mode; PRAGMA journal_mode=WAL; ensure index on (analysis_id, created_at DESC) exists on initialization."

23. PHASE / MODULE: Error messages/codes for validation failures (p0b)
    - DECISION I CANNOT MAKE: Rejections (bad status, malformed kind, artifacts) do not specify error class or message.
    - WHY IT STALLS ME: Consumers need consistent messages for debugging and tests.
    - RECOMMENDED RESOLUTION: "Define ValidationError with code strings: 'bad_status', 'bad_kind', 'bad_provenance', 'bad_artifact_uri', 'payload_too_large'."

24. PHASE / MODULE: read_detail behavior for missing blob (p0b)
    - DECISION I CANNOT MAKE: Not specified whether reading detail for a record without a blob raises or returns empty.
    - WHY IT STALLS ME: UI expectations differ.
    - RECOMMENDED RESOLUTION: "read_detail(record_id) raises DetailNotFound(record_id) if no blob exists; UI may handle and display 'no detail available'."

25. PHASE / MODULE: list_projects/list_arcs ordering (p0a)
    - DECISION I CANNOT MAKE: The PRD does not specify sort order of projects/arcs (name, created_at, filesystem order).
    - WHY IT STALLS ME: UI stability depends on deterministic ordering.
    - RECOMMENDED RESOLUTION: "Sort projects alphabetically by name; sort arcs alphabetically by slug."

26. PHASE / MODULE: read_arc behavior for invalid provenance (p0a)
    - DECISION I CANNOT MAKE: "invalid with a reason" but not whether read_arc returns None, an ArcRecord with valid=False, or raises.
    - WHY IT STALLS ME: API contract ambiguity impacts downstream handling.
    - RECOMMENDED RESOLUTION: "read_arc returns ArcRecord with valid=False and invalid_reason; never raises for per-arc issues."

27. PHASE / MODULE: CAC id normalization scope (p0c)
    - DECISION I CANNOT MAKE: It states 'app ids only', but not the exact entry points where normalization is applied (manifest names, CLI tokens, module names).
    - WHY IT STALLS ME: Implementing helpers without scope definition risks misuse.
    - RECOMMENDED RESOLUTION: "Provide functions normalize_app_id_hyphen(id:str)->str and underscore_form(id:str)->str; use only for KIND app identifiers; do not expose any automatic normalization of kind or subject."

28. PHASE / MODULE: Testing fixtures minimum content (p0d)
    - DECISION I CANNOT MAKE: Fixture details are broadly described but not enumerated file names/paths.
    - WHY IT STALLS ME: Building tests requires concrete fixture shapes.
    - RECOMMENDED RESOLUTION: "Define fixture directory schema: tests/fixtures/koros_arc_store/runs_tree/{projectA/PROVENANCE.json, projectA/arcs/arc1/PROVENANCE.json, projectB/ (no arcs/), projectC/arcs/bad_arc/PROVENANCE.json (malformed)}; subject_blobs/{<record_id>.json}; runs_db/{prepopulated.sqlite optional}."

29. PHASE / MODULE: Unknown kind flagging with known_kinds provided (p0b)
    - DECISION I CANNOT MAKE: Whether unknown_kind=1 for malformed kinds should also set a separate 'malformed_kind' flag.
    - WHY IT STALLS ME: Consumers may want to distinguish malformed vs unknown-but-well-formed.
    - RECOMMENDED RESOLUTION: "Add column 'malformed_kind INTEGER NOT NULL DEFAULT 0' distinct from unknown_kind to differentiate."

30. PHASE / MODULE: Producer-version format (p0b)
    - DECISION I CANNOT MAKE: Version string format not specified (semver vs free-form).
    - WHY IT STALLS ME: Validation may reject valid non-semver versions.
    - RECOMMENDED RESOLUTION: "Treat producer_version as opaque string; no format validation."

## FREE CRITIQUE (non-binding)

- The PRD is unusually detailed and near-implementable; most blockers are about pinning exact types, error classes, and ordering semantics to avoid cross-repo drift.
- Consider defining a small errors module with stable exception classes to make contract failures machine-checkable and testable.
- The integer-only contract_version gating is conservative but may frustrate future minor-compatible changes; plan for forward-compatibility by allowing a target_version parameter to KorosArcStore.
- Enforce JSON schema for provenance to prevent silent shape drift; provide a schema file for shared validation.
- Add hard caps and monitoring on blob sizes to protect pod disks; consider periodic compaction and VACUUM.
- WAL mode and careful index management will improve concurrent write/read behavior; measure after initial implementation.
- The unknown kind policy is good; adding a 'malformed_kind' distinction will enhance diagnostics without rejecting records.
- Clearly specify test fixture files to ensure consistent test construction across repos.
- Provide explicit logger names and prefixes for both enumeration and store write paths; document greppable messages in PRD.
- Document tie-breakers and ordering to improve determinism for UI consumers.
