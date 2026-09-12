"""The real ``bootstrap()`` capability adapter, wrapping :class:`BerdlCapability`.

``clearinghouse_schema.bootstrap()`` is the sanctioned, idempotent
table-creation entry point for the three clearinghouse Iceberg tables
(``entity``, ``canonical_content``, ``result``). Per the module-level note
above ``bootstrap()`` in :mod:`~kbutillib.domains.kbase.berdl.clearinghouse_schema`,
it requires its injected ``capability`` argument to expose three methods:

- ``load(*, dataset, tables, namespace, tenant=None, pipeline_name=None, ...)``
- ``table_exists(name, namespace=namespace) -> bool`` -- read-only.
- ``table_partition_spec(name, namespace=namespace) -> list[str] | None``
  -- read-only.

The real :class:`~kbutillib.domains.kbase.berdl.capability.BerdlCapability`
implements only ``load()`` -- passing a bare ``BerdlCapability()`` into
``bootstrap()`` raises ``AttributeError`` (wrapped by ``bootstrap()`` into
``BootstrapIndeterminateStateError``, since the lookup happens inside its
existence-check ``try``/``except``). :class:`ClearinghouseBootstrapCapability`
is the adapter that closes that gap: it **wraps** a ``BerdlCapability`` --
it does not subclass it, and this module never modifies ``capability.py``,
``transports.py``, or ``clearinghouse_schema.py``.

Why ``table_exists`` must delegate to the *same* probe ``load()`` uses:
``BerdlCapability.load()`` resolves existence with
``transport.table_exists(load_spark, name, namespace=namespace)`` (see
``capability.py`` around the per-table existence check, right before
``select_write_mode``). If this adapter's guard consulted a *different*
oracle than the write it guards -- e.g. a hand-rolled ``SHOW TABLES``
query -- the two could disagree, and the guard would be worthless. So the
adapter resolves exactly one Spark session, holds it, and uses it for both
the ``table_exists`` probe and (forwarded as ``spark=``) the wrapped
``load()`` call, so the probe and the write see one session.

Why ``table_partition_spec`` must never fall back to ``[]``/``None`` on
unparseable output: ``bootstrap()`` treats both ``None`` and ``[]`` as
"unpartitioned", and ``canonical_content`` is *genuinely* unpartitioned.
An adapter that returned ``[]`` when it failed to understand a catalog
response would silently pass ``canonical_content``'s partition check while
having determined nothing at all -- defeating the one safety check
``bootstrap()`` exists to provide. So this module raises
:class:`PartitionSpecUnparseableError` on any catalog output it does not
positively recognise, and returns ``[]`` only when the catalog positively
reports a table with no partition columns.

Everything in this module except the single
``capability.query(sql, engine="spark")`` call inside
:meth:`ClearinghouseBootstrapCapability.table_partition_spec` is a pure,
module-level function: a SQL emitter per recognised catalog probe shape,
a parser per shape, and a dispatch across both. None of them perform I/O,
hold a session, or import a pod-only package at module scope -- this
module imports cleanly off-pod, same as ``clearinghouse_schema.py``.

Two recognised catalog output shapes for a live partition spec (neither
verifiable off-pod, same caveat as the rest of this codebase's BERDL
modules -- see ``clearinghouse_schema.py``'s own module-level note):

- Spark's ``DESCRIBE TABLE EXTENDED`` on an Iceberg table: column
  definitions, then (only when partitioned) a ``# Partitioning`` header
  followed by numbered ``Part <N>`` rows naming each partition column (or
  transform expression), then a ``# Detailed Table Information`` header.
- ``SHOW CREATE TABLE``: a single-row, single-column DDL string containing
  a ``PARTITIONED BY (col_a, col_b)`` clause when partitioned.

A partition *transform* expression such as ``bucket(256, entity_hash)``
is not expressible through this codebase's write path (see
``clearinghouse_schema.py``'s no-parenthesis rule) -- if either parser
finds one, it raises immediately rather than reducing it to a bare column
name.
"""

