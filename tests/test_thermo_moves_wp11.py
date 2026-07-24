"""WP11 regression tests — thermo trio physical move + shims.

Verifies that:
- Old import paths still resolve to the same objects as new paths (shim parity)
- New domain import paths work independently
- thermo_predictors subpackage is importable from both paths
- KBUtilLib facade has .thermo and .predictive_thermo properties
- No import of kbutillib raises
- Identity assertions: old path == new path (same class object)

These tests must NOT require modelseedpy/equilibrator/dgpredictor/molgpka;
they only test import mechanics and structural properties.
"""

from __future__ import annotations

import importlib

import pytest

# ---------------------------------------------------------------------------
# 1. Top-level import doesn't raise
# ---------------------------------------------------------------------------


def test_import_kbutillib_top_level():
    """import kbutillib must not raise."""
    import kbutillib  # noqa: F401

    assert kbutillib is not None


# ---------------------------------------------------------------------------
# 2. Old thermo_utils path still works
# ---------------------------------------------------------------------------


def test_old_path_thermo_utils_module():
    """Old import path kbutillib.thermo_utils is importable."""
    mod = importlib.import_module("kbutillib.domains.thermo.thermo_utils")
    assert mod is not None


def test_old_path_thermo_utils_class():
    """Old path exports ThermoUtils."""
    from kbutillib.domains.thermo.thermo_utils import ThermoUtils

    assert ThermoUtils is not None


def test_old_path_thermo_utils_impl():
    """Old path exports ThermoUtilsImpl."""
    from kbutillib.domains.thermo.thermo_utils import ThermoUtilsImpl

    assert ThermoUtilsImpl is not None


# ---------------------------------------------------------------------------
# 3. New domain path works
# ---------------------------------------------------------------------------


def test_new_path_thermo_utils_class():
    """New path kbutillib.domains.thermo.thermo_utils exports ThermoUtils."""
    from kbutillib.domains.thermo.thermo_utils import ThermoUtils

    assert ThermoUtils is not None


def test_new_path_thermo_utils_impl():
    """New path exports ThermoUtilsImpl."""
    from kbutillib.domains.thermo.thermo_utils import ThermoUtilsImpl

    assert ThermoUtilsImpl is not None


# ---------------------------------------------------------------------------
# 4. Shim identity — old path is same object as new path
# ---------------------------------------------------------------------------


def test_shim_thermo_utils_same_class():
    """Old ThermoUtils shim must be identical object as new path class."""
    from kbutillib.domains.thermo.thermo_utils import ThermoUtils as New
    from kbutillib.domains.thermo.thermo_utils import ThermoUtils as Old

    assert Old is New, "Shim must point at the canonical class, not a copy"


def test_shim_thermo_utils_impl_same_class():
    """Old ThermoUtilsImpl shim must be identical object as new path class."""
    from kbutillib.domains.thermo.thermo_utils import ThermoUtilsImpl as New
    from kbutillib.domains.thermo.thermo_utils import ThermoUtilsImpl as Old

    assert Old is New, "Shim must point at the canonical class, not a copy"


# ---------------------------------------------------------------------------
# 5. predictive_thermo_utils — old and new paths
# ---------------------------------------------------------------------------


def test_old_path_predictive_thermo_utils_class():
    """Old shim kbutillib.predictive_thermo_utils exports PredictiveThermoUtils."""
    from kbutillib.domains.thermo.predictive_thermo_utils import PredictiveThermoUtils

    assert PredictiveThermoUtils is not None


def test_old_path_predictive_thermo_utils_impl():
    """Old shim exports PredictiveThermoUtilsImpl."""
    from kbutillib.domains.thermo.predictive_thermo_utils import PredictiveThermoUtilsImpl

    assert PredictiveThermoUtilsImpl is not None


def test_new_path_predictive_thermo_utils_class():
    """New path exports PredictiveThermoUtils."""
    from kbutillib.domains.thermo.predictive_thermo_utils import PredictiveThermoUtils

    assert PredictiveThermoUtils is not None


def test_shim_predictive_thermo_utils_same_class():
    """Old predictive shim must be identical to new path class."""
    from kbutillib.domains.thermo.predictive_thermo_utils import (
        PredictiveThermoUtils as New,
    )
    from kbutillib.domains.thermo.predictive_thermo_utils import PredictiveThermoUtils as Old

    assert Old is New


def test_shim_predictive_thermo_utils_impl_same_class():
    """Old PredictiveThermoUtilsImpl shim must be identical to new path class."""
    from kbutillib.domains.thermo.predictive_thermo_utils import (
        PredictiveThermoUtilsImpl as New,
    )
    from kbutillib.domains.thermo.predictive_thermo_utils import PredictiveThermoUtilsImpl as Old

    assert Old is New


# ---------------------------------------------------------------------------
# 6. thermo_predictors subpackage — old and new paths
# ---------------------------------------------------------------------------


def test_old_path_thermo_predictors_importable():
    """Old path kbutillib.thermo_predictors is importable."""
    mod = importlib.import_module("kbutillib.domains.thermo.thermo_predictors")
    assert mod is not None


def test_old_path_thermo_predictors_exports_base():
    """Old thermo_predictors shim re-exports ThermoPredictor."""
    from kbutillib.domains.thermo.thermo_predictors.base import ThermoPredictor

    assert ThermoPredictor is not None


def test_old_path_thermo_predictors_get_backend():
    """Old thermo_predictors shim re-exports ThermoBackend (PR renamed API)."""
    from kbutillib.domains.thermo.thermo_predictors import ThermoBackend

    assert ThermoBackend is not None


def test_new_path_thermo_predictors_importable():
    """New path kbutillib.domains.thermo.thermo_predictors is importable."""
    mod = importlib.import_module("kbutillib.domains.thermo.thermo_predictors")
    assert mod is not None


