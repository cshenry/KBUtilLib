"""Reaction-similarity utilities backed by the BERDL reaction-similarity table.

This module expands hypotheses from the function-annotation tools to chemically
similar reactions, and clusters reaction sets by chemical distinctness. It is
**remote-first**: all biochemistry data is read live from the BERDL data lake
(``kbase_msd_biochemistry``) through :class:`KBBERDLUtilsImpl`, so no local
database checkout is required.

Two similarity regimes are exposed and deliberately never mixed on one scale:

* ``method="berdl"`` (default) — the precomputed pairwise reaction similarity
  (the ``reaction_similarity`` table, ~6.7e8 pairs spanning the full range).
  Used for near-neighbour expansion, pairwise lookup, distance matrices, and
  clustering. This is the authoritative metric.
* ``method="drfp"`` — a client-side DRFP reaction-difference fingerprint
  recomputed from reconstructed reaction SMILES. Used for reactions / SMARTS
  that are not present in the table (e.g. novel queries). Its scale differs from
  the stored metric, so it is reported separately.

Entry points accept either a ModelSEED reaction id (``seed.reaction:rxnNNNNN``)
or a reaction SMILES/SMARTS string.

Heavy dependencies (``rdkit``, ``drfp``, ``numpy``, ``scipy``,
``scikit-learn``, ``hdbscan``) are imported lazily, so importing this module
never fails on a machine that only needs the BERDL lookups.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Any, Optional

from .shared_env_utils import SharedEnvUtils

if TYPE_CHECKING:  # pragma: no cover
    import numpy as np

    from .kb_berdl_utils import KBBERDLUtilsImpl
    from .ms_biochem_utils import MSBiochemUtilsImpl

DEFAULT_DATABASE = "kbase_msd_biochemistry"

# ModelSEED ids look like ``seed.reaction:rxn00001`` / ``seed.compound:cpd00001``.
# We only ever interpolate validated ids into SQL (the BERDL client takes raw
# SQL, no bound parameters), so this regex is also the injection guard.
_ID_RE = re.compile(r"^[A-Za-z0-9_.:\-]+$")


def _require(module: str, extra: str) -> Any:
    """Import an optional dependency, with an actionable error message.

    Args:
        module: Importable module name (e.g. ``"rdkit"``).
        extra: Human-readable install hint shown if the import fails.

    Returns:
        The imported module object.

    Raises:
        ImportError: If the module is not installed.
    """
    try:
        return __import__(module)
    except ImportError as exc:  # pragma: no cover - exercised via message only
        raise ImportError(
            f"'{module}' is required for this operation. Install it with: {extra}"
        ) from exc


class MSReactionSimilarityUtils(SharedEnvUtils):
    """Retrieve and cluster chemically similar reactions from BERDL.

    Composes a :class:`KBBERDLUtilsImpl` (data access) and, optionally, a
    :class:`MSBiochemUtilsImpl`. All data is read remotely from BERDL; nothing
    is cached to disk by this class beyond in-memory SMILES lookups for the
    lifetime of the instance.
    """

    def __init__(
        self,
        biochem: Optional["MSBiochemUtilsImpl"] = None,
        berdl: Optional["KBBERDLUtilsImpl"] = None,
        *,
        database: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the reaction-similarity utilities.

        Args:
            biochem: Optional ModelSEED biochemistry utility (reserved for future
                local-resolution paths; not required for remote operation).
            berdl: Optional BERDL utility. If omitted, one is created lazily from
                this instance's shared environment (and KBase token).
            database: BERDL database name holding the biochemistry tables.
                Defaults to ``kbase_msd_biochemistry``.
            **kwargs: Additional arguments forwarded to :class:`SharedEnvUtils`.
        """
        super().__init__(**kwargs)
        self._biochem = biochem
        self._berdl = berdl
        self.database = database or DEFAULT_DATABASE
        self._smiles_cache: dict[str, Optional[str]] = {}
        self._rxn_smiles_cache: dict[str, Optional[str]] = {}

    # ── data access ──────────────────────────────────────────────────────

    @property
    def berdl(self) -> "KBBERDLUtilsImpl":
        """The BERDL data-access utility (created lazily from the environment).

        Returns:
            A :class:`KBBERDLUtilsImpl` bound to this instance's token.
        """
        if self._berdl is None:
            from .kb_berdl_utils import KBBERDLUtilsImpl

            self._berdl = KBBERDLUtilsImpl(self)  # type: ignore[no-untyped-call]
        return self._berdl

    #: Maximum ``limit`` the BERDL delta API accepts in one request.
    MAX_PAGE = 1000
    #: Per-request timeout (s). Successful queries return in seconds; a longer
    #: wait usually means the shared engine hung, so we fail fast and retry.
    QUERY_TIMEOUT = 60
    #: Transient-failure retries (the shared engine occasionally 503s / times out).
    QUERY_RETRIES = 4

    def _query_page(self, sql: str, limit: int, offset: int) -> dict[str, Any]:
        """Run one query page, retrying transient failures.

        Args:
            sql: SQL string to execute.
            limit: Row limit for this page (<= ``MAX_PAGE``).
            offset: Row offset for this page.

        Returns:
            The successful BERDL result dict.

        Raises:
            RuntimeError: If every attempt fails.
        """
        result: dict[str, Any] = {}
        for attempt in range(self.QUERY_RETRIES):
            result = self.berdl.query(
                sql, limit=limit, offset=offset, timeout=self.QUERY_TIMEOUT
            )
            if result.get("success"):
                return result
            if attempt < self.QUERY_RETRIES - 1:
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(
            f"BERDL query failed after {self.QUERY_RETRIES} attempts: "
            f"{result.get('error')}\nSQL: {sql}"
        )

    def _fetch_all(self, sql: str, page_size: int = 1000, max_rows: int = 5_000_000) -> list[dict[str, Any]]:
        """Run a query and page through results until exhausted or capped.

        Args:
            sql: SQL string to execute against BERDL.
            page_size: Rows requested per page (clamped to ``MAX_PAGE`` = 1000,
                the API's maximum).
            max_rows: Stop once this many rows have been collected.

        Returns:
            A list of row dicts.

        Raises:
            RuntimeError: If a page query fails after retries.
        """
        page_size = min(page_size, self.MAX_PAGE)
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            result = self._query_page(sql, limit=page_size, offset=offset)
            page = result.get("data") or []
            rows.extend(page)
            if len(rows) >= max_rows or not result.get("has_more") or not page:
                break
            offset += len(page)
        return rows

    @staticmethod
    def _validate_id(identifier: str) -> str:
        """Validate a ModelSEED-style id before SQL interpolation.

        Args:
            identifier: Candidate reaction/compound id.

        Returns:
            The same id, unchanged.

        Raises:
            ValueError: If the id contains characters outside the safe set.
        """
        if not isinstance(identifier, str) or not _ID_RE.match(identifier):
            raise ValueError(f"Unsafe or malformed identifier: {identifier!r}")
        return identifier

    def _id_list_sql(self, ids: list[str]) -> str:
        """Build a validated, quoted SQL ``IN`` list from ids.

        Args:
            ids: Identifiers to include.

        Returns:
            A string like ``'a','b','c'`` safe to embed in an ``IN (...)``.
        """
        return ",".join("'" + self._validate_id(i) + "'" for i in ids)

    # ── id -> reaction SMILES ────────────────────────────────────────────

    def _load_molecule_smiles(self, molecule_ids: list[str]) -> None:
        """Populate the in-memory SMILES cache for the given compounds.

        Args:
            molecule_ids: Compound ids to look up (only uncached ones are queried).
        """
        missing = [m for m in dict.fromkeys(molecule_ids) if m not in self._smiles_cache]
        if not missing:
            return
        found: dict[str, Any] = {}
        for i in range(0, len(missing), 100):
            chunk = missing[i : i + 100]
            rows = self._fetch_all(
                f"SELECT id, smiles FROM {self.database}.molecule "
                f"WHERE id IN ({self._id_list_sql(chunk)})"
            )
            for r in rows:
                found[r["id"]] = r.get("smiles")
        for m in missing:
            smiles = found.get(m)
            self._smiles_cache[m] = str(smiles).strip() if smiles and str(smiles).strip() else None

    def _reagents_for(self, reaction_ids: list[str]) -> dict[str, list[tuple[str, float]]]:
        """Fetch reagent rows for each reaction (one equality query per id).

        A bare ``reaction_id IN (...)`` over the 2.6e5-row ``reagent`` table is
        not index-served on the shared engine and is unreliable; the equality
        predicate ``reaction_id = 'id'`` is fast, so we issue one per reaction.

        Args:
            reaction_ids: Reaction ids to fetch reagents for.

        Returns:
            A dict mapping each reaction id to a list of
            ``(molecule_id, stoichiometry)`` tuples.
        """
        out: dict[str, list[tuple[str, float]]] = {}
        for rid in dict.fromkeys(reaction_ids):
            self._validate_id(rid)
            rows = self._fetch_all(
                f"SELECT molecule_id, stoichiometry "
                f"FROM {self.database}.reagent WHERE reaction_id = '{rid}'"
            )
            out[rid] = [(r["molecule_id"], float(r["stoichiometry"])) for r in rows]
        return out

    def _build_reaction_smiles(
        self, reagents: list[tuple[str, float]]
    ) -> Optional[str]:
        """Assemble a reaction SMILES from (molecule_id, stoichiometry) reagents.

        Args:
            reagents: ``(molecule_id, stoichiometry)`` tuples for one reaction.

        Returns:
            A ``"reactants>>products"`` SMILES, or ``None`` if a participant lacks
            a SMILES or either side is empty.
        """
        reactants, products = [], []
        for mol, stoich in reagents:
            smiles = self._smiles_cache.get(mol)
            if not smiles:
                return None
            (reactants if stoich < 0 else products).append(smiles)
        if not reactants or not products:
            return None
        return ".".join(reactants) + ">>" + ".".join(products)

    def get_reaction_smiles(self, reaction_id: str) -> Optional[str]:
        """Reconstruct a reaction SMILES from BERDL reagent + molecule rows.

        Reactants (negative stoichiometry) and products (positive) are joined
        into ``"r1.r2>>p1.p2"``. Compounds without a SMILES make the reaction
        unresolvable. Uses two single-table queries (no SQL JOIN).

        Args:
            reaction_id: ModelSEED reaction id (``seed.reaction:rxnNNNNN``).

        Returns:
            A reaction SMILES, or ``None`` if any participant lacks a SMILES or
            either side is empty.
        """
        if reaction_id in self._rxn_smiles_cache:
            return self._rxn_smiles_cache[reaction_id]
        self._validate_id(reaction_id)
        reagents = self._reagents_for([reaction_id]).get(reaction_id, [])
        if not reagents:
            self._rxn_smiles_cache[reaction_id] = None
            return None
        self._load_molecule_smiles([m for m, _ in reagents])
        rxn = self._build_reaction_smiles(reagents)
        self._rxn_smiles_cache[reaction_id] = rxn
        return rxn

    def _reaction_smiles_batch(self, reaction_ids: list[str]) -> dict[str, Optional[str]]:
        """Reconstruct reaction SMILES for many reactions with two batch queries.

        Args:
            reaction_ids: Reaction ids to reconstruct.

        Returns:
            A dict mapping each reaction id to its reaction SMILES (or ``None``).
        """
        ids = [i for i in dict.fromkeys(reaction_ids) if i not in self._rxn_smiles_cache]
        if ids:
            reagents = self._reagents_for(ids)
            all_mols = [m for rgs in reagents.values() for m, _ in rgs]
            self._load_molecule_smiles(all_mols)
            for rid in ids:
                self._rxn_smiles_cache[rid] = self._build_reaction_smiles(
                    reagents.get(rid, [])
                )
        return {rid: self._rxn_smiles_cache.get(rid) for rid in reaction_ids}

    def resolve_to_smarts(self, query: str) -> Optional[str]:
        """Resolve a reaction id OR a raw reaction string to reaction SMILES.

        Args:
            query: A ModelSEED reaction id, or a reaction SMILES/SMARTS that
                already contains ``>>``.

        Returns:
            A reaction SMILES string, or ``None`` if an id cannot be resolved.
        """
        if ">>" in query:
            return query
        return self.get_reaction_smiles(query)

    # ── regime 1: BERDL near-neighbour expansion ─────────────────────────

    def similar_reactions(
        self,
        reaction_id: str,
        *,
        min_similarity: float = 0.0,
        top_k: Optional[int] = 50,
        exclude_self: bool = True,
        both_directions: bool = True,
    ) -> list[dict[str, Any]]:
        """Look up reactions similar to ``reaction_id`` in the BERDL table.

        Args:
            reaction_id: Query reaction id (``seed.reaction:rxnNNNNN``).
            min_similarity: Minimum stored similarity to return.
            top_k: Maximum number of neighbours (highest similarity first).
                ``None`` returns all above ``min_similarity``.
            exclude_self: Drop the query reaction from its own neighbour list.
            both_directions: The table stores each unordered pair once; when
                ``True`` both ``reaction_1`` and ``reaction_2`` are searched.

        Returns:
            A list of ``{"reaction_id": str, "similarity": float}`` sorted by
            descending similarity.
        """
        self._validate_id(reaction_id)
        tbl = f"{self.database}.reaction_similarity"
        directions = [("reaction_1", "reaction_2")]
        if both_directions:
            directions.append(("reaction_2", "reaction_1"))
        best: dict[str, float] = {}
        for me_col, other_col in directions:
            sql = (
                f"SELECT {other_col} AS rid, similarity FROM {tbl} "
                f"WHERE {me_col} = '{reaction_id}' AND similarity >= {float(min_similarity)} "
                f"ORDER BY similarity DESC"
            )
            rows = (
                self._fetch_all(sql, max_rows=int(top_k))
                if top_k is not None
                else self._fetch_all(sql)
            )
            for r in rows:
                rid = r["rid"]
                if exclude_self and rid == reaction_id:
                    continue
                sim = float(r["similarity"])
                if rid not in best or sim > best[rid]:
                    best[rid] = sim
        ranked = sorted(
            ({"reaction_id": k, "similarity": v} for k, v in best.items()),
            key=lambda d: d["similarity"],
            reverse=True,
        )
        return ranked[:top_k] if top_k is not None else ranked

    def expand_reactions(
        self,
        reaction_ids: list[str],
        *,
        min_similarity: float = 0.7,
        top_k_per: int = 5,
    ) -> dict[str, list[dict]]:
        """Expand each reaction in a set with its top similar neighbours.

        Intended for the inner-loop annotation/gap-fill pipeline: given the
        reactions hypothesised for a genome, surface chemically similar
        reactions as additional candidates.

        Args:
            reaction_ids: Reaction ids to expand.
            min_similarity: Minimum similarity for a neighbour to be included.
            top_k_per: Maximum neighbours returned per input reaction.

        Returns:
            A dict mapping each input reaction id to its neighbour list.
        """
        return {
            rid: self.similar_reactions(
                rid, min_similarity=min_similarity, top_k=top_k_per
            )
            for rid in reaction_ids
        }

    def similarity(self, a: str, b: str, *, method: str = "berdl") -> Optional[float]:
        """Return the similarity between two reactions.

        Args:
            a: First reaction id (or SMILES for ``method="drfp"``).
            b: Second reaction id (or SMILES for ``method="drfp"``).
            method: ``"berdl"`` reads the stored value (``None`` if the pair
                is absent); ``"drfp"`` recomputes a DRFP Tanimoto (its own scale).

        Returns:
            The similarity as a float, or ``None`` for an absent ``berdl`` pair.

        Raises:
            ValueError: If ``method`` is not recognised.
        """
        if method == "berdl":
            self._validate_id(a)
            self._validate_id(b)
            tbl = f"{self.database}.reaction_similarity"
            rows = self._fetch_all(
                f"SELECT similarity FROM {tbl} "
                f"WHERE (reaction_1='{a}' AND reaction_2='{b}') "
                f"OR (reaction_1='{b}' AND reaction_2='{a}')"
            )
            return float(rows[0]["similarity"]) if rows else None
        if method == "drfp":
            fa = self.reaction_fingerprint(a, method="drfp")
            fb = self.reaction_fingerprint(b, method="drfp")
            if fa is None or fb is None:
                return None
            return _drfp_tanimoto(fa, fb)
        raise ValueError(f"Unknown method: {method!r} (use 'berdl' or 'drfp')")

    # ── regime 2: DRFP recompute (novel SMARTS / absent pairs) ───────────

    def reaction_fingerprint(self, query: str, *, method: str = "drfp") -> Optional[Any]:
        """Compute a reaction fingerprint for a reaction id or SMILES/SMARTS.

        Args:
            query: Reaction id or reaction SMILES/SMARTS.
            method: Only ``"drfp"`` is supported in v1.

        Returns:
            A boolean ``numpy`` array (folded DRFP), or ``None`` if the reaction
            cannot be resolved to SMILES.

        Raises:
            ValueError: If ``method`` is not recognised.
        """
        if method != "drfp":
            raise ValueError(f"Unknown fingerprint method: {method!r}")
        smiles = self.resolve_to_smarts(query)
        if smiles is None:
            return None
        np = _require("numpy", "pip install numpy")
        from drfp import DrfpEncoder  # type: ignore

        try:
            return np.asarray(DrfpEncoder.encode([smiles])[0], dtype=bool)
        except Exception:
            return None

    def similar_to_smarts(
        self,
        query: str,
        candidate_ids: list[str],
        *,
        top_k: int = 25,
        exclude_self: bool = True,
    ) -> list[dict[str, Any]]:
        """Rank candidate reactions by recomputed (DRFP) similarity to a query.

        This is the entry path for a reaction **SMARTS / SMILES** (which has no
        id, so the stored table cannot be looked up): the query and each
        candidate are scored by a client-side DRFP reaction fingerprint. Results
        are on the recompute scale, distinct from the stored BERDL similarity.

        Args:
            query: A reaction SMILES/SMARTS (``"A>>B"``) or a reaction id (which
                is resolved to SMILES first).
            candidate_ids: The reaction ids to search over (e.g. the
                reaction-mapping output, a subsystem, or a catalogue subset). A
                novel SMARTS cannot be searched against all reactions without a
                prebuilt fingerprint index, so the search space is explicit.
            top_k: Maximum number of ranked candidates to return.
            exclude_self: Drop a candidate equal to the query id.

        Returns:
            ``[{"reaction_id", "similarity", "method": "drfp"}, ...]`` sorted by
            descending recomputed similarity.
        """
        np = _require("numpy", "pip install numpy")
        from drfp import DrfpEncoder  # type: ignore

        qsmiles = self.resolve_to_smarts(query)
        if qsmiles is None:
            return []
        try:
            qfp = np.asarray(DrfpEncoder.encode([qsmiles])[0], dtype=bool)
        except Exception:
            return []

        cand_smiles = self._reaction_smiles_batch(
            [c for c in dict.fromkeys(candidate_ids) if not (exclude_self and c == query)]
        )
        resolvable = [(rid, s) for rid, s in cand_smiles.items() if s]
        scored: list[dict[str, Any]] = []
        if resolvable:
            encoded = DrfpEncoder.encode([s for _, s in resolvable])
            for (rid, _), fp in zip(resolvable, encoded):
                scored.append({
                    "reaction_id": rid,
                    "similarity": _drfp_tanimoto(qfp, np.asarray(fp, dtype=bool)),
                    "method": "drfp",
                })
        scored.sort(key=lambda d: d["similarity"], reverse=True)
        return scored[:top_k]

    def find_similar(
        self,
        query: str,
        *,
        candidate_ids: Optional[list[str]] = None,
        top_k: int = 25,
        min_similarity: float = 0.0,
        exclude_self: bool = True,
    ) -> list[dict[str, Any]]:
        """Unified entry: a reaction **id OR SMARTS** -> similar reactions.

        Dispatches by input type:

        * **reaction id** -> stored BERDL similarity (:meth:`similar_reactions`),
          the authoritative metric (results tagged ``method="berdl"``);
        * **SMARTS / SMILES** (contains ``">>"``) -> recompute ranking over
          ``candidate_ids`` (:meth:`similar_to_smarts`), tagged ``method="drfp"``.

        Args:
            query: A reaction id or a reaction SMILES/SMARTS.
            candidate_ids: Required for a SMARTS query — the reactions to search.
            top_k: Maximum neighbours to return.
            min_similarity: Minimum similarity to keep.
            exclude_self: Exclude the query from its own results.

        Returns:
            A list of similar-reaction dicts (shape per the dispatched method).

        Raises:
            ValueError: If a SMARTS query is given without ``candidate_ids``.
        """
        if ">>" in query:
            if not candidate_ids:
                raise ValueError(
                    "A SMARTS/SMILES query requires candidate_ids (the reactions "
                    "to search): the stored table is keyed by reaction id, so a "
                    "novel reaction is scored by client-side recompute over an "
                    "explicit candidate set."
                )
            results = self.similar_to_smarts(
                query, candidate_ids, top_k=top_k, exclude_self=exclude_self
            )
            return [d for d in results if d["similarity"] >= min_similarity]
        out = self.similar_reactions(
            query, min_similarity=min_similarity, top_k=top_k, exclude_self=exclude_self
        )
        for d in out:
            d["method"] = "berdl"
        return out

    # ── distance matrix + clustering ─────────────────────────────────────

    def distance_matrix(
        self,
        reaction_ids: list[str],
        *,
        source: str = "berdl",
        fill_missing: float = 1.0,
    ) -> tuple["np.ndarray", list[str], dict[str, Any]]:
        """Build a pairwise distance matrix for a set of reactions.

        Args:
            reaction_ids: Reaction ids to compare.
            source: ``"berdl"`` builds distances from the stored similarities
                (``distance = 1 - clip(similarity, 0, 1)``; pairs absent from the
                table take ``fill_missing``). ``"drfp"`` recomputes DRFP
                distances (``1 - Tanimoto``) from reconstructed SMILES.
            fill_missing: Distance assigned to ``berdl`` pairs with no stored
                similarity.

        Returns:
            A tuple ``(D, ids, info)`` where ``D`` is an ``(n, n)`` float64
            matrix (zero diagonal, symmetric), ``ids`` is the ordered id list
            (``drfp`` drops unresolvable reactions), and ``info`` reports
            coverage / dropped ids.

        Raises:
            ValueError: If ``source`` is not recognised.
        """
        np = _require("numpy", "pip install numpy")
        ids = list(dict.fromkeys(reaction_ids))  # dedupe, preserve order
        for i in ids:
            self._validate_id(i)

        if source == "berdl":
            # Per-row equality+IN: a double IN(...) AND IN(...) over the 6.7e8-row
            # table is not index-served and times out, but reaction_1 = 'id' is.
            # Each stored unordered pair is found exactly once (when its
            # reaction_1 endpoint is the queried row), so N queries suffice.
            index = {rid: k for k, rid in enumerate(ids)}
            n = len(ids)
            D = np.full((n, n), float(fill_missing), dtype=np.float64)
            np.fill_diagonal(D, 0.0)
            id_sql = self._id_list_sql(ids)
            present = 0
            for rid in ids:
                rows = self._fetch_all(
                    f"SELECT reaction_2 AS other, similarity "
                    f"FROM {self.database}.reaction_similarity "
                    f"WHERE reaction_1 = '{rid}' AND reaction_2 IN ({id_sql})"
                )
                for r in rows:
                    other = r["other"]
                    if other in index and other != rid:
                        sim = min(max(float(r["similarity"]), 0.0), 1.0)
                        d = 1.0 - sim
                        D[index[rid], index[other]] = d
                        D[index[other], index[rid]] = d
                        present += 1
            possible = n * (n - 1) // 2
            info = {
                "source": "berdl",
                "n": n,
                "pairs_present": present,
                "pairs_possible": possible,
                "coverage": round(present / possible, 4) if possible else None,
                "dropped": [],
            }
            return D, ids, info

        if source == "drfp":
            from drfp import DrfpEncoder  # type: ignore

            smiles_map = self._reaction_smiles_batch(ids)
            resolved = [(rid, smiles_map[rid]) for rid in ids if smiles_map.get(rid)]
            kept, fps = [], []
            if resolved:
                try:
                    encoded = DrfpEncoder.encode([s for _, s in resolved])
                except Exception:
                    encoded = []
                for (rid, _), fp in zip(resolved, encoded):
                    kept.append(rid)
                    fps.append(np.asarray(fp, dtype=bool))
            n = len(kept)
            D = np.zeros((n, n), dtype=np.float64)
            if n:
                X = np.array(fps, dtype=np.float32)
                inter = X @ X.T
                sums = X.sum(1)
                union = sums[:, None] + sums[None, :] - inter
                with np.errstate(divide="ignore", invalid="ignore"):
                    sim = np.where(union > 0, inter / union, 1.0)
                D = np.clip(1.0 - sim, 0.0, 1.0)
                np.fill_diagonal(D, 0.0)
                D = (D + D.T) / 2
            info = {
                "source": "drfp",
                "n": n,
                "dropped": [i for i in ids if i not in set(kept)],
            }
            return D, kept, info

        raise ValueError(f"Unknown source: {source!r} (use 'berdl' or 'drfp')")

    def _transport_ids(self, reaction_ids: list[str]) -> set[str]:
        """Return the subset of ids flagged ``is_transport`` in BERDL.

        Args:
            reaction_ids: Reaction ids to check.

        Returns:
            The set of ids whose ``reaction.is_transport`` is true.
        """
        if not reaction_ids:
            return set()
        id_sql = self._id_list_sql(reaction_ids)
        rows = self._fetch_all(
            f"SELECT id FROM {self.database}.reaction "
            f"WHERE id IN ({id_sql}) AND is_transport = true"
        )
        return {r["id"] for r in rows}

    def cluster(
        self,
        reaction_ids: list[str],
        *,
        source: str = "berdl",
        algorithm: str = "agglomerative",
        distance_threshold: float = 0.4,
        min_cluster_size: int = 5,
        segregate_transport: bool = True,
        return_distance_matrix: bool = False,
    ) -> dict[str, Any]:
        """Cluster a set of reactions by chemical distinctness.

        Args:
            reaction_ids: Reaction ids to cluster.
            source: Distance source (``"berdl"`` or ``"drfp"``; see
                :meth:`distance_matrix`).
            algorithm: ``"agglomerative"`` (average linkage, ``distance_threshold``),
                ``"butina"`` (RDKit, ``distance_threshold``), or ``"hdbscan"``
                (``min_cluster_size``; label ``-1`` is noise).
            distance_threshold: Merge/neighbourhood cutoff for
                agglomerative/Butina.
            min_cluster_size: Minimum cluster size for HDBSCAN.
            segregate_transport: Put transport/identity reactions in their own
                group (label key ``"transport"``) instead of clustering them.
            return_distance_matrix: Include the distance matrix in the result.

        Returns:
            A dict with ``labels`` (id -> cluster label), ``clusters`` (label ->
            ids), ``representatives`` (label -> medoid id), ``n_clusters``,
            ``transport`` (segregated ids), ``info`` (distance-matrix info), and
            optionally ``distance_matrix`` / ``ids``.

        Raises:
            ValueError: If ``algorithm`` is not recognised.
        """
        np = _require("numpy", "pip install numpy")
        ids_in = list(dict.fromkeys(reaction_ids))
        transport: list[str] = []
        if segregate_transport:
            tset = self._transport_ids(ids_in)
            transport = [i for i in ids_in if i in tset]
            ids_in = [i for i in ids_in if i not in tset]

        D, ids, info = self.distance_matrix(ids_in, source=source)
        n = len(ids)
        if n == 0:
            labels_arr = np.zeros(0, dtype=int)
        elif algorithm == "agglomerative":
            from sklearn.cluster import AgglomerativeClustering

            labels_arr = AgglomerativeClustering(
                metric="precomputed", linkage="average",
                distance_threshold=distance_threshold, n_clusters=None,
            ).fit_predict(D) if n > 1 else np.zeros(1, dtype=int)
        elif algorithm == "butina":
            labels_arr = _butina_labels(D, distance_threshold)
        elif algorithm == "hdbscan":
            hdbscan = _require("hdbscan", "pip install hdbscan")
            labels_arr = (
                hdbscan.HDBSCAN(metric="precomputed", min_cluster_size=min_cluster_size)
                .fit_predict(D.astype(np.float64))
                if n > 1 else np.zeros(1, dtype=int)
            )
        else:
            raise ValueError(
                f"Unknown algorithm: {algorithm!r} "
                "(use 'agglomerative', 'butina', or 'hdbscan')"
            )

        labels = {ids[i]: int(labels_arr[i]) for i in range(n)}
        clusters: dict[int, list[str]] = {}
        for rid, lab in labels.items():
            clusters.setdefault(lab, []).append(rid)
        representatives = {
            lab: _medoid(members, ids, D, np)
            for lab, members in clusters.items()
            if lab != -1
        }

        result: dict[str, Any] = {
            "labels": labels,
            "clusters": {str(k): v for k, v in clusters.items()},
            "representatives": {str(k): v for k, v in representatives.items()},
            "n_clusters": len({lab for lab in labels.values() if lab != -1}),
            "transport": transport,
            "info": info,
        }
        if return_distance_matrix:
            result["distance_matrix"] = D
            result["ids"] = ids
        return result

    def print_docs(self) -> None:
        """Print a short usage summary to stdout."""
        print(self.__doc__)


