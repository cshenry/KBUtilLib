"""``seat_state`` follows WHY a listing was empty, and aliases date stamps.

Background: Anthropic's live ``/v1/models`` lists Haiku 4.5 ONLY as
``claude-haiku-4-5-20251001``; the dateless alias bills fine but was reported
``unavailable`` because readiness tested literal membership.  And a 401, a 404
or a 5xx on the listing is not evidence that the model is not served.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import pytest

from kbutillib.domains.ai.council.servable import (
    probe_endpoint,
    seat_state,
    served_name_matches,
)


class _Seat:
    seat_name = "proposer"
    endpoint = "main"
    endpoint_kind = "anthropic"
    verified = True
    canonical_model = "anthropic/claude-haiku-4-5"

    def __init__(self, model: str = "claude-haiku-4-5") -> None:
        self.model = model


class _Reporting:
    kind = "anthropic"
    base_url = "test://listing"

    def __init__(self, outcome: str, models: List[str], detail: str = "d") -> None:
        self._report = {"models": [{"id": m} for m in models], "outcome": outcome,
                        "detail": detail}
        self.models_calls = 0

    async def models(self) -> List[Dict[str, Any]]:
        self.models_calls += 1
        return self._report["models"]

    async def models_report(self) -> Dict[str, Any]:
        return self._report


class _ModelsOnly:
    kind = "anthropic"
    base_url = "test://modelsonly"

    def __init__(self, models: List[str]) -> None:
        self._models = models

    async def models(self) -> List[Dict[str, Any]]:
        return [{"id": m} for m in self._models]


def _state(api: Any, model: str = "claude-haiku-4-5", allow_unverified: bool = False) -> Dict[str, Any]:
    probe = asyncio.run(probe_endpoint("main", "anthropic", api))
    return seat_state("proposer", _Seat(model), probe, allow_unverified)


@pytest.mark.parametrize(
    "outcome, models, verdict",
    [
        ("answered", ["claude-haiku-4-5-20251001"], "ready"),
        ("answered", ["claude-haiku-4-5"], "ready"),
        ("answered", ["claude-opus-5"], "unavailable"),
        ("answered", [], "unavailable"),
        ("unreachable", [], "unavailable"),
        ("not_implemented", [], "unknown"),
        ("credential_rejected", [], "unknown"),
        ("http_error", [], "unknown"),
        ("malformed", [], "unknown"),
    ],
)
def test_verdict_follows_the_outcome(outcome: str, models: List[str], verdict: str) -> None:
    report = _state(_Reporting(outcome, models, detail="HTTP 401 from x"))
    assert report["state"] == verdict, report["detail"]
    if outcome != "answered" or not models:
        assert "listed no models" in report["detail"]
    if outcome not in ("answered",):
        assert outcome in report["detail"]


def test_probe_carries_outcome_and_detail() -> None:
    probe = asyncio.run(probe_endpoint("main", "anthropic", _Reporting("http_error", [], "HTTP 500")))
    assert probe["listing_outcome"] == "http_error"
    assert probe["listing_detail"] == "HTTP 500"


def test_an_api_type_without_models_report_is_answered() -> None:
    api = _ModelsOnly([])
    probe = asyncio.run(probe_endpoint("main", "anthropic", api))
    assert probe["listing_outcome"] == "answered"
    assert _state(api)["state"] == "unavailable"
    assert _state(_ModelsOnly(["claude-haiku-4-5-20251001"]))["state"] == "ready"


def test_unknown_is_admitted_by_allow_unverified_semantics() -> None:
    # 'unknown' is the state the existing submit gate already treats as
    # needing allow_unverified; no new state was introduced.
    from kbutillib.domains.ai.council.servable import SEAT_STATES

    assert _state(_Reporting("credential_rejected", []))["state"] in SEAT_STATES


@pytest.mark.parametrize(
    "configured, served, expected",
    [
        ("claude-haiku-4-5", ["claude-haiku-4-5-20251001"], True),
        ("claude-haiku-4-5-20251001", ["claude-haiku-4-5"], True),
        ("Claude-Haiku-4-5", ["claude-haiku-4-5-20251001"], True),
        ("claude-haiku-4-5", ["claude-haiku-4-5"], True),
        ("claude-opus-4-5", ["claude-opus-4-5-1"], False),
        ("claude-opus-4-5-1", ["claude-opus-4-5"], False),
        ("claude-haiku-4-5", ["claude-haiku-4-5-2025"], False),
        ("claude-haiku-4-5-20251001", ["claude-haiku-4-5-20250101"], False),
        ("claude-haiku", ["claude-haiku-4-5"], False),
    ],
)
def test_alias_resolution(configured: str, served: List[str], expected: bool) -> None:
    assert served_name_matches(configured, served) is expected


def test_opus_4_5_is_not_seated_by_opus_4_5_1_end_to_end() -> None:
    report = _state(_Reporting("answered", ["claude-opus-4-5-1"]), model="claude-opus-4-5")
    assert report["state"] == "unavailable"


@pytest.mark.parametrize("status, verdict", [(401, "unknown"), (404, "unknown"),
                                             (500, "unknown")])
def test_a_real_http_backend_with_a_failing_listing(status: int, verdict: str) -> None:
    """Through a real HttpBackend: the Haiku 4.5 lesson, a failed listing is not 'unavailable'."""
    import httpx

    from kbutillib.domains.ai.llm.apis.http import OpenAICompatibleBackend

    backend = OpenAICompatibleBackend(
        {"name": "t", "kind": "openai", "base_url": "http://127.0.0.1:1/v1"},
        transport=httpx.MockTransport(lambda r: httpx.Response(status, json={})),
    )
    try:
        report = _state(backend)
    finally:
        asyncio.run(backend.aclose())
    assert report["state"] == verdict


def test_a_real_http_backend_that_refuses_the_connection_is_unavailable() -> None:
    import httpx

    from kbutillib.domains.ai.llm.apis.http import OpenAICompatibleBackend

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    backend = OpenAICompatibleBackend(
        {"name": "t", "kind": "openai", "base_url": "http://127.0.0.1:1/v1"},
        transport=httpx.MockTransport(refuse),
    )
    try:
        report = _state(backend)
    finally:
        asyncio.run(backend.aclose())
    assert report["state"] == "unavailable"
