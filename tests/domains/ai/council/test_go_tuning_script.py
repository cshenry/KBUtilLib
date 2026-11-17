"""``scripts/council/go_tuning.py`` -- the extract/sample pipeline end to end.

Two real ``SqliteStore`` files in, a JSONL of classified conflicts and a
manifest out, through the CLI's own ``main``.  The point of running the whole
pipeline rather than the two library functions separately is the HAND-OFF:
``sample`` has to rebuild ``MatchedItem`` rows from what ``extract`` wrote,
and a field dropped in serialisation would not show up in either module's own
tests.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

pytest.importorskip("sqlalchemy")

from . import go_tuning_score_support as _S  # noqa: E402
from .go_tuning_support import row, two_stores  # noqa: E402

_SCRIPT = Path(__file__).resolve().parents[4] / "scripts/council/go_tuning.py"
_spec = importlib.util.spec_from_file_location("go_tuning_script", _SCRIPT)
S = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S)  # type: ignore[union-attr]

#: (entity label, candidate selection, baseline selection) for one of each
#: class, repeated enough times to clear the 100-conflict floor.
_SHAPES = (
    ("dj", ["GO:1"], ["GO:2"]),            # disjoint
    ("pa", ["GO:1", "GO:2"], ["GO:1"]),    # partial / alcf_superset
)


def _corpus() -> tuple:
    """60 disjoint + 60 partial conflicts, plus one row of every other class."""
    cand: List[Dict[str, Any]] = []
    base: List[Dict[str, Any]] = []
    for i in range(60):
        for prefix, csel, bsel in _SHAPES:
            label = f"{prefix}{i:03d}"
            source = "tool_a" if i % 3 else "tool_b"
            cand.append(row(f"c-{label}", label, selected=csel, source=source))
            base.append(row(f"b-{label}", label, selected=bsel, source=source))
    extras = (
        ("aa", ["GO:1"], ["GO:1"]),
        ("ad", [], []),
        ("al", [], ["GO:1"]),
        ("gm", ["GO:1"], []),
    )
    for label, csel, bsel in extras:
        cand.append(row(f"c-{label}", label, selected=csel))
        base.append(row(f"b-{label}", label, selected=bsel))
    return cand, base


@pytest.fixture
def stores(tmp_path):
    cand, base = _corpus()
    return two_stores(
        tmp_path,
        [{"job_id": "j-cand", "rows": cand}],
        [{"job_id": "j-base", "rows": base}],
    )


def test_extract_then_sample_end_to_end(tmp_path, stores, capsys) -> None:
    curl, cjobs, burl, bjobs = stores
    jsonl = tmp_path / "conflicts.jsonl"
    rc = S.main([
        "extract",
        "--candidate-store", curl, "--candidate-jobs", ",".join(cjobs),
        "--baseline-store", burl, "--baseline-jobs", ",".join(bjobs),
        "--out", str(jsonl),
    ])
    out = capsys.readouterr().out
    assert rc == 0

    # All six classes are printed, named, with their counts.
    for cls in ("agree_assigned", "agree_declined", "alcf_declined",
                "gomf_declined", "disjoint", "partial"):
        assert cls in out
    assert "conflicts (disjoint+partial): 120" in out
    assert "unstable_alcf" in out and "unstable_gomf" in out

    rows = [json.loads(line) for line in jsonl.read_text().splitlines()]
    assert len(rows) == 124
    assert sum(1 for r in rows if r["class"] == "disjoint") == 60
    assert sum(1 for r in rows if r["class"] == "partial") == 60
    assert {r["sub_kind"] for r in rows if r["class"] == "partial"} == {"alcf_superset"}
    # The adjudication context travels in the JSONL, both sides.
    assert all(r["candidate_decided_by"] == "unanimous" for r in rows)
    assert all(r["baseline_decided_by"] == "unanimous" for r in rows)
    assert all("menu" in r and "evidence_text" in r for r in rows)

    manifest_path = tmp_path / "manifest.json"
    rc = S.main([
        "sample", "--conflicts", str(jsonl), "--seed", "20261117",
        "--out", str(manifest_path),
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "manifest" in out and "pilot" in out

    manifest = json.loads(manifest_path.read_text())
    assert manifest["seed"] == "20261117"
    assert manifest["report"]["conflicts_available"] == 120
    assert manifest["report"]["pools"]["pilot"]["rows"] == 120
    assert manifest["report"]["pools"]["pilot"]["shortfall"] == 30
    # 120 rows plus the 30 position-swapped twins.
    assert manifest["report"]["entries_per_pool"]["pilot"] == 150
    # The hand-off preserved everything the sampler needs.
    pilot = [e for e in manifest["entries"] if e["pool"] == "pilot"]
    assert {e["class"] for e in pilot} == {"disjoint", "partial"}
    assert {e["source"] for e in pilot} == {"tool_a", "tool_b"}
    assert all(e["baseline_job_id"] == "j-base" for e in manifest["entries"])


def test_sample_is_byte_identical_across_two_invocations(tmp_path, stores, capsys) -> None:
    curl, cjobs, burl, bjobs = stores
    jsonl = tmp_path / "conflicts.jsonl"
    assert S.main([
        "extract",
        "--candidate-store", curl, "--candidate-jobs", ",".join(cjobs),
        "--baseline-store", burl, "--out", str(jsonl),
    ]) == 0
    paths = []
    for name in ("one.json", "two.json"):
        path = tmp_path / name
        assert S.main([
            "sample", "--conflicts", str(jsonl), "--seed", "s1", "--out", str(path)
        ]) == 0
        paths.append(path)
    capsys.readouterr()
    assert paths[0].read_bytes() == paths[1].read_bytes()


def test_sample_refuses_a_corpus_below_the_floor(tmp_path, capsys) -> None:
    jsonl = tmp_path / "small.jsonl"
    jsonl.write_text("".join(
        json.dumps({
            "item_id": f"c{i}", "entity_hash": f"{i:064d}", "data_hash": "0" * 64,
            "class": "disjoint", "sub_kind": None, "source": "tool_a",
            "evidence_text": "", "candidate_selected": ["GO:1"],
            "baseline_selected": ["GO:2"], "candidate_job_id": "j-cand",
            "baseline_item_id": f"b{i}", "baseline_job_id": "j-base",
            "candidate_answers": 1, "baseline_answers": 1, "menu": [],
        }) + "\n"
        for i in range(10)
    ), encoding="utf-8")
    rc = S.main(["sample", "--conflicts", str(jsonl), "--seed", "s", "--out",
                 str(tmp_path / "m.json")])
    err = capsys.readouterr().err
    assert rc == 2
    assert "REFUSED" in err and "only 10 disjoint+partial conflicts" in err


def test_extract_refuses_a_baseline_job_that_is_not_go_mf4(tmp_path, capsys) -> None:
    curl, cjobs, burl, bjobs = two_stores(
        tmp_path,
        [{"job_id": "j-cand", "rows": [row("c1", "e1", selected=["GO:1"])]}],
        [{"job_id": "j-v3", "query": ("go_mf", "3"),
          "rows": [row("b1", "e1", selected=["GO:2"])]}],
    )
    rc = S.main([
        "extract", "--candidate-store", curl, "--candidate-jobs", "j-cand",
        "--baseline-store", burl, "--baseline-jobs", "j-v3",
        "--out", str(tmp_path / "out.jsonl"),
    ])
    err = capsys.readouterr().err
    assert rc == 2
    assert "go_mf@4" in err and "j-v3" in err


def test_an_empty_jobs_option_is_an_error_not_a_wildcard(tmp_path, capsys) -> None:
    rc = S.main([
        "extract", "--candidate-store", "sqlite:///x", "--candidate-jobs", " , ",
        "--baseline-store", "sqlite:///y", "--out", str(tmp_path / "o.jsonl"),
    ])
    assert rc == 2
    assert "named no job" in capsys.readouterr().err


def test_both_subcommands_exist_and_a_command_is_required(capsys) -> None:
    assert S.parse_args(["extract", "--candidate-store", "s",
                         "--candidate-jobs", "j", "--baseline-store", "b",
                         "--out", "o"]).command == "extract"
    assert S.parse_args(["sample", "--conflicts", "c", "--seed", "1",
                         "--out", "o"]).command == "sample"
    with pytest.raises(SystemExit):
        S.parse_args([])


def test_plan_sizes_are_overridable_from_the_cli(capsys) -> None:
    args = S.parse_args(["sample", "--conflicts", "c", "--seed", "1", "--out", "o",
                         "--pilot-size", "40", "--swap-slice", "5",
                         "--regression-size", "7", "--decline-guard-size", "9"])
    assert (args.pilot_size, args.swap_slice) == (40, 5)
    assert (args.regression_size, args.decline_guard_size) == (7, 9)


# ---------------------------------------------------------------------------
# adjudicate
# ---------------------------------------------------------------------------
#
# The library behaviour is tested in ``test_go_tuning_adjudicate.py`` against
# the fake backend.  What is tested here is the SUBCOMMAND: that its options
# land on the library call unchanged, that the two files it reads are the two
# the previous steps wrote, and that a run which stopped at a gate does not
# exit 0.  ``run_tranches`` is recorded rather than run, because the CLI's own
# path builds a real ``CouncilUtils`` from ``--store`` -- the one thing in
# this pipeline that needs a configured endpoint.


def _manifest_and_conflicts(tmp_path, stores, capsys):
    """Run extract + sample, and return the two paths they wrote."""
    curl, cjobs, burl, bjobs = stores
    conflicts = tmp_path / "conflicts.jsonl"
    manifest = tmp_path / "manifest.json"
    assert (
        S.main(
            [
                "extract",
                "--candidate-store", curl,
                "--candidate-jobs", ",".join(cjobs),
                "--baseline-store", burl,
                "--out", str(conflicts),
            ]
        )
        == 0
    )
    assert (
        S.main(
            ["sample", "--conflicts", str(conflicts), "--seed", "7", "--out", str(manifest)]
        )
        == 0
    )
    capsys.readouterr()
    return manifest, conflicts


def test_adjudicate_passes_every_option_through_to_run_tranches(
    tmp_path, stores, capsys, monkeypatch
) -> None:
    manifest, conflicts = _manifest_and_conflicts(tmp_path, stores, capsys)
    seen: Dict[str, Any] = {}

    def recorder(store, profile, items, **kwargs):
        seen["store"] = store
        seen["profile"] = profile
        seen["items"] = [t.item_id for t in items]
        seen.update(kwargs)
        return {
            "manifest_id": kwargs["manifest_id"],
            "invocation_id": "inv-1",
            "query": "go_mf_adjudicate@1",
            "profile": profile,
            "roster_fingerprint": "f" * 64,
            "planned_tranches": 1,
            "token_ceiling": kwargs["token_ceiling"],
            "tranches": [
                {"index": 0, "outcome": "submitted", "job_id": "J1", "detail": "",
                 "usage": {"argo_tokens": 10, "unmetered_tokens": 99}}
            ],
            "job_ids": ["J1"],
            "stopped": None,
            "usage": {"argo_tokens": 10, "unmetered_tokens": 99},
            "overshoot": False,
        }

    monkeypatch.setattr(S, "run_tranches", recorder)
    code = S.main(
        [
            "adjudicate",
            "--manifest", str(manifest),
            "--matched", str(conflicts),
            "--pools", "pilot",
            "--store", "sqlite:///unused.sqlite",
            "--profile", "go_adjudicate",
            "--tranche-size", "25",
            "--token-ceiling", "123456",
            "--run-dir", str(tmp_path / "run"),
        ]
    )
    assert code == 0

    payload = json.loads(manifest.read_text())
    assert seen["store"] == "sqlite:///unused.sqlite"
    assert seen["profile"] == "go_adjudicate"
    assert seen["tranche_size"] == 25
    assert seen["token_ceiling"] == 123456
    assert seen["manifest_id"] == payload["manifest_id"]
    assert seen["work"] is True
    assert seen["retry_failed"] is False
    # The items are the PILOT's, in the manifest's own execution order.
    assert seen["items"] == list(payload["execution_order"]["pilot"])
    # And the ledger landed in --run-dir, which is where a resume looks.
    assert seen["ledger"].path == tmp_path / "run" / "ledger.jsonl"

    out = capsys.readouterr().out
    assert payload["manifest_id"] in out
    assert "NOT metered: 99" in out


def test_adjudicate_exits_3_when_the_run_stopped_at_a_gate(
    tmp_path, stores, capsys, monkeypatch
) -> None:
    """A partial corpus reported as success is how a hole gets carried forward."""
    manifest, conflicts = _manifest_and_conflicts(tmp_path, stores, capsys)

    monkeypatch.setattr(
        S,
        "run_tranches",
        lambda store, profile, items, **kwargs: {
            "manifest_id": "m1",
            "invocation_id": "inv-1",
            "query": "go_mf_adjudicate@1",
            "profile": profile,
            "roster_fingerprint": "f" * 64,
            "planned_tranches": 2,
            "token_ceiling": kwargs["token_ceiling"],
            "tranches": [],
            "job_ids": [],
            "stopped": {
                "tranche": 1,
                "reason": "refused_spot_check",
                "detail": "no calibration record matches",
            },
            "usage": {"argo_tokens": 0, "unmetered_tokens": 0},
            "overshoot": False,
        },
    )
    code = S.main(
        [
            "adjudicate",
            "--manifest", str(manifest),
            "--matched", str(conflicts),
            "--store", "sqlite:///unused.sqlite",
            "--run-dir", str(tmp_path / "run"),
        ]
    )
    assert code == 3
    assert "STOPPED at tranche 1: refused_spot_check" in capsys.readouterr().err


def test_adjudicate_defaults_to_every_pool_and_refuses_an_empty_pools_option(
    tmp_path, stores, capsys, monkeypatch
) -> None:
    manifest, conflicts = _manifest_and_conflicts(tmp_path, stores, capsys)
    seen: Dict[str, Any] = {}
    monkeypatch.setattr(
        S,
        "run_tranches",
        lambda store, profile, items, **kwargs: seen.update(
            items=[t.item_id for t in items]
        )
        or {
            "manifest_id": "m1", "invocation_id": "i", "query": "q", "profile": profile,
            "roster_fingerprint": "f", "planned_tranches": 1,
            "token_ceiling": kwargs["token_ceiling"], "tranches": [], "job_ids": [],
            "stopped": None, "usage": {"argo_tokens": 0, "unmetered_tokens": 0},
            "overshoot": False,
        },
    )
    base = [
        "adjudicate",
        "--manifest", str(manifest),
        "--matched", str(conflicts),
        "--store", "sqlite:///unused.sqlite",
        "--run-dir", str(tmp_path / "run"),
    ]
    assert S.main(base) == 0
    payload = json.loads(manifest.read_text())
    assert len(seen["items"]) == len(payload["entries"]), "not every pool was built"

    assert S.main([*base, "--pools", " , "]) == 2
    assert "named no pool" in capsys.readouterr().err


def test_the_three_subcommands_exist() -> None:
    for command in ("extract", "sample", "adjudicate"):
        args = S.parse_args([command, *_MINIMAL_ARGS[command]])
        assert args.command == command
        assert callable(args.func)


#: The required options of each subcommand, so the check above exercises the
#: parser rather than its defaults.
_MINIMAL_ARGS = {
    "extract": [
        "--candidate-store", "c", "--candidate-jobs", "j", "--baseline-store", "b",
        "--out", "o",
    ],
    "sample": ["--conflicts", "c", "--seed", "1", "--out", "o"],
    "adjudicate": ["--manifest", "m", "--matched", "x", "--store", "s", "--run-dir", "r"],
}


# ---------------------------------------------------------------------------
# pilot-table / spot-check / read-marks / calibrate
# ---------------------------------------------------------------------------
#
# The four human-facing subcommands, end to end through ``main`` but with
# ``read_results`` RECORDED rather than run: reading real results needs a
# council store with a settled adjudication job in it, which is
# ``test_go_tuning_adjudicate``'s subject.  What is this file's subject is
# the plumbing -- that ``--run-dir`` supplies the job ids, the spot-check ids
# and the spec/roster bindings, that the two files land where they were
# asked for, and that a marked workbook comes back through the same manifest.

_HAS_OPENPYXL = importlib.util.find_spec("openpyxl") is not None
_SPEC_HASH = "a" * 64
_ROSTER = "b" * 64


@pytest.mark.skipif(not _HAS_OPENPYXL, reason="openpyxl is the council-tuning extra")
class TestTableSubcommands:
    @pytest.fixture()
    def material(self, tmp_path, monkeypatch):
        """A manifest, its conflicts JSONL, a ledger with one tranche, results."""
        from kbutillib.domains.ai.council.go_tuning.ledger import Ledger

        from .go_tuning_table_support import material as build
        from .go_tuning_table_support import results_for, row_ids

        manifest, items = build()
        manifest.write(tmp_path / "manifest.json")
        (tmp_path / "conflicts.jsonl").write_text(
            "\n".join(json.dumps(i.as_dict(), sort_keys=True) for i in items) + "\n",
            encoding="utf-8",
        )
        results = results_for(manifest, items)
        monkeypatch.setattr(S, "read_results", lambda *a, **k: results)

        ids = row_ids(manifest)
        ledger = Ledger(tmp_path / "run")
        ledger.append(
            "tranche",
            phase="submitted",
            manifest_id=manifest.manifest_id,
            tranche_index=0,
            submission_token="tok-0",
            job_id="job-1",
            adjudicate_spec_hash=_SPEC_HASH,
            roster_fingerprint=_ROSTER,
            spot_check_ids=ids[:2],
        )
        return {
            "manifest": manifest,
            "ids": ids,
            "root": tmp_path,
            "common": [
                "--manifest", str(tmp_path / "manifest.json"),
                "--matched", str(tmp_path / "conflicts.jsonl"),
                "--store", "sqlite:///unused",
                "--run-dir", str(tmp_path / "run"),
            ],
        }

    def test_pilot_table_writes_both_files(self, material, capsys) -> None:
        root = material["root"]
        assert S.main([
            "pilot-table", *material["common"],
            "--xlsx", str(root / "pilot.xlsx"), "--md", str(root / "pilot.md"),
        ]) == 0
        out = capsys.readouterr().out
        assert (root / "pilot.xlsx").exists()
        assert (root / "pilot.md").exists()
        assert f"{len(material['ids'])} pilot row(s)" in out
        assert "answer sha256" in out

    def test_spot_check_uses_the_tranche_s_persisted_ids(self, material, capsys) -> None:
        root = material["root"]
        assert S.main([
            "spot-check", *material["common"],
            "--xlsx", str(root / "spot.xlsx"), "--md", str(root / "spot.md"),
        ]) == 0
        assert "2 spot-check row(s)" in capsys.readouterr().out

    def test_a_tranche_the_ledger_does_not_know_is_refused(self, material, capsys) -> None:
        root = material["root"]
        assert S.main([
            "spot-check", *material["common"], "--tranche", "7",
            "--xlsx", str(root / "s.xlsx"), "--md", str(root / "s.md"),
        ]) == 2
        assert "records no spot-check ids" in capsys.readouterr().err

    def test_read_marks_then_calibrate_through_the_ledger(
        self, material, capsys
    ) -> None:
        from kbutillib.domains.ai.council.go_tuning.ledger import Ledger

        from .go_tuning_table_support import mark

        root = material["root"]
        xlsx = root / "pilot.xlsx"
        S.main([
            "pilot-table", *material["common"],
            "--xlsx", str(xlsx), "--md", str(root / "pilot.md"),
        ])
        capsys.readouterr()

        # Unmarked: read-marks says so and does not claim success.
        assert S.main([
            "read-marks", "--manifest", str(root / "manifest.json"), "--xlsx", str(xlsx),
            "--out", str(root / "gold.json"),
        ]) == 3
        assert "still unmarked" in capsys.readouterr().err
        assert json.loads((root / "gold.json").read_text())["counts"]["blank"] == len(
            material["ids"]
        )

        mark(xlsx, {item_id: "R" for item_id in material["ids"]})

        assert S.main([
            "read-marks", "--manifest", str(root / "manifest.json"), "--xlsx", str(xlsx),
            "--run-dir", str(root / "run"), "--record",
        ]) == 0
        assert "recorded calibration" in capsys.readouterr().out

        assert S.main([
            "calibrate", *material["common"], "--xlsx", str(xlsx),
            "--out", str(root / "calibration.md"), "--record",
        ]) == 0
        out = capsys.readouterr().out
        assert "# go_mf adjudication calibration" in out
        assert (root / "calibration.md").exists()

        records = Ledger(root / "run").records("calibration")
        assert [r["source"] for r in records] == ["read-marks", "calibrate"]
        for record in records:
            assert record["manifest_id"] == material["manifest"].manifest_id
            assert record["adjudicate_spec_hash"] == _SPEC_HASH
            assert record["roster_fingerprint"] == _ROSTER
            assert record["band"] == "conditional"

    def test_a_disturbed_workbook_exits_2(self, material, capsys) -> None:
        import openpyxl

        from kbutillib.domains.ai.council.go_tuning import table as T

        root = material["root"]
        xlsx = root / "pilot.xlsx"
        S.main([
            "pilot-table", *material["common"],
            "--xlsx", str(xlsx), "--md", str(root / "pilot.md"),
        ])
        workbook = openpyxl.load_workbook(xlsx)
        workbook[T.TABLE_SHEET].delete_rows(3)
        workbook.save(xlsx)
        capsys.readouterr()
        assert S.main([
            "read-marks", "--manifest", str(root / "manifest.json"), "--xlsx", str(xlsx),
        ]) == 2
        assert "REFUSED" in capsys.readouterr().err

    def test_record_without_a_run_dir_is_a_parser_error(self) -> None:
        with pytest.raises(SystemExit):
            S.parse_args(["read-marks", "--manifest", "m", "--xlsx", "x", "--record"])

    def test_a_record_with_no_bindings_to_read_is_refused(
        self, material, capsys
    ) -> None:
        """An empty ledger cannot supply the spec/roster, so nothing is written."""
        from kbutillib.domains.ai.council.go_tuning.ledger import Ledger

        from .go_tuning_table_support import mark

        root = material["root"]
        xlsx = root / "pilot.xlsx"
        S.main([
            "pilot-table", *material["common"],
            "--xlsx", str(xlsx), "--md", str(root / "pilot.md"),
        ])
        mark(xlsx, {item_id: "R" for item_id in material["ids"]})
        capsys.readouterr()

        assert S.main([
            "read-marks", "--manifest", str(root / "manifest.json"), "--xlsx", str(xlsx),
            "--run-dir", str(root / "empty-run"), "--record",
        ]) == 2
        assert "bound to the adjudicate spec hash" in capsys.readouterr().err
        assert Ledger(root / "empty-run").records("calibration") == []

    def test_explicit_bindings_stand_in_for_the_ledger(self, material, capsys) -> None:
        from kbutillib.domains.ai.council.go_tuning.ledger import Ledger

        from .go_tuning_table_support import mark

        root = material["root"]
        xlsx = root / "pilot.xlsx"
        S.main([
            "pilot-table", *material["common"],
            "--xlsx", str(xlsx), "--md", str(root / "pilot.md"),
        ])
        mark(xlsx, {item_id: "R" for item_id in material["ids"]})
        capsys.readouterr()

        assert S.main([
            "read-marks", "--manifest", str(root / "manifest.json"), "--xlsx", str(xlsx),
            "--run-dir", str(root / "other-run"), "--record",
            "--spec-hash", "d" * 64, "--roster-fingerprint", "e" * 64,
        ]) == 0
        record = Ledger(root / "other-run").records("calibration")[0]
        assert record["adjudicate_spec_hash"] == "d" * 64
        assert record["roster_fingerprint"] == "e" * 64


def test_the_table_and_calibration_subcommands_exist() -> None:
    for command in ("pilot-table", "spot-check", "read-marks", "calibrate"):
        args = S.parse_args([command, *_MINIMAL_ARGS[command]])
        assert args.command == command
        assert callable(args.func)


_MINIMAL_ARGS.update(
    {
        "pilot-table": [
            "--manifest", "m", "--matched", "x", "--store", "s",
            "--xlsx", "t.xlsx", "--md", "t.md",
        ],
        "spot-check": [
            "--manifest", "m", "--matched", "x", "--store", "s",
            "--xlsx", "t.xlsx", "--md", "t.md",
        ],
        "read-marks": ["--manifest", "m", "--xlsx", "t.xlsx"],
        "calibrate": [
            "--manifest", "m", "--matched", "x", "--store", "s", "--xlsx", "t.xlsx",
        ],
    }
)


# ---------------------------------------------------------------------------
# fresh-run / score / audit
# ---------------------------------------------------------------------------
#
# The scoring subcommands, end to end through ``main`` against the SAME
# synthetic ``go_mf`` stores the scorer's own tests use.  What this file adds
# over ``test_go_tuning_score`` is the plumbing the library never sees: that
# ``--gold`` merges later-wins, that ``--band`` is read off the run's ledger
# when it is not typed, that ``--noise-run`` measures the floor here, and that
# the pilot refusal survives being asked for on a command line.


class TestScoringSubcommands:
    @pytest.fixture()
    def material(self, tmp_path):
        """A manifest file, a gold file, and the candidate/baseline stores."""
        manifest = _S.manifest_of(_S.corpus_entries())
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        gold_path = tmp_path / "gold.json"
        gold_path.write_text(
            json.dumps(
                {
                    "manifest_id": "m-test",
                    "scope": "heldout",
                    "marks": {},
                    "records": [
                        {"item_id": item_id, **record}
                        for item_id, record in _S.corpus_gold().items()
                    ],
                }
            ),
            encoding="utf-8",
        )

        runs = {}
        for name, right in (
            ("cand", _S.CANDIDATE_RIGHT),
            ("base", _S.BASELINE_RIGHT),
            ("base2", _S.BASELINE_RIGHT),
        ):
            runs[name] = (
                _S.run_store(
                    tmp_path / f"{name}.sqlite",
                    f"job-{name}",
                    _S.answers(right, pilot_right=_S.PILOT[:3] if name == "cand" else ()),
                ),
                f"job-{name}",
            )
        return {
            "root": tmp_path,
            "manifest": str(manifest_path),
            "gold": str(gold_path),
            "runs": runs,
            "run_dir": str(tmp_path / "run"),
        }

    def _score_args(self, material, *extra):
        cand_store, cand_job = material["runs"]["cand"]
        base_store, base_job = material["runs"]["base"]
        return [
            "score",
            "--candidate-store", cand_store, "--candidate-job", cand_job,
            "--baseline-store", base_store, "--baseline-job", base_job,
            "--gold", material["gold"],
            "--manifest", material["manifest"],
            "--run-dir", material["run_dir"],
            "--band", "trusted",
            "--noise-floor", "0.0",
            *extra,
        ]

    def test_score_prints_the_metrics_and_records_them(self, material, capsys) -> None:
        from kbutillib.domains.ai.council.go_tuning.ledger import Ledger

        out = material["root"] / "score.json"
        assert S.main(self._score_args(material, "--out", str(out))) == 0
        printed = capsys.readouterr().out
        assert "primary     candidate 80.0%  baseline 40.0%" in printed
        assert "GAIN: yes" in printed
        assert "REPLACED" in printed
        payload = json.loads(out.read_text())
        assert payload["primary"]["gain"] == 0.4
        records = Ledger(material["run_dir"]).records("score")
        assert len(records) == 1 and records[0]["replaces_incumbent"] is True

    def test_score_refuses_the_pilot_from_the_command_line(self, material, capsys) -> None:
        assert S.main(self._score_args(material, "--pools", "heldout,pilot")) == 2
        assert "LOCKED TEST SET" in capsys.readouterr().err

    def test_the_band_is_read_off_the_run_s_ledger_when_not_given(
        self, material, capsys
    ) -> None:
        from kbutillib.domains.ai.council.go_tuning.ledger import Ledger

        ledger = Ledger(material["run_dir"])
        ledger.append(
            "calibration",
            manifest_id="m-test",
            adjudicate_spec_hash="a" * 64,
            roster_fingerprint="b" * 64,
            band="not_trusted",
            marks={},
        )
        args = [a for a in self._score_args(material) if a not in ("--band", "trusted")]
        assert S.main(args) == 2
        assert "not_trusted" in capsys.readouterr().err

    def test_no_band_anywhere_is_refused_rather_than_assumed(self, material, capsys) -> None:
        args = [a for a in self._score_args(material) if a not in ("--band", "trusted")]
        assert S.main(args) == 2
        assert "carries a band" in capsys.readouterr().err

    def test_two_noise_runs_measure_the_floor_here(self, material, capsys) -> None:
        base_store, base_job = material["runs"]["base"]
        other_store, other_job = material["runs"]["base2"]
        args = [a for a in self._score_args(material) if a not in ("--noise-floor", "0.0")]
        assert S.main(
            [
                *args,
                "--noise-run", f"{base_store}#{base_job}",
                "--noise-run", f"{other_store}#{other_job}",
            ]
        ) == 0
        printed = capsys.readouterr().out
        assert "noise floor 0.0000" in printed
        assert "guard key(s) flipped" in printed

    def test_one_noise_run_is_refused(self, material, capsys) -> None:
        base_store, base_job = material["runs"]["base"]
        args = [a for a in self._score_args(material) if a not in ("--noise-floor", "0.0")]
        assert S.main([*args, "--noise-run", f"{base_store}#{base_job}"]) == 2
        assert "between TWO iteration-0 baselines" in capsys.readouterr().err

    def test_both_noise_options_at_once_are_refused(self, material, capsys) -> None:
        base_store, base_job = material["runs"]["base"]
        assert S.main(
            self._score_args(material, "--noise-run", f"{base_store}#{base_job}")
        ) == 2
        assert "not both" in capsys.readouterr().err

    def test_a_run_spec_without_a_job_is_refused(self, material, capsys) -> None:
        args = [a for a in self._score_args(material) if a not in ("--noise-floor", "0.0")]
        assert S.main([
            *args,
            "--noise-run", "sqlite:////tmp/x.sqlite",
            "--noise-run", "sqlite:////tmp/y.sqlite",
        ]) == 2
        assert "does not name a run" in capsys.readouterr().err

    def test_gold_merges_later_wins(self, material, capsys) -> None:
        """A second --gold file overrides the first for the items it covers.

        Which is the merge the PRD asks for: the adjudicated gold first,
        Chris's marks second.
        """
        override = material["root"] / "chris.json"
        override.write_text(
            json.dumps(
                {
                    "records": [
                        {"item_id": item_id, **_S.human(["GO:0016887"])}
                        for item_id in _S.HELDOUT
                    ]
                }
            ),
            encoding="utf-8",
        )
        assert S.main(self._score_args(material, "--gold", str(override))) == 0
        printed = capsys.readouterr().out
        # Every item is now gold-wrong for the candidate and gold-right for
        # the baseline's wrong answer, so the gain inverts.
        assert "primary     candidate 20.0%  baseline 60.0%" in printed
        assert "GAIN: no" in printed

    def test_audit_reads_the_pilot_and_says_so(self, material, capsys) -> None:
        from kbutillib.domains.ai.council.go_tuning.ledger import Ledger

        pilot_gold = material["root"] / "pilot-gold.json"
        pilot_gold.write_text(
            json.dumps(
                {
                    "records": [
                        {"item_id": item_id, **_S.human(_S.TRUTH[item_id])}
                        for item_id in _S.PILOT
                    ]
                }
            ),
            encoding="utf-8",
        )
        cand_store, cand_job = material["runs"]["cand"]
        base_store, base_job = material["runs"]["base"]
        assert S.main([
            "audit",
            "--candidate-store", cand_store, "--candidate-job", cand_job,
            "--baseline-store", base_store, "--baseline-job", base_job,
            "--gold", str(pilot_gold),
            "--manifest", material["manifest"],
            "--run-dir", material["run_dir"],
            "--noise-floor", "0.0",
        ]) == 0
        printed = capsys.readouterr().out
        assert "on pool pilot" in printed
        assert "LOCKED test set" in printed
        records = Ledger(material["run_dir"]).records("score")
        assert [r["audit"] for r in records] == [True]

    def test_score_reports_the_stop_rule_after_recording(self, material, capsys) -> None:
        """Three non-gaining candidates in one ledger, and the loop stops."""
        from kbutillib.domains.ai.council.go_tuning.ledger import Ledger

        base_store, base_job = material["runs"]["base"]
        other_store, other_job = material["runs"]["base2"]
        args = [
            "score",
            "--candidate-store", other_store, "--candidate-job", other_job,
            "--baseline-store", base_store, "--baseline-job", base_job,
            "--gold", material["gold"],
            "--manifest", material["manifest"],
            "--run-dir", material["run_dir"],
            "--band", "trusted",
            "--noise-floor", "0.0",
        ]
        for _ in range(2):
            assert S.main(args) == 0
            assert "continue:" in capsys.readouterr().out
        assert S.main(args) == 0
        captured = capsys.readouterr()
        assert "STOP (no_improvement_streak)" in captured.err
        assert Ledger(material["run_dir"]).records("stop")[0]["reason"] == (
            "no_improvement_streak"
        )


def test_the_scoring_subcommands_exist() -> None:
    for command in ("fresh-run", "score", "audit"):
        args = S.parse_args([command, *_MINIMAL_ARGS[command]])
        assert args.command == command
        assert callable(args.func)


_MINIMAL_ARGS.update(
    {
        "fresh-run": [
            "--spec-store", "s", "--query", "go_mf", "--version", "4",
            "--manifest", "m", "--matched", "x", "--run-dir", "r",
        ],
        "score": [
            "--candidate-store", "c", "--candidate-job", "jc",
            "--baseline-store", "b", "--baseline-job", "jb",
            "--gold", "g", "--manifest", "m", "--run-dir", "r",
        ],
        "audit": [
            "--candidate-store", "c", "--candidate-job", "jc",
            "--baseline-store", "b", "--baseline-job", "jb",
            "--gold", "g", "--manifest", "m", "--run-dir", "r",
        ],
    }
)


def test_fresh_run_passes_every_option_through(tmp_path, stores, capsys, monkeypatch) -> None:
    """``fresh-run``'s own plumbing, with the run itself RECORDED.

    Actually running it needs a ``CouncilConfig`` and a seated roster, which
    is ``test_go_tuning_run``'s subject; what belongs here is that the
    manifest and the extract become the right items, that ``--run-dir`` and
    ``--label`` reach the function unchanged, and that the ledger lands where
    a reader will look for it.
    """
    manifest, conflicts = _manifest_and_conflicts(tmp_path, stores, capsys)
    seen: Dict[str, Any] = {}

    def recorder(spec_store, query, version, items, profile, run_dir, **kwargs):
        seen.update(
            spec_store=spec_store,
            query=query,
            version=version,
            items=[t.item_id for t in items],
            evidence=[dict(t.evidence) for t in items],
            profile=profile,
            run_dir=run_dir,
            **kwargs,
        )
        return {
            "label": kwargs.get("label") or "generated",
            "store": "sqlite:///run/stores/iter0.sqlite",
            "job_id": "J1",
            "query": f"{query}@{version}",
            "n_items": len(items),
            "fresh": True,
            "reused": 0,
        }

    monkeypatch.setattr(S, "fresh_run", recorder)
    assert S.main([
        "fresh-run",
        "--spec-store", "sqlite:///loop.sqlite",
        "--query", "go_mf",
        "--version", "4",
        "--manifest", str(manifest),
        "--matched", str(conflicts),
        "--profile", "go_mf",
        "--run-dir", str(tmp_path / "run"),
        "--label", "iter0-baseline",
    ]) == 0

    payload = json.loads(manifest.read_text())
    assert seen["spec_store"] == "sqlite:///loop.sqlite"
    assert seen["query"] == "go_mf" and seen["version"] == "4"
    assert seen["profile"] == "go_mf"
    assert str(seen["run_dir"]) == str(tmp_path / "run")
    assert seen["label"] == "iter0-baseline"
    assert seen["work"] is True
    # Omitting --pools runs EVERY pool as one job over one frozen item set,
    # with each item once: the manifest's swapped pilot twins are a second
    # orientation of one question, and go_mf has no orientation to swap.
    unique = {e["item_id"] for e in payload["entries"]}
    assert len(payload["entries"]) > len(unique)
    assert sorted(seen["items"]) == sorted(unique)
    # A go_mf question is the annotation string and its source, not a
    # blinded A/B pair -- an adjudication item smuggled in here would score
    # against the wrong prompt entirely.
    assert set(seen["evidence"][0]) == {"text", "source"}
    assert seen["ledger"].path == tmp_path / "run" / "ledger.jsonl"
    assert "iter0-baseline" in capsys.readouterr().out


def test_fresh_run_with_no_work_exits_3(tmp_path, stores, capsys, monkeypatch) -> None:
    """Nothing was worked, so nothing was proven fresh: not a failure, not done."""
    manifest, conflicts = _manifest_and_conflicts(tmp_path, stores, capsys)

    monkeypatch.setattr(
        S,
        "fresh_run",
        lambda *a, **k: {
            "label": "iter0", "store": "sqlite:///x", "job_id": "J1",
            "query": "go_mf@4", "n_items": 1, "fresh": None, "reused": None,
        },
    )
    assert S.main([
        "fresh-run",
        "--spec-store", "sqlite:///loop.sqlite",
        "--query", "go_mf", "--version", "4",
        "--manifest", str(manifest), "--matched", str(conflicts),
        "--run-dir", str(tmp_path / "run"),
        "--no-work",
    ]) == 3
    assert "not checked" in capsys.readouterr().out
