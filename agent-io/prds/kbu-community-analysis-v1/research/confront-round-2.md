## STALL REPORT (the binding part)

1) PHASE / MODULE: Capability registration and public API surface
   - DECISION I CANNOT MAKE: "Capability names are public API and are bound here … These are `@capability`'s default `"<domain>.<fn.__name__>"`" and Acceptance Criterion 26 says "Capability names are exactly the thirteen `community.<method_name>` strings listed in Implementation Decisions."
   - WHY IT STALLS ME: The PRD lists fourteen methods under the capability section (`build_community`, `load_community`, `save_community`, `export_community_sbml`, `run_community_fba`, `predict_abundances`, `run_micom`, `test_member_growth`, `gapfill_community`, `cross_feeding_table`, `cross_feeding_graph`, `fluxes_by_member`, `render_community_map`, `render_member_map`). If tests or downstream tooling assert the count "thirteen" or a specific set, I could register the wrong set and break CLI/MCP contracts.
   - RECOMMENDED RESOLUTION: "Bind and register exactly these fourteen capabilities: community.build_community, community.load_community, community.save_community, community.export_community_sbml, community.run_community_fba, community.predict_abundances, community.run_micom, community.test_member_growth, community.gapfill_community, community.cross_feeding_table, community.cross_feeding_graph, community.fluxes_by_member, community.render_community_map, community.render_member_map."

2) PHASE / MODULE: Member projection banner (render_member_map)
   - DECISION I CANNOT MAKE: Acceptance Criterion 25: "The injected banner names the member id, the community id, the media id and the community growth rate, and contains the sentence stating that `EX_` exchange reactions are community-level and cannot be attributed to one member; `EX_` fluxes are included …"
   - WHY IT STALLS ME: No exact wording is specified. If downstream tests grep for a specific sentence or exact text, picking my own phrasing could fail silently. The figure provenance text also needs a canonical format to keep notebooks consistent.
   - RECOMMENDED RESOLUTION: "Inject the following exact banner text at the top of the HTML: 'Member {member_id} projected from community {community_id} on medium {media_id} (community growth {community_growth:.4f}/hr). Note: EX_ exchange reactions are community-level on the shared e0 compartment and cannot be attributed to a single member; EX_ fluxes are shown as community totals.'"

3) PHASE / MODULE: SBML export (export_community_sbml)
   - DECISION I CANNOT MAKE: The PRD specifies an `export_community_sbml(comm, path)` method but does not name the export API to call. KBUtilLib has Cobra converters, but no canonical "write SBML" helper is referenced: "export a community model to SBML" without stating whether to use `cobra.io.write_sbml_model` or `FBAModel` serialization.
   - WHY IT STALLS ME: Choosing the wrong exporter changes content (e.g., loss of KBase-specific fields or notes), and could produce SBML that downstream tools cannot read. SBML serialization has multiple variants in this codebase.
   - RECOMMENDED RESOLUTION: "Export using cobrapy: `cobra.io.write_sbml_model(comm.mscomm.model, path)`. If the model is a KBase `FBAModel`, first convert via `KBModelUtils.CobraModelConverter.to_cobra(FBAModel)` and then write SBML. Record that `kbutil.community` notes are preserved only in the KBase save path, not in SBML."

4) PHASE / MODULE: QP backend detection for MICOM (run_micom)
   - DECISION I CANNOT MAKE: "Detection reuses upstream's own table … `_QP_CAPABLE` and `_pick_qp_backend` are MODULE-level names in `mscommunity.mscommsim`".
   - WHY IT STALLS ME: Those symbols are not present in this repo; the PRD asserts their existence upstream but does not provide fallbacks for the case where they are renamed or absent. Without a concrete fallback contract, I'd be guessing how to detect QP capability in a way tests expect.
   - RECOMMENDED RESOLUTION: "Attempt to import `mscommunity.mscommsim` and read `_QP_CAPABLE` and `_pick_qp_backend`. If either is missing, fall back to `optlang.available_solvers` containment of one of {"gurobi","cplex","osqp","hybrid"}. Document this two-tier check in the method docstring and test both branches."

