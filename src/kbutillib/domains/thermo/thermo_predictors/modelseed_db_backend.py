"""ModelSEED-Database (Andrew Freiburger fork) baked-thermodynamics backend.

Andrew expanded eQuilibrator coverage for the ModelSEED biochemistry universe
by running stock eQuilibrator over a ModelSEED->MetaNetX structural mapping and
**baking the resulting transformed standard Gibbs energies directly into the
ModelSEED database TSVs** (``Biochemistry/compounds.tsv`` and
``reactions.tsv``). Roughly 10,600 compounds and ~43,000 reactions carry an
eQuilibrator-derived value, flagged ``EQU`` (compounds) / ``EQC``/``EQP``/
``EQU`` (reactions) in their ``notes`` column.

There is *no* custom thermodynamics library to call: "the expanded eQuilibrator
within MSDB" means reading those precomputed columns. This backend therefore
reads the TSVs directly with the standard library (no BiochemPy import, no
``sys.path`` manipulation, no heavy dependency) and is the **preferred path
when you are staying inside ModelSEED biochemistry** (it has broader coverage
than a live stock-eQuilibrator call and needs no network/model download).

Important caveats this backend is honest about
----------------------------------------------
* **Units.** The baked values are kcal/mol (Andrew converts from eQuilibrator's
  native kJ/mol). This backend converts back to **kJ/mol** (factor 4.184) so it
  matches the rest of :mod:`kbutillib.thermo_predictors`, which is kJ/mol
  throughout.
* **Transformed.** Values are standard transformed Gibbs energies (delta G'^0).
* **Fixed conditions.** They were computed at pH 7.0, ionic strength 0.25 M,
  298.15 K. They cannot be re-derived at other conditions. If a caller requests
  different conditions, the backend returns the baked value *and adds a warning*
  rather than silently pretending the value applies.
* **Provenance preference.** By default it only returns a value flagged ``EQU``
  (genuinely from the expanded eQuilibrator). A non-eQ Group-Contribution value
  may also be present; set ``require_equilibrator=False`` to accept it, in which
  case the provenance is reported in the warnings.

Configuration
-------------
The backend needs the path to the fork's ``Biochemistry`` directory. It is
resolved (in order) from: an explicit ``biochem_root`` constructor argument, the
config key ``thermo.modelseed_db.biochem_root``, or the environment variable
``MODELSEED_DATABASE_BIOCHEM_ROOT``. If none resolve to a readable
``compounds.tsv``, the backend reports ``available == False`` and never
fabricates a value.
"""

from __future__ import annotations

import csv
import os
from typing import Any, Dict, Mapping, Optional

from .base import (
    BackendUnavailableError,
    CompoundThermoEstimate,
    ReactionThermoEstimate,
)

#: kcal -> kJ.
_KCAL_TO_KJ = 4.184

#: Conditions the baked values were computed at (and only valid at).
_BAKED_PH = 7.0
_BAKED_IONIC_STRENGTH = 0.25
_BAKED_TEMPERATURE = 298.15

#: Config / environment keys for locating the database.
CONFIG_BIOCHEM_ROOT = "thermo.modelseed_db.biochem_root"
ENV_BIOCHEM_ROOT = "MODELSEED_DATABASE_BIOCHEM_ROOT"

#: Sentinels ModelSEED writes for an absent value.
_NULL_TOKENS = frozenset({"null", "none", "nan", "na", ""})

#: notes tags that mark an eQuilibrator-derived value.
_EQ_TAGS = frozenset({"EQU", "EQC", "EQP"})


def _clean_root(root: str) -> str:
    """Normalize to the directory that contains ``compounds.tsv``."""
    root = os.path.expanduser(root)
    # Accept either the Biochemistry dir or a path already pointing inside it.
    if os.path.isfile(os.path.join(root, "compounds.tsv")):
        return root
    nested = os.path.join(root, "Biochemistry")
    if os.path.isfile(os.path.join(nested, "compounds.tsv")):
        return nested
    return root


def _parse_float(token: Optional[str]) -> Optional[float]:
    """Parse a TSV cell to float, treating ModelSEED null sentinels as None."""
    if token is None:
        return None
    if token.strip().lower() in _NULL_TOKENS:
        return None
    try:
        return float(token)
    except (TypeError, ValueError):
        return None


