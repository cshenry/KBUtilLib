"""Static non-secret request headers on an ``openai`` endpoint.

A gateway in front of several providers routes on a HEADER rather than on the
URL: the Genesis Model Access Gateway (MAG) is OpenAI-compatible and picks
which provider to forward to from ``X-AmSC-MAG-Provider``.  An endpoint
therefore declares ``options.headers``, and this module pins the four things
that makes true and the one thing it must not change:

* the declared header reaches the chat-completions request AND the ``/models``
  request;
* ``Authorization`` still comes only from ``api_key_env``, and a static header
  cannot displace it even on a config that never went through the loader;
* a credential-shaped header name is refused AT CONFIG LOAD, naming the
  endpoint, so no token is spent discovering it;
* a changed header changes the route fingerprint (through the existing
  ``options`` mechanism -- no second one was added); and
* an endpoint that declares no headers sends a byte-identical header dict to
  the one it sent before this feature existed, as does ARGO.

Every on-the-wire test drives the REAL
:class:`~kbutillib.domains.ai.llm.apis.http.OpenAICompatibleBackend` through
``llm.acomplete`` / ``llm.models``, with only :class:`httpx.MockTransport`
substituted, and loads configuration through the real
:func:`~kbutillib.domains.ai.council.config.load_council_config`.  Nothing
here makes a network call or spends a token.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from typing import Any, Callable, Dict, List, Set, Union

import httpx
import pytest

from kbutillib.domains.ai import llm
from kbutillib.domains.ai.council.config import load_council_config
from kbutillib.domains.ai.council.types import ConfigError, SeatSpec, route_fingerprint
from kbutillib.domains.ai.llm.apis.http import ArgoBackend, OpenAICompatibleBackend

MAG_BASE = "https://mag.genesis.americansciencecloud.org/v1"
PROVIDER_HEADER = "X-AmSC-MAG-Provider"

#: A schema-free but well-formed answer object; the backend only needs to find
#: ONE JSON object in the content to report ``ok=True``.
ANSWER: Dict[str, Any] = {"picked": ["A:1"], "why": "because"}

#: Unique api-type names, so one test's registration can never be handed to
#: another through ``llm.get_api``'s cache.
_NAMES = itertools.count(1)


def run(coro: Any) -> Any:
    """Run one coroutine on a loop owned by this call.

    Deliberately NOT ``asyncio.run``: that leaves the thread with no current
    event loop, after which the deprecated ``asyncio.get_event_loop()``
    raises.  Other modules in this suite still call it and ``tests/domains``
    collects first, so ``asyncio.run`` here would break them from a distance
    -- the same helper and the same reason as ``test_llm_wrapper.run``.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class Recorder:
    """A :class:`httpx.MockTransport` handler that records every request."""

    def __init__(
        self,
        reply: Union[httpx.Response, Callable[[httpx.Request], httpx.Response]],
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
        return httpx.MockTransport(self)

    @property
    def log(self) -> List[str]:
        return [f"{r.method} {r.url.path}" for r in self.requests]


def chat_response(content: str) -> httpx.Response:
    """An OpenAI-shaped chat-completions reply carrying *content*."""
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-1",
            "model": "served-model-name",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        },
    )


def models_response() -> httpx.Response:
    return httpx.Response(200, json={"data": [{"id": "some-model"}]})


def reply_to(request: httpx.Request) -> httpx.Response:
    """Answer ``/models`` with a listing and anything else with an answer."""
    if request.url.path.endswith("/models"):
        return models_response()
    return chat_response(json.dumps(ANSWER))


def register_openai(recorder: Recorder) -> str:
    """Register the REAL ``OpenAICompatibleBackend`` under a fresh api-type name.

    The only thing substituted is the transport.  The class, its client
    construction, its header building, its body building and its parsing are
    all the shipped ones, which is what makes a passing assertion here
    evidence about the product rather than about a stub.
    """
    name = f"openai-under-test-{next(_NAMES)}"
    transport = recorder.transport
    llm.register_api_type(
        name, lambda endpoint: OpenAICompatibleBackend(endpoint, transport=transport)
    )
    return name


def httpx_own_header_names(method: str) -> Set[str]:
    """Header names ``httpx`` itself puts on a request of *method*.

    Computed rather than hard-coded: the set differs between httpx releases
    (``Connection`` came and went), and a hard-coded list would turn an
    upgrade into a false regression in the byte-identity test below.
    """
    recorder = Recorder(httpx.Response(200, json={}))

    async def _go() -> None:
        async with httpx.AsyncClient(transport=recorder.transport) as client:
            if method == "POST":
                await client.post("https://example.invalid/v1/x", json={"a": 1})
            else:
                await client.get("https://example.invalid/v1/x")

    run(_go())
    return {name.lower() for name in recorder.requests[-1].headers.keys()}


