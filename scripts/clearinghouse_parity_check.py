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
marked fixture to the live per-type ``<entity_type>_result`` tables of the
namespace named by ``--namespace`` -- never production's
(after the fifteen-table split there is no single ``result`` table; each
row lands in the table resolved by ``clearinghouse_schema.table_name`` for
its ``entity_type`` -- these protein fixture rows land in
``protein_result``) through the sanctioned write path
(:meth:`BerdlCapability.load`, which is what applies schema enforcement --
never a raw ``pyiceberg`` write; see the runbook's "if something goes
wrong" section for why that shortcut is refused even under failure
pressure), runs the real ``current_state_sql()`` SQL text against each via
Spark, and asserts the SAME properties the DuckDB tests assert -- every
case in ``clearinghouse_parity_fixture.PARITY_CASES``, including the
``parameter_set_forks_slot`` property that proves two parameter sets for
one tool version are both current and that a ``parameter_set_hashes``
filter returns exactly its own row.

THE FIXTURE IS SHARED, NOT RE-TYPED. Every row this script appends comes
from :mod:`kbutillib.domains.kbase.berdl.clearinghouse_parity_fixture` --
the exact same module ``tests/berdl/test_clearinghouse_derivation.py``
imports its rows from. A parity check run against a hand-retyped lookalike
fixture would prove nothing the moment the two fixtures drifted apart;
importing one shared module is what keeps that impossible.

THE FIXTURE NEVER GOES TO PRODUCTION. Every fixture row's ``source``
carries the ``parity-check/`` prefix (see that module's
``PARITY_SOURCE_PREFIX``), so these rows can never be mistaken for real
tool output and can be found again later with a simple ``source LIKE
'parity-check/%'`` filter. But that prefix is a LABEL, not an isolation
mechanism, and labelling turned out not to be enough: the demo readers
treat every row in ``kbaseincubator.clearinghouse`` as real data, so a
fixture row in a table a demo reads from is a fixture row somebody will
eventually quote back as a result. The namespace this script writes to is
therefore an explicit, REQUIRED ``--namespace`` argument, and
:func:`check_target_namespace` REFUSES the production namespace outright
-- before a Spark session is opened, let alone before any write. Parity
fixtures belong in a namespace of their own
(``kbaseincubator.clearinghouse_parity``), which is why OP-C2 in the
runbook creates one and bootstraps only the five ``<type>_result``
configs into it.

WITHIN its own namespace the fixture is permanent and harmless:
re-running this script appends *more* rows to the same fixture slots,
which changes nothing about the current-state answer for those slots
(same ``entity_hash``/``entity_type``/``result_type``/``source``/
``parameter_set_hash``, so still the same slot; newest
``(observed_at, ingest_batch_id)`` still wins).

Usage (from a kbhub notebook cell or terminal), after confirming OP1 and
OP2 in the runbook and after OP-C2 has created and bootstrapped the
parity namespace:

    python scripts/clearinghouse_parity_check.py \
        --namespace kbaseincubator.clearinghouse_parity

Prints one PASS/FAIL line per property and a final summary line. Exits 0
if every property passes, 1 if any fails, 2 if run off-pod, and 3 if
``--namespace`` is the production namespace or is otherwise malformed.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from kbutillib.domains.kbase.berdl.capability import POD_MACHINE, BerdlCapability
from kbutillib.domains.kbase.berdl.clearinghouse_derivation import current_state_sql
from kbutillib.domains.kbase.berdl.clearinghouse_parity_fixture import (
    ALL_PARITY_ROWS,
    PARAMETER_SET_EXPECTED_PAYLOADS,
    PARITY_CASES,
    ParityCase,
    rows_by_entity_type,
)
from kbutillib.domains.kbase.berdl.clearinghouse_schema import (
    CLEARINGHOUSE_NAMESPACE,
    ENTITY_TYPES,
    column_names,
    table_configs,
    table_name,
)


class ParityNamespaceError(ValueError):
    """``--namespace`` is the production namespace, or is malformed.

    Its own exception type so :func:`main` can map it to a distinct exit
    code (3) and the off-pod test can assert the refusal by type rather
    than by matching message text.
    """


