"""The one public wrapper every LLM call in KBUtilLib goes through.

Chris, review round 2 (2026-10-15): *"send ALL LLM calls through a common
wrapper function that accepts the API types as an argument, then we can
implement a wide range of API types within this function."*  That is what
this module is.  A caller says WHAT to ask (:class:`~.types.LLMRequest`),
WHICH kind of provider to ask it of (``api_type``, a string) and WHERE
(``endpoint``, a plain mapping); it never imports a provider class and never
learns one's constructor.

Example -- the whole public surface in five lines::

    from kbutillib.domains.ai import llm

    request = llm.LLMRequest(model="gemini35flash", user="Pick an id.")
    result = await llm.acomplete(request, api_type="argo", endpoint={})
    #   ... or, from synchronous code:
    result = llm.complete(request, api_type="argo", endpoint={})

Four API types are built in -- ``argo``, ``openai``, ``claude_cli`` and
``codex_cli`` -- and a fifth kind can be added from ANOTHER distribution
without editing this package, by declaring a factory in the
``kbutillib.llm_api_types`` entry-point group::

    [project.entry-points."kbutillib.llm_api_types"]
    my_gateway = "my_pkg.llm:make_api"

The group is read ONCE, lazily, the first time an unknown name is looked up:
scanning installed distributions is not free, and a process that only ever
uses the built-ins must not pay for it.  ``register_api_type`` is the
in-process door to the same registry, which is what tests (and a notebook)
use.

``fake`` and ``replay`` are deliberately NOT registered.  A test registers
them itself, so no shipped configuration can ever name an endpoint that
silently answers from a script instead of from a model.

The shipped api types live in the ``llm.apis`` subpackage and NOT in one
called ``api_types``, which is the name the design prompt used: importing a
submodule binds its name on the parent package, so a ``llm/api_types/``
directory would overwrite the :func:`api_types` function on this module the
moment the first provider was constructed -- and :func:`api_types` is the
name the config loader and the CLI call.  Nothing outside this package should
import from ``llm.apis`` at all; go through :func:`get_api`.

Three properties this module is built to keep:

* **Exactly one attempt per call.**  Retries, backoff, budgets, halts,
  concurrency limits and single-flight collapsing belong to the caller (for
  the council, to ``CouncilWorker``) because only the caller knows what a
  second attempt would cost.  :func:`acomplete` tries once.
* **It never raises for a call failure.**  An API type that misbehaves and
  raises anyway is converted, so the contract holds even for a third-party
  plug-in that has not read it.  A *programming* error -- an unknown
  ``api_type`` -- does raise, immediately and by name.
* **A client is never shared across event loops.**  :func:`get_api` caches
  per ``(api_type, endpoint, running loop)``, because an ``httpx.AsyncClient``
  or an ``asyncio`` primitive built on a closed loop fails when awaited, and
  the synchronous facades in this repository each own a fresh loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import logging
import threading
import weakref
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from .protocol import REQUIRED_MEMBERS, ApiType, missing_members
from .types import (
    LLM_ERROR_KINDS,
    AuthError,
    BillingGuardRefused,
    CallError,
    CallTimeout,
    InvalidOutput,
    LLMError,
    LLMException,
    LLMRequest,
    LLMResult,
    ModelUnavailable,
    QuotaExhausted,
    TransportError,
    as_llm_error,
    as_llm_request,
    generation_params,
)

logger = logging.getLogger(__name__)

__all__ = [
    "BUILTIN_API_TYPES",
    "ENTRY_POINT_GROUP",
    "LLM_ERROR_KINDS",
    "REQUIRED_MEMBERS",
    "ApiType",
    "AuthError",
    "BillingGuardRefused",
    "CallError",
    "CallTimeout",
    "InvalidOutput",
    "LLMError",
    "LLMException",
    "LLMRequest",
    "LLMResult",
    "ModelUnavailable",
    "QuotaExhausted",
    "TransportError",
    "aclose_all",
    "acomplete",
    "api_types",
    "as_llm_error",
    "as_llm_request",
    "complete",
    "endpoint_key",
    "generation_params",
    "get_api",
    "models",
    "models_api",
    "preflight",
    "preflight_api",
    "register_api_type",
]

#: Entry-point group another distribution declares an API type factory in.
ENTRY_POINT_GROUP = "kbutillib.llm_api_types"

#: The API types this package ships.  ``fake``/``replay`` are not here on
#: purpose -- see the module docstring.
BUILTIN_API_TYPES = ("argo", "claude_cli", "codex_cli", "openai")

#: ``{name: factory}``.  A factory takes one endpoint mapping and returns an
#: :class:`~.protocol.ApiType`.
_REGISTRY: Dict[str, Callable[[Mapping[str, Any]], Any]] = {}

#: ``{(api_type, endpoint key, loop id): instance}``.  See :func:`get_api`.
_CACHE: Dict[Tuple[str, str, int], Any] = {}

#: Instances dropped from the cache (by a ``replace=True`` registration, or
#: by losing a construction race) and still owing an ``aclose``.
_ORPHANS: List[Any] = []

#: ``{event loop: token}``.  The third part of a cache key, and deliberately
#: NOT ``id(loop)``: CPython reuses the address of a closed-and-collected
#: loop, so two successive ``complete()`` calls really do land on two loops
#: with the same ``id``, and the second would be handed the first's client.
#: A weak-keyed counter is stable for the life of a loop and reused by
#: nothing.  ``0`` is "no running loop".
_LOOP_TOKENS: "weakref.WeakKeyDictionary[Any, int]" = weakref.WeakKeyDictionary()
_LOOP_COUNTER = itertools.count(1)

#: One lock over all three, because a daemon runs the wrapper from several
#: tasks and a plug-in's first construction must not happen twice.
_LOCK = threading.RLock()

_entry_points_loaded = False


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


def register_api_type(
    name: str,
    factory: Callable[[Mapping[str, Any]], Any],
    *,
    replace: bool = False,
) -> None:
    """Register *factory* as the way to reach API type *name*.

    Args:
        name: the ``kind`` string a configuration names.
        factory: called with ONE endpoint mapping, returns an
            :class:`~.protocol.ApiType`.
        replace: permit overwriting an existing name.  Without it an existing
            name is a :class:`ValueError`, because two distributions quietly
            claiming one kind is how a call ends up at a provider nobody
            chose.

    Raises:
        ValueError: *name* is empty, *factory* is not callable, or *name* is
            already registered and *replace* is false.
        TypeError: *factory* is a CLASS that does not define
            :data:`REQUIRED_MEMBERS`.  Checked here, without constructing
            anything -- registering ``SomeClass`` is the usual shape and a
            malformed one is refused at registration rather than at the first
            call.  A factory FUNCTION cannot be inspected this way (its
            product does not exist yet), so one is checked by
            :func:`get_api` the first time it builds an instance; constructing
            a throwaway probe here instead would mean every registration
            opened a client nobody asked for.
    """
    if not name or not isinstance(name, str):
        raise ValueError(f"an API type name must be a non-empty string, got {name!r}")
    if not callable(factory):
        raise ValueError(
            f"the factory for api type {name!r} must be callable, got "
            f"{type(factory).__name__}"
        )
    if isinstance(factory, type):
        _require_api_shape(name, factory)

    with _LOCK:
        if name in _REGISTRY and not replace:
            raise ValueError(
                f"api type {name!r} is already registered; pass replace=True to "
                f"override it. Registered: {sorted(_REGISTRY)}"
            )
        if name in _REGISTRY:
            _drop_cached(name)
        _REGISTRY[name] = factory


def api_types() -> List[str]:
    """Every registered API type name, sorted.

    Reads the entry-point group first, so the answer is the whole set a
    configuration may legitimately name -- which is what
    ``council.config`` validates an endpoint's ``kind`` against.
    """
    _load_entry_points()
    with _LOCK:
        return sorted(_REGISTRY)


def get_api(api_type: str, endpoint: Optional[Mapping[str, Any]] = None) -> Any:
    """The :class:`~.protocol.ApiType` instance for *api_type* at *endpoint*.

    Cached per ``(api_type, stable hash of the endpoint mapping, id of the
    running event loop)``:

    * the **endpoint** is in the key because nine vLLM ports are nine
      clients, and because changing an endpoint's mapping must not keep
      handing back the client built for the old one;
    * the **loop** is in the key because the synchronous facades in this
      repository each create and close a loop of their own, and an
      ``httpx.AsyncClient`` (or any ``asyncio`` primitive) built on a closed
      loop raises when awaited.  Outside a running loop the id is 0.

    Raises:
        KeyError: *api_type* is not registered.  The message names every
            registered type, because this is nearly always a config typo.
        TypeError: the factory's product lacks :data:`REQUIRED_MEMBERS`.
    """
    factory = _resolve(api_type)
    key = (api_type, endpoint_key(endpoint), _loop_id())
    with _LOCK:
        api = _CACHE.get(key)
        if api is not None:
            return api
    api = factory(dict(endpoint or {}))
    _require_api_shape(api_type, api)
    with _LOCK:
        existing = _CACHE.get(key)
        if existing is not None:
            # Another task won the race; keep one instance per key and let the
            # loser be closed by aclose_all rather than leaked.
            _ORPHANS.append(api)
            return existing
        _CACHE[key] = api
    return api


def endpoint_key(endpoint: Optional[Mapping[str, Any]]) -> str:
    """A stable hash of *endpoint*, insensitive to key order.

    Two mappings that differ only in insertion order are one endpoint and
    must share one client; two that differ in any value are not.
    """
    payload = json.dumps(
        _jsonable(dict(endpoint or {})),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Calling
# ---------------------------------------------------------------------------


async def acomplete(
    request: Any,
    *,
    api_type: str,
    endpoint: Optional[Mapping[str, Any]] = None,
) -> LLMResult:
    """Run ONE model call and return its :class:`~.types.LLMResult`.

    Exactly one attempt: no retry, no backoff, no budget and no halt live
    here.  *request* may be an :class:`~.types.LLMRequest` or any record
    :func:`~.types.as_llm_request` accepts.

    Never raises for a call failure.  An API type that breaks its own
    contract and raises is converted to ``ok=False`` with a typed error, so
    one misbehaving plug-in cannot abort a caller's batch.  An unknown
    *api_type* DOES raise :class:`KeyError` -- that is a configuration
    mistake, not a model failure, and silently reporting it as one would have
    a caller retry a name that can never work.
    """
    api = get_api(api_type, endpoint)
    payload = as_llm_request(request)
    try:
        return await api.complete(payload)
    except Exception as exc:  # noqa: BLE001 - an api type never raises
        logger.warning(
            "llm: api type %r raised instead of returning a failed result: %s: %s",
            api_type,
            type(exc).__name__,
            exc,
        )
        return LLMResult(ok=False, error=as_llm_error(exc))


def complete(
    request: Any,
    *,
    api_type: str,
    endpoint: Optional[Mapping[str, Any]] = None,
) -> LLMResult:
    """The synchronous :func:`acomplete`.

    Runs on a loop this call owns.  Deliberately NOT ``asyncio.run``: that
    sets the thread's current event loop to ``None`` on the way out, and
    other modules in this repository still call ``asyncio.get_event_loop()``
    -- the council's own two synchronous facades avoid it for exactly that
    reason, recorded in ``engine/worker.py::_run_coroutine``.  Owning a loop
    locally gives the same "fresh loop per sync call" property and leaves the
    process-global asyncio state as it was found.

    Raises:
        RuntimeError: called from inside a running event loop.  A loop cannot
            be nested inside a loop; await :func:`acomplete` there.
    """
    coro = acomplete(request, api_type=api_type, endpoint=endpoint)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        coro.close()
        raise RuntimeError(
            "llm.complete() is the synchronous form and an event loop is already "
            "running on this thread; await llm.acomplete(...) instead"
        )
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# The free probes
# ---------------------------------------------------------------------------


async def preflight(
    api_type: str, endpoint: Optional[Mapping[str, Any]] = None
) -> Dict[str, Any]:
    """"Would this endpoint work, and how would it bill?"  Spends nothing.

    Returns ``{"state": "ready"|"refused"|"unknown", ...}``.  An API type
    with no ``preflight`` of its own is ``ready``: there is nothing about it
    that could refuse, and reporting ``unknown`` would make every HTTP
    endpoint look unverifiable.
    """
    return await preflight_api(get_api(api_type, endpoint))


async def preflight_api(api: Any) -> Dict[str, Any]:
    """:func:`preflight` for an ALREADY-RESOLVED API type instance.

    The instance form exists because a caller that has built its endpoints up
    front (``CouncilUtils.build_backends``) holds the instances already, and
    resolving them a second time would run a CLI's config-file reads twice
    per job.
    """
    probe = getattr(api, "preflight", None)
    if probe is None:
        return {
            "state": "ready",
            "reasons": [
                f"{type(api).__name__} declares no preflight, so there is nothing "
                "about it that could refuse"
            ],
            "checked": [],
            "detail": "",
        }
    report = dict(await probe() or {})
    report.setdefault("state", "unknown")
    report.setdefault("reasons", [])
    report.setdefault("checked", [])
    report.setdefault("detail", "; ".join(str(r) for r in report["reasons"]))
    return report


async def models(
    api_type: str, endpoint: Optional[Mapping[str, Any]] = None
) -> List[Dict[str, Any]]:
    """The models *api_type* can serve at *endpoint*.  Free; ``[]`` is not an error."""
    return await models_api(get_api(api_type, endpoint))


async def models_api(api: Any) -> List[Dict[str, Any]]:
    """:func:`models` for an already-resolved API type instance."""
    lister = getattr(api, "models", None)
    if lister is None:
        return []
    listing = await lister()
    return [dict(entry) for entry in (listing or []) if isinstance(entry, Mapping)]


async def aclose_all() -> None:
    """Release every cached (and every orphaned) API type instance.

    A close never fails a caller: an API type whose ``aclose`` raises is
    logged and the rest are still closed.
    """
    with _LOCK:
        victims = list(_CACHE.values()) + list(_ORPHANS)
        _CACHE.clear()
        del _ORPHANS[:]
    for api in victims:
        closer = getattr(api, "aclose", None)
        if closer is None:
            continue
        try:
            await closer()
        except Exception as exc:  # noqa: BLE001 - a close never fails a verb
            logger.debug(
                "llm: closing %s failed: %s: %s", type(api).__name__, type(exc).__name__, exc
            )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _resolve(api_type: str) -> Callable[[Mapping[str, Any]], Any]:
    """The factory for *api_type*, reading the entry-point group if needed."""
    with _LOCK:
        factory = _REGISTRY.get(api_type)
    if factory is not None:
        return factory
    _load_entry_points()
    with _LOCK:
        factory = _REGISTRY.get(api_type)
        if factory is None:
            raise KeyError(
                f"no LLM api type {api_type!r} is registered; registered types are "
                f"{sorted(_REGISTRY)}. Declare one in the {ENTRY_POINT_GROUP!r} "
                "entry-point group, or call llm.register_api_type()."
            )
    return factory


def _load_entry_points() -> None:
    """Read the :data:`ENTRY_POINT_GROUP` group once per process.

    Lazy because scanning installed distributions costs real time and a
    process that only uses the built-ins must not pay for it.  A plug-in
    whose object cannot be loaded is logged and skipped: one broken
    third-party distribution must not stop the council from reaching ARGO.
    """
    global _entry_points_loaded
    with _LOCK:
        if _entry_points_loaded:
            return
        _entry_points_loaded = True

    from importlib.metadata import entry_points

    try:
        found = entry_points(group=ENTRY_POINT_GROUP)
    except Exception as exc:  # noqa: BLE001 - discovery never breaks a call
        logger.warning("llm: could not read entry-point group %r: %s", ENTRY_POINT_GROUP, exc)
        return

    for entry in found:
        try:
            factory = entry.load()
        except Exception as exc:  # noqa: BLE001 - one bad plug-in is not fatal
            logger.warning(
                "llm: entry point %r in %r could not be loaded: %s: %s",
                getattr(entry, "name", "?"),
                ENTRY_POINT_GROUP,
                type(exc).__name__,
                exc,
            )
            continue
        name = str(getattr(entry, "name", "") or "")
        with _LOCK:
            if name in _REGISTRY:
                logger.debug(
                    "llm: entry point %r does not override the registered api type "
                    "of the same name",
                    name,
                )
                continue
        try:
            register_api_type(name, factory)
        except (ValueError, TypeError) as exc:
            logger.warning(
                "llm: entry point %r in %r is not a usable api type factory: %s",
                name,
                ENTRY_POINT_GROUP,
                exc,
            )


def _require_api_shape(api_type: str, api: Any) -> None:
    """Refuse *api* unless it carries every :data:`REQUIRED_MEMBERS` member."""
    missing = missing_members(api)
    if missing:
        raise TypeError(
            f"the factory for api type {api_type!r} produced a "
            f"{type(api).__name__}, which has no callable {', '.join(missing)}; "
            f"an api type must provide {', '.join(REQUIRED_MEMBERS)} "
            "(effective_params is what a caller content-addresses its calls on, "
            "so an api type without it cannot be cached or fingerprinted)"
        )


def _drop_cached(api_type: str) -> None:
    """Forget every cached instance of *api_type*; call under :data:`_LOCK`.

    A re-registration means the old factory's clients must not be handed out
    again.  They are moved to :data:`_ORPHANS` rather than dropped on the
    floor, because this is a synchronous function and ``aclose`` is a
    coroutine; :func:`aclose_all` collects them.
    """
    for key in [k for k in _CACHE if k[0] == api_type]:
        _ORPHANS.append(_CACHE.pop(key))


def _loop_id() -> int:
    """A stable token for the running event loop, or 0 when there is none.

    Not ``id(loop)``: a closed loop is collected and CPython hands its address
    straight back to the next one, so two sequential synchronous calls can
    see the same ``id`` for two different loops -- and the second would then
    be given a client bound to a loop that no longer exists.  The token comes
    from a weak-keyed counter instead, and a finalizer drops the loop's cached
    clients when the loop itself goes: they can never be used or even closed
    again, so keeping them would be a leak.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return 0
    with _LOCK:
        token = _LOOP_TOKENS.get(loop)
        if token is None:
            token = next(_LOOP_COUNTER)
            _LOOP_TOKENS[loop] = token
            weakref.finalize(loop, _forget_loop, token)
    return token


