"""Persistence for the council: jobs, items, call rows and verdicts.

There is ONE implementation, :class:`.sqlite.SqliteStore`, and it is a single
file on LOCAL disk -- by default ``~/.kbutillib/council/council.sqlite``.  Each
machine has its own store and nothing coordinates across hosts: two machines
running councils have two independent queues, and that is the whole design
(kbu-ai-council-v1 review round 2).  There is no database server, no port and
no shared store of any kind.

Open one with :func:`open_store`, which accepts a ``sqlite:`` URL or a bare
filesystem path.  The class is not imported at package import time: SQLAlchemy
comes from the optional ``council`` extra, and the council's types/config layer
must stay usable without it.

Example::

    from kbutillib.domains.ai.council.store import open_store

    store = open_store("sqlite:////home/me/.kbutillib/council/council.sqlite")
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

#: What a caller carrying a URL for the retired shared store is told.  Kept as
#: a named constant so the refusal reads identically from ``open_store`` and
#: from anything that wants to quote it.
REMOVED_SHARED_STORE_MESSAGE = (
    "the shared PostgreSQL store was removed in kbu-ai-council-v1 review "
    "round 2; use a local sqlite path"
)


def open_store(url: str, **kwargs: Any) -> "CouncilStore":
    """Return the store for *url*, which must name a local SQLite file.

    ``sqlite:`` URLs and bare filesystem paths both give a
    :class:`~.sqlite.SqliteStore`.  Extra keyword arguments are passed through
    -- ``check_version=False`` in particular, which is how ``kbu council db
    migrate`` opens a database whose schema version this code does not yet
    accept.

    Raises:
        StoreError: the URL names a scheme that is not ``sqlite`` -- in
            particular a ``postgresql+psycopg://`` URL left over from the
            shared-store design, which no longer exists.
        ImportError: SQLAlchemy is not installed; the message names the
            ``council`` extra.
    """
    text = (url or "").strip()
    scheme = text.split(":", 1)[0].split("+", 1)[0].lower()

    # A bare filesystem path is a sqlite path -- SqliteStore reads both -- and
    # so is ``:memory:``.  Neither contains "://", which is what distinguishes
    # them from a URL that names some other store.
    if scheme == "sqlite" or "://" not in text:
        if importlib.util.find_spec("sqlalchemy") is None:
            raise ImportError(
                f"the council store needs SQLAlchemy 2.x: {_EXTRA_HINT}"
            )
        from .sqlite import SqliteStore

        return SqliteStore(text, **kwargs)

    raise StoreError(f"{REMOVED_SHARED_STORE_MESSAGE} (got {url!r})")
