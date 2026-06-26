"""Offline + live tests for dram2_utils.py.

Test strategy
-------------
The Nextflow subprocess path (``_run_nextflow``, ``annotate`` when the
pipeline is reachable) is exercised only in the
``@pytest.mark.integration`` live test, which is doubly gated by both
``KBU_DRAM2_LIVE=1`` and ``DRAM2Utils().is_available()`` so it runs only
on h100.

The pure parse functions and CLI-builder are tested fully offline using
the committed golden fixture under ``tests/fixtures/dram2/``.

Coverage gate
-------------
The ``_run_nextflow`` body's success branch is reached via a
subprocess-mocking test; its failure (``CalledProcessError``) branch is
covered too.  Everything else — ``is_available`` returning False on each
missing dependency, the ``_guard_protein`` raise on nucleotide input,
the ``_require_available`` raise, and every line of
``_parse_annotations_tsv`` / ``_parse_dram2_version`` —  is covered by
the offline tests so the repo's ``fail_under=100`` gate stays green.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kbutillib.annotator_utils import (
    AnnotationRecord,
    AnnotationResult,
    Term,
    ToolUnavailableError,
)
from kbutillib.dram2_utils import (
    DRAM2Utils,
    _DEFAULT_DATABASES,
    _DEFAULT_NXF_VER,
    _parse_annotations_tsv,
    _parse_dram2_version,
)

# ---------------------------------------------------------------------------
# Fixture paths
# ---------------------------------------------------------------------------

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "dram2"
_GOLDEN_TSV = _FIXTURES / "raw-annotations.tsv"
_GOLDEN_FAA = _FIXTURES / "demo.faa"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_utils(**kwargs: Any) -> DRAM2Utils:
    """Construct DRAM2Utils with no filesystem discovery."""
    return DRAM2Utils(
        config_file=False,
        token_file=None,
        kbase_token_file=None,
        **kwargs,
    )


def _make_available_utils(tmp_path: Path) -> DRAM2Utils:
    """Construct a DRAM2Utils whose ``is_available()`` is True via stubs.

    Creates a fake nextflow binary on PATH (executable shim), a fake
    ``main.nf`` file, and points launch_dir at *tmp_path*.
    """
    pipeline = tmp_path / "main.nf"
    pipeline.write_text("// stub\n")
    launch = tmp_path / "launch"
    launch.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    nf = bin_dir / "nextflow"
    nf.write_text("#!/usr/bin/env bash\nexit 0\n")
    nf.chmod(0o755)
    utils = _make_utils()
    utils._nextflow_exe = str(nf)
    utils._pipeline = str(pipeline)
    utils._launch_dir = str(launch)
    return utils


def _read_faa_headers(path: Path) -> list[str]:
    """Read FASTA headers from a .faa, returning first token of each header."""
    ids: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(">"):
            ids.append(line[1:].split()[0])
    return ids


def _read_caller_ids_from_faa(path: Path) -> list[str]:
    """Read FASTA headers from a .faa, returning them in file order.

    Returns the first whitespace token (the id) for each header.
    """
    return _read_faa_headers(path)


def _make_identity_map(ids: list[str]) -> dict[str, str]:
    """Build an identity {id: id} map for adapting golden-fixture parse calls."""
    return {i: i for i in ids}


# ---------------------------------------------------------------------------
# _parse_annotations_tsv — pure parser tests against the golden fixture
# ---------------------------------------------------------------------------


class TestParseAnnotationsTsv:
    """Tests for _parse_annotations_tsv against the committed golden fixture.

    All calls now pass an identity {id: id} map (instead of the old list[str]
    signature) since query_ids in the golden fixture already match caller ids.
    """

    def test_returns_records_for_called_ids(self):
        ids = _read_caller_ids_from_faa(_GOLDEN_FAA)
        records = _parse_annotations_tsv(
            _GOLDEN_TSV.read_text(encoding="utf-8"),
            _make_identity_map(ids),
        )
        assert len(records) >= 3
        for rec in records:
            assert isinstance(rec, AnnotationRecord)
            assert rec.gene_id in ids

    def test_zero_hit_row_absent_from_records(self):
        """Row OWC_0000_k121_3157_1 has only the prefix columns populated
        (no DB hit anywhere) -> must be absent from records."""
        ids = _read_caller_ids_from_faa(_GOLDEN_FAA)
        records = _parse_annotations_tsv(
            _GOLDEN_TSV.read_text(encoding="utf-8"),
            _make_identity_map(ids),
        )
        rec_ids = {r.gene_id for r in records}
        assert "OWC_0000_k121_3157_1" not in rec_ids

    def test_caller_ids_outside_input_set_skipped(self):
        """A query_id that is NOT in the caller's input map is dropped."""
        # Pass only one of the five fixture ids
        records = _parse_annotations_tsv(
            _GOLDEN_TSV.read_text(encoding="utf-8"),
            {"OWC_0000_k121_3157_3": "OWC_0000_k121_3157_3"},
        )
        rec_ids = {r.gene_id for r in records}
        assert rec_ids == {"OWC_0000_k121_3157_3"}

    def test_kegg_id_emits_ko_namespace(self):
        """`kegg_id` column -> Term(namespace='KO', id=<K-number>)."""
        records = _parse_annotations_tsv(
            _GOLDEN_TSV.read_text(encoding="utf-8"),
            {"OWC_0000_k121_3157_2": "OWC_0000_k121_3157_2"},
        )
        rec = records[0]
        ko_terms = [t for t in rec.terms if t.namespace == "KO"]
        # Both kegg_id and kofam_id are K14127 for this row
        assert {t.id for t in ko_terms} == {"K14127"}

    def test_kofam_id_emits_ko_namespace(self):
        records = _parse_annotations_tsv(
            _GOLDEN_TSV.read_text(encoding="utf-8"),
            {"OWC_0001_k121_21365628_15": "OWC_0001_k121_21365628_15"},
        )
        rec = records[0]
        ko_terms = [t for t in rec.terms if t.namespace == "KO"]
        assert any(t.id == "K23356" for t in ko_terms)
        # The KO term must carry the source column in evidence
        kofam = next(t for t in ko_terms if t.evidence.get("source") == "kofam_id")
        assert kofam.id == "K23356"

    def test_ec_columns_split_and_prefix_stripped(self):
        """EC values like 'EC:1.8.98.5; EC:1.8.98.6' split into individual
        Terms with the 'EC:' prefix stripped."""
        records = _parse_annotations_tsv(
            _GOLDEN_TSV.read_text(encoding="utf-8"),
            {"OWC_0000_k121_3157_2": "OWC_0000_k121_3157_2"},
        )
        rec = records[0]
        ec_terms = [t for t in rec.terms if t.namespace == "EC"]
        ec_ids = {t.id for t in ec_terms}
        # Union of kegg_EC and kofam_EC (the 1.12.99.- comes from kofam)
        assert "1.8.98.5" in ec_ids
        assert "1.8.98.6" in ec_ids
        assert "1.12.99.-" in ec_ids
        assert all(not eid.upper().startswith("EC:") for eid in ec_ids)
        # The dbcan_EC (synthetic '2.4.1.-') also lands here
        assert "2.4.1.-" in ec_ids

    def test_dbcan_id_emits_cazy_namespace(self):
        """`dbcan_id` (synthetic row in fixture) -> namespace='CAZY'."""
        records = _parse_annotations_tsv(
            _GOLDEN_TSV.read_text(encoding="utf-8"),
            {"OWC_0000_k121_3157_2": "OWC_0000_k121_3157_2"},
        )
        rec = records[0]
        cazy_terms = [t for t in rec.terms if t.namespace == "CAZY"]
        assert len(cazy_terms) == 1
        assert cazy_terms[0].id == "GT4"
        assert cazy_terms[0].evidence.get("source") == "dbcan_id"

    def test_description_columns_emit_free_text(self):
        """`*_description` columns -> Term(namespace=None, id=None)."""
        records = _parse_annotations_tsv(
            _GOLDEN_TSV.read_text(encoding="utf-8"),
            {"OWC_0000_k121_3157_3": "OWC_0000_k121_3157_3"},
        )
        rec = records[0]
        free = [t for t in rec.terms if t.namespace is None]
        # KEGG + kofam descriptions both populated
        assert len(free) >= 2
        joined = " | ".join(t.value for t in free)
        assert "heterodisulfide reductase subunit A2" in joined

    def test_empty_text_returns_empty(self):
        assert _parse_annotations_tsv("", {"x": "x"}) == []

    def test_header_without_query_id_returns_empty(self):
        tsv = "foo\tbar\nA\tB\n"
        assert _parse_annotations_tsv(tsv, {"A": "A"}) == []

    def test_records_in_input_order_not_tsv_order(self):
        """Records are emitted in the caller's input (map insertion) order."""
        ids = [
            "OWC_0001_k121_21365628_15",
            "OWC_0000_k121_3157_3",
            "OWC_0000_k121_3157_2",
        ]
        records = _parse_annotations_tsv(
            _GOLDEN_TSV.read_text(encoding="utf-8"),
            _make_identity_map(ids),
        )
        result_order = [r.gene_id for r in records]
        assert result_order == ids

    def test_short_row_padded(self):
        """A TSV row with fewer fields than the header is padded with empty
        strings (no parse error)."""
        tsv = (
            "query_id\tkegg_id\tkegg_EC\tkegg_description\n"
            "g0\tK99999\n"  # short row — kegg_EC and kegg_description missing
        )
        records = _parse_annotations_tsv(tsv, {"g0": "g0"})
        assert len(records) == 1
        ko = [t for t in records[0].terms if t.namespace == "KO"]
        assert ko[0].id == "K99999"

    def test_blank_lines_in_body_skipped(self):
        tsv = (
            "query_id\tkegg_id\tkegg_description\n"
            "\n"
            "g0\tK1\thit description\n"
        )
        records = _parse_annotations_tsv(tsv, {"g0": "g0"})
        assert len(records) == 1

    def test_empty_ec_values_skipped(self):
        """A column value of an empty EC piece (e.g. 'EC:;') produces no
        empty-string Term."""
        tsv = (
            "query_id\tkegg_EC\n"
            "g0\tEC:1.1.1.1;EC:;EC:\n"  # second and third pieces strip to empty
        )
        records = _parse_annotations_tsv(tsv, {"g0": "g0"})
        ec = [t for t in records[0].terms if t.namespace == "EC"]
        assert {t.id for t in ec} == {"1.1.1.1"}

    def test_ec_whitespace_only_pieces_skipped(self):
        """Whitespace-only pieces from EC splitting are dropped before the
        EC-prefix-stripping branch even runs."""
        # A doubled tab in the EC column round-trips through re.split as ['', '']
        tsv = (
            "query_id\tkegg_EC\n"
            "g0\t  \n"  # entire EC value is whitespace
        )
        records = _parse_annotations_tsv(tsv, {"g0": "g0"})
        # No terms at all
        assert records == []

    def test_ec_split_produces_empty_pieces(self):
        """A trailing/leading delimiter produces empty pieces that must be
        dropped at the pre-prefix-strip ``if not piece`` guard."""
        tsv = (
            "query_id\tkegg_EC\n"
            "g0\t;1.1.1.1;\n"  # leading and trailing semicolons -> empty pieces
        )
        records = _parse_annotations_tsv(tsv, {"g0": "g0"})
        ec = [t for t in records[0].terms if t.namespace == "EC"]
        assert {t.id for t in ec} == {"1.1.1.1"}

    def test_unknown_id_column_falls_through_to_free_text(self):
        """An `<unknown>_id` column not in the namespace map gets
        namespace=None and the source column recorded in evidence."""
        tsv = (
            "query_id\tcustom_id\n"
            "g0\tXYZ123\n"
        )
        records = _parse_annotations_tsv(tsv, {"g0": "g0"})
        rec = records[0]
        assert len(rec.terms) == 1
        assert rec.terms[0].namespace is None
        assert rec.terms[0].id == "XYZ123"
        assert rec.terms[0].evidence == {"source": "custom_id"}

    def test_unknown_query_id_dropped(self):
        """A query_id that is absent from emitted_to_caller is silently dropped."""
        tsv = (
            "query_id\tkegg_id\n"
            "g_999\tK12345\n"   # not in the map
            "g_1\tK00001\n"     # in the map
        )
        records = _parse_annotations_tsv(tsv, {"g_1": "b0001"})
        assert len(records) == 1
        assert records[0].gene_id == "b0001"
        assert records[0].terms[0].id == "K00001"

    def test_translation_uses_caller_id_not_emitted_id(self):
        """gene_id in returned records must be the caller id, not the emitted g_<n>."""
        tsv = (
            "query_id\tkofam_id\n"
            "g_1\tK00001\n"
            "g_2\tK00002\n"
        )
        emap = {"g_1": "b0001", "g_2": "b0002"}
        records = _parse_annotations_tsv(tsv, emap)
        assert {r.gene_id for r in records} == {"b0001", "b0002"}
        gene_ids = [r.gene_id for r in records]
        assert gene_ids == ["b0001", "b0002"]


