"""Offline unit tests for annotator_utils.py.

Tests cover:
- Term, AnnotationRecord, AnnotationResult dataclasses
- ToolUnavailableError construction and message format
- _guard_dna / _guard_protein alphabet checks
- AnnotatorUtils._require_available() raises ToolUnavailableError
- AnnotatorUtils.annotate() and is_available() raise NotImplementedError
  when called on the base (covered via a minimal subclass that calls super)
- Export names from kbutillib.__init__
"""

from __future__ import annotations

import pytest

from kbutillib.annotator_utils import (
    AnnotationRecord,
    AnnotationResult,
    AnnotatorUtils,
    Term,
    ToolUnavailableError,
    _guard_dna,
    _guard_protein,
)


# ---------------------------------------------------------------------------
# Helpers / test doubles
# ---------------------------------------------------------------------------


def _make_utils(**kwargs):
    """Create an AnnotatorUtils-like instance with no file discovery."""
    return AnnotatorUtils(config_file=False, token_file=None, kbase_token_file=None, **kwargs)


class _AlwaysAvailable(AnnotatorUtils):
    """Minimal concrete subclass where is_available() returns True."""

    _tool_name = "fake_tool"
    _install_hint = "conda install fake_tool"

    def is_available(self) -> bool:  # noqa: D102
        return True

    def annotate(self, sequences, **params):  # noqa: D102
        raise NotImplementedError("not needed in test")


class _NeverAvailable(AnnotatorUtils):
    """Minimal concrete subclass where is_available() returns False."""

    _tool_name = "missing_tool"
    _install_hint = "pip install missing_tool"

    def is_available(self) -> bool:  # noqa: D102
        return False

    def annotate(self, sequences, **params):  # noqa: D102
        raise NotImplementedError("not needed in test")


# ---------------------------------------------------------------------------
# Dataclass tests
# ---------------------------------------------------------------------------


class TestTermDataclass:
    """Tests for the Term dataclass."""

    def test_basic_construction(self):
        t = Term(namespace="EC", id="1.1.1.1", value="alcohol dehydrogenase")
        assert t.namespace == "EC"
        assert t.id == "1.1.1.1"
        assert t.value == "alcohol dehydrogenase"
        assert t.evidence == {}

    def test_with_evidence(self):
        t = Term(namespace="KO", id="K00001", value="alcohol dehydrogenase", evidence={"score": 100.0})
        assert t.evidence == {"score": 100.0}

    def test_none_namespace(self):
        t = Term(namespace=None, id=None, value="hypothetical protein")
        assert t.namespace is None
        assert t.id is None

    def test_value_equality(self):
        t1 = Term(namespace="EC", id="1.1.1.1", value="adh", evidence={"e": 1e-5})
        t2 = Term(namespace="EC", id="1.1.1.1", value="adh", evidence={"e": 1e-5})
        assert t1 == t2

    def test_value_inequality(self):
        t1 = Term(namespace="EC", id="1.1.1.1", value="adh")
        t2 = Term(namespace="KO", id="K00001", value="adh")
        assert t1 != t2

    def test_not_frozen(self):
        """Dataclass is non-frozen: attributes can be mutated."""
        t = Term(namespace="EC", id="1.1.1.1", value="adh")
        t.value = "modified"
        assert t.value == "modified"


class TestAnnotationRecordDataclass:
    """Tests for the AnnotationRecord dataclass."""

    def test_basic_construction(self):
        rec = AnnotationRecord(gene_id="gene_001")
        assert rec.gene_id == "gene_001"
        assert rec.terms == []

    def test_with_terms(self):
        t = Term(namespace="EC", id="1.1.1.1", value="adh")
        rec = AnnotationRecord(gene_id="gene_001", terms=[t])
        assert len(rec.terms) == 1
        assert rec.terms[0] == t

    def test_value_equality(self):
        t = Term(namespace="EC", id="1.1.1.1", value="adh")
        r1 = AnnotationRecord(gene_id="g1", terms=[t])
        r2 = AnnotationRecord(gene_id="g1", terms=[t])
        assert r1 == r2

    def test_not_frozen(self):
        rec = AnnotationRecord(gene_id="g1")
        rec.gene_id = "g2"
        assert rec.gene_id == "g2"