def backend_contributed(request: httpx.Request) -> Dict[str, str]:
    """The headers the BACKEND added to *request*, with httpx's own removed."""
    own = httpx_own_header_names(request.method)
    return {
        name.lower(): value
        for name, value in request.headers.items()
        if name.lower() not in own
    }


def endpoint_config(**extra: Any) -> Dict[str, Any]:
    """A MAG-shaped endpoint mapping, as ``CouncilConfig.endpoint`` returns one."""
    cfg: Dict[str, Any] = {
        "name": "mag",
        "kind": "openai",
        "base_url": MAG_BASE,
        "api_key_env": "KBU_TEST_AMSC_TOKEN",
    }
    cfg.update(extra)
    return cfg


def config_dict(**endpoint_extra: Any) -> Dict[str, Any]:
    """A small but complete native-format config whose ``mag`` endpoint is seated."""
    mag: Dict[str, Any] = {
        "kind": "openai",
        "base_url": MAG_BASE,
        "api_key_env": "KBU_TEST_AMSC_TOKEN",
    }
    mag.update(endpoint_extra)
    return {
        "endpoints": {
            "argo": {"kind": "argo", "base_url": "https://argo.example/v1"},
            "mag": mag,
        },
        "models": {
            "a": {"endpoint": "argo", "canonical": "vendor/a"},
            "b": {"endpoint": "mag", "canonical": "vendor/b"},
            "c": {"endpoint": "mag", "canonical": "vendor/c"},
            "d": {"endpoint": "mag", "canonical": "vendor/d"},
        },
        "profiles": {"p": {"proposers": ["a", "b"], "critic": "c", "meta": "d"}},
        "default_profile": "p",
        "store": "sqlite:///council-test.sqlite",
    }


def seat(**overrides: Any) -> SeatSpec:
    fields: Dict[str, Any] = {
        "seat_name": "proposer_1",
        "endpoint": "mag",
        "endpoint_kind": "openai",
        "model": "some-model",
        "canonical_model": "vendor/b",
    }
    fields.update(overrides)
    return SeatSpec(**fields)


@pytest.fixture(autouse=True)
def _close_clients():
    """Release every client this module's registrations built."""
    yield
    run(llm.aclose_all())


# ---------------------------------------------------------------------------
# On the wire
# ---------------------------------------------------------------------------


class TestTheHeaderTravels:
    """The declared header is on every request this endpoint makes."""

    def test_it_reaches_the_chat_completions_request(self, monkeypatch):
        monkeypatch.setenv("KBU_TEST_AMSC_TOKEN", "amsc-secret-value")
        recorder = Recorder(reply_to)
        api_type = register_openai(recorder)
        endpoint = endpoint_config(options={"headers": {PROVIDER_HEADER: "anthropic"}})

        result = run(
            llm.acomplete(
                llm.LLMRequest(model="some-model", user="pick one"),
                api_type=api_type,
                endpoint=endpoint,
            )
        )

        assert result.ok, result.error
        assert recorder.log == ["POST /v1/chat/completions"]
        assert recorder.requests[-1].headers[PROVIDER_HEADER] == "anthropic"

    def test_it_reaches_the_models_request(self, monkeypatch):
        monkeypatch.setenv("KBU_TEST_AMSC_TOKEN", "amsc-secret-value")
        recorder = Recorder(reply_to)
        api_type = register_openai(recorder)
        endpoint = endpoint_config(options={"headers": {PROVIDER_HEADER: "anthropic"}})

        listing = run(llm.models(api_type, endpoint))

        assert listing == [{"id": "some-model"}]
        assert recorder.log == ["GET /v1/models"]
        assert recorder.requests[-1].headers[PROVIDER_HEADER] == "anthropic"

    def test_every_declared_header_travels_not_just_the_first(self, monkeypatch):
        monkeypatch.setenv("KBU_TEST_AMSC_TOKEN", "amsc-secret-value")
        recorder = Recorder(reply_to)
        api_type = register_openai(recorder)
        endpoint = endpoint_config(
            options={
                "headers": {
                    PROVIDER_HEADER: "anthropic",
                    "X-AmSC-Project": "genesis",
                    "X-Trace-Origin": "council",
                }
            }
        )

        run(
            llm.acomplete(
                llm.LLMRequest(model="some-model", user="pick one"),
                api_type=api_type,
                endpoint=endpoint,
            )
        )

        sent = recorder.requests[-1].headers
        assert sent[PROVIDER_HEADER] == "anthropic"
        assert sent["X-AmSC-Project"] == "genesis"
        assert sent["X-Trace-Origin"] == "council"


