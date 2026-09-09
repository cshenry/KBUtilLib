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

Partitioning: ``entity`` and ``canonical_content`` are deliberately
unpartitioned -- bucketing on a hash gives zero read pruning, since a good
hash scatters uniformly across buckets by design. ``result`` partitions on
the bare, stored column ``source`` (format ``<tool>/<version>``), because
that is a real column, not a transform expression. Iceberg partition
transforms such as ``bucket(256, entity_hash)``, ``truncate(...)``, or
``days(...)`` are not expressible through this write path: the wrapper
passes ``partition_by`` straight to PySpark's ``partitionedBy(*cols)`` as
bare strings, so a transform expression would be read as a nonexistent
column name and fail at runtime. Never emit one here.
"""

from __future__ import annotations

from typing import Any

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
    a ``name``, a ``schema_sql`` DDL fragment, and -- for ``result`` only --
    a ``partition_by`` naming the bare, stored ``source`` column. ``entity``
    and ``canonical_content`` carry no ``partition_by`` key at all: bucketing
    a hash column gives zero read pruning, so they are deliberately
    unpartitioned rather than partitioned on a poor key.

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
        },
        {
            "name": "canonical_content",
            "schema_sql": _schema_sql(_CANONICAL_CONTENT_COLUMNS),
        },
        {
            "name": "result",
            "schema_sql": _schema_sql(_RESULT_COLUMNS),
            "partition_by": "source",
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
