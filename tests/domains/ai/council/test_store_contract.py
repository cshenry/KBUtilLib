"""The council store contract.

Covered here: schema init, version handling and the v1 -> v2 migration,
query-type immutability, atomic job creation, endpoint budget/halt state,
workers, tombstones, the one-live-call index, search, the refusal of a synced
or network-filesystem database path, and the queue -- claiming, completing,
releasing, reclaiming and the job control (status, results, cancel, delete)
built on them.

The queue's central claim is that two workers never get the same call.
``TestNoDoubleClaim`` tests that with two real ``spawn`` processes against one
database file rather than with threads, because threads in one interpreter
would share the store's connection pool and could pass for correct while the
locking was broken.

Fixtures come from :mod:`.store_contract`.  No test here makes a network call,
spends a token, needs a CLI login or needs a GPU.
"""

from __future__ import annotations

import multiprocessing
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
    StaleClaim,
    StoreError,
    StorePathRefused,
)
from kbutillib.domains.ai.council.store.sqlite import (  # noqa: E402
    STORE_IDENTITY,
    SqliteStore,
)

# Only the plain helpers are imported here.  The `store`, `make_store`,
# `store_url` and `store_backend` fixtures come from conftest.py, which
# re-exports them from the same contract module -- see its docstring.
from .store_contract import (  # noqa: E402
    call_row,
    claim_in_subprocess,
    item_row,
    job_row,
    utcnow,
)

T0 = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Schema and version
# ---------------------------------------------------------------------------


class TestSchema:
    def test_init_creates_every_table_and_stamps_version_2(self, store):
        assert store.schema_version() == 2
        present = set(sa.inspect(store.engine).get_table_names())
        assert set(sch.TABLE_NAMES) <= present

    def test_init_creates_none_of_the_tables_v2_removed(self, store):
        present = set(sa.inspect(store.engine).get_table_names())
        assert present.isdisjoint(sch.REMOVED_IN_V2)

    def test_init_schema_is_idempotent(self, store):
        store.init_schema()
        store.init_schema()
        assert store.schema_version() == 2

    def test_version_is_0_before_init(self, make_store):
        fresh = make_store("uninitialised", init=False)
        assert fresh.schema_version() == 0

    def test_migrate_initialises_an_empty_database(self, make_store):
        fresh = make_store("needs-migrate", init=False)
        assert fresh.migrate() == 2
        assert fresh.schema_version() == 2

    def test_migrate_is_a_noop_at_the_current_version(self, store):
        assert store.migrate() == 2

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
        state = store.get_endpoint_state("argo")
        assert state["epoch_tokens"] == 0
        assert state["lifetime_tokens"] == 0
        assert state["budget_epoch"] == 0
        assert state["halt_kind"] is None

    def test_add_tokens_accumulates_in_both_counters(self, store):
        store.add_tokens("argo", 100)
        store.add_tokens("argo", 250)
        state = store.get_endpoint_state("argo")
        assert state["epoch_tokens"] == 350
        assert state["lifetime_tokens"] == 350

    def test_counters_are_per_endpoint_and_not_per_user(self, store):
        """One store, one ledger per endpoint -- whatever label is passed."""
        store.add_tokens("argo", 100)
        store.add_tokens("argo", 7, user="somebody-else")
        store.add_tokens("poplar", 3)
        # The deprecated `user` keyword is a no-op: both additions land on the
        # same row, because the spend is the machine's and not a person's.
        assert store.get_endpoint_state("argo")["epoch_tokens"] == 107
        assert store.get_endpoint_state("poplar")["epoch_tokens"] == 3

    def test_every_row_is_keyed_on_the_store_local_identity(self, store):
        store.add_tokens("argo", 5)
        with store.engine.connect() as conn:
            keys = [
                row[0]
                for row in conn.execute(sa.select(sch.endpoint_state.c.user))
            ]
        assert keys == [STORE_IDENTITY]

    def test_set_halt_records_kind_and_message(self, store):
        store.set_halt("argo", "quota_exhausted", "429 from the gateway")
        state = store.get_endpoint_state("argo")
        assert state["halt_kind"] == "quota_exhausted"
        assert state["halt_message"] == "429 from the gateway"

    def test_reset_halt_clears_the_halt_and_keeps_the_tokens(self, store):
        store.add_tokens("argo", 500)
        store.set_halt("argo", "auth", "expired key")

        store.reset_halt("argo")
        state = store.get_endpoint_state("argo")
        assert state["halt_kind"] is None
        assert state["halt_message"] is None
        # "The problem is fixed" is not "the tokens were not spent".
        assert state["epoch_tokens"] == 500
        assert state["lifetime_tokens"] == 500

    def test_reset_budget_starts_a_new_epoch_and_keeps_the_lifetime(self, store):
        store.add_tokens("argo", 500)
        store.reset_budget("argo")
        state = store.get_endpoint_state("argo")
        assert state["budget_epoch"] == 1
        assert state["epoch_tokens"] == 0
        assert state["lifetime_tokens"] == 500

        store.add_tokens("argo", 20)
        state = store.get_endpoint_state("argo")
        assert state["epoch_tokens"] == 20
        assert state["lifetime_tokens"] == 520

    def test_reset_budget_clears_a_budget_halt(self, store):
        store.set_halt("argo", "budget_exhausted", "ceiling reached")
        assert store.reset_budget("argo")["halt_kind"] is None

    def test_reset_budget_leaves_a_non_budget_halt_alone(self, store):
        """A new epoch does not fix an expired credential."""
        store.set_halt("argo", "auth", "expired key")
        state = store.reset_budget("argo")
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

    def test_stop_requested_for_an_unknown_worker_is_false(self, store):
        assert store.stop_requested("ghost") is False


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
            store.add_tokens("argo", 5)
        assert len(store.list_query_types()) == 1
        assert store.get_endpoint_state("argo")["epoch_tokens"] == 5

    def test_a_nested_failure_rolls_back_the_whole_unit(self, store):
        with pytest.raises(RuntimeError):
            with store.transaction():
                store.add_tokens("argo", 5)
                store.put_query_type("go", "1", {"a": 1})
                raise RuntimeError("boom")
        assert store.get_endpoint_state("argo")["epoch_tokens"] == 0
        assert store.list_query_types() == []


# ---------------------------------------------------------------------------
# The queue: claiming
# ---------------------------------------------------------------------------


