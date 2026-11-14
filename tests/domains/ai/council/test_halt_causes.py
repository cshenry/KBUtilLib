"""The halt CAUSE vocabulary, its classifier, and the cause's whole journey.

``halt_kind`` cannot say whether a halt may be resumed: both real council
halts on record were ``kind='auth'``, one of which cleared itself in 4h10m
and one of which was a permanent administrative state.  ``halt_cause`` is the
code that tells them apart, and these tests follow it end to end --
classified from the provider's structured error, carried on the
``CouncilError``, written to ``endpoint_state.halt_cause`` by the worker's
own halt path, and reported in ``_Supervisor.foreign_halts()``, which is the
record that reaches ``RUN.json`` and the campaign watchdog.

The two real cases in :data:`CASES` are driven through the SHIPPED request
path -- the real :class:`ArgoBackend` / :class:`OpenAICompatibleBackend`, their
own ``httpx.AsyncClient``, their own parsing -- with only the socket replaced
by :class:`httpx.MockTransport`, and then through the real
:class:`CouncilWorker` into a real SQLite store.  Nothing in the product is
monkeypatched, so a test passing here is evidence about the shipped code.

No test here makes a network call, spends a token, needs a CLI login or needs
a GPU.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Mapping

import httpx
import pytest

from kbutillib.domains.ai.council.backends.http import (
    ARGO_USER_ENV,
    ArgoBackend,
    OpenAICompatibleBackend,
)
from kbutillib.domains.ai.council.engine import CouncilWorker
from kbutillib.domains.ai.council.halt_causes import (
    ADMINISTRATIVELY_DISABLED,
    CREDENTIALS_REVOKED,
    HALT_CAUSES,
    INSUFFICIENT_SCOPE,
    NETWORK_ERROR,
    QUOTA_EXHAUSTED,
    RESUMABLE_HALT_CAUSES,
    TOKEN_EXPIRED,
    UNCLASSIFIED_AUTH_DENIAL,
    UPSTREAM_5XX,
    classify_http_halt_cause,
)
from kbutillib.domains.genome.ontology_mapping.council_run import _Supervisor

from .engine_support import (
    InstrumentedBackend,
    critic_answer,
    endpoint_config,
    fake_seat,
    make_query,
    make_roster,
    make_task,
    proposer_answer,
    run,
    scripted,
)

#: The two real halts, with the provider response each was built from.  Copied
#: out of PersistentAI's captured ``RUN.json`` fixtures; see the file's
#: ``_provenance``.
CASES: Dict[str, Any] = json.loads(
    (Path(__file__).parent / "fixtures" / "real_halt_cases.json").read_text()
)

MAG_BASE = "https://mag.genesis.americansciencecloud.org/v1"

#: Worker timings that keep a halt test to the one sweep it needs.
WORKER_FAST: Dict[str, Any] = {
    "retry_backoff_s": (0.0,),
    "retry_jitter": 0.0,
    "poll_interval_s": 0.0,
    "idle_grace_s": 0.0,
    "max_idle_s": 2.0,
    "heartbeat_interval_s": 0.0,
}


# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------


class TestTheVocabularyIsClosed:
    """Eight codes, four of them resumable, and no third set."""

    def test_the_eight_members_are_exactly_these(self) -> None:
        assert set(HALT_CAUSES) == {
            "token_expired",
            "unclassified_auth_denial",
            "upstream_5xx",
            "network_error",
            "credentials_revoked",
            "insufficient_scope",
            "quota_exhausted",
            "administratively_disabled",
        }
        assert len(HALT_CAUSES) == 8, "a duplicate would make the tuple lie"

    def test_resumable_is_a_subset_with_the_exact_four_members(self) -> None:
        assert set(RESUMABLE_HALT_CAUSES) <= set(HALT_CAUSES)
        assert set(RESUMABLE_HALT_CAUSES) == {
            "token_expired",
            "unclassified_auth_denial",
            "upstream_5xx",
            "network_error",
        }
        assert len(RESUMABLE_HALT_CAUSES) == 4

    def test_the_other_four_are_terminal(self) -> None:
        """The split is the whole product: a terminal code must never resume."""
        assert set(HALT_CAUSES) - set(RESUMABLE_HALT_CAUSES) == {
            "credentials_revoked",
            "insufficient_scope",
            "quota_exhausted",
            "administratively_disabled",
        }

    def test_the_council_package_exports_both_names(self) -> None:
        """What PersistentAI's watchdog imports."""
        from kbutillib.domains.ai import council

        assert council.HALT_CAUSES == HALT_CAUSES
        assert council.RESUMABLE_HALT_CAUSES == RESUMABLE_HALT_CAUSES


