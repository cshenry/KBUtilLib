"""Table configs and hash-encoding helpers for the content-hash clearinghouse.

This defines the three Apache Iceberg tables of the ``kbaseincubator``
tenant's ``clearinghouse`` namespace in the BER Data Lakehouse -- ``entity``,
``canonical_content``, and ``result`` -- as pure, pod-free config dicts
shaped for :meth:`~kbutillib.domains.kbase.berdl.capability.BerdlCapability.load`
(``tables=table_configs()``), plus a pair of hash-encoding helpers.

Why the hash needs an encoding boundary at all: the platform's only hashing
surface, :mod:`kbutillib.domains.identity.standardizers`, returns
``entity_hash()``/``content_hash()`` as a 64-character **hex string**
(``hashlib.sha256(...).hexdigest()``). This module stores that same hash as
raw **32-byte binary**, not hex text -- hex would double the width of the
largest column in a corpus heading toward roughly one billion rows, for no
benefit. That size decision creates a representation gap: a hex digest
handed to a probe query and a raw-bytes column in the table are different
values by every equality comparison, even though they encode the same
identity. :func:`encode_entity_hash` and :func:`decode_entity_hash` are the
one seam that crosses that gap, so no caller improvises its own hex/bytes
conversion at a query site.

The gap is not cosmetic. A caller who plugs a standardizer's hex digest
straight into a dedup probe against the binary ``entity_hash`` column --
whether as a bound parameter or, worse, interpolated as a hex literal in
SQL text -- gets zero matches against every already-ingested row,
concludes the clearinghouse is empty for that corpus, and fails open into
recomputing work that was already done. **Always** encode with
:func:`encode_entity_hash` before binding, and **always** bind the
resulting 32 raw bytes as a query parameter -- never format a hex string
into SQL text, both because parameter binding is what makes the
byte-for-byte comparison correct in the first place, and because
string-formatted SQL is an injection surface regardless.

Column types are declared exactly once, in ``_ENTITY_COLUMNS``,
``_CANONICAL_CONTENT_COLUMNS``, and ``_RESULT_COLUMNS`` below, as ordered
``(name, sql_type)`` pairs. ``entity_hash`` is declared as bare ``BINARY``,
not a parameterized ``BINARY 32`` spelling -- Spark SQL's ``BinaryType`` has
no length parameter, so a length suffix would not be valid DDL, and this
module's config values must in any case never contain a parenthesis (that
is also the guard against a partition-transform expression such as
``bucket 256 entity_hash`` leaking into a config; see "Partitioning"
below). The 32-byte width is enforced in Python, at the boundary, by
:func:`encode_entity_hash` and :func:`decode_entity_hash` -- not by the
DDL. Each table config carries the column declarations as a
``schema_sql`` DDL fragment (e.g. ``"entity_hash BINARY, entity_type
STRING, ..."``) rather than a ``schema`` object built from a PySpark
``StructType`` -- ``pyspark`` is not, and must not become, a dependency of
this off-pod module, and a DDL string is legible without one.
:func:`~kbutillib.domains.kbase.berdl.capability.build_ingest_config` and
``data_lakehouse_ingest.ingest`` pass either ``schema`` or ``schema_sql``
through unchanged and validate neither, so whether Spark/Iceberg accepts
bare ``BINARY`` through this specific write path, and whether the
resulting column is genuinely binary rather than STRING, is **not
verifiable off-pod** and is not asserted here as fact. Confirming that is
an in-pod operator step (OP2): create the three tables from a ``kbhub``
notebook using this module's ``table_configs()``, then inspect the created
columns' physical types before any production data is loaded.

Partitioning: bucketing on a hash gives zero read pruning, since a good
hash scatters uniformly across buckets by design -- ``entity_hash`` and
``content`` are therefore never partition keys anywhere in this module.
``entity`` partitions on the bare, stored ``entity_type`` column: the
dedup probe is typed by construction (``standardizers.entity_hash()``
takes ``entity_type`` as a mandatory positional argument), and identity
in this schema is the pair ``(entity_hash, entity_type)`` rather than the
hash alone -- ``entity_type`` is a real, five-valued stored column, which
is exactly what makes it a usable partition key. ``result`` partitions on
two bare, stored columns, ``source`` (format ``<tool>/<version>``) then
``entity_type``, in that order -- the order is significant, since an
Iceberg partition spec is compared by list equality, not by membership.
``canonical_content`` remains unpartitioned, but that is deferred pending
downstream query shapes, not settled: unlike ``entity`` and ``result``,
no partition key has yet been identified for it, not a considered
decision that none exists. Iceberg partition transforms such as
``bucket(256, entity_hash)``, ``truncate(...)``, or ``days(...)`` are not
expressible through this write path: the wrapper passes ``partition_by``
straight to PySpark's ``partitionedBy(*cols)`` as bare strings, so a
transform expression would be read as a nonexistent column name and fail
at runtime. Never emit one here.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

#: The BERDL tenant this namespace lives under.
TENANT = "kbaseincubator"

#: The Iceberg namespace within the tenant.
NAMESPACE = "clearinghouse"

#: Column declarations for the ``entity`` table, in DDL order. Declared once;
#: every other reference to these columns (``table_configs()``, tests)
#: derives from this tuple rather than repeating the list.
_ENTITY_COLUMNS: tuple[tuple[str, str], ...] = (
    ("entity_hash", "BINARY"),
    ("entity_type", "STRING"),
    ("standardizer_version", "STRING"),
    ("observed_at", "TIMESTAMP"),
    ("ingest_batch_id", "STRING"),
)

#: Column declarations for the ``canonical_content`` table, in DDL order.
_CANONICAL_CONTENT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("entity_hash", "BINARY"),
    ("content", "STRING"),
    ("standardizer_version", "STRING"),
    ("observed_at", "TIMESTAMP"),
    ("ingest_batch_id", "STRING"),
)

#: Column declarations for the ``result`` table, in DDL order.
_RESULT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("entity_hash", "BINARY"),
    ("entity_type", "STRING"),
    ("result_type", "STRING"),
    ("source", "STRING"),
    ("result_type_version", "STRING"),
    ("payload", "STRING"),
    ("observed_at", "TIMESTAMP"),
    ("ingest_batch_id", "STRING"),
)

#: The only ``entity_type`` values the clearinghouse recognizes -- matches
#: the ``entity_type`` values :mod:`kbutillib.domains.identity.standardizers`
#: standardizes.
ENTITY_TYPES = ("genome", "protein", "gene_dna", "function", "ontology_term")

#: Raw byte length of a sha256 digest -- the width of ``entity_hash``.
_HASH_BYTES = 32

#: Character length of a sha256 hex digest -- twice the raw byte width.
_HASH_HEX_CHARS = _HASH_BYTES * 2


def _schema_sql(columns: tuple[tuple[str, str], ...]) -> str:
    """Render ``(name, sql_type)`` pairs as a DDL column-list fragment."""
    return ", ".join(f"{name} {sql_type}" for name, sql_type in columns)


def table_configs() -> list[dict[str, Any]]:
    """Return the three clearinghouse table configs for ``BerdlCapability.load``.

    Each dict is shaped for the ``tables=`` argument of
    :meth:`~kbutillib.domains.kbase.berdl.capability.BerdlCapability.load`
    (and, underneath it, :func:`~kbutillib.domains.kbase.berdl.capability.build_ingest_config`):
    a ``name``, a ``schema_sql`` DDL fragment, and -- for ``entity`` and
    ``result`` -- a ``partition_by`` naming real, stored columns. ``entity``
    partitions on the bare ``entity_type`` column; ``result`` partitions on
    the two bare columns ``source`` and ``entity_type``, in that order.
    ``canonical_content`` carries no ``partition_by`` key at all: bucketing
    a hash column gives zero read pruning, and that table is
    content-addressed (two rows colliding on a hash carry identical
    ``content`` by construction), so it is deliberately unpartitioned
    rather than partitioned on a poor key.

    Returns:
        A new list of three dicts, in the order ``entity``,
        ``canonical_content``, ``result``. Freshly built on every call, so
        callers may freely mutate the result (e.g. to add ``bronze_path``
        for bronze-mode ingest) without affecting this module's constants.
    """
    return [
        {
            "name": "entity",
            "schema_sql": _schema_sql(_ENTITY_COLUMNS),
            "partition_by": "entity_type",
        },
        {
            "name": "canonical_content",
            "schema_sql": _schema_sql(_CANONICAL_CONTENT_COLUMNS),
        },
        {
            "name": "result",
            "schema_sql": _schema_sql(_RESULT_COLUMNS),
            "partition_by": ["source", "entity_type"],
        },
    ]


def encode_entity_hash(value: str | bytes) -> bytes:
    """Encode an ``entity_hash`` to the raw 32-byte form stored in Iceberg.

    Accepts either a 64-character hex string (as returned by
    :func:`kbutillib.domains.identity.standardizers.entity_hash` /
    ``content_hash``) or 32 raw bytes already in that form, and returns 32
    raw bytes in both cases -- the exact value to bind as a query parameter
    against the binary ``entity_hash`` column, never interpolated into
    SQL text as a hex literal.

    Args:
        value: A 64-character hex string (case-insensitive) or 32 raw
            bytes.

    Returns:
        The 32-byte raw encoding.

    Raises:
        ValueError: If ``value`` is a ``str`` of the wrong length, contains
            non-hex characters, is ``bytes`` of the wrong length, or is
            neither ``str`` nor ``bytes``.
    """
    if isinstance(value, bytes):
        if len(value) != _HASH_BYTES:
            raise ValueError(
                f"encode_entity_hash: expected {_HASH_BYTES} raw bytes, "
                f"got {len(value)}."
            )
        return value
    if isinstance(value, str):
        if len(value) != _HASH_HEX_CHARS:
            raise ValueError(
                f"encode_entity_hash: expected a {_HASH_HEX_CHARS}-character "
                f"hex string, got length {len(value)}."
            )
        try:
            return bytes.fromhex(value)
        except ValueError as exc:
            raise ValueError(
                f"encode_entity_hash: {value!r} is not a valid hex string."
            ) from exc
    raise ValueError(
        "encode_entity_hash: expected a str (hex) or bytes (raw), got "
        f"{type(value).__name__}."
    )


def decode_entity_hash(value: bytes) -> str:
    """Decode a raw 32-byte ``entity_hash`` to its lowercase hex form.

    The inverse of :func:`encode_entity_hash`'s bytes-producing direction:
    round-tripping ``decode_entity_hash(encode_entity_hash(hex_str))``
    reproduces ``hex_str.lower()`` exactly.

    Args:
        value: 32 raw bytes, as stored in the ``entity_hash`` column or
            returned by :func:`encode_entity_hash`.

    Returns:
        The lowercase 64-character hex form.

    Raises:
        ValueError: If ``value`` is not ``bytes`` or is not exactly 32
            bytes long.
    """
    if not isinstance(value, bytes):
        raise ValueError(
            f"decode_entity_hash: expected bytes, got {type(value).__name__}."
        )
    if len(value) != _HASH_BYTES:
        raise ValueError(
            f"decode_entity_hash: expected {_HASH_BYTES} raw bytes, got "
            f"{len(value)}."
        )
    return value.hex()


# --------------------------------------------------------------------------
# Idempotent bootstrap
# --------------------------------------------------------------------------
#
# ``BerdlCapability.load()`` (see capability.py) already resolves
# create-vs-append per table via ``select_write_mode()``, but it can only
# report what it found *after* it has already written (its per-table
# ``existed_before``/``effective_mode`` report is produced in the same pass
# that calls ``data_lakehouse_ingest.ingest``). A caller that wants to
# refuse a write *before* it happens -- specifically, before appending to a
# table whose live partitioning disagrees with this module's config --
# cannot use ``load()`` alone as a read-only probe, because calling it is
# itself the write.
#
# ``bootstrap()`` therefore requires its injected ``capability`` to expose
# two small, explicit, read-only operations *in addition to* ``load()``:
#
#   - ``capability.table_exists(name, namespace=namespace) -> bool``
#   - ``capability.table_partition_spec(name, namespace=namespace)
#         -> list[str] | None``
#
# Neither method exists on the real ``BerdlCapability`` today (as of this
# module's ``capability.py``) -- that class exposes ``load()``, ``query()``,
# ``databases()``, ``memberships()``, and ``locus()``, but no public,
# read-only "does this table exist / what is its live partition spec"
# lookup. Wiring a real adapter around ``BerdlCapability`` that implements
# these two methods (most plausibly via ``capability.query()`` against
# Iceberg/Spark catalog metadata, which is exactly the kind of thing that
# cannot be verified off-pod) is explicitly **not** done here -- see this
# module's tests, which pass a hand-built fake satisfying this contract,
# and the task's work-record, which states plainly that the live
# Iceberg-on-Polaris path is therefore unverified by this change.


class BootstrapIndeterminateStateError(RuntimeError):
    """Raised when a table's existence cannot be positively determined.

    Per the bootstrap safety contract: a lookup error, an ambiguous
    result, or any other unexpected exception from
    ``capability.table_exists()`` must never be treated as "the table does
    not exist" and fallen through to an overwrite. This error is raised
    instead, and no write is attempted for any table in the same
    :func:`bootstrap` call.
    """


class BootstrapPartitionSpecMismatchError(RuntimeError):
    """Raised when a live table's partition spec disagrees with the config.

    This is the load-bearing safety check :func:`bootstrap` exists to
    provide: there is no check anywhere in the ``BerdlCapability.load()``
    write path comparing a config's ``partition_by`` against a live
    table's actual partition spec before appending, and changing a live
    Iceberg table's partition spec through that path is unsupported and
    unmeasured. Refusing here, before ever calling ``load()``, is the only
    place that safety can live. No write is attempted for any table in the
    same :func:`bootstrap` call -- not just the mismatched one.
    """


#: Attached to every dry-run "would create" report entry. A dry-run
#: reporting "would create" for a table the operator believes already
#: exists is a red flag about namespace resolution (wrong tenant, wrong
#: personal-vs-tenant prefix, etc.) -- not a green light to proceed. This
#: module deliberately does not try to guess or hardcode the "correct"
#: namespace-resolution rule (tenant-qualified vs. a personal ``my.``
#: prefix); that is not verifiable off-pod, so :func:`bootstrap` always
#: takes ``namespace`` as an explicit, required, caller-supplied parameter.
_NAMESPACE_RESOLUTION_WARNING = (
    "This table was not found to exist under the namespace you supplied. "
    "If you expected it to already exist, this is more likely a wrong or "
    "misresolved 'namespace' argument than a genuinely new table -- "
    "verify the namespace before proceeding, since proceeding would "
    "create a *new* table rather than writing to the one you intended."
)


def _normalize_partition_columns(value: str | Sequence[str] | None) -> list[str]:
    """Normalize a ``partition_by``-shaped value to a canonical list.

    Accepts the same shapes as ``BerdlCapability.load()``'s ``partition_by``
    (a bare string, a sequence of strings, or absent/``None``) and a live
    capability's reported spec (``list[str] | None``), and reduces both to
    the same ``list[str]`` form -- ``[]`` for "unpartitioned" in either
    case -- so the two are directly comparable by plain equality. Order is
    preserved (and therefore significant in the comparison): partition
    column *order* is part of an Iceberg partition spec, not just its
    membership.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


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
    """Idempotently create-or-verify the clearinghouse tables.

    Safe to run repeatedly against the same namespace: the first run
    creates each table that does not yet exist; every subsequent run
    verifies each table's live partition spec against this config and
    either appends (spec matches) or refuses (spec disagrees) -- it never
    re-specs a live table's partitioning and never requests
    ``'overwrite'`` for a table that already exists.

    Required ``capability`` interface (a real :class:`BerdlCapability` does
    **not** yet implement the first two of these -- see the module-level
    note above this function; tests inject a fake):

    - ``load(*, dataset, tables, namespace, tenant=None,
      pipeline_name=None, ...)`` -- same signature/semantics as
      :meth:`~kbutillib.domains.kbase.berdl.capability.BerdlCapability.load`.
      Called once, for every table this call resolves to create/append,
      with every table's requested mode set to ``'append'`` -- ``bootstrap``
      itself never requests ``'overwrite'``; ``load()``'s own
      ``select_write_mode()`` promotes ``'append'`` to ``'overwrite'`` for
      a table it independently confirms does not yet exist (first
      creation only). Never called at all in ``dry_run`` mode, and never
      called if any table in this batch fails its partition-spec check or
      its existence check.
    - ``table_exists(name, namespace=namespace) -> bool`` -- read-only
      existence probe for one table. Any exception raised here is treated
      as "existence could not be positively determined" (see
      :class:`BootstrapIndeterminateStateError`), never as "does not
      exist."
    - ``table_partition_spec(name, namespace=namespace) -> list[str] |
      None`` -- read-only lookup of a live table's actual partition
      column names, in declared order. Only called for a table
      ``table_exists`` reports as already existing. ``None`` or ``[]``
      both mean "unpartitioned."

    Args:
        capability: The injected capability object -- see above. In
            production this is expected to eventually be (an adapter
            around) :class:`~kbutillib.domains.kbase.berdl.capability.BerdlCapability`;
            in tests, a fake.
        namespace: The Iceberg namespace to bootstrap the tables into.
            Required and must be given explicitly -- ``bootstrap`` never
            falls back to ``BerdlCapability.load()``'s ``namespace="default"``
            default, because doing so is a live path to silently
            overwriting a populated table looked up under the wrong
            namespace (see :class:`BootstrapPartitionSpecMismatchError`
            and the module-level "Idempotent bootstrap" note). Determining
            the *correct* namespace value (tenant-qualified vs. a personal
            ``my.`` prefix, etc.) is an operator decision this module
            deliberately does not make for you.
        tables: The table configs to bootstrap. Defaults to
            :func:`table_configs` (the three clearinghouse tables). Tests
            may pass a smaller/synthetic set.
        dataset: The ``load()`` call's top-level ``dataset``. Defaults to
            :data:`NAMESPACE`.
        tenant: The ``load()`` call's ``tenant`` (used for its
            membership check). Defaults to :data:`TENANT`.
        pipeline_name: Forwarded to ``load()`` unchanged.
        dry_run: When ``True``, performs every read-only check (existence,
            partition-spec comparison) but never calls ``load()`` -- no
            table is created, appended to, or refused-into. Use this to
            preview what a real run would do.

    Returns:
        A dict with ``'namespace'``, ``'dry_run'``, a ``'tables'`` list
        (one report dict per table -- ``'name'``, ``'exists'``,
        ``'expected_partition_by'``, ``'actual_partition_by'`` (present
        only when the table already existed), ``'action'`` (one of
        ``'create'``, ``'append'``, or -- ``dry_run`` only, in place of
        raising -- ``'refuse'`` with a ``'reason'`` string), and -- dry-run
        "create" entries only -- ``'namespace_warning'``), and
        ``'load_result'`` (the raw return value of the single
        ``capability.load()`` call covering every table in this batch, or
        ``None`` in ``dry_run`` mode). In ``dry_run`` mode, a table whose
        existence could not be determined is reported with
        ``'exists': None`` and ``'action': 'refuse'`` rather than raising,
        so the caller sees a full preview across every table instead of
        stopping at the first indeterminate one.

    Raises:
        ValueError: ``namespace`` is empty/falsy.
        BootstrapIndeterminateStateError: Non-``dry_run`` only. A table's
            existence could not be positively determined. No table in
            this batch is written.
        BootstrapPartitionSpecMismatchError: Non-``dry_run`` only. A
            table's live partition spec disagrees with this config's
            ``partition_by``. No table in this batch is written -- not
            just the mismatched one. The message names both the expected
            and the actual spec and directs the operator to rebuild by
            replay (the schema is append-only, so replay is cheap) rather
            than attempting live partition-spec evolution.
        BerdlLoadRefusedError: Propagated, untouched, from ``load()`` when
            ``capability`` is off-pod. No fallback write path is
            attempted -- in particular, this function never imports or
            uses ``pyiceberg``, which would bypass the schema enforcement
            that makes a write through ``load()`` sanctioned in the first
            place. Never raised in ``dry_run`` mode, since ``load()`` is
            never called.
    """
    if not namespace:
        raise ValueError(
            "bootstrap: 'namespace' is required and must be given "
            "explicitly -- never rely on BerdlCapability.load()'s "
            "namespace='default' fallback, which is a live path to "
            "overwriting a populated table looked up under the wrong "
            "namespace."
        )

    table_specs = list(tables) if tables is not None else table_configs()

    reports: list[dict[str, Any]] = []
    to_load: list[dict[str, Any]] = []

    for spec in table_specs:
        name = spec["name"]
        expected_partition = _normalize_partition_columns(spec.get("partition_by"))

        try:
            exists = capability.table_exists(name, namespace=namespace)
            actual_partition: list[str] | None = None
            if exists:
                actual_partition = _normalize_partition_columns(
                    capability.table_partition_spec(name, namespace=namespace)
                )
        except Exception as exc:
            indeterminate_message = (
                f"bootstrap: could not positively determine the state of "
                f"table {namespace!r}.{name!r} ({type(exc).__name__}: "
                f"{exc}). Refusing rather than treating an indeterminate "
                "lookup as 'does not exist' and falling through to an "
                "overwrite. No table in this bootstrap call has been "
                "written. Resolve the lookup failure and re-run."
            )
            if not dry_run:
                raise BootstrapIndeterminateStateError(indeterminate_message) from exc
            # Dry-run previews rather than raises: record the refusal this
            # table's indeterminate state would cause on a real run, and
            # keep checking the remaining tables.
            reports.append(
                {
                    "name": name,
                    "exists": None,
                    "expected_partition_by": expected_partition,
                    "action": "refuse",
                    "reason": indeterminate_message,
                }
            )
            continue

        report: dict[str, Any] = {
            "name": name,
            "exists": exists,
            "expected_partition_by": expected_partition,
        }

        if not exists:
            report["action"] = "create"
            if dry_run:
                report["namespace_warning"] = _NAMESPACE_RESOLUTION_WARNING
            reports.append(report)
            to_load.append({**dict(spec), "mode": "append"})
            continue

        report["actual_partition_by"] = actual_partition

        if actual_partition != expected_partition:
            mismatch_message = (
                f"bootstrap: refusing to write to {namespace!r}.{name!r} -- "
                f"its live partition spec {actual_partition!r} does not "
                f"match this config's partition_by {expected_partition!r}. "
                "Changing a live Iceberg table's partition spec through "
                "this write path is unsupported and unmeasured, so this "
                "bootstrap will never overwrite it or attempt live "
                "partition-spec evolution. No table in this bootstrap "
                "call has been written -- not just this one. If "
                f"{expected_partition!r} is the partitioning you actually "
                "want, rebuild the table by replay: drop it and recreate "
                "under the corrected partition_by, then re-ingest -- the "
                "clearinghouse schema is append-only, so replay is cheap."
            )
            if not dry_run:
                raise BootstrapPartitionSpecMismatchError(mismatch_message)
            report["action"] = "refuse"
            report["reason"] = mismatch_message
            reports.append(report)
            continue

        report["action"] = "append"
        reports.append(report)
        to_load.append({**dict(spec), "mode": "append"})

    if dry_run:
        return {
            "namespace": namespace,
            "dry_run": True,
            "tables": reports,
            "load_result": None,
        }

    load_result = capability.load(
        dataset=dataset,
        tables=to_load,
        namespace=namespace,
        tenant=tenant,
        pipeline_name=pipeline_name,
    )

    return {
        "namespace": namespace,
        "dry_run": False,
        "tables": reports,
        "load_result": load_result,
    }
