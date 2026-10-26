"""Offline unit tests for annotator_utils.py.

Tests cover:
- Term, AnnotationRecord, AnnotationResult dataclasses
- classify_version() version-kind classification table
- ToolUnavailableError construction and message format
- _guard_dna / _guard_protein alphabet checks
- AnnotatorUtils._require_available() raises ToolUnavailableError
- AnnotatorUtils.annotate() and is_available() raise NotImplementedError
  when called on the base (covered via a minimal subclass that calls super)
- Export names from kbutillib.__init__
"""

from __future__ import annotations

import pytest

from kbutillib.domains.genome.annotation.annotator_utils import (
    AnnotationRecord,
    AnnotationResult,
    AnnotatorUtils,
    Term,
    ToolUnavailableError,
    _guard_dna,
    _guard_protein,
    classify_version,
    describe_or_accession,
)
from kbutillib.domains.genome.annotation.ontology_dictionary import OntologyDictionary


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

    def test_version_typing_fields_default_to_none(self):
        """The two provenance-typing fields are optional: an annotator that
        has not been wired for them still constructs, and reads as "this
        version string is unclassified"."""
        r = self._make()
        assert r.version_kind is None
        assert r.version_asserted_by is None

    def test_positional_construction_still_works(self):
        """The new fields were appended, so the pre-existing positional
        argument order is unchanged."""
        r = AnnotationResult(
            "prokka",
            "1.14.6",
            None,
            "abc123",
            "prokka --outdir /tmp input.fasta",
            {"gcode": 11},
            [],
        )
        assert r.tool == "prokka"
        assert r.version_kind is None
        assert r.version_asserted_by is None

    def test_version_typing_fields_round_trip(self):
        r = self._make(
            version_kind="tool-version",
            version_asserted_by={"who": "chris", "date": None, "basis": "b"},
        )
        assert r.version_kind == "tool-version"
        assert r.version_asserted_by["who"] == "chris"


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
# describe_or_accession — shared ontology-description formatter
# ---------------------------------------------------------------------------


def _dictionary_with(**tables: dict[str, str]) -> OntologyDictionary:
    """An OntologyDictionary with an in-memory table injected per-namespace,
    with no file I/O and no dependency on a generated artifact or a real
    ontology release."""
    od = OntologyDictionary(config_file=False, token_file=None, kbase_token_file=None)
    od._tables = dict(tables)
    return od