def check_target_namespace(namespace: str) -> tuple[str, str]:
    """Validate ``--namespace`` and split it into ``(tenant, dataset)``.

    THE REFUSAL THIS EXISTS FOR. Parity fixtures must never be written to
    ``kbaseincubator.clearinghouse``
    (:data:`clearinghouse_schema.CLEARINGHOUSE_NAMESPACE`). The
    ``parity-check/`` source prefix labels the rows but does not isolate
    them, and the demo readers treat every row in the production namespace
    as real data -- so a fixture row landing there is a fixture row that
    will eventually be quoted back as a genuine annotation result. The
    tables are append-only and Iceberg offers no supported un-append, so
    the only place to stop it is before the write.

    FACTORED OUT, AND CALLED BEFORE ANY SPARK CALL, deliberately. The
    comparison is pure string work against a module constant: no Spark
    session, no ``berdl_notebook_utils``, no pod. That is what lets
    ``tests/berdl/test_clearinghouse_parity_script.py`` prove the refusal
    off-pod, in CI, instead of discovering in the pod that it does not
    fire. Keeping it a plain function rather than inline argparse logic is
    the whole reason the guarantee is testable.

    Args:
        namespace: The dotted namespace, e.g.
            ``"kbaseincubator.clearinghouse_parity"``. Exactly two
            segments: the tenant (Iceberg catalog) and the dataset.

    Returns:
        ``(tenant, dataset)`` -- the two forms the write path needs.
        ``BerdlCapability.load()`` wants the dataset bare and the
        namespace tenant-qualified, and both are derived from this one
        argument so they cannot disagree with each other or with the FQN
        the queries read from.

    Raises:
        ParityNamespaceError: ``namespace`` is the production namespace,
            or is not a two-segment dotted name with non-empty segments.
    """
    if namespace.strip() != namespace or not namespace:
        raise ParityNamespaceError(
            f"--namespace {namespace!r} has leading or trailing whitespace, "
            "or is empty. Pass a dotted two-segment name, e.g. "
            "kbaseincubator.clearinghouse_parity."
        )
    if namespace == CLEARINGHOUSE_NAMESPACE:
        raise ParityNamespaceError(
            f"REFUSED: --namespace {namespace!r} is the PRODUCTION "
            "clearinghouse namespace. Parity fixtures must never be written "
            "to production: the demo readers treat its rows as real data, so "
            "a 'parity-check/' fixture row in a production <type>_result "
            "table is a fabricated annotation that somebody will eventually "
            "quote back as a real result -- and the tables are append-only, "
            "so there is no supported way to take it back out. Run this "
            "against a namespace of its own, e.g. "
            "kbaseincubator.clearinghouse_parity (see OP-C2 in "
            "agent-io/docs/clearinghouse-schema-operator-runbook.md, which "
            "creates it and bootstraps the five <type>_result configs into "
            "it)."
        )
    segments = namespace.split(".")
    if len(segments) != 2 or not all(segment for segment in segments):
        raise ParityNamespaceError(
            f"--namespace {namespace!r} must be a dotted two-segment name "
            "'<tenant>.<dataset>' with both segments non-empty, e.g. "
            f"kbaseincubator.clearinghouse_parity (got {len(segments)} "
            "segment(s))."
        )
    tenant, dataset = segments
    return tenant, dataset


def _result_table_fqn(entity_type: str, namespace: str) -> str:
    """The per-type ``<entity_type>_result`` table's fully qualified name.

    Under the fifteen-table split there is no single ``result`` table to
    default to: each entity type has its own ``<entity_type>_result`` table,
    whose bare name is resolved through
    :func:`kbutillib.domains.kbase.berdl.clearinghouse_schema.table_name`
    (the ONLY sanctioned name builder -- never string-concatenated here) and
    qualified with the dotted ``namespace`` the caller was given on the
    command line. :mod:`clearinghouse_derivation` documents this three-part,
    tenant-qualified form (e.g.
    ``"kbaseincubator.clearinghouse_parity.protein_result"``) as its worked
    example, so that is what this script passes to :func:`current_state_sql`.

    THE NAMESPACE IS A PARAMETER, NOT A CONSTANT. It used to be built from
    the schema module's ``TENANT``/``NAMESPACE`` constants, which meant
    every read and write went to production with no way to say otherwise.
    It is now threaded from ``--namespace`` through every call site, so
    there is exactly one place the target is decided and
    :func:`check_target_namespace` guards it.

    Args:
        entity_type: Which ``<entity_type>_result`` table to name.
        namespace: The dotted ``<tenant>.<dataset>`` namespace, already
            validated by :func:`check_target_namespace`.

    NOTE, per the runbook's OP2 namespace-resolution warning:
    ``BerdlCapability.load()``'s own postflight row-count/history queries
    use a *two-part* form instead (bare ``namespace.table``, no tenant
    segment -- see ``capability.py``, ``load()``'s postflight block). Which
    form actually resolves against the live Iceberg catalog is not
    verifiable off-pod. If this script's query step fails to find the table
    under the three-part form, retry with the two-part form (the dataset
    segment of ``--namespace`` plus
    ``table_name(entity_type, 'result')``) before assuming anything else is
    wrong -- and note in your parity-check record which form worked, since
    the next operator will hit the same fork.
    """
    return f"{namespace}.{table_name(entity_type, 'result')}"

