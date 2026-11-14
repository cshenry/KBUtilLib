"""``scripts/council/alcf_bench_report.py`` on synthetic verdict sets (no store)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SCRIPT_PATH = _REPO_ROOT / "scripts/council/alcf_bench_report.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("alcf_bench_report", _SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


R = _load()


def item(item_id: str, picked: Optional[List[str]], *, status: str = "ok",
         conf: Optional[str] = "high", by: Optional[str] = "unanimous",
         text: Optional[str] = None, ent: str = "e", data: str = "d") -> Dict[str, Any]:
    verdict = None
    if status == "ok":
        verdict = {"status": "ok", "item_id": item_id, "selected_ids": picked or [],
                   "final_confidence": conf, "decided_by": by}
    elif status == "error":
        verdict = {"status": "error", "item_id": item_id, "selected_ids": []}
    return {"item_id": item_id, "entity_hash": ent + item_id, "data_hash": data + item_id,
            "status": status, "verdict": verdict, "evidence": {"text": text or f"ann {item_id}"}}


def bjob(items: List[Dict[str, Any]], job_id: str = "B1", proposers: int = 2,
         version: str = "4") -> Dict[str, Any]:
    return {"job_id": job_id, "query_name": "go_mf", "query_version": version,
            "items": items, "n_proposers": proposers}


def candidate(items: List[Dict[str, Any]], proposers: int = 2, **extra: Any) -> Dict[str, Any]:
    return {"job_id": "C1", "query_name": "go_mf", "query_version": "4",
            "items": items, "n_proposers": proposers, **extra}


# 8 items: both-declined, exact, 3 kinds of disagreement, partial overlap pair
def sets():
    cand = [
        item("i1", []), item("i2", ["GO:1"]),
        item("i3", [], text="cand declined | pipe"), item("i4", ["GO:4"]),
        item("i5", ["GO:5"], conf="low", by="meta"), item("i6", ["GO:6", "GO:7"]),
        item("i7", None, status="error"),  # failed candidate item
        item("i8", ["GO:8"], conf=None, by="deterministic_empty"),
        item("i9", None, status="pending"),
    ]
    base = [
        item("i1", []), item("i2", ["GO:1"]), item("i3", ["GO:3"]),
        item("i4", []), item("i5", ["GO:9"], conf="medium", by="meta"),
        item("i6", ["GO:6"]), item("i7", []), item("i8", ["GO:8"]),
        item("i9", ["GO:9"]),
    ]
    return cand, [bjob(base)]


def report():
    cand, bases = sets()
    return R.build_report(candidate(cand), bases)


def test_worksheet_groups_and_rows() -> None:
    rep = report()
    g = rep["groups"]
    assert [r["item_id"] for r in g["candidate-declined-baseline-picked"]] == ["i3"]
    assert [r["item_id"] for r in g["candidate-picked-baseline-declined"]] == ["i4"]
    assert [r["item_id"] for r in g["both-picked-differ"]] == ["i5", "i6"]
    row = g["both-picked-differ"][1]
    assert row["baseline"] == ["GO:6"] and row["candidate"] == ["GO:6", "GO:7"]
    sheet = R.render_worksheet(g, rep["agreement"]["n"])
    assert "NOT ground truth" in sheet and "R (candidate right)" in sheet
    assert "## candidate-declined-baseline-picked  (1 items)" in sheet
    # blank leading column, annotation string, baseline then candidate ids
    assert "|  | `cand declined \\| pipe` | GO:3 | (declined) |" in sheet
    assert "|  | `ann i5` | GO:9 | GO:5 |" in sheet
    assert sheet.index("candidate-declined") < sheet.index("candidate-picked-baseline") < sheet.index("both-picked")


def test_coverage_counts_are_separate() -> None:
    cov = report()["coverage"]
    assert (cov["submitted"], cov["successful"], cov["failed"], cov["pending"]) == (9, 7, 1, 1)
    assert cov["matched"] == 9  # matched = eligible baseline exists, regardless of candidate status


def test_failed_candidate_verdict_is_not_a_decline() -> None:
    rep = report()
    # i7 (failed) vs a baseline decline would have been "exact match" had it counted as empty
    assert rep["agreement"]["n"] == 7
    assert rep["profile"]["n"] == 7
    assert rep["profile"]["size"]["decline"]["candidate_n"] == 2  # i1, i3 only
    all_rows = [r["item_id"] for v in rep["groups"].values() for r in v]
    assert "i7" not in all_rows and "i9" not in all_rows


def test_exact_match_and_jaccard() -> None:
    agr = report()["agreement"]
    # exact: i1 (both declined), i2, i8 -> 3 of 7
    assert agr["exact"] == 3 and agr["n"] == 7
    assert agr["both_declined"] == 1
    assert agr["exact_excluding_both_declined"] == 2 and agr["n_excluding_both_declined"] == 6
    # jaccard: i1 1, i2 1, i3 0, i4 0, i5 0, i6 .5, i8 1
    assert agr["mean_jaccard"] == pytest.approx(3.5 / 7)


def test_jaccard_empty_empty_is_one() -> None:
    assert R.jaccard([], []) == 1.0
    assert R.jaccard(["a"], []) == 0.0
    assert R.jaccard(["a", "b"], ["b"]) == 0.5


def test_profile_deltas_on_matched_denominator_with_own_categories() -> None:
    prof = report()["profile"]
    assert prof["n"] == 7
    size = prof["size"]
    # candidate declines: i1, i3 ; baseline declines: i1, i4
    assert size["decline"]["candidate_pct"] == pytest.approx(200 / 7)
    assert size["decline"]["delta_pts"] == pytest.approx(0.0)
    assert size["exactly_2"]["candidate_n"] == 1 and size["exactly_2"]["baseline_n"] == 0
    assert size["exactly_2"]["delta_pts"] == pytest.approx(100 / 7)
    for section in ("size", "confidence", "decided_by"):
        assert sum(r["candidate_n"] for r in prof[section].values()) == 7
        assert sum(r["baseline_n"] for r in prof[section].values()) == 7
    conf = prof["confidence"]
    assert conf["unknown"]["candidate_n"] == 1 and conf["low"]["candidate_n"] == 1
    by = prof["decided_by"]
    assert by["deterministic_empty"]["candidate_n"] == 1
    assert by["meta"]["candidate_n"] == 1 and by["unknown"]["candidate_n"] == 0
    assert prof["decided_by_deltas_suppressed"] is None


def test_interval_from_matched_n_and_na_at_zero() -> None:
    lo, hi = R.wilson_interval(2, 7)
    assert 0 < lo < 2 / 7 * 100 < hi < 100
    wide = R.wilson_interval(20, 70)
    assert (wide[1] - wide[0]) < (hi - lo)  # same proportion, larger n, narrower
    assert R.wilson_interval(0, 0) is None
    assert report()["profile"]["decline_interval_candidate"] == pytest.approx(R.wilson_interval(2, 7))
    text = R.render_summary(*_summary_args(report()))
    assert "ONE proportion" in text or "one proportion" in text
    assert "NOT apply to exact-match agreement" in text.replace("does NOT apply to\nexact-match", "NOT apply to exact-match") or "NOT apply to exact-match agreement" in text
    empty = R.profile({}, 2, 2)
    assert empty["n"] == 0 and empty["decline_interval_candidate"] is None


def test_decided_by_deltas_suppressed_when_proposer_counts_differ() -> None:
    cand, bases = sets()
    rep = R.build_report(candidate(cand, proposers=3), bases)
    reason = rep["profile"]["decided_by_deltas_suppressed"]
    assert reason and "3 proposer" in reason and "2" in reason
    text = R.render_summary(*_summary_args(rep))
    assert "decided_by deltas SUPPRESSED" in text
    by_lines = text.split("decided_by:")[1].split("decided_by deltas SUPPRESSED")[0]
    assert "delta" not in by_lines
    # size/confidence deltas are still stated
    assert "delta +" in text or "delta -" in text or "delta N/A" in text


def _summary_args(rep: Dict[str, Any], inventory: Optional[Dict[str, Any]] = None):
    return (rep["coverage"], rep["agreement"], rep["profile"], rep["groups"],
            inventory or R.failure_inventory([], []), rep["halts"], "ws.md")


def _calls_and_attempts():
    calls = [
        {"call_id": "c1", "item_id": "i1", "role": "proposer", "seat_name": "A", "status": "ok"},
        {"call_id": "c2", "item_id": "i2", "role": "proposer", "seat_name": "A", "status": "ok"},
        {"call_id": "c3", "item_id": "i3", "role": "proposer", "seat_name": "A", "status": "pending",
         "error": {"kind": "rate_limited"}},
        {"call_id": "c4", "item_id": "i3", "role": "meta", "seat_name": "M", "status": "ok"},
        {"call_id": "c5", "item_id": "i1", "role": "proposer", "seat_name": "B", "status": "ok"},
    ]
    t = "2026-11-14"
    attempts = [
        {"call_id": "c1", "attempt_no": 1, "finished_at": t, "outcome_kind": "ok"},
        {"call_id": "c2", "attempt_no": 1, "finished_at": t, "outcome_kind": "invalid_output"},
        {"call_id": "c2", "attempt_no": 2, "finished_at": t, "outcome_kind": "ok"},
        {"call_id": "c3", "attempt_no": 1, "finished_at": t, "outcome_kind": "reused"},
        {"call_id": "c3", "attempt_no": 2, "finished_at": None, "outcome_kind": "ok"},  # not completed
        {"call_id": "zz", "attempt_no": 1, "finished_at": t, "outcome_kind": "ok"},  # other job
    ]
    return calls, attempts


def test_failure_inventory_excludes_reused_and_labels_no_attempt() -> None:
    calls, attempts = _calls_and_attempts()
    inv = R.failure_inventory(calls, attempts)
    a = inv["per_seat"]["A"]
    assert (a["ok"], a["failure"], a["attempts"]) == (2, 1, 3)
    assert a["reused"] == 1  # counted apart, in neither numerator nor denominator
    assert a["failure_rate"] == pytest.approx(100 / 3)
    assert dict(a["kinds"]) == {"ok": 2, "invalid_output": 1}
    assert a["no_recorded_attempt"] == 0
    b = inv["per_seat"]["B"]
    assert b["attempts"] == 0 and b["failure_rate"] is None and b["no_recorded_attempt"] == 1
    assert inv["call_final_errors"] == {"rate_limited": 1}
    assert inv["meta_invoked_items"] == 1 and inv["items_with_calls"] == 3
    assert inv["meta_invocation_rate"] == pytest.approx(100 / 3)
    text = R.render_summary(*_summary_args(report(), inv))
    assert "calls with no recorded attempt: 1" in text
    assert "never attempted" not in text.replace("not 'never attempted'", "")
    assert "N/A" in text  # seat B failure rate at zero attempts
    assert "_park writes none" in text


def test_duplicate_eligible_baseline_match_refuses() -> None:
    cand, bases = sets()
    dup = bjob([item("i2", ["GO:2"]), item("i1", [])], job_id="B2")
    with pytest.raises(R.Refused) as exc:
        R.build_report(candidate(cand), bases + [dup])
    assert sorted(exc.value.ids) == ["i1", "i2"]


def test_ineligible_baseline_rows_are_not_counted_duplicates_or_matches() -> None:
    cand, bases = sets()
    failed_dup = bjob([item("i2", None, status="error")], job_id="B2")
    rep = R.build_report(candidate(cand), bases + [failed_dup])  # only one ELIGIBLE row
    assert rep["agreement"]["n"] == 7


def test_mismatched_hash_and_wrong_query_refuse() -> None:
    cand, bases = sets()
    bad = [dict(i) for i in bases[0]["items"]]
    bad[1] = dict(bad[1], data_hash="other")
    with pytest.raises(R.Refused) as exc:
        R.build_report(candidate(cand), [bjob(bad)])
    assert exc.value.ids == ["i2"]
    with pytest.raises(R.Refused) as exc:
        R.build_report(candidate(cand), [bjob(bases[0]["items"], version="3")])
    assert exc.value.ids == ["B1"]


def test_item_absent_from_baseline_is_unmatched_not_refused() -> None:
    cand, bases = sets()
    rep = R.build_report(candidate(cand + [item("i10", ["GO:1"])]), bases)
    assert rep["unmatched"] == {"i10": "no baseline row in the named jobs"}
    assert rep["coverage"]["submitted"] == 10 and rep["coverage"]["matched"] == 9


def test_all_four_warnings_and_no_verdict_or_defect_language() -> None:
    rep = report()
    calls, attempts = _calls_and_attempts()
    text = R.render_summary(*_summary_args(rep, R.failure_inventory(calls, attempts)))
    low = text.lower()
    assert "survivorship-filtered" in low and "never quote it as an error rate" in low
    assert "model family and in requesting response_format" in " ".join(low.split())
    assert "does not apply to exact-match agreement" in " ".join(low.split())
    assert "direction" in low and "higher" in low and "lower" in low
    assert "diagnostic, not the score" in low and "no pass/fail verdict" in low
    for word in ("defect", "defective", "passed", "failed the", "pass:", "fail:", "verdict: pass", "verdict: fail"):
        assert word not in low
    assert "context only, not the comparison" in low
    assert "48,437" in text and "48.9%" in text


def test_main_requires_inputs_and_has_no_manifest_option(capsys: Any) -> None:
    with pytest.raises(SystemExit):
        R.parse_args(["--candidate-job", "x"])
    with pytest.raises(SystemExit):
        R.parse_args(["--candidate-store", "s", "--candidate-job", "j", "--baseline-store", "s",
                      "--baseline-jobs", "b", "--worksheet", "w", "--manifest", "m"])


def test_main_end_to_end_and_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                     capsys: Any) -> None:
    cand, bases = sets()
    cand_job = candidate(cand, calls=[], attempts=[], halts={})
    jobs = {"C1": cand_job, "B1": bases[0]}
    monkeypatch.setattr(R, "load_job", lambda url, jid, with_calls=False: jobs[jid])
    ws = tmp_path / "ws.md"
    argv = ["--candidate-store", "c", "--candidate-job", "C1", "--baseline-store", "b",
            "--baseline-jobs", "B1", "--worksheet", str(ws)]
    assert R.main(argv) == 0
    out = capsys.readouterr().out
    assert "candidate declined / baseline picked: 1" in out
    assert "both picked and differ: 2" in out
    assert ws.read_text().count("|  |") == 4

    dup = bjob([item("i2", ["GO:2"])], job_id="B2")
    jobs["B2"] = dup
    assert R.main(argv[:-4] + ["--baseline-jobs", "B1,B2", "--worksheet", str(ws)]) == 2
    err = capsys.readouterr().err
    assert "REFUSED" in err and "i2" in err
