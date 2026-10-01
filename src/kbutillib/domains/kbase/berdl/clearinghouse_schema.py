"""Table configs and hash-encoding helpers for the content-hash clearinghouse.

This defines the fifteen Apache Iceberg tables of the ``kbaseincubator``
tenant's ``clearinghouse`` namespace in the BER Data Lakehouse as pure,
pod-free config dicts shaped for
:meth:`~kbutillib.domains.kbase.berdl.capability.BerdlCapability.load`
(``tables=table_configs()``), plus a pair of hash-encoding helpers.

The fifteen tables are ``<entity_type>_<kind>`` (type-first) for each of the
five entity types -- ``genome``, ``protein``, ``gene``, ``function``,
``ontology_term`` -- and each of the three kinds -- ``entity``, ``content``,
``result``. This replaces the earlier three-table scheme (a single
``entity``/``canonical_content``/``result`` triple partitioned by
``entity_type``): the corpus carries a roughly 5000:1 row-count skew across
types (gene ~5B rows vs. function/ontology_term ~1M), and Iceberg table
PROPERTIES (target file size, compaction, sort order, snapshot expiry),
query PLANNING scope, and COMMIT concurrency are all table-level and cannot
be tuned per partition. Splitting one physical table per entity type is the
only place those knobs become per-type.

The ``entity`` and ``result`` kinds carry a GENERIC schema identical across
all five types (they differ only in physical table, partitioning and
properties). The ``content`` kind is TYPE-SPECIALIZED: every content table
carries the shared tail (``entity_hash``, ``standardizer_version``,
``observed_at``, ``ingest_batch_id``) but a different type-specific head
(protein/gene carry a sequence; genome carries assembly metadata plus a
``fasta_reference`` POINTER to the sequence and never the sequence itself,
since 10M genomes inlined would be ~50TB; and so on).

The public :func:`table_name` resolver is the ONLY place a clearinghouse
table name is constructed -- ``f"{entity_type}_{kind}"`` -- so the config
generator and every caller share one definition and cannot drift.

Why the hash is stored as hex STRING, and why it still has a boundary:
the platform's only hashing surface,
:mod:`kbutillib.domains.identity.standardizers`, returns
``entity_hash()``/``content_hash()`` as a 64-character lowercase **hex
string** (``hashlib.sha256(...).hexdigest()``), and ``entity_hash`` (and
``gene_content.protein_entity_hash``) store exactly that string.

These columns were first declared ``BINARY`` -- raw 32 bytes, half the
width of hex in a corpus heading toward roughly a billion rows. That was
reversed on 2026-09-21, by Chris's decision, after the on-pod OP3 run
measured that no row could be written: ``data_lakehouse_ingest``, which
every sanctioned write goes through, re-parses each table's ``schema_sql``
and its type map has no ``BINARY`` ("Unsupported data type 'BINARY' in
schema_sql", ``data_lakehouse_ingest/orchestrator/schema_utils.py``, dev
1219). Spark DDL accepted ``BINARY`` and OP2 verified the tables honoured
it, so the lake could be created but never written. **Do not reintroduce
``BINARY`` here** unless ``data_lakehouse_ingest`` has gained it and a
write -- not a create -- has been measured on-pod; a create proves only
that Spark accepts the type. The cost accepted is doubled hash width.

Hex storage removes the bytes-vs-hex gap but opens a narrower one: STRING
equality is case-sensitive, so an uppercase hex digest and the stored
lowercase form are different values by every comparison, even though they
encode the same identity. A dedup probe bound with an uppercase digest
gets zero matches against every already-ingested row, concludes the
clearinghouse is empty for that corpus, and fails open into recomputing
work already done. :func:`encode_entity_hash` and
:func:`decode_entity_hash` are the one seam that normalises to the stored
form, so no caller improvises its own conversion at a query site.
**Always** pass a hash through :func:`encode_entity_hash` before binding
it, and **always** bind it as a query parameter -- never format it into
SQL text, both because the seam is what guarantees the canonical form and
because string-formatted SQL is an injection surface regardless.

Column types are declared exactly once, in ``_ENTITY_COLUMNS``,
``_CONTENT_TYPE_HEAD`` + ``_CONTENT_COMMON_TAIL`` (a per-type mapping
composing each type's head onto a shared tail declared once), and
``_RESULT_COLUMNS`` below, as ordered ``(name, sql_type)`` pairs. The
64-character width and hex alphabet are enforced in Python, at the
boundary, by :func:`encode_entity_hash` and :func:`decode_entity_hash` --
not by the DDL. This module's config values must never contain a
parenthesis (the guard against a partition-transform expression such as
``bucket 256 entity_hash`` leaking into a config; see "Partitioning"
below). Each table config carries the column declarations as a
``schema_sql`` DDL fragment (e.g. ``"entity_hash STRING, entity_type
STRING, ..."``) rather than a ``schema`` object built from a PySpark
``StructType`` -- ``pyspark`` is not, and must not become, a dependency of
this off-pod module, and a DDL string is legible without one.

Partitioning: bucketing on a hash gives zero read pruning, since a good
hash scatters uniformly across buckets by design -- ``entity_hash`` is
therefore never a partition key anywhere in this module. Because each
entity type now has its own physical tables, ``entity_type`` is no longer a
useful partition key (it is single-valued within each table), so the
partition columns differ from the old scheme:

  - ``standardizer_version`` on ``gene_entity``, ``protein_entity``,
    ``gene_content``, ``protein_content`` -- the four high-volume tables
    where a standardizer bump is the axis worth pruning on.
  - ``source`` (format ``<tool>/<version>``) on all five ``<type>_result``
    tables.
  - NO partition key at all on the remaining six -- ``genome_entity``,
    ``function_entity``, ``ontology_term_entity``, ``genome_content``,
    ``function_content``, ``ontology_term_content`` -- whose row counts are
    low enough (~1M-10M) that a partition key buys nothing.

For the unpartitioned tables the config omits the ``partition_by`` key
entirely (rather than emitting a falsy value), so an absent key
unambiguously means "unpartitioned". Iceberg partition transforms such as
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

#: The fully-qualified WRITE-TARGET namespace of the clearinghouse corpus,
#: ``kbaseincubator.clearinghouse`` -- the fifteen ``<type>_<kind>`` tables
#: this module configures. Named here (rather than concatenated at a call
#: site) so nothing in the read/shard/ingest path has to spell it out, and
#: so it can never be confused with the SOURCE namespace it differs from by
#: five characters -- see :data:`SOURCE_GENOME_CLEARHOUSE_NAMESPACE` and its
#: adjacency warning.
CLEARINGHOUSE_NAMESPACE = f"{TENANT}.{NAMESPACE}"

#: The fully-qualified namespace of the genome SOURCE the ``lakehouse``
#: adapter reads: ``kbaseincubator.genome_clearhouse``. This is a REAL,
#: populated namespace in the same tenant and catalog as the write target
#: (~5.8M genomes in ``genome_quality``; see the operator runbook).
#:
#: It differs from :data:`CLEARINGHOUSE_NAMESPACE` by only five characters,
#: and the difference is a missing ``in`` (genome_clear**in**ghouse would be
#: the target's word; the source is genome_clear*house*). A clearinghouse
#: bootstrap that seeds from this source READS ``genome_clearhouse`` and
#: WRITES ``clearinghouse`` in the same run, so a one-word slip at a call
#: site would read the wrong table or -- far worse -- write the source. Both
#: namespaces are therefore taken from these named constants, never from a
#: string literal at a call site.
SOURCE_GENOME_CLEARHOUSE_NAMESPACE = f"{TENANT}.genome_clearhouse"

#: The only ``entity_type`` values the clearinghouse recognizes -- matches
#: the ``entity_type`` values :mod:`kbutillib.domains.identity.standardizers`
#: standardizes. This is also the order in which per-kind configs are
#: emitted by :func:`table_configs`.
ENTITY_TYPES = ("genome", "protein", "gene", "function", "ontology_term")

#: The three physical kinds a clearinghouse table can be.
_KINDS = ("entity", "content", "result")

#: The three cross-type UNION ALL views :func:`union_view_sql` can emit, one
#: per kind, each named ``all_<kind>``.
_VIEW_KINDS = ("all_entity", "all_content", "all_result")

#: Column declarations for every ``<type>_entity`` table, in DDL order.
#: GENERIC: identical for all five entity types. Declared once; every other
#: reference to these columns (``table_configs()``, tests) derives from this
#: tuple rather than repeating the list.
_ENTITY_COLUMNS: tuple[tuple[str, str], ...] = (
    ("entity_hash", "STRING"),
    ("entity_type", "STRING"),
    ("standardizer_version", "STRING"),
    ("observed_at", "TIMESTAMP"),
    ("ingest_batch_id", "STRING"),
)

#: Column declarations for every ``<type>_result`` table, in DDL order.
#: GENERIC: identical for all five entity types.
_RESULT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("entity_hash", "STRING"),
    ("entity_type", "STRING"),
    ("result_type", "STRING"),
    ("source", "STRING"),
    ("result_type_version", "STRING"),
    ("payload", "STRING"),
    ("observed_at", "TIMESTAMP"),
    ("ingest_batch_id", "STRING"),
)

#: The tail every ``<type>_content`` table shares, declared ONCE and composed
#: onto each type's type-specific head below. ``entity_hash`` leads every
#: content table and is prepended separately (see :func:`_content_columns`),
#: so this tuple holds only the trailing common columns.
_CONTENT_COMMON_TAIL: tuple[tuple[str, str], ...] = (
    ("standardizer_version", "STRING"),
    ("observed_at", "TIMESTAMP"),
    ("ingest_batch_id", "STRING"),
)

#: The columns common to ALL five ``<type>_content`` tables -- the ONLY
#: columns ``all_content`` may union. The five content tables have
#: deliberately divergent type-specialized heads (a sequence, assembly
#: metadata, a definition, ontology fields), so a full union across them is
#: impossible; ``all_content`` selects strictly this shared subset (the
#: leading ``entity_hash`` prepended by :func:`_content_columns`, plus
#: :data:`_CONTENT_COMMON_TAIL`'s names) and a literal ``entity_type``
#: discriminator, and reconciles none of the type-specialized columns. See
#: the module docstring and :func:`union_view_sql`.
_CONTENT_COMMON_COLUMNS: tuple[str, ...] = ("entity_hash",) + tuple(
    name for name, _sql_type in _CONTENT_COMMON_TAIL
)

#: The type-specific HEAD columns for each ``<type>_content`` table, in DDL
#: order, sitting between the leading ``entity_hash`` and the shared tail.
#: genome_content stores a POINTER (``fasta_reference``) to the sequence plus
#: assembly metadata -- never the sequence inlined -- so it has no ``sequence``
#: column; 10M genomes inlined would be ~50TB.
_CONTENT_TYPE_HEAD: dict[str, tuple[tuple[str, str], ...]] = {
    "genome": (
        ("fasta_reference", "STRING"),
        ("taxon_id", "STRING"),
        ("assembly_accession", "STRING"),
        ("n_contigs", "INT"),
        ("total_length", "BIGINT"),
        ("is_closed", "BOOLEAN"),
    ),
    "protein": (
        ("sequence", "STRING"),
        ("seq_length", "INT"),
    ),
    "gene": (
        ("sequence", "STRING"),
        ("seq_length", "INT"),
        ("protein_entity_hash", "STRING"),
    ),
    "function": (
        ("definition", "STRING"),
    ),
    "ontology_term": (
        ("term_id", "STRING"),
        ("name", "STRING"),
        ("namespace", "STRING"),
        ("definition", "STRING"),
    ),
}


def _content_columns(entity_type: str) -> tuple[tuple[str, str], ...]:
    """Compose a ``<type>_content`` column list: hash + type head + tail.

    ``entity_hash`` leads, the type-specific head follows, and the shared
    tail (declared once in :data:`_CONTENT_COMMON_TAIL`) closes -- so the
    common columns live in exactly one place across all five content tables.
    """
    return (
        (("entity_hash", "STRING"),)
        + _CONTENT_TYPE_HEAD[entity_type]
        + _CONTENT_COMMON_TAIL
    )


#: Which single bare, stored column each ``<type>_entity``/``<type>_content``
#: table partitions on, when it partitions at all. Only the four high-volume
#: tables appear here; a type absent from a kind's map is unpartitioned. See
#: the module docstring's "Partitioning" section for the rationale.
_ENTITY_PARTITION_BY: dict[str, str] = {
    "gene": "standardizer_version",
    "protein": "standardizer_version",
}
_CONTENT_PARTITION_BY: dict[str, str] = {
    "gene": "standardizer_version",
    "protein": "standardizer_version",
}

#: Raw byte length of a sha256 digest -- the width of ``entity_hash``.
_HASH_BYTES = 32

#: Character length of a sha256 hex digest -- twice the raw byte width.
_HASH_HEX_CHARS = _HASH_BYTES * 2


def _schema_sql(columns: tuple[tuple[str, str], ...]) -> str:
    """Render ``(name, sql_type)`` pairs as a DDL column-list fragment."""
    return ", ".join(f"{name} {sql_type}" for name, sql_type in columns)


def table_name(entity_type: str, kind: str) -> str:
    """Return the physical table name ``f"{entity_type}_{kind}"``.

    This is the ONLY place a clearinghouse table name is constructed, so the
    config generator and every caller share one definition and cannot drift.

    Args:
        entity_type: One of :data:`ENTITY_TYPES` (``"genome"``,
            ``"protein"``, ``"gene"``, ``"function"``, ``"ontology_term"``).
        kind: One of ``"entity"``, ``"content"``, ``"result"``.

    Returns:
        The ``<entity_type>_<kind>`` table name.

    Raises:
        ValueError: If ``entity_type`` is not a known entity type, or if
            ``kind`` is not a known kind. An unknown value must never
            silently yield a name for a table that does not exist.
    """
    if entity_type not in ENTITY_TYPES:
        raise ValueError(
            f"table_name: unknown entity_type {entity_type!r}; "
            f"expected one of {ENTITY_TYPES!r}."
        )
    if kind not in _KINDS:
        raise ValueError(
            f"table_name: unknown kind {kind!r}; "
            f"expected one of {_KINDS!r}."
        )
    return f"{entity_type}_{kind}"


def table_columns(entity_type: str, kind: str) -> tuple[tuple[str, str], ...]:
    """Return the ordered ``(name, sql_type)`` column pairs for one table.

    This is the ONLY sanctioned read of a clearinghouse table's column list:
    the manifest validator (:mod:`clearinghouse_manifest`) resolves the legal
    columns of a ``[source.content]`` / ``[source.result]`` block from this
    function rather than duplicating the per-type column lists, so the schema
    lives in exactly one place and a manifest naming ``sequence`` for
    ``genome`` is rejected against the SAME definition the DDL is built from.

    Args:
        entity_type: One of :data:`ENTITY_TYPES`.
        kind: One of ``"entity"``, ``"content"``, ``"result"``. ``"entity"``
            and ``"result"`` return the GENERIC schemas (identical across all
            five types); ``"content"`` returns the type-specialized schema.

    Returns:
        The column declarations in DDL order, as ``(name, sql_type)`` pairs.

    Raises:
        ValueError: If ``entity_type`` or ``kind`` is unknown (validated via
            :func:`table_name`).
    """
    # Validate both arguments through the one resolver, so an unknown type or
    # kind raises the same ValueError here as everywhere else.
    table_name(entity_type, kind)
    if kind == "entity":
        return _ENTITY_COLUMNS
    if kind == "result":
        return _RESULT_COLUMNS
    return _content_columns(entity_type)


def column_names(entity_type: str, kind: str) -> tuple[str, ...]:
    """Return just the ordered column NAMES for one table.

    Thin convenience over :func:`table_columns` for callers (the manifest
    validator) that only need the legal name set, not the SQL types.
    """
    return tuple(name for name, _sql_type in table_columns(entity_type, kind))


def table_configs() -> list[dict[str, Any]]:
    """Return the fifteen clearinghouse table configs for ``BerdlCapability.load``.

    Each dict is shaped for the ``tables=`` argument of
    :meth:`~kbutillib.domains.kbase.berdl.capability.BerdlCapability.load`
    (and, underneath it, :func:`~kbutillib.domains.kbase.berdl.capability.build_ingest_config`):
    a ``name`` (always :func:`table_name` for its type and kind, so the
    generator and resolver cannot drift), a ``schema_sql`` DDL fragment, and
    a ``partition_by`` ONLY on the tables that partition -- ``gene_entity``,
    ``protein_entity``, ``gene_content`` and ``protein_content`` on
    ``standardizer_version``, and all five ``<type>_result`` on ``source``.
    The other six configs carry no ``partition_by`` key at all (its absence
    means "unpartitioned"; see the module docstring's "Partitioning"
    section).

    Returns:
        A new list of fifteen dicts in a deterministic, documented order:
        all five ``<type>_entity`` configs in :data:`ENTITY_TYPES` order,
        then all five ``<type>_content``, then all five ``<type>_result``.
        Freshly built on every call, so callers may freely mutate the result
        (e.g. to add ``bronze_path`` for bronze-mode ingest) without
        affecting this module's constants.
    """
    configs: list[dict[str, Any]] = []

    for entity_type in ENTITY_TYPES:
        config: dict[str, Any] = {
            "name": table_name(entity_type, "entity"),
            "schema_sql": _schema_sql(_ENTITY_COLUMNS),
        }
        if entity_type in _ENTITY_PARTITION_BY:
            config["partition_by"] = _ENTITY_PARTITION_BY[entity_type]
        configs.append(config)

    for entity_type in ENTITY_TYPES:
        config = {
            "name": table_name(entity_type, "content"),
            "schema_sql": _schema_sql(_content_columns(entity_type)),
        }
        if entity_type in _CONTENT_PARTITION_BY:
            config["partition_by"] = _CONTENT_PARTITION_BY[entity_type]
        configs.append(config)

    for entity_type in ENTITY_TYPES:
        configs.append(
            {
                "name": table_name(entity_type, "result"),
                "schema_sql": _schema_sql(_RESULT_COLUMNS),
                "partition_by": "source",
            }
        )

    return configs


def _quote_fqn(fqn: str) -> str:
    """Backtick-quote each dot-separated segment of an identifier.

    Matches the convention used by ``_quote_fqn`` in
    :mod:`kbutillib.domains.kbase.berdl.clearinghouse_derivation` and by
    ``BerdlCapability.load()``: every segment between dots gets its own pair
    of backticks, so a multi-part identifier such as
    ``tenant.namespace.table`` is never misread with a dot inside a segment
    as a qualifier boundary. A segment that already arrives backtick-quoted
    is not double-quoted.
    """
    return ".".join(f"`{segment.strip('`')}`" for segment in fqn.split("."))


def union_view_sql(kind: str, *, fqn_prefix: str) -> str:
    """Return ``CREATE OR REPLACE VIEW`` DDL for one cross-type union view.

    After the fifteen-table split, the cross-type query surface is restored
    by three ``UNION ALL`` views, each named ``all_<kind>`` and each
    unioning the five per-type tables of that kind:

    - ``all_entity`` and ``all_result`` union EVERY column, because their
      five per-type tables are schema-identical (the ``entity`` and
      ``result`` kinds carry a GENERIC schema across all five types).
    - ``all_content`` CANNOT union every column: the five ``<type>_content``
      tables have deliberately divergent type-specialized columns
      (``sequence``, ``fasta_reference``/``taxon_id``, ``definition``,
      ``term_id``/``name``, ...). It therefore selects ONLY the columns
      common to all five (:data:`_CONTENT_COMMON_COLUMNS`) plus a literal
      ``entity_type`` discriminator, and reconciles none of the specialized
      columns. Attempting a full union of divergent content schemas is the
      failure mode this view exists to avoid.

    This module stays pod-free and emits SQL TEXT only: it performs no I/O,
    imports no ``pyspark``, and creates nothing itself.

    Args:
        kind: One of ``"all_entity"``, ``"all_content"``, ``"all_result"``.
        fqn_prefix: The fully-qualified prefix the per-type tables live
            under -- typically ``f"{TENANT}.{NAMESPACE}"`` -- taken as a
            parameter rather than hardcoded. Each segment is backtick-quoted
            (via :func:`_quote_fqn`) and joined with the per-type table name
            (also backtick-quoted) so the emitted references are fully
            qualified and safely quoted.

    Returns:
        The ``CREATE OR REPLACE VIEW <view> AS <SELECT> UNION ALL ...`` DDL
        text naming all five per-type tables of ``kind``.

    Raises:
        ValueError: If ``kind`` is not one of the three known view kinds.
    """
    if kind not in _VIEW_KINDS:
        raise ValueError(
            f"union_view_sql: unknown kind {kind!r}; "
            f"expected one of {_VIEW_KINDS!r}."
        )

    table_kind = kind[len("all_") :]
    view_fqn = _quote_fqn(f"{fqn_prefix}.{kind}")

    branches: list[str] = []
    for entity_type in ENTITY_TYPES:
        table_fqn = _quote_fqn(f"{fqn_prefix}.{table_name(entity_type, table_kind)}")
        if table_kind == "content":
            # Divergent content schemas: union only the shared subset plus a
            # literal entity_type discriminator (the physical content tables
            # carry no stored entity_type column).
            select_list = ", ".join(
                f"`{col}`" for col in _CONTENT_COMMON_COLUMNS
            )
            branches.append(
                f"SELECT {select_list}, "
                f"'{entity_type}' AS `entity_type` FROM {table_fqn}"
            )
        else:
            # Schema-identical tables: union every column.
            branches.append(f"SELECT * FROM {table_fqn}")

    body = "\n    UNION ALL\n    ".join(branches)
    return f"CREATE OR REPLACE VIEW {view_fqn} AS\n    {body}"


def encode_entity_hash(value: str | bytes) -> str:
    """Normalise an ``entity_hash`` to the lowercase hex form stored in Iceberg.

    Accepts either a 64-character hex string in any case (as returned by
    :func:`kbutillib.domains.identity.standardizers.entity_hash` /
    ``content_hash``) or 32 raw bytes, and returns the 64-character
    **lowercase** hex string in both cases -- the exact value to bind as a
    query parameter against the STRING ``entity_hash`` column, never
    interpolated into SQL text. Lowercasing is the point: STRING equality
    is case-sensitive, so an uppercase digest matches no stored row.

    Args:
        value: A 64-character hex string (case-insensitive) or 32 raw
            bytes.

    Returns:
        The 64-character lowercase hex form.

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
        return value.hex()
    if isinstance(value, str):
        return _canonical_hex(value, caller="encode_entity_hash")
    raise ValueError(
        "encode_entity_hash: expected a str (hex) or bytes (raw), got "
        f"{type(value).__name__}."
    )


