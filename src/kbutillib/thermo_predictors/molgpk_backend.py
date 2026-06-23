"""molGPK backend — compound pKa / major-microspecies via OPAM2 (MolGpKa-on-MSDB).

molGPK predicts molecular pKa values and the predominant ionic microspecies of a
compound at a given pH from chemical structure. Those microspecies are the
inputs a rigorous transformed-thermodynamics calculation needs.

Andrew Freiburger's variant lives at ``github.com/freiburgermsu/OPAM2`` — a fork
of `MolGpKa <https://github.com/Xundrug/MolGpKa>`_ whose graph-convolution model
has been fine-tuned on the ModelSEED biochemistry database (MSDB). It ships the
tuned weights (``models/weight_acid_modelseed.pth`` / ``weight_base_modelseed.pth``)
and exposes ``predict_pka.predict`` (per-atom acidic/basic pKa) plus
``protonate.Opam_protonate_mol`` (protonation-state enumeration at a target pH).

Why a subprocess
----------------
OPAM2 is research code with a heavy, fragile runtime: it imports ``torch`` and
``torch-geometric`` at module top, loads ``.pth`` model checkpoints, and its
internal imports are *bare* (``from utils.descriptor import mol2vec``,
``from predict_pka import predict_for_protonate``) so they only resolve with the
repo's ``src/`` directory on ``sys.path``. Importing all of that into the
KBUtilLib process would pin torch/PyG onto every consumer and pollute
``sys.path``.

Instead this backend shells out to a small worker script
(``_molgpk_worker.py``, bundled with KBUtilLib) run with the OPAM2 repo as its
working directory and with the repo's ``src/`` placed on ``sys.path``. The worker
loads the model (cached via ``lru_cache`` in ``predict_pka``) and answers a
single compound over a JSON stdin/stdout protocol. KBUtilLib never imports
OPAM2, torch, or torch-geometric. A repo-local ``kbutillib_molgpk_worker.py`` (if
present) takes precedence over the bundled copy.

Identifiers, units, conditions
------------------------------
* Input ``compound_id`` is the compound **structure** as a SMILES or InChI
  string. This is the only namespace OPAM2 understands (it operates on RDKit
  molecules); ModelSEED ``cpdNNNNN`` ids are not resolved here.
* ``pka_values`` are dimensionless pKa numbers for every acidic and basic
  ionization site the model identifies.
* ``major_microspecies`` is the dominant protonation state at the requested pH,
  returned as a SMILES string.
* ``dgf`` is **not** produced by this backend: OPAM2's pKa/microspecies output
  feeds eQuilibrator (the ``equilibrator`` backend) for transformed ΔG; mixing
  the two here would double-count. ``dgf`` is left ``None``.
* ``temperature`` / ``ionic_strength`` do not affect the pKa model and are
  recorded on the result for provenance only.

Configuration
-------------
Resolved (in order) from explicit args, the config resolver, then environment:

* repo path:   ``thermo.molgpk.repo_path``  / ``MOLGPK_REPO`` / ``OPAM2_REPO``
* python exe:  ``thermo.molgpk.python``      / ``MOLGPK_PYTHON``
  (must have rdkit + torch + torch-geometric; e.g. a dedicated ``opam2`` env)

If the repo / worker / model artifacts cannot be found, the backend reports
``available == False`` with a precise reason and never fabricates a pKa or a
microspecies assignment.
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
CONFIG_REPO_PATH = "thermo.molgpk.repo_path"
CONFIG_PYTHON = "thermo.molgpk.python"

#: Environment fallbacks (OPAM2_REPO accepted as an alias for the repo path).
ENV_REPO_PATH = "MOLGPK_REPO"
ENV_REPO_PATH_ALT = "OPAM2_REPO"
ENV_PYTHON = "MOLGPK_PYTHON"

#: Worker script name (a repo-local copy, if present, overrides the bundled one).
WORKER_SCRIPT = "kbutillib_molgpk_worker.py"

#: Bundled worker shipped with KBUtilLib (used when the repo has no local copy).
_BUNDLED_WORKER = os.path.join(os.path.dirname(__file__), "_molgpk_worker.py")

#: Relative paths (under the repo) of the ModelSEED-tuned model checkpoints.
_MODEL_RELPATHS = (
    os.path.join("models", "weight_acid_modelseed.pth"),
    os.path.join("models", "weight_base_modelseed.pth"),
)

#: Smallest plausible size (bytes) of a real .pth checkpoint; below this the
#: file is almost certainly an unpulled Git LFS pointer.
_MIN_MODEL_BYTES = 10_000

#: Per-call subprocess timeout (s): model load dominates the first call.
_TIMEOUT_S = 300


def _not_configured() -> str:
    return (
        "molGPK backend is not configured. Clone Andrew Freiburger's OPAM2 "
        "(github.com/freiburgermsu/OPAM2, a MolGpKa fork fine-tuned on the MSDB) "
        f"and set the repo path via the `{CONFIG_REPO_PATH}` config key or the "
        f"`{ENV_REPO_PATH}` environment variable. The interpreter used to run it "
        f"(set via `{CONFIG_PYTHON}` / `{ENV_PYTHON}`) needs rdkit, torch and "
        "torch-geometric installed. Until configured this backend reports "
        "unavailable and never fabricates pKa or microspecies values."
    )


class MolGPKBackend:
    """molGPK pKa / major-microspecies predictor (subprocess-isolated).

    Args:
        repo_path: Path to the OPAM2 repo. If omitted, resolved from config /
            environment.
        python_exe: Interpreter used to run the worker (needs rdkit + torch +
            torch-geometric). Defaults to config / env / the current interpreter.
        config_resolver: Optional callable ``(key, default) -> value`` to read
            configuration (e.g. ``SharedEnvUtils.get_config_value``).
        logger: Optional logger (duck-typed ``log_*`` / ``logging.Logger``).
    """

    name = "molgpk"

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
            or os.environ.get(ENV_REPO_PATH_ALT)
        )
        if not repo:
            self._unavailable_reason = _not_configured()
            return
        repo = os.path.abspath(os.path.expanduser(repo))
        if not os.path.isdir(repo):
            self._unavailable_reason = f"OPAM2 repo not found: {repo}"
            return

        worker = os.path.join(repo, WORKER_SCRIPT)
        if not os.path.isfile(worker):
            # Fall back to the worker bundled with KBUtilLib; it is run with the
            # repo as cwd and OPAM2_SRC pointing at the repo so its imports
            # resolve regardless of where the script itself lives.
            if os.path.isfile(_BUNDLED_WORKER):
                worker = _BUNDLED_WORKER
            else:
                self._unavailable_reason = (
                    f"molGPK worker script missing: neither {worker} nor the "
                    f"bundled {_BUNDLED_WORKER} was found."
                )
                return

        for rel in _MODEL_RELPATHS:
            model = os.path.join(repo, rel)
            if not os.path.isfile(model):
                self._unavailable_reason = (
                    f"OPAM2 model checkpoint missing: {model}. Ensure the "
                    "ModelSEED-tuned weights are present (git lfs pull if needed)."
                )
                return
            if os.path.getsize(model) < _MIN_MODEL_BYTES:
                self._unavailable_reason = (
                    f"OPAM2 model checkpoint at {model} is only "
                    f"{os.path.getsize(model)} bytes — this looks like an "
                    "unpulled Git LFS pointer, not a trained model."
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
        """Whether the backend can currently compute."""
        self._probe()
        return self._unavailable_reason is None

    @property
    def unavailable_reason(self) -> Optional[str]:
        """Explanation of why the backend is unavailable."""
        self._probe()
        return self._unavailable_reason

    @property
    def capabilities(self) -> "frozenset[str]":
        """Capabilities this backend provides."""
        return frozenset({"pka", "major_microspecies"})

    # -- compute -------------------------------------------------------------

    def compound_dgf(
        self,
        compound_id: str,
        ph: float = 7.0,
        ionic_strength: float = 0.25,
        temperature: float = 298.15,
        **kwargs: Any,
    ) -> CompoundThermoEstimate:
        """Predict a compound's pKa values and major microspecies at ``ph``.

        Args:
            compound_id: Compound **structure** as a SMILES or InChI string
                (OPAM2's only namespace). A ``smiles`` / ``structure`` keyword
                may override it explicitly.
            ph: Target pH for the major-microspecies assignment.
            ionic_strength: Recorded for provenance; does not affect the model.
            temperature: Recorded for provenance; does not affect the model.
            **kwargs: ``tph`` (borderline pKa window, default 1.0),
                ``smiles`` / ``structure`` (explicit structure override).

        Returns:
            A :class:`CompoundThermoEstimate` with ``pka_values`` and
            ``major_microspecies`` populated. ``dgf`` is left ``None`` (OPAM2's
            output feeds the equilibrator backend for ΔG).

        Raises:
            BackendUnavailableError: If the backend is not available.
        """
        self._probe()
        if self._unavailable_reason is not None:
            raise BackendUnavailableError(self._unavailable_reason)

        structure = kwargs.get("smiles") or kwargs.get("structure") or compound_id
        request = {
            "smiles": str(structure),
            "ph": float(ph),
            "tph": float(kwargs.get("tph", 1.0)),
            "microspecies_format": "smiles",
        }
        payload = self._run_worker(request)
        return self._estimate_from_payload(
            compound_id, payload, ph, ionic_strength, temperature
        )

    # -- subprocess + payload helpers ----------------------------------------

    def _run_worker(self, request: "dict[str, Any]") -> "dict[str, Any]":
        """Run the OPAM2 worker once and return its parsed JSON payload.

        A backend-unavailable condition (timeout, launch failure, non-zero exit,
        empty/non-JSON output) raises :class:`BackendUnavailableError`. A
        per-compound miss is *not* raised here — it comes back inside the payload
        (``{"ok": false}`` for a single call, or per-item in ``results`` for a
        batch) and is turned into an unestimated result by the caller.
        """
        try:
            worker_env = dict(os.environ)
            worker_env["OPAM2_SRC"] = self._repo  # type: ignore[assignment]
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
                f"OPAM2 worker timed out after {_TIMEOUT_S}s"
            ) from exc
        except OSError as exc:
            raise BackendUnavailableError(
                f"could not launch OPAM2 worker: {exc}"
            ) from exc

        if proc.returncode != 0:
            # Non-zero exit means a hard failure inside the worker (e.g. missing
            # torch/torch-geometric). Treat as unavailable, with stderr context.
            raise BackendUnavailableError(
                "OPAM2 worker failed (exit "
                f"{proc.returncode}); stderr: "
                + (proc.stderr or "").strip()[-500:]
            )

        stdout = (proc.stdout or "").strip()
        if not stdout:
            raise BackendUnavailableError(
                "OPAM2 worker produced no output; stderr: "
                + (proc.stderr or "").strip()[-500:]
            )
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise BackendUnavailableError(
                f"OPAM2 worker returned non-JSON: {stdout[:300]} ({exc})"
            ) from exc

    def _estimate_from_payload(
        self,
        compound_id: str,
        payload: "Mapping[str, Any]",
        ph: float,
        ionic_strength: float,
        temperature: float,
    ) -> CompoundThermoEstimate:
        """Turn one worker result object into a :class:`CompoundThermoEstimate`.

        A per-compound failure (``ok`` false, e.g. an unparseable structure)
        yields an estimate with no values and the reason recorded — never a
        backend-unavailable error and never a fabricated number.
        """
        if not payload.get("ok"):
            return CompoundThermoEstimate(
                compound_id=compound_id,
                backend=self.name,
                ph=ph,
                ionic_strength=ionic_strength,
                temperature=temperature,
                warnings=[
                    "molGPK could not estimate this compound: "
                    + str(payload.get("error", "unknown error"))
                ],
            )

        warnings = []
        if payload.get("major_microspecies") is None:
            warnings.append(
                "molGPK predicted pKa values but could not enumerate a major "
                "microspecies for this compound."
            )

        return CompoundThermoEstimate(
            compound_id=compound_id,
            backend=self.name,
            dgf=None,
            pka_values=[float(v) for v in payload.get("pka_values", [])],
            major_microspecies=payload.get("major_microspecies"),
            ph=float(payload.get("ph", ph)),
            ionic_strength=ionic_strength,
            temperature=temperature,
            warnings=warnings,
            raw={
                "acid_pka": payload.get("acid_pka", {}),
                "base_pka": payload.get("base_pka", {}),
                "microspecies": payload.get("microspecies", []),
                "model": payload.get("model"),
            },
        )

    def compounds_dgf(
        self,
        compound_ids: "Sequence[str]",
        ph: float = 7.0,
        ionic_strength: float = 0.25,
        temperature: float = 298.15,
        **kwargs: Any,
    ) -> "list[CompoundThermoEstimate]":
        """Batch variant of :meth:`compound_dgf` — one subprocess for all inputs.

        OPAM2's model load dominates the per-call cost, so predicting a whole
        compound set one subprocess-per-compound is wasteful. This sends every
        structure in a single worker invocation that loads the model once and
        loops, returning results positionally aligned with ``compound_ids``.

        Args:
            compound_ids: Compound structures (SMILES or InChI strings).
            ph: Target pH for the major-microspecies assignment.
            ionic_strength: Recorded for provenance; does not affect the model.
            temperature: Recorded for provenance; does not affect the model.
            **kwargs: ``tph`` (borderline pKa window, default 1.0).

        Returns:
            A list of :class:`CompoundThermoEstimate`, one per input (a failed
            compound carries empty values + a warning, never a fabricated pKa).

        Raises:
            BackendUnavailableError: If the backend is not available, or the
                worker fails hard (bad interpreter, missing torch).
        """
        self._probe()
        if self._unavailable_reason is not None:
            raise BackendUnavailableError(self._unavailable_reason)

        ids = list(compound_ids)
        if not ids:
            return []

        request = {
            "compounds": [{"smiles": str(cid)} for cid in ids],
            "ph": float(ph),
            "tph": float(kwargs.get("tph", 1.0)),
            "microspecies_format": "smiles",
        }
        payload = self._run_worker(request)

        results = payload.get("results")
        if not isinstance(results, list) or len(results) != len(ids):
            raise BackendUnavailableError(
                "OPAM2 batch worker returned a malformed/!misaligned result set"
            )

        return [
            self._estimate_from_payload(
                cid, item, ph, ionic_strength, temperature
            )
            for cid, item in zip(ids, results)
        ]

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
