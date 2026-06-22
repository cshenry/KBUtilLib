"""Tests for the predictive-thermodynamics facade and its backends.

These tests run without any optional scientific dependency installed. The
equilibrator success path is exercised with an injected fake
``ComponentContribution`` so no ~1 GB cache download is needed; the
graceful-degradation path is exercised by *not* injecting one (the real
``equilibrator_api`` import is expected to fail in CI).

Core invariants under test:
  * Importing the subpackage never requires an optional dependency.
  * Backends never raise at construction when their dependency is absent.
  * Backends never fabricate a value: unknown -> None + a warning.
  * The dispatcher walks the priority order and falls through cleanly.
  * The toolkit exposes the ``predictive_thermo`` lazy property.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from kbutillib.predictive_thermo_utils import (
    PredictiveThermoUtils,
    PredictiveThermoUtilsImpl,
)
from kbutillib.thermo_predictors import (
    CompoundThermoEstimate,
    DGPredictorBackend,
    EquilibratorBackend,
    ModelSEEDBackend,
    ModelSEEDDBBackend,
    MolGPKBackend,
)
from kbutillib.thermo_predictors.base import BackendUnavailableError

# ── fakes ───────────────────────────────────────────────────────────────


class _FakeThermoUtils:
    """Minimal stand-in for ThermoUtils used by the ModelSEED backend."""

    def __init__(self, compounds=None, reactions=None):
        self._compounds = compounds or {}
        self._reactions = reactions or {}

    def get_compound_deltag(self, compound_id):
        if compound_id not in self._compounds:
            raise ValueError(f"Compound '{compound_id}' not found")
        return self._compounds[compound_id]

    def calculate_reaction_deltag(self, reaction_id, **kwargs):
        if reaction_id not in self._reactions:
            raise ValueError(f"Reaction '{reaction_id}' not found")
        return self._reactions[reaction_id]


class _FakeQuantity:
    def __init__(self, value):
        self._value = value

    def m_as(self, _units):
        return self._value


class _FakeMeasurement:
    def __init__(self, value, error):
        self.value = _FakeQuantity(value)
        self.error = _FakeQuantity(error)


class _FakeReaction:
    def __init__(self, balanced=True):
        self._balanced = balanced

    def is_balanced(self):
        return self._balanced


class _FakeComponentContribution:
    """Just enough of eQuilibrator's API for the success path."""

    def __init__(self, dg=-32.0, err=2.0, balanced=True):
        self.p_h = None
        self.ionic_strength = None
        self.temperature = None
        self.p_mg = None
        self._dg = dg
        self._err = err
        self._balanced = balanced

    def parse_reaction_formula(self, _formula):
        return _FakeReaction(self._balanced)

    def standard_dg_prime(self, _reaction):
        return _FakeMeasurement(self._dg, self._err)

    def get_compound(self, compound_id):
        return SimpleNamespace(id=compound_id) if compound_id != "unknown:x" else None

    def standard_dg_formation(self, _compound):
        return (-2300.0, None)


# Patch Q_ application: the fake CC has plain attributes, so _apply_conditions
# must not blow up. equilibrator_api.Q_ is imported lazily inside the backend;
# we monkeypatch it via a stub module so the success path works offline.
@pytest.fixture
def fake_equilibrator_module(monkeypatch):
    import sys
    import types

    mod = types.ModuleType("equilibrator_api")
    mod.Q_ = lambda *a, **k: SimpleNamespace(args=a)  # noqa: E731
    mod.ComponentContribution = _FakeComponentContribution
    monkeypatch.setitem(sys.modules, "equilibrator_api", mod)
    return mod


# ── import safety ───────────────────────────────────────────────────────


def test_subpackage_imports_without_optional_deps():
    import kbutillib.thermo_predictors as tp

    assert hasattr(tp, "EquilibratorBackend")
    assert hasattr(tp, "ModelSEEDBackend")


# ── graceful degradation ────────────────────────────────────────────────