# ---------------------------------------------------------------------------
# The classifier, one case per vocabulary member with a structured trigger
# ---------------------------------------------------------------------------


class TestClassifyFromStructuredSignals:
    """Status plus the provider's ``error`` object, and nothing else."""

    def test_an_amsc_disabled_project_is_administratively_disabled(self) -> None:
        case = CASES["mag_project_disabled"]
        assert classify_http_halt_cause(case["status"], case["body"]) == (
            ADMINISTRATIVELY_DISABLED
        )

    def test_the_amsc_reason_field_alone_is_enough(self) -> None:
        """A gateway that stops sending ``type`` still classifies."""
        body = {
            "error": {
                "code": "403",
                "provider_specific_fields": {"amsc_reason": "project_disabled"},
            }
        }
        assert classify_http_halt_cause(403, body) == ADMINISTRATIVELY_DISABLED

    def test_invalid_grant_is_credentials_revoked(self) -> None:
        body = {"error": {"type": "invalid_grant", "message": "refresh token revoked"}}
        assert classify_http_halt_cause(401, body) == CREDENTIALS_REVOKED

    def test_insufficient_scope_is_insufficient_scope(self) -> None:
        body = {"error": {"code": "insufficient_scope"}}
        assert classify_http_halt_cause(403, body) == INSUFFICIENT_SCOPE

    def test_insufficient_quota_is_quota_exhausted(self) -> None:
        body = {"error": {"type": "insufficient_quota"}}
        assert classify_http_halt_cause(429, body) == QUOTA_EXHAUSTED

    def test_invalid_token_on_a_401_is_token_expired(self) -> None:
        """RFC 6750: ``invalid_token`` means refresh and retry once."""
        body = {"error": {"code": "invalid_token"}}
        assert classify_http_halt_cause(401, body) == TOKEN_EXPIRED

    def test_an_explicit_expired_code_is_token_expired(self) -> None:
        body = {"error": {"type": "expired_token"}}
        assert classify_http_halt_cause(401, body) == TOKEN_EXPIRED

    def test_a_bare_401_or_403_is_an_unclassified_auth_denial(self) -> None:
        assert classify_http_halt_cause(401, None) == UNCLASSIFIED_AUTH_DENIAL
        assert classify_http_halt_cause(403, {}) == UNCLASSIFIED_AUTH_DENIAL

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_every_5xx_is_upstream(self, status: int) -> None:
        assert classify_http_halt_cause(status, None) == UPSTREAM_5XX

    def test_an_expired_signal_without_a_401_is_not_token_expired(self) -> None:
        """A 403 saying "expired" is not a token to refresh."""
        assert classify_http_halt_cause(403, {"error": {"type": "expired"}}) == (
            UNCLASSIFIED_AUTH_DENIAL
        )

    @pytest.mark.parametrize("status", [400, 404, 409, 422, 429])
    def test_anything_else_is_unclassified(self, status: int) -> None:
        """Never guess: an unrecognised failure must read as not resumable."""
        assert classify_http_halt_cause(status, {"error": {"type": "nonsense"}}) is None

    def test_a_windowed_rate_limit_gets_no_cause(self) -> None:
        """A 429 is retried, not halted; neither set is honest about it."""
        assert classify_http_halt_cause(429, {"error": {"type": "rate_limit"}}) is None

    def test_an_unparseable_body_contributes_no_signal(self) -> None:
        """No signal is not the same as a denial."""
        assert classify_http_halt_cause(502, "<html>502 Bad Gateway</html>") == (
            UPSTREAM_5XX
        )
        assert classify_http_halt_cause(400, "<html>nope</html>") is None

    def test_every_cause_it_can_return_is_in_the_vocabulary(self) -> None:
        seen = {
            classify_http_halt_cause(status, body)
            for status, body in (
                (403, CASES["mag_project_disabled"]["body"]),
                (401, {"error": {"type": "invalid_grant"}}),
                (403, {"error": {"code": "insufficient_scope"}}),
                (429, {"error": {"type": "insufficient_quota"}}),
                (401, {"error": {"code": "invalid_token"}}),
                (401, None),
                (503, None),
            )
        }
        assert seen <= set(HALT_CAUSES)


