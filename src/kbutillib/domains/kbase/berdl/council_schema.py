"""Table configs for the council deliberation archive, ``kbaseincubator.council``.

This is a STRUCTURAL MIRROR of
:mod:`kbutillib.domains.kbase.berdl.clearinghouse_schema`: the Apache Iceberg
tables of the ``kbaseincubator`` tenant's ``council`` namespace declared as
pure, pod-free config dicts shaped for
:meth:`~kbutillib.domains.kbase.berdl.capability.BerdlCapability.load`
(``tables=table_configs()``). Every convention of that module is followed
here deliberately: each table's column list is declared ONCE as a
module-level tuple of ``(name, sql_type)`` pairs, everything else
(:func:`table_columns`, :func:`column_names`, :func:`table_configs`, the
``schema_sql`` DDL) is DERIVED from those tuples, no config value anywhere
contains a parenthesis (the guard against an Iceberg partition-transform
expression leaking into a config), the DDL path emits no ``NOT NULL``, and
the hash-encoding seam at the write boundary is
:func:`encode_entity_hash` / :func:`decode_entity_hash`.

WHAT THIS ARCHIVES, AND WHY. The council store is a SQLite file defined by
SQLAlchemy ``MetaData`` in
:mod:`kbutillib.domains.ai.council.store.schema` at ``SCHEMA_VERSION = 2``,
with eight tables: ``schema_version``, ``query_types``, ``jobs``, ``items``,
``calls``, ``attempts``, ``endpoint_state``, ``workers``. Nothing in it is
GO-specific -- ``query_name``/``query_version`` is the only council-kind
discriminator. A store is EPHEMERAL (the production one lived on
``/scratch``), and the deliberation it holds -- every proposer ballot with
its free-text "why", every critic audit, every meta adjudication, the full
candidate menu each seat was shown -- is not recoverable without re-running
and re-paying. Archiving it into a durable Iceberg namespace makes the whole
record queryable SQL, while ``clearinghouse.function_result`` keeps only the
high-level assertion plus a reference to the winning council item (see
:mod:`kbutillib.domains.genome.ontology_mapping.verdicts`).

NINE ARCHIVED TABLES, one that does not publish. Seven mirror project store
tables; :data:`JOB_DISPOSITION_TABLE` and :data:`ROLE_BRIEF_TABLE` are new
-- their rows are written by later tasks, but their DDL belongs with the
rest of the namespace, so it is declared here. ``schema_version`` is
deliberately NOT archived (see :data:`NOT_ARCHIVED`): the namespace carries
its own version, and a per-store SQLite schema number has no meaning once
rows from many stores and many snapshots sit in one Iceberg table.

IDENTITY COLUMNS (:data:`_IDENTITY_COLUMNS`) lead every archived table, in
DDL order, and the primary key of every table is
``("store_id", "snapshot_id") + <the source table's own primary key>``
(:func:`table_primary_key`):

- ``store_id`` -- a DECLARED, durable id for the originating store. It is
  NEVER derived from a filesystem path, a hostname, or a uuid: a store that
  moves off ``/scratch`` must keep its id, because every already-archived
  row is keyed on it and would otherwise become unjoinable, and the
  clearinghouse payload that carries this id is frozen on first write (the
  clearinghouse slot key excludes ``result_type_version``, so a corrected
  payload in the same slot is silently dropped as already-present). Nothing
  in this module invents one; declaring and enforcing it is the store's job
  (``ai.council.store`` identity).
- ``snapshot_id`` -- the VERIFIED UNCOMPRESSED DIGEST of the snapshot the
  row came from, not a label, a date or a filename. The digest is what makes
  "which bytes did this row come from" answerable years later.
- ``snapshot_seq`` -- an EXPLICIT monotonic sequence per store. Snapshot
  ordering cannot be recovered from ``snapshot_id`` (a digest has no order)
  and must not be inferred from ``loaded_at`` (replaying an older snapshot
  after a newer one is legitimate), so the sequence is stated, not derived.
- ``store_host``, ``loaded_at``, ``load_batch_id`` -- where the snapshot was
  taken, when these rows landed, and which load wrote them.

TWO DELIBERATE DENORMALISATIONS, not one. Both are explicit and both are
commented at their declaration site:

1. ``items`` has NO ``query_name`` column in the store
   (``store/schema.py`` ``items``), so the archived table derives it from
   ``jobs`` via ``(store_id, job_id)``. Without it the one table that most
   needs partition pruning could not be partitioned at all.
2. ``attempts`` takes ``query_name`` from its OWNING CALL, with
   ``jobs.query_name`` as the fallback for a legacy NULL call value. A
   CONTRADICTORY non-NULL name is REJECTED, not silently preferred --
   see :func:`attempt_query_name` and
   :class:`ContradictoryQueryNameError`. Silently preferring one side would
   bake a wrong council-kind discriminator into an append-only table.

PARTITIONING: ``query_name`` on ``calls``, ``items`` and ``attempts`` ONLY.
The other six are low cardinality and carry no ``partition_by`` key at all
(its absence means "unpartitioned", exactly as in ``clearinghouse_schema``).
``calls`` is the table that justifies the choice: the measured production
run is 82,270 live calls for one 19,276-item job, about 4.3 calls per item,
so 10M functions is on the order of 43M rows -- and repartitioning a live
Iceberg table later is a full rewrite, not a metadata change. As in
``clearinghouse_schema``, Iceberg partition TRANSFORMS (``bucket(...)``,
``truncate(...)``, ``days(...)``) are not expressible through this write
path -- ``partition_by`` is passed straight to PySpark's
``partitionedBy(*cols)`` as bare strings -- so only bare stored column names
may ever be emitted here.

MEASURED FACTS THIS SCHEMA RESPECTS.

- ``live`` IS ARCHIVED AS A COLUMN AND NEVER FILTERED. The snapshot holds
  203,821 calls of which 88,465 are live and 82,270 are the production run's
  live calls; ``calls`` carries a partial unique index ``ux_calls_one_live``
  over ``(job_id, item_id, role, seat_name, upstream_digest) WHERE live``, so
  superseded rows pile up behind the one live row. Dropping them at archive
  time would destroy the record of what was superseded and why.
- ``find_reusable`` (``SqliteCouncilStore.find_reusable``, which calls
  ``store/queries.py`` ``select_reusable``) accepts ``status='ok'`` across
  ALL jobs and does NOT filter ``live``. Any filtering of superseded answers
  therefore belongs wherever a reuse cache is built -- not there, and not
  here.
- ``items.entity_hash`` is the COUNCIL's OWN hash and matches the
  clearinghouse ``entity_hash`` 0 of 19,268 times. It is RENAMED
  ``council_entity_hash`` on archive precisely so no reader can join on it
  by habit (see :data:`COUNCIL_ENTITY_HASH_COLUMN`).
- ``endpoint_state`` is keyed ``(user, endpoint)`` with MUTABLE CUMULATIVE
  counters, shared by every job in a store, so it changes between any two
  snapshots of the same store. Its archived rows are POINT-IN-TIME
  OBSERVATIONS of a running total and are NEVER additive across snapshots --
  summing ``lifetime_tokens`` over snapshots multiplies the same spend.
- A REUSED call carries a FULL COPY of ``reply_text`` with ``fresh_usage``
  zeroed and ``attempts.outcome_kind = 'reused'``. So row counts over
  ``calls`` are not a cost measure (sum ``fresh_usage`` instead), and
  deduplicating ``calls`` by ``reply_text`` would corrupt the record.
- All six ``workers.version`` values in the 2026-10-23 snapshot are NULL.
  The table publishes anyway and PRESERVES the NULLs; inventing a version
  would be worse than recording that it was not captured.

THE HASH SEAM, AND WHICH COLUMNS IT COVERS. Every hash in the council store
is a SHA-256 hex digest in a ``String(64)`` column, which is the same stored
form ``clearinghouse_schema`` settled on, so this module RE-EXPORTS that
module's :func:`encode_entity_hash` / :func:`decode_entity_hash` rather than
growing a second copy that could drift from it. One normalisation rule, one
implementation: an uppercase digest bound against a lowercase stored value
matches no row, and that failure mode is identical in both namespaces.
The seam covers the sha256-hex columns -- ``items.council_entity_hash``,
``items.data_hash``, ``calls.entity_hash``, ``calls.data_hash``,
``calls.prompt_hash``, ``jobs.roster_fingerprint``,
``calls.route_fingerprint`` -- and NOT ``calls.upstream_digest``, whose
store-side default is the EMPTY STRING and which is therefore not a digest
at all in most rows; passing it through the seam would raise on the common
case.

Like ``clearinghouse_schema``, this module performs no I/O, imports no
``pyspark``, and creates nothing itself; :func:`bootstrap` takes an injected
capability. Nothing here has been verified against a live BERDL pod -- see
KBUtilLib#91 and the task work-record.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .clearinghouse_schema import (
    BootstrapIndeterminateStateError,
    BootstrapPartitionSpecMismatchError,
    bootstrap as _clearinghouse_bootstrap,
    decode_entity_hash,
    encode_entity_hash,
)

__all__ = [
    "TENANT",
    "NAMESPACE",
    "COUNCIL_NAMESPACE",
    "ARCHIVED_TABLES",
    "NOT_ARCHIVED",
    "JOB_DISPOSITION_TABLE",
    "ROLE_BRIEF_TABLE",
    "JOB_DISPOSITIONS",
    "ROLE_BRIEF_UNEXTRACTABLE",
    "COUNCIL_ENTITY_HASH_COLUMN",
    "IDENTITY_COLUMN_NAMES",
    "PARTITION_COLUMN",
    "ContradictoryQueryNameError",
    "BootstrapIndeterminateStateError",
    "BootstrapPartitionSpecMismatchError",
    "table_name",
    "table_columns",
    "column_names",
    "table_primary_key",
    "table_configs",
    "item_query_name",
    "attempt_query_name",
    "encode_entity_hash",
    "decode_entity_hash",
    "bootstrap",
]

#: The BERDL tenant this namespace lives under -- the same tenant as the
#: clearinghouse, so the two are joinable in one catalog.
TENANT = "kbaseincubator"

#: The Iceberg namespace within the tenant.
NAMESPACE = "council"

#: The fully-qualified WRITE-TARGET namespace of the council archive,
#: ``kbaseincubator.council``. Named here (rather than concatenated at a
#: call site) for the same reason
#: :data:`~kbutillib.domains.kbase.berdl.clearinghouse_schema.CLEARINGHOUSE_NAMESPACE`
#: is: nothing in the archive path should have to spell it out.
COUNCIL_NAMESPACE = f"{TENANT}.{NAMESPACE}"

#: Name of the NEW per-job disposition table. New in this namespace: the
#: store records a job's ``status`` (queued/done/cancelled/paused) but not
#: what the project DECIDED the job's output was FOR, which is the question
#: a later reader of 21 jobs actually has.
JOB_DISPOSITION_TABLE = "job_disposition"

#: Name of the NEW per-role mission brief table -- the project's answer to
#: "what was proposer_2 actually told to do".
#:
#: It is DERIVED, never authoritative: ``query_types.spec`` remains the
#: source of truth and is archived in full. This table exists so that no
#: downstream reader has to parse spec JSON to read a role's mission text.
#: A spec shape the extractor cannot read must be recorded as
#: :data:`ROLE_BRIEF_UNEXTRACTABLE` rather than guessed at -- a guessed
#: mission text is indistinguishable from a real one and would misattribute
#: instructions to a model that never received them.
ROLE_BRIEF_TABLE = "role_brief"

#: The disposition values :data:`JOB_DISPOSITION_TABLE` recognises.
#: Declared here so a writer and a reader share one list; this module's DDL
#: emits no CHECK constraint (the DDL path emits no constraints at all), so
#: the enum is enforced in Python at the write boundary, exactly as
#: ``clearinghouse_schema`` enforces hash width at the boundary and not in
#: the DDL.
JOB_DISPOSITIONS = ("published", "pilot", "trial", "cancelled", "superseded")

#: The sentinel ``mission_text`` value meaning "this spec shape could not be
#: extracted from". See :data:`ROLE_BRIEF_TABLE`.
ROLE_BRIEF_UNEXTRACTABLE = "<unextractable>"

#: The archived name of the council's own per-item hash. The store column is
#: ``items.entity_hash``; it is RENAMED here because it is NOT the
#: clearinghouse ``entity_hash`` -- the two matched 0 of 19,268 times in the
#: production run -- and a bare ``entity_hash`` column sitting in a BERDL
#: namespace is an invitation to join on it.
COUNCIL_ENTITY_HASH_COLUMN = "council_entity_hash"

#: The single bare, stored column the three high-volume tables partition on.
#: A module constant rather than a literal at three call sites, so the
#: config generator, the tests and any later archiver read one definition.
PARTITION_COLUMN = "query_name"

#: Store tables that deliberately do NOT publish into this namespace.
#: ``schema_version`` is a per-store SQLite schema number: the Iceberg
#: namespace carries its own version, and once rows from several stores and
#: several snapshots share one table, a column holding "2" says nothing.
NOT_ARCHIVED = ("schema_version",)


# --------------------------------------------------------------------------
# Identity: the six columns every archived table leads with
# --------------------------------------------------------------------------

#: Column declarations prefixed to EVERY archived table, in DDL order.
#: Declared once; :func:`table_columns` composes them onto each table's
#: source-column tuple, and :func:`table_primary_key` takes its
#: ``(store_id, snapshot_id)`` prefix from the first two names here, so the
#: identity block lives in exactly one place.
#:
#: ``snapshot_seq`` is ``BIGINT`` rather than ``INT``: it is a per-store
#: monotonic counter with no upper bound by design.
_IDENTITY_COLUMNS: tuple[tuple[str, str], ...] = (
    ("store_id", "STRING"),
    ("snapshot_id", "STRING"),
    ("snapshot_seq", "BIGINT"),
    ("store_host", "STRING"),
    ("loaded_at", "TIMESTAMP"),
    ("load_batch_id", "STRING"),
)

#: Just the names of :data:`_IDENTITY_COLUMNS`, for callers that only need
#: the name set (the archiver's column check, the tests).
IDENTITY_COLUMN_NAMES: tuple[str, ...] = tuple(
    name for name, _sql_type in _IDENTITY_COLUMNS
)

#: The two identity columns every primary key leads with, in order.
_PK_IDENTITY_PREFIX: tuple[str, ...] = IDENTITY_COLUMN_NAMES[:2]


# --------------------------------------------------------------------------
# Per-table SOURCE columns -- the single source of truth
# --------------------------------------------------------------------------
#
# One tuple per archived table, holding the columns BEYOND the shared
# identity block, in DDL order. Everything else in this module derives from
# these nine tuples; nothing repeats a column list.
#
# Store JSON columns (``sa.JSON``, which SQLite stores as TEXT) are declared
# STRING here, the same way ``clearinghouse_schema`` declares its ``payload``
# STRING: a JSON document archived as text round-trips exactly, and Iceberg
# struct typing would have to re-declare every ballot shape the council has
# ever emitted.

#: ``query_types`` -- ALL store columns. ``spec`` stays the authoritative
#: source of truth for what each role was told to do;
#: :data:`ROLE_BRIEF_TABLE` is a derived convenience over it, never a
#: replacement.
_QUERY_TYPES_COLUMNS: tuple[tuple[str, str], ...] = (
    ("name", "STRING"),
    ("version", "STRING"),
    ("status", "STRING"),
    ("spec", "STRING"),
    ("source", "STRING"),
    ("created_by", "STRING"),
    ("created_at", "TIMESTAMP"),
)

#: ``jobs`` -- all store columns EXCEPT ``priority``. ``priority`` is a
#: QUEUE-SCHEDULING knob: it orders pending work on one machine at one
#: moment (``ix_jobs_priority``) and says nothing about what a finished job
#: decided. Archiving it would preserve a scheduler's transient state as
#: though it were part of the record.
_JOBS_COLUMNS: tuple[tuple[str, str], ...] = (
    ("job_id", "STRING"),
    ("label", "STRING"),
    # Tombstone: delete_job() sets this and removes nothing, so a deleted
    # job's answers stay in the record. Archived for the same reason.
    ("deleted_at", "TIMESTAMP"),
    ("query_name", "STRING"),
    ("query_version", "STRING"),
    ("submitted_by", "STRING"),
    ("submitted_host", "STRING"),
    ("submitted_at", "TIMESTAMP"),
    ("status", "STRING"),
    # Seat names, endpoint kinds, served + canonical models and generation
    # params -- never credentials (the store layer refuses them).
    ("roster", "STRING"),
    ("roster_fingerprint", "STRING"),
    ("profile_name", "STRING"),
    ("options", "STRING"),
    ("parent_job_id", "STRING"),
    ("n_items", "INT"),
)

#: ``items`` -- all store columns EXCEPT ``item_order``, with
#: ``entity_hash`` renamed and a denormalised ``query_name`` added.
#:
#: ``item_order`` is dropped for the same reason as ``jobs.priority``: it is
#: the within-job presentation/claim order (``ix_items_order``), not part of
#: what was decided.
#:
#: DELIBERATE DENORMALISATION 1 of 2 -- ``query_name``. The store's
#: ``items`` table has NO such column; it is derived from ``jobs`` via
#: ``(store_id, job_id)`` (see :func:`item_query_name`). This is not an
#: accident of convenience: ``query_name`` is the ONLY council-kind
#: discriminator in the whole store, and without it on the row this table
#: could not be partitioned on the one axis worth pruning.
#:
#: ``council_entity_hash`` is the RENAME of ``items.entity_hash`` -- see
#: :data:`COUNCIL_ENTITY_HASH_COLUMN`. There is deliberately no bare
#: ``entity_hash`` column on this table.
_ITEMS_COLUMNS: tuple[tuple[str, str], ...] = (
    ("job_id", "STRING"),
    ("item_id", "STRING"),
    (COUNCIL_ENTITY_HASH_COLUMN, "STRING"),
    ("data_hash", "STRING"),
    ("evidence", "STRING"),
    ("menu", "STRING"),
    ("status", "STRING"),
    ("verdict", "STRING"),
    ("decided_at", "TIMESTAMP"),
    # Denormalised from jobs -- see this tuple's note above.
    ("query_name", "STRING"),
)

#: ``calls`` -- all store columns EXCEPT the five lease/queue columns
#: ``not_before``, ``claim_token``, ``claimed_by``, ``claimed_at`` and
#: ``lease_generation``.
#:
#: Those five are the QUEUE MECHANISM, not the record: they say which worker
#: held a lease on a pending call at some instant, and every one of them is
#: meaningless (and mostly NULL) once the call has finished. The deliberation
#: is in ``reply_text`` / ``reply_parsed`` / ``normalised`` / ``error``, all
#: of which are kept.
#:
#: ``live`` IS KEPT and is never filtered at archive time -- see the module
#: docstring. ``entity_hash`` here is the council's own hash exactly as
#: ``items.entity_hash`` is; it keeps the store's name because the archived
#: ``calls`` row is the call's own record and the project's naming decision
#: was to rename only on ``items``, where the join temptation is. It must not
#: be joined to a clearinghouse ``entity_hash`` either.
_CALLS_COLUMNS: tuple[tuple[str, str], ...] = (
    ("call_id", "STRING"),
    ("job_id", "STRING"),
    ("item_id", "STRING"),
    ("role", "STRING"),
    ("seat_name", "STRING"),
    ("endpoint", "STRING"),
    ("endpoint_kind", "STRING"),
    ("served_model", "STRING"),
    ("canonical_model", "STRING"),
    ("route_fingerprint", "STRING"),
    ("query_name", "STRING"),
    ("query_version", "STRING"),
    ("entity_hash", "STRING"),
    ("data_hash", "STRING"),
    # The reuse key, built from the reuse namespace and the canonical model
    # rather than the endpoint alias.
    ("prompt_hash", "STRING"),
    # NOT a digest in most rows: the store default is the empty string, so
    # this column never goes through encode_entity_hash().
    ("upstream_digest", "STRING"),
    ("status", "STRING"),
    ("attempt_count", "INT"),
    ("reply_text", "STRING"),
    # The ORIGINAL schema-valid reply, before postprocess or menu filtering.
    ("reply_parsed", "STRING"),
    # What engine/normalise.py repaired in that reply before the answer
    # schema saw it; NULL when the model answered in the asked-for shape.
    ("normalised", "STRING"),
    # ZEROED on a reused call, which is why row counts over this table are
    # not a cost measure -- sum this instead.
    ("fresh_usage", "STRING"),
    ("error", "STRING"),
    ("reused_from", "STRING"),
    ("submitted_by", "STRING"),
    ("created_at", "TIMESTAMP"),
    ("finished_at", "TIMESTAMP"),
    ("live", "BOOLEAN"),
)

#: ``attempts`` -- ALL store columns plus a denormalised ``query_name``.
#:
#: DELIBERATE DENORMALISATION 2 of 2. Taken from the OWNING CALL
#: (``calls.query_name``), with ``jobs.query_name`` as the fallback for a
#: legacy NULL call value, and a CONTRADICTORY non-NULL pair REJECTED rather
#: than silently resolved -- see :func:`attempt_query_name`. Present for the
#: same reason as on ``items``: it is this table's partition key.
_ATTEMPTS_COLUMNS: tuple[tuple[str, str], ...] = (
    ("call_id", "STRING"),
    ("attempt_no", "INT"),
    ("started_at", "TIMESTAMP"),
    ("finished_at", "TIMESTAMP"),
    # 'reused' here is what marks a call that carries a full copy of another
    # call's reply with fresh_usage zeroed.
    ("outcome_kind", "STRING"),
    ("usage", "STRING"),
    ("worker_id", "STRING"),
    # Service time is finished_at - started_at; queue wait is
    # started_at - queued_at.
    ("queued_at", "TIMESTAMP"),
    # Denormalised from the owning call -- see this tuple's note above.
    ("query_name", "STRING"),
)

#: ``endpoint_state`` -- all store columns EXCEPT ``halt_kind`` and
#: ``halt_message``.
#:
#: Those two are LIVE OPERATIONAL CONTROL: they stop a running daemon from
#: calling an endpoint, and are cleared by the operator. A halt observed in a
#: snapshot says nothing about the deliberation the snapshot records.
#:
#: The counters that remain are MUTABLE and CUMULATIVE, keyed
#: ``(user, endpoint)`` and shared by every job in the store, so these rows
#: are POINT-IN-TIME OBSERVATIONS of a running total and are NEVER additive
#: across snapshots. ``user`` is the store-local identity literal, not a
#: provenance label.
_ENDPOINT_STATE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("user", "STRING"),
    ("endpoint", "STRING"),
    ("budget_epoch", "INT"),
    # BIGINT, not INT: a long-lived store's lifetime token count has no
    # reason to stay under 2^31.
    ("epoch_tokens", "BIGINT"),
    ("lifetime_tokens", "BIGINT"),
    ("updated_at", "TIMESTAMP"),
)

#: ``workers`` -- ONLY ``worker_id``, ``host``, ``version``, ``started_at``.
#:
#: The rest of the store's ``workers`` row is live process state (``pid``,
#: ``heartbeat_at``, ``stop_requested_at``, ``endpoints``, ``kind``,
#: ``user``) describing a daemon that no longer exists. What survives is the
#: provenance question an archive is asked: which build, on which host,
#: produced these calls.
#:
#: All six ``version`` values in the 2026-10-23 snapshot are NULL. The
#: column publishes anyway and the NULLs are PRESERVED.
_WORKERS_COLUMNS: tuple[tuple[str, str], ...] = (
    ("worker_id", "STRING"),
    ("host", "STRING"),
    ("version", "STRING"),
    ("started_at", "TIMESTAMP"),
)

#: ``job_disposition`` (NEW) -- what the project decided a job's output was
#: FOR. ``store_id`` is not repeated here: it arrives with the shared
#: identity block, which already leads every archived table.
_JOB_DISPOSITION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("job_id", "STRING"),
    # One of JOB_DISPOSITIONS, enforced in Python at the write boundary.
    ("disposition", "STRING"),
    ("reason", "STRING"),
    ("declared_by", "STRING"),
    ("declared_at", "TIMESTAMP"),
)

#: ``role_brief`` (NEW) -- the per-role mission text extracted from
#: ``query_types.spec``, so nobody parses spec JSON to read it. DERIVED, not
#: authoritative; ``store_id`` arrives with the identity block.
#: ``mission_text`` carries :data:`ROLE_BRIEF_UNEXTRACTABLE` when the spec
#: shape could not be read, which is the only sanctioned alternative to a
#: real extraction.
_ROLE_BRIEF_COLUMNS: tuple[tuple[str, str], ...] = (
    ("query_name", "STRING"),
    ("query_version", "STRING"),
    ("role", "STRING"),
    ("mission_text", "STRING"),
    ("answer_schema", "STRING"),
)

#: Every archived table, in the order :func:`table_configs` emits them: the
#: seven store-derived tables in store creation order, then the two new ones.
ARCHIVED_TABLES: tuple[str, ...] = (
    "query_types",
    "jobs",
    "items",
    "calls",
    "attempts",
    "endpoint_state",
    "workers",
    JOB_DISPOSITION_TABLE,
    ROLE_BRIEF_TABLE,
)

#: Source columns per archived table -- the per-table half of the single
#: source of truth. Keyed by the names in :data:`ARCHIVED_TABLES`.
_SOURCE_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "query_types": _QUERY_TYPES_COLUMNS,
    "jobs": _JOBS_COLUMNS,
    "items": _ITEMS_COLUMNS,
    "calls": _CALLS_COLUMNS,
    "attempts": _ATTEMPTS_COLUMNS,
    "endpoint_state": _ENDPOINT_STATE_COLUMNS,
    "workers": _WORKERS_COLUMNS,
    JOB_DISPOSITION_TABLE: _JOB_DISPOSITION_COLUMNS,
    ROLE_BRIEF_TABLE: _ROLE_BRIEF_COLUMNS,
}

#: Each archived table's OWN primary key -- the source table's key, without
#: the identity prefix. :func:`table_primary_key` prepends
#: ``(store_id, snapshot_id)``.
#:
#: ``job_disposition`` is keyed on ``job_id`` alone (one disposition per job
#: per snapshot); ``role_brief`` on ``(query_name, query_version, role)``,
#: matching the ``(name, version)`` key of the ``query_types`` row it is
#: derived from plus the role it describes.
_SOURCE_PRIMARY_KEY: dict[str, tuple[str, ...]] = {
    "query_types": ("name", "version"),
    "jobs": ("job_id",),
    "items": ("job_id", "item_id"),
    "calls": ("call_id",),
    "attempts": ("call_id", "attempt_no"),
    "endpoint_state": ("user", "endpoint"),
    "workers": ("worker_id",),
    JOB_DISPOSITION_TABLE: ("job_id",),
    ROLE_BRIEF_TABLE: ("query_name", "query_version", "role"),
}

#: Which archived tables partition, and on what. Only the three high-volume
#: tables appear; a table absent from this map is UNPARTITIONED and its
#: config omits the ``partition_by`` key entirely. See the module docstring's
#: "PARTITIONING" paragraph for why these three and no others.
_PARTITION_BY: dict[str, str] = {
    "items": PARTITION_COLUMN,
    "calls": PARTITION_COLUMN,
    "attempts": PARTITION_COLUMN,
}


def _schema_sql(columns: tuple[tuple[str, str], ...]) -> str:
    """Render ``(name, sql_type)`` pairs as a DDL column-list fragment.

    Identical to ``clearinghouse_schema._schema_sql`` by design: the two
    namespaces are written through the same ``load()`` path, which re-parses
    ``schema_sql``, so the rendering must not differ between them.
    """
    return ", ".join(f"{name} {sql_type}" for name, sql_type in columns)


def table_name(table: str) -> str:
    """Return the physical table name for one archived council table.

    The council namespace's tables are not composed from an
    ``(entity_type, kind)`` pair the way the clearinghouse's are -- each is a
    named mirror of a store table (or one of the two new ones) -- so this
    resolver VALIDATES rather than composes. It is still the only sanctioned
    way to name a table, for the same reason: the config generator and every
    caller share one definition and cannot drift, and an unknown name never
    silently yields a table that does not exist.

    Args:
        table: One of :data:`ARCHIVED_TABLES`.

    Returns:
        The physical table name (equal to ``table``).

    Raises:
        ValueError: If ``table`` is not an archived table. A store table
            that deliberately does not publish (:data:`NOT_ARCHIVED`, i.e.
            ``schema_version``) gets a message saying so, rather than the
            generic unknown-name message, because asking for it is a
            different mistake from a typo.
    """
    if table in NOT_ARCHIVED:
        raise ValueError(
            f"table_name: {table!r} is a council STORE table that "
            "deliberately does not publish into the "
            f"{COUNCIL_NAMESPACE!r} namespace -- the namespace carries its "
            "own version. See council_schema.NOT_ARCHIVED."
        )
    if table not in ARCHIVED_TABLES:
        raise ValueError(
            f"table_name: unknown council table {table!r}; "
            f"expected one of {ARCHIVED_TABLES!r}."
        )
    return table


def table_columns(table: str) -> tuple[tuple[str, str], ...]:
    """Return the ordered ``(name, sql_type)`` column pairs for one table.

    The shared identity block (:data:`_IDENTITY_COLUMNS`) LEADS, followed by
    that table's source columns. This is the ONLY sanctioned read of an
    archived table's column list: an archiver resolving which columns to
    write, and the tests, both derive from here rather than repeating a list.

    Args:
        table: One of :data:`ARCHIVED_TABLES`.

    Returns:
        The column declarations in DDL order, as ``(name, sql_type)`` pairs.

    Raises:
        ValueError: If ``table`` is unknown or deliberately not archived
            (validated through :func:`table_name`).
    """
    table_name(table)
    return _IDENTITY_COLUMNS + _SOURCE_COLUMNS[table]


def column_names(table: str) -> tuple[str, ...]:
    """Return just the ordered column NAMES for one archived table.

    Thin convenience over :func:`table_columns`, mirroring
    ``clearinghouse_schema.column_names``.
    """
    return tuple(name for name, _sql_type in table_columns(table))


def table_primary_key(table: str) -> tuple[str, ...]:
    """Return one archived table's primary key, identity prefix first.

    Always ``("store_id", "snapshot_id") + <the source table's own primary
    key>``. The prefix is what makes the key correct across stores and
    across snapshots: ``job_id`` is unique only WITHIN one store, and the
    same ``job_id`` legitimately appears in every snapshot of that store, so
    the source key alone would collide on both axes.

    This is returned by an accessor rather than carried in the dicts
    :func:`table_configs` emits, deliberately: those dicts are consumed by
    ``BerdlCapability.load()`` / ``build_ingest_config``, which read only
    ``name``, ``schema_sql`` and ``partition_by``, and Iceberg tables written
    through that path declare no primary key. Putting an unread key in the
    config would be a claim the write path does not honour.

    Args:
        table: One of :data:`ARCHIVED_TABLES`.

    Returns:
        The primary-key column names, in order.

    Raises:
        ValueError: If ``table`` is unknown or deliberately not archived.
    """
    table_name(table)
    return _PK_IDENTITY_PREFIX + _SOURCE_PRIMARY_KEY[table]


def table_configs() -> list[dict[str, Any]]:
    """Return the nine council table configs for ``BerdlCapability.load``.

    Each dict is shaped for the ``tables=`` argument of
    :meth:`~kbutillib.domains.kbase.berdl.capability.BerdlCapability.load`:
    a ``name`` (always :func:`table_name`, so generator and resolver cannot
    drift), a ``schema_sql`` DDL fragment derived from
    :func:`table_columns`, and a ``partition_by`` ONLY on ``items``,
    ``calls`` and ``attempts`` -- all three on :data:`PARTITION_COLUMN`. The
    other six omit the key entirely, so its absence unambiguously means
    "unpartitioned".

    Returns:
        A new list of nine dicts in :data:`ARCHIVED_TABLES` order. Freshly
        built on every call, so callers may freely mutate the result (e.g.
        to add ``bronze_path``) without affecting this module's constants.
    """
    configs: list[dict[str, Any]] = []
    for table in ARCHIVED_TABLES:
        config: dict[str, Any] = {
            "name": table_name(table),
            "schema_sql": _schema_sql(table_columns(table)),
        }
        if table in _PARTITION_BY:
            config["partition_by"] = _PARTITION_BY[table]
        configs.append(config)
    return configs


# --------------------------------------------------------------------------
# The two deliberate denormalisations
# --------------------------------------------------------------------------


class ContradictoryQueryNameError(ValueError):
    """Raised when a call and its job disagree on a non-NULL ``query_name``.

    ``attempts.query_name`` is denormalised from the owning call, falling
    back to the job only when the call's value is NULL. When BOTH are
    non-NULL and they DIFFER, there is no safe choice: ``query_name`` is the
    only council-kind discriminator in the store, the archived table is
    append-only, and it is the partition key -- so a wrong value is baked
    into the physical layout and cannot be corrected without a full rewrite.
    Silently preferring either side would hide a real inconsistency in the
    source store behind a plausible-looking row.
    """


def item_query_name(*, job_query_name: str | None) -> str | None:
    """Resolve the denormalised ``items.query_name`` (1 of 2).

    The store's ``items`` table has no ``query_name`` column, so the archived
    value comes from the owning ``jobs`` row, joined on
    ``(store_id, job_id)``. There is exactly one source, so there is nothing
    to reconcile -- this function exists so the derivation is NAMED and
    testable rather than inlined as a join at an archiver call site, which is
    where it would quietly become "whatever the SQL happened to say".

    Args:
        job_query_name: ``jobs.query_name`` for the item's job. The store
            declares that column ``nullable=False``, so ``None`` here means
            the join found no job row.

    Returns:
        ``job_query_name`` unchanged, including ``None``.
    """
    return job_query_name


def attempt_query_name(
    *, call_query_name: str | None, job_query_name: str | None
) -> str | None:
    """Resolve the denormalised ``attempts.query_name`` (2 of 2).

    The owning call is the authority. ``calls.query_name`` is nullable in the
    store, and legacy rows carry NULL, so ``jobs.query_name`` is the declared
    FALLBACK for exactly that case -- and only that case.

    Args:
        call_query_name: ``calls.query_name`` for the attempt's call.
        job_query_name: ``jobs.query_name`` for that call's job, used only
            when ``call_query_name`` is ``None``.

    Returns:
        ``call_query_name`` when it is non-NULL; otherwise
        ``job_query_name`` (which may itself be ``None`` if the join found
        no job row).

    Raises:
        ContradictoryQueryNameError: Both values are non-NULL and they
            differ. Not resolved in favour of either side -- see that
            exception's docstring.
    """
    if call_query_name is None:
        return job_query_name
    if job_query_name is not None and job_query_name != call_query_name:
        raise ContradictoryQueryNameError(
            "attempt_query_name: the owning call says query_name="
            f"{call_query_name!r} and its job says {job_query_name!r}. "
            "These must agree. Refusing to prefer either: query_name is the "
            "only council-kind discriminator in the store and is the "
            f"partition key of the archived {'attempts'!r} table, so a "
            "wrong value is baked into the physical layout and cannot be "
            "corrected without a full rewrite. Fix the source store, or "
            "archive this attempt's call and job as the inconsistency they "
            "are."
        )
    return call_query_name


# --------------------------------------------------------------------------
# Idempotent bootstrap
# --------------------------------------------------------------------------
#
# DELEGATED, not duplicated. ``clearinghouse_schema.bootstrap()`` is not
# clearinghouse-specific: it reads only ``name``, ``partition_by`` and (via
# ``load()``) ``schema_sql`` from each config and never parses a name back
# into a type and a kind -- its own docstring says so explicitly. Its
# control flow IS the contract this namespace needs: refuse on an
# indeterminate existence probe rather than fall through to an overwrite,
# refuse when a live partition spec disagrees with the config, never request
# 'overwrite', write nothing at all if any table in the batch fails a check,
# and preview every table in dry-run instead of stopping at the first
# refusal.
#
# A second copy of that ~100 lines here would be free to drift from it, and
# the drift would be invisible until a live run -- which is the one place
# neither copy can be tested. So this module's ``bootstrap()`` is a thin
# wrapper that supplies the council defaults and delegates, and the two
# refusal types are RE-EXPORTED rather than redefined so a caller catching
# ``BootstrapPartitionSpecMismatchError`` catches it from either namespace.
#
# The same capability contract applies, and the same caveat: a real
# ``BerdlCapability`` does not implement ``table_exists`` /
# ``table_partition_spec``; ``ClearinghouseBootstrapCapability`` in
# ``clearinghouse_bootstrap_adapter`` is the adapter that does, and nothing
# about it is clearinghouse-specific either. None of this is verified
# against a live pod (KBUtilLib#91).


def bootstrap(
    capability: Any,
    *,
    namespace: str,
    tables: Sequence[Mapping[str, Any]] | None = None,
    dataset: str = NAMESPACE,
    tenant: str = TENANT,
    pipeline_name: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Idempotently create-or-verify the nine council archive tables.

    Same contract, same arguments, same return shape and same refusals as
    :func:`~kbutillib.domains.kbase.berdl.clearinghouse_schema.bootstrap`,
    which this delegates to (see the note above) with council defaults:
    ``tables`` defaults to :func:`table_configs`, ``dataset`` to
    :data:`NAMESPACE` and ``tenant`` to :data:`TENANT`.

    Safe to run repeatedly against the same namespace: the first run creates
    each table that does not yet exist; every subsequent run verifies each
    table's live partition spec against this config and either appends (spec
    matches) or refuses (spec disagrees). It never re-specs a live table's
    partitioning and never requests ``'overwrite'``.

    Args:
        capability: The injected capability -- must expose ``load()``,
            ``table_exists(name, namespace=...)`` and
            ``table_partition_spec(name, namespace=...)``. See the note
            above this function.
        namespace: The Iceberg namespace to bootstrap into. Required and
            explicit; there is no default, because falling back to
            ``load()``'s ``namespace="default"`` is a live path to writing a
            populated table looked up under the wrong namespace. Typically
            :data:`COUNCIL_NAMESPACE` or an operator-chosen disposable
            namespace for a smoke test.
        tables: The table configs to bootstrap. Defaults to
            :func:`table_configs` -- all nine. Tests may pass a subset.
        dataset: The ``load()`` call's top-level ``dataset``. Defaults to
            :data:`NAMESPACE`.
        tenant: The ``load()`` call's ``tenant``. Defaults to
            :data:`TENANT`.
        pipeline_name: Forwarded to ``load()`` unchanged.
        dry_run: Perform every read-only check but never call ``load()``.

    Returns:
        The delegate's dict: ``'namespace'``, ``'dry_run'``, a ``'tables'``
        list of per-table reports, and ``'load_result'``.

    Raises:
        ValueError: ``namespace`` is empty/falsy.
        BootstrapIndeterminateStateError: A table's existence could not be
            positively determined. No table in the batch is written.
        BootstrapPartitionSpecMismatchError: A live partition spec
            disagrees with this config. No table in the batch is written.
        BerdlLoadRefusedError: Propagated untouched from ``load()`` when
            ``capability`` is off-pod.
    """
    return _clearinghouse_bootstrap(
        capability,
        namespace=namespace,
        tables=table_configs() if tables is None else tables,
        dataset=dataset,
        tenant=tenant,
        pipeline_name=pipeline_name,
        dry_run=dry_run,
    )
