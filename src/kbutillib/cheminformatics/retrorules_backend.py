"""RetroRules reaction-rule expansion backend.

Applies `RetroRules <https://retrorules.org>`_ SMARTS reaction operators to
seed compounds with RDKit to enumerate predicted products. RetroRules is a
database of reaction rules at multiple "diameters" (the radius, in bonds,
around the reaction centre) extracted from MetaNetX/Rhea; larger diameters are
more specific.

Optional dependencies
---------------------
* RDKit -- for parsing/applying the SMARTS. Imported lazily; absent => backend
  reports ``available == False``.
* A RetroRules flat-rules TSV on disk (the dump is ~500 MB and is *not*
  shipped). Located via config ``cheminformatics.retrorules.rules_tsv`` or env
  ``KBUTILLIB_RETRORULES_TSV``.

Verified data schema (retrorules_rr02_rp3_nohs/retrorules_rr02_flat_all.tsv,
columns read directly from the file header)
-------------------------------------------------------------------------
``# Rule_ID, Legacy_ID, Reaction_ID, Diameter, Rule_order, Rule_SMARTS,
Substrate_ID, Substrate_SMILES, Product_IDs, Product_SMILES, Rule_SMILES,
Rule_SMARTS_lite, Score, Score_normalized, Reaction_EC_number,
Reaction_direction, Rule_relative_direction, Rule_usage``

``Rule_relative_direction`` is ``1`` (forward) or ``-1`` (reverse/retro). The
TSV holds ~350k rule rows across diameters {2,4,6,8,10,12,14,16}.

Verified application recipe (RDKit, confirmed against a known rule+substrate)
---------------------------------------------------------------------------
``rxn = AllChem.ReactionFromSmarts(rule_smarts)`` then ``rxn.RunReactants(
(mol,))`` with the substrate parsed by ``MolFromSmiles`` *without* AddHs;
each raw product is cleaned via ``UpdatePropertyCache(strict=False)`` +
partial ``SanitizeMol`` + a SMILES canonicalization round-trip. Products that
fail to sanitize are dropped (never fabricated).
"""

from __future__ import annotations

import csv
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .base import (
    BackendUnavailableError,
    ExpansionBackend,
    ExpansionResult,
    PredictedCompound,
    PredictedReaction,
)

#: Default diameter to use when the caller does not specify one. Diameter 6 is
#: a common middle ground between specificity (large) and recall (small).
_DEFAULT_DIAMETER = 6

#: Default direction: forward rules only.
_DEFAULT_DIRECTION = 1

# Column indices in the flat-rules TSV (0-based), per the verified header.
_COL_RULE_ID = 0
_COL_REACTION_ID = 2
_COL_DIAMETER = 3
_COL_RULE_SMARTS = 5
_COL_EC = 14
_COL_REL_DIRECTION = 16