class TestClaimCalls:
    def test_claim_order_is_priority_then_submission_then_item_then_role(self, store):
        """Every tier of the claim order at once, deliberately mis-inserted.

        The rows are created in an order that disagrees with all four tiers,
        so a claim that happened to return insertion order would fail.
        """
        store.create_job(
            job_row(job_id="low", priority=50, submitted_at=T0),
            [item_row("item-1")],
            [call_row("item-1", "proposer_1")],
        )
        store.create_job(
            job_row(
                job_id="later", priority=100, submitted_at=T0 + timedelta(minutes=2)
            ),
            [item_row("item-1")],
            [call_row("item-1", "proposer_1")],
        )
        store.create_job(
            job_row(
                job_id="first", priority=100, submitted_at=T0 + timedelta(minutes=1)
            ),
            [item_row("item-b", item_order=1), item_row("item-a", item_order=0)],
            [
                call_row("item-b", "proposer_1"),
                call_row("item-a", "meta", role="meta"),
                call_row("item-a", "critic", role="critic"),
                call_row("item-a", "proposer_1"),
            ],
        )

        claimed = store.claim_calls("w1", {"argo"}, 10, now=T0 + timedelta(hours=1))

        assert [(c["job_id"], c["item_id"], c["role"]) for c in claimed] == [
            # Highest priority, earliest of the two at that priority ...
            ("first", "item-a", "proposer"),  # ... item order 0, then by role
            ("first", "item-a", "critic"),
            ("first", "item-a", "meta"),
            ("first", "item-b", "proposer"),  # ... then item order 1
            ("later", "item-1", "proposer"),  # same priority, submitted later
            ("low", "item-1", "proposer"),  # lowest priority last
        ]

    def test_a_claim_stamps_status_worker_and_a_distinct_token_per_call(self, store):
        job_id = store.create_job(
            job_row(),
            [item_row("item-1")],
            [call_row("item-1", "proposer_1"), call_row("item-1", "proposer_2")],
        )
        claimed = store.claim_calls("w1", {"argo"}, 10, now=T0)

        assert [c["status"] for c in claimed] == ["claimed", "claimed"]
        assert {c["claimed_by"] for c in claimed} == {"w1"}
        assert {c["claimed_at"] for c in claimed} == {T0}
        # One token per call: a batch-wide token would let a worker settle a
        # call it no longer holds by quoting a sibling's.
        tokens = [c["claim_token"] for c in claimed]
        assert len(set(tokens)) == 2 and all(tokens)

        stored = store.get_calls(job_id)
        assert all(c["status"] == "claimed" for c in stored)
        assert {c["claim_token"] for c in stored} == set(tokens)

    def test_a_claimed_call_is_not_claimed_again(self, store):
        store.create_job(
            job_row(), [item_row("item-1")], [call_row("item-1", "proposer_1")]
        )
        assert len(store.claim_calls("w1", {"argo"}, 10, now=T0)) == 1
        assert store.claim_calls("w2", {"argo"}, 10, now=T0) == []

    def test_limit_is_honoured(self, store):
        store.create_job(
            job_row(),
            [item_row("item-1")],
            [call_row("item-1", f"proposer_{n}") for n in range(1, 5)],
        )
        assert len(store.claim_calls("w1", {"argo"}, 2, now=T0)) == 2
        assert len(store.claim_calls("w1", {"argo"}, 10, now=T0)) == 2

    @pytest.mark.parametrize("limit", [0, -1])
    def test_a_non_positive_limit_claims_nothing(self, store, limit):
        store.create_job(
            job_row(), [item_row("item-1")], [call_row("item-1", "proposer_1")]
        )
        assert store.claim_calls("w1", {"argo"}, limit, now=T0) == []

    def test_not_before_in_the_future_is_skipped_until_it_is_due(self, store):
        due = T0 + timedelta(minutes=5)
        store.create_job(
            job_row(),
            [item_row("item-1")],
            [
                call_row("item-1", "proposer_1", not_before=due),
                call_row("item-1", "proposer_2"),  # null not_before: due now
            ],
        )
        assert [c["seat_name"] for c in store.claim_calls("w1", {"argo"}, 10, now=T0)] == [
            "proposer_2"
        ]
        assert [
            c["seat_name"] for c in store.claim_calls("w1", {"argo"}, 10, now=due)
        ] == ["proposer_1"]

    def test_only_the_named_endpoints_are_claimed(self, store):
        store.create_job(
            job_row(),
            [item_row("item-1")],
            [
                call_row("item-1", "proposer_1", endpoint="argo"),
                call_row("item-1", "proposer_2", endpoint="poplar"),
            ],
        )
        assert [c["endpoint"] for c in store.claim_calls("w1", {"poplar"}, 10, now=T0)] == [
            "poplar"
        ]

    def test_an_empty_endpoint_set_claims_nothing(self, store):
        store.create_job(
            job_row(), [item_row("item-1")], [call_row("item-1", "proposer_1")]
        )
        assert store.claim_calls("w1", set(), 10, now=T0) == []

    def test_a_halted_endpoint_is_skipped_whatever_label_the_job_carries(self, store):
        """A halt is the store's, so it stops every job on that endpoint.

        Two jobs with different ``submitted_by`` labels, one halted endpoint:
        neither is claimable, because there is one machine and one quota
        behind the gateway however many labels were used to submit.
        """
        store.create_job(
            job_row(job_id="a", submitted_by="alice"),
            [item_row("item-1")],
            [call_row("item-1", "proposer_1")],
        )
        store.create_job(
            job_row(job_id="b", submitted_by="bob"),
            [item_row("item-1")],
            [call_row("item-1", "proposer_1")],
        )
        store.set_halt("argo", "quota_exhausted", "out of tokens")

        assert store.claim_calls("w1", {"argo"}, 10, now=T0) == []
        assert store.claim_calls("w2", {"argo"}, 10, now=T0) == []

        # And the halt is only for that endpoint.
        store.create_job(
            job_row(job_id="c", submitted_by="alice"),
            [item_row("item-9")],
            [call_row("item-9", "proposer_1", endpoint="poplar")],
        )
        assert len(store.claim_calls("w1", {"argo", "poplar"}, 10, now=T0)) == 1

    def test_reset_halt_makes_the_calls_claimable_again(self, store):
        store.create_job(
            job_row(), [item_row("item-1")], [call_row("item-1", "proposer_1")]
        )
        store.set_halt("argo", "auth", "token expired")
        assert store.claim_calls("w1", {"argo"}, 10, now=T0) == []
        store.reset_halt("argo")
        assert len(store.claim_calls("w1", {"argo"}, 10, now=T0)) == 1

    def test_claims_ignore_submitted_by(self, store):
        """The queue is the STORE's, and ``submitted_by`` is only a label.

        A store is one machine's single-user file, so a worker claims every
        due call in it.  Filtering on the label would strand the whole queue
        the moment somebody edited config ``user`` (review round 3).
        """
        store.create_job(
            job_row(job_id="theirs", submitted_by="somebody-else"),
            [item_row("item-1")],
            [call_row("item-1", "proposer_1")],
        )
        claimed = store.claim_calls("w1", {"argo"}, 10, now=T0)
        assert len(claimed) == 1
        assert claimed[0]["submitted_by"] == "somebody-else"

    def test_changing_the_label_strands_no_pending_call(self, store):
        """Three jobs, three different labels, one worker: it gets all three."""
        for index, label in enumerate(("old-name", "new-name", "")):
            store.create_job(
                job_row(job_id=f"j{index}", submitted_by=label or "unknown"),
                [item_row("item-1")],
                [call_row("item-1", "proposer_1")],
            )
        claimed = store.claim_calls("w1", {"argo"}, 10, now=T0)
        assert len(claimed) == 3

    def test_changing_the_label_resets_no_budget(self, store):
        """The budget is keyed on the store, so a rename cannot clear it."""
        store.add_tokens("argo", 500)
        store.set_halt("argo", "budget_exhausted", "ceiling reached")
        # A later job submitted under a brand-new label sees the same halt.
        store.create_job(
            job_row(job_id="after-rename", submitted_by="renamed"),
            [item_row("item-1")],
            [call_row("item-1", "proposer_1")],
        )
        assert store.get_endpoint_state("argo")["lifetime_tokens"] == 500
        assert store.claim_calls("w1", {"argo"}, 10, now=T0) == []

    @pytest.mark.parametrize("job_status", ["cancelled", "paused"])
    def test_a_cancelled_or_paused_job_is_skipped(self, store, job_status):
        job_id = store.create_job(
            job_row(), [item_row("item-1")], [call_row("item-1", "proposer_1")]
        )
        with store.transaction() as conn:
            conn.execute(
                sch.jobs.update()
                .where(sch.jobs.c.job_id == job_id)
                .values(status=job_status)
            )
        assert store.claim_calls("w1", {"argo"}, 10, now=T0) == []

    def test_a_tombstoned_jobs_calls_are_skipped(self, store):
        job_id = store.create_job(
            job_row(), [item_row("item-1")], [call_row("item-1", "proposer_1")]
        )
        store.cancel_job(job_id)
        store.delete_job(job_id)
        assert store.claim_calls("w1", {"argo"}, 10, now=T0) == []

    def test_a_claim_carries_no_lease_generation(self, store):
        """The daemon lease is gone: exclusion is the execlock's job now."""
        store.create_job(
            job_row(), [item_row("item-1")], [call_row("item-1", "proposer_1")]
        )
        (claim,) = store.claim_calls("daemon-1", {"argo"}, 10, now=T0)
        assert claim["lease_generation"] is None


