"""dGPredictor backend — reaction ΔG'° via the Maranas/Tyo-lab predictor.

dGPredictor (Wang et al., Maranas lab; the fork used here is Andrew Freiburger's
``freiburgermsu/dGPredictor`` at ``master``) predicts the standard transformed
Gibbs free energy of reaction (delta_r G'^0) from molecular-substructure
("group") features using a Bayesian-ridge model trained on TECRDB.

Why a subprocess
----------------
dGPredictor is research code with a heavy and fragile runtime: it imports
``rdkit`` and ``openbabel`` at module top, ships its own copy of the
component-contribution package under ``CC/`` that uses *bare* imports
(``from compound import ...``) and therefore only works with ``CC/`` on
``sys.path`` and the **current working directory set to the repo root**, and it
loads a ~150 MB joblib model plus several MB of group-signature JSON. Importing
all of that into the KBUtilLib process would pollute ``sys.path``/cwd and pin
those heavy deps onto every consumer.

Instead this backend shells out to a small worker script
(``kbutillib_dg_worker.py``) placed in the dGPredictor repo. The worker sets up
cwd/sys.path correctly, loads the model once per call, and answers a single
reaction over a JSON stdin/stdout protocol. KBUtilLib never imports dGPredictor
or its dependencies.

Identifiers, units, conditions
------------------------------
* Input ``stoichiometry`` is a mapping of **KEGG compound IDs** (e.g.
  ``"C00002"``) to signed coefficients (negative = substrate). This is the only
  namespace dGPredictor's group-decomposition tables understand.
* Output is **delta_r G'^0 in kJ/mol** with the model's posterior standard
  deviation as the uncertainty (the value already includes the Legendre
  transform to the requested pH / ionic strength via the bundled compound
  cache).
* Temperature is fixed at 298.15 K inside dGPredictor and cannot be varied;
  ``temperature`` and ``p_mg`` arguments are accepted but **ignored**, and a
  warning is attached when they deviate from the supported values.

Configuration
-------------
Resolved (in order) from explicit args, the config resolver, then environment:

* repo path:   ``thermo.dgpredictor.repo_path``  / ``DGPREDICTOR_REPO``
* python exe:  ``thermo.dgpredictor.python``      / ``DGPREDICTOR_PYTHON``
  (defaults to the interpreter running KBUtilLib; must have rdkit + openbabel)

If the repo / worker / model artifact cannot be found, the backend reports
``available == False`` with a precise reason and never fabricates a value. In
particular it detects the common failure where ``model/M12_model_BR.pkl`` is an
unpulled Git LFS pointer (a few dozen bytes) rather than the real model.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any, Mapping, Optional

from .base import (
    BackendUnavailableError,
    CompoundThermoEstimate,
    ReactionThermoEstimate,
)

#: Config keys (dot-notation).
CONFIG_REPO_PATH = "thermo.dgpredictor.repo_path"
CONFIG_PYTHON = "thermo.dgpredictor.python"

#: Environment fallbacks.
ENV_REPO_PATH = "DGPREDICTOR_REPO"
ENV_PYTHON = "DGPREDICTOR_PYTHON"

#: Worker script name (placed in the dGPredictor repo by the integration).
WORKER_SCRIPT = "kbutillib_dg_worker.py"

#: Smallest plausible size (bytes) of the real joblib model; below this the
#: file is almost certainly an unpulled Git LFS pointer.
_MIN_MODEL_BYTES = 1_000_000

#: dGPredictor's fixed temperature (K).
_FIXED_TEMPERATURE = 298.15

#: Per-call subprocess timeout (s): model load dominates first call.
_TIMEOUT_S = 600


def _not_configured() -> str:
    return (
        "dGPredictor backend is not configured. Set the repo path via the "
        f"`{CONFIG_REPO_PATH}` config key or the `{ENV_REPO_PATH}` environment "
        "variable (clone freiburgermsu/dGPredictor and ensure the joblib model "
        "model/M12_model_BR.pkl is present — run model_gen.py if it is an "
        "unpulled Git LFS pointer). The interpreter used to run it needs rdkit "
        "and openbabel installed."
    )


class DGPredictorBackend:
    """dGPredictor reaction ΔG'° predictor (subprocess-isolated).

    Args:
        repo_path: Path to the dGPredictor repo. If omitted, resolved from
            config / environment.
        python_exe: Interpreter used to run the worker (needs rdkit + openbabel).
            Defaults to the current interpreter.
        config_resolver: Optional callable ``(key, default) -> value``.
        logger: Optional logger.
    """

    name = "dgpredictor"

    def __init__(
        self,
        repo_path: Optional[str] = None,
        python_exe: Optional[str] = None,
        config_resolver: Any = None,
        logger: Any = None,
    ) -> None:
        self._config = config_resolver
        self._logger = logger
        self._explicit_repo = repo_path
        self._explicit_python = python_exe
        self._repo: Optional[str] = None
        self._python: Optional[str] = None
        self._worker: Optional[str] = None
        self._unavailable_reason: Optional[str] = None
        self._probed = False

    # -- resolution ----------------------------------------------------------

    def _cfg(self, key: str) -> Optional[str]:
        if self._config is None:
            return None
        try:
            val = self._config(key, None)
        except Exception:  # pragma: no cover - resolver is best-effort
            return None
        return str(val) if val else None

    def _probe(self) -> None:
        if self._probed:
            return
        self._probed = True

        repo = (
            self._explicit_repo
            or self._cfg(CONFIG_REPO_PATH)
            or os.environ.get(ENV_REPO_PATH)
        )
        if not repo:
            self._unavailable_reason = _not_configured()
            return
        repo = os.path.abspath(os.path.expanduser(repo))
        if not os.path.isdir(repo):
            self._unavailable_reason = f"dGPredictor repo not found: {repo}"
            return

        worker = os.path.join(repo, WORKER_SCRIPT)
        if not os.path.isfile(worker):
            self._unavailable_reason = (
                f"dGPredictor worker script missing: {worker}. It is shipped "
                "with the KBUtilLib integration; copy it into the repo."
            )
            return

        model = os.path.join(repo, "model", "M12_model_BR.pkl")
        if not os.path.isfile(model):
            self._unavailable_reason = (
                f"dGPredictor model artifact missing: {model}. Run model_gen.py "
                "/ regen_model.py or `git lfs pull`."
            )
            return
        if os.path.getsize(model) < _MIN_MODEL_BYTES:
            self._unavailable_reason = (
                f"dGPredictor model artifact at {model} is only "
                f"{os.path.getsize(model)} bytes — this is an unpulled Git LFS "
                "pointer, not the trained model. Run model_gen.py / "
                "regen_model.py or `git lfs pull`."
            )
            return

        python = (
            self._explicit_python
            or self._cfg(CONFIG_PYTHON)
            or os.environ.get(ENV_PYTHON)
            or sys.executable
        )

        self._repo = repo
        self._worker = worker
        self._python = python
        self._unavailable_reason = None

    @property
    def available(self) -> bool:
        self._probe()
        return self._unavailable_reason is None

    @property
    def unavailable_reason(self) -> Optional[str]:
        self._probe()
        return self._unavailable_reason

    @property
    def capabilities(self) -> "frozenset[str]":
        return frozenset({"reaction_dg"})

    # -- compute -------------------------------------------------------------

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
        self._probe()
        if self._unavailable_reason is not None:
            raise BackendUnavailableError(self._unavailable_reason)

        warnings = []
        if abs(temperature - _FIXED_TEMPERATURE) > 1e-6:
            warnings.append(
                f"dGPredictor fixes T={_FIXED_TEMPERATURE} K; requested "
                f"{temperature} K was ignored."
            )

        request = {
            "rxn_dict": {str(k): float(v) for k, v in stoichiometry.items()},
            "pH": float(ph),
            "I": float(ionic_strength),
            "rid": str(reaction_id),
        }

        try:
            proc = subprocess.run(
                [self._python, self._worker],  # type: ignore[list-item]
                input=json.dumps(request),
                capture_output=True,
                text=True,
                cwd=self._repo,
                timeout=_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as exc:
            raise BackendUnavailableError(
                f"dGPredictor worker timed out after {_TIMEOUT_S}s"
            ) from exc
        except OSError as exc:
            raise BackendUnavailableError(
                f"could not launch dGPredictor worker: {exc}"
            ) from exc

        stdout = (proc.stdout or "").strip()
        if not stdout:
            raise BackendUnavailableError(
                "dGPredictor worker produced no output; stderr: "
                + (proc.stderr or "").strip()[-500:]
            )
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise BackendUnavailableError(
                f"dGPredictor worker returned non-JSON: {stdout[:300]} ({exc})"
            ) from exc

        if not payload.get("ok"):
            # A per-reaction failure (e.g. an undecomposable compound) is not a
            # backend-unavailable condition: return an unestimated result with
            # the reason recorded, so dispatch can fall through to another
            # backend without aborting.
            warnings.append(
                "dGPredictor could not estimate this reaction: "
                + str(payload.get("error", "unknown error"))
            )
            return ReactionThermoEstimate(
                reaction_id=reaction_id,
                backend=self.name,
                ph=ph,
                ionic_strength=ionic_strength,
                temperature=_FIXED_TEMPERATURE,
                p_mg=None,
                warnings=warnings,
            )

        return ReactionThermoEstimate(
            reaction_id=reaction_id,
            backend=self.name,
            dg_prime=float(payload["dg_prime"]),
            dg_prime_uncertainty=(
                None
                if payload.get("uncertainty") is None
                else float(payload["uncertainty"])
            ),
            ph=float(payload.get("pH", ph)),
            ionic_strength=float(payload.get("ionic_strength", ionic_strength)),
            temperature=_FIXED_TEMPERATURE,
            p_mg=None,
            warnings=warnings,
            raw={k: payload[k] for k in ("units", "transformed") if k in payload},
        )

    def compound_dgf(
        self,
        compound_id: str,
        ph: float = 7.0,
        ionic_strength: float = 0.25,
        temperature: float = 298.15,
        **kwargs: Any,
    ) -> CompoundThermoEstimate:
        """dGPredictor targets reactions, not standalone compound formation.

        Raises:
            BackendUnavailableError: Always (capability not provided).
        """
        raise BackendUnavailableError(
            "dgpredictor does not provide standalone compound formation energies"
        )
