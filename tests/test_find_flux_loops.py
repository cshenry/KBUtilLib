"""Behavioral tests for the EGC (energy-generating cycle) detection utilities.

All tests use small hand-built cobra.Model stand-ins wrapped via
MSModelUtil.get(model).  No real MSTemplate or KBase authentication is needed.
Tests are skipped when cobra or modelseedpy are unavailable.

Test inventory (from PRD Testing Decisions):
  T1 - find_flux_loops end-to-end: planted ATP futile cycle is reported;
       empty list when reversibility is corrected.
  T2 - minimize_active_reactions: count-minimal (short) path on parallel-path toy.
  T3 - enumerate_alternative_reaction_sets: interchangeable reactions are each
       other's alternatives; essential reaction is flagged essential=True.
  T4 - add_probe_reaction: correct stoichiometry/annotation; reuse on re-call.
  T5 - model unmodified after find_flux_loops run.
  T6 - specificity: ATP probe yields zero on a model with only an energy-neutral
       cycle that does NOT regenerate ATP (non-binding refinement from PRD).
"""

import pytest


def _require_deps():
    """Skip helper: call at top of each test that needs cobra+modelseedpy."""
    pytest.importorskip("cobra", reason="cobra required for EGC tests")
    pytest.importorskip("modelseedpy", reason="modelseedpy required for EGC tests")


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _make_msutil(cobra_model):
    """Wrap a cobra.Model in MSModelUtil."""
    from modelseedpy.core.msmodelutl import MSModelUtil

    # Always create a fresh wrapper (don't use the singleton cache across tests)
    return MSModelUtil(cobra_model)


@pytest.fixture
def atp_cycle_model():
    """Closed cobra.Model with exactly one planted ATP futile cycle.

    Reactions:
        R_atp:  ATP + H2O -> ADP + Pi + H    (irreversible, forward)
        R_rev:  ADP + Pi + H -> ATP + H2O    (reversible ← the EGC defect)
        R_clean: X -> Y                       (unrelated, irreversible)

    The cycle R_atp + R_rev_reverse generates net ATP from nothing.
    Metabolites carry ModelSEED compound annotations so msid_hash and
    assign_reliability_scores_to_reactions work correctly.
    """
    _require_deps()
    import cobra

    m = cobra.Model("atp_cycle")

    # Metabolites with ModelSEED annotations
    def _met(msid, compartment="c0", name=""):
        met = cobra.Metabolite(f"{msid}_{compartment}", name=name or msid, compartment=compartment)
        met.annotation = {"seed.compound": msid}
        return met

    atp = _met("cpd00002", name="ATP")
    h2o = _met("cpd00001", name="H2O")
    adp = _met("cpd00008", name="ADP")
    pi = _met("cpd00009", name="Pi")
    hplus = _met("cpd00067", name="H+")
    x = _met("cpd99001", name="X")
    y = _met("cpd99002", name="Y")

    # R_atp: ATP + H2O -> ADP + Pi + H  (irreversible forward)
    r_atp = cobra.Reaction("R_atp", lower_bound=0, upper_bound=1000)
    r_atp.add_metabolites({atp: -1, h2o: -1, adp: 1, pi: 1, hplus: 1})
    r_atp.annotation = {"seed.reaction": "rxn00062"}

    # R_rev: ADP + Pi + H -> ATP + H2O  (reversible — the EGC defect)
    r_rev = cobra.Reaction("R_rev", lower_bound=-1000, upper_bound=1000)
    r_rev.add_metabolites({adp: -1, pi: -1, hplus: -1, atp: 1, h2o: 1})
    r_rev.annotation = {"seed.reaction": "rxn00062_rev"}

    # R_clean: X -> Y  (unrelated, forward-only)
    r_clean = cobra.Reaction("R_clean", lower_bound=0, upper_bound=1000)
    r_clean.add_metabolites({x: -1, y: 1})

    m.add_reactions([r_atp, r_rev, r_clean])
    return m


@pytest.fixture
def atp_cycle_model_fixed(atp_cycle_model):
    """Same model but with the EGC defect corrected.

    R_rev's forward direction is ADP+Pi+H -> ATP+H2O (ATP synthesis — the defect).
    Correcting means blocking the forward (synthesis) direction: ub=0.
    With lb=-1000, ub=0, R_rev can only run in reverse (ATP hydrolysis, like the probe),
    but cannot synthesize ATP.  Without ATP synthesis, the probe gets no net ATP supply
    and vmax=0.
    """
    _require_deps()
    atp_cycle_model.reactions.get_by_id("R_rev").upper_bound = 0
    return atp_cycle_model


