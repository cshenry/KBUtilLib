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

import pytest

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
    ClearinghouseWriteTargetMismatchError,
)
from kbutillib.domains.kbase.berdl.clearinghouse_schema import (
    ENTITY_TYPES,
    NAMESPACE,
    TENANT,
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
    """

    def __init__(
        self,
        *,
        locus="in_pod",
        write_ns=f"{TENANT}.{NAMESPACE}",
        probe_ns=f"{TENANT}.{NAMESPACE}",
        postflight_row_count=1,
        snapshot_rows=None,
    ):
        self._locus = locus
        self._write_ns = write_ns
        self._probe_ns = probe_ns
        self._postflight_row_count = postflight_row_count
        self._snapshot_rows = snapshot_rows if snapshot_rows is not None else []
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
    fake = _FakeWriteCapability()
    cap = ClearinghouseCapability(fake)
    cap.register(entity_type, [{"entity_hash": _hash(1)}], kind=kind)
    assert fake.load_calls[0]["tables"][0]["name"] == table_name(entity_type, kind)
    # Always requested as an append -- never overwrite from this layer.
    assert fake.load_calls[0]["tables"][0]["mode"] == "append"


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

    def __init__(self, *, locus="in_pod", count_rows=6, snapshot_ids=(123456,)):
        self._locus = locus
        self._count_rows = count_rows
        self._snapshot_ids = list(snapshot_ids)
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
        return []  # pragma: no cover - verify issues only the two above


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
