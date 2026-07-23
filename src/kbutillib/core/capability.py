"""``@capability`` decorator + collector for the KBUtilLib capability registry.

Design goals
------------
* **Inert to normal calls.**  Decorating a method with ``@capability`` changes
  absolutely nothing about calling it — same signature, same return value, same
  exceptions.  The decorator only attaches a ``__kbu_capability__`` attribute to
  the function for later discovery by the collector.
* **No heavy imports at module level.**  Pure stdlib; pydantic and core imports
  are local where heavy.
* **Collector is optional.**  Un-decorated methods work fine; they are simply
  not registered.  Decorated methods on objects that are never passed to
  ``register_all`` are also silently ignored.
* **Availability degrades gracefully.**  A capability whose owning util has no
  ``available``/``unavailable_reason`` probes is assumed always-available.  If
  the util *is* unavailable, the capability still registers (listed), but its
  ``availability()`` returns ``(False, reason)``.

Public API
----------
``capability(name=None, *, domain, summary=None, tags=(), input_model=None,
              output_model=None, visibility="public", transports=None,
              availability=None)``
    Decorator factory.  ``name`` defaults to ``"<domain>.<fn.__name__>"``.

``register_all(app, registry=None) -> list[CapabilitySpec]``
    Walk all ``*Impl`` util attributes on *app* (a ``KBUtilLib`` facade instance),
    find decorated methods, build ``CapabilitySpec``s, register into *registry*
    (defaults to the global singleton).  Returns the list of newly-registered
    specs.

``collect_capabilities(obj) -> list[CapabilitySpec]``
    Inspect a single object (does NOT register) — useful for tests and tooling.
    Returns ``CapabilitySpec`` objects with ``_availability_fn`` wired but not
    yet registered anywhere.
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from typing import Any, Callable

__all__ = [
    "capability",
    "register_all",
    "collect_capabilities",
    "CapabilityDraft",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal draft — attached to the function at decoration time
# ---------------------------------------------------------------------------


@dataclass
class CapabilityDraft:
    """Metadata stashed on a function by ``@capability``.

    This is NOT a :class:`~kbutillib.core.registry.CapabilitySpec`; it is
    a lightweight draft that the collector later converts into a full spec once
    a bound instance is available.
    """

    name: str | None  # None → resolved at collect time to f"{domain}.{fn.__name__}"
    domain: str
    summary: str | None
    tags: tuple[str, ...]
    input_model: Any | None  # type[BaseModel] | None
    output_model: Any | None  # type[BaseModel] | None
    visibility: str
    transports: frozenset[str] | None  # None → use CapabilitySpec default
    availability: Callable[[], tuple[bool, str | None]] | None


# ---------------------------------------------------------------------------
# Sentinel attribute name
# ---------------------------------------------------------------------------

_CAPABILITY_ATTR = "__kbu_capability__"


# ---------------------------------------------------------------------------
# @capability decorator
# ---------------------------------------------------------------------------


def capability(
    name: str | None = None,
    *,
    domain: str,
    summary: str | None = None,
    tags: tuple[str, ...] = (),
    input_model: Any | None = None,
    output_model: Any | None = None,
    visibility: str = "public",
    transports: frozenset[str] | None = None,
    availability: Callable[[], tuple[bool, str | None]] | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Mark a method as a KBUtilLib capability.

    The decorator is **completely inert** to normal calls: the returned
    callable behaves identically to the original (same signature, return
    value, and raised exceptions).  It only attaches a
    :class:`CapabilityDraft` to ``fn.__kbu_capability__`` for later
    discovery by :func:`register_all` / :func:`collect_capabilities`.

    Parameters
    ----------
    name:
        Canonical dotted capability identifier (e.g.
        ``"biochem.search_compounds"``).  Defaults to
        ``f"{domain}.{fn.__name__}"`` if not given.
    domain:
        Grouping domain string (required).
    summary:
        Short one-line description shown in catalogs.  Falls back to the
        first line of the function's docstring if omitted.
    tags:
        Tuple of arbitrary string tags for filtering.
    input_model:
        Optional pydantic ``BaseModel`` subclass for request parameters.
    output_model:
        Optional pydantic ``BaseModel`` subclass for responses.
    visibility:
        ``"public"`` (exposed externally) or ``"internal"`` (default for
        most capabilities; ``"public"`` here because the decorator is
        typically applied to methods intended for exposure).
    transports:
        Override the default transport set.  ``None`` keeps the
        :class:`~kbutillib.core.registry.CapabilitySpec` default of
        ``frozenset({"lib", "cli", "mcp", "api"})``.
    availability:
        Optional zero-arg callable returning ``(bool, reason_str | None)``
        or a plain ``bool``.  When provided, takes precedence over the owning
        util's ``available`` / ``unavailable_reason`` probes.

    Returns
    -------
    Callable
        The **original function unchanged** with an extra
        ``__kbu_capability__`` attribute attached.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        # Store draft metadata on the function.
        # The function itself is NOT wrapped — calling it is identical.
        draft = CapabilityDraft(
            name=name,
            domain=domain,
            summary=summary,
            tags=tags,
            input_model=input_model,
            output_model=output_model,
            visibility=visibility,
            transports=transports,
            availability=availability,
        )
        setattr(fn, _CAPABILITY_ATTR, draft)
        return fn  # <-- the EXACT same callable, not a wrapper

    return decorator


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_description(fn: Callable[..., Any]) -> str:
    """Return the full docstring of *fn*, stripped, or empty string."""
    doc = inspect.getdoc(fn)
    return doc if doc else ""


def _build_availability_fn(
    util_obj: Any,
    draft: CapabilityDraft,
) -> Callable[[], tuple[bool, str | None]] | None:
    """Build the zero-arg availability callable for a spec.

    Priority:
    1. ``draft.availability`` — explicit override from the decorator.
    2. ``util_obj.available`` property / attribute — the standard
       ``*Impl`` availability probe.
    3. ``None`` — always-available (registry default).

    The returned callable always returns ``(bool, str | None)``; it is
    wrapped so that :class:`~kbutillib.core.errors.BackendUnavailableError`
    and any other exception are caught by the registry's own
    :meth:`~kbutillib.core.registry.CapabilitySpec.availability` handler.
    """
    if draft.availability is not None:
        return draft.availability

    # Check if the util exposes the standard availability probe.
    # We cannot use hasattr() because it invokes the descriptor and will
    # suppress the exception if the property raises.  Instead we check the
    # MRO for a descriptor named "available", or catch AttributeError to
    # distinguish "not present" from "present but raises".
    _has_available = "available" in type(util_obj).__dict__ or any(
        "available" in vars(cls) for cls in type(util_obj).__mro__
    )

    if _has_available:
        # Capture util_obj in closure.
        def _util_probe() -> tuple[bool, str | None]:
            try:
                avail = util_obj.available
                if avail:
                    return True, None
                reason: str | None = None
                try:
                    reason = util_obj.unavailable_reason
                except Exception:  # noqa: BLE001
                    pass
                return False, reason
            except Exception as exc:  # noqa: BLE001
                # Raise as BackendUnavailableError so the registry catches it.
                from kbutillib.core.errors import BackendUnavailableError

                raise BackendUnavailableError(reason=str(exc)) from exc

        return _util_probe

    return None


def _spec_from_draft(
    draft: CapabilityDraft,
    fn: Callable[..., Any],
    bound_fn: Callable[..., Any],
    util_obj: Any,
) -> Any:  # -> CapabilitySpec
    """Build a :class:`~kbutillib.core.registry.CapabilitySpec` from a draft.

    Imports ``CapabilitySpec`` locally to avoid heavy import at module level.
    """
    from kbutillib.core.registry import CapabilitySpec

    resolved_name = draft.name if draft.name is not None else f"{draft.domain}.{fn.__name__}"
    description = _extract_description(fn)
    summary = draft.summary if draft.summary is not None else ""

    kwargs: dict[str, Any] = dict(
        name=resolved_name,
        fn=bound_fn,
        description=description,
        domain=draft.domain,
        summary=summary,
        tags=draft.tags,
        input_model=draft.input_model,
        output_model=draft.output_model,
        visibility=draft.visibility,
        _availability_fn=_build_availability_fn(util_obj, draft),
    )
    if draft.transports is not None:
        kwargs["transports"] = draft.transports

    return CapabilitySpec(**kwargs)


# ---------------------------------------------------------------------------
# collect_capabilities — introspect a single object (no registration)
# ---------------------------------------------------------------------------


def collect_capabilities(obj: Any) -> list[Any]:  # list[CapabilitySpec]
    """Collect :class:`~kbutillib.core.registry.CapabilitySpec`\\s from *obj*.

    Walks all methods on *obj* (including inherited ones) and returns a
    :class:`~kbutillib.core.registry.CapabilitySpec` for each decorated with
    ``@capability``.

    This function does **not** register anything; it is intended for tests and
    tooling that need to introspect a single object without a full ``KBUtilLib``
    facade.

    Parameters
    ----------
    obj:
        Any Python object whose methods may be decorated with ``@capability``.

    Returns
    -------
    list[CapabilitySpec]
        Zero or more specs; order follows method discovery (alphabetical by
        default in Python 3.7+ dict ordering).
    """
    specs: list[Any] = []
    seen: set[str] = set()

    for attr_name in dir(obj):
        if attr_name.startswith("__"):
            continue
        try:
            attr = getattr(type(obj), attr_name, None) or getattr(obj, attr_name)
        except Exception:  # noqa: BLE001
            continue

        # Unwrap bound methods, staticmethods, classmethods
        raw_fn = _unwrap(attr)
        if raw_fn is None or not callable(raw_fn):
            continue

        draft: CapabilityDraft | None = getattr(raw_fn, _CAPABILITY_ATTR, None)
        if draft is None:
            continue

        resolved_name = (
            draft.name if draft.name is not None else f"{draft.domain}.{raw_fn.__name__}"
        )
        if resolved_name in seen:
            continue
        seen.add(resolved_name)

        # Build a bound callable from the live instance
        try:
            bound = getattr(obj, attr_name)
        except Exception:  # noqa: BLE001
            continue

        spec = _spec_from_draft(draft, raw_fn, bound, obj)
        specs.append(spec)

    return specs


def _unwrap(attr: Any) -> Callable[..., Any] | None:
    """Return the underlying raw function from a method/staticmethod/etc."""
    if inspect.isfunction(attr):
        return attr
    if inspect.ismethod(attr):
        return attr.__func__
    if isinstance(attr, (staticmethod, classmethod)):
        return attr.__func__  # type: ignore[union-attr]
    if callable(attr):
        # Could be a bound method retrieved directly from instance
        underlying = getattr(attr, "__func__", None)
        if underlying is not None:
            return underlying
        # Plain callable with __kbu_capability__ set
        if hasattr(attr, _CAPABILITY_ATTR):
            return attr
    return None


# ---------------------------------------------------------------------------
# register_all — walk KBUtilLib facade and register everything
# ---------------------------------------------------------------------------

# Attribute names of *Impl util objects on the KBUtilLib facade.
# We look for objects that have at least one @capability-decorated method
# rather than hard-coding a list, so new utils are picked up automatically.
# However, we skip private/dunder attrs and known non-util scalars.

_SKIP_ATTRS = frozenset({"env"})


def register_all(
    app: Any,
    registry: Any = None,  # CapabilityRegistry | None
) -> list[Any]:  # list[CapabilitySpec]
    """Walk *app*'s util properties, collect decorated methods, and register.

    Iterates all attributes on *app* that look like ``*Impl`` utility objects
    (have at least one method decorated with ``@capability``), builds
    :class:`~kbutillib.core.registry.CapabilitySpec`\\s, and registers them
    into *registry* (defaults to the global singleton from
    :func:`~kbutillib.core.registry.get_registry`).

    **Binding semantics** — the callable stored in each spec is a *bound
    method* of the util object retrieved from *app*.  This means DI injection
    in the ``KBUtilLib`` facade (sibling sharing of biochem/ws/etc.) is
    preserved for free.

    **Availability semantics** — for each spec, the availability callable
    checks ``util.available`` if present, else the decorator's explicit
    ``availability`` kwarg.  A util whose ``available`` raises
    :class:`~kbutillib.core.errors.BackendUnavailableError` produces a spec
    that returns ``(False, reason)`` without crashing.

    **Non-eager** — accessing a lazy-property on the KBUtilLib facade *does*
    instantiate that util, but does NOT import heavy backends; the ``*Impl``
    constructors are designed to defer heavy imports to first use.  If a util
    property itself raises on instantiation, it is logged and skipped.

    Parameters
    ----------
    app:
        A ``KBUtilLib`` facade instance (or any object with util attributes).
    registry:
        A :class:`~kbutillib.core.registry.CapabilityRegistry` to register
        into.  Defaults to the global singleton.

    Returns
    -------
    list[CapabilitySpec]
        The newly-registered specs (does not include pre-existing ones).
    """
    from kbutillib.core.registry import get_registry

    if registry is None:
        registry = get_registry()

    registered: list[Any] = []

    for attr_name in dir(app):
        if attr_name.startswith("_") or attr_name in _SKIP_ATTRS:
            continue

        # Retrieve the util object — lazy properties are accessed here
        try:
            util_obj = getattr(app, attr_name)
        except Exception as exc:  # noqa: BLE001
            logger.debug("register_all: skipping %r — getattr raised: %s", attr_name, exc)
            continue

        if not hasattr(util_obj, "__dict__") and not inspect.ismodule(util_obj):
            # Scalars, None, strings, etc.
            continue

        # Collect capabilities from this util object
        specs = collect_capabilities(util_obj)
        for spec in specs:
            try:
                registry.register(spec)
                registered.append(spec)
            except ValueError as exc:
                # Duplicate name — skip with a warning
                logger.warning("register_all: skipping duplicate capability %r: %s", spec.name, exc)

    return registered
