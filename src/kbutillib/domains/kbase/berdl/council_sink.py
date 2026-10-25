"""The row-level READ/WRITE seam over ``kbaseincubator.council``.

:mod:`council_schema` declares the nine archived tables and creates nothing.
This module is the narrow seam everything that actually moves ROWS goes
through -- the disposition layer (:mod:`council_disposition`), the archiver
(:mod:`council_archive`) and the publication bundle
(``genome.ontology_mapping.council_bundle``). Each of those talks to a SINK
and never to a transport, which is what makes all three testable without a
BERDL pod.

THE SINK CONTRACT, which is deliberately tiny -- four methods:

* ``bootstrap()`` -- create-or-verify the tables, idempotently.
* ``append_rows(table, rows)`` -- append dict rows. NEVER overwrites, never
  replaces; the council namespace is append-only by snapshot version.
* ``select_rows(table, *, where=None, columns=None, order_by=None)`` -- read
  rows back as dicts.
* ``count_rows(table, *, where=None)`` -- a row count.

Only ``where`` equality (and ``IN`` for a sequence value) is expressible.
That is not a simplification that will need widening later: every read in
this namespace is keyed on some prefix of
``(store_id, snapshot_id, <source pk>)``, and a sink that accepted arbitrary
SQL would be a second place that could format a value into query text --
which is the failure ``clearinghouse_capability``'s Rule 4 exists to stop
(``STRING`` equality is case-sensitive, so a mis-encoded bind matches zero
rows, reports "empty", and fails OPEN into redoing work already done).

TWO IMPLEMENTATIONS, both real:

* :class:`SqliteCouncilSink` -- the nine tables materialised in ONE LOCAL
  SQLITE FILE from ``council_schema``'s own ``schema_sql`` DDL. This is not a
  test double: it is how an archive is staged and verified off-pod, on h100,
  where there is no Spark. It is also what the tests use, so they exercise
  real DDL, real appends and real SQL reads rather than a dict standing in
  for a table.
* :class:`BerdlCouncilSink` -- the pod sink. Appends through
  ``BerdlCapability.load(dataframes=...)`` and reads through
  ``BerdlCapability.query()``; it refuses off-pod exactly as that capability
  does, and NOTHING here has been run against a live pod (KBUtilLib#91).

WHAT SQLITE IS NOT. It is not Iceberg: no partition specs, no snapshot
history, no ``data_lakehouse_ingest`` type map, and its type affinities are
permissive where Iceberg's are not. Agreement between the two is NOT
established by any test in this repo and may not be claimed; the SQLite sink
proves the SQL and the row shapes, and live acceptance of this namespace on
kbhub is an operator-owned step. The same caveat
``tests/berdl/test_council_schema.py`` records for its DDL sink applies here
verbatim.

TYPES ACROSS THE SEAM. ``TIMESTAMP`` columns are written as ISO-8601 strings
and read back as strings; ``dict``/``list`` values destined for a ``STRING``
column (every store JSON column is declared ``STRING``) are serialised with
``json.dumps(..., sort_keys=True)`` so one row's bytes do not depend on dict
iteration order. Both conversions happen at the sink boundary and in one
place, which is the only way the on-disk and in-lakehouse copies of a bundle
can be compared at all.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .council_schema import (
    ARCHIVED_TABLES,
    COUNCIL_NAMESPACE,
    NAMESPACE,
    TENANT,
    column_names,
    table_configs,
    table_name,
)

__all__ = [
    "CouncilSinkError",
    "UnknownColumnError",
    "encode_value",
    "encode_row",
    "SqliteCouncilSink",
    "BerdlCouncilSink",
    "open_sink",
]


class CouncilSinkError(RuntimeError):
    """A sink could not do what it was asked."""


class UnknownColumnError(CouncilSinkError):
    """A row or a filter named a column the archived table does not declare.

    Refused rather than dropped. A silently-dropped column is the failure
    mode that matters here: the archived tables are append-only and a row
    written without ``live`` (or with a typo'd ``snapshot_seq``) is
    indistinguishable from a correct one until somebody queries it, by which
    time rewriting it is a full table rewrite.
    """


# --------------------------------------------------------------------------
# Value encoding -- one place, both sinks
# --------------------------------------------------------------------------


def encode_value(value: Any) -> Any:
    """Return *value* in the form a council table stores.

    ``datetime``/``date`` become ISO-8601 strings (the ``TIMESTAMP`` columns
    round-trip as text through both sinks); ``dict``/``list``/``tuple``
    become ``json.dumps(..., sort_keys=True)`` because every store JSON
    column is declared ``STRING`` and a row's bytes must not depend on dict
    iteration order. Everything else passes through untouched -- in
    particular ``None`` stays ``None``, so a NULL stays a NULL and never
    becomes the string ``'null'`` (the trap ``calls.reply_parsed`` already
    is: ``sa.JSON`` with ``none_as_null=False`` stores Python ``None`` as the
    TEXT ``'null'``, so ``IS NOT NULL`` is true for every row).
    """
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True, default=str)
    if isinstance(value, bool):
        return value
    return value


def encode_row(table: str, row: Mapping[str, Any]) -> dict[str, Any]:
    """Return *row* encoded and column-checked for *table*.

    Every declared column is present in the result (missing ones as
    ``None``), in DDL order, so a batch of rows has one uniform shape no
    matter how the caller built it.

    Raises:
        UnknownColumnError: *row* names a column *table* does not declare.
    """
    declared = column_names(table)
    unknown = sorted(set(row) - set(declared))
    if unknown:
        raise UnknownColumnError(
            f"{table}: row names column(s) {unknown!r} that "
            f"{COUNCIL_NAMESPACE}.{table} does not declare. Declared: "
            f"{list(declared)!r}. Nothing was written -- a dropped column in "
            "an append-only table cannot be corrected without a full "
            "rewrite."
        )
    return {name: encode_value(row.get(name)) for name in declared}


def _check_filter(table: str, where: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate a ``where`` mapping's column names against *table*."""
    if not where:
        return {}
    declared = set(column_names(table))
    unknown = sorted(set(where) - declared)
    if unknown:
        raise UnknownColumnError(
            f"{table}: filter names column(s) {unknown!r} that "
            f"{COUNCIL_NAMESPACE}.{table} does not declare."
        )
    return dict(where)


# --------------------------------------------------------------------------
# The local SQL sink
# --------------------------------------------------------------------------


class SqliteCouncilSink:
    """The nine council tables in one local SQLite file, from the real DDL.

    Created from ``council_schema.table_configs()``: each table's
    ``schema_sql`` is executed as ``CREATE TABLE`` DDL, so a malformed
    fragment, a duplicate column or a column list that disagrees with the
    declarations fails here for real.

    NO PRIMARY KEY IS DECLARED, matching the namespace: ``schema_sql`` emits
    no constraints and Iceberg tables written through
    ``data_lakehouse_ingest`` declare none either, so ``table_primary_key``
    is a contract the WRITERS honour, not something the storage enforces.
    Pinning it here would make this sink stricter than the thing it stands
    for, and a test would then pass against a sink that refuses what the
    lakehouse accepts.

    Args:
        path: The SQLite file to use. ``":memory:"`` is accepted.
        namespace: Prefix for the physical table names, so one file can hold
            a disposable namespace beside the real one. Defaults to
            :data:`~.council_schema.NAMESPACE`.
    """

    def __init__(self, path: Any, *, namespace: str = NAMESPACE) -> None:
        self.namespace = str(namespace)
        text = str(path)
        self.path: Path | None = None if text == ":memory:" else Path(text).expanduser()
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            ":memory:" if self.path is None else str(self.path)
        )
        self._connection.row_factory = sqlite3.Row

    # -- lifecycle ------------------------------------------------------

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "SqliteCouncilSink":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def physical_name(self, table: str) -> str:
        """``<namespace>__<table>`` -- the one place a name is composed."""
        return f"{self.namespace}__{table_name(table)}"

    def bootstrap(self, tables: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
        """Create every council table that does not exist yet. Idempotent."""
        configs = list(table_configs() if tables is None else tables)
        created: list[str] = []
        with self._connection:
            for config in configs:
                physical = self.physical_name(str(config["name"]))
                self._connection.execute(
                    f'CREATE TABLE IF NOT EXISTS "{physical}" ({config["schema_sql"]})'
                )
                created.append(str(config["name"]))
        return {"namespace": self.namespace, "tables": created}

    def table_exists(self, table: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (self.physical_name(table),),
        ).fetchone()
        return row is not None

    def catalog_columns(self, table: str) -> tuple[str, ...]:
        """One created table's columns, read back OUT OF the database."""
        rows = self._connection.execute(
            f'PRAGMA table_info("{self.physical_name(table)}")'
        ).fetchall()
        return tuple(row["name"] for row in rows)

    # -- the contract ---------------------------------------------------

    def append_rows(self, table: str, rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        """Append *rows* to *table*. Never replaces, never overwrites."""
        encoded = [encode_row(table, row) for row in rows]
        if not encoded:
            return {"table": table_name(table), "rows": 0}
        names = column_names(table)
        placeholders = ", ".join("?" * len(names))
        statement = (
            f'INSERT INTO "{self.physical_name(table)}" '
            f"({', '.join(names)}) VALUES ({placeholders})"
        )
        with self._connection:
            self._connection.executemany(
                statement, [tuple(row[name] for name in names) for row in encoded]
            )
        return {"table": table_name(table), "rows": len(encoded)}

    def _predicate(
        self, table: str, where: Mapping[str, Any] | None
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for name, value in _check_filter(table, where).items():
            if isinstance(value, (list, tuple, set, frozenset)):
                members = [encode_value(member) for member in value]
                if not members:
                    # An empty IN set matches nothing; say so in SQL rather
                    # than emitting `IN ()`, which is a syntax error.
                    return " WHERE 0", []
                clauses.append(f"{name} IN ({', '.join('?' * len(members))})")
                params.extend(members)
            elif value is None:
                clauses.append(f"{name} IS NULL")
            else:
                clauses.append(f"{name} = ?")
                params.append(encode_value(value))
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    def select_rows(
        self,
        table: str,
        *,
        where: Mapping[str, Any] | None = None,
        columns: Sequence[str] | None = None,
        order_by: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return rows of *table* matching *where*, as plain dicts."""
        wanted = tuple(columns) if columns else column_names(table)
        unknown = sorted(set(wanted) - set(column_names(table)))
        if unknown:
            raise UnknownColumnError(f"{table}: unknown column(s) {unknown!r}")
        predicate, params = self._predicate(table, where)
        order = ""
        if order_by:
            bad = sorted(set(order_by) - set(column_names(table)))
            if bad:
                raise UnknownColumnError(f"{table}: unknown order column(s) {bad!r}")
            order = " ORDER BY " + ", ".join(order_by)
        statement = (
            f"SELECT {', '.join(wanted)} FROM "
            f'"{self.physical_name(table)}"{predicate}{order}'
        )
        rows = self._connection.execute(statement, params).fetchall()
        return [{name: row[name] for name in wanted} for row in rows]

    def count_rows(self, table: str, *, where: Mapping[str, Any] | None = None) -> int:
        predicate, params = self._predicate(table, where)
        statement = (
            f'SELECT COUNT(*) AS n FROM "{self.physical_name(table)}"{predicate}'
        )
        return int(self._connection.execute(statement, params).fetchone()["n"])


# --------------------------------------------------------------------------
# The pod sink
# --------------------------------------------------------------------------


class BerdlCouncilSink:
    """The council namespace on the BERDL pod, through ``BerdlCapability``.

    COMPOSITION, NOT INHERITANCE -- the same posture
    ``ClearinghouseCapability`` takes: the capability is wrapped, so the raw
    ``query()``/``load()`` surface is not exposed on this object and a caller
    cannot reach past the seam to run its own un-encoded SQL.

    Appends go through ``load(dataframes=...)`` with ``mode='append'`` and
    never request ``'overwrite'``: the council namespace is append-only by
    snapshot version, and an overwrite would destroy the earlier snapshots
    that are the whole point of keying rows on ``snapshot_id``.

    NOT VERIFIED AGAINST A LIVE POD. ``BerdlCapability.load()`` raises
    ``BerdlLoadRefusedError`` off-pod and the BERDL transport has been broken
    on kbhub since the 2026-10-19 image (KBUtilLib#91), so every path here is
    unexercised end to end. Treat anything needing the pod as awaiting
    operator acceptance.

    Args:
        capability: A ``BerdlCapability`` (or anything exposing its
            ``load`` / ``query``).
        namespace: The Iceberg namespace to read and write. Defaults to
            :data:`~.council_schema.COUNCIL_NAMESPACE`.
        dataset: ``load()``'s top-level ``dataset``. Defaults to
            :data:`~.council_schema.NAMESPACE`.
        tenant: ``load()``'s ``tenant``. Defaults to
            :data:`~.council_schema.TENANT`.
    """

    def __init__(
        self,
        capability: Any,
        *,
        namespace: str = COUNCIL_NAMESPACE,
        dataset: str = NAMESPACE,
        tenant: str = TENANT,
        pipeline_name: str | None = None,
    ) -> None:
        self._capability = capability
        self.namespace = str(namespace)
        self.dataset = str(dataset)
        self.tenant = str(tenant)
        self.pipeline_name = pipeline_name

    def physical_name(self, table: str) -> str:
        return f"{self.namespace}.{table_name(table)}"

    def bootstrap(self, tables: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
        """Delegate to ``council_schema.bootstrap`` -- never a second copy."""
        from .council_schema import bootstrap as _bootstrap

        return _bootstrap(
            self._capability,
            namespace=self.namespace,
            tables=tables,
            dataset=self.dataset,
            tenant=self.tenant,
            pipeline_name=self.pipeline_name,
        )

    def append_rows(self, table: str, rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        """Append *rows* through ``load(dataframes=...)``, mode ``append``."""
        encoded = [encode_row(table, row) for row in rows]
        name = table_name(table)
        if not encoded:
            return {"table": name, "rows": 0}
        try:
            import pandas as pd
        except ImportError as exc:  # pragma: no cover - pandas is a pod dep
            raise CouncilSinkError(
                "appending to the council namespace needs pandas to build "
                "the DataFrame data_lakehouse_ingest consumes"
            ) from exc
        frame = pd.DataFrame(encoded, columns=list(column_names(table)))
        result = self._capability.load(
            dataset=self.dataset,
            tables=[{"name": name, "mode": "append"}],
            tenant=self.tenant,
            pipeline_name=self.pipeline_name,
            dataframes={name: frame},
            namespace=self.namespace,
        )
        return {"table": name, "rows": len(encoded), "load_result": result}

    def _predicate(
        self, table: str, where: Mapping[str, Any] | None
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for key, value in _check_filter(table, where).items():
            if isinstance(value, (list, tuple, set, frozenset)):
                members = [encode_value(member) for member in value]
                if not members:
                    return " WHERE 1 = 0", []
                clauses.append(f"{key} IN ({', '.join('?' * len(members))})")
                params.extend(members)
            elif value is None:
                clauses.append(f"{key} IS NULL")
            else:
                clauses.append(f"{key} = ?")
                params.append(encode_value(value))
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    def select_rows(
        self,
        table: str,
        *,
        where: Mapping[str, Any] | None = None,
        columns: Sequence[str] | None = None,
        order_by: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        wanted = tuple(columns) if columns else column_names(table)
        unknown = sorted(set(wanted) - set(column_names(table)))
        if unknown:
            raise UnknownColumnError(f"{table}: unknown column(s) {unknown!r}")
        predicate, params = self._predicate(table, where)
        order = ""
        if order_by:
            bad = sorted(set(order_by) - set(column_names(table)))
            if bad:
                raise UnknownColumnError(f"{table}: unknown order column(s) {bad!r}")
            order = " ORDER BY " + ", ".join(order_by)
        sql = (
            f"SELECT {', '.join(wanted)} FROM {self.physical_name(table)}"
            f"{predicate}{order}"
        )
        return self._rows(self._capability.query(sql, params=params), wanted)

    def count_rows(self, table: str, *, where: Mapping[str, Any] | None = None) -> int:
        predicate, params = self._predicate(table, where)
        sql = f"SELECT COUNT(*) AS n FROM {self.physical_name(table)}{predicate}"
        rows = self._rows(self._capability.query(sql, params=params), ("n",))
        return int(rows[0]["n"]) if rows else 0

    @staticmethod
    def _rows(result: Any, wanted: Sequence[str]) -> list[dict[str, Any]]:
        """Normalise whatever ``query()`` returned into a list of dicts.

        ``BerdlCapability.query`` answers with a pandas DataFrame on some
        engines and a list of row mappings on others, so the shape is
        resolved here rather than at each call site.
        """
        if result is None:
            return []
        to_dict = getattr(result, "to_dict", None)
        if callable(to_dict):
            return [dict(row) for row in to_dict("records")]
        return [{name: dict(row).get(name) for name in wanted} for row in result]


# --------------------------------------------------------------------------
# Resolving which sink a caller meant
# --------------------------------------------------------------------------

#: What a caller passes for ``target`` to mean "the real pod namespace".
POD_TARGET = "berdl"


def open_sink(target: Any, *, namespace: str | None = None) -> Any:
    """Return the sink *target* names.

    Args:
        target: :data:`POD_TARGET` (or ``None``) for the live
            :class:`BerdlCouncilSink`; anything else is a path and gives a
            :class:`SqliteCouncilSink` over that file, BOOTSTRAPPED. A local
            file is the normal case on h100, which has no Spark.
        namespace: Override the sink's namespace.

    Raises:
        CouncilSinkError: the pod sink was asked for and ``BerdlCapability``
            could not be constructed.
    """
    if target is None or str(target) == POD_TARGET:
        try:
            from .capability import BerdlCapability
        except ImportError as exc:  # pragma: no cover - pod dependency
            raise CouncilSinkError(
                "the BERDL council sink needs BerdlCapability, which is not "
                "importable here"
            ) from exc
        kwargs = {} if namespace is None else {"namespace": namespace}
        return BerdlCouncilSink(BerdlCapability(), **kwargs)
    kwargs = {} if namespace is None else {"namespace": namespace}
    sink = SqliteCouncilSink(target, **kwargs)
    sink.bootstrap()
    return sink
