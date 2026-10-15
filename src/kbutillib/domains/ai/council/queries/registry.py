"""``QueryRegistry``: validate a query type, then hand it to the store.

The registry is the only supported way a query type enters a store.  It does
three things the store deliberately does not:

1. **Validates.**  A spec that names an unknown renderer, uses a binding the
   template renderer does not provide, or invents its own answer shape is
   refused HERE -- before a job can be submitted against it and before a
   token is spent.  The store takes a dict and asks no questions, which is
   right for a store and wrong for an entry point.
2. **Returns models, not rows.**  ``store.get_query_type`` returns the row;
   :meth:`QueryRegistry.get` returns a :class:`..spec.QueryType` you can
   render with, reconstructed from the stored JSON.  That round trip is the
   point: the spec that renders is the spec the database holds, not the
   object that happened to be in memory when it was registered.
3. **Seeds the builtins.**  :meth:`seed_builtins` is idempotent because
   ``put_query_type`` treats an identical re-put as a no-op, so it is safe on
   every start and safe when two installations share one PostgreSQL store.

Immutability is the store's rule, not this module's: re-registering a
DIFFERENT spec under an existing ``(name, version)`` raises
``QueryTypeExists``.  A query type is edited by registering a new version,
because a finished job's verdicts only mean anything against the exact spec
that produced them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Union

from .builtin import (
    BUILTIN_QUERY_TYPES,
    load_builtin_spec,
    resolve_adapter,
    resolve_renderer,
)
from .spec import QueryType, load_spec_mapping

__all__ = ["QueryRegistry", "validate_spec"]

SpecSource = Union[QueryType, Mapping[str, Any], str, Path]


def validate_spec(source: SpecSource) -> QueryType:
    """Build a :class:`..spec.QueryType` from *source* and check it over.

    Accepts a model, a mapping, a path to a YAML file, or YAML text.  Raises
    :class:`..errors.QuerySpecError` (or one of its subclasses) for anything
    that would fail later: an unknown renderer or adapter, a renderer version
    this installation does not ship, a template binding outside the closed
    list, or a user spec that is not using the generic answer schemas.
    """
    spec = source if isinstance(source, QueryType) else QueryType.model_validate(
        load_spec_mapping(source)
    )
    if spec.renderer:
        resolve_renderer(spec.renderer)
    if spec.adapter:
        resolve_adapter(spec.adapter)
    spec.check_templates()
    spec.check_generic_schemas()
    return spec


class QueryRegistry:
    """Validated access to the query types saved in one store."""

    def __init__(self, store: Any) -> None:
        self.store = store

    # -- writing ----------------------------------------------------------

    def register(
        self,
        spec_or_yaml_path: SpecSource,
        created_by: Optional[str] = None,
        source: str = "user",
    ) -> QueryType:
        """Validate a spec and save it.  Immutable once saved.

        Args:
            spec_or_yaml_path: a :class:`..spec.QueryType`, a spec mapping, a
                path to a YAML file, or YAML text.
            created_by: who registered it, recorded on the row.
            source: ``"user"`` or ``"builtin"``; only
                :meth:`seed_builtins` passes the latter.

        Returns:
            The validated query type.

        Raises:
            QuerySpecError: the spec is not usable (see :func:`validate_spec`).
            QueryTypeExists: a DIFFERENT spec already holds this
                ``(name, version)``.
        """
        spec = validate_spec(spec_or_yaml_path)
        self.store.put_query_type(
            spec.name,
            spec.version,
            spec.to_spec(),
            source=source,
            created_by=created_by,
        )
        return spec

    def retire(self, name: str, version: str) -> bool:
        """Hide a query type from lookups without removing it."""
        return self.store.retire_query_type(name, version)

    def seed_builtins(self, created_by: str = "builtin") -> List[QueryType]:
        """Register ``go_mf@1`` and ``reaction_pickup@1``.  Idempotent.

        Safe to call on every start and from two installations against one
        shared store: an identical re-put is a no-op in the store, and the
        shipped YAMLs are the same bytes everywhere, so the specs compare
        equal.  If they ever do not, ``put_query_type`` raises
        ``QueryTypeExists`` -- which is the correct outcome, because it means
        two installations disagree about what ``go_mf@1`` asks.
        """
        out: List[QueryType] = []
        for name, _version in BUILTIN_QUERY_TYPES:
            out.append(
                self.register(
                    load_builtin_spec(name), created_by=created_by, source="builtin"
                )
            )
        return out

    # -- reading ----------------------------------------------------------

    def get(self, name: str, version: Optional[str] = None) -> QueryType:
        """Return a saved query type, latest active version by default.

        Reconstructed from the stored JSON and re-validated, so a spec whose
        renderer this installation does not ship is refused on READ as well as
        on write -- which is the case that matters, because the spec was
        probably written by a different installation.

        Raises:
            KeyError: no such query type.
            QuerySpecError: it is saved but this installation cannot use it.
        """
        row = self.store.get_query_type(name, version)
        if row is None:
            label = f"{name}@{version}" if version else name
            raise KeyError(f"no query type {label!r} in this store")
        return validate_spec(row["spec"])

    def list(self, include_retired: bool = False) -> List[Dict[str, Any]]:
        """Return the saved query-type ROWS, active ones only by default.

        Rows, not models: a listing has to show the retired and the
        unrenderable too (``name``, ``version``, ``status``, ``source``,
        ``created_by``), and building a model would refuse exactly those.
        """
        return self.store.list_query_types(include_retired=include_retired)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"QueryRegistry(store={self.store!r})"
