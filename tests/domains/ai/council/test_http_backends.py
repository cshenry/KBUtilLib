"""The council's HTTP backends: ARGO and any OpenAI-compatible server.

Every test here drives the REAL request path -- the backend's own
``httpx.AsyncClient``, its own body construction, its own parsing -- and only
replaces the socket, with :class:`httpx.MockTransport`.  Nothing in the
backend is monkeypatched, so a test passing is evidence about the shipped code
rather than about a stub.

Three tests deliberately use no mock transport at all:
:meth:`TestNoInjection.test_argo_without_a_username_fails_auth_on_the_real_client`
and
:meth:`TestNoInjection.test_openai_against_a_closed_local_port_waits_for_the_model`
build the real client and prove the two failures a user hits first, and
:meth:`TestProxyWiring.test_socks_proxy_reaches_the_client` builds a SOCKS
client without sending anything through it.

No test makes a network call beyond a refused connection to a closed
``127.0.0.1`` port, spends a token, needs a CLI login or needs a GPU.
"""

from __future__ import annotations

import asyncio
import json
import socket
from typing import Any, Callable, Dict, List, Optional, Union

import httpx
import pytest

from kbutillib.domains.ai.council.backends.http import (
    ARGO_BASE_URL,
    ARGO_REFUSAL_SIGNATURES,
    ARGO_USER_ENV,
    ArgoBackend,
    OpenAICompatibleBackend,
    argo_model_kwargs,
)
from kbutillib.domains.ai.council.backends.http_common import (
    extract_json_object,
    strip_think,
)
from kbutillib.domains.ai.council.protocols import Backend
from kbutillib.domains.ai.council.types import CompletionRequest, SeatSpec

POPLAR_BASE = "http://127.0.0.1:8301/v1"

#: A well-formed proposer answer, in the generic post-processed shape.
ANSWER: Dict[str, Any] = {
    "picked": [{"id": "GO:0003824", "confidence": "high", "why": "catalytic activity"}],
    "overall_confidence": "high",
    "no_functional_content": False,
    "notes": None,
}


