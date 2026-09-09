"""Current-state SQL derivation over the clearinghouse's append-only ``result`` table.

The ``result`` table (:mod:`kbutillib.domains.kbase.berdl.clearinghouse_schema`)
is APPEND-ONLY: every annotation call by every tool is written as an
immutable row, and a superseded row is never deleted or updated in place.
The BERDL platform cannot create persistent database views there (the
Trino view path is broken, not merely absent), so "the current state" of
a slot cannot be materialized as a view -- it has to be a query this
module builds, and every consumer that reads ``result`` directly without
running that query does not get a stale answer, it gets a **wrong** one,
because every superseded row is still sitting in the table.

THE SEMANTICS. Current state for a slot -- a slot being one
``(entity_hash, result_type, source)`` triple -- is the row with the
greatest ``(observed_at, ingest_batch_id)`` within that slot. This module
implements that with a window function:
``ROW_NUMBER() OVER (PARTITION BY entity_hash, result_type, source ORDER
BY observed_at DESC, ingest_batch_id DESC)``, keeping only rows where that
number is ``1``.

Two things are deliberate and must never be "fixed":

- ``ingest_batch_id`` in the ``ORDER BY`` is not optional. Two appends can
  share an ``observed_at``, and without the second key "the current row"
  is genuinely ambiguous. It is a ULID assigned per commit, so it is
  monotonic and lexicographically ordered, and comparing it as an
  ordinary string ``ORDER BY`` column is correct.
- ``result_type_version`` is recorded on every row but is **not** part of
  the slot key. If it were, bumping a result-type schema would fork every
  slot in the corpus and nothing would ever supersede its predecessor
  again.

DIALECT AUTHORITY IS SPARK SQL, compatible with Iceberg-on-Spark -- Trino
is a read client only and is not authoritative here. Table identifiers
are backtick-quoted per dot-separated segment, matching the existing
convention in :meth:`~kbutillib.domains.kbase.berdl.capability.BerdlCapability.load`
(e.g. ```` `namespace`.`table` ````). The emitted SQL stays inside the
DuckDB/Spark syntax intersection deliberately: plain ``ROW_NUMBER() OVER
(...)``, ordinary ``WHERE``/``IN``, no ``NULLS FIRST``/``NULLS LAST`` (both
ordering columns are non-null by construction -- a row missing either is
rejected before write, so encoding null-handling here would be encoding a
state that must never reach the table), and no DuckDB-only BLOB literal
or timestamp-helper syntax.

:func:`current_state_sql` is pure: it holds no session, performs no I/O,
makes no network call, and imports nothing pod-only, which is what makes
it testable off-pod. This module's own test suite
(``tests/berdl/test_clearinghouse_derivation.py``) executes the SQL this
function returns against DuckDB as a SURROGATE engine -- DuckDB proves the
*logic* of the derivation (which row wins a slot, which scope excludes
which), not the Spark-on-Iceberg dialect the query actually runs under in
production. Whether this exact SQL text is valid and performant against
the live Spark-on-Iceberg cluster is an in-pod operator parity check, not
something provable off-pod.
"""

from __future__ import annotations

from .clearinghouse_schema import table_configs

__all__ = ["current_state_sql"]

#: The slot key: current state is one row per unique combination of these
#: columns. ``result_type_version`` is deliberately excluded -- see the
#: module docstring.
_SLOT_KEY_COLUMNS = ("entity_hash", "result_type", "source")

#: The columns that break ties within a slot, in ``ORDER BY`` precedence.
#: Both are non-null by construction; see the module docstring for why
#: this module never emits ``NULLS FIRST``/``NULLS LAST``.
_ORDER_COLUMNS = ("observed_at", "ingest_batch_id")