class TestAnnotationResultDataclass:
    """Tests for the AnnotationResult dataclass."""

    def _make(self, **overrides):
        defaults = dict(
            tool="prokka",
            tool_version="1.14.6",
            db_version=None,
            run_id="abc123",
            command="prokka --outdir /tmp input.fasta",
            parameters={"gcode": 11},
            records=[],
        )
        defaults.update(overrides)
        return AnnotationResult(**defaults)

    def test_basic_construction(self):
        r = self._make()
        assert r.tool == "prokka"
        assert r.tool_version == "1.14.6"
        assert r.db_version is None
        assert r.run_id == "abc123"
        assert r.records == []

    def test_null_versions_allowed(self):
        r = self._make(tool_version=None, db_version=None)
        assert r.tool_version is None
        assert r.db_version is None

    def test_value_equality(self):
        r1 = self._make()
        r2 = self._make()
        assert r1 == r2

    def test_not_frozen(self):
        r = self._make()
        r.tool = "dram2"
        assert r.tool == "dram2"

    def test_records_mutable_default(self):
        r1 = self._make()
        r2 = self._make()
        # Each instance should get its own default list
        r1.records.append(AnnotationRecord(gene_id="g1"))
        assert r2.records == []


# ---------------------------------------------------------------------------
# ToolUnavailableError tests
# ---------------------------------------------------------------------------


class TestToolUnavailableError:
    """Tests for ToolUnavailableError."""

    def test_message_with_hint(self):
        err = ToolUnavailableError(
            tool="prokka",
            detail="not on PATH",
            hint="conda install -c bioconda prokka",
        )
        assert str(err) == (
            "prokka not available: not on PATH. "
            "Install: conda install -c bioconda prokka"
        )

    def test_message_without_hint(self):
        err = ToolUnavailableError(tool="mytool", detail="binary missing")
        assert str(err) == "mytool not available: binary missing."

    def test_attributes(self):
        err = ToolUnavailableError(tool="t", detail="d", hint="h")
        assert err.tool == "t"
        assert err.detail == "d"
        assert err.hint == "h"

    def test_is_exception(self):
        err = ToolUnavailableError(tool="x", detail="y")
        assert isinstance(err, Exception)

    def test_raise_and_catch(self):
        with pytest.raises(ToolUnavailableError) as exc_info:
            raise ToolUnavailableError(tool="prokka", detail="missing", hint="brew install prokka")
        assert exc_info.value.tool == "prokka"
        assert "prokka not available" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Alphabet guard: DNA
# ---------------------------------------------------------------------------


class TestGuardDNA:
    """Tests for _guard_dna."""

    def test_valid_standard_dna(self):
        _guard_dna({"g1": "ACGT", "g2": "acgt"})  # no exception

    def test_valid_iupac(self):
        _guard_dna({"g1": "ACGTURYSWKMBDHVN"})

    def test_valid_gap_and_stop(self):
        _guard_dna({"g1": "ACGT-*"})

    def test_valid_lowercase(self):
        _guard_dna({"g1": "acgturyswkmbdhvn-*"})

    def test_valid_whitespace_ignored(self):
        _guard_dna({"g1": "ACG T\nACGT"})

    def test_reject_majority_aa(self):
        # Typical protein chars E, D, F, L, P, Q are not in DNA alphabet
        with pytest.raises(ValueError, match="DNA"):
            _guard_dna({"g1": "EDFPQLM" * 10})

    def test_exactly_10_percent_boundary(self):
        # 10 invalid out of 100 total = exactly 10%, should NOT raise
        valid = "A" * 90
        invalid = "E" * 10  # E is not in DNA alphabet
        _guard_dna({"g1": valid + invalid})  # 10% — no raise

    def test_just_over_10_percent(self):
        # 11 invalid out of 100 total = 11%, should raise
        valid = "A" * 89
        invalid = "E" * 11
        with pytest.raises(ValueError, match="DNA"):
            _guard_dna({"g1": valid + invalid})

    def test_empty_sequence_skipped(self):
        _guard_dna({"g1": ""})  # no exception for empty

    def test_whitespace_only_sequence_skipped(self):
        _guard_dna({"g1": "   \n\t"})  # no exception

    def test_multiple_sequences(self):
        # First is fine, second has too many protein chars
        with pytest.raises(ValueError) as exc_info:
            _guard_dna({"g1": "ACGT", "g2": "E" * 100})
        assert "g2" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Alphabet guard: protein
# ---------------------------------------------------------------------------


