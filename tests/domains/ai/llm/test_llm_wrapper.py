"""The one wrapper every LLM call goes through, and the registry behind it.

What this module pins, in the order the classes appear:

* the four built-in api types resolve BY NAME, and an unknown name fails
  loudly with the registered names in the message;
* registration refuses a duplicate, allows ``replace=True``, and refuses a
  factory whose product is not shaped like an api type;
* entry-point discovery happens LAZILY, and really reads a ``*.dist-info``
  directory placed on ``sys.path`` -- not a monkeypatched stand-in for one;
* ``complete()`` returns ``ok=False`` rather than raising, whatever the api
  type does;
* ``get_api`` caches per ``(api type, endpoint, event loop)``;
* a config naming an unregistered kind fails AT LOAD, and the round-3 rules
  about ``reuse_namespace``, ``options`` and credential-shaped values hold;
* a whole council job runs end to end through ``llm.acomplete`` **itself**
  against a real temp SQLite store, with no council module changed and no
  substituted callable; and
* no module under ``council/`` reaches into ``llm/apis`` directly.

Nothing here makes a network call, spends a token, needs a CLI login or needs
a GPU: every api type used is registered by the test itself.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional

import pytest

from kbutillib.domains.ai import llm
from kbutillib.domains.ai.llm.protocol import ApiType
from kbutillib.domains.ai.llm.types import LLMError, LLMRequest, LLMResult

# ---------------------------------------------------------------------------
# Scaffolding
# ---------------------------------------------------------------------------


def run(coro: Any) -> Any:
    """Run one coroutine on a loop owned by this call.

    Deliberately NOT ``asyncio.run``: that leaves the thread with no current
    event loop, after which the deprecated ``asyncio.get_event_loop()``
    raises.  Other modules in this suite still call it and ``tests/domains``
    collects first, so ``asyncio.run`` here would break them from a distance
    -- the same reasoning (and the same helper) as
    ``council/engine_support.run``.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


#: A schema-valid proposer answer in the engine's generic post-processed shape.
PROPOSER_ANSWER: Dict[str, Any] = {
    "picked": [{"id": "A:1", "confidence": "high", "why": "because A:1"}],
    "overall_confidence": "high",
    "no_functional_content": False,
    "notes": "",
}
CRITIC_ANSWER: Dict[str, Any] = {
    "verdict": "consistent",
    "reason": "looks right",
    "overpicked": [],
    "missed": [],
    "contentless_input": False,
}
META_ANSWER: Dict[str, Any] = {
    "final_ids": ["A:1"],
    "final_confidence": "high",
    "no_functional_content": False,
    "reasoning": "adjudicated",
}


class ScriptedApi:
    """A minimal, conforming api type: answers from the prompt it was given.

    Built from an endpoint mapping exactly as a registered factory's product
    is, and deliberately as small as the :class:`ApiType` contract allows --
    ``kind``, ``complete`` and ``effective_params``, no ``preflight``, no
    ``models``, no ``aclose`` -- so that every test using it also proves the
    wrapper copes with an api type that supplies only the required members.

    Which role a call is for is read out of the SYSTEM prompt, because an
    ``LLMRequest`` carries no role: that is the point of the wrapper being
    domain-neutral, and a fake that cheated by reading a council field would
    not prove the council's prompts survive the trip.
    """

    #: Every request every instance has ever been asked, in order.  A class
    #: attribute because ``get_api`` owns the instances, so a test cannot hold
    #: the object it wants to interrogate.
    seen: List[LLMRequest] = []

    def __init__(self, endpoint: Optional[Mapping[str, Any]] = None) -> None:
        self.kind = "scripted"
        self.endpoint = dict(endpoint or {})

    async def complete(self, request: LLMRequest) -> LLMResult:
        ScriptedApi.seen.append(request)
        system = (request.system or "").lower()
        if "critic" in system:
            answer = CRITIC_ANSWER
        elif "meta" in system:
            answer = META_ANSWER
        else:
            answer = PROPOSER_ANSWER
        return LLMResult(
            ok=True,
            text=json.dumps(answer, sort_keys=True),
            parsed=dict(answer),
            resolved_model=request.model,
            usage={"prompt_tokens": 0, "completion_tokens": 0},
            finish_reason="stop",
            schema_enforced=False,
        )

    def effective_params(self, request: Any) -> Dict[str, Any]:
        return llm.generation_params(request)


class FailingApi(ScriptedApi):
    """An api type that returns a typed failure instead of an answer."""

    async def complete(self, request: LLMRequest) -> LLMResult:
        return LLMResult(
            ok=False,
            error=LLMError(kind="transport", message="nothing listening", retryable=True),
        )


class RaisingApi(ScriptedApi):
    """An api type that BREAKS the contract and raises."""

    async def complete(self, request: LLMRequest) -> LLMResult:
        raise OSError("the socket went away")


@pytest.fixture(autouse=True)
def pristine_registry() -> Iterator[None]:
    """Leave the process-global registry and client cache as they were found.

    The registry is module state by design (a plug-in registers once per
    process), so a test that adds to it would otherwise be visible to every
    later test in the session -- including the "unknown name names the
    registered types" assertion, which reads the whole set.
    """
    saved_registry = dict(llm._REGISTRY)
    saved_cache = dict(llm._CACHE)
    saved_loaded = llm._entry_points_loaded
    ScriptedApi.seen = []
    try:
        yield
    finally:
        run(llm.aclose_all())
        llm._REGISTRY.clear()
        llm._REGISTRY.update(saved_registry)
        llm._CACHE.clear()
        llm._CACHE.update(saved_cache)
        llm._entry_points_loaded = saved_loaded


def request(**kwargs: Any) -> LLMRequest:
    """One rendered invocation."""
    base: Dict[str, Any] = {
        "model": "m-1",
        "system": "You are a proposer.",
        "user": "Pick from the menu.",
        "json_schema": {"type": "object"},
        "max_output_tokens": 256,
        "temperature": 0,
        "timeout_s": 5,
        "trace_id": "item-1|proposer|proposer_1",
    }
    base.update(kwargs)
    return LLMRequest(**base)


# ---------------------------------------------------------------------------
# The built-ins
# ---------------------------------------------------------------------------


