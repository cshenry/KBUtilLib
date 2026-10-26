"""Offline tests for the Europe PMC client.

Every test here is fully offline.  Most inject the three documented seams
(HTTP session, clock/sleep, cache) and replay recorded Europe PMC JSON from
``tests/literature/fixtures/``.  :class:`TestDefaultWiring` deliberately does
the opposite: it constructs the client with no seams at all and intercepts at
the transport layer, so the default wiring itself is under test.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import requests
import requests.adapters

from kbutillib.core import shared_env_utils
from kbutillib.domains.literature.cache import (
    LiteratureCache,
    default_cache_path,
    resolve_cache_path,
)
from kbutillib.domains.literature.europepmc import (
    EUROPEPMC_SEARCH_URL,
    HTTP_CACHE_TABLE,
    IDS_PER_REQUEST,
    EuropePMCClient,
    EuropePMCClientImpl,
    alias_query,
    canonical_paper_id,
    merge_ids,
    normalise_paper_id,
)

FIXTURES = Path(__file__).parent / "fixtures"

HIT_KEYS = {"pmid", "pmcid", "doi", "title", "journal", "year", "abstract", "source"}


def load_fixture(name: str) -> dict:
    """Load a recorded Europe PMC response."""
    return json.loads((FIXTURES / name).read_text())


# ── test doubles ─────────────────────────────────────────────────────────


class FakeResponse:
    """Minimal stand-in for a requests.Response."""

    def __init__(self, status_code: int = 200, payload=None) -> None:
        self.status_code = status_code
        self.reason = "fake"
        self.url = EUROPEPMC_SEARCH_URL
        self._payload = {} if payload is None else payload

    def json(self):
        # Deep copy, so a caller mutating the result cannot corrupt the fixture.
        return json.loads(json.dumps(self._payload))

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class FakeSession:
    """Stands in for requests.Session at the ``get`` boundary.

    Raises on any call beyond the scripted responses, which is how the
    caching tests prove that no HTTP happened.
    """

    def __init__(self, *responses, clock=None) -> None:
        self._responses = list(responses)
        self._lock = threading.Lock()
        self.clock = clock
        self.calls = []

    def get(self, url, params=None, timeout=None):
        with self._lock:
            self.calls.append(
                {
                    "url": url,
                    "params": dict(params or {}),
                    "timeout": timeout,
                    "at": self.clock.now() if self.clock is not None else None,
                }
            )
            if not self._responses:
                raise AssertionError(
                    f"unexpected HTTP call #{len(self.calls)} to {url} params={params}"
                )
            return self._responses.pop(0)

    @property
    def call_count(self) -> int:
        return len(self.calls)


class FakeClock:
    """Deterministic clock whose ``sleep`` advances time.  Thread-safe."""

    def __init__(self, start: float = 1000.0) -> None:
        self._lock = threading.Lock()
        self.time = start
        self.sleeps = []

    def now(self) -> float:
        with self._lock:
            return self.time

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.sleeps.append(seconds)
            self.time += max(0.0, seconds)


def make_client(tmp_path, *responses, clock=None, config=None, **kwargs):
    """Build a fully seam-injected client.  Returns (client, session, clock)."""
    clock = clock if clock is not None else FakeClock()
    session = FakeSession(*responses, clock=clock)
    client = EuropePMCClient(
        session=session,
        clock=clock.now,
        sleep=clock.sleep,
        cache=LiteratureCache(tmp_path / "gene_literature.sqlite"),
        config={"gene_literature": {"europepmc_rps": 2}} if config is None else config,
        config_file=False,
        token_file=None,
        kbase_token_file=None,
        **kwargs,
    )
    return client, session, clock


# ── identifier handling ──────────────────────────────────────────────────


class TestCanonicalPaperId:
    """canonical_paper_id builds PMID > PMCID > DOI canonical forms."""

    def test_pmid_wins_over_other_ids(self):
        assert (
            canonical_paper_id(pmid="123", pmcid="PMC9", doi="10.1/x") == "PMID:123"
        )

    def test_pmcid_used_when_no_pmid(self):
        assert canonical_paper_id(pmcid="PMC9", doi="10.1/x") == "PMC:PMC9"

    def test_bare_pmcid_digits_get_the_pmc_prefix(self):
        assert canonical_paper_id(pmcid="9") == "PMC:PMC9"

    def test_prefixed_inputs_are_tolerated(self):
        assert canonical_paper_id(pmid="PMID:7") == "PMID:7"
        assert canonical_paper_id(pmcid="pmc:pmc12") == "PMC:PMC12"
        assert canonical_paper_id(doi="DOI:10.1/X") == "DOI:10.1/x"

    def test_doi_is_lowercased(self):
        assert canonical_paper_id(doi="10.1038/NSMB.1583") == "DOI:10.1038/nsmb.1583"

    def test_no_ids_raises(self):
        with pytest.raises(ValueError):
            canonical_paper_id()

    def test_malformed_pmid_raises(self):
        with pytest.raises(ValueError):
            canonical_paper_id(pmid="not-a-number")


class TestNormalisePaperId:
    """normalise_paper_id accepts every spelling we see in the wild."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("PMID:1", "PMID:1"),
            ("1234567", "PMID:1234567"),
            ("PMC123", "PMC:PMC123"),
            ("pmc:PMC123", "PMC:PMC123"),
            ("10.x/y", "DOI:10.x/y"),
            ("doi:10.x/y", "DOI:10.x/y"),
            ("https://doi.org/10.x/y", "DOI:10.x/y"),
            ("https://pubmed.ncbi.nlm.nih.gov/123/", "PMID:123"),
            ("https://pubmed.ncbi.nlm.nih.gov/123", "PMID:123"),
            ("  PMID: 42  ", "PMID:42"),
            ("pubmed:42", "PMID:42"),
            ("PMCID:PMC7", "PMC:PMC7"),
            ("10.1038/NSMB.1583", "DOI:10.1038/nsmb.1583"),
        ],
    )
    def test_recognised_forms(self, text, expected):
        assert normalise_paper_id(text) == expected

    @pytest.mark.parametrize(
        "text", [None, "", "   ", "dnaA", "PMID:abc", "PMC:xyz", "10.x", "notanid"]
    )
    def test_unrecognised_forms_return_none(self, text):
        assert normalise_paper_id(text) is None


