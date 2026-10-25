"""``council_archive`` -- a verified snapshot in, ``kbaseincubator.council`` out.

NOTHING IS HAND-BUILT. Every test below archives from a REAL SQLite council
store created by the real ``SqliteStore`` constructor and ``init_schema()``,
written through the store's own ``create_job`` / ``add_calls`` /
``record_attempt`` / ``add_tokens`` / ``register_worker`` / ``settle_item`` /
``cancel_job`` methods and the real ``queries.supersede_pending_calls_update``
the engine uses, snapshotted with the EXISTING ``SqliteStore.snapshot()``
(the sqlite3 backup API), and archived FROM THAT MANIFEST into a real SQL
sink built from ``council_schema``'s own DDL. There is no dict standing in
for a store and no fabricated manifest.

The crash-recovery tests interrupt the REAL archiver by failing its table
writer on a named table -- they do not write a half-finished ledger by hand,
because a hand-written ledger is a statement about what the archiver would
have done rather than what it did.

WHAT ALREADY EXISTED AND IS NOT DUPLICATED HERE. ``snapshot()`` itself, and
its refusal without a stored identity, were built and tested earlier:
``tests/domains/ai/council/test_store_identity.py``
``TestFreshStoreHasNoIdentity::test_snapshot_refuses_and_names_the_absence``
(the StoreIdentityMissing refusal),
``TestConfiguredVersusStoredMismatch::test_snapshot_refuses_naming_both_values``,
``TestSnapshotAndExportOutput::test_the_snapshot_is_a_real_openable_store_not_a_mid_write_copy``
and ``::test_snapshot_refuses_to_overwrite_by_default``. This module adds
only what was missing: the snapshot taken while a WRITE TRANSACTION IS OPEN,
and the `kbu council snapshot` CLI verb.

NOT A CLAIM ABOUT A LIVE POD. SQLite is not Iceberg; the transport has been
broken on kbhub since the 2026-10-19 image (KBUtilLib#91). Live acceptance of
this namespace is operator-owned and is asserted nowhere in this repo.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip(
    "sqlalchemy",
    reason="the council store needs the `council` extra: pip install 'kbutillib[council]'",
)

import sqlalchemy as sa  # noqa: E402
from click.testing import CliRunner  # noqa: E402

from kbutillib.cli import main  # noqa: E402
from kbutillib.domains.ai.council.store import queries as store_queries  # noqa: E402
from kbutillib.domains.ai.council.store import schema as store_schema  # noqa: E402
from kbutillib.domains.ai.council.store.sqlite import SqliteStore  # noqa: E402
from kbutillib.domains.kbase.berdl import council_archive  # noqa: E402
from kbutillib.domains.kbase.berdl.council_archive import (  # noqa: E402
    ARCHIVER_TABLES,
    MAX_BATCH_BYTES,
    NOT_WRITTEN_BY_THE_ARCHIVER,
    SPARK_LOCAL_RELATION_CACHE_THRESHOLD_BYTES,
    AmbiguousBatchError,
    ArchiveLedger,
    DeliveryBindingError,
    DeliveryIncompleteError,
    SnapshotConflictError,
    SnapshotPrepareError,
    archive_snapshot,
    build_inventory,
    current_projection,
    delivery_state_dir,
    is_publishable,
    prepare_snapshot,
    receipt,
    reconcile_delivery,
    require_complete_delivery,
    role_brief_rows,
    snapshot_row_counts,
)
from kbutillib.domains.kbase.berdl.council_schema import (  # noqa: E402
    ARCHIVED_TABLES,
    COUNCIL_ENTITY_HASH_COLUMN,
    IDENTITY_COLUMN_NAMES,
    JOB_DISPOSITION_TABLE,
    ROLE_BRIEF_TABLE,
    ROLE_BRIEF_UNEXTRACTABLE,
    ContradictoryQueryNameError,
    column_names,
)
from kbutillib.domains.kbase.berdl.council_sink import SqliteCouncilSink  # noqa: E402

STORE_ID = "poplar/council-1"
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)

#: The five lease/queue columns of ``calls`` that must NOT be archived.
DROPPED_CALL_COLUMNS = (
    "not_before",
    "claim_token",
    "claimed_by",
    "claimed_at",
    "lease_generation",
)


def _spec(**roles) -> dict:
    """A query-type spec of the shape the shipped builtins use."""
    return {
        "name": "go_mf",
        "version": "4",
        "entity_field": "text",
        "id_field": "go_id",
        "roles": roles
        or {
            "proposer": {
                "system": "You assign GO molecular-function terms.",
                "user": "Annotation: {text}",
                "schema": {"type": "object"},
            },
            "critic": {
                "system": "You audit a proposer's picks.",
                "user": "Ballots: {ballots}",
                "schema": {"type": "object"},
            },
        },
    }


def _call(job_id: str, item_id: str, role: str, seat: str, **extra) -> dict:
    row = {
        "job_id": job_id,
        "item_id": item_id,
        "role": role,
        "seat_name": seat,
        "endpoint": "argo",
        "endpoint_kind": "openai",
        "served_model": f"test/{seat}",
        "canonical_model": f"test/{seat}",
        "submitted_by": "miles",
        "created_at": NOW,
    }
    row.update(extra)
    return row


def _build_store(path: Path) -> SqliteStore:
    """A REAL multi-table council store, written through the store's own API.

    Deliberately carries every shape the archiver has to get right:

    * three jobs -- one published-looking, one CANCELLED, one RETRY round
      whose ``parent_job_id`` points at the first (so the distinct-entity
      count differs from the item row sum);
    * items in ``ok``, ``error`` and ``cancelled``;
    * a SUPERSEDED call (``live`` false) sitting behind a live one with the
      same ``(job_id, item_id, role, seat_name, upstream_digest)``, created
      through the engine's real supersede update;
    * a ``reused`` attempt outcome with ``fresh_usage`` zeroed;
    * two query types, one of whose spec is an UNEXTRACTABLE shape;
    * ``endpoint_state`` counters and a worker whose ``version`` is NULL.
    """
    store = SqliteStore(str(path))
    store.init_schema()
    store.assign_store_identity(STORE_ID, declared_by="miles")

    store.put_query_type(name="go_mf", version="4", spec=_spec(), created_by="miles")
    # An UNEXTRACTABLE spec shape: no `roles` mapping at all.
    store.put_query_type(
        name="opaque", version="1", spec={"name": "opaque"}, created_by="miles"
    )

    base_job = {
        "job_id": "job-base",
        "label": "chunk01",
        "query_name": "go_mf",
        "query_version": "4",
        "submitted_by": "miles",
        "submitted_at": NOW,
        "status": "done",
    }
    store.create_job(
        base_job,
        [
            {"item_id": "f1", "entity_hash": "a" * 64, "evidence": {"text": "alpha"}},
            {"item_id": "f2", "entity_hash": "b" * 64, "evidence": {"text": "beta"}},
            {"item_id": "f3", "entity_hash": "c" * 64, "evidence": {"text": "gamma"}},
        ],
        [
            _call("job-base", "f1", "proposer", "p1", call_id="c-f1-p1"),
            _call("job-base", "f2", "proposer", "p1", call_id="c-f2-p1"),
            _call("job-base", "f3", "proposer", "p1", call_id="c-f3-p1"),
        ],
    )
    store.settle_item("job-base", "f1", "ok", verdict={"go_id": "GO:0003824"})
    store.settle_item("job-base", "f2", "error", verdict=None)

    # SUPERSESSION through the engine's own update: the first round's pending
    # calls go live=False, then a replacement live call takes the same key.
    with store.transaction() as conn:
        conn.execute(
            sa.update(store_schema.calls)
            .where(store_schema.calls.c.call_id == "c-f3-p1")
            .values(status="superseded", live=False)
        )
    store.add_calls(
        [_call("job-base", "f3", "proposer", "p1", call_id="c-f3-p1-live")]
    )

    # A reused call: a full copy of another call's reply with fresh_usage
    # zeroed, which is why row counts over calls are not a cost measure.
    store.add_calls(
        [
            _call(
                "job-base",
                "f1",
                "critic",
                "cr",
                call_id="c-f1-cr",
                status="ok",
                reply_text="the same reply, copied",
                reused_from="c-f1-p1",
                fresh_usage={"input_tokens": 0, "output_tokens": 0},
            )
        ]
    )
    store.record_attempt(
        "c-f1-cr",
        1,
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=1),
        outcome_kind="reused",
        usage={"input_tokens": 0, "output_tokens": 0},
        worker_id="w1",
    )
    store.record_attempt(
        "c-f1-p1",
        1,
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=3),
        outcome_kind="ok",
        usage={"input_tokens": 100, "output_tokens": 20},
        worker_id="w1",
    )

    # A CANCELLED job that looks authoritative by label and size.
    store.create_job(
        {
            "job_id": "job-cancelled",
            "label": "ADP1+PMI 19,276 -- option b",
            "query_name": "go_mf",
            "query_version": "4",
            "submitted_by": "miles",
            "submitted_at": NOW,
        },
        [{"item_id": "f1", "entity_hash": "a" * 64}],
        [_call("job-cancelled", "f1", "proposer", "p1", call_id="c-cx-p1")],
    )
    store.cancel_job("job-cancelled")

    # A RETRY round: re-runs a SUBSET of the base job's items, so summing
    # item rows across jobs double-counts every retried item.
    store.create_job(
        {
            "job_id": "job-retry",
            "label": "error retry",
            "query_name": "go_mf",
            "query_version": "4",
            "submitted_by": "miles",
            "submitted_at": NOW,
            "status": "done",
            "parent_job_id": "job-base",
        },
        [{"item_id": "f2", "entity_hash": "b" * 64}],
        [_call("job-retry", "f2", "proposer", "p1", call_id="c-rt-p1")],
    )
    store.settle_item("job-retry", "f2", "ok", verdict={"go_id": "GO:0016740"})

    store.add_tokens("argo", 1200, user=None)
    store.register_worker(
        {"worker_id": "w1", "user": "local", "host": "poplar", "started_at": NOW}
    )
    return store


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
def snapshot(store: SqliteStore, tmp_path: Path) -> dict:
    """A REAL snapshot through the EXISTING SqliteStore.snapshot()."""
    return store.snapshot(tmp_path / "snaps" / "snap1.sqlite")


def _archived(sink, snapshot: dict, *, seq: int = 1, state_root: Path, **kwargs):
    return archive_snapshot(
        sink, snapshot, snapshot_seq=seq, state_root=state_root, **kwargs
    )


@pytest.fixture
def state_root(tmp_path: Path) -> Path:
    return tmp_path / "deliveries"


# --------------------------------------------------------------------------
# What this component writes, and what it deliberately does not
# --------------------------------------------------------------------------


class TestScope:
    def test_the_archiver_writes_eight_of_the_nine_tables(self) -> None:
        """job_disposition is NOT its business: it writes no dispositions."""
        assert set(ARCHIVER_TABLES) | set(NOT_WRITTEN_BY_THE_ARCHIVER) == set(
            ARCHIVED_TABLES
        )
        assert JOB_DISPOSITION_TABLE in NOT_WRITTEN_BY_THE_ARCHIVER
        assert JOB_DISPOSITION_TABLE not in ARCHIVER_TABLES

    def test_endpoint_state_is_written_FIRST(self) -> None:
        """It is the only per-gateway token record there is. A store wiped
        before it is archived loses the cost-calibration data permanently."""
        assert ARCHIVER_TABLES[0] == "endpoint_state"

    def test_role_brief_is_written_last(self) -> None:
        """Derived from query_types.spec, which is archived in full before it."""
        assert ARCHIVER_TABLES[-1] == ROLE_BRIEF_TABLE

    def test_no_docstring_promises_rollback(self) -> None:
        """load_batch_id exists for idempotency and provenance only."""
        source = Path(council_archive.__file__).read_text(encoding="utf-8")
        lowered = " ".join(source.lower().split())
        assert "rollback is not implemented" in lowered
        # Nothing anywhere offers it. Checked on the whitespace-collapsed
        # text, because a denial that wraps across two lines ("there is NO\n
        # ROLLBACK") is still a denial.
        for offer in (
            "rolls back",
            "will roll back",
            "can be rolled back",
            "rollback is supported",
            "supports rollback",
            "undo a committed",
        ):
            assert offer not in lowered, offer

    def test_batches_are_sized_by_bytes_under_the_spark_threshold(self) -> None:
        assert SPARK_LOCAL_RELATION_CACHE_THRESHOLD_BYTES == 1 << 20
        assert 0 < MAX_BATCH_BYTES < SPARK_LOCAL_RELATION_CACHE_THRESHOLD_BYTES


# --------------------------------------------------------------------------
# A faithful archive
# --------------------------------------------------------------------------


class TestAFaithfulArchive:
    def test_every_declared_column_is_present_on_every_archived_table(
        self, sink, snapshot, state_root
    ) -> None:
        _archived(sink, snapshot, state_root=state_root)
        for table in ARCHIVER_TABLES:
            rows = sink.select_rows(table)
            assert rows, table
            for row in rows:
                assert tuple(row) == column_names(table), table

    def test_every_row_count_equals_the_STORE_count_not_the_live_only_count(
        self, sink, snapshot, state_root
    ) -> None:
        counts = snapshot_row_counts(snapshot["path"])
        report = _archived(sink, snapshot, state_root=state_root)
        for table, expected in counts.items():
            assert report["reconciliation"]["rows_written"][table] == expected, table
            assert sink.count_rows(table) == expected, table
        # And the point of the claim: there ARE non-live calls, so a
        # live-only archive would have been smaller.
        assert report["reconciliation"]["calls"]["not_live"] > 0
        assert counts["calls"] > report["reconciliation"]["calls"]["live"]

    def test_the_cancelled_pilot_and_retry_jobs_are_all_archived(
        self, sink, snapshot, state_root
    ) -> None:
        """Selection is a separate layer; this one archives everything."""
        _archived(sink, snapshot, state_root=state_root)
        jobs = {row["job_id"]: row for row in sink.select_rows("jobs")}
        assert set(jobs) == {"job-base", "job-cancelled", "job-retry"}
        assert jobs["job-cancelled"]["status"] == "cancelled"
        assert jobs["job-retry"]["parent_job_id"] == "job-base"

    def test_live_is_readable_PER_ROW(self, sink, snapshot, state_root) -> None:
        _archived(sink, snapshot, state_root=state_root)
        by_id = {row["call_id"]: row for row in sink.select_rows("calls")}
        assert by_id["c-f3-p1"]["live"] in (0, False)
        assert by_id["c-f3-p1-live"]["live"] in (1, True)
        assert by_id["c-f3-p1"]["status"] == "superseded"

    def test_the_superseded_call_and_its_replacement_are_both_kept(
        self, sink, snapshot, state_root
    ) -> None:
        """Filtering live here would destroy the only record of what was
        superseded, and break reuse rehydration."""
        _archived(sink, snapshot, state_root=state_root)
        rows = sink.select_rows(
            "calls", where={"job_id": "job-base", "item_id": "f3", "role": "proposer"}
        )
        assert len(rows) == 2
        assert sorted(bool(r["live"]) for r in rows) == [False, True]

    def test_the_reused_call_keeps_its_full_reply_and_zeroed_usage(
        self, sink, snapshot, state_root
    ) -> None:
        """Deduplicating calls by reply_text would corrupt the record, and
        row counts over calls are not a cost measure."""
        _archived(sink, snapshot, state_root=state_root)
        row = sink.select_rows("calls", where={"call_id": "c-f1-cr"})[0]
        assert row["reply_text"] == "the same reply, copied"
        assert json.loads(row["fresh_usage"])["output_tokens"] == 0
        attempt = sink.select_rows("attempts", where={"call_id": "c-f1-cr"})[0]
        assert attempt["outcome_kind"] == "reused"

    def test_jobs_deleted_at_is_carried_as_a_column_and_not_acted_on(
        self, store, sink, tmp_path, state_root
    ) -> None:
        store.delete_job("job-retry")
        manifest = store.snapshot(tmp_path / "snaps" / "tomb.sqlite")
        _archived(sink, manifest, state_root=state_root)
        row = sink.select_rows("jobs", where={"job_id": "job-retry"})[0]
        assert row["deleted_at"] is not None
        # Still archived -- a tombstone is a record, not an exclusion.
        assert sink.count_rows("jobs") == 3

    def test_workers_version_NULL_is_preserved(
        self, sink, snapshot, state_root
    ) -> None:
        """Inventing a version would be worse than recording that it was not
        captured."""
        _archived(sink, snapshot, state_root=state_root)
        rows = sink.select_rows("workers")
        assert len(rows) == 1
        assert rows[0]["version"] is None
        assert rows[0]["worker_id"] == "w1"
        assert sink.count_rows("workers", where={"version": None}) == 1

    def test_the_dropped_columns_are_absent(self, sink, snapshot, state_root) -> None:
        _archived(sink, snapshot, state_root=state_root)
        call_columns = set(column_names("calls"))
        for dropped in DROPPED_CALL_COLUMNS:
            assert dropped not in call_columns, dropped
        assert "priority" not in set(column_names("jobs"))
        assert "item_order" not in set(column_names("items"))
        for dropped in ("halt_kind", "halt_message"):
            assert dropped not in set(column_names("endpoint_state"))
        for dropped in ("pid", "heartbeat_at", "endpoints", "kind", "user"):
            assert dropped not in set(column_names("workers")), dropped

    def test_items_carries_council_entity_hash_and_NO_bare_entity_hash(
        self, sink, snapshot, state_root
    ) -> None:
        """It matched the clearinghouse entity_hash 0 of 19,268 times; a bare
        column in a BERDL namespace is an invitation to join on it."""
        _archived(sink, snapshot, state_root=state_root)
        columns = set(column_names("items"))
        assert COUNCIL_ENTITY_HASH_COLUMN in columns
        assert "entity_hash" not in columns
        row = sink.select_rows("items", where={"item_id": "f1", "job_id": "job-base"})[0]
        assert row[COUNCIL_ENTITY_HASH_COLUMN] == "a" * 64

    def test_a_json_null_stays_distinguishable_from_a_sql_null(
        self, store, sink, tmp_path, state_root
    ) -> None:
        """The store writes Python None into a JSON column as the TEXT
        'null', so a typed read collapses it with a real NULL and
        `reply_parsed IS NOT NULL` is true for every row. The archiver reads
        raw text, so the two stay apart."""
        with store.transaction() as conn:
            conn.execute(
                sa.update(store_schema.calls)
                .where(store_schema.calls.c.call_id == "c-f1-p1")
                .values(reply_parsed=None)
            )
            # A real SQL NULL, written past the typed layer.
            conn.exec_driver_sql(
                "UPDATE calls SET reply_parsed = NULL WHERE call_id = 'c-f2-p1'"
            )
        manifest = store.snapshot(tmp_path / "snaps" / "nulls.sqlite")
        _archived(sink, manifest, state_root=state_root)

        as_json_null = sink.select_rows("calls", where={"call_id": "c-f1-p1"})[0]
        as_sql_null = sink.select_rows("calls", where={"call_id": "c-f2-p1"})[0]
        assert as_json_null["reply_parsed"] == "null"
        assert as_sql_null["reply_parsed"] is None


# --------------------------------------------------------------------------
# The two deliberate denormalisations
# --------------------------------------------------------------------------


class TestTheQueryNameDenormalisations:
    def test_items_query_name_comes_from_its_job(
        self, sink, snapshot, state_root
    ) -> None:
        _archived(sink, snapshot, state_root=state_root)
        for row in sink.select_rows("items"):
            assert row["query_name"] == "go_mf", row

    def test_attempts_query_name_comes_from_the_owning_call(
        self, sink, snapshot, state_root
    ) -> None:
        _archived(sink, snapshot, state_root=state_root)
        for row in sink.select_rows("attempts"):
            assert row["query_name"] == "go_mf", row

    def test_a_legacy_NULL_call_query_name_falls_back_to_the_job(
        self, store, sink, tmp_path, state_root
    ) -> None:
        with store.transaction() as conn:
            conn.exec_driver_sql(
                "UPDATE calls SET query_name = NULL WHERE call_id = 'c-f1-p1'"
            )
        manifest = store.snapshot(tmp_path / "snaps" / "legacy.sqlite")
        _archived(sink, manifest, state_root=state_root)
        row = sink.select_rows("attempts", where={"call_id": "c-f1-p1"})[0]
        assert row["query_name"] == "go_mf"
        assert sink.select_rows("calls", where={"call_id": "c-f1-p1"})[0][
            "query_name"
        ] is None

    def test_a_CONTRADICTORY_non_null_query_name_is_REJECTED(
        self, store, sink, tmp_path, state_root
    ) -> None:
        """Not resolved in favour of either side: query_name is the partition
        key of an append-only table, so a wrong value is baked into the
        physical layout and cannot be corrected without a full rewrite."""
        with store.transaction() as conn:
            conn.exec_driver_sql(
                "UPDATE calls SET query_name = 'something_else' "
                "WHERE call_id = 'c-f1-p1'"
            )
        manifest = store.snapshot(tmp_path / "snaps" / "contradiction.sqlite")
        with pytest.raises(ContradictoryQueryNameError) as excinfo:
            _archived(sink, manifest, state_root=state_root)
        assert "something_else" in str(excinfo.value)
        assert "go_mf" in str(excinfo.value)


# --------------------------------------------------------------------------
# role_brief: DERIVED, and unextractable rather than guessed
# --------------------------------------------------------------------------


class TestRoleBrief:
    def test_the_mission_text_is_the_roles_own_system_prompt(
        self, sink, snapshot, state_root
    ) -> None:
        _archived(sink, snapshot, state_root=state_root)
        rows = {
            (r["query_name"], r["role"]): r for r in sink.select_rows(ROLE_BRIEF_TABLE)
        }
        assert rows[("go_mf", "proposer")]["mission_text"].startswith(
            "You assign GO"
        )
        assert rows[("go_mf", "critic")]["mission_text"].startswith("You audit")
        assert json.loads(rows[("go_mf", "proposer")]["answer_schema"]) == {
            "type": "object"
        }

    def test_an_unextractable_spec_shape_is_RECORDED_not_guessed(
        self, sink, snapshot, state_root
    ) -> None:
        _archived(sink, snapshot, state_root=state_root)
        rows = sink.select_rows(ROLE_BRIEF_TABLE, where={"query_name": "opaque"})
        assert len(rows) == 1
        assert rows[0]["role"] == ROLE_BRIEF_UNEXTRACTABLE
        assert rows[0]["mission_text"] == ROLE_BRIEF_UNEXTRACTABLE

    def test_a_spec_that_is_the_json_literal_null_is_unextractable(
        self, store, tmp_path
    ) -> None:
        """The shape that would otherwise pass silently: the store stores a
        Python None as the TEXT 'null', so this has to test the PARSED value
        rather than the column for SQL NULL."""
        with store.transaction() as conn:
            conn.exec_driver_sql(
                "UPDATE query_types SET spec = 'null' WHERE name = 'opaque'"
            )
        manifest = store.snapshot(tmp_path / "snaps" / "nullspec.sqlite")
        connection = sqlite3.connect(manifest["path"])
        connection.row_factory = sqlite3.Row
        try:
            rows = {
                (r["query_name"], r["role"]): r for r in role_brief_rows(connection)
            }
        finally:
            connection.close()
        assert rows[("opaque", ROLE_BRIEF_UNEXTRACTABLE)][
            "mission_text"
        ] == ROLE_BRIEF_UNEXTRACTABLE

    def test_a_role_with_no_system_text_is_unextractable(self, tmp_path) -> None:
        store = SqliteStore(str(tmp_path / "s2" / "council.sqlite"))
        try:
            store.init_schema()
            store.put_query_type(
                name="half",
                version="1",
                spec={"roles": {"proposer": {"user": "only a user template"}}},
                created_by="miles",
            )
            connection = store.engine.raw_connection().driver_connection
            connection.row_factory = sqlite3.Row
            rows = role_brief_rows(connection)
        finally:
            store.close()
        assert len(rows) == 1
        assert rows[0]["role"] == "proposer"
        assert rows[0]["mission_text"] == ROLE_BRIEF_UNEXTRACTABLE


# --------------------------------------------------------------------------
# Identity: from the MANIFEST, never from the path
# --------------------------------------------------------------------------


class TestIdentityComesFromTheManifest:
    def test_the_identity_columns_are_the_manifests_values(
        self, sink, snapshot, state_root
    ) -> None:
        _archived(sink, snapshot, state_root=state_root, seq=7)
        for table in ARCHIVER_TABLES:
            for row in sink.select_rows(table):
                assert row["store_id"] == STORE_ID, table
                assert row["snapshot_id"] == snapshot["snapshot_id"], table
                assert int(row["snapshot_seq"]) == 7, table
                assert row["store_host"] == snapshot["store_host"], table
                assert row["load_batch_id"], table

    def test_a_MISLEADING_file_path_changes_nothing(
        self, store, sink, tmp_path, state_root
    ) -> None:
        """A store that moves off /scratch must keep its id, so the id may
        never be derived from where the file happens to sit."""
        misleading = tmp_path / "laptop" / "someone-elses-council" / "copy.sqlite"
        manifest = store.snapshot(misleading)
        _archived(sink, manifest, state_root=state_root)
        rows = sink.select_rows("jobs")
        assert {row["store_id"] for row in rows} == {STORE_ID}
        assert "laptop" not in STORE_ID
        assert {row["snapshot_id"] for row in rows} == {manifest["snapshot_id"]}

    def test_the_snapshot_id_is_the_verified_digest_of_the_file(
        self, snapshot
    ) -> None:
        import hashlib

        digest = hashlib.sha256(Path(snapshot["path"]).read_bytes()).hexdigest()
        assert snapshot["snapshot_id"] == digest


# --------------------------------------------------------------------------
# Snapshot versioning: two snapshots of a MUTATED store
# --------------------------------------------------------------------------


class TestSnapshotVersioning:
    @pytest.fixture
    def two_snapshots(self, store, sink, tmp_path, state_root):
        """Archive snapshot 1, MUTATE the store, archive snapshot 2."""
        first = store.snapshot(tmp_path / "snaps" / "s1.sqlite")
        _archived(sink, first, seq=1, state_root=state_root)

        # A NEW CALL UNDER AN OLD JOB, a CHANGED job status, and ADVANCED
        # endpoint_state counters -- the three mutations that make
        # append-only-by-job incoherent.
        store.add_calls(
            [_call("job-base", "f2", "critic", "cr", call_id="c-f2-cr-new")]
        )
        store.set_job_status("job-base", "paused")
        store.add_tokens("argo", 800, user=None)

        second = store.snapshot(tmp_path / "snaps" / "s2.sqlite")
        _archived(sink, second, seq=2, state_root=state_root)
        return {"first": first, "second": second}

    def test_the_two_snapshots_have_different_digests(self, two_snapshots) -> None:
        assert (
            two_snapshots["first"]["snapshot_id"]
            != two_snapshots["second"]["snapshot_id"]
        )

    def test_both_are_independently_queryable(self, sink, two_snapshots) -> None:
        first, second = two_snapshots["first"], two_snapshots["second"]
        s1 = sink.select_rows(
            "jobs", where={"snapshot_id": first["snapshot_id"], "job_id": "job-base"}
        )
        s2 = sink.select_rows(
            "jobs", where={"snapshot_id": second["snapshot_id"], "job_id": "job-base"}
        )
        assert len(s1) == 1 and len(s2) == 1
        assert s1[0]["status"] == "done"
        assert s2[0]["status"] == "paused"

    def test_NO_row_was_overwritten(self, sink, two_snapshots) -> None:
        """Snapshot 1's job-base row still says what it said."""
        rows = sink.select_rows("jobs", where={"job_id": "job-base"})
        assert len(rows) == 2
        assert sorted(int(r["snapshot_seq"]) for r in rows) == [1, 2]
        assert sorted(r["status"] for r in rows) == ["done", "paused"]

    def test_the_new_call_under_the_OLD_job_is_in_snapshot_2_only(
        self, sink, two_snapshots
    ) -> None:
        """Skipping known jobs would have lost it entirely."""
        rows = sink.select_rows("calls", where={"call_id": "c-f2-cr-new"})
        assert len(rows) == 1
        assert rows[0]["snapshot_id"] == two_snapshots["second"]["snapshot_id"]

    def test_the_current_projection_resolves_by_snapshot_seq(
        self, sink, two_snapshots
    ) -> None:
        rows = current_projection(
            sink, "jobs", store_id=STORE_ID, where={"job_id": "job-base"}
        )
        assert len(rows) == 1
        assert rows[0]["status"] == "paused"
        assert int(rows[0]["snapshot_seq"]) == 2

    def test_endpoint_state_is_NOT_additive_across_snapshots(
        self, sink, two_snapshots
    ) -> None:
        """Its counters are MUTABLE CUMULATIVE and shared by every job, so
        each archived row is a point-in-time observation of a running total.
        Summing across snapshots multiplies the same spend."""
        rows = sink.select_rows("endpoint_state", where={"endpoint": "argo"})
        assert len(rows) == 2
        by_seq = {int(r["snapshot_seq"]): int(r["lifetime_tokens"]) for r in rows}
        assert by_seq[1] == 1200
        assert by_seq[2] == 2000
        # The wrong-and-plausible reading, named so the test says what it
        # guards: the sum is 3200 and the real spend is 2000.
        assert sum(by_seq.values()) == 3200
        current = current_projection(
            sink, "endpoint_state", store_id=STORE_ID, where={"endpoint": "argo"}
        )
        assert len(current) == 1
        assert int(current[0]["lifetime_tokens"]) == 2000

    def test_a_conflicting_snapshot_seq_is_refused(
        self, store, sink, tmp_path, state_root, two_snapshots
    ) -> None:
        store.add_tokens("argo", 1, user=None)
        third = store.snapshot(tmp_path / "snaps" / "s3.sqlite")
        with pytest.raises(SnapshotConflictError) as excinfo:
            _archived(sink, third, seq=2, state_root=state_root)
        assert "snapshot_seq" in str(excinfo.value)

    def test_a_conflicting_digest_at_a_new_seq_is_refused(
        self, sink, state_root, two_snapshots
    ) -> None:
        """The same bytes cannot sit at two points in one store's history."""
        with pytest.raises(SnapshotConflictError) as excinfo:
            _archived(sink, two_snapshots["first"], seq=9, state_root=state_root)
        assert two_snapshots["first"]["snapshot_id"] in str(excinfo.value)

    def test_the_conflict_refusal_exits_non_zero_in_a_real_process(
        self, sink, state_root, two_snapshots, tmp_path
    ) -> None:
        import subprocess
        import sys

        script = f"""
import sys
sys.path.insert(0, {str(Path.cwd() / "src")!r})
from kbutillib.domains.kbase.berdl.council_archive import archive_snapshot
from kbutillib.domains.kbase.berdl.council_sink import SqliteCouncilSink
sink = SqliteCouncilSink({str(tmp_path / "lake" / "council.sqlite")!r})
archive_snapshot(
    sink,
    {two_snapshots["first"]!r},
    snapshot_seq=9,
    state_root={str(state_root)!r},
)
"""
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True
        )
        assert result.returncode != 0
        assert "SnapshotConflictError" in result.stderr


