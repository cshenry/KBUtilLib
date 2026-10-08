"""Run ``scripts/clearinghouse_parity_check.py``'s assertions off-pod.

The parity script is the in-pod (OP3) half of the derivation proof: it
appends the shared fixture to the real ``<entity_type>_result`` tables and
asserts one property per :data:`PARITY_CASES` entry against Spark. Those
assertions therefore only ever execute inside the BERDL pod -- which means
a mistake in one of them (a wrong expected payload, a property key with no
branch at all, a stale column list) is invisible until an operator runs
OP3 against the live lake, which is the most expensive place to discover
it.

This module closes that gap. It drives the script's own
:func:`_check_property` and :func:`_query_current_state` against the SAME
DuckDB surrogate ``tests/berdl/test_clearinghouse_derivation.py`` uses, by
injecting a fake capability whose ``query()`` executes the real
``current_state_sql()`` text locally. What that proves is the script's
ASSERTION LOGIC, not the Spark dialect -- same division of labour as the
derivation tests, and stated here so nobody reads this file as an in-pod
substitute.

It caught one real defect on arrival: ``genome_fasta_invariance`` had been
added to :data:`PARITY_CASES` without a matching branch in
:func:`_check_property`, so the script fell through to its
``unknown property_key`` fallback and would have reported FAIL for that
property on every in-pod run.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import duckdb
import pytest

from kbutillib.domains.kbase.berdl.clearinghouse_parity_fixture import (
    PARITY_CASES,
    rows_by_entity_type,
)
from kbutillib.domains.kbase.berdl.clearinghouse_schema import (
    ENTITY_TYPES,
    column_names,
    table_name,
)

#: Repo root, from this file: tests/berdl/<this> -> repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "clearinghouse_parity_check.py"

#: The declared result columns, read from the schema module -- the same
#: source the script derives its own projection from.
_RESULT_COLUMN_ORDER = column_names(ENTITY_TYPES[0], "result")


def _load_parity_script():
    """Import the parity script by path (``scripts/`` is not a package).

    Safe off-pod: every pod-only import in that module is deferred inside a
    function, so module execution touches no ``pyspark``.
    """
    spec = importlib.util.spec_from_file_location(
        "clearinghouse_parity_check", _SCRIPT_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


parity_script = _load_parity_script()


class _DuckDbCapability:
    """A fake ``BerdlCapability`` that runs the script's SQL on DuckDB.

    Implements only ``query(sql, engine=...)``, which is the entire surface
    :func:`_query_current_state` uses. The script emits Spark-dialect SQL
    naming a three-part, tenant-qualified table
    (``kbaseincubator.clearinghouse.protein_result``); DuckDB has neither
    backtick quoting nor that catalog, so this translates exactly those two
    things -- the quote character, and the qualified name down to the bare
    table -- and nothing else. The window function, the slot key, the
    ``WHERE`` predicates and the column list all execute verbatim.
    """

    def __init__(self) -> None:
        self.con = duckdb.connect()
        self._create_tables()
        self._append_fixture()

    def _create_tables(self) -> None:
        # One surrogate table per per-type result table, declared from the
        # schema module's own column list so it cannot drift from the DDL.
        # TIMESTAMP for observed_at, VARCHAR for everything else, matching
        # the declared STRING/TIMESTAMP types.
        for entity_type in ENTITY_TYPES:
            columns = ", ".join(
                f"{name} {'TIMESTAMP' if name == 'observed_at' else 'VARCHAR'}"
                for name in _RESULT_COLUMN_ORDER
            )
            self.con.execute(
                f"CREATE TABLE {table_name(entity_type, 'result')} ({columns})"
            )

    def _append_fixture(self) -> None:
        placeholders = ", ".join("?" for _ in _RESULT_COLUMN_ORDER)
        for entity_type, rows in rows_by_entity_type().items():
            target = table_name(entity_type, "result")
            for row in rows:
                values = [
                    json.dumps(row[name]) if name == "payload" else row[name]
                    for name in _RESULT_COLUMN_ORDER
                ]
                self.con.execute(
                    f"INSERT INTO {target} VALUES ({placeholders})", values
                )

    def _for_duckdb(self, sql: str) -> str:
        for entity_type in ENTITY_TYPES:
            bare = table_name(entity_type, "result")
            sql = sql.replace(
                f"`kbaseincubator`.`clearinghouse`.`{bare}`", f'"{bare}"'
            )
        return sql.replace("`", '"')

    def query(self, sql: str, engine: str = "spark") -> list[dict[str, Any]]:
        rows = self.con.execute(self._for_duckdb(sql)).fetchall()
        return [
            dict(zip(_RESULT_COLUMN_ORDER, row, strict=True)) for row in rows
        ]


@pytest.fixture
def capability() -> _DuckDbCapability:
    return _DuckDbCapability()


@pytest.mark.parametrize(
    "case", PARITY_CASES, ids=[case.property_key for case in PARITY_CASES]
)
def test_every_parity_property_passes_on_the_surrogate(capability, case):
    """Every property the in-pod script asserts passes off-pod too.

    Parametrized per property so a failure names the property that broke,
    and so a property added to PARITY_CASES is automatically covered here
    without an edit -- which is what makes the "no branch in
    _check_property" defect this file found unrepeatable.
    """
    current_rows = parity_script._query_current_state(
        capability, case.entity_type, case.sources
    )
    passed, reason = parity_script._check_property(
        capability, case, current_rows
    )
    assert passed, f"{case.property_key}: {reason}"


def test_the_parameter_set_property_is_among_the_cases():
    """Guards the parametrization above against silently losing the
    seventh property -- it must actually be in PARITY_CASES to be run.
    """
    assert "parameter_set_forks_slot" in {
        case.property_key for case in PARITY_CASES
    }


def test_every_case_has_a_real_branch_in_check_property(capability):
    """No property may fall through to the ``unknown property_key``
    fallback.

    That fallback raises an AssertionError which ``_check_property``
    converts into a FAIL line, so a missing branch does not crash -- it
    reports the property as failing, forever, on every in-pod run. This is
    the exact defect found in ``genome_fasta_invariance`` (see the module
    docstring), stated as its own named test so a recurrence is
    unambiguous.
    """
    for case in PARITY_CASES:
        _passed, reason = parity_script._check_property(
            capability,
            case,
            parity_script._query_current_state(
                capability, case.entity_type, case.sources
            ),
        )
        assert "unknown property_key" not in reason, case.property_key


def test_script_result_columns_match_the_declared_schema():
    """The script's row projection tracks the schema module.

    It is derived rather than hand-maintained precisely so a new column
    reaches it; this asserts the derivation, including
    ``parameter_set_hash``.
    """
    assert parity_script._RESULT_COLUMNS == _RESULT_COLUMN_ORDER
    assert "parameter_set_hash" in parity_script._RESULT_COLUMNS


def test_parameter_set_filter_round_trips_through_the_script_query(capability):
    """``_query_current_state``'s new ``parameter_set_hashes`` argument
    actually filters, rather than being accepted and dropped.
    """
    case = next(
        c for c in PARITY_CASES if c.property_key == "parameter_set_forks_slot"
    )
    unfiltered = [
        row
        for row in parity_script._query_current_state(
            capability, case.entity_type, case.sources
        )
        if row["entity_hash"] == case.entity_hash
    ]
    assert len(unfiltered) == 2

    _default_hash, evalue_hash = case.parameter_set_hashes
    filtered = [
        row
        for row in parity_script._query_current_state(
            capability, case.entity_type, case.sources, (evalue_hash,)
        )
        if row["entity_hash"] == case.entity_hash
    ]
    assert len(filtered) == 1
    assert filtered[0]["parameter_set_hash"] == evalue_hash