class TestBuiltins:
    """The four shipped kinds resolve by name, and nothing else does."""

    @pytest.mark.parametrize(
        "name, cls_name",
        [
            ("argo", "ArgoBackend"),
            ("openai", "OpenAICompatibleBackend"),
            ("claude_cli", "ClaudeCliBackend"),
            ("codex_cli", "CodexCliBackend"),
        ],
    )
    def test_each_builtin_resolves_by_name_to_a_conforming_api_type(
        self, name: str, cls_name: str
    ) -> None:
        endpoint = (
            {"name": name, "kind": name, "base_url": "http://127.0.0.1:9/v1"}
            if name in ("argo", "openai")
            else {"name": name, "kind": name, "executable": "no-such-cli"}
        )
        api = llm.get_api(name, endpoint)
        assert type(api).__name__ == cls_name
        # Both required members, and the runtime-checkable protocol.
        assert callable(api.complete)
        assert callable(api.effective_params)
        assert isinstance(api, ApiType)
        run(llm.aclose_all())

    def test_the_builtin_set_is_exactly_the_four_real_kinds(self) -> None:
        """``fake`` and ``replay`` are NOT registered, and that is the point.

        A packaged configuration naming ``kind: fake`` must fail at load
        rather than quietly answer from a script instead of from a model.  A
        test that wants them registers them itself.
        """
        assert llm.BUILTIN_API_TYPES == ("argo", "claude_cli", "codex_cli", "openai")
        names = llm.api_types()
        assert set(llm.BUILTIN_API_TYPES) <= set(names)
        assert "fake" not in names
        assert "replay" not in names

    def test_an_unknown_name_raises_keyerror_naming_the_registered_ones(self) -> None:
        with pytest.raises(KeyError) as excinfo:
            llm.get_api("gemini_direct", {})
        message = str(excinfo.value)
        assert "gemini_direct" in message
        for name in llm.BUILTIN_API_TYPES:
            assert name in message, f"{name} should be listed as available"
        assert llm.ENTRY_POINT_GROUP in message

    def test_importing_the_wrapper_does_not_import_a_provider_module(self) -> None:
        """The built-ins are lazy factories, which is a measurable claim.

        Run in a subprocess with a clean interpreter, because by the time this
        suite runs the provider modules are long since imported by the M1
        backend suites.

        What is asserted is that no module under ``llm.apis`` is loaded --
        NOT that ``httpx`` is absent: ``import kbutillib`` already pulls
        ``httpx`` in through its own root (measured, 2026-10-15), so an httpx
        assertion here would fail for a reason that has nothing to do with
        this wrapper.
        """
        code = textwrap.dedent(
            """
            import sys
            from kbutillib.domains.ai import llm
            assert sorted(llm.api_types()) == [
                "argo", "claude_cli", "codex_cli", "openai"
            ], sorted(llm.api_types())
            leaked = [name for name in sys.modules if ".llm.apis." in name]
            assert not leaked, leaked
            print("clean")
            """
        )
        done = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
        )
        assert done.returncode == 0, done.stderr
        assert "clean" in done.stdout


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    """The in-process door to the registry, and what it refuses."""

    def test_a_registered_type_resolves_and_appears_in_api_types(self) -> None:
        llm.register_api_type("scripted", ScriptedApi)
        assert "scripted" in llm.api_types()
        assert isinstance(llm.get_api("scripted", {}), ScriptedApi)

    def test_a_duplicate_registration_is_refused(self) -> None:
        llm.register_api_type("scripted", ScriptedApi)
        with pytest.raises(ValueError, match="already registered"):
            llm.register_api_type("scripted", ScriptedApi)
        with pytest.raises(ValueError, match="already registered"):
            llm.register_api_type("argo", ScriptedApi)

    def test_replace_is_allowed_and_drops_the_cached_client(self) -> None:
        llm.register_api_type("scripted", ScriptedApi)
        first = llm.get_api("scripted", {"name": "e1"})

        llm.register_api_type("scripted", FailingApi, replace=True)
        second = llm.get_api("scripted", {"name": "e1"})

        assert isinstance(second, FailingApi)
        assert second is not first, "a re-registration must not keep serving the old client"

    @pytest.mark.parametrize(
        "factory, missing",
        [
            # Neither required member.
            (type("Empty", (), {"kind": "empty"}), "complete"),
            # complete() but NO effective_params(): refused because a caller
            # content-addresses its calls on effective_params, so an api type
            # without one cannot be cached or route-fingerprinted at all.
            (
                type(
                    "HalfApi",
                    (),
                    {"kind": "half", "complete": lambda self, r: None},
                ),
                "effective_params",
            ),
            # effective_params() but no complete(): not an api type either.
            (
                type(
                    "Listless",
                    (),
                    {"kind": "listless", "effective_params": lambda self, r: {}},
                ),
                "complete",
            ),
        ],
    )
    def test_a_class_that_is_not_an_api_type_is_refused_at_registration(
        self, factory: Any, missing: str
    ) -> None:
        with pytest.raises(TypeError) as excinfo:
            llm.register_api_type("malformed", factory)
        assert missing in str(excinfo.value)
        assert "effective_params" in str(excinfo.value), "the message names the contract"
        assert "malformed" not in llm._REGISTRY

    def test_a_non_callable_factory_is_refused(self) -> None:
        with pytest.raises(ValueError, match="callable"):
            llm.register_api_type("nope", "not a factory")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="non-empty string"):
            llm.register_api_type("", ScriptedApi)

    def test_a_malformed_factory_FUNCTION_is_caught_on_first_use_instead(
        self,
    ) -> None:
        """A function's product does not exist until it is called.

        So a function factory is registered unchecked and its product is
        checked at :func:`llm.get_api` time, which is the first moment one
        exists.  Constructing a throwaway probe at registration would mean
        every registration opened a client nobody asked for -- which for the
        four built-ins would import ``httpx`` at import time, exactly what
        the lazy factories exist to avoid.
        """

        def make_not_an_api(endpoint: Mapping[str, Any]) -> Any:
            return object()

        llm.register_api_type("functional", make_not_an_api)
        assert "functional" in llm.api_types()
        with pytest.raises(TypeError, match="complete"):
            llm.get_api("functional", {"base_url": "http://x"})


# ---------------------------------------------------------------------------
# Entry-point discovery
# ---------------------------------------------------------------------------