def _drfp_tanimoto(a: Any, b: Any) -> float:
    """Tanimoto between two boolean DRFP arrays (empty-vs-empty == 1.0).

    Args:
        a: First boolean fingerprint array.
        b: Second boolean fingerprint array.

    Returns:
        The Tanimoto similarity in ``[0, 1]``.
    """
    import numpy as np

    aa, bb = bool(a.any()), bool(b.any())
    if not aa and not bb:
        return 1.0
    if aa != bb:
        return 0.0
    inter = int(np.count_nonzero(a & b))
    union = int(np.count_nonzero(a | b))
    return inter / union if union else 1.0


def _butina_labels(D: Any, threshold: float) -> Any:
    """Butina clustering on a precomputed distance matrix.

    Args:
        D: Square distance matrix.
        threshold: Distance cutoff.

    Returns:
        A ``numpy`` integer label array.
    """
    import numpy as np
    from rdkit.ML.Cluster import Butina

    n = D.shape[0]
    if n <= 1:
        return np.zeros(n, dtype=int)
    dists: list[float] = []
    for i in range(n):
        dists.extend(D[i, :i].tolist())
    clusters = Butina.ClusterData(dists, n, threshold, isDistData=True)
    labels = np.full(n, -1, dtype=int)
    for ci, members in enumerate(clusters):
        for m in members:
            labels[m] = ci
    return labels