def _quote_fqn(fqn: str) -> str:
    """Backtick-quote each dot-separated segment of a table name.

    Matches the convention already used in ``BerdlCapability.load()``
    (``f"\\`{namespace}\\`.\\`{report['name']}\\`"``): every segment
    between dots gets its own pair of backticks, so a multi-part
    identifier such as ``tenant.namespace.table`` is never misread with a
    dot inside a segment as a qualifier boundary. A segment that already
    arrives backtick-quoted is not double-quoted.

    Args:
        fqn: A dot-separated table name, e.g.
            ``"kbaseincubator.clearinghouse.result"``. Segments may
            already be backtick-quoted.

    Returns:
        The fully backtick-quoted identifier, e.g.
        ```` `kbaseincubator`.`clearinghouse`.`result` ````.
    """
    return ".".join(f"`{segment.strip('`')}`" for segment in fqn.split("."))


def _result_columns() -> list[str]:
    """Column names of the ``result`` table, in DDL order.

    Derived from :func:`clearinghouse_schema.table_configs` rather than
    re-typed here, so this module and the schema module cannot silently
    drift apart -- per the task's instruction to reuse the previous
    phase's table/column names rather than re-typing them as string
    literals.
    """
    configs = {table["name"]: table for table in table_configs()}
    schema_sql = configs["result"]["schema_sql"]
    return [part.strip().split(" ", 1)[0] for part in schema_sql.split(",")]


def _quote_source_literal(source: str) -> str:
    """Single-quote a ``source`` value for embedding in the ``WHERE`` clause.

    ``source`` values are internal, operator-controlled strings
    (``<tool>/<version>``, per ``clearinghouse_schema``'s partitioning
    note) -- never end-user input -- so literal embedding with the
    standard SQL single-quote escape (``'`` -> ``''``) is the correct
    choice here: :func:`current_state_sql` returns SQL *text* and nothing
    else, so there is no side channel through which a separate
    bind-parameter list could travel back to the caller.
    """
    return "'" + source.replace("'", "''") + "'"


def current_state_sql(
    result_table_fqn: str,
    *,
    sources: list[str] | None = None,
) -> str:
    """Build the Spark SQL that derives clearinghouse current state.

    Selects, from the append-only ``result`` table, exactly one row per
    ``(entity_hash, result_type, source)`` slot: the row with the
    greatest ``(observed_at, ingest_batch_id)`` in that slot. See the
    module docstring for the full semantics and the two deliberate
    choices (``ingest_batch_id`` as a mandatory tie-break,
    ``result_type_version`` excluded from the slot key) that must not be
    "fixed".

    Args:
        result_table_fqn: The ``result`` table's fully qualified name,
            dot-separated (e.g. ``"kbaseincubator.clearinghouse.result"``).
            Each segment is backtick-quoted in the emitted SQL.
        sources: If given, pre-filters the underlying rows to only these
            ``source`` values before windowing (``source`` is the
            table's partition column, so this is a pruning filter, not
            merely a convenience one). ``None`` (the default) applies no
            filter. An empty list is a request to match zero sources, so
            it filters everything out (``WHERE 1 = 0``) rather than being
            treated as "no filter".

    Returns:
        SQL text implementing the derivation. Performs no I/O; holds no
        session; makes no network call.
    """
    columns = _result_columns()
    select_list = ",\n        ".join(columns)
    partition_by = ", ".join(_SLOT_KEY_COLUMNS)
    order_by = ", ".join(f"{col} DESC" for col in _ORDER_COLUMNS)
    table = _quote_fqn(result_table_fqn)

    if sources is None:
        where_clause = ""
    elif len(sources) == 0:
        where_clause = "\n    WHERE 1 = 0"
    else:
        in_list = ", ".join(_quote_source_literal(source) for source in sources)
        where_clause = f"\n    WHERE source IN ({in_list})"

    return (
        "WITH ranked AS (\n"
        "    SELECT\n"
        f"        {select_list},\n"
        "        ROW_NUMBER() OVER (\n"
        f"            PARTITION BY {partition_by}\n"
        f"            ORDER BY {order_by}\n"
        "        ) AS rn\n"
        f"    FROM {table}"
        f"{where_clause}\n"
        ")\n"
        f"SELECT {select_list}\n"
        "FROM ranked\n"
        "WHERE rn = 1"
    )
