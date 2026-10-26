"""Offline tests for the adopted KBDL sweeper and locus_prefix."""
import argparse
import gzip
import json

from kbutillib.kbdl_index import sweep
from kbutillib.kbdl_index.subject import locus_prefix


class FakeKBDL:
    calls = {"list": 0, "get": 0}

    def list_jobs(self):
        FakeKBDL.calls["list"] += 1
        return [{"job_id": "j1", "job_type": "KBDLGenomeAnnotation",
                 "state": "completed", "submitted_at": "2026-10-01T00:00:00Z"}]

    def get_job_result(self, jid):
        FakeKBDL.calls["get"] += 1
        return {"ABC_0001": {}, "ABC_0002": {}}


def _args():
    return argparse.Namespace(types=list(sweep.DEFAULT_TYPES), dry_run=False,
                              max=0, no_mirror=True, loop=0)


def test_second_run_sweeps_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(sweep, "ROOT", str(tmp_path / "results"))
    monkeypatch.setattr(sweep, "LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setattr(sweep, "KBDLServiceUtils", FakeKBDL)
    FakeKBDL.calls.update(list=0, get=0)

    sweep.sweep_once(_args())
    assert FakeKBDL.calls["get"] == 1
    dest = tmp_path / "results" / "KBDLGenomeAnnotation" / "j1.json.gz"
    assert json.load(gzip.open(dest, "rt")) == {"ABC_0001": {}, "ABC_0002": {}}
    lines = (tmp_path / "ledger.jsonl").read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["locus_prefix"] == "ABC"

    sweep.sweep_once(_args())
    assert FakeKBDL.calls["get"] == 1          # nothing fetched again
    assert FakeKBDL.calls["list"] == 2         # no-op run costs one list_jobs
    assert len((tmp_path / "ledger.jsonl").read_text().splitlines()) == 1


def test_locus_prefix_shared():
    assert locus_prefix({"XYZ_00001": 1, "XYZ_00002": 2, "XYZ_00003": 3}) == "XYZ"


def test_locus_prefix_none():
    assert locus_prefix({"alpha": 1, "beta": 2}) is None
    assert locus_prefix(["not", "a", "dict"]) is None


def test_single_definition():
    assert sweep.locus_prefix is locus_prefix
