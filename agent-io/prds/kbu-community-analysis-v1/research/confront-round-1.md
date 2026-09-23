## STALL REPORT (the binding part)

1. PHASE / MODULE: Module placement and facade wiring (toolkit.py additions)
   - DECISION I CANNOT MAKE: The PRD says the facade gains a lazy property in toolkit.py with self._community and a community property, but does not bind exact attribute names in KBUtilLib.__init__ or whether to re-export in kbutillib/__init__.py.
   - WHY IT STALLS ME: I must choose public API names; a wrong choice creates incompatible surface or naming collisions.
   - RECOMMENDED RESOLUTION: Add: Add a community property and _community backing field to KBUtilLib, and re-export MSCommunityUtils and MSCommunityUtilsImpl in kbutillib/__init__.py under those exact symbols.

2. PHASE / MODULE: Dependency declaration and commit pin
   - DECISION I CANNOT MAKE: The PRD requires adding mscommunity to dependencies.yaml and pinning to a commit, and says to warn when HEAD differs, but does not specify the exact SHA nor where to implement the check.
   - WHY IT STALLS ME: An incorrect SHA yields false warnings; placing the check in the wrong layer duplicates or omits diagnostics.
   - RECOMMENDED RESOLUTION: Add: Declare mscommunity with path, git, and commit: b5f37c4b (full 7+ chars) in dependencies.yaml; implement HEAD mismatch warning inside ms_community_utils._import_mscommunity and do not change DependencyManager.

3. PHASE / MODULE: Provenance gate acceptance criteria
   - DECISION I CANNOT MAKE: The PRD mandates rejecting modelseedpy.community.MSCommunity but does not unambiguously define which module paths to accept for the standalone package or the exact failure message.
   - WHY IT STALLS ME: An overly strict allowlist rejects valid re-exports; a lax one admits the wrong class; tests need a stable message.
   - RECOMMENDED RESOLUTION: Add: Accept if MSCommunity.__module__ starts with mscommunity.; reject if it starts with modelseedpy.community.; set unavailable_reason to a standard message naming the rejected module path.

4. PHASE / MODULE: CommunityModel provenance persistence and load_community
   - DECISION I CANNOT MAKE: The PRD suggests reading member_ids from model.notes but does not define an authoritative notes key or schema to persist member_ids and source_model_ids.
   - WHY IT STALLS ME: Without a bound schema, round-trips may fail unpredictably; raising without guidance leaves saved models unusable.
   - RECOMMENDED RESOLUTION: Add: Persist member_ids and source_model_ids under model.notes[kbutil.community] as JSON; load_community reads that; if missing and no override provided, raise ValueError naming the missing key.

5. PHASE / MODULE: predict_abundances non-mutating behavior
   - DECISION I CANNOT MAKE: The PRD says copy and restore abundance state but does not enumerate precisely what to snapshot and restore.
   - WHY IT STALLS ME: Restoring too little leaves hidden mutation; restoring too much may conflict with upstream side-effects.
   - RECOMMENDED RESOLUTION: Add: When update=false, snapshot and restore both comm.mscomm.abundances and coefficients of all primary biomass reactions modified by set_abundance.

6. PHASE / MODULE: MICOM solver capability detection
   - DECISION I CANNOT MAKE: Acceptable solvers are listed but the detection mechanism and exact error message are not specified.
   - WHY IT STALLS ME: Incorrect detection causes false negatives; inconsistent messages break tests and UX.
   - RECOMMENDED RESOLUTION: Add: Detect via comm.mscomm._QP_CAPABLE when present; else check model.solver.interface.__name__ among gurobi_interface, cplex_interface, osqp_interface; otherwise raise CommunitySolverError with a standard message naming the acceptable solvers.

7. PHASE / MODULE: Cross-feeding msdb path binding
   - DECISION I CANNOT MAKE: The PRD says to resolve ModelSEEDDatabase via get_dependency_path but does not bind how to pass it into mscommviz.interactions (argument name vs. env var).
   - WHY IT STALLS ME: API differences across MSCommunity versions can break the call without explicit binding.
   - RECOMMENDED RESOLUTION: Add: Call mscommviz.interactions with msdb_path=... and visualize=False; if not supported, set environment variable MSCOMMUNITY_MSDB_PATH prior to import.

8. PHASE / MODULE: Python 3.9 staticmethod hazard policy
   - DECISION I CANNOT MAKE: The PRD adds an _unwrap helper for our calls but does not set a support policy or documentation for degraded upstream behavior on Python 3.9.
   - WHY IT STALLS ME: Tests and user guidance cannot be finalized without policy.
   - RECOMMENDED RESOLUTION: Add: Support Python >= 3.9; on 3.9 direct comm.mscomm.interactions may fail; kbu.community.cross_feeding_table remains supported via _unwrap; add a release note and test skip marker.

9. PHASE / MODULE: Escher projection helper and banner content
   - DECISION I CANNOT MAKE: The projection steps are outlined but the pure-function signature and banner fields/text are not bound.
   - WHY IT STALLS ME: Tests need concrete outputs; UX needs consistent phrasing.
   - RECOMMENDED RESOLUTION: Add: Implement a pure helper _project_member_fluxes(result_flux, member_index, bio_rxn_id)->dict and require banner fields: member_id, community_id, media_id, community_growth, plus an exchange disclaimer.