def test_equilibrator_unavailable_does_not_raise_on_construction():
    be = EquilibratorBackend()
    # Without the package/cache, available is False and a reason is given.
    if not be.available:
        assert be.unavailable_reason
        with pytest.raises(BackendUnavailableError):
            be.reaction_dg_prime("r", {"kegg:C00002": -1, "kegg:C00008": 1})


def test_dgpredictor_stub_is_unavailable_and_never_fabricates():
    be = DGPredictorBackend()
    assert be.available is False
    assert "not configured" in (be.unavailable_reason or "")
    with pytest.raises(BackendUnavailableError):
        be.reaction_dg_prime("r", {"x": -1})


def test_molgpk_stub_is_unavailable_and_never_fabricates():
    be = MolGPKBackend()
    assert be.available is False
    assert "not configured" in (be.unavailable_reason or "")
    with pytest.raises(BackendUnavailableError):
        be.compound_dgf("cpd00001")


# ── ModelSEED backend (lookup adapter) ──────────────────────────────────


def test_modelseed_backend_compound_hit():
    be = ModelSEEDBackend(_FakeThermoUtils(compounds={"cpd00002": -2300.0}))
    assert be.available is True
    est = be.compound_dgf("cpd00002")
    assert est.dgf == -2300.0
    assert est.backend == "modelseed"


def test_modelseed_backend_compound_miss_returns_none_not_fabricated():
    be = ModelSEEDBackend(_FakeThermoUtils(compounds={}))
    est = be.compound_dgf("cpd99999")
    assert est.dgf is None
    assert est.warnings  # explains the miss


def test_modelseed_backend_reaction_hit():
    rxn = {
        "deltag": -30.5,
        "deltag_error": 1.2,
        "equation": "A => B",
        "warnings": [],
        "missing_compounds": [],
    }
    be = ModelSEEDBackend(_FakeThermoUtils(reactions={"rxn00001": rxn}))
    est = be.reaction_dg_prime("rxn00001", {})
    assert est.dg_prime == -30.5
    assert est.dg_prime_uncertainty == 1.2
    assert est.equation == "A => B"


def test_modelseed_backend_missing_delegate_raises():
    be = ModelSEEDBackend(None)
    assert be.available is False
    with pytest.raises(BackendUnavailableError):
        be.compound_dgf("cpd00001")


# ── equilibrator success path (injected fake CC) ────────────────────────


def test_equilibrator_reaction_success_with_injected_cc(fake_equilibrator_module):
    be = EquilibratorBackend(component_contribution=_FakeComponentContribution(dg=-32.0, err=2.0))
    est = be.reaction_dg_prime(
        "atp_hydrolysis", {"kegg:C00002": -1, "kegg:C00001": -1, "kegg:C00008": 1, "kegg:C00009": 1}
    )
    assert est.dg_prime is not None
    assert math.isclose(est.dg_prime, -32.0)
    assert math.isclose(est.dg_prime_uncertainty, 2.0)
    assert est.backend == "equilibrator"


def test_equilibrator_unbalanced_warns(fake_equilibrator_module):
    cc = _FakeComponentContribution(dg=-10.0, err=1.0, balanced=False)
    be = EquilibratorBackend(component_contribution=cc)
    est = be.reaction_dg_prime("r", {"kegg:C00002": -1, "kegg:C00008": 1})
    assert any("not atom-balanced" in w for w in est.warnings)


# ── dispatcher / facade ─────────────────────────────────────────────────


def _facade(thermo_utils=None):
    return PredictiveThermoUtils(
        thermo_utils=thermo_utils,
        config_file=False,
        token_file=None,
        kbase_token_file=None,
    )


class _UnavailableBackend:
    """Stand-in backend that is always unavailable (deterministic dispatch).

    Used so dispatch tests do not depend on whether the optional
    equilibrator-api happens to be installed in the dev environment.
    """

    def __init__(self, name):
        self.name = name
        self.capabilities = frozenset({"reaction_dg", "compound_dgf"})

    @property
    def available(self):
        return False

    @property
    def unavailable_reason(self):
        return f"{self.name} disabled for test"

    def reaction_dg_prime(self, *a, **k):
        raise BackendUnavailableError(self.unavailable_reason)

    def compound_dgf(self, *a, **k):
        raise BackendUnavailableError(self.unavailable_reason)