def _medoid(members: list[str], ids: list[str], D: Any, np: Any) -> str:
    """Return the medoid (least total intra-cluster distance) of a cluster.

    Args:
        members: Reaction ids in the cluster.
        ids: Ordered id list matching the rows/cols of ``D``.
        D: Distance matrix.
        np: The ``numpy`` module.

    Returns:
        The medoid reaction id.
    """
    if len(members) == 1:
        return members[0]
    pos = {rid: k for k, rid in enumerate(ids)}
    idx = [pos[m] for m in members]
    sub = D[np.ix_(idx, idx)]
    return members[int(sub.sum(axis=1).argmin())]


class MSReactionSimilarityUtilsImpl:
    """Composition wrapper around :class:`MSReactionSimilarityUtils`.

    Holds a :class:`SharedEnvUtils` (and optional sibling utilities) instead of
    inheriting, matching the KBUtilLib facade pattern. All method calls are
    delegated to an internal :class:`MSReactionSimilarityUtils` instance.
    """

    def __init__(
        self,
        env: SharedEnvUtils,
        biochem: Optional["MSBiochemUtilsImpl"] = None,
        berdl: Optional["KBBERDLUtilsImpl"] = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the composition wrapper.

        Args:
            env: Shared environment (provides the KBase token).
            biochem: Optional ModelSEED biochemistry utility.
            berdl: Optional BERDL utility (created from ``env`` if omitted).
            **kwargs: Additional arguments forwarded to the delegate.
        """
        self._env = env
        # Mirror the composition pattern of the other *Impl classes
        # (kb_reads_utils, patric_ws_utils, ...): carry the standard "kbase"
        # token through to the delegate. The underlying BERDL client also falls
        # back to the KB_AUTH_TOKEN environment variable when this is absent.
        _kwargs: dict[str, Any] = {
            "config_file": False,
            "token_file": None,
            "kbase_token_file": None,
            "token": env.get_token("kbase"),
        }
        _kwargs.update(kwargs)
        self._delegate = MSReactionSimilarityUtils(
            biochem=biochem, berdl=berdl, **_kwargs
        )

    @property
    def env(self) -> SharedEnvUtils:
        """The shared environment backing this utility.

        Returns:
            The :class:`SharedEnvUtils` instance.
        """
        return self._env

    def __getattr__(self, name: str) -> Any:
        """Delegate attribute access to the wrapped implementation.

        Args:
            name: Attribute name.

        Returns:
            The delegated attribute.
        """
        return getattr(self._delegate, name)