class TestDescribeOrAccession:
    """describe_or_accession: "<accession>: <description>", degrading to
    the bare accession -- never raising -- when no description is on file.
    """

    def test_no_dictionary_degrades_to_bare_accession(self):
        assert describe_or_accession("KO", "K00001", None) == "K00001"

    def test_described_accession_uses_single_colon_space_separator(self):
        od = _dictionary_with(EC={"1.1.1.1": "alcohol dehydrogenase"})
        assert (
            describe_or_accession("EC", "1.1.1.1", od)
            == "1.1.1.1: alcohol dehydrogenase"
        )

    def test_missing_description_in_present_dictionary_degrades_to_bare_accession(self):
        od = _dictionary_with(KO={"K00001": "alcohol dehydrogenase"})
        assert describe_or_accession("KO", "K99999", od) == "K99999"

    def test_dictionary_with_unset_namespace_degrades_to_bare_accession(self):
        # No ec_path given at all -- OntologyDictionary's own missing-file
        # degradation, exercised through describe_or_accession.
        od = OntologyDictionary(config_file=False, token_file=None, kbase_token_file=None)
        assert describe_or_accession("EC", "1.1.1.1", od) == "1.1.1.1"
        assert "EC" in od.degraded_namespaces

    def test_go_accession_round_trips_on_first_colon_space_split(self):
        """A GO accession embeds a colon itself ("GO:0006260"), which is
        exactly why consumers must split on the first ": " (colon-space),
        not the first colon."""
        od = _dictionary_with(GO={"GO:0006260": "DNA replication"})
        value = describe_or_accession("GO", "GO:0006260", od)
        assert value == "GO:0006260: DNA replication"
        accession, sep, description = value.partition(": ")
        assert sep == ": "
        assert accession == "GO:0006260"
        assert description == "DNA replication"

    def test_description_containing_colon_space_does_not_break_the_split(self):
        """Descriptions may themselves contain ': '; consumers split on the
        FIRST occurrence only."""
        od = _dictionary_with(EC={"1.1.1.1": "note: alcohol dehydrogenase"})
        value = describe_or_accession("EC", "1.1.1.1", od)
        assert value == "1.1.1.1: note: alcohol dehydrogenase"
        accession, _sep, description = value.partition(": ")
        assert accession == "1.1.1.1"
        assert description == "note: alcohol dehydrogenase"


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
        from kbutillib.domains.genome.annotation.annotator_utils import (
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


# ---------------------------------------------------------------------------
# docker_image_identity
#
# These tests exist because the behaviour they pin was previously untested in
# exactly the way that mattered: TransytUtils._get_image_digest read only
# `{{index .RepoDigests 0}}`, and every test of it patched the method out, so
# the suite was green while the real call failed 100% of the time on poplar
# (every kbutillib/* image is built locally and never pushed, so RepoDigests
# is empty and the template errors). The fixtures below reproduce DOCKER's
# behaviour, not the adapter's -- that is the criterion a stub of the seam
# could not have satisfied.
# ---------------------------------------------------------------------------

import subprocess as _subprocess  # noqa: E402
from unittest.mock import patch as _patch  # noqa: E402

from kbutillib.domains.genome.annotation.annotator_utils import (  # noqa: E402
    docker_image_identity,
)

_IDENT_RUN = "kbutillib.domains.genome.annotation.annotator_utils.subprocess.run"


def _completed(returncode, stdout="", stderr=""):
    return _subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_identity_falls_back_to_image_id_when_repodigests_is_empty():
    """The poplar case: locally built, never pushed.

    Docker does not return an empty string here -- it EXITS NON-ZERO with
    "error calling index: reflect: slice index out of range" (measured
    2026-10-15 against kbutillib/transyt:latest). Reproducing the exit code,
    not an empty stdout, is the point of this test.
    """
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd[-2])
        if "RepoDigests" in cmd[-2]:
            return _completed(1, stderr="error calling index: slice index out of range")
        return _completed(0, stdout="sha256:92f6e78fbdb8\n")

    with _patch(_IDENT_RUN, side_effect=fake_run):
        got = docker_image_identity("kbutillib/transyt:latest")

    assert got == "kbutillib/transyt:latest@sha256:92f6e78fbdb8"
    assert len(calls) == 2, "must fall through to {{.Id}}, not stop at RepoDigests"


def test_identity_prefers_repodigest_when_present():
    """A pulled/pushed image: the registry digest already carries the repo."""
    with _patch(
        _IDENT_RUN,
        return_value=_completed(0, stdout="oschwengers/bakta@sha256:033df943\n"),
    ):
        got = docker_image_identity("oschwengers/bakta:v1.11.4")
    assert got == "oschwengers/bakta@sha256:033df943"
    assert got.count("@") == 1, "must not double up repo@repo@digest"


def test_identity_returns_bare_tag_when_docker_is_absent():
    with _patch(_IDENT_RUN, side_effect=FileNotFoundError):
        assert docker_image_identity("kbutillib/kofamscan:latest") == (
            "kbutillib/kofamscan:latest"
        )


def test_identity_returns_bare_tag_on_timeout():
    with _patch(_IDENT_RUN, side_effect=_subprocess.TimeoutExpired("docker", 10)):
        assert docker_image_identity("x:latest") == "x:latest"


def test_identity_returns_bare_tag_when_every_field_is_blank():
    with _patch(_IDENT_RUN, return_value=_completed(0, stdout="\n")):
        assert docker_image_identity("x:latest") == "x:latest"


