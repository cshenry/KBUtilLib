"""Capability registry — single source of truth for every exposed callable.

``CapabilitySpec`` records everything a transport (CLI, MCP, HTTP API, docs)
needs to expose a capability.  ``CapabilityRegistry`` stores and filters them.

Design goals
------------
* **No heavy imports at module level.**  Pure stdlib + pydantic; no rdkit,
  fastapi, mcp, or any optional backend.
* **Thread-safe for import-time registration.**  A plain ``dict`` protected by
  a ``threading.Lock`` is sufficient — registration happens at import time from
  a single thread in all realistic scenarios.
* **Graceful by default.**  ``CapabilitySpec.availability()`` *never* raises;
  it catches ``BackendUnavailableError`` and returns ``(False, reason)``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from kbutillib.core.errors import BackendUnavailableError, CapabilityNotFound

__all__ = [
    "CapabilitySpec",
    "CapabilityRegistry",
    "get_registry",
    "BackendUnavailableError",  # re-exported for convenience
]


# ---------------------------------------------------------------------------
# CapabilitySpec
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilitySpec:
    """Everything a transport needs to expose a single capability.

    Parameters
    ----------
    name:
        Canonical dotted identifier, e.g. ``"biochem.search_compounds"``.
        Must be unique across the registry.
    fn:
        The bound callable (resolver) — typically a bound ``*Impl`` method.
        Excluded from equality and hashing (only ``name`` matters there).
    description:
        Human-readable description; populated from the docstring when omitted.
    domain:
        Grouping string, e.g. ``"biochem"``, ``"thermo"``, ``"cheminformatics"``.
    summary:
        Short one-line summary (used in catalog / CLI help).  Defaults to the
        first line of *description*.
    tags:
        Tuple of arbitrary tag strings for filtering.
    input_model:
        Optional pydantic ``BaseModel`` subclass describing request parameters.
    output_model:
        Optional pydantic ``BaseModel`` subclass describing the response.
    visibility:
        ``"public"`` (opt-in for external exposure) or ``"internal"`` (default).
    transports:
        Frozenset of transport names this capability is exposed on.
        Defaults to all four: ``{"lib", "cli", "mcp", "api"}``.
    _availability_fn:
        Optional zero-arg callable that returns ``True`` / ``False`` or raises
        ``BackendUnavailableError``.  Called only on explicit
        ``spec.availability()`` / ``registry.status()`` — never at import time.
    """

    name: str
    fn: Callable[..., Any] = field(hash=False, compare=False)
    description: str = ""
    domain: str = ""
    summary: str = ""
    tags: tuple[str, ...] = ()
    input_model: type[BaseModel] | None = field(default=None, hash=False, compare=False)
    output_model: type[BaseModel] | None = field(default=None, hash=False, compare=False)
    visibility: str = "internal"  # "internal" | "public"
    transports: frozenset[str] = frozenset({"lib", "cli", "mcp", "api"})
    _availability_fn: Callable[[], bool] | None = field(
        default=None, hash=False, compare=False, repr=False
    )

    def availability(self) -> tuple[bool, str | None]:
        """Return ``(available: bool, reason: str | None)``.

        Calls the underlying availability function if one is registered and
        converts any raised ``BackendUnavailableError`` into
        ``(False, reason_string)``.  Returns ``(True, None)`` when no
        availability function was provided (optimistic default).

        This method **never raises**.
        """
        if self._availability_fn is None:
            return True, None
        try:
            result = self._availability_fn()
            if isinstance(result, tuple):
                # Allow the fn to return (bool, reason) directly.
                available, reason = result
                return bool(available), (str(reason) if reason is not None else None)
            return bool(result), None
        except BackendUnavailableError as exc:
            return False, str(exc) if str(exc) else exc.reason or exc.backend or "unavailable"
        except Exception as exc:  # noqa: BLE001
            return False, f"availability check failed: {exc}"

    def effective_summary(self) -> str:
        """Return summary, falling back to the first line of description."""
        if self.summary:
            return self.summary
        if self.description:
            return self.description.splitlines()[0]
        return self.name


# ---------------------------------------------------------------------------
# CapabilityRegistry
# ---------------------------------------------------------------------------


class CapabilityRegistry:
    """Thread-safe registry of :class:`CapabilitySpec` objects.

    Usage
    -----
    ::

        registry = CapabilityRegistry()
        registry.register(CapabilitySpec(name="bio.foo", fn=my_fn, domain="bio"))
        spec = registry.get("bio.foo")
        bio_caps = registry.list(domain="bio")
    """

    def __init__(self) -> None:
        self._specs: dict[str, CapabilitySpec] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Write API
    # ------------------------------------------------------------------

    def register(self, spec: CapabilitySpec) -> None:
        """Register a :class:`CapabilitySpec`.

        Parameters
        ----------
        spec:
            The capability spec to register.

        Raises
        ------
        ValueError
            If a capability with the same *name* is already registered.
        """
        if not isinstance(spec, CapabilitySpec):
            raise TypeError(f"Expected CapabilitySpec, got {type(spec).__name__}")
        with self._lock:
            if spec.name in self._specs:
                raise ValueError(
                    f"Capability {spec.name!r} is already registered. "
                    "Use a unique name or call clear() first."
                )
            self._specs[spec.name] = spec

    def clear(self) -> None:
        """Remove all registered capabilities (useful in tests)."""
        with self._lock:
            self._specs.clear()

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------

    def get(self, name: str) -> CapabilitySpec:
        """Return the :class:`CapabilitySpec` for *name*.

        Raises
        ------
        CapabilityNotFound
            If *name* is not in the registry.
        """
        with self._lock:
            try:
                return self._specs[name]
            except KeyError:
                raise CapabilityNotFound(name) from None

    def list(
        self,
        *,
        domain: str | None = None,
        tag: str | None = None,
        transport: str | None = None,
        visibility: str | None = None,
    ) -> list[CapabilitySpec]:
        """Return a filtered list of registered :class:`CapabilitySpec` objects.

        All filters are ANDed together; omit a filter (``None``) to skip it.

        Parameters
        ----------
        domain:
            Keep only specs whose ``domain`` equals this string.
        tag:
            Keep only specs that have this string in their ``tags``.
        transport:
            Keep only specs whose ``transports`` set contains this string.
        visibility:
            Keep only specs whose ``visibility`` equals this string
            (``"public"`` or ``"internal"``).
        """
        with self._lock:
            specs = list(self._specs.values())

        if domain is not None:
            specs = [s for s in specs if s.domain == domain]
        if tag is not None:
            specs = [s for s in specs if tag in s.tags]
        if transport is not None:
            specs = [s for s in specs if transport in s.transports]
        if visibility is not None:
            specs = [s for s in specs if s.visibility == visibility]
        return specs

    def status(self, name: str) -> tuple[bool, str | None]:
        """Return ``(available, reason)`` for *name* without raising.

        Delegates to :meth:`CapabilitySpec.availability`.  Raises
        :exc:`CapabilityNotFound` if *name* is not registered.
        """
        return self.get(name).availability()

    # ------------------------------------------------------------------
    # Container protocol
    # ------------------------------------------------------------------

    def __contains__(self, name: object) -> bool:
        with self._lock:
            return name in self._specs

    def __len__(self) -> int:
        with self._lock:
            return len(self._specs)

    def __iter__(self) -> Iterator[CapabilitySpec]:
        with self._lock:
            specs = list(self._specs.values())
        return iter(specs)

    def __repr__(self) -> str:
        return f"CapabilityRegistry(n={len(self)})"


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_registry: CapabilityRegistry | None = None
_registry_lock = threading.Lock()


def get_registry() -> CapabilityRegistry:
    """Return the global :class:`CapabilityRegistry` singleton.

    Creates the registry on first call (thread-safe).  All transports and
    collectors share this instance.

    Returns
    -------
    CapabilityRegistry
        The process-wide registry singleton.
    """
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = CapabilityRegistry()
    return _registry
