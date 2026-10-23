"""Tests for the FILL-ONLY shard ingest: the pure rules and the capability glue.

THE THING UNDER TEST, in one sentence: a load of a published shard run reads
each batch off the pod's own disk, drops the rows the target already holds, and
appends only the survivors as a DataFrame -- so loading the same run twice
writes nothing the second time and can be proven to have landed batch by batch.

Three layers, tested at three levels:

1. THE PURE RULES (``clearinghouse_ingest``) -- key modes, the anti-join
   itself, the integrity checks, the derived inventory, the lock. Tested
   directly over in-memory tables and tmp dirs; no capability, no SQL.
2. THE SPARK GLUE -- ``ClearinghouseCapability._fill_only_survivors`` driven
   against a Spark stand-in (:class:`_FakeSpark`) whose ``left_anti`` join IS
   :func:`clearinghouse_ingest.fill_only_survivors`. That is the point: the
   engine path and the pure function must not be able to drift, so the glue is
   exercised as written and its answer compared against the specification.
3. THE WHOLE INGEST -- ``ingest_shards`` against :class:`_FakeLake`, an
   in-memory stand-in for the lakehouse that actually STORES what it is
   handed. A stub that only records calls could not show idempotence; because
   this one keeps the rows, the second ingest genuinely finds them present and
   the "zero rows, no load()" result is earned rather than asserted.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from kbutillib.domains.kbase.berdl import clearinghouse_ingest as ci
from kbutillib.domains.kbase.berdl.capability import BerdlLoadRefusedError
from kbutillib.domains.kbase.berdl.clearinghouse_capability import (
    ClearinghouseCapability,
    ClearinghouseLedgerAmbiguousError,
    ClearinghouseReconcileMismatchError,
    ClearinghouseSchemaPreflightError,
)
from kbutillib.domains.kbase.berdl.clearinghouse_derivation import SLOT_KEY_COLUMNS
from kbutillib.domains.kbase.berdl.clearinghouse_schema import (
    NAMESPACE,
    PARAMETER_SET_TABLE,
    TENANT,
    table_configs,
)

_WRITE_NS = f"{TENANT}.{NAMESPACE}"


# ---------------------------------------------------------------------------
# Fixtures: the state dir and the lock dir must never be the real ones
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Point the execution state dir and the SHARED lock dir at tmp_path.

    Both are env-configured precisely so a test (and an operator on a second
    pod) can move them. Without this a test run would write the developer's
    real ``~/.kbu/clearinghouse/runs`` and contend for the production lock.
    """
    monkeypatch.setenv(ci.STATE_DIR_ENV, str(tmp_path / "state"))
    monkeypatch.setenv(ci.LOCK_DIR_ENV, str(tmp_path / "locks"))


# ---------------------------------------------------------------------------
# Row and shard-run builders
# ---------------------------------------------------------------------------


def _hash(text: str) -> str:
    """A 64-char lowercase hex hash -- the only shape the tables accept."""
    return hashlib.sha256(text.encode()).hexdigest()


def _entity_row(seed: str, batch_id: str, entity_type: str = "gene") -> dict:
    return {
        "entity_hash": _hash(seed),
        "entity_type": entity_type,
        "observed_at": "2026-10-06T00:00:00+00:00",
        "ingest_batch_id": batch_id,
    }


def _result_row(
    seed: str,
    batch_id: str,
    *,
    entity_type: str = "gene",
    result_type: str = "annotation",
    source: str = "bakta",
    parameter_set: str = "p1",
) -> dict:
    return {
        "entity_hash": _hash(seed),
        "entity_type": entity_type,
        "result_type": result_type,
        "source": source,
        "parameter_set_hash": _hash(parameter_set),
        "observed_at": "2026-10-06T00:00:00+00:00",
        "ingest_batch_id": batch_id,
    }


def _preloaded_entities(*seeds: str, entity_type: str = "gene") -> dict:
    """``{<type>_entity: [...]}`` for a ``_FakeLake``: identity already there.

    A ``<type>_result`` batch's join keys must RESOLVE against
    ``<type>_entity`` -- the pre-commit coverage assertion
    (:func:`clearinghouse_ingest.join_assertions`) -- because a result row
    whose ``entity_hash`` names no entity is an orphan: the hash is opaque, so
    nothing can say what the result is ABOUT.

    So a result-batch fixture has to declare which entities the clearinghouse
    already knows, which is also the only state a real load is ever in: the
    entity and content rows are loaded first (47,341 of each, for
    ``function``), and stage 1 reads the universe out of them before anything
    is submitted. A result load into a target with NO entities is the
    0-of-19,268 wrong-key case, not a normal run.
    """
    return {
        f"{entity_type}_entity": [
            _entity_row(seed, "PRELOADED", entity_type=entity_type)
            for seed in seeds
        ]
    }


def _registry_row(batch_id: str, parameter_set: str = "p1") -> dict:
    return {
        "parameter_set_hash": _hash(parameter_set),
        "canonical_json": "{}",
        "observed_at": "2026-10-06T00:00:00+00:00",
        "ingest_batch_id": batch_id,
    }


def _write_batch(root: Path, table: str, batch_id: str, rows: list[dict]) -> dict:
    """Write one batch file and return its run.json inventory entry."""
    directory = root / table
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{batch_id}.parquet"
    pq.write_table(pa.Table.from_pylist(rows), path)
    key_column = (
        "parameter_set_hash" if table == PARAMETER_SET_TABLE else "entity_hash"
    )
    keys = sorted(row[key_column] for row in rows)
    return {
        "table": table,
        "batch_id": batch_id,
        "file": f"{table}/{path.name}",
        "rows": len(rows),
        # Mirrors what publish_unit records, so the arrival check's
        # expected-versus-observed SIZE evidence is a real number here too.
        "bytes": path.stat().st_size,
        "rows_deduplicated": 0,
        "key_column": key_column,
        "hash_min": keys[0],
        "hash_max": keys[-1],
        "sha256": ci.file_sha256(path),
        "source": ["test"],
        "slice": None,
        "unit_id": None,
        "attempt_id": "A",
    }


def _publish_run(
    tmp_path,
    batches: list[tuple[str, str, list[dict]]],
    *,
    run_id: str = "0SHARDRUN0000000000000000",
    units: list[str] | None = None,
    manifest_version: int = ci.__dict__.get("_unused", 1),
) -> Path:
    """Lay out a shard run on disk the way ``publish_unit`` does.

    ``batches`` is ``(table, batch_id, rows)`` in the order ``run.json`` should
    LIST them -- which is deliberately settable, because load order must be
    imposed by the ingest rather than inherited from the file.
    """
    from kbutillib.domains.kbase.berdl.clearinghouse_shard import (
        RUN_MANIFEST_VERSION,
    )

    root = tmp_path / "shards"
    root.mkdir(parents=True, exist_ok=True)
    entries = []
    for table, batch_id, rows in batches:
        entry = _write_batch(root, table, batch_id, rows)
        entries.append(entry)
    unit_ids = units if units is not None else [None for _ in entries]
    for entry, unit_id in zip(entries, unit_ids):
        entry["unit_id"] = unit_id
    ordered_units: list = []
    for unit_id in unit_ids:
        if unit_id not in ordered_units:
            ordered_units.append(unit_id)
    run = {
        "run_manifest_version": (
            RUN_MANIFEST_VERSION if manifest_version == 1 else manifest_version
        ),
        "run_id": run_id,
        "created_at": "2026-10-06T00:00:00+00:00",
        "slices": [],
        "units": [{"unit_id": u, "attempt_id": "A", "slice": None} for u in ordered_units],
        "batches": entries,
        "skipped": [],
        "sources": [],
    }
    (root / "run.json").write_text(json.dumps(run, indent=2) + "\n")
    return root


# ---------------------------------------------------------------------------
# _FakeLake: an in-memory stand-in for the lakehouse write boundary
# ---------------------------------------------------------------------------


class _FakeLake:
    """Stores what ``load()`` is handed and answers the reads ingest makes.

    It is a tiny lakehouse rather than a call recorder on purpose: fill-only
    is a claim about what the target ALREADY HOLDS, so a fake that forgets its
    appends could never falsify it. Four read shapes are answered, matched off
    the SQL text (the real reads run on Spark, which has no bind channel, so
    every value is inlined and reading it back out of the text is exactly what
    the existing fakes in this suite do):

    - ``DESCRIBE <table>`` -- the schema preflight.
    - ``SELECT <keys> ... WHERE <col> BETWEEN 'lo' AND 'hi'`` -- the fill-only
      existing-key read.
    - ``SELECT COUNT(*) ... WHERE ingest_batch_id = '<ulid>'`` -- reconcile and
      verification.
    - ``SELECT DISTINCT parameter_set_hash ... IN (...)`` -- the unit registry
      check.
    """

    def __init__(
        self,
        *,
        locus="in_pod",
        rows=None,
        missing_column_tables=(),
        absent_tables=(),
        spark=None,
        on_before_append=None,
    ):
        self._locus_value = locus
        self.rows: dict[str, list[dict]] = {t: list(r) for t, r in (rows or {}).items()}
        self._missing_column_tables = set(missing_column_tables)
        self._absent_tables = set(absent_tables)
        self._spark = spark
        if spark is None:
            # A collaborator with NO Spark session at all: the attribute is
            # absent-equivalent, so the capability falls back to the pure
            # function instead of calling a session that cannot exist.
            self.spark_session = None
        self._on_before_append = on_before_append
        self.load_calls: list[dict] = []
        self.queries: list[str] = []

    # -- the capability surface ClearinghouseCapability consults ---------

    def locus(self):
        return self._locus_value

    def resolve_write_namespace(self, *, dataset, tenant):
        return _WRITE_NS

    def resolve_probe_namespace(self, *, dataset, tenant):
        return _WRITE_NS

    def spark_session(self):
        return self._spark

    def load(self, **kwargs):
        self.load_calls.append(kwargs)
        assert "paths" not in kwargs, (
            "ingest handed load() a 'paths' argument -- bronze mode. The pod's "
            "local shard disk is unreadable by Spark executors, so every batch "
            "must go through dataframes=."
        )
        for table in kwargs["tables"]:
            assert "bronze_path" not in table, (
                f"ingest handed load() a bronze_path for {table['name']!r}; "
                "the shards are on the pod's local disk and cannot be read "
                "cluster-side."
            )
        reports = []
        for table in kwargs["tables"]:
            name = table["name"]
            payload = kwargs["dataframes"][name]
            new_rows = payload.rows if hasattr(payload, "rows") else list(payload)
            if self._on_before_append is not None:
                self._on_before_append(self, name)
            self.rows.setdefault(name, []).extend(new_rows)
            reports.append(
                {
                    "name": name,
                    "row_count": len(self.rows[name]),
                    "new_snapshot_id": 1000 + len(self.load_calls),
                }
            )
        return {"ingest_result": {"success": True}, "tables": reports}

    def query(self, sql, *, params=None, engine=None, **kwargs):
        if engine == "spark" and params is not None:
            raise AssertionError(
                "in-pod spark query() received params; BerdlCapability.query "
                "raises ValueError for params on the Spark engine"
            )
        self.queries.append(sql)
        table = self._table_of(sql)
        if sql.startswith("DESCRIBE"):
            if table in self._absent_tables:
                raise RuntimeError(f"Table or view not found: {table}")
            return self._describe(table)
        if "ingest_batch_id = '" in sql:
            wanted = sql.split("ingest_batch_id = '")[1].split("'")[0]
            return [
                {
                    "row_count": sum(
                        1
                        for row in self.rows.get(table, [])
                        if row.get("ingest_batch_id") == wanted
                    )
                }
            ]
        if "DISTINCT parameter_set_hash" in sql:
            asked = {
                literal
                for literal in sql.split("'")[1::2]
                if ci.HEX64_RE.match(literal)
            }
            held = {
                row.get("parameter_set_hash")
                for row in self.rows.get(PARAMETER_SET_TABLE, [])
            }
            return [{"parameter_set_hash": h} for h in sorted(asked & held)]
        if "BETWEEN" in sql:
            low, high = sql.split("BETWEEN")[1].split("'")[1::2][:2]
            range_column = sql.split("WHERE ")[1].split(" BETWEEN")[0].strip()
            key_columns = [
                column.strip()
                for column in sql.split("SELECT ")[1].split(" FROM")[0].split(",")
            ]
            return [
                {column: row.get(column) for column in key_columns}
                for row in self.rows.get(table, [])
                if row.get(range_column) is not None
                and low <= row[range_column] <= high
            ]
        if "snapshots" in sql:
            return [{"snapshot_id": 1000 + n} for n in range(1, 50)]
        return []

    # -- helpers ---------------------------------------------------------

    def _describe(self, table):
        """DESCRIBE's answer, derived from the module's own DDL."""
        for config in table_configs():
            if config["name"] != table:
                continue
            columns = [
                part.strip().split()[0]
                for part in config["schema_sql"].split(",")
                if part.strip()
            ]
            if table in self._missing_column_tables:
                columns = [c for c in columns if c != "parameter_set_hash"]
            return [{"col_name": column} for column in columns]
        return []

    @staticmethod
    def _table_of(sql):
        for name in [config["name"] for config in table_configs()]:
            if f".`{name}`" in sql or f'."{name}"' in sql or f".{name}" in sql:
                return name
        return ""