class TestAliasQuery:
    """alias_query decides when an alias needs organism disambiguation."""

    def test_locus_tag_is_an_unqualified_exact_phrase(self):
        assert alias_query("ACIAD0380", ["Acinetobacter baylyi", "ADP1"]) == '"ACIAD0380"'

    def test_refseq_style_locus_tag_is_also_unqualified(self):
        assert alias_query("ACIAD_RS00005", ["Acinetobacter baylyi"]) == '"ACIAD_RS00005"'

    def test_gene_symbol_is_conjoined_with_organism_terms(self):
        assert alias_query("dnaA", ["Acinetobacter baylyi", "ADP1"]) == (
            '"dnaA" AND ("Acinetobacter baylyi" OR "ADP1")'
        )

    def test_short_alias_is_not_searchable(self):
        assert alias_query("ab", ["Acinetobacter baylyi"]) is None

    def test_gene_symbol_without_organisms_is_bare_phrase(self):
        assert alias_query("dnaA", []) == '"dnaA"'
        assert alias_query("dnaA", None) == '"dnaA"'

    def test_blank_organism_terms_are_dropped(self):
        assert alias_query("dnaA", ["ADP1", "", None, "  "]) == '"dnaA" AND ("ADP1")'

    def test_double_quotes_are_escaped_in_alias_and_organism(self):
        query = alias_query('gene"X', ['Strain "A"'])
        assert query == '"gene\\"X" AND ("Strain \\"A\\"")'
        # Three escaped quotes: one from the alias, two from the organism.
        # Any unescaped one would terminate the phrase and corrupt the query.
        assert query.count('\\"') == 3


# ── search / paging ──────────────────────────────────────────────────────