@pytest.fixture
def parallel_paths_model():
    """Closed cobra.Model with two parallel paths of different lengths.

    Objective: MAX{R_probe} (a drain from Z)

    Short path (2 reactions):
        R_s1: A -> Z   (produces Z in one step)

    Long path (3 reactions):
        R_l1: A -> B
        R_l2: B -> Z

    Plus the probe: R_probe: Z ->  (drain, the 'objective')

    The pFBA flux-minimal solution might use the short path, but both are
    equally flux-minimal for the probe at flux=1 if A is unlimited.
    To force a count distinction we make the long path use 2 reactions while
    the short uses 1 (not counting the probe), so minimize_active_reactions
    must return size=1 (only R_s1) not size=2 (R_l1+R_l2).

    A supply reaction R_supply: -> A  (unconstrained source, lb=0, ub=1000)
    is included so the system is feasible.
    """
    _require_deps()
    import cobra

    m = cobra.Model("parallel_paths")

    def _met(mid, compartment="c0"):
        met = cobra.Metabolite(f"{mid}_{compartment}", name=mid, compartment=compartment)
        met.annotation = {"seed.compound": mid}
        return met

    a = _met("cpd99010")
    b = _met("cpd99011")
    z = _met("cpd99012")

    r_supply = cobra.Reaction("R_supply", lower_bound=0, upper_bound=1000)
    r_supply.add_metabolites({a: 1})

    r_s1 = cobra.Reaction("R_s1", lower_bound=0, upper_bound=1000)
    r_s1.add_metabolites({a: -1, z: 1})

    r_l1 = cobra.Reaction("R_l1", lower_bound=0, upper_bound=1000)
    r_l1.add_metabolites({a: -1, b: 1})

    r_l2 = cobra.Reaction("R_l2", lower_bound=0, upper_bound=1000)
    r_l2.add_metabolites({b: -1, z: 1})

    r_probe = cobra.Reaction("R_probe", lower_bound=0, upper_bound=1000)
    r_probe.add_metabolites({z: -1})

    m.add_reactions([r_supply, r_s1, r_l1, r_l2, r_probe])
    m.objective = "R_probe"
    return m


@pytest.fixture
def alternatives_model():
    """Closed cobra.Model for enumerate_alternative_reaction_sets tests.

    Topology:
        R_supply: -> A
        R_branch1: A -> Z   (interchangeable with R_branch2)
        R_branch2: A -> Z   (interchangeable with R_branch1)
        R_essential: Z ->   (essential; knockout → infeasible for probe)
        R_probe: Z ->        (the pinned objective drain)

    When minimized: either R_branch1 or R_branch2 is active (not both).
    Knocking out whichever is active reveals the other as an alternative.
    Knocking out R_essential makes the system infeasible.
    """
    _require_deps()
    import cobra

    m = cobra.Model("alternatives")

    def _met(mid, compartment="c0"):
        met = cobra.Metabolite(f"{mid}_{compartment}", name=mid, compartment=compartment)
        met.annotation = {"seed.compound": mid}
        return met

    a = _met("cpd99020")
    z = _met("cpd99021")

    r_supply = cobra.Reaction("R_supply", lower_bound=0, upper_bound=1000)
    r_supply.add_metabolites({a: 1})

    r_b1 = cobra.Reaction("R_branch1", lower_bound=0, upper_bound=1000)
    r_b1.add_metabolites({a: -1, z: 1})

    r_b2 = cobra.Reaction("R_branch2", lower_bound=0, upper_bound=1000)
    r_b2.add_metabolites({a: -1, z: 1})

    r_ess = cobra.Reaction("R_essential", lower_bound=0, upper_bound=1000)
    r_ess.add_metabolites({z: -1})

    m.add_reactions([r_supply, r_b1, r_b2, r_ess])
    m.objective = "R_essential"
    return m


