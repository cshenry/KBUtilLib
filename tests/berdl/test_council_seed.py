"""``kbu council seed-store --from-lakehouse`` -- rebuilding a wiped reuse cache.

NOTHING IS INJECTED. Every test below builds a REAL council store through
``SqliteStore``'s own API, snapshots it with the REAL ``SqliteStore.snapshot()``
(the sqlite3 backup API), archives it with the REAL
``council_archive.archive_snapshot`` into a REAL ``SqliteCouncilSink`` built
from ``council_schema``'s own DDL, reads those rows back with the REAL
``council_seed.read_seed_rows``, seeds a fresh REAL store with the REAL
``SqliteStore.seed_from_rows``, and then asks the REAL
``SqliteStore.find_reusable`` and the REAL ``SqliteStore.claim_calls``. There
is no stub store, no patched ``find_reusable`` and no patched claim: a test
that injects a fake of the thing it claims to test proves nothing, and what is
being claimed here -- "a worker cannot execute an archived call, and a new job
can still reuse its answer" -- is a property of those two real queries
disagreeing about tombstoned jobs on purpose.

WHAT THIS IS WORTH. ``calls.prompt_hash`` is the reuse key and
``find_reusable`` serves any ``status='ok'`` row carrying it, so a wiped store
is a wiped CACHE: re-running the ADP1+PMI set (19,276 items) at the same query
version and roster against an empty store re-pays on the order of 199M tokens.

NOT A CLAIM ABOUT A LIVE POD. SQLite is not Iceberg and the BERDL transport
has been broken on kbhub since the 2026-10-19 image (KBUtilLib#91); live
acceptance of this namespace is operator-owned and is asserted nowhere here.
"""

from __future__ import annotations

import json
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
from kbutillib.domains.ai.council.store import schema as store_schema  # noqa: E402
from kbutillib.domains.ai.council.store.errors import (  # noqa: E402
    SeedDestinationNotEmpty,
    SeedError,
    SeedMultiOrigin,
)
from kbutillib.domains.ai.council.store.sqlite import (  # noqa: E402
    SEED_JOB_STATUS,
    SEED_PROVENANCE_KEY,
    SqliteStore,
)
from kbutillib.domains.kbase.berdl.council_archive import (  # noqa: E402
    DeliveryIncompleteError,
    archive_snapshot,
)
from kbutillib.domains.kbase.berdl.council_seed import read_seed_rows  # noqa: E402
from kbutillib.domains.kbase.berdl.council_sink import SqliteCouncilSink  # noqa: E402

STORE_ID = "poplar/council-1"
OTHER_STORE_ID = "emailmac/council-1"
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)

#: A query name NO shipped builtin and no machine-local ``query_dirs`` spec
#: can collide with. Deliberately not ``go_mf``: a destination store opened
#: through ``CouncilUtils`` registers the builtins and the user's configured
#: query directories, so a test that imported a ``go_mf`` spec of its own
#: invention would fail or pass depending on the machine's config file -- the
#: same machine-local leak that already accounts for six known failures in
#: ``tests/domains/ai/council/test_types_config.py``.
QUERY_NAME = "seedtest_mf"

#: The prompt hash of the answer the published chunk paid for.
PUBLISHED_HASH = "a1" * 32
#: The prompt hash of a PILOT job's answer. A pilot answer is still PAID, so
#: it is imported: restricting a seed to disposition='published' would discard
#: legitimate cached work.
PILOT_HASH = "b2" * 32
#: The prompt hash carried ONLY by a superseded (live=0) answer. After a seed
#: this hash must resolve to NOTHING: ``select_reusable`` does not filter
#: ``live``, so an imported superseded row would be served as a current answer.
SUPERSEDED_HASH = "c3" * 32
#: The prompt hash of an answer at the OTHER query version. query_version is
#: inside prompt_hash, so a reuse hit is only valid at the same version.
OTHER_VERSION_HASH = "d4" * 32
#: The prompt hash of a call that was never answered.
PENDING_HASH = "e5" * 32