class TestSearch:
    """search() sends the documented params and follows cursorMark."""

    def test_paging_follows_cursor_mark_and_concatenates_hits(self, tmp_path):
        page1 = load_fixture("search_dnaA_page1.json")
        page2 = load_fixture("search_dnaA_page2.json")
        client, session, _ = make_client(
            tmp_path, FakeResponse(payload=page1), FakeResponse(payload=page2)
        )

        hits = client.search('"dnaA" AND ("ADP1")', page_size=2, max_results=10)

        assert session.call_count == 2
        first, second = session.calls
        assert first["url"] == EUROPEPMC_SEARCH_URL
        assert first["params"] == {
            "query": '"dnaA" AND ("ADP1")',
            "format": "json",
            "resultType": "core",
            "pageSize": 2,
            "cursorMark": "*",
        }
        assert second["params"]["cursorMark"] == page1["nextCursorMark"]
        assert [hit["pmid"] for hit in hits] == ["15210978", "19383134", None]

    def test_paging_stops_when_the_cursor_repeats(self, tmp_path):
        # The page-2 fixture echoes its own cursorMark, which is how Europe PMC
        # signals the end of a result set.  A third request would make the
        # FakeSession raise.
        client, session, _ = make_client(
            tmp_path,
            FakeResponse(payload=load_fixture("search_dnaA_page1.json")),
            FakeResponse(payload=load_fixture("search_dnaA_page2.json")),
        )
        assert len(client.search("q", page_size=2, max_results=100)) == 3
        assert session.call_count == 2

    def test_hit_fields_are_exactly_the_published_set(self, tmp_path):
        client, _, _ = make_client(
            tmp_path, FakeResponse(payload=load_fixture("search_dnaA_page1.json"))
        )
        hits = client.search("q", page_size=2, max_results=2)

        assert set(hits[0]) == HIT_KEYS
        assert hits[0]["pmid"] == "15210978"
        assert hits[0]["pmcid"] == "PMC443546"
        assert hits[0]["doi"] == "10.1093/nar/gkh626"
        assert hits[0]["title"].startswith("Unique features revealed")
        assert hits[0]["journal"] == "Nucleic acids research"
        assert hits[0]["year"] == 2004
        assert hits[0]["abstract"].startswith("The genome of Acinetobacter sp. ADP1")
        assert hits[0]["source"] == "MED"

    def test_pmc_only_hit_maps_cleanly(self, tmp_path):
        client, _, _ = make_client(
            tmp_path, FakeResponse(payload=load_fixture("search_dnaA_page2.json"))
        )
        hit = client.search("q", page_size=2, max_results=1)[0]
        assert hit["pmid"] is None
        assert hit["pmcid"] == "PMC2828762"
        assert hit["journal"] == "BMC genomics"
        assert hit["year"] == 2010
        assert hit["source"] == "PMC"

    def test_max_results_caps_hits_and_shrinks_page_size(self, tmp_path):
        client, session, _ = make_client(
            tmp_path, FakeResponse(payload=load_fixture("search_dnaA_page1.json"))
        )
        hits = client.search("q", page_size=100, max_results=1)

        assert len(hits) == 1
        assert session.call_count == 1
        assert session.calls[0]["params"]["pageSize"] == 1

    def test_empty_result_list_ends_paging(self, tmp_path):
        client, session, _ = make_client(
            tmp_path, FakeResponse(payload=load_fixture("search_empty.json"))
        )
        assert client.search("q", page_size=25, max_results=100) == []
        assert session.call_count == 1

    def test_blank_query_makes_no_request(self, tmp_path):
        client, session, _ = make_client(tmp_path)
        assert client.search("") == []
        assert session.call_count == 0


# ── throttling ───────────────────────────────────────────────────────────