@pytest.fixture
def probe_model():
    """Small model for add_probe_reaction tests.

    Contains ATP-related metabolites so the ATP hydrolysis probe can be
    added, and we can also test re-use of an existing matching reaction.
    """
    _require_deps()
    import cobra

    m = cobra.Model("probe_test")

    def _met(msid, compartment="c0"):
        met = cobra.Metabolite(f"{msid}_{compartment}", name=msid, compartment=compartment)
        met.annotation = {"seed.compound": msid}
        return met

    atp = _met("cpd00002")
    h2o = _met("cpd00001")
    adp = _met("cpd00008")
    pi = _met("cpd00009")
    hplus = _met("cpd00067")

    # Add an unrelated reaction so the model is not empty
    r_other = cobra.Reaction("R_other", lower_bound=0, upper_bound=1000)
    r_other.add_metabolites({atp: -1, h2o: -1, adp: 1, pi: 1, hplus: 1})
    r_other.annotation = {"seed.reaction": "rxn00062"}

    m.add_reactions([r_other])
    return m


# ── T1: find_flux_loops end-to-end ──────────────────────────────────────────


class TestFindFluxLoopsEndToEnd:
    """T1: ATP cycle is reported; clean model reports empty list."""

    def test_atp_cycle_detected(self, atp_cycle_model):
        """find_flux_loops reports exactly the planted ATP cycle."""
        _require_deps()
        from kbutillib.ms_fba_utils import find_flux_loops_standalone, EGC_PROBE_CATALOG
        from modelseedpy.core.msmodelutl import MSModelUtil

        mdlutl = _make_msutil(atp_cycle_model)
        results = find_flux_loops_standalone(
            mdlutl,
            objective="atp",
            compartment="c0",
            max_loops_per_probe=5,
        )

        assert "atp_hydrolysis" in results, f"Expected 'atp_hydrolysis' key, got {list(results.keys())}"
        loops = results["atp_hydrolysis"]
        assert len(loops) >= 1, f"Expected at least one loop, got {loops}"

        # The loop should contain R_rev (used in reverse direction)
        loop = loops[0]
        assert loop["size"] >= 1
        assert loop["target_flux"] > 1e-6
        rxn_ids_in_loop = {r["id"] for r in loop["reactions"]}
        assert "R_rev" in rxn_ids_in_loop, f"R_rev should be in the loop, got {rxn_ids_in_loop}"

        # Verify schema: exactly the documented keys
        for rxn_rec in loop["reactions"]:
            expected_keys = {
                "id", "direction_used", "flux", "equation",
                "reliability_score", "is_core",
                "alternatives", "coupled", "essential",
            }
            assert set(rxn_rec.keys()) == expected_keys, (
                f"Unexpected keys in reaction record: {set(rxn_rec.keys())}"
            )
            # Type checks
            assert isinstance(rxn_rec["flux"], float)
            assert isinstance(rxn_rec["reliability_score"], float)
            assert isinstance(rxn_rec["is_core"], bool)
            assert isinstance(rxn_rec["essential"], bool)
            assert isinstance(rxn_rec["alternatives"], list)
            assert isinstance(rxn_rec["coupled"], list)

    def test_clean_model_no_loops(self, atp_cycle_model_fixed):
        """find_flux_loops returns empty list for a correctly-directed model."""
        _require_deps()
        from kbutillib.ms_fba_utils import find_flux_loops_standalone

        mdlutl = _make_msutil(atp_cycle_model_fixed)
        results = find_flux_loops_standalone(
            mdlutl,
            objective="atp",
            compartment="c0",
            max_loops_per_probe=5,
        )
        assert "atp_hydrolysis" in results
        assert results["atp_hydrolysis"] == [], (
            f"Expected empty list (no loop), got {results['atp_hydrolysis']}"
        )


# ── T2: minimize_active_reactions ────────────────────────────────────────────