#: What ``engine/normalise.py`` recorded repairing in one reply. Carried
#: through a seed INTACT: the reuse path falls back to the SOURCE's
#: ``normalised`` entries when re-normalisation produces no new repairs, so
#: losing the column changes what a reused answer reports about itself.
NORMALISED = [
    {"field": "go_id", "rule": "uppercase", "original": "go:0003824", "value": "GO:0003824"}
]


def _spec(name: str = QUERY_NAME, version: str = "4") -> dict:
    return {
        "name": name,
        "version": version,
        "entity_field": "text",
        "id_field": "go_id",
        "roles": {
            "proposer": {
                "system": "You assign GO molecular-function terms.",
                "user": "Annotation: {text}",
                "schema": {"type": "object"},
            }
        },
    }


def _call(job_id: str, item_id: str, **extra) -> dict:
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


def _build_source(path: Path) -> SqliteStore:
    """A REAL source store carrying every shape a seed has to get right.

    * ``job-published`` -- an ANSWERED call with a ``prompt_hash``, a
      non-null ``normalised``, and a SUPERSEDED (``live=0``) sibling whose
      ``status`` is also ``ok`` and whose ``prompt_hash`` nothing else carries;
    * a PENDING call, which is what a worker would execute for real money if a
      seed imported it;
    * ``job-pilot`` -- an answered call under a job the project dispositioned
      ``pilot``, which is still paid work;
    * ``job-v3`` -- an answered call at query version ``3``, which is a
      DIFFERENT reuse key because the version is inside ``prompt_hash``.
    """
    store = SqliteStore(str(path))
    store.init_schema()
    store.assign_store_identity(STORE_ID, declared_by="miles")
    store.put_query_type(name=QUERY_NAME, version="4", spec=_spec(), created_by="miles")
    store.put_query_type(
        name=QUERY_NAME, version="3", spec=_spec(version="3"), created_by="miles"
    )

    store.create_job(
        {
            "job_id": "job-published",
            "label": "chunk01",
            "query_name": QUERY_NAME,
            "query_version": "4",
            "submitted_by": "miles",
            "submitted_at": NOW,
            "status": "done",
        },
        [
            {"item_id": "f1", "entity_hash": "a" * 64, "evidence": {"text": "alpha"}},
            {"item_id": "f2", "entity_hash": "b" * 64, "evidence": {"text": "beta"}},
        ],
        [
            _call(
                "job-published",
                "f1",
                call_id="c-ok",
                status="ok",
                prompt_hash=PUBLISHED_HASH,
                reply_text="GO:0003824",
                reply_parsed={"go_id": "GO:0003824"},
                normalised=NORMALISED,
                finished_at=NOW + timedelta(seconds=2),
                fresh_usage={"input_tokens": 100, "output_tokens": 20},
            ),
            # Never answered. A worker DRAINS claimable work, so importing
            # this would spend real money silently.
            _call(
                "job-published",
                "f2",
                call_id="c-pending",
                status="pending",
                prompt_hash=PENDING_HASH,
            ),
        ],
    )
    # A SUPERSEDED answer: status 'ok' AND live=0, carrying a prompt_hash
    # nothing else carries. Written through the same update the engine's
    # supersede path uses.
    store.add_calls(
        [
            _call(
                "job-published",
                "f1",
                call_id="c-superseded",
                status="ok",
                prompt_hash=SUPERSEDED_HASH,
                upstream_digest="older",
                reply_text="GO:0000000",
                reply_parsed={"go_id": "GO:0000000"},
                finished_at=NOW + timedelta(seconds=1),
            )
        ]
    )
    with store.transaction() as conn:
        conn.execute(
            sa.update(store_schema.calls)
            .where(store_schema.calls.c.call_id == "c-superseded")
            .values(live=False)
        )
    store.settle_item("job-published", "f1", "ok", verdict={"go_id": "GO:0003824"})

    store.create_job(
        {
            "job_id": "job-pilot",
            "label": "pilot round",
            "query_name": QUERY_NAME,
            "query_version": "4",
            "submitted_by": "miles",
            "submitted_at": NOW,
            "status": "done",
        },
        [{"item_id": "p1", "entity_hash": "c" * 64}],
        [
            _call(
                "job-pilot",
                "p1",
                call_id="c-pilot",
                status="ok",
                prompt_hash=PILOT_HASH,
                reply_text="GO:0016740",
                reply_parsed={"go_id": "GO:0016740"},
                finished_at=NOW + timedelta(seconds=3),
            )
        ],
    )
    store.create_job(
        {
            "job_id": "job-v3",
            "label": "the previous query version",
            "query_name": QUERY_NAME,
            "query_version": "3",
            "submitted_by": "miles",
            "submitted_at": NOW,
            "status": "done",
        },
        [{"item_id": "f1", "entity_hash": "a" * 64}],
        [
            _call(
                "job-v3",
                "f1",
                call_id="c-v3",
                status="ok",
                query_version="3",
                prompt_hash=OTHER_VERSION_HASH,
                reply_text="GO:0003824",
                finished_at=NOW + timedelta(seconds=4),
            )
        ],
    )
    store.record_attempt(
        "c-ok",
        1,
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=2),
        outcome_kind="ok",
        usage={"input_tokens": 100, "output_tokens": 20},
        worker_id="w1",
    )
    store.register_worker(
        {"worker_id": "w1", "user": "local", "host": "poplar", "started_at": NOW}
    )
    return store