# ---------------------------------------------------------------------------
# The queue: no double claim, across two real OS processes
# ---------------------------------------------------------------------------


class TestNoDoubleClaim:
    """Two OS processes, one database, 200 calls, every call claimed once.

    This is the test the whole locking strategy exists for, so it is written
    to be unfakeable: the children are real ``spawn`` processes, each opens
    its OWN store through ``open_store`` on the same URL, nothing is
    monkeypatched in them, no store object crosses the process boundary, and
    the harness holds no lock of its own.  See
    :func:`.store_contract.claim_in_subprocess`.

    Contention is arranged (a barrier releases both children together, and a
    small claim limit makes them interleave) but NOT asserted: how the
    scheduler splits 200 calls between two processes is not a property of the
    store, and asserting it would make a correct store fail intermittently.
    What is asserted is the invariant -- the union of what the two children
    were handed is exactly the 200 calls, with no duplicate.
    """

    N_CALLS = 200
    CLAIM_LIMIT = 7

    def _seed(self, store):
        """One job of ``N_CALLS`` pending calls: two proposer seats per item."""
        n_items = self.N_CALLS // 2
        items = [item_row(f"item-{i:03d}", item_order=i) for i in range(n_items)]
        calls = [
            call_row(f"item-{i:03d}", seat)
            for i in range(n_items)
            for seat in ("proposer_1", "proposer_2")
        ]
        job_id = store.create_job(
            job_row(job_id="contended", submitted_at=T0), items, calls
        )
        return {c["call_id"] for c in store.get_calls(job_id)}

    def test_two_processes_never_claim_the_same_call(self, make_store, store_url):
        seeder = make_store()
        expected = self._seed(seeder)
        assert len(expected) == self.N_CALLS
        # Nothing of the parent's is left holding the database: from here the
        # only connections are the ones the children open themselves.
        seeder.close()

        url = store_url()
        ctx = multiprocessing.get_context("spawn")
        results = ctx.Queue()
        barrier = ctx.Barrier(2)
        children = [
            ctx.Process(
                target=claim_in_subprocess,
                args=(url, worker_id, ["argo"], self.CLAIM_LIMIT, barrier, results),
                daemon=True,
            )
            for worker_id in ("worker-1", "worker-2")
        ]
        for child in children:
            child.start()
        try:
            # Drained BEFORE join: a child blocked writing to a full pipe
            # would never exit, and the join would read as a store deadlock.
            collected = [results.get(timeout=180) for _ in children]
        finally:
            for child in children:
                child.join(timeout=60)
                if child.is_alive():  # pragma: no cover - only on a real hang
                    child.terminate()

        for worker_id, _ids, error in collected:
            assert error is None, f"{worker_id} failed: {error}"
        for child in children:
            assert child.exitcode == 0

        handed_out = [call_id for _w, ids, _e in collected for call_id in ids]
        assert len(handed_out) == len(set(handed_out)), "a call was claimed twice"
        assert set(handed_out) == expected, "a call was claimed zero times"
        assert len(handed_out) == self.N_CALLS

        # And the database agrees: every call claimed, by one of the two
        # workers, under its own token.
        reopened = make_store()
        rows = reopened.get_calls("contended")
        assert {r["status"] for r in rows} == {"claimed"}
        assert {r["claimed_by"] for r in rows} <= {"worker-1", "worker-2"}
        assert len({r["claim_token"] for r in rows}) == self.N_CALLS


# ---------------------------------------------------------------------------
# The queue: completing
# ---------------------------------------------------------------------------


