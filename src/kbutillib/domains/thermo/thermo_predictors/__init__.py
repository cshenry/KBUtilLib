"""Thermodynamic predictor backends for KBUtilLib.

Each backend implements the :class:`ThermoPredictor` protocol defined in
``base.py``.  Use :func:`get_backend` to obtain a lazily-initialised
backend instance by name.

Available backends (in dispatch priority order):

* ``equilibrator``  — eQuilibrator group-contribution method
* ``dgpredictor``   — ML-based deltaG predictor
* ``molgpka``       — pKa-adjusted species energies
* ``modelseed``     — stored ModelSEED deltaG values (legacy fallback)
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .base import ThermoPredictor

logger = logging.getLogger(__name__)

__all__ = [
    "ThermoPredictor",
    "get_backend",
]

# ---------------------------------------------------------------------------
# Backend factory
# ---------------------------------------------------------------------------

_BACKEND_REGISTRY: dict[str, Any] = {}


def get_backend(name: str) -> Optional["ThermoPredictor"]:
    """Return a backend instance by name, loading it lazily.

    Args:
        name: One of ``"equilibrator"``, ``"dgpredictor"``,
            ``"molgpka"``, ``"modelseed"``.

    Returns:
        Backend instance implementing :class:`ThermoPredictor`, or
        ``None`` if the backend's optional dependency is missing.

    Raises:
        ValueError: If *name* is not a known backend identifier.
    """
    known = {"equilibrator", "dgpredictor", "molgpka", "modelseed"}
    if name not in known:
        raise ValueError(f"Unknown backend {name!r}; choose from {sorted(known)}")

    if name in _BACKEND_REGISTRY:
        return _BACKEND_REGISTRY[name]

    backend: Optional[ThermoPredictor] = None
    try:
        if name == "equilibrator":
            from .equilibrator_backend import EquilibratorBackend  # noqa: PLC0415
            backend = EquilibratorBackend()
        elif name == "dgpredictor":
            from .dgpredictor_backend import DGPredictorBackend  # noqa: PLC0415
            backend = DGPredictorBackend()
        elif name == "molgpka":
            from .molgpka_backend import MolgpkaBackend  # noqa: PLC0415
            backend = MolgpkaBackend()
        elif name == "modelseed":
            from .modelseed_backend import ModelseedBackend  # noqa: PLC0415
            backend = ModelseedBackend()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Thermo backend %r unavailable: %s", name, exc)

    _BACKEND_REGISTRY[name] = backend
    return backend