# --------------------------------------------------------------------------
# Fixtures: a real store, a real snapshot, a real archive, a real sink
# --------------------------------------------------------------------------


@pytest.fixture
def source(tmp_path: Path):
    opened = _build_source(tmp_path / "source" / "council.sqlite")
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
def archived(source: SqliteStore, sink: SqliteCouncilSink, state_root: Path, tmp_path: Path):
    """The REAL archiver's report plus the manifest it archived from."""
    manifest = source.snapshot(tmp_path / "snaps" / "snap1.sqlite")
    report = archive_snapshot(sink, manifest, snapshot_seq=1, state_root=state_root)
    assert report["complete"] is True, report["status"]
    return {"manifest": manifest, "report": report}


@pytest.fixture
def destination(tmp_path: Path):
    """A FRESH, EMPTY destination store -- the wiped one being rebuilt."""
    opened = SqliteStore(str(tmp_path / "dest" / "council.sqlite"))
    opened.init_schema()
    try:
        yield opened
    finally:
        opened.close()


def _unfiltered_rows(sink, snapshot_id: str) -> dict:
    """EVERY archived row of the snapshot, filtered by nothing.

    ``read_seed_rows`` pre-filters ``status``/``live`` so a 203,821-row
    ``calls`` table is not dragged through the seam, but the AUTHORITATIVE
    filter is ``seed_from_rows``'. These tests hand it the unfiltered rows so
    what they exercise is the store's own refusal to import a superseded or
    unanswered call -- not the reader's convenience.
    """
    identity = {"store_id": STORE_ID, "snapshot_id": snapshot_id}
    return {
        table: sink.select_rows(table, where=identity)
        for table in ("query_types", "jobs", "items", "calls")
    }


def _seed(
    sink,
    destination: SqliteStore,
    archived,
    state_root: Path,
    **scope,
) -> dict:
    """Read the archived rows and seed, both through the real code."""
    snapshot_id = archived["manifest"]["snapshot_id"]
    read = read_seed_rows(
        sink,
        store_id=STORE_ID,
        snapshot_id=snapshot_id,
        state_root=state_root,
        **scope,
    )
    return destination.seed_from_rows(
        read["rows"],
        store_id=STORE_ID,
        snapshot_id=snapshot_id,
        snapshot_seq=1,
        **scope,
    )


