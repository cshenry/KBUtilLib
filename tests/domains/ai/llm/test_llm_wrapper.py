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
