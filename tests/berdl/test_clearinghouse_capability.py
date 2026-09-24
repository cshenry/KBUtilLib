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

from kbutillib.domains.kbase.berdl.clearinghouse_capability import (
    _HEX_RUN_RE,
    OFFPOD_PAGE_CAP,
    SPARK_PROMOTION_THRESHOLD,
    ClearinghouseCapability,
)
from kbutillib.domains.kbase.berdl.clearinghouse_schema import (
    ENTITY_TYPES,
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
    cap2.known("gene", [_hash(i) for i in range(SPARK_PROMOTION_THRESHOLD)], engine="trino")
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
        table_name(e, k)
        for k in ("entity", "content", "result")
        for e in ENTITY_TYPES
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