class _FakeDataFrame:
    """A Spark DataFrame stand-in holding an Arrow table."""

    def __init__(self, table: pa.Table):
        self.table = table
        self.cached = False
        self.counted = 0

    @property
    def rows(self):
        return self.table.to_pylist()

    def join(self, other, on, how):
        assert how == "left_anti", f"the fill-only join must be left_anti, got {how!r}"
        # THE WHOLE POINT: the stand-in engine's anti-join IS the pure
        # function, so a glue path that produced a different answer would
        # produce a different answer HERE too.
        return _FakeDataFrame(
            ci.fill_only_survivors(self.table, other.table, tuple(on))
        )

    def cache(self):
        self.cached = True
        return self

    def count(self):
        self.counted += 1
        return self.table.num_rows


class _FakeSpark:
    """A Spark session stand-in: ``createDataFrame`` over row dicts."""

    def createDataFrame(self, rows):  # noqa: N802 -- mirrors the Spark API
        return _FakeDataFrame(pa.Table.from_pylist(list(rows)))


def _capability(fake) -> ClearinghouseCapability:
    cap = ClearinghouseCapability(capability=fake)
    # The namespace-wide orphan audit is a separate, already-tested verb; the
    # fake answers its reads with empty sets, which is a sound registry.
    return cap


# ===========================================================================
# 1. THE PURE RULES
# ===========================================================================


def test_key_columns_per_table_family():
    assert ci.key_columns_for_table("gene_entity") == ("entity_hash",)
    assert ci.key_columns_for_table("protein_content") == ("entity_hash",)
    assert ci.key_columns_for_table("gene_result") == tuple(SLOT_KEY_COLUMNS)
    assert ci.key_columns_for_table(PARAMETER_SET_TABLE) == ("parameter_set_hash",)
    with pytest.raises(ValueError, match="known fill-only key mode"):
        ci.key_columns_for_table("something_else")


def test_result_key_columns_are_imported_not_restated():
    """The slot key must be the ONE tuple, not a copy that can drift."""
    assert ci.key_columns_for_table("gene_result") == tuple(SLOT_KEY_COLUMNS)
    assert "parameter_set_hash" in SLOT_KEY_COLUMNS


def _arrow(rows):
    """An Arrow table from row dicts (or from column arrays, for empty sets)."""
    if isinstance(rows, dict):
        return pa.table(rows)
    return pa.Table.from_pylist(rows)


def test_fill_only_all_present_drops_everything():
    batch = _arrow([_entity_row("a", "B1"), _entity_row("b", "B1")])
    existing = _arrow([{"entity_hash": _hash("a")}, {"entity_hash": _hash("b")}])
    assert ci.fill_only_survivors(batch, existing, ("entity_hash",)).num_rows == 0


def test_fill_only_none_present_keeps_everything_in_order():
    batch = _arrow([_entity_row("a", "B1"), _entity_row("b", "B1")])
    existing = _arrow({"entity_hash": pa.array([], type=pa.string())})
    survivors = ci.fill_only_survivors(batch, existing, ("entity_hash",))
    assert survivors.num_rows == 2
    assert survivors.column("entity_hash").to_pylist() == [_hash("a"), _hash("b")]


def test_fill_only_partial_keeps_only_the_absent_rows():
    batch = _arrow(
        [_entity_row("a", "B1"), _entity_row("b", "B1"), _entity_row("c", "B1")]
    )
    existing = _arrow([{"entity_hash": _hash("b")}])
    survivors = ci.fill_only_survivors(batch, existing, ("entity_hash",))
    assert survivors.column("entity_hash").to_pylist() == [_hash("a"), _hash("c")]


def test_fill_only_on_result_rows_uses_the_whole_slot_key():
    """A result row differing in ANY slot column is a different slot."""
    batch = _arrow(
        [
            _result_row("g1", "B1"),
            _result_row("g1", "B1", source="other"),
            _result_row("g1", "B1", parameter_set="p2"),
        ]
    )
    existing = _arrow(
        [
            {
                "entity_hash": _hash("g1"),
                "entity_type": "gene",
                "result_type": "annotation",
                "source": "bakta",
                "parameter_set_hash": _hash("p1"),
            }
        ]
    )
    survivors = ci.fill_only_survivors(batch, existing, tuple(SLOT_KEY_COLUMNS))
    assert survivors.num_rows == 2
    assert survivors.column("source").to_pylist() == ["other", "bakta"]


def test_fill_only_on_the_registry_matches_by_parameter_set_hash():
    batch = _arrow([_registry_row("B1", "p1"), _registry_row("B1", "p2")])
    existing = _arrow([{"parameter_set_hash": _hash("p1")}])
    survivors = ci.fill_only_survivors(batch, existing, ("parameter_set_hash",))
    assert survivors.column("parameter_set_hash").to_pylist() == [_hash("p2")]


def test_fill_only_treats_a_null_key_as_unmatched_like_sql_does():
    """NULL never equals NULL, so a null-keyed row survives -- as in Spark.

    A Python set would match ``None == None`` and drop the row, which is how
    the pure function and the engine would come to disagree about real data.
    """
    batch = _arrow(
        [
            {"entity_hash": None, "ingest_batch_id": "B1"},
            {"entity_hash": _hash("a"), "ingest_batch_id": "B1"},
        ]
    )
    existing = _arrow(
        {"entity_hash": pa.array([None, _hash("a")], type=pa.string())}
    )
    survivors = ci.fill_only_survivors(batch, existing, ("entity_hash",))
    assert survivors.column("entity_hash").to_pylist() == [None]


def test_fill_only_keeps_within_batch_duplicates_together():
    batch = _arrow([_entity_row("a", "B1"), _entity_row("a", "B1")])
    existing = _arrow({"entity_hash": pa.array([], type=pa.string())})
    assert ci.fill_only_survivors(batch, existing, ("entity_hash",)).num_rows == 2


def test_fill_only_refuses_a_missing_key_column_rather_than_failing_open():
    batch = _arrow([{"observed_at": "x"}])
    existing = _arrow([{"entity_hash": _hash("a")}])
    with pytest.raises(ValueError, match="no key column"):
        ci.fill_only_survivors(batch, existing, ("entity_hash",))
    with pytest.raises(ValueError, match="existing_keys has no key column"):
        ci.fill_only_survivors(
            _arrow([_entity_row("a", "B1")]), _arrow([{"x": 1}]), ("entity_hash",)
        )


# -- integrity --------------------------------------------------------------


def test_run_json_is_required(tmp_path):
    empty = tmp_path / "nothing"
    empty.mkdir()
    with pytest.raises(ci.ClearinghouseBatchIntegrityError, match="has no run.json"):
        ci.read_run_inventory(empty)


def test_a_manifest_version_this_code_does_not_implement_is_refused(tmp_path):
    root = _publish_run(
        tmp_path, [("gene_entity", "0BATCH0000000000000000001", [_entity_row("a", "0BATCH0000000000000000001")])]
    )
    run = json.loads((root / "run.json").read_text())
    run["run_manifest_version"] = 99
    (root / "run.json").write_text(json.dumps(run))
    with pytest.raises(
        ci.ClearinghouseBatchIntegrityError, match="run_manifest_version"
    ):
        ci.read_run_inventory(root)


def test_a_path_escaping_the_shard_root_is_refused(tmp_path):
    root = tmp_path / "shards"
    root.mkdir()
    with pytest.raises(ci.ClearinghouseBatchIntegrityError, match="OUTSIDE"):
        ci.resolve_batch_path(root, "../secrets/x.parquet")
    with pytest.raises(ci.ClearinghouseBatchIntegrityError, match="ABSOLUTE"):
        ci.resolve_batch_path(root, "/etc/passwd")


def test_verify_batch_file_checks_sha_rows_and_per_row_batch_id(tmp_path):
    batch_id = "0BATCH0000000000000000001"
    root = tmp_path / "shards"
    entry = _write_batch(
        root, "gene_entity", batch_id, [_entity_row("a", batch_id)]
    )
    table = ci.verify_batch_file(
        entry,
        shard_root=root,
        expected_batch_id=batch_id,
        expected_sha256=entry["sha256"],
    )
    assert table.num_rows == 1

    with pytest.raises(ci.ClearinghouseBatchIntegrityError, match="sha256"):
        ci.verify_batch_file(
            entry,
            shard_root=root,
            expected_batch_id=batch_id,
            expected_sha256=_hash("wrong"),
        )
    with pytest.raises(ci.ClearinghouseBatchIntegrityError, match="holds 1 rows"):
        ci.verify_batch_file(
            {**entry, "rows": 7},
            shard_root=root,
            expected_batch_id=batch_id,
            expected_sha256=entry["sha256"],
        )
    with pytest.raises(
        ci.ClearinghouseBatchIntegrityError, match="other ingest_batch_id"
    ):
        ci.verify_batch_file(
            entry,
            shard_root=root,
            expected_batch_id="0OTHER000000000000000000A",
            expected_sha256=entry["sha256"],
        )


# -- the derived inventory --------------------------------------------------


