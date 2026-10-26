"""``berdl.council_publish`` -- the LAKEHOUSE-SOURCED clearinghouse publisher.

NOTHING IS INJECTED AND NOTHING IS PATCHED. Every test below builds a REAL
council store through ``SqliteStore``'s own API, snapshots it with the REAL
``SqliteStore.snapshot()``, archives it with the REAL
``council_archive.archive_snapshot`` into a REAL ``SqliteCouncilSink``,
declares dispositions through the REAL ``council_disposition.declare`` +
``write_dispositions``, builds and publishes a REAL bundle through
``council_bundle.build_bundle`` + ``publish_bundle``, and then asks the
publisher. ``require_publishable``, ``require_published`` and
``require_complete_delivery`` are NEVER stubbed: they are the gates being
tested, and a test that injected a fake of its own gate would prove nothing.

THE ZERO-JOIN TEST'S FIXTURE IS BUILT SO A POST-JOIN ASSERTION WOULD PASS
VACUOUSLY -- a non-empty submitted inventory whose keys resolve nothing -- and
it asserts BOTH that the guard refuses and that the naive check over the
join's result is trivially true. That is the whole point of the guard running
on the source side.

NOT A CLAIM ABOUT A LIVE POD. Every step here runs off-pod: it reads a sink,
writes a parquet and writes a shard directory. The LOAD is the consumer
machine's verb and the BERDL transport has been broken on kbhub since the
2026-10-19 image (KBUtilLib#91); the receipt reports the load as awaiting
operator acceptance and no test here claims otherwise.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip(
    "sqlalchemy",
    reason="the council store needs the `council` extra: pip install 'kbutillib[council]'",
)
pytest.importorskip("pyarrow")

import pyarrow.parquet as pq  # noqa: E402
from click.testing import CliRunner  # noqa: E402

from kbutillib.cli import main  # noqa: E402
from kbutillib.domains.ai.council.store.sqlite import SqliteStore  # noqa: E402
from kbutillib.domains.genome.ontology_mapping import council_bundle as cb  # noqa: E402
from kbutillib.domains.genome.ontology_mapping import verdicts as vx  # noqa: E402
from kbutillib.domains.identity.parameter_sets import parameter_set_hash  # noqa: E402
from kbutillib.domains.kbase.berdl import clearinghouse_ingest as ci  # noqa: E402
from kbutillib.domains.kbase.berdl import council_disposition as cd  # noqa: E402
from kbutillib.domains.kbase.berdl import council_publish as lp  # noqa: E402
from kbutillib.domains.kbase.berdl.council_archive import (  # noqa: E402
    DeliveryIncompleteError,
    archive_snapshot,
)
from kbutillib.domains.kbase.berdl.council_schema import (  # noqa: E402
    COUNCIL_ENTITY_HASH_COLUMN,
)
from kbutillib.domains.kbase.berdl.council_sink import SqliteCouncilSink  # noqa: E402

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
STORE_ID = "poplar/council-1"
ROSTER = hashlib.sha256(b"roster").hexdigest()
BENCH = hashlib.sha256(b"bench").hexdigest()
MAPPING = hashlib.sha256(b"mapping").hexdigest()
QUERY = "go_mf@4"
PARAMETER_SET = {
    "query": QUERY,
    "bench_fingerprint": BENCH,
    "mapping_hash": MAPPING,
}
PARAMETER_SET_HASH = parameter_set_hash(PARAMETER_SET)


def qid(label: str) -> str:
    """A clearinghouse ``entity_hash``-shaped key: 64 lowercase hex."""
    return hashlib.sha256(f"query:{label}".encode("utf-8")).hexdigest()


#: The campaign's four submitted inputs: three fanout pairs over TWO items
#: (``i2`` fans out to two query_ids, which is why a row count is not an item
#: count) and one retrieval abstention, which has no council item at all.
F1, F2, F2B, ABSTAIN = qid("f1"), qid("f2"), qid("f2b"), qid("abstain")

#: The abstention's function text, as the plan's abstain record carries it.
#: A retrieval abstention has no council item, so this is the ONLY durable
#: source for it -- which is why the bundle carries it from v2 on.
ABSTAIN_TEXT = "DUF1234 family protein"

#: A fanout key whose item never settled -- the "eight residual items" case.
F_UNRESOLVED = qid("f-unresolved")


def _call(job_id: str, item_id: str, **extra: Any) -> dict:
    row = {
        "job_id": job_id,
        "item_id": item_id,
        "role": "proposer",
        "seat_name": "p1",
        "endpoint": "argo",
        "endpoint_kind": "openai",
        "served_model": "test/p1",
        "canonical_model": "test/p1",
        "submitted_by": "miles",
        "created_at": NOW,
    }
    row.update(extra)
    return row


def _verdict(go_id: str, decided_by: str = "unanimous") -> dict:
    return {
        "selected_ids": [go_id],
        "confidences": {go_id: "high"},
        "final_confidence": "high",
        "decided_by": decided_by,
        "n_valid": 3,
        "votes": {go_id: 3},
    }


def _spec(name: str = "go_mf", version: str = "4") -> dict:
    return {
        "name": name,
        "version": version,
        "entity_field": "text",
        "id_field": "go_id",
        "roles": {
            "proposer": {"system": "s", "user": "u", "schema": {"type": "object"}}
        },
    }


def _build_store(path: Path) -> SqliteStore:
    """A REAL store carrying every selection case the three gates must separate.

    * ``job-chunk1`` -- a base chunk, ``published``. Holds ``i1`` (ok),
      ``i2`` (ok, with the answer the retry later supersedes) and ``i9``
      (``error``: an item whose calls failed, which produces NO export row).
    * ``job-retry`` -- the retry pass, ``published``, re-runs ``i2`` with a
      different verdict. Its ``parent_job_id`` points at the chunk, so a
      publisher resolving by ``parent_job_id`` would get the SAME answer here
      as one resolving by declared order -- which is why the declared-order
      test also runs the bundle REVERSED.
    * ``job-pilot`` -- ``pilot``. Paid work, not a result.
    * ``job-cancelled`` -- ``cancelled``, labelled to look authoritative by
      size, carrying a genuine but superseded verdict for ``i1``.
    * ``job-otherkind`` -- ``published`` but ``query_name='other_kind'``, so
      only GATE 1 excludes it.
    * ``job-undeclared`` -- NO disposition row at all. Absence must never
      read as published.
    """
    store = SqliteStore(str(path))
    store.init_schema()
    store.assign_store_identity(STORE_ID, declared_by="miles")
    store.put_query_type(name="go_mf", version="4", spec=_spec(), created_by="miles")
    store.put_query_type(
        name="other_kind",
        version="1",
        spec=_spec("other_kind", "1"),
        created_by="miles",
    )
    base = {
        "query_name": "go_mf",
        "query_version": "4",
        "submitted_by": "miles",
        "submitted_at": NOW,
        "status": "done",
        "roster_fingerprint": ROSTER,
    }

    store.create_job(
        {**base, "job_id": "job-chunk1", "label": "chunk01"},
        [
            {"item_id": "i1", "evidence": {"text": "Glucokinase (EC 2.7.1.2)"}},
            {"item_id": "i2", "evidence": {"text": "Hexokinase"}},
            {"item_id": "i9", "evidence": {"text": "Never settled"}},
        ],
        [
            _call("job-chunk1", "i1", call_id="c1", status="ok"),
            _call("job-chunk1", "i2", call_id="c2", status="ok"),
            _call("job-chunk1", "i9", call_id="c9", status="error"),
        ],
    )
    store.settle_item("job-chunk1", "i1", "ok", verdict=_verdict("GO:0004340"))
    store.settle_item("job-chunk1", "i2", "ok", verdict=_verdict("GO:0000000"))
    store.settle_item("job-chunk1", "i9", "error", verdict=None)

    store.create_job(
        {
            **base,
            "job_id": "job-retry",
            "label": "error retry",
            "parent_job_id": "job-chunk1",
        },
        [{"item_id": "i2", "evidence": {"text": "Hexokinase"}}],
        [_call("job-retry", "i2", call_id="c3", status="ok")],
    )
    store.settle_item("job-retry", "i2", "ok", verdict=_verdict("GO:0004396", "meta"))

    store.create_job(
        {**base, "job_id": "job-pilot", "label": "pilot round"},
        [{"item_id": "i3", "evidence": {"text": "Pilot function"}}],
        [_call("job-pilot", "i3", call_id="c4", status="ok")],
    )
    store.settle_item("job-pilot", "i3", "ok", verdict=_verdict("GO:0016740"))

    store.create_job(
        {
            **base,
            "job_id": "job-cancelled",
            "label": "ADP1+PMI 19,276 -- option b",
            "status": "queued",
        },
        [{"item_id": "i1", "evidence": {"text": "Glucokinase (EC 2.7.1.2)"}}],
        [_call("job-cancelled", "i1", call_id="c5", status="ok")],
    )
    store.settle_item("job-cancelled", "i1", "ok", verdict=_verdict("GO:0009999"))
    store.cancel_job("job-cancelled")

    store.create_job(
        {
            **base,
            "job_id": "job-otherkind",
            "label": "a council about something else",
            "query_name": "other_kind",
            "query_version": "1",
        },
        [{"item_id": "i4", "evidence": {"text": "Other kind"}}],
        [
            _call(
                "job-otherkind",
                "i4",
                call_id="c6",
                status="ok",
                query_name="other_kind",
                query_version="1",
            )
        ],
    )
    store.settle_item("job-otherkind", "i4", "ok", verdict=_verdict("GO:0000001"))

    store.create_job(
        {**base, "job_id": "job-undeclared", "label": "nobody declared this"},
        [{"item_id": "i5", "evidence": {"text": "Undeclared"}}],
        [_call("job-undeclared", "i5", call_id="c7", status="ok")],
    )
    store.settle_item("job-undeclared", "i5", "ok", verdict=_verdict("GO:0000002"))
    # One real attempt row, so ``attempts`` is non-empty and the
    # crash-recovery fixture below can interrupt the archiver BETWEEN two
    # tables that both have rows -- which is the failure that reads as a
    # complete archive to every query that does not join the missing table.
    store.record_attempt(
        "c1",
        1,
        started_at=NOW,
        finished_at=NOW,
        outcome_kind="ok",
        usage={"input_tokens": 100, "output_tokens": 20},
        worker_id="w1",
    )
    store.register_worker(
        {"worker_id": "w1", "user": "local", "host": "poplar", "started_at": NOW}
    )
    return store


#: The dispositions the project declared, through the REAL write path.
DISPOSITIONS = (
    ("job-chunk1", "published"),
    ("job-retry", "published"),
    ("job-pilot", "pilot"),
    ("job-cancelled", "cancelled"),
    ("job-otherkind", "published"),
    # job-undeclared is DELIBERATELY absent.
)


@pytest.fixture
def store(tmp_path: Path):
    opened = _build_store(tmp_path / "store" / "council.sqlite")
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def sink(tmp_path: Path):
    opened = SqliteCouncilSink(tmp_path / "lake" / "council.sqlite")
    opened.bootstrap()
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def state_root(tmp_path: Path) -> Path:
    return tmp_path / "deliveries"


@pytest.fixture
def plan(tmp_path: Path) -> dict:
    """The run identity, as ``PLAN.json`` records it.

    ``parameter_set_hash`` is computed with the SAME function the manifest
    path hashes with, so the publish's partition check compares two values
    that were derived once -- a hand-written digest here would make the test
    pass against a partition nothing writes.
    """
    return {
        "query": QUERY,
        "bench_fingerprint": BENCH,
        "mapping_hash": MAPPING,
        "roster_fingerprint": ROSTER,
        "parameter_set": dict(PARAMETER_SET),
        "parameter_set_hash": PARAMETER_SET_HASH,
        "plan_hash": hashlib.sha256(b"plan").hexdigest(),
        "source": f"council/{QUERY}",
        "generated_at": "2026-10-25T12:00:00Z",
        "items_file": str(tmp_path / "scratch" / "items.jsonl"),
        "counts": {
            "universe": 10,
            "done": 0,
            "unrun": 4,
            "submit": 3,
            "abstain": 1,
            "fanout": 3,
        },
    }


@pytest.fixture
def archived(store: SqliteStore, sink, state_root: Path, tmp_path: Path) -> dict:
    """A REAL archive of a REAL snapshot, with REAL dispositions beside it."""
    manifest = store.snapshot(tmp_path / "snaps" / "snap1.sqlite")
    report = archive_snapshot(sink, manifest, snapshot_seq=1, state_root=state_root)
    assert report["complete"] is True, report["status"]
    ledger = cd.DispositionLedger(tmp_path / "dispositions" / "ledger.jsonl")
    for job_id, disposition in DISPOSITIONS:
        cd.declare(
            ledger,
            job_id=job_id,
            disposition=disposition,
            reason="fixture",
            declared_by="miles",
        )
    cd.write_dispositions(
        sink,
        ledger,
        store_id=STORE_ID,
        snapshot_id=manifest["snapshot_id"],
        snapshot_seq=1,
        store_host="poplar",
        load_batch_id="batch-0",
    )
    return {"manifest": manifest, "snapshot_id": manifest["snapshot_id"]}


def _bundle(
    plan: dict,
    archived: dict,
    *,
    campaign_id: str = "camp-1",
    fanout=None,
    abstain=None,
    job_ids=("job-chunk1",),
    retry_job_ids=("job-retry",),
    snapshot_id: str | None = None,
) -> dict:
    return cb.build_bundle(
        plan,
        campaign_id=campaign_id,
        store_id=STORE_ID,
        snapshot_id=snapshot_id or archived["snapshot_id"],
        # UPDATED FOR BUNDLE v2 (KBUtilLib#97): the abstain row carries its
        # `text`. It always did in the plan's abstain record -- this fixture
        # simply omitted the field, which v1 tolerated because the publisher
        # took abstention texts from a `texts=` kwarg and counted the ones it
        # did not get. v2 carries the text IN THE BUNDLE and refuses a blank
        # one by name, so a fixture without it now (correctly) refuses.
        abstain=abstain
        if abstain is not None
        else [
            {
                "query_id": ABSTAIN,
                "text": ABSTAIN_TEXT,
                "abstain_reason": "no_candidates",
                "decline_reason": "abstained:no_candidates",
            }
        ],
        fanout=fanout
        if fanout is not None
        else [
            {"query_id": F1, "item_id": "i1"},
            {"query_id": F2, "item_id": "i2"},
            {"query_id": F2B, "item_id": "i2"},
        ],
        job_ids=list(job_ids),
        retry_job_ids=list(retry_job_ids),
        written_by="miles",
    )


@pytest.fixture
def bundle(plan: dict, archived: dict, sink, state_root: Path) -> dict:
    built = _bundle(plan, archived)
    cb.publish_bundle(sink, built, archive_state_root=state_root)
    return built


def _sharded_rows(record: dict, table: str = "function_result") -> list[dict]:
    """Every row of one target table in the published shard run."""
    rows: list[dict] = []
    for batch in (record.get("publish") or record)["batches"]:
        if batch["table"] != table:
            continue
        rows.extend(
            pq.read_table(
                str(Path((record.get("publish") or record)["shard_dir"]) / batch["file"])
            ).to_pylist()
        )
    return rows


# --------------------------------------------------------------------------
# The three gates, each on its own
# --------------------------------------------------------------------------


class TestTheThreeGates:
    def test_a_bundle_listed_published_job_yields_rows(
        self, sink, bundle, state_root
    ) -> None:
        selection = lp.select_verdicts(sink, bundle, state_root=state_root)
        assert selection["ordered_job_ids"] == ["job-chunk1", "job-retry"]
        built = lp.lakehouse_export_rows(bundle, selection)
        assert {row["query_id"] for row in built["rows"]} == {F1, F2, F2B, ABSTAIN}

    def test_a_published_job_absent_from_the_bundle_contributes_nothing(
        self, sink, plan, archived, state_root
    ) -> None:
        """GATE 2. ``job-retry`` is declared ``published`` and is still excluded.

        A job can be a genuine result of a DIFFERENT campaign, and publishing
        it here would attribute its verdicts to the wrong parameter set. So
        the chunk's own (superseded) answer for ``i2`` is what resolves.
        """
        unlisted = _bundle(plan, archived, retry_job_ids=())
        selection = lp.select_verdicts(sink, unlisted, state_root=state_root)
        assert selection["ordered_job_ids"] == ["job-chunk1"]
        assert selection["verdicts"]["i2"]["job_id"] == "job-chunk1"
        built = lp.lakehouse_export_rows(unlisted, selection)
        councils = {
            row["query_id"]: (row["council"] or {}).get("job_id")
            for row in built["rows"]
        }
        assert councils[F2] == "job-chunk1"
        assert "job-retry" not in set(councils.values())

    def test_asking_for_an_unlisted_job_fails_naming_it(
        self, sink, plan, archived
    ) -> None:
        unlisted = _bundle(plan, archived, retry_job_ids=())
        with pytest.raises(cb.JobNotInBundleError) as caught:
            cb.require_bundle_listed(unlisted, ["job-retry"])
        assert "job-retry" in str(caught.value)

    def test_a_bundle_listed_pilot_job_is_refused(
        self, sink, plan, archived, state_root
    ) -> None:
        """GATE 3. A pilot answer is PAID work and still not a result."""
        pilot = _bundle(
            plan,
            archived,
            campaign_id="camp-pilot",
            fanout=[{"query_id": qid("p3"), "item_id": "i3"}],
            abstain=[],
            job_ids=("job-pilot",),
            retry_job_ids=(),
        )
        with pytest.raises(cd.JobNotPublishableError) as caught:
            lp.select_verdicts(sink, pilot, state_root=state_root)
        assert "job-pilot" in str(caught.value)
        assert "pilot" in str(caught.value)

    def test_a_job_with_no_disposition_row_is_refused_naming_it(
        self, sink, plan, archived, state_root
    ) -> None:
        """GATE 3, the dangerous default: absence is NOT an implicit published."""
        undeclared = _bundle(
            plan,
            archived,
            campaign_id="camp-undeclared",
            fanout=[{"query_id": qid("u5"), "item_id": "i5"}],
            abstain=[],
            job_ids=("job-undeclared",),
            retry_job_ids=(),
        )
        with pytest.raises(cd.JobNotPublishableError) as caught:
            lp.select_verdicts(sink, undeclared, state_root=state_root)
        assert "job-undeclared" in str(caught.value)
        assert "NO disposition row" in str(caught.value)

    def test_a_bundle_listed_job_of_another_council_kind_is_refused(
        self, sink, plan, archived, state_root
    ) -> None:
        """GATE 1 -- the only gate this module owns.

        ``job-otherkind`` is bundle-listed AND declared ``published``, so
        gates 2 and 3 both pass and only the council-kind gate stops it.
        """
        other = _bundle(
            plan,
            archived,
            campaign_id="camp-other",
            fanout=[{"query_id": qid("o4"), "item_id": "i4"}],
            abstain=[],
            job_ids=("job-otherkind",),
            retry_job_ids=(),
        )
        with pytest.raises(lp.WrongQueryKindError) as caught:
            lp.select_verdicts(sink, other, state_root=state_root)
        assert "job-otherkind" in str(caught.value)
        assert "other_kind" in str(caught.value)

    def test_gates_two_and_three_pass_for_the_other_kind_job(
        self, sink, plan, archived
    ) -> None:
        """Proof that the previous test isolated GATE 1 and nothing else."""
        other = _bundle(
            plan,
            archived,
            campaign_id="camp-other",
            fanout=[{"query_id": qid("o4"), "item_id": "i4"}],
            abstain=[],
            job_ids=("job-otherkind",),
            retry_job_ids=(),
        )
        assert cb.require_publishable(
            sink, other, ["job-otherkind"], snapshot_id=archived["snapshot_id"]
        ) == ["job-otherkind"]

    def test_a_bundle_that_wrongly_lists_a_cancelled_job_is_still_refused(
        self, sink, plan, archived, state_root
    ) -> None:
        """The two cancelled 19,276-item jobs look authoritative by label.

        One carries 49 genuine but superseded verdicts, so its rows would
        publish if the bundle alone decided -- which is why the disposition
        gate is a SECOND, independent gate rather than a nicety.
        """
        wrong = _bundle(
            plan,
            archived,
            campaign_id="camp-cancelled",
            fanout=[{"query_id": qid("c1"), "item_id": "i1"}],
            abstain=[],
            job_ids=("job-cancelled",),
            retry_job_ids=(),
        )
        assert cb.require_bundle_listed(wrong, ["job-cancelled"]) == ["job-cancelled"]
        with pytest.raises(cd.JobNotPublishableError) as caught:
            lp.select_verdicts(sink, wrong, state_root=state_root)
        assert "cancelled" in str(caught.value)


# --------------------------------------------------------------------------
# The pin, the fanout and the winner order
# --------------------------------------------------------------------------


class TestPinFanoutAndWinners:
    def test_an_incompletely_archived_snapshot_is_refused(
        self, sink, store, plan, state_root, tmp_path
    ) -> None:
        """``require_complete_delivery`` -- the ONE completeness test.

        The nine namespace tables commit INDEPENDENTLY, so a crash after
        ``calls`` and before ``attempts`` reads as a complete archive to every
        query that does not join the missing table.
        """
        manifest = store.snapshot(tmp_path / "snaps" / "partial.sqlite")

        def _stop_at_attempts(target_sink, table, rows):
            if table == "attempts":
                raise RuntimeError("the pod went away between two commits")
            return target_sink.append_rows(table, rows)

        with pytest.raises(RuntimeError):
            archive_snapshot(
                sink,
                manifest,
                snapshot_seq=1,
                state_root=state_root,
                table_writer=_stop_at_attempts,
            )
        partial = _bundle(
            plan,
            {"snapshot_id": manifest["snapshot_id"]},
            campaign_id="camp-partial",
        )
        with pytest.raises(DeliveryIncompleteError) as caught:
            lp.select_verdicts(sink, partial, state_root=state_root)
        assert "attempts" in str(caught.value)

    def test_one_item_fanning_out_to_two_query_ids_yields_two_rows(
        self, sink, bundle, state_root
    ) -> None:
        """A row count is NOT an item count, in this direction.

        Distinct ``query_id``s sharing one definition text are answered ONCE
        and fanned back out, so the clearinghouse key set is not derivable
        from ``council.items``.
        """
        selection = lp.select_verdicts(sink, bundle, state_root=state_root)
        built = lp.lakehouse_export_rows(bundle, selection)
        for_i2 = [
            row
            for row in built["rows"]
            if (row["council"] or {}).get("item_id") == "i2"
        ]
        assert {row["query_id"] for row in for_i2} == {F2, F2B}
        assert len({tuple(row["go_ids"]) for row in for_i2}) == 1

    def test_winners_resolve_by_the_bundles_declared_order(
        self, sink, bundle, state_root
    ) -> None:
        selection = lp.select_verdicts(sink, bundle, state_root=state_root)
        assert selection["verdicts"]["i2"]["job_id"] == "job-retry"
        assert selection["verdicts"]["i2"]["verdict"]["selected_ids"] == ["GO:0004396"]

    def test_reversing_the_declared_order_reverses_the_winner(
        self, sink, plan, archived, state_root
    ) -> None:
        """NOT ``parent_job_id``, NOT insertion order, NOT latest timestamp.

        ``job-retry``'s ``parent_job_id`` points at ``job-chunk1`` and it was
        inserted second, so every one of those three rules would pick the
        retry. Only DECLARED ORDER can pick the chunk -- which is what this
        bundle declares.
        """
        reversed_bundle = _bundle(
            plan,
            archived,
            campaign_id="camp-reversed",
            job_ids=("job-retry",),
            retry_job_ids=("job-chunk1",),
        )
        assert reversed_bundle["ordered_job_ids"] == ["job-retry", "job-chunk1"]
        selection = lp.select_verdicts(sink, reversed_bundle, state_root=state_root)
        assert selection["verdicts"]["i2"]["job_id"] == "job-chunk1"
        assert selection["verdicts"]["i2"]["verdict"]["selected_ids"] == ["GO:0000000"]


# --------------------------------------------------------------------------
# The payload: council as object-or-null, and never council_entity_hash
# --------------------------------------------------------------------------


class TestThePayload:
    def test_council_is_an_object_on_every_decided_row_and_null_on_the_abstention(
        self, sink, bundle, state_root
    ) -> None:
        selection = lp.select_verdicts(sink, bundle, state_root=state_root)
        rows = {row["query_id"]: row for row in lp.lakehouse_export_rows(bundle, selection)["rows"]}
        assert rows[ABSTAIN]["council"] is None
        assert rows[ABSTAIN]["status"] is None
        assert rows[ABSTAIN]["decline_reason"] == "abstained:no_candidates"
        for query_id in (F1, F2, F2B):
            reference = rows[query_id]["council"]
            assert set(reference) == set(vx.COUNCIL_REFERENCE_FIELDS)
            assert reference["store_id"] == STORE_ID
            assert rows[query_id]["status"] == "ok"

    def test_the_councils_own_hash_is_never_published(
        self, sink, bundle, state_root
    ) -> None:
        """``council_entity_hash`` matched the clearinghouse hash 0 of 19,268
        times, so a loader joining on it would load nothing and look like it
        had worked."""
        selection = lp.select_verdicts(sink, bundle, state_root=state_root)
        rows = lp.lakehouse_export_rows(bundle, selection)["rows"]
        assert COUNCIL_ENTITY_HASH_COLUMN not in set(vx.EXPORT_COLUMNS)
        for row in rows:
            assert COUNCIL_ENTITY_HASH_COLUMN not in row
            assert COUNCIL_ENTITY_HASH_COLUMN not in json.dumps(row, default=str)

    def test_the_rows_are_exactly_the_exports_declared_columns(
        self, sink, bundle, state_root
    ) -> None:
        """Byte-compatible with the run-directory export, by construction.

        The rows are built by ``verdicts.export_rows`` rather than by a second
        row builder here, which is why this can be asserted at all.
        """
        selection = lp.select_verdicts(sink, bundle, state_root=state_root)
        for row in lp.lakehouse_export_rows(bundle, selection)["rows"]:
            # By NAME, not by dict order: ``export_rows`` splices the run
            # identity in as a mapping, so its key order is its own and the
            # parquet's column order comes from the DECLARED
            # ``verdicts.export_schema()`` either way.
            assert set(row) == set(vx.EXPORT_COLUMNS)

    def test_the_decided_rows_carry_the_text_from_the_archive(
        self, sink, bundle, state_root
    ) -> None:
        selection = lp.select_verdicts(sink, bundle, state_root=state_root)
        rows = {row["query_id"]: row for row in lp.lakehouse_export_rows(bundle, selection)["rows"]}
        assert rows[F1]["function_text"] == "Glucokinase (EC 2.7.1.2)"
        assert rows[F2]["function_text"] == "Hexokinase"

    def test_an_abstentions_text_COMES_FROM_THE_BUNDLE(
        self, sink, bundle, state_root
    ) -> None:
        """REWRITTEN FOR BUNDLE v2 (KBUtilLib#97).

        It used to assert the v1 behaviour: the lake has no text for an
        abstention, so the publisher reported ``abstentions_without_text`` and
        a caller could fill it with ``texts=``. Both halves of that are now
        wrong by design -- the text rides IN THE BUNDLE, copied from the plan's
        abstain record, and a caller who forgets a kwarg is exactly how 760
        permanently-empty cells would have been published. The kwarg is now a
        FANOUT-ONLY override and is not consulted for abstentions, which the
        second half of this test pins.
        """
        selection = lp.select_verdicts(sink, bundle, state_root=state_root)
        built = lp.lakehouse_export_rows(bundle, selection)
        assert built["abstentions_without_text"] == []
        rows = {row["query_id"]: row for row in built["rows"]}
        assert rows[ABSTAIN]["function_text"] == ABSTAIN_TEXT

        # texts= CANNOT override an abstention: the bundle is the one source.
        overridden = lp.lakehouse_export_rows(
            bundle, selection, texts={ABSTAIN: "something else entirely"}
        )
        assert {
            row["query_id"]: row["function_text"] for row in overridden["rows"]
        }[ABSTAIN] == ABSTAIN_TEXT


# --------------------------------------------------------------------------
# THE ZERO-JOIN GUARD -- before the join, never on its result
# --------------------------------------------------------------------------


class TestTheZeroJoinGuard:
    def test_the_guard_reports_a_count_for_each_of_the_four(
        self, sink, bundle, state_root
    ) -> None:
        selection = lp.select_verdicts(sink, bundle, state_root=state_root)
        report = lp.guard_join(
            bundle,
            selection["verdicts"],
            parameter_set_hash=PARAMETER_SET_HASH,
            source=f"council/{QUERY}",
        )
        assert report["source_rows"] == 4
        assert report["source_keys"] == 4
        assert report["null_keys"] == 0
        assert report["ambiguous_slots"] == 0
        assert report["coverage"] == 1.0
        assert report["coverage_floor"] == ci.DEFAULT_COVERAGE_FLOOR == 1.0
        assert report["evaluated"] == "source-side, before the join"

    def test_a_non_empty_input_that_resolves_nothing_is_refused(
        self, sink, plan, archived, state_root
    ) -> None:
        """THE FIXTURE IS BUILT SO A POST-JOIN TEST WOULD PASS VACUOUSLY.

        Four real, well-formed, non-null ``query_id``s are submitted and NONE
        of their item ids exists in the pinned snapshot, so the join returns
        zero rows -- and every relationship assertion over zero rows is
        trivially true. That is exactly how this defect class survives having
        tests, and it is why the guard is evaluated on the source side.
        """
        nothing = _bundle(
            plan,
            archived,
            campaign_id="camp-nothing",
            abstain=[],
            fanout=[
                {"query_id": qid(f"ghost{n}"), "item_id": f"ghost-item-{n}"}
                for n in range(4)
            ],
        )
        selection = lp.select_verdicts(sink, nothing, state_root=state_root)

        # The POST-join view: zero rows, and the naive checks all "pass".
        built = lp.lakehouse_export_rows(nothing, selection)
        assert built["rows"] == []
        assert all(row["query_id"] for row in built["rows"])  # vacuously true
        assert all(row["council"] for row in built["rows"])  # vacuously true

        # The guard, on the source side, refuses.
        with pytest.raises(ci.ClearinghouseJoinAssertionError) as caught:
            lp.guard_join(
                nothing,
                selection["verdicts"],
                parameter_set_hash=PARAMETER_SET_HASH,
                source=f"council/{QUERY}",
            )
        assert "ZERO of 4" in str(caught.value)
        assert "no coverage floor waives it" in str(caught.value)

    def test_zero_resolution_is_refused_even_at_a_floor_of_zero(
        self, sink, plan, archived, state_root
    ) -> None:
        nothing = _bundle(
            plan,
            archived,
            campaign_id="camp-nothing",
            abstain=[],
            fanout=[
                {"query_id": qid(f"ghost{n}"), "item_id": f"ghost-item-{n}"}
                for n in range(4)
            ],
        )
        selection = lp.select_verdicts(sink, nothing, state_root=state_root)
        with pytest.raises(ci.ClearinghouseJoinAssertionError):
            lp.guard_join(
                nothing,
                selection["verdicts"],
                parameter_set_hash=PARAMETER_SET_HASH,
                source=f"council/{QUERY}",
                coverage_floor=0.0,
            )

    def test_an_empty_submitted_inventory_is_refused(
        self, sink, plan, archived, state_root
    ) -> None:
        """A campaign that submitted nothing is a PRODUCER failure."""
        empty = _bundle(
            plan, archived, campaign_id="camp-empty", abstain=[], fanout=[]
        )
        selection = lp.select_verdicts(sink, empty, state_root=state_root)
        with pytest.raises(ci.ClearinghouseJoinAssertionError) as caught:
            lp.guard_join(
                empty,
                selection["verdicts"],
                parameter_set_hash=PARAMETER_SET_HASH,
                source=f"council/{QUERY}",
            )
        assert "EMPTY" in str(caught.value)

    def test_a_malformed_join_key_is_counted_as_a_null_key(
        self, sink, plan, archived, state_root
    ) -> None:
        """Re-canonicalised through ``encode_entity_hash``, never trusted raw.

        A truncated or uppercased key would otherwise land as a plausible
        wrong hash; here it becomes a NULL key and a COUNT.
        """
        malformed = _bundle(
            plan,
            archived,
            campaign_id="camp-malformed",
            abstain=[],
            fanout=[
                {"query_id": F1, "item_id": "i1"},
                {"query_id": "not-a-sha256", "item_id": "i2"},
            ],
        )
        selection = lp.select_verdicts(sink, malformed, state_root=state_root)
        with pytest.raises(ci.ClearinghouseJoinAssertionError) as caught:
            lp.guard_join(
                malformed,
                selection["verdicts"],
                parameter_set_hash=PARAMETER_SET_HASH,
                source=f"council/{QUERY}",
            )
        assert "NULL entity_hash" in str(caught.value)

    def test_a_coverage_shortfall_is_refused_at_the_default_floor(
        self, sink, plan, archived, state_root
    ) -> None:
        """The eight residual items of the measured run, in miniature.

        ``i9``'s calls failed, so it never settled and its ``query_id``
        resolves to nothing. The EXISTING default floor of 1.0 is adopted
        rather than a new tolerance policy, so this refuses -- and lowering
        the floor is the operator's explicit act.
        """
        partial = _bundle(
            plan,
            archived,
            campaign_id="camp-partial-coverage",
            fanout=[
                {"query_id": F1, "item_id": "i1"},
                {"query_id": F2, "item_id": "i2"},
                {"query_id": F2B, "item_id": "i2"},
                {"query_id": F_UNRESOLVED, "item_id": "i9"},
            ],
        )
        selection = lp.select_verdicts(sink, partial, state_root=state_root)
        with pytest.raises(ci.ClearinghouseJoinAssertionError) as caught:
            lp.guard_join(
                partial,
                selection["verdicts"],
                parameter_set_hash=PARAMETER_SET_HASH,
                source=f"council/{QUERY}",
            )
        assert "below the declared floor" in str(caught.value)
        report = lp.guard_join(
            partial,
            selection["verdicts"],
            parameter_set_hash=PARAMETER_SET_HASH,
            source=f"council/{QUERY}",
            coverage_floor=0.5,
        )
        assert report["resolved_keys"] == 4
        assert report["unresolved_keys"] == 1


# --------------------------------------------------------------------------
# The whole producer half, and the receipt
# --------------------------------------------------------------------------


@pytest.fixture
def published(sink, bundle, state_root, tmp_path) -> dict:
    return lp.publish_from_lakehouse(
        sink,
        bundle,
        publish_dir=tmp_path / "publish",
        state_root=state_root,
    )


class TestThePublishAndTheReceipt:
    def test_the_rows_are_sharded_through_the_existing_path(
        self, published, tmp_path
    ) -> None:
        record = published["publish"]
        assert record["preset"] == "function-to-go-council"
        assert Path(record["manifest_file"]).is_file()
        assert Path(record["run_manifest"]).is_file()
        rows = _sharded_rows(published)
        assert len(rows) == 4
        assert {row["parameter_set_hash"] for row in rows} == {PARAMETER_SET_HASH}
        assert {row["result_type"] for row in rows} == {lp.RESULT_TYPE}

    def test_the_frozen_result_type_is_never_written(self, published) -> None:
        """The 19,268 pre-existing ``go_council_mapping`` rows are untouched.

        They are frozen by fill-only -- the slot key excludes
        ``result_type_version``, so a corrected payload in the same slot is
        silently dropped as already-present -- and nothing here issues an
        Iceberg delete.
        """
        assert lp.RESULT_TYPE != lp.FROZEN_RESULT_TYPE
        rows = _sharded_rows(published)
        assert lp.FROZEN_RESULT_TYPE not in {row["result_type"] for row in rows}

    def test_the_result_type_agrees_with_the_shipped_preset(self) -> None:
        """One declaration, pinned against the TOML that actually ships."""
        from kbutillib.domains.kbase.berdl import clearinghouse_backfill as cbf

        path, _name = cbf.resolve_manifest_path("function-to-go-council")
        text = path.read_text(encoding="utf-8")
        assert f'result_type         = "@const({lp.RESULT_TYPE})"' in text
        assert f'entity_type = "{lp.ENTITY_TYPE}"' in text

    def test_the_receipt_distinguishes_query_ids_from_result_rows(
        self, published
    ) -> None:
        """Three different numbers, all of them stated.

        One item can carry several GO terms and one item can fan out to
        several ``query_id``s, so a ``function_result`` row count is neither
        an item count nor a ``query_id`` count.
        """
        assert published["query_ids_submitted"] == 4
        assert published["query_ids_with_verdict"] == 4
        assert published["query_ids_unresolved"] == 0
        assert published["items_submitted"] == 2
        assert published["inputs_submitted"] == 4
        assert published["result_rows_written"] == 4
        assert published["reconciled_on"] == "distinct query_id"
        assert published["reconciled"] is True

    def test_the_receipt_reconciles_on_distinct_query_ids_with_a_retry_present(
        self, published
    ) -> None:
        """Summing across jobs double-counts every retried item.

        ``i2`` is settled by BOTH ``job-chunk1`` and ``job-retry`` -- two item
        rows for one function -- which is how a previous manifest reported
        19,405 items where the truth over unique functions is 19,276
        submitted and 19,268 decided.
        """
        assert "job-retry" in published["ordered_job_ids"]
        rows = published["publish"]["counts"]
        assert rows["items_submitted"] == 4  # the plan's abstain + fanout inputs
        assert published["query_ids_submitted"] == 4
        assert (
            published["query_ids_with_verdict"] + published["query_ids_unresolved"]
            == published["query_ids_submitted"]
        )

    def test_the_receipt_names_the_unresolved_query_ids(
        self, sink, plan, archived, state_root, tmp_path
    ) -> None:
        """The residual inputs are the interesting number, so they are NAMED."""
        partial = _bundle(
            plan,
            archived,
            campaign_id="camp-partial-receipt",
            fanout=[
                {"query_id": F1, "item_id": "i1"},
                {"query_id": F2, "item_id": "i2"},
                {"query_id": F2B, "item_id": "i2"},
                {"query_id": F_UNRESOLVED, "item_id": "i9"},
            ],
        )
        cb.publish_bundle(sink, partial, archive_state_root=state_root)
        receipt = lp.publish_from_lakehouse(
            sink,
            partial,
            publish_dir=tmp_path / "publish-partial",
            state_root=state_root,
            coverage_floor=0.5,
        )
        assert receipt["query_ids_submitted"] == 5
        assert receipt["query_ids_unresolved"] == 1
        assert receipt["unresolved_query_ids"] == [F_UNRESOLVED]
        assert receipt["reconciled"] is True
        assert receipt["result_rows_written"] == 4

    def test_the_receipt_carries_the_parameter_set_hash_the_rows_landed_under(
        self, published
    ) -> None:
        rows = _sharded_rows(published)
        assert published["parameter_set_hash"] == PARAMETER_SET_HASH
        assert {row["parameter_set_hash"] for row in rows} == {
            published["parameter_set_hash"]
        }

    def test_the_receipt_reports_the_load_as_awaiting_operator_acceptance(
        self, published
    ) -> None:
        """NOT as success, and the target snapshot id is NOT invented.

        The Iceberg commit that assigns it is made by ``kbu clearinghouse
        load`` in the pod; this half runs off-pod.
        """
        assert published["target_table_snapshot_id"] is None
        assert published["load"]["state"] == "awaiting operator acceptance"
        assert "kbu clearinghouse load" in published["load"]["load"]
        assert "kbu clearinghouse verify" in published["load"]["verify"]

    def test_the_declines_are_tallied_including_the_abstention(
        self, published
    ) -> None:
        assert published["declines_by_reason"] == {"abstained:no_candidates": 1}

    def test_the_guard_report_travels_in_the_receipt(self, published) -> None:
        assert published["guard"]["ok"] is True
        assert published["guard"]["coverage"] == 1.0


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------


class TestIdempotency:
    def test_running_twice_does_not_duplicate_rows(
        self, sink, bundle, state_root, tmp_path
    ) -> None:
        """Tested LOCALLY through the producer path, never through a live load.

        Two runs produce the SAME slot keys with the same payload, and the
        clearinghouse loader is fill-only, so the second load appends nothing.
        No row is duplicated inside either run either: the guard's
        "at most one source row per target slot" count is 0 both times.
        """
        first = lp.publish_from_lakehouse(
            sink, bundle, publish_dir=tmp_path / "publish-1", state_root=state_root
        )
        second = lp.publish_from_lakehouse(
            sink, bundle, publish_dir=tmp_path / "publish-2", state_root=state_root
        )
        slot = ("entity_hash", "entity_type", "result_type", "source", "parameter_set_hash")
        keys_first = sorted(tuple(row[c] for c in slot) for row in _sharded_rows(first))
        keys_second = sorted(tuple(row[c] for c in slot) for row in _sharded_rows(second))
        assert keys_first == keys_second
        assert len(keys_first) == len(set(keys_first))
        assert first["guard"]["ambiguous_slots"] == 0
        assert second["guard"]["ambiguous_slots"] == 0
        assert first["query_ids_with_verdict"] == second["query_ids_with_verdict"]

    def test_the_payloads_of_the_two_runs_are_identical(
        self, sink, bundle, state_root, tmp_path
    ) -> None:
        first = lp.publish_from_lakehouse(
            sink, bundle, publish_dir=tmp_path / "publish-1", state_root=state_root
        )
        second = lp.publish_from_lakehouse(
            sink, bundle, publish_dir=tmp_path / "publish-2", state_root=state_root
        )

        def _payloads(record):
            return sorted(
                (row["entity_hash"], row["payload"]) for row in _sharded_rows(record)
            )

        assert _payloads(first) == _payloads(second)

    def test_republishing_into_the_same_directory_is_a_no_op_on_the_plan(
        self, sink, bundle, state_root, tmp_path
    ) -> None:
        out = tmp_path / "publish"
        lp.publish_from_lakehouse(
            sink, bundle, publish_dir=out, state_root=state_root
        )
        from kbutillib.domains.ai.council.quote import PLAN_FILENAME

        before = (out / PLAN_FILENAME).read_bytes()
        lp.publish_from_lakehouse(
            sink, bundle, publish_dir=out, state_root=state_root
        )
        assert (out / PLAN_FILENAME).read_bytes() == before

    def test_a_publish_dir_holding_a_different_plan_is_refused(
        self, sink, bundle, state_root, tmp_path
    ) -> None:
        from kbutillib.domains.ai.council.quote import PLAN_FILENAME

        out = tmp_path / "publish"
        out.mkdir(parents=True)
        (out / PLAN_FILENAME).write_text('{"query": "go_mf@9"}', encoding="utf-8")
        with pytest.raises(lp.CouncilPublishError) as caught:
            lp.publish_from_lakehouse(
                sink, bundle, publish_dir=out, state_root=state_root
            )
        assert "DIFFERENT plan" in str(caught.value)

    def test_republishing_the_bundle_itself_is_a_no_op(
        self, sink, bundle, state_root
    ) -> None:
        again = cb.publish_bundle(sink, bundle, archive_state_root=state_root)
        assert again["written"] is False
        assert (
            sink.count_rows(
                cb.PUBLICATION_BUNDLE_TABLE, where={"campaign_id": "camp-1"}
            )
            == 1
        )


# --------------------------------------------------------------------------
# NO-INJECTION: the verb through the REAL CLI, exit codes and all
# --------------------------------------------------------------------------


class TestTheCli:
    @staticmethod
    def _invoke(*args: str):
        return CliRunner().invoke(main, ["ontomap", *args])

    @staticmethod
    def _payload(result) -> dict:
        for line in reversed(result.output.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                return json.loads(line)
        raise AssertionError(f"no JSON envelope in output: {result.output!r}")

    def _bundle_file(self, bundle: dict, tmp_path: Path) -> Path:
        path = tmp_path / "BUNDLE.json"
        cb.write_bundle(bundle, path)
        return path

    def test_the_verb_publishes_and_emits_the_receipt(
        self, sink, bundle, state_root, tmp_path
    ) -> None:
        result = self._invoke(
            "council-publish-lakehouse",
            "--bundle",
            str(self._bundle_file(bundle, tmp_path)),
            "--lakehouse",
            str(sink.path),
            "--publish-dir",
            str(tmp_path / "publish"),
            "--state-root",
            str(state_root),
            "--json",
        )
        assert result.exit_code == 0, result.output
        data = self._payload(result)["data"]
        assert data["query_ids_submitted"] == 4
        assert data["result_rows_written"] == 4
        assert data["load"]["state"] == "awaiting operator acceptance"

    def test_an_undispositioned_job_exits_non_zero_naming_it(
        self, sink, plan, archived, state_root, tmp_path
    ) -> None:
        undeclared = _bundle(
            plan,
            archived,
            campaign_id="camp-undeclared",
            fanout=[{"query_id": qid("u5"), "item_id": "i5"}],
            abstain=[],
            job_ids=("job-undeclared",),
            retry_job_ids=(),
        )
        result = self._invoke(
            "council-publish-lakehouse",
            "--bundle",
            str(self._bundle_file(undeclared, tmp_path)),
            "--lakehouse",
            str(sink.path),
            "--publish-dir",
            str(tmp_path / "publish"),
            "--state-root",
            str(state_root),
            "--json",
        )
        assert result.exit_code != 0
        assert "job-undeclared" in result.output

    def test_a_non_empty_input_resolving_zero_rows_exits_non_zero(
        self, sink, plan, archived, state_root, tmp_path
    ) -> None:
        nothing = _bundle(
            plan,
            archived,
            campaign_id="camp-nothing",
            abstain=[],
            fanout=[
                {"query_id": qid(f"ghost{n}"), "item_id": f"ghost-item-{n}"}
                for n in range(4)
            ],
        )
        result = self._invoke(
            "council-publish-lakehouse",
            "--bundle",
            str(self._bundle_file(nothing, tmp_path)),
            "--lakehouse",
            str(sink.path),
            "--publish-dir",
            str(tmp_path / "publish"),
            "--state-root",
            str(state_root),
            "--json",
        )
        assert result.exit_code != 0
        assert "ZERO of 4" in result.output
        assert not (tmp_path / "publish" / "shard").exists()


# --------------------------------------------------------------------------
# BUNDLE v2 AT PUBLISH: the three refusals and the abstention date
# (KBUtilLib#97)
# --------------------------------------------------------------------------


def _v1(bundle: dict) -> dict:
    """The same campaign as a v1 bundle: no abstention texts, no plan_origin.

    Built by DEGRADING a real v2 bundle rather than by hand, so it is the
    shape a v1 bundle actually had -- a hand-written dict would prove that the
    publisher reads a field, not that the field means what v1 wrote.
    """
    degraded = {
        **bundle,
        "schema_version": 1,
        "abstentions": [
            {k: v for k, v in row.items() if k != "text"}
            for row in bundle["abstentions"]
        ],
    }
    degraded.pop("plan_origin", None)
    degraded["bundle_digest"] = cb.content_digest(degraded)
    return degraded


class TestTheAbstentionTextRefusal:
    """A. A blank abstention text is a refusal that NAMES the query_ids."""

    def test_a_BLANKED_text_refuses_and_names_the_query_id(
        self, sink, plan, archived, state_root
    ) -> None:
        blanked = _bundle(
            plan,
            archived,
            abstain=[
                {
                    "query_id": ABSTAIN,
                    "text": "",
                    "abstain_reason": "no_candidates",
                    "decline_reason": "abstained:no_candidates",
                }
            ],
        )
        cb.publish_bundle(sink, blanked, archive_state_root=state_root)
        selection = lp.select_verdicts(sink, blanked, state_root=state_root)
        with pytest.raises(lp.AbstentionTextMissingError) as caught:
            lp.lakehouse_export_rows(blanked, selection)
        message = str(caught.value)
        assert ABSTAIN in message
        assert "1 of 1" in message
        assert "FIRST WRITE" in message

    def test_a_WHITESPACE_ONLY_text_is_blank_too(
        self, sink, plan, archived, state_root
    ) -> None:
        blanked = _bundle(
            plan,
            archived,
            abstain=[
                {
                    "query_id": ABSTAIN,
                    "text": " \t \n ",
                    "abstain_reason": "no_candidates",
                    "decline_reason": "abstained:no_candidates",
                }
            ],
        )
        cb.publish_bundle(sink, blanked, archive_state_root=state_root)
        selection = lp.select_verdicts(sink, blanked, state_root=state_root)
        with pytest.raises(lp.AbstentionTextMissingError):
            lp.lakehouse_export_rows(blanked, selection)

    def test_the_WHOLE_PUBLISH_refuses_and_writes_nothing(
        self, sink, plan, archived, state_root, tmp_path
    ) -> None:
        """It is a refusal, not a count in the receipt: no parquet, no shard."""
        blanked = _bundle(
            plan,
            archived,
            abstain=[
                {
                    "query_id": ABSTAIN,
                    "text": "",
                    "abstain_reason": "no_candidates",
                    "decline_reason": "abstained:no_candidates",
                }
            ],
        )
        cb.publish_bundle(sink, blanked, archive_state_root=state_root)
        out = tmp_path / "publish-blanked"
        with pytest.raises(lp.AbstentionTextMissingError):
            lp.publish_from_lakehouse(
                sink, blanked, publish_dir=out, state_root=state_root
            )
        assert not (out / "verdicts.parquet").exists()
        assert not (out / "shard").exists()

    def test_the_refusal_names_EVERY_blank_one_not_just_the_first(
        self, sink, plan, archived, state_root
    ) -> None:
        second = qid("abstain2")
        blanked = _bundle(
            plan,
            archived,
            abstain=[
                {
                    "query_id": ABSTAIN,
                    "text": "",
                    "abstain_reason": "no_candidates",
                    "decline_reason": "abstained:no_candidates",
                },
                {
                    "query_id": second,
                    "text": "",
                    "abstain_reason": "placeholder",
                    "decline_reason": "abstained:placeholder",
                },
            ],
        )
        cb.publish_bundle(sink, blanked, archive_state_root=state_root)
        selection = lp.select_verdicts(sink, blanked, state_root=state_root)
        with pytest.raises(lp.AbstentionTextMissingError) as caught:
            lp.lakehouse_export_rows(blanked, selection)
        message = str(caught.value)
        assert ABSTAIN in message and second in message
        assert "2 of 2" in message

    def test_a_GOOD_bundle_reports_abstentions_without_text_as_EMPTY(
        self, sink, bundle, state_root
    ) -> None:
        """The receipt key survives and is now structurally []: a blank raises."""
        selection = lp.select_verdicts(sink, bundle, state_root=state_root)
        assert (
            lp.lakehouse_export_rows(bundle, selection)["abstentions_without_text"]
            == []
        )


class TestTheV1BundleRefusal:
    """A. A v1 bundle that carries abstentions is refused, with a rebuild."""

    def test_a_v1_bundle_WITH_abstentions_is_refused_told_to_REBUILD(
        self, sink, bundle, state_root
    ) -> None:
        with pytest.raises(lp.StaleBundleSchemaError) as caught:
            lp.require_publishable_bundle(_v1(bundle))
        message = str(caught.value)
        assert "schema_version 1" in message
        assert "REBUILD" in message
        assert "build_bundle_from_run" in message
        assert "FIRST WRITE" in message

    def test_it_is_refused_at_PUBLISH_before_anything_is_written(
        self, sink, bundle, state_root, tmp_path
    ) -> None:
        out = tmp_path / "publish-v1"
        with pytest.raises(lp.StaleBundleSchemaError):
            lp.publish_from_lakehouse(
                sink, _v1(bundle), publish_dir=out, state_root=state_root
            )
        assert not (out / "verdicts.parquet").exists()
        assert not (out / "shard").exists()

    def test_the_v1_refusal_WINS_over_the_blank_text_one(
        self, sink, bundle, state_root
    ) -> None:
        """A v1 bundle's abstentions have no text at all, so the blank-text
        check would also fire -- and its message ("fix it in the plan's
        abstain record") is the wrong instruction. The schema check runs
        first so the operator is told to rebuild."""
        selection = lp.select_verdicts(sink, bundle, state_root=state_root)
        with pytest.raises(lp.StaleBundleSchemaError):
            lp.lakehouse_export_rows(_v1(bundle), selection)

    def test_a_v1_bundle_with_NO_abstentions_is_NOT_refused(
        self, sink, plan, archived, state_root
    ) -> None:
        """There is nothing it could be missing."""
        v1 = _v1(_bundle(plan, archived, abstain=[]))
        assert v1["schema_version"] == 1
        assert lp.require_publishable_bundle(v1) == {}


class TestTheEmptyPlanHashRefusal:
    """B. ``plan_hash == ''`` is refused unless the origin says
    ``reconstructed``."""

    def test_a_blank_plan_hash_with_a_STAGE1_origin_is_REFUSED(
        self, plan, archived
    ) -> None:
        built = _bundle({**plan, "plan_hash": ""}, archived)
        assert built["plan_origin"]["kind"] == "stage1"
        with pytest.raises(lp.UnreconstructedPlanError) as caught:
            lp.require_publishable_bundle(built)
        message = str(caught.value)
        assert "EMPTY plan_hash" in message
        assert "'stage1'" in message
        assert "reconstructed" in message

    def test_a_blank_plan_hash_DECLARED_RECONSTRUCTED_is_accepted(
        self, plan, archived
    ) -> None:
        """The 2026-10-19 campaign's case: its plan was hand-assembled, so it
        has no plan_hash and never will. Accepted and RECORDED as what it is
        rather than regenerated."""
        built = cb.build_bundle(
            {**plan, "plan_hash": ""},
            campaign_id="camp-1",
            store_id=STORE_ID,
            snapshot_id=archived["snapshot_id"],
            abstain=[
                {
                    "query_id": ABSTAIN,
                    "text": ABSTAIN_TEXT,
                    "abstain_reason": "no_candidates",
                    "decline_reason": "abstained:no_candidates",
                }
            ],
            fanout=[{"query_id": F1, "item_id": "i1"}],
            job_ids=["job-chunk1"],
            plan_origin={
                "kind": "reconstructed",
                "assembled_on": "2026-10-19",
                "reason": "the campaign predates stage 1",
                "generated_at": "2026-10-19T04:12:07Z",
                "generated_at_source": "min(council.jobs.submitted_at)",
            },
            written_by="miles",
        )
        assert lp.require_publishable_bundle(built)["kind"] == "reconstructed"

    def test_a_NON_BLANK_plan_hash_needs_no_declaration(self, bundle) -> None:
        assert bundle["plan_hash"]
        assert lp.require_publishable_bundle(bundle)["kind"] == "stage1"

    def test_it_is_refused_at_PUBLISH_before_anything_is_written(
        self, sink, plan, archived, state_root, tmp_path
    ) -> None:
        built = _bundle({**plan, "plan_hash": ""}, archived)
        cb.publish_bundle(sink, built, archive_state_root=state_root)
        out = tmp_path / "publish-hashless"
        with pytest.raises(lp.UnreconstructedPlanError):
            lp.publish_from_lakehouse(
                sink, built, publish_dir=out, state_root=state_root
            )
        assert not (out / "verdicts.parquet").exists()


class TestTheAbstentionDecidedAt:
    """B. Every abstention's ``decided_at`` is ``plan_origin.generated_at``.

    The preset maps ``decided_at`` onto the clearinghouse ``observed_at`` and
    that slot freezes on first write, so this value is the PERMANENT
    observation time of the abstention. ``verdicts.export_rows`` would
    otherwise fall back to ``now()``, making it the publish date.
    """

    def test_it_comes_from_plan_origin_NOT_from_the_plans_own_date(
        self, sink, plan, archived, state_root
    ) -> None:
        """The plan says 2026-10-25; the declared origin says 2026-10-19. The
        ORIGIN wins -- for a reconstruction the plan's date is just when
        somebody wrote the file."""
        assert plan["generated_at"] == "2026-10-25T12:00:00Z"
        built = cb.build_bundle(
            {**plan, "plan_hash": ""},
            campaign_id="camp-1",
            store_id=STORE_ID,
            snapshot_id=archived["snapshot_id"],
            abstain=[
                {
                    "query_id": ABSTAIN,
                    "text": ABSTAIN_TEXT,
                    "abstain_reason": "no_candidates",
                    "decline_reason": "abstained:no_candidates",
                }
            ],
            fanout=[{"query_id": F1, "item_id": "i1"}],
            job_ids=("job-chunk1",),
            retry_job_ids=("job-retry",),
            plan_origin={
                "kind": "reconstructed",
                "assembled_on": "2026-10-19",
                "reason": "the campaign predates stage 1",
                "generated_at": "2026-10-19T04:12:07Z",
                "generated_at_source": "min(council.jobs.submitted_at)",
            },
            written_by="miles",
        )
        cb.publish_bundle(sink, built, archive_state_root=state_root)
        selection = lp.select_verdicts(sink, built, state_root=state_root)
        rows = {
            row["query_id"]: row
            for row in lp.lakehouse_export_rows(built, selection)["rows"]
        }
        assert rows[ABSTAIN]["decided_at"].startswith("2026-10-19T04:12:07")
        assert not rows[ABSTAIN]["decided_at"].startswith("2026-10-25")

    def test_EVERY_abstention_carries_exactly_that_value(
        self, sink, bundle, state_root
    ) -> None:
        selection = lp.select_verdicts(sink, bundle, state_root=state_root)
        built = lp.lakehouse_export_rows(bundle, selection)
        generated_at = cb.bundle_plan_origin(bundle)["generated_at"]
        abstained = [row for row in built["rows"] if row["council"] is None]
        assert abstained
        for row in abstained:
            assert row["decided_at"] == vx._iso(generated_at)

    def test_the_receipt_carries_the_origin_so_the_claim_is_auditable(
        self, sink, bundle, state_root, tmp_path
    ) -> None:
        receipt = lp.publish_from_lakehouse(
            sink,
            bundle,
            publish_dir=tmp_path / "publish-origin",
            state_root=state_root,
        )
        assert receipt["plan_origin"] == cb.bundle_plan_origin(bundle)
        assert receipt["plan_origin"]["generated_at"]


class TestTheCoverageFloorHelpText:
    """C. The help text's submitted inventory is 20,036, not 19,276."""

    def test_the_help_text_counts_the_abstentions_in_the_submitted_set(self) -> None:
        result = CliRunner().invoke(
            main, ["ontomap", "council-publish-lakehouse", "--help"]
        )
        assert result.exit_code == 0, result.output
        collapsed = " ".join(result.output.split())
        assert "8 of 20,036 submitted query_ids" in collapsed
        assert "19,276 fanout + 760 abstentions" in collapsed
        # The old wording described the campaign MINUS its abstentions.
        assert "of 19,276 inputs" not in collapsed