#: Column order for rows returned from the Spark query, matching
#: ``clearinghouse_schema``'s ``<type>_result`` table declaration.
#:
#: DERIVED from the schema module's own accessor rather than re-typed here.
#: This constant WAS a hand-maintained tuple, which is the same failure
#: shape as task 914 (recorded in :func:`_build_fixture_dataframe`'s
#: docstring): a column added to the result schema did not reach the
#: hand-maintained list, and an operator first met the mismatch at the live
#: table. ``parameter_set_hash`` joining the schema is exactly that event,
#: so the list is now derived and cannot miss the next one. The result
#: schema is GENERIC across all five ``<type>_result`` tables, so the first
#: entity type defines the column list for every one of them.
_RESULT_COLUMNS = column_names(ENTITY_TYPES[0], "result")

def _build_fixture_dataframe(
    spark: Any, entity_type: str, rows_for_type: tuple[dict[str, Any], ...]
):
    """Build the Spark DataFrame of one entity type's parity fixture rows.

    Deferred pyspark import -- see module docstring. Parses each fixture
    row's ``observed_at`` string into a real ``datetime`` and each
    ``payload`` dict into its JSON-text form, matching the
    ``<entity_type>_result`` table's declared ``TIMESTAMP``/``STRING``
    column types (see ``clearinghouse_schema._RESULT_COLUMNS`` -- the
    ``result`` schema is GENERIC across all five per-type result tables) --
    an explicit schema is built here rather than left to inference
    precisely so this doesn't silently write ``observed_at`` as a string.

    BOTH THE SCHEMA AND THE ROWS ARE BUILT FROM THE SAME PARSED ``columns``
    list, and the rows are built POSITIONALLY rather than from a
    hand-maintained ``Row(field=...)`` keyword list. That is deliberate: it
    closes two failure modes the keyword list left open.

    * A column added to ``clearinghouse_schema._RESULT_COLUMNS`` is picked
      up here automatically. The keyword list did not track ``entity_type``
      when it joined the slot key, so the schema declared eight
      non-nullable fields while the rows supplied seven -- a mismatch an
      operator would first have met at the OP3 write step, against the
      live table (task 914).
    * ``Row(**kwargs)`` field ORDER is a pyspark-version-dependent detail
      (Spark sorted keyword fields alphabetically before 3.0 and preserves
      entry order from 3.0 on), so a keyword-built row's positional
      alignment against a declared ``StructType`` is not something this
      script could settle off-pod. Building positionally from the list
      that built the schema makes the question moot.

    A fixture row missing a declared column now raises ``KeyError`` naming
    that column, here and off-pod, rather than silently short-supplying
    the write.
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
    result_table = table_name(entity_type, "result")
    result_config = next(t for t in table_configs() if t["name"] == result_table)
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

    def cell(name: str, value: Any) -> Any:
        """Coerce one fixture value to its declared Spark column type."""
        if name == "payload":
            return json.dumps(value)
        if name == "observed_at":
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        return value

    rows = [
        Row(*(cell(name, row[name]) for name, _sql_type in columns))
        for row in rows_for_type
    ]
    return spark.createDataFrame(rows, schema=schema)


def _append_fixture_rows(
    capability: BerdlCapability, spark: Any, namespace: str
) -> dict[str, dict[str, Any]]:
    """Append the parity fixture rows to each per-type ``<type>_result`` table.

    Writes into the namespace named by ``--namespace``, never production --
    see :func:`check_target_namespace`, which the caller has already run.

    Under the fifteen-table split the fixture no longer targets a single
    ``result`` table: each row belongs in its own ``<entity_type>_result``
    table. :func:`rows_by_entity_type` groups the shared fixture rows by
    ``entity_type`` (iterating :data:`ENTITY_TYPES`, not a local list), and
    each group is written to its own table, resolved via
    :func:`table_name` -- never a string-concatenated name.

    Every write goes through :meth:`BerdlCapability.load` (->
    ``data_lakehouse_ingest.ingest``), the same sanctioned write path OP2
    uses to create the tables in the first place -- this function never
    imports or uses ``pyiceberg`` directly, which would bypass the schema
    enforcement that makes a write through ``load()`` sanctioned. See the
    runbook's "if something goes wrong" section.

    Args:
        capability: The in-pod capability whose ``load()`` performs the write.
        spark: The live Spark session the DataFrames are built against.
        namespace: The dotted ``<tenant>.<dataset>`` target, already
            validated by :func:`check_target_namespace`. Both the bare
            ``dataset`` and the tenant-qualified ``namespace`` that
            ``load()`` wants are derived from it here, so they cannot drift
            apart or point at different namespaces.

    Returns:
        A dict mapping each per-type ``result`` table name to that table's
        ``load()`` return value.
    """
    tenant, dataset = check_target_namespace(namespace)
    load_results: dict[str, dict[str, Any]] = {}
    for entity_type, rows_for_type in rows_by_entity_type().items():
        result_table = table_name(entity_type, "result")
        result_config = next(
            t for t in table_configs() if t["name"] == result_table
        )
        df = _build_fixture_dataframe(spark, entity_type, rows_for_type)
        load_results[result_table] = capability.load(
            dataset=dataset,
            tables=[{**result_config, "mode": "append"}],
            # TENANT-QUALIFIED, not bare NAMESPACE. load() probes existence
            # under this value, while data_lakehouse_ingest derives its own
            # target as f"{tenant}.{dataset}" and ignores the parameter
            # (in-pod defect D1, dev 1206). Passing bare "clearinghouse"
            # made every probe miss -- the tables live at
            # "kbaseincubator.clearinghouse" -- so select_write_mode() saw
            # table_exists=False and silently promoted this 'append' to
            # 'overwrite'. Harmless only while the *_result tables are
            # empty; those tables are exactly where the real annotation
            # corpus lands, so it is a data-loss path the moment they are
            # not. This makes the probe read where the write actually goes;
            # it does NOT fix D1 itself, which is capability-level.
            namespace=namespace,
            tenant=tenant,
            dataframes={result_table: df},
            spark=spark,
        )
    return load_results


def _query_current_state(
    capability: BerdlCapability,
    entity_type: str,
    sources: tuple[str, ...],
    parameter_set_hashes: tuple[str, ...] | None = None,
    *,
    namespace: str,
) -> list[dict[str, Any]]:
    """Run ``current_state_sql()`` against one type's ``result`` table.

    The table is the ``<entity_type>_result`` table resolved via
    :func:`table_name` (never string-concatenated) and qualified by
    :func:`_result_table_fqn`.

    Args:
        capability: The in-pod capability to run the query through.
        entity_type: Which ``<entity_type>_result`` table to read.
        sources: Scopes the read to this case's ``source`` values.
        parameter_set_hashes: When given, additionally scopes the read to
            these ``parameter_set_hash`` values -- the filter the
            ``parameter_set_forks_slot`` property exercises. ``None``
            (the default) applies no parameter-set filter, matching every
            other property's read.
        namespace: The dotted namespace to read from. KEYWORD-ONLY and
            REQUIRED, with no default: a default would have to be some
            namespace, and the only obvious candidate is production --
            exactly the value this change exists to stop reaching. An
            omitted argument is a ``TypeError`` at the call site rather
            than a silent read of the wrong namespace.
    """
    sql = current_state_sql(
        _result_table_fqn(entity_type, namespace),
        sources=list(sources),
        parameter_set_hashes=(
            None if parameter_set_hashes is None else list(parameter_set_hashes)
        ),
    )
    spark_rows = capability.query(sql, engine="spark")
    return [
        {column: row[column] for column in _RESULT_COLUMNS} for row in spark_rows
    ]


def _check_property(
    capability: BerdlCapability,
    case: ParityCase,
    current_rows: list[dict[str, Any]],
    *,
    namespace: str,
) -> tuple[bool, str]:
    """Assert one property, mirroring the equivalent DuckDB test's assertion.

    Returns ``(passed, reason)`` -- ``reason`` is empty on success, an
    explanatory message on failure. Never raises: a failed property is
    reported as a FAIL line, not a crashed script, so every property gets
    evaluated and printed even if an earlier one fails.

    ``namespace`` is keyword-only and required for the same reason as in
    :func:`_query_current_state`: two properties (``source_isolation`` and
    ``parameter_set_forks_slot``) issue their OWN follow-up reads to prove
    a filter prunes, and those must hit the same namespace as the read
    that produced ``current_rows``.
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
                for row in _query_current_state(
                    capability, case.entity_type, (source_a,), namespace=namespace
                )
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
        elif case.property_key == "genome_fasta_invariance":
            # All four renderings share one entity_hash, one source and one
            # parameter set, so they are one slot: exactly one current row.
            assert len(rows_for_entity) == 1, (
                f"expected exactly 1 current row, got {len(rows_for_entity)}"
            )
        elif case.property_key == "parameter_set_forks_slot":
            default_hash, evalue_hash = case.parameter_set_hashes
            by_hash = {row["parameter_set_hash"]: row for row in rows_for_entity}
            # Both parameter sets are current at once, even though the two
            # rows share entity_hash/entity_type/result_type/source: the
            # slot key carries parameter_set_hash.
            assert set(by_hash) == {default_hash, evalue_hash}, (
                "expected both parameter sets current, got "
                f"{sorted(by_hash)!r}"
            )
            for hash_value, expected_payload in (
                PARAMETER_SET_EXPECTED_PAYLOADS.items()
            ):
                actual = json.loads(by_hash[hash_value]["payload"])
                assert actual == expected_payload, (
                    f"parameter set {hash_value} carries payload {actual!r}, "
                    f"expected {expected_payload!r}"
                )
            # Filtering current state by ONE parameter_set_hash returns
            # exactly its row and not the other parameter set's.
            filtered_rows = [
                row
                for row in _query_current_state(
                    capability,
                    case.entity_type,
                    case.sources,
                    (evalue_hash,),
                    namespace=namespace,
                )
                if row["entity_hash"] == case.entity_hash
            ]
            filtered_hashes = {row["parameter_set_hash"] for row in filtered_rows}
            assert filtered_hashes == {evalue_hash}, (
                f"parameter_set_hashes filter should prune to "
                f"{{{evalue_hash!r}}}, got {filtered_hashes!r}"
            )
            assert len(filtered_rows) == 1, (
                f"expected exactly 1 filtered row, got {len(filtered_rows)}"
            )
            assert json.loads(filtered_rows[0]["payload"]) == (
                PARAMETER_SET_EXPECTED_PAYLOADS[evalue_hash]
            )
        else:  # pragma: no cover - exhaustive over PARITY_CASES
            raise AssertionError(f"unknown property_key {case.property_key!r}")
    except AssertionError as exc:
        return False, str(exc) or "assertion failed"
    return True, ""


