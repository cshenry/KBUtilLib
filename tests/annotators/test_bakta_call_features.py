"""Tests for Bakta structural gene calling (``BaktaUtils.call_features``).

Three layers, because no single one of them can establish the contract:

1. :class:`TestCallFeaturesParsing` / :class:`TestCallFeaturesErrors` --
   ``call_features`` against a fake driver that is handed the real argv and
   writes canned driver JSON to the ``--out`` path it finds there. Covers
   parsing, the kept/discarded split, discard counts, the version block and
   every error mapping, offline and in milliseconds.
2. :class:`TestDriverStub` -- the driver
   (``bakta_call_features.py``) run in a real subprocess against a STUB
   ``bakta`` package on ``PYTHONPATH`` whose annotation entry points raise.
   This is what proves the documented structural call ORDER and that no
   functional-annotation function is reached. A mock inside this process
   could not prove it: the driver is a separate program.
3. :class:`TestRealImageIntegration` -- the whole thing end to end through
   ``call_features``'s DEFAULT runner path (no fakes, no monkeypatching) in
   the real ``kbutillib/bakta:1.11.4`` image on a real E. coli sequence.
   Skipped unless the image is present AND ``KBUTILLIB_TEST_BAKTA_DB``
   names a Bakta database.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from kbutillib.domains.genome.annotation.annotator_utils import ToolUnavailableError
from kbutillib.domains.genome.annotation.bakta_utils import (
    _DRIVER_FILENAME,
    _DRIVER_PINNED_BAKTA_VERSION,
    BaktaCallError,
    BaktaCallResult,
    BaktaUtils,
    _completeness,
    _split_call_features,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DRIVER_PATH = (
    REPO_ROOT
    / "src"
    / "kbutillib"
    / "domains"
    / "genome"
    / "annotation"
    / _DRIVER_FILENAME
)

#: Image tag the integration test requires, matching docker/bakta/Dockerfile.
REAL_IMAGE = "kbutillib/bakta:1.11.4"

#: Env var naming a Bakta database for the integration test. On poplar:
#: /scratch/kbdl/refdata/bakta-db-6.0-full
BAKTA_DB_ENV = "KBUTILLIB_TEST_BAKTA_DB"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_utils(docker_image: str | None = "", **kwargs: Any) -> BaktaUtils:
    """Build a BaktaUtils with no filesystem/config discovery.

    Args:
        docker_image: Passed straight through; the default ``""`` selects
            native mode, which is what the fake-driver tests want (native
            mode's availability probe only needs ``python3`` on PATH).
        **kwargs: Forwarded to ``BaktaUtils``.

    Returns:
        The instance.
    """
    kwargs.setdefault("db_path", "/fake/db")
    return BaktaUtils(
        config_file=False,
        token_file=None,
        kbase_token_file=None,
        docker_image=docker_image,
        **kwargs,
    )


def _out_path_from_cmd(cmd: list[str]) -> Path:
    """Extract the HOST-side ``--out`` path from a driver argv.

    The fake driver reads its own argv exactly as the real one does, so this
    also checks that ``_build_call_features_cmd`` actually emits ``--out``.
    In native mode ``--out`` is a top-level argv token. In Docker mode it
    lives inside the trailing ``bash -lc`` string as a CONTAINER path, so it
    is mapped back through the ``-v <host>:/work`` bind mount.

    Args:
        cmd: The argv built by ``_build_call_features_cmd``.

    Returns:
        The output JSON path on this host.
    """
    if "--out" in cmd:
        return Path(cmd[cmd.index("--out") + 1])
    inner = shlex.split(cmd[-1])
    container_out = Path(inner[inner.index("--out") + 1])
    work = next(
        arg.split(":")[0]
        for arg, flag in zip(cmd[1:], cmd)
        if flag == "-v" and arg.endswith(":/work")
    )
    return Path(work) / container_out.name


def _canned_payload(
    features: list[dict[str, Any]] | None = None,
    contig_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Build a driver output payload shaped exactly as the driver emits one.

    Args:
        features: Feature records, or ``None`` for :func:`_canned_features`.
        contig_ids: Contig ids, or ``None`` for ``["c1"]``.

    Returns:
        The payload dict.
    """
    return {
        "features": _canned_features() if features is None else features,
        "contig_ids": ["c1"] if contig_ids is None else contig_ids,
        "versions": {
            "bakta": _DRIVER_PINNED_BAKTA_VERSION,
            "db": {"major": 6, "minor": 0, "type": "full"},
        },
    }


def _canned_features() -> list[dict[str, Any]]:
    """One record per interesting shape the driver can emit.

    Covers: a complete CDS, a both-ends-truncated CDS, a CRISPR array with
    Bakta's ``?`` strand, a tRNA with ``anticodon``/``amino_acid``, an rRNA
    with an Rfam accession, an ncRNA with a class, and one discard of each
    of Bakta's three types (``overlap`` on a tRNA, so the counts cannot be
    confused with the CDS-only ``discarded["cds"]`` list).

    Returns:
        The feature records.
    """
    return [
        {
            "type": "cds",
            "contig_id": "c1",
            "start": 100,
            "stop": 400,
            "strand": "+",
            "nt": "ATG" * 100,
            "product": None,
            "truncated": None,
            "edge": False,
            "aa": "MKV*",
        },
        {
            "type": "cds",
            "contig_id": "c1",
            "start": 1,
            "stop": 90,
            "strand": "-",
            "nt": "ATG" * 30,
            "product": None,
            "truncated": "both",
            "edge": False,
            "aa": "MKL",
        },
        {
            "type": "tRNA",
            "contig_id": "c1",
            "start": 500,
            "stop": 575,
            "strand": "-",
            "nt": "ACGT" * 19,
            "product": "tRNA-Thr(ggt)",
            "truncated": None,
            "edge": False,
            "amino_acid": "Thr",
            "anticodon": "ggt",
            "score": 79.8,
        },
        {
            "type": "rRNA",
            "contig_id": "c1",
            "start": 700,
            "stop": 815,
            "strand": "+",
            "nt": "G" * 116,
            "product": "5S ribosomal RNA",
            "truncated": "3-prime",
            "edge": False,
            "score": 90.0,
            "rfam": "RF00001",
        },
        {
            "type": "ncRNA",
            "contig_id": "c1",
            "start": 900,
            "stop": 999,
            "strand": "-",
            "nt": "T" * 100,
            "product": "some ncRNA",
            "truncated": None,
            "edge": False,
            "score": 107.0,
            "rfam": "RF01830",
            "ncrna_class": "ribozyme",
        },
        {
            "type": "crispr",
            "contig_id": "c1",
            "start": 1100,
            "stop": 1300,
            "strand": "?",
            "nt": "C" * 201,
            "product": "CRISPR array with 3 repeats",
            "truncated": None,
            "edge": False,
        },
        {
            "type": "tRNA",
            "contig_id": "c1",
            "start": 1400,
            "stop": 1470,
            "strand": "+",
            "nt": "A" * 71,
            "product": "tRNA-Xxx",
            "truncated": None,
            "edge": False,
            "discarded": {
                "type": "overlap",
                "feature_type": "tmRNA",
                "description": (
                    "tmRNA overlap with (transfer-messenger RNA, SsrA) at [1400,1450]"
                ),
                "overlap_start": 1400,
                "overlap_stop": 1450,
            },
        },
        {
            "type": "cds",
            "contig_id": "c1",
            "start": 1600,
            "stop": 1900,
            "strand": "-",
            "nt": "ATG" * 100,
            "product": None,
            "truncated": None,
            "edge": False,
            "aa": "MSPURIOUS",
            "discarded": {
                "type": "spurious",
                "feature_type": None,
                "description": (
                    "(partial) homology to spurious sequence HMM (AntiFam:ANF00012)"
                ),
                "overlap_start": None,
                "overlap_stop": None,
            },
        },
        {
            "type": "cds",
            "contig_id": "c1",
            "start": 2000,
            "stop": 2300,
            "strand": "+",
            "nt": "ATG" * 100,
            "product": None,
            "truncated": None,
            "edge": False,
            "aa": "MLONG",
            "discarded": {
                "type": "length",
                "feature_type": None,
                "description": (
                    "Predicted CDS length over suspicious length threshold (150000 bp)"
                ),
                "overlap_start": None,
                "overlap_stop": None,
            },
        },
        {
            "type": "cds",
            "contig_id": "c1",
            "start": 2500,
            "stop": 2800,
            "strand": "+",
            "nt": "ATG" * 100,
            "product": None,
            "truncated": None,
            "edge": False,
            "aa": "MOVERLAP",
            "discarded": {
                "type": "overlap",
                "feature_type": "rRNA",
                "description": "rRNA overlap with (5S ribosomal RNA) at [2500,2700]",
                "overlap_start": 2500,
                "overlap_stop": 2700,
            },
        },
    ]


def _install_fake_driver(
    bu: BaktaUtils,
    payload: Any = None,
    returncode: int = 0,
    stderr: str = "",
    raw_text: str | None = None,
) -> list[list[str]]:
    """Replace ``bu._run_driver`` with a fake that behaves like the driver.

    The fake reads the ``--out`` path out of the argv it was handed, writes
    *payload* (or *raw_text*) there, and returns *(returncode, stderr)* --
    i.e. the only thing it fakes is Bakta, not the interface.

    Args:
        bu: The instance to patch (instance attribute only).
        payload: Object to ``json.dump``, or ``None`` for
            :func:`_canned_payload`.
        returncode: Exit code to report.
        stderr: stderr text to report.
        raw_text: When given, written verbatim instead of *payload* -- used
            to simulate unparseable output.

    Returns:
        A list that accumulates every argv the fake was called with.
    """
    calls: list[list[str]] = []

    def _fake(cmd: list[str]) -> tuple[int, str]:
        calls.append(list(cmd))
        out_path = _out_path_from_cmd(cmd)
        if raw_text is not None:
            out_path.write_text(raw_text, encoding="utf-8")
        elif returncode == 0:
            body = _canned_payload() if payload is None else payload
            out_path.write_text(json.dumps(body), encoding="utf-8")
        return returncode, stderr

    bu._run_driver = _fake  # type: ignore[method-assign]
    return calls


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestCompleteness:
    """``_completeness`` maps Bakta's truncation marker to two bools (R2)."""

    @pytest.mark.parametrize(
        "truncated,expected",
        [
            (None, (True, True)),
            ("5-prime", (False, True)),
            ("3-prime", (True, False)),
            ("both", (False, False)),
            ("nonsense", (True, True)),
        ],
    )
    def test_marker_mapping(self, truncated, expected):
        assert _completeness(truncated) == expected

    def test_marker_is_already_strand_resolved(self):
        """A '5-prime' marker means "no start codon" on EITHER strand.

        Bakta resolves the strand when it sets the marker
        (bakta/features/cds.py 1.11.4 lines 118-124), so there is no second
        flip here -- a reverse-strand CDS truncated at its biological 5'
        end must report start_complete=False just like a forward one.
        """
        forward = {
            "type": "cds",
            "contig_id": "c1",
            "start": 1,
            "stop": 30,
            "strand": "+",
            "truncated": "5-prime",
            "aa": "MK",
        }
        reverse = dict(forward, strand="-")
        kept, _ = _split_call_features([forward, reverse])
        assert [f["start_complete"] for f in kept] == [False, False]
        assert [f["stop_complete"] for f in kept] == [True, True]


class TestSplitCallFeatures:
    """``_split_call_features`` kept/discarded split and counts."""

    def test_counts_cover_all_three_discard_types(self):
        _, discarded = _split_call_features(_canned_features())
        # overlap: one tRNA + one CDS; spurious: one CDS; length: one CDS.
        assert discarded["counts"] == {"overlap": 2, "spurious": 1, "length": 1}

    def test_counts_are_seeded_so_every_type_is_indexable(self):
        _, discarded = _split_call_features([])
        assert discarded["counts"] == {"overlap": 0, "spurious": 0, "length": 0}

    def test_discarded_cds_list_is_cds_only(self):
        _, discarded = _split_call_features(_canned_features())
        reasons = sorted(entry["reason"] for entry in discarded["cds"])
        assert reasons == ["length", "overlap", "spurious"]
        assert all(entry["contig_id"] == "c1" for entry in discarded["cds"])

    def test_discarded_cds_carries_overlap_interval_and_type(self):
        _, discarded = _split_call_features(_canned_features())
        overlap = next(e for e in discarded["cds"] if e["reason"] == "overlap")
        assert overlap["overlap_type"] == "rRNA"
        assert (overlap["overlap_start"], overlap["overlap_stop"]) == (2500, 2700)

    def test_length_discard_has_no_overlap_fields(self):
        _, discarded = _split_call_features(_canned_features())
        length = next(e for e in discarded["cds"] if e["reason"] == "length")
        assert length["overlap_type"] is None
        assert length["overlap_start"] is None
        assert length["overlap_stop"] is None

    def test_non_dict_records_are_ignored(self):
        kept, discarded = _split_call_features(["nope", None, 7])
        assert kept == []
        assert discarded["cds"] == []


# ---------------------------------------------------------------------------
# call_features via a fake driver
# ---------------------------------------------------------------------------


class TestCallFeaturesParsing:
    """``call_features`` against a fake driver returning canned JSON."""

    @pytest.fixture()
    def result(self) -> BaktaCallResult:
        bu = _make_utils()
        _install_fake_driver(bu)
        return bu.call_features([("c1", "ACGT" * 100)])

    def test_returns_frozen_dataclass(self, result):
        assert isinstance(result, BaktaCallResult)
        with pytest.raises(Exception):
            result.features = []  # frozen

    def test_kept_features_exclude_every_discard(self, result):
        assert len(result.features) == 6
        assert all("discarded" not in f for f in result.features)

    def test_kept_feature_types(self, result):
        assert sorted({f["type"] for f in result.features}) == [
            "cds",
            "crispr",
            "ncRNA",
            "rRNA",
            "tRNA",
        ]

    def test_coordinates_are_one_based_inclusive_and_ordered(self, result):
        for feature in result.features:
            assert feature["start"] >= 1
            assert feature["start"] <= feature["stop"]

    def test_cds_aa_has_trailing_asterisk_stripped(self, result):
        cds = next(f for f in result.features if f["start"] == 100)
        assert cds["aa"] == "MKV"

    def test_cds_carries_completeness_flags(self, result):
        """R2: every CDS carries start_complete/stop_complete."""
        cdss = [f for f in result.features if f["type"] == "cds"]
        assert cdss
        for cds in cdss:
            assert "start_complete" in cds
            assert "stop_complete" in cds
        truncated = next(f for f in cdss if f["start"] == 1)
        assert truncated["start_complete"] is False
        assert truncated["stop_complete"] is False
        assert truncated["partial"] is True

    def test_non_cds_features_have_no_completeness_flags(self, result):
        trna = next(f for f in result.features if f["type"] == "tRNA")
        assert "start_complete" not in trna
        assert "aa" not in trna

    def test_partial_tracks_the_truncated_marker(self, result):
        rrna = next(f for f in result.features if f["type"] == "rRNA")
        assert rrna["partial"] is True
        crispr = next(f for f in result.features if f["type"] == "crispr")
        assert crispr["partial"] is False

    def test_trna_carries_amino_acid_and_anticodon(self, result):
        trna = next(f for f in result.features if f["type"] == "tRNA")
        assert trna["amino_acid"] == "Thr"
        assert trna["anticodon"] == "ggt"
        assert trna["score"] == pytest.approx(79.8)

    def test_rfam_and_ncrna_class_are_carried(self, result):
        rrna = next(f for f in result.features if f["type"] == "rRNA")
        assert rrna["rfam"] == "RF00001"
        ncrna = next(f for f in result.features if f["type"] == "ncRNA")
        assert ncrna["ncrna_class"] == "ribozyme"

    def test_crispr_strand_question_mark_is_accepted(self, result):
        """R1: '?' is Bakta's STRAND_UNKNOWN and crispr alone may carry it."""
        crispr = next(f for f in result.features if f["type"] == "crispr")
        assert crispr["strand"] == "?"
        assert {f["strand"] for f in result.features if f["type"] != "crispr"} <= {
            "+",
            "-",
        }

    def test_db_xrefs_and_go_terms_are_dropped(self, result):
        for feature in result.features:
            assert "db_xrefs" not in feature
            assert not any(
                isinstance(v, str) and v.startswith("GO:") for v in feature.values()
            )

    def test_discarded_counts_and_cds_list(self, result):
        assert result.discarded["counts"] == {
            "overlap": 2,
            "spurious": 1,
            "length": 1,
        }
        assert len(result.discarded["cds"]) == 3

    def test_versions_carry_driver_block_plus_provenance(self, result):
        """R8: image_id and driver_sha256 travel with every result."""
        assert result.versions["bakta"] == _DRIVER_PINNED_BAKTA_VERSION
        assert result.versions["db"] == {"major": 6, "minor": 0, "type": "full"}
        assert result.versions["image"] == "native"
        assert result.versions["image_id"] == "native"
        assert len(result.versions["driver_sha256"]) == 64
        assert result.versions["driver_sha256"] == BaktaUtils._driver_sha256()

    def test_call_non_coding_is_echoed_true(self, result):
        """R3."""
        assert result.call_non_coding is True

    def test_call_non_coding_is_echoed_false_and_passed_to_the_driver(self):
        bu = _make_utils()
        calls = _install_fake_driver(bu)
        result = bu.call_features([("c1", "ACGT" * 100)], call_non_coding=False)
        assert result.call_non_coding is False
        assert "--no-non-coding" in calls[0]

    def test_call_non_coding_true_does_not_pass_the_flag(self):
        bu = _make_utils()
        calls = _install_fake_driver(bu)
        bu.call_features([("c1", "ACGT" * 100)])
        assert "--no-non-coding" not in calls[0]

    def test_threads_and_translation_table_reach_the_driver(self):
        bu = _make_utils()
        calls = _install_fake_driver(bu)
        bu.call_features([("c1", "ACGT" * 100)], threads=7, translation_table=4)
        cmd = calls[0]
        assert cmd[cmd.index("--threads") + 1] == "7"
        assert cmd[cmd.index("--translation-table") + 1] == "4"


class TestCallFeaturesErrors:
    """Error mapping for ``call_features``."""

    def test_contig_id_mismatch_raises(self):
        bu = _make_utils()
        _install_fake_driver(bu, payload=_canned_payload(contig_ids=["other"]))
        with pytest.raises(BaktaCallError, match="not exactly the input ids"):
            bu.call_features([("c1", "ACGT" * 100)])

    def test_contig_id_subset_raises(self):
        bu = _make_utils()
        _install_fake_driver(bu, payload=_canned_payload(contig_ids=["c1"]))
        with pytest.raises(BaktaCallError, match="not exactly the input ids"):
            bu.call_features([("c1", "ACGT" * 100), ("c2", "ACGT" * 100)])

    def test_exit_three_names_both_versions(self):
        bu = _make_utils()
        _install_fake_driver(
            bu,
            returncode=3,
            stderr=(
                "Bakta version mismatch: driver pinned 1.11.4, image has 1.11.3\n"
            ),
        )
        with pytest.raises(BaktaCallError) as excinfo:
            bu.call_features([("c1", "ACGT" * 100)])
        message = str(excinfo.value)
        assert _DRIVER_PINNED_BAKTA_VERSION in message
        assert "1.11.3" in message

    def test_exit_three_without_a_parseable_line_still_reports(self):
        bu = _make_utils()
        _install_fake_driver(bu, returncode=3, stderr="something else entirely")
        with pytest.raises(BaktaCallError) as excinfo:
            bu.call_features([("c1", "ACGT" * 100)])
        assert "unknown" in str(excinfo.value)
        assert "something else entirely" in str(excinfo.value)

    def test_other_non_zero_exit_quotes_the_stderr_tail(self):
        bu = _make_utils()
        _install_fake_driver(bu, returncode=1, stderr="x" * 5000 + "TAILMARKER")
        with pytest.raises(BaktaCallError) as excinfo:
            bu.call_features([("c1", "ACGT" * 100)])
        message = str(excinfo.value)
        assert "exited 1" in message
        assert "TAILMARKER" in message
        assert len(message) < 5000  # tail, not the whole stream

    def test_unparseable_json_raises(self):
        bu = _make_utils()
        _install_fake_driver(bu, raw_text="{not json")
        with pytest.raises(BaktaCallError, match="could not be read"):
            bu.call_features([("c1", "ACGT" * 100)])

    def test_missing_output_file_raises(self):
        bu = _make_utils()

        def _fake(cmd: list[str]) -> tuple[int, str]:
            return 0, ""  # exits 0 but writes nothing

        bu._run_driver = _fake  # type: ignore[method-assign]
        with pytest.raises(BaktaCallError, match="could not be read"):
            bu.call_features([("c1", "ACGT" * 100)])

    def test_non_object_payload_raises(self):
        bu = _make_utils()
        _install_fake_driver(bu, payload=["a", "list"])
        with pytest.raises(BaktaCallError, match="not a JSON object"):
            bu.call_features([("c1", "ACGT" * 100)])

    def test_missing_features_array_raises(self):
        payload = _canned_payload()
        del payload["features"]
        bu = _make_utils()
        _install_fake_driver(bu, payload=payload)
        with pytest.raises(BaktaCallError, match="no 'features' array"):
            bu.call_features([("c1", "ACGT" * 100)])

    def test_unknown_feature_type_raises(self):
        payload = _canned_payload(
            features=[
                {
                    "type": "sorf",
                    "contig_id": "c1",
                    "start": 1,
                    "stop": 9,
                    "strand": "+",
                }
            ]
        )
        bu = _make_utils()
        _install_fake_driver(bu, payload=payload)
        with pytest.raises(BaktaCallError, match="unexpected feature type"):
            bu.call_features([("c1", "ACGT" * 100)])

    def test_unknown_strand_on_a_non_crispr_feature_raises(self):
        """R1: '?' is permitted for crispr ONLY."""
        payload = _canned_payload(
            features=[
                {
                    "type": "cds",
                    "contig_id": "c1",
                    "start": 1,
                    "stop": 9,
                    "strand": "?",
                    "aa": "M",
                }
            ]
        )
        bu = _make_utils()
        _install_fake_driver(bu, payload=payload)
        with pytest.raises(BaktaCallError, match="expected one of"):
            bu.call_features([("c1", "ACGT" * 100)])

    def test_empty_contigs_raises_value_error(self):
        bu = _make_utils()
        _install_fake_driver(bu)
        with pytest.raises(ValueError, match="at least one contig"):
            bu.call_features([])

    def test_duplicate_contig_id_raises_value_error(self):
        bu = _make_utils()
        _install_fake_driver(bu)
        with pytest.raises(ValueError, match="Duplicate contig id"):
            bu.call_features([("c1", "ACGT"), ("c1", "ACGT")])

    def test_whitespace_contig_id_raises_value_error(self):
        bu = _make_utils()
        _install_fake_driver(bu)
        with pytest.raises(ValueError, match="no whitespace"):
            bu.call_features([("c 1", "ACGT" * 100)])

    def test_empty_sequence_raises_value_error(self):
        bu = _make_utils()
        _install_fake_driver(bu)
        with pytest.raises(ValueError, match="empty sequence"):
            bu.call_features([("c1", "   ")])

    def test_protein_input_raises_value_error(self):
        bu = _make_utils()
        _install_fake_driver(bu)
        with pytest.raises(ValueError, match="DNA"):
            bu.call_features([("c1", "MKVLAQWERTYIPSDFHK" * 10)])

    def test_missing_native_python_raises_tool_unavailable(self):
        bu = _make_utils()
        bu._call_python = "/nonexistent/python-that-is-not-there"
        with pytest.raises(ToolUnavailableError, match="bakta.python"):
            bu.call_features([("c1", "ACGT" * 100)])

    def test_missing_docker_image_raises_tool_unavailable(self, monkeypatch):
        bu = _make_utils(docker_image="definitely/not-present:0")
        monkeypatch.setattr(bu, "_docker_image_present", lambda: False)
        with pytest.raises(ToolUnavailableError, match="not present locally"):
            bu.call_features([("c1", "ACGT" * 100)])


# ---------------------------------------------------------------------------
# R6 -- docker_image constructor override
# ---------------------------------------------------------------------------


class TestDockerImageOverride:
    """R6: ``docker_image=`` beats the ambient config, and there is no
    per-call ``image`` kwarg."""

    AMBIENT = {"bakta": {"docker_image": "ambient/from-config:9"}}

    def test_config_is_used_when_no_explicit_image(self):
        bu = BaktaUtils(
            db_path="/fake/db",
            config=self.AMBIENT,
            token_file=None,
            kbase_token_file=None,
        )
        assert bu._docker_image == "ambient/from-config:9"

    def test_explicit_image_overrides_ambient_config(self):
        bu = BaktaUtils(
            db_path="/fake/db",
            config=self.AMBIENT,
            token_file=None,
            kbase_token_file=None,
            docker_image="explicit/chosen:1",
        )
        assert bu._docker_image == "explicit/chosen:1"

    def test_explicit_empty_image_forces_native_despite_config(self):
        """The empty string is a CHOICE (native), not "unset"."""
        bu = BaktaUtils(
            db_path="/fake/db",
            config=self.AMBIENT,
            token_file=None,
            kbase_token_file=None,
            docker_image="",
        )
        assert bu._docker_image == ""

    def test_call_features_uses_the_instance_image_not_a_kwarg(self):
        bu = BaktaUtils(
            db_path="/fake/db",
            config=self.AMBIENT,
            token_file=None,
            kbase_token_file=None,
            docker_image="explicit/chosen:1",
        )
        # The image is present as far as the probe is concerned ...
        bu._docker_image_present = lambda: True  # type: ignore[method-assign]
        bu._docker_image_id = lambda: "sha256:deadbeef"  # type: ignore[method-assign]
        calls = _install_fake_driver(bu)
        result = bu.call_features([("c1", "ACGT" * 100)])
        assert "explicit/chosen:1" in calls[0]
        assert "ambient/from-config:9" not in calls[0]
        assert result.versions["image"] == "explicit/chosen:1"
        assert result.versions["image_id"] == "sha256:deadbeef"

    def test_no_per_call_image_kwarg(self):
        import inspect

        params = inspect.signature(BaktaUtils.call_features).parameters
        assert "image" not in params
        assert set(params) == {
            "self",
            "contigs",
            "translation_table",
            "threads",
            "call_non_coding",
        }

    def test_docker_image_id_is_native_in_native_mode(self):
        assert _make_utils(docker_image="")._docker_image_id() == "native"


class TestDockerCommandShape:
    """The Docker argv ``call_features`` builds."""

    @pytest.fixture()
    def cmd(self, tmp_path) -> list[str]:
        bu = _make_utils(docker_image="kbutillib/bakta:1.11.4", db_path=str(tmp_path))
        return bu._build_call_features_cmd(
            work=tmp_path,
            contigs_path=tmp_path / "contigs.fna",
            out_path=tmp_path / "call_features.json",
            threads=3,
            translation_table=11,
            call_non_coding=True,
        )

    def test_runs_with_no_network_and_an_explicit_user(self, cmd):
        assert "--network" in cmd and cmd[cmd.index("--network") + 1] == "none"
        assert "--user" in cmd
        assert cmd[cmd.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"

    def test_database_is_mounted_read_only(self, cmd):
        assert any(arg.endswith(":/db:ro") for arg in cmd)

    def test_driver_is_bind_mounted_read_only(self, cmd):
        mounts = [arg for arg in cmd if _DRIVER_FILENAME in arg and arg.endswith(":ro")]
        assert len(mounts) == 1
        host_side = mounts[0].split(":")[0]
        assert Path(host_side).is_file()

    def test_conda_bin_is_prefixed_onto_path(self, cmd):
        assert "PATH=/opt/conda/bin:$PATH" in cmd[-1]

    def test_driver_is_invoked_inside_the_container(self, cmd):
        assert "/driver/bakta_call_features.py" in cmd[-1]

    def test_native_command_uses_the_configured_python(self, tmp_path):
        bu = _make_utils(docker_image="", db_path=str(tmp_path))
        bu._call_python = "/usr/bin/python3.11"
        cmd = bu._build_call_features_cmd(
            work=tmp_path,
            contigs_path=tmp_path / "contigs.fna",
            out_path=tmp_path / "out.json",
            threads=1,
            translation_table=11,
            call_non_coding=False,
        )
        assert cmd[0] == "/usr/bin/python3.11"
        assert cmd[1].endswith(_DRIVER_FILENAME)
        assert "docker" not in cmd
        assert "--no-non-coding" in cmd


# ---------------------------------------------------------------------------
# The driver itself, against a stub `bakta`
# ---------------------------------------------------------------------------

#: Relative path -> source, for a stub `bakta` package.
#:
#: Every function the driver is ALLOWED to call appends its name to the file
#: named by $BAKTA_STUB_TRACE; every function it must NEVER call raises
#: instead. The stub needs no third-party dependency (no Biopython, no
#: pyrodigal) -- it exists to observe orchestration, not to annotate.
_STUB_FILES: dict[str, str] = {
    "bakta/__init__.py": '''
import json
import os

__version__ = os.environ.get("BAKTA_STUB_VERSION", "1.11.4")
__db_schema_version__ = 6


def record(name, **extra):
    """Append one call to the trace file."""
    with open(os.environ["BAKTA_STUB_TRACE"], "a") as fh:
        fh.write(json.dumps({"call": name, **extra}) + "\\n")


def forbidden(name):
    """Build a stub entry point that must never be reached."""

    def _raise(*args, **kwargs):
        record("FORBIDDEN:" + name)
        raise AssertionError("structural-only driver called " + name)

    return _raise
''',
    "bakta/so.py": '''
from collections import namedtuple

SO = namedtuple("SO", ["name", "id"])
SO_NCRNA_GENE_RIBOZYME = SO("ribozyme", "SO:0000374")
''',
    "bakta/config.py": '''
import tempfile
from pathlib import Path

import bakta

# Mirrors the subset of bakta.config the driver reads.
threads = None
db_path = None
db_info = None
tmp_path = None
genome_path = None
prefix = None
output_path = None
genus = None
species = None
strain = None
taxon = None
complete = None
gram = "?"
translation_table = None
min_sequence_length = None
keep_sequence_headers = None
replicons = None
plasmid = None
locus = None


def check_output_path(output, force):
    global output_path
    bakta.record("config.check_output_path", output=str(output), force=bool(force))
    output_path = Path(output)
    output_path.mkdir(parents=True, exist_ok=True)
    return output_path


def setup(args):
    global threads, db_path, tmp_path, genome_path, translation_table
    global min_sequence_length, keep_sequence_headers, complete
    bakta.record("config.setup", argv_translation_table=args.translation_table)
    threads = args.threads
    db_path = Path(args.db)
    tmp_path = Path(tempfile.mkdtemp(prefix="bakta_stub_tmp_"))
    genome_path = Path(args.genome)
    translation_table = args.translation_table
    min_sequence_length = args.min_contig_length
    keep_sequence_headers = args.keep_contig_headers
    complete = args.complete
''',
    "bakta/db.py": '''
import bakta


def check(db_path):
    bakta.record("db.check")
    return {"major": 6, "minor": 0, "type": "full", "date": "2025-02-24"}
''',
    "bakta/utils.py": '''
import argparse
import sys

import bakta


def parse_arguments():
    bakta.record("utils.parse_arguments", argv=list(sys.argv))
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("genome")
    parser.add_argument("--db", default=None)
    parser.add_argument("--output", "-o", default=".")
    parser.add_argument("--prefix", default=None)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--min-contig-length", type=int, default=1,
                        dest="min_contig_length")
    parser.add_argument("--translation-table", type=int, default=11,
                        dest="translation_table")
    parser.add_argument("--threads", type=int, default=0)
    parser.add_argument("--keep-contig-headers", action="store_true",
                        dest="keep_contig_headers")
    parser.add_argument("--complete", action="store_true")
    parser.add_argument("--compliant", action="store_true")
    parser.add_argument("--replicons", default=None)
    for flag in ("trna", "tmrna", "rrna", "ncrna", "ncrna-region", "crispr",
                 "cds", "pseudo", "sorf", "gap", "ori", "filter", "plot"):
        parser.add_argument("--skip-" + flag, action="store_true",
                            dest="skip_" + flag.replace("-", "_"))
    return parser.parse_args()


def parse_replicon_table(path):
    bakta.record("utils.parse_replicon_table")
    return None


def qc_sequences(sequences, replicons):
    bakta.record("utils.qc_sequences", n=len(sequences))
    return sequences, False
''',
    "bakta/io/__init__.py": "",
    "bakta/io/fasta.py": '''
import bakta


def import_sequences(path, is_genomic=True, is_dna=True):
    bakta.record("fasta.import_sequences", path=str(path))
    sequences = []
    seq_id = None
    chunks = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line.startswith(">"):
                if seq_id is not None:
                    sequences.append((seq_id, "".join(chunks)))
                seq_id = line[1:].split()[0]
                chunks = []
            elif line:
                chunks.append(line)
    if seq_id is not None:
        sequences.append((seq_id, "".join(chunks)))
    return [
        {
            "id": sid,
            "description": "",
            "nt": nt.upper(),
            "length": len(nt),
            "complete": False,
            "type": "contig",
            "topology": "linear",
        }
        for sid, nt in sequences
    ]


def export_sequences(sequences, path, description=False, wrap=False):
    bakta.record("fasta.export_sequences", path=str(path))
    with open(path, "w") as fh:
        for seq in sequences:
            fh.write(">" + seq["id"] + "\\n" + seq["nt"] + "\\n")
''',
    "bakta/features/__init__.py": "",
    "bakta/features/t_rna.py": '''
import bakta


def predict_t_rnas(data, sequences_path):
    bakta.record("t_rna.predict_t_rnas")
    contig = data["sequences"][0]["id"]
    return [
        {
            "type": "tRNA",
            "sequence": contig,
            "start": 10,
            "stop": 85,
            "strand": "-",
            "nt": "ACGT" * 19,
            "product": "tRNA-Thr(ggt)",
            "amino_acid": "Thr",
            "anti_codon": "ggt",
            "score": 79.8,
            "db_xrefs": ["SO:0000253", "GO:0030533"],
        },
        {
            "type": "tRNA",
            "sequence": contig,
            "start": 400,
            "stop": 470,
            "strand": "+",
            "nt": "A" * 71,
            "product": "tRNA-Xxx",
            "db_xrefs": [],
            "_stub_fate": "overlap",
        },
    ]
''',
    "bakta/features/tm_rna.py": '''
import bakta


def predict_tm_rnas(data, sequences_path):
    bakta.record("tm_rna.predict_tm_rnas")
    return []
''',
    "bakta/features/r_rna.py": '''
import bakta


def predict_r_rnas(data, sequences_path):
    bakta.record("r_rna.predict_r_rnas")
    return [
        {
            "type": "rRNA",
            "sequence": data["sequences"][0]["id"],
            "start": 600,
            "stop": 715,
            "strand": "+",
            "nt": "G" * 116,
            "product": "5S ribosomal RNA",
            "truncated": "3-prime",
            "score": 90.0,
            "db_xrefs": ["RFAM:RF00001", "KEGG:K01985", "SO:0000652"],
        }
    ]
''',
    "bakta/features/nc_rna.py": '''
import bakta
import bakta.so as so


def predict_nc_rnas(data, sequences_path):
    bakta.record("nc_rna.predict_nc_rnas")
    return [
        {
            "type": "ncRNA",
            "sequence": data["sequences"][0]["id"],
            "start": 800,
            "stop": 899,
            "strand": "-",
            "nt": "T" * 100,
            "product": "a ribozyme",
            "class": so.SO_NCRNA_GENE_RIBOZYME,
            "score": 107.0,
            "db_xrefs": ["RFAM:RF01830", "GO:0003824"],
        }
    ]
''',
    "bakta/features/nc_rna_region.py": '''
import bakta

predict_nc_rna_regions = bakta.forbidden("nc_rna_region.predict_nc_rna_regions")
''',
    "bakta/features/crispr.py": '''
import bakta


def predict_crispr(data, sequences_path):
    bakta.record("crispr.predict_crispr")
    return [
        {
            "type": "crispr",
            "sequence": data["sequences"][0]["id"],
            "start": 1000,
            "stop": 1200,
            "strand": "?",
            "nt": "C" * 201,
            "product": "CRISPR array with 3 repeats",
            "db_xrefs": ["SO:0001459"],
        }
    ]
''',
    "bakta/features/cds.py": '''
import bakta

# Each stub CDS declares its fate in `_stub_fate`; the two filters below act
# on it. That keeps the stub honest about ORDER (which is what is under test)
# without needing a 150 kb nucleotide string or a real AntiFam HMM.
_FATES = ("keep", "length", "spurious")


def predict(data):
    bakta.record("cds.predict")
    contig = data["sequences"][0]["id"]
    cdss = []
    for index, fate in enumerate(_FATES):
        cdss.append(
            {
                "type": "cds",
                "sequence": contig,
                "start": 2000 + index * 400,
                "stop": 2300 + index * 400,
                "strand": "+",
                "nt": "ATG" * 100,
                "aa": "MKV*",
                # Raw bytes, exactly as Bakta's own aa_digest is. A driver
                # that json.dump-ed Bakta's dicts would die here.
                "aa_digest": b"\\x00\\x01\\x02",
                "aa_hexdigest": "000102",
                "product": None,
                "db_xrefs": ["SO:0000316"],
                "_stub_fate": fate,
            }
        )
    return cdss


def filter_length(cdss):
    bakta.record("cds.filter_length", n=len(cdss))
    discarded = []
    for cds in cdss:
        if cds.get("_stub_fate") == "length":
            cds["discarded"] = {
                "type": "length",
                "description": "Predicted CDS length over suspicious length "
                "threshold (150000 bp)",
            }
            discarded.append(cds)
    return discarded


revise_translational_exceptions = bakta.forbidden(
    "cds.revise_translational_exceptions"
)
revise_special_cases_annotated = bakta.forbidden("cds.revise_special_cases_annotated")
import_user_cdss = bakta.forbidden("cds.import_user_cdss")
predict_pfam = bakta.forbidden("cds.predict_pfam")
predict_pseudo_candidates = bakta.forbidden("cds.predict_pseudo_candidates")
detect_pseudogenes = bakta.forbidden("cds.detect_pseudogenes")
analyze_proteins = bakta.forbidden("cds.analyze_proteins")
''',
    "bakta/features/orf.py": '''
import bakta


def detect_spurious(orfs):
    bakta.record("orf.detect_spurious", n=len(orfs))
    discarded = []
    for orf in orfs:
        if orf.get("_stub_fate") == "spurious":
            orf["discarded"] = {
                "type": "spurious",
                "description": "(partial) homology to spurious sequence HMM "
                "(AntiFam:ANF00012)",
                "score": 12.5,
                "evalue": 1e-30,
            }
            discarded.append(orf)
    return discarded


write_internal_faa = bakta.forbidden("orf.write_internal_faa")
''',
    "bakta/features/annotation.py": '''
import bakta


def detect_feature_overlaps(data):
    bakta.record(
        "annotation.detect_feature_overlaps",
        seen=[[f["type"], f["start"]] for f in data["features"]],
    )
    for feature in data["features"]:
        if feature.get("_stub_fate") == "overlap":
            feature["discarded"] = {
                "type": "overlap",
                "feature_type": "tmRNA",
                "description": "tmRNA overlap with (transfer-messenger RNA, "
                "SsrA) at [400,450]",
            }


combine_annotation = bakta.forbidden("annotation.combine_annotation")
select_gene_symbols = bakta.forbidden("annotation.select_gene_symbols")
''',
    "bakta/features/s_orf.py": '''
import bakta

extract = bakta.forbidden("s_orf.extract")
overlap_filter = bakta.forbidden("s_orf.overlap_filter")
annotation_filter = bakta.forbidden("s_orf.annotation_filter")
''',
    "bakta/ups.py": '''
import bakta

lookup = bakta.forbidden("ups.lookup")
''',
    "bakta/ips.py": '''
import bakta

lookup = bakta.forbidden("ips.lookup")
''',
    "bakta/psc.py": '''
import bakta

search = bakta.forbidden("psc.search")
lookup = bakta.forbidden("psc.lookup")
''',
    "bakta/pscc.py": '''
import bakta

search = bakta.forbidden("pscc.search")
lookup = bakta.forbidden("pscc.lookup")
''',
    "bakta/expert/__init__.py": "",
    "bakta/expert/amrfinder.py": '''
import bakta

search = bakta.forbidden("exp_amr.search")
''',
    "bakta/expert/protein_sequences.py": '''
import bakta

search = bakta.forbidden("exp_aa_seq.search")
''',
    "bakta/expert/protein_hmms.py": '''
import bakta

search = bakta.forbidden("exp_aa_hmms.search")
''',
}

#: Functional-annotation entry points that must never be reached. Each is a
#: `bakta.forbidden(...)` in the stub, so reaching one both raises AND leaves
#: a FORBIDDEN: marker in the trace.
_FORBIDDEN_CALLS = (
    "ups.lookup",
    "ips.lookup",
    "psc.search",
    "psc.lookup",
    "pscc.search",
    "pscc.lookup",
    "exp_amr.search",
    "exp_aa_seq.search",
    "exp_aa_hmms.search",
    "cds.predict_pfam",
    "cds.predict_pseudo_candidates",
    "cds.detect_pseudogenes",
    "cds.analyze_proteins",
    "annotation.combine_annotation",
    "nc_rna_region.predict_nc_rna_regions",
    "s_orf.extract",
)


def _write_stub(root: Path) -> Path:
    """Materialise the stub ``bakta`` package under *root*.

    Args:
        root: Directory to create ``bakta/`` in; becomes the PYTHONPATH
            entry.

    Returns:
        *root*, for chaining.
    """
    for rel, source in _STUB_FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    return root


def _run_driver_against_stub(
    tmp_path: Path,
    extra_args: list[str] | None = None,
    stub_version: str | None = None,
) -> tuple[subprocess.CompletedProcess, list[dict[str, Any]], Path]:
    """Run the real driver as a subprocess against the stub ``bakta``.

    Args:
        tmp_path: Scratch dir.
        extra_args: Extra driver flags (e.g. ``["--no-non-coding"]``).
        stub_version: ``bakta.__version__`` the stub should report.

    Returns:
        ``(completed_process, trace_entries, out_json_path)``.
    """
    stub_root = _write_stub(tmp_path / "stub")
    trace = tmp_path / "trace.jsonl"
    contigs = tmp_path / "contigs.fna"
    contigs.write_text(">c1\n" + "ACGT" * 250 + "\n", encoding="utf-8")
    out_json = tmp_path / "out.json"

    env = dict(os.environ)
    env["PYTHONPATH"] = str(stub_root)
    env["BAKTA_STUB_TRACE"] = str(trace)
    if stub_version is not None:
        env["BAKTA_STUB_VERSION"] = stub_version
    # The stub must be the ONLY bakta importable, and kbutillib must be
    # unreachable -- the driver is not allowed to need it.
    env.pop("PYTHONHOME", None)

    completed = subprocess.run(
        [
            sys.executable,
            str(DRIVER_PATH),
            "--db",
            str(tmp_path / "fake-db"),
            "--contigs",
            str(contigs),
            "--out",
            str(out_json),
            "--threads",
            "2",
            "--translation-table",
            "11",
            *(extra_args or []),
        ],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=env,
        check=False,
    )
    entries: list[dict[str, Any]] = []
    if trace.exists():
        entries = [
            json.loads(line) for line in trace.read_text().splitlines() if line.strip()
        ]
    return completed, entries, out_json


class TestDriverStub:
    """The driver's call order and abstinence, proven in a subprocess."""

    @pytest.fixture()
    def run(self, tmp_path):
        completed, entries, out_json = _run_driver_against_stub(tmp_path)
        assert completed.returncode == 0, completed.stderr
        return completed, entries, out_json

    def test_structural_call_order_is_exactly_main_pys(self, run):
        _, entries, _ = run
        calls = [entry["call"] for entry in entries]
        assert calls == [
            "utils.parse_arguments",
            "config.check_output_path",
            "config.setup",
            "db.check",
            "fasta.import_sequences",
            "utils.qc_sequences",
            "fasta.export_sequences",
            "t_rna.predict_t_rnas",
            "tm_rna.predict_tm_rnas",
            "r_rna.predict_r_rnas",
            "nc_rna.predict_nc_rnas",
            "crispr.predict_crispr",
            "cds.predict",
            "cds.filter_length",
            "orf.detect_spurious",
            "annotation.detect_feature_overlaps",
        ]

    def test_no_annotation_function_is_called(self, run):
        _, entries, _ = run
        calls = {entry["call"] for entry in entries}
        for forbidden in _FORBIDDEN_CALLS:
            assert "FORBIDDEN:" + forbidden not in calls
        assert not any(call.startswith("FORBIDDEN:") for call in calls)

    def test_bakta_argv_carries_the_required_flags(self, run):
        _, entries, _ = run
        argv = next(e for e in entries if e["call"] == "utils.parse_arguments")["argv"]
        for flag in (
            "--keep-contig-headers",
            "--skip-ncrna-region",
            "--skip-sorf",
            "--skip-gap",
            "--skip-ori",
            "--skip-pseudo",
            "--skip-plot",
        ):
            assert flag in argv
        assert argv[argv.index("--min-contig-length") + 1] == "1"
        assert argv[argv.index("--threads") + 1] == "2"
        assert argv[argv.index("--translation-table") + 1] == "11"

    def test_complete_and_compliant_are_never_passed(self, run):
        _, entries, _ = run
        argv = next(e for e in entries if e["call"] == "utils.parse_arguments")["argv"]
        assert "--complete" not in argv
        assert "--compliant" not in argv

    def test_non_coding_skips_are_absent_by_default(self, run):
        _, entries, _ = run
        argv = next(e for e in entries if e["call"] == "utils.parse_arguments")["argv"]
        for flag in (
            "--skip-trna",
            "--skip-tmrna",
            "--skip-rrna",
            "--skip-ncrna",
            "--skip-crispr",
        ):
            assert flag not in argv

    def test_sequences_fna_is_written_into_the_bakta_tmp_dir(self, run):
        _, entries, _ = run
        exported = next(e for e in entries if e["call"] == "fasta.export_sequences")
        assert Path(exported["path"]).name == "sequences.fna"

    def test_discarded_cds_never_reach_the_overlap_pass(self, run):
        """R5: the length/spurious accumulator is separate from
        data['features']."""
        _, entries, _ = run
        seen = next(
            e for e in entries if e["call"] == "annotation.detect_feature_overlaps"
        )["seen"]
        cds_starts = sorted(start for kind, start in seen if kind == "cds")
        # cds.predict makes three (2000 keep / 2400 length / 2800 spurious);
        # only the kept one may be present.
        assert cds_starts == [2000]

    def test_filter_length_runs_before_antifam_on_the_full_set(self, run):
        _, entries, _ = run
        length = next(e for e in entries if e["call"] == "cds.filter_length")
        spurious = next(e for e in entries if e["call"] == "orf.detect_spurious")
        assert length["n"] == 3  # all predicted CDS
        assert spurious["n"] == 2  # the length discard is already gone

    def test_output_json_shape(self, run):
        _, _, out_json = run
        payload = json.loads(out_json.read_text())
        assert payload["contig_ids"] == ["c1"]
        assert payload["versions"] == {
            "bakta": _DRIVER_PINNED_BAKTA_VERSION,
            "db": {"major": 6, "minor": 0, "type": "full"},
        }
        # 2 tRNA + 1 rRNA + 1 ncRNA + 1 crispr + 3 CDS (1 kept,
        # 1 length-discarded, 1 spurious-discarded).
        assert len(payload["features"]) == 8

    def test_output_carries_discarded_features_with_reasons(self, run):
        _, _, out_json = run
        payload = json.loads(out_json.read_text())
        discards = {
            feature["discarded"]["type"]
            for feature in payload["features"]
            if "discarded" in feature
        }
        assert discards == {"length", "spurious", "overlap"}

    def test_overlap_interval_is_recovered_from_the_description(self, run):
        _, _, out_json = run
        payload = json.loads(out_json.read_text())
        overlap = next(
            f
            for f in payload["features"]
            if f.get("discarded", {}).get("type") == "overlap"
        )
        assert overlap["discarded"]["overlap_start"] == 400
        assert overlap["discarded"]["overlap_stop"] == 450
        assert overlap["discarded"]["feature_type"] == "tmRNA"

    def test_output_is_built_field_by_field_not_dumped(self, run):
        """R5: no bytes digests, no raw Bakta dicts."""
        _, _, out_json = run
        payload = json.loads(out_json.read_text())
        for feature in payload["features"]:
            assert "aa_digest" not in feature
            assert "aa_hexdigest" not in feature
            assert "db_xrefs" not in feature
            assert "_stub_fate" not in feature
            assert "sequence" not in feature  # renamed to contig_id

    def test_bakta_keys_are_renamed_for_output(self, run):
        _, _, out_json = run
        payload = json.loads(out_json.read_text())
        trna = next(
            f for f in payload["features"] if f["type"] == "tRNA" and f["start"] == 10
        )
        assert trna["contig_id"] == "c1"
        assert trna["anticodon"] == "ggt"  # Bakta's key is `anti_codon`
        assert trna["amino_acid"] == "Thr"

    def test_rfam_is_extracted_and_go_xrefs_dropped(self, run):
        _, _, out_json = run
        payload = json.loads(out_json.read_text())
        rrna = next(f for f in payload["features"] if f["type"] == "rRNA")
        assert rrna["rfam"] == "RF00001"
        for feature in payload["features"]:
            assert not any(
                isinstance(v, str) and v.startswith("GO:") for v in feature.values()
            )

    def test_so_namedtuple_class_is_rendered_as_a_string(self, run):
        _, _, out_json = run
        payload = json.loads(out_json.read_text())
        ncrna = next(f for f in payload["features"] if f["type"] == "ncRNA")
        assert ncrna["ncrna_class"] == "ribozyme"

    def test_no_non_coding_skips_every_non_coding_caller(self, tmp_path):
        completed, entries, out_json = _run_driver_against_stub(
            tmp_path, extra_args=["--no-non-coding"]
        )
        assert completed.returncode == 0, completed.stderr
        calls = [entry["call"] for entry in entries]
        for call in (
            "t_rna.predict_t_rnas",
            "tm_rna.predict_tm_rnas",
            "r_rna.predict_r_rnas",
            "nc_rna.predict_nc_rnas",
            "crispr.predict_crispr",
        ):
            assert call not in calls
        argv = next(e for e in entries if e["call"] == "utils.parse_arguments")["argv"]
        for flag in (
            "--skip-trna",
            "--skip-tmrna",
            "--skip-rrna",
            "--skip-ncrna",
            "--skip-crispr",
        ):
            assert flag in argv
        payload = json.loads(out_json.read_text())
        assert {f["type"] for f in payload["features"]} == {"cds"}

    def test_version_mismatch_exits_three_before_doing_anything(self, tmp_path):
        completed, entries, out_json = _run_driver_against_stub(
            tmp_path, stub_version="1.11.3"
        )
        assert completed.returncode == 3
        assert "Bakta version mismatch" in completed.stderr
        assert _DRIVER_PINNED_BAKTA_VERSION in completed.stderr
        assert "1.11.3" in completed.stderr
        assert entries == []  # nothing ran
        assert not out_json.exists()


class TestNativeDriverEndToEnd:
    """``call_features`` through its DEFAULT NATIVE runner path, no fakes.

    ``_run_driver`` is not replaced here: ``call_features`` builds its own
    native argv, really spawns the real driver as a subprocess, and really
    parses what comes back. Only *Bakta* is substituted, by putting the stub
    package on the child's ``PYTHONPATH``. That makes this a no-injection
    check of the whole native branch -- argv construction, subprocess,
    contig-id round trip, kept/discarded split, version block -- on a host
    with neither Docker nor a Bakta install.
    """

    @pytest.fixture()
    def result(self, tmp_path, monkeypatch) -> BaktaCallResult:
        stub_root = _write_stub(tmp_path / "stub")
        monkeypatch.setenv("PYTHONPATH", str(stub_root))
        monkeypatch.setenv("BAKTA_STUB_TRACE", str(tmp_path / "trace.jsonl"))
        bu = _make_utils(docker_image="", db_path=str(tmp_path))
        bu._call_python = sys.executable
        return bu.call_features([("c1", "ACGT" * 250)], threads=2)

    def test_kept_features_are_parsed(self, result):
        # 1 tRNA (the other is overlap-discarded) + 1 rRNA + 1 ncRNA
        # + 1 crispr + 1 CDS (of 3 predicted).
        assert len(result.features) == 5
        assert sorted(f["type"] for f in result.features) == [
            "cds",
            "crispr",
            "ncRNA",
            "rRNA",
            "tRNA",
        ]

    def test_discarded_counts_are_parsed(self, result):
        assert result.discarded["counts"] == {
            "overlap": 1,
            "spurious": 1,
            "length": 1,
        }
        assert len(result.discarded["cds"]) == 2

    def test_versions_report_native(self, result):
        assert result.versions["bakta"] == _DRIVER_PINNED_BAKTA_VERSION
        assert result.versions["db"]["type"] == "full"
        assert result.versions["image"] == "native"
        assert result.versions["image_id"] == "native"
        assert len(result.versions["driver_sha256"]) == 64

    def test_crispr_keeps_its_unknown_strand(self, result):
        crispr = next(f for f in result.features if f["type"] == "crispr")
        assert crispr["strand"] == "?"

    def test_cds_carries_completeness_and_stripped_aa(self, result):
        cds = next(f for f in result.features if f["type"] == "cds")
        assert cds["aa"] == "MKV"  # stub emits "MKV*"
        assert cds["start_complete"] is True
        assert cds["stop_complete"] is True
        assert cds["partial"] is False

    def test_contig_id_round_trips(self, result):
        assert {f["contig_id"] for f in result.features} == {"c1"}

    def test_version_mismatch_maps_to_bakta_call_error(
        self, tmp_path, monkeypatch
    ):
        """The exit-3 path, end to end through the real subprocess."""
        stub_root = _write_stub(tmp_path / "stub")
        monkeypatch.setenv("PYTHONPATH", str(stub_root))
        monkeypatch.setenv("BAKTA_STUB_TRACE", str(tmp_path / "trace.jsonl"))
        monkeypatch.setenv("BAKTA_STUB_VERSION", "1.11.3")
        bu = _make_utils(docker_image="", db_path=str(tmp_path))
        bu._call_python = sys.executable
        with pytest.raises(BaktaCallError) as excinfo:
            bu.call_features([("c1", "ACGT" * 250)])
        message = str(excinfo.value)
        assert _DRIVER_PINNED_BAKTA_VERSION in message
        assert "1.11.3" in message


# ---------------------------------------------------------------------------
# Driver packaging / purity
# ---------------------------------------------------------------------------


class TestDriverPackaging:
    """The driver ships, and stays importable only against Bakta."""

    def test_driver_file_exists_beside_bakta_utils(self):
        assert DRIVER_PATH.is_file()
        assert BaktaUtils._driver_path().name == _DRIVER_FILENAME

    def test_driver_is_declared_as_package_data(self):
        pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert f'"domains/genome/annotation/{_DRIVER_FILENAME}"' in pyproject

    def test_driver_imports_only_bakta_and_stdlib(self):
        import ast

        tree = ast.parse(DRIVER_PATH.read_text(encoding="utf-8"))
        roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
        assert "kbutillib" not in roots
        allowed = set(sys.stdlib_module_names) | {"bakta", "__future__"}
        assert roots <= allowed, f"non-stdlib, non-bakta imports: {roots - allowed}"

    def test_driver_pin_matches_the_adapters_pin(self):
        source = DRIVER_PATH.read_text(encoding="utf-8")
        assert (
            f'PINNED_BAKTA_VERSION = "{_DRIVER_PINNED_BAKTA_VERSION}"' in source
        )

    def test_exported_from_annotation_package(self):
        from kbutillib.domains.genome.annotation import (
            BaktaCallError as ExportedError,
        )
        from kbutillib.domains.genome.annotation import (
            BaktaCallResult as ExportedResult,
        )

        assert ExportedError is BaktaCallError
        assert ExportedResult is BaktaCallResult


# ---------------------------------------------------------------------------
# Real-image integration (NO-INJECTION CHECK)
# ---------------------------------------------------------------------------
#
# A 4,801 bp slice of Escherichia coli K-12 MG1655, NC_000913.3
# 3,422,400-3,427,200 (plus strand of the reference). It spans the end of the
# yhdZ gene and the whole rrnD ribosomal RNA operon, which is transcribed on
# the MINUS strand, so most features below come back with strand '-'.
#
# NCBI RefSeq annotation, in LOCAL 1-based coordinates of the string below
# (local = absolute - 3,422,399):
#
#     yhdZ  CDS       37 - 795     (+)  ABC transporter ATP-binding subunit
#     rrfF  5S rRNA   1024 - 1143  (-)
#     thrV  tRNA-Thr  1181 - 1256  (-)
#     rrfD  5S rRNA   1269 - 1388  (-)
#     rrlD  23S rRNA  1481 - 4384  (-)
#     alaU  tRNA-Ala  4559 - 4634  (-)
#     ileU  tRNA-Ile  4677 - 4753  (-)
#
# That is a three-tRNA cluster plus two complete 5S rRNA genes -- chosen so
# the assertions below ("at least one tRNA", "at least one rRNA") are testing
# a region where Bakta genuinely has something to find, and so a coordinate
# regression would show up as a shifted gene rather than an empty result.
_ECOLI_RRND_REGION = "".join(
    "GTTTTAACACCGGGCGTACACCGCATTGAGGACACTATGAGCCAAATTTTACTGCAACCTGCTAACGCGA"
    "TGATTACGCTGGAAAACGTCAATAAATGGTATGGACAATTCCATGTTTTGAAAAATATAAATTTAACCGT"
    "GCAACCGGGAGAACGGATCGTTCTGTGTGGCCCTTCAGGTTCCGGTAAATCGACAACCATTCGTTGTATT"
    "AATCATCTGGAAGAACATCAACAGGGACGGATCGTGGTAGATGGCATCGAACTTAATGAAGATATCCGCA"
    "ATATTGAGCGCGTCAGGCAGGAAGTGGGAATGGTCTTTCAGCATTTCAATCTCTTCCCTCATCTGACCGT"
    "TTTACAGAACTGTACCCTGGCACCGATTTGGGTACGCAAGATGCCTAAGAAAGAGGCTGAAGATCTGGCG"
    "GTGCATTACCTAGAGCGGGTGAGAATTGCCGAACATGCGCATAAGTTTCCCGGACAGATTTCAGGTGGTC"
    "AGCAGCAACGCGTTGCCATTGCGCGTTCGCTGTGTATGAAGCCGAAAATTATGTTGTTTGATGAGCCAAC"
    "GTCGGCGCTCGATCCTGAGATGGTGAAAGAGGTGCTGGATACGATGATTGGGCTGGCGCAGTCGGGTATG"
    "ACAATGTTGTGTGTAACACATGAGATGGGGTTTGCACGAACCGTCGCTGACCGGGTAATTTTTATGGATC"
    "GTGGGGAAATAGTGGAGCAAGCTGCACCTGATGAATTTTTTGCGCATCCTAAATCAGAGCGTACGAGGGC"
    "ATTTTTATCGCAGGTAATCCATTAATTGAATGTTAGTTCGAAAAGCAAAAAGGCCATCCTTTCGGATGGC"
    "CTTTCGCTTGATTTGATGTCTGGCAGTTTATGGCGGGCGTCCTGCCCGCCACCCTCCGGGCCGTTGCTTC"
    "GCAACGTTCAAATCCGCTCCCGGCGGATTTGTCCTACTCGGGAGAGTGTTCACCGACAAACAACAGATAA"
    "AACAAAAGGCCCAGTCTTCCGACTGAGCCTTTTGTTTTATTTGATGTCTGGCAGTTCCCTACTCTCGCAT"
    "GGGGAGACCCCACACTACCATCGGCGCTACGGCGTTTCACTTCTGAGTTCGGCATGGGGTCAGGTGGGAC"
    "CACCGCGCTACTGCCGCCAGACAAATTCTTTTCTAATCTGCCGAACTTTAACCTAAAAAGTGGTGCTGAT"
    "ACCCAGAGTCGAACTGGGGACCTCACCCTTACCAAGGGTGCGCTCTACCAACTGAGCCATATCAGCACGC"
    "TAAATTTGATGCCTGGCAGTTCCCTACTCTCGCATGGGGAGACCCCACACTACCATCGGCGCTACGGCGT"
    "TTCACTTCTGAGTTCGGCATGGGGTCAGGTGGGACCACCGCGCTACGGCCGCCAGGCAAATTCTGTTTTA"
    "TCAGACCGCTTCTGCGTTCTGATTTAATCTGTATCAGGCTGAAAATCTTCTCTCATCCGCCAAAACAGCT"
    "TCGGCGTTGTAAGGTTAAGCCTCACGGTTCATTAGTACCGGTTAGCTCAACGCATCGCTGCGCTTACACA"
    "CCCGGCCTATCAACGTCGTCGTCTTCAACGTTCCTTCAGGACCCTTAAAGGGTCAGGGAGAACTCATCTC"
    "GGGGCAAGTTTCGTGCTTAGATGCTTTCAGCACTTATCTCTTCCGCATTTAGCTACCGGGCAGTGCCATT"
    "GGCATGACAACCCGAACACCAGTGATGCGTCCACTCCGGTCCTCTCGTACTAGGAGCAGCCCCCCTCAGT"
    "TCTCCAGCGCCCACGGCAGATAGGGACCGAACTGTCTCACGACGTTCTAAACCCAGCTCGCGTACCACTT"
    "TAAATGGCGAACAGCATCACCCTTGGGACCTACTTCAGCCCCAGGATGTGATGAGCCGACATCGAGGTGC"
    "CAAACACCGCCGTCGATATGAACTCTTGGGCGGTATCAGCCTGTTATCCCCGGAGTACCTTTTATCCGTT"
    "GAGCGATGGCCCTTCCATTCAGAACCACCGGATCACTATGACCTGCTTTCGCACCTGCTCGCGCCGTCAC"
    "GCTCGCAGTCAAGCTGGCTTATGCCATTGCACTAACCTCCTGATGTCCGACCAGGATTAGCCAACCTTCG"
    "TGCTCCTCCGTTACTCTTTAGGAGGAGACCGCCCCAGTCAAACTACCCACCAGACACTGTCCGCAACCCG"
    "GATTACGGGTCAACGTTAGAACATCAAACATTAAAGGGTGGTATTTCAAGGTCGGCTCCATGCAGACTGG"
    "CGTCCACACTTCTAAGCCTCCCACCTATCCTACACATCAAGGCTCAATGTTCAGTGTCAAGCTATAGTAA"
    "AGGTTCACGGGGTCTTTCCGTCTTGCCGCGGGTACACTGCATCTTCACAGCGAGTTCAATTTCACTGAGT"
    "CTCGGGTGGAGACAGCCTGGCCATCATTACGCCATTCGTGCAGGTCGGAACTTACCCGACAAGGAATTTC"
    "GCTACCTTAGGACCGTTATAGTTACGGCCGCCGTTTACCGGGGCTTCGATCAAGAGCTTCGCTTGCGCTA"
    "ACCCCATCAATTAACCTTCCGGCACCGGGCAGGCGTCACACCGTATACGTCCACTTTCGTGTTTGCACAG"
    "TGCTGTGTTTTTAATAAACAGTTGCAGCCAGCTGGTATCTTCGACTGATTTCAGCTCCACGAGCAAGTCG"
    "CTTCACCTACATATCAGCGTGCCTTCTCCCGAAGTTACGGCACCATTTTGCCTAGTTCCTTCACCCGAGT"
    "TCTCTCAAGCGCCTTGGTATTCTCTACCTGACCACCTGTGTCGGTTTGGGGTACGATTTGATGTTACCTG"
    "ATGCTTAGAGGCTTTTCCTGGAAGCAGGGCATTTGTTGCTTCAGCACCGTAGTGCCTCGTCATCACGCCT"
    "CAGCCTTGATTTTCCGGATTTGCCTGGAAAACCAGCCTACACGCTTAAACCGGGACAACCGTCGCCCGGC"
    "CAACATAGCCTTCTCCGTCCCCCCTTCGCAGTAACACCAAGTACAGGAATATTAACCTGTTTCCCATCGA"
    "CTACGCCTTTCGGCCTCGCCTTAGGGGTCGACTCACCCTGCCCCGATTAACGTTGGACAGGAACCCTTGG"
    "TCTTCCGGCGAGCGGGCTTTTCACCCGCTTTATCGTTACTTATGTCAGCATTCGCACTTCTGATACCTCC"
    "AGCATGCCTCACAGCACACCTTCGCAGGCTTACAGAACGCTCCCCTACCCAACAACGCATAAGCGTCGCT"
    "GCCGCAGCTTCGGTGCATGGTTTAGCCCCGTTACATCTTCCGCGCAGGCCGACTCGACCAGTGAGCTATT"
    "ACGCTTTCTTTAAATGATGGCTGCTTCTAAGCCAACATCCTGGCTGTCTGGGCCTTCCCACATCGTTTCC"
    "CACTTAACCATGACTTTGGGACCTTAGCTGGCGGTCTGGGTTGTTTCCCTCTTCACGACGGACGTTAGCA"
    "CCCGCCGTGTGTCTCCCGTGATAACATTCTCCGGTATTCGCAGTTTGCATCGGGTTGGTAAGTCGGGATG"
    "ACCCCCTTGCCGAAACAGTGCTCTACCCCCGGAGATGAATTCACGAGGCGCTACCTAAATAGCTTTCGGG"
    "GAGAACCAGCTATCTCCCGGTTTGATTGGCCTTTCACCCCCAGCCACAAGTCATCCGCTAATTTTTCAAC"
    "ATTAGTCGGTTCGGTCCTCCAGTTAGTGTTACCCAACCTTCAACCTGCCCATGGCTAGATCACCGGGTTT"
    "CGGGTCTATACCCTGCAACTTAACGCCCAGTTAAGACTCGGTTTCCCTTCGGCTCCCCTATTCGGTTAAC"
    "CTTGCTACAGAATATAAGTCGCTGACCCATTATACAAAAGGTACGCAGTCACACGCCTAAGCGTGCTCCC"
    "ACTGCTTGTACGTACACGGTTTCAGGTTCTTTTTCACTCCCCTCGCCGGGGTTCTTTTCGCCTTTCCCTC"
    "ACGGTACTGGTTCACTATCGGTCAGTCAGGAGTATTTAGCCTTGGAGGATGGTCCCCCCATATTCAGACA"
    "GGATACCACGTGTCCCGCCCTACTCATCGAGCTCACAGCATGTGCATTTTTGTGTACGGGGCTGTCACCC"
    "TGTATCGCGCGCCTTTCCAGACGCTTCCACTAACACACACACTGATTCAGGCTCTGGGCTGCTCCCCGTT"
    "CGCTCGCCGCTACTGGGGGAATCTCGGTTGATTTCTTTTCCTCGGGGTACTTAGATGTTTCAGTTCCCCC"
    "GGTTCGCCTCATTAACCTATGGATTCAGTTAATGATAGTGTGTCGAAACACACTGGGTTTCCCCATTCGG"
    "AAATCGCCGGTTATAACGGTTCATATCACCTTACCGACGCTTATCGCAGATTAGCACGTCCTTCATCGCC"
    "TCTGACTGCCAGGGCATCCACCGTGTACGCTTAGTCGCTTAACCTCACAACCCGAAGATGTTTCACTTCA"
    "GAGTTGCGAAAATTTGAGAGACTCACGAACAACTTTCGTTGTTCAGTGTTTCAATTTTCAGCTTGATCCA"
    "GATTTTTAAAGAGCAAAACTTCGCAGTGAACCTTTGCAGGTACACTCTGAAGTATTTTTTATTTAATCAC"
    "TACAGAGATGGTGGAGCTATGCGGGATCGAACCGCAGACCTCCTGCGTGCAAAGCAGGCGCTCTCCCAGC"
    "TGAGCTATAGCCCCATAACATGTAGTTAAAACCTCTTCAAATTTGCCGTGCAAATTTGGTAGGCCTGAGT"
    "GGACTTGAACCACCGACCTCACCCTTATCAGGGGTGCGCTCTAACCACCTGAGCTACAAGCCTGTAGAGG"
    "TTTTACTGCTCATTTTCATCAGACAATCTGTGTGAGCACTG"
)


def _real_image_present() -> bool:
    """Return True if ``docker image inspect kbutillib/bakta:1.11.4`` exits 0.

    Returns:
        True when the pinned image is present locally.
    """
    try:
        completed = subprocess.run(
            ["docker", "image", "inspect", REAL_IMAGE],
            capture_output=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    return completed.returncode == 0


def _reverse_complement(sequence: str) -> str:
    """Reverse-complement a DNA string.

    Bakta stores a minus-strand feature's ``nt`` as the reverse complement of
    the contig slice (``bakta.utils.extract_feature_sequence``), so the
    "nt equals the input slice" assertion has to account for the strand.

    Args:
        sequence: A DNA string over ACGT (any case).

    Returns:
        Its reverse complement, uppercase.
    """
    return sequence.upper().translate(str.maketrans("ACGT", "TGCA"))[::-1]


@pytest.mark.skipif(
    not os.environ.get(BAKTA_DB_ENV),
    reason=f"{BAKTA_DB_ENV} is not set to a Bakta database path",
)
@pytest.mark.skipif(
    not _real_image_present(),
    reason=f"Docker image {REAL_IMAGE} is not present locally",
)
class TestRealImageIntegration:
    """``call_features`` end to end through its DEFAULT runner path.

    Nothing is faked here: no fake runner, no monkeypatched invocation
    helper. ``call_features`` builds its own argv, runs real Docker, runs the
    real Bakta 1.11.4, and the result is checked against the NCBI annotation
    of the embedded region.
    """

    @staticmethod
    @pytest.fixture(scope="class")
    def result() -> BaktaCallResult:
        bu = BaktaUtils(
            db_path=os.environ[BAKTA_DB_ENV],
            config_file=False,
            token_file=None,
            kbase_token_file=None,
            docker_image=REAL_IMAGE,
        )
        return bu.call_features(
            [("NC_000913.3_rrnD", _ECOLI_RRND_REGION)], threads=4
        )

    def test_finds_at_least_one_trna(self, result):
        trnas = [f for f in result.features if f["type"] == "tRNA"]
        assert len(trnas) >= 1
        for trna in trnas:
            assert trna["amino_acid"]
            assert trna["anticodon"]

    def test_finds_at_least_one_rrna(self, result):
        rrnas = [f for f in result.features if f["type"] == "rRNA"]
        assert len(rrnas) >= 1
        assert any(f["rfam"] for f in rrnas)

    def test_every_kept_feature_nt_equals_the_input_slice(self, result):
        for feature in result.features:
            sliced = _ECOLI_RRND_REGION[feature["start"] - 1 : feature["stop"]]
            expected = (
                sliced if feature["strand"] == "+" else _reverse_complement(sliced)
            )
            assert feature["nt"] == expected, (
                f"{feature['type']} {feature['start']}-{feature['stop']}"
                f"{feature['strand']} nt does not match the input slice"
            )

    def test_coordinates_are_one_based_inclusive_and_ordered(self, result):
        for feature in result.features:
            assert 1 <= feature["start"] <= feature["stop"]
            assert feature["stop"] <= len(_ECOLI_RRND_REGION)

    def test_strands_respect_r1(self, result):
        for feature in result.features:
            if feature["type"] == "crispr":
                assert feature["strand"] in {"+", "-", "?"}
            else:
                assert feature["strand"] in {"+", "-"}

    def test_calls_the_yhdz_cds_at_its_refseq_coordinates(self, result):
        cdss = [f for f in result.features if f["type"] == "cds"]
        assert any(f["start"] == 37 and f["stop"] == 795 for f in cdss), [
            (f["start"], f["stop"]) for f in cdss
        ]

    def test_no_functional_annotation_leaks_into_the_result(self, result):
        """Structural only: a CDS has no product and no ontology accessions."""
        for cds in (f for f in result.features if f["type"] == "cds"):
            assert cds["product"] is None
            assert "psc" not in cds
            assert "ips" not in cds
            assert "gene" not in cds

    def test_every_cds_carries_completeness_flags(self, result):
        for cds in (f for f in result.features if f["type"] == "cds"):
            assert isinstance(cds["start_complete"], bool)
            assert isinstance(cds["stop_complete"], bool)
            assert cds["partial"] == (
                not cds["start_complete"] or not cds["stop_complete"]
            )

    def test_versions_record_real_provenance(self, result):
        assert result.versions["bakta"] == _DRIVER_PINNED_BAKTA_VERSION
        assert result.versions["image"] == REAL_IMAGE
        assert result.versions["image_id"].startswith("sha256:")
        assert len(result.versions["driver_sha256"]) == 64
        assert result.versions["db"]["type"] in {"full", "light"}

    def test_discard_counts_are_present_for_all_three_types(self, result):
        assert set(result.discarded["counts"]) == {"overlap", "spurious", "length"}

    def test_call_non_coding_false_returns_cds_only(self):
        bu = BaktaUtils(
            db_path=os.environ[BAKTA_DB_ENV],
            config_file=False,
            token_file=None,
            kbase_token_file=None,
            docker_image=REAL_IMAGE,
        )
        result = bu.call_features(
            [("NC_000913.3_rrnD", _ECOLI_RRND_REGION)],
            threads=4,
            call_non_coding=False,
        )
        assert result.call_non_coding is False
        assert {f["type"] for f in result.features} <= {"cds"}
