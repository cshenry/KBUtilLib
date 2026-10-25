"""The council reference on a published verdict row, end to end.

``clearinghouse.function_result`` keeps the high-level assertion; the FULL
deliberation -- every proposer ballot with its free-text "why", the critic
audit, the meta adjudication, the candidate menu each seat was shown -- lives
in ``kbaseincubator.council``. The pointer between them is
``council = {store_id, job_id, item_id}``, and this module is the guard over
it.

All three parts are load-bearing. ``items`` is keyed ``(job_id, item_id)``, so
``job_id`` alone names a job of 19,276 items rather than a function; and
``job_id`` is unique only WITHIN one store, so without ``store_id`` the triple
identifies nothing. The nullable case is the POINT rather than an edge: a
retrieval abstention has NO council item, so a non-null triple there would
fabricate provenance.

WHAT IS REAL HERE. ``TestEndToEndThroughARealStore`` builds a REAL SQLite
council store with the real store code, declares its identity with the real
``assign_store_identity``, reads the settled rows back with the real
``results()``, drives the REAL ``export_rows`` and the REAL
``write_verdict_parquet``, and then asserts the three columns and the
null-abstention row BY READING THE FILE BACK WITH PYARROW -- not from an
in-memory dict the test assembled. The rest of the module is a guard over a
representative payload set, because these are the assertions that have to
survive future edits to the exporter.

``pyarrow`` is imported at module scope (``write_verdict_parquet`` imports it
inside the function to keep ``verdicts`` import-cheap), which is why this
module skips rather than fails on a host without it -- the same arrangement
``tests/domains/test_verdict_export.py`` uses.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

pytest.importorskip("pyarrow", reason="the verdict export writes parquet")
pytest.importorskip(
    "sqlalchemy",
    reason="the council store needs the `council` extra: pip install 'kbutillib[council]'",
)

import pyarrow.parquet as pq  # noqa: E402

from kbutillib.domains.ai.council.store import open_store  # noqa: E402
from kbutillib.domains.genome.ontology_mapping import verdicts as vx  # noqa: E402

#: The store's DECLARED identity -- never a path, a hostname or a uuid.
STORE_ID = "poplar/council-1"

MAPPING_HASH = "e" * 64
ROSTER_FINGERPRINT = "r" * 64

IDENTITY: Dict[str, Any] = {
    "query_name": "go_mf",
    "query_version": "4",
    "roster_fingerprint": ROSTER_FINGERPRINT,
    "mapping_hash": MAPPING_HASH,
}

PLANNED_AT = "2026-10-22T09:00:00Z"


def _hash(n: int) -> str:
    """A plausible clearinghouse ``query_id`` -- the join key, carried through."""
    return f"{n:064x}"


def _verdict(*, go_ids: List[str], decline: Any = None) -> Dict[str, Any]:
    return {
        "selected_ids": go_ids,
        "final_confidence": "high" if go_ids else None,
        "decided_by": "unanimous",
        "decline_reason": decline,
        "n_valid": 3,
        "votes": [{"go_id": gid, "votes": 3} for gid in go_ids],
        "ballots": [
            {
                "status": "ok",
                "picks": [{"id": gid, "confidence": "high"} for gid in go_ids],
                "no_functional_content": False,
            }
            for _ in range(3)
        ],
        "decided_at": "2026-10-22T10:00:00+00:00",
    }


def _settled(verdict: Dict[str, Any], *, job_id: str) -> Dict[str, Any]:
    return {
        "status": "ok",
        "verdict": verdict,
        "decided_at": "2026-10-22T10:00:00+00:00",
        "job_id": job_id,
    }


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
    kwargs.setdefault("store_id", STORE_ID)
    return vx.export_rows(**kwargs)


def _representative() -> Dict[str, Any]:
    """One row per published class, decided and declined, in one export.

    A guard over a representative SET rather than one example: the council
    reference has to behave identically on a selected term, on both council
    decline classes, on a retried row and on a retrieval abstention, and the
    only one of those that is nullable is the last.
    """
    return _build(
        abstain=[_abstain(5, "hypothetical protein", "placeholder function")],
        fanout=[
            _fanout(1, "item-term", "Glucokinase (EC 2.7.1.2)"),
            _fanout(2, "item-nomatch", "Unusual oxidoreductase"),
            _fanout(3, "item-contentless", "conserved protein"),
            _fanout(4, "item-retried", "Alcohol dehydrogenase"),
        ],
        verdicts={
            "item-term": _settled(
                _verdict(go_ids=["GO:0004340"]), job_id="job-chunk-0"
            ),
            "item-nomatch": _settled(
                _verdict(go_ids=[], decline="no_candidate_matched"),
                job_id="job-chunk-0",
            ),
            "item-contentless": _settled(
                _verdict(go_ids=[], decline="contentless"), job_id="job-chunk-0"
            ),
            "item-retried": _settled(
                _verdict(go_ids=["GO:0004022"]), job_id="job-retry"
            ),
        },
        job_ids=["job-chunk-0", "job-retry"],
        retry_job_ids=["job-retry"],
    )


def _by_query_id(built: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {row["query_id"]: row for row in built["rows"]}


# --------------------------------------------------------------------------
# The declared schema
# --------------------------------------------------------------------------


class TestTheDeclaredSchema:
    def test_the_three_reference_fields_are_declared_once(self):
        assert vx.COUNCIL_REFERENCE_FIELDS == ("store_id", "job_id", "item_id")

    def test_council_and_status_are_export_columns(self):
        assert "council" in vx.EXPORT_COLUMNS
        assert "status" in vx.EXPORT_COLUMNS

    def test_every_pre_existing_column_keeps_its_name_and_position(self):
        # The shipped preset names columns out of EXPORT_COLUMNS and a reader
        # of an older parquet must still find them where they were, so the
        # two new columns are APPENDED rather than interleaved.
        before = (
            "query_id",
            "function_text",
            "genomes",
            "go_ids",
            "go_confidences",
            "final_confidence",
            "decided_by",
            "no_functional_content",
            "decline_reason",
            "n_valid_proposers",
            "votes",
            "decided_at",
            "query_name",
            "query_version",
            "roster_fingerprint",
            "route_fingerprints",
            "mapping_hash",
            "job_ids",
            "retried",
        )
        assert vx.EXPORT_COLUMNS[: len(before)] == before

    def test_the_council_column_is_a_declared_struct_of_three_strings(self):
        import pyarrow as pa

        field = vx.export_schema().field("council")
        assert pa.types.is_struct(field.type)
        assert [sub.name for sub in field.type] == list(
            vx.COUNCIL_REFERENCE_FIELDS
        )
        assert all(pa.types.is_string(sub.type) for sub in field.type)

    def test_the_struct_is_nullable_so_an_abstention_can_say_nothing(self):
        assert vx.export_schema().field("council").nullable

    def test_the_parameter_set_fields_are_unchanged(self):
        # The parent PRD fixed these; the council reference rides in the
        # PAYLOAD and must not touch the partition key.
        assert vx.EXPORT_IDENTITY_FIELDS == (
            "query_name",
            "query_version",
            "roster_fingerprint",
            "mapping_hash",
        )


# --------------------------------------------------------------------------
# The reference on a representative payload set
# --------------------------------------------------------------------------


class TestCouncilDecidedRows:
    def test_every_council_decided_row_carries_all_three_fields_non_null(self):
        rows = _by_query_id(_representative())
        for n in (1, 2, 3, 4):
            council = rows[_hash(n)]["council"]
            assert council is not None, n
            assert set(council) == set(vx.COUNCIL_REFERENCE_FIELDS)
            assert all(council[field] for field in vx.COUNCIL_REFERENCE_FIELDS)

    def test_the_reference_names_the_winning_item_not_a_list(self):
        rows = _by_query_id(_representative())
        assert rows[_hash(1)]["council"]["item_id"] == "item-term"
        assert rows[_hash(1)]["council"]["job_id"] == "job-chunk-0"
        # job_ids stays the LIST of every job the export read -- the two are
        # different questions and both are published.
        assert rows[_hash(1)]["job_ids"] == ["job-chunk-0", "job-retry"]

    def test_a_retried_row_names_the_retry_job_that_won(self):
        # verdicts_by_item lets a later job overwrite an earlier one's entry,
        # so the winning job is the retry's -- 105 of the 113 items that
        # failed on a truncated ARGO response came back on a plain retry.
        row = _by_query_id(_representative())[_hash(4)]
        assert row["retried"] is True
        assert row["council"]["job_id"] == "job-retry"

    def test_both_council_decline_classes_still_carry_the_reference(self):
        # no_candidate_matched and contentless ARE council decisions: the
        # council looked and said so, and that deliberation is archived.
        rows = _by_query_id(_representative())
        assert rows[_hash(2)]["decline_reason"] == "no_candidate_matched"
        assert rows[_hash(2)]["council"]["item_id"] == "item-nomatch"
        assert rows[_hash(3)]["decline_reason"] == "contentless"
        assert rows[_hash(3)]["council"]["item_id"] == "item-contentless"

    def test_the_store_id_is_the_declared_one_on_every_decided_row(self):
        rows = _by_query_id(_representative())
        for n in (1, 2, 3, 4):
            assert rows[_hash(n)]["council"]["store_id"] == STORE_ID


class TestRetrievalAbstentionsCarryNull:
    def test_an_abstention_carries_council_null(self):
        row = _by_query_id(_representative())[_hash(5)]
        assert row["council"] is None

    def test_an_abstention_retains_its_decline_fields(self):
        row = _by_query_id(_representative())[_hash(5)]
        assert row["decline_reason"] == "abstained:placeholder function"
        assert row["go_ids"] == []
        assert row["go_confidences"] == []
        assert row["final_confidence"] is None
        assert row["decided_by"] is None
        # NOT False: the council never saw this text, so "the council said
        # there is no function here" would be a claim nobody made.
        assert row["no_functional_content"] is None
        assert row["n_valid_proposers"] == 0
        assert row["votes"] == []
        # Still carries a real timestamp -- the shard SKIPS a row whose
        # observed_at is null, which would drop every abstention.
        assert row["decided_at"] == PLANNED_AT

    def test_an_abstention_carries_a_null_status_not_an_empty_string(self):
        # It never had a council item, so it never had an item status.
        assert _by_query_id(_representative())[_hash(5)]["status"] is None

    def test_an_abstain_only_export_needs_no_store_id_at_all(self):
        # Nothing to fabricate and nothing to resolve: every row is null.
        built = vx.export_rows(
            abstain=[_abstain(5, "hypothetical protein", "placeholder")],
            fanout=[],
            verdicts={},
            identity=IDENTITY,
            job_ids=["job-a"],
            abstained_at=PLANNED_AT,
        )
        (row,) = built["rows"]
        assert row["council"] is None


class TestStatusAndRetriedAreAlwaysPresent:
    def test_both_are_present_on_every_row_of_every_class(self):
        for row in _representative()["rows"]:
            assert "status" in row
            assert "retried" in row

    def test_a_decided_row_reports_the_council_items_status(self):
        rows = _by_query_id(_representative())
        for n in (1, 2, 3, 4):
            assert rows[_hash(n)]["status"] == "ok"

    def test_retried_is_a_bool_on_every_row(self):
        for row in _representative()["rows"]:
            assert isinstance(row["retried"], bool)


class TestNeverTheCouncilsOwnEntityHash:
    def test_no_row_carries_the_councils_own_entity_hash(self):
        # Measured 2026-10-21: items.entity_hash matched 0 of 19,268
        # clearinghouse hashes. The published row's key is query_id -- the
        # clearinghouse's own digest, carried through -- and the council's
        # hash must never appear on it under any name.
        for row in _representative()["rows"]:
            assert "entity_hash" not in row
            assert "council_entity_hash" not in row
            council = row["council"]
            if council is not None:
                assert "entity_hash" not in council

    def test_no_declared_column_is_named_for_a_council_hash(self):
        for name in vx.EXPORT_COLUMNS:
            assert "entity_hash" not in name

    def test_the_reference_is_ids_only_and_carries_no_digest_fields(self):
        assert vx.COUNCIL_REFERENCE_FIELDS == ("store_id", "job_id", "item_id")


class TestAPartialReferenceIsRefused:
    def test_a_decided_row_without_a_store_id_is_refused(self):
        with pytest.raises(ValueError) as excinfo:
            vx.export_rows(
                abstain=[],
                fanout=[_fanout(1, "item-term", "Glucokinase")],
                verdicts={
                    "item-term": _settled(
                        _verdict(go_ids=["GO:0004340"]), job_id="job-a"
                    )
                },
                identity=IDENTITY,
                job_ids=["job-a"],
                abstained_at=PLANNED_AT,
            )
        assert "store_id" in str(excinfo.value)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"store_id": "", "job_id": "j", "item_id": "i"},
            {"store_id": "s", "job_id": "", "item_id": "i"},
            {"store_id": "s", "job_id": "j", "item_id": ""},
            {"store_id": "   ", "job_id": "j", "item_id": "i"},
        ],
    )
    def test_council_reference_refuses_any_empty_part(self, kwargs):
        with pytest.raises(ValueError):
            vx.council_reference(**kwargs)

    def test_council_reference_returns_exactly_the_three_fields(self):
        assert vx.council_reference(store_id="s", job_id="j", item_id="i") == {
            "store_id": "s",
            "job_id": "j",
            "item_id": "i",
        }


# --------------------------------------------------------------------------
# Parquet round trip
# --------------------------------------------------------------------------


class TestParquetRoundTrip:
    def test_the_three_new_columns_round_trip_through_the_file(self, tmp_path):
        built = _representative()
        out = tmp_path / "verdicts.parquet"
        vx.write_verdict_parquet(built["rows"], out)

        table = pq.read_table(out)
        assert "council" in table.schema.names
        assert "status" in table.schema.names
        read_back = {row["query_id"]: row for row in table.to_pylist()}

        decided = read_back[_hash(1)]
        assert decided["council"] == {
            "store_id": STORE_ID,
            "job_id": "job-chunk-0",
            "item_id": "item-term",
        }
        assert decided["status"] == "ok"

        abstained = read_back[_hash(5)]
        assert abstained["council"] is None
        assert abstained["status"] is None

    def test_an_all_abstention_export_still_declares_the_struct(self, tmp_path):
        # The failure this guards: an inferred type would be ``null`` for a
        # file in which every row abstained, and a shard reading two such
        # files would see two different schemas for one source.
        import pyarrow as pa

        built = vx.export_rows(
            abstain=[_abstain(5, "hypothetical protein", "placeholder")],
            fanout=[],
            verdicts={},
            identity=IDENTITY,
            job_ids=["job-a"],
            abstained_at=PLANNED_AT,
        )
        out = tmp_path / "abstentions.parquet"
        vx.write_verdict_parquet(built["rows"], out)
        field = pq.read_schema(out).field("council")
        assert pa.types.is_struct(field.type)
        assert [sub.name for sub in field.type] == list(
            vx.COUNCIL_REFERENCE_FIELDS
        )


# --------------------------------------------------------------------------
# End to end, through a REAL council store
# --------------------------------------------------------------------------


class TestEndToEndThroughARealStore:
    """The real store, the real exporter, the real parquet, read back.

    No fake store object and no in-memory settled dict: the settled rows come
    out of a real SQLite council store via the real ``results()``, and the
    assertions are made against the FILE.
    """

    def _store(self, tmp_path: Path):
        store = open_store(str(tmp_path / "council" / "council.sqlite"))
        store.init_schema()
        store.assign_store_identity(STORE_ID, declared_by="chenry")
        store.put_query_type(
            name="go_mf",
            version="4",
            spec={"roles": {}},
            created_by="chenry",
        )
        return store

    def _job(self, store, job_id: str, item_ids: List[str]) -> None:
        """A real job with real items, through the real ``create_job``."""
        store.create_job(
            {
                "job_id": job_id,
                "query_name": "go_mf",
                "query_version": "4",
                "submitted_by": "chenry",
                "submitted_at": datetime.now(timezone.utc),
                "n_items": len(item_ids),
            },
            [
                {"item_id": item_id, "item_order": index}
                for index, item_id in enumerate(item_ids)
            ],
            # One proposer call per item, which is what create_job writes as
            # the job's first round. The export reads verdicts, not calls, so
            # the call rows only have to exist.
            [
                {
                    "call_id": f"{job_id}-{item_id}-p0",
                    "job_id": job_id,
                    "item_id": item_id,
                    "role": "proposer",
                    "seat_name": "proposer_0",
                    "endpoint": "fixture",
                    "endpoint_kind": "http",
                    "submitted_by": "chenry",
                    "created_at": datetime.now(timezone.utc),
                }
                for item_id in item_ids
            ],
        )

    def test_the_file_carries_the_real_stores_declared_identity(self, tmp_path):
        store = self._store(tmp_path)
        try:
            self._job(store, "job-chunk-0", ["item-term", "item-nomatch"])
            self._job(store, "job-retry", ["item-retried"])
            store.settle_item(
                "job-chunk-0",
                "item-term",
                "ok",
                verdict=_verdict(go_ids=["GO:0004340"]),
            )
            store.settle_item(
                "job-chunk-0",
                "item-nomatch",
                "ok",
                verdict=_verdict(go_ids=[], decline="no_candidate_matched"),
            )
            store.settle_item(
                "job-retry",
                "item-retried",
                "ok",
                verdict=_verdict(go_ids=["GO:0004022"]),
            )

            # The identity comes OFF THE STORE, not out of the test.
            declared = store.store_identity()["store_id"]

            settled: Dict[str, Dict[str, Any]] = {}
            for job_id in ("job-chunk-0", "job-retry"):
                vx.verdicts_by_item(
                    store.results(job_id), job_id=job_id, into=settled
                )

            built = vx.export_rows(
                abstain=[_abstain(9, "hypothetical protein", "placeholder")],
                fanout=[
                    _fanout(1, "item-term", "Glucokinase (EC 2.7.1.2)"),
                    _fanout(2, "item-nomatch", "Unusual oxidoreductase"),
                    _fanout(3, "item-retried", "Alcohol dehydrogenase"),
                ],
                verdicts=settled,
                identity=IDENTITY,
                job_ids=["job-chunk-0", "job-retry"],
                retry_job_ids=["job-retry"],
                abstained_at=PLANNED_AT,
                store_id=declared,
            )
            out = tmp_path / "verdicts.parquet"
            vx.write_verdict_parquet(built["rows"], out)

            # EVERY assertion below reads the FILE.
            read_back = {
                row["query_id"]: row for row in pq.read_table(out).to_pylist()
            }
            assert len(read_back) == 4

            decided = read_back[_hash(1)]
            assert decided["council"]["store_id"] == STORE_ID
            assert decided["council"]["job_id"] == "job-chunk-0"
            assert decided["council"]["item_id"] == "item-term"
            assert decided["status"] == "ok"

            retried = read_back[_hash(3)]
            assert retried["retried"] is True
            assert retried["council"]["job_id"] == "job-retry"

            abstained = read_back[_hash(9)]
            assert abstained["council"] is None
            assert abstained["status"] is None
            assert abstained["decline_reason"] == "abstained:placeholder"
        finally:
            store.close()

    def test_the_triple_joins_back_to_the_real_stores_item_row(self, tmp_path):
        store = self._store(tmp_path)
        try:
            self._job(store, "job-chunk-0", ["item-term"])
            store.settle_item(
                "job-chunk-0",
                "item-term",
                "ok",
                verdict=_verdict(go_ids=["GO:0004340"]),
            )
            settled = vx.verdicts_by_item(
                store.results("job-chunk-0"), job_id="job-chunk-0"
            )
            built = vx.export_rows(
                abstain=[],
                fanout=[_fanout(1, "item-term", "Glucokinase")],
                verdicts=settled,
                identity=IDENTITY,
                job_ids=["job-chunk-0"],
                abstained_at=PLANNED_AT,
                store_id=store.store_identity()["store_id"],
            )
            out = tmp_path / "verdicts.parquet"
            vx.write_verdict_parquet(built["rows"], out)
            (row,) = pq.read_table(out).to_pylist()
            council = row["council"]

            # The whole point of the reference: read the triple off the
            # published file and find the deliberation it names.
            assert council["store_id"] == store.store_identity()["store_id"]
            items = store.get_items(council["job_id"])
            matched = [
                item for item in items if item["item_id"] == council["item_id"]
            ]
            assert len(matched) == 1
            assert matched[0]["status"] == "ok"
            assert json.loads(json.dumps(matched[0]["verdict"]))[
                "selected_ids"
            ] == ["GO:0004340"]
        finally:
            store.close()
