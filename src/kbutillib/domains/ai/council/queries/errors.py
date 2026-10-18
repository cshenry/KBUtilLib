"""Failures raised while loading, validating or rendering a query type.

These sit beside the store's own taxonomy (:mod:`..store.errors`) rather than
in :mod:`..types`, for the same reason: they are properties of one layer.  A
backend never raises them, and the engine only sees them at registration time
-- a query type that validates is a query type that renders.

All of them derive from :class:`..types.CouncilException`, so one ``except``
clause still catches every council failure.
"""

from __future__ import annotations

from ..types import CouncilException

__all__ = [
    "QuerySpecError",
    "UnknownRenderer",
    "UnknownAdapter",
    "TemplateBindingError",
    "NonGenericSchema",
]


class QuerySpecError(CouncilException):
    """A query type specification is not usable as written."""


class UnknownRenderer(QuerySpecError):
    """The spec names a builtin renderer this installation does not ship.

    Raised both for an unknown NAME and for a known name at a version this
    installation does not have.  A query type is refused rather than rendered
    with whatever renderer happens to be present, because a renderer is the
    prompt: the wrong one silently asks a different question.
    """


class UnknownAdapter(QuerySpecError):
    """The spec names a builtin answer adapter this installation lacks.

    Same reasoning as :class:`UnknownRenderer` from the other end: the adapter
    is what turns a domain reply into the generic shape the engine counts
    votes on, so the wrong one silently miscounts.
    """


class TemplateBindingError(QuerySpecError):
    """A template uses a ``$binding`` that is not on the closed list.

    The binding list is closed on purpose.  A template renderer that quietly
    substituted the empty string for an unknown name would turn a typo
    (``$candidates_fully``) into a prompt with a missing candidate block, and
    the resulting answers would look like a bad model rather than a bad
    template.
    """


class NonGenericSchema(QuerySpecError):
    """A user-registered spec's answer schema is not the generic one.

    User query types do not get to choose their answer shapes: the engine
    counts votes on the generic shape, and a spec free to invent its own would
    need an adapter, which only the shipped builtins have.  The only
    permitted difference is the name of the id field.
    """
