"""OntologyMapping: function description -> GO, as a fixed KBUtilLib API.

Two capabilities, meant to be run in sequence:

* :meth:`OntologyMappingUtils.function_to_go` -- a RANKED CANDIDATE LIST of GO
  molecular-function terms per description. Today's backend is the retrieval
  stage of the 2026-09-10 GO council (see :mod:`.go_text`); it abstains on
  placeholder functions (see :mod:`.placeholder`).
* :meth:`OntologyMappingUtils.function_to_go_council` -- runs KBUtilLib's AI
  council (query ``go_mf@1``) over those ranked lists to pick the final GO
  term(s) per description.

The API is the contract; the backend underneath is expected to change (e.g.
to Vibhav's Type 4 fusion once it can score new text). Every result therefore
carries ``mapping_version`` and ``mapping_hash``: a backend swap changes them,
and anything that caches or hashes results (the function clearinghouse) must
fold them into its own key.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from kbutillib.core.capability import capability
from kbutillib.core.shared_env_utils import SharedEnvUtils

from . import go_text
from .placeholder import PLACEHOLDER_RULES_VERSION, placeholder_reason

#: The backend name. Bump on any change to retrieval behaviour.
BACKEND = "ontomap-go-text-union"
BACKEND_VERSION = "1.0.0"
DEFAULT_COUNCIL_QUERY = "go_mf@1"

FunctionsArg = Union[Sequence[str], Sequence[Dict[str, Any]], Dict[str, str]]


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normalise_functions(functions: FunctionsArg) -> List[Dict[str, str]]:
    """Accept ``["text", ...]``, ``{id: text}`` or ``[{"id"/"query_id", "text", "source"?}]``."""
    if isinstance(functions, str):
        functions = [functions]
    if isinstance(functions, dict):
        return [{"query_id": str(k), "text": str(v), "source": ""} for k, v in functions.items()]
    out: List[Dict[str, str]] = []
    for i, f in enumerate(functions):
        if isinstance(f, str):
            out.append({"query_id": f"Q{i + 1}", "text": f, "source": ""})
        else:
            qid = f.get("query_id", f.get("id", f"Q{i + 1}"))
            out.append({"query_id": str(qid), "text": str(f["text"]), "source": str(f.get("source") or "")})
    ids = [f["query_id"] for f in out]
    if len(set(ids)) != len(ids):
        raise ValueError("function_to_go: query ids must be unique")
    return out


class OntologyMappingUtils(SharedEnvUtils):
    """Function-description -> GO mapping (ranked candidates, then council)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._version: Optional[Dict[str, Any]] = None

    # -- provenance --------------------------------------------------------

    def mapping_version(self) -> Dict[str, Any]:
        """The identity of the retrieval backend: what produced a ranked list."""
        if self._version is None:
            models = go_text.load_union_models()
            meta = go_text.load_metadata()
            parts = {
                "backend": BACKEND,
                "backend_version": BACKEND_VERSION,
                "models": [
                    {"artifact": m.artifact_version,
                     "ontomap_method_version": go_text.METHOD_VERSIONS[m.artifact_version],
                     "sha256": m.sha256}
                    for m in models
                ],
                "go_release": meta.obo_version,
                "go_metadata_sha256": _sha256_file(go_text.GO_METADATA_PATH),
                "placeholder_rules": PLACEHOLDER_RULES_VERSION,
                "params": {"export_top_n": go_text.EXPORT_TOP_N, "per_model_top_k": go_text.PER_MODEL_TOP_K,
                           "max_height": go_text.MAX_HEIGHT, "union_max_candidates": go_text.UNION_MAX_CANDIDATES},
            }
            digest = hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()
            versions = "+".join(go_text.METHOD_VERSIONS[m.artifact_version] for m in models)
            self._version = {
                "mapping_version": f"{BACKEND}@{BACKEND_VERSION} (go-text {versions}; GO {meta.obo_version})",
                "mapping_hash": digest,
                "provenance": parts,
            }
        return self._version

    # -- stage 1: ranked candidates -----------------------------------------

    @capability(
        domain="ontology_mapping",
        summary="Rank candidate GO molecular-function terms for function descriptions.",
        tags=("ontology", "go", "annotation"),
        visibility="public",
    )
    def function_to_go(
        self,
        functions: FunctionsArg,
        max_candidates: int = go_text.UNION_MAX_CANDIDATES,
    ) -> Dict[str, Any]:
        """Return a ranked GO candidate list for each function description.

        Args:
            functions: descriptions, as a list of strings, a ``{query_id: text}``
                mapping, or a list of ``{"query_id"|"id", "text", "source"?}``.
            max_candidates: cap on each merged list (the council used 40).

        Returns:
            ``{"mapping_version", "mapping_hash", "provenance", "results": [...]}``
            with one result per input, in input order::

                {"query_id", "text", "source",
                 "abstained": bool, "abstain_reason": str | None,
                 "candidates": [{"go_id", "name", "def", "score", "height", "rank"}]}

            Placeholder functions abstain with an empty candidate list. Scores
            are ranking evidence from two models and are NOT comparable across
            candidates from different models -- use ``rank``.
        """
        items = _normalise_functions(functions)
        version = self.mapping_version()
        models = go_text.load_union_models()
        meta = go_text.load_metadata()

        reasons = {f["query_id"]: placeholder_reason(f["text"]) for f in items}
        texts = sorted({f["text"] for f in items if reasons[f["query_id"]] is None})
        per_model = [dict(zip(texts, m.rank(texts))) if texts else {} for m in models]

        results = []
        for f in items:
            reason = reasons[f["query_id"]]
            cands: List[Dict[str, Any]] = []
            if reason is None:
                lists = [go_text.council_cut(ranked[f["text"]], meta) for ranked in per_model]
                cands = go_text.interleave(lists, max_candidates)
                if not cands:
                    reason = "no candidate survived the GO height filter"
            results.append({**f, "abstained": reason is not None, "abstain_reason": reason, "candidates": cands})
        return {**version, "results": results}

    # -- stage 2: council vote ----------------------------------------------

    @capability(
        domain="ontology_mapping",
        summary="Run the AI council (go_mf@1) over function_to_go's ranked GO candidates.",
        tags=("ontology", "go", "annotation", "council", "llm"),
        visibility="public",
    )
    def function_to_go_council(
        self,
        ranked: Optional[Dict[str, Any]] = None,
        functions: Optional[FunctionsArg] = None,
        profile: Optional[str] = None,
        query: str = DEFAULT_COUNCIL_QUERY,
        allow_unverified: bool = False,
        label: Optional[str] = None,
        job_id: Optional[str] = None,
        **seat_overrides: Any,
    ) -> Dict[str, Any]:
        """Have the AI council choose GO term(s) from each ranked candidate list.

        Pass the output of :meth:`function_to_go` as ``ranked`` (or pass
        ``functions`` and it is computed). Abstained descriptions are not sent
        to the council. Identical texts are sent once and fanned back out.
        To resume an interrupted run, pass the ``job_id`` it returned.

        Spends LLM tokens. The council store, roster and credentials come from
        the ``ai_council`` config section (``kbu council doctor`` checks them).

        Returns:
            The ranked result's ``mapping_version``/``mapping_hash`` plus
            ``council`` (job id, query, profile, final job status) and, per
            input, ``selected_go_ids``, ``final_confidence``, ``decline_reason``,
            ``decided_by`` and the raw council ``verdict`` row.
        """
        if ranked is None:
            if functions is None:
                raise ValueError("function_to_go_council needs ranked= or functions=")
            ranked = self.function_to_go(functions)
        council = self._council()

        by_text: Dict[str, Dict[str, Any]] = {}
        for r in ranked["results"]:
            if not r["abstained"] and r["candidates"] and r["text"] not in by_text:
                by_text[r["text"]] = r
        item_of_text = {t: f"t{i}" for i, t in enumerate(sorted(by_text))}
        tasks = [
            {
                "item_id": item_of_text[t],
                "evidence": {"text": t, "source": r.get("source") or ""},
                "menu": [
                    {"id": c["go_id"], "label": c["name"], "description": c.get("def") or "",
                     "rank": c["rank"], "score": c["score"]}
                    for c in r["candidates"]
                ],
            }
            for t, r in sorted(by_text.items())
        ]

        status: Dict[str, Any] = {}
        verdicts: Dict[str, Dict[str, Any]] = {}
        if tasks:
            if job_id is None:
                job_id = council.submit(query, tasks, label=label or "function_to_go_council",
                                        profile=profile, allow_unverified=allow_unverified, **seat_overrides)
            status = council.run(job_id)
            for row in council.results(job_id):
                verdicts[str(row.get("item_id"))] = row

        results = []
        for r in ranked["results"]:
            out = {k: r[k] for k in ("query_id", "text", "source", "abstained", "abstain_reason")}
            row = verdicts.get(item_of_text.get(r["text"], ""))
            verdict = _verdict_of(row)
            out.update({
                "selected_go_ids": list(verdict.get("selected_ids") or []),
                "final_confidence": verdict.get("final_confidence"),
                "decline_reason": verdict.get("decline_reason") if row else (
                    "abstained" if r["abstained"] else ("no_candidates" if not r["candidates"] else "not_decided")),
                "decided_by": verdict.get("decided_by"),
                "names": {g: go_text.load_metadata().name.get(g) for g in (verdict.get("selected_ids") or [])},
                "verdict": row,
            })
            results.append(out)
        return {
            "mapping_version": ranked["mapping_version"],
            "mapping_hash": ranked["mapping_hash"],
            "council": {"job_id": job_id, "query": query, "profile": profile,
                        "status": {k: v for k, v in status.items() if k != "worker"},
                        "worker": status.get("worker")},
            "results": results,
        }

    def _council(self) -> Any:
        from kbutillib.domains.ai.council_utils import CouncilUtils

        return CouncilUtils(config_file=False, token_file=None, kbase_token_file=None)