from __future__ import annotations

import re
from typing import Any, NoReturn, Sequence

from .transports import InPodTransport

#: Header line Spark's ``DESCRIBE TABLE EXTENDED`` prints ahead of the
#: numbered ``Part <N>`` rows, only when the table is partitioned.
_PARTITIONING_HEADER = "# Partitioning"

#: Header line Spark's ``DESCRIBE TABLE EXTENDED`` always prints, whether
#: or not the table is partitioned -- its presence is what distinguishes
#: "this is a DESCRIBE TABLE EXTENDED result that says unpartitioned" from
#: "this is not a DESCRIBE TABLE EXTENDED result at all".
_DETAILED_INFO_HEADER = "# Detailed Table Information"

#: Matches a ``DESCRIBE TABLE EXTENDED`` partitioning row's first field,
#: e.g. ``"Part 0"``.
_PART_ROW_PATTERN = re.compile(r"^Part\s+(\d+)$")

#: Substring (case-insensitive) identifying a ``SHOW CREATE TABLE`` result.
_CREATE_TABLE_MARKER = "CREATE TABLE"

#: Matches the start of a ``PARTITIONED BY (`` clause in DDL text.
_PARTITIONED_BY_RE = re.compile(r"PARTITIONED BY\s*\(", re.IGNORECASE)


class PartitionSpecUnparseableError(RuntimeError):
    """Raised when a live table's partition spec cannot be positively determined.

    Raised on any catalog output that is not recognisable as either of
    this module's two known shapes, or that *is* recognisable but names a
    partition transform expression this codebase cannot express as a bare
    ``partition_by`` column. Never converted to ``[]`` or ``None`` --
    either would be silently indistinguishable from a genuinely
    unpartitioned table (the ``canonical_content`` trap), defeating
    ``bootstrap()``'s one safety check before it runs.
    """


def _row_get(row: Any, index: int) -> Any:
    """Positionally read one field off a row-like object, or ``None``.

    Works uniformly whether ``row`` is a plain ``tuple``/``list`` (as used
    in this module's tests) or a real ``pyspark.sql.Row`` (a tuple
    subclass supporting the same positional indexing) -- callers never
    need to know which.
    """
    try:
        return row[index]
    except (IndexError, TypeError, KeyError):
        return None


def _raise_unparseable(name: str, namespace: str, raw: Any, *, reason: str) -> NoReturn:
    """Raise :class:`PartitionSpecUnparseableError` naming the table and
    quoting the first ~200 characters of the raw catalog output, so the
    operator can fix it in one round trip.
    """
    snippet = repr(raw)
    if len(snippet) > 200:
        snippet = snippet[:200] + "...<truncated>"
    raise PartitionSpecUnparseableError(
        f"table_partition_spec: could not determine the live partition "
        f"spec of table {namespace!r}.{name!r}: {reason}. Refusing to "
        "fall back to [] or None here -- either would be silently "
        "indistinguishable from a genuinely unpartitioned table (the "
        "canonical_content trap), which would defeat bootstrap()'s "
        "partition-spec safety check without ever running it. First ~200 "
        f"chars of the raw catalog output: {snippet}"
    )


# --------------------------------------------------------------------------
# Pure SQL emitters
# --------------------------------------------------------------------------


def _quote_fqn(name: str, namespace: str) -> str:
    """Backtick-quote ``namespace.name``, matching ``capability.py``'s
    own ``` `ns`.`table` ``` quoting for the postflight row-count query.
    """
    return f"`{namespace}`.`{name}`"


def _describe_table_extended_sql(name: str, namespace: str) -> str:
    """Emit ``DESCRIBE TABLE EXTENDED`` SQL for one table."""
    return f"DESCRIBE TABLE EXTENDED {_quote_fqn(name, namespace)}"