class TestThrottle:
    """Requests are separated by the configured minimum interval."""

    def test_requests_are_spaced_by_the_configured_interval(self, tmp_path):
        clock = FakeClock(start=500.0)
        client, session, _ = make_client(
            tmp_path,
            FakeResponse(payload=load_fixture("search_empty.json")),
            FakeResponse(payload=load_fixture("search_empty.json")),
            clock=clock,
        )

        client.search("query-one", page_size=25, max_results=25)
        client.search("query-two", page_size=25, max_results=25)

        stamps = [call["at"] for call in session.calls]
        assert stamps[0] == 500.0
        # 2 requests/s -> the second request waits half a second.
        assert stamps[1] - stamps[0] == pytest.approx(0.5)
        assert clock.sleeps == [pytest.approx(0.5)]

    def test_rate_comes_from_config(self, tmp_path):
        clock = FakeClock()
        client, _, _ = make_client(
            tmp_path,
            FakeResponse(payload=load_fixture("search_empty.json")),
            FakeResponse(payload=load_fixture("search_empty.json")),
            clock=clock,
            config={"gene_literature": {"europepmc_rps": 4}},
        )
        assert client.min_interval == pytest.approx(0.25)

        client.search("query-one", page_size=25, max_results=25)
        client.search("query-two", page_size=25, max_results=25)
        assert clock.sleeps == [pytest.approx(0.25)]

    def test_default_rate_is_two_per_second(self, tmp_path):
        client, _, _ = make_client(tmp_path, config={})
        assert client.min_interval == pytest.approx(0.5)

    def test_non_numeric_rate_falls_back_to_the_default(self, tmp_path):
        client, _, _ = make_client(
            tmp_path, config={"gene_literature": {"europepmc_rps": "fast"}}
        )
        assert client.min_interval == pytest.approx(0.5)

    def test_default_throttle_is_process_wide(self, tmp_path):
        # The rate limit belongs to the service, so two default-constructed
        # clients must gate each other rather than each keeping its own pace.
        shared = {
            "cache": LiteratureCache(tmp_path / "c.sqlite"),
            "config": {},
            "config_file": False,
            "token_file": None,
            "kbase_token_file": None,
        }
        first = EuropePMCClient(session=FakeSession(), **shared)
        second = EuropePMCClient(session=FakeSession(), **shared)
        assert first._throttle is second._throttle

    def test_injected_clock_gets_a_private_throttle(self, tmp_path):
        # A fake clock must not leave its fake timestamps in the process-wide
        # throttle, where a real request would then wait on them.
        faked, _, _ = make_client(tmp_path)
        default = EuropePMCClient(
            session=FakeSession(),
            cache=LiteratureCache(tmp_path / "c.sqlite"),
            config={},
            config_file=False,
            token_file=None,
            kbase_token_file=None,
        )
        assert faked._throttle is not default._throttle

    def test_throttle_holds_across_asyncio_tasks(self, tmp_path):
        clock = FakeClock(start=0.0)
        empty = load_fixture("search_empty.json")
        task_count = 3
        client, session, _ = make_client(
            tmp_path,
            *[FakeResponse(payload=empty) for _ in range(task_count)],
            clock=clock,
        )

        async def drive():
            await asyncio.gather(
                *[
                    asyncio.to_thread(
                        client.search, f"task-query-{index}", 25, 25
                    )
                    for index in range(task_count)
                ]
            )

        asyncio.run(drive())

        assert session.call_count == task_count
        stamps = sorted(call["at"] for call in session.calls)
        gaps = [later - earlier for earlier, later in zip(stamps, stamps[1:])]
        assert all(gap >= 0.5 - 1e-9 for gap in gaps), gaps

    def test_throttle_serialises_requests_from_many_threads(self, tmp_path):
        clock = FakeClock(start=0.0)
        empty = load_fixture("search_empty.json")
        thread_count = 4
        client, session, _ = make_client(
            tmp_path,
            *[FakeResponse(payload=empty) for _ in range(thread_count)],
            clock=clock,
        )

        errors = []

        def worker(index):
            try:
                client.search(f"thread-query-{index}", page_size=25, max_results=25)
            except Exception as exc:  # pragma: no cover - surfaced via errors
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(index,))
            for index in range(thread_count)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert not [thread for thread in threads if thread.is_alive()]
        assert errors == []
        assert session.call_count == thread_count
        stamps = sorted(call["at"] for call in session.calls)
        gaps = [later - earlier for earlier, later in zip(stamps, stamps[1:])]
        assert all(gap >= 0.5 - 1e-9 for gap in gaps), gaps


# ── retry ────────────────────────────────────────────────────────────────