# --------------------------------------------------------------------------
# The load ledger: completion, crash recovery, receipts
# --------------------------------------------------------------------------


class TestTheLoadLedger:
    def test_a_finished_delivery_is_complete_and_publishable(
        self, sink, snapshot, state_root
    ) -> None:
        report = _archived(sink, snapshot, state_root=state_root)
        assert report["complete"] is True
        assert report["publishable"] is True
        assert report["status"]["incomplete"] == []
        assert require_complete_delivery(
            sink,
            store_id=STORE_ID,
            snapshot_id=snapshot["snapshot_id"],
            state_root=state_root,
        )["complete"] is True

    def test_the_delivery_is_bound_to_the_digest_namespace_and_version(
        self, sink, snapshot, state_root
    ) -> None:
        _archived(sink, snapshot, state_root=state_root)
        ledger = ArchiveLedger(
            delivery_state_dir(
                store_id=STORE_ID,
                snapshot_id=snapshot["snapshot_id"],
                root=state_root,
            )
        )
        binding = ledger.read_delivery()
        assert binding["snapshot_id"] == snapshot["snapshot_id"]
        assert binding["namespace"] == "kbaseincubator.council"
        assert binding["archive_schema_version"] == 1
        assert binding["not_written_here"] == [JOB_DISPOSITION_TABLE]

    def test_the_inventory_records_expected_rows_and_a_digest_per_batch(
        self, snapshot
    ) -> None:
        inventory = build_inventory(
            snapshot["path"], snapshot_id=snapshot["snapshot_id"]
        )
        for table in ARCHIVER_TABLES:
            entry = inventory["tables"][table]
            assert entry["expected_rows"] >= 0
            for batch in entry["batches"]:
                assert batch["batch_id"].startswith(snapshot["snapshot_id"][:16])
                assert batch["expected_rows"] > 0
                assert len(batch["expected_digest"]) == 64

    def test_the_inventory_is_reproducible(self, snapshot) -> None:
        """A resume compares against the ORIGINAL digests, so a second read
        of the same snapshot must produce the same ones."""
        first = build_inventory(
            snapshot["path"], snapshot_id=snapshot["snapshot_id"]
        )
        second = build_inventory(
            snapshot["path"], snapshot_id=snapshot["snapshot_id"]
        )
        assert first == second

    def test_resuming_against_a_different_binding_is_refused(
        self, sink, snapshot, state_root
    ) -> None:
        _archived(sink, snapshot, state_root=state_root, seq=1)
        ledger = ArchiveLedger(
            delivery_state_dir(
                store_id=STORE_ID,
                snapshot_id=snapshot["snapshot_id"],
                root=state_root,
            )
        )
        with pytest.raises(DeliveryBindingError):
            ledger.write_delivery(
                {
                    "store_id": STORE_ID,
                    "snapshot_id": snapshot["snapshot_id"],
                    "snapshot_seq": 1,
                    "namespace": "kbaseincubator.somewhere_else",
                    "archive_schema_version": 1,
                }
            )

    def test_a_receipt_is_re_exported_from_the_completion_record(
        self, sink, snapshot, state_root
    ) -> None:
        _archived(sink, snapshot, state_root=state_root, seq=3)
        ledger = ArchiveLedger(
            delivery_state_dir(
                store_id=STORE_ID,
                snapshot_id=snapshot["snapshot_id"],
                root=state_root,
            )
        )
        first = receipt(sink, ledger)
        second = receipt(sink, ledger)
        assert first["complete"] is True
        assert first["snapshot_id"] == snapshot["snapshot_id"]
        assert first["snapshot_seq"] == 3
        assert first["tables"] == second["tables"]


