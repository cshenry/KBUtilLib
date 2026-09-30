"""Tests for ms_community_utils that need NEITHER MSCommunity NOR a solver.

These exercise the pure helpers, the dependency gate, and the composition
wrapper's unavailability behavior.  The most important test here is the
provenance gate (:class:`TestProvenanceGate`), which guards against importing
modelseedpy's superseded ``MSCommunity`` copy.
"""

import sys
import types
from pathlib import Path

import pytest

from kbutillib.core.errors import BackendUnavailableError, KBUtilLibError
from kbutillib.domains.modeling import ms_community_utils as mcu
from kbutillib.domains.modeling.ms_community_utils import (
    CommunityDependencyError,
    CommunityModel,
    CommunitySolverError,
    CommunityVisualizationError,
    _import_mscommunity,
    _mscommunity_unavailable_reason,
    _reset_mscommunity_cache,
    _unwrap,
    normalize_abundances,
    project_member_fluxes,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    """Reset the module-level import cache around every test."""
    _reset_mscommunity_cache()
    yield
    _reset_mscommunity_cache()


# ── _unwrap ─────────────────────────────────────────────────────────────────


class TestUnwrap:
    def test_returns_underlying_function_for_staticmethod(self):
        def plain(x):
            return x + 1

        sm = staticmethod(plain)
        unwrapped = _unwrap(sm)
        assert unwrapped is plain
        # And it is directly callable (the Python 3.9 hazard the helper fixes).
        assert unwrapped(1) == 2

    def test_identity_for_plain_function(self):
        def plain():
            return 42

        assert _unwrap(plain) is plain


# ── _import_mscommunity: absent ─────────────────────────────────────────────


class TestImportAbsent:
    def test_returns_none_when_package_absent(self, monkeypatch):
        # Remove any real/fake mscommunity and force the import to fail.
        monkeypatch.setitem(sys.modules, "mscommunity", None)

        # get_dependency_path yields nothing.
        import kbutillib.core.dependency_manager as dm

        monkeypatch.setattr(dm, "get_dependency_path", lambda name: None)

        assert _import_mscommunity() is None


# ── Provenance gate (the most important test) ───────────────────────────────


class TestProvenanceGate:
    def _install_superseded(self, monkeypatch):
        """Inject a fake mscommunity whose MSCommunity is modelseedpy's copy."""
        fake = types.ModuleType("mscommunity")

        class MSCommunity:
            pass

        # Pretend this class was defined in modelseedpy's superseded module.
        MSCommunity.__module__ = "modelseedpy.community.mscommunity"
        fake.MSCommunity = MSCommunity
        monkeypatch.setitem(sys.modules, "mscommunity", fake)
        return fake

    def test_import_rejects_modelseedpy_copy(self, monkeypatch):
        self._install_superseded(monkeypatch)
        assert _import_mscommunity() is None

    def test_unavailable_reason_names_superseded_copy(self, monkeypatch):
        self._install_superseded(monkeypatch)
        reason = _mscommunity_unavailable_reason()
        assert reason is not None
        assert "superseded copy" in reason

    def test_import_accepts_standalone_package(self, monkeypatch):
        fake = types.ModuleType("mscommunity")

        class MSCommunity:
            pass

        MSCommunity.__module__ = "mscommunity.mscommsim"
        fake.MSCommunity = MSCommunity
        fake.__file__ = "/nonexistent/mscommunity/__init__.py"
        monkeypatch.setitem(sys.modules, "mscommunity", fake)

        assert _import_mscommunity() is fake
        assert _mscommunity_unavailable_reason() is None


# ── normalize_abundances ────────────────────────────────────────────────────


class TestNormalizeAbundances:
    def test_normalizes_to_unit_sum(self):
        assert normalize_abundances({"a": 3, "b": 1}) == {"a": 0.75, "b": 0.25}

    def test_empty_raises_value_error(self):
        with pytest.raises(ValueError):
            normalize_abundances({})

    def test_nonpositive_total_raises_value_error(self):
        with pytest.raises(ValueError):
            normalize_abundances({"a": 0, "b": 0})


# ── project_member_fluxes ───────────────────────────────────────────────────


class TestProjectMemberFluxes:
    def test_member_projection(self):
        fluxes = {
            "rxn00001_c2": 5.0,
            "rxn00002_c1": 1.0,
            "EX_cpd00027_e0": -3.0,
            "bio3": 0.4,
        }
        result = project_member_fluxes(fluxes, member_index=2, member_biomass_id="bio3")
        assert result == {
            "rxn00001_c0": 5.0,
            "EX_cpd00027_e0": -3.0,
            "bio1": 0.4,
        }

    def test_mid_string_compartment_not_corrupted(self):
        # An id whose compartment pattern appears mid-string must be kept as-is
        # (str.replace would corrupt it; the regex anchors at the end).
        fluxes = {"rxn_c2_special": 2.0}
        result = project_member_fluxes(fluxes, member_index=1, member_biomass_id=None)
        assert result == {"rxn_c2_special": 2.0}


# ── CommunityModel.member_index ─────────────────────────────────────────────


class TestCommunityModel:
    def _model(self):
        return CommunityModel(
            mscomm=None,
            member_ids=["A", "B", "C"],
            abundances={"A": 0.5, "B": 0.3, "C": 0.2},
            source_model_ids={"A": "modelA", "B": "modelB", "C": "modelC"},
            kinetic_coeff=750.0,
        )

    def test_member_index_round_trips(self):
        cm = self._model()
        assert cm.member_index("A") == 1
        assert cm.member_index("B") == 2
        assert cm.member_index("C") == 3

    def test_member_index_unknown_raises_keyerror_listing_ids(self):
        cm = self._model()
        with pytest.raises(KeyError) as excinfo:
            cm.member_index("Z")
        # KeyError repr includes the message; assert the known ids appear.
        msg = str(excinfo.value)
        assert "A" in msg and "B" in msg and "C" in msg


# ── Exception hierarchy ─────────────────────────────────────────────────────


class TestExceptionHierarchy:
    def test_dependency_error_is_backend_unavailable(self):
        assert issubclass(CommunityDependencyError, BackendUnavailableError)

    def test_solver_error_is_kbutillib_error(self):
        assert issubclass(CommunitySolverError, KBUtilLibError)

    def test_visualization_error_is_kbutillib_error(self):
        assert issubclass(CommunityVisualizationError, KBUtilLibError)


# ── MSCommunityUtilsImpl unavailability ─────────────────────────────────────


class TestImplUnavailable:
    def test_forced_none_delegate_reports_unavailable(self, monkeypatch):
        impl = mcu.MSCommunityUtilsImpl(
            env=None, model=None, fba=None, escher=None, biochem=None
        )
        # Force the delegate to None regardless of environment.
        impl._delegate = None

        assert impl.available is False
        assert impl.unavailable_reason
        with pytest.raises(RuntimeError):
            impl.some_unknown_attribute


# ── Construction / simulation methods (no solver, no mscommunity) ────────────
#
# These exercise the guard rails on MSCommunityUtils' methods.  A bare
# MSCommunityUtils instance is built via object.__new__ so KBModelUtils' heavy
# __init__ (KBase clients, optional scientific deps) never runs -- each test
# only reaches the pure-Python guard it targets.


def _bare_util():
    """Return an un-initialized MSCommunityUtils (no KBModelUtils.__init__)."""
    return object.__new__(mcu.MSCommunityUtils)


class TestDependencyGate:
    def test_build_community_raises_dependency_error_when_absent(self, monkeypatch):
        # _import_mscommunity resolving to None must surface as a
        # CommunityDependencyError with a useful message.
        monkeypatch.setattr(mcu, "_import_mscommunity", lambda: None)
        monkeypatch.setattr(
            mcu,
            "_mscommunity_unavailable_reason",
            lambda: "the mscommunity package is not importable",
        )
        util = _bare_util()
        with pytest.raises(CommunityDependencyError) as excinfo:
            util.build_community([object()])
        assert "mscommunity" in str(excinfo.value)


class TestRunMicomSolverGate:
    def test_run_micom_raises_solver_error_naming_backends(self, monkeypatch):
        # mscommunity present, but the QP probe reports no capable backend.
        monkeypatch.setattr(mcu, "_import_mscommunity", lambda: object())
        monkeypatch.setattr(
            mcu.MSCommunityUtils, "_qp_backend_available", lambda self: False
        )
        util = _bare_util()
        with pytest.raises(CommunitySolverError) as excinfo:
            util.run_micom(comm=object(), media=None)
        msg = str(excinfo.value)
        for backend in ("gurobi", "cplex", "osqp", "hybrid"):
            assert backend in msg


class TestRunCommunityFBAOverSpecification:
    def test_min_member_growth_with_fixed_abundances_refuses(self, monkeypatch):
        # A per-member growth floor combined with caller-supplied fixed
        # abundances over-determines the system -> named refusal BEFORE solving.
        monkeypatch.setattr(mcu, "_import_mscommunity", lambda: object())

        comm = CommunityModel(
            mscomm=object(),
            member_ids=["A", "B"],
            abundances={"A": 0.5, "B": 0.5},
            source_model_ids={},
            kinetic_coeff=750.0,
            abundances_were_supplied=True,
        )
        util = _bare_util()
        with pytest.raises(CommunitySolverError) as excinfo:
            util.run_community_fba(comm, media=None, min_member_growth=0.1)
        msg = str(excinfo.value)
        # Names BOTH inputs and says dropping either resolves it.
        assert "min_member_growth" in msg
        assert "abundances" in msg
        assert "drop" in msg.lower()


class TestLoadCommunityRecovery:
    def test_raises_value_error_naming_all_routes(self, monkeypatch):
        # A model whose notes lack member_biomass_cpds, with no member_ids arg,
        # and no kbutil.community blob -> ValueError naming all three routes.
        monkeypatch.setattr(mcu, "_import_mscommunity", lambda: object())

        class _StubModel:
            notes: dict = {}

        class _StubMdlUtl:
            model = _StubModel()

        util = _bare_util()
        monkeypatch.setattr(
            mcu.MSCommunityUtils,
            "get_model",
            lambda self, id_or_ref, ws=None: _StubMdlUtl(),
        )
        with pytest.raises(ValueError) as excinfo:
            util.load_community("some/ref")
        msg = str(excinfo.value)
        assert "kbutil.community" in msg
        assert "member_ids" in msg
        assert "member_biomass_cpds" in msg


class TestPredictAbundancesRestore:
    """predict_abundances(update=False) must restore both snapshotted things."""

    def _make_stub_comm(self):
        # A fake community whose mscomm records add_metabolites calls and whose
        # abundances/biomass can be mutated by the fake predict_abundances.
        class _FakeMember:
            def __init__(self, mid):
                self.id = mid
                self.abundance = None

        class _FakeBiomass:
            def __init__(self):
                # metabolite -> coefficient (upstream shape)
                self.metabolites = {"cpd_bio_c0": 1.0, "mem_A": -0.5, "mem_B": -0.5}
                self.add_metabolites_calls = []

            def add_metabolites(self, mapping, combine=False):
                # Emulate combine=False: overwrite coefficients wholesale.
                self.add_metabolites_calls.append((dict(mapping), combine))
                self.metabolites = dict(mapping)

        class _FakeMSComm:
            def __init__(self):
                self.abundances = {"A": 0.5, "B": 0.5}
                self.members = [_FakeMember("A"), _FakeMember("B")]
                self.primary_biomass = _FakeBiomass()

            def predict_abundances(
                self,
                media=None,
                pfba=True,
                regularization=True,
                update_abundances=False,
                determinize=False,
            ):
                # Mutate BOTH the abundances mapping and the biomass stoich, the
                # way real set_abundance would, then return the new vector.
                self.abundances = {"A": 0.8, "B": 0.2}
                self.primary_biomass.metabolites = {
                    "cpd_bio_c0": 1.0,
                    "mem_A": -0.8,
                    "mem_B": -0.2,
                }
                return {"A": 0.8, "B": 0.2}

        mscomm = _FakeMSComm()
        comm = CommunityModel(
            mscomm=mscomm,
            member_ids=["A", "B"],
            abundances={"A": 0.5, "B": 0.5},
            source_model_ids={},
            kinetic_coeff=750.0,
        )
        return comm, mscomm

    def test_update_false_restores_both(self, monkeypatch):
        monkeypatch.setattr(mcu, "_import_mscommunity", lambda: object())
        util = _bare_util()
        comm, mscomm = self._make_stub_comm()

        original_abund = dict(mscomm.abundances)
        original_biomass = dict(mscomm.primary_biomass.metabolites)

        result = util.predict_abundances(comm, update=False)

        # The returned vector is upstream's prediction ...
        assert result == {"A": 0.8, "B": 0.2}
        # ... but the community was restored to its pre-call state (both things).
        assert mscomm.abundances == original_abund
        assert mscomm.primary_biomass.metabolites == original_biomass
        # The restore went through add_metabolites(..., combine=False).
        assert mscomm.primary_biomass.add_metabolites_calls
        last_mapping, combine = mscomm.primary_biomass.add_metabolites_calls[-1]
        assert combine is False
        assert last_mapping == original_biomass

    def test_update_true_does_not_restore(self, monkeypatch):
        monkeypatch.setattr(mcu, "_import_mscommunity", lambda: object())
        util = _bare_util()
        comm, mscomm = self._make_stub_comm()

        result = util.predict_abundances(comm, update=True)

        # update=True keeps upstream's mutation: NO restore.
        assert result == {"A": 0.8, "B": 0.2}
        assert mscomm.abundances == {"A": 0.8, "B": 0.2}
        assert mscomm.primary_biomass.metabolites == {
            "cpd_bio_c0": 1.0,
            "mem_A": -0.8,
            "mem_B": -0.2,
        }
        # No restore call was made.
        assert mscomm.primary_biomass.add_metabolites_calls == []


# ── Marker strings vs. upstream literal text ─────────────────────────────────


class TestMarkerStrings:
    """The stdout markers must match upstream's literal print() text.

    ms_community_utils scans MSCommunity's stdout for these substrings to detect
    that kinetic constraints were silently disabled (or that a community does not
    grow).  If upstream rewords those prints, this test fails loudly rather than
    the flag silently ceasing to be set.  We assert the markers against the
    literal source lines in mscommunity/mscommsim.py -- located via
    dependencies.yaml -- and SKIP when the checkout is not present (the markers
    still can't drift undetected wherever the source IS available, e.g. CI).
    """

    def _mscommsim_source(self):
        import kbutillib.core.dependency_manager as dm

        path = dm.get_dependency_path("mscommunity")
        if path is None:
            pytest.skip("mscommunity checkout not resolvable via dependencies.yaml")
        src = Path(path) / "mscommunity" / "mscommsim.py"
        if not src.is_file():
            pytest.skip(f"mscommsim.py not found at {src}")
        return src.read_text()

    def test_kinetics_marker_matches_upstream(self):
        source = self._mscommsim_source()
        assert mcu.KINETICS_RELAXED_MARKER in source
        # Guard the exact print the scanner keys on.
        assert 'print(f"Kinetic constraints disabled for' in source

    def test_no_growth_marker_matches_upstream(self):
        source = self._mscommsim_source()
        assert mcu.NO_GROWTH_MARKER in source
