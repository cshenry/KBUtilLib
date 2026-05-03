"""Serializer registry — dispatch objects to format-specific serializers.

Hard rule: NO pickle anywhere.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

_REGISTRY: dict[str, "Serializer"] = {}
_DISPATCH_ORDER: list["Serializer"] = []


@runtime_checkable
class Serializer(Protocol):
    """Protocol every serializer must satisfy."""

    type_name: str
    file_extension: str  # including leading dot, e.g. ".json"

    def can_handle(self, obj: Any) -> bool: ...
    def serialize(self, obj: Any, path: Path) -> dict[str, Any]: ...
    def deserialize(self, path: Path, metadata: dict) -> Any: ...


def register_serializer(s: Serializer) -> None:
    """Register a serializer instance (keyed by type_name)."""
    _REGISTRY[s.type_name] = s
    # Prepend so later-registered (more specific) serializers win
    _DISPATCH_ORDER.insert(0, s)


def get_serializer(type_name: str) -> Serializer:
    """Look up a serializer by its type_name."""
    if type_name not in _REGISTRY:
        raise ValueError(
            f"No serializer registered for type {type_name!r}. "
            f"Available: {list(_REGISTRY.keys())}"
        )
    return _REGISTRY[type_name]


def auto_dispatch(obj: Any) -> Serializer:
    """Find the first registered serializer that can handle *obj*."""
    for s in _DISPATCH_ORDER:
        if s.can_handle(obj):
            return s
    raise TypeError(
        f"No serializer can handle object of type {type(obj).__name__}. "
        f"Register a custom serializer or pass type_hint explicitly."
    )


def list_serializers() -> list[str]:
    """Return all registered type_names."""
    return list(_REGISTRY.keys())


# Auto-import built-in serializers so they register themselves on first use.
def _boot() -> None:
    from . import (  # noqa: F401
        serialize_json,
        serialize_dict,
        serialize_dataframe,
        serialize_text,
        serialize_msgenome,
        serialize_cobra_model,
        serialize_msmodelutil,
        serialize_msexpression,
    )


_boot()
