"""The v1 -> v2 store migration, against a real version 1 database file.

Every test here builds an actual SQLite file from
``fixtures/council_v1_schema.sql`` -- the DDL KBUtilLib main 16e3fe4 really
created, checked in so it cannot drift when ``schema.py`` does -- fills it
with jobs, items, calls, attempts, workers, query types and ``endpoint_state``
rows, and then migrates it through the shipped code path: ``CouncilUtils``'s
``db_migrate``, which is what ``kbu council db migrate`` calls.  Nothing is
mocked and no connection is stubbed: the point of the suite is that a user's
existing council survives the upgrade, and only a real file can show that.

What the migration must do (review rounds 2 and 3):

* drop exactly ``schema.REMOVED_IN_V2`` -- ``daemon_lease``, ``local_models``,
  ``model_resources``, ``model_loads``, ``gpu_lease`` -- and nothing else;
* keep every job, item, call, attempt, workers and query_types row intact;
* re-key ``endpoint_state`` onto the store-local identity, summing the token
  counters across historical user labels, keeping the HIGHEST budget epoch,
  and keeping every halt (combining the messages when there are several);
* stamp version 2;
* be a no-op the second time.

No test here makes a network call, spends a token, needs a CLI login or needs
a GPU.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

sa = pytest.importorskip(
    "sqlalchemy",
    reason="the council store needs the `council` extra: pip install 'kbutillib[council]'",
)

from kbutillib.domains.ai.council.store import open_store  # noqa: E402
from kbutillib.domains.ai.council.store import schema as sch  # noqa: E402
from kbutillib.domains.ai.council.store.sqlite import STORE_IDENTITY  # noqa: E402
from kbutillib.domains.ai.council_utils import CouncilUtils  # noqa: E402

V1_DDL = Path(__file__).parent / "fixtures" / "council_v1_schema.sql"

T0 = "2026-10-15 12:00:00.000000"

#: The five tables version 1 defined and version 2 does not.
V1_ONLY_TABLES = (
    "daemon_lease",
    "local_models",
    "model_resources",
    "model_loads",
    "gpu_lease",
)


# ---------------------------------------------------------------------------
# Building a real v1 database
# ---------------------------------------------------------------------------


def build_v1_store(path: Path, *, endpoint_state=()) -> None:
    """Create a REAL version 1 council database at *path*.

    Written with the stdlib's ``sqlite3`` against the checked-in M1 DDL, so
    none of KBUtilLib's own store code is involved in producing the thing
    under test.  Carries one job, two items, three calls, an attempt, a
    worker, a query type and a ``daemon_lease`` row, plus whatever
    *endpoint_state* rows the caller wants as
    ``(user, endpoint, budget_epoch, epoch_tokens, lifetime_tokens,
    halt_kind, halt_message)`` tuples.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(V1_DDL.read_text())
        conn.execute("INSERT INTO schema_version (version) VALUES (1)")
        conn.execute(
            "INSERT INTO query_types (name, version, status, spec, source, created_by,"
            " created_at) VALUES ('go_mf', '1', 'active', '{}', 'builtin', 'alice', ?)",
            (T0,),
        )
        conn.execute(
            "INSERT INTO jobs (job_id, label, query_name, query_version, submitted_by,"
            " submitted_host, submitted_at, priority, status, n_items)"
            " VALUES ('job-1', 'a v1 job', 'go_mf', '1', 'alice', 'oldhost', ?, 100,"
            " 'queued', 2)",
            (T0,),
        )
        for index, item_id in enumerate(("item-1", "item-2")):
            conn.execute(
                "INSERT INTO items (job_id, item_id, entity_hash, data_hash, evidence,"
                " menu, item_order, status) VALUES ('job-1', ?, ?, ?, '{}', '[]', ?,"
                " 'pending')",
                (item_id, "a" * 64, "b" * 64, index),
            )
        for index, (item_id, seat) in enumerate(
            (("item-1", "proposer_1"), ("item-1", "proposer_2"), ("item-2", "proposer_1"))
        ):
            conn.execute(
                "INSERT INTO calls (call_id, job_id, item_id, role, seat_name, endpoint,"
                " endpoint_kind, served_model, canonical_model, prompt_hash,"
                " upstream_digest, status, attempt_count, submitted_by, created_at, live)"
                " VALUES (?, 'job-1', ?, 'proposer', ?, 'argo', 'argo', 'gpt55',"
                " 'openai/gpt-5.5', ?, '', 'pending', 0, 'alice', ?, 1)",
                (f"call-{index}", item_id, seat, f"{index}" * 64, T0),
            )
        conn.execute(
            "INSERT INTO attempts (call_id, attempt_no, started_at, finished_at,"
            " outcome_kind, worker_id) VALUES ('call-0', 1, ?, ?, 'transport', 'w1')",
            (T0, T0),
        )
        conn.execute(
            "INSERT INTO workers (worker_id, user, kind, host, pid, started_at,"
            " heartbeat_at) VALUES ('w1', 'alice', 'daemon', 'oldhost', 1234, ?, ?)",
            (T0, T0),
        )
        # A row in one of the tables the migration must DROP.
        conn.execute(
            "INSERT INTO daemon_lease (user, holder_worker_id, generation, expires_at)"
            " VALUES ('alice', 'w1', 7, ?)",
            (T0,),
        )
        conn.execute("INSERT INTO gpu_lease (id, holder_worker_id, generation) VALUES (1, 'w1', 3)")
        for row in endpoint_state:
            conn.execute(
                "INSERT INTO endpoint_state (user, endpoint, budget_epoch, epoch_tokens,"
                " lifetime_tokens, halt_kind, halt_message, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (*row, T0),
            )
        conn.commit()
    finally:
        conn.close()


