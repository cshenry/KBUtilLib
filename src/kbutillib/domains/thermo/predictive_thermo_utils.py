"""Predictive thermodynamic utilities — dispatcher facade.

Dispatches deltaG / thermodynamic prediction requests to one of several
backends in priority order:

    1. equilibrator  (preferred; most accurate group-contribution)
    2. dgpredictor   (ML-based)
    3. molgpka       (for pKa-adjusted species)
    4. modelseed     (stored ModelSEED values; the legacy fallback)

Usage::

    from kbutillib.domains.thermo.predictive_thermo_utils import (
        PredictiveThermoUtils,
        PredictiveThermoUtilsImpl,
    )
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from kbutillib.shared_env_utils import SharedEnvUtils

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Backend order (class names; resolved lazily so missing deps don't crash)
# ---------------------------------------------------------------------------
_BACKEND_ORDER: List[str] = [
    "equilibrator",
    "dgpredictor",
    "molgpka",
    "modelseed",
]


class PredictiveThermoUtils(SharedEnvUtils):
    """Dispatcher facade over multiple thermodynamic prediction backends.

    Iterates through available backends in priority order and delegates
    deltaG / thermodynamic queries to the first one that can handle the
    compound or reaction.
    """

    def __init__(self, **kwargs: Any) -> None:
        """Initialise PredictiveThermoUtils.

        Args:
            **kwargs: Forwarded to SharedEnvUtils.
        """
        super().__init__(**kwargs)
        self._backends: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Backend loading
    # ------------------------------------------------------------------

    def _get_backend(self, name: str) -> Optional[Any]:
        """Return a backend instance, loading it lazily on first access.

        Args:
            name: Backend identifier (``equilibrator``, ``dgpredictor``,
                ``molgpka``, ``modelseed``).

        Returns:
            Backend instance or ``None`` if the backend is unavailable.
        """
        if name in self._backends:
            return self._backends[name]
        try:
            from kbutillib.domains.thermo.thermo_predictors import (  # noqa: PLC0415
                get_backend,
            )
            backend = get_backend(name)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Predictive-thermo backend %r unavailable: %s", name, exc)
            backend = None
        self._backends[name] = backend
        return backend

    @property
    def available_backends(self) -> List[str]:
        """Return names of backends that successfully initialise."""
        return [
            name
            for name in _BACKEND_ORDER
            if self._get_backend(name) is not None
        ]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def predict_compound_deltag(
        self,
        compound_id: str,
        *,
        ph: float = 7.0,
        ionic_strength: float = 0.1,
        temperature: float = 298.15,
    ) -> Optional[float]:
        """Predict standard Gibbs free energy of formation for a compound.

        Tries each backend in priority order and returns the first result.

        Args:
            compound_id: ModelSEED or KEGG compound identifier.
            ph: pH (default 7.0).
            ionic_strength: Ionic strength in mol/L (default 0.1).
            temperature: Temperature in Kelvin (default 298.15 = 25 °C).

        Returns:
            Predicted deltaGf in kJ/mol, or ``None`` if no backend can
            estimate the value.
        """
        for name in _BACKEND_ORDER:
            backend = self._get_backend(name)
            if backend is None:
                continue
            try:
                value = backend.predict_compound_deltag(
                    compound_id,
                    ph=ph,
                    ionic_strength=ionic_strength,
                    temperature=temperature,
                )
                if value is not None:
                    return value
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "Backend %r failed for compound %r: %s", name, compound_id, exc
                )
        return None

    def predict_reaction_deltag(
        self,
        reaction_id: str,
        *,
        ph: float = 7.0,
        ionic_strength: float = 0.1,
        temperature: float = 298.15,
        require_all_compounds: bool = False,
    ) -> Dict[str, Any]:
        """Predict standard Gibbs free energy change for a reaction.

        Args:
            reaction_id: ModelSEED reaction identifier.
            ph: pH (default 7.0).
            ionic_strength: Ionic strength in mol/L (default 0.1).
            temperature: Temperature in Kelvin (default 298.15 = 25 °C).
            require_all_compounds: If ``True``, raise ``ValueError`` when
                no backend can estimate deltaG.

        Returns:
            Dictionary with keys ``deltag`` (float or ``None``),
            ``backend`` (str or ``None``), ``reaction_id`` (str), and
            ``warnings`` (list of str).

        Raises:
            ValueError: If ``require_all_compounds`` is ``True`` and no
                backend succeeds.
        """
        result: Dict[str, Any] = {
            "deltag": None,
            "backend": None,
            "reaction_id": reaction_id,
            "warnings": [],
        }
        for name in _BACKEND_ORDER:
            backend = self._get_backend(name)
            if backend is None:
                continue
            try:
                value = backend.predict_reaction_deltag(
                    reaction_id,
                    ph=ph,
                    ionic_strength=ionic_strength,
                    temperature=temperature,
                )
                if value is not None:
                    result["deltag"] = value
                    result["backend"] = name
                    return result
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "Backend %r failed for reaction %r: %s", name, reaction_id, exc
                )
        if require_all_compounds and result["deltag"] is None:
            raise ValueError(
                f"No available backend could predict deltaG for reaction '{reaction_id}'"
            )
        if result["deltag"] is None:
            result["warnings"].append(
                "No backend could estimate deltaG; all backends failed or are unavailable"
            )
        return result


# ---------------------------------------------------------------------------
# Composition-based implementation
# ---------------------------------------------------------------------------


class PredictiveThermoUtilsImpl:
    """Composition-based predictive thermodynamic utilities.

    Wraps an internal :class:`PredictiveThermoUtils` instance, delegating
    all attribute access so callers can use it identically.
    """

    def __init__(self, env: SharedEnvUtils, biochem: Any = None, **kwargs: Any) -> None:
        """Initialise PredictiveThermoUtilsImpl.

        Args:
            env: Shared environment / config instance.
            biochem: Optional biochemistry utils instance (used by the
                ModelSEED backend).
            **kwargs: Additional keyword arguments.
        """
        self._env = env
        self._biochem = biochem
        _kw: Dict[str, Any] = {
            "config_file": False,
            "token_file": None,
            "kbase_token_file": None,
        }
        _kw.update(kwargs)
        self._delegate = PredictiveThermoUtils(**_kw)

    @property
    def env(self) -> SharedEnvUtils:
        """Return the shared environment instance."""
        return self._env

    @property
    def biochem(self) -> Any:
        """Return the biochemistry utils instance."""
        return self._biochem

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)
