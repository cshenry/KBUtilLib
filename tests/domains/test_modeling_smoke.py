"""Smoke tests for domains.modeling — verifies import paths + basic API contract.

These tests are OFFLINE — no cobra/modelseedpy required for the
model_directionality and model_helpers submodules.
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# 1. Submodule-level imports from canonical domain path
# ---------------------------------------------------------------------------


def test_model_directionality_submodule_importable() -> None:
    """domains.modeling.model_directionality is importable."""
    from kbutillib.domains.modeling import model_directionality  # noqa: PLC0415
    assert model_directionality is not None


def test_model_helpers_submodule_importable() -> None:
    """domains.modeling.model_helpers is importable."""
    from kbutillib.domains.modeling import model_helpers  # noqa: PLC0415
    assert model_helpers is not None


def test_model_standardization_utils_importable() -> None:
    """domains.modeling.model_standardization_utils is importable."""
    from kbutillib.domains.modeling import model_standardization_utils  # noqa: PLC0415
    assert model_standardization_utils is not None


# ---------------------------------------------------------------------------
# 2. model_directionality constants and callables (no deps)
# ---------------------------------------------------------------------------


def test_direction_conversion_is_dict() -> None:
    """model_directionality.direction_conversion is a dict."""
    from kbutillib.domains.modeling.model_directionality import direction_conversion  # noqa: PLC0415
    assert isinstance(direction_conversion, dict)
    assert len(direction_conversion) > 0


def test_direction_conversion_has_forward() -> None:
    """direction_conversion maps 'forward' to '>'."""
    from kbutillib.domains.modeling.model_directionality import direction_conversion  # noqa: PLC0415
    assert direction_conversion["forward"] == ">"


def test_direction_conversion_has_reverse() -> None:
    """direction_conversion maps 'reverse' to '<'."""
    from kbutillib.domains.modeling.model_directionality import direction_conversion  # noqa: PLC0415
    assert direction_conversion["reverse"] == "<"


def test_directionality_from_bounds_callable() -> None:
    """directionality_from_bounds is a callable function."""
    from kbutillib.domains.modeling.model_directionality import directionality_from_bounds  # noqa: PLC0415
    assert callable(directionality_from_bounds)


def test_combine_directionality_signals_callable() -> None:
    """combine_directionality_signals is a callable function."""
    from kbutillib.domains.modeling.model_directionality import combine_directionality_signals  # noqa: PLC0415
    assert callable(combine_directionality_signals)


def test_directionality_from_bounds_forward() -> None:
    """directionality_from_bounds with a forward-only reaction returns a string."""
    from kbutillib.domains.modeling.model_directionality import directionality_from_bounds  # noqa: PLC0415
    # directionality_from_bounds takes a Reaction-like object with .lower_bound/.upper_bound
    class _Rxn:
        lower_bound = 0.0
        upper_bound = 1000.0
    result = directionality_from_bounds(_Rxn())
    assert isinstance(result, str)
    assert len(result) > 0


def test_directionality_from_bounds_reversible() -> None:
    """directionality_from_bounds with a reversible reaction returns a string."""
    from kbutillib.domains.modeling.model_directionality import directionality_from_bounds  # noqa: PLC0415
    class _Rxn:
        lower_bound = -1000.0
        upper_bound = 1000.0
    result = directionality_from_bounds(_Rxn())
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# 3. model_helpers constants (no deps)
# ---------------------------------------------------------------------------


def test_model_helpers_compartment_types_is_dict() -> None:
    """model_helpers.compartment_types is a dict."""
    from kbutillib.domains.modeling import model_helpers  # noqa: PLC0415
    assert isinstance(model_helpers.compartment_types, dict)
    assert len(model_helpers.compartment_types) > 0


def test_model_helpers_compartment_types_has_cytosol() -> None:
    """model_helpers.compartment_types has 'cytosol' key."""
    from kbutillib.domains.modeling import model_helpers  # noqa: PLC0415
    assert "cytosol" in model_helpers.compartment_types


# ---------------------------------------------------------------------------
# 4. ModelStandardizationUtils structural API
# ---------------------------------------------------------------------------


def test_model_standardization_utils_class_importable() -> None:
    """ModelStandardizationUtils is importable."""
    from kbutillib.domains.modeling.model_standardization_utils import ModelStandardizationUtils  # noqa: PLC0415
    assert ModelStandardizationUtils is not None
    assert callable(ModelStandardizationUtils)


def test_model_standardization_utils_has_model_standardization() -> None:
    """ModelStandardizationUtils defines model_standardization method."""
    from kbutillib.domains.modeling.model_standardization_utils import ModelStandardizationUtils  # noqa: PLC0415
    assert hasattr(ModelStandardizationUtils, "model_standardization")
    assert callable(ModelStandardizationUtils.model_standardization)
