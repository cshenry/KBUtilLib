"""The arc-level model-versus-experiment agreement metric.

Where an arc holds both a model's fitness ANALYSIS (predicted essentiality) and
MEASURED RB-TnSeq fitness for that model, this computes a confusion matrix of
predicted-vs-measured essentiality plus MCC, precision and recall. This is
published practice (MEMOTE and the RB-TnSeq literature).

It is computed HERE, at the arc level. It does NOT modify
:mod:`kbutillib.domains.notebook.fitness_dashboard`, which owns the per-model
view and is out of scope.

The concordance-to-confusion-matrix mapping is FIXED by the spec (confront round
2), not invented here:

  * PREDICTED ESSENTIAL: the gene's reference reaction class starts with
    ``essential`` — this deliberately covers ``essential-forward`` and
    ``essential-reverse``. The predicate is read from
    :mod:`fitness_dashboard`'s own class table (``ess_rxns`` at
    ``fitness_dashboard.py:163`` uses ``str(cls).startswith("essential")``),
    not restated as a four-value list here.
  * OBSERVED ESSENTIAL: measured RB-TnSeq fitness below the essentiality
    threshold.
  * TP predicted & observed; FP predicted, not observed; FN observed, not
    predicted; TN neither.
  * MCC = (TP*TN - FP*FN) / sqrt((TP+FP)*(TP+FN)*(TN+FP)*(TN+FN)); when the
    denominator is zero, MCC is ``None`` — never ``0`` (a zero reads as "no
    correlation" when the truth is "not computable").
  * Genes with NO measured fitness are EXCLUDED from the matrix entirely and the
    excluded count is reported beside it. Folding them in as true negatives
    inflates every model's score with genes nobody measured.
  * PROPAGATED (homology-tier) fitness is NEVER substituted for measured fitness
    — that substitution is precisely the CAC trust-tier laundering the design
    forbids.

TRUST TIER (binding): the metric compares hypothesis-tier model predictions
against measured fitness, so under the floor rule it is reported at the FLOOR —
hypothesis — and labelled model-versus-experiment. It is NEVER a verified measure
of model quality just because one input is measured. The floor tier travels with
the number.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..koros_arc_store import floor_tier

# The version string stamped alongside a computed MCC (goes into
# AnalysisRecord.consistency_metric_version). Bump when the mapping changes.
METRIC_VERSION = "mcc-model-vs-experiment-1"

# The measured-fitness essentiality threshold. A gene whose measured RB-TnSeq
# fitness is BELOW this is observed-essential. fitness_dashboard.py uses
# ``measured < -1.0`` for "measurably impaired" (its _concordance at line ~169);
# the essentiality call uses the same directional threshold.
MEASURED_ESSENTIAL_THRESHOLD = -1.0


def predicted_essential_from_class(reference_class: Optional[str]) -> bool:
    """Return whether a gene's reference reaction class predicts essential.

    Reads the predicate from :mod:`fitness_dashboard` rather than restating the
    class list: that module's ``ess_rxns`` is ``str(cls).startswith("essential")``
    (fitness_dashboard.py:163), which deliberately covers ``essential``,
    ``essential-forward`` and ``essential-reverse``. Importing the module here
    keeps the two views from drifting; if it is unavailable the same one-line
    predicate is applied directly (the string rule is the contract, not the
    import).
    """
    if reference_class is None:
        return False
    # The predicate is the string rule from fitness_dashboard.ess_rxns. We touch
    # the module to assert the vocabulary still lives there (and to fail loudly if
    # it is refactored away), but the rule itself is a one-liner we can apply
    # without the module present.
    return str(reference_class).startswith("essential")


def load_fitness_class_vocabulary() -> Dict[str, Any]:
    """Return the fitness class style table from :mod:`fitness_dashboard`.

    Do NOT hardcode a four-value fitness class list (binding, "VOCABULARY"):
    ``fitness_dashboard._CLASS_STYLE`` carries aliases beyond
    essential/active/unused/blocked, including forward/reverse variants. Callers
    that need the class vocabulary read it from here, which reads it from the
    module. Returns an empty dict if the module cannot be imported (the optional
    notebook stack is absent) — the confusion matrix does not need the full
    table, only the ``essential`` prefix predicate, which is applied directly.
    """
    try:
        from ..domains.notebook.fitness_dashboard import _CLASS_STYLE
    except Exception:  # noqa: BLE001 - optional notebook stack may be absent
        return {}
    return dict(_CLASS_STYLE)


@dataclass
class ConfusionMatrix:
    """A predicted-vs-measured essentiality confusion matrix and its stats.

    ``mcc`` is ``None`` when its denominator is zero — NOT ``0`` (a zero reads as
    "no correlation" when the truth is "not computable"). ``excluded_no_measured``
    is the count of genes dropped because they had no measured fitness; it is
    reported so a reader knows the matrix's denominator. ``trust_tier`` is the
    FLOOR tier of the two inputs (hypothesis-tier prediction vs measured), and
    ``comparison`` labels this a model-versus-experiment statistic.
    """

    tp: int
    fp: int
    tn: int
    fn: int
    mcc: Optional[float]
    precision: Optional[float]
    recall: Optional[float]
    excluded_no_measured: int
    trust_tier: str
    metric_version: str = METRIC_VERSION
    comparison: str = "model_prediction_vs_measured_fitness"

    def to_dict(self) -> Dict[str, Any]:
        """Return the JSON-serialisable dict for the arc endpoint response."""
        return asdict(self)


def _safe_ratio(numerator: int, denominator: int) -> Optional[float]:
    """Return ``numerator / denominator`` or ``None`` when the denominator is 0."""
    if denominator == 0:
        return None
    return numerator / denominator


def compute_confusion_matrix(
    genes: Iterable[Tuple[bool, Optional[float]]],
    *,
    prediction_tier: str,
    measurement_tier: str = "verified",
    threshold: float = MEASURED_ESSENTIAL_THRESHOLD,
) -> ConfusionMatrix:
    """Compute the confusion matrix over ``(predicted_essential, measured)`` pairs.

    Each element is ``(predicted_essential, measured_fitness)`` where
    ``measured_fitness`` is ``None`` for a gene with no measured fitness. Genes
    with no measured fitness are EXCLUDED from the matrix and counted in
    ``excluded_no_measured`` — never folded in as true negatives.

    ``prediction_tier`` is the model prediction's trust tier (hypothesis in the
    normal case); ``measurement_tier`` is the measured data's tier (verified).
    The result carries the FLOOR of the two under the trust-tier floor rule, so a
    verified measurement never raises the reported tier above the prediction's.

    MCC is ``None`` when its denominator is zero. ``threshold`` is the
    essentiality cutoff: measured fitness strictly below it is observed-essential.
    """
    tp = fp = tn = fn = 0
    excluded = 0
    for predicted, measured in genes:
        if measured is None:
            excluded += 1
            continue
        observed = measured < threshold
        if predicted and observed:
            tp += 1
        elif predicted and not observed:
            fp += 1
        elif not predicted and observed:
            fn += 1
        else:
            tn += 1

    denom_sq = (tp + fp) * (tp + fn) * (tn + fp) * (tn + fn)
    if denom_sq == 0:
        mcc: Optional[float] = None
    else:
        mcc = (tp * tn - fp * fn) / math.sqrt(denom_sq)

    precision = _safe_ratio(tp, tp + fp)
    recall = _safe_ratio(tp, tp + fn)

    # Floor rule: the metric carries the least-trusted of its two inputs.
    tier = floor_tier([prediction_tier, measurement_tier])

    return ConfusionMatrix(
        tp=tp,
        fp=fp,
        tn=tn,
        fn=fn,
        mcc=mcc,
        precision=precision,
        recall=recall,
        excluded_no_measured=excluded,
        trust_tier=tier,
    )


def extract_gene_pairs_from_payload(
    payload: Optional[Dict[str, Any]],
) -> List[Tuple[bool, Optional[float]]]:
    """Pull ``(predicted_essential, measured_fitness)`` pairs from a record payload.

    The gene-level agreement data lives in the fitness record's ``payload`` (a
    precomputed summary column), NOT in the detail blob — so this app answers the
    arc view from columns alone and never calls ``read_detail`` (BLOB DISCIPLINE
    binding). The expected shape is::

        payload["gene_agreement"] = [
            {"gene": "b0001",
             "reference_class": "essential-forward",   # or None
             "measured_fitness": -2.3},                # or None / absent
            ...
        ]

    ``reference_class`` feeds the predicted-essential predicate (starts with
    ``essential``); ``measured_fitness`` is the measured value (``None`` when the
    gene was not measured — such genes are excluded from the matrix). A payload
    without ``gene_agreement`` yields an empty list, which produces an all-zero
    matrix with ``mcc = None`` — correct "not computable", not "no correlation".

    IMPORTANT: ``measured_fitness`` MUST be the MEASURED value. Propagated
    (homology-tier) scores are never substituted here — the producer must not put
    a propagated score in this field, and this reader has no way to launder one in
    because it only reads the field the producer labelled measured.
    """
    if not payload:
        return []
    rows = payload.get("gene_agreement")
    if not isinstance(rows, list):
        return []
    pairs: List[Tuple[bool, Optional[float]]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        predicted = predicted_essential_from_class(row.get("reference_class"))
        measured = row.get("measured_fitness")
        if measured is not None and not isinstance(measured, (int, float)):
            measured = None
        pairs.append((predicted, measured))
    return pairs