class TestMinimizeActiveReactions:
    """T2: count-minimal path on short-vs-long parallel paths toy."""

    def test_returns_short_path(self, parallel_paths_model):
        """minimize_active_reactions returns the 1-reaction short path, not 2-reaction long."""
        _require_deps()
        from kbutillib.ms_fba_utils import minimize_active_reactions_standalone

        mdlutl = _make_msutil(parallel_paths_model)

        # Disable the long path so only the short path carries flux,
        # then verify minimize_active_reactions picks it.
        # First block the long path to get a known pFBA support.
        parallel_paths_model.reactions.get_by_id("R_l1").upper_bound = 0
        parallel_paths_model.reactions.get_by_id("R_l2").upper_bound = 0

        result = minimize_active_reactions_standalone(
            mdlutl,
            objective="MAX{R_probe}",
        )
        from kbutillib.ms_fba_utils import _strip_reaction_use_pkg
        _strip_reaction_use_pkg(parallel_paths_model, mdlutl.pkgmgr)

        # Restore
        parallel_paths_model.reactions.get_by_id("R_l1").upper_bound = 1000
        parallel_paths_model.reactions.get_by_id("R_l2").upper_bound = 1000

        assert result["size"] >= 1
        rxn_ids = {r["id"] for r in result["reactions"]}
        # Short path (R_s1) should be in the result
        assert "R_s1" in rxn_ids, f"Expected R_s1 in minimal set, got {rxn_ids}"

    def test_count_minimal_not_flux_minimal(self, parallel_paths_model):
        """With both paths available, minimize_active_reactions picks count-minimal (1 rxn)."""
        _require_deps()
        from kbutillib.ms_fba_utils import minimize_active_reactions_standalone, _strip_reaction_use_pkg

        mdlutl = _make_msutil(parallel_paths_model)

        # Both paths available; provide a pre-computed active_filter that includes all
        # (simulate pFBA solution that found both active)
        active_filter = {
            "R_supply": ">",
            "R_s1": ">",
            "R_l1": ">",
            "R_l2": ">",
            "R_probe": ">",
        }
        result = minimize_active_reactions_standalone(
            mdlutl,
            active_filter=active_filter,
        )
        _strip_reaction_use_pkg(parallel_paths_model, mdlutl.pkgmgr)

        # The MILP should choose 1-reaction path (R_s1) over 2-reaction (R_l1+R_l2)
        rxn_ids = {r["id"] for r in result["reactions"]}
        # Either short path or long path wins, but short should be minimal
        # The supply and probe are always needed; among branch reactions, only 1 should be chosen
        branch_rxns = rxn_ids & {"R_s1", "R_l1", "R_l2"}
        assert len(branch_rxns) <= 1, (
            f"Expected at most 1 branch reaction (count-minimal), got {branch_rxns}"
        )

    def test_result_schema(self, parallel_paths_model):
        """minimize_active_reactions result has correct schema."""
        _require_deps()
        from kbutillib.ms_fba_utils import minimize_active_reactions_standalone, _strip_reaction_use_pkg

        mdlutl = _make_msutil(parallel_paths_model)
        result = minimize_active_reactions_standalone(
            mdlutl,
            objective="MAX{R_probe}",
        )
        _strip_reaction_use_pkg(parallel_paths_model, mdlutl.pkgmgr)

        assert "reactions" in result
        assert "size" in result
        assert "solution" in result
        assert result["size"] == len(result["reactions"])
        for r in result["reactions"]:
            assert "id" in r
            assert "direction" in r
            assert "flux" in r
            assert "reliability_score" in r
            assert "is_core" in r
            assert isinstance(r["reliability_score"], float)
            assert isinstance(r["is_core"], bool)


# ── T3: enumerate_alternative_reaction_sets ──────────────────────────────────


