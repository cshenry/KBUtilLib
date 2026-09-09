#!/usr/bin/env python3
"""OP3 -- prove ``current_state_sql()`` against the real Spark/Iceberg engine.

RUN THIS ONLY FROM INSIDE THE BERDL JUPYTERHUB POD (host ``kbhub``), and only
after OP1 (the three-package import check) and OP2 (table creation) have
both succeeded -- see
``agent-io/docs/clearinghouse-schema-operator-runbook.md``. Every pod-only
import (``pyspark``, and transitively ``berdl_notebook_utils`` /
``data_lakehouse_ingest`` via :class:`BerdlCapability`) is deferred inside
functions, exactly like the rest of this codebase's off-pod-safe modules
(see ``capability.py``'s own deferred ``from data_lakehouse_ingest import
ingest``) -- so importing this module, and even calling :func:`main`, are
safe to attempt off-pod: it fails fast and legibly (locus check, then
``BerdlLoadRefusedError``) rather than at a bare ``ModuleNotFoundError``
deep in an unrelated stack frame.

WHAT THIS PROVES, AND WHAT IT DOES NOT.
``tests/berdl/test_clearinghouse_derivation.py`` executes
:func:`current_state_sql`'s SQL against DuckDB as a SURROGATE engine -- it
proves the derivation's *logic* (which row wins a slot, which scope
excludes which), never the Spark-on-Iceberg dialect the query actually
runs under in production. This script is the one thing in the whole
change that exercises the real dependency: it appends a small, clearly
marked fixture to the REAL, live ``result`` table through the sanctioned
write path (:meth:`BerdlCapability.load`, which is what applies schema
enforcement -- never a raw ``pyiceberg`` write; see the runbook's "if
something goes wrong" section for why that shortcut is refused even under
failure pressure), runs the real ``current_state_sql()`` SQL text against
it via Spark, and asserts the SAME six properties the DuckDB tests assert.

THE FIXTURE IS SHARED, NOT RE-TYPED. Every row this script appends comes
from :mod:`kbutillib.domains.kbase.berdl.clearinghouse_parity_fixture` --
the exact same module ``tests/berdl/test_clearinghouse_derivation.py``
imports its rows from. A parity check run against a hand-retyped lookalike
fixture would prove nothing the moment the two fixtures drifted apart;
importing one shared module is what keeps that impossible.

THE FIXTURE IS PERMANENT AND HARMLESS. Every fixture row's ``source``
carries the ``parity-check/`` prefix (see that module's
``PARITY_SOURCE_PREFIX``), so these rows can never be mistaken for real
tool output and can be found again later with a simple ``source LIKE
'parity-check/%'`` filter. They are expected to remain in the append-only
``result`` table forever -- re-running this script appends *more* rows to
the same fixture slots, which changes nothing about the current-state
answer for those slots (same ``entity_hash``/``result_type``/``source``,
so still the same slot; newest ``(observed_at, ingest_batch_id)`` still
wins). They can never shadow, or be shadowed by, a real annotation's slot.

Usage (from a kbhub notebook cell or terminal), after confirming OP1 and
OP2 in the runbook:

    python scripts/clearinghouse_parity_check.py

Prints one PASS/FAIL line per property and a final summary line. Exits 0
if all six pass, 1 if any fails, 2 if run off-pod.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from kbutillib.domains.kbase.berdl.capability import POD_MACHINE, BerdlCapability
from kbutillib.domains.kbase.berdl.clearinghouse_derivation import current_state_sql
from kbutillib.domains.kbase.berdl.clearinghouse_parity_fixture import (
    ALL_PARITY_ROWS,
    PARITY_CASES,
    ParityCase,
)
from kbutillib.domains.kbase.berdl.clearinghouse_schema import (
    NAMESPACE,
    TENANT,
    table_configs,
)

#: The ``result`` table's fully qualified name, as passed to
#: :func:`current_state_sql`. :mod:`clearinghouse_derivation`'s own
#: docstring uses the three-part, tenant-qualified form
#: (``"kbaseincubator.clearinghouse.result"``) as its worked example, so
#: that is what this script defaults to. NOTE, per the runbook's OP2
#: namespace-resolution warning: ``BerdlCapability.load()``'s own
#: postflight row-count/history queries use a *two-part* form instead
#: (bare ``namespace.table``, no tenant segment -- see ``capability.py``,
#: ``load()``'s postflight block). Which form actually resolves against
#: the live Iceberg catalog is not verifiable off-pod. If this script's
#: query step fails to find the table under the three-part form, retry
#: with the two-part form (``f"{NAMESPACE}.result"``) before assuming
#: anything else is wrong -- and note in your parity-check record which
#: form worked, since the next operator will hit the same fork.
RESULT_TABLE_FQN = f"{TENANT}.{NAMESPACE}.result"

#: Column order for rows returned from the Spark query, matching
#: ``clearinghouse_schema``'s ``result`` table declaration.
_RESULT_COLUMNS = (
    "entity_hash",
    "result_type",
    "source",
    "result_type_version",
    "payload",
    "observed_at",
    "ingest_batch_id",
)

def _build_fixture_dataframe(spark: Any):
    """Build the Spark DataFrame of every parity fixture row.

    Deferred pyspark import -- see module docstring. Parses each fixture
    row's ``observed_at`` string into a real ``datetime`` and each
    ``payload`` dict into its JSON-text form, matching the ``result``
    table's declared ``TIMESTAMP``/``STRING`` column types (see
    ``clearinghouse_schema._RESULT_COLUMNS``) -- an explicit schema is
    built here rather than left to inference precisely so this doesn't
    silently write ``observed_at`` as a string.
    """
    from datetime import datetime

    from pyspark.sql import Row
    from pyspark.sql.types import (
        BinaryType,
        StringType,
        StructField,
        StructType,
        TimestampType,
    )

    type_by_sql_name = {
        "BINARY": BinaryType(),
        "STRING": StringType(),
        "TIMESTAMP": TimestampType(),
    }
    result_config = next(t for t in table_configs() if t["name"] == "result")
    columns = [
        part.strip().split(" ", 1)
        for part in result_config["schema_sql"].split(",")
    ]
    schema = StructType(
        [
            StructField(name, type_by_sql_name[sql_type], nullable=False)
            for name, sql_type in columns
        ]
    )

    rows = [
        Row(
            entity_hash=row["entity_hash"],
            result_type=row["result_type"],
            source=row["source"],
            result_type_version=row["result_type_version"],
            payload=json.dumps(row["payload"]),
            observed_at=datetime.strptime(row["observed_at"], "%Y-%m-%d %H:%M:%S"),
            ingest_batch_id=row["ingest_batch_id"],
        )
        for row in ALL_PARITY_ROWS
    ]
    return spark.createDataFrame(rows, schema=schema)


def _append_fixture_rows(capability: BerdlCapability, spark: Any) -> dict[str, Any]:
    """Append every parity fixture row to the real ``result`` table.

    Goes through :meth:`BerdlCapability.load` (-> ``data_lakehouse_ingest.
    ingest``), the same sanctioned write path OP2 uses to create the
    tables in the first place -- this function never imports or uses
    ``pyiceberg`` directly, which would bypass the schema enforcement
    that makes a write through ``load()`` sanctioned. See the runbook's
    "if something goes wrong" section.
    """
    result_config = next(t for t in table_configs() if t["name"] == "result")
    df = _build_fixture_dataframe(spark)
    return capability.load(
        dataset=NAMESPACE,
        tables=[{**result_config, "mode": "append"}],
        namespace=NAMESPACE,
        tenant=TENANT,
        dataframes={"result": df},
        spark=spark,
    )


def _query_current_state(
    capability: BerdlCapability, sources: tuple[str, ...]
) -> list[dict[str, Any]]:
    """Run ``current_state_sql()`` through Spark and shape rows as dicts."""
    sql = current_state_sql(RESULT_TABLE_FQN, sources=list(sources))
    spark_rows = capability.query(sql, engine="spark")
    return [
        {column: row[column] for column in _RESULT_COLUMNS} for row in spark_rows
    ]


def _check_property(
    capability: BerdlCapability,
    case: ParityCase,
    current_rows: list[dict[str, Any]],
) -> tuple[bool, str]:
    """Assert one property, mirroring the equivalent DuckDB test's assertion.

    Returns ``(passed, reason)`` -- ``reason`` is empty on success, an
    explanatory message on failure. Never raises: a failed property is
    reported as a FAIL line, not a crashed script, so every property gets
    evaluated and printed even if an earlier one fails.
    """
    rows_for_entity = [
        row for row in current_rows if row["entity_hash"] == case.entity_hash
    ]
    try:
        if case.property_key == "duplicate_collapse":
            assert len(rows_for_entity) == 1, (
                f"expected exactly 1 current row, got {len(rows_for_entity)}"
            )
        elif case.property_key == "newest_wins":
            assert len(rows_for_entity) == 1
            payload = json.loads(rows_for_entity[0]["payload"])
            assert payload == {"ns": {"k": "new"}}, f"unexpected payload {payload!r}"
        elif case.property_key == "ingest_batch_id_tie_break":
            winner = max(case.rows, key=lambda row: row["ingest_batch_id"])
            assert len(rows_for_entity) == 1
            assert (
                rows_for_entity[0]["ingest_batch_id"] == winner["ingest_batch_id"]
            ), (
                f"expected ingest_batch_id {winner['ingest_batch_id']!r}, got "
                f"{rows_for_entity[0]['ingest_batch_id']!r}"
            )
            payload = json.loads(rows_for_entity[0]["payload"])
            assert payload == winner["payload"], f"unexpected payload {payload!r}"
        elif case.property_key == "term_removal":
            assert len(rows_for_entity) == 1
            payload = json.loads(rows_for_entity[0]["payload"])
            assert "ec" not in payload, f"'ec' should have been dropped: {payload!r}"
            assert payload == {"go": {"term": "GO:0001"}}
        elif case.property_key == "source_isolation":
            by_source = {row["source"]: row for row in rows_for_entity}
            source_a, source_b = case.sources
            assert set(by_source) == {source_a, source_b}, (
                f"expected both sources current, got {set(by_source)!r}"
            )
            assert json.loads(by_source[source_a]["payload"]) == {
                "ns": {"k": "from_a"}
            }
            assert json.loads(by_source[source_b]["payload"]) == {
                "ns": {"k": "from_b"}
            }
            filtered_rows = [
                row
                for row in _query_current_state(capability, (source_a,))
                if row["entity_hash"] == case.entity_hash
            ]
            filtered_sources = {row["source"] for row in filtered_rows}
            assert filtered_sources == {source_a}, (
                f"sources filter should prune to {{{source_a!r}}}, got "
                f"{filtered_sources!r}"
            )
        elif case.property_key == "result_type_version_outside_slot_key":
            assert len(rows_for_entity) == 1
            assert rows_for_entity[0]["result_type_version"] == "v2"
            payload = json.loads(rows_for_entity[0]["payload"])
            assert payload == {"ns": {"k": "new_version"}}
        else:  # pragma: no cover - exhaustive over PARITY_CASES
            raise AssertionError(f"unknown property_key {case.property_key!r}")
    except AssertionError as exc:
        return False, str(exc) or "assertion failed"
    return True, ""


def main() -> int:
    capability = BerdlCapability()
    if capability.locus() != "in_pod":
        print(
            "FAIL: this script must run inside the BERDL JupyterHub pod "
            f"({POD_MACHINE}) -- 'berdl_notebook_utils' is not importable "
            "here. See OP1/OP3 in "
            "agent-io/docs/clearinghouse-schema-operator-runbook.md.",
            file=sys.stderr,
        )
        return 2

    from kbutillib.domains.kbase.berdl.transports import InPodTransport

    transport = InPodTransport()
    spark = transport.spark_session()

    print(
        f"Appending {len(ALL_PARITY_ROWS)} parity-check fixture rows to "
        f"{RESULT_TABLE_FQN} ..."
    )
    _append_fixture_rows(capability, spark)

    all_passed = True
    for case in PARITY_CASES:
        current_rows = _query_current_state(capability, case.sources)
        passed, reason = _check_property(capability, case, current_rows)
        status = "PASS" if passed else "FAIL"
        suffix = f" ({reason})" if reason else ""
        print(f"{status}: {case.property_key} -- {case.description}{suffix}")
        all_passed = all_passed and passed

    print()
    if all_passed:
        print("PARITY CHECK: ALL SIX PROPERTIES PASS")
        return 0
    print("PARITY CHECK: AT LEAST ONE PROPERTY FAILED", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