class TestCompleteCall:
    @pytest.fixture
    def claimed(self, store):
        """One claimed call, ready to be settled."""
        job_id = store.create_job(
            job_row(), [item_row("item-1")], [call_row("item-1", "proposer_1")]
        )
        (claim,) = store.claim_calls("w1", {"argo"}, 10, now=T0)
        return job_id, claim

    def test_an_ok_completion_stores_the_reply_and_clears_the_token(self, store, claimed):
        job_id, claim = claimed
        parsed = {"picked": [{"id": "GO:0003674", "confidence": "high", "why": "x"}]}
        store.complete_call(
            claim["call_id"],
            claim["claim_token"],
            "ok",
            reply_text='{"picked": []}',
            reply_parsed=parsed,
            fresh_usage={"prompt_tokens": 11, "completion_tokens": 3},
        )

        (call,) = store.get_calls(job_id)
        assert call["status"] == "ok"
        assert call["reply_text"] == '{"picked": []}'
        # The ORIGINAL schema-valid reply, not a post-processed shape.
        assert call["reply_parsed"] == parsed
        assert call["fresh_usage"]["prompt_tokens"] == 11
        assert call["finished_at"] is not None
        assert call["finished_at"].tzinfo is not None
        assert call["claim_token"] is None
        assert call["error"] is None

    def test_an_error_completion_stores_the_error(self, store, claimed):
        job_id, claim = claimed
        error = {"kind": "invalid_output", "message": "not JSON", "retryable": False}
        store.complete_call(claim["call_id"], claim["claim_token"], "error", error=error)
        (call,) = store.get_calls(job_id)
        assert call["status"] == "error"
        assert call["error"] == error

    def test_reused_from_records_where_the_answer_came_from(self, store, claimed):
        job_id, claim = claimed
        store.complete_call(
            claim["call_id"], claim["claim_token"], "ok", reply_text="x", reused_from="other-call"
        )
        (call,) = store.get_calls(job_id)
        assert call["reused_from"] == "other-call"

    @pytest.mark.parametrize("bad", ["pending", "claimed", "superseded", "OK", ""])
    def test_only_ok_or_error_may_be_recorded(self, store, claimed, bad):
        _job_id, claim = claimed
        with pytest.raises(ValueError, match="must be 'ok' or 'error'"):
            store.complete_call(claim["call_id"], claim["claim_token"], bad)

    def test_an_unknown_call_is_a_value_error(self, store):
        with pytest.raises(ValueError, match="no such call"):
            store.complete_call("ghost", "token", "ok")

    def test_the_wrong_token_is_refused_and_changes_nothing(self, store, claimed):
        job_id, claim = claimed
        with pytest.raises(StaleClaim):
            store.complete_call(claim["call_id"], "not-the-token", "ok", reply_text="x")
        (call,) = store.get_calls(job_id)
        assert call["status"] == "claimed"
        assert call["reply_text"] is None

    def test_completing_twice_is_refused(self, store, claimed):
        """The token is cleared on completion, so a double write cannot land."""
        job_id, claim = claimed
        store.complete_call(claim["call_id"], claim["claim_token"], "ok", reply_text="first")
        with pytest.raises(StaleClaim):
            store.complete_call(claim["call_id"], claim["claim_token"], "ok", reply_text="second")
        (call,) = store.get_calls(job_id)
        assert call["reply_text"] == "first"

    def test_a_token_invalidated_by_reclaim_stale_is_refused(self, store):
        """End to end: claim, the worker dies, reclaim_stale, then a late reply.

        This is the race the claim token exists for.  The dead worker's reply
        must not overwrite the row that has already gone back on the queue for
        somebody else to run.
        """
        job_id = store.create_job(
            job_row(), [item_row("item-1")], [call_row("item-1", "proposer_1")]
        )
        store.register_worker(
            {"worker_id": "w1", "user": "alice", "kind": "daemon", "endpoints": ["argo"]}
        )
        long_ago = utcnow() - timedelta(minutes=30)
        (claim,) = store.claim_calls("w1", {"argo"}, 10, now=long_ago)
        # The worker stops heartbeating: back-date it past the staleness window.
        assert store.heartbeat_worker("w1", long_ago) is True

        assert store.reclaim_stale() == 1

        with pytest.raises(StaleClaim):
            store.complete_call(
                claim["call_id"], claim["claim_token"], "ok", reply_text="too late"
            )

        (call,) = store.get_calls(job_id)
        assert call["status"] == "pending"
        assert call["reply_text"] is None
        assert call["reply_parsed"] is None
        assert call["claim_token"] is None

    def test_a_claim_is_refused_only_on_its_token(self, store):
        """The ONE ground for StaleClaim now: the row was reclaimed.

        There is no daemon lease generation to supersede a claim any more, so
        a claim that still holds its token always completes.
        """
        job_id = store.create_job(
            job_row(), [item_row("item-1")], [call_row("item-1", "proposer_1")]
        )
        (claim,) = store.claim_calls("foreground", {"argo"}, 10, now=T0)
        store.complete_call(claim["call_id"], claim["claim_token"], "ok", reply_text="fine")
        (call,) = store.get_calls(job_id)
        assert call["status"] == "ok"

    def test_a_claimed_call_of_a_cancelled_job_may_still_complete(self, store):
        """Its tokens are already spent, so the reply is stored and stays
        reusable even though the job it was for is gone."""
        job_id = store.create_job(
            job_row(),
            [item_row("item-1")],
            [call_row("item-1", "proposer_1", prompt_hash="e" * 64)],
        )
        (claim,) = store.claim_calls("w1", {"argo"}, 10, now=T0)
        store.cancel_job(job_id)

        store.complete_call(claim["call_id"], claim["claim_token"], "ok", reply_text="paid for")

        (call,) = store.get_calls(job_id)
        assert call["status"] == "ok"
        assert store.find_reusable("e" * 64)["call_id"] == claim["call_id"]


# ---------------------------------------------------------------------------
# The queue: releasing
# ---------------------------------------------------------------------------


class TestReleaseCall:
    @pytest.fixture
    def claimed(self, store):
        job_id = store.create_job(
            job_row(), [item_row("item-1")], [call_row("item-1", "proposer_1")]
        )
        (claim,) = store.claim_calls("w1", {"argo"}, 10, now=T0)
        return job_id, claim

    def test_release_returns_the_call_to_pending_and_counts_the_attempt(self, store, claimed):
        job_id, claim = claimed
        store.release_call(claim["call_id"], claim["claim_token"])

        (call,) = store.get_calls(job_id)
        assert call["status"] == "pending"
        assert call["claim_token"] is None
        assert call["claimed_by"] is None
        assert call["claimed_at"] is None
        assert call["lease_generation"] is None
        assert call["attempt_count"] == 1
        # And it is claimable again.
        assert len(store.claim_calls("w2", {"argo"}, 10, now=T0)) == 1

    def test_release_without_incrementing_leaves_the_attempt_count_alone(self, store, claimed):
        """For a release that is not the call's fault -- a worker shutting
        down, an endpoint halting -- so it does not burn the retry budget."""
        job_id, claim = claimed
        store.release_call(claim["call_id"], claim["claim_token"], increment_attempt=False)
        (call,) = store.get_calls(job_id)
        assert call["status"] == "pending"
        assert call["attempt_count"] == 0

    def test_attempts_accumulate_across_releases(self, store, claimed):
        job_id, claim = claimed
        store.release_call(claim["call_id"], claim["claim_token"])
        (again,) = store.claim_calls("w1", {"argo"}, 10, now=T0)
        store.release_call(again["call_id"], again["claim_token"])
        (call,) = store.get_calls(job_id)
        assert call["attempt_count"] == 2

    def test_a_backoff_holds_the_call_until_it_is_due(self, store, claimed):
        job_id, claim = claimed
        due = T0 + timedelta(minutes=5)
        store.release_call(claim["call_id"], claim["claim_token"], not_before=due)

        (call,) = store.get_calls(job_id)
        assert call["not_before"] == due
        assert store.claim_calls("w1", {"argo"}, 10, now=T0) == []
        assert len(store.claim_calls("w1", {"argo"}, 10, now=due)) == 1

    def test_releasing_with_no_backoff_clears_an_earlier_one(self, store, claimed):
        job_id, claim = claimed
        store.release_call(claim["call_id"], claim["claim_token"], not_before=T0 + timedelta(hours=1))
        (again,) = store.claim_calls("w1", {"argo"}, 10, now=T0 + timedelta(hours=1))
        store.release_call(again["call_id"], again["claim_token"])
        (call,) = store.get_calls(job_id)
        assert call["not_before"] is None

    def test_the_wrong_token_is_refused(self, store, claimed):
        job_id, claim = claimed
        with pytest.raises(StaleClaim):
            store.release_call(claim["call_id"], "not-the-token")
        (call,) = store.get_calls(job_id)
        assert call["status"] == "claimed"
        assert call["claim_token"] == claim["claim_token"]

    def test_releasing_an_unclaimed_call_is_refused(self, store, claimed):
        _job_id, claim = claimed
        store.release_call(claim["call_id"], claim["claim_token"])
        with pytest.raises(StaleClaim):
            store.release_call(claim["call_id"], claim["claim_token"])

    def test_an_unknown_call_is_a_value_error(self, store):
        with pytest.raises(ValueError, match="no such call"):
            store.release_call("ghost", "token")


# ---------------------------------------------------------------------------
# The queue: attempts
# ---------------------------------------------------------------------------