5) PHASE / MODULE: Drain reopening semantics (test_member_growth)
   - DECISION I CANNOT MAKE: The PRD instructs to reopen member drains from `member.biomass_drain_bounds` when `close_member_drains` is True and `interacting=False`.
   - WHY IT STALLS ME: The attribute path (`member.biomass_drain_bounds`) is not defined in this repo and could differ by upstream version. If it is wrong, solo growth will still read as zero and the guard will silently fail. I need an exact attribute location or a snapshot API to recover drain bounds.
   - RECOMMENDED RESOLUTION: "Mandate upstream attribute path: `comm.mscomm.member_biomass_drains[member_id]` (dict of `(lower, upper)`), with a guaranteed snapshot taken at build time. If the actual upstream path differs, the PRD must name the exact attribute and its type."

6) PHASE / MODULE: Kinetic-relaxation stdout markers (run_community_fba / predict_abundances)
   - DECISION I CANNOT MAKE: Acceptance Criterion 16: "A test asserts the stdout marker strings against the literal text present in `mscommunity/mscommsim.py`" and the PRD mentions matching "Kinetic constraints disabled" and "doesn't grow".
   - WHY IT STALLS ME: Without the exact strings (including punctuation/casing), I cannot write a robust detector or tests. Upstream wording changes could silently break flagging.
   - RECOMMENDED RESOLUTION: "Bind the exact markers: 'Kinetic constraints disabled' and 'doesn\'t grow'. Tests should assert these literals. Document that wording changes upstream require updating the constants here."

7) PHASE / MODULE: Provenance gate error message text
   - DECISION I CANNOT MAKE: Acceptance Criterion 3 requires the rejection reason to contain the literal phrase "the superseded copy" followed by the module path resolved.
   - WHY IT STALLS ME: The rest of the message is not bound. If tests expect more exact text, my implementation could differ. This needs a canonical, full error string to avoid mismatched assertions.
   - RECOMMENDED RESOLUTION: "Use the exact rejection message: 'resolved MSCommunity is modelseedpy.community.MSCommunity (the superseded copy), not the standalone mscommunity package: {resolved_module_path}'."

8) PHASE / MODULE: Community handle provenance fields
   - DECISION I CANNOT MAKE: The `CommunityModel` dataclass fields are named, but exact types and whether `source_model_ids` maps member-id→source-model-id or model-id→member-id are not illustrated with an example.
   - WHY IT STALLS ME: Getting the mapping direction wrong breaks `render_member_map` (cannot find the source model) and corrupts provenance. I need an explicit schema example.
   - RECOMMENDED RESOLUTION: "Define `CommunityModel.source_model_ids` as a dict `{member_id: source_model_id}`; example: `{"A": "iML1515", "B": "Bth"}`."

9) PHASE / MODULE: `load_community` fallbacks
   - DECISION I CANNOT MAKE: "Falls back to an explicit `member_ids` argument, then upstream's `model.notes["member_biomass_cpds"]`".
   - WHY IT STALLS ME: The exact shape of `member_biomass_cpds` is unspecified (list? dict?). Without it, parsing could be wrong and member ordering miscomputed.
   - RECOMMENDED RESOLUTION: "Bind `model.notes["member_biomass_cpds"]` to an ordered list of member biomass compound ids in community order, e.g. `["cpdXXXX_c1", "cpdYYYY_c2"]`. Document that this list defines `member_ids` order."

10) PHASE / MODULE: `cross_feeding_graph` node/edge identity semantics
   - DECISION I CANNOT MAKE: The PRD says "one node per member plus an `Environment` node" and edges carry `metabolite`, `flux`, `abs_flux`, but does not specify edge keys (e.g., multi-edges per metabolite vs aggregate edges) or whether donor→recipient edges are per metabolite or aggregated.
   - WHY IT STALLS ME: Choosing aggregated vs per-metabolite edges changes graph size and downstream metrics. Tests may expect one or the other.
   - RECOMMENDED RESOLUTION: "Emit one directed edge per donor→recipient metabolite with a unique key `metabolite`, i.e., use a `networkx.MultiDiGraph` and set `graph.is_multigraph = True`."