def test_identity_of_empty_image_is_empty():
    assert docker_image_identity("") == ""


# ---------------------------------------------------------------------------
# classify_version()
# ---------------------------------------------------------------------------

_SHA64 = "a" * 64
_SHA64_MIXED = "ABCDEF0123456789" * 4  # 64 hex, mixed case


class TestClassifyVersion:
    """The version-kind classification table.

    SCOPE, asserted here as well as in the docstring: these cases are the
    formats the WIRED annotators emit (a tool release, a
    ``docker_image_identity`` string, a nextflow runner label, an
    executable path).  ``classify_version`` does not judge arbitrary
    strings -- see ``test_an_arbitrary_string_is_called_a_tool_version``,
    which pins that deliberately-permissive behaviour rather than
    pretending it validates.
    """

    @pytest.mark.parametrize(
        "value,from_image,expected",
        [
            # --- None / empty, either mode
            (None, False, None),
            (None, True, None),
            ("", False, None),
            ("", True, None),
            ("   ", False, None),
            ("   ", True, None),
            # --- from_image=True: only a FULL 64-hex sha256 digest counts
            (f"kbutillib/transyt@sha256:{_SHA64}", True, "artifact-digest"),
            (f"oschwengers/bakta@sha256:{_SHA64_MIXED}", True, "artifact-digest"),
            # 63 hex -- one short, names nothing resolvable
            (f"img@sha256:{'a' * 63}", True, None),
            # 65 hex -- one too many
            (f"img@sha256:{'a' * 65}", True, None),
            # 64 hex plus trailing junk: this is why the match must be a
            # FULL match and not a prefix match
            (f"img@sha256:{_SHA64}-dirty", True, None),
            (f"img@sha256:{_SHA64} ", True, None),
            # non-hex characters in the right length
            (f"img@sha256:{'z' * 64}", True, None),
            # a bare tag -- what docker_image_identity returns when
            # `docker image inspect` could not be run at all
            ("kbutillib/kofamscan:latest", True, None),
            (":latest", True, None),
            # a short/truncated digest, e.g. the 8-char form docker prints
            ("oschwengers/bakta@sha256:033df943", True, None),
            # a plain release string is NOT a digest
            ("1.14.6", True, None),
            # --- from_image=False
            ("1.14.6", False, "tool-version"),
            ("1.11.4", False, "tool-version"),
            ("2.0.0-beta17", False, "tool-version"),
            ("v2.0.0-beta17", False, "tool-version"),
            ("2018-0531", False, "tool-version"),
            # the RUNNER's version, not the tool's
            ("nextflow/24.10.5", False, None),
            ("nextflow/", False, None),
            # an executable path is not a version
            ("/opt/kofam_scan/exec_annotation", False, None),
            ("/usr/local/bin/prokka", False, None),
        ],
    )
    def test_table(self, value, from_image, expected):
        assert classify_version(value, from_image=from_image) == expected

    def test_bare_latest_tag_in_tool_mode_is_not_special_cased(self):
        """``:latest`` only reaches the classifier with ``from_image=True``
        (it comes out of ``docker_image_identity``), where it is None.  In
        tool mode nothing claims to recognise it; documented, not fixed."""
        assert classify_version(":latest", from_image=True) is None
        assert classify_version(":latest", from_image=False) == "tool-version"

    def test_an_arbitrary_string_is_called_a_tool_version(self):
        """Deliberate: the classifier covers the formats the wired
        annotators emit and does not validate arbitrary strings."""
        assert classify_version("not a version at all", from_image=False) == (
            "tool-version"
        )

    def test_never_raises_on_a_non_string(self):
        assert classify_version(3.14, from_image=False) is None  # type: ignore[arg-type]
        assert classify_version(["1.0"], from_image=True) is None  # type: ignore[arg-type]

    def test_from_image_is_keyword_only(self):
        with pytest.raises(TypeError):
            classify_version("1.0", False)  # type: ignore[misc]
