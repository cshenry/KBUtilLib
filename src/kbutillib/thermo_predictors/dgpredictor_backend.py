"""dGPredictor backend — reaction ΔG'° via Andrew Freiburger's ModelSEED fork.

dGPredictor (Wang et al., Maranas lab) predicts the standard transformed Gibbs
free energy of reaction (delta_r G'^0) from molecular-substructure ("group")
features using a Bayesian-ridge model. The variant wrapped here is Andrew
Freiburger's ModelSEED-native fork (``github.com/freiburgermsu/dGPredictor`` at
``master``): its ``dg_prediction_modelseed.ModelSEEDdGPredictor`` is retrained on
the ModelSEED biochemistry database and speaks ModelSEED identifiers directly
(``rxnNNNNN`` / ``cpdNNNNN``) instead of KEGG.

Why a subprocess
----------------
dGPredictor is research code with a heavy and fragile runtime: it imports
``rdkit`` and (via ``CC/compound.py``) ``openbabel`` at module top, ships its own
copy of the component-contribution package under ``CC/`` that uses *bare* imports
and a hard-coded ``sys.path.insert(0, 'CC')`` (so it only works with the repo
root on ``sys.path`` and the current working directory set to the repo root), and
on construction loads a joblib BayesianRidge model plus several MB of
group-signature JSON and a gzipped compound cache. Importing all of that into the
KBUtilLib process would pollute ``sys.path``/cwd and pin those heavy deps onto
every consumer.

Instead this backend shells out to a small worker script
(``_dgpredictor_worker.py``, bundled with KBUtilLib) run with the dGPredictor
repo as its working directory and ``DGPREDICTOR_REPO`` pointing at it. The worker
constructs the predictor once and answers a reaction (or a whole batch) over a
JSON stdin/stdout protocol. KBUtilLib never imports dGPredictor or its
dependencies. A repo-local ``kbutillib_dg_worker.py`` (if present) takes
precedence over the bundled copy.

Identifiers, units, conditions
------------------------------
* A reaction is addressed by a ModelSEED ``rxnNNNNN`` accession (resolved against
  the repo's bundled stoichiometry table) OR by an explicit ``stoichiometry``
  mapping of ModelSEED ``cpdNNNNN`` -> signed coefficient (negative = substrate),
  which the worker renders to an equation string. KEGG ids are not used by this
  fork.
* Output is **delta_r G'^0 in kJ/mol** with the model's posterior standard
  deviation as the uncertainty (the value already includes the pH / ionic-
  strength Legendre correction via the bundled compound cache).
* Temperature is effectively fixed at 298.15 K by the model; ``temperature`` is
  accepted but a warning is attached when it deviates.

Configuration
-------------
Resolved (in order) from explicit args, the config resolver, then environment:

* repo path:   ``thermo.dgpredictor.repo_path``  / ``DGPREDICTOR_REPO``
* python exe:  ``thermo.dgpredictor.python``      / ``DGPREDICTOR_PYTHON``
  (defaults to the interpreter running KBUtilLib; must have rdkit + openbabel +
  scikit-learn + scipy + pandas + joblib)

If the repo / worker / model artifact cannot be found, the backend reports
``available == False`` with a precise reason and never fabricates a value. In
particular it detects the common failure where
``model/modelseed_M12_model_BR.pkl`` is missing or an unpulled Git LFS pointer
(a few dozen bytes) rather than the real trained model.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any, Mapping, Optional, Sequence

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

#: Worker script name (a repo-local copy, if present, overrides the bundled one).
WORKER_SCRIPT = "kbutillib_dg_worker.py"

#: Bundled worker shipped with KBUtilLib (used when the repo has no local copy).
_BUNDLED_WORKER = os.path.join(os.path.dirname(__file__), "_dgpredictor_worker.py")

#: Relative path (under the repo) of the ModelSEED-retrained joblib model.
_MODEL_RELPATH = os.path.join("model", "modelseed_M12_model_BR.pkl")

#: Smallest plausible size (bytes) of the real joblib model; below this the file
#: is almost certainly an unpulled Git LFS pointer.
_MIN_MODEL_BYTES = 100_000

#: dGPredictor's effectively fixed temperature (K).
_FIXED_TEMPERATURE = 298.15

#: Per-call subprocess timeout (s): model + cache load dominates the first call.
_TIMEOUT_S = 600


def _not_configured() -> str:
    return (
        "dGPredictor backend is not configured. Clone Andrew Freiburger's "
        "ModelSEED fork (github.com/freiburgermsu/dGPredictor) and set the repo "
        f"path via the `{CONFIG_REPO_PATH}` config key or the `{ENV_REPO_PATH}` "
        "environment variable. Ensure the trained model "
        "model/modelseed_M12_model_BR.pkl is present (it is a Git-LFS / "
        "generated artifact — rebuild it with the repo's retrain pipeline if "
        "absent). The interpreter used to run it needs rdkit, openbabel, "
        "scikit-learn, scipy, pandas and joblib."
    )


class DGPredictorBackend:
    """dGPredictor reaction ΔG'° predictor (subprocess-isolated, ModelSEED fork).

    Args:
        repo_path: Path to the dGPredictor repo. If omitted, resolved from
            config / environment.
        python_exe: Interpreter used to run the worker (needs rdkit + openbabel
            + scikit-learn + scipy + pandas + joblib). Defaults to config / env /
            the current interpreter.
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
            # Fall back to the worker bundled with KBUtilLib; it is run with the
            # repo as cwd and DGPREDICTOR_REPO pointing at the repo so its
            # imports resolve regardless of where the script itself lives.
            if os.path.isfile(_BUNDLED_WORKER):
                worker = _BUNDLED_WORKER
            else:
                self._unavailable_reason = (
                    f"dGPredictor worker script missing: neither {worker} nor "
                    f"the bundled {_BUNDLED_WORKER} was found."
                )
                return

        model = os.path.join(repo, _MODEL_RELPATH)
        if not os.path.isfile(model):
            self._unavailable_reason = (
                f"dGPredictor model artifact missing: {model}. Rebuild it with "
                "the repo's retrain pipeline (retrain_modelseed.py) or fetch the "
                "Git-LFS object."
            )
            return
        if os.path.getsize(model) < _MIN_MODEL_BYTES:
            self._unavailable_reason = (
                f"dGPredictor model artifact at {model} is only "
                f"{os.path.getsize(model)} bytes — this looks like an unpulled "
                "Git LFS pointer, not the trained model."
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

    # -- subprocess ----------------------------------------------------------

    def _run_worker(self, request: "dict[str, Any]") -> "dict[str, Any]":
        """Run the dGPredictor worker once and return its parsed JSON payload.

        A backend-unavailable condition (timeout, launch failure, non-zero exit,
        empty/non-JSON output) raises :class:`BackendUnavailableError`. A
        per-reaction miss is *not* raised here — it comes back inside the payload
        (``{"ok": false}``) and is turned into an unestimated result by callers.
        """
        worker_env = dict(os.environ)
        worker_env[ENV_REPO_PATH] = self._repo  # type: ignore[assignment]
        try:
            proc = subprocess.run(
                [self._python, self._worker],  # type: ignore[list-item]
                input=json.dumps(request),
                capture_output=True,
                text=True,
                cwd=self._repo,
                env=worker_env,
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

        if proc.returncode != 0:
            raise BackendUnavailableError(
                "dGPredictor worker failed (exit "
                f"{proc.returncode}); stderr: "
                + (proc.stderr or "").strip()[-500:]
            )

        stdout = (proc.stdout or "").strip()
        if not stdout:
            raise BackendUnavailableError(
                "dGPredictor worker produced no output; stderr: "
                + (proc.stderr or "").strip()[-500:]
            )
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise BackendUnavailableError(
                f"dGPredictor worker returned non-JSON: {stdout[:300]} ({exc})"
            ) from exc

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
        """Estimate a reaction's transformed Gibbs free energy via dGPredictor.

        Args:
            reaction_id: ModelSEED ``rxnNNNNN`` accession. When a non-empty
                ``stoichiometry`` is supplied it takes precedence (the reaction
                is predicted from its compounds); otherwise the accession is
                resolved against the repo's bundled stoichiometry table.
            stoichiometry: Mapping of ModelSEED ``cpdNNNNN`` -> signed
                coefficient (negative = substrate). May be empty to use the
                accession path.
            ph, ionic_strength: Conditions applied by the model's Legendre
                correction.
            temperature: Recorded; the model fixes T=298.15 K.
            p_mg: Accepted for API symmetry; not used by dGPredictor.
            **kwargs: ``equation`` (explicit ModelSEED equation string),
                ``arrow`` (default ``"<=>"``).

        Returns:
            A :class:`ReactionThermoEstimate`. A reaction the model cannot
            predict yields an unestimated result with the reason recorded — never
            a fabricated number.

        Raises:
            BackendUnavailableError: If the backend is not available or the
                worker fails hard.
        """
        self._probe()
        if self._unavailable_reason is not None:
            raise BackendUnavailableError(self._unavailable_reason)

        warnings = []
        if abs(temperature - _FIXED_TEMPERATURE) > 1e-6:
            warnings.append(
                f"dGPredictor fixes T={_FIXED_TEMPERATURE} K; requested "
                f"{temperature} K was ignored."
            )

        request: "dict[str, Any]" = {
            "pH": float(ph),
            "I": float(ionic_strength),
            "T": float(temperature),
        }
        stoich = {str(k): float(v) for k, v in (stoichiometry or {}).items()}
        if kwargs.get("equation"):
            request["equation"] = str(kwargs["equation"])
        elif stoich:
            request["stoichiometry"] = stoich
        else:
            request["rxn_id"] = str(reaction_id)
        if kwargs.get("arrow"):
            request["arrow"] = str(kwargs["arrow"])

        payload = self._run_worker(request)

        if not payload.get("ok"):
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

    def reactions_dg_prime(
        self,
        requests: "Sequence[Mapping[str, Any]]",
        ph: float = 7.0,
        ionic_strength: float = 0.25,
        temperature: float = 298.15,
        **kwargs: Any,
    ) -> "list[ReactionThermoEstimate]":
        """Batch variant — one subprocess (one model load) for many reactions.

        The model + cache load dominates per-call cost, so predicting a whole
        reaction set one subprocess-per-reaction is wasteful. This sends every
        reaction in a single worker invocation that builds the predictor once.

        Args:
            requests: A sequence of reaction specs. Each item may carry
                ``reaction_id``/``rxn_id``, ``stoichiometry`` (ModelSEED
                cpd -> coeff), or ``equation``, plus optional per-item ``pH`` /
                ``I`` / ``T`` / ``arrow``.
            ph, ionic_strength, temperature: Batch-wide defaults.
            **kwargs: Unused (reserved).

        Returns:
            A list of :class:`ReactionThermoEstimate`, positionally aligned with
            ``requests`` (a reaction the model cannot predict carries no value +
            a warning, never a fabricated number).

        Raises:
            BackendUnavailableError: If the backend is not available or the
                worker fails hard.
        """
        self._probe()
        if self._unavailable_reason is not None:
            raise BackendUnavailableError(self._unavailable_reason)

        specs = list(requests)
        if not specs:
            return []

        items = []
        labels = []
        for spec in specs:
            label = str(spec.get("reaction_id") or spec.get("rxn_id") or "")
            labels.append(label)
            item: "dict[str, Any]" = {}
            if spec.get("equation"):
                item["equation"] = str(spec["equation"])
            elif spec.get("stoichiometry"):
                item["stoichiometry"] = {
                    str(k): float(v) for k, v in spec["stoichiometry"].items()
                }
            elif label:
                item["rxn_id"] = label
            for k in ("pH", "I", "T", "arrow"):
                if k in spec:
                    item[k] = spec[k]
            items.append(item)

        request = {
            "reactions": items,
            "pH": float(ph),
            "I": float(ionic_strength),
            "T": float(temperature),
        }
        payload = self._run_worker(request)

        results = payload.get("results")
        if not isinstance(results, list) or len(results) != len(specs):
            raise BackendUnavailableError(
                "dGPredictor batch worker returned a malformed/misaligned result set"
            )

        out = []
        for label, item in zip(labels, results):
            if not item.get("ok"):
                out.append(
                    ReactionThermoEstimate(
                        reaction_id=label,
                        backend=self.name,
                        ph=ph,
                        ionic_strength=ionic_strength,
                        temperature=_FIXED_TEMPERATURE,
                        p_mg=None,
                        warnings=[
                            "dGPredictor could not estimate this reaction: "
                            + str(item.get("error", "unknown error"))
                        ],
                    )
                )
                continue
            out.append(
                ReactionThermoEstimate(
                    reaction_id=label,
                    backend=self.name,
                    dg_prime=float(item["dg_prime"]),
                    dg_prime_uncertainty=(
                        None
                        if item.get("uncertainty") is None
                        else float(item["uncertainty"])
                    ),
                    ph=float(item.get("pH", ph)),
                    ionic_strength=float(item.get("ionic_strength", ionic_strength)),
                    temperature=_FIXED_TEMPERATURE,
                    p_mg=None,
                    raw={k: item[k] for k in ("units", "transformed") if k in item},
                )
            )
        return out

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