class _FailOn:
    """A table writer that commits every table except one, which raises.

    This is how the REAL archiver is interrupted: it is handed a writer that
    fails on `attempts`, so `calls` commits and `attempts` does not -- the
    exact shape of a crash between two independent Iceberg commits. Nothing
    about the ledger is written by hand.
    """

    def __init__(self, table: str) -> None:
        self.table = table
        self.written: list[str] = []

    def __call__(self, sink, table, rows):
        if table == self.table:
            raise RuntimeError(f"simulated crash before committing {table!r}")
        self.written.append(table)
        return sink.append_rows(table, rows)


class TestCrashBetweenTables:
    """A crash after `calls` and before `attempts`.

    It leaves convincing but INCOMPLETE provenance: it reads as a complete
    archive to every query that does not join `attempts`, and checking only
    `jobs` gives a false no-op.
    """

    @pytest.fixture
    def crashed(self, sink, snapshot, state_root):
        writer = _FailOn("attempts")
        with pytest.raises(RuntimeError, match="simulated crash"):
            _archived(sink, snapshot, state_root=state_root, table_writer=writer)
        return {"writer": writer, "snapshot": snapshot}

    def test_calls_committed_and_attempts_did_not(self, sink, crashed) -> None:
        assert sink.count_rows("calls") > 0
        assert sink.count_rows("attempts") == 0
        assert "calls" in crashed["writer"].written

    def test_the_delivery_reports_INCOMPLETE(self, sink, crashed, state_root) -> None:
        ledger = ArchiveLedger(
            delivery_state_dir(
                store_id=STORE_ID,
                snapshot_id=crashed["snapshot"]["snapshot_id"],
                root=state_root,
            )
        )
        status = reconcile_delivery(sink, ledger)
        assert status["complete"] is False
        assert "attempts" in status["incomplete"]
        # The tables that DID land reconcile -- which is exactly why
        # checking one table is not a completeness test.
        assert status["tables"]["calls"]["reconciled"] is True
        assert status["tables"]["jobs"]["reconciled"] is True

    def test_it_is_NOT_publishable_and_the_refusal_names_the_table(
        self, sink, crashed, state_root
    ) -> None:
        ledger = ArchiveLedger(
            delivery_state_dir(
                store_id=STORE_ID,
                snapshot_id=crashed["snapshot"]["snapshot_id"],
                root=state_root,
            )
        )
        assert is_publishable(sink, ledger) is False
        with pytest.raises(DeliveryIncompleteError) as excinfo:
            require_complete_delivery(
                sink,
                store_id=STORE_ID,
                snapshot_id=crashed["snapshot"]["snapshot_id"],
                state_root=state_root,
            )
        assert "attempts" in str(excinfo.value)

    def test_a_snapshot_with_NO_delivery_at_all_is_refused_with_its_reason(
        self, sink, state_root
    ) -> None:
        """No ledger is not "complete by default", and the refusal must not
        trip over its own empty per-table report."""
        with pytest.raises(DeliveryIncompleteError) as excinfo:
            require_complete_delivery(
                sink,
                store_id=STORE_ID,
                snapshot_id="f" * 64,
                state_root=state_root,
            )
        assert "no delivery binding" in str(excinfo.value)

    def test_the_failed_batch_is_recorded_as_failed_not_silently_retried(
        self, crashed, state_root
    ) -> None:
        ledger = ArchiveLedger(
            delivery_state_dir(
                store_id=STORE_ID,
                snapshot_id=crashed["snapshot"]["snapshot_id"],
                root=state_root,
            )
        )
        failed = [
            key
            for key, record in ledger.records().items()
            if record["state"] == "failed"
        ]
        assert failed and all("attempts" in key for key in failed)

    def test_it_RESUMES_without_duplicating_the_calls_rows(
        self, sink, crashed, state_root
    ) -> None:
        """The whole point. Replaying the snapshot would duplicate every
        committed row, and there is no rollback."""
        calls_before = sink.count_rows("calls")
        report = _archived(sink, crashed["snapshot"], state_root=state_root)

        assert report["complete"] is True
        assert sink.count_rows("calls") == calls_before
        assert sink.count_rows("attempts") > 0
        # `calls` was skipped from the ledger, not re-read and re-appended.
        assert report["tables"]["calls"]["written"] == 0
        assert report["tables"]["calls"]["skipped"] == calls_before
        assert report["tables"]["attempts"]["written"] > 0

    def test_the_resumed_delivery_is_publishable(
        self, sink, crashed, state_root
    ) -> None:
        _archived(sink, crashed["snapshot"], state_root=state_root)
        assert require_complete_delivery(
            sink,
            store_id=STORE_ID,
            snapshot_id=crashed["snapshot"]["snapshot_id"],
            state_root=state_root,
        )["complete"] is True