# --------------------------------------------------------------------------
# The round trip -- the test that matters
# --------------------------------------------------------------------------


class TestTheRoundTrip:
    def test_find_reusable_returns_the_archived_answer_for_a_known_hash(
        self, sink, destination, archived, state_root
    ) -> None:
        """Archive -> seed -> the REAL find_reusable, on a real prompt_hash."""
        receipt = _seed(sink, destination, archived, state_root)
        assert receipt["imported"] is True
        hit = destination.find_reusable(PUBLISHED_HASH)
        assert hit is not None
        assert hit["call_id"] == "c-ok"
        assert hit["status"] == "ok"
        assert hit["reply_text"] == "GO:0003824"
        assert hit["reply_parsed"] == {"go_id": "GO:0003824"}
        assert hit["canonical_model"] == "test/p1"

    def test_normalised_round_trips_intact(
        self, sink, destination, archived, state_root
    ) -> None:
        """The repair record, not a re-serialised approximation of it.

        The archive reads JSON columns as RAW TEXT, so a seed has to parse
        them back; binding the text straight into an ``sa.JSON`` column would
        double-encode it into a string that still passes ``is not None``.
        """
        _seed(sink, destination, archived, state_root)
        hit = destination.find_reusable(PUBLISHED_HASH)
        assert hit["normalised"] == NORMALISED
        assert isinstance(hit["normalised"], list)

    def test_a_call_whose_normalised_was_json_null_stays_json_null(
        self, sink, destination, archived, state_root
    ) -> None:
        """JSON null, never SQL NULL -- the store writes Python None as 'null'.

        ``calls.reply_parsed`` is ``sa.JSON`` with ``none_as_null=False``, so
        ``IS NOT NULL`` is true for every row and a filter written that way
        silently matches everything. The seeded row has to behave the same way
        the store's own writes do, or a later reader learns the wrong rule.
        """
        _seed(sink, destination, archived, state_root)
        hit = destination.find_reusable(PILOT_HASH)
        assert hit["normalised"] is None
        with destination._read() as conn:
            raw = conn.exec_driver_sql(
                "SELECT normalised FROM calls WHERE call_id = 'c-pilot'"
            ).scalar_one()
        assert raw == "null"

    def test_the_identity_rows_the_calls_need_come_too(
        self, sink, destination, archived, state_root
    ) -> None:
        """Referentially valid: the store has foreign keys ON."""
        receipt = _seed(sink, destination, archived, state_root)
        assert receipt["counts"]["jobs"] == 3
        assert receipt["counts"]["query_types"] == 2
        assert destination.get_job("job-published") is not None
        assert [row["item_id"] for row in destination.get_items("job-published")] == ["f1"]
        assert destination.get_query_type(QUERY_NAME, "4") is not None

    def test_a_pilot_answer_is_imported_because_it_was_paid_for(
        self, sink, destination, archived, state_root
    ) -> None:
        """Every status='ok' answer, REGARDLESS OF DISPOSITION.

        Restricting a seed to disposition='published' would discard
        legitimate cached work -- a pilot or trial answer cost exactly as much
        as a published one.
        """
        _seed(sink, destination, archived, state_root)
        hit = destination.find_reusable(PILOT_HASH)
        assert hit is not None
        assert hit["call_id"] == "c-pilot"

    def test_a_superseded_answer_is_not_served_as_reusable(
        self, sink, destination, archived, state_root
    ) -> None:
        """live=0 is excluded BY THE SEED, because nothing downstream excludes it.

        ``select_reusable`` accepts status='ok' across all jobs and does NOT
        filter ``live``, so a superseded row that reached a reuse cache would
        be served as a current answer.
        """
        snapshot_id = archived["manifest"]["snapshot_id"]
        receipt = destination.seed_from_rows(
            _unfiltered_rows(sink, snapshot_id),
            store_id=STORE_ID,
            snapshot_id=snapshot_id,
        )
        assert receipt["skipped"]["not_live"] == 1
        assert destination.find_reusable(SUPERSEDED_HASH) is None
        assert [row["call_id"] for row in destination.get_calls("job-published")] == ["c-ok"]

    def test_the_reader_and_the_store_agree_on_what_is_reusable(
        self, sink, destination, archived, state_root, tmp_path
    ) -> None:
        """Both filters, same answer -- one of them is authoritative and both run."""
        snapshot_id = archived["manifest"]["snapshot_id"]
        through_reader = _seed(sink, destination, archived, state_root)
        other = SqliteStore(str(tmp_path / "dest2" / "council.sqlite"))
        other.init_schema()
        try:
            direct = other.seed_from_rows(
                _unfiltered_rows(sink, snapshot_id),
                store_id=STORE_ID,
                snapshot_id=snapshot_id,
            )
            assert through_reader["counts"] == direct["counts"]
            assert other.find_reusable(PUBLISHED_HASH)["call_id"] == "c-ok"
            assert other.find_reusable(SUPERSEDED_HASH) is None
        finally:
            other.close()

    def test_the_archive_itself_still_holds_the_superseded_row(
        self, sink, destination, archived, state_root
    ) -> None:
        """The seed filters; the ARCHIVE never does. Both facts at once."""
        _seed(sink, destination, archived, state_root)
        assert (
            sink.count_rows(
                "calls",
                where={
                    "store_id": STORE_ID,
                    "snapshot_id": archived["manifest"]["snapshot_id"],
                    "call_id": "c-superseded",
                },
            )
            == 1
        )