10. PHASE / MODULE: Capability registration names
   - DECISION I CANNOT MAKE: The PRD requires capabilities but does not bind exact capability names, summaries, and tags.
   - WHY IT STALLS ME: Capability names are public API; wrong choices break CLI and HTTP contracts.
   - RECOMMENDED RESOLUTION: Add: Bind names community.build_community, community.run_community_fba, community.predict_abundances, community.run_micom, community.test_member_growth, community.gapfill_community, community.cross_feeding_table, community.cross_feeding_graph, community.render_cross_feeding, community.render_member_map.

11. PHASE / MODULE: FVA behavior in run_community_fba
   - DECISION I CANNOT MAKE: The signature includes fva_reactions but the return format and interaction with pfba are unspecified.
   - WHY IT STALLS ME: Ambiguity risks unsupported or conflicting combinations.
   - RECOMMENDED RESOLUTION: Add: If fva_reactions provided, run FVA post-FBA and attach result.fva mapping rxn_id to (min,max); disallow with pfba=true with a clear error.

12. PHASE / MODULE: Exception hierarchy and export
   - DECISION I CANNOT MAKE: The PRD names three exceptions but not their base classes or export location relative to existing errors.
   - WHY IT STALLS ME: CLI/API error mapping depends on hierarchy; tests need a stable import path.
   - RECOMMENDED RESOLUTION: Add: Define CommunityDependencyError (derives from BackendUnavailableError), CommunitySolverError, and CommunityVisualizationError (derive from Exception) in ms_community_utils.py and export via __all__.

13. PHASE / MODULE: Workspace save/load object type
   - DECISION I CANNOT MAKE: The PRD says save a community model to the workspace but does not bind the object type or tagging to mark it as community.
   - WHY IT STALLS ME: Choosing the wrong type breaks interoperability.
   - RECOMMENDED RESOLUTION: Add: Save as KBaseFBA.FBAModel with notes kbutil.community JSON including member_ids and source_model_ids; include a version tag.

14. PHASE / MODULE: Media resolution and result media_id
   - DECISION I CANNOT MAKE: The PRD allows media=None or names but does not define resolution rules or how to set media_id in results.
   - WHY IT STALLS ME: Inconsistent IDs harm provenance.
   - RECOMMENDED RESOLUTION: Add: Resolve via KBModelUtils.get_media and set media_id to the resolved ID or provided name; when None, set default.

15. PHASE / MODULE: Cross-feeding graph schema and thresholds
   - DECISION I CANNOT MAKE: Edge attributes and default min_abs_flux are not bound; negative flux handling is unspecified; size warning threshold is not fixed.
   - WHY IT STALLS ME: Downstream reproducibility requires a fixed schema and thresholds.
   - RECOMMENDED RESOLUTION: Add: Default min_abs_flux=1e-4; edges carry metabolite, flux (signed donor->recipient), abs_flux; reverse edge for negative values; warn when node count > 40 via logger.warning.

16. PHASE / MODULE: Batched-LP backend passthrough
   - DECISION I CANNOT MAKE: The PRD says passthrough only but does not bind the parameter name or allowed values.
   - WHY IT STALLS ME: Mismatched names break runtime calls; docs need explicit options.
   - RECOMMENDED RESOLUTION: Add: Expose backend with allowed values cpu, jax, cupy, pdlp; default cpu; forward unchanged.

17. PHASE / MODULE: Tests access to _import_mscommunity
   - DECISION I CANNOT MAKE: The PRD references using _import_mscommunity in test skip conditions but does not bind its import path or sanctioned monkeypatching approach.
   - WHY IT STALLS ME: Tests need a stable module path and guidance to fabricate fake modules for the provenance test.
   - RECOMMENDED RESOLUTION: Add: Expose _import_mscommunity at kbutillib.domains.modeling.ms_community_utils._import_mscommunity and permit tests to patch sys.modules to simulate scenarios.

18. PHASE / MODULE: Exchange reactions in projection
   - DECISION I CANNOT MAKE: The PRD says include EX_ reactions and add a banner note but does not bind whether to scale or attribute per member.
   - WHY IT STALLS ME: Different choices change visuals and interpretation.
   - RECOMMENDED RESOLUTION: Add: Include EX_ reactions unmodified at community totals; do not attempt per-member attribution; banner notes this explicitly.

## FREE CRITIQUE (non-binding)
- The PRD is detailed but mixes findings with requirements; include a brief implementor checklist to reduce risk.
- Upstream silent kinetic-constraint removal and stdout prints degrade provenance; consider capturing warnings into result fields.
- Graphviz is fragile on servers; consider a networkx + matplotlib fallback for rendering when dot is missing.
- Provide a small env-check to validate mscommunity provenance and HEAD vs pin before use.
- Given Python 3.9 hazards, consider raising min Python to 3.10 or vendoring a shim for mscommviz functions.