class TestTheAmbiguousAppend:
    def test_a_batch_left_started_whose_rows_ALL_landed_is_resolved_not_retried(
        self, sink, snapshot, state_root
    ) -> None:
        """A `started` line with no terminal line is ambiguous: the rows may
        or may not be in the table. It is resolved FROM THE BATCH IDENTITY
        before being retried."""

        class _WriteThenVanish:
            """Commits the rows but never reports back, so the ledger keeps
            its `started` line -- the page-cache-death shape."""

            def __call__(self, inner_sink, table, rows):
                inner_sink.append_rows(table, rows)
                if table == "attempts":
                    raise SystemExit("died after the append, before the ledger line")
                return None

        with pytest.raises(SystemExit):
            _archived(
                sink, snapshot, state_root=state_root, table_writer=_WriteThenVanish()
            )
        attempts_before = sink.count_rows("attempts")
        assert attempts_before > 0

        report = _archived(sink, snapshot, state_root=state_root)
        assert sink.count_rows("attempts") == attempts_before
        assert report["resumed"]
        assert report["complete"] is True

    def test_a_PARTIALLY_landed_batch_raises_rather_than_duplicating(
        self, sink, snapshot, state_root
    ) -> None:
        """Retrying would duplicate the rows that landed and there is no
        rollback, so it stops and says which batch."""

        class _WriteHalfThenVanish:
            def __call__(self, inner_sink, table, rows):
                if table == "attempts":
                    inner_sink.append_rows(table, rows[:1])
                    raise SystemExit("died mid-append")
                return inner_sink.append_rows(table, rows)

        with pytest.raises(SystemExit):
            _archived(
                sink,
                snapshot,
                state_root=state_root,
                table_writer=_WriteHalfThenVanish(),
            )
        with pytest.raises(AmbiguousBatchError) as excinfo:
            _archived(sink, snapshot, state_root=state_root)
        assert "attempts" in str(excinfo.value)
        assert "no rollback" in str(excinfo.value).lower()