class TestRetry:
    """429 and 5xx are retried with exponential backoff; 4xx is not."""

    def test_429_then_success(self, tmp_path):
        clock = FakeClock()
        client, session, _ = make_client(
            tmp_path,
            FakeResponse(429),
            FakeResponse(payload=load_fixture("search_dnaA_page1.json")),
            clock=clock,
        )

        hits = client.search("q", page_size=2, max_results=2)

        assert len(hits) == 2
        assert session.call_count == 2
        assert clock.sleeps == [pytest.approx(1.0)]

    def test_5xx_retries_to_the_budget_then_raises(self, tmp_path):
        clock = FakeClock()
        client, session, _ = make_client(
            tmp_path, *[FakeResponse(503) for _ in range(5)], clock=clock
        )

        with pytest.raises(requests.HTTPError):
            client.search("q", page_size=25, max_results=25)

        assert session.call_count == 5
        assert clock.sleeps == [
            pytest.approx(1.0),
            pytest.approx(2.0),
            pytest.approx(4.0),
            pytest.approx(8.0),
        ]

    def test_max_tries_is_configurable(self, tmp_path):
        client, session, _ = make_client(
            tmp_path, *[FakeResponse(500) for _ in range(2)], max_tries=2
        )
        with pytest.raises(requests.HTTPError):
            client.search("q", page_size=25, max_results=25)
        assert session.call_count == 2

    def test_client_error_is_not_retried(self, tmp_path):
        client, session, _ = make_client(tmp_path, FakeResponse(404))
        with pytest.raises(requests.HTTPError):
            client.search("q", page_size=25, max_results=25)
        assert session.call_count == 1


# ── caching ──────────────────────────────────────────────────────────────


class TestCaching:
    """Every request URL+params is cached in the http_cache table."""

    def test_second_identical_search_makes_no_http_call(self, tmp_path):
        # Exactly one scripted response: a second HTTP call would raise.
        client, session, _ = make_client(
            tmp_path, FakeResponse(payload=load_fixture("search_dnaA_page1.json"))
        )

        first = client.search("q", page_size=2, max_results=2)
        second = client.search("q", page_size=2, max_results=2)

        assert session.call_count == 1
        assert second == first

    def test_cache_is_shared_across_client_instances(self, tmp_path):
        cache_path = tmp_path / "shared.sqlite"
        page1 = load_fixture("search_dnaA_page1.json")

        warm_session = FakeSession(FakeResponse(payload=page1))
        warm = EuropePMCClient(
            session=warm_session,
            clock=FakeClock().now,
            sleep=FakeClock().sleep,
            cache=LiteratureCache(cache_path),
            config={},
            config_file=False,
            token_file=None,
            kbase_token_file=None,
        )
        expected = warm.search("q", page_size=2, max_results=2)
        assert warm_session.call_count == 1

        cold_session = FakeSession()  # no scripted responses at all
        cold = EuropePMCClient(
            session=cold_session,
            clock=FakeClock().now,
            sleep=FakeClock().sleep,
            cache=LiteratureCache(cache_path),
            config={},
            config_file=False,
            token_file=None,
            kbase_token_file=None,
        )
        assert cold.search("q", page_size=2, max_results=2) == expected
        assert cold_session.call_count == 0

    def test_cache_row_holds_the_request_url_and_response(self, tmp_path):
        client, _, _ = make_client(
            tmp_path, FakeResponse(payload=load_fixture("search_dnaA_page1.json"))
        )
        client.search("dnaA", page_size=2, max_results=2)

        cache = LiteratureCache(tmp_path / "gene_literature.sqlite")
        key = f"{EUROPEPMC_SEARCH_URL}?cursorMark=%2A&format=json&pageSize=2&query=dnaA&resultType=core"
        assert cache.get(HTTP_CACHE_TABLE, key)["hitCount"] == 3

    def test_failed_requests_are_not_cached(self, tmp_path):
        cache = LiteratureCache(tmp_path / "gene_literature.sqlite")
        failing, _, _ = make_client(tmp_path, *[FakeResponse(503) for _ in range(5)])
        with pytest.raises(requests.HTTPError):
            failing.search("q", page_size=25, max_results=25)

        retry_session = FakeSession(FakeResponse(payload=load_fixture("search_empty.json")))
        retrying = EuropePMCClient(
            session=retry_session,
            clock=FakeClock().now,
            sleep=FakeClock().sleep,
            cache=cache,
            config={},
            config_file=False,
            token_file=None,
            kbase_token_file=None,
        )
        assert retrying.search("q", page_size=25, max_results=25) == []
        assert retry_session.call_count == 1


# ── get / resolve ────────────────────────────────────────────────────────


