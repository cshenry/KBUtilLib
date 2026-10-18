"""Query types: the only place in the council a domain vocabulary lives.

A query type says what an item's evidence looks like, what a menu entry looks
like, how the proposer/critic/meta prompts are written, what shape each role
must answer in, and how to rename that answer's domain field names to the
generic ones the engine counts votes on.  Everything else in the council --
the queue, the backends, the vote arithmetic -- is written against the generic
shapes and never learns the word "GO term".

Three things live here:

* :class:`.spec.QueryType` -- the spec model, which IS the ``QueryRenderer``
  protocol implementation.
* :mod:`.template` -- the ``string.Template`` renderer a user-registered query
  type gets, with its closed list of bindings.
* :class:`.registry.QueryRegistry` -- validation plus the store, including
  :meth:`~.registry.QueryRegistry.seed_builtins`, which puts the two shipped
  query types (``go_mf`` and ``reaction_pickup``, both verbatim ports of Jose
  Faria's CONCORDIA councils) into a store idempotently.

Example::

    from kbutillib.domains.ai.council.queries import QueryRegistry
    from kbutillib.domains.ai.council.store import open_store

    store = open_store("sqlite:////home/me/.kbutillib/council.sqlite")
    store.init_schema()
    registry = QueryRegistry(store)
    registry.seed_builtins()
    go = registry.get("go_mf")
    system, user = go.render("proposer", task, {})
"""

from __future__ import annotations

from .directories import (
    SOURCE_DIRECTORY,
    annotate_sources,
    expand_query_dirs,
    register_query_dirs,
    spec_files,
)
from .errors import (
    NonGenericSchema,
    QuerySpecError,
    TemplateBindingError,
    UnknownAdapter,
    UnknownRenderer,
)
from .registry import QueryRegistry
from .spec import (
    QueryType,
    RoleSpec,
    generic_postprocess,
    generic_schema_for,
    generic_schemas,
    load_spec_mapping,
)
from .template import (
    CANDIDATE_BINDINGS,
    CONTEXT_BINDINGS,
    ROLES,
    candidate_block,
    render_with_templates,
)

__all__ = [
    "SOURCE_DIRECTORY",
    "annotate_sources",
    "expand_query_dirs",
    "register_query_dirs",
    "spec_files",
    "QueryType",
    "QueryRegistry",
    "RoleSpec",
    "ROLES",
    "CANDIDATE_BINDINGS",
    "CONTEXT_BINDINGS",
    "candidate_block",
    "render_with_templates",
    "generic_schemas",
    "generic_schema_for",
    "generic_postprocess",
    "load_spec_mapping",
    "QuerySpecError",
    "UnknownRenderer",
    "UnknownAdapter",
    "TemplateBindingError",
    "NonGenericSchema",
]
