"""Ontomap utilities for reaction function mapping via Vibhav Setlur's ontomap package.

This module provides a composable utility class wrapping the ontomap capability-2
reaction Pipeline for mapping functional descriptions to biochemical reactions.

ontomap is an optional dependency; this module imports cleanly even when it is
not installed.  The Pipeline is imported lazily at first call time.

Reference: https://github.com/VibhavSetlur/ontomap
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .shared_env_utils import SharedEnvUtils

logger = logging.getLogger(__name__)

# Chunk size below which CPU inference is safe per the ontomap recommendation.
_CPU_CHUNK_SIZE = 99


class OntomapUtils(SharedEnvUtils):
    """Utility class wrapping the ontomap capability-2 reaction Pipeline.

    Provides function-description-to-reaction mapping via a fused-score
    nearest-neighbour approach.  The underlying ontomap Pipeline is loaded
    lazily on first use and cached on the instance, so repeated calls within
    the same session pay the model-load cost only once.

    ontomap is a soft dependency: the module imports cleanly when ontomap is
    not installed, and an ``ImportError`` is raised only when
    :meth:`map_functions` is actually called.

    Example::

        from kbutillib import OntomapUtils
        util = OntomapUtils()
        results = util.map_functions(
            descriptions=["ATP synthase subunit alpha", "glucose kinase"],
            ids=["gene_001", "gene_002"],
            top_k=10,
        )
        for r in results:
            print(r["query_id"], r["candidates"][0]["reaction_id"])

    Args:
        direction: Mapping direction for the ontomap Pipeline.  Defaults to
            ``"sso"`` (sequence-to-structure-to-ontology).
        device: Compute device override (``"cpu"`` or ``"cuda"``).  When
            ``None`` (default) CUDA is used if available, otherwise CPU.
        **kwargs: Additional keyword arguments forwarded to
            :class:`~kbutillib.shared_env_utils.SharedEnvUtils`.
    """

    def __init__(
        self,
        direction: str = "sso",
        device: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)

        self._direction = direction
        self._device_override = device
        self._pipeline = None  # lazy

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_device(self) -> str:
        """Return the compute device string, auto-selecting CUDA when available."""
        if self._device_override is not None:
            return self._device_override
        try:
            import torch  # type: ignore[import]
            return "cuda" if torch.cuda.is_available() else "cpu"
        except ImportError:
            return "cpu"

    def _get_pipeline(self):
        """Return the cached ontomap Pipeline, loading it on first call.

        Raises:
            ImportError: If ontomap is not installed.
        """
        if self._pipeline is None:
            try:
                from ontomap import Pipeline  # type: ignore[import]
            except ImportError as exc:
                raise ImportError(
                    "ontomap is required for OntomapUtils.map_functions. "
                    "Install it with: pip install ontomap"
                ) from exc

            device = self._get_device()
            self.log_info(
                f"Loading ontomap Pipeline (direction={self._direction!r}, device={device!r})"
            )
            self._pipeline = Pipeline.from_pretrained(self._direction, device)
            self.log_info("ontomap Pipeline loaded and cached.")
        return self._pipeline

    @staticmethod
    def _build_confidence_band(fused_score: float) -> str:
        """Map a fused score to a qualitative confidence band.

        Thresholds are intentionally conservative; adjust as ontomap
        benchmarks are published.
        """
        if fused_score >= 0.9:
            return "high"
        if fused_score >= 0.7:
            return "medium"
        return "low"

    @staticmethod
    def _build_top1_margin(candidates: list) -> Optional[float]:
        """Compute the score margin between rank-1 and rank-2 candidates."""
        if len(candidates) < 2:
            return None
        return round(candidates[0]["fused_score"] - candidates[1]["fused_score"], 6)

    @classmethod
    def _translate_map_result(
        cls,
        query_id: str,
        description: str,
        map_result: Any,
        top_k: int,
    ) -> Dict[str, Any]:
        """Convert an ``ontomap.MapResult`` to the KBUtilLib flat shape.

        Args:
            query_id: Caller-supplied identifier for this query.
            description: The function description that was mapped.
            map_result: An ``ontomap.MapResult`` instance with the fields
                ``predictions``, ``reaction_meta``, and ``source_ec``.
            top_k: Maximum number of candidates to return.

        Returns:
            A dict with keys ``query_id``, ``description``, ``source_ec``,
            and ``candidates``.
        """
        predictions: list = map_result.predictions or []
        reaction_meta: dict = map_result.reaction_meta or {}
        source_ec: Optional[str] = getattr(map_result, "source_ec", None)

        candidates = []
        for rank, (rxn_id, fused_score) in enumerate(predictions[:top_k], start=1):
            meta = reaction_meta.get(rxn_id) or {}

            # ec_numbers comes from ec_list key in reaction_meta
            ec_numbers: List[str] = meta.get("ec_list", [])
            # Normalise: some versions return a bare string
            if isinstance(ec_numbers, str):
                ec_numbers = [ec_numbers] if ec_numbers else []

            candidate: Dict[str, Any] = {
                "rank": rank,
                "reaction_id": rxn_id,
                "fused_score": fused_score,
                "name": meta.get("name", ""),
                "ec_numbers": ec_numbers,
                "equation": meta.get("equation", ""),
                "pathways": meta.get("pathway", []),
                "ec_match_level": meta.get("ec_match_level", None),
                # confidence_band and top1_margin filled in after loop
                "confidence_band": cls._build_confidence_band(fused_score),
                "top1_margin": None,
            }
            candidates.append(candidate)

        # Back-fill top1_margin on the rank-1 entry
        if candidates:
            candidates[0]["top1_margin"] = cls._build_top1_margin(candidates)

        return {
            "query_id": query_id,
            "description": description,
            "source_ec": source_ec,
            "candidates": candidates,
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def map_functions(
        self,
        descriptions: List[str],
        ids: Optional[List[str]] = None,
        top_k: int = 20,
        direction: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Map functional descriptions to biochemical reactions via ontomap.

        Descriptions are processed in chunks of at most
        :data:`_CPU_CHUNK_SIZE` on CPU (to avoid OOM) and in a single
        batch on CUDA.  Results preserve the input order.

        Args:
            descriptions: Free-text function descriptions to map, one per
                gene/protein.
            ids: Optional identifiers aligned 1-to-1 with ``descriptions``.
                When omitted, zero-based integer strings are used.
            top_k: Maximum number of ranked candidates per query.
            direction: Optionally override the direction for this call only.
                Changing direction invalidates the cached Pipeline.

        Returns:
            List of dicts, one per input description, each with the shape::

                {
                    "query_id": str,
                    "description": str,
                    "source_ec": str | None,
                    "candidates": [
                        {
                            "rank": int,
                            "reaction_id": str,
                            "fused_score": float,
                            "name": str,
                            "ec_numbers": list[str],
                            "equation": str,
                            "pathways": list,
                            "confidence_band": str,
                            "ec_match_level": str | None,
                            "top1_margin": float | None,
                        },
                        ...
                    ],
                }

        Raises:
            ImportError: If ontomap is not installed.
            ValueError: If ``ids`` is provided but its length does not match
                ``descriptions``.
        """
        if not descriptions:
            return []

        if ids is not None and len(ids) != len(descriptions):
            raise ValueError(
                f"ids length ({len(ids)}) must match descriptions length "
                f"({len(descriptions)})"
            )

        effective_ids: List[str] = (
            ids if ids is not None else [str(i) for i in range(len(descriptions))]
        )

        # If the caller overrides direction for this call we may need a new pipeline.
        if direction is not None and direction != self._direction:
            self.log_info(
                f"Direction changed from {self._direction!r} to {direction!r}; "
                "resetting cached Pipeline."
            )
            self._direction = direction
            self._pipeline = None

        pipeline = self._get_pipeline()
        device = self._get_device()

        # Determine chunking: CPU is memory-constrained, CUDA can take all at once.
        chunk_size = _CPU_CHUNK_SIZE if device == "cpu" else len(descriptions)
        total = len(descriptions)
        all_results: List[Dict[str, Any]] = []

        for chunk_start in range(0, total, chunk_size):
            chunk_end = min(chunk_start + chunk_size, total)
            chunk_descs = descriptions[chunk_start:chunk_end]
            chunk_ids = effective_ids[chunk_start:chunk_end]

            self.log_info(
                f"ontomap: mapping descriptions {chunk_start + 1}-{chunk_end} "
                f"of {total}"
            )

            # ontomap Pipeline accepts a list of description strings and returns
            # a list of MapResult objects in the same order.
            map_results = pipeline(chunk_descs)

            for qid, desc, map_result in zip(chunk_ids, chunk_descs, map_results):
                all_results.append(
                    self._translate_map_result(qid, desc, map_result, top_k)
                )

        return all_results


# ── Composition-based implementation ──────────────────────────────────────────


class OntomapUtilsImpl:
    """Composition-based OntomapUtils.

    Holds ``env: SharedEnvUtils`` instead of inheriting from it.
    Delegates all method calls to an internal :class:`OntomapUtils` instance.
    """

    def __init__(self, env, **kwargs):
        self._env = env
        _kwargs: Dict[str, Any] = {
            "config_file": False,
            "token_file": None,
            "kbase_token_file": None,
        }
        _kwargs.update(kwargs)
        self._delegate = OntomapUtils(**_kwargs)

    @property
    def env(self):
        return self._env

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)