11) PHASE / MODULE: `render_community_map` style default
   - DECISION I CANNOT MAKE: The method signature includes `style` mapped to `escher_edit.MapStyle` but no default is specified.
   - WHY IT STALLS ME: Passing `None` may select a library default that changes over time. A bound default keeps figures stable and testable.
   - RECOMMENDED RESOLUTION: "Default `style` to `escher_edit.MapStyle()` constructed with its library defaults, and bind `layout='default'` in `render_map_svg`."

12) PHASE / MODULE: Compound name resolution failures
   - DECISION I CANNOT MAKE: The PRD says "falls back to using the ids as their own names when biochem is unavailable" but does not specify whether to strip `_c#`/`_e0` suffixes in labels.
   - WHY IT STALLS ME: Escher node labels with compartment suffixes are noisy; tests may expect bare compound ids without compartment suffix.
   - RECOMMENDED RESOLUTION: "Strip compartment suffixes (`_c\d+`, `_e0`) from compound ids before using them as display names when biochem resolution fails."

13) PHASE / MODULE: `export_community_sbml` path semantics
   - DECISION I CANNOT MAKE: Whether to create parent directories and whether to overwrite existing files is unstated.
   - WHY IT STALLS ME: Silent overwrites vs fail-on-exist change user expectations and test behavior.
   - RECOMMENDED RESOLUTION: "Create parent directories (`parents=True, exist_ok=True`) and overwrite existing files by default; add `overwrite: bool = True` to the signature to let callers opt out."

14) PHASE / MODULE: Abundance normalization rounding/precision
   - DECISION I CANNOT MAKE: The PRD says abundances are normalized to sum to 1, but does not specify precision/rounding behavior or tolerance for sums slightly off due to float error.
   - WHY IT STALLS ME: Persisted `abundances` in `kbutil.community` notes could differ between runs if rounding is inconsistent; tests comparing JSON may fail.
   - RECOMMENDED RESOLUTION: "Normalize abundances to exact sum 1.0 using `Decimal` with 1e-12 precision, and emit values rounded to 8 decimal places in `kbutil.community` notes."

15) PHASE / MODULE: `gapfill_community` argument binding
   - DECISION I CANNOT MAKE: The PRD names `templates`, `models`, and `target` but does not bind them to upstream `MSGapfill` parameter names or types.
   - WHY IT STALLS ME: Misbound arguments make gapfilling do the wrong thing silently (PRD itself warns about a historical positional bug). I need exact param names/types.
   - RECOMMENDED RESOLUTION: "Bind `templates` to a list of template model ids, `models` to a list of MSModelUtil/cobra models to draw reactions from, and `target` to a float community growth target. Forward these by keyword to `MSCommunity.gapfill(templates=..., models=..., target=..., solver=...)`."

16) PHASE / MODULE: `fluxes_by_member` environment handling
   - DECISION I CANNOT MAKE: The PRD says "drop the `Environment` column" but does not specify whether compounds only present in `Environment` should be excluded from `compound_names` resolution and map legend.
   - WHY IT STALLS ME: Including such compounds in label resolution wastes time and can skew counts displayed in `n_compounds`.
   - RECOMMENDED RESOLUTION: "Exclude compounds that appear only in the `Environment` column (no member has non-zero flux) from both `fluxes_by_member` and the `compound_names` resolution."

17) PHASE / MODULE: `load_community` error message on missing provenance
   - DECISION I CANNOT MAKE: Acceptance Criterion 8 says "raises `ValueError` naming all three routes"; exact text is not bound.
   - WHY IT STALLS ME: Tests may assert exact wording; free-form messages risk mismatch.
   - RECOMMENDED RESOLUTION: "Raise exactly: 'Cannot reconstruct community provenance: missing kbutil.community notes, no member_ids argument supplied, and no member_biomass_cpds in model.notes.'."