class TestTheCredentialStaysItsOwnChannel:
    """``Authorization`` comes from ``api_key_env`` and from nowhere else."""

    def test_authorization_is_the_value_of_the_named_environment_variable(
        self, monkeypatch
    ):
        monkeypatch.setenv("KBU_TEST_AMSC_TOKEN", "amsc-secret-value")
        recorder = Recorder(reply_to)
        api_type = register_openai(recorder)
        endpoint = endpoint_config(options={"headers": {PROVIDER_HEADER: "anthropic"}})

        run(
            llm.acomplete(
                llm.LLMRequest(model="some-model", user="pick one"),
                api_type=api_type,
                endpoint=endpoint,
            )
        )

        assert (
            recorder.requests[-1].headers["Authorization"] == "Bearer amsc-secret-value"
        )

    def test_static_headers_add_nothing_but_themselves(self, monkeypatch):
        """The header dict is the old one PLUS the declared header, exactly."""
        monkeypatch.setenv("KBU_TEST_AMSC_TOKEN", "amsc-secret-value")
        recorder = Recorder(reply_to)
        api_type = register_openai(recorder)
        endpoint = endpoint_config(options={"headers": {PROVIDER_HEADER: "anthropic"}})

        run(
            llm.acomplete(
                llm.LLMRequest(model="some-model", user="pick one"),
                api_type=api_type,
                endpoint=endpoint,
            )
        )

        assert backend_contributed(recorder.requests[-1]) == {
            "authorization": "Bearer amsc-secret-value",
            PROVIDER_HEADER.lower(): "anthropic",
        }

    def test_a_hand_built_config_cannot_displace_the_credential(self, monkeypatch):
        """Belt and braces for a config that never went through the loader.

        ``load_council_config`` refuses an ``Authorization`` entry outright
        (see :class:`TestConfigLoadRefusals`), but a caller may build an
        endpoint mapping by hand and hand it to ``llm.acomplete``.  The merge
        order in ``HttpBackend._headers`` -- static headers BEFORE the
        credential -- is what makes that harmless, and this is the test of it.
        """
        monkeypatch.setenv("KBU_TEST_AMSC_TOKEN", "amsc-secret-value")
        recorder = Recorder(reply_to)
        api_type = register_openai(recorder)
        endpoint = endpoint_config(
            options={"headers": {"Authorization": "Bearer attacker-token"}}
        )

        run(
            llm.acomplete(
                llm.LLMRequest(model="some-model", user="pick one"),
                api_type=api_type,
                endpoint=endpoint,
            )
        )

        sent = recorder.requests[-1].headers
        assert sent["Authorization"] == "Bearer amsc-secret-value"
        assert "attacker-token" not in sent.get("Authorization", "")

    def test_the_header_value_is_never_mistaken_for_the_credential(self, monkeypatch):
        """No ``api_key_env`` set -> vLLM's ``EMPTY``, and the header still travels."""
        monkeypatch.delenv("KBU_TEST_AMSC_TOKEN", raising=False)
        recorder = Recorder(reply_to)
        api_type = register_openai(recorder)
        endpoint = endpoint_config(options={"headers": {PROVIDER_HEADER: "anthropic"}})

        run(
            llm.acomplete(
                llm.LLMRequest(model="some-model", user="pick one"),
                api_type=api_type,
                endpoint=endpoint,
            )
        )

        sent = recorder.requests[-1].headers
        assert sent["Authorization"] == "Bearer EMPTY"
        assert sent[PROVIDER_HEADER] == "anthropic"


# ---------------------------------------------------------------------------
# Nothing changed for anyone who did not ask for it
# ---------------------------------------------------------------------------


