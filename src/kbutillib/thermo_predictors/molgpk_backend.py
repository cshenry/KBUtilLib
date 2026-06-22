"""molGPK backend (stub — pending repository access from Andrew).

molGPK predicts molecular pKa values and the predominant ionic microspecies of
a compound at a given pH from chemical structure. Those microspecies are the
inputs a rigorous transformed-thermodynamics calculation needs. Like
dGPredictor it is research code with a custom install and a model artifact.

Andrew Freiburger's variant lives at ``github.com/freiburgermsu/OPAM2`` (a
previous intern named the project OPAM2; it is a fork of MolGpKa fine-tuned on
the ModelSEED biochemistry database / MSDB). As of integration time that repo
is private and not yet shared, so this backend stays a faithful interface stub
that reports ``available == False``. It never fabricates pKa values or a
microspecies assignment.

Wiring it up later (drop-in, once OPAM2 access is granted)
---------------------------------------------------------
Source: ``github.com/freiburgermsu/OPAM2`` (MolGpKa fork, fine-tuned on MSDB).
Andrew's note: "Andrew knows the method." MolGpKa upstream takes a molecule
(SMILES/mol via RDKit) and returns per-atom acidic/basic pKa predictions from a
graph-convolution model; OPAM2 fine-tunes that on MSDB compounds.

1. ``_probe`` — replace with the real import + model-load (defer the heavy
   RDKit/torch import; set ``self._model`` on success or
   ``self._unavailable_reason`` on failure; never raise at import). Resolve the
   repo + model artifact via the config keys below.
2. ``compound_dgf`` — replace the ``BackendUnavailableError`` body with a call
   into the model, populating ``pka_values``, ``major_microspecies`` and (if
   the model provides it) a transformed ``dgf`` on the
   :class:`CompoundThermoEstimate`. Confirm output UNITS and the microspecies
   schema against OPAM2's README/tests before trusting any number.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from .base import (
    BackendUnavailableError,
    CompoundThermoEstimate,
    ReactionThermoEstimate,
)

#: Config keys (dot-notation) consulted when this backend is wired up.
CONFIG_REPO_PATH = "thermo.molgpk.repo_path"
CONFIG_MODEL_PATH = "thermo.molgpk.model_path"
CONFIG_PYTHON = "thermo.molgpk.python"

_NOT_CONFIGURED = (
    "molGPK backend is not configured. Andrew Freiburger's variant (OPAM2, a "
    "MolGpKa fork fine-tuned on the MSDB) lives at github.com/freiburgermsu/OPAM2 "
    "and is not yet accessible. Once access is granted, install the research code "
    "+ model artifact, set `{repo}` and `{model}` in config, and complete _probe/"
    "compound_dgf. Until then this backend reports unavailable and never "
    "fabricates pKa or microspecies values."
).format(repo=CONFIG_REPO_PATH, model=CONFIG_MODEL_PATH)


class MolGPKBackend:
    """molGPK pKa / microspecies predictor (interface stub).

    Args:
        config_resolver: Optional callable ``(key, default) -> value`` to read
            configuration (e.g. ``SharedEnvUtils.get_config_value``).
        logger: Optional logger (duck-typed ``log_*`` / ``logging.Logger``).
    """

    name = "molgpk"

    def __init__(
        self,
        config_resolver: Any = None,
        logger: Any = None,
    ) -> None:
        self._config = config_resolver
        self._logger = logger
        self._model: Any = None
        self._unavailable_reason: Optional[str] = _NOT_CONFIGURED
        self._probed = False

    def _probe(self) -> None:
        """Resolve availability. Currently always unavailable by design.

        Replace this body when integrating: defer the real import, load the
        model from the configured path, and on success set ``self._model`` and
        clear ``self._unavailable_reason``.
        """
        if self._probed:
            return
        self._probed = True
        self._unavailable_reason = _NOT_CONFIGURED

    @property
    def available(self) -> bool:
        """Whether the backend can compute (False until integrated)."""
        self._probe()
        return self._model is not None

    @property
    def unavailable_reason(self) -> Optional[str]:
        """Explanation of why the backend is unavailable."""
        self._probe()
        return self._unavailable_reason

    @property
    def capabilities(self) -> "frozenset[str]":
        """Capabilities this backend will provide once integrated."""
        return frozenset({"pka", "major_microspecies"})

    def compound_dgf(
        self,
        compound_id: str,
        ph: float = 7.0,
        ionic_strength: float = 0.25,
        temperature: float = 298.15,
        **kwargs: Any,
    ) -> CompoundThermoEstimate:
        """Not implemented until integrated; never fabricates pKa/microspecies.

        Raises:
            BackendUnavailableError: Always, until the integration is completed.
        """
        raise BackendUnavailableError(self.unavailable_reason or _NOT_CONFIGURED)

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
        """molGPK targets compound pKa/microspecies, not reaction ΔG'°.

        Raises:
            BackendUnavailableError: Always (capability not provided).
        """
        raise BackendUnavailableError(
            "molgpk does not provide reaction free energies"
        )
