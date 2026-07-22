"""eQuilibrator component-contribution backend.

Wraps the MIT-licensed ``equilibrator-api`` package
(https://gitlab.com/equilibrator/equilibrator-api) to estimate standard
transformed Gibbs free energies of reaction and compound formation from
identifiers (KEGG, BiGG, MetaNetX, ChEBI), InChI, or InChIKey.

The dependency is *optional*. Importing this module never imports
``equilibrator_api``; the import is deferred until the backend is first used.
If the package (or its ~1 GB local cache) is unavailable, the backend reports
``available == False`` with an explanatory reason rather than raising.

Identifier handling
-------------------
``equilibrator-api`` resolves compounds through accession namespaces using a
``namespace:accession`` syntax (e.g. ``kegg:C00002``, ``bigg.metabolite:atp``,
``metanetx.chemical:MNXM3``). This backend accepts identifiers that already
carry such a prefix and passes them through. Plain ModelSEED ``cpdNNNNN`` IDs
are *not* natively known to eQuilibrator; the umbrella dispatcher is
responsible for translating those (via cross-references) before calling this
backend.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from .base import (
    BackendUnavailableError,
    CompoundThermoEstimate,
    ReactionThermoEstimate,
)


class EquilibratorBackend:
    """Predict reaction/compound thermodynamics via eQuilibrator.

    Args:
        logger: Optional logger for diagnostics (duck-typed ``log_*`` / standard
            ``logging.Logger`` both work; only ``warning``/``info`` are used and
            calls are guarded).
        component_contribution: Optional pre-built ``ComponentContribution``
            instance (mainly for testing / dependency injection).
    """

    name = "equilibrator"

    def __init__(
        self,
        logger: Any = None,
        component_contribution: Any = None,
    ) -> None:
        self._logger = logger
        self._cc = component_contribution
        self._unavailable_reason: Optional[str] = None
        self._probed = component_contribution is not None

    # ── availability ────────────────────────────────────────────────────

    def _log_warning(self, msg: str) -> None:
        if self._logger is None:
            return
        warn = getattr(self._logger, "log_warning", None) or getattr(
            self._logger, "warning", None
        )
        if callable(warn):
            warn(msg)

    def _probe(self) -> None:
        """Lazily import equilibrator-api and build a ComponentContribution.

        Sets ``self._cc`` on success or ``self._unavailable_reason`` on failure.
        Safe to call repeatedly; only the first call does work.
        """
        if self._probed:
            return
        self._probed = True
        try:
            from equilibrator_api import ComponentContribution  # noqa: F401
        except Exception as exc:  # ImportError or transitive import failure
            self._unavailable_reason = (
                "equilibrator-api is not installed "
                f"({type(exc).__name__}: {exc}). Install with "
                "`pip install equilibrator-api` (MIT licensed)."
            )
            self._log_warning(self._unavailable_reason)
            return
        try:
            self._cc = ComponentContribution()
        except Exception as exc:
            # Typically a missing/corrupt local cache (~1 GB download on first
            # use) or a network problem fetching it.
            self._unavailable_reason = (
                "equilibrator-api is installed but ComponentContribution() "
                f"could not be constructed ({type(exc).__name__}: {exc}). "
                "This usually means the local cache has not been downloaded yet."
            )
            self._log_warning(self._unavailable_reason)
            self._cc = None

    @property
    def available(self) -> bool:
        """Whether eQuilibrator is importable and a cache is ready."""
        self._probe()
        return self._cc is not None

    @property
    def unavailable_reason(self) -> Optional[str]:
        """Why the backend is unavailable, or ``None`` if it is available."""
        self._probe()
        return self._unavailable_reason

    @property
    def capabilities(self) -> "frozenset[str]":
        """Capabilities provided by this backend."""
        return frozenset({"reaction_dg", "compound_dgf"})

    def _require(self) -> Any:
        if not self.available:
            raise BackendUnavailableError(
                self._unavailable_reason or "equilibrator backend unavailable"
            )
        return self._cc

    # ── conditions ──────────────────────────────────────────────────────

    def _apply_conditions(
        self,
        cc: Any,
        ph: float,
        ionic_strength: float,
        temperature: float,
        p_mg: float,
    ) -> None:
        """Set physiological conditions on the ComponentContribution object."""
        from equilibrator_api import Q_

        cc.p_h = Q_(ph)
        cc.ionic_strength = Q_(f"{ionic_strength}M")
        cc.temperature = Q_(f"{temperature}K")
        cc.p_mg = Q_(p_mg)

    @staticmethod
    def _coeff_to_formula(stoichiometry: Mapping[str, float]) -> str:
        """Render a stoichiometry mapping as an eQuilibrator reaction formula.

        Substrates (negative coefficients) go on the left, products (positive)
        on the right, e.g. ``kegg:C00002 + kegg:C00001 = kegg:C00008 +
        kegg:C00009``.
        """
        left, right = [], []
        for cid, coeff in stoichiometry.items():
            if coeff == 0:
                continue
            mag = abs(coeff)
            token = cid if mag == 1 else f"{mag} {cid}"
            (left if coeff < 0 else right).append(token)
        return f"{' + '.join(left)} = {' + '.join(right)}"

    # ── reaction ────────────────────────────────────────────────────────

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
            reaction_id: Label for the result.
            stoichiometry: compound id (namespaced) -> signed coefficient.
            ph: Reaction pH.
            ionic_strength: Ionic strength in M.
            temperature: Temperature in K.
            p_mg: pMg.
            **kwargs: Ignored (accepted for interface compatibility).

        Returns:
            A :class:`ReactionThermoEstimate`. On a parse/estimate failure the
            estimate carries ``dg_prime=None`` plus a warning rather than
            raising (so the dispatcher can fall through to another backend).

        Raises:
            BackendUnavailableError: If the backend is not available.
        """
        cc = self._require()
        estimate = ReactionThermoEstimate(
            reaction_id=reaction_id,
            backend=self.name,
            ph=ph,
            ionic_strength=ionic_strength,
            temperature=temperature,
            p_mg=p_mg,
        )
        formula = self._coeff_to_formula(stoichiometry)
        estimate.equation = formula
        try:
            self._apply_conditions(cc, ph, ionic_strength, temperature, p_mg)
            reaction = cc.parse_reaction_formula(formula)
            if not reaction.is_balanced():
                estimate.warnings.append(
                    "Reaction is not atom-balanced according to eQuilibrator; "
                    "ΔG'° may be unreliable."
                )
            dg = cc.standard_dg_prime(reaction)
            # dg is a measurement with .value (a pint Quantity) and .error
            estimate.dg_prime = float(dg.value.m_as("kJ/mol"))
            estimate.dg_prime_uncertainty = float(dg.error.m_as("kJ/mol"))
        except Exception as exc:
            estimate.warnings.append(
                f"equilibrator could not estimate ΔG'° ({type(exc).__name__}: {exc})"
            )
            self._log_warning(
                f"equilibrator reaction estimate failed for {reaction_id}: {exc}"
            )
        return estimate

    # ── compound ────────────────────────────────────────────────────────

    def compound_dgf(
        self,
        compound_id: str,
        ph: float = 7.0,
        ionic_strength: float = 0.25,
        temperature: float = 298.15,
        **kwargs: Any,
    ) -> CompoundThermoEstimate:
        """Estimate a compound's standard transformed formation energy.

        Args:
            compound_id: Namespaced identifier (e.g. ``kegg:C00002``) or InChI.
            ph: pH.
            ionic_strength: Ionic strength in M.
            temperature: Temperature in K.
            **kwargs: Ignored (accepted for interface compatibility).

        Returns:
            A :class:`CompoundThermoEstimate`. On failure, ``dgf=None`` plus a
            warning rather than raising.

        Raises:
            BackendUnavailableError: If the backend is not available.
        """
        cc = self._require()
        estimate = CompoundThermoEstimate(
            compound_id=compound_id,
            backend=self.name,
            ph=ph,
            ionic_strength=ionic_strength,
            temperature=temperature,
        )
        try:
            self._apply_conditions(cc, ph, ionic_strength, temperature, 14.0)
            compound = cc.get_compound(compound_id)
            if compound is None:
                estimate.warnings.append(
                    f"equilibrator does not recognize compound '{compound_id}'"
                )
                return estimate
            # standard_dg_formation returns (mu, sigmas_fin_or_None):
            #   mu      -> standard formation energy in kJ/mol (float) or None
            #   residual -> group-contribution residual vector (np.ndarray) or None
            # Note: this is the *non*-transformed standard ΔGf at the reference
            # state, not pH-transformed. We surface the central value; callers
            # needing ΔG'f should derive it from reactions.
            dgf, _residual = cc.standard_dg_formation(compound)
            if dgf is not None:
                estimate.dgf = float(dgf)
                estimate.warnings.append(
                    "equilibrator standard_dg_formation returns the untransformed "
                    "standard ΔGf (not pH-transformed); treat with care."
                )
            else:
                estimate.warnings.append(
                    f"equilibrator has no formation energy for '{compound_id}'"
                )
        except Exception as exc:
            estimate.warnings.append(
                f"equilibrator could not estimate ΔGf'° "
                f"({type(exc).__name__}: {exc})"
            )
            self._log_warning(
                f"equilibrator compound estimate failed for {compound_id}: {exc}"
            )
        return estimate