def _verdict_of(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Pull the verdict dict out of a council results row, however it is nested."""
    if not row:
        return {}
    v = row.get("verdict", row)
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return {}
    return v if isinstance(v, dict) else {}


class OntologyMappingUtilsImpl:
    """Composition wrapper registered on :class:`kbutillib.toolkit.KBUtilLib`."""

    def __init__(self, env: Any, **kwargs: Any) -> None:
        self._env = env
        _kwargs = {"config_file": False, "token_file": None, "kbase_token_file": None}
        _kwargs.update(kwargs)
        self._delegate = OntologyMappingUtils(**_kwargs)

    @property
    def env(self) -> Any:
        return self._env

    @property
    def available(self) -> bool:
        """True when scikit-learn/joblib import and the vendored data is present."""
        return self.unavailable_reason is None

    @property
    def unavailable_reason(self) -> Optional[str]:
        try:
            import joblib  # noqa: F401
            import sklearn  # noqa: F401
        except ImportError as exc:
            return f"ontology_mapping needs scikit-learn and joblib: {exc}"
        if not go_text.GO_METADATA_PATH.exists():
            return f"GO metadata table missing at {go_text.GO_METADATA_PATH}"
        return None

    def __dir__(self) -> list:
        base = list(super().__dir__())
        base.extend(a for a in dir(self._delegate) if not a.startswith("__") and a not in base)
        return base

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)
