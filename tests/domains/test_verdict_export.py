"""The verdict export's row derivation -- :mod:`...ontology_mapping.verdicts`.

Pure: no store, no clearinghouse, no network, no tokens. The module under
test imports neither numpy nor pyarrow at import time (pyarrow is imported
inside :func:`write_verdict_parquet`), which is why these tests run on a host
without the retrieval stack -- the same reason ``test_unrun_set.py`` exists
beside ``test_ontomap_council_plan.py``.

WHAT MATTERS MOST HERE is that all THREE decline classes are written as rows
and that a ``council_error`` item is not. Un-run is defined as the absence of
a current-state result row, so an ABSENT row is indistinguishable from "not
yet run": dropping a decline makes stage 1 re-select it forever (the completed
run had 9,356 declines, 4,893 of them ``no_candidate_matched``, each a full
five-seat council pass), while WRITING an item that has no verdict publishes
an answer the council never gave.

The second thing pinned here is that ``query_id`` comes off the fanout and
abstain records and is never resolved through the text: two functions sharing
one definition get one paid answer and TWO rows, each under its own
``query_id``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

import pytest

from kbutillib.domains.genome.ontology_mapping import verdicts as vx

MAPPING_HASH = "e" * 64
ROSTER_FINGERPRINT = "r" * 64

#: The run identity every row carries. ``roster_fingerprint`` is provenance
#: and not a key -- the bench partitions the corpus, the transport does not.
IDENTITY: Dict[str, Any] = {
    "query_name": "go_mf",
    "query_version": "1",
    "roster_fingerprint": ROSTER_FINGERPRINT,
    "mapping_hash": MAPPING_HASH,
}

PLANNED_AT = "2026-10-22T09:00:00Z"
#: Spelled the way the store spells it -- ``datetime.isoformat()``, so
#: ``+00:00`` rather than ``Z``. The export normalises it.
DECIDED_AT = "2026-10-20T12:00:00+00:00"


def _hash(n: int) -> str:
    """A 64-char lowercase-hex entity hash, distinct per ``n``."""
    return f"{n:064x}"


# ---------------------------------------------------------------------------
# Verdicts, shaped like the ones the council store holds
# ---------------------------------------------------------------------------


def _ballot(*picks: Any, contentless: bool = False, status: str = "ok") -> Dict[str, Any]:
    return {
        "seat": "proposer",
        "status": status,
        "picks": [{"id": go_id, "confidence": confidence, "why": ""} for go_id, confidence in picks],
        "overall_confidence": "high",
        "no_functional_content": contentless,
    }


def _unanimous_verdict() -> Dict[str, Any]:
    """A decided item: three proposers agreed, the critic approved, no meta."""
    return {
        "item_id": "t0",
        "status": "ok",
        "selected_ids": ["GO:0004340"],
        "decline_reason": None,
        "final_confidence": "high",
        "decided_by": "unanimous",
        "ballots": [
            _ballot(("GO:0004340", "high")),
            _ballot(("GO:0004340", "high")),
            _ballot(("GO:0004340", "medium")),
        ],
        "votes": {"GO:0004340": 3},
        "n_valid": 3,
        "critic": {"verdict": "consistent", "contentless_input": False},
        "meta": None,
        "meta_skipped": True,
        "query": {"name": "go_mf", "version": "1"},
        "roster_fingerprint": ROSTER_FINGERPRINT,
        "decided_at": DECIDED_AT,
    }


def _meta_verdict(
    *,
    selected: List[str],
    decline_reason: Any,
    contentless: bool,
) -> Dict[str, Any]:
    """An item meta adjudicated -- the 24.1% path."""
    return {
        "item_id": "t0",
        "status": "ok",
        "selected_ids": list(selected),
        "decline_reason": decline_reason,
        "final_confidence": "medium",
        "decided_by": "meta",
        "ballots": [
            _ballot(("GO:0004340", "low"), contentless=contentless),
            _ballot(contentless=contentless),
            _ballot(("GO:0016301", "medium"), contentless=contentless),
        ],
        "votes": {"GO:0004340": 1, "GO:0016301": 1},
        "n_valid": 3,
        "critic": {"verdict": "ambiguous", "contentless_input": contentless},
        "meta": {
            "final_go_ids": list(selected),
            "final_confidence": "medium",
            "no_functional_content": contentless,
            "reasoning": "",
        },
        "meta_skipped": False,
        "query": {"name": "go_mf", "version": "1"},
        "roster_fingerprint": ROSTER_FINGERPRINT,
        "decided_at": DECIDED_AT,
    }


def _error_verdict() -> Dict[str, Any]:
    """What the store holds for an item whose calls all failed.

    ``status`` is ``error`` and ``selected_ids`` is empty -- which is exactly
    why it must not be published: an empty pick here means "nobody answered",
    not "the council chose nothing".
    """
    return {
        "item_id": "t2",
        "status": "error",
        "selected_ids": [],
        "decline_reason": None,
        "final_confidence": None,
        "decided_by": None,
        "ballots": [_ballot(status="error")],
        "votes": {},
        "n_valid": 0,
        "errors": [
            {
                "stage": "proposer",
                "seat": "proposer_1",
                "kind": "invalid_output",
                "message": "the reply carried no parsed answer",
            }
        ],
        "query": {"name": "go_mf", "version": "1"},
        "roster_fingerprint": ROSTER_FINGERPRINT,
        "decided_at": DECIDED_AT,
    }


def _settled(
    verdict: Dict[str, Any], *, status: str = "ok", job_id: str = "job-a"
) -> Dict[str, Any]:
    return {
        "status": status,
        "verdict": verdict,
        "decided_at": DECIDED_AT,
        "job_id": job_id,
    }


# ---------------------------------------------------------------------------
# The plan's two partition records
# ---------------------------------------------------------------------------


def _abstain(n: int, text: str, reason: str) -> Dict[str, Any]:
    return {
        "query_id": _hash(n),
        "text": text,
        "source": "RAST",
        "abstain_reason": reason,
        "decline_reason": f"abstained:{reason}",
    }


def _fanout(n: int, item_id: str, text: str) -> Dict[str, Any]:
    return {"query_id": _hash(n), "item_id": item_id, "text": text}


def _build(**kwargs: Any) -> Dict[str, Any]:
    kwargs.setdefault("abstain", [])
    kwargs.setdefault("fanout", [])
    kwargs.setdefault("verdicts", {})
    kwargs.setdefault("identity", IDENTITY)
    kwargs.setdefault("job_ids", ["job-a"])
    kwargs.setdefault("abstained_at", PLANNED_AT)
    return vx.export_rows(**kwargs)


def _by_query_id(built: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {row["query_id"]: row for row in built["rows"]}


# ---------------------------------------------------------------------------
# A verdict row
# ---------------------------------------------------------------------------


class TestVerdictRow:
    def test_a_decided_item_carries_its_terms_confidences_and_votes(self) -> None:
        built = _build(
            fanout=[_fanout(1, "t0", "glucokinase")],
            verdicts={"t0": _settled(_unanimous_verdict())},
        )
        (row,) = built["rows"]
        assert row["query_id"] == _hash(1)
        assert row["function_text"] == "glucokinase"
        assert row["go_ids"] == ["GO:0004340"]
        assert row["final_confidence"] == "high"
        assert row["decided_by"] == "unanimous"
        assert row["decline_reason"] is None
        assert row["n_valid_proposers"] == 3
        assert row["votes"] == [{"go_id": "GO:0004340", "votes": 3}]
        # Normalised: the store carries "+00:00" and PLAN.json carries "Z",
        # and both land in this one column.
        assert row["decided_at"] == "2026-10-20T12:00:00Z"
        assert row["retried"] is False

    def test_every_row_carries_the_run_identity(self) -> None:
        built = _build(
            fanout=[_fanout(1, "t0", "glucokinase")],
            verdicts={"t0": _settled(_unanimous_verdict())},
            job_ids=["job-a", "job-retry"],
        )
        (row,) = built["rows"]
        assert row["query_name"] == "go_mf"
        assert row["query_version"] == "1"
        assert row["roster_fingerprint"] == ROSTER_FINGERPRINT
        assert row["mapping_hash"] == MAPPING_HASH
        assert row["job_ids"] == ["job-a", "job-retry"]

    def test_every_column_of_the_declared_schema_is_present_on_every_row(self) -> None:
        built = _build(
            abstain=[_abstain(9, "hypothetical protein", "placeholder function")],
            fanout=[_fanout(1, "t0", "glucokinase")],
            verdicts={"t0": _settled(_unanimous_verdict())},
        )
        for row in built["rows"]:
            assert set(row) == set(vx.EXPORT_COLUMNS)

    def test_per_term_confidence_is_the_modal_ballot_confidence(self) -> None:
        """high/high/medium is high; the per-term figure comes off the ballots."""
        built = _build(
            fanout=[_fanout(1, "t0", "glucokinase")],
            verdicts={"t0": _settled(_unanimous_verdict())},
        )
        (row,) = built["rows"]
        assert row["go_confidences"] == ["high"]

    def test_a_term_no_ballot_picked_has_a_null_confidence_not_a_low_one(self) -> None:
        """Meta may select a menu entry the proposers did not vote for."""
        verdict = _meta_verdict(
            selected=["GO:0008150"], decline_reason=None, contentless=False
        )
        built = _build(
            fanout=[_fanout(1, "t0", "glucokinase")],
            verdicts={"t0": _settled(verdict)},
        )
        (row,) = built["rows"]
        assert row["go_ids"] == ["GO:0008150"]
        assert row["go_confidences"] == [None]

    def test_a_retry_pass_verdict_is_marked_retried(self) -> None:
        built = _build(
            fanout=[_fanout(1, "t0", "glucokinase")],
            verdicts={"t0": _settled(_unanimous_verdict(), job_id="job-retry")},
            job_ids=["job-a", "job-retry"],
            retry_job_ids=["job-retry"],
        )
        (row,) = built["rows"]
        assert row["retried"] is True
        assert built["counts"]["retried"] == 1

    def test_a_datetime_decided_at_is_normalised_to_an_iso_z_string(self) -> None:
        """The store hands back a datetime; the shard wants ISO-8601."""
        verdict = {**_unanimous_verdict()}
        verdict.pop("decided_at")
        settled = {
            "status": "ok",
            "verdict": verdict,
            "decided_at": datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc),
            "job_id": "job-a",
        }
        built = _build(
            fanout=[_fanout(1, "t0", "glucokinase")], verdicts={"t0": settled}
        )
        (row,) = built["rows"]
        assert row["decided_at"] == "2026-10-20T12:00:00Z"


# ---------------------------------------------------------------------------
# The three decline classes -- the point of this module
# ---------------------------------------------------------------------------


class TestDeclineClasses:
    def test_an_abstained_function_is_a_row_with_its_reason_and_no_terms(self) -> None:
        built = _build(
            abstain=[_abstain(9, "hypothetical protein", "placeholder function")]
        )
        (row,) = built["rows"]
        assert row["query_id"] == _hash(9)
        assert row["decline_reason"] == "abstained:placeholder function"
        assert row["go_ids"] == []
        assert row["go_confidences"] == []
        assert row["final_confidence"] is None
        assert row["decided_by"] is None
        assert row["n_valid_proposers"] == 0
        assert row["votes"] == []
        assert built["counts"]["abstained"] == 1

    def test_an_abstained_row_carries_a_non_null_decided_at(self) -> None:
        """A null ``observed_at`` makes the SHARD skip the row.

        ``resolve_observed_at`` refuses a null and the sharder counts it as
        ``SKIP_NULL_OBSERVED_AT``, so an abstained row with no timestamp would
        be dropped at shard time -- the exact disappearance recording the
        abstentions exists to prevent. The honest value is when retrieval
        declined, i.e. the plan's ``generated_at``.
        """
        built = _build(abstain=[_abstain(9, "hypothetical protein", "placeholder")])
        (row,) = built["rows"]
        assert row["decided_at"] == PLANNED_AT

    def test_an_abstained_rows_content_flag_is_null_not_false(self) -> None:
        """No seat saw the text, so the council expressed no judgement."""
        built = _build(abstain=[_abstain(9, "hypothetical protein", "placeholder")])
        (row,) = built["rows"]
        assert row["no_functional_content"] is None

    def test_no_candidate_matched_is_a_row_with_no_terms(self) -> None:
        verdict = _meta_verdict(
            selected=[], decline_reason="no_candidate_matched", contentless=False
        )
        built = _build(
            fanout=[_fanout(1, "t0", "ABC transporter, partial")],
            verdicts={"t0": _settled(verdict)},
        )
        (row,) = built["rows"]
        assert row["decline_reason"] == "no_candidate_matched"
        assert row["go_ids"] == []
        assert row["go_confidences"] == []
        assert row["no_functional_content"] is False
        assert row["decided_by"] == "meta"
        # The council DID work: the vote table and the seat count survive, so
        # a reader can tell this apart from an abstention that cost nothing.
        assert row["n_valid_proposers"] == 3
        assert row["votes"]
        assert built["counts"]["no_candidate_matched"] == 1

    def test_contentless_is_a_row_with_no_terms_and_the_flag_set(self) -> None:
        verdict = _meta_verdict(
            selected=[], decline_reason="contentless", contentless=True
        )
        built = _build(
            fanout=[_fanout(1, "t0", "yfiA")], verdicts={"t0": _settled(verdict)}
        )
        (row,) = built["rows"]
        assert row["decline_reason"] == "contentless"
        assert row["go_ids"] == []
        assert row["no_functional_content"] is True
        assert built["counts"]["contentless"] == 1

    def test_all_three_classes_appear_in_one_export(self) -> None:
        built = _build(
            abstain=[_abstain(9, "hypothetical protein", "placeholder function")],
            fanout=[
                _fanout(1, "t0", "ABC transporter, partial"),
                _fanout(2, "t1", "yfiA"),
            ],
            verdicts={
                "t0": _settled(
                    _meta_verdict(
                        selected=[],
                        decline_reason="no_candidate_matched",
                        contentless=False,
                    )
                ),
                "t1": _settled(
                    _meta_verdict(
                        selected=[], decline_reason="contentless", contentless=True
                    )
                ),
            },
        )
        rows = _by_query_id(built)
        assert rows[_hash(9)]["decline_reason"] == "abstained:placeholder function"
        assert rows[_hash(1)]["decline_reason"] == "no_candidate_matched"
        assert rows[_hash(2)]["decline_reason"] == "contentless"
        assert all(row["go_ids"] == [] for row in built["rows"])
        assert built["counts"]["rows"] == built["counts"]["inputs"] == 3
        assert built["counts"]["withheld"] == 0


# ---------------------------------------------------------------------------
# council_error -- the one thing withheld
# ---------------------------------------------------------------------------


class TestCouncilErrorIsWithheld:
    def test_an_errored_item_is_not_written(self) -> None:
        built = _build(
            fanout=[
                _fanout(1, "t0", "glucokinase"),
                _fanout(2, "t2", "glucose oxidase"),
            ],
            verdicts={
                "t0": _settled(_unanimous_verdict()),
                "t2": _settled(_error_verdict(), status="error"),
            },
        )
        assert [row["query_id"] for row in built["rows"]] == [_hash(1)]
        assert _hash(2) not in _by_query_id(built)

    def test_an_item_with_no_store_row_at_all_is_not_written(self) -> None:
        """Never settled -- still un-run, so still not a result."""
        built = _build(fanout=[_fanout(1, "t0", "glucokinase")], verdicts={})
        assert built["rows"] == []
        assert built["counts"]["withheld"] == 1

    def test_a_cancelled_item_is_not_written(self) -> None:
        built = _build(
            fanout=[_fanout(1, "t0", "glucokinase")],
            verdicts={"t0": _settled({}, status="cancelled")},
        )
        assert built["rows"] == []
        assert built["counts"]["withheld"] == 1

    def test_rows_plus_withheld_equals_inputs(self) -> None:
        """The arithmetic is stated, not inferred.

        A withheld item is the interesting number: "exported 19,268" is not a
        complete reconciliation when 19,276 were submitted.
        """
        built = _build(
            abstain=[_abstain(9, "hypothetical protein", "placeholder")],
            fanout=[
                _fanout(1, "t0", "glucokinase"),
                _fanout(2, "t2", "glucose oxidase"),
            ],
            verdicts={
                "t0": _settled(_unanimous_verdict()),
                "t2": _settled(_error_verdict(), status="error"),
            },
        )
        counts = built["counts"]
        assert counts["inputs"] == 3
        assert counts["rows"] == 2
        assert counts["withheld"] == 1
        assert counts["rows"] + counts["withheld"] == counts["inputs"]


# ---------------------------------------------------------------------------
# The join key
# ---------------------------------------------------------------------------


class TestTheJoinKey:
    def test_two_functions_sharing_a_text_get_two_rows_from_one_answer(self) -> None:
        """One paid answer, fanned back out -- never collapsed onto the text."""
        built = _build(
            fanout=[
                _fanout(1, "t0", "glucokinase"),
                _fanout(2, "t0", "glucokinase"),
            ],
            verdicts={"t0": _settled(_unanimous_verdict())},
        )
        assert sorted(row["query_id"] for row in built["rows"]) == [
            _hash(1),
            _hash(2),
        ]
        assert all(row["go_ids"] == ["GO:0004340"] for row in built["rows"])
        assert built["counts"]["inputs"] == 2

    def test_the_query_id_is_never_the_councils_own_item_id_or_entity_hash(self) -> None:
        built = _build(
            fanout=[_fanout(1, "t0", "glucokinase")],
            verdicts={"t0": _settled(_unanimous_verdict())},
        )
        (row,) = built["rows"]
        assert row["query_id"] == _hash(1)
        assert row["query_id"] != "t0"

    def test_a_record_with_no_query_id_is_refused_not_guessed(self) -> None:
        with pytest.raises(ValueError, match="no query_id"):
            _build(fanout=[{"item_id": "t0", "text": "glucokinase"}])
        with pytest.raises(ValueError, match="no query_id"):
            _build(abstain=[{"text": "hypothetical protein", "abstain_reason": "x"}])

    def test_one_query_id_in_both_partitions_is_refused(self) -> None:
        with pytest.raises(ValueError, match="both the abstain and the fanout"):
            _build(
                abstain=[_abstain(1, "glucokinase", "placeholder")],
                fanout=[_fanout(1, "t0", "glucokinase")],
            )


# ---------------------------------------------------------------------------
# The run identity
# ---------------------------------------------------------------------------


class TestRunIdentity:
    @pytest.mark.parametrize("missing", list(vx.EXPORT_IDENTITY_FIELDS))
    def test_an_incomplete_identity_is_refused(self, missing: str) -> None:
        identity = {**IDENTITY, missing: ""}
        with pytest.raises(ValueError, match="omits"):
            _build(identity=identity)

    def test_the_roster_fingerprint_is_recorded_but_is_not_the_parameter_set(
        self,
    ) -> None:
        """Provenance, not a key -- re-routing a seat must re-select nothing."""
        assert "roster_fingerprint" in vx.EXPORT_IDENTITY_FIELDS
        assert "bench_fingerprint" not in vx.EXPORT_COLUMNS


# ---------------------------------------------------------------------------
# The content flag
# ---------------------------------------------------------------------------


class TestNoFunctionalContent:
    def test_a_meta_decided_item_reports_metas_own_flag(self) -> None:
        verdict = _meta_verdict(
            selected=["GO:0004340"], decline_reason=None, contentless=True
        )
        assert vx.no_functional_content(verdict) is True

    def test_a_unanimous_item_uses_the_councils_own_majority_rule(self) -> None:
        """Reused from ``decide.content_flag``, not re-implemented here.

        Two of three proposers flagged it, so the majority carries without
        the critic being consulted.
        """
        verdict = {
            **_unanimous_verdict(),
            "ballots": [
                _ballot(("GO:0004340", "high"), contentless=True),
                _ballot(("GO:0004340", "high"), contentless=True),
                _ballot(("GO:0004340", "medium"), contentless=False),
            ],
        }
        assert vx.no_functional_content(verdict) is True

    def test_a_unanimous_tie_is_broken_by_the_critic(self) -> None:
        verdict = {
            **_unanimous_verdict(),
            "n_valid": 2,
            "ballots": [
                _ballot(("GO:0004340", "high"), contentless=True),
                _ballot(("GO:0004340", "high"), contentless=False),
            ],
            "critic": {"verdict": "consistent", "contentless_input": True},
        }
        assert vx.no_functional_content(verdict) is True

    def test_a_deterministically_empty_menu_reports_null(self) -> None:
        """Nobody spoke, so ``False`` would be a claim nobody made."""
        verdict = {
            "selected_ids": [],
            "decline_reason": "no_candidates",
            "decided_by": "deterministic_empty",
            "n_valid": 0,
        }
        assert vx.no_functional_content(verdict) is None

    def test_a_contentless_decline_reports_true_without_reading_a_reply(self) -> None:
        assert vx.no_functional_content({"decline_reason": "contentless"}) is True


# ---------------------------------------------------------------------------
# verdicts_by_item
# ---------------------------------------------------------------------------


class TestVerdictsByItem:
    def test_several_chunk_jobs_fold_into_one_mapping(self) -> None:
        settled: Dict[str, Dict[str, Any]] = {}
        vx.verdicts_by_item(
            [{"item_id": "t0", "status": "ok", "verdict": {"selected_ids": ["GO:1"]}}],
            job_id="job-a",
            into=settled,
        )
        vx.verdicts_by_item(
            [{"item_id": "t1", "status": "ok", "verdict": {"selected_ids": ["GO:2"]}}],
            job_id="job-b",
            into=settled,
        )
        assert sorted(settled) == ["t0", "t1"]
        assert settled["t0"]["job_id"] == "job-a"
        assert settled["t1"]["job_id"] == "job-b"

    def test_a_later_job_supersedes_an_earlier_one_for_the_same_item(self) -> None:
        """The retry pass's answer is the run's answer for a retried item."""
        settled: Dict[str, Dict[str, Any]] = {}
        vx.verdicts_by_item(
            [{"item_id": "t0", "status": "error", "verdict": {}}],
            job_id="job-a",
            into=settled,
        )
        vx.verdicts_by_item(
            [{"item_id": "t0", "status": "ok", "verdict": {"selected_ids": ["GO:1"]}}],
            job_id="job-retry",
            into=settled,
        )
        assert settled["t0"]["status"] == "ok"
        assert settled["t0"]["job_id"] == "job-retry"

    def test_a_rows_own_job_id_wins_over_the_fallback(self) -> None:
        settled = vx.verdicts_by_item(
            [{"item_id": "t0", "status": "ok", "verdict": {}, "job_id": "job-z"}],
            job_id="job-a",
        )
        assert settled["t0"]["job_id"] == "job-z"


# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------


class TestTheParquetFile:
    """PARQUET, not JSONL and not gzipped JSONL.

    ``FileSourceAdapter._format()`` accepts exactly parquet | jsonl | tsv and
    ``_iter_jsonl`` opens with a plain ``path.open("r")``, so gzipped JSONL is
    unreadable by the manifest path; ``estimate_rows()`` is exact from a
    parquet footer and returns ``None`` for text; and ``read_batch_table``
    refuses anything but parquet downstream.
    """

    @pytest.fixture(autouse=True)
    def _needs_pyarrow(self) -> None:
        pytest.importorskip("pyarrow")

    def _write(self, tmp_path: Any, built: Dict[str, Any]) -> Dict[str, Any]:
        return vx.write_verdict_parquet(built["rows"], tmp_path / "verdicts.parquet")

    def test_the_row_count_equals_the_plans_input_count(self, tmp_path: Any) -> None:
        """One row per INPUT query_id, counted off the file's own footer.

        Read the way the manifest reads it:
        ``FileSourceAdapter.estimate_rows()`` is the parquet footer's
        ``num_rows``, which is why this stage writes parquet at all.
        """
        import pyarrow.parquet as pq

        built = _build(
            abstain=[
                _abstain(8, "hypothetical protein", "placeholder function"),
                _abstain(9, "conserved protein", "placeholder function"),
            ],
            fanout=[
                _fanout(1, "t0", "glucokinase"),
                _fanout(2, "t0", "glucokinase"),
                _fanout(3, "t1", "ABC transporter, partial"),
                _fanout(4, "t2", "yfiA"),
            ],
            verdicts={
                "t0": _settled(_unanimous_verdict()),
                "t1": _settled(
                    _meta_verdict(
                        selected=[],
                        decline_reason="no_candidate_matched",
                        contentless=False,
                    )
                ),
                "t2": _settled(
                    _meta_verdict(
                        selected=[], decline_reason="contentless", contentless=True
                    )
                ),
            },
        )
        assert built["counts"]["inputs"] == 6
        assert built["counts"]["withheld"] == 0
        written = self._write(tmp_path, built)
        assert written["rows"] == 6
        footer = pq.ParquetFile(written["path"]).metadata.num_rows
        assert footer == built["counts"]["inputs"] == 6

    def test_the_schema_is_declared_rather_than_inferred(self, tmp_path: Any) -> None:
        """An all-decline export must not infer ``list<null>`` for go_ids."""
        import pyarrow as pa
        import pyarrow.parquet as pq

        built = _build(
            abstain=[_abstain(9, "hypothetical protein", "placeholder function")]
        )
        written = self._write(tmp_path, built)
        schema = pq.read_table(written["path"]).schema
        assert schema.names == list(vx.EXPORT_COLUMNS)
        assert schema.field("go_ids").type == pa.list_(pa.string())
        assert schema.field("n_valid_proposers").type == pa.int32()
        assert schema.field("no_functional_content").type == pa.bool_()

    def test_an_empty_export_still_writes_a_readable_typed_file(
        self, tmp_path: Any
    ) -> None:
        import pyarrow.parquet as pq

        written = self._write(tmp_path, _build())
        assert written["rows"] == 0
        assert pq.read_table(written["path"]).schema.names == list(vx.EXPORT_COLUMNS)

    def test_the_vote_table_reads_back_as_dicts_not_tuples(
        self, tmp_path: Any
    ) -> None:
        """Which is why it is a list of structs and not a parquet map.

        ``MapArray.to_pylist()`` yields ``(key, value)`` tuples, and
        ``FileSourceAdapter._iter_parquet`` is exactly ``batch.to_pylist()``
        -- so a map column would hand the shard tuples to put in a payload.
        """
        import pyarrow.parquet as pq

        built = _build(
            fanout=[_fanout(1, "t0", "glucokinase")],
            verdicts={"t0": _settled(_unanimous_verdict())},
        )
        written = self._write(tmp_path, built)
        (row,) = pq.read_table(written["path"]).to_pylist()
        assert row["votes"] == [{"go_id": "GO:0004340", "votes": 3}]
        assert row["go_ids"] == ["GO:0004340"]
        assert row["query_id"] == _hash(1)

    def test_the_write_is_atomic(self, tmp_path: Any) -> None:
        """No ``.partial`` survives, and a reader never sees a half file."""
        built = _build(
            abstain=[_abstain(9, "hypothetical protein", "placeholder function")]
        )
        written = self._write(tmp_path, built)
        assert list(tmp_path.glob("*.partial")) == []
        assert written["bytes"] > 0
