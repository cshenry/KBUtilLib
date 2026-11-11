"""The ``anthropic`` api type: Anthropic's Messages API through the official SDK.

Every SDK-dependent test drives the REAL request path -- the installed SDK's
own client, serialisation, response-model parsing and raw-response header
access -- and replaces only the socket, with ``httpx2.MockTransport``.

Envelopes are built from the ``FIXTURE BASIS`` section of
``agent-io/work-records/anthropic-sdk-smoke.md``.  PROVENANCE: the SDK parsing
these envelopes proves CLIENT compatibility, not provider truth -- the
envelopes were written from documentation, not captured from the API.  The
live counterpart is ``scripts/council/anthropic_acceptance_rehearsal.py``.

Assertions are on :class:`LLMResult`, on published dicts and on the recorded
upstream requests -- never on private internals.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
import textwrap
from typing import Any, Callable, Dict, List, Optional, Union

import pytest

from kbutillib.domains.ai import llm
from kbutillib.domains.ai.council.engine.worker import _token_total
from kbutillib.domains.ai.llm.apis.http_common import extract_json_object, strip_think
from kbutillib.domains.ai.llm.types import LLMRequest

HAS_SDK = importlib.util.find_spec("anthropic") is not None
needs_sdk = pytest.mark.skipif(
    not HAS_SDK, reason="anthropic SDK not installed (kbutillib[anthropic], Python >= 3.10)"
)

KEY = "sk-ant-test-NOT-A-REAL-KEY-0123456789"


def run(coro: Any) -> Any:
    """Run one coroutine on a loop owned by this call (see test_http_backends.run)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def message(
    content: Optional[List[Dict[str, Any]]] = None,
    *,
    stop_reason: str = "end_turn",
    usage: Union[Dict[str, Any], None, str] = "default",
    stop_details: Optional[Dict[str, Any]] = None,
    model: str = "claude-haiku-4-5-20251001",
) -> Dict[str, Any]:
    """A Messages API success envelope per the FIXTURE BASIS."""
    env: Dict[str, Any] = {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content if content is not None else [{"type": "text", "text": '{"answer": 1}'}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
    }
    if usage == "default":
        env["usage"] = {"input_tokens": 3, "output_tokens": 5}
    elif usage is not None:
        env["usage"] = usage
    if stop_details is not None:
        env["stop_details"] = stop_details
    return env


def error_body(etype: str, msg: str, code: Optional[str] = None) -> Dict[str, Any]:
    err: Dict[str, Any] = {"type": etype, "message": msg}
    if code:
        err["details"] = {"error_code": code}
    return {"type": "error", "error": err}


RATE_HEADERS = {
    "anthropic-ratelimit-requests-limit": "50",
    "anthropic-ratelimit-requests-remaining": "49",
    "anthropic-ratelimit-requests-reset": "2026-11-01T00:00:01Z",
    "anthropic-ratelimit-input-tokens-limit": "30000",
    "anthropic-ratelimit-output-tokens-limit": "8000",
}


class Recorder:
    """An ``httpx2.MockTransport`` handler recording every upstream request."""

    def __init__(self, reply: Union[Callable[[Any], Any], Dict[str, Any]], status: int = 200,
                 headers: Optional[Dict[str, str]] = None) -> None:
        self.reply = reply
        self.status = status
        self.headers = headers or {}
        self.requests: List[Any] = []

    def bodies(self) -> List[Dict[str, Any]]:
        return [json.loads(r.content or b"{}") for r in self.requests]

    def __call__(self, request: Any) -> Any:
        import httpx2

        self.requests.append(request)
        if callable(self.reply):
            return self.reply(request)
        return httpx2.Response(self.status, json=self.reply, headers=self.headers)


def backend(recorder: Optional[Recorder] = None, **cfg: Any) -> Any:
    from kbutillib.domains.ai.llm.apis.anthropic import AnthropicBackend

    transport = None
    if recorder is not None:
        import httpx2

        transport = httpx2.MockTransport(recorder)
    cfg.setdefault("api_key", KEY)
    return AnthropicBackend(cfg, transport=transport)


def req(**kw: Any) -> LLMRequest:
    kw.setdefault("model", "claude-haiku-4-5")
    kw.setdefault("user", "Pick one.")
    kw.setdefault("temperature", None)
    return LLMRequest(**kw)


def call(recorder: Recorder, request: Optional[LLMRequest] = None, **cfg: Any) -> Any:
    b = backend(recorder, **cfg)
    try:
        return run(b.complete(request or req()))
    finally:
        run(b.aclose())


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


@needs_sdk
class TestRegistration:
    def test_registered_and_shaped(self) -> None:
        assert "anthropic" in llm.BUILTIN_API_TYPES
        assert "anthropic" in llm.api_types()
        api = llm.get_api("anthropic", {"api_key": KEY, "name": "reg-shape"})
        assert llm.protocol.missing_members(api) == []
        assert isinstance(api, llm.ApiType)
        assert api.kind == "anthropic"
        assert api.supports_web_search is False
        run(llm.aclose_all())


# ---------------------------------------------------------------------------
# response envelopes (table-driven)
# ---------------------------------------------------------------------------


ENVELOPES = [
    # (id, envelope, expect ok, expected parsed, expected text-in-result)
    ("text_only", message(), True, {"answer": 1}),
    (
        "thinking_first_then_text",
        message([{"type": "thinking", "thinking": "", "signature": "s"},
                 {"type": "text", "text": '{"answer": 2}'}]),
        True,
        {"answer": 2},
    ),
    (
        "multiple_text_blocks",
        message([{"type": "text", "text": '{"answer": '}, {"type": "text", "text": "3}"}]),
        True,
        {"answer": 3},
    ),
    (
        "fenced_json",
        message([{"type": "text", "text": 'Here:\n```json\n{"answer": 4}\n```'}]),
        True,
        {"answer": 4},
    ),
    (
        "thinking_tag_leak_with_brace",
        message([{"type": "text",
                  "text": '<thinking>draft {"answer": 0} hmm}</thinking>{"answer": 5}'}]),
        True,
        {"answer": 5},
    ),
    (
        "think_tag_leak_with_brace",
        message([{"type": "text", "text": '<think>{"answer": 0}</think>{"answer": 6}'}]),
        True,
        {"answer": 6},
    ),
]


@needs_sdk
class TestResponseParse:
    @pytest.mark.parametrize("name,envelope,ok,parsed", ENVELOPES, ids=[e[0] for e in ENVELOPES])
    def test_envelope(self, name: str, envelope: Dict[str, Any], ok: bool, parsed: Any) -> None:
        result = call(Recorder(envelope))
        assert result.ok is ok, result.error
        assert result.parsed == parsed
        assert result.schema_enforced is False
        assert result.resolved_model == "claude-haiku-4-5-20251001"

    def test_max_tokens_is_truncated_failure(self) -> None:
        result = call(Recorder(message(stop_reason="max_tokens")))
        assert result.ok is False
        assert result.truncated is True
        assert result.error.kind == "invalid_output"
        assert "not a vote" in result.error.message
        assert result.usage == {"prompt_tokens": 3, "completion_tokens": 5}

    def test_refusal_names_category_and_is_not_retryable(self) -> None:
        env = message(stop_reason="refusal",
                      stop_details={"type": "refusal", "category": "cyber", "explanation": "x"})
        result = call(Recorder(env))
        assert result.ok is False
        assert result.error.kind == "invalid_output"
        assert result.error.retryable is False
        assert "cyber" in result.error.message

    def test_refusal_without_stop_details_does_not_raise(self) -> None:
        result = call(Recorder(message(stop_reason="refusal")))
        assert result.ok is False and result.error.kind == "invalid_output"

    def test_no_json_is_invalid_output(self) -> None:
        result = call(Recorder(message([{"type": "text", "text": "no object here"}])))
        assert result.ok is False and result.error.kind == "invalid_output"

    def test_schema_enforced_false_everywhere(self) -> None:
        assert call(Recorder(message(stop_reason="max_tokens"))).schema_enforced is False


# ---------------------------------------------------------------------------
# usage -- THE line that makes the budget bite
# ---------------------------------------------------------------------------


@needs_sdk
class TestUsage:
    def test_all_three_input_fields_are_summed(self) -> None:
        env = message(usage={"input_tokens": 3, "output_tokens": 5,
                             "cache_creation_input_tokens": 7, "cache_read_input_tokens": 11})
        result = call(Recorder(env))
        assert result.usage == {"prompt_tokens": 21, "completion_tokens": 5}

    def test_only_input_and_output(self) -> None:
        result = call(Recorder(message(usage={"input_tokens": 12, "output_tokens": 4})))
        assert result.usage == {"prompt_tokens": 12, "completion_tokens": 4}

    def test_usage_absent_stays_none(self) -> None:
        """``None`` is how the engine tells "unmeasurable" from "free"."""
        result = call(Recorder(message(usage=None)))
        assert result.ok is True
        assert result.usage is None

    def test_NEGATIVE_anthropic_field_names_still_yield_nonzero_prompt_tokens(self) -> None:
        """A pass-through implementation (Anthropic's names copied through)
        reports zero ``prompt_tokens`` and MUST fail this test."""
        result = call(Recorder(message(usage={"input_tokens": 40, "output_tokens": 9})))
        assert "prompt_tokens" in result.usage
        assert result.usage["prompt_tokens"] > 0
        assert result.usage["completion_tokens"] > 0
        assert "input_tokens" not in result.usage

    def test_engines_own_token_total_is_nonzero(self) -> None:
        """Calls the REAL ``council.engine.worker._token_total`` -- the function
        the budget reads -- on the usage dict this backend produced."""
        result = call(Recorder(message(usage={"input_tokens": 40, "output_tokens": 9})))
        assert _token_total(result.usage) == 49
        cached = call(Recorder(message(usage={"input_tokens": 0, "output_tokens": 0,
                                              "cache_read_input_tokens": 30})))
        assert _token_total(cached.usage) == 30


# ---------------------------------------------------------------------------
# error taxonomy (one case per row, in the documented order)
# ---------------------------------------------------------------------------


TAXONOMY = [
    # id, status, body, kind, retryable, halts, rate_limited
    ("operator_spend_limit_400", 400,
     error_body("invalid_request_error",
                "You have reached your specified API usage limits. You will regain access on 2026-12-01."),
     "quota_exhausted", False, True, False),
    ("operator_spend_limit_400_case", 400,
     error_body("invalid_request_error", "YOU HAVE REACHED YOUR SPECIFIED API USAGE LIMITS."),
     "quota_exhausted", False, True, False),
    ("org_spend_cap_429", 429,
     error_body("rate_limit_error", "spend cap", "enforced_spend_limit_reached"),
     "quota_exhausted", False, True, False),
    ("auth_401", 401, error_body("authentication_error", "invalid x-api-key"), "auth", False, True, False),
    ("auth_403", 403, error_body("permission_error", "nope"), "auth", False, True, False),
    ("plain_429", 429, error_body("rate_limit_error", "slow down"), "transport", True, False, True),
    ("server_500", 500, error_body("api_error", "boom"), "transport", True, False, False),
    ("overloaded_529", 529, error_body("overloaded_error", "busy"), "transport", True, False, False),
    ("other_4xx_400", 400, error_body("invalid_request_error", "temperature is not supported"),
     "invalid_output", False, False, False),
    ("other_4xx_404", 404, error_body("not_found_error", "model: claude-nope"),
     "invalid_output", False, False, False),
]


@needs_sdk
class TestErrorTaxonomy:
    @pytest.mark.parametrize("name,status,body,kind,retryable,halts,rate_limited", TAXONOMY,
                             ids=[t[0] for t in TAXONOMY])
    def test_row(self, name: str, status: int, body: Dict[str, Any], kind: str,
                 retryable: bool, halts: bool, rate_limited: bool) -> None:
        recorder = Recorder(body, status=status)
        result = call(recorder)
        assert result.ok is False
        assert result.error.kind == kind
        assert result.error.retryable is retryable
        assert result.error.halts_endpoint is halts
        assert result.error.rate_limited is rate_limited
        assert len(recorder.requests) == 1
        assert KEY not in result.error.message

    def test_operator_limit_quotes_the_matched_prefix(self) -> None:
        body = error_body("invalid_request_error", "You have reached your specified API usage limits.")
        result = call(Recorder(body, status=400))
        assert "You have reached your specified API usage limits" in result.error.message

    def test_connection_failure_is_transport(self) -> None:
        import httpx2

        def refuse(request: Any) -> Any:
            raise httpx2.ConnectError("refused", request=request)

        result = call(Recorder(refuse))
        assert result.ok is False
        assert result.error.kind == "transport" and result.error.retryable is True
        assert result.error.rate_limited is False

    def test_deadline_is_timeout(self) -> None:
        import httpx2

        def slow(request: Any) -> Any:
            raise httpx2.ReadTimeout("slow", request=request)

        result = call(Recorder(slow))
        assert result.ok is False and result.error.kind == "timeout"


# ---------------------------------------------------------------------------
# one attempt per call
# ---------------------------------------------------------------------------


@needs_sdk
class TestNoSdkRetries:
    def test_constructed_client_has_retries_disabled(self) -> None:
        """Inspects the CONSTRUCTED client object, not the source line."""
        b = backend()
        try:
            assert b.client is not None
            assert b.client.max_retries == 0
        finally:
            run(b.aclose())

    def test_client_built_lazily_also_has_retries_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A key exported AFTER construction builds the client on first call;
        that client must be one-attempt too."""
        monkeypatch.delenv("KBU_TEST_ANTHROPIC_KEY", raising=False)
        recorder = Recorder(error_body("rate_limit_error", "slow"), status=429)
        b = backend(recorder, api_key=None, api_key_env="KBU_TEST_ANTHROPIC_KEY")
        assert b.client is None
        monkeypatch.setenv("KBU_TEST_ANTHROPIC_KEY", KEY)
        try:
            result = run(b.complete(req()))
            assert result.error.rate_limited is True
            assert b.client is not None and b.client.max_retries == 0
            assert len(recorder.requests) == 1
        finally:
            run(b.aclose())

    def test_a_429_is_exactly_one_upstream_request(self) -> None:
        recorder = Recorder(error_body("rate_limit_error", "cap", "enforced_spend_limit_reached"), status=429)
        call(recorder)
        assert len(recorder.requests) == 1

    def test_a_plain_429_and_a_500_are_one_request_each(self) -> None:
        for status in (429, 500):
            recorder = Recorder(error_body("api_error", "x"), status=status)
            call(recorder)
            assert len(recorder.requests) == 1, status

    def test_models_listing_is_one_attempt(self) -> None:
        recorder = Recorder(error_body("api_error", "x"), status=500)
        b = backend(recorder)
        try:
            assert run(b.models()) == []
        finally:
            run(b.aclose())
        assert len(recorder.requests) == 1


# ---------------------------------------------------------------------------
# request construction
# ---------------------------------------------------------------------------


@needs_sdk
class TestRequest:
    def test_body_shape(self) -> None:
        recorder = Recorder(message())
        call(recorder, req(system="be terse", max_output_tokens=777, model="claude-sonnet-4-6"))
        (body,) = recorder.bodies()
        assert body["model"] == "claude-sonnet-4-6"
        assert body["max_tokens"] == 777
        assert body["system"] == "be terse"
        assert body["messages"] == [{"role": "user", "content": "Pick one."}]
        assert "thinking" not in body
        assert "output_config" not in body
        assert "temperature" not in body
        assert recorder.requests[0].url.path == "/v1/messages"

    def test_empty_system_is_omitted(self) -> None:
        recorder = Recorder(message())
        call(recorder, req(system=""))
        assert "system" not in recorder.bodies()[0]

    def test_api_key_is_sent_as_header_only(self) -> None:
        recorder = Recorder(message())
        call(recorder)
        # SDK 2.3.0 sends the key as ``Authorization: Bearer`` (phase-1 record).
        assert recorder.requests[0].headers["authorization"] == f"Bearer {KEY}"
        assert KEY not in recorder.requests[0].content.decode()

    def test_temperature_none_is_not_sent(self) -> None:
        recorder = Recorder(message())
        call(recorder, req(temperature=None))
        assert "temperature" not in recorder.bodies()[0]

    def test_direct_caller_field_default_zero_is_sent(self) -> None:
        """PRD Q4: the rule is "send iff ``req.temperature is not None``".  A
        DIRECT ``LLMRequest`` caller who leaves the field default (0) gets 0
        sent -- every backend's pre-existing behaviour, deliberately not
        changed here.  The council's omitted case reaches the backend as
        ``None`` through ``_seat`` (see test_anthropic_config's end-to-end
        sampling test)."""
        recorder = Recorder(message())
        call(recorder, LLMRequest(model="claude-sonnet-5", user="q"))
        assert recorder.bodies()[0]["temperature"] == 0

    def test_explicit_zero_is_sent(self) -> None:
        recorder = Recorder(message())
        call(recorder, req(temperature=0))
        assert recorder.bodies()[0]["temperature"] == 0

    def test_explicit_nonzero_is_sent(self) -> None:
        recorder = Recorder(message())
        call(recorder, req(temperature=0.3))
        assert recorder.bodies()[0]["temperature"] == 0.3

    def test_top_p_top_k_sent_only_when_not_none(self) -> None:
        recorder = Recorder(message())
        call(recorder, req(params={"top_p": None, "top_k": 5}))
        body = recorder.bodies()[0]
        assert "top_p" not in body
        assert body["top_k"] == 5

    def test_extras_merge_and_drop_non_params(self) -> None:
        recorder = Recorder(message())
        call(recorder, req(params={"metadata": {"user_id": "u"}, "web_search": False,
                                   "extra_body": {"service_tier": "auto"}}),
             extra_body={"stop_sequences": ["END"]})
        body = recorder.bodies()[0]
        assert body["metadata"] == {"user_id": "u"}
        assert body["service_tier"] == "auto"
        assert body["stop_sequences"] == ["END"]
        assert "web_search" not in body and "extra_body" not in body

    def test_operator_set_thinking_and_output_config_pass_through(self) -> None:
        """Not requested by default, but NOT refused (PRD): only cache_control
        is refused.  An operator may set these knowingly."""
        recorder = Recorder(message())
        extras = {"thinking": {"type": "enabled", "budget_tokens": 1024},
                  "output_config": {"format": {"type": "json_schema", "schema": {}}}}
        result = call(recorder, req(params=extras))
        assert result.ok is True
        body = recorder.bodies()[0]
        assert body["thinking"] == extras["thinking"]
        assert body["output_config"] == extras["output_config"]
        assert result.schema_enforced is False  # documented as inaccurate in this case

    @pytest.mark.parametrize("key", ["model", "messages", "max_tokens", "stream", "tools"])
    def test_reserved_and_forbidden_extras_refused_unsent(self, key: str) -> None:
        recorder = Recorder(message())
        result = call(recorder, req(params={key: "x"}))
        assert result.ok is False and result.error.kind == "invalid_output"
        assert key in result.error.message
        assert recorder.requests == []

    def test_reserved_key_in_endpoint_extra_body_refused(self) -> None:
        recorder = Recorder(message())
        result = call(recorder, extra_body={"model": "claude-opus-5"})
        assert result.ok is False and recorder.requests == []

    @pytest.mark.parametrize("where", ["top", "nested_params", "endpoint_nested"])
    def test_cache_control_refused_recursively_unsent(self, where: str) -> None:
        recorder = Recorder(message())
        if where == "top":
            result = call(recorder, req(params={"cache_control": {"type": "ephemeral"}}))
        elif where == "nested_params":
            result = call(recorder, req(params={"metadata": {"deep": [{"cache_control": {}}]}}))
        else:
            result = call(recorder, extra_body={"x": {"y": {"cache_control": {"type": "ephemeral"}}}})
        assert result.ok is False
        assert result.error.kind == "invalid_output"
        assert "cache_control" in result.error.message
        assert recorder.requests == []

    def test_web_search_refused_unsent(self) -> None:
        recorder = Recorder(message())
        result = call(recorder, req(params={"web_search": True}))
        assert result.ok is False and recorder.requests == []

    def test_proxy_reaches_the_http_client_config(self) -> None:
        b = backend(proxy="socks5://127.0.0.1:1080")
        try:
            assert b.http_client_kwargs["proxy"] == "socks5://127.0.0.1:1080"
        finally:
            run(b.aclose())

    def test_ratelimit_headers_recorded(self) -> None:
        recorder = Recorder(message(), headers=RATE_HEADERS)
        b = backend(recorder)
        try:
            run(b.complete(req()))
            assert b.last_ratelimit["anthropic-ratelimit-requests-limit"] == "50"
            assert b.last_ratelimit["anthropic-ratelimit-input-tokens-limit"] == "30000"
        finally:
            run(b.aclose())


# ---------------------------------------------------------------------------
# effective_params
# ---------------------------------------------------------------------------


@needs_sdk
class TestEffectiveParams:
    def test_no_temperature_by_default(self) -> None:
        b = backend()
        try:
            assert "temperature" not in b.effective_params(req())  # temperature=None
            # a direct caller's field default (0) is sent, so it is reported
            assert b.effective_params(LLMRequest(model="m"))["temperature"] == 0
            assert b.effective_params(req(temperature=0))["temperature"] == 0
            assert b.effective_params(req(temperature=0.7))["temperature"] == 0.7
        finally:
            run(b.aclose())

    def test_generation_only_and_deterministic(self) -> None:
        b = backend(proxy="socks5://127.0.0.1:1", extra_body={"top_k": 3})
        try:
            first = b.effective_params(req(max_output_tokens=99))
            assert first == b.effective_params(req(max_output_tokens=99))
            assert first == {"max_tokens": 99, "top_k": 3}
            assert KEY not in json.dumps(first)
        finally:
            run(b.aclose())


# ---------------------------------------------------------------------------
# never raises, credential handling, preflight, models
# ---------------------------------------------------------------------------


@needs_sdk
class TestContract:
    def test_wrongly_shaped_request_is_typed_failure(self) -> None:
        b = backend(Recorder(message()))
        try:
            result = run(b.complete(object()))
        finally:
            run(b.aclose())
        assert result.ok is False and result.error is not None

    def test_missing_key_is_auth_failure_unsent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KBU_TEST_ANTHROPIC_KEY", raising=False)
        recorder = Recorder(message())
        result = call(recorder, api_key=None, api_key_env="KBU_TEST_ANTHROPIC_KEY")
        assert result.ok is False
        assert result.error.kind == "auth" and result.error.halts_endpoint is True
        assert "KBU_TEST_ANTHROPIC_KEY" in result.error.message
        assert recorder.requests == []

    def test_no_EMPTY_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        b = backend(api_key=None)
        assert b.client is None
        assert run(b.preflight())["state"] == "refused"

    def test_preflight_names_source_not_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KBU_TEST_ANTHROPIC_KEY", KEY)
        b = backend(api_key=None, api_key_env="KBU_TEST_ANTHROPIC_KEY")
        try:
            report = run(b.preflight())
        finally:
            run(b.aclose())
        assert report["state"] == "ready"
        assert "KBU_TEST_ANTHROPIC_KEY" in json.dumps(report)
        assert KEY not in json.dumps(report)

    def test_preflight_refused_names_variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KBU_TEST_ANTHROPIC_KEY", raising=False)
        report = run(backend(api_key=None, api_key_env="KBU_TEST_ANTHROPIC_KEY").preflight())
        assert report["state"] == "refused"
        assert "KBU_TEST_ANTHROPIC_KEY" in report["detail"]

    def test_models_lists(self) -> None:
        listing = {"data": [{"type": "model", "id": "claude-haiku-4-5", "display_name": "Claude Haiku 4.5",
                             "created_at": "2025-10-01T00:00:00Z"}],
                   "has_more": False, "first_id": "claude-haiku-4-5", "last_id": "claude-haiku-4-5"}
        b = backend(Recorder(listing))
        try:
            models = run(b.models())
        finally:
            run(b.aclose())
        assert [m["id"] for m in models] == ["claude-haiku-4-5"]

    def test_models_without_key_is_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KBU_TEST_ANTHROPIC_KEY", raising=False)
        assert run(backend(api_key=None, api_key_env="KBU_TEST_ANTHROPIC_KEY").models()) == []


# ---------------------------------------------------------------------------
# SDK absent (simulated -- the package is NOT uninstalled)
# ---------------------------------------------------------------------------


class TestSdkAbsent:
    def test_import_kbutillib_works_and_construction_names_extra_and_python(self) -> None:
        code = textwrap.dedent(
            """
            import sys
            sys.modules["anthropic"] = None  # simulate the package being absent
            import kbutillib
            from kbutillib.domains.ai import llm
            assert "anthropic" in llm.api_types()
            try:
                llm.get_api("anthropic", {"api_key": "x"})
            except ImportError as exc:
                print("IMPORT-ERROR:", exc)
            else:
                raise SystemExit("constructed without the SDK")
            """
        )
        done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
        assert done.returncode == 0, done.stderr
        out = done.stdout
        assert "IMPORT-ERROR:" in out
        assert "kbutillib[anthropic]" in out
        assert "Python >= 3.10" in out

    def test_in_process_simulated_absence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kbutillib.domains.ai.llm.apis.anthropic import AnthropicBackend

        monkeypatch.setitem(sys.modules, "anthropic", None)
        with pytest.raises(ImportError) as excinfo:
            AnthropicBackend({"api_key": "x"})
        assert "kbutillib[anthropic]" in str(excinfo.value)
        assert "Python >= 3.10" in str(excinfo.value)


# ---------------------------------------------------------------------------
# the shared cleaner is untouched (ARGO / openai behaviour byte-identical)
# ---------------------------------------------------------------------------


class TestSharedCleanerUnchanged:
    """``strip_think`` is shared by ARGO and every OpenAI-compatible endpoint.
    The anthropic backend has its own cleaner; these expected values are the
    base-commit (0bb8a88) behaviour and must not move."""

    CASES = [
        ("<think>a{b}</think>{\"x\": 1}", "{\"x\": 1}"),
        ("<THINK>q</THINK>r", "r"),
        ("<thinking>{\"x\": 0}</thinking>{\"x\": 1}", "<thinking>{\"x\": 0}</thinking>{\"x\": 1}"),
        ("plain", "plain"),
        ("<think>a</think>mid<think>b</think>end", "midend"),
    ]

    @pytest.mark.parametrize("raw,expected", CASES)
    def test_strip_think_is_base_behaviour(self, raw: str, expected: str) -> None:
        assert strip_think(raw) == expected

    def test_openai_backend_still_misparses_thinking_tag_like_base(self) -> None:
        # Base behaviour: the shared path does NOT strip <thinking>, so the
        # outermost-object rule spans both objects and the parse fails.
        assert extract_json_object(strip_think('<thinking>{"x": 0}</thinking>{"x": 1}')) is None