def _show_create_table_sql(name: str, namespace: str) -> str:
    """Emit ``SHOW CREATE TABLE`` SQL for one table."""
    return f"SHOW CREATE TABLE {_quote_fqn(name, namespace)}"


# --------------------------------------------------------------------------
# Pure parsers
# --------------------------------------------------------------------------


def _parse_describe_table_extended_partition_spec(
    rows: Sequence[Any], *, name: str, namespace: str
) -> list[str] | None:
    """Parse a ``DESCRIBE TABLE EXTENDED`` result's partitioning block.

    Args:
        rows: The collected rows from running :func:`_describe_table_extended_sql`
            via ``capability.query(sql, engine="spark")`` -- any sequence
            of row-like objects supporting positional ``row[0]``/``row[1]``
            access.
        name: Table name, used only to build an error message.
        namespace: Namespace, used only to build an error message.

    Returns:
        ``None`` if ``rows`` is not recognisable as this shape at all (no
        ``# Detailed Table Information`` header found -- the caller should
        try the next recognised shape). ``[]`` if the shape *is*
        recognised and the table is positively reported as unpartitioned
        (no ``# Partitioning`` header). Otherwise the partition column
        names, in declared (``Part <N>``) order.

    Raises:
        PartitionSpecUnparseableError: A ``Part`` row's value contains a
            parenthesis -- a partition *transform* expression (e.g.
            ``bucket(256, entity_hash)``) rather than a bare column name.
            Raised immediately; never silently reduced to a bare column
            name and never allows falling through to try the other shape.
    """
    saw_detailed_info = False
    saw_partitioning = False
    parts_by_index: dict[int, str] = {}

    for row in rows:
        raw_col_name = _row_get(row, 0)
        col_name = "" if raw_col_name is None else str(raw_col_name).strip()

        if col_name == _DETAILED_INFO_HEADER:
            saw_detailed_info = True
            continue
        if col_name == _PARTITIONING_HEADER:
            saw_partitioning = True
            continue

        match = _PART_ROW_PATTERN.match(col_name)
        if match is None:
            continue

        raw_value = _row_get(row, 1)
        value = "" if raw_value is None else str(raw_value).strip()
        if "(" in value or ")" in value:
            _raise_unparseable(
                name,
                namespace,
                rows,
                reason=(
                    f"partition field {col_name!r} is a transform "
                    f"expression ({value!r}), not a bare column name -- "
                    "this codebase cannot express partition transforms "
                    "(see clearinghouse_schema.py's no-parenthesis rule)"
                ),
            )
        parts_by_index[int(match.group(1))] = value

    if not saw_detailed_info:
        return None
    if not saw_partitioning:
        return []
    return [parts_by_index[i] for i in sorted(parts_by_index)]


def _extract_balanced_parens(text: str, open_paren_index: int) -> str:
    """Return the text strictly between a matched ``(`` ``)`` pair.

    ``text[open_paren_index]`` must be ``'('``. Tracks nesting depth so a
    transform expression's own internal parentheses (e.g.
    ``bucket(256, entity_hash)``) don't terminate the match early.

    Raises:
        ValueError: The parentheses starting at ``open_paren_index`` are
            never closed.
    """
    depth = 0
    for i in range(open_paren_index, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren_index + 1 : i]
    raise ValueError("_extract_balanced_parens: unbalanced parentheses")


def _split_top_level_commas(text: str) -> list[str]:
    """Split ``text`` on commas that are not nested inside parentheses.

    A naive ``str.split(",")`` would break a transform expression like
    ``bucket(256, entity_hash)`` into two pieces at its internal comma;
    this tracks paren depth so only top-level commas split.
    """
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in text:
        if ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


