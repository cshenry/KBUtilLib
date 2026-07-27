"""Smoke tests for domains.cheminformatics — verifies import paths + basic API contract.

These tests are OFFLINE — no pickaxe/retrorules packages required for import-level checks.
Backend classes raise BackendUnavailableError at runtime when deps are missing.
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# 1. Core classes importable from canonical domain path
# ---------------------------------------------------------------------------


def test_backend_unavailable_error_importable() -> None:
    """BackendUnavailableError is importable from the canonical domain path."""
    from kbutillib.domains.cheminformatics import BackendUnavailableError  # noqa: PLC0415
    assert BackendUnavailableError is not None
    assert issubclass(BackendUnavailableError, RuntimeError)


def test_pickaxe_backend_importable() -> None:
    """PickaxeBackend is importable from the canonical domain path."""
    from kbutillib.domains.cheminformatics import PickaxeBackend  # noqa: PLC0415
    assert PickaxeBackend is not None


def test_retrorules_backend_importable() -> None:
    """RetroRulesBackend is importable from the canonical domain path."""
    from kbutillib.domains.cheminformatics import RetroRulesBackend  # noqa: PLC0415
    assert RetroRulesBackend is not None


def test_expansion_backend_importable() -> None:
    """ExpansionBackend is importable from the canonical domain path."""
    from kbutillib.domains.cheminformatics import ExpansionBackend  # noqa: PLC0415
    assert ExpansionBackend is not None


def test_expansion_result_importable() -> None:
    """ExpansionResult is importable from the canonical domain path."""
    from kbutillib.domains.cheminformatics import ExpansionResult  # noqa: PLC0415
    assert ExpansionResult is not None


def test_predicted_compound_importable() -> None:
    """PredictedCompound is importable from the canonical domain path."""
    from kbutillib.domains.cheminformatics import PredictedCompound  # noqa: PLC0415
    assert PredictedCompound is not None


def test_predicted_reaction_importable() -> None:
    """PredictedReaction is importable from the canonical domain path."""
    from kbutillib.domains.cheminformatics import PredictedReaction  # noqa: PLC0415
    assert PredictedReaction is not None


# ---------------------------------------------------------------------------
# 2. ExpansionResult structural API (data class — no deps needed)
# ---------------------------------------------------------------------------


def test_expansion_result_has_to_dict() -> None:
    """ExpansionResult has a to_dict method."""
    from kbutillib.domains.cheminformatics import ExpansionResult  # noqa: PLC0415
    assert hasattr(ExpansionResult, "to_dict")
    assert callable(ExpansionResult.to_dict)


def test_expansion_result_has_is_expanded() -> None:
    """ExpansionResult has is_expanded attribute."""
    from kbutillib.domains.cheminformatics import ExpansionResult  # noqa: PLC0415
    assert hasattr(ExpansionResult, "is_expanded")


def test_expansion_result_has_n_compounds() -> None:
    """ExpansionResult has n_compounds attribute."""
    from kbutillib.domains.cheminformatics import ExpansionResult  # noqa: PLC0415
    assert hasattr(ExpansionResult, "n_compounds")


# ---------------------------------------------------------------------------
# 3. BackendUnavailableError is a RuntimeError subclass (offline contract)
# ---------------------------------------------------------------------------


def test_backend_unavailable_error_is_runtime_error() -> None:
    """BackendUnavailableError is a subclass of RuntimeError."""
    from kbutillib.domains.cheminformatics.base import BackendUnavailableError  # noqa: PLC0415
    assert issubclass(BackendUnavailableError, RuntimeError)


def test_backend_unavailable_error_instantiable() -> None:
    """BackendUnavailableError can be raised and caught as RuntimeError."""
    from kbutillib.domains.cheminformatics.base import BackendUnavailableError  # noqa: PLC0415
    exc = BackendUnavailableError("pickaxe not installed")
    assert isinstance(exc, RuntimeError)
    assert "pickaxe" in str(exc)


# ---------------------------------------------------------------------------
# 4. Domain __all__ contract
# ---------------------------------------------------------------------------


def test_cheminformatics_domain_all_contains_expected() -> None:
    """domains.cheminformatics.__all__ contains the core public names."""
    import kbutillib.domains.cheminformatics as chem  # noqa: PLC0415
    expected = {"BackendUnavailableError", "PickaxeBackend", "RetroRulesBackend", "ExpansionResult"}
    assert expected.issubset(set(chem.__all__))