def run(coro: Any) -> Any:
    """Run one coroutine on a loop owned by this call.

    Deliberately NOT ``asyncio.run``: that leaves the thread with no current
    event loop, after which the deprecated ``asyncio.get_event_loop()`` raises.
    Other modules in this suite still call it and ``tests/domains`` collects
    first, so ``asyncio.run`` here would break them from a distance.  Same
    helper and same reason as ``engine_support.run``.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class Recorder:
    """A :class:`httpx.MockTransport` handler that records every request.

    *reply* is either a fixed :class:`httpx.Response` or a callable taking the
    request and returning one (or raising, to simulate a transport failure).
    """

    def __init__(
        self, reply: Union[httpx.Response, Callable[[httpx.Request], httpx.Response]]
    ) -> None:
        self.requests: List[httpx.Request] = []
        self._reply = reply

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if callable(self._reply):
            return self._reply(request)
        return self._reply

    @property
    def transport(self) -> httpx.MockTransport:
        """The transport to hand a backend."""
        return httpx.MockTransport(self)

    @property
    def body(self) -> Dict[str, Any]:
        """The JSON body of the last recorded request."""
        return json.loads(self.requests[-1].content)

    @property
    def log(self) -> List[str]:
        """``"METHOD /path"`` for every recorded request, in order."""
        return [f"{r.method} {r.url.path}" for r in self.requests]


def chat_response(
    content: str,
    *,
    finish_reason: str = "stop",
    model: str = "served-model-name",
    usage: Optional[Dict[str, int]] = None,
    status: int = 200,
) -> httpx.Response:
    """Build an OpenAI-shaped chat-completions reply carrying *content*."""
    payload: Dict[str, Any] = {
        "id": "chatcmpl-1",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
    }
    if usage is not None:
        payload["usage"] = usage
    return httpx.Response(status, json=payload)


def seat(
    model: str = "gemini35flash",
    *,
    kind: str = "argo",
    endpoint: str = "argo",
    temperature: Optional[float] = 0,
    max_output_tokens: int = 4000,
    extra: Optional[Dict[str, Any]] = None,
    timeout_s: float = 180,
) -> SeatSpec:
    """One resolved seat."""
    return SeatSpec(
        seat_name="proposer_1",
        endpoint=endpoint,
        endpoint_kind=kind,
        model=model,
        canonical_model=f"vendor/{model}",
        max_output_tokens=max_output_tokens,
        temperature=temperature,
        timeout_s=timeout_s,
        extra=dict(extra or {}),
    )


def request(seat_spec: Optional[SeatSpec] = None, **kwargs: Any) -> CompletionRequest:
    """One rendered invocation for *seat_spec*."""
    return CompletionRequest(
        seat=seat_spec if seat_spec is not None else seat(),
        role=kwargs.pop("role", "proposer"),
        system=kwargs.pop("system", "You are a curator."),
        user=kwargs.pop("user", "Pick from the menu."),
        json_schema=kwargs.pop("json_schema", {"type": "object"}),
        trace_id=kwargs.pop("trace_id", "item-1|proposer|proposer_1"),
        deadline_s=kwargs.pop("deadline_s", 180),
    )


def argo(recorder: Optional[Recorder] = None, **config: Any) -> ArgoBackend:
    """An :class:`ArgoBackend` over *recorder* (or the real client if none)."""
    cfg: Dict[str, Any] = {
        "name": "argo",
        "kind": "argo",
        "base_url": ARGO_BASE_URL,
        "user_env": ARGO_USER_ENV,
    }
    cfg.update(config)
    return ArgoBackend(cfg, transport=recorder.transport if recorder else None)


def openai(
    recorder: Optional[Recorder] = None, **config: Any
) -> OpenAICompatibleBackend:
    """An :class:`OpenAICompatibleBackend` over *recorder* (or the real client)."""
    cfg: Dict[str, Any] = {
        "name": "gpt-oss-120b",
        "kind": "openai",
        "base_url": POPLAR_BASE,
    }
    cfg.update(config)
    return OpenAICompatibleBackend(cfg, transport=recorder.transport if recorder else None)


def complete(backend: Any, req: Optional[CompletionRequest] = None) -> Any:
    """Run one ``complete()`` and close the backend on the same event loop."""

    async def _once() -> Any:
        try:
            return await backend.complete(req if req is not None else request())
        finally:
            await backend.aclose()

    return run(_once())


def list_models(backend: Any) -> List[Dict[str, Any]]:
    """Run one ``models()`` and close the backend on the same event loop."""

    async def _once() -> List[Dict[str, Any]]:
        try:
            return await backend.models()
        finally:
            await backend.aclose()

    return run(_once())


# ---------------------------------------------------------------------------
# The ARGO parameter mapping
# ---------------------------------------------------------------------------


class TestArgoParameterMapping:
    """One case per branch of Jose Faria's ``model_kwargs`` port.

    ARGO fronts several vendors and passes their parameter quirks straight
    through, so getting the token-ceiling parameter wrong is a hard 400 rather
    than a soft degradation.  Each row here is a family, not an example.
    """

    @pytest.fixture(autouse=True)
    def _argo_user(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ARGO_USER", "chenry")

    @pytest.mark.parametrize(
        "model,temperature,n,expected",
        [
            # Anthropic family: max_tokens, temperature allowed.
            ("claudeopus4", 0, 4000, {"max_tokens": 4000, "temperature": 0}),
            ("claudesonnet45", 0.3, 100, {"max_tokens": 100, "temperature": 0.3}),
            ("claudehaiku45", 0, 2000, {"max_tokens": 2000, "temperature": 0}),
            # ... except the three that reject a temperature outright.
            ("claudeopus47", 0, 4000, {"max_tokens": 4000}),
            ("claudeopus48", 0.7, 4000, {"max_tokens": 4000}),
            ("claudesonnet5", 0, 4000, {"max_tokens": 4000}),
            # A None temperature means "send none", whatever the family.
            ("claudeopus4", None, 4000, {"max_tokens": 4000}),
            # The four models that accept only temperature 1.
            ("gpt55", 0, 4000, {"max_completion_tokens": 4000, "temperature": 1}),
            ("gpt56sol", 0, 10, {"max_completion_tokens": 10, "temperature": 1}),
            ("gpt56terra", None, 10, {"max_completion_tokens": 10, "temperature": 1}),
            ("gpt56luna", 0.2, 10, {"max_completion_tokens": 10, "temperature": 1}),
            # o-series reasoning models: no temperature at all.
            ("gpto3mini", 0, 4000, {"max_completion_tokens": 4000}),
            ("gpto4mini", 0.9, 4000, {"max_completion_tokens": 4000}),
            # Other gpt5*/gpt41* models: max_completion_tokens plus temperature.
            ("gpt5mini", 0, 4000, {"max_completion_tokens": 4000, "temperature": 0}),
            ("gpt41", 0.5, 4000, {"max_completion_tokens": 4000, "temperature": 0.5}),
            ("gpt41mini", None, 4000, {"max_completion_tokens": 4000}),
            # Gemini: max_tokens, with a floor for the two thin ones.
            ("gemini35flash", 0, 400, {"max_tokens": 1500, "temperature": 0}),
            ("gemini31flashlite", 0, 400, {"max_tokens": 1500, "temperature": 0}),
            ("gemini35flash", 0, 4000, {"max_tokens": 4000, "temperature": 0}),
            ("gemini25pro", 0, 400, {"max_tokens": 400, "temperature": 0}),
            ("gemini25pro", None, 400, {"max_tokens": 400}),
            # Anything else: the plain OpenAI-compatible shape.
            ("llama31405b", 0, 4000, {"max_tokens": 4000, "temperature": 0}),
            ("llama31405b", None, 4000, {"max_tokens": 4000}),
        ],
    )
    def test_mapping_table(
        self,
        model: str,
        temperature: Optional[float],
        n: int,
        expected: Dict[str, Any],
    ) -> None:
        assert argo_model_kwargs(model, temperature, n) == expected

    def test_gpt55_is_matched_before_the_gpt5_prefix(self) -> None:
        """``gpt55`` starts with ``gpt5``; the exact-set rule must win.

        If the prefix branch were checked first, ``gpt55`` would be sent the
        seat's temperature, which it rejects.
        """
        assert argo_model_kwargs("gpt55", 0, 4000)["temperature"] == 1
        assert "temperature" not in argo_model_kwargs("gpto3mini", 0, 4000)

    def test_the_mapping_is_what_reaches_the_wire(self) -> None:
        """The body really carries the mapped parameters, not the seat's."""
        recorder = Recorder(chat_response(json.dumps(ANSWER)))
        result = complete(
            argo(recorder),
            request(seat("gemini35flash", max_output_tokens=400)),
        )
        assert result.ok, result.error
        assert recorder.body["max_tokens"] == 1500
        assert recorder.body["model"] == "gemini35flash"
        assert recorder.log == ["POST /argoapi/v1/chat/completions"]

    def test_the_body_is_system_then_user_and_asks_for_no_response_format(self) -> None:
        """Servers disagree about ``response_format``; the engine validates."""
        recorder = Recorder(chat_response(json.dumps(ANSWER)))
        complete(argo(recorder), request(system="SYS", user="USR"))
        body = recorder.body
        assert body["messages"] == [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "USR"},
        ]
        assert "response_format" not in body

    def test_model_params_and_extra_body_reach_the_body(self) -> None:
        """A model's configured params and the endpoint's extra_body are sent."""
        recorder = Recorder(chat_response(json.dumps(ANSWER)))
        spec = seat(
            "gemini25pro",
            extra={"top_p": 0.9, "extra_body": {"chat_template_kwargs": {"x": 1}}},
        )
        complete(argo(recorder), request(spec))
        body = recorder.body
        assert body["top_p"] == 0.9
        assert body["chat_template_kwargs"] == {"x": 1}