def _parse_show_create_table_partition_spec(
    rows: Sequence[Any], *, name: str, namespace: str
) -> list[str] | None:
    """Parse a ``SHOW CREATE TABLE`` result's ``PARTITIONED BY`` clause.

    Args:
        rows: The collected rows from running :func:`_show_create_table_sql`
            via ``capability.query(sql, engine="spark")`` -- expected to be
            a single row whose first field is the full DDL text.
        name: Table name, used only to build an error message.
        namespace: Namespace, used only to build an error message.

    Returns:
        ``None`` if ``rows`` is not recognisable as this shape at all (not
        exactly one row, or that row's first field is not a string
        containing ``"CREATE TABLE"``). ``[]`` if the shape *is*
        recognised and the DDL text has no ``PARTITIONED BY`` clause at
        all (a positive "unpartitioned" report). Otherwise the partition
        column names, in the order they appear in the clause.

    Raises:
        PartitionSpecUnparseableError: The ``PARTITIONED BY (...)`` clause
            has unbalanced parentheses, or one of its comma-separated
            entries contains a parenthesis -- a partition transform
            expression rather than a bare column name.
    """
    if not rows or len(rows) != 1:
        return None
    text = _row_get(rows[0], 0)
    if not isinstance(text, str) or _CREATE_TABLE_MARKER not in text.upper():
        return None

    match = _PARTITIONED_BY_RE.search(text)
    if match is None:
        # A recognisable, well-formed CREATE TABLE statement with no
        # PARTITIONED BY clause at all is a positive unpartitioned report.
        return []

    open_paren_index = match.end() - 1
    try:
        inner = _extract_balanced_parens(text, open_paren_index)
    except ValueError:
        _raise_unparseable(
            name,
            namespace,
            rows,
            reason="the PARTITIONED BY clause has unbalanced parentheses",
        )

    columns: list[str] = []
    for raw_item in _split_top_level_commas(inner):
        item = raw_item.strip()
        if not item:
            continue
        if "(" in item or ")" in item:
            _raise_unparseable(
                name,
                namespace,
                rows,
                reason=(
                    f"PARTITIONED BY entry {item!r} is a transform "
                    "expression, not a bare column name -- this codebase "
                    "cannot express partition transforms (see "
                    "clearinghouse_schema.py's no-parenthesis rule)"
                ),
            )
        columns.append(item.strip("`"))
    return columns


def _parse_partition_spec(
    rows: Sequence[Any], *, name: str, namespace: str
) -> list[str]:
    """Try both recognised catalog shapes; raise if neither matches.

    Tries :func:`_parse_describe_table_extended_partition_spec` first,
    then :func:`_parse_show_create_table_partition_spec`. Each returns
    ``None`` when ``rows`` is not recognisable as its shape at all (as
    opposed to recognisable-and-positively-unpartitioned, which is
    ``[]``); this dispatch keeps trying only in that ``None`` case.

    Raises:
        PartitionSpecUnparseableError: Neither shape recognised ``rows``
            at all, or a recognised shape's own parser raised (a
            transform expression, or unbalanced parentheses) -- that
            raise propagates unchanged, never converted to a "try the
            next shape" ``None``.
    """
    for parser in (
        _parse_describe_table_extended_partition_spec,
        _parse_show_create_table_partition_spec,
    ):
        result = parser(rows, name=name, namespace=namespace)
        if result is not None:
            return result
    _raise_unparseable(
        name,
        namespace,
        rows,
        reason=(
            "the catalog output did not match either recognised shape (a "
            "DESCRIBE TABLE EXTENDED partitioning block or a SHOW CREATE "
            "TABLE PARTITIONED BY clause)"
        ),
    )


# --------------------------------------------------------------------------
# The adapter
# --------------------------------------------------------------------------