def _parse_notes(token: Optional[str]) -> "frozenset[str]":
    if not token or token.strip().lower() in _NULL_TOKENS:
        return frozenset()
    return frozenset(p.strip() for p in token.split("|") if p.strip())


class ModelSEEDDBBackend:
    """Reads baked transformed Gibbs energies from the ModelSEED Database fork.

    Args:
        biochem_root: Path to the fork's ``Biochemistry`` directory (or a parent
            containing it). If omitted, resolved from config / environment.
        config_resolver: Optional callable ``(key, default) -> value``.
        require_equilibrator: When True (default) only values flagged with an
            eQuilibrator tag (``EQU``/``EQC``/``EQP``) are returned; otherwise
            any baked value is returned with its provenance in the warnings.
        logger: Optional logger.
    """

    name = "modelseed_db"
    capabilities = frozenset({"reaction_dg", "compound_dgf"})

    def __init__(
        self,
        biochem_root: Optional[str] = None,
        config_resolver: Any = None,
        require_equilibrator: bool = True,
        logger: Any = None,
    ) -> None:
        self._config = config_resolver
        self._logger = logger
        self._require_eq = require_equilibrator
        self._root = self._resolve_root(biochem_root)
        self._compounds: Optional[Dict[str, Dict[str, Any]]] = None
        self._reactions: Optional[Dict[str, Dict[str, Any]]] = None
        self._unavailable_reason: Optional[str] = None
        if self._root is None or not os.path.isfile(
            os.path.join(self._root, "compounds.tsv")
        ):
            self._unavailable_reason = (
                "ModelSEED Database fork not found. Set a path to its "
                f"Biochemistry directory via the `{CONFIG_BIOCHEM_ROOT}` config "
                f"key or the `{ENV_BIOCHEM_ROOT}` environment variable. This "
                "backend reads baked eQuilibrator deltaG values from "
                "compounds.tsv / reactions.tsv (Andrew Freiburger's fork)."
            )

    # -- resolution / availability ------------------------------------------

    def _resolve_root(self, explicit: Optional[str]) -> Optional[str]:
        candidates = [explicit]
        if self._config is not None:
            try:
                candidates.append(self._config(CONFIG_BIOCHEM_ROOT, None))
            except Exception:  # pragma: no cover - resolver is best-effort
                pass
        candidates.append(os.environ.get(ENV_BIOCHEM_ROOT))
        for cand in candidates:
            if cand:
                return _clean_root(str(cand))
        return None

    @property
    def available(self) -> bool:
        return self._unavailable_reason is None

    @property
    def unavailable_reason(self) -> Optional[str]:
        return self._unavailable_reason

    # -- lazy TSV loading ----------------------------------------------------

    def _load_table(self, filename: str) -> Dict[str, Dict[str, Any]]:
        assert self._root is not None  # guarded by `available` before any call
        path = os.path.join(self._root, filename)
        index: Dict[str, Dict[str, Any]] = {}
        with open(path, newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            for row in reader:
                cid = (row.get("id") or "").strip()
                if not cid:
                    continue
                index[cid] = {
                    "deltag": _parse_float(row.get("deltag")),
                    "deltagerr": _parse_float(row.get("deltagerr")),
                    "notes": _parse_notes(row.get("notes")),
                    "name": (row.get("name") or "").strip(),
                    "equation": (row.get("equation") or "").strip(),
                }
        return index

    @property
    def _compound_index(self) -> Dict[str, Dict[str, Any]]:
        if self._compounds is None:
            self._compounds = self._load_table("compounds.tsv")
        return self._compounds

    @property
    def _reaction_index(self) -> Dict[str, Dict[str, Any]]:
        if self._reactions is None:
            self._reactions = self._load_table("reactions.tsv")
        return self._reactions

    # -- helpers -------------------------------------------------------------

    def _condition_warnings(
        self, ph: float, ionic_strength: float, temperature: float
    ) -> list:
        warnings = []
        if (
            abs(ph - _BAKED_PH) > 1e-6
            or abs(ionic_strength - _BAKED_IONIC_STRENGTH) > 1e-6
            or abs(temperature - _BAKED_TEMPERATURE) > 1e-6
        ):
            warnings.append(
                "ModelSEED Database values are precomputed at pH "
                f"{_BAKED_PH}, I={_BAKED_IONIC_STRENGTH} M, "
                f"T={_BAKED_TEMPERATURE} K and cannot be recomputed at the "
                "requested conditions; returning the baked value as-is."
            )
        return warnings

    def _provenance_ok(self, notes: "frozenset[str]", warnings: list) -> bool:
        is_eq = bool(notes & _EQ_TAGS)
        if self._require_eq and not is_eq:
            return False
        if not is_eq:
            warnings.append(
                "Value is not from the expanded eQuilibrator (no EQU/EQC/EQP "
                f"tag); provenance notes={sorted(notes)}."
            )
        return True

    # -- compute -------------------------------------------------------------

    def compound_dgf(
        self,
        compound_id: str,
        ph: float = 7.0,
        ionic_strength: float = 0.25,
        temperature: float = 298.15,
        **kwargs: Any,
    ) -> CompoundThermoEstimate:
        if not self.available:
            raise BackendUnavailableError(self._unavailable_reason)
        warnings = self._condition_warnings(ph, ionic_strength, temperature)
        entry = self._compound_index.get(compound_id)
        if entry is None:
            warnings.append(f"{compound_id} not in ModelSEED Database fork.")
            return CompoundThermoEstimate(
                compound_id=compound_id, backend=self.name, warnings=warnings,
                ph=ph, ionic_strength=ionic_strength, temperature=temperature,
            )
        dgf_kcal = entry["deltag"]
        if dgf_kcal is None or not self._provenance_ok(entry["notes"], warnings):
            if dgf_kcal is None:
                warnings.append(f"No baked deltaG for {compound_id}.")
            return CompoundThermoEstimate(
                compound_id=compound_id, backend=self.name, warnings=warnings,
                ph=ph, ionic_strength=ionic_strength, temperature=temperature,
            )
        err_kcal = entry["deltagerr"]
        return CompoundThermoEstimate(
            compound_id=compound_id,
            backend=self.name,
            dgf=dgf_kcal * _KCAL_TO_KJ,
            dgf_uncertainty=None if err_kcal is None else err_kcal * _KCAL_TO_KJ,
            ph=_BAKED_PH,
            ionic_strength=_BAKED_IONIC_STRENGTH,
            temperature=_BAKED_TEMPERATURE,
            warnings=warnings,
            raw={"deltag_kcal": dgf_kcal, "notes": sorted(entry["notes"]),
                 "name": entry["name"]},
        )

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
        if not self.available:
            raise BackendUnavailableError(self._unavailable_reason)
        warnings = self._condition_warnings(ph, ionic_strength, temperature)
        entry = self._reaction_index.get(reaction_id)
        if entry is None:
            warnings.append(f"{reaction_id} not in ModelSEED Database fork.")
            return ReactionThermoEstimate(
                reaction_id=reaction_id, backend=self.name, warnings=warnings,
                ph=ph, ionic_strength=ionic_strength, temperature=temperature,
                p_mg=p_mg,
            )
        dg_kcal = entry["deltag"]
        if dg_kcal is None or not self._provenance_ok(entry["notes"], warnings):
            if dg_kcal is None:
                warnings.append(f"No baked deltaG for {reaction_id}.")
            return ReactionThermoEstimate(
                reaction_id=reaction_id, backend=self.name, warnings=warnings,
                ph=ph, ionic_strength=ionic_strength, temperature=temperature,
                p_mg=p_mg, equation=entry["equation"] or None,
            )
        err_kcal = entry["deltagerr"]
        return ReactionThermoEstimate(
            reaction_id=reaction_id,
            backend=self.name,
            dg_prime=dg_kcal * _KCAL_TO_KJ,
            dg_prime_uncertainty=(
                None if err_kcal is None else err_kcal * _KCAL_TO_KJ
            ),
            ph=_BAKED_PH,
            ionic_strength=_BAKED_IONIC_STRENGTH,
            temperature=_BAKED_TEMPERATURE,
            p_mg=None,
            equation=entry["equation"] or None,
            warnings=warnings,
            raw={"deltag_kcal": dg_kcal, "notes": sorted(entry["notes"]),
                 "name": entry["name"]},
        )