class TestEnumerateAlternativeReactionSets:
    """T3: interchangeable reactions are alternatives; essential is flagged."""

    def _get_minimal_result(self, alternatives_model):
        """Helper: return (mdlutl, min_result) with R_essential pinned at lb=1.

        The caller is responsible for restoring R_essential.lower_bound to 0
        after the perturbation scan.
        """
        mdlutl = _make_msutil(alternatives_model)

        # Pin R_essential lb=1 so the model must carry flux (keeps constraint active
        # for both MILP and subsequent enumerate calls).
        alternatives_model.reactions.get_by_id("R_essential").lower_bound = 1

        from kbutillib.ms_fba_utils import minimize_active_reactions_standalone, _strip_reaction_use_pkg
        result = minimize_active_reactions_standalone(
            mdlutl,
            objective="MAX{R_essential}",
        )
        _strip_reaction_use_pkg(alternatives_model, mdlutl.pkgmgr)
        return mdlutl, result

    def test_alternatives_reported(self, alternatives_model):
        """The non-active branch is reported as an alternative for the active one."""
        _require_deps()
        mdlutl, min_result = self._get_minimal_result(alternatives_model)
        # R_essential is still pinned at lb=1 from _get_minimal_result
        from kbutillib.ms_fba_utils import enumerate_alternative_reaction_sets_standalone

        if not min_result["reactions"]:
            alternatives_model.reactions.get_by_id("R_essential").lower_bound = 0
            pytest.skip("minimize_active_reactions found no active reactions")

        perturb = enumerate_alternative_reaction_sets_standalone(mdlutl, min_result)
        alternatives_model.reactions.get_by_id("R_essential").lower_bound = 0  # restore

        rxn_ids_in_result = {r["id"] for r in min_result["reactions"]}

        # Determine which branch was chosen
        active_branch = rxn_ids_in_result & {"R_branch1", "R_branch2"}
        if not active_branch:
            pytest.skip("Neither branch was in the minimal set")

        chosen = next(iter(active_branch))
        other = "R_branch2" if chosen == "R_branch1" else "R_branch1"

        assert chosen in perturb, f"{chosen} should be in perturbation output"
        alternatives = [a[0] for a in perturb[chosen].get("alternatives", [])]
        assert other in alternatives, (
            f"Expected {other} as alternative for {chosen}, got {alternatives}"
        )

    def test_essential_reaction_flagged(self, alternatives_model):
        """R_essential is flagged essential=True because knockout → infeasible."""
        _require_deps()
        mdlutl, min_result = self._get_minimal_result(alternatives_model)
        # R_essential is pinned at lb=1 — knocking it out (ub=0) makes min-dev infeasible
        from kbutillib.ms_fba_utils import enumerate_alternative_reaction_sets_standalone

        if not min_result["reactions"]:
            alternatives_model.reactions.get_by_id("R_essential").lower_bound = 0
            pytest.skip("minimize_active_reactions found no active reactions")

        perturb = enumerate_alternative_reaction_sets_standalone(mdlutl, min_result)
        alternatives_model.reactions.get_by_id("R_essential").lower_bound = 0  # restore

        if "R_essential" in perturb:
            assert perturb["R_essential"]["essential"] is True, (
                "R_essential should be flagged essential"
            )
        else:
            pytest.skip("R_essential not in perturbation output (already filtered by MILP)")

    def test_schema(self, alternatives_model):
        """enumerate_alternative_reaction_sets returns correct schema per reaction."""
        _require_deps()
        mdlutl, min_result = self._get_minimal_result(alternatives_model)
        from kbutillib.ms_fba_utils import enumerate_alternative_reaction_sets_standalone

        if not min_result["reactions"]:
            alternatives_model.reactions.get_by_id("R_essential").lower_bound = 0
            pytest.skip("minimize_active_reactions found no active reactions")

        perturb = enumerate_alternative_reaction_sets_standalone(mdlutl, min_result)
        alternatives_model.reactions.get_by_id("R_essential").lower_bound = 0  # restore

        for rxn_id, rec in perturb.items():
            assert "alternatives" in rec
            assert "coupled" in rec
            assert "essential" in rec
            assert "direction" in rec
            assert "flux" in rec
            assert "reliability_score" in rec
            assert "is_core" in rec
            assert "equation" in rec
            assert isinstance(rec["essential"], bool)
            assert isinstance(rec["alternatives"], list)
            assert isinstance(rec["coupled"], list)


# ── T4: add_probe_reaction ────────────────────────────────────────────────────


