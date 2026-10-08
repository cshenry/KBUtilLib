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
    CLEARINGHOUSE_NAMESPACE,
    ENTITY_TYPES,
    column_names,
    table_name,
)

#: The namespace these tests read and write through -- the SAME
#: non-production namespace OP-C2 creates for the real in-pod run. Using the
#: parity namespace here rather than production is not cosmetic: it means no
#: test in this module can pass while the script still points at
#: ``kbaseincubator.clearinghouse``.
_PARITY_NAMESPACE = "kbaseincubator.clearinghouse_parity"

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
    (``kbaseincubator.clearinghouse_parity.protein_result``); DuckDB has
    neither backtick quoting nor that catalog, so this translates exactly
    those two things -- the quote character, and the qualified name down to
    the bare table -- and nothing else. The window function, the slot key,
    the ``WHERE`` predicates and the column list all execute verbatim.

    The qualified prefix it strips is built from :data:`_PARITY_NAMESPACE`
    rather than hardcoded, so the translation follows the namespace the
    tests actually pass instead of silently failing to match if it changes.
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
        prefix = ".".join(
            f"`{segment}`" for segment in _PARITY_NAMESPACE.split(".")
        )
        for entity_type in ENTITY_TYPES:
            bare = table_name(entity_type, "result")
            sql = sql.replace(f"{prefix}.`{bare}`", f'"{bare}"')
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
        capability, case.entity_type, case.sources, namespace=_PARITY_NAMESPACE
    )
    passed, reason = parity_script._check_property(
        capability, case, current_rows, namespace=_PARITY_NAMESPACE
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
                capability,
                case.entity_type,
                case.sources,
                namespace=_PARITY_NAMESPACE,
            ),
            namespace=_PARITY_NAMESPACE,
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
            capability, case.entity_type, case.sources, namespace=_PARITY_NAMESPACE
        )
        if row["entity_hash"] == case.entity_hash
    ]
    assert len(unfiltered) == 2

    _default_hash, evalue_hash = case.parameter_set_hashes
    filtered = [
        row
        for row in parity_script._query_current_state(
            capability,
            case.entity_type,
            case.sources,
            (evalue_hash,),
            namespace=_PARITY_NAMESPACE,
        )
        if row["entity_hash"] == case.entity_hash
    ]
    assert len(filtered) == 1
    assert filtered[0]["parameter_set_hash"] == evalue_hash


# ==========================================================================
# --namespace IS REQUIRED, AND PRODUCTION IS REFUSED
# ==========================================================================
#
# The parity fixture used to be written straight into
# kbaseincubator.clearinghouse, on the theory that the `parity-check/`
# source prefix made the rows self-evidently fake. It does label them, but
# it does not isolate them: the demo readers treat every row in the
# production namespace as real data, and the <type>_result tables are
# append-only with no supported un-append. So the target namespace is now a
# required argument and production is refused OUTRIGHT.
#
# These tests exist because that guarantee is otherwise only observable
# inside the pod, against the live lake -- the single most expensive place
# to find out the guard does not fire. `check_target_namespace` is factored
# out as a pure string check against a module constant precisely so the
# refusal can be proven here, off-pod, with no Spark session and no pod.


def test_namespace_is_a_required_argument():
    """Omitting ``--namespace`` is an argparse error, not a default.

    A default would have to name some namespace, and the only one anybody
    would default to is production -- the one namespace this must never
    touch. SystemExit(2) is argparse's own "bad usage" exit.
    """
    with pytest.raises(SystemExit) as excinfo:
        parity_script.build_arg_parser().parse_args([])
    assert excinfo.value.code == 2


def test_production_namespace_is_refused():
    """The production namespace raises, rather than being written to."""
    with pytest.raises(parity_script.ParityNamespaceError) as excinfo:
        parity_script.check_target_namespace(CLEARINGHOUSE_NAMESPACE)
    message = str(excinfo.value)
    assert "REFUSED" in message
    assert CLEARINGHOUSE_NAMESPACE in message
    # The message must say WHY, not just "no": the next operator under
    # pressure needs to know this is a data-integrity rule and not a
    # configuration nit they can edit around.
    assert "real data" in message


def test_the_refused_value_is_the_real_production_constant():
    """The guard compares against the schema module's own constant.

    Not a retyped literal: a retyped "kbaseincubator.clearinghouse" would
    keep passing this suite after the real namespace moved, which is
    exactly when the guard would need to still work.
    """
    assert CLEARINGHOUSE_NAMESPACE == "kbaseincubator.clearinghouse"
    with pytest.raises(parity_script.ParityNamespaceError):
        parity_script.check_target_namespace("kbaseincubator.clearinghouse")


def test_the_parity_namespace_is_accepted_and_split():
    """A non-production namespace passes and yields (tenant, dataset).

    Both forms the write path needs come from this one argument, so they
    cannot disagree about where the rows go.
    """
    tenant, dataset = parity_script.check_target_namespace(_PARITY_NAMESPACE)
    assert (tenant, dataset) == ("kbaseincubator", "clearinghouse_parity")


@pytest.mark.parametrize(
    "bad",
    [
        "clearinghouse_parity",  # one segment -- no tenant
        "a.b.c",  # three segments
        "kbaseincubator.",  # empty dataset
        ".clearinghouse_parity",  # empty tenant
        "",  # empty
        " kbaseincubator.clearinghouse_parity",  # stray whitespace
    ],
)
def test_malformed_namespaces_are_refused(bad):
    """A malformed namespace is refused too, not half-resolved.

    ``kbaseincubator.`` in particular would otherwise produce an empty
    dataset segment and an FQN like ``kbaseincubator..protein_result``.
    """
    with pytest.raises(parity_script.ParityNamespaceError):
        parity_script.check_target_namespace(bad)


def test_main_refuses_production_before_any_spark_call(monkeypatch):
    """``main()`` exits 3 on production WITHOUT constructing a capability.

    This is the test that actually pins "before any Spark call". It makes
    ``BerdlCapability`` explode if touched: the guard must return first, so
    an implementation that validated after opening a session -- or after
    the locus check -- fails here instead of in the pod. Exit 3 is distinct
    from 1 (a property failed) and 2 (run off-pod) so an operator can tell
    a refusal from a failure.
    """

    def _exploding_capability(*_args, **_kwargs):
        raise AssertionError(
            "BerdlCapability was constructed before the namespace was "
            "checked -- the refusal must come first."
        )

    monkeypatch.setattr(
        parity_script, "BerdlCapability", _exploding_capability
    )
    assert parity_script.main(["--namespace", CLEARINGHOUSE_NAMESPACE]) == 3


def test_result_table_fqn_follows_the_given_namespace():
    """The read/write FQN is built from the argument, not a constant.

    Production must not appear in the FQN when the parity namespace was
    asked for -- the bug this whole change closes.
    """
    fqn = parity_script._result_table_fqn("protein", _PARITY_NAMESPACE)
    assert fqn == "kbaseincubator.clearinghouse_parity.protein_result"
    assert "kbaseincubator.clearinghouse." not in fqn
    # Built through table_name(), never string-concatenated by hand.
    assert fqn.endswith(table_name("protein", "result"))