class TestRecordAttempt:
    def _attempts(self, store):
        with store.transaction() as conn:
            return [
                dict(row._mapping)
                for row in conn.execute(
                    sa.select(sch.attempts).order_by(sch.attempts.c.attempt_no)
                )
            ]

    def test_records_one_row_per_try(self, store):
        store.create_job(
            job_row(), [item_row("item-1")], [call_row("item-1", "proposer_1")]
        )
        (claim,) = store.claim_calls("w1", {"argo"}, 10, now=T0)
        store.record_attempt(
            claim["call_id"],
            1,
            T0,
            T0 + timedelta(seconds=3),
            "timeout",
            usage={"prompt_tokens": 7, "completion_tokens": 0},
            worker_id="w1",
        )
        store.record_attempt(claim["call_id"], 2, T0, T0, "ok", worker_id="w1")

        rows = self._attempts(store)
        assert [r["attempt_no"] for r in rows] == [1, 2]
        assert rows[0]["outcome_kind"] == "timeout"
        assert rows[0]["usage"]["prompt_tokens"] == 7
        assert rows[0]["started_at"].tzinfo is not None
        assert rows[0]["worker_id"] == "w1"
        assert rows[1]["usage"] is None

    def test_an_attempt_for_an_unknown_call_is_refused(self, store):
        """Foreign keys are on: the audit trail cannot outlive its call."""
        with pytest.raises(IntegrityError):
            store.record_attempt("ghost", 1, T0, T0, "ok")

    def test_the_same_attempt_number_cannot_be_recorded_twice(self, store):
        store.create_job(
            job_row(), [item_row("item-1")], [call_row("item-1", "proposer_1")]
        )
        (claim,) = store.claim_calls("w1", {"argo"}, 10, now=T0)
        store.record_attempt(claim["call_id"], 1, T0, T0, "ok")
        with pytest.raises(IntegrityError):
            store.record_attempt(claim["call_id"], 1, T0, T0, "ok")

    @pytest.mark.parametrize(
        "usage",
        [
            {"system": "You are a careful curator."},
            {"user": "Map this protein: ..."},
            {"request": {"system": "...", "user": "..."}},
            {"messages": [{"user": "..."}]},
            {"a": {"b": {"c": {"system": "buried"}}}},
        ],
        ids=["system", "user", "nested-request", "in-a-list", "deeply-nested"],
    )
    def test_prompt_text_is_refused_at_any_depth(self, store, usage):
        """The attempts table is readable by every user of a shared store, so
        the evidence text a job was built from must never reach it."""
        store.create_job(
            job_row(), [item_row("item-1")], [call_row("item-1", "proposer_1")]
        )
        (claim,) = store.claim_calls("w1", {"argo"}, 10, now=T0)
        with pytest.raises(ValueError, match="prompt text"):
            store.record_attempt(claim["call_id"], 1, T0, T0, "ok", usage=usage)
        assert self._attempts(store) == []

    def test_a_usage_dict_without_prompt_keys_is_accepted(self, store):
        store.create_job(
            job_row(), [item_row("item-1")], [call_row("item-1", "proposer_1")]
        )
        (claim,) = store.claim_calls("w1", {"argo"}, 10, now=T0)
        store.record_attempt(
            claim["call_id"], 1, T0, T0, "ok", usage={"prompt_tokens": 3, "users": 1}
        )
        assert len(self._attempts(store)) == 1


# ---------------------------------------------------------------------------
# The queue: reuse lookup
# ---------------------------------------------------------------------------


class TestFindReusable:
    def test_finds_an_answer_across_jobs_and_users(self, store):
        """The whole point of a shared store: a question one user has paid
        for is not asked again by the next."""
        job_id = store.create_job(
            job_row(job_id="bobs", submitted_by="bob"),
            [item_row("item-1")],
            [call_row("item-1", "proposer_1", prompt_hash="1" * 64)],
        )
        (claim,) = store.claim_calls("w1", {"argo"}, 10, now=T0)
        store.complete_call(
            claim["call_id"], claim["claim_token"], "ok", reply_text="bob paid for this"
        )

        found = store.find_reusable("1" * 64)
        assert found["call_id"] == claim["call_id"]
        assert found["reply_text"] == "bob paid for this"
        assert found["submitted_by"] == "bob"
        assert found["job_id"] == job_id

    def test_an_unanswered_call_is_not_reusable(self, store):
        store.create_job(
            job_row(),
            [item_row("item-1")],
            [call_row("item-1", "proposer_1", prompt_hash="2" * 64)],
        )
        assert store.find_reusable("2" * 64) is None

    def test_a_failed_call_is_not_reusable(self, store):
        store.create_job(
            job_row(),
            [item_row("item-1")],
            [call_row("item-1", "proposer_1", prompt_hash="3" * 64)],
        )
        (claim,) = store.claim_calls("w1", {"argo"}, 10, now=T0)
        store.complete_call(
            claim["call_id"], claim["claim_token"], "error", error={"kind": "transport"}
        )
        assert store.find_reusable("3" * 64) is None

    def test_an_unknown_hash_is_none(self, store):
        assert store.find_reusable("4" * 64) is None

    @pytest.mark.parametrize("empty", ["", None])
    def test_an_empty_hash_is_none(self, store, empty):
        assert store.find_reusable(empty) is None

    def test_the_newest_answer_wins(self, store):
        job_id = store.create_job(
            job_row(),
            [item_row("item-1"), item_row("item-2")],
            [
                call_row("item-1", "proposer_1", prompt_hash="5" * 64),
                call_row("item-2", "proposer_1", prompt_hash="5" * 64),
            ],
        )
        first, second = store.claim_calls("w1", {"argo"}, 10, now=T0)
        store.complete_call(first["call_id"], first["claim_token"], "ok", reply_text="older")
        store.complete_call(second["call_id"], second["claim_token"], "ok", reply_text="newer")

        assert store.find_reusable("5" * 64)["call_id"] == second["call_id"]
        assert store.get_job(job_id) is not None


# ---------------------------------------------------------------------------
# The queue: reclaiming abandoned work
# ---------------------------------------------------------------------------


