"""Relational schema for the council store -- one :class:`MetaData`, one
dialect.

The store is where jobs, items, call rows and verdicts live, and the call rows
ARE the queue.  There is one implementation, a local SQLite file
(:mod:`.sqlite`), one per machine; review round 2 removed the shared
PostgreSQL store, so nothing here has to compile on a second dialect any more.

Conventions followed here:

* JSON columns are :data:`sqlalchemy.JSON`, so SQLite stores them as TEXT and
  the table definitions carry no serialisation code.
* Hashes and ids are ``String(64)``: every hash in :mod:`..types` is a SHA-256
  hex digest and every generated id is a ``uuid4().hex``, both 64 and 32
  characters respectively.
* Timestamps are :class:`UtcDateTime` (see its docstring) rather than a bare
  ``DateTime(timezone=True)``, because the SQLite dialect silently discards
  ``tzinfo`` on the way in and returns naive datetimes on the way out.

Schema version 2 is the LOCAL-ONLY schema.  Version 1 additionally carried
five tables for designs that no longer exist -- ``daemon_lease`` (replaced by
the file lock in :mod:`..execlock`), and ``local_models``,
``model_resources``, ``model_loads`` and ``gpu_lease`` (self-hosted model
management, now out of the product).  They are named in
:data:`REMOVED_IN_V2`, which is what ``SqliteStore.migrate()`` drops.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

import sqlalchemy as sa

__all__ = [
    "SCHEMA_VERSION",
    "ADDITIVE_COLUMNS",
    "UtcDateTime",
    "metadata",
    "schema_version",
    "query_types",
    "jobs",
    "items",
    "calls",
    "attempts",
    "endpoint_state",
    "workers",
    "REMOVED_IN_V2",
    "TABLE_NAMES",
]

#: Version of this schema.  Bumped only by a migration; a store file carrying
#: a different number refuses to open (see ``SqliteStore.__init__``).
SCHEMA_VERSION = 2

#: Tables version 1 defined and version 2 does not.  ``SqliteStore.migrate()``
#: issues one ``DROP TABLE IF EXISTS`` per name, in one transaction, and
#: touches nothing else -- every job, item, call, attempt, endpoint_state,
#: workers and query_types row survives a v1 -> v2 migration intact.
REMOVED_IN_V2 = (
    "daemon_lease",
    "local_models",
    "model_resources",
    "model_loads",
    "gpu_lease",
)

#: Columns added to this schema AFTER stores had been created at the same
#: SCHEMA_VERSION.  Each is nullable with no default and is written by code
#: that treats its absence as "nothing to say", so adding one cannot change
#: the meaning of an existing row -- which is why it does not need a version
#: bump.  ``SqliteStore.migrate()`` adds any that a store is missing; nothing
#: else in the store layer may use this list to skip a real migration.
ADDITIVE_COLUMNS = (("calls", "normalised"), ("attempts", "queued_at"))


class UtcDateTime(sa.types.TypeDecorator):
    """A timestamp that is timezone-aware UTC on both sides of the wire.

    SQLAlchemy's SQLite ``DATETIME`` formats a datetime field by field and
    simply ignores ``tzinfo``, so an aware timestamp goes in and a naive one
    comes back -- and on this fleet local time is not UTC, so that silently
    shifts the instant by hours.  This decorator closes that hole: everything
    is converted to UTC on the way in and re-tagged as UTC on the way out.

    A naive datetime is REJECTED rather than assumed to be UTC.  Assuming
    would turn a caller's ``datetime.now()`` into a wrong instant with no
    symptom; raising turns it into an immediate, obvious error.
    """

    impl = sa.DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(
        self, value: Optional[datetime], dialect: Any
    ) -> Optional[datetime]:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"expected a datetime, got {type(value).__name__}: {value!r}")
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError(
                f"naive datetime {value!r} refused: council timestamps must be "
                "timezone-aware UTC (use datetime.now(timezone.utc))"
            )
        return value.astimezone(timezone.utc)

    def process_result_value(
        self, value: Optional[datetime], dialect: Any
    ) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


metadata = sa.MetaData()

# Column type aliases.  SHA-256 hex is 64 chars and uuid4().hex is 32, so one
# String(64) covers both; names (users, endpoints, aliases) get 255.
_HASH = sa.String(64)
_ID = sa.String(64)
_NAME = sa.String(255)
_ENUM = sa.String(32)


# ---------------------------------------------------------------------------
# Schema bookkeeping
# ---------------------------------------------------------------------------

schema_version = sa.Table(
    "schema_version",
    metadata,
    # Single row.  The version is the primary key so a second, conflicting row
    # cannot be inserted by accident; migrations replace it rather than add.
    sa.Column("version", sa.Integer, primary_key=True, nullable=False),
)


# ---------------------------------------------------------------------------
# Query types
# ---------------------------------------------------------------------------

query_types = sa.Table(
    "query_types",
    metadata,
    # (name, version) is immutable once written: a saved query type is what a
    # finished job's verdicts were produced against, so editing one in place
    # would retro-actively change the meaning of stored results.
    sa.Column("name", _NAME, primary_key=True, nullable=False),
    sa.Column("version", sa.String(64), primary_key=True, nullable=False),
    sa.Column("status", _ENUM, nullable=False, default="active"),
    sa.Column("spec", sa.JSON, nullable=False),
    sa.Column("source", _ENUM, nullable=False, default="user"),
    sa.Column("created_by", _NAME, nullable=True),
    sa.Column("created_at", UtcDateTime, nullable=True),
)


# ---------------------------------------------------------------------------
# Jobs and items
# ---------------------------------------------------------------------------

jobs = sa.Table(
    "jobs",
    metadata,
    sa.Column("job_id", _ID, primary_key=True, nullable=False),
    sa.Column("label", sa.Text, nullable=True),
    # Tombstone.  delete_job() sets this and removes nothing: the job's calls
    # stay reusable by this machine's future jobs, which is the point of
    # keeping a deleted job's answers at all.
    sa.Column("deleted_at", UtcDateTime, nullable=True),
    sa.Column("query_name", _NAME, nullable=False),
    sa.Column("query_version", sa.String(64), nullable=False),
    sa.Column("submitted_by", _NAME, nullable=False),
    sa.Column("submitted_host", _NAME, nullable=True),
    sa.Column("submitted_at", UtcDateTime, nullable=False),
    sa.Column("priority", sa.Integer, nullable=False, default=100),
    sa.Column("status", _ENUM, nullable=False, default="queued"),
    # Seat names, endpoint kinds, served + canonical models and generation
    # params.  NEVER credentials: a store is a plain file a user may hand to
    # somebody else to explain a verdict.
    sa.Column("roster", sa.JSON, nullable=True),
    sa.Column("roster_fingerprint", _HASH, nullable=True),
    sa.Column("profile_name", _NAME, nullable=True),
    sa.Column("options", sa.JSON, nullable=True),
    sa.Column("parent_job_id", _ID, nullable=True),
    sa.Column("n_items", sa.Integer, nullable=False, default=0),
    sa.Index("ix_jobs_user_status", "submitted_by", "status"),
    sa.Index("ix_jobs_priority", "priority", "submitted_at"),
)

items = sa.Table(
    "items",
    metadata,
    sa.Column("job_id", _ID, sa.ForeignKey("jobs.job_id"), primary_key=True),
    sa.Column("item_id", _NAME, primary_key=True),
    sa.Column("entity_hash", _HASH, nullable=True),
    sa.Column("data_hash", _HASH, nullable=True),
    sa.Column("evidence", sa.JSON, nullable=True),
    sa.Column("menu", sa.JSON, nullable=True),
    sa.Column("item_order", sa.Integer, nullable=False, default=0),
    sa.Column("status", _ENUM, nullable=False, default="pending"),
    sa.Column("verdict", sa.JSON, nullable=True),
    sa.Column("decided_at", UtcDateTime, nullable=True),
    sa.Index("ix_items_job_status", "job_id", "status"),
    sa.Index("ix_items_order", "job_id", "item_order"),
)


# ---------------------------------------------------------------------------
# Calls -- the queue
# ---------------------------------------------------------------------------

calls = sa.Table(
    "calls",
    metadata,
    sa.Column("call_id", _ID, primary_key=True, nullable=False),
    sa.Column("job_id", _ID, nullable=False),
    sa.Column("item_id", _NAME, nullable=False),
    sa.Column("role", _ENUM, nullable=False),
    sa.Column("seat_name", _NAME, nullable=False),
    sa.Column("endpoint", _NAME, nullable=False),
    sa.Column("endpoint_kind", _ENUM, nullable=False),
    sa.Column("served_model", _NAME, nullable=True),
    sa.Column("canonical_model", _NAME, nullable=True),
    sa.Column("route_fingerprint", _HASH, nullable=True),
    sa.Column("query_name", _NAME, nullable=True),
    sa.Column("query_version", sa.String(64), nullable=True),
    sa.Column("entity_hash", _HASH, nullable=True),
    sa.Column("data_hash", _HASH, nullable=True),
    # The reuse key.  Built from the reuse namespace and the canonical model,
    # not the endpoint alias, so two jobs asking the same deployment the same
    # question share one answer.
    sa.Column("prompt_hash", _HASH, nullable=True),
    sa.Column("upstream_digest", sa.String(64), nullable=False, default=""),
    sa.Column("status", _ENUM, nullable=False, default="pending"),
    sa.Column("not_before", UtcDateTime, nullable=True),
    sa.Column("claim_token", _ID, nullable=True),
    sa.Column("claimed_by", _ID, nullable=True),
    sa.Column("claimed_at", UtcDateTime, nullable=True),
    sa.Column("lease_generation", sa.Integer, nullable=True),
    sa.Column("attempt_count", sa.Integer, nullable=False, default=0),
    sa.Column("reply_text", sa.Text, nullable=True),
    # The ORIGINAL schema-valid reply, before postprocess or menu filtering.
    # Storing the post-processed shape instead would make the row useless to a
    # later query-type version that maps the same answer differently.
    sa.Column("reply_parsed", sa.JSON, nullable=True),
    # What ``engine/normalise.py`` repaired in that reply before the answer
    # schema saw it: ``[{"field", "rule", "original", "value"}, ...]``, or
    # NULL when the model answered in the shape its query type asked for.
    # ``reply_text`` above is the untouched original either way, so the two
    # columns together hold both what the model said and what was read.
    sa.Column("normalised", sa.JSON, nullable=True),
    sa.Column("fresh_usage", sa.JSON, nullable=True),
    sa.Column("error", sa.JSON, nullable=True),
    sa.Column("reused_from", _ID, nullable=True),
    sa.Column("submitted_by", _NAME, nullable=False),
    sa.Column("created_at", UtcDateTime, nullable=False),
    sa.Column("finished_at", UtcDateTime, nullable=True),
    sa.Column("live", sa.Boolean, nullable=False, default=True),
    sa.ForeignKeyConstraint(
        ["job_id", "item_id"], ["items.job_id", "items.item_id"], name="fk_calls_item"
    ),
    # Claim scan: "pending calls that are due".
    sa.Index("ix_calls_status_not_before", "status", "not_before"),
    # Partitioned claim scan: "this seat's pending calls that are due".  Left
    # alongside the unpartitioned index above rather than replacing it: a
    # worker with no seats declared still scans by (status, not_before) alone.
    sa.Index("ix_calls_status_seat_not_before", "status", "seat_name", "not_before"),
    # Reuse lookup: find_reusable(prompt_hash) wants status 'ok' rows.
    sa.Index("ix_calls_prompt_hash_status", "prompt_hash", "status"),
    # search_calls(): "what has this model already said about this entity?"
    sa.Index("ix_calls_query_model_entity", "query_name", "canonical_model", "entity_hash"),
    sa.Index("ix_calls_job_item", "job_id", "item_id"),
    # "One live call per (item, role, seat, upstream)".  The prompt writes the
    # constraint as UNIQUE over six columns including `live`; `live` is the
    # index PREDICATE rather than a key column, which is what a partial unique
    # index WHERE live means -- superseded rows (live false) are exempt and
    # any number of them may pile up behind the one live row.
    sa.Index(
        "ux_calls_one_live",
        "job_id",
        "item_id",
        "role",
        "seat_name",
        "upstream_digest",
        unique=True,
        sqlite_where=sa.text("live"),
    ),
)

attempts = sa.Table(
    "attempts",
    metadata,
    # One row per try at a call.  Deliberately narrow: prompt text never
    # enters this table (see SqliteStore.record_attempt, which refuses it).
    sa.Column("call_id", _ID, sa.ForeignKey("calls.call_id"), primary_key=True),
    sa.Column("attempt_no", sa.Integer, primary_key=True),
    sa.Column("started_at", UtcDateTime, nullable=True),
    sa.Column("finished_at", UtcDateTime, nullable=True),
    sa.Column("outcome_kind", _ENUM, nullable=True),
    sa.Column("usage", sa.JSON, nullable=True),
    sa.Column("worker_id", _ID, nullable=True),
    # When the call entered the slot queue, written by the same code that
    # writes ``started_at`` once a slot is acquired.  Nullable with no
    # default (see ADDITIVE_COLUMNS): an existing row's absence of this
    # column means "nothing to say", not "zero wait", so adding it changes
    # the meaning of no row and needs no SCHEMA_VERSION bump.  Service time
    # is ``finished_at - started_at``; queue wait is ``started_at -
    # queued_at``.
    sa.Column("queued_at", UtcDateTime, nullable=True),
)


# ---------------------------------------------------------------------------
# Endpoint state: budget and halts
# ---------------------------------------------------------------------------

endpoint_state = sa.Table(
    "endpoint_state",
    metadata,
    # The `user` column is the store-local IDENTITY, always the literal
    # "local" (see sqlite.STORE_IDENTITY) -- not the provenance label on a
    # job.  A store is one machine's, so its spend against a gateway is one
    # number; keying it on the provenance label instead would reset a budget
    # the moment somebody edited config `user`.  The column itself stays
    # because v1 stores have rows in it and the migration re-keys them.
    sa.Column("user", _NAME, primary_key=True),
    sa.Column("endpoint", _NAME, primary_key=True),
    sa.Column("budget_epoch", sa.Integer, nullable=False, default=0),
    sa.Column("epoch_tokens", sa.Integer, nullable=False, default=0),
    sa.Column("lifetime_tokens", sa.Integer, nullable=False, default=0),
    sa.Column("halt_kind", _ENUM, nullable=True),
    sa.Column("halt_message", sa.Text, nullable=True),
    sa.Column("updated_at", UtcDateTime, nullable=True),
)


# ---------------------------------------------------------------------------
# Workers
# ---------------------------------------------------------------------------

workers = sa.Table(
    "workers",
    metadata,
    sa.Column("worker_id", _ID, primary_key=True),
    sa.Column("user", _NAME, nullable=False),
    sa.Column("kind", _ENUM, nullable=False, default="foreground"),
    sa.Column("host", _NAME, nullable=True),
    sa.Column("pid", sa.Integer, nullable=True),
    sa.Column("started_at", UtcDateTime, nullable=True),
    sa.Column("heartbeat_at", UtcDateTime, nullable=True),
    sa.Column("version", sa.String(64), nullable=True),
    sa.Column("endpoints", sa.JSON, nullable=True),
    sa.Column("stop_requested_at", UtcDateTime, nullable=True),
    sa.Index("ix_workers_user_heartbeat", "user", "heartbeat_at"),
)

#: Every table name this schema defines, in creation order.
TABLE_NAMES = tuple(t.name for t in metadata.sorted_tables)
