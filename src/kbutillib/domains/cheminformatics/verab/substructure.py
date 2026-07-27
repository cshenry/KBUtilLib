"""MethoxyAromaticFilter — RDKit-lazy substructure filter over a biochem DB.

Scans ``biochem.biochem_db.compounds`` for compounds whose SMILES contain an
aromatic methoxy group (SMARTS ``c-[OX2]-[CH3]``), using RDKit for the actual
substructure match.  RDKit is imported lazily via :meth:`MethoxyAromaticFilter._probe`;
if it is absent the filter reports ``available == False`` and raises
:class:`~kbutillib.cheminformatics.base.BackendUnavailableError` on any compute
call, rather than crashing at import time.

The filter result is cached per database-object identity so that repeated calls
on the same biochem DB do not repeat the full scan.

No top-level RDKit import — see the module ``_probe`` / ``available`` pattern
copied from :class:`~kbutillib.cheminformatics.retrorules_backend.RetroRulesBackend`.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..base import BackendUnavailableError
from .smarts import METHOXY_AROMATIC_SMARTS


class MethoxyAromaticFilter:
    """Substructure filter that identifies methoxy-aromatic compounds in a
    biochem DB by iterating over compound SMILES and running an RDKit
    ``HasSubstructMatch`` against :data:`~.smarts.METHOXY_AROMATIC_SMARTS`.

    Args:
        logger: Optional object exposing ``log_info`` / ``log_warning`` methods
            (same contract as RetroRulesBackend's logger parameter).
    """

    def __init__(self, logger: Optional[Any] = None) -> None:
        self._logger = logger
        # Tri-state: None = not yet probed, True = available, False = unavailable.
        self._available: Optional[bool] = None
        self._reason: Optional[str] = None
        # Cache: db_identity (id of the biochem_db object) -> full enumerate result
        self._cache: Dict[int, Dict[str, Any]] = {}

    # ── logging helpers ─────────────────────────────────────────────────

    def _log_info(self, msg: str) -> None:
        if self._logger is not None and hasattr(self._logger, "log_info"):
            self._logger.log_info(msg)

    def _log_warning(self, msg: str) -> None:
        if self._logger is not None and hasattr(self._logger, "log_warning"):
            self._logger.log_warning(msg)

    # ── availability probe (RDKit-lazy, copy retrorules pattern) ────────

    def _probe(self) -> bool:
        """Lazily test whether RDKit is importable.

        Sets :attr:`_available` and :attr:`_reason` on first call; returns
        the cached result on subsequent calls.
        """
        if self._available is not None:
            return self._available
        try:
            import rdkit  # noqa: F401
            from rdkit.Chem import MolFromSmarts, MolFromSmiles  # noqa: F401
        except Exception as exc:
            self._available = False
            self._reason = (
                f"rdkit not importable: {type(exc).__name__}: {exc}. "
                "Install with `conda install -c conda-forge rdkit` or "
                "`pip install rdkit`."
            )
            self._log_warning(f"[MethoxyAromaticFilter] {self._reason}")
            return False

        self._available = True
        self._reason = None
        self._log_info("[MethoxyAromaticFilter] RDKit available.")
        return True

    @property
    def available(self) -> bool:
        """``True`` when RDKit is importable and the filter can run."""
        return self._probe()

    @property
    def unavailable_reason(self) -> Optional[str]:
        """Human-readable explanation when :attr:`available` is ``False``."""
        self._probe()
        return self._reason

    # ── public API ───────────────────────────────────────────────────────

    def is_methoxy_aromatic(self, smiles: str) -> bool:
        """Return ``True`` if *smiles* contains an aromatic methoxy group.

        Uses RDKit ``MolFromSmiles`` + ``HasSubstructMatch`` against
        :data:`~.smarts.METHOXY_AROMATIC_SMARTS`.

        Args:
            smiles: A SMILES string to classify.

        Returns:
            ``True`` if the compound matches the aromatic-methoxy pattern,
            ``False`` otherwise (including if the SMILES cannot be parsed).

        Raises:
            BackendUnavailableError: If RDKit is not installed.
        """
        if not self._probe():
            raise BackendUnavailableError(
                f"MethoxyAromaticFilter.is_methoxy_aromatic requires RDKit. "
                f"{self._reason}"
            )

        from rdkit.Chem import MolFromSmarts, MolFromSmiles  # noqa: PLC0415

        mol = MolFromSmiles(smiles)
        if mol is None:
            return False
        query = MolFromSmarts(METHOXY_AROMATIC_SMARTS)
        if query is None:
            # Should never happen with a constant valid SMARTS, but be safe.
            return False
        return mol.HasSubstructMatch(query)

    def enumerate_from_biochem(
        self,
        biochem: Any,
        *,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Scan *biochem*'s compound DB for methoxy-aromatic compounds.

        Iterates ``biochem.biochem_db.compounds``, reads each compound's
        ``cpd.annotation.get("SMILE")`` SMILES, skips obsolete or
        missing-SMILE entries, and classifies the rest with
        :meth:`is_methoxy_aromatic`.  Results are cached per DB-object
        identity so repeated calls on the same biochem instance are cheap.

        Args:
            biochem: Any object exposing ``.biochem_db.compounds`` as an
                iterable of compound objects, each with:

                * ``.annotation`` — ``dict``-like with an optional ``"SMILE"``
                  key.
                * ``.is_obsolete`` — bool/truthy.
                * ``.id`` — compound identifier string.
                * ``.name`` — display name string.
                * ``.formula`` — molecular formula string or ``None``.

            limit: If given, stop after collecting *limit* matching compounds
                (useful for quick sampling).

        Returns:
            A dict with two keys:

            * ``"compounds"`` — list of dicts, one per matched compound,
              each with keys ``id``, ``name``, ``smiles``, ``formula``.
            * ``"skipped"`` — dict with counts:
              ``"obsolete"``, ``"missing_smile"``, ``"unparseable_smile"``,
              ``"total_scanned"``.

        Raises:
            BackendUnavailableError: If RDKit is not installed.
        """
        if not self._probe():
            raise BackendUnavailableError(
                f"MethoxyAromaticFilter.enumerate_from_biochem requires RDKit. "
                f"{self._reason}"
            )

        db = biochem.biochem_db
        db_key = id(db)

        if db_key in self._cache:
            self._log_info(
                f"[MethoxyAromaticFilter] returning cached result for db id={db_key}"
            )
            return self._cache[db_key]

        hits: List[Dict[str, Any]] = []
        skipped_obsolete = 0
        skipped_missing = 0
        skipped_unparseable = 0
        total_scanned = 0

        for cpd in db.compounds:
            total_scanned += 1

            if getattr(cpd, "is_obsolete", False):
                skipped_obsolete += 1
                continue

            annotation = getattr(cpd, "annotation", None) or {}
            smiles = annotation.get("SMILE") if hasattr(annotation, "get") else None
            if not smiles:
                skipped_missing += 1
                continue

            # Validate parseable
            from rdkit.Chem import MolFromSmiles  # noqa: PLC0415

            mol = MolFromSmiles(smiles)
            if mol is None:
                skipped_unparseable += 1
                continue

            from rdkit.Chem import MolFromSmarts  # noqa: PLC0415

            query = MolFromSmarts(METHOXY_AROMATIC_SMARTS)
            if mol.HasSubstructMatch(query):
                hits.append(
                    {
                        "id": getattr(cpd, "id", None),
                        "name": getattr(cpd, "name", None),
                        "smiles": smiles,
                        "formula": getattr(cpd, "formula", None),
                    }
                )
                if limit is not None and len(hits) >= limit:
                    break

        result: Dict[str, Any] = {
            "compounds": hits,
            "skipped": {
                "obsolete": skipped_obsolete,
                "missing_smile": skipped_missing,
                "unparseable_smile": skipped_unparseable,
                "total_scanned": total_scanned,
            },
        }

        # Only cache when no limit was imposed (full scan result is complete).
        if limit is None:
            self._cache[db_key] = result

        self._log_info(
            f"[MethoxyAromaticFilter] scanned {total_scanned} compounds, "
            f"found {len(hits)} methoxy-aromatic hits "
            f"(obsolete={skipped_obsolete}, missing_smile={skipped_missing}, "
            f"unparseable={skipped_unparseable})."
        )

        return result
