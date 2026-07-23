"""Canonical error types for KBUtilLib.

This is the single source of truth for ``BackendUnavailableError`` and its
sibling core errors.  Any module that previously defined its own copy should
re-import from here so ``isinstance`` checks work across module boundaries.
"""

from __future__ import annotations

__all__ = [
    "KBUtilLibError",
    "BackendUnavailableError",
    "CapabilityError",
    "CapabilityNotFound",
]


class KBUtilLibError(Exception):
    """Base class for all KBUtilLib-specific exceptions."""


class BackendUnavailableError(KBUtilLibError):
    """Raised when a required backend dependency is not installed or reachable.

    Catch this error to implement graceful degradation — an unavailable backend
    should never crash ``import kbutillib`` or the capability registry.

    Parameters
    ----------
    backend:
        Human-readable name of the missing backend (e.g. ``"rdkit"``,
        ``"equilibrator"``).
    reason:
        Optional detail message explaining why the backend is unavailable.
    """

    def __init__(self, backend: str = "", reason: str = "") -> None:
        self.backend = backend
        self.reason = reason
        parts = [backend] if backend else []
        if reason:
            parts.append(reason)
        super().__init__(": ".join(parts) if parts else "Backend unavailable")

    def __repr__(self) -> str:  # pragma: no cover
        return f"BackendUnavailableError(backend={self.backend!r}, reason={self.reason!r})"


class CapabilityError(KBUtilLibError):
    """Base class for capability-registry errors."""


class CapabilityNotFound(CapabilityError):
    """Raised when a requested capability name does not exist in the registry.

    Parameters
    ----------
    name:
        The dotted capability name that was not found.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"Capability not found: {name!r}")