def test_a_new_execution_mints_fresh_batch_ids_and_keeps_provenance():
    run = {
        "run_id": "0SRC00000000000000000000A",
        "units": [{"unit_id": "u1"}],
        "batches": [
            {
                "table": "gene_entity",
                "batch_id": "0SRCBATCH000000000000001",
                "file": "gene_entity/x.parquet",
                "rows": 2,
                "hash_min": _hash("a"),
                "hash_max": _hash("b"),
                "sha256": "abc",
                "unit_id": "u1",
            }
        ],
    }
    [entry] = ci.derive_execution_inventory(run, execution_id="EXEC")
    assert entry["batch_id"] != "0SRCBATCH000000000000001"
    assert ci.ULID_RE.match(entry["batch_id"])
    assert entry["source_batch_id"] == "0SRCBATCH000000000000001"
    assert entry["source_run_id"] == "0SRC00000000000000000000A"
    assert entry["source_sha256"] == "abc"

    [adopted] = ci.derive_execution_inventory(
        run, execution_id="EXEC", adopt_source_batch_ids=True
    )
    assert adopted["batch_id"] == "0SRCBATCH000000000000001"


def test_the_derived_inventory_puts_each_units_registry_batch_first():
    run = {
        "run_id": "R",
        "units": [{"unit_id": "u1"}, {"unit_id": "u2"}],
        "batches": [
            {"table": "gene_result", "batch_id": "r1", "unit_id": "u1"},
            {"table": PARAMETER_SET_TABLE, "batch_id": "p1", "unit_id": "u1"},
            {"table": "gene_result", "batch_id": "r2", "unit_id": "u2"},
            {"table": PARAMETER_SET_TABLE, "batch_id": "p2", "unit_id": "u2"},
        ],
    }
    order = [
        entry["source_batch_id"]
        for entry in ci.derive_execution_inventory(run, execution_id="E")
    ]
    assert order == ["p1", "r1", "p2", "r2"]


# -- the ledger and the lock ------------------------------------------------


def test_the_ledger_lives_under_the_state_dir(tmp_path):
    ledger = ci.ExecutionLedger(ci.execution_state_dir("EXEC1"))
    assert str(ledger.path).startswith(str(tmp_path / "state"))
    ledger.append({"batch_id": "b1", "state": ci.LEDGER_STARTED})
    ledger.append({"batch_id": "b1", "state": ci.LEDGER_COMMITTED, "rows_ingested": 3})
    assert ledger.state_of("b1") == ci.LEDGER_COMMITTED
    assert ledger.records()["b1"]["rows_ingested"] == 3


def test_two_state_dirs_contend_for_one_namespace_lock(tmp_path, monkeypatch):
    """The lock is keyed by NAMESPACE and lives in one shared directory.

    A lock under the state dir would let two executions with different state
    dirs load one namespace simultaneously -- which is the double-append the
    lock exists to prevent.
    """
    first = ci.NamespaceLock(_WRITE_NS)
    first.acquire(execution_id="EXEC1")
    monkeypatch.setenv(ci.STATE_DIR_ENV, str(tmp_path / "other-state"))
    second = ci.NamespaceLock(_WRITE_NS)
    try:
        with pytest.raises(ci.ClearinghouseNamespaceLockedError, match="pid"):
            second.acquire(execution_id="EXEC2")
    finally:
        first.release()
    # Released: the next acquire succeeds.
    third = ci.NamespaceLock(_WRITE_NS)
    third.acquire(execution_id="EXEC3")
    third.release()


def test_a_lock_whose_holder_is_gone_is_reported_and_taken_over(tmp_path):
    """Process death releases an flock, so a dead holder never wedges a run."""
    lock_path = ci.resolve_lock_dir() / ci._lock_file_name(_WRITE_NS)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps(
            {"namespace": _WRITE_NS, "host": "deadhost", "pid": 999999, "started_at": "x"}
        )
        + "\n"
    )
    lock = ci.NamespaceLock(_WRITE_NS)
    lock.acquire(execution_id="EXEC1")
    try:
        assert lock.previous_holder is not None
        assert lock.previous_holder["pid"] == 999999
    finally:
        lock.release()


def test_an_unwritable_lock_directory_refuses_rather_than_going_private(
    tmp_path, monkeypatch
):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setenv(ci.LOCK_DIR_ENV, str(blocker / "locks"))
    with pytest.raises(ci.ClearinghouseLockUnavailableError, match="no private"):
        ci.NamespaceLock(_WRITE_NS).acquire(execution_id="EXEC1")


def test_validators_refuse_anything_that_is_not_a_ulid_or_lowercase_hex():
    with pytest.raises(ValueError, match="not a ULID"):
        ci.validate_batch_id("'; DROP TABLE x--")
    with pytest.raises(ValueError, match="lowercase hex"):
        ci.validate_hex64(_hash("a").upper(), where="hash_min")


# ===========================================================================
# 2. THE SPARK GLUE
# ===========================================================================


def test_the_spark_glue_answers_exactly_what_the_pure_function_says(tmp_path):
    """The engine path is driven as written, against a stand-in that applies
    the pure function -- so the two cannot drift silently."""
    batch_id = "0BATCH0000000000000000001"
    root = tmp_path / "shards"
    entry = _write_batch(
        root,
        "gene_entity",
        batch_id,
        [_entity_row("a", batch_id), _entity_row("b", batch_id)],
    )
    batch = pq.read_table(root / entry["file"])
    fake = _FakeLake(
        rows={"gene_entity": [_entity_row("a", "OLD")]}, spark=_FakeSpark()
    )
    cap = _capability(fake)
    payload, count = cap._fill_only_survivors(
        {**entry, "batch_id": batch_id}, batch=batch, write_ns=_WRITE_NS
    )
    assert isinstance(payload, _FakeDataFrame)
    assert payload.cached, "the survivors must be materialised before counting"
    assert count == 1
    assert payload.rows[0]["entity_hash"] == _hash("b")

    expected = ci.fill_only_survivors(
        batch,
        pa.Table.from_pylist([{"entity_hash": _hash("a")}]),
        ("entity_hash",),
    )
    assert payload.rows == expected.to_pylist()


def test_without_a_spark_session_the_pure_function_answers_directly(tmp_path):
    batch_id = "0BATCH0000000000000000001"
    root = tmp_path / "shards"
    entry = _write_batch(root, "gene_entity", batch_id, [_entity_row("a", batch_id)])
    batch = pq.read_table(root / entry["file"])
    cap = _capability(_FakeLake(rows={"gene_entity": [_entity_row("a", "OLD")]}))
    payload, count = cap._fill_only_survivors(
        {**entry, "batch_id": batch_id}, batch=batch, write_ns=_WRITE_NS
    )
    assert (payload, count) == ([], 0)


# ===========================================================================
# 3. THE WHOLE INGEST
# ===========================================================================

_B1 = "0BATCH0000000000000000001"
_B2 = "0BATCH0000000000000000002"
_BP = "0BATCHP000000000000000001"


def _simple_run(tmp_path, spark=None, **lake_kwargs):
    root = _publish_run(
        tmp_path,
        [
            ("gene_entity", _B1, [_entity_row("a", _B1), _entity_row("b", _B1)]),
            ("gene_content", _B2, [_entity_row("a", _B2)]),
        ],
    )
    return root, _FakeLake(spark=spark, **lake_kwargs)


def test_a_directory_without_run_json_is_refused(tmp_path):
    bare = tmp_path / "bare"
    bare.mkdir()
    fake = _FakeLake()
    with pytest.raises(ci.ClearinghouseBatchIntegrityError, match="has no run.json"):
        _capability(fake).ingest_shards(bare)
    assert fake.load_calls == []


def test_off_pod_refuses_before_reading_anything(tmp_path):
    root, fake = _simple_run(tmp_path)
    fake._locus_value = "off_pod"
    with pytest.raises(BerdlLoadRefusedError):
        _capability(fake).ingest_shards(root)
    assert fake.load_calls == [] and fake.queries == []


def test_every_batch_reaches_load_as_a_dataframe_and_never_a_bronze_path(tmp_path):
    root, fake = _simple_run(tmp_path, spark=_FakeSpark())
    result = _capability(fake).ingest_shards(root)
    assert [call["tables"][0]["name"] for call in fake.load_calls] == [
        "gene_content",
        "gene_entity",
    ] or [call["tables"][0]["name"] for call in fake.load_calls] == [
        "gene_entity",
        "gene_content",
    ]
    for call in fake.load_calls:
        assert "dataframes" in call and call["dataframes"]
        assert "paths" not in call
        assert "bronze_path" not in call["tables"][0]
        assert call["tables"][0]["mode"] == "append"
        assert call["namespace"] == _WRITE_NS
    assert [report["rows_ingested"] for report in result["tables"]] == [2, 1]
    assert len(fake.rows["gene_entity"]) == 2


def test_a_second_ingest_of_the_same_shard_dir_ingests_nothing(tmp_path):
    """The success criterion: zero rows, and load() called for no batch."""
    root, fake = _simple_run(tmp_path, spark=_FakeSpark())
    cap = _capability(fake)
    cap.ingest_shards(root)
    appended = len(fake.load_calls)
    assert appended == 2

    second = cap.ingest_shards(root)  # a NEW execution over the same shards
    assert len(fake.load_calls) == appended, (
        "the second ingest called load() again; every row was already present, "
        "so fill-only must leave no survivors and make no call"
    )
    assert [report["rows_ingested"] for report in second["tables"]] == [0, 0]
    assert [report["rows_dropped_present"] for report in second["tables"]] == [2, 1]
    assert len(fake.rows["gene_entity"]) == 2


def test_the_rows_appended_carry_this_executions_batch_id(tmp_path):
    root, fake = _simple_run(tmp_path, spark=_FakeSpark())
    result = _capability(fake).ingest_shards(root)
    ids = {row["ingest_batch_id"] for row in fake.rows["gene_entity"]}
    assert ids == {
        report["batch_id"]
        for report in result["tables"]
        if report["name"] == "gene_entity"
    }
    assert _B1 not in ids, "a new execution must not adopt the shard run's ids"


def test_the_ledger_is_written_under_the_state_dir_with_the_row_accounting(
    tmp_path,
):
    root, fake = _simple_run(tmp_path, spark=_FakeSpark())
    result = _capability(fake).ingest_shards(root)
    state_dir = Path(result["state_dir"])
    assert state_dir.parent == tmp_path / "state"
    assert not (root / ci.LEDGER_FILENAME).exists(), (
        "the ledger must not live beside the shards: that directory is "
        "sharder output and may be regenerated or deleted"
    )
    records = ci.ExecutionLedger(state_dir).records()
    assert len(records) == 2
    for record in records.values():
        assert record["state"] == ci.LEDGER_COMMITTED
        assert record["namespace"] == _WRITE_NS
        assert record["rows_in"] == record["rows_ingested"]
        assert record["rows_dropped_present"] == 0
        assert record["sha256"]


def _mismatched_run(tmp_path, *, age_s: float = 0.0):
    """A two-batch run whose FIRST batch's bytes are not its published sha256.

    ``age_s`` back-dates ``run.json``'s mtime, which is what the transfer
    deadline is measured from -- so the same on-disk condition can be posed
    both as "the hand-off is minutes old" and as "the hand-off is a day old".
    """
    import os as _os

    root = _publish_run(
        tmp_path,
        [
            ("gene_entity", _B1, [_entity_row("a", _B1)]),
            ("gene_content", _B2, [_entity_row("a", _B2)]),
        ],
    )
    path = root / "run.json"
    run = json.loads(path.read_text())
    run["batches"][0]["sha256"] = _hash("tampered")
    path.write_text(json.dumps(run))
    if age_s:
        stamp = path.stat().st_mtime - age_s
        _os.utime(path, (stamp, stamp))
    return root