def build_arg_parser() -> argparse.ArgumentParser:
    """The CLI. ``--namespace`` is REQUIRED and has no default.

    No default is the point. A default would be a namespace this script
    writes fixture rows into whenever an operator forgets the flag, and the
    only namespace anyone would think to default to is production -- which
    is the one namespace it must never touch. Requiring the flag makes the
    target an explicit decision on every run.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Prove current_state_sql() against the real Spark/Iceberg "
            "engine by appending the shared parity fixture to a "
            "NON-PRODUCTION namespace's <type>_result tables. Run from "
            "inside the BERDL JupyterHub pod. See OP-C2 in "
            "agent-io/docs/clearinghouse-schema-operator-runbook.md."
        )
    )
    parser.add_argument(
        "--namespace",
        required=True,
        metavar="TENANT.DATASET",
        help=(
            "REQUIRED dotted namespace to write and read the parity fixture "
            "in, e.g. kbaseincubator.clearinghouse_parity. The production "
            f"namespace ({CLEARINGHOUSE_NAMESPACE}) is REFUSED: demo "
            "readers treat its rows as real data."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    # THE REFUSAL COMES FIRST -- before BerdlCapability(), before the locus
    # check, before any Spark session exists. A guard that runs after a
    # session is opened is a guard that has already paid for the thing it
    # was meant to prevent, and in an append-only lake "we noticed late" is
    # indistinguishable from "we wrote it".
    try:
        check_target_namespace(args.namespace)
    except ParityNamespaceError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 3
    namespace = args.namespace

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

    target_tables = ", ".join(
        _result_table_fqn(entity_type, namespace)
        for entity_type in rows_by_entity_type()
    )
    print(
        f"Appending {len(ALL_PARITY_ROWS)} parity-check fixture rows to "
        f"{target_tables} ..."
    )
    _append_fixture_rows(capability, spark, namespace)

    all_passed = True
    for case in PARITY_CASES:
        current_rows = _query_current_state(
            capability, case.entity_type, case.sources, namespace=namespace
        )
        passed, reason = _check_property(
            capability, case, current_rows, namespace=namespace
        )
        status = "PASS" if passed else "FAIL"
        suffix = f" ({reason})" if reason else ""
        print(f"{status}: {case.property_key} -- {case.description}{suffix}")
        all_passed = all_passed and passed

    print()
    if all_passed:
        print(f"PARITY CHECK: ALL {len(PARITY_CASES)} PROPERTIES PASS")
        return 0
    print("PARITY CHECK: AT LEAST ONE PROPERTY FAILED", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
