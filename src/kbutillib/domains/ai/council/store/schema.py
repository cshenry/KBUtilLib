"""Relational schema for the council store -- one :class:`MetaData` for both
dialects.

The store is where jobs, items, call rows and verdicts live, and the call rows
ARE the queue.  Two implementations sit on this one schema: a local SQLite
file (:mod:`.sqlite`) and a shared PostgreSQL database that every KBUtilLib
installation can point at (:mod:`.postgres`).  Because reuse lookup crosses
users -- a question already answered by the same deployment is not asked again
-- the two must agree on column names and types exactly, which is why there is
a single module of table definitions rather than one per dialect.

Dialect portability rules followed here:

* JSON columns are :data:`sqlalchemy.JSON`, never a dialect JSONB, so SQLite
  stores them as TEXT and PostgreSQL as ``json`` without any branch in the
  table definitions.
* Hashes and ids are ``String(64)``: every hash in :mod:`..types` is a SHA-256
  hex digest and every generated id is a ``uuid4().hex``, both 64 and 32
  characters respectively.
* Timestamps are :class:`UtcDateTime` (see its docstring) rather than a bare
  ``DateTime(timezone=True)``, because the SQLite dialect silently discards
  ``tzinfo`` on the way in and returns naive datetimes on the way out.

Every table the council will ever need is defined here, including the ones
only later milestones write to (``local_models``, ``model_resources``,
``model_loads``, ``gpu_lease``).  That is deliberate: creating them now costs
nothing and means those milestones ship without a schema migration.
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
    "daemon_lease",
    "local_models",
    "model_resources",
    "model_loads",
    "gpu_lease",
    "TABLE_NAMES",
]

#: Version of this schema.  Bumped only by a migration; a store file carrying
#: a different number refuses to open (see ``SqliteStore.__init__``).
SCHEMA_VERSION = 1

#: Columns added to this schema AFTER stores had been created at the same
#: SCHEMA_VERSION.  Each is nullable with no default and is written by code
#: that treats its absence as "nothing to say", so adding one cannot change
#: the meaning of an existing row -- which is why it does not need a version
#: bump.  ``SqliteStore.migrate()`` adds any that a store is missing; nothing
#: else in the store layer may use this list to skip a real migration.
ADDITIVE_COLUMNS = (("calls", "normalised"),)


class UtcDateTime(sa.types.TypeDecorator):
    """A timestamp that is timezone-aware UTC on both sides of the wire.

    SQLAlchemy's SQLite ``DATETIME`` formats a datetime field by field and
    simply ignores ``tzinfo``, so an aware timestamp goes in and a naive one
    comes back -- and on this fleet local time is not UTC, so that silently
    shifts the instant by hours.  PostgreSQL's ``timestamptz`` has no such
    problem.  This decorator makes the two behave the same: everything is
    converted to UTC on the way in and re-tagged as UTC on the way out.

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
    # stay reusable for everyone else's future jobs, which is the whole point
    # of a shared store.
    sa.Column("deleted_at", UtcDateTime, nullable=True),
    sa.Column("query_name", _NAME, nullable=False),
    sa.Column("query_version", sa.String(64), nullable=False),
    sa.Column("submitted_by", _NAME, nullable=False),
    sa.Column("submitted_host", _NAME, nullable=True),
    sa.Column("submitted_at", UtcDateTime, nullable=False),
    sa.Column("priority", sa.Integer, nullable=False, default=100),
    sa.Column("status", _ENUM, nullable=False, default="queued"),
    # Seat names, endpoint kinds, served + canonical models and generation
    # params.  NEVER credentials: a shared store is readable by every user.
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
    # not the endpoint alias, so two users asking the same deployment the same
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
    # Reuse lookup: find_reusable(prompt_hash) wants status 'ok' rows.
    sa.Index("ix_calls_prompt_hash_status", "prompt_hash", "status"),
    # search_calls(): "what has this model already said about this entity?"
    sa.Index("ix_calls_query_model_entity", "query_name", "canonical_model", "entity_hash"),
    sa.Index("ix_calls_job_item", "job_id", "item_id"),
    # "One live call per (item, role, seat, upstream)".  The prompt writes the
    # constraint as UNIQUE over six columns including `live`; `live` is the
    # index PREDICATE rather than a key column, which is what a partial unique
    # index WHERE live means -- superseded rows (live false) are exempt and
    # any number of them may pile up behind the one live row.  Both SQLite and
    # PostgreSQL support partial indexes, so this is portable as written.
    sa.Index(
        "ux_calls_one_live",
        "job_id",
        "item_id",
        "role",
        "seat_name",
        "upstream_digest",
        unique=True,
        sqlite_where=sa.text("live"),
        postgresql_where=sa.text("live"),
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
)