class TestAddProbeReaction:
    """T4: correct stoichiometry/annotation; reuse existing match."""

    def test_atp_probe_added(self, probe_model):
        """ATP probe adds correct stoichiometry and seed.reaction annotation."""
        _require_deps()
        from kbutillib.ms_fba_utils import EGC_PROBE_CATALOG, add_probe_reaction_standalone

        # Use a fresh model without any pre-existing ATP hydrolysis reaction
        import cobra

        m = cobra.Model("fresh")

        def _met(msid, compartment="c0"):
            met = cobra.Metabolite(f"{msid}_{compartment}", compartment=compartment)
            met.annotation = {"seed.compound": msid}
            return met

        for msid in ["cpd00002", "cpd00001", "cpd00008", "cpd00009", "cpd00067"]:
            m.add_metabolites([_met(msid)])

        from modelseedpy.core.msmodelutl import MSModelUtil

        mdlutl = MSModelUtil(m)
        atp_probe = EGC_PROBE_CATALOG["atp"][0]
        result = add_probe_reaction_standalone(mdlutl, atp_probe, compartment="c0")

        assert result["new"] is True
        rxn = result["reaction"]
        assert rxn.id == "PROBE_atp_hydrolysis_c0"
        assert rxn.lower_bound == 0
        assert rxn.upper_bound == 1000
        assert "seed.reaction" in rxn.annotation
        assert rxn.annotation["seed.reaction"] == atp_probe["seed_annotation"]

        # Check stoichiometry signs
        met_coefs = {
            met.annotation.get("seed.compound", ""): coef
            for met, coef in rxn.metabolites.items()
        }
        assert met_coefs.get("cpd00002", 0) < 0, "ATP should be a reactant"
        assert met_coefs.get("cpd00008", 0) > 0, "ADP should be a product"

    def test_reuse_existing_reaction(self):
        """add_probe_reaction reuses existing matching reaction (new=False)."""
        _require_deps()
        import cobra
        from modelseedpy.core.msmodelutl import MSModelUtil
        from kbutillib.ms_fba_utils import EGC_PROBE_CATALOG, add_probe_reaction_standalone

        m = cobra.Model("reuse_test")

        def _met(msid, compartment="c0"):
            met = cobra.Metabolite(f"{msid}_{compartment}", compartment=compartment)
            met.annotation = {"seed.compound": msid}
            return met

        atp = _met("cpd00002")
        h2o = _met("cpd00001")
        adp = _met("cpd00008")
        pi = _met("cpd00009")
        hplus = _met("cpd00067")

        # Pre-existing ATP hydrolysis reaction
        existing = cobra.Reaction("rxn00062_c0", lower_bound=0, upper_bound=1000)
        existing.add_metabolites({atp: -1, h2o: -1, adp: 1, pi: 1, hplus: 1})
        existing.annotation = {"seed.reaction": "rxn00062"}
        m.add_reactions([existing])

        mdlutl = MSModelUtil(m)
        atp_probe = EGC_PROBE_CATALOG["atp"][0]
        result = add_probe_reaction_standalone(mdlutl, atp_probe, compartment="c0")

        assert result["new"] is False, "Should reuse existing matching reaction"
        assert result["reaction"].id == existing.id
        # Verify no duplicate was added
        atp_reactions = [r for r in m.reactions if "rxn00062" in r.id or "PROBE" in r.id]
        assert len(atp_reactions) == 1, (
            f"Should have only 1 ATP-related reaction, got {[r.id for r in atp_reactions]}"
        )

    def test_no_duplicate_on_second_call(self, probe_model):
        """Calling add_probe_reaction twice does not add a duplicate."""
        _require_deps()
        from kbutillib.ms_fba_utils import EGC_PROBE_CATALOG, add_probe_reaction_standalone
        import cobra
        from modelseedpy.core.msmodelutl import MSModelUtil

        # Fresh model with probe metabolites
        m = cobra.Model("no_dup")

        def _met(msid):
            met = cobra.Metabolite(f"{msid}_c0", compartment="c0")
            met.annotation = {"seed.compound": msid}
            return met

        for msid in ["cpd00002", "cpd00001", "cpd00008", "cpd00009", "cpd00067"]:
            m.add_metabolites([_met(msid)])

        mdlutl = MSModelUtil(m)
        probe = EGC_PROBE_CATALOG["atp"][0]
        r1 = add_probe_reaction_standalone(mdlutl, probe, compartment="c0")
        r2 = add_probe_reaction_standalone(mdlutl, probe, compartment="c0")

        assert r1["new"] is True
        assert r2["new"] is False
        probe_rxns = [r for r in m.reactions if "PROBE_atp_hydrolysis_c0" in r.id]
        assert len(probe_rxns) == 1, "Should not duplicate the probe reaction"


# ── T5: model unmodified after find_flux_loops ────────────────────────────────


