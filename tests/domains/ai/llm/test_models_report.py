"""``models_report()``: WHY a listing is what it is.

``HttpBackend`` is driven over :class:`httpx.MockTransport`.
``AnthropicBackend`` is driven with a stand-in SDK module (the real SDK is an
optional extra and is not installed everywhere), so its exception mapping is
tested without it.  ``models()`` must return exactly what it returned before
``models_report`` existed; the expected lists below are what the old
implementation produced for each body.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

import httpx
import pytest

from kbutillib.domains.ai.llm.apis import anthropic as anthropic_mod
from kbutillib.domains.ai.llm.apis.anthropic import AnthropicBackend
from kbutillib.domains.ai.llm.apis.http import ArgoBackend, OpenAICompatibleBackend
from kbutillib.domains.ai.llm.apis.http_common import LISTING_OUTCOMES

SECRET = "sk-listing-SECRET-0123456789"


def run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def openai(handler: Callable[[httpx.Request], httpx.Response], **cfg: Any) -> OpenAICompatibleBackend:
    config = {"name": "t", "kind": "openai", "base_url": "http://127.0.0.1:8301/v1",
              "api_key": SECRET}
    config.update(cfg)
    return OpenAICompatibleBackend(config, transport=httpx.MockTransport(handler))


def both(backend: Any) -> Any:
    async def _once() -> Any:
        try:
            report = await backend.models_report()
            models = await backend.models()
            return report, models
        finally:
            await backend.aclose()

    return run(_once())


def reply(status: int, body: Any = None, *, raw: Optional[bytes] = None) -> Callable[..., httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if raw is not None:
            return httpx.Response(status, content=raw)
        return httpx.Response(status, json=body if body is not None else {})
    return handler


def boom(exc: Exception) -> Callable[..., httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc
    return handler


ROSTER = {"data": [{"id": "gpt-oss-120b", "object": "model"}, {"id": "other"}]}
ROSTER_LIST = [{"id": "a"}, {"id": "b"}]


@pytest.mark.parametrize(
    "handler, outcome, models",
    [
        (reply(401, {"error": "no"}), "credential_rejected", []),
        (reply(403, {"error": "no"}), "credential_rejected", []),
        (reply(404, {"error": "no"}), "not_implemented", []),
        (reply(405, {"error": "no"}), "not_implemented", []),
        (reply(429, {"error": "slow"}), "http_error", []),
        (reply(500, {"error": "boom"}), "http_error", []),
        (reply(503, {"error": "boom"}), "http_error", []),
        (boom(httpx.ConnectError("refused")), "unreachable", []),
        (boom(httpx.ReadTimeout("slow")), "unreachable", []),
        (boom(httpx.ConnectTimeout("slow")), "unreachable", []),
        (boom(httpx.RemoteProtocolError("garbled")), "malformed", []),
        (reply(200, raw=b"<html>not json"), "malformed", []),
        (reply(200, {"data": "nope"}), "malformed", []),
        (reply(200, {"nodata": []}), "malformed", []),
        (reply(200, {"data": [{"id": 7}, {"id": 8}]}), "malformed",
         [{"id": 7}, {"id": 8}]),
        (reply(200, {"data": [{"id": "ok"}, "stray", 3]}), "malformed", [{"id": "ok"}]),
        (reply(200, ROSTER), "answered", ROSTER["data"]),
        (reply(200, ROSTER_LIST), "answered", ROSTER_LIST),
        (reply(200, {"data": []}), "answered", []),
        (reply(200, []), "answered", []),
    ],
    ids=["401", "403", "404", "405", "429", "500", "503", "connect", "read-timeout",
         "connect-timeout", "protocol-error", "invalid-json", "data-not-list",
         "no-data-key", "non-string-leaves", "partially-malformed", "roster",
         "bare-list", "empty-roster", "empty-bare-list"],
)
def test_outcome_and_models_match(handler: Any, outcome: str, models: List[Dict[str, Any]]) -> None:
    report, listed = both(openai(handler))
    assert report["outcome"] == outcome
    assert outcome in LISTING_OUTCOMES
    # models() is the models-only view of the same call, and is what the
    # pre-change implementation returned.
    assert report["models"] == models
    assert listed == models
    assert isinstance(report["detail"], str) and report["detail"]
    assert SECRET not in json.dumps(report)


def test_token_failure_is_credential_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARGO_USER", raising=False)
    calls: List[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=ROSTER)

    backend = ArgoBackend({"name": "argo", "kind": "argo", "base_url": "http://x/v1",
                           "user_env": "ARGO_USER"}, transport=httpx.MockTransport(handler))
    report, listed = both(backend)
    assert report["outcome"] == "credential_rejected"
    assert report["models"] == [] and listed == []
    assert calls == []  # nothing was sent


def test_detail_redacts_a_credential_in_an_exception() -> None:
    report, _ = both(openai(boom(httpx.ConnectError(f"cannot reach, Bearer {SECRET}"))))
    assert report["outcome"] == "unreachable"
    assert SECRET not in report["detail"]


def test_report_never_raises_on_an_unexpected_failure() -> None:
    backend = openai(reply(200, ROSTER))

    async def _bad() -> Any:
        raise RuntimeError(f"surprise api_key={SECRET}")

    backend._models_report = _bad  # type: ignore[assignment]
    report, listed = both(backend)
    assert report["outcome"] == "malformed" and listed == []
    assert SECRET not in report["detail"]


# ---------------------------------------------------------------------------
# AnthropicBackend, over a stand-in SDK
# ---------------------------------------------------------------------------


class _APIError(Exception):
    pass


class _APIConnectionError(_APIError):
    pass


class _APITimeoutError(_APIConnectionError):
    pass


class _APIStatusError(_APIError):
    def __init__(self, status: int) -> None:
        super().__init__(f"status {status}: body quoting {SECRET}")
        self.status_code = status


class _Entry:
    def __init__(self, data: Dict[str, Any]) -> None:
        self._data = data

    def model_dump(self) -> Dict[str, Any]:
        return dict(self._data)


class _Models:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome

    async def list(self) -> Any:
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def _anthropic(monkeypatch: pytest.MonkeyPatch, outcome: Any, *,
               key: Optional[str] = SECRET) -> AnthropicBackend:
    sdk = SimpleNamespace(
        APIConnectionError=_APIConnectionError, APITimeoutError=_APITimeoutError,
        APIStatusError=_APIStatusError,
    )
    monkeypatch.setattr(anthropic_mod, "_import_sdk", lambda: sdk)
    monkeypatch.setattr(AnthropicBackend, "_build_client", lambda self, k: SimpleNamespace(
        models=_Models(outcome)))
    return AnthropicBackend({"api_key": key, "api_key_env": "KBU_LISTING_TEST_KEY"})


def _page(*ids: Any) -> Any:
    return SimpleNamespace(data=[_Entry({"id": i, "type": "model"}) for i in ids])


@pytest.mark.parametrize(
    "outcome, expected",
    [
        (_APIStatusError(404), "not_implemented"),
        (_APIStatusError(405), "not_implemented"),
        (_APIStatusError(401), "credential_rejected"),
        (_APIStatusError(403), "credential_rejected"),
        (_APIStatusError(429), "http_error"),
        (_APIStatusError(500), "http_error"),
        (_APIConnectionError("refused"), "unreachable"),
        (_APITimeoutError("slow"), "unreachable"),
        (ValueError("bad payload"), "malformed"),
        (SimpleNamespace(data=None), "malformed"),
        (SimpleNamespace(data=["stray"]), "malformed"),
        (_page(7), "malformed"),
        (_page("claude-haiku-4-5-20251001"), "answered"),
        (SimpleNamespace(data=[]), "answered"),
    ],
    ids=["404", "405", "401", "403", "429", "500", "connect", "timeout", "client-failure",
         "no-data", "stray-entry", "non-string-id", "roster", "empty-roster"],
)
def test_anthropic_mapping(monkeypatch: pytest.MonkeyPatch, outcome: Any, expected: str) -> None:
    backend = _anthropic(monkeypatch, outcome)
    report = run(backend.models_report())
    assert report["outcome"] == expected
    assert run(backend.models()) == report["models"]
    assert SECRET not in json.dumps(report)


def test_anthropic_models_is_the_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _anthropic(monkeypatch, _page("claude-haiku-4-5-20251001", "claude-opus-5"))
    assert [m["id"] for m in run(backend.models())] == [
        "claude-haiku-4-5-20251001", "claude-opus-5"]


def test_anthropic_no_key_is_credential_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KBU_LISTING_TEST_KEY", raising=False)
    backend = _anthropic(monkeypatch, _page("x"), key=None)
    report = run(backend.models_report())
    assert report["outcome"] == "credential_rejected" and report["models"] == []
    assert run(backend.models()) == []


# ---------------------------------------------------------------------------
# The rehearsal script's check 4
# ---------------------------------------------------------------------------


def _script() -> Any:
    from pathlib import Path

    path = Path(__file__).resolve().parents[4] / "scripts/council/anthropic_acceptance_rehearsal.py"
    spec = importlib.util.spec_from_file_location("anthropic_acceptance_rehearsal_t", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _Step:
    def __init__(self) -> None:
        self.findings: List[str] = []
        self.notes: List[str] = []

    def find(self, text: str) -> None:
        self.findings.append(text)

    def note(self, text: str) -> None:
        self.notes.append(text)


def _check4(monkeypatch: pytest.MonkeyPatch, outcome: Any, served: List[str]) -> _Step:
    backend = _anthropic(monkeypatch, outcome)
    step = _Step()
    run(_script().check_listing(step, "anthropic", backend, served))
    return step


def test_check4_credential_rejected_makes_no_naming_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    step = _check4(monkeypatch, _APIStatusError(401), ["claude-haiku-4-5"])
    assert len(step.findings) == 1
    assert "credential_rejected" in step.findings[0]
    assert "not in the model listing" not in step.findings[0]
    assert "no claim is made about model naming" in step.findings[0]


def test_check4_answered_without_the_model_emits_the_naming_finding(
        monkeypatch: pytest.MonkeyPatch) -> None:
    step = _check4(monkeypatch, _page("claude-opus-5"), ["claude-haiku-4-5"])
    assert len(step.findings) == 1 and "not in the model listing" in step.findings[0]


def test_check4_dated_snapshot_satisfies_the_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    step = _check4(monkeypatch, _page("claude-haiku-4-5-20251001"), ["claude-haiku-4-5"])
    assert step.findings == []