class TestByteIdenticalWithoutOptions:
    """An endpoint with no ``options.headers`` sends what it sent before."""

    def test_the_wire_carries_exactly_content_type_and_authorization(
        self, monkeypatch
    ):
        monkeypatch.setenv("KBU_TEST_AMSC_TOKEN", "amsc-secret-value")
        recorder = Recorder(reply_to)
        api_type = register_openai(recorder)

        run(
            llm.acomplete(
                llm.LLMRequest(model="some-model", user="pick one"),
                api_type=api_type,
                endpoint=endpoint_config(),
            )
        )
        run(llm.models(api_type, endpoint_config()))

        assert recorder.log == ["POST /v1/chat/completions", "GET /v1/models"]
        for request in recorder.requests:
            # Nothing beyond httpx's own headers and the backend's two. (The
            # backend sets Content-Type on the GET as well, which httpx would
            # not -- that is pre-existing and deliberately preserved.)
            allowed = httpx_own_header_names(request.method) | {
                "content-type",
                "authorization",
            }
            assert {name.lower() for name in request.headers} <= allowed
            assert request.headers["Content-Type"] == "application/json"
            assert request.headers["Authorization"] == "Bearer amsc-secret-value"

    def test_the_header_dict_is_literally_the_pre_change_one(self):
        """The exact dict, not a superset: two keys, in this order."""
        backend = OpenAICompatibleBackend(endpoint_config())
        try:
            built = backend._headers("some-token")
        finally:
            run(backend.aclose())
        assert built == {
            "Content-Type": "application/json",
            "Authorization": "Bearer some-token",
        }
        assert list(built) == ["Content-Type", "Authorization"]

    def test_an_empty_headers_mapping_changes_nothing(self):
        backend = OpenAICompatibleBackend(endpoint_config(options={"headers": {}}))
        try:
            built = backend._headers("some-token")
        finally:
            run(backend.aclose())
        assert built == {
            "Content-Type": "application/json",
            "Authorization": "Bearer some-token",
        }

    def test_argo_ignores_options_headers_entirely(self):
        """ARGO's requests are untouched, even if an options block is present.

        Only the ``openai`` kind opts in; ARGO's header dict must stay
        byte-identical, because a gateway that never asked for a routing
        header should not start receiving one.
        """
        backend = ArgoBackend(
            {
                "name": "argo",
                "kind": "argo",
                "base_url": "https://argo.example/v1",
                "options": {"headers": {PROVIDER_HEADER: "anthropic"}},
            }
        )
        try:
            built = backend._headers("someuser")
        finally:
            run(backend.aclose())
        assert built == {
            "Content-Type": "application/json",
            "Authorization": "Bearer someuser",
        }


# ---------------------------------------------------------------------------
# Config load
# ---------------------------------------------------------------------------


class TestConfigLoadRefusals:
    """A credential never travels in ``options.headers``, and the load says so."""

    @pytest.mark.parametrize(
        "name",
        [
            "Authorization",
            "authorization",
            "Proxy-Authorization",
            "proxy-authorization",
            "Cookie",
            "cookie",
            "X-Api-Key",
            "x-api-key",
            "Api-Key",
            "api-key",
        ],
    )
    def test_each_refused_name_fails_the_load(self, name):
        raw = config_dict(options={"headers": {name: "whatever"}})
        with pytest.raises(ConfigError, match="credential header"):
            load_council_config(raw)

    @pytest.mark.parametrize(
        "name",
        ["X-Gateway-Token", "X-Shared-Secret", "X-User-Password", "X-Signing-Key"],
    )
    def test_a_name_containing_a_credential_word_fails_the_load(self, name):
        raw = config_dict(options={"headers": {name: "whatever"}})
        with pytest.raises(ConfigError, match="credential header"):
            load_council_config(raw)

    def test_the_error_names_the_endpoint(self):
        raw = config_dict(options={"headers": {"Authorization": "Bearer x"}})
        with pytest.raises(ConfigError, match="endpoint 'mag'"):
            load_council_config(raw)

    def test_a_malformed_header_name_fails_the_load(self):
        raw = config_dict(options={"headers": {"X-Bad Name": "v"}})
        with pytest.raises(ConfigError, match="not a valid HTTP header name"):
            load_council_config(raw)

    def test_a_non_string_value_fails_the_load(self):
        raw = config_dict(options={"headers": {"X-Retries": 3}})
        with pytest.raises(ConfigError, match="must be a string"):
            load_council_config(raw)

    def test_headers_that_are_not_a_mapping_fail_the_load(self):
        raw = config_dict(options={"headers": ["X-AmSC-MAG-Provider: anthropic"]})
        with pytest.raises(ConfigError, match="must be a mapping"):
            load_council_config(raw)

    def test_a_plain_routing_header_loads_and_is_preserved_verbatim(self):
        raw = config_dict(options={"headers": {PROVIDER_HEADER: "anthropic"}})
        cfg = load_council_config(raw)
        assert cfg.endpoint("mag")["options"] == {"headers": {PROVIDER_HEADER: "anthropic"}}

    def test_the_refusal_also_applies_to_a_non_openai_endpoint(self):
        """The rule is "no credential in options", wherever it was written.

        ``argo`` does not SEND static headers, but a secret written there
        would still reach the store's config snapshot and the route
        fingerprint, so it is refused at load for every kind.
        """
        raw = config_dict()
        raw["endpoints"]["argo"]["options"] = {"headers": {"X-Api-Key": "abc"}}
        with pytest.raises(ConfigError, match="endpoint 'argo'"):
            load_council_config(raw)

    def test_a_credential_VALUE_under_a_header_name_is_still_refused(self):
        """The existing value heuristic covers what the name rule does not.

        ``X-Forwarded-Auth`` is not on the refused list, but a value shaped
        like a live key is refused by ``_refuse_credential_values``, which
        already walks ``options``.
        """
        raw = config_dict(
            options={"headers": {"X-Forwarded-Auth": "sk-" + "a" * 40}}
        )
        with pytest.raises(ConfigError, match="live credential"):
            load_council_config(raw)