class TestGet:
    """get() OR-s id clauses, batching at IDS_PER_REQUEST."""

    def test_ids_are_or_ed_into_one_query(self, tmp_path):
        client, session, _ = make_client(
            tmp_path, FakeResponse(payload=load_fixture("get_batch.json"))
        )

        records = client.get(
            ["15210978", "PMC2828762", "doi:10.1038/nsmb.1583", "garbage"]
        )

        query = session.calls[0]["params"]["query"]
        assert "(EXT_ID:15210978 AND SRC:MED)" in query
        assert "(PMCID:PMC2828762)" in query
        assert '(DOI:"10.1038/nsmb.1583")' in query
        assert query.count(" OR ") == 2
        assert "garbage" not in query

        assert set(records) == {"PMID:15210978", "PMC:PMC2828762"}
        assert records["PMID:15210978"]["title"].startswith("Unique features")
        assert records["PMC:PMC2828762"]["year"] == 2010

    def test_record_is_also_keyed_by_the_requested_id(self, tmp_path):
        client, _, _ = make_client(
            tmp_path, FakeResponse(payload=load_fixture("get_batch.json"))
        )
        records = client.get(["10.1093/nar/gkh626"])

        # Asked by DOI, answered by a record whose primary id is its PMID:
        # both keys are present and hold the same record.
        assert records["DOI:10.1093/nar/gkh626"] is records["PMID:15210978"]

    def test_ids_are_batched_fifty_at_a_time(self, tmp_path):
        empty = load_fixture("search_empty.json")
        client, session, _ = make_client(
            tmp_path, FakeResponse(payload=empty), FakeResponse(payload=empty)
        )

        ids = [str(100000 + index) for index in range(IDS_PER_REQUEST + 10)]
        assert client.get(ids) == {}

        assert session.call_count == 2
        assert session.calls[0]["params"]["query"].count("EXT_ID:") == IDS_PER_REQUEST
        assert session.calls[1]["params"]["query"].count("EXT_ID:") == 10

    def test_duplicate_ids_are_requested_once(self, tmp_path):
        client, session, _ = make_client(
            tmp_path, FakeResponse(payload=load_fixture("search_empty.json"))
        )
        client.get(["123", "PMID:123", "https://pubmed.ncbi.nlm.nih.gov/123/"])
        assert session.calls[0]["params"]["query"].count("EXT_ID:") == 1

    def test_unrecognisable_ids_alone_make_no_request(self, tmp_path):
        client, session, _ = make_client(tmp_path)
        assert client.get(["nope", "", None]) == {}
        assert session.call_count == 0


class TestResolve:
    """resolve() returns a single record carrying its canonical ids."""

    def test_resolve_returns_canonical_ids(self, tmp_path):
        client, _, _ = make_client(
            tmp_path, FakeResponse(payload=load_fixture("get_batch.json"))
        )
        record = client.resolve("https://pubmed.ncbi.nlm.nih.gov/15210978/")

        assert record["canonical_id"] == "PMID:15210978"
        assert record["canonical_ids"] == [
            "PMID:15210978",
            "PMC:PMC443546",
            "DOI:10.1093/nar/gkh626",
        ]
        assert record["title"].startswith("Unique features")

    def test_resolve_returns_none_when_not_found(self, tmp_path):
        client, _, _ = make_client(
            tmp_path, FakeResponse(payload=load_fixture("search_empty.json"))
        )
        assert client.resolve("99999999999") is None

    def test_resolve_returns_none_for_unrecognisable_id(self, tmp_path):
        client, session, _ = make_client(tmp_path)
        assert client.resolve("not an id") is None
        assert session.call_count == 0


# ── merge_ids ────────────────────────────────────────────────────────────


