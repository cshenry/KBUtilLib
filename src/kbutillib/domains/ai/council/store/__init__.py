"""Persistence for the council: jobs, items, call rows, verdicts and leases.

Two implementations share one schema (:mod:`.schema`):

* :class:`.sqlite.SqliteStore` -- a local file, the default for a single user
  on one machine.
* :class:`.postgres.PostgresStore` -- a shared database every KBUtilLib
  installation can point at, so that a question one user has already paid for
  is not asked again by the next.

Pick one with :func:`open_store`, which dispatches on the URL scheme.  Neither
class is imported at package import time: SQLAlchemy and psycopg come from the
optional ``council`` extra, and the council's types/config layer must stay
usable without them.

Example::

    from kbutillib.domains.ai.council.store import open_store

    store = open_store("sqlite:////home/me/.kbutillib/council.sqlite")
    store.init_schema()
"""

from __future__ import annotations

import importlib.util
from typing import TYPE_CHECKING, Any

from .errors import (
    JobCancelled,
    NotOwner,
    QueryTypeExists,
    SchemaVersionMismatch,
    StaleClaim,
    StoreError,
    StorePathRefused,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..protocols import CouncilStore

__all__ = [
    "open_store",
    "StoreError",
    "StorePathRefused",
    "SchemaVersionMismatch",
    "QueryTypeExists",
    "StaleClaim",
    "NotOwner",
    "JobCancelled",
]

#: Install hint used by every ImportError this module raises.
_EXTRA_HINT = "pip install 'kbutillib[council]'"


def open_store(url: str, **kwargs: Any) -> "CouncilStore":
    """Return the store implementation for *url*.

    ``sqlite:`` URLs give a :class:`~.sqlite.SqliteStore`; ``postgresql:`` (or
    ``postgres:``) URLs give a :class:`~.postgres.PostgresStore`.  Extra
    keyword arguments are passed through to the implementation --
    ``check_version=False`` in particular, which is how ``kbu council db
    migrate`` opens a database whose schema version this code does not yet
    accept.

    Raises:
        ValueError: the URL names no scheme this store layer implements.
        ImportError: the driver for that scheme is not installed; the message
            names the ``council`` extra.
    """
    text = (url or "").strip()
    scheme = text.split(":", 1)[0].split("+", 1)[0].lower()

    if scheme == "sqlite":
        if importlib.util.find_spec("sqlalchemy") is None:
            raise ImportError(
                f"the council store needs SQLAlchemy 2.x: {_EXTRA_HINT}"
            )
        from .sqlite import SqliteStore

        return SqliteStore(text, **kwargs)

    if scheme in ("postgresql", "postgres"):
        if importlib.util.find_spec("sqlalchemy") is None:
            raise ImportError(
                f"the council store needs SQLAlchemy 2.x: {_EXTRA_HINT}"
            )
        # Checked before the import so a missing DRIVER reports as a missing
        # driver.  Letting `from .postgres import ...` fail would blame the
        # module rather than psycopg, which is the thing the user must install.
        if importlib.util.find_spec("psycopg") is None:
            raise ImportError(
                f"a PostgreSQL council store needs psycopg 3: {_EXTRA_HINT}"
            )
        from .postgres import PostgresStore

        return PostgresStore(text, **kwargs)

    raise ValueError(
        f"unsupported council store URL {url!r}: expected a sqlite: or "
        "postgresql: URL"
    )
