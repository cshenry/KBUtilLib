"""Tests for ms_community_utils that need NEITHER MSCommunity NOR a solver.

These exercise the pure helpers, the dependency gate, and the composition
wrapper's unavailability behavior.  The most important test here is the
provenance gate (:class:`TestProvenanceGate`), which guards against importing
modelseedpy's superseded ``MSCommunity`` copy.
"""

import sys
import types

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