class TestMergeIds:
    """merge_ids unions records transitively on any shared id."""

    def test_records_are_unioned_transitively(self):
        records = [
            {"pmid": "1", "title": "A"},
            {"pmcid": "PMC9", "doi": "10.1/x", "journal": "J"},
            {"pmid": "1", "pmcid": "PMC9", "year": 2004},
            {"doi": "10.2/y", "title": "B"},
            {"title": "no ids at all"},
        ]

        merged, aliases, unkeyed = merge_ids(records)

        assert set(merged) == {"PMID:1", "DOI:10.2/y"}
        combined = merged["PMID:1"]
        assert combined["title"] == "A"  # first non-empty value wins
        assert combined["journal"] == "J"
        assert combined["year"] == 2004
        assert combined["pmcid"] == "PMC9"
        assert combined["doi"] == "10.1/x"
        assert combined["canonical_id"] == "PMID:1"
        assert combined["canonical_ids"] == ["PMID:1", "PMC:PMC9", "DOI:10.1/x"]
        assert aliases == {
            "PMID:1": "PMID:1",
            "PMC:PMC9": "PMID:1",
            "DOI:10.1/x": "PMID:1",
            "DOI:10.2/y": "DOI:10.2/y",
        }
        assert unkeyed == [{"title": "no ids at all"}]

    def test_unkeyed_records_are_never_given_an_arbitrary_key(self):
        result = merge_ids([{"title": "x"}, {"pmid": None, "doi": ""}])
        assert result.merged == {}
        assert result.aliases == {}
        assert result.unkeyed == [{"title": "x"}, {"pmid": None, "doi": ""}]

    def test_unrelated_records_stay_separate(self):
        merged, aliases, unkeyed = merge_ids(
            [{"pmid": "1"}, {"pmid": "2"}, {"doi": "10.1/z"}]
        )
        assert set(merged) == {"PMID:1", "PMID:2", "DOI:10.1/z"}
        assert len(aliases) == 3
        assert unkeyed == []

    def test_search_hits_can_be_merged_directly(self, tmp_path):
        client, _, _ = make_client(
            tmp_path, FakeResponse(payload=load_fixture("get_batch.json"))
        )
        hits = client.search("q", page_size=25, max_results=25)
        merged = merge_ids(hits).merged
        assert set(merged) == {"PMID:15210978", "PMC:PMC2828762"}


# ── cache helper ─────────────────────────────────────────────────────────


class TestLiteratureCache:
    """The shared SQLite cache: round-trip, defaults, concurrency."""

    def test_round_trip(self, tmp_path):
        cache = LiteratureCache(tmp_path / "c.sqlite")
        cache.put("http_cache", "k", {"a": [1, 2], "b": None})
        assert cache.get("http_cache", "k") == {"a": [1, 2], "b": None}

    def test_missing_key_and_missing_table_return_none(self, tmp_path):
        cache = LiteratureCache(tmp_path / "c.sqlite")
        assert cache.get("http_cache", "absent") is None
        assert cache.get("never_used_table", "absent") is None

    def test_put_replaces_an_existing_value(self, tmp_path):
        cache = LiteratureCache(tmp_path / "c.sqlite")
        cache.put("http_cache", "k", {"v": 1})
        cache.put("http_cache", "k", {"v": 2})
        assert cache.get("http_cache", "k") == {"v": 2}
        conn = sqlite3.connect(str(tmp_path / "c.sqlite"))
        try:
            count = conn.execute("SELECT COUNT(*) FROM http_cache").fetchone()[0]
        finally:
            conn.close()
        assert count == 1

    def test_tables_are_created_on_first_use(self, tmp_path):
        cache = LiteratureCache(tmp_path / "c.sqlite")
        cache.put("other_table", "k", 1)
        conn = sqlite3.connect(str(tmp_path / "c.sqlite"))
        try:
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            conn.close()
        assert "other_table" in names

    def test_bad_table_name_is_rejected(self, tmp_path):
        cache = LiteratureCache(tmp_path / "c.sqlite")
        with pytest.raises(ValueError):
            cache.put('http"; DROP TABLE x; --', "k", 1)

    def test_default_path_comes_from_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert default_cache_path() == tmp_path / ".kbutillib" / "gene_literature.sqlite"
        assert resolve_cache_path(None) == default_cache_path()
        assert resolve_cache_path("/tmp/x.sqlite") == Path("/tmp/x.sqlite")

    def test_cache_is_safe_from_concurrent_threads(self, tmp_path):
        cache = LiteratureCache(tmp_path / "c.sqlite")
        errors = []

        def worker(index):
            try:
                for step in range(20):
                    key = f"k{index}-{step}"
                    cache.put("http_cache", key, {"index": index, "step": step})
                    assert cache.get("http_cache", key) == {
                        "index": index,
                        "step": step,
                    }
            except Exception as exc:  # pragma: no cover - surfaced via errors
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        assert errors == []
        conn = sqlite3.connect(str(tmp_path / "c.sqlite"))
        try:
            count = conn.execute("SELECT COUNT(*) FROM http_cache").fetchone()[0]
        finally:
            conn.close()
        assert count == 8 * 20