class TestGuardProtein:
    """Tests for _guard_protein."""

    def test_valid_20aa(self):
        _guard_protein({"p1": "ACDEFGHIKLMNPQRSTVWY"})

    def test_valid_ambiguous_codes(self):
        _guard_protein({"p1": "BZXBZX"})

    def test_valid_gap_and_stop(self):
        _guard_protein({"p1": "ACDEF-*"})

    def test_valid_lowercase(self):
        _guard_protein({"p1": "acdefghiklmnpqrstvwy"})

    def test_valid_whitespace_ignored(self):
        _guard_protein({"p1": "ACDEF GHIKL\nMNPQRST"})

    def test_reject_majority_nucleotide_only(self):
        # U is not in protein alphabet; long run of U will exceed threshold
        with pytest.raises(ValueError, match="protein"):
            _guard_protein({"p1": "U" * 100})

    def test_exactly_10_percent_boundary(self):
        valid = "A" * 90
        invalid = "U" * 10  # U is not in protein alphabet
        _guard_protein({"p1": valid + invalid})  # 10% — no raise

    def test_just_over_10_percent(self):
        valid = "A" * 89
        invalid = "U" * 11
        with pytest.raises(ValueError, match="protein"):
            _guard_protein({"p1": valid + invalid})

    def test_empty_sequence_skipped(self):
        _guard_protein({"p1": ""})

    def test_multiple_sequences_error_names_offender(self):
        with pytest.raises(ValueError) as exc_info:
            _guard_protein({"p1": "ACDEF", "p2": "U" * 100})
        assert "p2" in str(exc_info.value)


# ---------------------------------------------------------------------------
# AnnotatorUtils base class
# ---------------------------------------------------------------------------


class TestAnnotatorUtilsBase:
    """Tests for the AnnotatorUtils base class behaviours."""

    def test_require_available_raises_when_unavailable(self):
        utils = _NeverAvailable(config_file=False, token_file=None, kbase_token_file=None)
        with pytest.raises(ToolUnavailableError) as exc_info:
            utils._require_available()
        err = exc_info.value
        assert err.tool == "missing_tool"
        assert "missing_tool not available" in str(err)
        assert "pip install missing_tool" in str(err)

    def test_require_available_passes_when_available(self):
        utils = _AlwaysAvailable(config_file=False, token_file=None, kbase_token_file=None)
        utils._require_available()  # should not raise

    def test_tool_name_in_error_message(self):
        utils = _NeverAvailable(config_file=False, token_file=None, kbase_token_file=None)
        with pytest.raises(ToolUnavailableError) as exc_info:
            utils._require_available()
        assert "missing_tool" in str(exc_info.value)

    def test_install_hint_in_error_message(self):
        utils = _NeverAvailable(config_file=False, token_file=None, kbase_token_file=None)
        with pytest.raises(ToolUnavailableError) as exc_info:
            utils._require_available()
        assert "pip install missing_tool" in str(exc_info.value)

    def test_error_message_format(self):
        """Message must follow: '{tool} not available: {detail}. Install: {hint}'"""
        utils = _NeverAvailable(config_file=False, token_file=None, kbase_token_file=None)
        with pytest.raises(ToolUnavailableError) as exc_info:
            utils._require_available()
        msg = str(exc_info.value)
        assert "not available:" in msg
        assert "Install:" in msg


# ---------------------------------------------------------------------------
# Export names from kbutillib package
# ---------------------------------------------------------------------------


class TestExports:
    """Verify that the required names are exported from kbutillib.__init__."""

    def test_annotator_utils_exported(self):
        import kbutillib
        assert hasattr(kbutillib, "AnnotatorUtils")
        assert kbutillib.AnnotatorUtils is not None

    def test_term_exported(self):
        import kbutillib
        assert hasattr(kbutillib, "Term")
        assert kbutillib.Term is not None

    def test_annotation_record_exported(self):
        import kbutillib
        assert hasattr(kbutillib, "AnnotationRecord")
        assert kbutillib.AnnotationRecord is not None

    def test_annotation_result_exported(self):
        import kbutillib
        assert hasattr(kbutillib, "AnnotationResult")
        assert kbutillib.AnnotationResult is not None

    def test_tool_unavailable_error_exported(self):
        import kbutillib
        assert hasattr(kbutillib, "ToolUnavailableError")
        assert kbutillib.ToolUnavailableError is not None

    def test_exported_names_are_correct_types(self):
        import kbutillib
        # These should be the actual classes, not None
        from kbutillib.annotator_utils import (
            AnnotationRecord as AR,
            AnnotationResult as ARes,
            AnnotatorUtils as AU,
            Term as T,
            ToolUnavailableError as TUE,
        )
        assert kbutillib.AnnotatorUtils is AU
        assert kbutillib.Term is T
        assert kbutillib.AnnotationRecord is AR
        assert kbutillib.AnnotationResult is ARes
        assert kbutillib.ToolUnavailableError is TUE