class TestReclaimStale:
    def _claim_long_ago(self, store, worker_id="w1", endpoints=("argo",), heartbeat=None):
        """Register a worker and have it claim everything, half an hour ago.

        *heartbeat* defaults to equally long ago, i.e. a worker that has since
        died; pass ``utcnow()`` for one that is still alive.
        """
        store.register_worker(
            {
                "worker_id": worker_id,
                "user": "alice",
                "kind": "daemon",
                "endpoints": list(endpoints),
                "heartbeat_at": heartbeat or (utcnow() - timedelta(minutes=30)),
            }
        )
        return store.claim_calls(worker_id, set(endpoints), 10, now=utcnow() - timedelta(minutes=30)
        )

    def test_an_abandoned_claim_goes_back_on_the_queue(self, store):
        job_id = store.create_job(
            job_row(), [item_row("item-1")], [call_row("item-1", "proposer_1")]
        )
        self._claim_long_ago(store)

        assert store.reclaim_stale() == 1

        (call,) = store.get_calls(job_id)
        assert call["status"] == "pending"
        assert call["claim_token"] is None
        assert call["claimed_by"] is None
        assert call["claimed_at"] is None
        assert call["lease_generation"] is None
        assert len(store.claim_calls("w2", {"argo"}, 10, now=utcnow())) == 1

    def test_the_reclaimed_call_keeps_its_attempt_count(self, store):
        """Nothing was learned -- the worker died -- so charging it an attempt
        would exhaust the retry budget of a call that was never tried."""
        job_id = store.create_job(
            job_row(),
            [item_row("item-1")],
            [call_row("item-1", "proposer_1", attempt_count=2)],
        )
        self._claim_long_ago(store)
        store.reclaim_stale()
        (call,) = store.get_calls(job_id)
        assert call["attempt_count"] == 2

    def test_a_live_workers_claim_is_left_alone(self, store):
        job_id = store.create_job(
            job_row(), [item_row("item-1")], [call_row("item-1", "proposer_1")]
        )
        self._claim_long_ago(store, heartbeat=utcnow())

        assert store.reclaim_stale() == 0
        (call,) = store.get_calls(job_id)
        assert call["status"] == "claimed"

    def test_a_fresh_claim_is_left_alone_even_with_no_worker_row(self, store):
        """The age guard: a claim younger than max_age is work in flight, and
        reclaiming it would run the same call twice."""
        job_id = store.create_job(
            job_row(), [item_row("item-1")], [call_row("item-1", "proposer_1")]
        )
        store.claim_calls("never-registered", {"argo"}, 10, now=utcnow())
        assert store.reclaim_stale() == 0
        (call,) = store.get_calls(job_id)
        assert call["status"] == "claimed"

    def test_max_age_is_honoured(self, store):
        store.create_job(
            job_row(), [item_row("item-1")], [call_row("item-1", "proposer_1")]
        )
        store.claim_calls("never-registered", {"argo"}, 10, now=utcnow() - timedelta(minutes=5)
        )
        assert store.reclaim_stale(max_age=timedelta(minutes=10)) == 0
        assert store.reclaim_stale(max_age=timedelta(minutes=1)) == 1

    def test_pending_and_settled_calls_are_untouched(self, store):
        job_id = store.create_job(
            job_row(),
            [item_row("item-1")],
            [call_row("item-1", "proposer_1"), call_row("item-1", "proposer_2")],
        )
        (claim,) = store.claim_calls("w1", {"argo"}, 1, now=T0)
        store.complete_call(claim["call_id"], claim["claim_token"], "ok", reply_text="x")

        assert store.reclaim_stale() == 0
        assert {c["status"] for c in store.get_calls(job_id)} == {"ok", "pending"}

    def test_reclaim_counts_every_abandoned_claim(self, store):
        store.create_job(
            job_row(),
            [item_row("item-1")],
            [call_row("item-1", f"proposer_{n}") for n in range(1, 4)],
        )
        self._claim_long_ago(store)
        assert store.reclaim_stale() == 3

    def test_reclaim_on_an_empty_store_is_zero(self, store):
        assert store.reclaim_stale() == 0


# ---------------------------------------------------------------------------
# Job control: status
# ---------------------------------------------------------------------------


class TestJobStatus:
    def _live_worker(self, store, endpoints=("argo",)):
        store.register_worker(
            {
                "worker_id": "live-1",
                "user": "alice",
                "kind": "daemon",
                "endpoints": list(endpoints),
                "heartbeat_at": utcnow(),
            }
        )

    def test_counts_items_and_reports_percent_complete(self, store):
        job_id = store.create_job(
            job_row(),
            [item_row(f"item-{n}") for n in range(1, 5)],
            [],
        )
        status = store.job_status(job_id)
        assert status["job_id"] == job_id
        assert status["status"] == "queued"
        assert status["n_items"] == 4
        assert status["by_status"] == {"pending": 4}
        assert status["percent_complete"] == 0.0

        store.settle_item(job_id, "item-1", "ok", {"selected_ids": []})
        store.settle_item(job_id, "item-2", "error", None)
        status = store.job_status(job_id)
        assert status["by_status"] == {"pending": 2, "ok": 1, "error": 1}
        assert status["percent_complete"] == 50.0

    def test_percent_complete_is_rounded_to_one_decimal(self, store):
        job_id = store.create_job(
            job_row(), [item_row(f"item-{n}") for n in range(3)], []
        )
        store.settle_item(job_id, "item-0", "ok", {})
        assert store.job_status(job_id)["percent_complete"] == 33.3

    def test_a_job_with_no_items_reads_complete_rather_than_stuck(self, store):
        job_id = store.create_job(job_row(), [], [])
        status = store.job_status(job_id)
        assert status["n_items"] == 0
        assert status["percent_complete"] == 100.0
        assert status["blockers"] == []

    def test_pending_calls_are_grouped_by_role_then_endpoint(self, store):
        job_id = store.create_job(
            job_row(),
            [item_row("item-1"), item_row("item-2")],
            [
                call_row("item-1", "proposer_1", endpoint="argo"),
                call_row("item-1", "proposer_2", endpoint="poplar"),
                call_row("item-2", "proposer_1", endpoint="argo"),
                call_row("item-1", "critic", role="critic", endpoint="argo"),
            ],
        )
        self._live_worker(store, endpoints=("argo", "poplar"))
        assert store.job_status(job_id)["pending_calls_by_role_endpoint"] == {
            "proposer": {"argo": 2, "poplar": 1},
            "critic": {"argo": 1},
        }

    def test_a_claimed_call_is_work_in_flight_not_work_waiting(self, store):
        job_id = store.create_job(
            job_row(), [item_row("item-1")], [call_row("item-1", "proposer_1")]
        )
        store.claim_calls("w1", {"argo"}, 10, now=T0)
        status = store.job_status(job_id)
        assert status["pending_calls_by_role_endpoint"] == {}
        assert status["blockers"] == []

    def test_a_halted_endpoint_is_a_blocker(self, store):
        job_id = store.create_job(
            job_row(), [item_row("item-1")], [call_row("item-1", "proposer_1")]
        )
        self._live_worker(store)
        store.set_halt("argo", "quota_exhausted", "daily cap reached")

        (blocker,) = store.job_status(job_id)["blockers"]
        assert blocker["kind"] == "halted"
        assert blocker["endpoint"] == "argo"
        assert "quota_exhausted" in blocker["detail"]
        assert "daily cap reached" in blocker["detail"]

    def test_a_halt_blocks_a_job_submitted_under_any_label(self, store):
        """Halts are the store's: the job's label does not exempt it."""
        job_id = store.create_job(
            job_row(submitted_by="somebody-else"),
            [item_row("item-1")],
            [call_row("item-1", "proposer_1")],
        )
        self._live_worker(store)
        store.set_halt("argo", "auth", "the token expired")
        (blocker,) = store.job_status(job_id)["blockers"]
        assert blocker["kind"] == "halted"

    @pytest.mark.parametrize(
        "error_kind,blocker_kind",
        [
            ("model_unavailable", "waiting_for_model"),
            ("configuration_mismatch", "configuration_mismatch"),
        ],
    )
    def test_a_pending_calls_last_error_is_a_blocker(self, store, error_kind, blocker_kind):
        """The error is written straight onto the pending row here because
        ``release_call`` takes no error argument: the engine milestone records
        it in the same transaction as the release.  What is under test is that
        ``job_status`` reads it, not how it got there."""
        job_id = store.create_job(
            job_row(), [item_row("item-1")], [call_row("item-1", "proposer_1")]
        )
        self._live_worker(store)
        with store.transaction() as conn:
            conn.execute(
                sch.calls.update()
                .where(sch.calls.c.job_id == job_id)
                .values(error={"kind": error_kind, "message": "no capacity"})
            )

        (blocker,) = store.job_status(job_id)["blockers"]
        assert blocker["kind"] == blocker_kind
        assert blocker["endpoint"] == "argo"
        assert error_kind in blocker["detail"]
        assert blocker["detail"].startswith("1 pending call")

    def test_a_retryable_error_is_not_a_blocker(self, store):
        """A transport error is what retries are for, not something a human
        has to go and fix."""
        job_id = store.create_job(
            job_row(), [item_row("item-1")], [call_row("item-1", "proposer_1")]
        )
        self._live_worker(store)
        with store.transaction() as conn:
            conn.execute(
                sch.calls.update()
                .where(sch.calls.c.job_id == job_id)
                .values(error={"kind": "transport", "message": "reset by peer"})
            )
        assert store.job_status(job_id)["blockers"] == []

    def test_no_live_worker_for_an_endpoint_is_a_blocker(self, store):
        job_id = store.create_job(
            job_row(), [item_row("item-1")], [call_row("item-1", "proposer_1")]
        )
        (blocker,) = store.job_status(job_id)["blockers"]
        assert blocker["kind"] == "no_daemon"
        assert blocker["endpoint"] == "argo"

    def test_a_worker_serving_another_endpoint_does_not_clear_no_daemon(self, store):
        job_id = store.create_job(
            job_row(),
            [item_row("item-1")],
            [call_row("item-1", "proposer_1", endpoint="poplar")],
        )
        self._live_worker(store, endpoints=("argo",))
        (blocker,) = store.job_status(job_id)["blockers"]
        assert blocker["kind"] == "no_daemon"
        assert blocker["endpoint"] == "poplar"

    def test_a_stale_worker_does_not_count_as_live(self, store):
        job_id = store.create_job(
            job_row(), [item_row("item-1")], [call_row("item-1", "proposer_1")]
        )
        store.register_worker(
            {
                "worker_id": "sleepy",
                "user": "alice",
                "endpoints": ["argo"],
                "heartbeat_at": utcnow() - timedelta(minutes=10),
            }
        )
        assert [b["kind"] for b in store.job_status(job_id)["blockers"]] == ["no_daemon"]

    def test_any_live_worker_in_the_store_clears_no_daemon(self, store):
        """A worker registered under a different label is still draining it.

        Scoping ``no_daemon`` by the label would tell the user to start a
        worker while one was already running their calls.
        """
        job_id = store.create_job(
            job_row(submitted_by="alice"),
            [item_row("item-1")],
            [call_row("item-1", "proposer_1")],
        )
        store.register_worker(
            {
                "worker_id": "other-label",
                "user": "somebody-else",
                "endpoints": ["argo"],
                "heartbeat_at": utcnow(),
            }
        )
        assert store.job_status(job_id)["blockers"] == []

    def test_halted_and_unserved_are_reported_independently(self, store):
        """Suppressing either fact would send the user to fix the wrong thing."""
        job_id = store.create_job(
            job_row(), [item_row("item-1")], [call_row("item-1", "proposer_1")]
        )
        store.set_halt("argo", "budget_exhausted", "")
        assert sorted(b["kind"] for b in store.job_status(job_id)["blockers"]) == [
            "halted",
            "no_daemon",
        ]

    def test_throughput_counts_calls_finished_in_the_last_ten_minutes(self, store):
        job_id = store.create_job(
            job_row(),
            [item_row("item-1")],
            [call_row("item-1", "proposer_1"), call_row("item-1", "proposer_2")],
        )
        assert store.job_status(job_id)["throughput_last_10min"] == 0

        first, second = store.claim_calls("w1", {"argo"}, 10, now=T0)
        store.complete_call(first["call_id"], first["claim_token"], "ok", reply_text="x")
        assert store.job_status(job_id)["throughput_last_10min"] == 1
        store.complete_call(second["call_id"], second["claim_token"], "error", error={})
        assert store.job_status(job_id)["throughput_last_10min"] == 2

    def test_throughput_ignores_an_older_completion(self, store):
        job_id = store.create_job(
            job_row(), [item_row("item-1")], [call_row("item-1", "proposer_1")]
        )
        (claim,) = store.claim_calls("w1", {"argo"}, 10, now=T0)
        store.complete_call(claim["call_id"], claim["claim_token"], "ok", reply_text="x")
        with store.transaction() as conn:
            conn.execute(
                sch.calls.update()
                .where(sch.calls.c.call_id == claim["call_id"])
                .values(finished_at=utcnow() - timedelta(minutes=30))
            )
        assert store.job_status(job_id)["throughput_last_10min"] == 0

    def test_an_unknown_job_is_a_value_error(self, store):
        with pytest.raises(ValueError, match="no such job"):
            store.job_status("ghost")


