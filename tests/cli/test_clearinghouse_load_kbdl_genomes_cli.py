"""``kbu clearinghouse load-kbdl-genomes`` (lw-9). Drives the real click CLI.

The lake is lw-1's in-memory ``_MiniLake`` behind a real
``ClearinghouseCapability`` injected through ``_capability``; the genome and
annotation inputs are the lw-9 fixtures (see
tests/berdl/fixtures/kbdl_genome_loader/PROVENANCE.md).
"""

from __future__ import annotations

import gzip
import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

import kbutillib.interfaces.cli.clearinghouse as ch
from kbutillib.cli import main
from kbutillib.domains.kbase.berdl import clearinghouse_ingest as ci
from kbutillib.domains.kbase.berdl.clearinghouse_capability import ClearinghouseCapability

from tests.berdl.test_clearinghouse_links import _MiniLake

FIX = Path(__file__).resolve().parent.parent / "berdl" / "fixtures" / "kbdl_genome_loader"
OBJECT_ID = "aa50dcedd9471457172a79ffecfdd007cc1599972c25e133e1404421b96b3b6a"
_KEYS = {"schema_version", "verb", "generated_at", "locus", "data", "warnings"}


@pytest.fixture()
def inputs(tmp_path, monkeypatch):
    monkeypatch.setenv(ci.STATE_DIR_ENV, str(tmp_path / "state"))
    monkeypatch.setenv(ci.LOCK_DIR_ENV, str(tmp_path / "locks"))
    objects = tmp_path / "objects"
    objects.mkdir()
    shutil.copy(FIX / "adp1_genome_slice.json", objects / f"{OBJECT_ID}.json")
    archive = tmp_path / "archive"
    archive.mkdir()
    with gzip.open(archive / "f072e65c6f784629852a922b3bad4d75.json.gz", "wt") as fh:
        fh.write((FIX / "adp1_annotation_slice.json").read_text())
    external = tmp_path / "external.json"
    external.write_text(json.dumps({OBJECT_ID: {"ncbi_assembly": "GCF_000046845.1", "gene_call_tool": "SOURCE_GFF"}}))
    ids = tmp_path / "ids.txt"
    ids.write_text(f"# the urgent set\n{OBJECT_ID}  # ADP1\n\n")
    return {
        "args": [
            "clearinghouse", "load-kbdl-genomes", "--from-file", str(ids), "--external-ids", str(external),
            "--term-map", str(FIX / "term_function_map.tsv"), "--object-dir", str(objects),
            "--archive-dir", str(archive),
        ],
        "tmp": tmp_path,
    }


class _InPod(ClearinghouseCapability):
    pass


def _run(args, **kw):
    return CliRunner().invoke(main, args, **kw)


def test_dry_run_json_envelope_and_counts_without_any_capability(inputs, monkeypatch):
    monkeypatch.setattr(ch, "_capability", lambda: pytest.fail("a dry run must not construct a capability"))
    result = _run([*inputs["args"], "--dry-run", "--json"])
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.stdout)
    assert set(envelope) == _KEYS and envelope["verb"] == "load-kbdl-genomes"
    data = envelope["data"]
    assert data["dry_run"] is True and data["genomes_loaded"] == 1
    assert data["rows_per_table"]["gene_x_genome"] == 3 and data["rows_per_table"]["protein_x_function"] == 33
    assert data["written"] == {}
    assert data["genomes"][0]["annotation_results_used"] == ["f072e65c6f784629852a922b3bad4d75.json.gz"]


def test_dry_run_human_output(inputs):
    result = _run([*inputs["args"], "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "rows protein_x_function: 33" in result.output
    assert "skip kofamscan_function_ignored: 2" in result.output


def test_a_real_run_writes_and_a_rerun_writes_zero_rows(inputs, monkeypatch):
    lake = _MiniLake()
    monkeypatch.setattr(ch, "_capability", lambda: ClearinghouseCapability(capability=lake))
    first = _run([*inputs["args"], "--json"])
    assert first.exit_code == 0, first.output
    data = json.loads(first.stdout)["data"]
    assert data["written"]["protein_x_function"] == 33 and data["written"]["gene_x_genome"] == 3
    loads = len(lake.load_calls)
    second = _run([*inputs["args"], "--json"])
    assert second.exit_code == 0, second.output
    again = json.loads(second.stdout)["data"]
    assert sum(again["written"].values()) == 0 and len(lake.load_calls) == loads


def test_a_real_run_off_pod_is_refused_naming_the_locus_before_anything_is_written(inputs, monkeypatch):
    lake = _MiniLake()
    lake.locus = lambda: "off_pod"
    monkeypatch.setattr(ch, "_capability", lambda: ClearinghouseCapability(capability=lake))
    result = _run(inputs["args"])
    assert result.exit_code != 0
    assert "IN-POD ONLY" in result.output and "off_pod" in result.output
    assert lake.load_calls == []


def test_usage_errors(inputs):
    assert _run(["clearinghouse", "load-kbdl-genomes", "--dry-run"]).exit_code == 2
    no_map = [a for a in inputs["args"] if a != "--term-map" and a != str(FIX / "term_function_map.tsv")]
    result = _run([*no_map, "--dry-run"])
    assert result.exit_code == 2 and "term_function_map is required" in result.output


def test_an_unloadable_genome_exits_non_zero_and_is_reported(inputs):
    result = _run(["clearinghouse", "load-kbdl-genomes", "--object-id", "f" * 64, "--term-map",
                   str(FIX / "term_function_map.tsv"), "--object-dir", str(inputs["tmp"] / "nowhere"),
                   "--archive-dir", str(inputs["tmp"]), "--dry-run", "--json"])
    assert result.exit_code == 1
    assert "f" * 64 in json.loads(result.stdout)["data"]["genomes_failed"]


def test_the_verb_is_registered_on_the_group():
    assert "load-kbdl-genomes" in ch.clearinghouse_cmd.commands
