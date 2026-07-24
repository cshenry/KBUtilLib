"""Smoke tests for domains.genome — verifies import paths + basic API contract.

These tests are OFFLINE — no external genome tools (DRAM2, MMSeqs, SKANI)
are invoked. Tests verify class presence and method signatures only.
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# 1. Submodule-level imports from canonical domain path
# ---------------------------------------------------------------------------


def test_genome_annotation_submodule_importable() -> None:
    """domains.genome.annotation is importable."""
    from kbutillib.domains.genome import annotation  # noqa: PLC0415
    assert annotation is not None


def test_genome_mmseqs_utils_importable() -> None:
    """domains.genome.mmseqs_utils is importable."""
    from kbutillib.domains.genome import mmseqs_utils  # noqa: PLC0415
    assert mmseqs_utils is not None


def test_genome_skani_utils_importable() -> None:
    """domains.genome.skani_utils is importable."""
    from kbutillib.domains.genome import skani_utils  # noqa: PLC0415
    assert skani_utils is not None


def test_genome_ontomap_utils_importable() -> None:
    """domains.genome.ontomap_utils is importable."""
    from kbutillib.domains.genome import ontomap_utils  # noqa: PLC0415
    assert ontomap_utils is not None


# ---------------------------------------------------------------------------
# 2. Annotation subpackage: AnnotatorUtils API
# ---------------------------------------------------------------------------


def test_annotation_record_importable() -> None:
    """AnnotationRecord is importable from domains.genome.annotation."""
    from kbutillib.domains.genome.annotation import AnnotationRecord  # noqa: PLC0415
    assert AnnotationRecord is not None


def test_annotator_utils_importable() -> None:
    """AnnotatorUtils is importable from domains.genome.annotation."""
    from kbutillib.domains.genome.annotation import AnnotatorUtils  # noqa: PLC0415
    assert AnnotatorUtils is not None
    assert callable(AnnotatorUtils)


def test_annotator_utils_has_annotate() -> None:
    """AnnotatorUtils defines annotate method."""
    from kbutillib.domains.genome.annotation import AnnotatorUtils  # noqa: PLC0415
    assert hasattr(AnnotatorUtils, "annotate")
    assert callable(AnnotatorUtils.annotate)


def test_annotator_utils_has_is_available() -> None:
    """AnnotatorUtils defines is_available method."""
    from kbutillib.domains.genome.annotation import AnnotatorUtils  # noqa: PLC0415
    assert hasattr(AnnotatorUtils, "is_available")


def test_tool_unavailable_error_importable() -> None:
    """ToolUnavailableError is importable from domains.genome.annotation."""
    from kbutillib.domains.genome.annotation import ToolUnavailableError  # noqa: PLC0415
    assert ToolUnavailableError is not None
    assert issubclass(ToolUnavailableError, Exception)


# ---------------------------------------------------------------------------
# 3. MMSeqsUtils structural API
# ---------------------------------------------------------------------------


def test_mmseqs_utils_class_importable() -> None:
    """MMSeqsUtils is importable."""
    from kbutillib.domains.genome.mmseqs_utils import MMSeqsUtils  # noqa: PLC0415
    assert MMSeqsUtils is not None


def test_mmseqs_utils_has_cluster_proteins() -> None:
    """MMSeqsUtils defines cluster_proteins method."""
    from kbutillib.domains.genome.mmseqs_utils import MMSeqsUtils  # noqa: PLC0415
    assert hasattr(MMSeqsUtils, "cluster_proteins")
    assert callable(MMSeqsUtils.cluster_proteins)


# ---------------------------------------------------------------------------
# 4. SKANIUtils structural API
# ---------------------------------------------------------------------------


def test_skani_utils_class_importable() -> None:
    """SKANIUtils is importable."""
    from kbutillib.domains.genome.skani_utils import SKANIUtils  # noqa: PLC0415
    assert SKANIUtils is not None


def test_skani_utils_has_query_genomes() -> None:
    """SKANIUtils defines query_genomes method."""
    from kbutillib.domains.genome.skani_utils import SKANIUtils  # noqa: PLC0415
    assert hasattr(SKANIUtils, "query_genomes")
    assert callable(SKANIUtils.query_genomes)


# ---------------------------------------------------------------------------
# 5. OntomapUtils structural API
# ---------------------------------------------------------------------------


def test_ontomap_utils_class_importable() -> None:
    """OntomapUtils is importable."""
    from kbutillib.domains.genome.ontomap_utils import OntomapUtils  # noqa: PLC0415
    assert OntomapUtils is not None


def test_ontomap_utils_has_map_functions() -> None:
    """OntomapUtils defines map_functions method."""
    from kbutillib.domains.genome.ontomap_utils import OntomapUtils  # noqa: PLC0415
    assert hasattr(OntomapUtils, "map_functions")
    assert callable(OntomapUtils.map_functions)