class RetroRulesBackend:
    """Optional RetroRules (SMARTS rules + RDKit) expansion backend.

    Args:
        config_resolver: Optional ``(key, default=None) -> value`` callable for
            configuration lookups (rules TSV path, default diameter).
        logger: Optional object exposing ``log_info`` / ``log_warning``.
    """

    name = "retrorules"

    def __init__(
        self,
        config_resolver: Optional[Any] = None,
        logger: Optional[Any] = None,
    ) -> None:
        self._cfg = config_resolver
        self._logger = logger
        self._available: Optional[bool] = None
        self._reason: Optional[str] = None
        self._rules_tsv: Optional[Path] = None
        # Cache compiled rules keyed by (diameter, direction).
        self._rule_cache: Dict[Tuple[int, int], List[Tuple[str, str, Any]]] = {}

    # ── logging / config helpers ────────────────────────────────────────

    def _log_info(self, msg: str) -> None:
        if self._logger is not None and hasattr(self._logger, "log_info"):
            self._logger.log_info(msg)

    def _log_warning(self, msg: str) -> None:
        if self._logger is not None and hasattr(self._logger, "log_warning"):
            self._logger.log_warning(msg)

    def _config(self, key: str, default: Any = None) -> Any:
        if self._cfg is None:
            return default
        try:
            return self._cfg(key, default=default)
        except TypeError:
            try:
                return self._cfg(key, default)
            except Exception:
                return default
        except Exception:
            return default

    # ── availability ────────────────────────────────────────────────────

    def _probe(self) -> bool:
        if self._available is not None:
            return self._available
        try:
            import rdkit  # noqa: F401
            from rdkit.Chem import AllChem  # noqa: F401
        except Exception as exc:
            self._available = False
            self._reason = (
                f"RDKit not importable: {type(exc).__name__}: {exc}. "
                f"Install with `pip install rdkit`."
            )
            self._log_warning(f"[retrorules] {self._reason}")
            return False

        tsv = self._locate_rules_tsv()
        if tsv is None:
            self._available = False
            self._reason = (
                "RetroRules rules TSV not found. Download the RetroRules dump "
                "(e.g. retrorules_rr02_rp3_nohs) and point KBUtilLib at "
                "retrorules_rr02_flat_all.tsv via config key "
                "'cheminformatics.retrorules.rules_tsv' or env var "
                "KBUTILLIB_RETRORULES_TSV."
            )
            self._log_warning(f"[retrorules] {self._reason}")
            return False

        self._rules_tsv = tsv
        self._available = True
        self._reason = None
        self._log_info(f"[retrorules] available; rules TSV: {tsv}")
        return True

    def _locate_rules_tsv(self) -> Optional[Path]:
        import os

        candidates: List[Path] = []
        cfg = self._config("cheminformatics.retrorules.rules_tsv", None)
        if cfg:
            candidates.append(Path(str(cfg)).expanduser())
        env = os.environ.get("KBUTILLIB_RETRORULES_TSV")
        if env:
            candidates.append(Path(env).expanduser())
        for cand in candidates:
            try:
                if cand.is_file():
                    return cand.resolve()
            except OSError:
                continue
        return None

    @property
    def available(self) -> bool:
        """Whether RDKit and a RetroRules TSV are both present."""
        return self._probe()

    @property
    def unavailable_reason(self) -> Optional[str]:
        """Explanation when :attr:`available` is ``False``."""
        self._probe()
        return self._reason

    @property
    def capabilities(self) -> "frozenset[str]":
        """Capability tags. RetroRules supports expansion and single-rule apply."""
        return frozenset({"expand", "apply_rule"})

    # ── rule loading ────────────────────────────────────────────────────

    def _load_rules(
        self, diameter: int, direction: int, max_rules: Optional[int]
    ) -> List[Tuple[str, str, Any]]:
        """Stream the TSV and compile rules matching diameter + direction.

        Returns a list of ``(rule_id, ec_number, compiled_rxn)``. Streamed line
        by line so the ~500 MB file is never fully resident. Compiled reactions
        are cached per (diameter, direction); ``max_rules`` truncates for speed.
        """
        from rdkit import RDLogger
        from rdkit.Chem import AllChem

        RDLogger.DisableLog("rdApp.*")

        key = (diameter, direction)
        if key in self._rule_cache and max_rules is None:
            return self._rule_cache[key]

        assert self._rules_tsv is not None
        compiled: List[Tuple[str, str, Any]] = []
        with open(self._rules_tsv, newline="") as fh:
            reader = csv.reader(fh, delimiter="\t")
            header_seen = False
            for row in reader:
                if not header_seen:
                    header_seen = True
                    continue  # skip header line
                if len(row) <= _COL_REL_DIRECTION:
                    continue
                try:
                    if int(row[_COL_DIAMETER]) != diameter:
                        continue
                    if int(row[_COL_REL_DIRECTION]) != direction:
                        continue
                except ValueError:
                    continue
                smarts = row[_COL_RULE_SMARTS]
                rxn = AllChem.ReactionFromSmarts(smarts)
                if rxn is None:
                    continue
                compiled.append((row[_COL_RULE_ID], row[_COL_EC], rxn))
                if max_rules is not None and len(compiled) >= max_rules:
                    break

        if max_rules is None:
            self._rule_cache[key] = compiled
        self._log_info(
            f"[retrorules] compiled {len(compiled)} rule(s) "
            f"(diameter={diameter}, direction={direction})"
        )
        return compiled

    # ── product cleanup ─────────────────────────────────────────────────

    @staticmethod
    def _clean_product(prod: Any) -> Optional[str]:
        """Sanitize a raw RDKit product mol into a canonical SMILES or None.

        Uses the verified RetroRules recipe: update property cache (non-strict),
        partial sanitize, then a SMILES canonicalization round-trip. Returns
        ``None`` if the product cannot be made into a valid molecule -- never a
        fabricated structure.
        """
        from rdkit import Chem

        try:
            prod.UpdatePropertyCache(strict=False)
            Chem.SanitizeMol(
                prod,
                Chem.SanitizeFlags.SANITIZE_ALL
                ^ Chem.SanitizeFlags.SANITIZE_PROPERTIES,
                catchErrors=True,
            )
            smi = Chem.MolToSmiles(prod)
            m2 = Chem.MolFromSmiles(smi)
            if m2 is None:
                return None
            return Chem.MolToSmiles(m2)
        except Exception:
            return None

    @staticmethod
    def _compound_id(smiles: str) -> str:
        """Deterministic short id for a generated compound (hash of SMILES)."""
        h = hashlib.sha1(smiles.encode("utf-8")).hexdigest()[:12]
        return f"RR_C{h}"

    # ── expansion ───────────────────────────────────────────────────────

    def expand(
        self,
        seed_smiles: Mapping[str, str],
        generations: int = 1,
        diameter: Optional[int] = None,
        direction: Optional[int] = None,
        max_rules: Optional[int] = None,
        **kwargs: Any,
    ) -> ExpansionResult:
        """Apply RetroRules operators to seed compounds.

        Args:
            seed_smiles: Mapping of compound id -> SMILES for the seed set.
            generations: Number of expansion rounds (products feed the next
                round). Diameters are large, so >1 generation can blow up; keep
                small.
            diameter: Rule diameter to use (one of 2..16). Defaults to config
                ``cheminformatics.retrorules.diameter`` or 6.
            direction: ``1`` forward (default) or ``-1`` retro.
            max_rules: Optional cap on the number of rules to apply (speed/test
                bound); ``None`` uses all rules at the diameter.
            **kwargs: Ignored (forward-compatible).

        Returns:
            An :class:`ExpansionResult`.

        Raises:
            BackendUnavailableError: If RDKit or the rules TSV is unavailable.
        """
        if not self.available:
            raise BackendUnavailableError(self._reason or "retrorules unavailable")
        if not seed_smiles:
            raise BackendUnavailableError("no seed compounds supplied")

        from rdkit import Chem

        diam = int(
            diameter
            if diameter is not None
            else (self._config("cheminformatics.retrorules.diameter", _DEFAULT_DIAMETER))
        )
        direc = int(direction if direction is not None else _DEFAULT_DIRECTION)

        result = ExpansionResult(backend=self.name, generations=generations)
        result.raw.update({"diameter": diam, "direction": direc})

        rules = self._load_rules(diam, direc, max_rules)
        if not rules:
            result.warnings.append(
                f"no RetroRules rules at diameter={diam}, direction={direc}"
            )
            return result

        # Seed the result with the starting compounds.
        # frontier maps compound_id -> (smiles, rdkit mol)
        frontier: Dict[str, Tuple[str, Any]] = {}
        for cid, smi in seed_smiles.items():
            mol = Chem.MolFromSmiles(smi)
            if mol is None:
                result.warnings.append(f"could not parse seed SMILES for '{cid}': {smi}")
                continue
            canon = Chem.MolToSmiles(mol)
            result.compounds[cid] = PredictedCompound(
                compound_id=cid, smiles=canon, generation=0, is_seed=True
            )
            frontier[cid] = (canon, mol)

        if not frontier:
            return result

        seen_smiles = {c.smiles for c in result.compounds.values() if c.smiles}
        for gen in range(1, generations + 1):
            next_frontier: Dict[str, Tuple[str, Any]] = {}
            for sub_id, (sub_smi, sub_mol) in frontier.items():
                for rule_id, ec, rxn in rules:
                    try:
                        product_sets = rxn.RunReactants((sub_mol,))
                    except Exception:
                        continue
                    for ps in product_sets:
                        product_ids: List[str] = []
                        for raw_prod in ps:
                            psmi = self._clean_product(raw_prod)
                            if not psmi:
                                continue
                            pcid = self._compound_id(psmi)
                            if pcid not in result.compounds:
                                result.compounds[pcid] = PredictedCompound(
                                    compound_id=pcid,
                                    smiles=psmi,
                                    generation=gen,
                                    is_seed=False,
                                )
                            product_ids.append(pcid)
                            if psmi not in seen_smiles:
                                seen_smiles.add(psmi)
                                pmol = Chem.MolFromSmiles(psmi)
                                if pmol is not None:
                                    next_frontier[pcid] = (psmi, pmol)
                        if not product_ids:
                            continue
                        rxn_hash = hashlib.sha1(
                            f"{rule_id}|{sub_smi}|{'.'.join(sorted(product_ids))}".encode()
                        ).hexdigest()[:14]
                        result.reactions.append(
                            PredictedReaction(
                                reaction_id=f"RR_R{rxn_hash}",
                                backend=self.name,
                                operator=rule_id,
                                reactant_ids=[sub_id],
                                product_ids=product_ids,
                                generation=gen,
                                raw={"ec_number": ec, "diameter": diam},
                            )
                        )
            frontier = next_frontier
            if not frontier:
                break

        if not result.is_expanded:
            result.warnings.append(
                f"no RetroRules rule (diameter={diam}, direction={direc}) matched "
                f"the supplied seeds"
            )
        return result


# Static structural conformance check.
_: type[ExpansionBackend] = RetroRulesBackend