class ClearinghouseBootstrapCapability:
    """Wraps a :class:`BerdlCapability` to satisfy ``bootstrap()``'s contract.

    Does **not** subclass ``BerdlCapability`` and does not modify
    ``capability.py``, ``transports.py``, or ``clearinghouse_schema.py`` --
    it wraps an existing capability instance and exposes exactly the three
    methods ``bootstrap()`` requires: :meth:`load`, :meth:`table_exists`,
    and :meth:`table_partition_spec`.

    Args:
        capability: The wrapped :class:`~kbutillib.domains.kbase.berdl.capability.BerdlCapability`
            (or anything exposing the same ``load()``/``query()``
            surface -- tests inject a fake).
        spark: An existing Spark session to use for every probe and for
            the wrapped ``load()`` call. When not given, one is obtained
            lazily (on first use, not in ``__init__``) from ``transport``.
        transport: The transport used for the ``table_exists`` probe.
            When not given, an :class:`~kbutillib.domains.kbase.berdl.transports.InPodTransport`
            is constructed lazily (on first use, not in ``__init__`` --
            so simply constructing this adapter never requires the
            pod-only ``berdl_notebook_utils`` package).
    """

    def __init__(
        self,
        capability: Any,
        *,
        spark: Any = None,
        transport: Any = None,
    ) -> None:
        self._capability = capability
        self._spark = spark
        self._transport = transport

    def _get_transport(self) -> Any:
        """Return the transport, constructing an ``InPodTransport`` lazily."""
        if self._transport is None:
            self._transport = InPodTransport()
        return self._transport

    def _get_spark(self) -> Any:
        """Return the one Spark session shared by every probe and ``load()``.

        Resolved once, on first use, and cached -- this is what makes the
        ``table_exists`` probe and the wrapped ``load()`` call see the
        identical session object.
        """
        if self._spark is None:
            self._spark = self._get_transport().spark_session()
        return self._spark

    def load(
        self,
        *,
        dataset: str,
        tables: Sequence[Any],
        namespace: str,
        tenant: str | None = None,
        pipeline_name: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Forward to the wrapped capability's ``load()``.

        Passes this adapter's single resolved Spark session as ``spark=``
        -- the same session object :meth:`table_exists` probes with --
        overriding any ``spark`` the caller supplied, so the probe and the
        write can never end up looking at two different sessions.
        """
        kwargs.pop("spark", None)
        return self._capability.load(
            dataset=dataset,
            tables=tables,
            namespace=namespace,
            tenant=tenant,
            pipeline_name=pipeline_name,
            spark=self._get_spark(),
            **kwargs,
        )

    def table_exists(self, name: str, *, namespace: str) -> bool:
        """Read-only existence probe, delegating to the transport's own check.

        Calls ``transport.table_exists(spark, name, namespace=namespace)``
        -- the exact probe :meth:`~kbutillib.domains.kbase.berdl.capability.BerdlCapability.load`
        itself uses to resolve create-vs-append -- rather than a
        hand-rolled query, so this guard can never disagree with the write
        it guards.

        Any exception raised by the transport propagates unchanged: it is
        never caught and converted to ``False``. ``bootstrap()`` is the
        caller responsible for turning an exception here into
        ``BootstrapIndeterminateStateError`` with nothing written; this
        method must never pre-empt that by silently reporting "does not
        exist" for a lookup that actually failed.
        """
        transport = self._get_transport()
        return transport.table_exists(self._get_spark(), name, namespace=namespace)

    def table_partition_spec(self, name: str, *, namespace: str) -> list[str]:
        """Read-only lookup of a live table's partition column names.

        Makes exactly one ``capability.query(sql, engine="spark")`` call
        (``engine="spark"`` explicitly, since ``query()`` otherwise
        defaults to Trino in-pod, and the Iceberg catalog metadata this
        probe reads is Spark-side) and parses the result with
        :func:`_parse_partition_spec`, which raises
        :class:`PartitionSpecUnparseableError` -- never returns ``[]`` or
        ``None`` as a fallback -- on any catalog output it does not
        positively recognise.
        """
        sql = _describe_table_extended_sql(name, namespace)
        rows = self._capability.query(sql, engine="spark")
        return _parse_partition_spec(rows, name=name, namespace=namespace)
