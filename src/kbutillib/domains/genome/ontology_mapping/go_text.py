"""go-text: Vibhav Setlur's OntoMap text -> GO retrieval models, ported in-process.

This is the retrieval stage that fed the 2026-09-10 GO council
(``AIAssistant/go-council-run-2026-09-10/``), reproduced exactly:

1. Score the description with each of two OntoMap ``go-text`` models
   (TF-IDF word + char_wb centroid cosine, averaged), keeping the top
   :data:`EXPORT_TOP_N` -- the size of Vibhav's sqlite export.
2. Per model, resolve alt_ids, drop ids that are not live GO terms, drop
   candidates whose GO height exceeds :data:`MAX_HEIGHT`, and keep the first
   :data:`PER_MODEL_TOP_K` survivors (``build_input.py``).
3. Interleave the two lists by rank -- depth model first -- de-duplicate, and
   cap at :data:`UNION_MAX_CANDIDATES` (``union_candidates.py``).

The models are the immutable artifacts from https://github.com/VibhavSetlur/ontomap
(MIT), vendored byte-for-byte under ``kbutillib/data/ontology_mapping/go_text/``
with their own manifests; each file is checked against its manifest sha256 on
load. The scoring arithmetic is ``ontomap/plugins/go.py::FilipeGOPlugin.map``.
Only scikit-learn / joblib / numpy are needed -- not OntoMap's torch stack.

Scores are ranking scores, not calibrated probabilities, and are NOT
comparable across the two models -- which is why the union interleaves by rank.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

_DATA_ROOT = Path(__file__).resolve().parents[3] / "data" / "ontology_mapping"
DEFAULT_ARTIFACT_ROOT = _DATA_ROOT / "go_text"
GO_METADATA_PATH = _DATA_ROOT / "go_metadata.json"

#: OntoMap method versions, keyed by vendored artifact directory.
METHOD_VERSIONS = {
    "filipe-go-filtered-v1": "2.0.0",            # the council's "depth" export
    "filipe-go-height-lte-1-dedup-v1": "4.0.0",  # the council's "le1" export
}
#: Union order matters: the interleave takes the depth model's rank i first.
UNION_ARTIFACTS: Tuple[str, ...] = ("filipe-go-filtered-v1", "filipe-go-height-lte-1-dedup-v1")

EXPORT_TOP_N = 100
PER_MODEL_TOP_K = 20
MAX_HEIGHT = 1
UNION_MAX_CANDIDATES = 40
_CHUNK = 512


class GoTextModel:
    """One immutable OntoMap go-text artifact, loaded lazily and verified."""

    def __init__(self, artifact_dir: Path) -> None:
        self.artifact_dir = Path(artifact_dir)
        self.manifest = json.loads((self.artifact_dir / "manifest.json").read_text())
        self.artifact_version = self.manifest["artifact_version"]
        self.model_file = self.manifest.get("training", {}).get("model") or self._guess_model_file()
        self.sha256 = self.manifest["files"][self.model_file]["sha256"]
        self._model: Optional[Dict[str, Any]] = None

    def _guess_model_file(self) -> str:
        joblibs = [name for name in self.manifest["files"] if name.endswith(".joblib")]
        if len(joblibs) != 1:
            raise RuntimeError(f"{self.artifact_dir}: cannot identify the model file in manifest")
        return joblibs[0]

    @property
    def model(self) -> Dict[str, Any]:
        if self._model is None:
            path = self.artifact_dir / self.model_file
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != self.sha256:
                raise RuntimeError(
                    f"go-text artifact {self.artifact_version} failed its manifest check: "
                    f"{path.name} sha256 {digest} != {self.sha256}"
                )
            import joblib

            self._model = joblib.load(path)
        return self._model

    @property
    def labels(self) -> List[str]:
        return [str(label) for label in self.model["labels"]]

    def rank(self, texts: Sequence[str], top_n: int = EXPORT_TOP_N) -> List[List[Tuple[str, float]]]:
        """Return, per text, the top ``top_n`` ``(go_id, score)`` pairs, best first.

        Ties break on GO id, as ``FilipeGOPlugin.map`` does.
        """
        from sklearn.preprocessing import normalize

        m = self.model
        labels = np.asarray(self.labels)
        out: List[List[Tuple[str, float]]] = []
        for start in range(0, len(texts), _CHUNK):
            chunk = list(texts[start:start + _CHUNK])
            word = normalize(m["word_vectorizer"].transform(chunk))
            char = normalize(m["char_vectorizer"].transform(chunk))
            scores = ((word @ m["word_centroids"].T).toarray() + (char @ m["char_centroids"].T).toarray()) / 2
            n = min(top_n, scores.shape[1])
            for row in scores:
                part = np.argpartition(-row, n - 1)[:n] if n < row.size else np.arange(row.size)
                # widen to every index tied with the n-th score so the id tie-break is exact
                cutoff = row[part].min()
                tied = np.nonzero(row >= cutoff)[0]
                order = sorted(tied, key=lambda i: (-float(row[i]), labels[i]))[:n]
                out.append([(str(labels[i]), float(row[i])) for i in order])
        return out


class GoMetadata:
    """GO names, definitions, namespace, depth and height (see build_go_text_metadata)."""

    def __init__(self, path: Path = GO_METADATA_PATH) -> None:
        if not path.exists():
            raise RuntimeError(
                f"GO metadata table missing at {path}; build it with "
                "`python -m kbutillib.domains.genome.ontology_mapping.build_go_text_metadata --obo go-basic.obo`"
            )
        table = json.loads(path.read_text())
        self.obo_version: str = table["obo_version"]
        self.name: Dict[str, str] = table["name"]
        self.definition: Dict[str, str] = table["def"]
        self.namespace: Dict[str, str] = table["ns"]
        self.depth: Dict[str, int] = table["depth"]
        self.height: Dict[str, int] = table["height"]
        self.alt_id: Dict[str, str] = table["alt_id"]

    def resolve(self, go_id: str) -> str:
        return self.alt_id.get(go_id, go_id)


def council_cut(ranked: List[Tuple[str, float]], meta: GoMetadata,
                top_k: int = PER_MODEL_TOP_K, max_height: int = MAX_HEIGHT) -> List[Dict[str, Any]]:
    """Apply ``build_input.py``'s per-model cut to one model's ranked list."""
    cands: List[Dict[str, Any]] = []
    for go_id, score in ranked:
        g = meta.resolve(go_id)
        if g not in meta.name:
            continue
        if meta.height.get(g, 99) > max_height:
            continue
        cands.append({
            "go_id": g,
            "name": meta.name[g],
            "def": meta.definition.get(g, ""),
            "score": round(score, 4),
            "height": meta.height[g],
            "rank": len(cands) + 1,
        })
        if len(cands) >= top_k:
            break
    return cands


def interleave(lists: Sequence[List[Dict[str, Any]]], max_candidates: int = UNION_MAX_CANDIDATES) -> List[Dict[str, Any]]:
    """``union_candidates.py``: interleave by rank, de-duplicate, cap, renumber."""
    merged: List[Dict[str, Any]] = []
    seen = set()
    for i in range(max((len(lst) for lst in lists), default=0)):
        for lst in lists:
            if i < len(lst) and lst[i]["go_id"] not in seen:
                seen.add(lst[i]["go_id"])
                merged.append(dict(lst[i]))
    merged = merged[:max_candidates]
    for i, c in enumerate(merged, 1):
        c["rank"] = i
    return merged


@lru_cache(maxsize=None)
def load_union_models(root: str = str(DEFAULT_ARTIFACT_ROOT)) -> Tuple[GoTextModel, ...]:
    return tuple(GoTextModel(Path(root) / name) for name in UNION_ARTIFACTS)


@lru_cache(maxsize=None)
def load_metadata(path: str = str(GO_METADATA_PATH)) -> GoMetadata:
    return GoMetadata(Path(path))