def test_a_sha256_mismatch_past_the_deadline_is_a_failed_delivery(tmp_path):
    """Past the deadline the batch FAILS, is ledgered, and the others carry on.

    The evidence is the point: which batch, expected versus observed digest,
    expected versus observed size. "The digests differ" alone cannot tell an
    operator whether to wait, re-sync or re-shard.
    """
    root = _mismatched_run(tmp_path, age_s=ci.DEFAULT_TRANSFER_DEADLINE_SECONDS + 60)
    fake = _FakeLake(spark=_FakeSpark())
    result = _capability(fake).ingest_shards(root)
    by_table = {report["name"]: report for report in result["tables"]}
    assert by_table["gene_entity"]["action"] == "failed"
    error = by_table["gene_entity"]["error"]
    assert "FAILED DELIVERY" in error
    assert "sha256" in error
    transfer = by_table["gene_entity"]["transfer"]
    assert transfer["state"] == "failed_delivery"
    assert transfer["reason"] == ci.TRANSFER_DIGEST_MISMATCH
    assert transfer["expected_sha256"] == _hash("tampered")
    assert transfer["observed_sha256"] != transfer["expected_sha256"]
    # Expected versus observed SIZE, both real numbers: the sharder records
    # each batch's byte count in run.json for exactly this report.
    assert isinstance(transfer["expected_bytes"], int)
    assert isinstance(transfer["observed_bytes"], int)

    # One bad batch does not invalidate the others.
    assert by_table["gene_content"]["action"] == "ingest"
    assert [call["tables"][0]["name"] for call in fake.load_calls] == ["gene_content"]
    ledger = ci.ExecutionLedger(Path(result["state_dir"])).records()
    failed = [r for r in ledger.values() if r["state"] == ci.LEDGER_FAILED]
    assert len(failed) == 1 and failed[0]["table"] == "gene_entity"


def test_a_first_sha256_mismatch_is_still_syncing_and_is_retried(tmp_path):
    """Inside the deadline the SAME condition is a retry, not corruption.

    The sync client does not guarantee the producer's write order, so
    ``run.json`` can arrive before the bytes it describes -- and the client's
    own "up to date" is not a completeness signal either. A FIRST observation
    cannot tell sync lag from a lost file; only elapsed time can.
    """
    root = _mismatched_run(tmp_path)
    fake = _FakeLake(spark=_FakeSpark())
    result = _capability(fake).ingest_shards(root)
    by_table = {report["name"]: report for report in result["tables"]}
    assert by_table["gene_entity"]["action"] == "syncing"
    assert "STILL SYNCING" in by_table["gene_entity"]["error"]
    assert by_table["gene_entity"]["transfer"]["state"] == "syncing"
    # NOT a terminal failure: the state is `syncing`, so a re-run re-checks it.
    ledger = ci.ExecutionLedger(Path(result["state_dir"])).records()
    states = {record["table"]: record["state"] for record in ledger.values()}
    assert states["gene_entity"] == ci.LEDGER_SYNCING
    assert states["gene_content"] == ci.LEDGER_COMMITTED


def test_a_retry_resumes_the_deadline_from_the_persisted_first_sighting(tmp_path):
    """PROGRESS IS PERSISTED: the retry does not restart verification.

    Two things are proven here. The first sighting's timestamp survives into
    the retry (so the deadline is measured from when the mismatch was FIRST
    seen, not from this attempt), and the batches that already committed are
    not re-verified or re-appended.
    """
    root = _mismatched_run(tmp_path)
    fake = _FakeLake(spark=_FakeSpark())
    cap = _capability(fake)
    first = cap.ingest_shards(root)
    ledger = ci.ExecutionLedger(Path(first["state_dir"]))
    first_seen = [
        record["first_observed_at"]
        for record in ledger.records().values()
        if record["state"] == ci.LEDGER_SYNCING
    ]
    assert len(first_seen) == 1

    second = cap.ingest_shards(root, run_id=first["run_id"])
    by_table = {report["name"]: report for report in second["tables"]}
    # Still syncing, and carrying the ORIGINAL first sighting.
    assert by_table["gene_entity"]["action"] == "syncing"
    assert by_table["gene_entity"]["transfer"]["first_observed_at"] == first_seen[0]
    # The committed batch is skipped, not re-appended: one load() in total.
    assert by_table["gene_content"]["action"] == "skip"
    assert len(fake.load_calls) == 1


def test_an_absent_batch_file_inside_the_deadline_is_also_still_syncing(tmp_path):
    """The marker can arrive before the bytes, so an ABSENT file is a wait.

    ``run.json`` EXISTING is not proof that the batches arrived. The observed
    failure mode was precisely this: the client reported the hand-off
    directory up to date while one of its three files was absent.
    """
    root = _publish_run(
        tmp_path,
        [("gene_entity", _B1, [_entity_row("a", _B1)])],
    )
    run = json.loads((root / "run.json").read_text())
    (root / run["batches"][0]["file"]).unlink()
    fake = _FakeLake(spark=_FakeSpark())
    result = _capability(fake).ingest_shards(root)
    [report] = result["tables"]
    assert report["action"] == "syncing"
    assert report["transfer"]["reason"] == ci.TRANSFER_ABSENT
    assert report["transfer"]["observed_sha256"] is None
    assert fake.load_calls == []


def test_dry_run_writes_nothing_at_all(tmp_path):
    root, fake = _simple_run(tmp_path, spark=_FakeSpark())
    result = _capability(fake).ingest_shards(root, dry_run=True)
    assert fake.load_calls == []
    assert [report["action"] for report in result["tables"]] == [
        "would_ingest",
        "would_ingest",
    ]
    assert [report["rows_ingested"] for report in result["tables"]] == [2, 1]
    assert not (tmp_path / "state").exists()
    assert not (tmp_path / "locks" / ci._lock_file_name(_WRITE_NS)).exists()


# -- the schema preflight ---------------------------------------------------


def test_the_preflight_refuses_a_result_table_without_parameter_set_hash(tmp_path):
    root, fake = _simple_run(
        tmp_path, spark=_FakeSpark(), missing_column_tables={"protein_result"}
    )
    with pytest.raises(ClearinghouseSchemaPreflightError, match="protein_result"):
        _capability(fake).ingest_shards(root)
    assert fake.load_calls == []
    assert not (tmp_path / "state").exists()


def test_the_preflight_refuses_a_target_without_the_registry_table(tmp_path):
    root, fake = _simple_run(
        tmp_path, spark=_FakeSpark(), absent_tables={PARAMETER_SET_TABLE}
    )
    with pytest.raises(
        ClearinghouseSchemaPreflightError, match="does not exist"
    ):
        _capability(fake).ingest_shards(root)
    assert fake.load_calls == []


# -- registry ordering ------------------------------------------------------


def test_the_registry_batch_is_ingested_first_even_when_listed_last(tmp_path):
    """run.json lists the registry LAST; the ingest must still load it first."""
    root = _publish_run(
        tmp_path,
        [
            ("gene_result", _B1, [_result_row("g1", _B1)]),
            (PARAMETER_SET_TABLE, _BP, [_registry_row(_BP)]),
        ],
        units=["u1", "u1"],
    )
    fake = _FakeLake(spark=_FakeSpark(), rows=_preloaded_entities("g1"))
    result = _capability(fake).ingest_shards(root)
    assert [call["tables"][0]["name"] for call in fake.load_calls] == [
        PARAMETER_SET_TABLE,
        "gene_result",
    ]
    assert [report["name"] for report in result["tables"]] == [
        PARAMETER_SET_TABLE,
        "gene_result",
    ]


def test_a_re_run_ingests_zero_registry_rows(tmp_path):
    root = _publish_run(
        tmp_path,
        [
            (PARAMETER_SET_TABLE, _BP, [_registry_row(_BP)]),
            ("gene_result", _B1, [_result_row("g1", _B1)]),
        ],
        units=["u1", "u1"],
    )
    fake = _FakeLake(spark=_FakeSpark(), rows=_preloaded_entities("g1"))
    cap = _capability(fake)
    cap.ingest_shards(root)
    assert len(fake.rows[PARAMETER_SET_TABLE]) == 1
    # The result batch actually landed; without the preloaded entity it would
    # have been refused by the coverage assertion and this test would be
    # asserting registry counts over a result batch that never ingested.
    assert len(fake.rows["gene_result"]) == 1
    second = cap.ingest_shards(root)
    assert len(fake.rows[PARAMETER_SET_TABLE]) == 1
    registry_report = [
        r for r in second["tables"] if r["name"] == PARAMETER_SET_TABLE
    ][0]
    assert registry_report["rows_ingested"] == 0
    assert registry_report["rows_dropped_present"] == 1


# -- reconcile --------------------------------------------------------------


def _crash_after_append(tmp_path, *, rows_landed=True):
    """Run one batch, append it, but die before the committed ledger line.

    Simulates the kill the pod routinely delivers: the rows are in the table
    and the ledger shows only ``started``.
    """
    root = _publish_run(
        tmp_path, [("gene_entity", _B1, [_entity_row("a", _B1), _entity_row("b", _B1)])]
    )
    fake = _FakeLake(spark=_FakeSpark())
    cap = _capability(fake)
    real_append = ci.ExecutionLedger.append

    def crash_on_commit(self, record):
        if record.get("state") == ci.LEDGER_COMMITTED:
            raise KeyboardInterrupt("pod restart")
        return real_append(self, record)

    ci.ExecutionLedger.append = crash_on_commit
    try:
        with pytest.raises(KeyboardInterrupt):
            cap.ingest_shards(root)
    finally:
        ci.ExecutionLedger.append = real_append
    executions = sorted((tmp_path / "state").iterdir())
    assert len(executions) == 1
    execution_id = executions[0].name
    if not rows_landed:
        fake.rows["gene_entity"] = []
    return root, fake, cap, execution_id


def test_a_started_batch_refuses_without_reconcile(tmp_path):
    root, fake, cap, execution_id = _crash_after_append(tmp_path)
    with pytest.raises(ClearinghouseLedgerAmbiguousError, match="started"):
        cap.ingest_shards(root, run_id=execution_id)


def test_reconcile_commits_a_started_batch_whose_count_matches(tmp_path):
    """Crash after the append: resume KEEPS the batch, reconciles it, verifies."""
    root, fake, cap, execution_id = _crash_after_append(tmp_path)
    before = len(fake.load_calls)
    result = cap.ingest_shards(root, run_id=execution_id, reconcile=True)
    assert len(fake.load_calls) == before, "the rows had landed; do not re-append"
    assert len(fake.rows["gene_entity"]) == 2
    ledger = ci.ExecutionLedger(ci.execution_state_dir(execution_id)).records()
    [record] = list(ledger.values())
    assert record["state"] == ci.LEDGER_COMMITTED and record["reconciled"] is True
    assert record["rows_ingested"] == 2

    verified = cap.verify_run(root, run_id=execution_id)
    assert verified["ok"], verified["discrepancies"]
    assert verified["batches"][0]["live_rows"] == 2