# --------------------------------------------------------------------------
# NO IMPORTED ROW IS CLAIMABLE -- the sharpest hazard in the task
# --------------------------------------------------------------------------


class TestNothingImportedIsClaimable:
    """A worker DRAINS claimable work, so an archived pending call sitting in
    a seeded store would be EXECUTED -- real money, silently. Both halves of
    the guarantee are asserted against the REAL ``claim_calls``."""

    def test_a_real_claim_against_a_freshly_seeded_store_returns_nothing(
        self, sink, destination, archived, state_root
    ) -> None:
        _seed(sink, destination, archived, state_root)
        claimed = destination.claim_calls("w-seed", {"argo"}, 100)
        assert claimed == []

    def test_the_archived_pending_call_was_not_imported_at_all(
        self, sink, destination, archived, state_root
    ) -> None:
        """Enforcement one: only status='ok' rows are imported.

        ``select_claimable`` claims only ``status='pending'`` rows, so there
        is nothing of the right status in the store to claim.
        """
        snapshot_id = archived["manifest"]["snapshot_id"]
        receipt = destination.seed_from_rows(
            _unfiltered_rows(sink, snapshot_id),
            store_id=STORE_ID,
            snapshot_id=snapshot_id,
        )
        assert receipt["skipped"]["not_ok"] == 1
        with destination._read() as conn:
            statuses = {
                row[0]
                for row in conn.execute(sa.select(store_schema.calls.c.status).distinct())
            }
        assert statuses == {"ok"}
        assert destination.find_reusable(PENDING_HASH) is None

    def test_even_a_pending_call_added_to_a_seeded_job_cannot_be_claimed(
        self, sink, destination, archived, state_root
    ) -> None:
        """Enforcement two: every imported job is TOMBSTONED.

        ``select_claimable`` requires ``jobs.deleted_at IS NULL``, so a
        pending call attached to a seeded job afterwards is still unclaimable
        -- while ``select_reusable`` deliberately INCLUDES tombstoned jobs, so
        the answers stay reusable. Those two queries disagreeing on purpose is
        what makes "reusable but never executable" expressible at all.
        """
        _seed(sink, destination, archived, state_root)
        job = destination.get_job("job-published")
        assert job["status"] == SEED_JOB_STATUS
        assert job["deleted_at"] is not None
        destination.add_calls(
            [
                _call(
                    "job-published",
                    "f1",
                    call_id="c-later",
                    status="pending",
                    upstream_digest="later",
                )
            ]
        )
        assert destination.claim_calls("w-seed", {"argo"}, 100) == []
        # ...and the answer is still reusable, which is the whole point.
        assert destination.find_reusable(PUBLISHED_HASH)["call_id"] == "c-ok"

    def test_a_seeded_job_is_invisible_to_the_job_listing(
        self, sink, destination, archived, state_root
    ) -> None:
        """Excluded from progress accounting by the same tombstone."""
        _seed(sink, destination, archived, state_root)
        assert destination.list_jobs() == []
        assert len(destination.list_jobs(include_deleted=True)) == 3