18) PHASE / MODULE: `render_community_map` multi-condition input
   - DECISION I CANNOT MAKE: The PRD allows `result_or_results` to be a dict of `{label: result}` but does not specify label validation (allowed chars, uniqueness) or sort order.
   - WHY IT STALLS ME: Unstable block ordering makes figures non-deterministic and tests flaky.
   - RECOMMENDED RESOLUTION: "Require unique string labels; sort blocks lexicographically by label before assembly; document this deterministic ordering."

19) PHASE / MODULE: Escher-edit import path handling
   - DECISION I CANNOT MAKE: The PRD says "add `<path>/src` to `sys.path`" for editEscher but does not specify whether to prefer an already-installed `escher_edit` on `sys.path` over the dependency path.
   - WHY IT STALLS ME: Choosing precedence affects which version is used when both are present; mismatch against the pinned commit could confuse users.
   - RECOMMENDED RESOLUTION: "Prefer an already-importable `escher_edit` on `sys.path`; only add `<dependency path>/src` if import fails. Log a warning when the resolved version differs from the pinned commit, but do not refuse."

20) PHASE / MODULE: Community ID binding (build_community)
   - DECISION I CANNOT MAKE: The PRD includes `model_id` and `name` in the signature but does not bind how these map onto upstream `MSCommunity` fields.
   - WHY IT STALLS ME: Misbinding could set the wrong identifier visible in downstream outputs and saved models.
   - RECOMMENDED RESOLUTION: "Bind `model_id` to the underlying COBRA model id (`comm.mscomm.model.id`) and `name` to a human-friendly display name stored in `comm.mscomm.model.name` and echoed in `kbutil.community` notes."

21) PHASE / MODULE: `CommunityFBAResult.fluxes` type
   - DECISION I CANNOT MAKE: The PRD says "the full `pandas.Series`" but does not bind index dtype (string ids vs `cobra.Reaction`) or NaN handling.
   - WHY IT STALLS ME: Downstream consumers may expect strings; a mismatch breaks JSON serialization.
   - RECOMMENDED RESOLUTION: "Bind `fluxes` to a `pandas.Series` with string reaction ids as index and float values, no NaNs (drop missing)."

22) PHASE / MODULE: `render_community_map` counts
   - DECISION I CANNOT MAKE: Acceptance Criterion 36: return `n_members`, `n_compounds`, `n_blocks` — but it does not specify whether `n_compounds` is total across all blocks or max-per-block.
   - WHY IT STALLS ME: Different counting semantics change acceptance. I need a bound definition.
   - RECOMMENDED RESOLUTION: "Define `n_compounds` as the total distinct compound ids across all blocks (union)."

23) PHASE / MODULE: Python version gate documentation
   - DECISION I CANNOT MAKE: The PRD states support policy for 3.9 but does not bind how the module reports version-related upstream failures (e.g., calling `comm.mscomm.interactions()` directly via escape hatch).
   - WHY IT STALLS ME: Users may misinterpret upstream TypeErrors; a bound logging/error policy avoids confusion.
   - RECOMMENDED RESOLUTION: "Document in the module README: 'On Python 3.9, upstream mscommunity.mscommviz staticmethod calls may raise TypeError when accessed directly; use KBUtilLib wrappers which guard this via `_unwrap`.' Emit a `logging.WARNING` when detecting Python 3.9 and escape-hatch calls."

24) PHASE / MODULE: `member_groups` taxonomy helper wiring
   - DECISION I CANNOT MAKE: The PRD suggests forwarding `escher_edit.palette.taxon_groups` but does not specify where to source taxonomy strings or how to inject them.
   - WHY IT STALLS ME: Without an explicit input contract, I could overreach into KBUtilLib genome utilities incorrectly.
   - RECOMMENDED RESOLUTION: "Accept `member_groups` as `{member_id: group}` or a callable `(member_id) -> group`. Do not attempt to auto-derive taxonomy; only forward the mapping to `escher_edit`."