def test_reconcile_re_ingests_a_started_batch_that_counted_zero(tmp_path):
    root, fake, cap, execution_id = _crash_after_append(tmp_path, rows_landed=False)
    before = len(fake.load_calls)
    result = cap.ingest_shards(root, run_id=execution_id, reconcile=True)
    assert len(fake.load_calls) == before + 1, "nothing landed; the batch must retry"
    assert len(fake.rows["gene_entity"]) == 2
    assert result["tables"][0]["rows_ingested"] == 2
    # The SAME immutable batch, under the SAME id, retried.
    assert result["tables"][0]["batch_id"] == (
        ci.read_execution(ci.execution_state_dir(execution_id))["inventory"][0][
            "batch_id"
        ]
    )


def test_reconcile_stops_the_run_on_a_partial_append(tmp_path):
    root, fake, cap, execution_id = _crash_after_append(tmp_path)
    fake.rows["gene_entity"] = fake.rows["gene_entity"][:1]  # one of two landed
    with pytest.raises(ClearinghouseReconcileMismatchError, match="PARTIAL"):
        cap.ingest_shards(root, run_id=execution_id, reconcile=True)


def test_reconcile_happens_before_any_other_batch_is_touched(tmp_path):
    """A started batch is decided before the run reads or appends anything."""
    root = _publish_run(
        tmp_path,
        [
            ("gene_entity", _B1, [_entity_row("a", _B1)]),
            ("gene_content", _B2, [_entity_row("a", _B2)]),
        ],
    )
    fake = _FakeLake(spark=_FakeSpark())
    cap = _capability(fake)
    execution_id = ci.new_execution_id()
    run = ci.read_run_inventory(root)
    binding = ci.bind_execution(
        execution_id=execution_id,
        shard_dir=root,
        namespace=_WRITE_NS,
        tenant=TENANT,
        dataset=NAMESPACE,
        run=run,
    )
    ci.write_execution(ci.execution_state_dir(execution_id), binding)
    # The SECOND batch is the one left started, so a run that ingested in
    # inventory order before reconciling would append the first batch first.
    second = binding["inventory"][1]
    ci.ExecutionLedger(ci.execution_state_dir(execution_id)).append(
        {
            "batch_id": second["batch_id"],
            "table": second["table"],
            "state": ci.LEDGER_STARTED,
            "expected_rows": 1,
        }
    )
    # Two rows already carry that batch id against an expectation of one: a
    # PARTIAL append, which must stop the run -- and it can only stop it
    # before the first batch is appended if reconcile truly runs first.
    fake.rows[second["table"]] = [
        _entity_row("x", second["batch_id"]),
        _entity_row("y", second["batch_id"]),
    ]
    with pytest.raises(ClearinghouseReconcileMismatchError):
        cap.ingest_shards(root, run_id=execution_id, reconcile=True)
    assert fake.load_calls == [], (
        "the run appended a batch before deciding the started one"
    )


# -- executions, promotion and re-load --------------------------------------


def test_a_new_execution_over_already_landed_files_verifies_with_zero_rows(
    tmp_path,
):
    root, fake = _simple_run(tmp_path, spark=_FakeSpark())
    cap = _capability(fake)
    cap.ingest_shards(root)
    second = cap.ingest_shards(root)
    verified = cap.verify_run(root, run_id=second["run_id"])
    assert verified["ok"], verified["discrepancies"]
    assert all(report["ledger_rows"] == 0 for report in verified["batches"])
    assert all(report["live_rows"] == 0 for report in verified["batches"])


def test_promotion_from_scratch_to_production_gets_fresh_ids_and_provenance(
    tmp_path,
):
    """One shard run, two targets: each load is its own provable execution."""
    root, scratch = _simple_run(tmp_path, spark=_FakeSpark())
    first = _capability(scratch).ingest_shards(root)
    production = _FakeLake(spark=_FakeSpark())
    second = _capability(production).ingest_shards(root)

    first_ids = {report["batch_id"] for report in first["tables"]}
    second_ids = {report["batch_id"] for report in second["tables"]}
    assert first_ids.isdisjoint(second_ids)
    assert first["source_run_id"] == second["source_run_id"]
    for execution in (first, second):
        inventory = ci.read_execution(Path(execution["state_dir"]))["inventory"]
        assert all(entry["source_batch_id"] in (_B1, _B2) for entry in inventory)
        assert all(entry["source_sha256"] for entry in inventory)
    assert len(production.rows["gene_entity"]) == 2


def test_resuming_an_execution_bound_to_another_namespace_refuses(tmp_path):
    root, fake = _simple_run(tmp_path, spark=_FakeSpark())
    result = _capability(fake).ingest_shards(root)
    state_dir = Path(result["state_dir"])
    binding = ci.read_execution(state_dir)
    binding["namespace"] = "someone.else"
    ci.write_execution(state_dir, binding)
    with pytest.raises(
        ci.ClearinghouseExecutionBindingError, match="bound to namespace"
    ):
        _capability(fake).ingest_shards(root, run_id=result["run_id"])


def test_a_concurrent_write_between_the_count_and_the_append_changes_nothing(
    tmp_path,
):
    """``expected_rows`` is counted from the MATERIALISED survivors.

    A writer that lands a colliding row after the survivors were counted must
    not change how many rows this append carries -- if it did, the ledger's
    expectation and the table's reality would disagree and reconcile could
    never decide the batch.
    """
    root = _publish_run(
        tmp_path, [("gene_entity", _B1, [_entity_row("a", _B1), _entity_row("b", _B1)])]
    )

    def land_a_colliding_row(lake, table):
        lake.rows.setdefault(table, []).append(_entity_row("b", "SOMEONE_ELSE"))

    fake = _FakeLake(spark=_FakeSpark(), on_before_append=land_a_colliding_row)
    result = _capability(fake).ingest_shards(root)
    report = result["tables"][0]
    assert report["rows_ingested"] == 2
    ledger = ci.ExecutionLedger(Path(result["state_dir"])).records()
    [record] = [r for r in ledger.values() if r["state"] == ci.LEDGER_COMMITTED]
    assert record["expected_rows"] == 2
    appended = [
        row
        for row in fake.rows["gene_entity"]
        if row["ingest_batch_id"] == report["batch_id"]
    ]
    assert len(appended) == record["expected_rows"]


def test_a_concurrent_ingest_of_the_same_namespace_is_refused(tmp_path):
    root, fake = _simple_run(tmp_path, spark=_FakeSpark())
    held = ci.NamespaceLock(_WRITE_NS)
    held.acquire(execution_id="SOMEONE_ELSE")
    try:
        with pytest.raises(ci.ClearinghouseNamespaceLockedError, match="pid"):
            _capability(fake).ingest_shards(root)
    finally:
        held.release()
    assert fake.load_calls == []


# -- verification -----------------------------------------------------------


def test_verify_compares_live_counts_against_the_ledgers_rows_ingested(tmp_path):
    root, fake = _simple_run(tmp_path, spark=_FakeSpark())
    cap = _capability(fake)
    result = cap.ingest_shards(root)
    verified = cap.verify_run(root, run_id=result["run_id"])
    assert verified["ok"], verified["discrepancies"]
    assert {r["name"]: (r["ledger_rows"], r["live_rows"]) for r in verified["batches"]} == {
        "gene_entity": (2, 2),
        "gene_content": (1, 1),
    }

    # Rows the ledger says landed have gone missing: a discrepancy, reported.
    fake.rows["gene_entity"] = []
    degraded = cap.verify_run(root, run_id=result["run_id"])
    assert not degraded["ok"]
    assert any("are missing" in line for line in degraded["discrepancies"])


def test_verify_flags_a_batch_appended_twice_as_excess(tmp_path):
    root, fake = _simple_run(tmp_path, spark=_FakeSpark())
    cap = _capability(fake)
    result = cap.ingest_shards(root)
    batch_id = result["tables"][0]["batch_id"]
    table = result["tables"][0]["name"]
    fake.rows[table].append(_entity_row("zz", batch_id))
    verified = cap.verify_run(root, run_id=result["run_id"])
    assert not verified["ok"]
    assert any("EXCESS" in line for line in verified["discrepancies"])


def test_verify_blocks_on_a_failed_batch(tmp_path):
    root = _publish_run(
        tmp_path,
        [
            ("gene_entity", _B1, [_entity_row("a", _B1)]),
            ("gene_content", _B2, [_entity_row("a", _B2)]),
        ],
    )
    run = json.loads((root / "run.json").read_text())
    run["batches"][0]["sha256"] = _hash("tampered")
    (root / "run.json").write_text(json.dumps(run))
    fake = _FakeLake(spark=_FakeSpark())
    cap = _capability(fake)
    result = cap.ingest_shards(root)
    verified = cap.verify_run(root, run_id=result["run_id"])
    assert not verified["ok"]
    assert any("block completion" in line for line in verified["discrepancies"])


def test_a_unit_whose_result_hash_is_not_in_the_registry_is_not_complete(tmp_path):
    """The UNIT REGISTRY CHECK: an unexplained parameter set blocks the unit."""
    root = _publish_run(
        tmp_path,
        [("gene_result", _B1, [_result_row("g1", _B1)])],
        units=["u1"],
    )
    fake = _FakeLake(spark=_FakeSpark(), rows=_preloaded_entities("g1"))
    cap = _capability(fake)
    result = cap.ingest_shards(root)
    verified = cap.verify_run(root, run_id=result["run_id"])
    [unit] = verified["units"]
    assert unit["ok"] is False
    assert unit["missing_parameter_sets"] == [_hash("p1")]
    assert not verified["ok"]

    # With the registry row present, the same unit completes.
    fake.rows.setdefault(PARAMETER_SET_TABLE, []).append(_registry_row("X"))
    again = cap.verify_run(root, run_id=result["run_id"])
    assert again["units"][0]["ok"] is True
    assert again["ok"], again["discrepancies"]


def test_verify_says_so_when_the_execution_is_unknown(tmp_path):
    root, fake = _simple_run(tmp_path, spark=_FakeSpark())
    verified = _capability(fake).verify_run(root, run_id="0NOSUCHEXECUTION00000000A")
    assert not verified["ok"]
    assert any("no inventory" in line for line in verified["discrepancies"])


# ===========================================================================
# The guards this verb shares with register(), proven on the ingest path
# ===========================================================================
# These moved here from test_clearinghouse_capability.py when the ingest
# stopped scanning directories and started reading the run inventory: the
# guards are unchanged, but proving them now needs a published run and a fake
# that answers the reads.


def test_a_probe_write_namespace_mismatch_refuses_with_zero_loads(tmp_path):
    """dev 1206: the probe read "default" while the write went elsewhere.

    An existence probe against a namespace the write never touches always
    finds nothing, so ``select_write_mode('append', table_exists=False)``
    returns ``'overwrite'`` and a requested append destroys the table. It must
    FAIL CLOSED, before any load() and before the ledger exists.
    """
    root, fake = _simple_run(tmp_path, spark=_FakeSpark())
    fake.resolve_probe_namespace = lambda **kwargs: "default"
    from kbutillib.domains.kbase.berdl.clearinghouse_capability import (
        ClearinghouseWriteTargetMismatchError,
    )

    with pytest.raises(ClearinghouseWriteTargetMismatchError):
        _capability(fake).ingest_shards(root)
    assert fake.load_calls == []
    assert not (tmp_path / "state").exists()

    # And in dry-run too: the guard precedes the read-only report.
    with pytest.raises(ClearinghouseWriteTargetMismatchError):
        _capability(fake).ingest_shards(root, dry_run=True)
    assert fake.load_calls == []