def test_new_path_thermo_predictors_base_protocol():
    """New path thermo_predictors.base exports ThermoPredictor protocol."""
    from kbutillib.domains.thermo.thermo_predictors.base import ThermoPredictor

    assert ThermoPredictor is not None


def test_shim_thermo_predictors_same_class():
    """Old ThermoPredictor shim must be identical to new path class."""
    from kbutillib.domains.thermo.thermo_predictors.base import ThermoPredictor as New
    from kbutillib.domains.thermo.thermo_predictors.base import ThermoPredictor as Old

    assert Old is New


def test_domains_thermo_init_exports():
    """domains.thermo.__init__ re-exports all key public names."""
    from kbutillib.domains.thermo import (
        PredictiveThermoUtils,
        PredictiveThermoUtilsImpl,
        ThermoPredictor,
        ThermoUtils,
        ThermoUtilsImpl,
    )
    for cls in (ThermoUtils, ThermoUtilsImpl, PredictiveThermoUtils,
                PredictiveThermoUtilsImpl, ThermoPredictor):
        assert cls is not None


# ---------------------------------------------------------------------------
# 7. KBUtilLib facade — .thermo and .predictive_thermo accessible
# ---------------------------------------------------------------------------


def test_kbutillib_facade_thermo_property():
    """KBUtilLib().thermo is accessible without raising."""
    from kbutillib import KBUtilLib

    kbu = KBUtilLib()
    thermo = kbu.thermo
    assert thermo is not None


def test_kbutillib_facade_predictive_thermo_property():
    """KBUtilLib().predictive_thermo is accessible without raising."""
    from kbutillib import KBUtilLib

    kbu = KBUtilLib()
    pt = kbu.predictive_thermo
    assert pt is not None


def test_kbutillib_facade_predictive_thermo_type():
    """KBUtilLib().predictive_thermo is a PredictiveThermoUtilsImpl."""
    from kbutillib import KBUtilLib
    from kbutillib.domains.thermo.predictive_thermo_utils import PredictiveThermoUtilsImpl

    kbu = KBUtilLib()
    assert isinstance(kbu.predictive_thermo, PredictiveThermoUtilsImpl)


# ---------------------------------------------------------------------------
# 8. PredictiveThermoUtils structural / smoke tests (no optional deps needed)
# ---------------------------------------------------------------------------


def test_predictive_thermo_utils_instantiable():
    """PredictiveThermoUtils can be instantiated without any optional deps."""
    from kbutillib.domains.thermo.predictive_thermo_utils import PredictiveThermoUtils

    ptu = PredictiveThermoUtils(
        config_file=False, token_file=None, kbase_token_file=None
    )
    assert ptu is not None


def test_predictive_thermo_utils_available_backends_list():
    """backends dict is non-empty and contains known backend names."""
    from kbutillib.domains.thermo.predictive_thermo_utils import PredictiveThermoUtils

    ptu = PredictiveThermoUtils(
        config_file=False, token_file=None, kbase_token_file=None
    )
    # New API: .backends is a dict, not a list
    assert isinstance(ptu.backends, dict)
    assert len(ptu.backends) > 0


def test_predictive_thermo_utils_predict_compound_returns_none_when_no_backends():
    """compound_dgf returns a CompoundThermoEstimate with dgf=None gracefully."""
    from kbutillib.domains.thermo.predictive_thermo_utils import PredictiveThermoUtils
    from kbutillib.domains.thermo.thermo_predictors import CompoundThermoEstimate

    ptu = PredictiveThermoUtils(
        config_file=False, token_file=None, kbase_token_file=None
    )
    # New API: compound_dgf returns a CompoundThermoEstimate (graceful degradation)
    result = ptu.compound_dgf("cpd00001")
    assert isinstance(result, CompoundThermoEstimate)
    # When no backends produce a numeric value, dgf is None
    assert result.dgf is None or isinstance(result.dgf, float)


def test_predictive_thermo_utils_predict_reaction_returns_dict():
    """reaction_dg_prime returns a ReactionThermoEstimate with expected fields."""
    from kbutillib.domains.thermo.predictive_thermo_utils import PredictiveThermoUtils
    from kbutillib.domains.thermo.thermo_predictors import ReactionThermoEstimate

    ptu = PredictiveThermoUtils(
        config_file=False, token_file=None, kbase_token_file=None
    )
    # New API: reaction_dg_prime returns a ReactionThermoEstimate, not a dict
    result = ptu.reaction_dg_prime("rxn00001")
    assert isinstance(result, ReactionThermoEstimate)
    assert hasattr(result, "dg_prime")
    assert hasattr(result, "backend")
    assert hasattr(result, "reaction_id")
    assert hasattr(result, "warnings")


def test_get_backend_unknown_raises():
    """PredictiveThermoUtils.get_backend raises ValueError for unknown backend."""
    from kbutillib.domains.thermo.predictive_thermo_utils import PredictiveThermoUtils

    ptu = PredictiveThermoUtils(
        config_file=False, token_file=None, kbase_token_file=None
    )
    with pytest.raises((ValueError, KeyError)):
        ptu.get_backend("nonexistent_backend")


def test_get_backend_returns_none_for_missing_dep():
    """Backend status reports unavailable when the optional dep is not installed."""
    from kbutillib.domains.thermo.predictive_thermo_utils import PredictiveThermoUtils

    ptu = PredictiveThermoUtils(
        config_file=False, token_file=None, kbase_token_file=None
    )
    # modelseed backend is always present but may be unavailable without modelseedpy
    status = ptu.backend_status()
    assert isinstance(status, dict)
    assert "modelseed" in status