# --------------------------------------------------------------------------
# Scope, provenance and what was recorded
# --------------------------------------------------------------------------


class TestScopeAndProvenance:
    def test_version_scoped_seeding_excludes_another_versions_rows(
        self, sink, destination, archived, state_root
    ) -> None:
        """A reuse hit is only valid at the SAME query version.

        ``query_version`` is inside ``prompt_hash``, which is why go_mf@3 ->
        @4 re-hashed every prompt and charged 475 re-paid items.
        """
        receipt = _seed(
            sink,
            destination,
            archived,
            state_root,
            query_name=QUERY_NAME,
            query_version="4",
        )
        assert receipt["scope"] == {"query_name": QUERY_NAME, "query_version": "4"}
        assert destination.find_reusable(PUBLISHED_HASH) is not None
        assert destination.find_reusable(OTHER_VERSION_HASH) is None
        assert destination.get_job("job-v3") is None

    def test_what_was_seeded_is_recorded_in_the_store(
        self, sink, destination, archived, state_root
    ) -> None:
        _seed(sink, destination, archived, state_root, query_name=QUERY_NAME)
        records = destination.seed_records()
        assert len(records) == 1
        assert records[0]["store_id"] == STORE_ID
        assert records[0]["snapshot_id"] == archived["manifest"]["snapshot_id"]
        assert records[0]["query_name"] == QUERY_NAME
        assert records[0]["snapshot_seq"] == 1
        options = destination.get_job("job-published")["options"]
        assert options[SEED_PROVENANCE_KEY]["snapshot_id"] == records[0]["snapshot_id"]

    def test_a_query_version_without_a_name_is_refused(
        self, sink, destination, archived, state_root
    ) -> None:
        with pytest.raises(SeedError):
            destination.seed_from_rows(
                {}, store_id=STORE_ID, snapshot_id="x" * 64, query_version="4"
            )

    def test_an_origin_with_no_store_id_is_refused(self, destination) -> None:
        with pytest.raises(SeedError):
            destination.seed_from_rows({}, store_id="", snapshot_id="x" * 64)


# --------------------------------------------------------------------------
# The refusals
# --------------------------------------------------------------------------