def test_a_null_postflight_is_a_failed_load_and_is_ledgered(tmp_path):
    """dev 1194: load() reports success with a null row_count. That is a
    FAILED load -- and it may fire after the rows landed, which is the correct
    direction to be wrong in, so the ledger must record the attempt."""
    root, fake = _simple_run(tmp_path, spark=_FakeSpark())
    from kbutillib.domains.kbase.berdl.clearinghouse_capability import (
        ClearinghouseLoadPostflightError,
    )

    real_load = fake.load

    def null_postflight(**kwargs):
        result = real_load(**kwargs)
        for report in result["tables"]:
            report["row_count"] = None
            report["row_count_error"] = "postflight could not read table"
        return result

    fake.load = null_postflight
    with pytest.raises(ClearinghouseLoadPostflightError):
        _capability(fake).ingest_shards(root)
    executions = list((tmp_path / "state").iterdir())
    records = ci.ExecutionLedger(executions[0]).records()
    assert any(r["state"] == ci.LEDGER_FAILED for r in records.values())
    assert any("Postflight" in r.get("error", "") for r in records.values())


def test_resuming_the_same_execution_skips_its_committed_batches(tmp_path):
    """Resume is the same call with the same execution id -- no second code
    path, and no re-append of a batch already proven committed."""
    root, fake = _simple_run(tmp_path, spark=_FakeSpark())
    cap = _capability(fake)
    first = cap.ingest_shards(root)
    assert len(fake.load_calls) == 2

    second = cap.ingest_shards(root, run_id=first["run_id"])
    assert [report["action"] for report in second["tables"]] == ["skip", "skip"]
    assert len(fake.load_calls) == 2
    assert [report["rows_ingested"] for report in second["tables"]] == [2, 1]


def test_another_executions_started_line_does_not_block_this_one(tmp_path):
    """A ledger belongs to ONE execution, in its own state directory, so a
    crashed execution's ambiguous batch cannot stall an unrelated load."""
    root, fake = _simple_run(tmp_path, spark=_FakeSpark())
    other = ci.ExecutionLedger(ci.execution_state_dir("0OTHEREXECUTION000000000A"))
    other.append(
        {"batch_id": _B1, "table": "gene_entity", "state": ci.LEDGER_STARTED}
    )
    result = _capability(fake).ingest_shards(root)
    assert [report["action"] for report in result["tables"]] == ["ingest", "ingest"]


def test_verify_folds_the_namespace_orphan_report_into_its_discrepancies(
    tmp_path,
):
    """verify_run runs lake-5's namespace-wide registry check and treats every
    finding as a discrepancy -- an orphaned result cannot be interpreted, which
    is the same class of problem as a missing row."""
    root, fake = _simple_run(tmp_path, spark=_FakeSpark())
    cap = _capability(fake)
    result = cap.ingest_shards(root)
    clean = cap.verify_run(root, run_id=result["run_id"])
    assert clean["ok"] and clean["parameter_set_integrity"]["failures"] == []

    cap.parameter_set_orphans = lambda **kwargs: {
        "ok": False,
        "failures": ["gene_result: 1 parameter_set_hash with no registry row"],
        "missing_from_registry": ["deadbeef"],
    }
    flagged = cap.verify_run(root, run_id=result["run_id"])
    assert not flagged["ok"]
    assert "no registry row" in " ".join(flagged["discrepancies"])
    # The batches this load proved are still proven: another writer's orphan
    # is not this execution's batch failing.
    assert all(report["ok"] for report in flagged["batches"])


# ===========================================================================
# 6. THE PRE-COMMIT JOIN ASSERTIONS -- AND THE VACUITY THEY EXIST TO BEAT
# ===========================================================================
#
# The defect: a load that joins nothing SUCCEEDS. Fill-only commits a batch
# with no surviving rows WITHOUT a write at all, which is exactly right for a
# re-run of a landed run and exactly wrong for a wrong join key -- and the two
# are the same envelope, the same zero and the same exit status. Measured
# 2026-10-21 on the function_to_go council run: the council's own
# `items.entity_hash` matched 0 of 19,268 clearinghouse hashes, and that load
# would have been reported as a clean idempotent no-op.
#
# THE TRAP, and it is why this defect class survives having tests: A
# RELATIONSHIP TEST EVALUATED OVER AN EMPTY JOIN PASSES VACUOUSLY. So the
# tests below prove the assertions are evaluated on the INPUTS, including the
# contrast with the same claim evaluated on the join's result.


def _result_rows_keyed(seeds, batch_id, *, entity_type="gene"):
    """Result rows whose entity_hash is the hash of each of ``seeds``.

    The seed is the COLUMN the producer hashed. Pass the right column's values
    and the keys resolve; pass the wrong column's and none of them do, which
    is the whole 0-of-19,268 case in one argument.
    """
    return [
        _result_row(seed, batch_id, entity_type=entity_type) for seed in seeds
    ]


def _wrong_column_run(tmp_path, *, right, wrong):
    """A shard run whose result batch was keyed on the WRONG column.

    ``right`` names the entities the target actually holds; ``wrong`` names
    what the producer hashed instead -- a definition text, say, or the
    council's own item hash. Returns ``(shard_root, fake_lake)``.
    """
    root = _publish_run(
        tmp_path,
        [
            (PARAMETER_SET_TABLE, _BP, [_registry_row(_BP)]),
            ("gene_result", _B1, _result_rows_keyed(wrong, _B1)),
        ],
        units=["u1", "u1"],
    )
    return root, _FakeLake(spark=_FakeSpark(), rows=_preloaded_entities(*right))


# -- the pure rule, one assertion at a time ---------------------------------


def test_an_empty_batch_is_a_producer_failure_not_a_clean_load():
    schema = _arrow([_entity_row("a", _B1)]).schema
    report = ci.join_assertions(
        pa.Table.from_pylist([], schema=schema),
        table="gene_entity",
        batch_id=_B1,
    )
    assert report["source_rows"] == 0
    assert not report["ok"]
    assert "EMPTY" in " ".join(report["failures"])


def test_a_null_join_key_is_reported_with_its_count():
    rows = [_entity_row("a", _B1), {**_entity_row("b", _B1), "entity_hash": None}]
    report = ci.join_assertions(_arrow(rows), table="gene_entity", batch_id=_B1)
    assert report["source_rows"] == 2
    assert report["null_keys"] == 1
    assert report["source_keys"] == 1
    assert not report["ok"]
    assert "NULL entity_hash" in " ".join(report["failures"])


def test_two_rows_claiming_one_target_slot_are_ambiguous():
    """The slot key is the whole five-tuple, so this is a genuine collision.

    Which of the two the current-state derivation picks is then decided by an
    observed_at tie-break rather than by the producer -- and the sharder
    deduplicates within a batch, so a violation is a producer bug.
    """
    rows = _result_rows_keyed(["g1", "g1"], _B1)
    report = ci.join_assertions(_arrow(rows), table="gene_result", batch_id=_B1)
    assert report["slot_key"] == list(SLOT_KEY_COLUMNS)
    assert report["ambiguous_slots"] == 1
    assert not report["ok"]
    assert "claimed by more than one source row" in " ".join(report["failures"])


def test_two_slots_for_one_key_are_not_ambiguous():
    """One entity legitimately carries results under two parameter sets.

    The arc preset's TRANSYT rows do exactly this -- the parameter set varies
    per record -- so a check that refused it would refuse a correct producer.
    """
    rows = [
        _result_row("g1", _B1, parameter_set="p1"),
        _result_row("g1", _B1, parameter_set="p2"),
    ]
    report = ci.join_assertions(
        _arrow(rows),
        table="gene_result",
        batch_id=_B1,
        resolvable={_hash("g1")},
    )
    assert report["ambiguous_slots"] == 0
    assert report["source_keys"] == 1
    assert report["coverage"] == 1.0
    assert report["ok"]


def test_coverage_is_counted_and_full_coverage_passes():
    rows = _result_rows_keyed(["g1", "g2"], _B1)
    report = ci.join_assertions(
        _arrow(rows),
        table="gene_result",
        batch_id=_B1,
        resolvable={_hash("g1"), _hash("g2"), _hash("g3")},
    )
    assert (report["source_keys"], report["resolved_keys"]) == (2, 0 + 2)
    assert report["unresolved_keys"] == 0
    assert report["resolvable_keys"] == 3
    assert report["coverage"] == 1.0
    assert report["ok"]


def test_partial_coverage_fails_the_default_floor_and_says_how_short():
    rows = _result_rows_keyed(["g1", "g2"], _B1)
    report = ci.join_assertions(
        _arrow(rows), table="gene_result", batch_id=_B1, resolvable={_hash("g1")}
    )
    assert (report["resolved_keys"], report["unresolved_keys"]) == (1, 1)
    assert report["coverage"] == 0.5
    assert report["coverage_floor"] == ci.DEFAULT_COVERAGE_FLOOR == 1.0
    assert not report["ok"]
    assert "below the declared floor" in " ".join(report["failures"])
    # A DECLARED floor is what makes a partial load expressible.
    relaxed = ci.join_assertions(
        _arrow(rows),
        table="gene_result",
        batch_id=_B1,
        resolvable={_hash("g1")},
        coverage_floor=0.5,
    )
    assert relaxed["ok"]


def test_zero_resolution_is_fatal_whatever_the_floor_says():
    """A floor expresses tolerance for a PARTIAL mismatch.

    Total non-resolution is not partial: it is a wrong join key, and
    ``--coverage-floor 0`` must not be a way to wave it through.
    """
    rows = _result_rows_keyed(["wrong1", "wrong2"], _B1)
    report = ci.join_assertions(
        _arrow(rows),
        table="gene_result",
        batch_id=_B1,
        resolvable={_hash("right1")},
        coverage_floor=0.0,
    )
    assert report["resolved_keys"] == 0
    assert report["coverage"] == 0.0
    assert not report["ok"]
    assert "ZERO of 2" in " ".join(report["failures"])
    assert "WRONG JOIN KEY" in " ".join(report["failures"])
    with pytest.raises(ci.ClearinghouseJoinAssertionError, match="ZERO of 2"):
        ci.raise_for_join_report(report)


def test_coverage_is_skipped_rather_than_passed_for_identity_batches():
    """An entity batch MINTS identity, so it has nothing to resolve against.

    Recorded as skipped and not as passing: "this check did not apply" and
    "this check succeeded" are different facts, and only the first one is true.
    """
    report = ci.join_assertions(
        _arrow([_entity_row("a", _B1)]), table="gene_entity", batch_id=_B1
    )
    assert report["coverage_checked"] is False
    assert report["coverage"] is None and report["resolved_keys"] is None
    assert report["ok"]


def test_a_missing_join_key_column_refuses_rather_than_passing():
    with pytest.raises(ValueError, match="no join key column"):
        ci.join_assertions(
            _arrow([{"ingest_batch_id": _B1}]), table="gene_entity", batch_id=_B1
        )