# --------------------------------------------------------------------------
# The reconciliation report
# --------------------------------------------------------------------------


class TestTheReconciliationReport:
    def test_rows_read_and_written_per_table(self, sink, snapshot, state_root) -> None:
        report = _archived(sink, snapshot, state_root=state_root)["reconciliation"]
        assert set(report["rows_read"]) == set(ARCHIVER_TABLES)
        assert all(report["reconciled"].values())

    def test_the_calls_live_split_is_reported(self, sink, snapshot, state_root) -> None:
        report = _archived(sink, snapshot, state_root=state_root)["reconciliation"]
        assert report["calls"]["live"] + report["calls"]["not_live"] == (
            report["calls"]["rows"]
        )
        assert report["calls"]["not_live"] == 2  # the superseded + the cancelled

    def test_items_are_reported_by_status(self, sink, snapshot, state_root) -> None:
        report = _archived(sink, snapshot, state_root=state_root)["reconciliation"]
        assert report["items_by_status"]["ok"] == 2
        assert report["items_by_status"]["error"] == 1
        assert report["items_by_status"]["cancelled"] >= 1

    def test_the_DISTINCT_entity_count_differs_from_the_row_sum(
        self, sink, snapshot, state_root
    ) -> None:
        """Summing item rows across jobs double-counts every retried item:
        the retry job re-runs a subset of the base job's items. This is how a
        previous manifest reported 19,405 items where the truth over unique
        functions is 19,276."""
        report = _archived(sink, snapshot, state_root=state_root)["reconciliation"]
        assert report["items_rows"] == 5  # 3 base + 1 cancelled + 1 retry
        assert report["distinct_items"] == 3  # f1, f2, f3
        assert report["distinct_items"] < report["items_rows"]
        assert report["retry_jobs"] == ["job-retry"]

    def test_retry_rounds_are_identified_by_parent_job_id(
        self, sink, snapshot, state_root
    ) -> None:
        report = _archived(sink, snapshot, state_root=state_root)["reconciliation"]
        assert report["retry_jobs"] == ["job-retry"]

    def test_the_report_says_endpoint_state_is_not_additive(
        self, sink, snapshot, state_root
    ) -> None:
        report = _archived(sink, snapshot, state_root=state_root)["reconciliation"]
        assert "never additive" in report["endpoint_state_note"].lower()

    def test_the_report_says_job_disposition_is_not_its_business(
        self, sink, snapshot, state_root
    ) -> None:
        report = _archived(sink, snapshot, state_root=state_root)["reconciliation"]
        assert "not written by the archiver" in report["job_disposition"]
        assert sink.count_rows(JOB_DISPOSITION_TABLE) == 0