# ---------------------------------------------------------------------------
# Identity of the route
# ---------------------------------------------------------------------------


class TestRouteFingerprint:
    """Headers change WHERE a call lands, so they change the route's identity."""

    def test_a_header_change_changes_the_fingerprint(self):
        anthropic = route_fingerprint(
            seat(), endpoint_config(options={"headers": {PROVIDER_HEADER: "anthropic"}})
        )
        openai = route_fingerprint(
            seat(), endpoint_config(options={"headers": {PROVIDER_HEADER: "openai"}})
        )
        assert anthropic != openai

    def test_adding_a_header_changes_the_fingerprint(self):
        without = route_fingerprint(seat(), endpoint_config())
        with_header = route_fingerprint(
            seat(), endpoint_config(options={"headers": {PROVIDER_HEADER: "anthropic"}})
        )
        assert without != with_header

    def test_an_endpoint_with_no_options_keeps_the_fingerprint_it_had(self):
        """No second mechanism was added, so a header-free endpoint is unmoved.

        ``route_fingerprint`` adds ``options`` only when there ARE options,
        precisely so every endpoint configured before this feature keeps the
        fingerprint already stored on its call rows.
        """
        assert route_fingerprint(seat(), endpoint_config()) == route_fingerprint(
            seat(), endpoint_config(options={})
        )

    def test_the_same_headers_fingerprint_identically(self):
        first = route_fingerprint(
            seat(),
            endpoint_config(
                options={"headers": {PROVIDER_HEADER: "anthropic", "X-P": "g"}}
            ),
        )
        second = route_fingerprint(
            seat(),
            endpoint_config(
                options={"headers": {"X-P": "g", PROVIDER_HEADER: "anthropic"}}
            ),
        )
        assert first == second

    def test_headers_stay_out_of_effective_params(self):
        """``effective_params`` is generation parameters ONLY, as it documents.

        The headers enter the fingerprint through ``options``, which is the
        existing route-identity channel; putting them in ``effective_params``
        as well would change every prompt hash and break answer reuse.
        """
        backend = OpenAICompatibleBackend(
            endpoint_config(options={"headers": {PROVIDER_HEADER: "anthropic"}})
        )
        try:
            params = backend.effective_params(
                llm.LLMRequest(model="some-model", user="x")
            )
        finally:
            run(backend.aclose())
        assert "headers" not in json.dumps(params)


# ---------------------------------------------------------------------------
# The packaged example
# ---------------------------------------------------------------------------


class TestPackagedExample:
    """The MAG example in ``default_config.yaml`` is commented and loadable."""

    def test_the_default_config_declares_no_mag_endpoint(self):
        """Commented, not active: nothing routes through MAG until a user says so."""
        cfg = load_council_config()
        assert "mag" not in cfg.endpoints

    def test_the_example_is_present_and_names_no_model_ids(self):
        from pathlib import Path

        import kbutillib.domains.ai.council as council

        text = Path(council.__file__).with_name("default_config.yaml").read_text()
        example = text[text.index("# mag:") :]
        assert "https://mag.genesis.americansciencecloud.org/v1" in example
        assert "api_key_env: AMSC_TOKEN" in example
        assert f"{PROVIDER_HEADER}: <provider>" in example
        # The provider value and the model ids are portal facts this repo
        # cannot verify, so the example must not invent either.
        assert "<provider>" in example
        assert "MyAmSC" in example

    def test_the_example_loads_once_uncommented(self):
        """Uncommenting it yields a valid endpoint -- the example is not aspirational."""
        raw = config_dict(
            options={"headers": {PROVIDER_HEADER: "<provider>"}}
        )
        cfg = load_council_config(raw)
        endpoint = cfg.endpoint("mag")
        assert endpoint["base_url"] == MAG_BASE
        assert endpoint["api_key_env"] == "KBU_TEST_AMSC_TOKEN"
        assert endpoint["options"]["headers"][PROVIDER_HEADER] == "<provider>"