# -- THE TRAP, proven: inputs versus the join's result ----------------------


def test_the_assertion_is_on_the_inputs_not_on_the_join_result(tmp_path):
    """THE PROOF. The same claim is vacuous on the join and decisive on the inputs.

    "Every key that resolved is non-null and unambiguous" is TRUE of zero
    rows, so a relationship test evaluated over the resolution join's output
    passes on exactly the input that is broken. The assertion here is computed
    from the batch's own rows and the target's key space read independently,
    so it reports 0 of 3 and fails.
    """
    wrong = ["wrong1", "wrong2", "wrong3"]
    batch = _arrow(_result_rows_keyed(wrong, _B1))
    resolvable = {_hash("right1"), _hash("right2"), _hash("right3")}

    # (a) The RESULT of the resolution join: empty, because nothing matches.
    joined = [
        key
        for key in batch.column("entity_hash").to_pylist()
        if key in resolvable
    ]
    assert joined == []
    # Every relationship claim over it is vacuously true.
    assert all(key is not None for key in joined)
    assert len(set(joined)) == len(joined)

    # (b) The same claims on the INPUTS: decisive.
    report = ci.join_assertions(
        batch, table="gene_result", batch_id=_B1, resolvable=resolvable
    )
    assert report["source_keys"] == 3 and report["resolved_keys"] == 0
    assert not report["ok"]


# -- end to end: a wrong-column shard run exits non-zero -------------------


def test_a_wrong_column_shard_run_is_refused_before_anything_is_committed(
    tmp_path,
):
    """Hash the WRONG column and the result batch never reaches load().

    The registry batch ahead of it still lands -- one producer defect in one
    batch does not invalidate the batches that committed -- and the refusal is
    LEDGERED, so verify_run blocks on it and it cannot be lost.
    """
    root, fake = _wrong_column_run(
        tmp_path, right=["g1", "g2"], wrong=["text:g1", "text:g2"]
    )
    cap = _capability(fake)
    result = cap.ingest_shards(root)
    by_table = {report["name"]: report for report in result["tables"]}

    refused = by_table["gene_result"]
    assert refused["action"] == "refused"
    assert refused["join_assertions"]["resolved_keys"] == 0
    assert refused["join_assertions"]["source_keys"] == 2
    assert "ZERO of 2" in refused["error"]
    # Nothing was committed for it: no rows, and load() was never called.
    assert "gene_result" not in fake.rows
    assert [call["tables"][0]["name"] for call in fake.load_calls] == [
        PARAMETER_SET_TABLE
    ]
    # Ledgered as FAILED, with the counts, so it blocks verification.
    ledger = ci.ExecutionLedger(Path(result["state_dir"])).records()
    failed = [r for r in ledger.values() if r["state"] == ci.LEDGER_FAILED]
    assert len(failed) == 1
    assert failed[0]["join_assertions"]["coverage"] == 0.0
    assert not cap.verify_run(root, run_id=result["run_id"])["ok"]


def test_the_same_run_keyed_on_the_right_column_lands(tmp_path):
    """The control. Without it the test above could be passing for any reason."""
    root, fake = _wrong_column_run(tmp_path, right=["g1", "g2"], wrong=["g1", "g2"])
    result = _capability(fake).ingest_shards(root)
    by_table = {report["name"]: report for report in result["tables"]}
    assert by_table["gene_result"]["action"] == "ingest"
    assert by_table["gene_result"]["rows_ingested"] == 2
    assert by_table["gene_result"]["join_assertions"]["coverage"] == 1.0
    assert len(fake.rows["gene_result"]) == 2


def test_the_dry_run_gate_catches_a_wrong_column_too(tmp_path):
    """--dry-run is the operator's last gate, so it must run the assertions.

    A gate that passed exactly the load that fails would be worse than no gate.
    """
    root, fake = _wrong_column_run(
        tmp_path, right=["g1"], wrong=["council-item:g1"]
    )
    result = _capability(fake).ingest_shards(root, dry_run=True)
    by_table = {report["name"]: report for report in result["tables"]}
    assert by_table["gene_result"]["action"] == "would_refuse"
    assert by_table["gene_result"]["join_assertions"]["resolved_keys"] == 0
    # Genuinely read-only: no load(), no ledger.
    assert fake.load_calls == []
    assert not (Path(result["state_dir"]) / ci.LEDGER_FILENAME).exists()


def test_the_declared_floor_and_deadline_are_echoed_in_the_result(tmp_path):
    """A coverage number with no floor beside it cannot be judged."""
    root, fake = _wrong_column_run(tmp_path, right=["g1"], wrong=["g1"])
    result = _capability(fake).ingest_shards(
        root, coverage_floor=0.75, transfer_deadline_seconds=99
    )
    assert result["coverage_floor"] == 0.75
    assert result["transfer_deadline_seconds"] == 99


def test_an_out_of_range_floor_is_an_operator_error_not_a_batch_refusal(tmp_path):
    """Raised ONCE, before the first batch, and never ledgered.

    ``_join_assertions`` turns a ValueError into a per-batch refusal because a
    batch with no join-key column genuinely is a producer defect. An
    out-of-range floor is not -- reporting it once per batch would read as the
    DATA being wrong.
    """
    root, fake = _wrong_column_run(tmp_path, right=["g1"], wrong=["g1"])
    with pytest.raises(ValueError, match="outside"):
        _capability(fake).ingest_shards(root, coverage_floor=1.5)
    assert fake.load_calls == []


def test_kbu_clearinghouse_load_exits_nonzero_on_a_wrong_column_run(tmp_path, monkeypatch):
    """THROUGH THE CLI, because the exit status is what a script reads.

    Before this, a non-empty shard run that resolved zero rows exited 0: the
    envelope said an action and a zero, and "nothing was written" was
    indistinguishable from "everything was already there".
    """
    from click.testing import CliRunner

    from kbutillib.interfaces.cli import clearinghouse as ch

    root, fake = _wrong_column_run(
        tmp_path, right=["g1", "g2"], wrong=["text:g1", "text:g2"]
    )
    cap = _capability(fake)
    monkeypatch.setattr(ch, "_capability", lambda: cap)
    result = CliRunner().invoke(
        ch.clearinghouse_cmd, ["load", str(root), "--json"], catch_exceptions=False
    )
    assert result.exit_code == 1, result.output
    envelope = json.loads(result.output.strip().splitlines()[-1])
    refused = [
        row for row in envelope["data"]["tables"] if row["action"] == "refused"
    ]
    assert len(refused) == 1
    assert refused[0]["join_assertions"]["resolved_keys"] == 0
    # The envelope is STILL published -- the operator needs the counts -- and
    # the failure is in warnings, which the contract says is never empty for a
    # degraded answer.
    assert any("ZERO of 2" in line for line in envelope["warnings"])
    assert envelope["data"]["coverage_floor"] == 1.0


def test_kbu_clearinghouse_load_exits_nonzero_while_a_batch_is_still_syncing(
    tmp_path, monkeypatch
):
    """A load that has not finished arriving is not a completed load."""
    from click.testing import CliRunner

    from kbutillib.interfaces.cli import clearinghouse as ch

    root = _mismatched_run(tmp_path)
    cap = _capability(_FakeLake(spark=_FakeSpark()))
    monkeypatch.setattr(ch, "_capability", lambda: cap)
    result = CliRunner().invoke(
        ch.clearinghouse_cmd, ["load", str(root), "--json"], catch_exceptions=False
    )
    assert result.exit_code == 1, result.output
    envelope = json.loads(result.output.strip().splitlines()[-1])
    assert any("STILL SYNCING" in line for line in envelope["warnings"])
    assert envelope["data"]["transfer_deadline_seconds"] == (
        ci.DEFAULT_TRANSFER_DEADLINE_SECONDS
    )


# ===========================================================================
# 7. THE RECEIPT -- reconciled against ITEMS, and RE-EXPORTABLE
# ===========================================================================


def _publish_record(tmp_path, counts, *, declines=None):
    """Write a producer PUBLISH.json beside a shard dir, as council-publish does."""
    path = tmp_path / ci.PUBLISH_RECORD_FILENAME
    path.write_text(
        json.dumps(
            {"counts": dict(counts), "declines_by_reason": dict(declines or {})}
        )
    )
    return path


def test_the_receipt_reconciles_against_items_submitted_not_rows_written(tmp_path):
    """"Loaded 19,268" is not a reconciliation when the plan submitted 19,276.

    The eight residual items are the interesting number, and only the producer
    can count them: an item whose every call failed produced NO row, so it is
    invisible to the shard run and to everything downstream of the export.
    """
    root = _publish_run(
        tmp_path,
        [
            (PARAMETER_SET_TABLE, _BP, [_registry_row(_BP)]),
            ("gene_result", _B1, _result_rows_keyed(["g1", "g2"], _B1)),
        ],
        units=["u1", "u1"],
    )
    _publish_record(
        tmp_path,
        {
            "items_submitted": 3,
            "items_with_verdict": 2,
            "items_unresolved": 1,
        },
        declines={"abstained:no_candidates": 1},
    )
    fake = _FakeLake(spark=_FakeSpark(), rows=_preloaded_entities("g1", "g2"))
    cap = _capability(fake)
    loaded = cap.ingest_shards(root)
    verified = cap.verify_run(root, run_id=loaded["run_id"])
    receipt = ci.build_receipt(
        verified, publish_record=ci.read_publish_record(root)
    )

    assert receipt["items_submitted"] == 3
    assert receipt["items_with_verdict"] == 2
    assert receipt["items_unresolved"] == 1
    # ROWS, which are not items: one item can carry several GO terms, so the
    # two are reported separately and neither stands in for the other.
    assert receipt["result_rows_written"] == 2
    assert receipt["declines_by_reason"] == {"abstained:no_candidates": 1}
    assert receipt["parameter_set_hashes"] == [_hash("p1")]
    assert receipt["snapshot_ids"]["gene_result"]
    assert receipt["reconciled"] is True
    assert receipt["ok"] is True
    # The residual is SAID, not swallowed.
    assert any("UNRESOLVED" in note for note in receipt["notes"])


def test_the_receipt_declines_to_guess_an_item_count_it_does_not_have(tmp_path):
    """No PUBLISH.json: the item fields are null with a note, never a row count."""
    root, fake = _wrong_column_run(tmp_path, right=["g1"], wrong=["g1"])
    cap = _capability(fake)
    loaded = cap.ingest_shards(root)
    verified = cap.verify_run(root, run_id=loaded["run_id"])
    receipt = ci.build_receipt(
        verified, publish_record=ci.read_publish_record(root)
    )
    assert receipt["items_submitted"] is None
    assert receipt["items_with_verdict"] is None
    assert receipt["result_rows_written"] == 1
    assert receipt["reconciled"] is False
    assert any(ci.PUBLISH_RECORD_FILENAME in note for note in receipt["notes"])