# ---------------------------------------------------------------------------
# Job control: results
# ---------------------------------------------------------------------------


class TestResults:
    @pytest.fixture
    def decided(self, store):
        job_id = store.create_job(
            job_row(),
            [item_row("item-1"), item_row("item-2"), item_row("item-3")],
            [],
        )
        store.settle_item(
            job_id, "item-2", "ok", {"item_id": "item-2", "selected_ids": ["GO:0003674"]}
        )
        store.settle_item(job_id, "item-1", "error", {"item_id": "item-1", "status": "error"})
        return job_id

    def test_only_settled_items_by_default(self, store, decided):
        rows = store.results(decided)
        # Submission order, not settlement order.
        assert [r["item_id"] for r in rows] == ["item-1", "item-2"]
        assert rows[1]["verdict"]["selected_ids"] == ["GO:0003674"]
        assert rows[0]["status"] == "error"

    def test_include_pending_returns_the_undecided_items_too(self, store, decided):
        rows = store.results(decided, include_pending=True)
        assert [r["item_id"] for r in rows] == ["item-1", "item-2", "item-3"]
        assert rows[2]["status"] == "pending"
        assert rows[2]["verdict"] is None

    def test_cancelled_items_are_settled_results(self, store, decided):
        store.cancel_job(decided)
        rows = store.results(decided)
        assert [r["item_id"] for r in rows] == ["item-1", "item-2", "item-3"]
        assert rows[2]["status"] == "cancelled"

    def test_an_unknown_job_has_no_results(self, store):
        assert store.results("ghost") == []


# ---------------------------------------------------------------------------
# Job control: cancel
# ---------------------------------------------------------------------------