def decode_entity_hash(value: str) -> str:
    """Validate a stored ``entity_hash`` and return its canonical hex form.

    The column now holds hex, so decoding is validation plus normalisation:
    it confirms a value read back from the ``entity_hash`` column is a
    well-formed 64-character hex digest and returns it lowercased.
    Round-tripping ``decode_entity_hash(encode_entity_hash(hex_str))``
    reproduces ``hex_str.lower()`` exactly.

    Args:
        value: A 64-character hex string, as stored in the ``entity_hash``
            column or returned by :func:`encode_entity_hash`.

    Returns:
        The lowercase 64-character hex form.

    Raises:
        ValueError: If ``value`` is not a ``str``, is not 64 characters,
            or contains non-hex characters. Raw ``bytes`` are refused: a
            ``bytes`` value read from this column means a row was written
            under the retired ``BINARY`` schema, which is worth failing on
            rather than silently converting.
    """
    if not isinstance(value, str):
        raise ValueError(
            f"decode_entity_hash: expected str, got {type(value).__name__}."
        )
    return _canonical_hex(value, caller="decode_entity_hash")


def _canonical_hex(value: str, *, caller: str) -> str:
    """Validate a 64-character hex digest and return it lowercased."""
    if len(value) != _HASH_HEX_CHARS:
        raise ValueError(
            f"{caller}: expected a {_HASH_HEX_CHARS}-character hex string, "
            f"got length {len(value)}."
        )
    try:
        bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError(f"{caller}: {value!r} is not a valid hex string.") from exc
    return value.lower()


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
            :func:`table_configs` (the fifteen clearinghouse tables). Tests
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