def test_the_receipt_is_re_exportable_and_reproduces_byte_for_byte(tmp_path):
    """RE-EXPORTABLE, NOT A ONE-SHOT.

    The Iceberg commit and the LOADED.json write are two unrelated writes with
    no transaction between them, so a crash in the gap loses the
    ACKNOWLEDGEMENT while the data sits safely landed -- and a producer that
    read "no receipt" as "not loaded" would ask for a reload of rows that are
    already there. Verify is derivable from the ledger and the snapshot
    history, so re-running it reproduces the same receipt.
    """
    root = _publish_run(
        tmp_path,
        [
            (PARAMETER_SET_TABLE, _BP, [_registry_row(_BP)]),
            ("gene_result", _B1, _result_rows_keyed(["g1"], _B1)),
        ],
        units=["u1", "u1"],
    )
    _publish_record(
        tmp_path,
        {"items_submitted": 1, "items_with_verdict": 1, "items_unresolved": 0},
    )
    fake = _FakeLake(spark=_FakeSpark(), rows=_preloaded_entities("g1"))
    cap = _capability(fake)
    loaded = cap.ingest_shards(root)
    publish = ci.read_publish_record(root)

    first = ci.build_receipt(
        cap.verify_run(root, run_id=loaded["run_id"]), publish_record=publish
    )
    # The acknowledgement is lost: written, then gone.
    path = ci.write_receipt(tmp_path / ci.RECEIPT_FILENAME, first)
    body = path.read_text()
    path.unlink()

    # Re-exported from nothing but the ledger, the inventory and the table.
    second = ci.build_receipt(
        cap.verify_run(root, run_id=loaded["run_id"]), publish_record=publish
    )
    assert second == first
    assert ci.write_receipt(path, second).read_text() == body
    # No timestamp and no nonce in the body -- that is what makes it
    # comparable to the receipt that was lost.
    assert "generated_at" not in body


def test_kbu_clearinghouse_verify_carries_the_receipt_and_can_write_it(
    tmp_path, monkeypatch
):
    """Through the CLI: data.receipt is the receipt, and --receipt files it."""
    from click.testing import CliRunner

    from kbutillib.interfaces.cli import clearinghouse as ch

    root = _publish_run(
        tmp_path,
        [
            (PARAMETER_SET_TABLE, _BP, [_registry_row(_BP)]),
            ("gene_result", _B1, _result_rows_keyed(["g1"], _B1)),
        ],
        units=["u1", "u1"],
    )
    _publish_record(
        tmp_path,
        {"items_submitted": 2, "items_with_verdict": 1, "items_unresolved": 1},
        declines={"contentless": 1},
    )
    fake = _FakeLake(spark=_FakeSpark(), rows=_preloaded_entities("g1"))
    cap = _capability(fake)
    loaded = cap.ingest_shards(root)
    monkeypatch.setattr(ch, "_capability", lambda: cap)

    receipt_path = tmp_path / ci.RECEIPT_FILENAME
    result = CliRunner().invoke(
        ch.clearinghouse_cmd,
        [
            "verify",
            str(root),
            "--run-id",
            loaded["run_id"],
            "--receipt",
            str(receipt_path),
            "--json",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.output.strip().splitlines()[-1])
    receipt = envelope["data"]["receipt"]
    assert receipt["items_submitted"] == 2
    assert receipt["items_unresolved"] == 1
    assert receipt["declines_by_reason"] == {"contentless": 1}
    assert receipt["parameter_set_hashes"] == [_hash("p1")]
    assert envelope["data"]["receipt_file"] == str(receipt_path)
    assert json.loads(receipt_path.read_text()) == receipt


# ===========================================================================
# 8. THE ARRIVAL DECISION, as pure arithmetic over run.json's mtime
# ===========================================================================


def _entry_for(root, table="gene_entity", batch_id=None):
    run = json.loads((root / "run.json").read_text())
    entry = dict(run["batches"][0])
    # The ingest reads the digest under its PROVENANCE name, because a load
    # execution mints its own batch ids and keeps the sharder's as source.
    entry["source_sha256"] = entry["sha256"]
    return entry


def test_an_arrived_batch_hands_back_the_digest_it_streamed(tmp_path):
    """So the five-check gate does not hash a full-size batch a second time."""
    root = _publish_run(tmp_path, [("gene_entity", _B1, [_entity_row("a", _B1)])])
    entry = _entry_for(root)
    state = ci.transfer_state(
        entry, shard_root=root, manifest_mtime=ci.run_manifest_mtime(root)
    )
    assert ci.has_arrived(state)
    assert state["observed_sha256"] == entry["source_sha256"]
    # The gate accepts it instead of re-reading, and its answer is unchanged.
    table = ci.verify_batch_file(
        entry,
        shard_root=root,
        expected_batch_id=_B1,
        expected_sha256=entry["source_sha256"],
        observed_sha256=state["observed_sha256"],
    )
    assert table.num_rows == 1
    # And a WRONG pre-computed digest still refuses -- the check is the same
    # check, not a check that was skipped.
    with pytest.raises(ci.ClearinghouseBatchIntegrityError, match="sha256"):
        ci.verify_batch_file(
            entry,
            shard_root=root,
            expected_batch_id=_B1,
            expected_sha256=entry["source_sha256"],
            observed_sha256=_hash("not the bytes"),
        )


@pytest.mark.parametrize(
    "elapsed_s,expected",
    [
        (0, "syncing"),
        (ci.DEFAULT_TRANSFER_DEADLINE_SECONDS - 1, "syncing"),
        (ci.DEFAULT_TRANSFER_DEADLINE_SECONDS, "syncing"),
        (ci.DEFAULT_TRANSFER_DEADLINE_SECONDS + 1, "failed_delivery"),
    ],
)
def test_only_elapsed_time_separates_sync_lag_from_a_lost_file(
    tmp_path, elapsed_s, expected
):
    """A FIRST OBSERVATION CANNOT TELL THEM APART.

    The on-disk condition is identical in all four rows: the file is absent.
    What changes is how long it has been absent SINCE RUN.JSON WAS WRITTEN --
    which is the only thing that can distinguish normal sync lag from a
    delivery that will never complete.
    """
    root = _publish_run(tmp_path, [("gene_entity", _B1, [_entity_row("a", _B1)])])
    entry = _entry_for(root)
    (root / entry["file"]).unlink()
    mtime = ci.run_manifest_mtime(root)
    state = ci.transfer_state(
        entry, shard_root=root, manifest_mtime=mtime, now=mtime + elapsed_s
    )
    assert state["state"] == expected
    assert state["reason"] == ci.TRANSFER_ABSENT
    assert state["elapsed_s"] == elapsed_s
    assert state["deadline_seconds"] == ci.DEFAULT_TRANSFER_DEADLINE_SECONDS


def test_the_deadline_is_measured_from_the_marker_and_not_from_the_attempt(
    tmp_path,
):
    """Otherwise every re-run would reset the clock and nothing would ever fail.

    An operator re-running a stuck load is the EXPECTED response to "still
    syncing", so a deadline measured from the attempt would make the
    failed-delivery verdict unreachable in exactly the case it exists for.
    """
    root = _publish_run(tmp_path, [("gene_entity", _B1, [_entity_row("a", _B1)])])
    entry = _entry_for(root)
    (root / entry["file"]).unlink()
    marker = ci.run_manifest_mtime(root)
    old = marker - ci.DEFAULT_TRANSFER_DEADLINE_SECONDS * 2
    os.utime(root / "run.json", (old, old))
    state = ci.transfer_state(
        entry, shard_root=root, manifest_mtime=ci.run_manifest_mtime(root), now=marker
    )
    assert state["state"] == "failed_delivery"


def test_a_vanished_run_json_is_not_a_transfer_in_progress(tmp_path):
    """No marker means no clock to be patient against."""
    root = _publish_run(tmp_path, [("gene_entity", _B1, [_entity_row("a", _B1)])])
    entry = _entry_for(root)
    (root / entry["file"]).unlink()
    state = ci.transfer_state(entry, shard_root=root, manifest_mtime=None)
    assert state["state"] == "failed_delivery"
    assert state["elapsed_s"] is None
    with pytest.raises(ci.ClearinghouseFailedDeliveryError):
        ci.raise_for_transfer(state)


def test_a_path_that_escapes_the_root_is_not_waited_out(tmp_path):
    """It is refused outright by the normal gate; waiting could never fix it."""
    root = _publish_run(tmp_path, [("gene_entity", _B1, [_entity_row("a", _B1)])])
    entry = {**_entry_for(root), "file": "../outside.parquet"}
    state = ci.transfer_state(entry, shard_root=root, manifest_mtime=None)
    assert ci.has_arrived(state)
    # NO digest is handed on: the gate must re-read and refuse, rather than be
    # given a digest for a file outside the root.
    assert state["observed_sha256"] is None
    with pytest.raises(ci.ClearinghouseBatchIntegrityError, match="OUTSIDE"):
        ci.verify_batch_file(
            entry,
            shard_root=root,
            expected_batch_id=_B1,
            expected_sha256=entry["source_sha256"],
            observed_sha256=state["observed_sha256"],
        )


def test_the_syncing_message_tells_the_operator_what_to_do(tmp_path):
    root = _publish_run(tmp_path, [("gene_entity", _B1, [_entity_row("a", _B1)])])
    entry = _entry_for(root)
    (root / entry["file"]).unlink()
    mtime = ci.run_manifest_mtime(root)
    syncing = ci.transfer_state(
        entry, shard_root=root, manifest_mtime=mtime, now=mtime
    )
    message = ci.describe_transfer(syncing)
    assert "STILL SYNCING" in message
    assert "Re-run the load" in message
    assert str(entry["batch_id"]) in message
    with pytest.raises(ci.ClearinghouseTransferIncompleteError):
        ci.raise_for_transfer(syncing)

    failed = ci.transfer_state(
        entry,
        shard_root=root,
        manifest_mtime=mtime,
        now=mtime + ci.DEFAULT_TRANSFER_DEADLINE_SECONDS + 1,
    )
    evidence = ci.describe_transfer(failed)
    assert "FAILED DELIVERY" in evidence
    assert "re-sync the shard directory" in evidence
    # Expected versus observed, both sides, both fields.
    assert f"expected sha256 {entry['source_sha256']}" in evidence
    assert f"{entry['bytes']} byte(s)" in evidence


def test_the_human_render_prints_a_count_for_every_assertion(tmp_path):
    """The COUNTS are the diagnostic, so they must survive the human path too.

    Projected out of the same dict --json serialises (output-contract rule 1),
    never recomputed; a nested report in a fixed-width cell is unreadable.
    """
    import click.testing

    from kbutillib.interfaces.cli import clearinghouse as ch

    root, fake = _wrong_column_run(
        tmp_path, right=["g1", "g2"], wrong=["text:g1", "text:g2"]
    )
    envelope = ch._build_load(
        _capability(fake),
        shard_dir=str(root),
        run_id=None,
        dry_run=True,
        reconcile=False,
    )
    runner = click.testing.CliRunner()
    with runner.isolation() as streams:
        ch._render(envelope)
    text = streams[0].getvalue().decode()
    assert "coverage floor=1.0" in text
    line = [row for row in text.splitlines() if row.startswith("assertions ")][-1]
    for field in (
        "rows=2",
        "keys=2",
        "null_keys=0",
        "ambiguous_slots=0",
        "resolved=0",
        "coverage=0.0",
        "ok=False",
    ):
        assert field in line, line
    # The nested report is NOT left in the fixed-width table.
    assert "join_assertions" not in text
