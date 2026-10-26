"""``council_export`` -- stage 5's verdict export, end to end over real files.

Spends nothing and reaches nothing. The plan these tests export is written by
the REAL :meth:`OntologyMappingUtils.council_plan` (with the clearinghouse,
the council configuration and the retrieval stage injected), so what is
exercised is the file contract BETWEEN the two stages rather than a
hand-written ``PLAN.json`` that could drift from what stage 1 actually emits.
The council store is injected too: ``results()`` returns verdict rows shaped
like the ones the sqlite store holds.

The row derivation itself -- every cell, every decline class, the arithmetic
-- is covered in ``test_verdict_export.py``, which needs neither numpy nor a
plan. What is pinned HERE is the stage's own behaviour: which files it reads,
where it refuses to write, how it resolves the run's job ids, and that the
parquet it lands holds one row per function the plan recorded as un-run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

pytest.importorskip("numpy")
pytest.importorskip("pyarrow")

import pyarrow.parquet as pq  # noqa: E402

from kbutillib.domains.ai.council.types import (  # noqa: E402
    Roster,
    SeatSpec,
)
from kbutillib.domains.genome.ontology_mapping import (  # noqa: E402
    ontology_mapping_utils as omu,
)

QUERY = "go_mf@1"
MAPPING_HASH = "e" * 64
DECIDED_AT = "2026-10-20T12:00:00+00:00"


def _hash(n: int) -> str:
    return f"{n:064x}"


#: Six functions: five with content (two of which share a definition, so they
#: are ONE paid answer and TWO published rows) and one placeholder retrieval
#: declines.
UNIVERSE: List[Dict[str, Any]] = [
    {"entity_hash": _hash(1), "definition": "ABC transporter, partial"},
    {"entity_hash": _hash(2), "definition": "glucokinase"},
    {"entity_hash": _hash(3), "definition": "glucokinase"},
    {"entity_hash": _hash(4), "definition": "yfiA"},
    {"entity_hash": _hash(5), "definition": "glucose oxidase"},
    {"entity_hash": _hash(6), "definition": "hypothetical protein"},
]
ABSTAIN_TEXTS = {"hypothetical protein"}

#: The item ids ``unrun_set`` assigns -- ``t<index>`` over the SORTED distinct
#: non-declined texts, which is the same convention
#: ``function_to_go_council`` uses.
ITEM_OF_TEXT = {
    "ABC transporter, partial": "t0",
    "glucokinase": "t1",
    "glucose oxidase": "t2",
    "yfiA": "t3",
}


# ---------------------------------------------------------------------------
# The injected collaborators
# ---------------------------------------------------------------------------


class _FakeClearinghouse:
    def universe(self, entity_type: str, **_kw: Any) -> List[Dict[str, Any]]:
        return [dict(row) for row in UNIVERSE]

    def current_state(self, entity_type: str, **_kw: Any) -> List[Dict[str, Any]]:
        return []


class _FakeQueue:
    def __init__(self, seats: List[str], concurrency: int) -> None:
        self.seats = seats
        self.concurrency = concurrency
        self.workers = 1


class _FakeConfig:
    def __init__(self) -> None:
        self.default_profile = "go_council_v3"
        self.queues = {"mag_sol": _FakeQueue(["proposer_2"], 3)}

    def endpoint(self, name: str) -> Dict[str, Any]:
        return {
            "argo": {"kind": "argo", "base_url": "https://argo.example/api",
                     "account": "argo-anl", "max_concurrency": 24},
            "mag": {"kind": "openai", "base_url": "https://mag.example/v1",
                    "account": "mag-shared", "max_concurrency": 20,
                    "max_requests_per_minute": 120},
        }[name]


def _seat(seat_name: str, model: str, endpoint: str = "argo") -> SeatSpec:
    return SeatSpec(
        seat_name=seat_name,
        endpoint=endpoint,
        endpoint_kind="argo" if endpoint == "argo" else "openai",
        model=model,
        canonical_model=model,
    )


def _roster() -> Roster:
    """The 2026-10-20 bench: three proposers, a critic and a meta seat."""
    return Roster(
        proposers=[
            _seat("proposer_1", "gemini-3.5-flash"),
            _seat("proposer_2", "gpt-5.6-sol", endpoint="mag"),
            _seat("proposer_3", "gpt-5.6-luna", endpoint="mag"),
        ],
        critic=_seat("critic", "claude-sonnet-5"),
        meta=_seat("meta", "claude-opus-5"),
    )


#: The council store's DECLARED identity, as the stage reads it back off the
#: store. Stated here because ``verdicts.export_rows`` refuses to publish a
#: decided row without one: ``(store_id, job_id, item_id)`` is the whole join
#: key into ``kbaseincubator.council``, and a partial triple looks joinable.
STORE_ID = "poplar/council-1"


class _FakeStore:
    """The one store surface stage 5 reads: the DECLARED archive identity.

    Shaped exactly like ``SqliteStore.store_identity()``'s return value, so
    the stage's resolution path is the real one. The record is tested against
    the real store code in ``tests/domains/ai/council/test_store_identity.py``
    and end to end against a real store in
    ``tests/domains/test_verdict_council_reference.py``; here it only has to
    be present.
    """

    def __init__(self, store_id: Optional[str] = STORE_ID) -> None:
        self._store_id = store_id

    def store_identity(self) -> Optional[Dict[str, Any]]:
        if self._store_id is None:
            return None
        return {
            "declared_seq": 1,
            "record_version": 1,
            "store_id": self._store_id,
            "declared_at": "2026-10-22T09:00:00+00:00",
            "declared_by": "chenry",
            "supersedes": None,
        }


class _FakeCouncil:
    """The council surfaces the two stages use: config/roster, results, store.

    ``results`` records the job ids it was asked for, because "which jobs did
    the export read" is the whole of the run identity this stage contributes.
    ``store`` exposes the DECLARED archive identity the published ``council``
    reference is built from.
    """

    def __init__(
        self,
        results_by_job: Dict[str, List[Dict[str, Any]]],
        *,
        store_id: Optional[str] = STORE_ID,
    ) -> None:
        self._config = _FakeConfig()
        self._roster = _roster()
        self.results_by_job = results_by_job
        self.asked: List[str] = []
        self._store = _FakeStore(store_id)

    def store(self, **_kw: Any) -> _FakeStore:
        return self._store

    def config(self) -> _FakeConfig:
        return self._config

    def roster(self, profile: Optional[str] = None, **_kw: Any) -> Roster:
        return self._roster

    def results(self, job_id: str) -> List[Dict[str, Any]]:
        self.asked.append(job_id)
        return [dict(row) for row in self.results_by_job.get(job_id, [])]


class _Stage(omu.OntologyMappingUtils):
    """``OntologyMappingUtils`` with the backend and collaborators injected.

    ``mapping_version`` and ``function_to_go`` are the retrieval backend
    (scikit-learn plus the vendored GO artifacts); neither stage's own logic
    needs them, so they are stubbed and everything else -- ``write_ranked``,
    the partition, ``PLAN.json``, the export -- is the real code path.
    """

    def __init__(self, council: _FakeCouncil) -> None:
        super().__init__(config_file=False, token_file=None, kbase_token_file=None)
        self._fake_clearinghouse = _FakeClearinghouse()
        self._fake_council = council

    def mapping_version(self) -> Dict[str, Any]:
        return {
            "mapping_version": "ontomap-go-text-union@1.0.0 (test)",
            "mapping_hash": MAPPING_HASH,
            "provenance": {"backend": "ontomap-go-text-union"},
        }

    def function_to_go(self, functions: Any, max_candidates: int = 40) -> Dict[str, Any]:
        results = []
        for row in functions:
            abstained = row["text"] in ABSTAIN_TEXTS
            results.append(
                {
                    "query_id": row["query_id"],
                    "text": row["text"],
                    "source": row.get("source") or "",
                    "abstained": abstained,
                    "abstain_reason": "placeholder function" if abstained else None,
                    "candidates": (
                        []
                        if abstained
                        else [
                            {"go_id": "GO:0004340", "name": "glucokinase activity",
                             "def": "", "score": 0.9, "height": 5, "rank": 1},
                            {"go_id": "GO:0016301", "name": "kinase activity",
                             "def": "", "score": 0.5, "height": 3, "rank": 2},
                        ]
                    ),
                }
            )
        return {**self.mapping_version(), "results": results}

    def _clearinghouse(self) -> Any:
        return self._fake_clearinghouse

    def _council(self) -> Any:
        return self._fake_council


# ---------------------------------------------------------------------------
# Verdict rows, shaped like the store's
# ---------------------------------------------------------------------------


def _ballot(*picks: Any, contentless: bool = False) -> Dict[str, Any]:
    return {
        "seat": "proposer",
        "status": "ok",
        "picks": [{"id": gid, "confidence": conf, "why": ""} for gid, conf in picks],
        "overall_confidence": "high",
        "no_functional_content": contentless,
    }


def _ok_row(item_id: str, job_id: str, verdict: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "job_id": job_id,
        "item_id": item_id,
        "status": "ok",
        "verdict": {"item_id": item_id, "status": "ok", **verdict},
        "decided_at": DECIDED_AT,
    }


def _selected(item_id: str, job_id: str = "job-chunk-0") -> Dict[str, Any]:
    return _ok_row(
        item_id,
        job_id,
        {
            "selected_ids": ["GO:0004340"],
            "decline_reason": None,
            "final_confidence": "high",
            "decided_by": "unanimous",
            "ballots": [_ballot(("GO:0004340", "high"))] * 3,
            "votes": {"GO:0004340": 3},
            "n_valid": 3,
            "critic": {"verdict": "consistent", "contentless_input": False},
            "decided_at": DECIDED_AT,
        },
    )


def _declined(item_id: str, reason: str, job_id: str = "job-chunk-0") -> Dict[str, Any]:
    contentless = reason == "contentless"
    return _ok_row(
        item_id,
        job_id,
        {
            "selected_ids": [],
            "decline_reason": reason,
            "final_confidence": "medium",
            "decided_by": "meta",
            "ballots": [_ballot(contentless=contentless)] * 3,
            "votes": {},
            "n_valid": 3,
            "critic": {"verdict": "ambiguous", "contentless_input": contentless},
            "meta": {"final_go_ids": [], "final_confidence": "medium",
                     "no_functional_content": contentless, "reasoning": ""},
            "decided_at": DECIDED_AT,
        },
    )


def _errored(item_id: str, job_id: str = "job-chunk-0") -> Dict[str, Any]:
    """Every call failed: status ``error`` and an EMPTY verdict.

    Not a result. An empty pick here means "nobody answered", which must
    never be published as "the council chose nothing".
    """
    return {
        "job_id": job_id,
        "item_id": item_id,
        "status": "error",
        "verdict": {
            "item_id": item_id,
            "status": "error",
            "selected_ids": [],
            "n_valid": 0,
            "errors": [{"stage": "proposer", "seat": "proposer_1",
                        "kind": "invalid_output", "message": "truncated"}],
        },
        "decided_at": DECIDED_AT,
    }


def _rows(*, error_item: Optional[str] = None) -> List[Dict[str, Any]]:
    """One verdict row per submitted item: a pick, both declines, and a pick.

    ``error_item`` turns one of them into an item whose calls all failed.
    """
    rows = [
        _declined("t0", "no_candidate_matched"),
        _selected("t1"),
        _selected("t2"),
        _declined("t3", "contentless"),
    ]
    if error_item is None:
        return rows
    return [_errored(error_item) if row["item_id"] == error_item else row for row in rows]


# ---------------------------------------------------------------------------
# The fixture: a real plan, then an export
# ---------------------------------------------------------------------------


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    return tmp_path / "runs" / "2026-10-22-adp1"


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    return tmp_path / "scratch"


def _planned(
    run_dir: Path,
    scratch: Path,
    *,
    results_by_job: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> tuple:
    """Run the real stage 1, returning ``(stage, plan)``."""
    council = _FakeCouncil(
        results_by_job
        if results_by_job is not None
        else {"job-chunk-0": _rows()}
    )
    stage = _Stage(council)
    plan = stage.council_plan(run_dir, query=QUERY, scratch_dir=scratch)
    return stage, plan


def _read(path: str) -> List[Dict[str, Any]]:
    return pq.read_table(path).to_pylist()


# ---------------------------------------------------------------------------
# One row per input
# ---------------------------------------------------------------------------


class TestOneRowPerInput:
    def test_the_plan_records_the_partition_this_export_covers(
        self, run_dir: Path, scratch: Path
    ) -> None:
        """The input set is the un-run set: abstain plus fanout."""
        _stage, plan = _planned(run_dir, scratch)
        counts = plan["counts"]
        assert counts["universe"] == 6
        assert counts["unrun"] == 6
        assert counts["submit"] == 4
        assert counts["abstain"] == 1
        assert counts["fanout"] == 5
        assert counts["abstain"] + counts["fanout"] == counts["unrun"]

    def test_the_files_row_count_equals_the_plans_input_count(
        self, run_dir: Path, scratch: Path
    ) -> None:
        stage, plan = _planned(run_dir, scratch)
        export = stage.council_export(
            run_dir, scratch / "verdicts.parquet", job_ids=["job-chunk-0"]
        )
        assert export["counts"]["inputs"] == plan["counts"]["unrun"] == 6
        assert export["rows"] == 6
        assert pq.ParquetFile(export["out_path"]).metadata.num_rows == 6
        assert export["warnings"] == []

    def test_every_un_run_query_id_appears_exactly_once(
        self, run_dir: Path, scratch: Path
    ) -> None:
        stage, _plan = _planned(run_dir, scratch)
        export = stage.council_export(
            run_dir, scratch / "verdicts.parquet", job_ids=["job-chunk-0"]
        )
        query_ids = [row["query_id"] for row in _read(export["out_path"])]
        assert sorted(query_ids) == [_hash(n) for n in range(1, 7)]

    def test_two_functions_sharing_a_definition_get_two_rows(
        self, run_dir: Path, scratch: Path
    ) -> None:
        """One paid answer, fanned back out on ``query_id``."""
        stage, _plan = _planned(run_dir, scratch)
        export = stage.council_export(
            run_dir, scratch / "verdicts.parquet", job_ids=["job-chunk-0"]
        )
        shared = [
            row
            for row in _read(export["out_path"])
            if row["function_text"] == "glucokinase"
        ]
        assert sorted(row["query_id"] for row in shared) == [_hash(2), _hash(3)]
        assert all(row["go_ids"] == ["GO:0004340"] for row in shared)


# ---------------------------------------------------------------------------
# The three decline classes
# ---------------------------------------------------------------------------


class TestTheThreeDeclineClasses:
    def test_all_three_appear_with_their_reason_and_no_terms(
        self, run_dir: Path, scratch: Path
    ) -> None:
        stage, _plan = _planned(run_dir, scratch)
        export = stage.council_export(
            run_dir, scratch / "verdicts.parquet", job_ids=["job-chunk-0"]
        )
        rows = {row["query_id"]: row for row in _read(export["out_path"])}
        # abstained -- retrieval declined the placeholder function
        assert rows[_hash(6)]["decline_reason"] == "abstained:placeholder function"
        # no_candidate_matched -- the council looked, the term was not offered
        assert rows[_hash(1)]["decline_reason"] == "no_candidate_matched"
        # contentless -- the council found no functional content
        assert rows[_hash(4)]["decline_reason"] == "contentless"
        for query_id in (_hash(6), _hash(1), _hash(4)):
            assert rows[query_id]["go_ids"] == []
        assert export["counts"]["abstained"] == 1
        assert export["counts"]["no_candidate_matched"] == 1
        assert export["counts"]["contentless"] == 1

    def test_an_abstained_row_is_timestamped_from_the_plan(
        self, run_dir: Path, scratch: Path
    ) -> None:
        """A null ``observed_at`` makes the sharder skip the row."""
        stage, plan = _planned(run_dir, scratch)
        export = stage.council_export(
            run_dir, scratch / "verdicts.parquet", job_ids=["job-chunk-0"]
        )
        rows = {row["query_id"]: row for row in _read(export["out_path"])}
        assert rows[_hash(6)]["decided_at"] == plan["generated_at"]
        assert all(row["decided_at"] for row in rows.values())


# ---------------------------------------------------------------------------
# council_error
# ---------------------------------------------------------------------------


class TestCouncilErrorIsNotPublished:
    def test_an_errored_item_is_absent_and_warned_about(
        self, run_dir: Path, scratch: Path
    ) -> None:
        stage, _plan = _planned(
            run_dir, scratch, results_by_job={"job-chunk-0": _rows(error_item="t2")}
        )
        export = stage.council_export(
            run_dir, scratch / "verdicts.parquet", job_ids=["job-chunk-0"]
        )
        query_ids = {row["query_id"] for row in _read(export["out_path"])}
        # t2 is "glucose oxidase", which is entity hash 5.
        assert _hash(5) not in query_ids
        assert export["rows"] == 5
        assert export["counts"]["inputs"] == 6
        assert export["counts"]["withheld"] == 1
        assert export["counts"]["rows"] + export["counts"]["withheld"] == 6
        (warning,) = export["warnings"]
        assert "WITHHELD" in warning
        assert "re-select" in warning

    def test_a_retry_job_supersedes_the_error_and_marks_the_row_retried(
        self, run_dir: Path, scratch: Path
    ) -> None:
        """105 of 113 truncated items came back on a plain retry."""
        stage, _plan = _planned(
            run_dir,
            scratch,
            results_by_job={
                "job-chunk-0": _rows(error_item="t2"),
                "job-retry": [_selected("t2", job_id="job-retry")],
            },
        )
        export = stage.council_export(
            run_dir,
            scratch / "verdicts.parquet",
            job_ids=["job-chunk-0", "job-retry"],
            retry_job_ids=["job-retry"],
        )
        rows = {row["query_id"]: row for row in _read(export["out_path"])}
        assert export["rows"] == 6
        assert export["counts"]["withheld"] == 0
        assert rows[_hash(5)]["go_ids"] == ["GO:0004340"]
        assert rows[_hash(5)]["retried"] is True
        assert rows[_hash(2)]["retried"] is False

    def test_the_retry_job_is_read_last_whatever_order_it_was_given_in(
        self, run_dir: Path, scratch: Path
    ) -> None:
        stage, _plan = _planned(
            run_dir,
            scratch,
            results_by_job={
                "job-chunk-0": _rows(error_item="t2"),
                "job-retry": [_selected("t2", job_id="job-retry")],
            },
        )
        export = stage.council_export(
            run_dir,
            scratch / "verdicts.parquet",
            job_ids=["job-retry", "job-chunk-0"],
            retry_job_ids=["job-retry"],
        )
        assert export["job_ids"] == ["job-chunk-0", "job-retry"]
        assert stage._fake_council.asked == ["job-chunk-0", "job-retry"]
        assert export["counts"]["withheld"] == 0


# ---------------------------------------------------------------------------
# The run identity
# ---------------------------------------------------------------------------


class TestRunIdentity:
    def test_every_row_carries_the_plans_identity(
        self, run_dir: Path, scratch: Path
    ) -> None:
        stage, plan = _planned(run_dir, scratch)
        export = stage.council_export(
            run_dir, scratch / "verdicts.parquet", job_ids=["job-chunk-0"]
        )
        for row in _read(export["out_path"]):
            assert row["query_name"] == "go_mf"
            assert row["query_version"] == "1"
            assert row["mapping_hash"] == plan["mapping_hash"]
            assert row["roster_fingerprint"] == plan["roster_fingerprint"]
            assert row["job_ids"] == ["job-chunk-0"]

    def test_every_row_carries_the_plans_per_seat_routes_as_provenance(
        self, run_dir: Path, scratch: Path
    ) -> None:
        """The plan's transport_allocation reaches the published payload.

        Stage 1 records WHICH gateway each seat was routed over because that
        is the thing the pipeline iterates on to allocate token cost; the
        published row carries the per-seat route_fingerprint so it can still
        say where its answer came from. It is provenance, never a key --
        ``clearinghouse_parameters.COUNCIL_REFUSED_KEYS`` refuses it in the
        parameter set, because a re-routed seat must re-select nothing.
        """
        from kbutillib.domains.genome.ontology_mapping.verdicts import (
            route_fingerprint_cells,
        )

        stage, plan = _planned(run_dir, scratch)
        expected = route_fingerprint_cells(plan["transport_allocation"])
        assert expected, "the plan recorded no route fingerprints to publish"
        export = stage.council_export(
            run_dir, scratch / "verdicts.parquet", job_ids=["job-chunk-0"]
        )
        rows = _read(export["out_path"])
        assert rows
        for row in rows:
            assert row["route_fingerprints"] == expected

    def test_the_export_record_names_the_parameter_set_it_belongs_to(
        self, run_dir: Path, scratch: Path
    ) -> None:
        """So the publish stage does not re-derive the partition key."""
        stage, plan = _planned(run_dir, scratch)
        export = stage.council_export(
            run_dir, scratch / "verdicts.parquet", job_ids=["job-chunk-0"]
        )
        assert export["parameter_set_hash"] == plan["parameter_set_hash"]
        assert export["source"] == plan["source"] == "council/go_mf@1"
        assert export["plan_hash"] == plan["plan_hash"]
        assert export["query"] == QUERY


# ---------------------------------------------------------------------------
# Where the file goes
# ---------------------------------------------------------------------------


class TestWhereTheFileGoes:
    def test_the_default_lands_under_the_scratch_root_not_the_run_dir(
        self, run_dir: Path, scratch: Path
    ) -> None:
        stage, _plan = _planned(run_dir, scratch)
        export = stage.council_export(
            run_dir, job_ids=["job-chunk-0"], scratch_dir=scratch
        )
        out = Path(export["out_path"])
        assert out.name == "verdicts.parquet"
        assert scratch.resolve() in out.resolve().parents
        assert run_dir.resolve() not in out.resolve().parents
        assert list(run_dir.iterdir()) == [run_dir / "PLAN.json"]

    def test_an_out_path_inside_the_run_dir_is_refused(
        self, run_dir: Path, scratch: Path
    ) -> None:
        """The run directory is a synced surface; this file is 100+ MB of input."""
        stage, _plan = _planned(run_dir, scratch)
        with pytest.raises(ValueError, match="inside the run directory"):
            stage.council_export(
                run_dir, run_dir / "verdicts.parquet", job_ids=["job-chunk-0"]
            )

    def test_nothing_is_written_when_the_path_is_refused(
        self, run_dir: Path, scratch: Path
    ) -> None:
        stage, _plan = _planned(run_dir, scratch)
        with pytest.raises(ValueError):
            stage.council_export(
                run_dir, run_dir / "verdicts.parquet", job_ids=["job-chunk-0"]
            )
        assert not (run_dir / "verdicts.parquet").exists()


# ---------------------------------------------------------------------------
# Resolving the run's job ids
# ---------------------------------------------------------------------------


class TestJobIds:
    def test_no_job_ids_and_no_run_json_is_a_refusal(
        self, run_dir: Path, scratch: Path
    ) -> None:
        """Exporting with no job would write every item as withheld."""
        stage, _plan = _planned(run_dir, scratch)
        with pytest.raises(ValueError, match="no council job ids"):
            stage.council_export(run_dir, scratch / "verdicts.parquet")

    def test_the_job_ids_come_from_run_json_when_it_is_there(
        self, run_dir: Path, scratch: Path
    ) -> None:
        stage, _plan = _planned(
            run_dir,
            scratch,
            results_by_job={
                "job-chunk-0": _rows(error_item="t2"),
                "job-retry": [_selected("t2", job_id="job-retry")],
            },
        )
        (run_dir / omu.RUN_FILENAME).write_text(
            json.dumps(
                {
                    "chunks": [{"index": 0, "job_id": "job-chunk-0"}],
                    "retry_job_ids": ["job-retry"],
                }
            )
        )
        export = stage.council_export(run_dir, scratch / "verdicts.parquet")
        assert export["job_ids"] == ["job-chunk-0", "job-retry"]
        assert export["retry_job_ids"] == ["job-retry"]
        assert export["counts"]["withheld"] == 0
        assert export["counts"]["retried"] == 1

    def test_an_explicit_job_id_overrides_run_json(
        self, run_dir: Path, scratch: Path
    ) -> None:
        stage, _plan = _planned(run_dir, scratch)
        (run_dir / omu.RUN_FILENAME).write_text(json.dumps({"job_ids": ["job-wrong"]}))
        export = stage.council_export(
            run_dir, scratch / "verdicts.parquet", job_ids=["job-chunk-0"]
        )
        assert stage._fake_council.asked == ["job-chunk-0"]
        assert export["job_ids"] == ["job-chunk-0"]

    def test_a_repeated_job_id_is_read_once(
        self, run_dir: Path, scratch: Path
    ) -> None:
        stage, _plan = _planned(run_dir, scratch)
        (run_dir / omu.RUN_FILENAME).write_text(
            json.dumps(
                {
                    "job_id": "job-chunk-0",
                    "chunks": [
                        {"index": 0, "job_id": "job-chunk-0"},
                        {"index": 1, "job_id": "job-chunk-0"},
                    ],
                }
            )
        )
        export = stage.council_export(run_dir, scratch / "verdicts.parquet")
        assert export["job_ids"] == ["job-chunk-0"]
        assert stage._fake_council.asked == ["job-chunk-0"]


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


class TestRefusals:
    def test_a_run_dir_with_no_plan_is_a_file_not_found(
        self, tmp_path: Path
    ) -> None:
        stage = _Stage(_FakeCouncil({}))
        with pytest.raises(FileNotFoundError, match="council-plan"):
            stage.council_export(tmp_path / "empty", tmp_path / "out.parquet")

    def test_a_missing_partition_record_is_named_rather_than_skipped(
        self, run_dir: Path, scratch: Path
    ) -> None:
        """``/scratch`` is shared, ephemeral and not backed up on this host."""
        stage, plan = _planned(run_dir, scratch)
        Path(plan["fanout_file"]).unlink()
        with pytest.raises(FileNotFoundError, match="fanout.jsonl"):
            stage.council_export(
                run_dir, scratch / "verdicts.parquet", job_ids=["job-chunk-0"]
            )

    def test_records_that_disagree_with_the_plans_counts_are_refused(
        self, run_dir: Path, scratch: Path
    ) -> None:
        """Two plans' files would publish an input set nobody can reconstruct."""
        stage, plan = _planned(run_dir, scratch)
        fanout = Path(plan["fanout_file"])
        lines = fanout.read_text().splitlines()
        fanout.write_text("\n".join(lines[:-1]) + "\n")
        with pytest.raises(ValueError, match="disagree"):
            stage.council_export(
                run_dir, scratch / "verdicts.parquet", job_ids=["job-chunk-0"]
            )
