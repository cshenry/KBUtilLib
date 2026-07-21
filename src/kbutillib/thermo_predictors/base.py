"""Backend protocol and result types for predictive thermodynamics.

These definitions deliberately depend only on the Python standard library so
that importing :mod:`kbutillib.thermo_predictors` never pulls in a heavy or
optional scientific dependency. Concrete backends import their dependencies
lazily inside their own modules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Protocol, runtime_checkable


class BackendUnavailableError(RuntimeError):
    """Raised when a thermodynamic backend is asked to compute a value but its
    dependency or external tool is not installed/configured.

    Callers that prefer graceful degradation should check
    :attr:`ThermoBackend.available` first, or use
    :class:`kbutillib.predictive_thermo_utils.PredictiveThermoUtils`, which
    catches this and tries the next backend.
    """


def dependency_repo_path(dep_name: str) -> Optional[str]:
    """Resolve a research-repo backend path from KBUtilLib's DependencyManager.

    Lets the dGPredictor / molGPK backends find their source checkout when it is
    declared in ``dependencies.yaml`` (the same mechanism used for ModelSEEDpy /
    ModelSEEDDatabase / cobrakbase) without the user having to also set a
    ``thermo.*.repo_path`` config key or an environment variable.

    Returns the absolute path as a string if the dependency is registered and
    present on disk, else ``None``. Import + lookup are best-effort and never
    raise (a missing/empty dependencies.yaml simply yields ``None``).
    """
    try:
        from ..dependency_manager import get_dependency_path

        path = get_dependency_path(dep_name)
        return str(path) if path else None
    except Exception:  # pragma: no cover - best-effort resolver
        return None


# Sentinel used throughout to flag "value genuinely not known" as distinct from
# a numeric zero. Backends must never substitute a fabricated number for an
# unknown value.
UNKNOWN: None = None


@dataclass
class CompoundThermoEstimate:
    """A backend's estimate of a compound's formation thermodynamics.

    All energies are in kJ/mol. ``None`` means "not estimated by this backend";
    it is never a stand-in for a real value.

    Attributes:
        units: Machine-readable energy unit for ``dgf`` / ``dgf_uncertainty``
            (default ``"kJ/mol"``). ``pka_values`` are dimensionless and not
            covered by this field.
    """

    compound_id: str
    backend: str
    dgf: Optional[float] = None
    dgf_uncertainty: Optional[float] = None
    pka_values: List[float] = field(default_factory=list)
    major_microspecies: Optional[str] = None
    ph: Optional[float] = None
    ionic_strength: Optional[float] = None
    temperature: Optional[float] = None
    units: str = "kJ/mol"
    warnings: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Return a plain JSON-serializable dict representation."""
        return {
            "compound_id": self.compound_id,
            "backend": self.backend,
            "dgf": self.dgf,
            "dgf_uncertainty": self.dgf_uncertainty,
            "units": self.units,
            "pka_values": list(self.pka_values),
            "major_microspecies": self.major_microspecies,
            "ph": self.ph,
            "ionic_strength": self.ionic_strength,
            "temperature": self.temperature,
            "warnings": list(self.warnings),
        }


@dataclass
class ReactionThermoEstimate:
    """A backend's estimate of a reaction's transformed Gibbs free energy.

    ``dg_prime`` is the standard transformed Gibbs energy of reaction
    (delta_r G'^0) at the given conditions, in kJ/mol. ``None`` means the
    backend could not estimate it (e.g. a participating compound was
    unidentifiable); it is never a stand-in for a real value.

    Attributes:
        units: Machine-readable energy unit for ``dg_prime`` /
            ``dg_prime_uncertainty`` (default ``"kJ/mol"``).
    """

    reaction_id: str
    backend: str
    dg_prime: Optional[float] = None
    dg_prime_uncertainty: Optional[float] = None
    ph: Optional[float] = None
    ionic_strength: Optional[float] = None
    temperature: Optional[float] = None
    p_mg: Optional[float] = None
    equation: Optional[str] = None
    missing_compounds: List[str] = field(default_factory=list)
    units: str = "kJ/mol"
    warnings: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_estimated(self) -> bool:
        """True if a numeric free-energy value was produced."""
        return self.dg_prime is not None

    def to_dict(self) -> Dict[str, Any]:
        """Return a plain JSON-serializable dict representation."""
        return {
            "reaction_id": self.reaction_id,
            "backend": self.backend,
            "dg_prime": self.dg_prime,
            "dg_prime_uncertainty": self.dg_prime_uncertainty,
            "units": self.units,
            "ph": self.ph,
            "ionic_strength": self.ionic_strength,
            "temperature": self.temperature,
            "p_mg": self.p_mg,
            "equation": self.equation,
            "missing_compounds": list(self.missing_compounds),
            "warnings": list(self.warnings),
        }


@runtime_checkable
class ThermoBackend(Protocol):
    """Structural interface every predictive-thermodynamics backend implements.

    A backend is *optional*. Construction must never raise merely because the
    backend's dependency is missing; instead the backend reports
    :attr:`available` as ``False`` and explains via :attr:`unavailable_reason`.
    Compute methods raise :class:`BackendUnavailableError` if called while
    unavailable.
    """

    #: Short stable identifier, e.g. ``"equilibrator"``.
    name: str

    @property
    def available(self) -> bool:
        """Whether this backend can currently perform computations."""
        ...

    @property
    def unavailable_reason(self) -> Optional[str]:
        """Human-readable explanation when :attr:`available` is ``False``."""
        ...

    @property
    def capabilities(self) -> "frozenset[str]":
        """Set of capability tags this backend supports.

        Recognized tags: ``"reaction_dg"``, ``"compound_dgf"``, ``"pka"``,
        ``"major_microspecies"``.
        """
        ...

    def reaction_dg_prime(
        self,
        reaction_id: str,
        stoichiometry: Mapping[str, float],
        ph: float = 7.0,
        ionic_strength: float = 0.25,
        temperature: float = 298.15,
        p_mg: float = 14.0,
        **kwargs: Any,
    ) -> ReactionThermoEstimate:
        """Estimate standard transformed Gibbs energy of a reaction.

        Args:
            reaction_id: Identifier used only for labeling the result.
            stoichiometry: Mapping of compound identifier -> signed coefficient
                (negative for substrates, positive for products). The identifier
                namespace a backend accepts is backend-specific (e.g. ModelSEED
                ``cpdNNNNN``, KEGG ``CNNNNN``, InChI, or SMILES).
            ph: Reaction pH.
            ionic_strength: Ionic strength in M.
            temperature: Temperature in K.
            p_mg: pMg (negative log of free Mg2+ activity).
            **kwargs: Backend-specific options.

        Returns:
            A :class:`ReactionThermoEstimate`.

        Raises:
            BackendUnavailableError: If the backend is not available.
        """
        ...

    def compound_dgf(
        self,
        compound_id: str,
        ph: float = 7.0,
        ionic_strength: float = 0.25,
        temperature: float = 298.15,
        **kwargs: Any,
    ) -> CompoundThermoEstimate:
        """Estimate a compound's transformed formation energy / microspecies.

        Args:
            compound_id: Compound identifier (namespace is backend-specific).
            ph: pH.
            ionic_strength: Ionic strength in M.
            temperature: Temperature in K.
            **kwargs: Backend-specific options.

        Returns:
            A :class:`CompoundThermoEstimate`.

        Raises:
            BackendUnavailableError: If the backend is not available.
        """
        ...