class TestRefusals:
    def test_multi_origin_rows_are_refused(
        self, sink, destination, archived, state_root
    ) -> None:
        """ONE store_id per seed.

        ``calls.call_id`` is an UNQUALIFIED primary key carrying foreign keys
        to items/jobs while the lakehouse guarantees only store-scoped
        identity, so dropping ``store_id`` can collide two different calls
        onto one id.
        """
        snapshot_id = archived["manifest"]["snapshot_id"]
        read = read_seed_rows(
            sink, store_id=STORE_ID, snapshot_id=snapshot_id, state_root=state_root
        )
        rows = {table: [dict(row) for row in value] for table, value in read["rows"].items()}
        rows["calls"][0]["store_id"] = OTHER_STORE_ID
        with pytest.raises(SeedMultiOrigin) as caught:
            destination.seed_from_rows(
                rows, store_id=STORE_ID, snapshot_id=snapshot_id
            )
        assert OTHER_STORE_ID in str(caught.value)

    def test_multi_snapshot_rows_are_refused(
        self, sink, destination, archived, state_root
    ) -> None:
        snapshot_id = archived["manifest"]["snapshot_id"]
        read = read_seed_rows(
            sink, store_id=STORE_ID, snapshot_id=snapshot_id, state_root=state_root
        )
        rows = {table: [dict(row) for row in value] for table, value in read["rows"].items()}
        rows["calls"][0]["snapshot_id"] = "f" * 64
        with pytest.raises(SeedMultiOrigin):
            destination.seed_from_rows(
                rows, store_id=STORE_ID, snapshot_id=snapshot_id
            )

    def test_an_incomplete_snapshot_is_refused_by_the_one_completeness_test(
        self, sink, source, state_root, tmp_path
    ) -> None:
        """``require_complete_delivery``, not a second opinion.

        The nine namespace tables commit INDEPENDENTLY, so a crash after
        ``calls`` and before ``attempts`` reads as a complete archive to every
        query that does not join the missing table -- and a reuse cache built
        from it has holes nobody can see.
        """
        manifest = source.snapshot(tmp_path / "snaps" / "partial.sqlite")

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
        with pytest.raises(DeliveryIncompleteError) as caught:
            read_seed_rows(
                sink,
                store_id=STORE_ID,
                snapshot_id=manifest["snapshot_id"],
                state_root=state_root,
            )
        assert "attempts" in str(caught.value)

    def test_a_snapshot_nobody_archived_is_refused(self, sink, state_root) -> None:
        with pytest.raises(DeliveryIncompleteError):
            read_seed_rows(
                sink, store_id=STORE_ID, snapshot_id="e" * 64, state_root=state_root
            )

    def test_a_non_empty_destination_is_refused(
        self, sink, destination, archived, state_root
    ) -> None:
        """A seed rebuilds an EMPTY store, it does not merge into a busy one."""
        destination.put_query_type(
            name=QUERY_NAME, version="4", spec=_spec(), created_by="someone"
        )
        destination.create_job(
            {
                "job_id": "job-local",
                "query_name": QUERY_NAME,
                "query_version": "4",
                "submitted_by": "someone",
                "submitted_at": NOW,
            },
            [{"item_id": "x1"}],
            [_call("job-local", "x1", call_id="c-local")],
        )
        with pytest.raises(SeedDestinationNotEmpty) as caught:
            _seed(sink, destination, archived, state_root)
        assert "jobs" in str(caught.value)

    def test_a_destination_holding_a_different_seed_is_refused(
        self, sink, destination, archived, state_root
    ) -> None:
        """The multi-origin refusal, arriving from the other side."""
        _seed(sink, destination, archived, state_root)
        snapshot_id = archived["manifest"]["snapshot_id"]
        read = read_seed_rows(
            sink, store_id=STORE_ID, snapshot_id=snapshot_id, state_root=state_root
        )
        with pytest.raises(SeedMultiOrigin):
            destination.seed_from_rows(
                read["rows"],
                store_id=STORE_ID,
                snapshot_id=snapshot_id,
                query_name=QUERY_NAME,
                query_version="4",
            )


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------