def _write_plugin_distribution(root: Path, *, name: str, api_type: str) -> Path:
    """Install a fake distribution declaring one api-type entry point.

    A real ``*.dist-info`` directory with a real ``entry_points.txt``, next to
    a real importable module, on ``sys.path``.  Monkeypatching
    ``importlib.metadata.entry_points`` would have proved only that the code
    calls the function; this proves a third party can ship an api type without
    this repository knowing anything about it.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}.py").write_text(
        textwrap.dedent(
            '''
            """A third-party api type, as a plug-in would really ship one."""


            class PluginApi:
                kind = "plugin"

                def __init__(self, endpoint=None):
                    self.endpoint = dict(endpoint or {})

                async def complete(self, request):
                    from kbutillib.domains.ai.llm.types import LLMResult

                    return LLMResult(
                        ok=True,
                        text="{}",
                        parsed={"from": "the plug-in", "model": request.model},
                        resolved_model=request.model,
                    )

                def effective_params(self, request):
                    from kbutillib.domains.ai import llm

                    return llm.generation_params(request)


            def make_api(endpoint=None):
                return PluginApi(endpoint)
            '''
        ),
        encoding="utf-8",
    )
    dist = root / f"{name}-1.0.dist-info"
    dist.mkdir(exist_ok=True)
    (dist / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n", encoding="utf-8"
    )
    (dist / "entry_points.txt").write_text(
        f"[{llm.ENTRY_POINT_GROUP}]\n{api_type} = {name}:make_api\n", encoding="utf-8"
    )
    return root


class TestEntryPointDiscovery:
    """A plug-in kind arrives from another distribution, lazily."""

    def test_an_entry_point_is_discovered_from_a_dist_info_on_sys_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        site = _write_plugin_distribution(
            tmp_path / "site", name="kbu_plugin_demo", api_type="demo_gateway"
        )
        monkeypatch.syspath_prepend(str(site))
        llm._entry_points_loaded = False

        assert "demo_gateway" in llm.api_types()
        api = llm.get_api("demo_gateway", {"base_url": "http://x"})
        assert type(api).__name__ == "PluginApi"
        result = run(llm.acomplete(request(model="plug-1"), api_type="demo_gateway"))
        assert result.ok is True
        assert result.parsed == {"from": "the plug-in", "model": "plug-1"}

    def test_discovery_is_lazy_and_happens_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Scanning distributions is not free, so a built-in call must not.

        Counted through the real function: resolving ``argo`` must not read
        the group at all, and resolving an unknown name must read it exactly
        once however many times it is asked.
        """
        site = _write_plugin_distribution(
            tmp_path / "site", name="kbu_plugin_lazy", api_type="lazy_gateway"
        )
        monkeypatch.syspath_prepend(str(site))
        llm._entry_points_loaded = False

        scans: List[str] = []
        import importlib.metadata as md

        real = md.entry_points

        def counting(*args: Any, **kwargs: Any) -> Any:
            scans.append(str(kwargs.get("group") or args))
            return real(*args, **kwargs)

        monkeypatch.setattr(md, "entry_points", counting)

        llm.get_api("argo", {"base_url": "http://127.0.0.1:9/v1"})
        assert scans == [], "a built-in must resolve without scanning distributions"

        llm.get_api("lazy_gateway", {})
        assert len(scans) == 1
        for _ in range(3):
            llm.get_api("lazy_gateway", {})
            with pytest.raises(KeyError):
                llm.get_api("still_unknown", {})
        assert len(scans) == 1, "the group is read once per process, not per lookup"
        run(llm.aclose_all())

    def test_an_entry_point_never_overrides_a_builtin(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An installed package must not silently become the ARGO route.

        "Which gateway did that call go to?" has to stay answerable from this
        repository, so a name collision loses to the built-in rather than
        winning by install order.
        """
        site = _write_plugin_distribution(
            tmp_path / "site", name="kbu_plugin_hijack", api_type="argo"
        )
        monkeypatch.syspath_prepend(str(site))
        llm._entry_points_loaded = False

        llm.api_types()
        api = llm.get_api("argo", {"base_url": "http://127.0.0.1:9/v1"})
        assert type(api).__name__ == "ArgoBackend"
        run(llm.aclose_all())

    def test_a_broken_entry_point_is_skipped_not_fatal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One bad third-party distribution must not stop the council."""
        site = tmp_path / "site"
        site.mkdir()
        dist = site / "kbu_plugin_broken-1.0.dist-info"
        dist.mkdir()
        (dist / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: kbu-plugin-broken\nVersion: 1.0\n",
            encoding="utf-8",
        )
        (dist / "entry_points.txt").write_text(
            f"[{llm.ENTRY_POINT_GROUP}]\nbroken = no_such_module_anywhere:make\n",
            encoding="utf-8",
        )
        monkeypatch.syspath_prepend(str(site))
        llm._entry_points_loaded = False

        names = llm.api_types()
        assert "broken" not in names
        assert set(llm.BUILTIN_API_TYPES) <= set(names)


# ---------------------------------------------------------------------------
# Calling
# ---------------------------------------------------------------------------


class TestCalling:
    """One attempt, and never a raise for a call failure."""

    def test_acomplete_returns_the_api_types_result(self) -> None:
        llm.register_api_type("scripted", ScriptedApi)
        result = run(llm.acomplete(request(), api_type="scripted", endpoint={}))
        assert result.ok is True
        assert result.parsed == PROPOSER_ANSWER
        assert result.resolved_model == "m-1"

    def test_complete_is_the_sync_twin_and_returns_ok_false_for_a_failure(
        self,
    ) -> None:
        llm.register_api_type("failing", FailingApi)
        result = llm.complete(request(), api_type="failing", endpoint={})
        assert isinstance(result, LLMResult)
        assert result.ok is False
        assert result.error is not None
        assert result.error.kind == "transport"
        assert result.error.retryable is True

    def test_an_api_type_that_raises_is_converted_not_propagated(self) -> None:
        """The contract holds even for a plug-in that has not read it."""
        llm.register_api_type("raising", RaisingApi)
        result = run(llm.acomplete(request(), api_type="raising", endpoint={}))
        assert result.ok is False
        assert result.error is not None
        assert result.error.kind == "transport"
        assert "the socket went away" in result.error.message

    def test_complete_inside_a_running_loop_raises_a_clear_runtimeerror(self) -> None:
        llm.register_api_type("scripted", ScriptedApi)

        async def inside() -> None:
            with pytest.raises(RuntimeError, match="already running"):
                llm.complete(request(), api_type="scripted", endpoint={})

        run(inside())

    def test_an_unknown_api_type_raises_rather_than_reporting_a_call_failure(
        self,
    ) -> None:
        """A config typo is not a model failure, and must not be retried as one."""
        with pytest.raises(KeyError, match="no LLM api type"):
            run(llm.acomplete(request(), api_type="typo", endpoint={}))

    def test_a_legacy_completion_request_still_works(self) -> None:
        """Council code and its tests build the M1 record; both must keep working."""
        from kbutillib.domains.ai.council.types import CompletionRequest, SeatSpec

        legacy = CompletionRequest(
            seat=SeatSpec(
                seat_name="proposer_1",
                endpoint="e1",
                endpoint_kind="argo",
                model="gemini35flash",
                canonical_model="google/gemini-3.5-flash",
                max_output_tokens=1234,
                temperature=0.5,
                timeout_s=7,
                extra={"top_p": 0.9},
            ),
            role="critic",
            system="You are the critic.",
            user="Audit the table.",
            json_schema={"type": "object"},
            trace_id="item-1|critic|critic",
            deadline_s=99,
        )
        llm.register_api_type("scripted", ScriptedApi)
        result = run(llm.acomplete(legacy, api_type="scripted", endpoint={}))

        assert result.ok is True
        assert result.parsed == CRITIC_ANSWER, "the critic's system prompt survived"
        asked = ScriptedApi.seen[-1]
        assert isinstance(asked, LLMRequest)
        assert asked.model == "gemini35flash"
        assert asked.max_output_tokens == 1234
        assert asked.temperature == 0.5
        assert asked.params == {"top_p": 0.9}
        assert asked.timeout_s == 7
        assert asked.trace_id == "item-1|critic|critic"

    def test_a_seat_timeout_of_zero_falls_back_to_the_requests_deadline(self) -> None:
        """``deadline_s`` was the M1 fallback, and the adapter keeps it."""
        from kbutillib.domains.ai.council.types import CompletionRequest, SeatSpec

        legacy = CompletionRequest(
            seat=SeatSpec(
                seat_name="s",
                endpoint="e",
                endpoint_kind="argo",
                model="m",
                canonical_model="v/m",
                timeout_s=0,
            ),
            role="proposer",
            system="s",
            user="u",
            deadline_s=42,
        )
        assert llm.as_llm_request(legacy).timeout_s == 42

    def test_an_unreadable_request_shape_is_a_typeerror(self) -> None:
        with pytest.raises(TypeError, match="LLMRequest"):
            llm.as_llm_request(object())


# ---------------------------------------------------------------------------
# The probes
# ---------------------------------------------------------------------------


class TestProbes:
    """``preflight`` and ``models``: free, and never a precondition."""

    def test_an_api_type_without_a_preflight_is_ready(self) -> None:
        llm.register_api_type("scripted", ScriptedApi)
        report = run(llm.preflight("scripted", {}))
        assert report["state"] == "ready"
        assert report["checked"] == []

    def test_a_refusing_preflight_is_reported_verbatim_with_a_detail(self) -> None:
        class Refusing(ScriptedApi):
            async def preflight(self) -> Dict[str, Any]:
                return {"state": "refused", "reasons": ["settings.json sets a key"]}

        llm.register_api_type("refusing", Refusing)
        report = run(llm.preflight("refusing", {}))
        assert report["state"] == "refused"
        # ``detail`` is what a caller puts in its refusal message; an empty one
        # means the operator is told only "no reason given".
        assert report["detail"] == "settings.json sets a key"

    def test_models_is_empty_for_an_api_type_that_cannot_enumerate(self) -> None:
        llm.register_api_type("scripted", ScriptedApi)
        assert run(llm.models("scripted", {})) == []

    def test_models_passes_a_listing_through_as_plain_dicts(self) -> None:
        class Listing(ScriptedApi):
            async def models(self) -> List[Dict[str, Any]]:
                return [{"id": "a"}, "not a mapping", {"id": "b"}]  # type: ignore[list-item]

        llm.register_api_type("listing", Listing)
        assert run(llm.models("listing", {})) == [{"id": "a"}, {"id": "b"}]


# ---------------------------------------------------------------------------
# Client caching
# ---------------------------------------------------------------------------


class TestClientCaching:
    """One client per (api type, endpoint, loop) -- and never across loops."""

    def test_the_same_endpoint_reuses_one_client(self) -> None:
        llm.register_api_type("scripted", ScriptedApi)
        endpoint = {"name": "e1", "base_url": "http://x/v1"}
        assert llm.get_api("scripted", endpoint) is llm.get_api("scripted", endpoint)

    def test_key_order_is_not_a_different_endpoint(self) -> None:
        llm.register_api_type("scripted", ScriptedApi)
        a = llm.get_api("scripted", {"name": "e1", "base_url": "http://x/v1"})
        b = llm.get_api("scripted", {"base_url": "http://x/v1", "name": "e1"})
        assert a is b

    def test_a_changed_endpoint_mapping_does_not_reuse_the_old_client(self) -> None:
        llm.register_api_type("scripted", ScriptedApi)
        before = llm.get_api("scripted", {"name": "e1", "base_url": "http://x/v1"})
        after = llm.get_api("scripted", {"name": "e1", "base_url": "http://y/v1"})
        assert after is not before
        assert after.endpoint["base_url"] == "http://y/v1"

    def test_two_endpoints_of_one_kind_are_two_clients(self) -> None:
        """Nine vLLM ports are nine clients, not one shared by accident."""
        llm.register_api_type("scripted", ScriptedApi)
        clients = {
            id(llm.get_api("scripted", {"name": f"port{port}", "base_url": f"http://x:{port}/v1"}))
            for port in range(8301, 8310)
        }
        assert len(clients) == 9

    def test_a_client_is_never_reused_across_event_loops(self) -> None:
        """An ``httpx.AsyncClient`` built on a closed loop fails when awaited.

        The synchronous facades in this repository each own a fresh loop, so
        without the loop in the cache key the SECOND sync call of a process
        would be handed a client bound to a loop that no longer exists.
        """
        llm.register_api_type("scripted", ScriptedApi)
        endpoint = {"name": "e1"}

        async def grab() -> Any:
            return llm.get_api("scripted", endpoint)

        first = run(grab())
        second = run(grab())
        outside = llm.get_api("scripted", endpoint)

        # Three loops (two owned by ``run``, then none), so three clients.
        # This is also the regression test for keying on ``id(loop)``:
        # CPython hands a collected loop's address to the next one, so with
        # ``id`` these two were the SAME key and ``second`` was ``first``.
        assert first is not second
        assert outside is not first and outside is not second

    def test_aclose_all_closes_cached_and_orphaned_clients(self) -> None:
        """Including the one a ``replace=True`` registration took out of play."""
        closed: List[str] = []

        class Closing(ScriptedApi):
            def __init__(self, endpoint: Optional[Mapping[str, Any]] = None) -> None:
                super().__init__(endpoint)
                self.tag = str(dict(endpoint or {}).get("name") or "?")

            async def aclose(self) -> None:
                closed.append(self.tag)

        llm.register_api_type("closing", Closing)
        llm.get_api("closing", {"name": "kept"})
        llm.register_api_type("closing", Closing, replace=True)  # orphans "kept"
        llm.get_api("closing", {"name": "fresh"})

        run(llm.aclose_all())
        assert sorted(closed) == ["fresh", "kept"]
        assert llm._CACHE == {}
        assert llm._ORPHANS == []

    def test_a_close_that_raises_does_not_stop_the_others(self) -> None:
        closed: List[str] = []

        class Hostile(ScriptedApi):
            async def aclose(self) -> None:
                raise OSError("will not close")

        class Polite(ScriptedApi):
            async def aclose(self) -> None:
                closed.append("polite")

        llm.register_api_type("hostile", Hostile)
        llm.register_api_type("polite", Polite)
        llm.get_api("hostile", {})
        llm.get_api("polite", {})

        run(llm.aclose_all())
        assert closed == ["polite"]


# ---------------------------------------------------------------------------
# What a configuration may say
# ---------------------------------------------------------------------------


def config_dict(**endpoints: Mapping[str, Any]) -> Dict[str, Any]:
    """A loadable council config over *endpoints*, with one seatable model each."""
    models = {
        f"m{index}": {"endpoint": alias, "canonical": f"test/m{index}"}
        for index, alias in enumerate(endpoints, start=1)
    }
    first = sorted(models)[0]
    return {
        "endpoints": dict(endpoints),
        "models": models,
        "profiles": {
            "test": {"proposers": sorted(models), "critic": first, "meta": first}
        },
    }


class TestConfigAcceptsRegisteredKindsOnly:
    """``kind`` is no longer a Literal, so the registry is what validates it."""

    def test_an_unregistered_kind_fails_at_load(self) -> None:
        """At LOAD, before a token is spent -- not at the first call."""
        from kbutillib.domains.ai.council.config import load_council_config
        from kbutillib.domains.ai.council.types import ConfigError

        with pytest.raises(ConfigError) as excinfo:
            load_council_config(
                config_dict(weird={"kind": "no_such_gateway", "base_url": "http://x/v1"})
            )
        message = str(excinfo.value)
        assert "no_such_gateway" in message
        for name in llm.BUILTIN_API_TYPES:
            assert name in message, "the message lists what IS registered"

    def test_a_registered_fake_kind_loads(self) -> None:
        """``fake``/``replay`` work where a test registers them, and only there."""
        from kbutillib.domains.ai.council.config import load_council_config

        llm.register_api_type("fake", ScriptedApi)
        cfg = load_council_config(
            config_dict(fake1={"kind": "fake", "reuse_namespace": "test:fake1"})
        )
        assert cfg.endpoints["fake1"].kind == "fake"
        # And the default per-endpoint concurrency for an unknown kind is the
        # fallback, not a KeyError out of DEFAULT_MAX_CONCURRENCY.
        assert cfg.endpoints["fake1"].max_concurrency == 4

    def test_the_four_builtin_kinds_still_load(self) -> None:
        from kbutillib.domains.ai.council.config import load_council_config

        cfg = load_council_config()  # the packaged default
        assert {ep.kind for ep in cfg.endpoints.values()} <= set(llm.BUILTIN_API_TYPES)


class TestReuseNamespaceForBaseUrllessKinds:
    """A kind with no URL has to say how its answers may be reused."""

    def test_a_base_urlless_plugin_kind_without_reuse_namespace_is_refused(
        self,
    ) -> None:
        from kbutillib.domains.ai.council.config import load_council_config
        from kbutillib.domains.ai.council.types import ConfigError

        llm.register_api_type("local_socket", ScriptedApi)
        with pytest.raises(ConfigError, match="reuse_namespace"):
            load_council_config(config_dict(sock={"kind": "local_socket"}))

    def test_declaring_one_is_enough(self) -> None:
        from kbutillib.domains.ai.council.config import load_council_config

        llm.register_api_type("local_socket", ScriptedApi)
        cfg = load_council_config(
            config_dict(sock={"kind": "local_socket", "reuse_namespace": "sock:v1"})
        )
        assert cfg.endpoints["sock"].reuse_namespace == "sock:v1"

    def test_a_base_url_is_the_other_way_to_satisfy_it(self) -> None:
        from kbutillib.domains.ai.council.config import load_council_config

        llm.register_api_type("local_socket", ScriptedApi)
        cfg = load_council_config(
            config_dict(sock={"kind": "local_socket", "base_url": "http://x/v1"})
        )
        assert cfg.endpoints["sock"].reuse_namespace is None

    def test_the_two_builtin_cli_kinds_are_exempt(self) -> None:
        """Their namespace is ``<kind>:<served model>``, which is a real identity.

        Exempting them is not a loophole: ``default_reuse_namespace`` has
        something to build from for a CLI and nothing for an unknown kind.
        """
        from kbutillib.domains.ai.council.config import load_council_config
        from kbutillib.domains.ai.council.types import default_reuse_namespace

        cfg = load_council_config(
            config_dict(
                claude={"kind": "claude_cli"},
                codex={"kind": "codex_cli"},
            )
        )
        assert cfg.endpoints["claude"].reuse_namespace is None
        assert (
            default_reuse_namespace(cfg.endpoint("claude"), "sonnet")
            == "claude_cli:sonnet"
        )

    def test_an_http_kind_still_requires_a_base_url(self) -> None:
        from kbutillib.domains.ai.council.config import load_council_config
        from kbutillib.domains.ai.council.types import ConfigError

        with pytest.raises(ConfigError, match="no base_url"):
            load_council_config(config_dict(broken={"kind": "openai"}))


class TestPluginOptions:
    """``options:`` reaches the factory unchanged, and enters the fingerprint."""

    def test_options_survive_the_endpoint_key_allowlist(self) -> None:
        """The loader filters endpoint keys; ``options`` must not be filtered.

        A plug-in's own fields cannot be in this repository's allowlist by
        name -- nobody here wrote the plug-in -- so they live under one key
        that is.
        """
        from kbutillib.domains.ai.council.config import load_council_config

        llm.register_api_type("gw", ScriptedApi)
        cfg = load_council_config(
            config_dict(
                gw1={
                    "kind": "gw",
                    "base_url": "http://x/v1",
                    "options": {"region": "us-east", "retries": 3, "nested": {"a": 1}},
                }
            )
        )
        assert cfg.endpoint("gw1")["options"] == {
            "region": "us-east",
            "retries": 3,
            "nested": {"a": 1},
        }

    def test_options_are_passed_to_the_factory_unchanged(self) -> None:
        from kbutillib.domains.ai.council.config import load_council_config

        llm.register_api_type("gw", ScriptedApi)
        cfg = load_council_config(
            config_dict(
                gw1={
                    "kind": "gw",
                    "base_url": "http://x/v1",
                    "options": {"region": "us-east", "nested": {"a": 1}},
                }
            )
        )
        api = llm.get_api("gw", cfg.endpoint("gw1"))
        assert api.endpoint["options"] == {"region": "us-east", "nested": {"a": 1}}

    def test_options_appear_in_the_snapshot_because_they_are_not_secret(
        self,
    ) -> None:
        from kbutillib.domains.ai.council.config import load_council_config

        llm.register_api_type("gw", ScriptedApi)
        cfg = load_council_config(
            config_dict(
                gw1={"kind": "gw", "base_url": "http://x/v1", "options": {"region": "us"}}
            )
        )
        assert cfg.snapshot()["endpoints"]["gw1"]["options"] == {"region": "us"}

    def test_options_change_the_route_fingerprint(self) -> None:
        """An option that changes the answer must change the route's identity."""
        from kbutillib.domains.ai.council.types import SeatSpec, route_fingerprint

        seat = SeatSpec(
            seat_name="proposer_1",
            endpoint="gw1",
            endpoint_kind="openai",
            model="m",
            canonical_model="test/m",
        )
        plain = {"kind": "openai", "base_url": "http://x/v1"}
        with_options = dict(plain, options={"region": "us-east"})
        other_options = dict(plain, options={"region": "eu-west"})

        assert route_fingerprint(seat, plain) != route_fingerprint(seat, with_options)
        assert route_fingerprint(seat, with_options) != route_fingerprint(
            seat, other_options
        )

    def test_an_empty_options_block_does_not_change_an_existing_fingerprint(
        self,
    ) -> None:
        """Every call row written before M2.1 has to keep matching.

        A route fingerprint is stored on each queued call and compared when
        the call runs; a fingerprint that changed for every endpoint on
        upgrade would make every queued call report
        ``configuration_mismatch`` and park itself.
        """
        from kbutillib.domains.ai.council.types import SeatSpec, route_fingerprint

        seat = SeatSpec(
            seat_name="proposer_1",
            endpoint="gw1",
            endpoint_kind="openai",
            model="m",
            canonical_model="test/m",
        )
        plain = {"kind": "openai", "base_url": "http://x/v1"}
        assert route_fingerprint(seat, plain) == route_fingerprint(
            seat, dict(plain, options={})
        )


class TestCredentialsAreNamesNotValues:
    """A credential-shaped value under a secret key name is a ConfigError."""

    @pytest.mark.parametrize(
        "value",
        [
            "sk-proj-AAAABBBBCCCCDDDDEEEEFFFF",
            "ghp_0123456789abcdefghijklmnopqrstuvwx",
            "AIzaSyB1234567890abcdefghijklmnopqrstu",
            "Bearer abcdef123456",
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.x",
            "k" * 40,
        ],
    )
    def test_a_live_looking_key_is_refused_at_load(self, value: str) -> None:
        from kbutillib.domains.ai.council.config import load_council_config
        from kbutillib.domains.ai.council.types import ConfigError

        with pytest.raises(ConfigError) as excinfo:
            load_council_config(
                config_dict(
                    local={"kind": "openai", "base_url": "http://x/v1", "api_key": value}
                )
            )
        message = str(excinfo.value)
        assert "api_key_env" in message, "the message says what to do instead"
        assert value not in message, "and does not echo the secret back"

    def test_the_same_value_under_an_env_key_is_fine(self) -> None:
        """A ``*_env`` key names a variable; that is the point of the rule."""
        from kbutillib.domains.ai.council.config import load_council_config

        cfg = load_council_config(
            config_dict(
                local={
                    "kind": "openai",
                    "base_url": "http://x/v1",
                    "api_key_env": "OPENAI_API_KEY",
                }
            )
        )
        assert cfg.endpoints["local"].api_key_env == "OPENAI_API_KEY"

    @pytest.mark.parametrize("value", ["EMPTY", "REDACTED", "sekrit", "", "unused"])
    def test_a_placeholder_is_not_refused(self, value: str) -> None:
        """``EMPTY`` is vLLM's documented placeholder and ``REDACTED`` is how a
        config gets shared for diagnosis; refusing either would break working
        setups while protecting nobody.  This is why the rule is a heuristic
        on the VALUE and not a ban on the key.
        """
        from kbutillib.domains.ai.council.config import load_council_config

        cfg = load_council_config(
            config_dict(
                local={"kind": "openai", "base_url": "http://x/v1", "api_key": value}
            )
        )
        assert cfg.endpoints["local"].api_key == value

    def test_a_credential_hidden_inside_options_is_also_refused(self) -> None:
        """``options`` is non-secret by contract, so this is where it is enforced."""
        from kbutillib.domains.ai.council.config import load_council_config
        from kbutillib.domains.ai.council.types import ConfigError

        llm.register_api_type("gw", ScriptedApi)
        with pytest.raises(ConfigError, match="options.auth.token"):
            load_council_config(
                config_dict(
                    gw1={
                        "kind": "gw",
                        "base_url": "http://x/v1",
                        "options": {"auth": {"token": "sk-live-AAAABBBBCCCCDDDDEEEE"}},
                    }
                )
            )

    def test_a_budget_is_not_a_credential_however_its_name_reads(self) -> None:
        from kbutillib.domains.ai.council.config import load_council_config

        cfg = load_council_config(
            config_dict(
                local={
                    "kind": "openai",
                    "base_url": "http://x/v1",
                    "max_total_tokens": 1_000_000,
                }
            )
        )
        assert cfg.endpoints["local"].max_total_tokens == 1_000_000


# ---------------------------------------------------------------------------
# A whole council job, through the wrapper itself
# ---------------------------------------------------------------------------
#
# These are the "no injection" checks.  Every other council test drives a
# backend instance handed straight to CouncilWorker, which proves the engine
# works but says nothing about whether the call went through llm.acomplete.
# Here the worker is given endpoint ALIASES ONLY, so there is nothing to
# dispatch to but the wrapper, and the api type is reached the way a real one
# is: resolved by name from the endpoint's `kind`.


@pytest.fixture()
def sqlite_store(tmp_path: Path) -> Iterator[Any]:
    """A real, initialised SQLite council store in ``tmp_path``."""
    from kbutillib.domains.ai.council.store.sqlite import SqliteStore

    store = SqliteStore(f"sqlite:///{tmp_path}/council/council.sqlite")
    store.init_schema()
    try:
        yield store
    finally:
        close = getattr(store, "close", None)
        if close is not None:
            close()


def council_scaffolding() -> Any:
    """The engine suite's own roster/query/task builders.

    Imported from ``tests.domains.ai.council.engine_support`` rather than
    re-written here on purpose: a council assembled slightly differently
    would not prove that THE council runs through the wrapper.
    """
    from ..council import engine_support

    return engine_support


WORKER_TIMINGS: Dict[str, Any] = {
    "heartbeat_interval_s": 0.0,
    "poll_interval_s": 0.01,
    "retry_backoff_s": (0.02, 0.05),
    "retry_jitter": 0.0,
    "idle_grace_s": 0.3,
    "max_idle_s": 15.0,
}


class TestACouncilJobEndToEndThroughTheWrapper:
    """Submit and work a real job with nothing injected anywhere."""

    def test_the_default_callable_IS_llm_acomplete(self) -> None:
        """Identity, not behaviour: there is nothing else it could be.

        A worker handed endpoint aliases has no api type instance to dispatch
        to, so if this were anything but ``llm.acomplete`` the council would
        not be going through the wrapper at all.
        """
        from kbutillib.domains.ai.council.engine.worker import CouncilWorker

        worker = CouncilWorker(object(), None, ["fake1"], "alice", "w-1")
        assert worker._acomplete is llm.acomplete

    def test_a_job_runs_to_a_verdict_through_llm_acomplete(
        self, sqlite_store: Any
    ) -> None:
        from kbutillib.domains.ai.council.engine.worker import CouncilWorker

        support = council_scaffolding()
        llm.register_api_type("fake", ScriptedApi)

        worker = CouncilWorker(
            sqlite_store,
            support.endpoint_config(fake1={}),
            ["fake1"],  # ALIASES ONLY: nothing to dispatch to but the wrapper
            "alice",
            "w-1",
            **WORKER_TIMINGS,
        )
        assert worker._acomplete is llm.acomplete

        query = support.make_query()
        roster = support.make_roster(3)
        job_id = worker.submit(query, [support.make_task()], roster)
        summary = run(worker.work(job_id=job_id, until_idle=True))

        results = sqlite_store.results(job_id)
        assert len(results) == 1, summary
        verdict = results[0]["verdict"]
        assert verdict["status"] == "ok"
        assert verdict["selected_ids"] == ["A:1"]
        assert verdict["decided_by"] == "unanimous"

        # And it really went through the registered api type: four calls, one
        # per proposer plus the critic, with meta skipped on a unanimous vote.
        assert len(ScriptedApi.seen) == 4
        assert all(isinstance(req, LLMRequest) for req in ScriptedApi.seen)
        # The three proposers run concurrently, so the ORDER they arrive in is
        # the scheduler's business; the SET of traces is the claim.
        assert {req.trace_id for req in ScriptedApi.seen} == {
            "item-1|proposer|proposer_1",
            "item-1|proposer|proposer_2",
            "item-1|proposer|proposer_3",
            "item-1|critic|critic",
        }
        # The rendered prompts survived the trip, menu and all.
        proposals = [r for r in ScriptedApi.seen if "proposer" in (r.trace_id or "")]
        assert "Candidates:" in proposals[0].user
        assert "A:1" in proposals[0].user
        assert proposals[0].max_output_tokens == 256
        assert proposals[0].temperature == 0

    def test_patching_the_injected_callable_counts_every_model_call(
        self, sqlite_store: Any
    ) -> None:
        """A 3-item job makes 12 model calls, and every one is countable here.

        This is the proof that there is exactly ONE call site: if any council
        module reached an api type directly, its calls would be missing from
        this count.  The substitute delegates to ``llm.acomplete``, so the
        job still really runs -- a stub that answered by itself would prove
        only that the stub was called.
        """
        from kbutillib.domains.ai.council.engine.worker import CouncilWorker

        support = council_scaffolding()
        llm.register_api_type("fake", ScriptedApi)

        counted: List[Dict[str, Any]] = []

        async def counting(request: Any, *, api_type: str, endpoint: Any) -> Any:
            counted.append({"api_type": api_type, "endpoint": dict(endpoint)})
            return await llm.acomplete(request, api_type=api_type, endpoint=endpoint)

        worker = CouncilWorker(
            sqlite_store,
            support.endpoint_config(fake1={}),
            ["fake1"],
            "alice",
            "w-1",
            complete=counting,
            **WORKER_TIMINGS,
        )
        assert worker._acomplete is counting

        tasks = [
            support.make_task(item_id=f"item-{n}", text=f"annotation number {n}")
            for n in (1, 2, 3)
        ]
        job_id = worker.submit(support.make_query(), tasks, support.make_roster(3))
        summary = run(worker.work(job_id=job_id, until_idle=True))

        assert len(sqlite_store.results(job_id)) == 3, summary
        # 3 items x (3 proposers + 1 critic); meta is skipped on unanimity.
        assert len(counted) == 12
        assert len(ScriptedApi.seen) == 12, "every counted call really reached the api"
        assert {entry["api_type"] for entry in counted} == {"fake"}
        # The endpoint mapping names the alias, which is what keeps two
        # aliases from sharing one cached client.
        assert {entry["endpoint"]["name"] for entry in counted} == {"fake1"}

    def test_the_route_fingerprint_uses_the_api_types_effective_params(
        self, sqlite_store: Any
    ) -> None:
        """Round 3: the prompt hash and the fingerprint come from the api type.

        Proved with an api type that reports something the SEAT never asked
        for -- the way ARGO forces ``temperature: 1`` on some models -- so a
        fingerprint built from the seat's own parameters cannot pass.
        """
        from kbutillib.domains.ai.council.engine.worker import CouncilWorker

        class Opinionated(ScriptedApi):
            def effective_params(self, request: Any) -> Dict[str, Any]:
                params = llm.generation_params(request)
                params["temperature"] = 1  # not what any seat asked for
                return params

        support = council_scaffolding()
        llm.register_api_type("fake", Opinionated)

        worker = CouncilWorker(
            sqlite_store,
            support.endpoint_config(fake1={}),
            ["fake1"],
            "alice",
            "w-1",
            **WORKER_TIMINGS,
        )
        seat = support.make_roster(3).proposers[0]
        assert seat.temperature == 0
        assert worker._effective_params(seat)["temperature"] == 1

        # And the api type is asked with an LLMRequest, not with the seat.
        asked: List[Any] = []

        class Recording(ScriptedApi):
            def effective_params(self, request: Any) -> Dict[str, Any]:
                asked.append(request)
                return llm.generation_params(request)

        llm.register_api_type("fake", Recording, replace=True)
        worker._effective_params(seat)
        assert len(asked) == 1
        assert isinstance(asked[0], LLMRequest)
        assert asked[0].model == seat.model
        assert asked[0].system == "", "effective_params needs no prompts"


class TestNoCouncilModuleReachesAProviderDirectly:
    """The structural half of the same claim, checked by reading the source."""

    #: The four re-export shims.  They exist precisely to import from
    #: ``llm.apis``, so they are the only files allowed to.
    SHIMS = {"http.py", "http_common.py", "cli.py", "cli_common.py"}

    def test_no_module_under_council_imports_an_api_type_module(self) -> None:
        import kbutillib.domains.ai.council as council_pkg

        root = Path(council_pkg.__file__).parent
        offenders: List[str] = []
        for path in sorted(root.rglob("*.py")):
            if path.parent.name == "backends" and path.name in self.SHIMS:
                continue
            text = path.read_text(encoding="utf-8")
            for line_no, line in enumerate(text.splitlines(), start=1):
                stripped = line.strip()
                # Only real import statements.  A docstring that NAMES
                # ``llm.apis`` to say "this is a shim over it" is documentation,
                # and a grep that failed on prose would be unmaintainable.
                if not stripped.startswith(("from ", "import ")):
                    continue
                if "llm.apis" in stripped or "llm import apis" in stripped:
                    offenders.append(f"{path.relative_to(root)}:{line_no}: {stripped}")
        assert offenders == [], (
            "a council module reached into llm/apis directly; go through "
            "llm.get_api / llm.acomplete instead:\n" + "\n".join(offenders)
        )

    def test_no_module_under_council_names_a_provider_class(self) -> None:
        """Not even by name: ``_backend_for``'s dispatch is gone for good."""
        import kbutillib.domains.ai.council as council_pkg

        root = Path(council_pkg.__file__).parent
        classes = (
            "ArgoBackend",
            "OpenAICompatibleBackend",
            "ClaudeCliBackend",
            "CodexCliBackend",
            "HttpBackend",
            "CliBackend",
        )
        offenders: List[str] = []
        for path in sorted(root.rglob("*.py")):
            if path.parent.name == "backends" and path.name in self.SHIMS:
                continue
            for line_no, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                stripped = line.strip()
                if stripped.startswith("#") or ":mod:" in stripped:
                    continue
                for name in classes:
                    if name in stripped and (
                        stripped.startswith(("from ", "import "))
                        or f"{name}(" in stripped
                    ):
                        offenders.append(
                            f"{path.relative_to(root)}:{line_no}: {stripped}"
                        )
        assert offenders == [], "\n".join(offenders)

    def test_council_utils_no_longer_has_a_backend_dispatch(self) -> None:
        from kbutillib.domains.ai.council_utils import CouncilUtils

        assert not hasattr(CouncilUtils, "_backend_for")


# ---------------------------------------------------------------------------
# The CLI billing preflight, through the wrapper
# ---------------------------------------------------------------------------


class TestTheCliBillingGateFollowsItsFiles:
    """Round 3: cached per instance, invalidated by the files it read.

    Covered from this side as well as from the CLI api type's own suite,
    because what a caller actually sees is ``llm.preflight`` /
    ``llm.preflight_api`` -- and the question the invalidation answers is a
    caller's question: "is the answer I was given still true?"
    """

    @staticmethod
    def _backend(home: Path) -> Any:
        from kbutillib.domains.ai.llm.apis.cli import ClaudeCliBackend

        (home / ".claude").mkdir(parents=True, exist_ok=True)
        (home / ".claude" / "settings.json").write_text(
            json.dumps({"env": {}}), encoding="utf-8"
        )
        return ClaudeCliBackend("claude", _home=home, _parent_env={})

    def test_preflight_reports_which_files_it_read(self, tmp_path: Path) -> None:
        """``checked`` is what the invalidation is built on, so it is contract."""
        backend = self._backend(tmp_path / "home")
        report = run(llm.preflight_api(backend))
        assert report["state"] == "ready"
        assert str(tmp_path / "home" / ".claude" / "settings.json") in report["checked"]

    def test_the_gate_re_runs_once_a_file_it_read_changes(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        backend = self._backend(home)

        # First call: ready, and the verdict is cached with the file's stamp.
        assert run(backend._billing_check()) is None
        assert backend._billing_stamps, "the verdict records what it was read from"

        # Unchanged: still cached, no re-read.
        assert run(backend._billing_check()) is None

        # A `claude login` (or an edit) mid-batch changes how the NEXT call
        # bills.  M1 cached the verdict for the life of the instance and would
        # have kept spending on the old answer.
        (home / ".claude" / "settings.json").write_text(
            json.dumps({"apiKeyHelper": "/usr/bin/get-a-key"}), encoding="utf-8"
        )
        gate = run(backend._billing_check())
        assert gate is not None
        assert gate.kind == "billing_guard"
        assert gate.halts_endpoint is True

    def test_a_file_appearing_also_invalidates_it(self, tmp_path: Path) -> None:
        """"There is no settings.local.json" is a fact the verdict rests on."""
        home = tmp_path / "home"
        backend = self._backend(home)
        assert run(backend._billing_check()) is None

        local = home / ".claude" / "settings.local.json"
        assert str(local) in backend._billing_stamps
        assert backend._billing_stamps[str(local)] is None, "recorded as absent"

        local.write_text(
            json.dumps({"env": {"ANTHROPIC_API_KEY": "sk-whatever"}}), encoding="utf-8"
        )
        gate = run(backend._billing_check())
        assert gate is not None
        assert gate.kind == "billing_guard"

    def test_an_api_type_with_no_preflight_is_ready_through_the_wrapper(self) -> None:
        """So "nothing to check" is never reported as "could not check".

        ``CouncilWorker._preflight`` refuses a job on anything but ``ready``,
        so an HTTP endpoint (which has no billing preflight at all) must not
        answer ``unknown`` here or no job would ever be submittable.
        """
        llm.register_api_type("scripted", ScriptedApi)
        assert run(llm.preflight("scripted", {}))["state"] == "ready"


# ---------------------------------------------------------------------------
# Web-search capability
# ---------------------------------------------------------------------------


class SearchingApi(ScriptedApi):
    """A third-party api type that DOES declare the optional attribute."""

    supports_web_search = True


def _builtin_endpoint(name: str) -> Dict[str, Any]:
    """An endpoint mapping for built-in *name* that constructs without a network."""
    if name in ("argo", "openai"):
        return {"name": name, "kind": name, "base_url": "http://127.0.0.1:9/v1"}
    return {"name": name, "kind": name, "executable": "no-such-cli"}


class TestWebSearchCapability:
    """``llm.supports_web_search`` -- an OPTIONAL member, read defensively.

    The capability is a class attribute rather than a probe, so the answer is
    free: nothing is constructed beyond the api type instance itself, no
    provider is asked and no token is spent.
    """

    @pytest.mark.parametrize(
        "name, expected",
        [
            ("argo", False),
            ("openai", False),
            ("claude_cli", True),
            ("codex_cli", True),
        ],
    )
    def test_each_builtin_reports_its_own_answer(
        self, name: str, expected: bool
    ) -> None:
        assert llm.supports_web_search(name, _builtin_endpoint(name)) is expected
        run(llm.aclose_all())

    def test_only_the_two_cli_types_can_search(self) -> None:
        """Stated as a set, so a new built-in cannot quietly join the true side."""
        searching = {
            name
            for name in llm.BUILTIN_API_TYPES
            if llm.supports_web_search(name, _builtin_endpoint(name))
        }
        run(llm.aclose_all())
        assert searching == {"claude_cli", "codex_cli"}

    def test_every_builtin_exposes_the_attribute_on_its_instance(self) -> None:
        for name in llm.BUILTIN_API_TYPES:
            api = llm.get_api(name, _builtin_endpoint(name))
            assert isinstance(api.supports_web_search, bool), name
        run(llm.aclose_all())

    def test_a_third_party_type_without_the_attribute_registers_and_reports_false(
        self,
    ) -> None:
        """The compatibility claim, in one test.

        ``ScriptedApi`` supplies ONLY the two required members -- no
        ``preflight``, no ``models``, no ``aclose`` and no
        ``supports_web_search``.  It must still register, still satisfy the
        runtime-checkable protocol, and answer ``False`` rather than raise
        ``AttributeError`` at the one place a caller asks.
        """
        assert not hasattr(ScriptedApi, "supports_web_search")

        llm.register_api_type("scripted", ScriptedApi)

        api = llm.get_api("scripted", {})
        assert isinstance(api, ApiType), "an optional member cannot break isinstance"
        assert llm.supports_web_search("scripted", {}) is False

    def test_a_third_party_type_may_declare_it_true(self) -> None:
        """The attribute is the whole contract: no registration, no subclass."""
        llm.register_api_type("searching", SearchingApi)
        assert llm.supports_web_search("searching", {}) is True

    def test_it_is_not_a_required_member(self) -> None:
        """Required means "registration refuses you without it", which this is not."""
        assert "supports_web_search" not in llm.REQUIRED_MEMBERS
        assert llm.REQUIRED_MEMBERS == ("complete", "effective_params")
        # And the protocol itself does not demand it either, which is what
        # keeps ``isinstance`` usable as the sanity check it is advertised as.
        assert "supports_web_search" not in getattr(
            ApiType, "__protocol_attrs__", set()
        )

    def test_an_unknown_api_type_raises_keyerror_like_every_other_verb(self) -> None:
        with pytest.raises(KeyError) as excinfo:
            llm.supports_web_search("gemini_direct", {})
        assert "gemini_direct" in str(excinfo.value)

    def test_it_is_part_of_the_published_surface(self) -> None:
        assert "supports_web_search" in llm.__all__

    def test_asking_costs_nothing_and_sends_nothing(self) -> None:
        """A capability question must not become a call.

        ``ScriptedApi`` records every request any instance has ever been
        asked, so an empty log is evidence that asking is free.
        """
        llm.register_api_type("scripted", ScriptedApi)
        llm.supports_web_search("scripted", {})
        assert ScriptedApi.seen == []