def table_names(path: Path) -> set:
    conn = sqlite3.connect(path)
    try:
        return {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    finally:
        conn.close()


def migrate(path: Path) -> dict:
    """Run the SHIPPED ``db migrate`` against *path* and return its report."""
    util = CouncilUtils(council_config={}, store_url=f"sqlite:///{path}")
    try:
        return util.db_migrate()
    finally:
        util.close()


# ---------------------------------------------------------------------------
# The migration itself
# ---------------------------------------------------------------------------


class TestMigrateV1ToV2:
    @pytest.fixture
    def v1_path(self, tmp_path: Path) -> Path:
        path = tmp_path / "council" / "council.sqlite"
        build_v1_store(path, endpoint_state=[("alice", "argo", 0, 100, 400, None, None)])
        return path

    def test_the_fixture_really_is_a_version_1_store(self, v1_path: Path) -> None:
        """Guard on the premise: without this the rest proves nothing."""
        assert V1_ONLY_TABLES[0] in table_names(v1_path)
        assert set(V1_ONLY_TABLES) <= table_names(v1_path)
        store = open_store(f"sqlite:///{v1_path}", check_version=False)
        try:
            assert store.schema_version() == 1
        finally:
            store.close()

    def test_migrate_reports_one_to_two(self, v1_path: Path) -> None:
        report = migrate(v1_path)
        assert report["schema_version_before"] == 1
        assert report["schema_version"] == 2
        assert report["migrated"] is True

    def test_exactly_the_five_tables_are_dropped(self, v1_path: Path) -> None:
        before = table_names(v1_path)
        migrate(v1_path)
        after = table_names(v1_path)
        assert after.isdisjoint(V1_ONLY_TABLES)
        # And nothing else went with them.
        assert before - after == set(V1_ONLY_TABLES)

    def test_every_other_row_survives(self, v1_path: Path) -> None:
        migrate(v1_path)
        store = open_store(f"sqlite:///{v1_path}")
        try:
            job = store.get_job("job-1")
            assert job["label"] == "a v1 job"
            assert job["submitted_by"] == "alice"
            assert job["submitted_host"] == "oldhost"
            assert job["n_items"] == 2
            assert [i["item_id"] for i in store.get_items("job-1")] == ["item-1", "item-2"]
            assert len(store.get_calls("job-1")) == 3
            assert [w["worker_id"] for w in store.live_workers(now=_T0())] == ["w1"]
            assert store.get_query_type("go_mf", "1") is not None
            with store.engine.connect() as conn:
                attempts = conn.execute(sa.select(sa.func.count()).select_from(sch.attempts))
                assert attempts.scalar() == 1
        finally:
            store.close()

    def test_the_migrated_store_takes_a_new_job_and_claims_it(self, v1_path: Path) -> None:
        """A migrated store is a working store, not merely a readable one."""
        migrate(v1_path)
        store = open_store(f"sqlite:///{v1_path}")
        try:
            claimed = store.claim_calls("w-new", {"argo"}, 10)
            assert len(claimed) == 3
        finally:
            store.close()

    def test_db_check_reports_version_two(self, v1_path: Path) -> None:
        migrate(v1_path)
        util = CouncilUtils(council_config={}, store_url=f"sqlite:///{v1_path}")
        try:
            report = util.db_check()
        finally:
            util.close()
        assert report["schema_version"] == 2
        assert report["ok"] is True

    def test_a_second_migrate_is_a_noop(self, v1_path: Path) -> None:
        migrate(v1_path)
        second = migrate(v1_path)
        assert second["schema_version_before"] == 2
        assert second["schema_version"] == 2
        assert second["migrated"] is False


# ---------------------------------------------------------------------------
# endpoint_state: re-keyed onto the store-local identity
# ---------------------------------------------------------------------------


class TestEndpointStateMerge:
    def _state(self, path: Path) -> dict:
        store = open_store(f"sqlite:///{path}")
        try:
            with store.engine.connect() as conn:
                rows = [
                    dict(row._mapping)
                    for row in conn.execute(sa.select(sch.endpoint_state))
                ]
        finally:
            store.close()
        return {(r["user"], r["endpoint"]): r for r in rows}

    def test_counters_are_summed_across_historical_labels(self, tmp_path: Path) -> None:
        path = tmp_path / "merge.sqlite"
        build_v1_store(
            path,
            endpoint_state=[
                ("alice", "argo", 2, 100, 1_000, None, None),
                ("bob", "argo", 5, 7, 70, None, None),
                ("carol", "argo", 1, 3, 30, None, None),
            ],
        )
        migrate(path)
        state = self._state(path)
        assert list(state) == [(STORE_IDENTITY, "argo")]
        row = state[(STORE_IDENTITY, "argo")]
        assert row["epoch_tokens"] == 110
        assert row["lifetime_tokens"] == 1_100
        # The HIGHEST epoch: a lower one would resurrect a budget already reset.
        assert row["budget_epoch"] == 5

    def test_a_single_halt_keeps_its_message_verbatim(self, tmp_path: Path) -> None:
        path = tmp_path / "one-halt.sqlite"
        build_v1_store(
            path,
            endpoint_state=[
                ("alice", "argo", 0, 1, 1, "quota_exhausted", "429 from the gateway"),
                ("bob", "argo", 0, 1, 1, None, None),
            ],
        )
        migrate(path)
        row = self._state(path)[(STORE_IDENTITY, "argo")]
        assert row["halt_kind"] == "quota_exhausted"
        assert row["halt_message"] == "429 from the gateway"

    def test_two_halts_are_both_kept_each_prefixed_with_its_label(
        self, tmp_path: Path
    ) -> None:
        """Discarding either would resume a call somebody stopped."""
        path = tmp_path / "two-halts.sqlite"
        build_v1_store(
            path,
            endpoint_state=[
                ("alice", "argo", 0, 1, 1, "quota_exhausted", "out of tokens"),
                ("bob", "argo", 0, 1, 1, "auth", "bob's key expired"),
            ],
        )
        migrate(path)
        row = self._state(path)[(STORE_IDENTITY, "argo")]
        assert row["halt_kind"] in ("quota_exhausted", "auth")
        assert "[alice] out of tokens" in row["halt_message"]
        assert "[bob] bob's key expired" in row["halt_message"]

    def test_a_merged_halt_still_stops_the_queue(self, tmp_path: Path) -> None:
        path = tmp_path / "halt-blocks.sqlite"
        build_v1_store(
            path,
            endpoint_state=[
                ("alice", "argo", 0, 1, 1, None, None),
                ("bob", "argo", 0, 1, 1, "auth", "expired"),
            ],
        )
        migrate(path)
        store = open_store(f"sqlite:///{path}")
        try:
            assert store.claim_calls("w-new", {"argo"}, 10) == []
            assert store.get_endpoint_state("argo")["halt_kind"] == "auth"
        finally:
            store.close()

    def test_endpoints_are_merged_independently(self, tmp_path: Path) -> None:
        path = tmp_path / "two-endpoints.sqlite"
        build_v1_store(
            path,
            endpoint_state=[
                ("alice", "argo", 0, 10, 10, None, None),
                ("bob", "argo", 0, 5, 5, None, None),
                ("alice", "claude", 0, 2, 2, None, None),
            ],
        )
        migrate(path)
        state = self._state(path)
        assert set(state) == {(STORE_IDENTITY, "argo"), (STORE_IDENTITY, "claude")}
        assert state[(STORE_IDENTITY, "argo")]["epoch_tokens"] == 15
        assert state[(STORE_IDENTITY, "claude")]["epoch_tokens"] == 2

    def test_an_empty_endpoint_state_migrates_cleanly(self, tmp_path: Path) -> None:
        path = tmp_path / "no-state.sqlite"
        build_v1_store(path)
        assert migrate(path)["schema_version"] == 2
        assert self._state(path) == {}


def _T0() -> datetime:
    return datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