25) PHASE / MODULE: `save_community` notes schema evolvability
   - DECISION I CANNOT MAKE: The PRD sets `schema_version = 1` but does not specify migration behavior when loading older/newer versions.
   - WHY IT STALLS ME: Future changes could break load unless a migration path is defined.
   - RECOMMENDED RESOLUTION: "On load, accept known schema versions {1}; if an unknown version is encountered, raise `ValueError('Unsupported kbutil.community schema_version: {v}')`. Document this in `load_community`."

26) PHASE / MODULE: `render_member_map` source model retrieval failure
   - DECISION I CANNOT MAKE: Acceptance Criterion 24 mandates a named `ValueError` when `source_model_ids` lacks the member, but does not bind the exact message.
   - WHY IT STALLS ME: Tests may assert text; I need a canonical message.
   - RECOMMENDED RESOLUTION: "Raise exactly: 'No source model id recorded for member {member_id}; cannot render member projection.'"

27) PHASE / MODULE: `run_micom` return semantics
   - DECISION I CANNOT MAKE: Signature says `-> list[CommunityFBAResult]` without binding whether it's a single-element list for one tradeoff or a time series of intermediate points.
   - WHY IT STALLS ME: Downstream code needs to know what to expect. Returning a list when callers expect a single result causes confusion.
   - RECOMMENDED RESOLUTION: "Return a single `CommunityFBAResult` for a single `tradeoff` value; change the signature to `-> CommunityFBAResult`."

28) PHASE / MODULE: `predict_abundances` determinize/regularization semantics
   - DECISION I CANNOT MAKE: The PRD surfaces `regularization=True, determinize=False` but does not bind their effect on the returned dict vs notes.
   - WHY IT STALLS ME: I need to decide whether to annotate `notes` with which subroutine ran for provenance.
   - RECOMMENDED RESOLUTION: "Record `notes['predict_abundances'] = {'regularization': regularization, 'determinize': determinize}` in the returned result (or on `CommunityFBAResult` when routed via FBA)."

29) PHASE / MODULE: `dependencies.yaml` commit mismatch logging format
   - DECISION I CANNOT MAKE: Acceptance Criterion 4 says "logs a warning naming both SHAs" without binding prefix/suffix.
   - WHY IT STALLS ME: Tests may grep exact format.
   - RECOMMENDED RESOLUTION: "Log exactly: 'MSCommunity commit mismatch: pinned {PINNED_SHA}, found {FOUND_SHA}'."


## FREE CRITIQUE (non-binding)

- The PRD is impressively thorough, but it relies on upstream symbol names (`_QP_CAPABLE`, `_pick_qp_backend`, `member.biomass_drain_bounds`) that are not visible in this repo. Where those names are binding (tests assert them), consider quoting exact upstream code references or embedding an interface shim to decouple from upstream drift.
- `export_community_sbml` needs a concrete exporter choice (cobrapy vs KBase FBAModel serialization). SBML fidelity varies; choosing cobrapy and documenting the limitations is pragmatic.
- The capability count contradiction (thirteen vs fourteen) should be fixed to avoid confusion in tooling and acceptance audits.
- `render_community_map` should bind deterministic ordering for multi-condition blocks and a default style; figure reproducibility is otherwise at risk.
- Compound-name fallback should strip compartment suffixes to improve readability when biochemistry is unavailable.
- `run_micom` returning a list for a single tradeoff value is confusing; prefer a single result and expose batch tradeoff sweeps separately later.
- Drain reopening relies on a specific upstream attribute that may change; consider snapshotting bounds at build time on the handle to make reopen robust.
- The kinetic-relaxation markers must be constant-literal-bound in this module to make tests less brittle to upstream wording changes.
- Notes schema should declare a migration policy now (even if just "raise on unknown version"); silent evolution will break load later.
- The PRD drops the graphviz renderer, which is sensible, but some teams may expect continuity with existing figures; consider documenting that `escher_edit` is the canonical replacement and linking examples.