def _forget_loop(token: int) -> None:
    """Drop every cached client built on the loop *token* identified."""
    with _LOCK:
        dead = [key for key in _CACHE if key[2] == token]
        for key in dead:
            _CACHE.pop(key, None)
    if dead:
        logger.debug(
            "llm: dropped %d cached client(s) whose event loop is gone", len(dead)
        )


def _jsonable(value: Any) -> Any:
    """Reduce *value* to JSON-native types, recursively, for hashing."""
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


# ---------------------------------------------------------------------------
# The built-ins
# ---------------------------------------------------------------------------
#
# Registered through lazy factories, so ``import kbutillib.domains.ai.llm``
# pulls in neither ``httpx`` nor the subprocess machinery.  Registered through
# the private door rather than ``register_api_type`` for the same reason: the
# public door probes a factory's product, and probing these four would import
# httpx at import time, which is exactly what the lazy loader exists to avoid.
# Their shape is pinned by their own suites and by
# ``test_llm_wrapper.py::TestBuiltins``.


def _make_argo(endpoint: Mapping[str, Any]) -> Any:
    from .apis.http import ArgoBackend

    return ArgoBackend(endpoint)


def _make_openai(endpoint: Mapping[str, Any]) -> Any:
    from .apis.http import OpenAICompatibleBackend

    return OpenAICompatibleBackend(endpoint)


def _make_claude_cli(endpoint: Mapping[str, Any]) -> Any:
    from .apis.cli import ClaudeCliBackend

    return ClaudeCliBackend(
        config=endpoint, allow_unverified=bool(dict(endpoint).get("allow_unverified"))
    )


def _make_codex_cli(endpoint: Mapping[str, Any]) -> Any:
    from .apis.cli import CodexCliBackend

    return CodexCliBackend(
        config=endpoint, allow_unverified=bool(dict(endpoint).get("allow_unverified"))
    )


def _register_builtin(name: str, factory: Callable[[Mapping[str, Any]], Any]) -> None:
    """Register a shipped API type without probing its product."""
    with _LOCK:
        _REGISTRY[name] = factory


_register_builtin("argo", _make_argo)
_register_builtin("openai", _make_openai)
_register_builtin("claude_cli", _make_claude_cli)
_register_builtin("codex_cli", _make_codex_cli)