# ---------------------------------------------------------------------------
# The real request path: a mocked HTTP response through the shipped backend
# ---------------------------------------------------------------------------


def mag_backend(status: int, body: Any) -> OpenAICompatibleBackend:
    """The real MAG backend, answering *status* with *body* and no socket."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    return OpenAICompatibleBackend(
        {"name": "mag", "kind": "openai", "base_url": MAG_BASE},
        transport=httpx.MockTransport(handler),
    )


def argo_backend(text: str) -> ArgoBackend:
    """The real ARGO backend, answering HTTP 200 with *text* and no socket."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=text)

    return ArgoBackend(
        {"name": "argo", "kind": "argo"}, transport=httpx.MockTransport(handler)
    )


class TestTheShippedBackendCarriesTheCause:
    """The cause is decided where the response is, and rides on the error."""

    def test_the_real_mag_403_classifies_administratively_disabled(self) -> None:
        case = CASES["mag_project_disabled"]
        backend = mag_backend(case["status"], case["body"])
        try:
            result = run(backend.complete(_request()))
        finally:
            run(backend.aclose())
        assert result.ok is False
        assert result.error.kind == case["expected_halt_kind"]
        assert result.error.halt_cause == case["expected_halt_cause"]
        assert result.error.halts_endpoint is True

    def test_the_real_argo_notice_classifies_unclassified_auth_denial(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        case = CASES["argo_authentication_notice"]
        monkeypatch.setenv(ARGO_USER_ENV, "chenry")
        backend = argo_backend(case["body_text"])
        try:
            result = run(backend.complete(_request()))
        finally:
            run(backend.aclose())
        assert result.ok is False
        assert result.error.kind == case["expected_halt_kind"]
        assert result.error.halt_cause == case["expected_halt_cause"]

    def test_a_500_carries_upstream_5xx(self) -> None:
        backend = mag_backend(500, {"error": {"message": "upstream exploded"}})
        try:
            result = run(backend.complete(_request()))
        finally:
            run(backend.aclose())
        assert result.error.halt_cause == UPSTREAM_5XX

    def test_a_refused_connection_carries_network_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exception class is the only structured signal there is."""
        monkeypatch.setenv(ARGO_USER_ENV, "chenry")

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("nothing listening", request=request)

        backend = ArgoBackend(
            {"name": "argo", "kind": "argo"}, transport=httpx.MockTransport(handler)
        )
        try:
            result = run(backend.complete(_request()))
        finally:
            run(backend.aclose())
        assert result.error.halt_cause == NETWORK_ERROR

    def test_a_timeout_carries_network_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow", request=request)

        backend = OpenAICompatibleBackend(
            {"name": "mag", "kind": "openai", "base_url": MAG_BASE},
            transport=httpx.MockTransport(handler),
        )
        try:
            result = run(backend.complete(_request()))
        finally:
            run(backend.aclose())
        assert result.error.kind == "timeout"
        assert result.error.halt_cause == NETWORK_ERROR

    def test_a_bad_request_carries_no_cause(self) -> None:
        """A 400 is the request being wrong; it halts nothing and guesses nothing."""
        backend = mag_backend(400, {"error": {"message": "unknown parameter"}})
        try:
            result = run(backend.complete(_request()))
        finally:
            run(backend.aclose())
        assert result.error.halt_cause is None


def _request() -> Any:
    """One prompt-bearing request, in the shape a seat sends."""
    from kbutillib.domains.ai.llm.types import LLMRequest

    return LLMRequest(
        model="gpt-oss-120b", system="s", user="u", max_output_tokens=64, timeout_s=5
    )


# ---------------------------------------------------------------------------
# The whole journey: response -> worker -> store -> foreign_halts()
# ---------------------------------------------------------------------------


def worker_over(store: Any, backends: Mapping[str, Any]) -> CouncilWorker:
    """A worker over *backends*, with test-fast timings."""
    config = endpoint_config(**{name: {} for name in backends})
    return CouncilWorker(store, config, dict(backends), "alice", "w-1", **WORKER_FAST)


def roster_with_one_seat_elsewhere(endpoint: str) -> Any:
    """Three proposers, the first of them on *endpoint* and the rest on ``fake1``."""
    roster = make_roster(3)
    roster.proposers[0] = fake_seat("proposer_1", "p1", endpoint=endpoint)
    return roster


def supervisor_over(store: Any, endpoint: str, run_dir: Path) -> _Supervisor:
    """The run supervisor whose ``foreign_halts()`` reaches ``RUN.json``."""
    return _Supervisor(
        store,
        ceiling_tokens={"amsc": 10_000_000},
        endpoints_by_gateway={"amsc": [endpoint, "fake1"]},
        run_dir=run_dir,
    )


def halt_through_the_worker(store: Any, endpoint: str, backend: Any) -> Any:
    """Run one job whose first proposer hits *backend*; return the job id."""
    answers = {"proposer": proposer_answer("A:1"), "critic": critic_answer()}
    worker = worker_over(
        store, {endpoint: backend, "fake1": InstrumentedBackend(scripted(answers))}
    )
    job_id = worker.submit(make_query(), [make_task()], roster_with_one_seat_elsewhere(endpoint))
    run(worker.work(job_id=job_id, until_idle=True))
    return job_id


class TestTheCauseReachesTheStoreAndTheRunRecord:
    """The two real halts, end to end through the shipped worker."""

    def test_the_mag_403_lands_as_administratively_disabled(
        self, store: Any, tmp_path: Path
    ) -> None:
        case = CASES["mag_project_disabled"]
        backend = mag_backend(case["status"], case["body"])
        try:
            halt_through_the_worker(store, "mag", backend)
        finally:
            run(backend.aclose())

        state = store.get_endpoint_state("mag")
        assert state["halt_kind"] == "auth", "the kind is unchanged by this work"
        assert state["halt_cause"] == ADMINISTRATIVELY_DISABLED
        assert state["halt_cause"] not in RESUMABLE_HALT_CAUSES

        halts = supervisor_over(store, "mag", tmp_path).foreign_halts()
        assert [row["endpoint"] for row in halts] == ["mag"]
        assert halts[0]["halt_kind"] == "auth"
        assert halts[0]["halt_cause"] == ADMINISTRATIVELY_DISABLED

    def test_the_argo_notice_lands_as_an_unclassified_auth_denial(
        self, store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        case = CASES["argo_authentication_notice"]
        monkeypatch.setenv(ARGO_USER_ENV, "chenry")
        backend = argo_backend(case["body_text"])
        try:
            halt_through_the_worker(store, "argo", backend)
        finally:
            run(backend.aclose())

        state = store.get_endpoint_state("argo")
        assert state["halt_kind"] == "auth"
        assert state["halt_cause"] == UNCLASSIFIED_AUTH_DENIAL
        assert state["halt_cause"] in RESUMABLE_HALT_CAUSES, (
            "this one really did clear itself after 4h10m"
        )

        halts = supervisor_over(store, "argo", tmp_path).foreign_halts()
        assert halts[0]["halt_cause"] == UNCLASSIFIED_AUTH_DENIAL

    def test_the_two_real_halts_are_told_apart_by_cause_alone(self) -> None:
        """The defect this field exists to fix, stated as one assertion."""
        mag_case = CASES["mag_project_disabled"]
        argo_case = CASES["argo_authentication_notice"]
        assert mag_case["expected_halt_kind"] == argo_case["expected_halt_kind"]
        assert mag_case["expected_halt_cause"] != argo_case["expected_halt_cause"]
        assert mag_case["expected_halt_cause"] not in RESUMABLE_HALT_CAUSES
        assert argo_case["expected_halt_cause"] in RESUMABLE_HALT_CAUSES

    def test_a_budget_halt_keeps_no_cause(self, store: Any, tmp_path: Path) -> None:
        """A budget halt is not a gateway failure; None reads as not resumable."""
        store.set_halt("fake1", "budget_exhausted", "spent 11 of a 10-token budget")
        state = store.get_endpoint_state("fake1")
        assert state["halt_kind"] == "budget_exhausted"
        assert state["halt_cause"] is None

        halts = supervisor_over(store, "mag", tmp_path).foreign_halts()
        assert [row["endpoint"] for row in halts] == ["fake1"]
        assert "halt_cause" in halts[0], "the key must be present even when null"
        assert halts[0]["halt_cause"] is None


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


class TestTheStoreKeepsAndClearsTheCause:
    """``set_halt`` records it, ``reset_halt`` clears it with the kind."""

    def test_set_halt_records_the_cause(self, store: Any) -> None:
        store.set_halt("argo", "auth", "the token expired", cause=TOKEN_EXPIRED)
        assert store.get_endpoint_state("argo")["halt_cause"] == TOKEN_EXPIRED

    def test_set_halt_without_a_cause_leaves_it_null(self, store: Any) -> None:
        store.set_halt("argo", "auth", "no idea why")
        assert store.get_endpoint_state("argo")["halt_cause"] is None

    def test_reset_halt_clears_the_cause_with_the_kind(self, store: Any) -> None:
        store.set_halt("argo", "auth", "expired key", cause=TOKEN_EXPIRED)
        store.reset_halt("argo")
        state = store.get_endpoint_state("argo")
        assert state["halt_kind"] is None
        assert state["halt_message"] is None
        assert state["halt_cause"] is None

    def test_a_second_halt_with_no_cause_does_not_inherit_the_first(
        self, store: Any
    ) -> None:
        """A stale cause on a new kind would be worse than no cause at all."""
        store.set_halt("argo", "auth", "expired key", cause=TOKEN_EXPIRED)
        store.set_halt("argo", "budget_exhausted", "ceiling reached")
        assert store.get_endpoint_state("argo")["halt_cause"] is None

    def test_reset_budget_clears_a_budget_halt_including_its_cause(
        self, store: Any
    ) -> None:
        store.set_halt("argo", "budget_exhausted", "ceiling reached")
        store.reset_budget("argo")
        state = store.get_endpoint_state("argo")
        assert state["halt_kind"] is None
        assert state["halt_cause"] is None

    def test_reset_budget_leaves_an_auth_halt_and_its_cause_alone(
        self, store: Any
    ) -> None:
        store.set_halt("argo", "auth", "expired key", cause=TOKEN_EXPIRED)
        store.reset_budget("argo")
        state = store.get_endpoint_state("argo")
        assert state["halt_kind"] == "auth"
        assert state["halt_cause"] == TOKEN_EXPIRED


class TestAnExistingStoreGainsTheColumnOnOpen:
    """No ``kbu council db migrate`` is needed for an additive column."""

    @staticmethod
    def _without_the_column(url: str) -> Path:
        """Drop ``endpoint_state.halt_cause``, i.e. rewind to the older build."""
        path = Path(url.split("///", 1)[1])
        with sqlite3.connect(path) as conn:
            conn.execute("ALTER TABLE endpoint_state DROP COLUMN halt_cause")
        columns = _columns(path, "endpoint_state")
        assert "halt_cause" not in columns, "the fixture did not rewind"
        return path

    def test_a_store_created_without_the_column_gains_it_on_open(
        self, make_store: Any, store_url: Any
    ) -> None:
        first = make_store("old")
        first.set_halt("argo", "auth", "written before the column existed")
        first.close()
        path = self._without_the_column(store_url("old"))

        # Opening it is all it takes -- NOT migrate(), NOT init_schema().
        reopened = make_store("old", init=False)
        assert "halt_cause" in _columns(path, "endpoint_state")

        # And the pre-existing halt is intact, with a null cause.
        state = reopened.get_endpoint_state("argo")
        assert state["halt_kind"] == "auth"
        assert state["halt_cause"] is None

    def test_the_reopened_store_can_be_halted_with_a_cause(
        self, make_store: Any, store_url: Any
    ) -> None:
        first = make_store("old")
        first.close()
        self._without_the_column(store_url("old"))
        reopened = make_store("old", init=False)
        reopened.set_halt("mag", "auth", "disabled", cause=ADMINISTRATIVELY_DISABLED)
        assert reopened.get_endpoint_state("mag")["halt_cause"] == (
            ADMINISTRATIVELY_DISABLED
        )

    def test_the_schema_version_is_not_bumped_by_the_new_column(
        self, make_store: Any
    ) -> None:
        from kbutillib.domains.ai.council.store import schema as sch

        assert sch.SCHEMA_VERSION == 2
        assert ("endpoint_state", "halt_cause") in sch.ADDITIVE_COLUMNS
        assert make_store("fresh").schema_version() == 2


def _columns(path: Path, table: str) -> set:
    with sqlite3.connect(path) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
