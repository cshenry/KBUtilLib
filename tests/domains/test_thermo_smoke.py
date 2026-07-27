"""Smoke tests for domains.thermo — verifies import paths + basic API contract.

These tests are OFFLINE — no modelseedpy/equilibrator/dgpredictor required.
They verify the class hierarchy, method presence, and abstract interface contract.
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# 1. Core classes importable from canonical domain path
# ---------------------------------------------------------------------------


def test_thermo_utils_importable() -> None:
    """ThermoUtils is importable from the canonical domain path."""
    from kbutillib.domains.thermo import ThermoUtils  # noqa: PLC0415
    assert ThermoUtils is not None
    assert callable(ThermoUtils)


def test_thermo_utils_impl_importable() -> None:
    """ThermoUtilsImpl is importable from the canonical domain path."""
    from kbutillib.domains.thermo import ThermoUtilsImpl  # noqa: PLC0415
    assert ThermoUtilsImpl is not None


def test_predictive_thermo_utils_importable() -> None:
    """PredictiveThermoUtils is importable from the canonical domain path."""
    from kbutillib.domains.thermo import PredictiveThermoUtils  # noqa: PLC0415
    assert PredictiveThermoUtils is not None


def test_predictive_thermo_utils_impl_importable() -> None:
    """PredictiveThermoUtilsImpl is importable from the canonical domain path."""
    from kbutillib.domains.thermo import PredictiveThermoUtilsImpl  # noqa: PLC0415
    assert PredictiveThermoUtilsImpl is not None


def test_thermo_backend_importable() -> None:
    """ThermoBackend is importable from the canonical domain path."""
    from kbutillib.domains.thermo import ThermoBackend  # noqa: PLC0415
    assert ThermoBackend is not None


def test_thermo_predictor_importable() -> None:
    """ThermoPredictor is importable from the thermo_predictors submodule."""
    from kbutillib.domains.thermo.thermo_predictors.base import ThermoPredictor  # noqa: PLC0415
    assert ThermoPredictor is not None


# ---------------------------------------------------------------------------
# 2. Abstract interface contract on ThermoBackend
# ---------------------------------------------------------------------------


def test_thermo_backend_has_available() -> None:
    """ThermoBackend defines abstract `available` property."""
    from kbutillib.domains.thermo import ThermoBackend  # noqa: PLC0415
    assert hasattr(ThermoBackend, "available")


def test_thermo_backend_has_unavailable_reason() -> None:
    """ThermoBackend defines abstract `unavailable_reason` property."""
    from kbutillib.domains.thermo import ThermoBackend  # noqa: PLC0415
    assert hasattr(ThermoBackend, "unavailable_reason")


def test_thermo_backend_has_compound_dgf() -> None:
    """ThermoBackend defines `compound_dgf` method."""
    from kbutillib.domains.thermo import ThermoBackend  # noqa: PLC0415
    assert hasattr(ThermoBackend, "compound_dgf")


def test_thermo_backend_has_reaction_dg_prime() -> None:
    """ThermoBackend defines `reaction_dg_prime` method."""
    from kbutillib.domains.thermo import ThermoBackend  # noqa: PLC0415
    assert hasattr(ThermoBackend, "reaction_dg_prime")


# ---------------------------------------------------------------------------
# 3. ThermoUtils structural API
# ---------------------------------------------------------------------------


def test_thermo_utils_has_calculate_reaction_deltag() -> None:
    """ThermoUtils has calculate_reaction_deltag method."""
    from kbutillib.domains.thermo import ThermoUtils  # noqa: PLC0415
    assert hasattr(ThermoUtils, "calculate_reaction_deltag")
    assert callable(ThermoUtils.calculate_reaction_deltag)


def test_thermo_utils_has_get_compound_deltag() -> None:
    """ThermoUtils has get_compound_deltag method."""
    from kbutillib.domains.thermo import ThermoUtils  # noqa: PLC0415
    assert hasattr(ThermoUtils, "get_compound_deltag")
    assert callable(ThermoUtils.get_compound_deltag)


# ---------------------------------------------------------------------------
# 4. Domain __all__ contract
# ---------------------------------------------------------------------------


def test_thermo_domain_all_contains_expected() -> None:
    """domains.thermo.__all__ contains the core public names."""
    import kbutillib.domains.thermo as thermo  # noqa: PLC0415
    expected = {"ThermoUtils", "ThermoUtilsImpl", "PredictiveThermoUtils", "PredictiveThermoUtilsImpl", "ThermoBackend"}
    assert expected.issubset(set(thermo.__all__))
