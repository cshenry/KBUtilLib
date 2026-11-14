"""``alcf_bench_report.load_job`` and ``main`` end to end against a REAL SqliteStore.

The synthetic-verdict tests never run the store reader; this does, built with the
library's own write APIs (create_job, add_calls, settle_item, record_attempt, set_halt).
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

pytest.importorskip("sqlalchemy")

from kbutillib.domains.ai.council.store.sqlite import SqliteStore  # noqa: E402

from .store_contract import call_row, item_row, job_row  # noqa: E402

_SCRIPT = Path(__file__).resolve().parents[4] / "scripts/council/alcf_bench_report.py"
_spec = importlib.util.spec_from_file_location("alcf_bench_report_store", _SCRIPT)
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)  # type: ignore[union-attr]

NOW = datetime(2026, 11, 14, tzinfo=timezone.utc)


def _verdict(item_id: str, picked: List[str]) -> Dict[str, Any]:
    return {"status": "ok", "item_id": item_id, "selected_ids": picked,
            "final_confidence": "high", "decided_by": "unanimous"}


def _make(path: Path, picks: Dict[str, List[str]], *, seats=("p1", "p2"),
          endpoint="argo", entity="a" * 64, query=("go_mf", "4")) -> tuple:
    url = f"sqlite:///{path}"
    store = SqliteStore(url)
    store.init_schema()
    items = [item_row(i, entity_hash=entity, data_hash="b" * 64) for i in picks]
    calls = [call_row(i, seat, endpoint=endpoint, entity_hash=entity)
             for i in picks for seat in seats]
    job_id = store.create_job(
        job_row(query_name=query[0], query_version=query[1]), items, calls)
    for i, sel in picks.items():
        store.settle_item(job_id, i, "ok", _verdict(i, sel))
    return store, url, job_id


def test_load_job_and_main_against_real_stores(tmp_path, capsys):
    bstore, burl, bjob = _make(tmp_path / "base.sqlite",
                               {"i1": ["GO:1"], "i2": [], "i3": ["GO:3"], "i4": ["GO:4"]})
    bstore.close()
    cstore, curl, cjob = _make(tmp_path / "cand.sqlite",
                               {"i1": ["GO:1"], "i2": ["GO:2"], "i3": [], "i4": ["GO:9"]},
                               seats=("o1", "o2", "o3"))
    meta_call = cstore.add_calls([call_row("i4", "meta", role="meta", job_id=cjob)])[0]
    calls = cstore.get_calls(cjob)
    cstore.record_attempt(calls[0]["call_id"], 1, NOW, NOW, "ok")
    cstore.record_attempt(calls[1]["call_id"], 1, NOW, NOW, "timeout")
    cstore.record_attempt(calls[2]["call_id"], 1, NOW, NOW, "reused")
    cstore.set_halt("argo", "budget", "spent")
    cstore.close()

    loaded = R.load_job(curl, cjob, with_calls=True)
    assert loaded["query_name"] == "go_mf" and loaded["query_version"] == "4"
    assert len(loaded["items"]) == 4 and loaded["n_proposers"] == 3
    assert len(loaded["attempts"]) == 3
    assert loaded["halts"] == {"argo": "budget: spent"}
    assert meta_call in {c["call_id"] for c in loaded["calls"]}

    ws = tmp_path / "ws.md"
    rc = R.main(["--candidate-store", curl, "--candidate-job", cjob,
                 "--baseline-store", burl, "--baseline-jobs", bjob, "--worksheet", str(ws)])
    out = capsys.readouterr().out
    assert rc == 0
    sheet = ws.read_text()
    for gid in ("GO:2", "GO:3", "GO:4", "GO:9"):
        assert gid in sheet
    assert "diagnostic" in out.lower()
    assert "no recorded attempt" in out


def test_main_refuses_non_go_mf4_baseline_and_missing_job(tmp_path, capsys):
    bstore, burl, bjob = _make(tmp_path / "b.sqlite", {"i1": ["GO:1"]}, query=("go_mf", "3"))
    bstore.close()
    cstore, curl, cjob = _make(tmp_path / "c.sqlite", {"i1": ["GO:1"]})
    cstore.close()
    ws = str(tmp_path / "ws.md")
    base = ["--candidate-store", curl, "--candidate-job", cjob,
            "--baseline-store", burl, "--worksheet", ws]
    assert R.main(base + ["--baseline-jobs", bjob]) == 2
    assert bjob in capsys.readouterr().err
    assert R.main(base + ["--baseline-jobs", "nope"]) == 2
    assert "nope" in capsys.readouterr().err