def _facade_only_modelseed(thermo_utils):
    """Facade with all backends above modelseed forced unavailable."""
    facade = _facade(thermo_utils)
    # Touch .backends to build the dict, then disable the upstream backends so
    # the priority walk deterministically reaches modelseed.
    facade.backends["equilibrator"] = _UnavailableBackend("equilibrator")
    facade.backends["dgpredictor"] = _UnavailableBackend("dgpredictor")
    facade.backends["modelseed_db"] = _UnavailableBackend("modelseed_db")
    return facade


def test_backend_status_reports_all_backends():
    facade = _facade(_FakeThermoUtils())
    status = facade.backend_status()
    assert set(status) == {
        "equilibrator",
        "dgpredictor",
        "modelseed_db",
        "molgpk",
        "modelseed",
    }
    assert status["modelseed"]["available"] is True
    assert status["dgpredictor"]["available"] is False
    assert status["molgpk"]["available"] is False


def test_dispatch_falls_through_to_modelseed():
    # equilibrator + dgpredictor unavailable -> modelseed answers.
    facade = _facade_only_modelseed(_FakeThermoUtils(reactions={
        "rxn00001": {"deltag": -30.0, "deltag_error": 1.0, "equation": "A=>B",
                     "warnings": [], "missing_compounds": []},
    }))
    est = facade.reaction_dg_prime("rxn00001", {"cpd00002": -1})
    assert est.dg_prime == -30.0
    assert est.backend == "modelseed"
    # provenance note recorded
    assert any("modelseed" in w for w in est.warnings)


def test_dispatch_no_backend_returns_honest_none():
    facade = _facade_only_modelseed(_FakeThermoUtils(reactions={}))  # modelseed misses
    est = facade.reaction_dg_prime("rxnZZZ", {"cpd00002": -1})
    assert est.dg_prime is None
    assert est.warnings  # lists what was attempted


def test_explicit_backend_selection():
    facade = _facade(_FakeThermoUtils(compounds={"cpd00002": -2300.0}))
    est = facade.compound_dgf("cpd00002", backend="modelseed")
    assert est.dgf == -2300.0
    assert est.backend == "modelseed"


def test_explicit_unknown_backend_raises():
    facade = _facade(_FakeThermoUtils())
    with pytest.raises(KeyError):
        facade.get_backend("does-not-exist")


def test_microspecies_routes_to_molgpk_and_degrades():
    facade = _facade(_FakeThermoUtils())
    est = facade.compound_microspecies("cpd00001")
    assert isinstance(est, CompoundThermoEstimate)
    assert est.pka_values == []
    assert any("molgpk" in w for w in est.warnings)


# ── toolkit registration ────────────────────────────────────────────────


def test_toolkit_has_predictive_thermo_attribute_without_heavy_deps():
    # The property must exist and be lazy: merely importing the toolkit and
    # constructing it must not require modelseedpy (the chain is built only on
    # first access of `predictive_thermo`).
    from kbutillib.toolkit import KBUtilLib

    assert isinstance(KBUtilLib.predictive_thermo, property)
    kit = KBUtilLib(config_file=False, token_file=None, kbase_token_file=None)
    assert kit._predictive_thermo is None  # not built yet


def test_toolkit_exposes_predictive_thermo_property():
    # The toolkit's predictive_thermo builds the `thermo` delegate, which in
    # turn lazily constructs the ModelSEED biochem chain. That chain needs the
    # heavy `modelseedpy` dependency (same convention as test_composition_smoke).
    pytest.importorskip("modelseedpy", reason="modelseedpy required for biochem chain")
    from kbutillib.toolkit import KBUtilLib

    kit = KBUtilLib(config_file=False, token_file=None, kbase_token_file=None)
    pt = kit.predictive_thermo
    assert isinstance(pt, PredictiveThermoUtilsImpl)
    # lazy: second access returns the same instance
    assert kit.predictive_thermo is pt
    # status query works through the Impl delegate
    status = pt.backend_status()
    assert "modelseed" in status