# ---------------------------------------------------------------------------
# Per-user endpoint state: budget and halts
# ---------------------------------------------------------------------------

endpoint_state = sa.Table(
    "endpoint_state",
    metadata,
    # Keyed on (user, endpoint) and not on endpoint alone: a shared store
    # holds several users' spend against the same gateway, and one user's
    # exhausted quota must not halt everybody.
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
# Workers and leases
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

daemon_lease = sa.Table(
    "daemon_lease",
    metadata,
    # One daemon per user.  `generation` is monotonic and NEVER reset: a call
    # claimed under generation 3 is refused at completion once generation 4
    # exists, which is how a superseded daemon's late reply is rejected.
    sa.Column("user", _NAME, primary_key=True),
    sa.Column("holder_worker_id", _ID, nullable=True),
    sa.Column("generation", sa.Integer, nullable=False, default=0),
    sa.Column("expires_at", UtcDateTime, nullable=True),
)


# ---------------------------------------------------------------------------
# Local model registry (poplar) -- written by later milestones
# ---------------------------------------------------------------------------

local_models = sa.Table(
    "local_models",
    metadata,
    sa.Column("alias", _NAME, primary_key=True),
    sa.Column("served_name", _NAME, nullable=True),
    sa.Column("canonical_model", _NAME, nullable=True),
    sa.Column("endpoint_url", sa.Text, nullable=True),
    sa.Column("control_url", sa.Text, nullable=True),
    sa.Column("gpu_mem_gib", sa.Float, nullable=True),
    sa.Column("gpu_count", sa.Integer, nullable=True),
    sa.Column("default_gpus", sa.JSON, nullable=True),
    sa.Column("registered_by", _NAME, nullable=True),
    sa.Column("registered_at", UtcDateTime, nullable=True),
    sa.Column("notes", sa.Text, nullable=True),
)

model_resources = sa.Table(
    "model_resources",
    metadata,
    # Operator-measured.  An ABSENT row means "never auto-load this model":
    # the council will not guess how many GPUs something needs on a shared
    # host, so the absence is the safety interlock, not a missing default.
    sa.Column("alias", _NAME, primary_key=True),
    sa.Column("min_gpu_count", sa.Integer, nullable=True),
    sa.Column("per_gpu_peak_gib", sa.Float, nullable=True),
    sa.Column("startup_settings", sa.JSON, nullable=True),
    sa.Column("measured_by", _NAME, nullable=True),
    sa.Column("measured_at", UtcDateTime, nullable=True),
    sa.Column("notes", sa.Text, nullable=True),
)

model_loads = sa.Table(
    "model_loads",
    metadata,
    sa.Column("alias", _NAME, primary_key=True),
    sa.Column("loaded_at", UtcDateTime, primary_key=True),
    sa.Column("gpus", sa.JSON, nullable=True),
    sa.Column("loaded_by_user", _NAME, nullable=True),
    sa.Column("by_council", sa.Boolean, nullable=False, default=False),
    sa.Column("instance_started_at", UtcDateTime, nullable=True),
    sa.Column("instance_pid", sa.Integer, nullable=True),
    sa.Column("last_used_at", UtcDateTime, nullable=True),
    sa.Column("dropped_at", UtcDateTime, nullable=True),
)

gpu_lease = sa.Table(
    "gpu_lease",
    metadata,
    # Fleet-wide GPU residency lease: exactly one row, id 1.  The singleton is
    # modelled as a one-row table rather than a file lock so it lives in the
    # same transaction as the model_loads row it guards.
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("holder_worker_id", _ID, nullable=True),
    sa.Column("generation", sa.Integer, nullable=False, default=0),
    sa.Column("expires_at", UtcDateTime, nullable=True),
)

#: Every table name this schema defines, in creation order.
TABLE_NAMES = tuple(t.name for t in metadata.sorted_tables)
