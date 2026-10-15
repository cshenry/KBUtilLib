"""The optional client for Filipe Liu's deployed GO council on poplar:8300.

Every test here drives the REAL request path -- the client's own
``httpx.Client``, its own body construction, its own error mapping -- and only
replaces the socket, with :class:`httpx.MockTransport`.  Nothing in the client
is monkeypatched, so a test passing is evidence about the shipped code rather
than about a stub.

The bodies the client builds are validated against
``fixtures/concordia_openapi.json``, which is the **live** OpenAPI document of
the deployed service, captured with a free ``GET`` (see
``concordia_openapi.meta.json`` for when).  The deployed schema has diverged
from Filipe's readable checkout before, so the capture is the contract.
Validation is by :mod:`jsonschema` when it is installed and by a required-field
check when it is not; either way it also rejects a key that is not a declared
property of the request schema, which the schema itself -- FastAPI emits no
``additionalProperties: false`` -- would happily accept.

:class:`TestNoInjection` deliberately builds the real client with no mock
transport at all, against a ``127.0.0.1`` port that was bound and then closed:
any request actually sent there surfaces as a connection error rather than as
the missing-key refusal the test asserts.

No test makes a network call, spends a token, needs an API key or needs a GPU.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
import pytest

from kbutillib.domains.ai.council.service_client import (
    COUNCIL_MAX_ITEMS,
    DEFAULT_API_KEY_ENV,
    DEFAULT_BASE_URL,
    ConcordiaServiceClient,
    CouncilServiceAuthError,
    CouncilServiceUnavailable,
)

FIXTURES = Path(__file__).parent / "fixtures"

#: The captured live contract.  Loaded once: it is 54 KB of JSON.
OPENAPI: Dict[str, Any] = json.loads((FIXTURES / "concordia_openapi.json").read_text())
SCHEMAS: Dict[str, Any] = OPENAPI["components"]["schemas"]

#: The captured ``GET /concordia_go_council`` payload, used as the canned reply
#: of :meth:`ConcordiaServiceClient.status` so the assertions are about a real
#: response shape rather than an invented one.
STATUS: Dict[str, Any] = json.loads((FIXTURES / "concordia_status.json").read_text())

try:  # jsonschema is not a declared dependency of any extra; validate if present
    from jsonschema import Draft202012Validator as _Validator
except Exception:  # pragma: no cover - exercised only where jsonschema is absent
    _Validator = None  # type: ignore[assignment]

ITEM = {"id": "row1", "text": "Bifunctional EC 1.5.1.5, EC 3.5.4.9", "source": "RAST_annotation"}

#: One council row, in the captured ``CouncilRow`` shape, trimmed to the fields
#: ``row_to_comparison`` reads plus enough context to prove the row survives.
COUNCIL_ROW: Dict[str, Any] = {
    "id": "row1",
    "text": ITEM["text"],
    "source": ITEM["source"],
    "gene_id": None,
    "decided_by": "council",
    "status": "ok",
    "final_go_ids": [
        {"go_id": "GO:0004488", "name": "methylenetetrahydrofolate dehydrogenase (NADP+) activity"},
        {"go_id": "GO:0004477", "name": "methenyltetrahydrofolate cyclohydrolase activity"},
    ],
    "final_confidence": "high",
    "decline_reason": None,
    "triage_class": "free_text",
    "menu_supplied": False,
    "n_candidates": 2,
}


def schema_of(name: str) -> Dict[str, Any]:
    """The named request schema, rooted so its ``#/components`` refs resolve.

    ``CouncilRequest`` refers to ``#/components/schemas/CouncilItem``; carrying
    ``components`` into the schema document makes that pointer resolvable
    without a version-specific ref resolver or registry.
    """
    return {**SCHEMAS[name], "components": OPENAPI["components"]}


def validation_error(body: Dict[str, Any], schema_name: str) -> Optional[str]:
    """Why *body* is not a valid *schema_name*, or ``None`` if it is.

    Returns a message rather than raising so the negative test (which proves
    the check is not vacuous) reads the same whether :mod:`jsonschema` is
    installed or not.
    """
    schema = SCHEMAS[schema_name]
    problems: List[str] = []

    undeclared = sorted(set(body) - set(schema.get("properties", {})))
    if undeclared:
        problems.append(f"keys not declared by {schema_name}: {undeclared}")

    if _Validator is not None:
        problems += [
            f"{list(error.path)}: {error.message}"
            for error in _Validator(schema_of(schema_name)).iter_errors(body)
        ]
    else:  # required-field check: the part of the schema a client can get wrong
        for field in schema.get("required", []):
            if field not in body:
                problems.append(f"{schema_name} requires {field!r}")
        item_schema = SCHEMAS["CouncilItem" if schema_name == "CouncilRequest" else "MenuItem"]
        for index, item in enumerate(body.get("items", [])):
            for field in item_schema.get("required", []):
                if field not in item:
                    problems.append(f"items[{index}] requires {field!r}")

    return "; ".join(problems) or None


def assert_valid(body: Dict[str, Any], schema_name: str) -> None:
    """Assert *body* satisfies the captured live *schema_name*."""
    problem = validation_error(body, schema_name)
    assert problem is None, f"{schema_name}: {problem}"


def recorder(
    handler: Optional[Callable[[httpx.Request], httpx.Response]] = None,
    *,
    json_body: Any = None,
    status_code: int = 200,
    text: Optional[str] = None,
) -> Tuple[httpx.MockTransport, List[httpx.Request]]:
    """A mock transport plus the list of requests that actually reached it."""
    seen: List[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if handler is not None:
            return handler(request)
        if text is not None:
            return httpx.Response(status_code, text=text)
        return httpx.Response(status_code, json=json_body)

    return httpx.MockTransport(record), seen


def client(transport: httpx.MockTransport, **kwargs: Any) -> ConcordiaServiceClient:
    """A client on *transport*, with an explicit key unless told otherwise."""
    kwargs.setdefault("api_key", "test-key-not-a-real-one")
    return ConcordiaServiceClient(transport=transport, **kwargs)


def body_of(request: httpx.Request) -> Dict[str, Any]:
    """The JSON body the client built, as the transport received it."""
    return json.loads(request.content)


class TestEndpointRouting:
    """Each method makes exactly one request, to the documented method+path."""

    def test_health_gets_health(self) -> None:
        transport, seen = recorder(json_body={"ok": True, "ko_loaded": True, "ko_terms": 11421})
        reply = client(transport).health()
        assert [(r.method, r.url.path) for r in seen] == [("GET", "/health")]
        assert reply["ko_terms"] == 11421

    def test_models_gets_models(self) -> None:
        transport, seen = recorder(json_body={"paid": [], "free": []})
        reply = client(transport).models()
        assert [(r.method, r.url.path) for r in seen] == [("GET", "/models")]
        assert reply == {"paid": [], "free": []}

    def test_status_gets_the_council_endpoint(self) -> None:
        transport, seen = recorder(json_body=STATUS)
        reply = client(transport).status()
        assert [(r.method, r.url.path) for r in seen] == [("GET", "/concordia_go_council")]
        assert reply["available"] is True
        assert reply["auth"]["header"] == "X-API-Key"

    def test_menu_posts_to_the_menu_endpoint(self) -> None:
        transport, seen = recorder(json_body={"items": []})
        client(transport).menu([ITEM], cap=20, use_layer1=False)
        assert [(r.method, r.url.path) for r in seen] == [("POST", "/concordia/go/menu")]
        assert body_of(seen[0]) == {"items": [ITEM], "cap": 20, "use_layer1": False}

    def test_council_posts_to_the_council_endpoint(self) -> None:
        transport, seen = recorder(json_body={"items": [COUNCIL_ROW]})
        reply = client(transport).council([ITEM])
        assert [(r.method, r.url.path) for r in seen] == [("POST", "/concordia_go_council")]
        assert reply["items"][0]["id"] == "row1"

    def test_the_default_base_url_is_the_poplar_deployment(self) -> None:
        transport, seen = recorder(json_body={"ok": True})
        client(transport).health()
        assert str(seen[0].url) == f"{DEFAULT_BASE_URL}/health"

    def test_a_trailing_slash_does_not_double_up(self) -> None:
        transport, seen = recorder(json_body={"ok": True})
        client(transport, base_url=f"{DEFAULT_BASE_URL}/").health()
        assert str(seen[0].url) == f"{DEFAULT_BASE_URL}/health"


class TestAuthHeader:
    """Only the council spends money, so only the council authenticates."""

    def test_council_sends_the_api_key_header(self) -> None:
        transport, seen = recorder(json_body={"items": []})
        ConcordiaServiceClient(transport=transport, api_key="sekrit").council([ITEM])
        assert seen[0].headers["X-API-Key"] == "sekrit"

    def test_council_takes_the_key_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(DEFAULT_API_KEY_ENV, "from-the-env")
        transport, seen = recorder(json_body={"items": []})
        ConcordiaServiceClient(transport=transport).council([ITEM])
        assert seen[0].headers["X-API-Key"] == "from-the-env"

    def test_an_explicit_key_beats_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(DEFAULT_API_KEY_ENV, "from-the-env")
        transport, seen = recorder(json_body={"items": []})
        ConcordiaServiceClient(transport=transport, api_key="explicit").council([ITEM])
        assert seen[0].headers["X-API-Key"] == "explicit"

    def test_menu_sends_no_api_key_even_when_one_is_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The menu endpoint is open; the key must not leak onto it."""
        monkeypatch.setenv(DEFAULT_API_KEY_ENV, "from-the-env")
        transport, seen = recorder(json_body={"items": []})
        ConcordiaServiceClient(transport=transport, api_key="explicit").menu([ITEM])
        assert "x-api-key" not in {name.lower() for name in seen[0].headers}
        assert "explicit" not in seen[0].headers.get("authorization", "")

    def test_the_open_get_endpoints_send_no_api_key(self) -> None:
        transport, seen = recorder(json_body={"ok": True})
        service = ConcordiaServiceClient(transport=transport, api_key="explicit")
        service.health()
        service.models()
        service.status()
        for request in seen:
            assert "x-api-key" not in {name.lower() for name in request.headers}