class TestIdempotency:
    def test_seeding_twice_produces_the_same_store(
        self, sink, destination, archived, state_root
    ) -> None:
        """A second seed of the SAME origin AND scope is a NO-OP, not a refusal.

        Rule 1's "EMPTY destination" and rule 7's "idempotent" would otherwise
        contradict each other and idempotency would be untestable: the only
        store a second identical seed can run against is one that already
        holds exactly that seed.
        """
        first = _seed(sink, destination, archived, state_root)
        before = destination._row_counts()
        second = _seed(sink, destination, archived, state_root)
        assert first["imported"] is True
        assert second["imported"] is False
        assert second["idempotent"] is True
        assert destination._row_counts() == before
        assert len(destination.seed_records()) == 1

    def test_the_second_seed_does_not_move_the_recorded_provenance(
        self, sink, destination, archived, state_root
    ) -> None:
        _seed(sink, destination, archived, state_root)
        first = destination.seed_records()[0]
        _seed(sink, destination, archived, state_root)
        assert destination.seed_records()[0] == first

    def test_find_reusable_answers_identically_after_a_second_seed(
        self, sink, destination, archived, state_root
    ) -> None:
        _seed(sink, destination, archived, state_root)
        before = destination.find_reusable(PUBLISHED_HASH)
        _seed(sink, destination, archived, state_root)
        assert destination.find_reusable(PUBLISHED_HASH) == before


# --------------------------------------------------------------------------
# NO-INJECTION: the verb through the REAL CLI, exit codes and all
# --------------------------------------------------------------------------


class TestTheSeedStoreCli:
    @staticmethod
    def _invoke(*args: str):
        return CliRunner().invoke(main, ["council", *args])

    @staticmethod
    def _payload(result) -> dict:
        """The envelope out of a CliRunner run -- the one line that is JSON."""
        for line in reversed(result.output.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                return json.loads(line)
        raise AssertionError(f"no JSON envelope in output: {result.output!r}")

    def test_the_verb_seeds_a_store_and_emits_the_receipt(
        self, sink, destination, archived, state_root, tmp_path
    ) -> None:
        result = self._invoke(
            "--store",
            str(destination.path),
            "seed-store",
            "--from-lakehouse",
            "--store-id",
            STORE_ID,
            "--snapshot-id",
            archived["manifest"]["snapshot_id"],
            "--lakehouse",
            str(sink.path),
            "--state-root",
            str(state_root),
            "--receipt",
            str(tmp_path / "seed-receipt.json"),
            "--json",
        )
        assert result.exit_code == 0, result.output
        data = self._payload(result)["data"]
        assert data["imported"] is True
        assert data["counts"]["calls"] == 3
        assert data["origin"]["store_id"] == STORE_ID
        written = json.loads((tmp_path / "seed-receipt.json").read_text())
        assert written["origin"]["snapshot_id"] == archived["manifest"]["snapshot_id"]
        assert destination.find_reusable(PUBLISHED_HASH)["call_id"] == "c-ok"

    def test_an_incomplete_snapshot_exits_non_zero_through_the_cli(
        self, sink, destination, state_root
    ) -> None:
        result = self._invoke(
            "--store",
            str(destination.path),
            "seed-store",
            "--from-lakehouse",
            "--store-id",
            STORE_ID,
            "--snapshot-id",
            "e" * 64,
            "--lakehouse",
            str(sink.path),
            "--state-root",
            str(state_root),
            "--json",
        )
        assert result.exit_code != 0
        assert "DeliveryIncompleteError" in result.output

    def test_a_non_empty_destination_exits_non_zero_through_the_cli(
        self, sink, destination, archived, state_root
    ) -> None:
        destination.put_query_type(
            name=QUERY_NAME, version="4", spec=_spec(), created_by="someone"
        )
        destination.create_job(
            {
                "job_id": "job-local",
                "query_name": QUERY_NAME,
                "query_version": "4",
                "submitted_by": "someone",
                "submitted_at": NOW,
            },
            [{"item_id": "x1"}],
            [_call("job-local", "x1", call_id="c-local")],
        )
        result = self._invoke(
            "--store",
            str(destination.path),
            "seed-store",
            "--from-lakehouse",
            "--store-id",
            STORE_ID,
            "--snapshot-id",
            archived["manifest"]["snapshot_id"],
            "--lakehouse",
            str(sink.path),
            "--state-root",
            str(state_root),
            "--json",
        )
        assert result.exit_code != 0
        assert "SeedDestinationNotEmpty" in result.output