# --------------------------------------------------------------------------
# prepare_snapshot: a DECLARED scratch path, and failing clean
# --------------------------------------------------------------------------


class TestPrepareSnapshot:
    def test_an_uncompressed_snapshot_verifies_in_place(
        self, snapshot, tmp_path
    ) -> None:
        prepared = prepare_snapshot(
            snapshot["path"],
            snapshot_id=snapshot["snapshot_id"],
            scratch_dir=tmp_path / "scratch",
        )
        assert prepared["decompressed"] is False
        assert prepared["verified"] is True
        assert prepared["path"] == snapshot["path"]

    def test_an_xz_snapshot_decompresses_to_the_declared_scratch_path(
        self, snapshot, tmp_path
    ) -> None:
        import lzma

        compressed = tmp_path / "hand-off" / "snap.sqlite.xz"
        compressed.parent.mkdir(parents=True, exist_ok=True)
        compressed.write_bytes(lzma.compress(Path(snapshot["path"]).read_bytes()))

        scratch = tmp_path / "declared-scratch"
        prepared = prepare_snapshot(
            compressed, snapshot_id=snapshot["snapshot_id"], scratch_dir=scratch
        )
        assert prepared["decompressed"] is True
        assert Path(prepared["path"]).parent == scratch
        assert snapshot_row_counts(prepared["path"])["jobs"] == 3

    def test_a_digest_that_disagrees_leaves_NOTHING_behind(
        self, snapshot, tmp_path
    ) -> None:
        """A half-written SQLite file opens fine and is wrong, so a failed
        prepare must not leave one."""
        import lzma

        compressed = tmp_path / "snap.sqlite.xz"
        compressed.write_bytes(lzma.compress(Path(snapshot["path"]).read_bytes()))
        scratch = tmp_path / "scratch"
        with pytest.raises(SnapshotPrepareError) as excinfo:
            prepare_snapshot(compressed, snapshot_id="f" * 64, scratch_dir=scratch)
        assert "snapshot_id" in str(excinfo.value)
        assert list(scratch.iterdir()) == []

    def test_an_absent_source_is_refused(self, tmp_path) -> None:
        with pytest.raises(SnapshotPrepareError):
            prepare_snapshot(
                tmp_path / "nope.sqlite",
                snapshot_id="a" * 64,
                scratch_dir=tmp_path / "scratch",
            )

    def test_a_corrupt_xz_leaves_nothing_behind(self, tmp_path) -> None:
        compressed = tmp_path / "broken.sqlite.xz"
        compressed.write_bytes(b"this is not xz data")
        scratch = tmp_path / "scratch"
        with pytest.raises(SnapshotPrepareError):
            prepare_snapshot(
                compressed, snapshot_id="a" * 64, scratch_dir=scratch
            )
        assert list(scratch.iterdir()) == []