# ---------------------------------------------------------------------------
# ARGO authentication
# ---------------------------------------------------------------------------


class TestArgoAuthentication:
    """ARGO authenticates by ANL username, carried as a bearer token."""

    def test_bearer_header_carries_the_username(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ARGO_USER", "chenry")
        recorder = Recorder(chat_response(json.dumps(ANSWER)))
        result = complete(argo(recorder))
        assert result.ok, result.error
        assert recorder.requests[0].headers["Authorization"] == "Bearer chenry"

    def test_a_custom_user_env_is_honoured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ARGO_USER", raising=False)
        monkeypatch.setenv("MY_ANL_USER", "someone")
        recorder = Recorder(chat_response(json.dumps(ANSWER)))
        result = complete(argo(recorder, user_env="MY_ANL_USER"))
        assert result.ok, result.error
        assert recorder.requests[0].headers["Authorization"] == "Bearer someone"

    def test_a_missing_username_fails_the_call_without_sending_anything(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No username is an ``auth`` halt, decided before any request."""
        monkeypatch.delenv("ARGO_USER", raising=False)
        recorder = Recorder(chat_response(json.dumps(ANSWER)))
        result = complete(argo(recorder))
        assert result.ok is False
        assert result.error is not None
        assert result.error.kind == "auth"
        assert result.error.halts_endpoint is True
        assert result.error.retryable is False
        assert recorder.requests == []

    def test_an_empty_username_is_treated_as_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ARGO_USER", "   ")
        recorder = Recorder(chat_response(json.dumps(ANSWER)))
        result = complete(argo(recorder))
        assert result.ok is False
        assert result.error is not None and result.error.kind == "auth"
        assert recorder.requests == []

    def test_constructing_a_backend_without_a_username_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A config gap on one endpoint must not stop the worker's others."""
        monkeypatch.delenv("ARGO_USER", raising=False)
        backend = argo(Recorder(chat_response("{}")))
        assert backend.kind == "argo"
        run(backend.aclose())


# ---------------------------------------------------------------------------
# The ARGO budget breaker
# ---------------------------------------------------------------------------


class TestArgoRefusalSignatures:
    """ARGO's spend/auth guard rails answer HTTP 200 with prose.

    On 2026-10-05 that silently killed 64 scheduled wakes, because an HTTP 200
    with a non-JSON body looked like a model that had merely answered badly.
    These tests are the detector for it.
    """

    @pytest.mark.parametrize(
        "signature,expected_kind",
        [
            ("IMPORTANT USAGE NOTICE FROM ARGO", "quota_exhausted"),
            ("ACCESS REVOKED", "quota_exhausted"),
            ("Monthly limit exceeded", "quota_exhausted"),
            ("AUTHENTICATION NOTICE", "auth"),
            ("msg_blocked_", "auth"),
        ],
    )
    def test_each_signature_maps_to_its_error_kind(
        self, monkeypatch: pytest.MonkeyPatch, signature: str, expected_kind: str
    ) -> None:
        monkeypatch.setenv("ARGO_USER", "chenry")
        body = f"Dear user, {signature} 1234: your account is affected."
        result = complete(argo(Recorder(chat_response(body))))
        assert result.ok is False
        assert result.error is not None
        assert result.error.kind == expected_kind
        assert result.error.halts_endpoint is True
        assert signature[:20] in (result.error.message or "")

    def test_every_declared_signature_is_covered_by_the_table_above(self) -> None:
        """The parametrised list above is the module constant, not a subset."""
        assert {sig for sig, _ in ARGO_REFUSAL_SIGNATURES} == {
            "IMPORTANT USAGE NOTICE FROM ARGO",
            "ACCESS REVOKED",
            "Monthly limit exceeded",
            "AUTHENTICATION NOTICE",
            "msg_blocked_",
        }

    def test_matching_is_case_insensitive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ARGO_USER", "chenry")
        result = complete(
            argo(Recorder(chat_response("important usage notice from argo: stop")))
        )
        assert result.ok is False
        assert result.error is not None and result.error.kind == "quota_exhausted"

    def test_the_first_500_characters_of_the_body_are_kept(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Enough to recognise the banner later; not the whole error page."""
        monkeypatch.setenv("ARGO_USER", "chenry")
        body = "ACCESS REVOKED. " + ("x" * 200 + "MIDDLE" + "y" * 400)
        result = complete(argo(Recorder(chat_response(body))))
        assert result.error is not None
        message = result.error.message
        assert "MIDDLE" in message
        assert "y" * 400 not in message
        assert "ACCESS REVOKED" in message

    def test_a_banner_in_a_non_json_envelope_is_still_detected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The breaker sometimes answers 200 with no envelope at all."""
        monkeypatch.setenv("ARGO_USER", "chenry")
        reply = httpx.Response(
            200, text="IMPORTANT USAGE NOTICE FROM ARGO: monthly spend exceeded"
        )
        result = complete(argo(Recorder(reply)))
        assert result.ok is False
        assert result.error is not None and result.error.kind == "quota_exhausted"

    def test_a_valid_answer_quoting_a_signature_is_not_a_refusal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole reason the check runs only on unparseable content.

        A proposer may legitimately write "ACCESS REVOKED" in its ``why``
        field -- that is a vote, and treating it as a budget halt would stop
        every endpoint over a quoted phrase.
        """
        monkeypatch.setenv("ARGO_USER", "chenry")
        answer = {
            "picked": [
                {
                    "id": "GO:0003824",
                    "confidence": "low",
                    "why": "the source text literally says ACCESS REVOKED "
                    "and AUTHENTICATION NOTICE, which is not evidence",
                }
            ],
            "overall_confidence": "low",
            "no_functional_content": False,
            "notes": "mentions msg_blocked_ too",
        }
        result = complete(argo(Recorder(chat_response(json.dumps(answer)))))
        assert result.ok is True
        assert result.error is None
        assert result.parsed == answer
        assert "ACCESS REVOKED" in result.parsed["picked"][0]["why"]

    def test_a_refusal_in_a_429_body_is_still_a_transport_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Status wins over content: a 429 is rate limiting, and retryable."""
        monkeypatch.setenv("ARGO_USER", "chenry")
        reply = httpx.Response(429, text="Monthly limit exceeded")
        result = complete(argo(Recorder(reply)))
        assert result.ok is False
        assert result.error is not None and result.error.kind == "transport"
        assert result.error.retryable is True


# ---------------------------------------------------------------------------
# Status and transport mapping (shared by both backends)
# ---------------------------------------------------------------------------


class TestFailureMapping:
    """HTTP statuses and transport exceptions onto the council's taxonomy."""

    @pytest.fixture(autouse=True)
    def _argo_user(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ARGO_USER", "chenry")

    @pytest.mark.parametrize("status", [429, 500, 502, 504])
    def test_429_and_5xx_are_retryable_transport_failures(self, status: int) -> None:
        result = complete(argo(Recorder(httpx.Response(status, text="upstream sad"))))
        assert result.ok is False
        assert result.error is not None
        assert result.error.kind == "transport"
        assert result.error.retryable is True
        assert result.error.halts_endpoint is False

    @pytest.mark.parametrize("status", [401, 403])
    def test_401_and_403_halt_the_endpoint_as_auth(self, status: int) -> None:
        result = complete(argo(Recorder(httpx.Response(status, text="nope"))))
        assert result.ok is False
        assert result.error is not None
        assert result.error.kind == "auth"
        assert result.error.halts_endpoint is True

    def test_a_400_is_the_request_being_wrong_not_the_credential(self) -> None:
        """A rejected request must not halt the endpoint or be retried."""
        result = complete(
            argo(Recorder(httpx.Response(400, text="unsupported parameter")))
        )
        assert result.ok is False
        assert result.error is not None
        assert result.error.kind == "invalid_output"
        assert result.error.halts_endpoint is False
        assert result.error.retryable is False

    def test_a_timeout_is_a_timeout(self) -> None:
        def _timeout(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("took too long", request=request)

        result = complete(argo(Recorder(_timeout)))
        assert result.ok is False
        assert result.error is not None
        assert result.error.kind == "timeout"
        assert result.error.retryable is True

    def test_a_read_error_is_a_transport_failure(self) -> None:
        def _broken(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadError("connection reset", request=request)

        result = complete(argo(Recorder(_broken)))
        assert result.ok is False
        assert result.error is not None and result.error.kind == "transport"

    def test_argo_treats_a_refused_connection_as_transport(self) -> None:
        """For a gateway, a refused connection is a blip -- not an idle model."""

        def _refused(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        result = complete(argo(Recorder(_refused)))
        assert result.ok is False
        assert result.error is not None and result.error.kind == "transport"

    def test_a_truncated_reply_is_not_a_vote(self) -> None:
        result = complete(
            argo(
                Recorder(
                    chat_response('{"picked": [{"id": "GO:00', finish_reason="length")
                )
            )
        )
        assert result.ok is False
        assert result.truncated is True
        assert result.error is not None
        assert result.error.kind == "invalid_output"
        assert "truncated" in result.error.message
        assert result.finish_reason == "length"

    def test_unparseable_content_fails_but_keeps_the_text(self) -> None:
        result = complete(argo(Recorder(chat_response("I would rather not say."))))
        assert result.ok is False
        assert result.error is not None and result.error.kind == "invalid_output"
        assert result.text == "I would rather not say."

    def test_an_envelope_without_a_message_fails_invalid_output(self) -> None:
        reply = httpx.Response(200, json={"model": "m", "choices": []})
        result = complete(argo(Recorder(reply)))
        assert result.ok is False
        assert result.error is not None and result.error.kind == "invalid_output"

    def test_an_error_message_never_carries_the_bearer_token(self) -> None:
        """A transport exception can quote the request headers back at us."""

        def _leaky(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadError(
                "failed sending Authorization: Bearer chenry", request=request
            )

        result = complete(argo(Recorder(_leaky)))
        assert result.error is not None
        assert "chenry" not in result.error.message
        assert "***" in result.error.message


# ---------------------------------------------------------------------------
# Parsing a reply
# ---------------------------------------------------------------------------


class TestReplyParsing:
    """Fences, reasoning blocks, usage and the resolved model."""

    @pytest.fixture(autouse=True)
    def _argo_user(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ARGO_USER", "chenry")

    def test_fenced_json_is_parsed(self) -> None:
        content = "```json\n" + json.dumps(ANSWER) + "\n```"
        result = complete(argo(Recorder(chat_response(content))))
        assert result.ok is True, result.error
        assert result.parsed == ANSWER

    def test_prose_around_the_object_is_tolerated(self) -> None:
        content = "Here is my answer:\n" + json.dumps(ANSWER) + "\nHope that helps."
        result = complete(argo(Recorder(chat_response(content))))
        assert result.ok is True, result.error
        assert result.parsed == ANSWER

    def test_think_blocks_are_stripped_before_parsing(self) -> None:
        """A scratchpad routinely contains draft JSON; it must not be the answer."""
        content = (
            '<think>Maybe {"picked": [{"id": "WRONG", "confidence": "low", '
            '"why": "draft"}]} but let me reconsider.</think>\n'
            + json.dumps(ANSWER)
        )
        result = complete(openai(Recorder(chat_response(content))), request(seat(
            "qwen3-32b", kind="openai", endpoint="qwen3-32b"
        )))
        assert result.ok is True, result.error
        assert result.parsed == ANSWER

    def test_think_stripping_handles_two_blocks(self) -> None:
        assert strip_think("<think>a</think>X<think>b</think>Y") == "XY"

    def test_extract_json_object_needs_a_parseable_brace_span(self) -> None:
        """Only a ``{...}`` span that parses as an object comes back."""
        assert extract_json_object("no braces at all") is None
        assert extract_json_object("") is None
        assert extract_json_object("{not json at all}") is None
        # Two objects in a list: the outermost span is not itself valid JSON.
        assert extract_json_object('[{"a": 1}, {"b": 2}]') is None
        # One object in a list IS a brace span, so it comes back and the
        # engine's schema check -- not this helper -- rejects the shape.
        assert extract_json_object('[{"id": "GO:1"}]') == {"id": "GO:1"}

    def test_usage_and_resolved_model_are_reported(self) -> None:
        reply = chat_response(
            json.dumps(ANSWER),
            model="gemini-3.5-flash-002",
            usage={"prompt_tokens": 120, "completion_tokens": 45},
        )
        result = complete(argo(Recorder(reply)))
        assert result.ok is True, result.error
        assert result.usage == {"prompt_tokens": 120, "completion_tokens": 45}
        assert result.resolved_model == "gemini-3.5-flash-002"
        assert result.finish_reason == "stop"
        assert result.schema_enforced is False
        assert result.from_cache is False

    def test_a_reply_with_no_usage_block_reports_none(self) -> None:
        """``None`` is what lets the worker tell "free" from "unmeasurable"."""
        result = complete(argo(Recorder(chat_response(json.dumps(ANSWER)))))
        assert result.ok is True, result.error
        assert result.usage is None


# ---------------------------------------------------------------------------
# The OpenAI-compatible backend
# ---------------------------------------------------------------------------


class TestOpenAICompatible:
    """Self-hosted vLLM ports: unavailability is waiting, not failure."""

    def test_503_is_model_unavailable_and_names_the_endpoint(self) -> None:
        recorder = Recorder(httpx.Response(503, text="no model loaded"))
        result = complete(openai(recorder), request(seat("gpt-oss-120b", kind="openai")))
        assert result.ok is False
        assert result.error is not None
        assert result.error.kind == "model_unavailable"
        assert result.error.retryable is True
        assert result.error.halts_endpoint is False
        assert "gpt-oss-120b" in result.error.message

    def test_503_names_no_control_plane(self) -> None:
        """There is none to name: model management is out of the product."""
        recorder = Recorder(httpx.Response(503, text="no model loaded"))
        result = complete(openai(recorder), request(seat("gpt-oss-120b", kind="openai")))
        assert result.error is not None
        assert "/service/" not in result.error.message
        assert "status" not in result.error.message

    def test_503_sends_no_request_to_any_service_path(self) -> None:
        """Lifecycle is another component's job; a seat must never provoke it."""
        recorder = Recorder(httpx.Response(503, text="no model loaded"))
        complete(openai(recorder), request(seat("gpt-oss-120b", kind="openai")))
        assert recorder.log == ["POST /v1/chat/completions"]
        assert not [r for r in recorder.requests if "/service/" in r.url.path]

    def test_a_404_naming_the_model_is_model_unavailable(self) -> None:
        """vLLM's answer when the server is up but the model is not resident."""
        recorder = Recorder(
            httpx.Response(
                404, json={"error": {"message": "The model `gpt-oss-120b` does not exist."}}
            )
        )
        result = complete(openai(recorder), request(seat("gpt-oss-120b", kind="openai")))
        assert result.error is not None
        assert result.error.kind == "model_unavailable"
        assert result.error.retryable is True

    def test_a_404_not_naming_the_model_stays_a_request_error(self) -> None:
        """A typo'd alias or a wrong path must not be parked for ever."""
        recorder = Recorder(httpx.Response(404, text="Not Found"))
        result = complete(openai(recorder), request(seat("gpt-oss-120b", kind="openai")))
        assert result.error is not None
        assert result.error.kind == "invalid_output"

    def test_the_only_paths_it_ever_requests_are_the_two_allowed_ones(self) -> None:
        """The whole surface of this backend, read off the request log."""
        def _reply(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/models"):
                return httpx.Response(200, json={"data": [{"id": "gpt-oss-120b"}]})
            return httpx.Response(503, text="no model loaded")

        recorder = Recorder(_reply)
        complete(openai(recorder), request(seat("gpt-oss-120b", kind="openai")))
        list_models(openai(recorder))
        assert set(recorder.log) == {"POST /v1/chat/completions", "GET /v1/models"}

    def test_a_refused_connection_is_model_unavailable(self) -> None:
        def _refused(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        result = complete(
            openai(Recorder(_refused)), request(seat("gpt-oss-120b", kind="openai"))
        )
        assert result.ok is False
        assert result.error is not None
        assert result.error.kind == "model_unavailable"

    def test_params_are_the_plain_openai_shape(self) -> None:
        recorder = Recorder(chat_response(json.dumps(ANSWER)))
        spec = seat(
            "qwen3-32b",
            kind="openai",
            max_output_tokens=2048,
            temperature=0.2,
            extra={"top_p": 0.8},
        )
        complete(openai(recorder), request(spec))
        body = recorder.body
        assert body["max_tokens"] == 2048
        assert body["temperature"] == 0.2
        assert body["top_p"] == 0.8
        assert "max_completion_tokens" not in body

    def test_a_none_temperature_is_not_sent(self) -> None:
        recorder = Recorder(chat_response(json.dumps(ANSWER)))
        complete(
            openai(recorder),
            request(seat("qwen3-32b", kind="openai", temperature=None)),
        )
        assert "temperature" not in recorder.body

    def test_an_explicit_api_key_is_sent_as_a_bearer_token(self) -> None:
        recorder = Recorder(chat_response(json.dumps(ANSWER)))
        complete(
            openai(recorder, api_key="sk-local"),
            request(seat("qwen3-32b", kind="openai")),
        )
        assert recorder.requests[0].headers["Authorization"] == "Bearer sk-local"

    def test_the_api_key_env_var_is_read_when_no_key_is_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MY_VLLM_KEY", "sk-from-env")
        recorder = Recorder(chat_response(json.dumps(ANSWER)))
        complete(
            openai(recorder, api_key_env="MY_VLLM_KEY"),
            request(seat("qwen3-32b", kind="openai")),
        )
        assert recorder.requests[0].headers["Authorization"] == "Bearer sk-from-env"

    def test_without_a_key_the_conventional_empty_is_sent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """vLLM wants the header present and ignores the value."""
        monkeypatch.delenv("MY_VLLM_KEY", raising=False)
        recorder = Recorder(chat_response(json.dumps(ANSWER)))
        complete(
            openai(recorder, api_key_env="MY_VLLM_KEY"),
            request(seat("qwen3-32b", kind="openai")),
        )
        assert recorder.requests[0].headers["Authorization"] == "Bearer EMPTY"


# ---------------------------------------------------------------------------
# models()
# ---------------------------------------------------------------------------


class TestModelListing:
    """``models()`` is free, diagnostic, and never fails a call."""

    @pytest.fixture(autouse=True)
    def _argo_user(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ARGO_USER", "chenry")

    def test_a_models_body_is_parsed(self) -> None:
        reply = httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": "gemini35flash", "object": "model"},
                    {"id": "gpt55", "object": "model"},
                ],
            },
        )
        recorder = Recorder(reply)
        models = list_models(argo(recorder))
        assert [m["id"] for m in models] == ["gemini35flash", "gpt55"]
        assert recorder.log == ["GET /argoapi/v1/models"]

    def test_a_bare_list_body_is_parsed_too(self) -> None:
        reply = httpx.Response(200, json=[{"id": "qwen3-32b"}])
        models = list_models(openai(Recorder(reply)))
        assert models == [{"id": "qwen3-32b"}]

    def test_an_http_error_yields_an_empty_list(self) -> None:
        models = list_models(argo(Recorder(httpx.Response(500, text="boom"))))
        assert models == []

    def test_a_transport_failure_yields_an_empty_list(self) -> None:
        def _refused(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        models = list_models(openai(Recorder(_refused)))
        assert models == []

    def test_a_missing_argo_username_yields_an_empty_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ARGO_USER", raising=False)
        recorder = Recorder(httpx.Response(200, json={"data": []}))
        assert list_models(argo(recorder)) == []
        assert recorder.requests == []

    def test_a_failed_listing_provokes_no_second_request(self) -> None:
        """No control plane to consult, so one GET and nothing else."""
        recorder = Recorder(httpx.Response(503, text="no model loaded"))
        assert list_models(openai(recorder)) == []
        assert recorder.log == ["GET /v1/models"]


# ---------------------------------------------------------------------------
# effective_params
# ---------------------------------------------------------------------------

_BANNED_SUBSTRINGS = ("auth", "key", "token", "proxy")
_TOKEN_CEILING_KEYS = ("max_tokens", "max_completion_tokens", "max_output_tokens")


def _offending_keys(value: Any, path: str = "") -> List[str]:
    """Every key under *value* whose name may not appear in a cache key."""
    offenders: List[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            name = str(key)
            lowered = name.lower()
            if name not in _TOKEN_CEILING_KEYS and any(
                bad in lowered for bad in _BANNED_SUBSTRINGS
            ):
                offenders.append(f"{path}{name}")
            offenders.extend(_offending_keys(child, f"{path}{name}."))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            offenders.extend(_offending_keys(child, f"{path}{index}."))
    return offenders


class TestEffectiveParams:
    """``effective_params`` feeds the prompt hash, so it is generation-only.

    It must carry no header, no credential and no routing detail: those belong
    to ``route_fingerprint``, and mixing them in would make a cross-user cache
    hit impossible (and could persist a secret).
    """

    @pytest.mark.parametrize("which", ["argo", "openai"])
    def test_no_credential_or_routing_key_appears_at_any_depth(
        self, monkeypatch: pytest.MonkeyPatch, which: str
    ) -> None:
        monkeypatch.setenv("ARGO_USER", "chenry")
        extra = {
            "top_p": 0.9,
            "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
        }
        if which == "argo":
            backend: Any = argo(
                Recorder(chat_response("{}")),
                proxy="socks5://127.0.0.1:1080",
                api_key="sk-should-never-appear",
            )
            spec = seat("gemini35flash", extra=extra)
        else:
            backend = openai(
                Recorder(chat_response("{}")),
                proxy="socks5://127.0.0.1:1080",
                api_key="sk-should-never-appear",
            )
            spec = seat("qwen3-32b", kind="openai", extra=extra)

        params = backend.effective_params(spec)
        assert _offending_keys(params) == []
        assert "sk-should-never-appear" not in json.dumps(params)
        assert "socks5" not in json.dumps(params)
        run(backend.aclose())

    def test_it_is_deterministic_and_reports_the_parameter_actually_sent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ARGO_USER", "chenry")
        backend = argo(Recorder(chat_response("{}")))
        spec = seat("gpt55", max_output_tokens=4000)
        first = backend.effective_params(spec)
        assert first == backend.effective_params(spec)
        # The seat asked for temperature 0; ARGO forces 1 for this model, and
        # the cache key must record what was SENT.
        assert first == {"max_completion_tokens": 4000, "temperature": 1}
        run(backend.aclose())

    def test_extra_body_is_included_because_it_changes_the_answer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ARGO_USER", "chenry")
        backend = openai(Recorder(chat_response("{}")))
        plain = seat("qwen3-32b", kind="openai")
        thinking = seat(
            "qwen3-32b",
            kind="openai",
            extra={"extra_body": {"chat_template_kwargs": {"enable_thinking": True}}},
        )
        assert backend.effective_params(plain) != backend.effective_params(thinking)
        run(backend.aclose())


# ---------------------------------------------------------------------------
# Proxy wiring, and the two no-injection checks
# ---------------------------------------------------------------------------


class TestProxyWiring:
    """A configured proxy reaches the client -- without dialling it."""

    def test_socks_proxy_reaches_the_client(self) -> None:
        backend = argo(proxy="socks5://127.0.0.1:1080")
        try:
            # These are literally the kwargs ``httpx.AsyncClient(**kwargs)``
            # was constructed with, so asserting on them asserts on the client.
            assert backend.client_kwargs["proxy"] == "socks5://127.0.0.1:1080"
            assert "transport" not in backend.client_kwargs
            pools = [
                type(getattr(transport, "_pool", None)).__name__
                for transport in backend.client._mounts.values()
            ]
            assert any("SOCKS" in name for name in pools), pools
        finally:
            run(backend.aclose())

    def test_an_empty_proxy_string_means_no_proxy(self) -> None:
        """The packaged config spells "unset" as ``proxy: ${KBU_ARGO_PROXY:-}``."""
        backend = argo(proxy="")
        try:
            assert backend.proxy is None
            assert "proxy" not in backend.client_kwargs
        finally:
            run(backend.aclose())


class TestNoInjection:
    """Both backends on the real ``httpx`` path, with nothing patched."""

    def test_both_backends_satisfy_the_backend_protocol(self) -> None:
        for backend in (argo(), openai()):
            assert isinstance(backend, Backend)
            run(backend.aclose())

    def test_argo_without_a_username_fails_auth_on_the_real_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No mock transport, no patched backend, no request attempted."""
        monkeypatch.delenv("ARGO_USER", raising=False)
        backend = ArgoBackend({"name": "argo", "kind": "argo", "base_url": ARGO_BASE_URL})
        assert "transport" not in backend.client_kwargs
        result = complete(backend)
        assert result.ok is False
        assert result.error is not None
        assert result.error.kind == "auth"
        assert result.error.halts_endpoint is True

    def test_openai_against_a_closed_local_port_waits_for_the_model(self) -> None:
        """A real refused connection on loopback -- the commonest poplar state."""
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()

        backend = OpenAICompatibleBackend(
            {
                "name": "qwen3-32b",
                "kind": "openai",
                "base_url": f"http://127.0.0.1:{port}/v1",
            }
        )
        assert "transport" not in backend.client_kwargs
        result = complete(
            backend, request(seat("qwen3-32b", kind="openai", timeout_s=5))
        )
        assert result.ok is False
        assert result.error is not None
        assert result.error.kind == "model_unavailable"
        assert f"127.0.0.1:{port}" in result.error.message


# ---------------------------------------------------------------------------
# Web search: both HTTP backends refuse, and spend nothing doing it
# ---------------------------------------------------------------------------


class TestWebSearchIsRefusedWithoutARequest:
    """Neither HTTP backend has live web search, and both say so honestly.

    The point of a refusal rather than a silent downgrade: a caller that
    asked for the live web and was quietly handed the model's training data
    has no way to tell it happened.  Every test here asserts that the
    recorder saw NO request, which is what makes "spends nothing" a measured
    fact and not a claim about where the guard was placed.
    """

    @pytest.mark.parametrize("kind", ["argo", "openai"])
    def test_the_attribute_is_false_on_both(self, kind: str) -> None:
        backend = argo() if kind == "argo" else openai()
        assert backend.supports_web_search is False
        run(backend.aclose())

    @pytest.mark.parametrize("kind", ["argo", "openai"])
    def test_a_web_search_request_is_refused_and_nothing_is_sent(
        self, kind: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Exported so the refusal cannot be mistaken for the missing-username
        # auth failure: ARGO's credential is PRESENT and the call is still
        # refused, which is also what proves the guard runs before _bearer.
        monkeypatch.setenv(ARGO_USER_ENV, "someuser")
        recorder = Recorder(chat_response(json.dumps(ANSWER)))
        backend = argo(recorder) if kind == "argo" else openai(recorder)

        result = complete(
            backend,
            request(seat(kind=kind, extra={"web_search": True})),
        )

        assert result.ok is False
        assert result.error is not None
        assert result.error.kind == "invalid_output"
        assert result.error.message == f"{kind} has no web search; use context mode"
        assert recorder.requests == [], "a refusal must not reach the transport"

    def test_argo_refuses_even_with_no_username_exported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The web-search refusal is reported, not the auth failure.

        Ordering matters for the caller: ``auth`` halts the whole endpoint and
        is what a user chases a credential over, while this is a per-request
        mistake the caller can fix by switching to context mode.
        """
        monkeypatch.delenv(ARGO_USER_ENV, raising=False)
        recorder = Recorder(chat_response(json.dumps(ANSWER)))

        result = complete(
            argo(recorder), request(seat(extra={"web_search": True}))
        )

        assert result.ok is False
        assert result.error is not None
        assert result.error.kind == "invalid_output"
        assert recorder.requests == []

    @pytest.mark.parametrize("kind", ["argo", "openai"])
    def test_a_falsy_opt_in_is_answered_normally(
        self, kind: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``web_search=False`` is the same as absent: the call goes through."""
        monkeypatch.setenv(ARGO_USER_ENV, "someuser")
        recorder = Recorder(chat_response(json.dumps(ANSWER)))
        backend = argo(recorder) if kind == "argo" else openai(recorder)

        result = complete(
            backend,
            request(seat(kind=kind, extra={"web_search": False})),
        )

        assert result.ok is True, result.error
        assert len(recorder.requests) == 1

    @pytest.mark.parametrize("value", [False, None, 0])
    def test_web_search_is_never_posted_as_a_model_parameter(
        self, value: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """It is an instruction to the API TYPE, not a knob on the model.

        A strict OpenAI-compatible server answers 400 for an unknown body
        field, so forwarding the key a caller always writes would break every
        call on exactly the servers that validate hardest.  Only falsy values
        are exercised because a truthy one never reaches the body at all.
        """
        monkeypatch.setenv(ARGO_USER_ENV, "someuser")
        recorder = Recorder(chat_response(json.dumps(ANSWER)))

        result = complete(
            argo(recorder),
            request(seat(extra={"web_search": value, "top_p": 0.9})),
        )

        assert result.ok is True, result.error
        body = recorder.body
        assert "web_search" not in body
        # The sibling param is still forwarded, so this is not "params were
        # dropped wholesale".
        assert body["top_p"] == 0.9

    def test_web_search_is_absent_from_effective_params_too(self) -> None:
        """The caller content-addresses its calls on this; a refused knob is not one."""
        backend = argo()
        try:
            params = backend.effective_params(
                seat(extra={"web_search": False, "top_p": 0.9})
            )
        finally:
            run(backend.aclose())
        assert "web_search" not in params
        assert params["top_p"] == 0.9