# ── ModelSEEDDBBackend (baked eQuilibrator) ──────────────────────────────


def _write_msdb_fixture(tmp_path):
    """Write a minimal Biochemistry/ dir with two TSVs the backend reads."""
    biochem = tmp_path / "Biochemistry"
    biochem.mkdir()
    compounds = biochem / "compounds.tsv"
    compounds.write_text(
        "id\tname\tdeltag\tdeltagerr\tnotes\n"
        # EQU-tagged: -548.85 kcal/mol -> -2296.39 kJ/mol
        "cpd00002\tATP\t-548.85\t0.36\tGC|EQ|EQU\n"
        # value present but NOT eQuilibrator-tagged
        "cpd99999\tFooGC\t-100.0\t1.0\tGC\n"
        # no value
        "cpd00000\tNoData\tnull\tnull\tGC\n"
    )
    reactions = biochem / "reactions.tsv"
    reactions.write_text(
        "id\tname\tdeltag\tdeltagerr\tnotes\n"
        # -3.46 kcal/mol -> -14.476 kJ/mol
        "rxn00001\tATPhydro\t-3.46\t0.05\tGCC|HB|EQC|EQU\n"
    )
    return str(biochem)


def test_msdb_backend_compound_kcal_to_kj(tmp_path):
    root = _write_msdb_fixture(tmp_path)
    backend = ModelSEEDDBBackend(biochem_root=root)
    assert backend.available is True
    est = backend.compound_dgf("cpd00002")
    assert est.dgf is not None
    assert math.isclose(est.dgf, -548.85 * 4.184, rel_tol=1e-9)
    assert math.isclose(est.dgf_uncertainty, 0.36 * 4.184, rel_tol=1e-9)
    # fixed conditions reported
    assert est.ph == 7.0
    assert est.ionic_strength == 0.25
    assert est.temperature == 298.15


def test_msdb_backend_reaction_kcal_to_kj(tmp_path):
    root = _write_msdb_fixture(tmp_path)
    backend = ModelSEEDDBBackend(biochem_root=root)
    est = backend.reaction_dg_prime("rxn00001", {"cpd00002": -1})
    assert est.dg_prime is not None
    assert math.isclose(est.dg_prime, -3.46 * 4.184, rel_tol=1e-9)


def test_msdb_backend_unknown_compound_returns_none(tmp_path):
    root = _write_msdb_fixture(tmp_path)
    backend = ModelSEEDDBBackend(biochem_root=root)
    est = backend.compound_dgf("cpd_missing")
    assert est.dgf is None
    assert est.warnings  # honest: explains why, never fabricates


def test_msdb_backend_requires_equilibrator_tag_by_default(tmp_path):
    root = _write_msdb_fixture(tmp_path)
    backend = ModelSEEDDBBackend(biochem_root=root)
    # value exists but is GC-only (no EQU/EQC/EQP) -> not returned by default
    est = backend.compound_dgf("cpd99999")
    assert est.dgf is None
    # with the gate relaxed the value is returned, provenance in warnings
    relaxed = ModelSEEDDBBackend(biochem_root=root, require_equilibrator=False)
    est2 = relaxed.compound_dgf("cpd99999")
    assert est2.dgf is not None
    assert math.isclose(est2.dgf, -100.0 * 4.184, rel_tol=1e-9)


def test_msdb_backend_unavailable_when_root_missing(tmp_path):
    backend = ModelSEEDDBBackend(biochem_root=str(tmp_path / "nope"))
    assert backend.available is False
    assert backend.unavailable_reason
    with pytest.raises(BackendUnavailableError):
        backend.compound_dgf("cpd00002")