# --------------------------------------------------------------------------
# snapshot() under an OPEN WRITE TRANSACTION -- the gap in the earlier tests
# --------------------------------------------------------------------------


class TestSnapshotUnderAnOpenWriteTransaction:
    """A plain file copy of a live WAL SQLite yields a torn backup that OPENS
    FINE AND IS WRONG. The backup API does not.

    The earlier identity tests
    (``test_store_identity.py::TestSnapshotAndExportOutput::test_the_snapshot_is_a_real_openable_store_not_a_mid_write_copy``)
    showed the snapshot is openable and carries committed data; neither they
    nor any other test took one while a write transaction was OPEN. That is
    what this class adds.
    """

    def test_the_snapshot_matches_COMMITTED_state_not_the_open_transaction(
        self, store, tmp_path
    ) -> None:
        committed = snapshot_row_counts(
            store.snapshot(tmp_path / "before.sqlite")["path"]
        )
        target = tmp_path / "during.sqlite"

        with store.transaction() as conn:
            # An UNCOMMITTED insert, live in the WAL, while the snapshot runs.
            conn.execute(
                store_schema.workers.insert(),
                {
                    "worker_id": "w-uncommitted",
                    "user": "local",
                    "kind": "foreground",
                    "host": "poplar",
                    "started_at": NOW,
                },
            )
            manifest = store.snapshot(target)

        from_snapshot = snapshot_row_counts(target)
        assert from_snapshot["workers"] == committed["workers"]
        assert manifest["row_counts"]["workers"] == committed["workers"]

        # The file OPENS as a real store and does not carry the uncommitted row.
        reopened = SqliteStore(str(target))
        try:
            assert reopened.schema_version() == store.schema_version()
            with reopened.engine.begin() as conn:
                found = conn.execute(
                    sa.select(store_schema.workers.c.worker_id).where(
                        store_schema.workers.c.worker_id == "w-uncommitted"
                    )
                ).fetchall()
            assert found == []
        finally:
            reopened.close()

        # And the committed row IS there afterwards -- the transaction was
        # real, not rolled back by the snapshot.
        assert snapshot_row_counts(
            store.snapshot(tmp_path / "after.sqlite")["path"]
        )["workers"] == committed["workers"] + 1

    def test_the_snapshot_is_archivable_and_reconciles(
        self, store, sink, tmp_path, state_root
    ) -> None:
        """A torn backup would reconcile against the wrong counts; this one
        reconciles against its own."""
        with store.transaction() as conn:
            conn.execute(
                store_schema.workers.insert(),
                {
                    "worker_id": "w-uncommitted-2",
                    "user": "local",
                    "kind": "foreground",
                    "host": "poplar",
                },
            )
            manifest = store.snapshot(tmp_path / "during2.sqlite")
        report = _archived(sink, manifest, state_root=state_root)
        assert report["complete"] is True
        assert sink.count_rows("workers", where={"worker_id": "w-uncommitted-2"}) == 0