class TestRefusalsBeforeTheWire:
    """Two things the client refuses itself, sending nothing."""

    def test_more_than_twenty_items_is_refused(self) -> None:
        transport, seen = recorder(json_body={"items": []})
        items = [{"id": f"row{n}", "text": f"annotation {n}"} for n in range(COUNCIL_MAX_ITEMS + 1)]
        with pytest.raises(ValueError, match=f"at most {COUNCIL_MAX_ITEMS} items"):
            client(transport).council(items)
        assert seen == []

    def test_exactly_twenty_items_is_sent(self) -> None:
        transport, seen = recorder(json_body={"items": []})
        items = [{"id": f"row{n}", "text": f"annotation {n}"} for n in range(COUNCIL_MAX_ITEMS)]
        client(transport).council(items)
        assert len(seen) == 1
        assert len(body_of(seen[0])["items"]) == COUNCIL_MAX_ITEMS

    def test_the_menu_endpoint_has_no_twenty_item_cap(self) -> None:
        """The captured ``MenuRequest`` allows 1000; only the council is capped."""
        transport, seen = recorder(json_body={"items": []})
        items = [{"id": f"row{n}", "text": f"annotation {n}"} for n in range(50)]
        client(transport).menu(items)
        assert len(body_of(seen[0])["items"]) == 50

    def test_a_missing_key_is_refused_without_a_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(DEFAULT_API_KEY_ENV, raising=False)
        transport, seen = recorder(json_body={"items": []})
        service = ConcordiaServiceClient(transport=transport)
        with pytest.raises(CouncilServiceAuthError, match="X-API-Key"):
            service.council([ITEM])
        assert seen == []

    def test_a_missing_key_does_not_stop_the_open_endpoints(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(DEFAULT_API_KEY_ENV, raising=False)
        transport, seen = recorder(json_body={"items": []})
        service = ConcordiaServiceClient(transport=transport)
        service.menu([ITEM])
        service.status()
        assert len(seen) == 2


class TestStatusMapping:
    """401, 503 and 422 are the three the endpoint documents."""

    def test_401_is_an_auth_error(self) -> None:
        transport, _ = recorder(status_code=401, json_body={"detail": "invalid API key"})
        with pytest.raises(CouncilServiceAuthError, match="401"):
            client(transport).council([ITEM])

    def test_503_is_unavailable_and_quotes_the_body(self) -> None:
        detail = "council unavailable: no API key for seat gpt55"
        transport, _ = recorder(status_code=503, json_body={"detail": detail})
        with pytest.raises(CouncilServiceUnavailable) as caught:
            client(transport).council([ITEM])
        assert detail in str(caught.value)
        assert "/concordia_go_council" in str(caught.value)

    def test_422_is_a_value_error_carrying_the_body(self) -> None:
        detail = {"detail": [{"loc": ["body", "items", 0, "text"], "msg": "Field required"}]}
        transport, _ = recorder(status_code=422, json_body=detail)
        with pytest.raises(ValueError) as caught:
            client(transport).council([{"id": "row1"}])
        assert "Field required" in str(caught.value)
        assert not isinstance(caught.value, CouncilServiceAuthError)

    def test_422_from_the_menu_endpoint_is_mapped_too(self) -> None:
        transport, _ = recorder(status_code=422, text="at most 1000 items per request")
        with pytest.raises(ValueError, match="at most 1000 items"):
            client(transport).menu([ITEM])

    def test_any_other_failure_is_left_to_httpx(self) -> None:
        transport, _ = recorder(status_code=500, text="Internal Server Error")
        with pytest.raises(httpx.HTTPStatusError):
            client(transport).health()

    def test_the_mapped_errors_never_quote_the_key(self) -> None:
        """A 401 message names where the key came from, never the key."""
        transport, _ = recorder(status_code=401, text="unauthorized")
        service = ConcordiaServiceClient(transport=transport, api_key="sekrit-value")
        with pytest.raises(CouncilServiceAuthError) as caught:
            service.council([ITEM])
        assert "sekrit-value" not in str(caught.value)

    def test_a_body_echoing_the_key_is_redacted(self) -> None:
        transport, _ = recorder(status_code=503, text="X-API-Key: sekrit-value rejected")
        service = ConcordiaServiceClient(transport=transport, api_key="sekrit-value")
        with pytest.raises(CouncilServiceUnavailable) as caught:
            service.council([ITEM])
        assert "sekrit-value" not in str(caught.value)


class TestRequestBodies:
    """What the client puts on the wire, against the captured live schema."""

    def test_the_council_body_validates_against_councilrequest(self) -> None:
        transport, seen = recorder(json_body={"items": []})
        client(transport).council(
            [ITEM],
            profile="argo",
            roles={"critic": "claudeopus45"},
            force_council=True,
            use_layer1=True,
            cap=20,
            retrieval_k=30,
        )
        body = body_of(seen[0])
        assert_valid(body, "CouncilRequest")
        assert body == {
            "items": [ITEM],
            "profile": "argo",
            "roles": {"critic": "claudeopus45"},
            "force_council": True,
            "use_layer1": True,
            "cap": 20,
            "retrieval_k": 30,
        }

    def test_the_minimal_council_body_validates_and_omits_the_unset_seats(self) -> None:
        transport, seen = recorder(json_body={"items": []})
        client(transport).council([{"text": ITEM["text"]}])
        body = body_of(seen[0])
        assert_valid(body, "CouncilRequest")
        assert "profile" not in body
        assert "roles" not in body
        assert body["force_council"] is False
        assert body["use_layer1"] is False

    def test_the_menu_body_validates_against_menurequest(self) -> None:
        transport, seen = recorder(json_body={"items": []})
        client(transport).menu([ITEM], cap=10, max_height=0, model="minilm")
        body = body_of(seen[0])
        assert_valid(body, "MenuRequest")
        assert body == {"items": [ITEM], "cap": 10, "max_height": 0, "model": "minilm"}

    def test_a_council_item_may_carry_a_gene_id_and_its_own_menu(self) -> None:
        """``CouncilItem`` in the capture accepts both; the client passes them through."""
        item = {
            "id": "row1",
            "text": ITEM["text"],
            "gene_id": "fig|83333.1.peg.1234",
            "candidates": [
                {"go_id": "GO:0004488", "name": "mtd activity", "origin": "ec_lookup:1.5.1.5"},
                {"go_id": "GO:0016491", "name": "oxidoreductase activity", "score": 0.61},
            ],
        }
        transport, seen = recorder(json_body={"items": []})
        client(transport).council([item])
        assert_valid(body_of(seen[0]), "CouncilRequest")

    def test_the_schema_check_rejects_a_body_the_service_would_reject(self) -> None:
        """Proof the validation above is not vacuous, on both check paths."""
        assert validation_error({"items": [{"id": "row1"}]}, "CouncilRequest") is not None
        assert validation_error({}, "CouncilRequest") is not None
        assert validation_error({"items": [ITEM], "use_layer_1": False}, "CouncilRequest") is not None

    def test_the_captured_schema_is_the_one_the_client_was_built_against(self) -> None:
        """Guards against a later re-capture silently changing the contract."""
        assert OPENAPI["info"]["title"] == "mfgopredict service"
        assert OPENAPI["info"]["version"] == "0.1.0"
        council = OPENAPI["paths"]["/concordia_go_council"]["post"]
        assert council["requestBody"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/CouncilRequest"
        }
        assert {"APIKeyHeader": []} in council["security"]
        assert OPENAPI["components"]["securitySchemes"]["APIKeyHeader"]["name"] == "X-API-Key"
        assert "at most 20" in SCHEMAS["CouncilRequest"]["properties"]["items"]["description"]
        menu = OPENAPI["paths"]["/concordia/go/menu"]["post"]
        assert menu["requestBody"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/MenuRequest"
        }
        assert "security" not in menu


class TestRowToComparison:
    """The one conversion: ``{go_id, name}`` objects to ids."""

    def test_go_term_objects_become_ids_in_order(self) -> None:
        comparison = ConcordiaServiceClient.row_to_comparison(COUNCIL_ROW)
        assert comparison["selected_ids"] == ["GO:0004488", "GO:0004477"]
        assert comparison["status"] == "ok"
        assert comparison["final_confidence"] == "high"
        assert comparison["decline_reason"] is None

    def test_the_original_row_is_kept_whole(self) -> None:
        comparison = ConcordiaServiceClient.row_to_comparison(COUNCIL_ROW)
        assert comparison["service_row"] == COUNCIL_ROW
        assert comparison["service_row"]["decided_by"] == "council"
        assert comparison["service_row"]["final_go_ids"][0]["name"].startswith("methylene")

    def test_a_declined_row_has_no_ids_and_keeps_its_reason(self) -> None:
        row = {
            **COUNCIL_ROW,
            "decided_by": "guard",
            "final_go_ids": [],
            "final_confidence": None,
            "decline_reason": "contentless",
        }
        comparison = ConcordiaServiceClient.row_to_comparison(row)
        assert comparison["selected_ids"] == []
        assert comparison["decline_reason"] == "contentless"
        assert comparison["final_confidence"] is None

    def test_an_error_row_is_reported_as_an_error(self) -> None:
        row = {**COUNCIL_ROW, "status": "error", "final_go_ids": [], "final_confidence": None}
        comparison = ConcordiaServiceClient.row_to_comparison(row)
        assert comparison["status"] == "error"
        assert comparison["selected_ids"] == []

    def test_it_returns_a_plain_dict_and_never_a_verdict(self) -> None:
        comparison = ConcordiaServiceClient.row_to_comparison(COUNCIL_ROW)
        assert type(comparison) is dict
        assert set(comparison) == {
            "selected_ids",
            "status",
            "decline_reason",
            "final_confidence",
            "service_row",
        }

    def test_the_whole_response_can_be_mapped_row_by_row(self) -> None:
        transport, _ = recorder(json_body={"items": [COUNCIL_ROW, {**COUNCIL_ROW, "id": "row2"}]})
        reply = client(transport).council([ITEM, {"id": "row2", "text": "glxK"}])
        mapped = [ConcordiaServiceClient.row_to_comparison(row) for row in reply["items"]]
        assert [m["service_row"]["id"] for m in mapped] == ["row1", "row2"]


class TestNoInjection:
    """No mock transport: the real client, proving nothing was sent."""

    def test_a_missing_key_refuses_on_the_real_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A closed loopback port stands in for the service.

        Any request the client actually made would be refused by the kernel and
        would surface as :class:`httpx.ConnectError`, so the missing-key error
        below can only mean nothing reached the socket.
        """
        monkeypatch.delenv(DEFAULT_API_KEY_ENV, raising=False)
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()

        service = ConcordiaServiceClient(base_url=f"http://127.0.0.1:{port}")
        assert "transport" not in service.client_kwargs
        with pytest.raises(CouncilServiceAuthError) as caught:
            service.council([ITEM])
        assert DEFAULT_API_KEY_ENV in str(caught.value)
        assert "No request was made" in str(caught.value)
        service.close()

    def test_the_timeout_reaches_the_real_client(self) -> None:
        """900 s by default: one request runs the whole council, no polling."""
        service = ConcordiaServiceClient()
        assert service.client_kwargs["timeout"] == 900.0
        assert "transport" not in service.client_kwargs
        service.close()