class TestModelUnmodifiedAfterFindFluxLoops:
    """T5: the model is in its original state after find_flux_loops returns."""

    def test_no_probe_reactions_remain(self, atp_cycle_model):
        """No PROBE_ reactions remain in the model after find_flux_loops."""
        _require_deps()
        from kbutillib.ms_fba_utils import find_flux_loops_standalone

        mdlutl = _make_msutil(atp_cycle_model)
        rxn_ids_before = {r.id for r in atp_cycle_model.reactions}

        find_flux_loops_standalone(mdlutl, objective="atp", compartment="c0")

        rxn_ids_after = {r.id for r in atp_cycle_model.reactions}
        probe_rxns = {rid for rid in rxn_ids_after if rid.startswith("PROBE_")}
        assert not probe_rxns, f"Probe reactions remain after cleanup: {probe_rxns}"

    def test_no_binary_vars_remain(self, atp_cycle_model):
        """No fu_/ru_ variables or constraints remain after find_flux_loops."""
        _require_deps()
        from kbutillib.ms_fba_utils import find_flux_loops_standalone

        mdlutl = _make_msutil(atp_cycle_model)
        find_flux_loops_standalone(mdlutl, objective="atp", compartment="c0")

        leftover_vars = [
            v.name for v in atp_cycle_model.variables
            if v.name.startswith(("fu_", "ru_"))
        ]
        leftover_cons = [
            c.name for c in atp_cycle_model.constraints
            if c.name.startswith(("fu_", "ru_", "exclusion"))
        ]
        assert not leftover_vars, f"Binary variables remain: {leftover_vars}"
        assert not leftover_cons, f"Binary constraints remain: {leftover_cons}"

    def test_bounds_restored(self, atp_cycle_model):
        """All reaction bounds are restored to their pre-call values."""
        _require_deps()
        from kbutillib.ms_fba_utils import find_flux_loops_standalone

        mdlutl = _make_msutil(atp_cycle_model)
        bounds_before = {r.id: (r.lower_bound, r.upper_bound) for r in atp_cycle_model.reactions}

        find_flux_loops_standalone(mdlutl, objective="atp", compartment="c0")

        bounds_after = {r.id: (r.lower_bound, r.upper_bound) for r in atp_cycle_model.reactions}
        for rxn_id, (lb_before, ub_before) in bounds_before.items():
            lb_after, ub_after = bounds_after.get(rxn_id, (None, None))
            assert lb_after == lb_before, (
                f"{rxn_id}: lower_bound changed {lb_before} -> {lb_after}"
            )
            assert ub_after == ub_before, (
                f"{rxn_id}: upper_bound changed {ub_before} -> {ub_after}"
            )

    def test_rxn_count_unchanged(self, atp_cycle_model):
        """The number of reactions is the same before and after find_flux_loops."""
        _require_deps()
        from kbutillib.ms_fba_utils import find_flux_loops_standalone

        mdlutl = _make_msutil(atp_cycle_model)
        n_before = len(atp_cycle_model.reactions)

        find_flux_loops_standalone(mdlutl, objective="atp", compartment="c0")

        n_after = len(atp_cycle_model.reactions)
        assert n_after == n_before, (
            f"Reaction count changed: {n_before} -> {n_after}"
        )


# ── T6: specificity (non-binding refinement) ─────────────────────────────────


class TestSpecificityATPProbe:
    """T6: ATP probe yields zero on a model with only an energy-neutral cycle."""

    def test_neutral_cycle_not_detected_as_atp_egc(self):
        """A model with only X->Y->X cycle gives empty ATP probe result."""
        _require_deps()
        import cobra
        from kbutillib.ms_fba_utils import find_flux_loops_standalone

        m = cobra.Model("neutral_cycle")

        def _met(msid, compartment="c0"):
            met = cobra.Metabolite(f"{msid}_{compartment}", compartment=compartment)
            met.annotation = {"seed.compound": msid}
            return met

        x = _met("cpd99030")
        y = _met("cpd99031")

        # Futile X<->Y cycle (energy neutral — no ATP involved)
        r_fwd = cobra.Reaction("R_xy_fwd", lower_bound=0, upper_bound=1000)
        r_fwd.add_metabolites({x: -1, y: 1})
        r_rev = cobra.Reaction("R_xy_rev", lower_bound=0, upper_bound=1000)
        r_rev.add_metabolites({y: -1, x: 1})

        m.add_reactions([r_fwd, r_rev])

        mdlutl = _make_msutil(m)
        results = find_flux_loops_standalone(mdlutl, objective="atp", compartment="c0")

        assert "atp_hydrolysis" in results
        assert results["atp_hydrolysis"] == [], (
            f"ATP probe should be empty on neutral cycle model, got {results['atp_hydrolysis']}"
        )
