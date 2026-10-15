"""Failures the store raises.

These live here rather than in :mod:`..types` because they are properties of
the persistence layer, not of the council protocol: a backend or an engine
never raises them, and the types module must stay importable with no database
driver installed.

All of them derive from :class:`..types.CouncilException` so a caller can
catch every council failure with one clause.
:class:`StorePathRefused` additionally derives from :class:`ValueError`,
because refusing a bad database path is an argument error and callers were
told to expect one.
"""

from __future__ import annotations

from ..types import CouncilException

__all__ = [
    "StoreError",
    "StorePathRefused",
    "SchemaVersionMismatch",
    "QueryTypeExists",
    "StaleClaim",
    "NotOwner",
    "JobCancelled",
]


class StoreError(CouncilException):
    """Base class for every council store failure."""


class StorePathRefused(StoreError, ValueError):
    """The requested database location is one the store will not open.

    Currently the only refusal is a SQLite file inside a synced directory: a
    live SQLite database is a file plus a write-ahead log, and a file syncer
    will upload them mid-transaction and later "resolve" the pair into a
    corrupt database.  Mirrors the identical refusal in
    :func:`..config._resolve_store_url`, so a store is refused whether its URL
    came from the config file or straight from a caller.
    """


class SchemaVersionMismatch(StoreError):
    """The database was written by a different version of this schema."""


class QueryTypeExists(StoreError):
    """A different spec is already stored under this (name, version).

    Query types are immutable once saved, because a finished job's verdicts
    only mean anything against the exact spec that produced them.  Re-putting
    an IDENTICAL spec is not an error -- it is how an idempotent bootstrap
    works -- so this is raised only when the spec actually differs.
    """


class StaleClaim(StoreError):
    """The call was completed under a claim that is no longer the live one.

    Either the row was reclaimed and re-handed to another worker (its
    ``claim_token`` changed), or the completing worker holds a daemon lease
    generation that has since been superseded.  The reply is discarded rather
    than written over whatever the current claimant is doing.
    """


class NotOwner(StoreError):
    """The caller is not the user who submitted this job."""


class JobCancelled(StoreError):
    """The job is cancelled, which is terminal: nothing may be added to it.

    Calls already claimed when the cancellation landed are still allowed to
    complete -- their replies are stored and stay reusable -- but no new call
    or verdict may be attached.
    """