# ---------------------------------------------------------------------------
# _parse_dram2_version
# ---------------------------------------------------------------------------


class TestParseDram2Version:
    def test_revision_tag_extracted(self):
        text = (
            "Launching `repo/main.nf` [foobar] DSL2 - "
            "revision: a1b2c3d4 [v2.0.0-beta17]\n"
        )
        assert _parse_dram2_version(text) == "v2.0.0-beta17"

    def test_nextflow_version_extracted(self):
        text = "N E X T F L O W  ~  version 24.10.5\nLaunching ...\n"
        assert _parse_dram2_version(text) == "24.10.5"

    def test_none_when_no_match(self):
        assert _parse_dram2_version("nothing recognisable here") is None

    def test_empty_returns_none(self):
        assert _parse_dram2_version("") is None


# ---------------------------------------------------------------------------
# is_available — all three checks
# ---------------------------------------------------------------------------


class TestIsAvailable:
    def test_false_when_nextflow_missing_from_path(self, tmp_path: Path):
        utils = _make_utils()
        utils._nextflow_exe = "definitely-not-a-real-binary-xyz"
        utils._pipeline = str(tmp_path / "main.nf")
        utils._launch_dir = str(tmp_path)
        (tmp_path / "main.nf").write_text("")
        assert utils.is_available() is False

    def test_false_when_nextflow_exe_empty(self):
        utils = _make_utils()
        utils._nextflow_exe = ""
        assert utils.is_available() is False

    def test_false_when_nextflow_path_not_executable(self, tmp_path: Path):
        """shutil.which() returns None for a non-executable absolute path."""
        nf = tmp_path / "nextflow"
        nf.write_text("not executable")
        nf.chmod(0o644)  # readable but not +x -> shutil.which returns None
        utils = _make_utils()
        utils._nextflow_exe = str(nf)
        utils._pipeline = str(tmp_path / "main.nf")
        utils._launch_dir = str(tmp_path)
        (tmp_path / "main.nf").write_text("")
        assert utils.is_available() is False

    def test_false_when_pipeline_unset(self, tmp_path: Path):
        utils = _make_utils()
        # Use a real binary on PATH (sh) so the first check passes
        utils._nextflow_exe = "sh"
        utils._pipeline = ""
        utils._launch_dir = str(tmp_path)
        assert utils.is_available() is False

    def test_false_when_pipeline_file_missing(self, tmp_path: Path):
        utils = _make_utils()
        utils._nextflow_exe = "sh"
        utils._pipeline = str(tmp_path / "no-such-main.nf")
        utils._launch_dir = str(tmp_path)
        assert utils.is_available() is False

    def test_false_when_launch_dir_unset(self, tmp_path: Path):
        pipeline = tmp_path / "main.nf"
        pipeline.write_text("")
        utils = _make_utils()
        utils._nextflow_exe = "sh"
        utils._pipeline = str(pipeline)
        utils._launch_dir = ""
        assert utils.is_available() is False

    def test_false_when_launch_dir_missing(self, tmp_path: Path):
        pipeline = tmp_path / "main.nf"
        pipeline.write_text("")
        utils = _make_utils()
        utils._nextflow_exe = "sh"
        utils._pipeline = str(pipeline)
        utils._launch_dir = str(tmp_path / "no-such-dir")
        assert utils.is_available() is False

    def test_true_when_all_three_present(self, tmp_path: Path):
        utils = _make_available_utils(tmp_path)
        assert utils.is_available() is True

    def test_true_when_nextflow_on_path(self, tmp_path: Path):
        """Resolving the nextflow binary via PATH (shutil.which) is enough."""
        pipeline = tmp_path / "main.nf"
        pipeline.write_text("")
        utils = _make_utils()
        utils._nextflow_exe = "sh"  # always on PATH
        utils._pipeline = str(pipeline)
        utils._launch_dir = str(tmp_path)
        assert utils.is_available() is True

    def test_warns_when_pipeline_not_under_launch_dir(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        """is_available() must WARN (not fail) when pipeline is not under launch_dir."""
        # pipeline in subdir_a; launch in subdir_b -> not under launch_dir
        subdir_a = tmp_path / "subdir_a"
        subdir_a.mkdir()
        subdir_b = tmp_path / "subdir_b"
        subdir_b.mkdir()
        pipeline = subdir_a / "main.nf"
        pipeline.write_text("// stub\n")
        utils = _make_utils()
        utils._nextflow_exe = "sh"
        utils._pipeline = str(pipeline)
        utils._launch_dir = str(subdir_b)
        import logging
        with caplog.at_level(logging.WARNING, logger="kbutillib.dram2_utils"):
            result = utils.is_available()
        # Must still return True (warning, not failure)
        assert result is True
        assert any("does not resolve under" in m for m in caplog.messages)

    def test_no_warn_when_pipeline_under_launch_dir(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        """is_available() must NOT warn when pipeline is under launch_dir."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        pipeline = subdir / "main.nf"
        pipeline.write_text("// stub\n")
        utils = _make_utils()
        utils._nextflow_exe = "sh"
        utils._pipeline = str(pipeline)
        utils._launch_dir = str(tmp_path)
        import logging
        with caplog.at_level(logging.WARNING, logger="kbutillib.dram2_utils"):
            result = utils.is_available()
        assert result is True
        assert not any("does not resolve under" in m for m in caplog.messages)


# ---------------------------------------------------------------------------
# annotate — guards and integration with the parser via a mocked _run_nextflow
# ---------------------------------------------------------------------------


class TestAnnotateGuards:
    def test_raises_tool_unavailable_when_pipeline_missing(self):
        utils = _make_utils()  # nothing configured
        with pytest.raises(ToolUnavailableError) as exc:
            utils.annotate({"p1": "MKTAYIAKQRQ"})
        assert exc.value.tool == "dram2"

    def test_raises_value_error_on_nucleotide_input(self, tmp_path: Path):
        utils = _make_available_utils(tmp_path)
        # The protein alphabet check excludes U/O/J — a U-rich RNA-looking
        # sequence is over the 10% threshold and trips the guard.  (Plain
        # ATGC strings are NOT rejected because A, C, G, T are all valid
        # amino acid codes — exactly the ambiguity ProkkaUtils' DNA guard
        # is designed to catch in the opposite direction.)
        with pytest.raises(ValueError, match="protein"):
            utils.annotate({"p1": "UUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUU"})


def _remap_tsv_query_ids(tsv_text: str, old_to_new: dict[str, str]) -> str:
    """Remap query_id values in a TSV using old_to_new mapping.

    Used by tests that need to feed the golden-fixture TSV through annotate()
    with emitted g_<n> ids instead of the original OWC_... caller ids.
    """
    lines = tsv_text.splitlines()
    if not lines:
        return tsv_text
    header = lines[0].split("\t")
    if "query_id" not in header:
        return tsv_text
    qi = header.index("query_id")
    new_lines = [lines[0]]
    for line in lines[1:]:
        if not line.strip():
            new_lines.append(line)
            continue
        parts = line.split("\t")
        if parts[qi] in old_to_new:
            parts[qi] = old_to_new[parts[qi]]
        new_lines.append("\t".join(parts))
    return "\n".join(new_lines)


class TestAnnotateWithMockedNextflow:
    """End-to-end annotate() with a mocked _run_nextflow.

    Since annotate() now uses g_<n> emitted ids, the golden TSV must be
    remapped so its query_ids match the emitted ids that _write_faa would
    produce for the caller proteins dict.
    """

    def _make_mock_with_caller_ids(self, ids: list[str]) -> Any:
        """Build a mock _run_nextflow that returns golden-fixture TSV content
        with query_ids remapped to emitted g_<n> ids matching the caller ids."""
        # Build the old->new map: each caller id -> g_<n>
        old_to_new = {cid: f"g_{n}" for n, cid in enumerate(ids, start=1)}
        original_tsv = _GOLDEN_TSV.read_text(encoding="utf-8")
        remapped_tsv = _remap_tsv_query_ids(original_tsv, old_to_new)
        return MagicMock(
            return_value=(remapped_tsv, "v2.0.0-beta17", "nextflow run ...")
        )

    def _make_mock_simple(self) -> Any:
        """Return a mock that returns an empty TSV (for tests that don't check records)."""
        return MagicMock(
            return_value=("", "v2.0.0-beta17", "nextflow run ...")
        )

    def test_returns_annotation_result(self, tmp_path: Path):
        utils = _make_available_utils(tmp_path)
        ids = _read_caller_ids_from_faa(_GOLDEN_FAA)
        proteins = {cid: "MKTAYIAKQ" * 10 for cid in ids}
        with patch.object(utils, "_run_nextflow", self._make_mock_with_caller_ids(ids)):
            result = utils.annotate(proteins)
        assert isinstance(result, AnnotationResult)
        assert result.tool == "dram2"
        assert result.tool_version == "v2.0.0-beta17"

    def test_records_keyed_by_caller_ids(self, tmp_path: Path):
        utils = _make_available_utils(tmp_path)
        ids = _read_caller_ids_from_faa(_GOLDEN_FAA)
        proteins = {cid: "MKTAYIAKQ" * 10 for cid in ids}
        with patch.object(utils, "_run_nextflow", self._make_mock_with_caller_ids(ids)):
            result = utils.annotate(proteins)
        rec_ids = {r.gene_id for r in result.records}
        # Zero-hit row absent; others present
        assert "OWC_0000_k121_3157_1" not in rec_ids
        assert "OWC_0000_k121_3157_2" in rec_ids
        assert "OWC_0000_k121_3157_3" in rec_ids

    def test_parameters_captured(self, tmp_path: Path):
        utils = _make_available_utils(tmp_path)
        proteins = {"g1": "MKTAYIAKQ" * 10}
        with patch.object(utils, "_run_nextflow", self._make_mock_simple()):
            result = utils.annotate(
                proteins, databases=["kofam", "pfam"], threads=2,
                extra_note="hello",
            )
        assert result.parameters["databases"] == ["kofam", "pfam"]
        assert result.parameters["threads"] == 2
        assert result.parameters["input_protein_count"] == 1
        assert result.parameters["extra_note"] == "hello"

    def test_parameters_include_work_dir_and_kept(self, tmp_path: Path):
        """annotate() must always include work_dir and kept in parameters."""
        utils = _make_available_utils(tmp_path)
        utils._work_root = str(tmp_path / "wroot")
        proteins = {"g1": "MKTAYIAKQ" * 10}
        with patch.object(utils, "_run_nextflow", self._make_mock_simple()):
            result = utils.annotate(proteins)
        assert "work_dir" in result.parameters
        assert "kept" in result.parameters
        assert isinstance(result.parameters["kept"], bool)

    def test_db_version_is_joined_databases(self, tmp_path: Path):
        utils = _make_available_utils(tmp_path)
        proteins = {"g1": "MKTAYIAKQ" * 10}
        with patch.object(utils, "_run_nextflow", self._make_mock_simple()):
            result = utils.annotate(proteins, databases=["kofam", "dbcan"])
        assert result.db_version == "kofam,dbcan"

    def test_db_version_none_when_databases_empty(self, tmp_path: Path):
        utils = _make_available_utils(tmp_path)
        proteins = {"g1": "MKTAYIAKQ" * 10}
        with patch.object(utils, "_run_nextflow", self._make_mock_simple()):
            result = utils.annotate(proteins, databases=[])
        assert result.db_version is None

    def test_run_id_is_uuid_hex(self, tmp_path: Path):
        utils = _make_available_utils(tmp_path)
        proteins = {"g1": "MKTAYIAKQ" * 10}
        with patch.object(utils, "_run_nextflow", self._make_mock_simple()):
            result = utils.annotate(proteins)
        import uuid
        run_uuid = uuid.UUID(hex=result.run_id)
        assert run_uuid.version == 4

    def test_keep_work_kwarg_false_deletes_scratch(self, tmp_path: Path):
        """When keep_work=False, scratch dir is deleted on success."""
        utils = _make_available_utils(tmp_path)
        work_root = tmp_path / "wroot"
        utils._work_root = str(work_root)
        proteins = {"g1": "MKTAYIAKQ" * 10}
        with patch.object(utils, "_run_nextflow", self._make_mock_simple()):
            result = utils.annotate(proteins, keep_work=False)
        # scratch dir should be gone; work_root itself stays
        scratch_dirs = list(work_root.glob("dram2_*"))
        assert scratch_dirs == []
        assert result.parameters["kept"] is False

    def test_keep_work_kwarg_true_preserves_scratch(self, tmp_path: Path):
        """When keep_work=True, scratch dir is preserved on success."""
        utils = _make_available_utils(tmp_path)
        work_root = tmp_path / "wroot"
        utils._work_root = str(work_root)
        proteins = {"g1": "MKTAYIAKQ" * 10}
        with patch.object(utils, "_run_nextflow", self._make_mock_simple()):
            result = utils.annotate(proteins, keep_work=True)
        scratch_dirs = list(work_root.glob("dram2_*"))
        assert len(scratch_dirs) == 1
        assert result.parameters["kept"] is True

    def test_keep_work_kwarg_overrides_config(self, tmp_path: Path):
        """keep_work kwarg takes precedence over dram2.keep_work config."""
        utils = _make_available_utils(tmp_path)
        work_root = tmp_path / "wroot"
        utils._work_root = str(work_root)
        utils._keep_work = True  # config says keep
        proteins = {"g1": "MKTAYIAKQ" * 10}
        with patch.object(utils, "_run_nextflow", self._make_mock_simple()):
            result = utils.annotate(proteins, keep_work=False)  # kwarg wins
        scratch_dirs = list(work_root.glob("dram2_*"))
        assert scratch_dirs == []
        assert result.parameters["kept"] is False


# ---------------------------------------------------------------------------
# _write_faa — basic functionality
# ---------------------------------------------------------------------------


class TestWriteFaa:
    def test_writes_records_with_synthetic_ids(self, tmp_path: Path):
        """Emitted headers use g_<n> synthetic ids, not the caller ids."""
        utils = _make_utils()
        path = tmp_path / "input.faa"
        result = utils._write_faa(path, {"b0001": "MKT", "b0002": "AYI"})
        text = path.read_text(encoding="utf-8")
        # Synthetic headers present
        assert ">g_1" in text
        assert ">g_2" in text
        # Caller ids NOT in headers
        assert ">b0001" not in text
        assert ">b0002" not in text
        assert "MKT" in text
        assert "AYI" in text
        # Return value maps emitted -> caller
        assert result == {"g_1": "b0001", "g_2": "b0002"}

    def test_strips_seq_whitespace(self, tmp_path: Path):
        utils = _make_utils()
        path = tmp_path / "input.faa"
        utils._write_faa(path, {"g1": "  MKTAY  "})
        text = path.read_text(encoding="utf-8")
        assert "MKTAY" in text
        assert "  MKTAY" not in text

    def test_returns_emitted_to_caller_map(self, tmp_path: Path):
        """_write_faa returns {g_1: caller_1, g_2: caller_2, ...} in order."""
        utils = _make_utils()
        path = tmp_path / "input.faa"
        proteins = {"alpha": "MKT", "beta": "AYI", "gamma": "LVP"}
        result = utils._write_faa(path, proteins)
        assert result == {"g_1": "alpha", "g_2": "beta", "g_3": "gamma"}

    def test_emitted_id_last_token_is_int(self, tmp_path: Path):
        """Emitted ids g_<n> satisfy combine_annotations int() requirement."""
        utils = _make_utils()
        path = tmp_path / "input.faa"
        proteins = {"b0001": "MKT", "b0002": "AYI"}
        utils._write_faa(path, proteins)
        headers = _read_faa_headers(path)
        for h in headers:
            last_token = h.split("_")[-1]
            int(last_token)  # must not raise ValueError


# ---------------------------------------------------------------------------
# _write_faa — prodigal-header emission
# ---------------------------------------------------------------------------


class TestWriteFaaProdigalHeaders:
    """Verify that _write_faa always emits prodigal-style headers.

    DRAM2 parses the ``start_position``, ``stop_position``, and
    ``strandedness`` columns from the FASTA header in the prodigal format::

        >{emitted_id} # {start} # {stop} # {strand} #

    These tests exercise the synthetic-coords path (no gene_coords), the
    real-coords path, and strand normalisation — all offline, no Nextflow.

    Note: header id token is now the synthetic g_<n>, NOT the caller id.
    """

    def test_synthetic_coords_when_no_gene_coords(self, tmp_path: Path):
        """When gene_coords is absent, header uses start=1, stop=3*len, strand=1."""
        utils = _make_utils()
        path = tmp_path / "input.faa"
        seq = "MKTAY"  # len=5 -> stop=15
        utils._write_faa(path, {"prot1": seq})
        lines = path.read_text(encoding="utf-8").splitlines()
        header = lines[0]
        # g_1 is emitted id; coords from synthetic fallback
        assert header == ">g_1 # 1 # 15 # 1 #"
        assert lines[1] == seq

    def test_synthetic_coords_stop_is_three_times_len(self, tmp_path: Path):
        """stop = 3 * len(seq) for each sequence under synthetic coords."""
        utils = _make_utils()
        path = tmp_path / "input.faa"
        utils._write_faa(path, {"short": "MK", "longer": "MKTAYIAKQR"})
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        # g_1=short: len=2 -> stop=6; g_2=longer: len=10 -> stop=30
        g1_header = next(l for l in lines if l.startswith(">g_1"))
        g2_header = next(l for l in lines if l.startswith(">g_2"))
        assert g1_header == ">g_1 # 1 # 6 # 1 #"
        assert g2_header == ">g_2 # 1 # 30 # 1 #"

    def test_real_coords_used_when_gene_coords_provided(self, tmp_path: Path):
        """When gene_coords contains the CALLER id, real coords are used."""
        utils = _make_utils()
        path = tmp_path / "input.faa"
        # gene_coords keyed by caller id b0001, not emitted id g_1
        gene_coords = {"b0001": (100, 400, 1)}
        utils._write_faa(path, {"b0001": "MKTAY"}, gene_coords=gene_coords)
        lines = path.read_text(encoding="utf-8").splitlines()
        assert lines[0] == ">g_1 # 100 # 400 # 1 #"

    def test_strand_positive_normalised_to_1(self, tmp_path: Path):
        """Any positive strand value (e.g. +1 or +2) is normalised to 1."""
        utils = _make_utils()
        path = tmp_path / "input.faa"
        gene_coords = {"prot": (1, 100, 2)}  # strand=2 -> should normalise to 1
        utils._write_faa(path, {"prot": "MKTAY"}, gene_coords=gene_coords)
        lines = path.read_text(encoding="utf-8").splitlines()
        assert lines[0] == ">g_1 # 1 # 100 # 1 #"

    def test_strand_negative_normalised_to_minus_1(self, tmp_path: Path):
        """Negative strand raw values are normalised to -1."""
        utils = _make_utils()
        path = tmp_path / "input.faa"
        gene_coords = {"prot": (200, 500, -1)}
        utils._write_faa(path, {"prot": "MKTAY"}, gene_coords=gene_coords)
        lines = path.read_text(encoding="utf-8").splitlines()
        assert lines[0] == ">g_1 # 200 # 500 # -1 #"

    def test_mixed_coords_and_synthetic(self, tmp_path: Path):
        """When gene_coords only covers some caller ids, the rest get synthetic."""
        utils = _make_utils()
        path = tmp_path / "input.faa"
        gene_coords = {"prot1": (50, 200, -1)}  # prot2 not in gene_coords
        utils._write_faa(
            path, {"prot1": "MKTAY", "prot2": "AYI"}, gene_coords=gene_coords
        )
        lines = path.read_text(encoding="utf-8").splitlines()
        g1_header = next(l for l in lines if l.startswith(">g_1"))
        g2_header = next(l for l in lines if l.startswith(">g_2"))
        assert g1_header == ">g_1 # 50 # 200 # -1 #"
        assert g2_header == ">g_2 # 1 # 9 # 1 #"  # len("AYI")=3 -> stop=9

    def test_emitted_id_is_first_whitespace_token(self, tmp_path: Path):
        """The emitted g_<n> id is the first token in the header (before the first space).

        DRAM2 reads ``query_id`` from the first whitespace-delimited token of
        the FASTA header.  This test verifies the token after ``>`` and before
        the first space is exactly the synthetic g_<n> id.
        """
        utils = _make_utils()
        path = tmp_path / "input.faa"
        utils._write_faa(path, {"myprotein_001": "MKT"})
        header = path.read_text(encoding="utf-8").splitlines()[0]
        # Strip leading '>'
        first_token = header[1:].split()[0]
        # Synthetic id g_1, not the caller id myprotein_001
        assert first_token == "g_1"

    def test_header_format_matches_prodigal_pattern(self, tmp_path: Path):
        """Header must match the prodigal pattern: >{emitted_id} # {n} # {n} # {s} #"""
        utils = _make_utils()
        path = tmp_path / "input.faa"
        utils._write_faa(
            path, {"gene_1": "MKTAYIAK"}, gene_coords={"gene_1": (1, 300, -1)}
        )
        header = path.read_text(encoding="utf-8").splitlines()[0]
        pattern = re.compile(r"^>(\S+) # (\d+) # (\d+) # (-?1) #$")
        m = pattern.match(header)
        assert m is not None, f"header {header!r} does not match prodigal pattern"
        # Emitted id is g_1, not the caller id
        assert m.group(1) == "g_1"
        assert m.group(4) == "-1"


# ---------------------------------------------------------------------------
# b0001 round-trip acceptance test (Acceptance Criterion 1, Decision 2-4)
# ---------------------------------------------------------------------------


class TestB0001RoundTrip:
    """Offline acceptance test for the b0001/b0002 id round-trip.

    This pins the core contract: proteins with non-prodigal ids (like
    store locus_ids b0001, b0002) emit FASTA headers with numeric-final-
    token synthetic ids (g_1, g_2), and the parsed AnnotationResult carries
    the original caller ids as gene_id.
    """

    def test_write_faa_emits_numeric_final_token(self, tmp_path: Path):
        """Emitted ids have split('_')[-1] parseable as int (combines_annotations)."""
        utils = _make_utils()
        path = tmp_path / "input.faa"
        proteins = {"b0001": "MKTAYIAKQRQ" * 5, "b0002": "MAAQAAKLT" * 5}
        result = utils._write_faa(path, proteins)
        headers = _read_faa_headers(path)
        assert len(headers) == 2
        for h in headers:
            last = h.split("_")[-1]
            assert int(last) >= 1, f"header {h!r} final token not an int"
        # Map covers both inputs
        assert set(result.values()) == {"b0001", "b0002"}

    def test_parse_tsv_translates_back_to_b_ids(self, tmp_path: Path):
        """Parser with emitted_to_caller map yields gene_id == original b-id."""
        # Build emitted_to_caller as _write_faa would
        emap = {"g_1": "b0001", "g_2": "b0002"}
        # Synthetic TSV using emitted ids as query_id
        tsv = (
            "query_id\tkofam_id\tkofam_description\n"
            "g_1\tK00001\thyperABC transporter\n"
            "g_2\tK00002\tsome other enzyme\n"
        )
        records = _parse_annotations_tsv(tsv, emap)
        gene_ids = [r.gene_id for r in records]
        assert "b0001" in gene_ids
        assert "b0002" in gene_ids
        # No synthetic g_<n> ids in output
        assert "g_1" not in gene_ids
        assert "g_2" not in gene_ids

    def test_full_roundtrip_write_then_parse(self, tmp_path: Path):
        """End-to-end offline round-trip: write FAA -> build TSV -> parse."""
        utils = _make_utils()
        path = tmp_path / "input.faa"
        proteins = {"b0001": "MKTAYIAKQRQ" * 5, "b0002": "MAAQAAKLT" * 5}
        emap = utils._write_faa(path, proteins)

        # Validate emitted header ids from the written file
        headers = _read_faa_headers(path)
        assert headers == ["g_1", "g_2"]

        # Build a synthetic TSV using the emitted ids
        tsv = (
            "query_id\tkofam_id\tkofam_description\n"
            f"{headers[0]}\tK00001\thyperABC transporter\n"
            f"{headers[1]}\tK00002\tsome other enzyme\n"
        )
        records = _parse_annotations_tsv(tsv, emap)

        # Parsed records carry the CALLER (b-style) ids
        assert len(records) == 2
        gene_ids = [r.gene_id for r in records]
        assert gene_ids == ["b0001", "b0002"]

        # Records appear in proteins insertion order
        assert records[0].gene_id == "b0001"
        assert records[1].gene_id == "b0002"

        # Terms have KO namespace
        ko_ids = {t.id for r in records for t in r.terms if t.namespace == "KO"}
        assert "K00001" in ko_ids
        assert "K00002" in ko_ids


# ---------------------------------------------------------------------------
# Env builder tests (Acceptance Criterion 6-7)
# ---------------------------------------------------------------------------


class TestBuildSubprocessEnv:
    """Tests for _build_subprocess_env NXF_VER and PATH behavior."""

    def test_nxf_ver_set_to_default(self):
        utils = _make_utils()
        env = utils._build_subprocess_env()
        assert env["NXF_VER"] == _DEFAULT_NXF_VER

    def test_nxf_ver_uses_configured_value(self):
        utils = _make_utils()
        utils._nxf_ver = "23.04.0"
        env = utils._build_subprocess_env()
        assert env["NXF_VER"] == "23.04.0"

    def test_env_path_empty_does_not_modify_path(self):
        """When env_path is empty, PATH is inherited unchanged."""
        utils = _make_utils()
        utils._env_path = ""
        original_path = os.environ.get("PATH", "")
        env = utils._build_subprocess_env()
        assert env.get("PATH") == original_path

    def test_env_path_prepended_to_path(self, monkeypatch: pytest.MonkeyPatch):
        """When env_path is set, it is prepended to PATH."""
        original_path = "/usr/bin:/bin"
        monkeypatch.setenv("PATH", original_path)
        utils = _make_utils()
        utils._env_path = "/opt/nf/bin:/opt/micromamba/bin"
        env = utils._build_subprocess_env()
        expected = "/opt/nf/bin:/opt/micromamba/bin" + os.pathsep + original_path
        assert env["PATH"] == expected
        # Original PATH still at the END
        assert env["PATH"].endswith(original_path)

    def test_subprocess_run_receives_env_with_nxf_ver(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """subprocess.run must be called with env containing NXF_VER."""
        utils = _make_available_utils(tmp_path)
        utils._nxf_ver = "24.10.5"
        utils._env_path = "/fake/bin"
        utils._work_root = str(tmp_path / "wroot")

        captured_env: dict[str, str] = {}

        def mock_run(argv, *, cwd, capture_output, text, check, env):
            nonlocal captured_env
            captured_env = dict(env)
            mock_r = MagicMock()
            mock_r.returncode = 0
            mock_r.stdout = ""
            mock_r.stderr = ""
            return mock_r

        with patch("kbutillib.dram2_utils.subprocess.run", side_effect=mock_run):
            proteins = {"b0001": "MKTAYIAKQRQ" * 5}
            utils.annotate(proteins)

        assert captured_env.get("NXF_VER") == "24.10.5"
        assert captured_env.get("PATH", "").startswith("/fake/bin")

    def test_run_nextflow_passes_env_to_subprocess(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """_run_nextflow must pass the built env to subprocess.run."""
        utils = _make_available_utils(tmp_path)
        utils._nxf_ver = "24.10.5"
        utils._env_path = "/opt/dram/bin:/opt/micromamba/bin"
        genes = tmp_path / "genes"; genes.mkdir()
        out = tmp_path / "out"; out.mkdir()
        work = tmp_path / "work"; work.mkdir()
        # Stage output file so no FileNotFoundError
        raw = out / "RAW"; raw.mkdir()
        (raw / "raw-annotations.tsv").write_text("")

        captured_env: dict[str, str] = {}

        def mock_run(argv, **kwargs):
            nonlocal captured_env
            captured_env = dict(kwargs.get("env") or {})
            mr = MagicMock()
            mr.returncode = 0
            mr.stdout = ""
            mr.stderr = ""
            return mr

        with patch("kbutillib.dram2_utils.subprocess.run", side_effect=mock_run):
            utils._run_nextflow(
                genes_dir=genes, outdir=out, workdir=work,
                databases=("kofam",), threads=1,
            )
        assert captured_env.get("NXF_VER") == "24.10.5"
        assert captured_env.get("PATH", "").startswith("/opt/dram/bin")


# ---------------------------------------------------------------------------
# Keep-on-failure tests (Acceptance Criteria 8-11, Decision 8)
# ---------------------------------------------------------------------------


class TestKeepOnFailure:
    """Tests for failure preservation: scratch dir retained, failed-<run_id>/
    created, pipeline_info/ and .nextflow.log copied when present."""

    def _stage_available_utils(self, tmp_path: Path) -> tuple[DRAM2Utils, Path]:
        """Return (utils, work_root) with a real main.nf under launch_dir."""
        launch = tmp_path / "launch"
        launch.mkdir()
        pipeline = launch / "main.nf"
        pipeline.write_text("// stub\n")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        nf = bin_dir / "nextflow"
        nf.write_text("#!/usr/bin/env bash\nexit 0\n")
        nf.chmod(0o755)
        work_root = tmp_path / "wroot"
        utils = _make_utils()
        utils._nextflow_exe = str(nf)
        utils._pipeline = str(pipeline)
        utils._launch_dir = str(launch)
        utils._work_root = str(work_root)
        return utils, work_root

    def test_scratch_under_work_root_not_tmp(self, tmp_path: Path):
        """Scratch dir must be created under work_root, never under /tmp."""
        utils, work_root = self._stage_available_utils(tmp_path)
        proteins = {"b0001": "MKTAYIAKQRQ" * 5}

        def mock_run(argv, **kwargs):
            mr = MagicMock()
            mr.returncode = 1  # non-zero -> failure
            mr.stdout = ""
            mr.stderr = "boom"
            return mr

        with patch("kbutillib.dram2_utils.subprocess.run", side_effect=mock_run):
            with pytest.raises(subprocess.CalledProcessError):
                utils.annotate(proteins)

        # Scratch dir must exist under work_root
        scratch_dirs = list(work_root.glob("dram2_*"))
        assert len(scratch_dirs) >= 1
        for d in scratch_dirs:
            assert not str(d).startswith("/tmp"), f"scratch under /tmp: {d}"

    def test_failed_dir_created_on_nonzero_exit(self, tmp_path: Path):
        """failed-<run_id>/ is created under work_root on non-zero Nextflow exit."""
        utils, work_root = self._stage_available_utils(tmp_path)
        proteins = {"b0001": "MKTAYIAKQRQ" * 5}

        def mock_run(argv, **kwargs):
            mr = MagicMock()
            mr.returncode = 1
            mr.stdout = ""
            mr.stderr = "pipeline failed"
            return mr

        with patch("kbutillib.dram2_utils.subprocess.run", side_effect=mock_run):
            with pytest.raises(subprocess.CalledProcessError):
                utils.annotate(proteins)

        failed_dirs = list(work_root.glob("failed-*"))
        assert len(failed_dirs) == 1

    def test_pipeline_info_copied_when_present(self, tmp_path: Path):
        """pipeline_info/ from scratch/out/ is copied to failed-<run_id>/."""
        utils, work_root = self._stage_available_utils(tmp_path)
        proteins = {"b0001": "MKTAYIAKQRQ" * 5}

        def mock_run(argv, **kwargs):
            # Find the scratch dir (just created) and stage pipeline_info/
            scratch_dirs = list(work_root.glob("dram2_*"))
            if scratch_dirs:
                out_dir = scratch_dirs[0] / "out"
                out_dir.mkdir(parents=True, exist_ok=True)
                pi = out_dir / "pipeline_info"
                pi.mkdir()
                (pi / "execution_trace.txt").write_text("some trace\n")
            mr = MagicMock()
            mr.returncode = 1
            mr.stdout = ""
            mr.stderr = "fail"
            return mr

        with patch("kbutillib.dram2_utils.subprocess.run", side_effect=mock_run):
            with pytest.raises(subprocess.CalledProcessError):
                utils.annotate(proteins)

        failed_dirs = list(work_root.glob("failed-*"))
        assert len(failed_dirs) == 1
        copied_pi = failed_dirs[0] / "pipeline_info"
        assert copied_pi.is_dir()
        assert (copied_pi / "execution_trace.txt").exists()

    def test_nextflow_log_copied_when_present(self, tmp_path: Path):
        """.nextflow.log from launch_dir is copied to failed-<run_id>/nextflow.log."""
        utils, work_root = self._stage_available_utils(tmp_path)
        # Stage .nextflow.log in launch_dir
        nxf_log = Path(utils._launch_dir) / ".nextflow.log"
        nxf_log.write_text("nextflow log content\n")
        proteins = {"b0001": "MKTAYIAKQRQ" * 5}

        def mock_run(argv, **kwargs):
            mr = MagicMock()
            mr.returncode = 1
            mr.stdout = ""
            mr.stderr = "fail"
            return mr

        with patch("kbutillib.dram2_utils.subprocess.run", side_effect=mock_run):
            with pytest.raises(subprocess.CalledProcessError):
                utils.annotate(proteins)

        failed_dirs = list(work_root.glob("failed-*"))
        assert len(failed_dirs) == 1
        assert (failed_dirs[0] / "nextflow.log").exists()
        assert "nextflow log content" in (failed_dirs[0] / "nextflow.log").read_text()

    def test_missing_pipeline_info_skipped_without_raising(self, tmp_path: Path):
        """Absence of pipeline_info/ does not cause a secondary exception."""
        utils, work_root = self._stage_available_utils(tmp_path)
        proteins = {"b0001": "MKTAYIAKQRQ" * 5}

        def mock_run(argv, **kwargs):
            mr = MagicMock()
            mr.returncode = 1
            mr.stdout = ""
            mr.stderr = "fail"
            return mr

        # No pipeline_info/ staged; must not raise beyond CalledProcessError
        with patch("kbutillib.dram2_utils.subprocess.run", side_effect=mock_run):
            with pytest.raises(subprocess.CalledProcessError):
                utils.annotate(proteins)
        # failed-* dir still created
        assert len(list(work_root.glob("failed-*"))) == 1

    def test_missing_nextflow_log_skipped_without_raising(self, tmp_path: Path):
        """Absence of .nextflow.log does not cause a secondary exception."""
        utils, work_root = self._stage_available_utils(tmp_path)
        # Explicitly ensure no .nextflow.log in launch_dir
        nxf_log = Path(utils._launch_dir) / ".nextflow.log"
        assert not nxf_log.exists()
        proteins = {"b0001": "MKTAYIAKQRQ" * 5}

        def mock_run(argv, **kwargs):
            mr = MagicMock()
            mr.returncode = 1
            mr.stdout = ""
            mr.stderr = "fail"
            return mr

        with patch("kbutillib.dram2_utils.subprocess.run", side_effect=mock_run):
            with pytest.raises(subprocess.CalledProcessError):
                utils.annotate(proteins)
        # No nextflow.log in failed dir
        failed_dirs = list(work_root.glob("failed-*"))
        assert len(failed_dirs) == 1
        assert not (failed_dirs[0] / "nextflow.log").exists()

    def test_scratch_preserved_on_failure(self, tmp_path: Path):
        """Full scratch dir (not just failed-*) is preserved after failure."""
        utils, work_root = self._stage_available_utils(tmp_path)
        proteins = {"b0001": "MKTAYIAKQRQ" * 5}

        def mock_run(argv, **kwargs):
            mr = MagicMock()
            mr.returncode = 1
            mr.stdout = ""
            mr.stderr = "fail"
            return mr

        with patch("kbutillib.dram2_utils.subprocess.run", side_effect=mock_run):
            with pytest.raises(subprocess.CalledProcessError):
                utils.annotate(proteins)

        scratch_dirs = list(work_root.glob("dram2_*"))
        assert len(scratch_dirs) >= 1


# ---------------------------------------------------------------------------
# _write_faa
# ---------------------------------------------------------------------------


class TestWriteFaaOld:
    """Retain original _write_faa tests where still valid.

    The old tests used caller ids as header tokens; update them for the
    new synthetic-id behaviour.
    """

    def test_writes_records(self, tmp_path: Path):
        utils = _make_utils()
        path = tmp_path / "input.faa"
        utils._write_faa(path, {"b001": "MKT", "b002": "AYI"})
        text = path.read_text(encoding="utf-8")
        # Synthetic ids present
        assert ">g_1" in text
        assert ">g_2" in text
        assert "MKT" in text
        assert "AYI" in text


# ---------------------------------------------------------------------------
# _DEFAULT_DATABASES — metabolic set, no pfam (offline)
# ---------------------------------------------------------------------------


class TestDefaultDatabases:
    """Verify _DEFAULT_DATABASES contains the expected metabolic databases.

    pfam is intentionally excluded from the default set for the inner-loop
    context; this test pins that decision so it cannot silently revert.
    """

    def test_default_databases_equals_metabolic_set(self):
        """_DEFAULT_DATABASES must be exactly (kofam, dbcan, merops, vog)."""
        assert _DEFAULT_DATABASES == ("kofam", "dbcan", "merops", "vog")

    def test_pfam_absent_from_default_databases(self):
        """pfam must not be in the default database set."""
        assert "pfam" not in _DEFAULT_DATABASES

    def test_default_databases_is_tuple(self):
        """_DEFAULT_DATABASES must be a tuple (immutable constant)."""
        assert isinstance(_DEFAULT_DATABASES, tuple)

    def test_annotate_uses_default_databases_when_none_passed(
        self, tmp_path: Path
    ):
        """annotate() with databases=None must record _DEFAULT_DATABASES in
        result.parameters and result.db_version."""
        utils = _make_available_utils(tmp_path)
        mock_run = MagicMock(
            return_value=("", "v2.0.0-beta17", "nextflow run ...")
        )
        with patch.object(utils, "_run_nextflow", mock_run):
            result = utils.annotate({"g1": "MKTAYIAKQ" * 10})
        assert result.parameters["databases"] == list(_DEFAULT_DATABASES)
        assert result.db_version == ",".join(_DEFAULT_DATABASES)


# ---------------------------------------------------------------------------
# _build_nextflow_command — argv shape pinned against the live h100 install
# ---------------------------------------------------------------------------


class TestBuildNextflowCommand:
    def test_pinned_invocation(self, tmp_path: Path):
        utils = _make_available_utils(tmp_path)
        genes = tmp_path / "genes"
        out = tmp_path / "out"
        work = tmp_path / "work"
        genes.mkdir(); out.mkdir(); work.mkdir()
        cmd = utils._build_nextflow_command(
            genes_dir=genes,
            outdir=out,
            workdir=work,
            databases=("kofam", "dbcan"),
            threads=4,
        )
        assert cmd[0] == utils._nextflow_exe
        assert cmd[1] == "run"
        assert cmd[2] == utils._pipeline
        assert "-profile" in cmd and cmd[cmd.index("-profile") + 1] == "conda"
        assert "--annotate" in cmd
        i = cmd.index("--input_genes")
        assert cmd[i + 1] == str(genes)
        o = cmd.index("--outdir")
        assert cmd[o + 1] == str(out)
        w = cmd.index("-work-dir")
        assert cmd[w + 1] == str(work)
        t = cmd.index("--threads")
        assert cmd[t + 1] == "4"
        assert "--use_kofam" in cmd
        assert "--use_dbcan" in cmd

    def test_extra_config_appended(self, tmp_path: Path):
        utils = _make_available_utils(tmp_path)
        cmd = utils._build_nextflow_command(
            genes_dir=tmp_path, outdir=tmp_path, workdir=tmp_path,
            databases=(), threads=1,
            effective_config="/tmp/extra.config",
        )
        assert "-c" in cmd
        ci = cmd.index("-c")
        assert cmd[ci + 1] == "/tmp/extra.config"


# ---------------------------------------------------------------------------
# _run_nextflow — subprocess mocking covers success + non-zero exit
# ---------------------------------------------------------------------------


class TestRunNextflow:
    def test_success_returns_tsv_text(self, tmp_path: Path):
        utils = _make_available_utils(tmp_path)
        genes = tmp_path / "genes"
        out = tmp_path / "out"
        work = tmp_path / "work"
        genes.mkdir(); out.mkdir(); work.mkdir()
        # Pretend Nextflow wrote raw-annotations.tsv
        raw_dir = out / "RAW"
        raw_dir.mkdir()
        (raw_dir / "raw-annotations.tsv").write_text("query_id\nfoo\n")

        with patch("kbutillib.dram2_utils.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="N E X T F L O W  ~  version 24.10.5\n",
                stderr="",
            )
            tsv, ver, cmd = utils._run_nextflow(
                genes_dir=genes, outdir=out, workdir=work,
                databases=("kofam",), threads=1,
            )
        assert "query_id" in tsv
        assert ver == "24.10.5"
        assert "nextflow" in cmd or utils._nextflow_exe in cmd

    def test_missing_output_file_yields_empty_tsv(self, tmp_path: Path):
        utils = _make_available_utils(tmp_path)
        genes = tmp_path / "genes"
        out = tmp_path / "out"
        work = tmp_path / "work"
        genes.mkdir(); out.mkdir(); work.mkdir()

        with patch("kbutillib.dram2_utils.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            tsv, ver, _ = utils._run_nextflow(
                genes_dir=genes, outdir=out, workdir=work,
                databases=(), threads=1,
            )
        assert tsv == ""
        assert ver is None

    def test_nonzero_exit_raises(self, tmp_path: Path):
        utils = _make_available_utils(tmp_path)
        genes = tmp_path / "genes"
        out = tmp_path / "out"
        work = tmp_path / "work"
        genes.mkdir(); out.mkdir(); work.mkdir()

        with patch("kbutillib.dram2_utils.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="boom"
            )
            with pytest.raises(subprocess.CalledProcessError):
                utils._run_nextflow(
                    genes_dir=genes, outdir=out, workdir=work,
                    databases=(), threads=1,
                )


# ---------------------------------------------------------------------------
# Export verification
# ---------------------------------------------------------------------------


class TestDram2Exports:
    def test_dram2_utils_exported(self):
        import kbutillib
        assert hasattr(kbutillib, "DRAM2Utils")
        assert kbutillib.DRAM2Utils is not None

    def test_dram2_utils_is_correct_class(self):
        import kbutillib
        from kbutillib.dram2_utils import DRAM2Utils as DU
        assert kbutillib.DRAM2Utils is DU


# ---------------------------------------------------------------------------
# Live integration test (h100-only)
# ---------------------------------------------------------------------------


def _dram2_live_available() -> bool:
    if os.environ.get("KBU_DRAM2_LIVE") != "1":
        return False
    try:
        utils = DRAM2Utils(
            config_file=False, token_file=None, kbase_token_file=None,
        )
        return utils.is_available()
    except Exception:
        return False


@pytest.mark.integration
@pytest.mark.skipif(
    not _dram2_live_available(),
    reason="DRAM2 live integration requires KBU_DRAM2_LIVE=1 and a reachable install",
)
class TestDram2LiveIntegration:
    """Live integration — runs only on h100 with KBU_DRAM2_LIVE=1."""

    def test_annotate_real_proteins(self):
        # A small real proteome — the FASTA from the golden fixture
        proteins: dict[str, str] = {}
        for record in _GOLDEN_FAA.read_text(encoding="utf-8").split(">"):
            record = record.strip()
            if not record:
                continue
            header, *seq_lines = record.splitlines()
            proteins[header.strip()] = "".join(seq_lines).strip()

        utils = DRAM2Utils(
            config_file=False, token_file=None, kbase_token_file=None,
        )
        result = utils.annotate(proteins, databases=["kofam"], threads=1)
        assert isinstance(result, AnnotationResult)
        assert result.tool == "dram2"
        # Even with zero hits, the structure must be intact
        for rec in result.records:
            assert rec.gene_id in proteins
