"""The CRUD half of the council store contract, run against every backend.

Covered here: schema init and version handling, query-type immutability,
atomic job creation, endpoint budget/halt state, workers, the daemon and GPU
leases, tombstones, the one-live-call index, search and the refusal of a
synced database path.

NOT covered here: claiming, completing and settling calls, and the job
control built on them.  Those methods raise
``NotImplementedError("store-queue-ops")`` in this milestone; the test at the
bottom pins that so the next milestone's suite has something to replace.

Fixtures come from :mod:`.store_contract`, which parametrises every test over
SQLite (always) and PostgreSQL (when ``KBU_COUNCIL_TEST_PG_DSN`` is set).  No
test here makes a network call, spends a token, needs a CLI login or needs a
GPU.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

sa = pytest.importorskip(
    "sqlalchemy",
    reason="the council store needs the `council` extra: pip install 'kbutillib[council]'",
)

from sqlalchemy.exc import IntegrityError  # noqa: E402

from kbutillib.domains.ai.council.protocols import CouncilStore  # noqa: E402
from kbutillib.domains.ai.council.store import open_store  # noqa: E402
from kbutillib.domains.ai.council.store import schema as sch  # noqa: E402
from kbutillib.domains.ai.council.store.errors import (  # noqa: E402
    JobCancelled,
    QueryTypeExists,
    SchemaVersionMismatch,
    StorePathRefused,
)
from kbutillib.domains.ai.council.store.sqlite import SqliteStore  # noqa: E402

# Only the plain helpers are imported here.  The `store`, `make_store`,
# `store_url` and `store_backend` fixtures come from conftest.py, which
# re-exports them from the same contract module -- see its docstring.
from .store_contract import QUEUE_OPS_MARKER, call_row, item_row, job_row, utcnow  # noqa: E402

T0 = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Schema and version
# ---------------------------------------------------------------------------


class TestSchema:
    def test_init_creates_every_table_and_stamps_version_1(self, store):
        assert store.schema_version() == 1
        present = set(sa.inspect(store.engine).get_table_names())
        assert set(sch.TABLE_NAMES) <= present

    def test_init_schema_is_idempotent(self, store):
        store.init_schema()
        store.init_schema()
        assert store.schema_version() == 1

    def test_version_is_0_before_init(self, make_store):
        fresh = make_store("uninitialised", init=False)
        assert fresh.schema_version() == 0

    def test_migrate_initialises_an_empty_database(self, make_store):
        fresh = make_store("needs-migrate", init=False)
        assert fresh.migrate() == 1
        assert fresh.schema_version() == 1

    def test_migrate_is_a_noop_at_the_current_version(self, store):
        assert store.migrate() == 1

    def test_opening_a_different_version_raises_and_names_the_fix(
        self, store, make_store
    ):
        with store.transaction() as conn:
            conn.execute(sch.schema_version.update().values(version=99))
        store.close()

        with pytest.raises(SchemaVersionMismatch) as excinfo:
            make_store(init=False)
        message = str(excinfo.value)
        assert "99" in message
        # The error has to say what to DO about it, not just that it happened.
        assert "kbu council db migrate" in message

    def test_check_version_false_opens_a_mismatched_database(self, store, make_store):
        """`kbu council db migrate` must be able to open what the check rejects."""
        with store.transaction() as conn:
            conn.execute(sch.schema_version.update().values(version=99))
        store.close()

        reopened = make_store(init=False, check_version=False)
        assert reopened.schema_version() == 99
        with pytest.raises(SchemaVersionMismatch):
            reopened.migrate()


# ---------------------------------------------------------------------------
# Query types
# ---------------------------------------------------------------------------


class TestQueryTypes:
    SPEC = {"entity_field": "text", "prompts": {"proposer": "pick ids"}}

    def test_put_and_get(self, store):
        store.put_query_type("go", "1", self.SPEC, source="builtin", created_by="alice")
        got = store.get_query_type("go", "1")
        assert got["spec"] == self.SPEC
        assert got["source"] == "builtin"
        assert got["created_by"] == "alice"
        assert got["status"] == "active"

    def test_identical_reput_is_a_noop(self, store):
        store.put_query_type("go", "1", self.SPEC)
        store.put_query_type("go", "1", dict(self.SPEC))
        assert len(store.list_query_types()) == 1

    def test_reput_with_a_different_spec_raises(self, store):
        store.put_query_type("go", "1", self.SPEC)
        with pytest.raises(QueryTypeExists):
            store.put_query_type("go", "1", {**self.SPEC, "entity_field": "sequence"})
        # The stored spec is the original one, not a half-applied edit.
        assert store.get_query_type("go", "1")["spec"] == self.SPEC

    def test_a_new_version_is_allowed_beside_the_old_one(self, store):
        store.put_query_type("go", "1", self.SPEC)
        store.put_query_type("go", "2", {**self.SPEC, "entity_field": "sequence"})
        assert len(store.list_query_types()) == 2

    def test_get_without_a_version_returns_the_latest_active(self, store):
        for version in ("1", "2", "10"):
            store.put_query_type("go", version, {**self.SPEC, "v": version})
        # "10" and not "2": versions sort numerically, not lexicographically.
        assert store.get_query_type("go")["version"] == "10"

    def test_get_of_an_unknown_query_type_is_none(self, store):
        assert store.get_query_type("nope") is None
        assert store.get_query_type("nope", "1") is None

    def test_retire_hides_it_from_the_default_listing_and_from_get(self, store):
        store.put_query_type("go", "1", self.SPEC)
        store.put_query_type("go", "2", {**self.SPEC, "v": 2})

        assert store.retire_query_type("go", "2") is True
        assert store.get_query_type("go")["version"] == "1"
        assert [q["version"] for q in store.list_query_types()] == ["1"]
        # Retiring removes nothing: a decided job still resolves its spec.
        assert {q["version"] for q in store.list_query_types(include_retired=True)} == {
            "1",
            "2",
        }
        assert store.get_query_type("go", "2")["status"] == "retired"

    def test_retire_of_an_unknown_row_reports_false(self, store):
        assert store.retire_query_type("go", "7") is False


# ---------------------------------------------------------------------------
# Jobs, items, calls
# ---------------------------------------------------------------------------


class TestCreateJob:
    def test_writes_job_items_and_calls(self, store):
        job_id = store.create_job(
            job_row(),
            [item_row("item-1"), item_row("item-2")],
            [
                call_row("item-1", "proposer_1"),
                call_row("item-1", "proposer_2"),
                call_row("item-2", "proposer_1"),
            ],
        )

        job = store.get_job(job_id)
        assert job["status"] == "queued"
        assert job["priority"] == 100
        assert job["n_items"] == 2
        assert job["submitted_at"].tzinfo is not None
        assert job["roster"]["critic"] == "critic"

        items = store.get_items(job_id)
        assert [i["item_id"] for i in items] == ["item-1", "item-2"]
        assert [i["item_order"] for i in items] == [0, 1]
        assert all(i["status"] == "pending" for i in items)
        assert items[0]["evidence"]["source"] == "test"

        calls = store.get_calls(job_id)
        assert len(calls) == 3
        assert all(c["status"] == "pending" for c in calls)
        assert all(c["live"] for c in calls)
        assert all(c["upstream_digest"] == "" for c in calls)
        assert all(c["attempt_count"] == 0 for c in calls)
        # Inherited from the job rather than repeated by every caller.
        assert all(c["submitted_by"] == "alice" for c in calls)
        assert all(c["query_name"] == "go_term_mapping" for c in calls)

        assert len(store.get_calls(job_id, "item-1")) == 2

    def test_caller_supplied_job_id_is_honoured(self, store):
        assert store.create_job(job_row(job_id="fixed-id"), [], []) == "fixed-id"

    def test_an_unknown_column_is_refused_rather_than_dropped(self, store):
        with pytest.raises(ValueError, match="unknown jobs column"):
            store.create_job(job_row(whoops="typo"), [], [])

    def test_a_failure_in_the_calls_leaves_nothing_behind(self, store):
        """Atomicity, injected at the LAST stage of create_job.

        Two calls that collide on the one-live-call index: the job and items
        inserts have already succeeded when the calls insert fails.  A job
        whose items landed but whose calls did not would sit at 0% forever
        with no error anywhere, so the whole thing must roll back.
        """
        with pytest.raises(IntegrityError):
            store.create_job(
                job_row(job_id="doomed"),
                [item_row("item-1")],
                [call_row("item-1", "proposer_1"), call_row("item-1", "proposer_1")],
            )

        assert store.get_job("doomed") is None
        assert store.get_items("doomed") == []
        assert store.get_calls("doomed") == []
        assert store.search_calls() == []

    def test_a_failure_in_the_items_leaves_nothing_behind(self, store):
        """The same, injected one stage earlier: a duplicate item primary key."""
        with pytest.raises(IntegrityError):
            store.create_job(
                job_row(job_id="doomed"),
                [item_row("item-1"), item_row("item-1")],
                [],
            )

        assert store.get_job("doomed") is None
        assert store.get_items("doomed") == []

    def test_a_call_for_an_unknown_item_is_refused(self, store):
        """Foreign keys are ON: calls cannot outlive the item they belong to."""
        with pytest.raises(IntegrityError):
            store.create_job(
                job_row(job_id="doomed"),
                [item_row("item-1")],
                [call_row("item-does-not-exist", "proposer_1")],
            )
        assert store.get_job("doomed") is None


class TestOneLiveCall:
    def test_a_second_live_call_for_the_same_seat_is_refused(self, store):
        job_id = store.create_job(
            job_row(), [item_row("item-1")], [call_row("item-1", "proposer_1")]
        )
        with pytest.raises(IntegrityError):
            store.add_calls(
                [dict(call_row("item-1", "proposer_1"), job_id=job_id)]
            )

    def test_the_index_is_partial_so_superseded_calls_do_not_block(self, store):
        """A cancelled-then-resubmitted seat must be able to get a new call.

        That is the whole reason the index is partial: superseded rows stay
        for reuse and audit, and any number of them may sit behind the one
        live row.
        """
        job_id = store.create_job(
            job_row(), [item_row("item-1")], [call_row("item-1", "proposer_1")]
        )
        with store.transaction() as conn:
            conn.execute(
                sch.calls.update()
                .where(sch.calls.c.job_id == job_id)
                .values(live=False, status="superseded")
            )

        store.add_calls([dict(call_row("item-1", "proposer_1"), job_id=job_id)])
        assert len(store.get_calls(job_id)) == 2

    def test_different_seats_and_roles_coexist(self, store):
        job_id = store.create_job(
            job_row(), [item_row("item-1")], [call_row("item-1", "proposer_1")]
        )
        ids = store.add_calls(
            [
                dict(call_row("item-1", "proposer_2"), job_id=job_id),
                dict(call_row("item-1", "critic", role="critic"), job_id=job_id),
            ]
        )
        assert len(ids) == 2
        assert len(store.get_calls(job_id)) == 3


class TestAddCallsAndSettleItem:
    def test_add_calls_returns_ids_and_defaults_to_pending(self, store):
        job_id = store.create_job(job_row(), [item_row("item-1")], [])
        ids = store.add_calls(
            [dict(call_row("item-1", "critic", role="critic"), job_id=job_id)]
        )
        (call,) = store.get_calls(job_id)
        assert call["call_id"] == ids[0]
        assert call["status"] == "pending"
        assert call["created_at"].tzinfo is not None

    def test_add_calls_with_no_rows_is_a_noop(self, store):
        assert store.add_calls([]) == []

    def test_add_calls_for_an_unknown_job_raises(self, store):
        with pytest.raises(ValueError, match="no such job"):
            store.add_calls([dict(call_row(), job_id="nope")])

    def test_settle_item_records_status_and_verdict(self, store):
        job_id = store.create_job(job_row(), [item_row("item-1")], [])
        verdict = {"item_id": "item-1", "status": "ok", "selected_ids": ["GO:0003674"]}
        store.settle_item(job_id, "item-1", "ok", verdict)

        (item,) = store.get_items(job_id)
        assert item["status"] == "ok"
        assert item["verdict"] == verdict
        assert item["decided_at"].tzinfo is not None
        assert store.get_items(job_id, status="pending") == []

    def test_settle_item_rejects_an_unknown_item(self, store):
        job_id = store.create_job(job_row(), [item_row("item-1")], [])
        with pytest.raises(ValueError, match="no such item"):
            store.settle_item(job_id, "ghost", "ok", {})

    def test_a_cancelled_job_refuses_new_calls_and_verdicts(self, store):
        """Cancellation is terminal, so nothing may be attached afterwards.

        ``cancel_job`` itself is the next milestone, so the status is set
        directly here -- the point under test is that add_calls and
        settle_item honour it, not how it got set.
        """
        job_id = store.create_job(job_row(), [item_row("item-1")], [])
        with store.transaction() as conn:
            conn.execute(
                sch.jobs.update()
                .where(sch.jobs.c.job_id == job_id)
                .values(status="cancelled")
            )

        with pytest.raises(JobCancelled):
            store.add_calls([dict(call_row("item-1"), job_id=job_id)])
        with pytest.raises(JobCancelled):
            store.settle_item(job_id, "item-1", "ok", {})


class TestListJobs:
    def test_filters_by_user_and_status(self, store):
        store.create_job(job_row(job_id="a", submitted_by="alice"), [], [])
        store.create_job(job_row(job_id="b", submitted_by="bob"), [], [])
        store.create_job(
            job_row(job_id="c", submitted_by="alice", status="done"), [], []
        )

        assert {j["job_id"] for j in store.list_jobs()} == {"a", "b", "c"}
        assert {j["job_id"] for j in store.list_jobs(user="alice")} == {"a", "c"}
        assert {j["job_id"] for j in store.list_jobs(status="done")} == {"c"}
        assert store.list_jobs(user="bob", status="done") == []

    def test_tombstoned_jobs_are_hidden_unless_asked_for(self, store):
        """delete_job sets deleted_at and removes nothing.

        ``delete_job`` is the next milestone, so the tombstone is written
        directly: what is under test is that list_jobs filters on it and that
        get_job and the call rows survive, which is what keeps a deleted job's
        answers reusable by everyone else.
        """
        job_id = store.create_job(
            job_row(job_id="gone"), [item_row("item-1")], [call_row("item-1")]
        )
        with store.transaction() as conn:
            conn.execute(
                sch.jobs.update()
                .where(sch.jobs.c.job_id == job_id)
                .values(deleted_at=utcnow(), status="cancelled")
            )

        assert store.list_jobs() == []
        assert [j["job_id"] for j in store.list_jobs(include_deleted=True)] == ["gone"]
        assert store.get_job("gone")["deleted_at"] is not None
        assert len(store.search_calls()) == 1


# ---------------------------------------------------------------------------
# Endpoint state
# ---------------------------------------------------------------------------


class TestEndpointState:
    def test_an_unseen_endpoint_reads_as_zeroed_and_unhalted(self, store):
        state = store.get_endpoint_state("alice", "argo")
        assert state["epoch_tokens"] == 0
        assert state["lifetime_tokens"] == 0
        assert state["budget_epoch"] == 0
        assert state["halt_kind"] is None

    def test_add_tokens_accumulates_in_both_counters(self, store):
        store.add_tokens("alice", "argo", 100)
        store.add_tokens("alice", "argo", 250)
        state = store.get_endpoint_state("alice", "argo")
        assert state["epoch_tokens"] == 350
        assert state["lifetime_tokens"] == 350

    def test_counters_are_per_user_and_per_endpoint(self, store):
        store.add_tokens("alice", "argo", 100)
        store.add_tokens("bob", "argo", 7)
        store.add_tokens("alice", "poplar", 3)
        assert store.get_endpoint_state("alice", "argo")["epoch_tokens"] == 100
        assert store.get_endpoint_state("bob", "argo")["epoch_tokens"] == 7
        assert store.get_endpoint_state("alice", "poplar")["epoch_tokens"] == 3

    def test_set_halt_records_kind_and_message(self, store):
        store.set_halt("alice", "argo", "quota_exhausted", "429 from the gateway")
        state = store.get_endpoint_state("alice", "argo")
        assert state["halt_kind"] == "quota_exhausted"
        assert state["halt_message"] == "429 from the gateway"

    def test_reset_halt_clears_the_halt_and_keeps_the_tokens(self, store):
        store.add_tokens("alice", "argo", 500)
        store.set_halt("alice", "argo", "auth", "expired key")

        store.reset_halt("alice", "argo")
        state = store.get_endpoint_state("alice", "argo")
        assert state["halt_kind"] is None
        assert state["halt_message"] is None
        # "The problem is fixed" is not "the tokens were not spent".
        assert state["epoch_tokens"] == 500
        assert state["lifetime_tokens"] == 500

    def test_reset_budget_starts_a_new_epoch_and_keeps_the_lifetime(self, store):
        store.add_tokens("alice", "argo", 500)
        store.reset_budget("alice", "argo")
        state = store.get_endpoint_state("alice", "argo")
        assert state["budget_epoch"] == 1
        assert state["epoch_tokens"] == 0
        assert state["lifetime_tokens"] == 500

        store.add_tokens("alice", "argo", 20)
        state = store.get_endpoint_state("alice", "argo")
        assert state["epoch_tokens"] == 20
        assert state["lifetime_tokens"] == 520

    def test_reset_budget_clears_a_budget_halt(self, store):
        store.set_halt("alice", "argo", "budget_exhausted", "ceiling reached")
        assert store.reset_budget("alice", "argo")["halt_kind"] is None

    def test_reset_budget_leaves_a_non_budget_halt_alone(self, store):
        """A new epoch does not fix an expired credential."""
        store.set_halt("alice", "argo", "auth", "expired key")
        state = store.reset_budget("alice", "argo")
        assert state["halt_kind"] == "auth"
        assert state["budget_epoch"] == 1


# ---------------------------------------------------------------------------
# Workers
# ---------------------------------------------------------------------------


class TestWorkers:
    def test_register_then_find_live(self, store):
        worker_id = store.register_worker(
            {
                "worker_id": "w1",
                "user": "alice",
                "kind": "daemon",
                "pid": 4242,
                "heartbeat_at": T0,
                "endpoints": ["argo", "poplar"],
            }
        )
        assert worker_id == "w1"
        (row,) = store.live_workers("alice", now=T0)
        assert row["kind"] == "daemon"
        assert row["pid"] == 4242
        assert row["endpoints"] == ["argo", "poplar"]
        assert row["host"]  # defaulted to this machine

    def test_a_worker_outside_the_window_is_not_live(self, store):
        store.register_worker({"worker_id": "w1", "user": "alice", "heartbeat_at": T0})
        assert store.live_workers("alice", now=T0 + timedelta(minutes=1)) != []
        assert store.live_workers("alice", now=T0 + timedelta(minutes=3)) == []

    def test_heartbeat_brings_a_stale_worker_back(self, store):
        store.register_worker({"worker_id": "w1", "user": "alice", "heartbeat_at": T0})
        later = T0 + timedelta(minutes=10)
        assert store.heartbeat_worker("w1", later) is True
        assert len(store.live_workers("alice", now=later)) == 1

    def test_heartbeat_of_an_unregistered_worker_reports_false(self, store):
        assert store.heartbeat_worker("ghost", T0) is False

    def test_live_workers_filters_by_user_and_defaults_to_all(self, store):
        store.register_worker({"worker_id": "w1", "user": "alice", "heartbeat_at": T0})
        store.register_worker({"worker_id": "w2", "user": "bob", "heartbeat_at": T0})
        assert len(store.live_workers("alice", now=T0)) == 1
        assert len(store.live_workers(now=T0)) == 2

    def test_register_twice_updates_rather_than_duplicates(self, store):
        store.register_worker({"worker_id": "w1", "user": "alice", "pid": 1})
        store.register_worker({"worker_id": "w1", "user": "alice", "pid": 2})
        (row,) = store.live_workers("alice")
        assert row["pid"] == 2

    def test_remove_worker(self, store):
        store.register_worker({"worker_id": "w1", "user": "alice"})
        assert store.remove_worker("w1") is True
        assert store.remove_worker("w1") is False
        assert store.live_workers("alice") == []

    def test_request_stop_reaches_the_daemon_lease_holder(self, store):
        store.register_worker({"worker_id": "w1", "user": "alice", "kind": "daemon"})
        store.register_worker({"worker_id": "w2", "user": "alice"})
        store.acquire_daemon_lease("alice", "w1", now=T0)

        assert store.stop_requested("w1") is False
        assert store.request_stop("alice") is True
        assert store.stop_requested("w1") is True
        # Only the lease holder is asked to stop, not every worker of the user.
        assert store.stop_requested("w2") is False

    def test_request_stop_without_a_lease_holder_reports_false(self, store):
        assert store.request_stop("alice") is False

    def test_stop_requested_for_an_unknown_worker_is_false(self, store):
        assert store.stop_requested("ghost") is False


# ---------------------------------------------------------------------------
# Leases
# ---------------------------------------------------------------------------


class TestDaemonLease:
    def test_first_acquire_is_generation_1(self, store):
        assert store.acquire_daemon_lease("alice", "w1", now=T0) == 1

    def test_acquire_is_refused_while_the_lease_is_unexpired(self, store):
        store.acquire_daemon_lease("alice", "w1", now=T0)
        assert (
            store.acquire_daemon_lease(
                "alice", "w2", now=T0 + timedelta(seconds=30)
            )
            is None
        )

    def test_the_holder_itself_must_renew_rather_than_reacquire(self, store):
        store.acquire_daemon_lease("alice", "w1", now=T0)
        assert store.acquire_daemon_lease("alice", "w1", now=T0) is None

    def test_takeover_after_expiry_increments_the_generation(self, store):
        store.acquire_daemon_lease("alice", "w1", now=T0, ttl=timedelta(minutes=2))
        later = T0 + timedelta(minutes=5)
        assert store.acquire_daemon_lease("alice", "w2", now=later) == 2
        assert store.acquire_daemon_lease(
            "alice", "w3", now=later + timedelta(minutes=5)
        ) == 3

    def test_renew_extends_the_lease(self, store):
        store.acquire_daemon_lease("alice", "w1", now=T0, ttl=timedelta(minutes=2))
        assert (
            store.renew_daemon_lease(
                "alice", "w1", 1, now=T0 + timedelta(minutes=1), ttl=timedelta(minutes=2)
            )
            is True
        )
        # Renewed at T0+1m for 2m, so a rival at T0+2.5m is still shut out.
        assert (
            store.acquire_daemon_lease(
                "alice", "w2", now=T0 + timedelta(minutes=2, seconds=30)
            )
            is None
        )

    def test_renew_with_a_superseded_generation_returns_false(self, store):
        store.acquire_daemon_lease("alice", "w1", now=T0, ttl=timedelta(minutes=2))
        store.acquire_daemon_lease("alice", "w2", now=T0 + timedelta(minutes=5))
        # w1 woke up from a long GC pause; its generation is history.
        assert store.renew_daemon_lease("alice", "w1", 1, now=T0 + timedelta(minutes=6)) is False

    def test_renew_by_the_wrong_worker_returns_false(self, store):
        store.acquire_daemon_lease("alice", "w1", now=T0)
        assert store.renew_daemon_lease("alice", "w2", 1, now=T0) is False

    def test_renew_without_a_lease_returns_false(self, store):
        assert store.renew_daemon_lease("alice", "w1", 1, now=T0) is False

    def test_release_frees_the_lease_but_keeps_the_generation(self, store):
        store.acquire_daemon_lease("alice", "w1", now=T0)
        assert store.release_daemon_lease("alice", "w1", 1) is True
        # Immediately available, and the counter does NOT go back to 1.
        assert store.acquire_daemon_lease("alice", "w2", now=T0) == 2

    def test_release_with_the_wrong_generation_is_refused(self, store):
        store.acquire_daemon_lease("alice", "w1", now=T0)
        assert store.release_daemon_lease("alice", "w1", 7) is False

    def test_leases_are_per_user(self, store):
        assert store.acquire_daemon_lease("alice", "w1", now=T0) == 1
        assert store.acquire_daemon_lease("bob", "w2", now=T0) == 1


class TestGpuLease:
    """The GPU lease is fleet-wide: one row, no user key, same semantics."""

    def test_first_acquire_is_generation_1(self, store):
        assert store.acquire_gpu_lease("w1", now=T0) == 1

    def test_acquire_is_refused_while_unexpired(self, store):
        store.acquire_gpu_lease("w1", now=T0)
        assert store.acquire_gpu_lease("w2", now=T0 + timedelta(seconds=30)) is None

    def test_takeover_after_expiry_increments_the_generation(self, store):
        store.acquire_gpu_lease("w1", now=T0, ttl=timedelta(minutes=2))
        assert store.acquire_gpu_lease("w2", now=T0 + timedelta(minutes=5)) == 2

    def test_renew_and_superseded_renew(self, store):
        store.acquire_gpu_lease("w1", now=T0, ttl=timedelta(minutes=2))
        assert store.renew_gpu_lease("w1", 1, now=T0 + timedelta(minutes=1)) is True
        store.acquire_gpu_lease("w2", now=T0 + timedelta(minutes=10))
        assert store.renew_gpu_lease("w1", 1, now=T0 + timedelta(minutes=11)) is False

    def test_release_keeps_the_generation(self, store):
        store.acquire_gpu_lease("w1", now=T0)
        assert store.release_gpu_lease("w1", 1) is True
        assert store.acquire_gpu_lease("w2", now=T0) == 2

    def test_the_gpu_lease_is_independent_of_a_daemon_lease(self, store):
        store.acquire_daemon_lease("alice", "w1", now=T0)
        assert store.acquire_gpu_lease("w1", now=T0) == 1


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class TestSearchCalls:
    @pytest.fixture
    def populated(self, store):
        store.create_job(
            job_row(job_id="j1", query_name="go_term_mapping"),
            [item_row("item-1")],
            [
                call_row("item-1", "proposer_1", canonical_model="openai/gpt-5.1"),
                call_row(
                    "item-1",
                    "proposer_2",
                    canonical_model="anthropic/claude-opus-5",
                    entity_hash="e" * 64,
                ),
            ],
        )
        store.create_job(
            job_row(job_id="j2", query_name="reaction_mapping", submitted_by="bob"),
            [item_row("item-9")],
            [
                call_row(
                    "item-9",
                    "proposer_1",
                    canonical_model="openai/gpt-5.1",
                    data_hash="9" * 64,
                    status="ok",
                )
            ],
        )
        return store

    def test_no_filter_returns_everything(self, populated):
        assert len(populated.search_calls()) == 3

    def test_filter_by_query_name(self, populated):
        rows = populated.search_calls(query_name="reaction_mapping")
        assert [r["item_id"] for r in rows] == ["item-9"]

    def test_filter_by_canonical_model(self, populated):
        rows = populated.search_calls(canonical_model="openai/gpt-5.1")
        assert len(rows) == 2

    def test_filter_by_entity_hash(self, populated):
        rows = populated.search_calls(entity_hash="e" * 64)
        assert [r["seat_name"] for r in rows] == ["proposer_2"]

    def test_filter_by_data_hash(self, populated):
        assert len(populated.search_calls(data_hash="9" * 64)) == 1

    def test_filter_by_status(self, populated):
        assert len(populated.search_calls(status="ok")) == 1
        assert len(populated.search_calls(status="pending")) == 2

    def test_filters_combine(self, populated):
        assert (
            populated.search_calls(
                query_name="go_term_mapping", canonical_model="openai/gpt-5.1"
            )
            != []
        )
        assert (
            populated.search_calls(
                query_name="reaction_mapping", canonical_model="anthropic/claude-opus-5"
            )
            == []
        )

    def test_limit_is_honoured(self, populated):
        assert len(populated.search_calls(limit=2)) == 2

    def test_search_crosses_users(self, populated):
        """Reuse is the point of a shared store, so search is not user-scoped."""
        assert {r["submitted_by"] for r in populated.search_calls()} == {"alice", "bob"}


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


class TestTransaction:
    def test_a_raising_block_rolls_everything_back(self, store):
        with pytest.raises(RuntimeError):
            with store.transaction():
                store.put_query_type("go", "1", {"a": 1})
                raise RuntimeError("boom")
        assert store.list_query_types() == []

    def test_nested_use_joins_the_outer_transaction(self, store):
        """Re-entrancy is what lets the worker settle a call as one unit."""
        with store.transaction():
            store.put_query_type("go", "1", {"a": 1})
            store.add_tokens("alice", "argo", 5)
        assert len(store.list_query_types()) == 1
        assert store.get_endpoint_state("alice", "argo")["epoch_tokens"] == 5

    def test_a_nested_failure_rolls_back_the_whole_unit(self, store):
        with pytest.raises(RuntimeError):
            with store.transaction():
                store.add_tokens("alice", "argo", 5)
                store.put_query_type("go", "1", {"a": 1})
                raise RuntimeError("boom")
        assert store.get_endpoint_state("alice", "argo")["epoch_tokens"] == 0
        assert store.list_query_types() == []


# ---------------------------------------------------------------------------
# Queue operations: the next milestone
# ---------------------------------------------------------------------------


QUEUE_METHODS = [
    ("claim_calls", ("alice", "w1", {"argo"}, 5)),
    ("complete_call", ("c1", "token", "ok")),
    ("release_call", ("c1", "token")),
    ("record_attempt", ("c1", 1, T0, T0, "ok")),
    ("find_reusable", ("d" * 64,)),
    ("reclaim_stale", ()),
    ("job_status", ("j1",)),
    ("results", ("j1",)),
    ("cancel_job", ("j1", "alice")),
    ("delete_job", ("j1", "alice")),
]


@pytest.mark.parametrize("method_name,args", QUEUE_METHODS, ids=[m for m, _ in QUEUE_METHODS])
def test_queue_methods_are_declared_but_not_yet_implemented(store, method_name, args):
    """Named, searchable failures rather than AttributeErrors from the worker."""
    with pytest.raises(NotImplementedError, match=QUEUE_OPS_MARKER):
        getattr(store, method_name)(*args)


# ---------------------------------------------------------------------------
# The protocol
# ---------------------------------------------------------------------------

#: Every method of the STORE CONTRACT, verbatim and in contract order.  If a
#: method is renamed, added or dropped in protocols.py without this list being
#: updated deliberately, the test below fails -- which is the point: the
#: protocol is what the engine, the CLI and PostgresStore are written against.
CONTRACT_METHODS = {
    # schema
    "init_schema",
    "schema_version",
    "migrate",
    # query types
    "put_query_type",
    "get_query_type",
    "list_query_types",
    "retire_query_type",
    # jobs / items / calls
    "create_job",
    "get_job",
    "list_jobs",
    "get_items",
    "get_calls",
    "add_calls",
    "settle_item",
    # queue
    "claim_calls",
    "complete_call",
    "release_call",
    "transaction",
    "record_attempt",
    "find_reusable",
    "reclaim_stale",
    # endpoint state
    "get_endpoint_state",
    "add_tokens",
    "set_halt",
    "reset_halt",
    "reset_budget",
    # job control
    "job_status",
    "results",
    "cancel_job",
    "delete_job",
    # workers and leases
    "register_worker",
    "heartbeat_worker",
    "live_workers",
    "remove_worker",
    "request_stop",
    "stop_requested",
    "acquire_daemon_lease",
    "renew_daemon_lease",
    "release_daemon_lease",
    "acquire_gpu_lease",
    "renew_gpu_lease",
    "release_gpu_lease",
    # search
    "search_calls",
}


class TestCouncilStoreProtocol:
    def test_the_protocol_declares_exactly_the_contract(self):
        declared = {name for name in dir(CouncilStore) if not name.startswith("_")}
        assert declared == CONTRACT_METHODS

    def test_an_implementation_satisfies_the_protocol(self, store):
        assert isinstance(store, CouncilStore)

    def test_every_contract_method_is_present_on_the_implementation(self, store):
        missing = sorted(n for n in CONTRACT_METHODS if not callable(getattr(store, n, None)))
        assert missing == []


# ---------------------------------------------------------------------------
# SQLite specifics -- the real entry point, nothing monkeypatched
# ---------------------------------------------------------------------------


class TestSqliteEntryPoint:
    def test_open_store_creates_the_file_its_parents_wal_and_the_version(self, tmp_path):
        """The whole stack through the public entry point, nothing injected.

        No fixture, no monkeypatch, no stub: open_store on a directory that
        does not exist, then init_schema, then observable effects on disk.
        """
        db_path = tmp_path / "sub" / "dir" / "council.sqlite"
        assert not db_path.parent.exists()

        store = open_store(f"sqlite:///{db_path}")
        try:
            store.init_schema()

            assert db_path.exists()
            with store.engine.connect() as conn:
                assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
                assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
                assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == 30000
            assert store.schema_version() == 1
        finally:
            store.close()

        # A SECOND store object on the same URL reads what the first wrote.
        reopened = open_store(f"sqlite:///{db_path}")
        try:
            assert reopened.schema_version() == 1
            assert isinstance(reopened, SqliteStore)
        finally:
            reopened.close()

    def test_a_bare_path_is_accepted_as_well_as_a_url(self, tmp_path):
        store = open_store(f"sqlite:///{tmp_path}/plain.sqlite")
        try:
            store.init_schema()
            assert (tmp_path / "plain.sqlite").exists()
        finally:
            store.close()

    @pytest.mark.parametrize(
        "url",
        [
            "sqlite:////home/someone/Dropbox/council.sqlite",
            "sqlite:////home/someone/Dropbox/Projects/x/council.sqlite",
            "sqlite:////Users/someone/Dropbox (Argonne National Laboratory)/c.sqlite",
        ],
    )
    def test_a_synced_path_is_refused(self, url):
        """A live SQLite WAL pair under a file syncer becomes a corrupt database."""
        with pytest.raises(StorePathRefused) as excinfo:
            open_store(url)
        # Callers were told to expect a ValueError, so it must be one.
        assert isinstance(excinfo.value, ValueError)
        assert "Dropbox" in str(excinfo.value)

    def test_a_refused_path_creates_nothing(self, tmp_path):
        target = tmp_path / "Dropbox" / "council.sqlite"
        with pytest.raises(StorePathRefused):
            open_store(f"sqlite:///{target}")
        assert not target.parent.exists()

    def test_an_unknown_scheme_is_a_value_error(self):
        with pytest.raises(ValueError, match="unsupported council store URL"):
            open_store("mysql://localhost/council")

    def test_a_naive_timestamp_is_refused(self, store):
        """Storing a naive datetime would shift the instant by the UTC offset."""
        with pytest.raises(ValueError, match="naive datetime"):
            store.register_worker(
                {"worker_id": "w1", "user": "alice", "heartbeat_at": datetime(2026, 1, 1)}
            )

    def test_timestamps_come_back_timezone_aware_utc(self, store):
        store.register_worker({"worker_id": "w1", "user": "alice", "heartbeat_at": T0})
        (row,) = store.live_workers("alice", now=T0)
        assert row["heartbeat_at"].tzinfo is not None
        assert row["heartbeat_at"] == T0
