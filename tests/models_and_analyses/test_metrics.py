"""Tests for the arc-level model-versus-experiment agreement metric.

The concordance-to-confusion-matrix mapping is FIXED by the spec, not invented:
predicted-essential is the ``essential``-prefix predicate read from
fitness_dashboard; observed-essential is measured fitness below threshold; MCC is
null (never 0) when its denominator is zero; genes with no measured fitness are
excluded and counted; propagated fitness is never substituted for measured; and
the metric carries the FLOOR trust tier.
"""

from __future__ import annotations

import math

from kbutillib.models_and_analyses.metrics import (
    compute_confusion_matrix,
    extract_gene_pairs_from_payload,
    load_fitness_class_vocabulary,
    predicted_essential_from_class,
)


def test_predicted_essential_covers_forward_and_reverse():
    assert predicted_essential_from_class("essential") is True
    assert predicted_essential_from_class("essential-forward") is True
    assert predicted_essential_from_class("essential-reverse") is True
    assert predicted_essential_from_class("active") is False
    assert predicted_essential_from_class("blocked") is False
    assert predicted_essential_from_class(None) is False


def test_confusion_matrix_counts():
    # (predicted, measured): TP, FP, FN, TN, plus one excluded (measured None)
    pairs = [
        (True, -2.0),   # predicted + observed essential -> TP
        (True, 0.5),    # predicted, not observed -> FP
        (False, -2.0),  # not predicted, observed -> FN
        (False, 0.5),   # neither -> TN
        (True, None),   # no measured -> EXCLUDED (not TN)
    ]
    cm = compute_confusion_matrix(pairs, prediction_tier="hypothesis")
    assert (cm.tp, cm.fp, cm.fn, cm.tn) == (1, 1, 1, 1)
    assert cm.excluded_no_measured == 1


def test_mcc_value_correct():
    pairs = [(True, -2.0), (True, 0.5), (False, -2.0), (False, 0.5)]
    cm = compute_confusion_matrix(pairs, prediction_tier="hypothesis")
    # Balanced 1/1/1/1 -> MCC == 0.0 (genuinely computable, not null).
    assert cm.mcc == 0.0


def test_perfect_agreement_mcc_is_one():
    pairs = [(True, -2.0), (False, 0.5)]
    cm = compute_confusion_matrix(pairs, prediction_tier="hypothesis")
    assert cm.mcc == 1.0


def test_mcc_is_null_not_zero_when_denominator_zero():
    """A degenerate matrix (only predicted-essentials) must report MCC as None."""
    pairs = [(True, -2.0), (True, -3.0)]  # tp only; (tn+fp)=(tn+fn)=0
    cm = compute_confusion_matrix(pairs, prediction_tier="hypothesis")
    assert cm.mcc is None  # NOT 0.0


def test_genes_with_no_measured_fitness_excluded_and_reported():
    pairs = [(True, None), (False, None), (True, -2.0)]
    cm = compute_confusion_matrix(pairs, prediction_tier="hypothesis")
    assert cm.excluded_no_measured == 2
    # only the one measured gene is in the matrix
    assert cm.tp + cm.fp + cm.tn + cm.fn == 1


def test_metric_carries_floor_trust_tier():
    """Comparing hypothesis prediction against verified measurement -> hypothesis."""
    cm = compute_confusion_matrix(
        [(True, -2.0)],
        prediction_tier="hypothesis",
        measurement_tier="verified",
    )
    assert cm.trust_tier == "hypothesis"
    assert cm.comparison == "model_prediction_vs_measured_fitness"


def test_precision_and_recall():
    pairs = [(True, -2.0), (True, 0.5), (False, -2.0), (False, 0.5)]
    cm = compute_confusion_matrix(pairs, prediction_tier="hypothesis")
    # tp=1 fp=1 fn=1 tn=1 -> precision=recall=0.5
    assert math.isclose(cm.precision, 0.5)
    assert math.isclose(cm.recall, 0.5)


def test_extract_gene_pairs_from_payload():
    payload = {
        "gene_agreement": [
            {"reference_class": "essential-forward", "measured_fitness": -2.0},
            {"reference_class": "active", "measured_fitness": None},
            {"reference_class": "essential", "measured_fitness": 0.1},
        ]
    }
    pairs = extract_gene_pairs_from_payload(payload)
    assert pairs == [(True, -2.0), (False, None), (True, 0.1)]


def test_extract_gene_pairs_empty_payload():
    assert extract_gene_pairs_from_payload(None) == []
    assert extract_gene_pairs_from_payload({}) == []
    assert extract_gene_pairs_from_payload({"gene_agreement": "not-a-list"}) == []


def test_class_vocabulary_read_from_fitness_dashboard():
    """The vocabulary is read from the module, not restated as a 4-value list.

    When the notebook stack is present the table carries the forward/reverse
    aliases; when it is absent the loader returns {} (the matrix needs only the
    essential-prefix predicate, which is applied directly).
    """
    vocab = load_fitness_class_vocabulary()
    if vocab:  # only assert content when the optional stack is installed
        assert "essential-forward" in vocab
        assert "essential-reverse" in vocab