# --------------------------------------------------------------------------
# `kbu council snapshot` -- the CLI verb that was missing
# --------------------------------------------------------------------------


class TestTheSnapshotCli:
    """The store method existed; the verb did not.

    Its refusals are the method's, already pinned in
    ``test_store_identity.py`` (``test_snapshot_refuses_and_names_the_absence``
    for the absent identity, ``test_snapshot_refuses_naming_both_values`` for
    the mismatch). What is tested here is the VERB: that it exists, calls the
    method, offers no copy-based path, and exits non-zero on the refusal.
    """

    @staticmethod
    def _invoke(*args: str):
        return CliRunner().invoke(main, ["council", *args])

    @staticmethod
    def _payload(result) -> dict:
        """The envelope out of a CliRunner run.

        ``SharedEnvUtils`` logs to stdout, and CliRunner captures the two
        together, so the envelope is the one line that parses as JSON rather
        than the whole output. A real subprocess keeps them apart (stdout
        discipline is pinned in tests/cli/test_council_cli.py); this helper
        exists only because CliRunner does not.
        """
        for line in reversed(result.output.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                return json.loads(line)
        raise AssertionError(f"no JSON envelope in output: {result.output!r}")

    def test_the_verb_writes_a_snapshot_and_emits_the_manifest(
        self, store, tmp_path
    ) -> None:
        target = tmp_path / "cli-snap.sqlite"
        result = self._invoke(
            "--store", str(store.path), "snapshot", str(target), "--json"
        )
        assert result.exit_code == 0, result.output
        manifest = self._payload(result)["data"]
        assert manifest["store_id"] == STORE_ID
        assert len(manifest["snapshot_id"]) == 64
        assert target.is_file()
        assert snapshot_row_counts(target)["jobs"] == 3

    def test_the_emitted_snapshot_id_is_the_digest_of_the_file(
        self, store, tmp_path
    ) -> None:
        import hashlib

        target = tmp_path / "cli-snap2.sqlite"
        result = self._invoke(
            "--store", str(store.path), "snapshot", str(target), "--json"
        )
        manifest = self._payload(result)["data"]
        assert manifest["snapshot_id"] == hashlib.sha256(
            target.read_bytes()
        ).hexdigest()

    def test_it_refuses_a_store_with_NO_declared_identity_non_zero(
        self, tmp_path
    ) -> None:
        bare = SqliteStore(str(tmp_path / "bare" / "council.sqlite"))
        try:
            bare.init_schema()
        finally:
            bare.close()
        result = self._invoke(
            "--store",
            str(tmp_path / "bare" / "council.sqlite"),
            "snapshot",
            str(tmp_path / "nope.sqlite"),
        )
        assert result.exit_code != 0
        assert "StoreIdentityMissing" in result.output + str(result.exception or "")
        assert not (tmp_path / "nope.sqlite").exists()

    def test_it_refuses_to_overwrite_without_the_flag(self, store, tmp_path) -> None:
        target = tmp_path / "twice.sqlite"
        assert (
            self._invoke(
                "--store", str(store.path), "snapshot", str(target), "--json"
            ).exit_code
            == 0
        )
        again = self._invoke("--store", str(store.path), "snapshot", str(target))
        assert again.exit_code != 0
        with_flag = self._invoke(
            "--store", str(store.path), "snapshot", str(target), "--overwrite", "--json"
        )
        assert with_flag.exit_code == 0, with_flag.output

    def test_there_is_no_copy_based_option_anywhere_on_the_verb(self) -> None:
        """The CLI must offer no copy path: a `cp` of a live WAL store opens
        fine and is wrong, and nothing downstream can tell."""
        help_text = self._invoke("snapshot", "--help").output.lower()
        assert "--copy" not in help_text
        assert "backup api" in help_text
        for forbidden in ("cp ", "shutil", "file copy"):
            assert f"--{forbidden}" not in help_text
