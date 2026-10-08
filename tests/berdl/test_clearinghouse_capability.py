"""Unit tests for ``ClearinghouseCapability`` -- the READ half of the API.

Pure logic, no network, no live BERDL pod, no Spark: every scenario drives
:class:`~kbutillib.domains.kbase.berdl.clearinghouse_capability.ClearinghouseCapability`
against a hand-built fake capability (:class:`_FakeCapability` below) that
records every ``query()`` call -- its SQL text, bound params, engine, and
(off-pod) limit/offset -- and returns canned rows. That fake stands in for
the lakehouse boundary: it lets these tests prove the READER's contract (the
five hard rules and the seven success criteria in
``clearinghouse_capability.py``'s module docstring) WITHOUT proving anything
actually works against real Iceberg-on-Polaris. Off-pod is unproven end to
end; see the task work-record.

The success criteria these map to (from the module docstring):
    (a) each read verb queries the table ``table_name()`` names, for all
        five entity types -- ``test_criterion_a_*``
    (b) a two-type batch issues two queries against two tables or raises,
        never one -- ``test_criterion_b_*``
    (c) uppercase and lowercase hex produce the SAME bound parameter --
        ``test_criterion_c_*``
    (d) a malformed digest raises at the boundary before any query --
        ``test_criterion_d_*``
    (e) no emitted in-pod SQL text contains a 64-char hex run --
        ``test_criterion_e_*``
    (f) off-pod a 12000-hash lookup is chunked to <= the 5000 page cap and
        reassembled without duplicates; in-pod it is NOT chunked at that
        size -- ``test_criterion_f_*``
    (g) stats() derives row counts from an explicit COUNT(*), never from a
        transport ``row_count`` field -- ``test_criterion_g_*``
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from kbutillib.domains.identity import (
    DEFAULT_PARAMETER_SET_HASH,
    ParameterSetError,
    canonical_parameter_set,
    parameter_set_hash,
)
from kbutillib.domains.kbase.berdl.capability import BerdlLoadRefusedError
from kbutillib.domains.kbase.berdl.clearinghouse_capability import (
    _HEX_RUN_RE,
    _quote_fqn,
    LEDGER_FILENAME,
    OFFPOD_PAGE_CAP,
    SPARK_PROMOTION_THRESHOLD,
    ClearinghouseCapability,
    ClearinghouseLedgerAmbiguousError,
    ClearinghouseLoadPostflightError,
    ClearinghousePartialWriteError,
    ClearinghouseWriteTargetMismatchError,
    order_shards_for_load,
)
from kbutillib.domains.kbase.berdl.clearinghouse_schema import (
    ENTITY_TYPES,
    NAMESPACE,
    PARAMETER_SET_TABLE,
    TENANT,
    table_configs,
    table_name,
)


def _hash(seed: int) -> str:
    """Return a deterministic, valid 64-char lowercase hex digest."""
    return f"{seed:064x}"


class _Call:
    """One recorded ``query()`` call."""

    def __init__(self, sql, params, engine, limit, offset):
        self.sql = sql
        self.params = params
        self.engine = engine
        self.limit = limit
        self.offset = offset


class _FakeCapability:
    """Records every ``query()`` call and returns canned rows.

    Args:
        locus: ``"in_pod"`` or ``"off_pod"`` -- what :meth:`locus` reports.
        responder: ``callable(_Call) -> rows`` producing the rows a call
            returns. In-pod rows are returned as-is (list of dicts); off-pod
            rows are wrapped in the ``{'success', 'data', 'row_count', ...}``
            envelope :class:`OffPodTransport` returns. Defaults to no rows.
    """

    def __init__(self, locus="in_pod", responder=None):
        self._locus = locus
        self._responder = responder or (lambda call: [])
        self.calls: list[_Call] = []

    def locus(self):
        return self._locus

    def query(self, sql, *, params=None, engine=None, limit=None, offset=0):
        # Mirror BerdlCapability.query's contract: params only bind in-pod on
        # Trino. Off-pod the reader must never pass params (it inlines first).
        if self._locus == "off_pod" and params is not None:
            raise AssertionError(
                "off-pod query() received params; reader must inline instead"
            )
        call = _Call(sql, params, engine, limit, offset)
        self.calls.append(call)
        rows = self._responder(call)
        if self._locus == "off_pod":
            return {
                "success": True,
                "data": list(rows),
                "columns": [],
                "row_count": len(rows),
                "query": sql,
                "error": None,
            }
        return rows


# --------------------------------------------------------------------------
# Criterion (a): each read verb hits the table table_name() names, all types
# --------------------------------------------------------------------------


@pytest.mark.parametrize("entity_type", ENTITY_TYPES)
def test_criterion_a_known_queries_entity_table(entity_type):
    fake = _FakeCapability()
    cap = ClearinghouseCapability(fake)
    cap.known(entity_type, [_hash(1)])
    assert len(fake.calls) == 1
    assert table_name(entity_type, "entity") in fake.calls[0].sql


@pytest.mark.parametrize("entity_type", ENTITY_TYPES)
def test_criterion_a_content_queries_content_table(entity_type):
    fake = _FakeCapability()
    cap = ClearinghouseCapability(fake)
    cap.content(entity_type, [_hash(1)])
    assert table_name(entity_type, "content") in fake.calls[0].sql


@pytest.mark.parametrize("entity_type", ENTITY_TYPES)
def test_criterion_a_results_queries_result_table(entity_type):
    fake = _FakeCapability()
    cap = ClearinghouseCapability(fake)
    cap.results(entity_type, [_hash(1)])
    assert table_name(entity_type, "result") in fake.calls[0].sql


@pytest.mark.parametrize("entity_type", ENTITY_TYPES)
def test_criterion_a_current_state_queries_result_table(entity_type):
    fake = _FakeCapability()
    cap = ClearinghouseCapability(fake)
    cap.current_state(entity_type)
    assert table_name(entity_type, "result") in fake.calls[0].sql


def test_criterion_a_content_all_types_uses_union_view():
    fake = _FakeCapability()
    cap = ClearinghouseCapability(fake)
    cap.content_all_types([_hash(1)])
    assert "all_content" in fake.calls[0].sql
    # And it must NOT name any single-type content table.
    for etype in ENTITY_TYPES:
        assert table_name(etype, "content") not in fake.calls[0].sql


# --------------------------------------------------------------------------
# Criterion (b): a two-type batch is split per type -- two tables, or raise;
#                never probed as one set. (Rule 3: standardizer collision.)
# --------------------------------------------------------------------------


def test_criterion_b_no_verb_accepts_a_set_of_types():
    """There is no read verb taking a *collection* of entity types.

    Rule 3 is enforced structurally: the entity type is a single positional
    argument on every hash verb, so a caller physically cannot hand one call
    a mixed-type batch. Splitting per type is the caller's obligation, and
    each split is a separate call against a separate table.
    """
    fake = _FakeCapability()
    cap = ClearinghouseCapability(fake)
    # A caller with two types must issue two calls -> two tables.
    cap.known("gene", [_hash(1)])
    cap.known("protein", [_hash(1)])
    assert len(fake.calls) == 2
    assert table_name("gene", "entity") in fake.calls[0].sql
    assert table_name("protein", "entity") in fake.calls[1].sql
    # The two calls hit DIFFERENT tables even for a colliding hash.
    assert fake.calls[0].sql != fake.calls[1].sql


def test_criterion_b_unknown_type_raises_before_query():
    fake = _FakeCapability()
    cap = ClearinghouseCapability(fake)
    with pytest.raises(ValueError):
        cap.known("polypeptide", [_hash(1)])
    assert fake.calls == []


# --------------------------------------------------------------------------
# Criterion (c): uppercase and lowercase hex bind to the SAME parameter.
# --------------------------------------------------------------------------


def test_criterion_c_uppercase_and_lowercase_bind_identically():
    lower = _hash(0xABC)
    upper = lower.upper()
    assert lower != upper  # precondition: they differ as raw strings

    fake_lower = _FakeCapability()
    fake_upper = _FakeCapability()
    ClearinghouseCapability(fake_lower).known("gene", [lower])
    ClearinghouseCapability(fake_upper).known("gene", [upper])

    assert fake_lower.calls[0].params == fake_upper.calls[0].params
    # And the bound value is the stored lowercase form.
    assert fake_lower.calls[0].params == [lower]
    assert all(p == p.lower() for p in fake_upper.calls[0].params)


# --------------------------------------------------------------------------
# Criterion (d): a malformed digest raises at the boundary, before any query.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "xyz",  # not hex
        "abc",  # too short
        _hash(1) + "00",  # too long
        _hash(1)[:-1] + "g",  # non-hex char
        "",  # empty
    ],
)
def test_criterion_d_malformed_hash_raises_before_query(bad):
    fake = _FakeCapability()
    cap = ClearinghouseCapability(fake)
    with pytest.raises(ValueError):
        cap.known("gene", [_hash(1), bad])
    assert fake.calls == []  # nothing queried


def test_criterion_d_malformed_hash_in_content_raises_before_query():
    fake = _FakeCapability()
    cap = ClearinghouseCapability(fake)
    with pytest.raises(ValueError):
        cap.content("protein", ["not-a-hash"])
    assert fake.calls == []


# --------------------------------------------------------------------------
# Criterion (e): no emitted in-pod SQL text contains a 64-char hex run.
# --------------------------------------------------------------------------


def test_criterion_e_inpod_sql_has_no_hex_run():
    fake = _FakeCapability(locus="in_pod")
    cap = ClearinghouseCapability(fake)
    cap.known("gene", [_hash(1), _hash(2), _hash(3)])
    call = fake.calls[0]
    assert _HEX_RUN_RE.search(call.sql) is None, call.sql
    # The hashes live in the bound params instead.
    assert call.params == [_hash(1), _hash(2), _hash(3)]
    # Placeholders, not literals, in the SQL.
    assert call.sql.count("?") == 3


def test_criterion_e_offpod_inlines_but_only_validated_hex():
    """Off-pod there is no bind channel, so the hash DOES reach SQL text --
    but only after re-validation, so criterion (e)'s guarantee is explicitly
    in-pod-only and the off-pod inlined value is still provably safe hex.
    """
    fake = _FakeCapability(locus="off_pod")
    cap = ClearinghouseCapability(fake)
    cap.known("gene", [_hash(1)])
    call = fake.calls[0]
    # Off-pod the reader inlines (no params passed to the transport)...
    assert call.params is None
    # ...and what it inlines is the exact validated lowercase hex, quoted.
    assert f"'{_hash(1)}'" in call.sql


# --------------------------------------------------------------------------
# Criterion (f): off-pod chunking + dedup reassembly; in-pod not chunked.
# --------------------------------------------------------------------------


def test_criterion_f_offpod_12000_chunked_to_page_cap_and_deduped():
    hashes = [_hash(i) for i in range(12000)]

    def responder(call):
        # Return one row per inlined hash on this page (fully-known set).
        found = re.findall(r"'([0-9a-f]{64})'", call.sql)
        # Simulate paging: the reader pages with limit/offset; return the
        # slice the page asks for so a full page keeps paging.
        page = found[call.offset : call.offset + call.limit]
        return [{"entity_hash": h} for h in page]

    fake = _FakeCapability(locus="off_pod", responder=responder)
    cap = ClearinghouseCapability(fake)
    result = cap.known("gene", hashes)

    # No single query asked for more than the page cap.
    assert all(c.limit is not None and c.limit <= OFFPOD_PAGE_CAP for c in fake.calls)
    # No inlined chunk carried more than the page cap of hashes.
    for c in fake.calls:
        assert len(re.findall(r"'[0-9a-f]{64}'", c.sql)) <= OFFPOD_PAGE_CAP
    # Reassembled to one deduplicated answer covering every hash.
    assert len(result) == 12000
    assert len(set(result)) == 12000
    assert set(result) == set(hashes)


def test_criterion_f_offpod_dedup_across_page_boundary():
    """A row repeated across page boundaries collapses to one."""
    hashes = [_hash(i) for i in range(OFFPOD_PAGE_CAP + 10)]

    def responder(call):
        found = re.findall(r"'([0-9a-f]{64})'", call.sql)
        page = found[call.offset : call.offset + call.limit]
        rows = [{"entity_hash": h} for h in page]
        # Inject a duplicate of the first hash on every page.
        rows.append({"entity_hash": hashes[0]})
        return rows

    fake = _FakeCapability(locus="off_pod", responder=responder)
    cap = ClearinghouseCapability(fake)
    result = cap.content("gene", hashes)
    # The duplicated first-hash row appears once, not once per page.
    assert sum(1 for r in result if r["entity_hash"] == hashes[0]) == 1


def test_criterion_f_inpod_12000_not_chunked():
    hashes = [_hash(i) for i in range(12000)]
    fake = _FakeCapability(locus="in_pod")
    cap = ClearinghouseCapability(fake)
    cap.known("gene", hashes)
    # In-pod carries no page cap: one query, whole batch bound.
    assert len(fake.calls) == 1
    assert len(fake.calls[0].params) == 12000
    assert fake.calls[0].limit is None


# --------------------------------------------------------------------------
# Engine selection: default per verb + Spark promotion threshold.
# --------------------------------------------------------------------------


def test_known_defaults_to_trino_below_threshold():
    fake = _FakeCapability()
    cap = ClearinghouseCapability(fake)
    cap.known("gene", [_hash(1)])
    assert fake.calls[0].engine == "trino"


def test_known_promotes_to_spark_at_threshold():
    hashes = [_hash(i) for i in range(SPARK_PROMOTION_THRESHOLD)]
    fake = _FakeCapability()
    cap = ClearinghouseCapability(fake)
    cap.known("gene", hashes)
    assert all(c.engine == "spark" for c in fake.calls)


def test_explicit_engine_passes_through_unchanged():
    fake = _FakeCapability()
    cap = ClearinghouseCapability(fake)
    cap.known("gene", [_hash(1)], engine="spark")
    assert fake.calls[0].engine == "spark"
    # And overrides promotion the other way too.
    fake2 = _FakeCapability()
    cap2 = ClearinghouseCapability(fake2)
    cap2.known(
        "gene", [_hash(i) for i in range(SPARK_PROMOTION_THRESHOLD)], engine="trino"
    )
    assert all(c.engine == "trino" for c in fake2.calls)


def test_current_state_defaults_to_spark():
    fake = _FakeCapability()
    cap = ClearinghouseCapability(fake)
    cap.current_state("gene")
    assert fake.calls[0].engine == "spark"


# --------------------------------------------------------------------------
# Criterion (g): stats() row counts come from COUNT(*), not row_count field.
# --------------------------------------------------------------------------


_SENTINEL_ROW_COUNT = 5000  # the materialized off-pod page size trap


def test_criterion_g_stats_uses_count_star_not_row_count_field():
    """A fake whose envelope ``row_count`` is a fixed sentinel that must not
    appear in output; the real COUNT(*) scalar is a different number.
    """
    real_counts = {}
    for i, (kind, etype) in enumerate(
        [(k, e) for k in ("entity", "content", "result") for e in ENTITY_TYPES]
    ):
        real_counts[table_name(etype, kind)] = 100 + i  # never 5000

    def responder(call):
        assert "COUNT(*)" in call.sql, call.sql
        # Find which table this COUNT is over.
        for name, count in real_counts.items():
            if name in call.sql:
                return [{"row_count": count}]
        raise AssertionError(f"unrecognized stats query: {call.sql}")

    # Off-pod: the envelope row_count is the sentinel page size.
    fake = _FakeCapability(locus="off_pod", responder=responder)
    cap = ClearinghouseCapability(fake)
    report = cap.stats()

    reported = {t["name"]: t["row_count"] for t in report["tables"]}
    assert reported == real_counts
    # The sentinel page-size never leaks into any reported row_count.
    assert _SENTINEL_ROW_COUNT not in reported.values()
    assert len(report["tables"]) == 15


def test_stats_include_files_offpod_warns_and_omits_file_fields():
    def responder(call):
        return [{"row_count": 7}]

    fake = _FakeCapability(locus="off_pod", responder=responder)
    cap = ClearinghouseCapability(fake)
    report = cap.stats(include_files=True)
    assert report["warnings"]  # non-empty
    for t in report["tables"]:
        assert "data_file_count" not in t
        assert "avg_file_size_bytes" not in t
    # And no Iceberg `.files` metadata table was queried off-pod.
    assert all("`files`" not in c.sql for c in fake.calls)


def test_stats_include_files_inpod_reads_files_metadata():
    def responder(call):
        if "`files`" in call.sql:
            return [{"data_file_count": 3, "avg_file_size_bytes": 1024.0}]
        return [{"row_count": 42}]

    fake = _FakeCapability(locus="in_pod", responder=responder)
    cap = ClearinghouseCapability(fake)
    report = cap.stats(include_files=True)
    assert report["warnings"] == []
    assert any("`files`" in c.sql for c in fake.calls)
    for t in report["tables"]:
        assert t["data_file_count"] == 3
        assert t["avg_file_size_bytes"] == 1024.0


# --------------------------------------------------------------------------
# tables() and sources()
# --------------------------------------------------------------------------


def test_tables_lists_all_fifteen_with_partition_spec():
    def responder(call):
        return [{"row_count": 1}]

    fake = _FakeCapability(responder=responder)
    cap = ClearinghouseCapability(fake)
    listing = cap.tables()
    assert len(listing) == 15
    names = {t["name"] for t in listing}
    assert names == {
        table_name(e, k) for k in ("entity", "content", "result") for e in ENTITY_TYPES
    }
    # result tables partition by source per the config.
    result_entries = [t for t in listing if t["kind"] == "result"]
    assert all(t["partition_by"] == ["source"] for t in result_entries)


def test_sources_all_types_aggregates_every_result_table():
    def responder(call):
        return [{"entity_type": "x", "source": "s", "row_count": 1}]

    fake = _FakeCapability(responder=responder)
    cap = ClearinghouseCapability(fake)
    cap.sources()
    # One grouped-count query per result table.
    assert len(fake.calls) == len(ENTITY_TYPES)
    for etype in ENTITY_TYPES:
        assert any(table_name(etype, "result") in c.sql for c in fake.calls)
    assert all("COUNT(*)" in c.sql and "GROUP BY source" in c.sql for c in fake.calls)


def test_sources_single_type_aggregates_one_table():
    fake = _FakeCapability(responder=lambda c: [])
    cap = ClearinghouseCapability(fake)
    cap.sources("gene")
    assert len(fake.calls) == 1
    assert table_name("gene", "result") in fake.calls[0].sql


# --------------------------------------------------------------------------
# results()/current_state() wrap the derivation builder (Rule 5)
# --------------------------------------------------------------------------


def test_results_wraps_current_state_window_function():
    fake = _FakeCapability()
    cap = ClearinghouseCapability(fake)
    cap.results("gene", [_hash(1)])
    sql = fake.calls[0].sql
    # The hallmark of current_state_sql: a ROW_NUMBER() window ranked and
    # filtered to rn = 1.
    assert "ROW_NUMBER()" in sql
    assert "rn = 1" in sql or "rn=1" in sql
    # And it filters to the requested hashes on top.
    assert "entity_hash IN" in sql


def test_results_result_types_filter_applied():
    fake = _FakeCapability()
    cap = ClearinghouseCapability(fake)
    cap.results("gene", [_hash(1)], result_types=["annotation"])
    sql = fake.calls[0].sql
    assert "result_type IN" in sql
    assert "'annotation'" in sql


def test_empty_hashes_short_circuits_without_query():
    fake = _FakeCapability()
    cap = ClearinghouseCapability(fake)
    assert cap.known("gene", []) == []
    assert cap.content("gene", []) == []
    assert cap.content_all_types([]) == []
    assert cap.results("gene", []) == []
    assert fake.calls == []


# ==========================================================================
# WRITE HALF -- register() and ingest_shards()
# ==========================================================================
#
# The write verbs are proven against a second fake (``_FakeWriteCapability``)
# that stands in for the whole lakehouse write boundary. It records every
# ``load()`` call, exposes the two namespace resolvers the dev 1206 pre-write
# assertion consults (independently settable so a test can make the probe and
# write namespaces DISAGREE), returns a configurable postflight (so a null
# ``row_count`` can be simulated -- dev 1194), and answers the reconcile
# snapshot-history ``query()``. No test requires a pod.
#
# The success criteria these map to (from the task):
#   (a) a probe/write namespace mismatch causes ingest_shards() to RAISE and
#       call load() EXACTLY ZERO times -- ``test_write_criterion_a_*``
#   (b) a null postflight row_count is a FAILED load -- ``test_write_criterion_b_*``
#   (c) a ledger with 0-3 ingested + 4 started skips 0-3, refuses at 4 without
#       reconcile, and proceeds with it -- ``test_write_criterion_c_*``
#   (d) off-pod register()/ingest_shards() raise before any transport call --
#       ``test_write_criterion_d_*``


class _FakeWriteCapability:
    """Records ``load()`` calls; drives the write-half pre/post-flight guards.

    Args:
        locus: ``"in_pod"`` or ``"off_pod"``.
        write_ns / probe_ns: What the two dev 1206 resolvers return. When they
            differ (or either is falsy) the pre-write assertion must refuse.
            Default: both equal ``TENANT.NAMESPACE`` (the agreeing case).
        postflight_row_count: The ``row_count`` every per-table load report
            carries. ``None`` simulates the dev 1194 null-postflight failure.
        snapshot_rows: Rows the reconcile snapshot-history ``query()`` returns.
        registry_hashes: The ``parameter_set_hash`` values the parameter-set
            REGISTRY table already holds, as ``register()``'s pre-write
            lookup sees them. Defaults to empty (a fresh registry), so a
            ``result`` call writes a registry row for every distinct set.
        fail_tables: Table names whose ``load()`` raises ``RuntimeError``,
            standing in for a transport failure mid-ingest.
    """

    def __init__(
        self,
        *,
        locus="in_pod",
        write_ns=f"{TENANT}.{NAMESPACE}",
        probe_ns=f"{TENANT}.{NAMESPACE}",
        postflight_row_count=1,
        snapshot_rows=None,
        registry_hashes=(),
        fail_tables=(),
    ):
        self._locus = locus
        self._write_ns = write_ns
        self._probe_ns = probe_ns
        self._postflight_row_count = postflight_row_count
        self._snapshot_rows = snapshot_rows if snapshot_rows is not None else []
        self._registry_hashes = set(registry_hashes)
        self._fail_tables = set(fail_tables)
        self.load_calls: list[dict] = []
        self.queries: list[str] = []

    def locus(self):
        return self._locus

    def resolve_write_namespace(self, *, dataset, tenant):
        return self._write_ns

    def resolve_probe_namespace(self, *, dataset, tenant):
        return self._probe_ns

    def load(self, **kwargs):
        self.load_calls.append(kwargs)
        failing = [t["name"] for t in kwargs["tables"] if t["name"] in self._fail_tables]
        if failing:
            raise RuntimeError(f"simulated transport failure writing {failing!r}")
        return {
            "ingest_result": {"success": True},
            "tables": [
                {
                    "name": t["name"],
                    "row_count": self._postflight_row_count,
                    "new_snapshot_id": None
                    if self._postflight_row_count is None
                    else 123456,
                    **(
                        {"row_count_error": "postflight could not read table"}
                        if self._postflight_row_count is None
                        else {}
                    ),
                }
                for t in kwargs["tables"]
            ],
        }

    def query(self, sql, *, params=None, engine=None, **kwargs):
        self.queries.append(sql)
        if PARAMETER_SET_TABLE in sql and "parameter_set_hash" in sql:
            # register()'s pre-write registry lookup: answer with whichever of
            # the asked-for hashes this registry already holds.
            asked = list(params or [])
            return [
                {"parameter_set_hash": h}
                for h in asked
                if h in self._registry_hashes
            ]
        return list(self._snapshot_rows)


def _write_shard_tree(tmp_path, table_batches):
    """Lay out ``<tmp_path>/<table>/<batch>.parquet`` shard files.

    ``table_batches`` maps a table name to a list of batch ids. Returns the
    shard-root path.
    """
    for name, batches in table_batches.items():
        table_dir = tmp_path / name
        table_dir.mkdir(parents=True, exist_ok=True)
        for batch in batches:
            (table_dir / f"{batch}.parquet").write_bytes(b"")
    return tmp_path


def _write_ledger(tmp_path, run_id, lines):
    """Write ledger JSON-lines. ``lines`` is a list of (table, batch, state)."""
    import json

    ledger = tmp_path / LEDGER_FILENAME
    with ledger.open("w", encoding="utf-8") as handle:
        for table, batch, state in lines:
            handle.write(
                json.dumps(
                    {
                        "run_id": run_id,
                        "table": table,
                        "batch": batch,
                        "shard_path": f"{table}/{batch}.parquet",
                        "rows": None,
                        "state": state,
                        "snapshot_id": None,
                        "at": "2026-09-24T00:00:00+00:00",
                    }
                )
                + "\n"
            )
    return ledger


def _read_ledger(tmp_path):
    """Return the parsed ledger records as a list of dicts."""
    import json

    ledger = tmp_path / LEDGER_FILENAME
    if not ledger.exists():
        return []
    return [
        json.loads(line) for line in ledger.read_text().splitlines() if line.strip()
    ]


# --------------------------------------------------------------------------
# Write criterion (a): probe/write namespace mismatch -> RAISE, zero load()s.
# --------------------------------------------------------------------------


def test_write_criterion_a_namespace_mismatch_raises_and_never_loads(tmp_path):
    # Probe reads "default"; the write goes to tenant.dataset -- the exact
    # dev 1206 shape. ingest_shards() must FAIL CLOSED.
    fake = _FakeWriteCapability(probe_ns="default", write_ns=f"{TENANT}.{NAMESPACE}")
    _write_shard_tree(tmp_path, {"gene_entity": ["0000"]})
    cap = ClearinghouseCapability(fake)
    with pytest.raises(ClearinghouseWriteTargetMismatchError):
        cap.ingest_shards(tmp_path, run_id="r1")
    assert fake.load_calls == []  # load() called EXACTLY zero times


def test_write_criterion_a_mismatch_raises_even_in_dry_run(tmp_path):
    fake = _FakeWriteCapability(probe_ns="default")
    _write_shard_tree(tmp_path, {"gene_entity": ["0000"]})
    cap = ClearinghouseCapability(fake)
    with pytest.raises(ClearinghouseWriteTargetMismatchError):
        cap.ingest_shards(tmp_path, run_id="r1", dry_run=True)
    assert fake.load_calls == []
    # And no ledger was written -- the refusal precedes opening it.
    assert _read_ledger(tmp_path) == []


def test_write_criterion_a_register_mismatch_raises_and_never_loads():
    fake = _FakeWriteCapability(probe_ns="default")
    cap = ClearinghouseCapability(fake)
    with pytest.raises(ClearinghouseWriteTargetMismatchError):
        cap.register("gene", [{"entity_hash": _hash(1)}], kind="entity")
    assert fake.load_calls == []


def test_write_criterion_a_falsy_namespace_refuses():
    # A resolver returning "" means agreement cannot be SHOWN -> refuse.
    fake = _FakeWriteCapability(write_ns="", probe_ns="")
    cap = ClearinghouseCapability(fake)
    with pytest.raises(ClearinghouseWriteTargetMismatchError):
        cap.register("gene", [{"entity_hash": _hash(1)}], kind="entity")
    assert fake.load_calls == []


def test_write_criterion_a_missing_resolvers_refuses():
    """A capability lacking the resolvers cannot show agreement -> refuse."""

    class _NoResolvers:
        def locus(self):
            return "in_pod"

        def load(self, **kwargs):  # pragma: no cover - must never be reached
            raise AssertionError("load() must not be called")

    cap = ClearinghouseCapability(_NoResolvers())
    with pytest.raises(ClearinghouseWriteTargetMismatchError):
        cap.register("gene", [{"entity_hash": _hash(1)}], kind="entity")


# --------------------------------------------------------------------------
# Write criterion (b): a null postflight row_count is a FAILED load.
# --------------------------------------------------------------------------


def test_write_criterion_b_null_postflight_is_failed_load():
    fake = _FakeWriteCapability(postflight_row_count=None)
    cap = ClearinghouseCapability(fake)
    with pytest.raises(ClearinghouseLoadPostflightError):
        cap.register("gene", [{"entity_hash": _hash(1)}], kind="entity")
    # load() WAS called (the rows may have landed) -- the failure is postflight.
    assert len(fake.load_calls) == 1


def test_write_criterion_b_null_postflight_in_ingest_marks_failed(tmp_path):
    fake = _FakeWriteCapability(postflight_row_count=None)
    _write_shard_tree(tmp_path, {"gene_entity": ["0000"]})
    cap = ClearinghouseCapability(fake)
    with pytest.raises(ClearinghouseLoadPostflightError):
        cap.ingest_shards(tmp_path, run_id="r1")
    # The ledger records the attempt: a 'started' then a 'failed' line, never
    # an 'ingested' one, so a resume treats it correctly.
    records = _read_ledger(tmp_path)
    states = [r["state"] for r in records if r["table"] == "gene_entity"]
    assert "started" in states
    assert "failed" in states
    assert "ingested" not in states


def test_write_criterion_b_nonnull_postflight_succeeds():
    fake = _FakeWriteCapability(postflight_row_count=42)
    cap = ClearinghouseCapability(fake)
    result = cap.register("gene", [{"entity_hash": _hash(1)}], kind="entity")
    assert result["tables"][0]["row_count"] == 42


# --------------------------------------------------------------------------
# Write criterion (c): ledger 0-3 ingested + 4 started -> skip, refuse, proceed.
# --------------------------------------------------------------------------


def _five_batch_tree(tmp_path):
    return _write_shard_tree(
        tmp_path, {"gene_entity": ["0000", "0001", "0002", "0003", "0004"]}
    )


def test_write_criterion_c_started_batch_refuses_without_reconcile(tmp_path):
    _five_batch_tree(tmp_path)
    _write_ledger(
        tmp_path,
        "r1",
        [
            ("gene_entity", "0000", "ingested"),
            ("gene_entity", "0001", "ingested"),
            ("gene_entity", "0002", "ingested"),
            ("gene_entity", "0003", "ingested"),
            ("gene_entity", "0004", "started"),
        ],
    )
    fake = _FakeWriteCapability()
    cap = ClearinghouseCapability(fake)
    with pytest.raises(ClearinghouseLedgerAmbiguousError):
        cap.ingest_shards(tmp_path, run_id="r1")
    # Refused at batch 4 -- and crucially never re-ingested batches 0-3.
    assert fake.load_calls == []


def test_write_criterion_c_skips_ingested_and_proceeds_with_reconcile(tmp_path):
    _five_batch_tree(tmp_path)
    _write_ledger(
        tmp_path,
        "r1",
        [
            ("gene_entity", "0000", "ingested"),
            ("gene_entity", "0001", "ingested"),
            ("gene_entity", "0002", "ingested"),
            ("gene_entity", "0003", "ingested"),
            ("gene_entity", "0004", "started"),
        ],
    )
    # Reconcile: snapshot history shows the batch-4 shard did NOT land (no
    # snapshots), so it is safe to re-ingest it.
    fake = _FakeWriteCapability(snapshot_rows=[])
    cap = ClearinghouseCapability(fake)
    result = cap.ingest_shards(tmp_path, run_id="r1", reconcile=True)

    actions = {(r["batch"]): r["action"] for r in result["tables"]}
    # 0-3 skipped (already ingested), 4 ingested now.
    assert actions["0000"] == "skip"
    assert actions["0003"] == "skip"
    assert actions["0004"] == "ingest"
    # Exactly ONE load() -- for batch 4 only; 0-3 were not re-ingested.
    assert len(fake.load_calls) == 1
    assert fake.load_calls[0]["tables"][0]["name"] == "gene_entity"


def test_write_criterion_c_reconcile_treats_landed_shard_as_skip(tmp_path):
    _five_batch_tree(tmp_path)
    _write_ledger(
        tmp_path,
        "r1",
        [("gene_entity", f"{i:04d}", "ingested") for i in range(4)]
        + [("gene_entity", "0004", "started")],
    )
    # Snapshot history shows a snapshot -> treat the started shard as landed.
    fake = _FakeWriteCapability(snapshot_rows=[{"snapshot_id": 999}])
    cap = ClearinghouseCapability(fake)
    result = cap.ingest_shards(tmp_path, run_id="r1", reconcile=True)
    actions = {r["batch"]: r["action"] for r in result["tables"]}
    assert actions["0004"] == "reconcile"
    # Reconcile decided landed -> no re-ingest of batch 4 either.
    assert fake.load_calls == []
    # The ledger now carries a terminal 'ingested' line for batch 4.
    records = _read_ledger(tmp_path)
    batch4 = [r for r in records if r["batch"] == "0004"]
    assert batch4[-1]["state"] == "ingested"


def test_write_criterion_c_other_run_id_started_line_is_ignored(tmp_path):
    """A 'started' line from a DIFFERENT run_id must not block this run."""
    _write_shard_tree(tmp_path, {"gene_entity": ["0000"]})
    _write_ledger(tmp_path, "OTHER", [("gene_entity", "0000", "started")])
    fake = _FakeWriteCapability()
    cap = ClearinghouseCapability(fake)
    result = cap.ingest_shards(tmp_path, run_id="r1")
    assert result["tables"][0]["action"] == "ingest"
    assert len(fake.load_calls) == 1


# --------------------------------------------------------------------------
# Write criterion (d): off-pod register()/ingest_shards() raise before any
#                      transport call.
# --------------------------------------------------------------------------


def test_write_criterion_d_offpod_register_refuses_before_transport():
    fake = _FakeWriteCapability(locus="off_pod")
    cap = ClearinghouseCapability(fake)
    with pytest.raises(BerdlLoadRefusedError):
        cap.register("gene", [{"entity_hash": _hash(1)}], kind="entity")
    assert fake.load_calls == []
    assert fake.queries == []


def test_write_criterion_d_offpod_ingest_shards_refuses_before_transport(tmp_path):
    fake = _FakeWriteCapability(locus="off_pod")
    _write_shard_tree(tmp_path, {"gene_entity": ["0000"]})
    cap = ClearinghouseCapability(fake)
    with pytest.raises(BerdlLoadRefusedError):
        cap.ingest_shards(tmp_path, run_id="r1")
    assert fake.load_calls == []
    assert fake.queries == []
    # Off-pod refusal precedes opening the ledger.
    assert _read_ledger(tmp_path) == []


# --------------------------------------------------------------------------
# register(): table selection + dry-run/ingest general behaviour
# --------------------------------------------------------------------------


@pytest.mark.parametrize("entity_type", ENTITY_TYPES)
@pytest.mark.parametrize("kind", ["entity", "content", "result"])
def test_register_selects_the_type_kind_table(entity_type, kind):
    """register() routes to <type>_<kind> through table_name(), as an append.

    A ``result`` row now has to carry a ``parameter_set`` (it is required --
    see the parameter-set tests below), and the call then makes TWO appends:
    the parameter-set registry first, then the rows. So the row's table is the
    LAST load() call for a result, and the only one otherwise.
    """
    fake = _FakeWriteCapability()
    cap = ClearinghouseCapability(fake)
    row = {"entity_hash": _hash(1)}
    if kind == "result":
        row["parameter_set"] = {}
    cap.register(entity_type, [row], kind=kind)
    if kind == "result":
        # Registry FIRST, then the result rows.
        assert fake.load_calls[0]["tables"][0]["name"] == PARAMETER_SET_TABLE
        assert len(fake.load_calls) == 2
    else:
        assert len(fake.load_calls) == 1
    assert fake.load_calls[-1]["tables"][0]["name"] == table_name(entity_type, kind)
    # Always requested as an append -- never overwrite from this layer.
    assert all(call["tables"][0]["mode"] == "append" for call in fake.load_calls)


def test_register_unknown_type_or_kind_raises_before_load():
    fake = _FakeWriteCapability()
    cap = ClearinghouseCapability(fake)
    with pytest.raises(ValueError):
        cap.register("polypeptide", [{"x": 1}], kind="entity")
    with pytest.raises(ValueError):
        cap.register("gene", [{"x": 1}], kind="snapshot")
    assert fake.load_calls == []


def test_ingest_shards_dry_run_writes_nothing(tmp_path):
    _write_shard_tree(tmp_path, {"gene_entity": ["0000", "0001"]})
    fake = _FakeWriteCapability()
    cap = ClearinghouseCapability(fake)
    result = cap.ingest_shards(tmp_path, run_id="r1", dry_run=True)
    assert result["dry_run"] is True
    assert all(t["action"] == "would_ingest" for t in result["tables"])
    # Genuinely read-only: no load(), no ledger lines.
    assert fake.load_calls == []
    assert _read_ledger(tmp_path) == []


def test_ingest_shards_ledger_records_ingested_and_resume_skips(tmp_path):
    _write_shard_tree(tmp_path, {"gene_entity": ["0000", "0001"]})
    fake = _FakeWriteCapability()
    cap = ClearinghouseCapability(fake)
    first = cap.ingest_shards(tmp_path, run_id="r1")
    assert all(t["action"] == "ingest" for t in first["tables"])
    assert len(fake.load_calls) == 2

    # Resume against the SAME run_id: every batch is now 'ingested' -> skipped,
    # with no separate resume code path and no re-ingest.
    fake2 = _FakeWriteCapability()
    cap2 = ClearinghouseCapability(fake2)
    second = cap2.ingest_shards(tmp_path, run_id="r1")
    assert all(t["action"] == "skip" for t in second["tables"])
    assert fake2.load_calls == []


def test_ingest_shards_ignores_ledger_file_and_unknown_dirs(tmp_path):
    _write_shard_tree(tmp_path, {"gene_entity": ["0000"]})
    # A stray non-table directory must not be treated as a shard table.
    (tmp_path / "not_a_table").mkdir()
    (tmp_path / "not_a_table" / "0000.parquet").write_bytes(b"")
    fake = _FakeWriteCapability()
    cap = ClearinghouseCapability(fake)
    result = cap.ingest_shards(tmp_path, run_id="r1")
    names = {t["name"] for t in result["tables"]}
    assert names == {"gene_entity"}


# --------------------------------------------------------------------------
# verify_run(): re-check a completed run's row counts + snapshots vs the ledger.
# --------------------------------------------------------------------------


class _FakeVerifyCapability:
    """Fake driving :meth:`ClearinghouseCapability.verify_run`.

    Routes ``query()`` by SQL: a ``COUNT(*)`` returns ``count_rows``; a
    ``.snapshots`` read returns one row per id in ``snapshot_ids``. Exposes the
    dev 1206 resolvers so verify's write-target guard resolves cleanly.
    """

    def __init__(
        self,
        *,
        locus="in_pod",
        count_rows=6,
        snapshot_ids=(123456,),
        result_parameter_sets=(),
        registry_pairs=(),
    ):
        self._locus = locus
        self._count_rows = count_rows
        self._snapshot_ids = list(snapshot_ids)
        #: What the parameter-set integrity check sees. Both default to EMPTY,
        #: so every test written before that check existed still reconciles
        #: cleanly (no result hashes means nothing can be orphaned).
        self._result_parameter_sets = list(result_parameter_sets)
        self._registry_pairs = list(registry_pairs)
        self.queries: list[str] = []

    def locus(self):
        return self._locus

    def resolve_write_namespace(self, *, dataset, tenant):
        return f"{TENANT}.{NAMESPACE}"

    def resolve_probe_namespace(self, *, dataset, tenant):
        return f"{TENANT}.{NAMESPACE}"

    def query(self, sql, *, params=None, engine=None, **kwargs):
        self.queries.append(sql)
        if "snapshots" in sql:
            return [{"snapshot_id": sid} for sid in self._snapshot_ids]
        if "COUNT(*)" in sql:
            return [{"row_count": self._count_rows}]
        # The registry probe must be matched BEFORE the result-table one: both
        # start "SELECT DISTINCT parameter_set_hash" and only the registry's
        # also selects canonical_json.
        if "canonical_json" in sql:
            return [
                {"parameter_set_hash": digest, "canonical_json": text}
                for digest, text in self._registry_pairs
            ]
        if "DISTINCT parameter_set_hash" in sql:
            return [
                {"parameter_set_hash": digest}
                for digest in self._result_parameter_sets
            ]
        return []  # pragma: no cover


def _write_verify_ledger(tmp_path, run_id, lines):
    """Write ledger JSON-lines with explicit rows/snapshot_id per (table,batch)."""
    import json

    ledger = tmp_path / LEDGER_FILENAME
    with ledger.open("w", encoding="utf-8") as handle:
        for table, batch, state, rows, snap in lines:
            handle.write(
                json.dumps(
                    {
                        "run_id": run_id,
                        "table": table,
                        "batch": batch,
                        "shard_path": f"{table}/{batch}.parquet",
                        "rows": rows,
                        "state": state,
                        "snapshot_id": snap,
                        "at": "2026-09-24T00:00:00+00:00",
                    }
                )
                + "\n"
            )
    return ledger


def test_verify_run_off_pod_refuses_before_any_query(tmp_path):
    fake = _FakeVerifyCapability(locus="off_pod")
    cap = ClearinghouseCapability(fake)
    with pytest.raises(BerdlLoadRefusedError):
        cap.verify_run(tmp_path, run_id="r1")
    assert fake.queries == []  # no transport call was made


def test_verify_run_reconciles_matching_counts_and_snapshots(tmp_path):
    _write_verify_ledger(
        tmp_path,
        "r1",
        [("gene_entity", "0000", "ingested", 6, 123456)],
    )
    fake = _FakeVerifyCapability(count_rows=6, snapshot_ids=(123456,))
    cap = ClearinghouseCapability(fake)
    result = cap.verify_run(tmp_path, run_id="r1")
    assert result["discrepancies"] == []
    report = result["tables"][0]
    assert report["name"] == "gene_entity"
    assert report["ledger_rows"] == 6
    assert report["live_rows"] == 6
    assert report["ok"] is True


def test_verify_run_flags_missing_rows(tmp_path):
    _write_verify_ledger(
        tmp_path,
        "r1",
        [("gene_entity", "0000", "ingested", 6, 123456)],
    )
    # Live table has fewer rows than the ledger says landed -> discrepancy.
    fake = _FakeVerifyCapability(count_rows=2, snapshot_ids=(123456,))
    cap = ClearinghouseCapability(fake)
    result = cap.verify_run(tmp_path, run_id="r1")
    assert result["discrepancies"], "a missing-rows discrepancy must be reported"
    assert result["tables"][0]["ok"] is False


def test_verify_run_flags_missing_snapshot(tmp_path):
    _write_verify_ledger(
        tmp_path,
        "r1",
        [("gene_entity", "0000", "ingested", 6, 999999)],
    )
    # The ledger's recorded snapshot id is absent from the table's history.
    fake = _FakeVerifyCapability(count_rows=6, snapshot_ids=(123456,))
    cap = ClearinghouseCapability(fake)
    result = cap.verify_run(tmp_path, run_id="r1")
    assert any("snapshot" in d for d in result["discrepancies"])
    assert result["tables"][0]["missing_snapshots"] == ["999999"]


def test_verify_run_ignores_non_ingested_ledger_lines(tmp_path):
    _write_verify_ledger(
        tmp_path,
        "r1",
        [
            ("gene_entity", "0000", "started", None, None),
            ("gene_entity", "0000", "ingested", 6, 123456),
            ("gene_content", "0000", "failed", None, None),
        ],
    )
    fake = _FakeVerifyCapability(count_rows=6, snapshot_ids=(123456,))
    cap = ClearinghouseCapability(fake)
    result = cap.verify_run(tmp_path, run_id="r1")
    # Only the ingested gene_entity is verified; the failed gene_content is not.
    assert {t["name"] for t in result["tables"]} == {"gene_entity"}


# --- criterion (h): identifier quoting matches the executing dialect --------
#
# Regression guard for the 2026-09-28 defect. `_quote_fqn` hardcoded
# backticks; Trino rejects them outright ("backquoted identifiers are not
# supported; use double quotes to quote identifiers"). Every Trino-routed
# read verb therefore failed on the pod -- `stats` DEGRADED SILENTLY to
# em-dash row counts, `sources`/`results` raised -- while all 360 tests in
# this directory passed, because none of them executes SQL against a real
# engine. These tests close that specific hole at the SQL-TEXT level, which
# is the layer a fake CAN police.


def test_criterion_h_quote_fqn_is_dialect_correct():
    """Trino gets double quotes; Spark gets backticks; neither leaks."""
    fqn = "kbaseincubator.clearinghouse.protein_result"

    trino = _quote_fqn(fqn, "trino")
    assert trino == '"kbaseincubator"."clearinghouse"."protein_result"'
    assert "`" not in trino, "backtick leaked into Trino SQL -- Trino rejects it"

    spark = _quote_fqn(fqn, "spark")
    assert spark == "`kbaseincubator`.`clearinghouse`.`protein_result`"
    assert '"' not in spark

    # Default stays Spark, so Spark-only callers keep working untouched.
    assert _quote_fqn(fqn) == spark

    # Re-quoting is idempotent in BOTH directions -- a segment arriving in
    # the other dialect's quotes must not end up double-quoted.
    assert _quote_fqn(trino, "trino") == trino
    assert _quote_fqn(spark, "spark") == spark
    assert _quote_fqn(spark, "trino") == trino
    assert _quote_fqn(trino, "spark") == spark


def test_criterion_h_no_backticks_in_any_trino_sql():
    """The integration-level guard: nothing Trino-routed may contain a backtick.

    This is the assertion whose absence let the defect ship. It walks the
    read verbs, forces the Trino engine, and inspects every emitted SQL
    string -- so a future caller that forgets to thread ``engine`` through
    a new ``_quote_fqn`` call site fails HERE rather than on the pod.
    """
    hexes = ["a" * 64, "b" * 64]

    for verb, args, kwargs in [
        ("known", ("protein", hexes), {}),
        ("content", ("protein", hexes), {}),
        ("results", ("protein", hexes), {}),
        ("sources", ("protein",), {}),
        ("stats", (), {}),
        ("tables", (), {}),
    ]:
        fake = _FakeCapability(
            locus="in_pod", responder=lambda call: [{"row_count": 0}]
        )
        cap = ClearinghouseCapability(fake)
        method = getattr(cap, verb)
        method(*args, engine="trino", **kwargs)

        assert fake.calls, f"{verb} issued no query to inspect"
        for call in fake.calls:
            if call.engine != "trino":
                continue
            assert "`" not in call.sql, (
                f"{verb} emitted a backquoted identifier on the Trino path -- "
                f"Trino rejects these outright:\n{call.sql}"
            )


# ==========================================================================
# THE PARAMETER SET on register(), and registry-first load ordering
# ==========================================================================
#
# register() for kind='result' must: validate EVERY row before the first
# write; write the parameter-set registry rows BEFORE the result rows; store
# parameter_set_hash and NEVER the parameter_set input field; and refuse the
# WHOLE call -- writing nothing -- for a missing, invalid, non-canonical,
# duplicate-key or hash-mismatched parameter set.
#
# The fake's load() is the lowest-level table writer, so `fake.load_calls` is
# the record of what was actually written, in order. Nothing in
# clearinghouse_capability is monkeypatched in any of these.


def _result_row(seed=1, parameter_set=None, **extra):
    """A minimal <type>_result row for register(), with a parameter set."""
    row = {
        "entity_hash": _hash(seed),
        "result_type": "functional_annotation",
        "source": "bakta/1.9",
        "payload": "{}",
    }
    if parameter_set is not None:
        row["parameter_set"] = parameter_set
    row.update(extra)
    return row


def _loads_of(fake, table):
    """Indices of ``fake.load_calls`` that wrote ``table``."""
    return [
        i
        for i, call in enumerate(fake.load_calls)
        if any(t["name"] == table for t in call["tables"])
    ]


def test_register_result_writes_registry_before_result_rows():
    """REAL-PATH for register(): the real verb, with only the lowest-level
    table writer (the fake's load()) replaced, and the ORDER read off that
    writer's own recorded calls -- registry append strictly first."""
    fake = _FakeWriteCapability()
    cap = ClearinghouseCapability(fake)
    result = cap.register("gene", [_result_row(parameter_set={})], kind="result")

    (registry_index,) = _loads_of(fake, PARAMETER_SET_TABLE)
    (rows_index,) = _loads_of(fake, "gene_result")
    assert registry_index < rows_index, (
        "the parameter-set registry must be written BEFORE the result rows "
        "that reference its hashes"
    )
    assert len(fake.load_calls) == 2

    # The registry row says what the hash means.
    registry_rows = fake.load_calls[registry_index]["dataframes"][PARAMETER_SET_TABLE]
    assert registry_rows == [
        {
            "parameter_set_hash": DEFAULT_PARAMETER_SET_HASH,
            "canonical_json": "{}",
            "observed_at": None,
            "ingest_batch_id": None,
        }
    ]
    assert result["parameter_set_registry"]["written"] == [DEFAULT_PARAMETER_SET_HASH]


def test_register_result_stores_the_hash_and_never_the_input_field():
    fake = _FakeWriteCapability()
    cap = ClearinghouseCapability(fake)
    cap.register(
        "gene", [_result_row(parameter_set={"evalue": "1e-5"})], kind="result"
    )
    (rows_index,) = _loads_of(fake, "gene_result")
    (stored,) = fake.load_calls[rows_index]["dataframes"]["gene_result"]
    assert stored["parameter_set_hash"] == parameter_set_hash({"evalue": "1e-5"})
    # parameter_set is an INPUT field and is not a column of any result table.
    assert "parameter_set" not in stored


def test_register_result_does_not_mutate_the_callers_rows():
    fake = _FakeWriteCapability()
    cap = ClearinghouseCapability(fake)
    row = _result_row(parameter_set={})
    cap.register("gene", [row], kind="result")
    assert row["parameter_set"] == {}
    assert "parameter_set_hash" not in row


def test_register_result_accepts_canonical_json_text():
    fake = _FakeWriteCapability()
    cap = ClearinghouseCapability(fake)
    text = canonical_parameter_set({"a": 1, "b": "x"})
    cap.register("gene", [_result_row(parameter_set=text)], kind="result")
    (rows_index,) = _loads_of(fake, "gene_result")
    (stored,) = fake.load_calls[rows_index]["dataframes"]["gene_result"]
    assert stored["parameter_set_hash"] == parameter_set_hash({"a": 1, "b": "x"})


def test_register_result_accepts_a_matching_supplied_hash():
    fake = _FakeWriteCapability()
    cap = ClearinghouseCapability(fake)
    cap.register(
        "gene",
        [
            _result_row(
                parameter_set={"evalue": "1e-5"},
                parameter_set_hash=parameter_set_hash({"evalue": "1e-5"}),
            )
        ],
        kind="result",
    )
    assert len(fake.load_calls) == 2


# -- the refusals: every one writes NOTHING --------------------------------


@pytest.mark.parametrize(
    "parameter_set, needle",
    [
        (None, "carries no 'parameter_set'"),                 # missing
        ({"evalue": 1e-5}, "float not allowed"),              # invalid: a float
        ({"Evalue": "1e-5"}, "invalid key"),                  # invalid: bad key
        ({"evalue": None}, "None not allowed"),               # invalid: None
        ('{"b":1,"a":2}', "not canonical"),                   # non-canonical text
        ('{"a": 1}', "not canonical"),                        # non-canonical spacing
        ("{not json}", "not valid JSON"),                     # invalid JSON
        ('{"a":1,"a":2}', "repeats the key"),                 # duplicate keys
        ("[]", "not an object"),                              # JSON but not an object
        (7, "must be a mapping"),                             # neither mapping nor text
    ],
)
def test_register_refuses_the_whole_call_and_writes_nothing(parameter_set, needle):
    fake = _FakeWriteCapability()
    cap = ClearinghouseCapability(fake)
    with pytest.raises(ParameterSetError) as exc:
        cap.register("gene", [_result_row(parameter_set=parameter_set)], kind="result")
    assert needle in str(exc.value)
    assert "rows[0]" in str(exc.value)
    # NOTHING was written -- not the registry, not the rows.
    assert fake.load_calls == []


def test_register_refuses_a_hash_that_disagrees_with_the_parameter_set():
    fake = _FakeWriteCapability()
    cap = ClearinghouseCapability(fake)
    with pytest.raises(ParameterSetError) as exc:
        cap.register(
            "gene",
            [
                _result_row(
                    parameter_set={"evalue": "1e-5"},
                    parameter_set_hash=DEFAULT_PARAMETER_SET_HASH,
                )
            ],
            kind="result",
        )
    message = str(exc.value)
    assert "rows[0]" in message
    assert "disagree" in message
    assert fake.load_calls == []


def test_register_validates_every_row_before_the_first_write():
    """A bad row at index 1 refuses the call with row 0 unwritten -- validation
    is a whole pass, not interleaved with writing."""
    fake = _FakeWriteCapability()
    cap = ClearinghouseCapability(fake)
    rows = [
        _result_row(seed=1, parameter_set={}),
        _result_row(seed=2, parameter_set={"evalue": 1e-5}),
        _result_row(seed=3, parameter_set={}),
    ]
    with pytest.raises(ParameterSetError) as exc:
        cap.register("gene", rows, kind="result")
    assert "rows[1]" in str(exc.value)
    assert fake.load_calls == []


def test_register_collects_distinct_parameter_sets_across_rows():
    """Two rows sharing a set contribute ONE registry row; a third with a
    different set contributes a second."""
    fake = _FakeWriteCapability()
    cap = ClearinghouseCapability(fake)
    cap.register(
        "gene",
        [
            _result_row(seed=1, parameter_set={}),
            _result_row(seed=2, parameter_set={}),
            _result_row(seed=3, parameter_set={"evalue": "1e-5"}),
        ],
        kind="result",
    )
    (registry_index,) = _loads_of(fake, PARAMETER_SET_TABLE)
    registry_rows = fake.load_calls[registry_index]["dataframes"][PARAMETER_SET_TABLE]
    assert [row["parameter_set_hash"] for row in registry_rows] == [
        DEFAULT_PARAMETER_SET_HASH,
        parameter_set_hash({"evalue": "1e-5"}),
    ]


def test_register_retry_adds_no_registry_row_for_a_hash_already_present():
    """An ordinary retry of the same call writes the result rows again (the
    schema is append-only and the current-state derivation collapses them) but
    adds NO second registry row: the hash is looked up first and skipped."""
    fake = _FakeWriteCapability(registry_hashes={DEFAULT_PARAMETER_SET_HASH})
    cap = ClearinghouseCapability(fake)
    result = cap.register("gene", [_result_row(parameter_set={})], kind="result")

    assert _loads_of(fake, PARAMETER_SET_TABLE) == []  # no registry append at all
    assert len(_loads_of(fake, "gene_result")) == 1
    assert result["parameter_set_registry"]["written"] == []
    assert result["parameter_set_registry"]["already_present"] == [
        DEFAULT_PARAMETER_SET_HASH
    ]
    assert result["parameter_set_registry"]["load_result"] is None


def test_register_writes_only_the_unregistered_hashes():
    """A mixed call writes exactly the hashes the registry lacks."""
    new_hash = parameter_set_hash({"evalue": "1e-5"})
    fake = _FakeWriteCapability(registry_hashes={DEFAULT_PARAMETER_SET_HASH})
    cap = ClearinghouseCapability(fake)
    cap.register(
        "gene",
        [
            _result_row(seed=1, parameter_set={}),
            _result_row(seed=2, parameter_set={"evalue": "1e-5"}),
        ],
        kind="result",
    )
    (registry_index,) = _loads_of(fake, PARAMETER_SET_TABLE)
    registry_rows = fake.load_calls[registry_index]["dataframes"][PARAMETER_SET_TABLE]
    assert [row["parameter_set_hash"] for row in registry_rows] == [new_hash]


def test_register_reports_a_partial_commit_rather_than_claiming_nothing_wrote():
    """If the result append fails AFTER the registry append committed, the call
    says so -- it never claims nothing was written -- and says it is safe to
    retry."""
    fake = _FakeWriteCapability(fail_tables={"gene_result"})
    cap = ClearinghouseCapability(fake)
    with pytest.raises(ClearinghousePartialWriteError) as exc:
        cap.register("gene", [_result_row(parameter_set={})], kind="result")
    message = str(exc.value)
    assert DEFAULT_PARAMETER_SET_HASH in message
    assert "WERE written" in message
    assert "safe to retry" in message
    # The registry append really did happen; the result append was attempted.
    assert _loads_of(fake, PARAMETER_SET_TABLE) == [0]
    assert _loads_of(fake, "gene_result") == [1]


def test_register_non_result_kinds_need_no_parameter_set():
    """Only result rows are keyed by a parameter set; entity/content are not."""
    for kind in ("entity", "content"):
        fake = _FakeWriteCapability()
        cap = ClearinghouseCapability(fake)
        cap.register("gene", [{"entity_hash": _hash(1)}], kind=kind)
        assert len(fake.load_calls) == 1
        assert _loads_of(fake, PARAMETER_SET_TABLE) == []


# -- the shared TRANSYT validator, in register() ---------------------------


@pytest.mark.parametrize("source", ["transyt/1.0", "transyt_local/1.0", "TranSyT/1.0"])
def test_register_transyt_refuses_a_default_parameter_set(source):
    fake = _FakeWriteCapability()
    cap = ClearinghouseCapability(fake)
    with pytest.raises(ParameterSetError) as exc:
        cap.register(
            "gene", [_result_row(parameter_set={}, source=source)], kind="result"
        )
    assert "taxonomy_id" in str(exc.value)
    assert fake.load_calls == []


@pytest.mark.parametrize(
    "parameter_set",
    [
        {},                            # missing
        {"taxonomy_id": ""},           # empty
        {"taxonomy_id": 562},          # an integer, not a string
        {"taxonomy_id": True},         # a bool, not a string
        {"taxonomy_id": "-562"},       # negative
        {"taxonomy_id": "0562"},       # leading zero
        {"taxonomy_id": "56a"},        # not a decimal
    ],
)
def test_register_transyt_refuses_a_bad_taxonomy_id(parameter_set):
    fake = _FakeWriteCapability()
    cap = ClearinghouseCapability(fake)
    with pytest.raises(ParameterSetError) as exc:
        cap.register(
            "gene",
            [_result_row(parameter_set=parameter_set, source="transyt/1.0")],
            kind="result",
        )
    assert "taxonomy_id" in str(exc.value)
    assert fake.load_calls == []


@pytest.mark.parametrize("source", ["transyt/1.0", "transyt_local/1.0"])
def test_register_transyt_accepts_a_decimal_taxonomy_id(source):
    fake = _FakeWriteCapability()
    cap = ClearinghouseCapability(fake)
    cap.register(
        "gene",
        [_result_row(parameter_set={"taxonomy_id": "562"}, source=source)],
        kind="result",
    )
    (rows_index,) = _loads_of(fake, "gene_result")
    (stored,) = fake.load_calls[rows_index]["dataframes"]["gene_result"]
    assert stored["parameter_set_hash"] == parameter_set_hash({"taxonomy_id": "562"})


def test_register_non_transyt_source_accepts_a_default_parameter_set():
    fake = _FakeWriteCapability()
    cap = ClearinghouseCapability(fake)
    cap.register(
        "gene", [_result_row(parameter_set={}, source="bakta/1.9")], kind="result"
    )
    (rows_index,) = _loads_of(fake, "gene_result")
    (stored,) = fake.load_calls[rows_index]["dataframes"]["gene_result"]
    assert stored["parameter_set_hash"] == DEFAULT_PARAMETER_SET_HASH


# --------------------------------------------------------------------------
# Registry-first LOAD order, in every shard-load entry point
# --------------------------------------------------------------------------


def test_order_shards_for_load_puts_the_registry_first():
    """The ordering function itself, against a deliberately hostile input."""
    triples = [
        ("protein_result", "0000", Path("protein_result/0000.parquet")),
        ("gene_result", "0001", Path("gene_result/0001.parquet")),
        (PARAMETER_SET_TABLE, "0000", Path(f"{PARAMETER_SET_TABLE}/0000.parquet")),
        ("gene_entity", "0000", Path("gene_entity/0000.parquet")),
    ]
    ordered = order_shards_for_load(list(reversed(triples)))
    assert ordered[0][0] == PARAMETER_SET_TABLE
    # Everything else keeps a deterministic (table, batch) order.
    assert [t[0] for t in ordered[1:]] == [
        "gene_entity",
        "gene_result",
        "protein_result",
    ]


def test_ingest_shards_ingests_the_registry_before_any_result_table(tmp_path):
    _write_shard_tree(
        tmp_path,
        {
            "gene_result": ["0000"],
            "protein_result": ["0000"],
            PARAMETER_SET_TABLE: ["0000"],
        },
    )
    fake = _FakeWriteCapability()
    cap = ClearinghouseCapability(fake)
    cap.ingest_shards(tmp_path, run_id="r1")
    loaded = [call["tables"][0]["name"] for call in fake.load_calls]
    assert loaded[0] == PARAMETER_SET_TABLE
    assert loaded.index(PARAMETER_SET_TABLE) < loaded.index("gene_result")
    assert loaded.index(PARAMETER_SET_TABLE) < loaded.index("protein_result")


def test_ingest_shards_reorders_a_reversed_order_handed_to_it(tmp_path, monkeypatch):
    """A deliberately REVERSED table order handed to the shard loader still
    ingests 'parameter_set' first.

    Registry-first must be a property of the ingest verb, not of its discovery
    helper: a different producer (a future discovery function, a caller
    assembling its own list) must not be able to put a result table ahead of
    the registry. So discovery is replaced with one that returns the worst
    possible order and the verb is required to fix it.
    """
    import kbutillib.domains.kbase.berdl.clearinghouse_capability as cc

    _write_shard_tree(
        tmp_path, {"gene_result": ["0000"], PARAMETER_SET_TABLE: ["0000"]}
    )
    reversed_order = [
        ("gene_result", "0000", tmp_path / "gene_result" / "0000.parquet"),
        (
            PARAMETER_SET_TABLE,
            "0000",
            tmp_path / PARAMETER_SET_TABLE / "0000.parquet",
        ),
    ]
    monkeypatch.setattr(cc, "_discover_shards", lambda root: list(reversed_order))

    fake = _FakeWriteCapability()
    cap = ClearinghouseCapability(fake)
    cap.ingest_shards(tmp_path, run_id="r1")
    loaded = [call["tables"][0]["name"] for call in fake.load_calls]
    assert loaded == [PARAMETER_SET_TABLE, "gene_result"]


def test_discover_shards_recognises_the_registry_directory(tmp_path):
    """The registry table is not entity-typed, so it is named by its constant
    rather than by table_name() -- a discovery that only knew the fifteen
    per-type names would silently skip its shards."""
    import kbutillib.domains.kbase.berdl.clearinghouse_capability as cc

    _write_shard_tree(
        tmp_path, {PARAMETER_SET_TABLE: ["0000"], "gene_entity": ["0000"]}
    )
    discovered = cc._discover_shards(tmp_path)
    assert [name for name, _batch, _path in discovered] == [
        PARAMETER_SET_TABLE,
        "gene_entity",
    ]


def test_register_failure_with_no_registry_write_is_not_called_partial():
    """When every hash was already registered, no registry append was made, so
    a result-append failure really IS 'nothing was written' -- the original
    error propagates rather than being dressed up as a partial commit."""
    fake = _FakeWriteCapability(
        registry_hashes={DEFAULT_PARAMETER_SET_HASH}, fail_tables={"gene_result"}
    )
    cap = ClearinghouseCapability(fake)
    with pytest.raises(RuntimeError) as exc:
        cap.register("gene", [_result_row(parameter_set={})], kind="result")
    assert not isinstance(exc.value, ClearinghousePartialWriteError)
    assert _loads_of(fake, PARAMETER_SET_TABLE) == []


# ==========================================================================
# PARAMETER SETS ON THE READ VERBS
#
# results()/current_state() pre-filter, sources()'s two grains, the registry
# verb, and the registry-integrity check.
#
# WHY THESE TESTS EXECUTE SQL INSTEAD OF ASSERTING ON CANNED ROWS. Every
# claim in this section is a claim about AGGREGATION -- "row_count keeps its
# existing population", "a duplicated registry row does not multiply the
# counts", "one row per hash even when the registry holds three rows". A fake
# that returns canned rows cannot test any of them: it would pin what the
# fake was told to say, and the SQL could be wrong in exactly the way the
# test claims it is not. So :class:`_SqlExecutor` below runs the reader's
# REAL generated SQL on in-memory SQLite, with the tables created from
# ``table_configs()``'s own DDL, and the expected-output fixture asserts on
# what that SQL actually computes.
# ==========================================================================

#: The one non-default parameter set the fixture uses: a tuned threshold,
#: passed as a STRING because floats are rejected by the parameter-set rule.
_TUNED_PARAMS = {"threshold": "1e-5"}
_TUNED_HASH = parameter_set_hash(_TUNED_PARAMS)
_TUNED_JSON = canonical_parameter_set(_TUNED_PARAMS)

#: Collapses the three-part clearinghouse FQN to a bare table name, in either
#: engine's quoting style, so the reader's SQL runs against SQLite (which has
#: no tenant/namespace qualifiers). This is the ONLY edit made to the SQL
#: under test -- no clause is rewritten, nothing is simplified, and the
#: window functions, COUNT(DISTINCT ...) and LEFT JOIN all execute as emitted.
_FQN_RE = re.compile(r'(["`])kbaseincubator\1\.\1clearinghouse\1\.\1(\w+)\1')


#: Maps the schema module's SQL types to SQLite ones. This is NOT cosmetic.
#: SQLite assigns a column whose declared type is ``STRING`` NUMERIC
#: affinity (the name contains none of INT/CHAR/CLOB/TEXT/BLOB/REAL), so a
#: stored ``entity_hash`` of "0000...0001" is silently coerced to the INTEGER
#: 1 and every assertion about hash values quietly tests the wrong thing.
#: Declaring TEXT keeps the fixture's values byte-identical to what the
#: lakehouse stores. A type not in this map raises KeyError rather than
#: defaulting, so a new column type cannot reintroduce the coercion silently.
_SQLITE_TYPES = {
    "STRING": "TEXT",
    "TIMESTAMP": "TEXT",
    "INT": "INTEGER",
    "BIGINT": "INTEGER",
    "BOOLEAN": "INTEGER",
}


def _sqlite_ddl(schema_sql):
    """Re-type a ``schema_sql`` fragment for SQLite, keeping names and order.

    The column list still comes from :func:`table_configs`, so the fixture
    cannot drift from the shipped schema -- only the declared types are
    mapped (see :data:`_SQLITE_TYPES`).
    """
    columns = []
    for part in schema_sql.split(","):
        name, _, sql_type = part.strip().partition(" ")
        columns.append(f'"{name}" {_SQLITE_TYPES[sql_type.strip()]}')
    return ", ".join(columns)


class _SqlExecutor:
    """Executes the reader's real SQL on in-memory SQLite. IN-POD only.

    Stands in at the SAME seam :class:`_FakeCapability` does -- ``locus()``
    plus ``query()`` -- but instead of canned rows it runs the SQL. Tables are
    created from :func:`table_configs`'s own ``schema_sql``, so the fixture
    cannot drift from the shipped schema: add a result column and these
    tables gain it automatically.

    In-pod only, deliberately. In-pod is the locus where ``params`` bind
    through a real ``?`` channel (which SQLite shares), and off-pod paging is
    already covered by the criterion (f) tests; re-implementing
    ``limit``/``offset`` here would test this fake rather than the reader.
    """

    def __init__(self, *, result_rows=(), registry_rows=(), entity_type="protein"):
        self.sql: list[str] = []
        self._db = sqlite3.connect(":memory:")
        for config in table_configs():
            self._db.execute(
                f'CREATE TABLE "{config["name"]}" '
                f"({_sqlite_ddl(config['schema_sql'])})"
            )
        for row in result_rows:
            self._db.execute(
                f'INSERT INTO "{table_name(entity_type, "result")}" '
                "(entity_hash, entity_type, result_type, source, "
                "parameter_set_hash, result_type_version, payload, "
                "observed_at, ingest_batch_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
        for row in registry_rows:
            self._db.execute(
                f'INSERT INTO "{PARAMETER_SET_TABLE}" '
                "(parameter_set_hash, canonical_json, observed_at, "
                "ingest_batch_id) VALUES (?, ?, ?, ?)",
                row,
            )
        self._db.commit()

    def locus(self):
        return "in_pod"

    def query(self, sql, *, params=None, engine=None, limit=None, offset=0):
        self.sql.append(sql)
        cursor = self._db.execute(
            _FQN_RE.sub(r'"\2"', sql), tuple(params or ())
        )
        columns = [description[0] for description in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _fixture_result_row(
    entity_hash,
    digest,
    source,
    observed_at,
    batch,
    *,
    entity_type="protein",
    result_type="annotation",
):
    """One ``<type>_result`` row in ``_SqlExecutor``'s insert order."""
    return (
        entity_hash,
        entity_type,
        result_type,
        source,
        digest,
        "v1",
        "{}",
        observed_at,
        batch,
    )


#: THE EXPECTED-OUTPUT FIXTURE. Two parameter sets, a superseded result row,
#: two sources, and a DUPLICATED registry row -- every condition the count
#: semantics have to survive, in one table.
#:
#:   - entity 1 / DEFAULT set: TWO rows in one slot (b1 superseded by b2).
#:     Both are stored, so both count toward row_count; only b2 is current.
#:   - entity 1 / TUNED set: a DIFFERENT slot, not a supersede -- this is the
#:     whole point of parameter_set_hash being in the slot key.
#:   - entity 2 / DEFAULT set, under each of two sources.
_FIXTURE_RESULT_ROWS = (
    _fixture_result_row(_hash(1), DEFAULT_PARAMETER_SET_HASH, "blastp/2.14", "2026-01-01", "b1"),
    _fixture_result_row(_hash(1), DEFAULT_PARAMETER_SET_HASH, "blastp/2.14", "2026-01-02", "b2"),
    _fixture_result_row(_hash(1), _TUNED_HASH, "blastp/2.14", "2026-01-01", "b1"),
    _fixture_result_row(_hash(2), DEFAULT_PARAMETER_SET_HASH, "blastp/2.14", "2026-01-01", "b1"),
    _fixture_result_row(_hash(2), DEFAULT_PARAMETER_SET_HASH, "diamond/2.1", "2026-01-01", "b1"),
)

#: The registry: one row for the default set and TWO for the tuned set (a
#: retry appended the second), both carrying the SAME canonical_json. The
#: duplicate is the trap: a naive join would double the tuned set's counts.
_FIXTURE_REGISTRY_ROWS = (
    (DEFAULT_PARAMETER_SET_HASH, "{}", "2026-01-01", "b1"),
    (_TUNED_HASH, _TUNED_JSON, "2026-01-01", "b1"),
    (_TUNED_HASH, _TUNED_JSON, "2026-01-03", "b3"),
)


def _fixture_cap():
    """A capability over the expected-output fixture."""
    return ClearinghouseCapability(
        _SqlExecutor(
            result_rows=_FIXTURE_RESULT_ROWS,
            registry_rows=_FIXTURE_REGISTRY_ROWS,
        )
    )


# -- results()/current_state(): the parameter-set PRE-filter ---------------


def test_results_narrows_by_parameter_set_hash():
    """results() returns only the requested parameter set's current-state row."""
    cap = _fixture_cap()
    rows = cap.results(
        "protein", [_hash(1)], parameter_set_hashes=[_TUNED_HASH]
    )
    assert [row["parameter_set_hash"] for row in rows] == [_TUNED_HASH]


def test_results_parameter_set_filter_is_a_pre_filter_not_a_post_filter():
    """The filter is applied BEFORE the window, so a slot wins its own window.

    Entity 1 carries a LATER default-set row (b2) than its tuned-set row, so
    a filter applied AFTER the window would rank all three rows together,
    pick b2, and then discard it for not matching the tuned hash -- returning
    NOTHING. Pre-filtering returns the tuned slot's own current-state row.
    """
    cap = _fixture_cap()
    rows = cap.results(
        "protein", [_hash(1)], parameter_set_hashes=[_TUNED_HASH]
    )
    assert len(rows) == 1, "a post-filter would have returned zero rows here"
    assert rows[0]["ingest_batch_id"] == "b1"
    # And the predicate really is inside the windowed CTE rather than applied
    # to its output: it appears BEFORE the "WHERE rn = 1" that reads the
    # window's result.
    sql = cap._capability.sql[-1]
    assert sql.index(_TUNED_HASH) < sql.index("WHERE rn = 1")


def test_results_default_parameter_set_returns_the_superseding_row():
    """Within one (entity, ..., parameter_set) slot the latest row still wins."""
    cap = _fixture_cap()
    rows = cap.results(
        "protein", [_hash(1)], parameter_set_hashes=[DEFAULT_PARAMETER_SET_HASH]
    )
    assert [row["ingest_batch_id"] for row in rows] == ["b2"]


def test_results_empty_parameter_set_list_returns_nothing():
    """``[]`` is a request to match nothing, not a request for no filter."""
    cap = _fixture_cap()
    assert cap.results("protein", [_hash(1)], parameter_set_hashes=[]) == []
    assert "1 = 0" in cap._capability.sql[-1]


def test_current_state_narrows_by_parameter_set_hash():
    cap = _fixture_cap()
    rows = cap.current_state("protein", parameter_set_hashes=[_TUNED_HASH])
    assert [row["parameter_set_hash"] for row in rows] == [_TUNED_HASH]


def test_current_state_unfiltered_keeps_one_row_per_parameter_set_slot():
    """parameter_set_hash is in the slot key, so the two sets do not collide."""
    cap = _fixture_cap()
    rows = cap.current_state("protein")
    slots = {
        (row["entity_hash"], row["source"], row["parameter_set_hash"])
        for row in rows
    }
    assert len(rows) == 4 and len(slots) == 4
    assert (_hash(1), "blastp/2.14", _TUNED_HASH) in slots
    assert (_hash(1), "blastp/2.14", DEFAULT_PARAMETER_SET_HASH) in slots


def test_current_state_empty_parameter_set_list_returns_nothing():
    cap = _fixture_cap()
    assert cap.current_state("protein", parameter_set_hashes=[]) == []


@pytest.mark.parametrize(
    "bad",
    [
        "nothex" * 10 + "abcd",
        DEFAULT_PARAMETER_SET_HASH.upper(),
        "abc",
        None,
        1234,
    ],
)
@pytest.mark.parametrize("verb", ["results", "current_state"])
def test_non_hex_parameter_set_hash_raises_before_any_query(bad, verb):
    """A malformed filter value raises at the boundary, before any query."""
    fake = _FakeCapability()
    cap = ClearinghouseCapability(fake)
    with pytest.raises(ValueError, match="parameter_set_hashes"):
        if verb == "results":
            cap.results("protein", [_hash(1)], parameter_set_hashes=[bad])
        else:
            cap.current_state("protein", parameter_set_hashes=[bad])
    assert fake.calls == []


def test_malformed_parameter_set_hash_raises_even_with_an_empty_hash_list():
    """The digest is validated BEFORE the empty-``hashes`` short-circuit.

    Otherwise ``results(t, [], parameter_set_hashes=["BAD"])`` would return
    ``[]`` and the caller would never learn its digest was unusable.
    """
    fake = _FakeCapability()
    cap = ClearinghouseCapability(fake)
    with pytest.raises(ValueError, match="parameter_set_hashes"):
        cap.results("protein", [], parameter_set_hashes=["not-a-hash"])
    assert fake.calls == []


# -- sources(): the frozen default grain, and the opt-in finer grain -------


def test_sources_keeps_one_row_per_source_and_adds_parameter_set_count():
    """THE DEFAULT GRAIN AND THE EXISTING COUNT ARE PINNED HERE.

    ``row_count`` keeps exactly its existing population: COUNT(*) over every
    STORED row in the group, superseded rows included (sources() does not
    apply the current-state derivation and must not start). blastp/2.14 holds
    four stored rows across two parameter sets; only three of them are
    current, and the answer is still 4.
    """
    cap = _fixture_cap()
    rows = cap.sources("protein")
    assert rows == [
        {
            "entity_type": "protein",
            "source": "blastp/2.14",
            "row_count": 4,
            "parameter_set_count": 2,
        },
        {
            "entity_type": "protein",
            "source": "diamond/2.1",
            "row_count": 1,
            "parameter_set_count": 1,
        },
    ]


def test_sources_by_parameter_set_switches_grain_and_adds_canonical_json():
    """One row per (source, hash), with the registry text, counts unmultiplied.

    The tuned set has TWO registry rows. Its row_count must be 1 -- the count
    of its RESULT rows -- not 2. A LEFT JOIN against the raw registry would
    report 2 here and look entirely plausible doing it.
    """
    cap = _fixture_cap()
    rows = sorted(
        cap.sources("protein", by_parameter_set=True),
        key=lambda row: (row["source"], row["parameter_set_hash"]),
    )
    assert rows == sorted(
        [
            {
                "entity_type": "protein",
                "source": "blastp/2.14",
                "parameter_set_hash": DEFAULT_PARAMETER_SET_HASH,
                "row_count": 3,
                "parameter_set_count": 1,
                "canonical_json": "{}",
            },
            {
                "entity_type": "protein",
                "source": "blastp/2.14",
                "parameter_set_hash": _TUNED_HASH,
                "row_count": 1,
                "parameter_set_count": 1,
                "canonical_json": _TUNED_JSON,
            },
            {
                "entity_type": "protein",
                "source": "diamond/2.1",
                "parameter_set_hash": DEFAULT_PARAMETER_SET_HASH,
                "row_count": 1,
                "parameter_set_count": 1,
                "canonical_json": "{}",
            },
        ],
        key=lambda row: (row["source"], row["parameter_set_hash"]),
    )


def test_sources_grains_agree_on_the_total():
    """The finer grain partitions the coarser one -- it does not re-count."""
    cap = _fixture_cap()
    coarse = sum(row["row_count"] for row in cap.sources("protein"))
    fine = sum(
        row["row_count"] for row in cap.sources("protein", by_parameter_set=True)
    )
    assert coarse == fine == len(_FIXTURE_RESULT_ROWS)


def test_sources_by_parameter_set_reports_null_canonical_json_for_an_orphan():
    """A result hash with no registry row still gets a row, with no text."""
    cap = ClearinghouseCapability(
        _SqlExecutor(
            result_rows=(
                _fixture_result_row(_hash(1), _TUNED_HASH, "blastp/2.14", "2026-01-01", "b1"),
            ),
            registry_rows=(),
        )
    )
    rows = cap.sources("protein", by_parameter_set=True)
    assert len(rows) == 1
    assert rows[0]["parameter_set_hash"] == _TUNED_HASH
    assert rows[0]["canonical_json"] is None


def test_sources_default_grain_is_unchanged_when_the_registry_is_empty():
    """The default grain never touches the registry, so it cannot be affected."""
    cap = ClearinghouseCapability(
        _SqlExecutor(result_rows=_FIXTURE_RESULT_ROWS, registry_rows=())
    )
    assert cap.sources("protein") == _fixture_cap().sources("protein")


# -- parameter_sets(): one row per hash ------------------------------------


def test_parameter_sets_returns_one_row_per_hash_despite_duplicates():
    """The registry holds THREE rows for two hashes; the verb returns two.

    A ``SELECT DISTINCT`` over all four columns would return three rows here,
    because the two tuned rows differ in observed_at/ingest_batch_id. This is
    distinct ON the hash, and the representative is the latest row.
    """
    cap = _fixture_cap()
    rows = cap.parameter_sets()
    assert len(rows) == 2
    by_hash = {row["parameter_set_hash"]: row for row in rows}
    assert set(by_hash) == {DEFAULT_PARAMETER_SET_HASH, _TUNED_HASH}
    assert by_hash[_TUNED_HASH]["canonical_json"] == _TUNED_JSON
    # Deterministic representative: the LATEST row for that hash, not an
    # arbitrary one.
    assert by_hash[_TUNED_HASH]["ingest_batch_id"] == "b3"


def test_parameter_sets_filters_to_the_requested_hashes():
    cap = _fixture_cap()
    rows = cap.parameter_sets([_TUNED_HASH])
    assert [row["parameter_set_hash"] for row in rows] == [_TUNED_HASH]


def test_parameter_sets_empty_list_matches_nothing_without_querying():
    fake = _FakeCapability()
    cap = ClearinghouseCapability(fake)
    assert cap.parameter_sets([]) == []
    assert fake.calls == []


def test_parameter_sets_rejects_a_malformed_hash():
    fake = _FakeCapability()
    cap = ClearinghouseCapability(fake)
    with pytest.raises(ValueError, match="parameter_set_hashes"):
        cap.parameter_sets(["nope"])
    assert fake.calls == []


def test_default_parameter_set_hash_is_registered_in_the_fixture():
    """Guards the constant against drift from parameter_set_hash({})."""
    assert DEFAULT_PARAMETER_SET_HASH == parameter_set_hash({})
    cap = _fixture_cap()
    rows = cap.parameter_sets([DEFAULT_PARAMETER_SET_HASH])
    assert rows[0]["canonical_json"] == "{}"


# -- parameter_set_orphans(): the four integrity conditions ----------------


def test_parameter_set_orphans_is_clean_on_a_sound_registry():
    report = _fixture_cap().parameter_set_orphans()
    assert report["ok"] is True
    assert report["failures"] == []
    assert report["registry_hashes"] == 2
    assert report["result_hashes"] == 2


def test_parameter_set_orphans_reports_a_result_hash_missing_from_the_registry():
    cap = ClearinghouseCapability(
        _SqlExecutor(
            result_rows=(
                _fixture_result_row(_hash(1), _TUNED_HASH, "blastp/2.14", "2026-01-01", "b1"),
            ),
            registry_rows=(),
        )
    )
    report = cap.parameter_set_orphans()
    assert report["ok"] is False
    assert report["missing_from_registry"] == [
        {"parameter_set_hash": _TUNED_HASH, "entity_types": ["protein"]}
    ]
    assert any(_TUNED_HASH in line for line in report["failures"])


def test_parameter_set_orphans_names_every_entity_type_an_orphan_appears_in():
    executor = _SqlExecutor(
        result_rows=(
            _fixture_result_row(_hash(1), _TUNED_HASH, "blastp/2.14", "2026-01-01", "b1"),
        ),
        registry_rows=(),
    )
    executor._db.execute(
        f'INSERT INTO "{table_name("gene", "result")}" '
        "(entity_hash, entity_type, result_type, source, parameter_set_hash, "
        "result_type_version, payload, observed_at, ingest_batch_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        _fixture_result_row(
            _hash(1), _TUNED_HASH, "blastp/2.14", "2026-01-01", "b1",
            entity_type="gene",
        ),
    )
    executor._db.commit()
    report = ClearinghouseCapability(executor).parameter_set_orphans()
    assert report["missing_from_registry"][0]["entity_types"] == ["gene", "protein"]


@pytest.mark.parametrize("bad", ["not-64-hex", DEFAULT_PARAMETER_SET_HASH.upper(), None])
def test_parameter_set_orphans_reports_a_malformed_result_hash(bad):
    """A stored hash that is not 64 lowercase hex -- NULL included.

    The column is required and non-null by a Python check at the WRITE
    boundary and never by the DDL, so a NULL that got in is exactly the
    violation only a read can catch.
    """
    cap = ClearinghouseCapability(
        _SqlExecutor(
            result_rows=(
                _fixture_result_row(_hash(1), bad, "blastp/2.14", "2026-01-01", "b1"),
            ),
            registry_rows=_FIXTURE_REGISTRY_ROWS,
        )
    )
    report = cap.parameter_set_orphans()
    assert report["ok"] is False
    assert report["malformed_result_hashes"] == [
        {"parameter_set_hash": bad, "entity_types": ["protein"]}
    ]
    assert any("malformed" in line for line in report["failures"])
    # A malformed hash is reported as malformed, NOT also as missing: an
    # operator must not be sent looking for a registry row for 'None'.
    assert report["missing_from_registry"] == []


def test_parameter_set_orphans_reports_conflicting_canonical_json():
    """Two registry rows for one hash carrying DIFFERENT text."""
    cap = ClearinghouseCapability(
        _SqlExecutor(
            result_rows=(),
            registry_rows=(
                (_TUNED_HASH, _TUNED_JSON, "2026-01-01", "b1"),
                (_TUNED_HASH, '{"threshold":"1e-9"}', "2026-01-02", "b2"),
            ),
        )
    )
    report = cap.parameter_set_orphans()
    assert report["ok"] is False
    conflict = report["conflicting_canonical_json"]
    assert len(conflict) == 1
    assert conflict[0]["parameter_set_hash"] == _TUNED_HASH
    assert len(conflict[0]["canonical_json"]) == 2
    assert any("DIFFERENT canonical_json" in line for line in report["failures"])


def test_parameter_set_orphans_tolerates_duplicate_but_identical_registry_rows():
    """A retry's duplicate row is harmless and must NOT be a conflict."""
    cap = _fixture_cap()
    report = cap.parameter_set_orphans()
    assert report["conflicting_canonical_json"] == []
    assert report["ok"] is True


def test_parameter_set_orphans_reports_canonical_json_that_misses_its_key():
    """Text that parses perfectly but keys the wrong row -- the dangerous one."""
    cap = ClearinghouseCapability(
        _SqlExecutor(
            result_rows=(),
            registry_rows=(
                # Valid, canonical text filed under the DEFAULT hash.
                (DEFAULT_PARAMETER_SET_HASH, _TUNED_JSON, "2026-01-01", "b1"),
            ),
        )
    )
    report = cap.parameter_set_orphans()
    assert report["ok"] is False
    assert len(report["mishashed_registry_rows"]) == 1
    entry = report["mishashed_registry_rows"][0]
    assert entry["parameter_set_hash"] == DEFAULT_PARAMETER_SET_HASH
    assert entry["computed_parameter_set_hash"] == _TUNED_HASH
    assert any("mis-hashed" in line for line in report["failures"])


@pytest.mark.parametrize(
    "text", ["{not json", '{"Bad Key": 1}', '{"a": 1.5}', "[]", None]
)
def test_parameter_set_orphans_reports_unhashable_canonical_json(text):
    """Text that cannot be parsed or fails the parameter-set rule counts too.

    It cannot be SHOWN to hash to its key, which is the same finding: a float,
    an uppercase key, a non-object and a NULL are all registry rows that
    cannot explain the hash they are filed under.
    """
    cap = ClearinghouseCapability(
        _SqlExecutor(
            result_rows=(),
            registry_rows=((DEFAULT_PARAMETER_SET_HASH, text, "2026-01-01", "b1"),),
        )
    )
    report = cap.parameter_set_orphans()
    assert report["ok"] is False
    assert len(report["mishashed_registry_rows"]) == 1
    assert report["mishashed_registry_rows"][0][
        "computed_parameter_set_hash"
    ] is None


def test_parameter_set_orphans_finds_all_four_conditions_at_once():
    """One broken registry, four findings -- the check does not stop at the first."""
    cap = ClearinghouseCapability(
        _SqlExecutor(
            result_rows=(
                # (1) missing from the registry
                _fixture_result_row(_hash(3), _hash(9), "blastp/2.14", "2026-01-01", "b1"),
                # (2) malformed
                _fixture_result_row(_hash(4), "SHORT", "blastp/2.14", "2026-01-01", "b1"),
            ),
            registry_rows=(
                # (3) conflicting text for one hash
                (_TUNED_HASH, _TUNED_JSON, "2026-01-01", "b1"),
                (_TUNED_HASH, '{"threshold":"1e-9"}', "2026-01-02", "b2"),
                # (4) text that does not hash to its key
                (DEFAULT_PARAMETER_SET_HASH, _TUNED_JSON, "2026-01-01", "b1"),
            ),
        )
    )
    report = cap.parameter_set_orphans()
    assert report["ok"] is False
    assert len(report["missing_from_registry"]) == 1
    assert len(report["malformed_result_hashes"]) == 1
    assert len(report["conflicting_canonical_json"]) == 1
    # Both conflicting rows are checked against the key, and one of them
    # happens to be the tuned text under the tuned key (sound), so the
    # mis-hashed findings are the 1e-9 row and the default-key row.
    assert len(report["mishashed_registry_rows"]) == 2
    assert len(report["failures"]) >= 4


def test_parameter_set_orphans_writes_nothing():
    """An integrity check is a READ. It must never load or mutate."""
    cap = _fixture_cap()
    cap.parameter_set_orphans()
    assert all(
        sql.lstrip().upper().startswith("SELECT") for sql in cap._capability.sql
    )


# -- verify_run(): a registry finding is a DISCREPANCY ---------------------


def test_verify_run_reports_a_parameter_set_orphan_as_a_discrepancy(tmp_path):
    """A registry finding fails verify, exactly as a missing row does.

    An orphaned registry entry means a stored result cannot be attributed to
    the parameters that produced it, which is the same class of problem as
    rows the ledger says landed being absent -- so it lands in the same
    ``discrepancies`` list, not in a softer channel.
    """
    _write_verify_ledger(
        tmp_path, "r1", [("gene_entity", "0000", "ingested", 6, 123456)]
    )
    fake = _FakeVerifyCapability(
        count_rows=6,
        snapshot_ids=(123456,),
        result_parameter_sets=(_TUNED_HASH,),
        registry_pairs=(),  # the hash is used but never registered
    )
    cap = ClearinghouseCapability(fake)
    result = cap.verify_run(tmp_path, run_id="r1")
    assert result["parameter_set_integrity"]["ok"] is False
    assert any(_TUNED_HASH in line for line in result["discrepancies"])
    # The ledger leg itself reconciled -- the discrepancy is purely the
    # registry's, so a clean ledger no longer means a clean verify.
    assert result["tables"][0]["ok"] is True


def test_verify_run_is_clean_when_the_registry_is_sound(tmp_path):
    _write_verify_ledger(
        tmp_path, "r1", [("gene_entity", "0000", "ingested", 6, 123456)]
    )
    fake = _FakeVerifyCapability(
        count_rows=6,
        snapshot_ids=(123456,),
        result_parameter_sets=(_TUNED_HASH,),
        registry_pairs=((_TUNED_HASH, _TUNED_JSON),),
    )
    cap = ClearinghouseCapability(fake)
    result = cap.verify_run(tmp_path, run_id="r1")
    assert result["parameter_set_integrity"]["ok"] is True
    assert result["discrepancies"] == []