# ── composition wrapper ──────────────────────────────────────────────────


class TestEuropePMCClientImpl:
    """The Impl wrapper holds an env and delegates via __getattr__."""

    def test_delegates_and_carries_config(self, tmp_path):
        env = shared_env_utils.SharedEnvUtils(
            config={"gene_literature": {"europepmc_rps": 4}},
            config_file=False,
            token_file=None,
            kbase_token_file=None,
        )
        session = FakeSession(FakeResponse(payload=load_fixture("search_empty.json")))
        impl = EuropePMCClientImpl(
            env,
            session=session,
            cache=LiteratureCache(tmp_path / "c.sqlite"),
            clock=FakeClock().now,
            sleep=FakeClock().sleep,
        )

        assert impl.env is env
        assert impl.min_interval == pytest.approx(0.25)
        assert impl.search("q", page_size=25, max_results=25) == []
        assert session.call_count == 1


# ── default wiring (no injected seams) ───────────────────────────────────


class TestDefaultWiring:
    """The no-injection check.

    Builds EuropePMCClient with no seams at all -- real requests.Session,
    real clock and sleep, cache path resolved from config under a relocated
    HOME -- and intercepts only at the transport layer (the requests
    HTTPAdapter), so what is under test is the default wiring itself.
    """

    def test_default_wiring_sends_documented_request_and_writes_cache(
        self, tmp_path, monkeypatch
    ):
        home = tmp_path / "home"
        (home / ".kbutillib").mkdir(parents=True)
        cache_path = home / ".kbutillib" / "gene_literature.sqlite"
        config_path = home / ".kbutillib" / "config.yaml"
        config_path.write_text(
            "gene_literature:\n"
            f"  cache_path: {cache_path}\n"
            # A high rate keeps the real throttle's real sleep negligible.
            "  europepmc_rps: 50\n"
        )
        monkeypatch.setenv("HOME", str(home))
        # SharedEnvUtils binds the user-config location at import time, so the
        # relocated HOME has to be pointed at explicitly.
        monkeypatch.setattr(shared_env_utils, "DEFAULT_CONFIG_FILE", config_path)

        payload = load_fixture("search_dnaA_page1.json")
        captured = []

        def fake_send(adapter, request, **kwargs):
            captured.append(request)
            response = requests.Response()
            response.status_code = 200
            response.encoding = "utf-8"
            response.headers["Content-Type"] = "application/json"
            response._content = json.dumps(payload).encode("utf-8")
            response.url = request.url
            response.request = request
            return response

        monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", fake_send)

        # No seams: no session, no clock, no sleep, no cache, no config dict.
        # token_file/kbase_token_file are not seams of this client; they are
        # silenced only to keep the real HOME's token files out of the test.
        client = EuropePMCClient(token_file=None, kbase_token_file=None)

        hits = client.search('"dnaA" AND ("ADP1")', page_size=2, max_results=2)

        assert len(hits) == 2
        assert isinstance(client.session, requests.Session)
        assert client.cache.path == cache_path

        # The documented URL and parameters, as actually prepared by requests.
        assert len(captured) == 1
        parsed = urlparse(captured[0].url)
        assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == EUROPEPMC_SEARCH_URL
        assert parse_qs(parsed.query) == {
            "query": ['"dnaA" AND ("ADP1")'],
            "format": ["json"],
            "resultType": ["core"],
            "pageSize": ["2"],
            "cursorMark": ["*"],
        }

        # ...and the cache row on disk, in the documented table.
        assert cache_path.exists()
        conn = sqlite3.connect(str(cache_path))
        try:
            rows = conn.execute(
                f'SELECT key, value FROM "{HTTP_CACHE_TABLE}"'
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 1
        key, value = rows[0]
        assert key.startswith(f"{EUROPEPMC_SEARCH_URL}?")
        assert "format=json" in key
        assert "resultType=core" in key
        assert json.loads(value)["hitCount"] == payload["hitCount"]