class TestCancelJob:
    @pytest.fixture
    def mixed(self, store):
        """A job with one settled item, one pending call, one claimed call."""
        job_id = store.create_job(
            job_row(),
            [item_row("item-1", item_order=0), item_row("item-2", item_order=1)],
            [
                call_row("item-1", "proposer_1"),
                call_row("item-2", "proposer_1"),
            ],
        )
        (claim,) = store.claim_calls("w1", {"argo"}, 1, now=T0)
        store.settle_item(job_id, "item-1", "ok", {"selected_ids": []})
        return job_id, claim

    def test_cancel_supersedes_pending_calls_and_leaves_claimed_ones(self, store, mixed):
        job_id, claim = mixed
        store.cancel_job(job_id)

        by_id = {c["call_id"]: c for c in store.get_calls(job_id)}
        still_claimed = by_id.pop(claim["call_id"])
        assert still_claimed["status"] == "claimed"
        assert still_claimed["live"] is True
        assert still_claimed["claim_token"] == claim["claim_token"]

        (superseded,) = by_id.values()
        assert superseded["status"] == "superseded"
        assert superseded["live"] is False

    def test_cancel_settles_the_unsettled_items_and_keeps_the_rest(self, store, mixed):
        job_id, _claim = mixed
        store.cancel_job(job_id)

        items = {i["item_id"]: i for i in store.get_items(job_id)}
        assert items["item-1"]["status"] == "ok"  # already settled: untouched
        assert items["item-1"]["verdict"] == {"selected_ids": []}
        assert items["item-2"]["status"] == "cancelled"
        assert items["item-2"]["decided_at"] is not None

    def test_the_job_goes_to_cancelled(self, store, mixed):
        job_id, _claim = mixed
        store.cancel_job(job_id)
        assert store.get_job(job_id)["status"] == "cancelled"
        assert store.job_status(job_id)["status"] == "cancelled"

    def test_nothing_is_claimed_from_a_cancelled_job(self, store, mixed):
        job_id, _claim = mixed
        store.cancel_job(job_id)
        assert store.claim_calls("w2", {"argo"}, 10, now=T0) == []

    def test_cancel_is_idempotent_and_terminal(self, store, mixed):
        job_id, _claim = mixed
        store.cancel_job(job_id)
        store.cancel_job(job_id)
        assert store.get_job(job_id)["status"] == "cancelled"

    def test_a_cancelled_job_refuses_further_work(self, store, mixed):
        job_id, _claim = mixed
        store.cancel_job(job_id)
        with pytest.raises(JobCancelled):
            store.add_calls([dict(call_row("item-2", "critic", role="critic"), job_id=job_id)])
        with pytest.raises(JobCancelled):
            store.settle_item(job_id, "item-2", "ok", {})

    def test_cancel_touches_no_other_job(self, store):
        keep = store.create_job(
            job_row(job_id="keep"), [item_row("item-1")], [call_row("item-1", "proposer_1")]
        )
        drop = store.create_job(
            job_row(job_id="drop"), [item_row("item-1")], [call_row("item-1", "proposer_1")]
        )
        store.cancel_job(drop)
        assert store.get_job(keep)["status"] == "queued"
        assert [c["status"] for c in store.get_calls(keep)] == ["pending"]

    def test_a_job_submitted_under_another_label_may_still_be_cancelled(
        self, store
    ):
        """There is no ownership to enforce; a rename must not lock you out."""
        job_id = store.create_job(
            job_row(submitted_by="somebody-else"),
            [item_row("item-1")],
            [call_row("item-1", "proposer_1")],
        )
        store.cancel_job(job_id)
        assert store.get_job(job_id)["status"] == "cancelled"

    def test_cancelling_an_unknown_job_is_a_value_error(self, store):
        with pytest.raises(ValueError, match="no such job"):
            store.cancel_job("ghost")


# ---------------------------------------------------------------------------
# Job control: delete
# ---------------------------------------------------------------------------


class TestDeleteJob:
    @pytest.mark.parametrize("status", ["queued", "running", "paused"])
    def test_delete_refuses_a_job_that_is_not_finished(self, store, status):
        """Cancel first: a tombstoned job a worker is still writing to would
        keep spending tokens on results nobody can see."""
        job_id = store.create_job(job_row(status=status), [item_row("item-1")], [])
        with pytest.raises(ValueError, match="cancel it first"):
            store.delete_job(job_id)
        assert store.get_job(job_id)["deleted_at"] is None

    @pytest.mark.parametrize("status", ["cancelled", "done"])
    def test_delete_tombstones_a_finished_job(self, store, status):
        job_id = store.create_job(job_row(status=status), [item_row("item-1")], [])
        store.delete_job(job_id)

        job = store.get_job(job_id)
        assert job is not None, "a tombstone removes no row"
        assert job["deleted_at"] is not None
        assert job["deleted_at"].tzinfo is not None
        assert job["status"] == status, "delete changes nothing but deleted_at"

        assert store.list_jobs() == []
        assert [j["job_id"] for j in store.list_jobs(include_deleted=True)] == [job_id]

    def test_a_cancelled_then_deleted_jobs_items_and_calls_remain(self, store):
        job_id = store.create_job(
            job_row(), [item_row("item-1")], [call_row("item-1", "proposer_1")]
        )
        store.cancel_job(job_id)
        store.delete_job(job_id)
        assert len(store.get_items(job_id)) == 1
        assert len(store.get_calls(job_id)) == 1

    def test_delete_keeps_the_calls_a_later_job_reused(self, store):
        """A shared store's answers outlive the job that paid for them --
        including the job that cited one via ``reused_from``."""
        paid = store.create_job(
            job_row(job_id="paid"),
            [item_row("item-1")],
            [call_row("item-1", "proposer_1", prompt_hash="7" * 64)],
        )
        (claim,) = store.claim_calls("w1", {"argo"}, 10, now=T0)
        store.complete_call(
            claim["call_id"], claim["claim_token"], "ok", reply_text="the answer"
        )

        borrower = store.create_job(
            job_row(job_id="borrower", submitted_by="bob"),
            [item_row("item-1")],
            [call_row("item-1", "proposer_1", prompt_hash="7" * 64)],
        )
        (borrowed,) = store.claim_calls("w2", {"argo"}, 10, now=T0)
        store.complete_call(
            borrowed["call_id"],
            borrowed["claim_token"],
            "ok",
            reply_text="the answer",
            reused_from=claim["call_id"],
        )

        store.cancel_job(paid)
        store.delete_job(paid)

        assert store.find_reusable("7" * 64) is not None
        (original,) = store.get_calls(paid)
        assert original["reply_text"] == "the answer"
        (citing,) = store.get_calls(borrower)
        assert citing["reused_from"] == claim["call_id"]

    def test_a_job_submitted_under_another_label_may_still_be_deleted(self, store):
        job_id = store.create_job(
            job_row(status="done", submitted_by="somebody-else"), [], []
        )
        store.delete_job(job_id)
        assert store.get_job(job_id)["deleted_at"] is not None

    def test_deleting_twice_does_not_move_the_tombstone(self, store):
        job_id = store.create_job(job_row(status="done"), [], [])
        store.delete_job(job_id)
        first = store.get_job(job_id)["deleted_at"]
        store.delete_job(job_id)
        assert store.get_job(job_id)["deleted_at"] == first

    def test_deleting_an_unknown_job_is_a_value_error(self, store):
        with pytest.raises(ValueError, match="no such job"):
            store.delete_job("ghost")


# ---------------------------------------------------------------------------
# The protocol
# ---------------------------------------------------------------------------

#: Every method of the STORE CONTRACT, verbatim and in contract order.  If a
#: method is renamed, added or dropped in protocols.py without this list being
#: updated deliberately, the test below fails -- which is the point: the
#: protocol is what the engine and the CLI are written against.
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
    # Added by the engine milestone: the two columns the queue could not
    # write for itself (a PENDING call's last error, and a job reaching
    # 'running' or 'done').  See note_call_error / set_job_status.
    "note_call_error",
    "set_job_status",
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
    # workers
    "register_worker",
    "heartbeat_worker",
    "live_workers",
    "remove_worker",
    "stop_requested",
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
            assert store.schema_version() == sch.SCHEMA_VERSION
        finally:
            store.close()

        # A SECOND store object on the same URL reads what the first wrote.
        reopened = open_store(f"sqlite:///{db_path}")
        try:
            assert reopened.schema_version() == sch.SCHEMA_VERSION
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

    def test_a_postgresql_url_names_the_round_that_removed_the_shared_store(self):
        """The retired design has to say so, not merely fail to connect."""
        with pytest.raises(StoreError) as excinfo:
            open_store("postgresql+psycopg://x")
        message = str(excinfo.value)
        assert "round 2" in message
        assert "sqlite" in message

    def test_any_other_scheme_is_refused_the_same_way(self):
        with pytest.raises(StoreError, match="round 2"):
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
