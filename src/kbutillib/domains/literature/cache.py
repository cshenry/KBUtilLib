"""Shared SQLite cache for the gene-literature research stack.

Every client in this package (Europe PMC today, full-text and extraction
passes later) stores its intermediate results here, so that re-running a
literature pipeline is cheap and so that a recorded run can be replayed with
no network at all.

The store is deliberately minimal: a set of key/value tables, one per logical
namespace (``http_cache``, ...), each created on first use.  Values are JSON
documents.

Concurrency
-----------
One :class:`LiteratureCache` may be shared by threads and by asyncio tasks
running on the same loop.  Every statement runs under an instance ``RLock``
on a connection opened with ``check_same_thread=False``, and the database is
opened in WAL mode with a busy timeout so that separate instances -- or
separate processes -- can interleave safely too.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

# Config key holding an explicit cache location (read by the clients, which
# are the objects that own a config; see EuropePMCClient.cache).
CACHE_PATH_CONFIG_KEY = "gene_literature.cache_path"

# Location used when config says nothing.  Kept relative and joined to
# ``Path.home()`` at call time rather than at import time, so that a test (or
# a service) which relocates HOME gets the relocated path.
DEFAULT_CACHE_RELPATH = Path(".kbutillib") / "gene_literature.sqlite"

# Table names are interpolated into DDL/DML (SQLite cannot parameterise an
# identifier), so they are restricted to plain identifiers.
_TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_BUSY_TIMEOUT_SECONDS = 30.0


def default_cache_path() -> Path:
    """Return ``~/.kbutillib/gene_literature.sqlite``, resolved now."""
    return Path.home() / DEFAULT_CACHE_RELPATH


def resolve_cache_path(configured: Optional[Union[str, Path]] = None) -> Path:
    """Resolve the cache location from a configured value.

    Args:
        configured: Value of ``gene_literature.cache_path``, or None/empty
            when it is not configured.

    Returns:
        The configured path (``~`` expanded), else :func:`default_cache_path`.
    """
    if configured:
        return Path(str(configured)).expanduser()
    return default_cache_path()


class LiteratureCache:
    """Key/value JSON cache backed by a single SQLite file.

    Args:
        path: Path to the SQLite file.  Defaults to
            :func:`default_cache_path`.  Parent directories are created.

    Example::

        cache = LiteratureCache()
        cache.put("http_cache", "https://...?query=dnaA", {"hitCount": 1})
        cache.get("http_cache", "https://...?query=dnaA")
    """

    def __init__(self, path: Optional[Union[str, Path]] = None) -> None:
        self.path = resolve_cache_path(path)
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._known_tables: set = set()

    # ── connection ─────────────────────────────────────────────────────

    def _connection(self) -> sqlite3.Connection:
        """Open the database on first use (caller holds ``self._lock``)."""
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(self.path),
                check_same_thread=False,
                timeout=_BUSY_TIMEOUT_SECONDS,
            )
            conn.row_factory = sqlite3.Row
            # WAL lets a reader proceed while another connection writes; the
            # busy timeout covers the remaining write/write contention.
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(f"PRAGMA busy_timeout={int(_BUSY_TIMEOUT_SECONDS * 1000)};")
            self._conn = conn
        return self._conn

    def _ensure_table(self, table: str) -> str:
        """Create ``table`` if needed and return its validated name."""
        if not _TABLE_NAME_RE.match(table or ""):
            raise ValueError(
                f"Invalid cache table name {table!r}: must be a plain identifier"
            )
        if table not in self._known_tables:
            conn = self._connection()
            conn.execute(
                f'CREATE TABLE IF NOT EXISTS "{table}" ('
                "  key TEXT PRIMARY KEY,"
                "  value TEXT NOT NULL,"
                "  updated_at TEXT NOT NULL"
                ")"
            )
            conn.commit()
            self._known_tables.add(table)
        return table

    # ── public API ────────────────────────────────────────────────────

    def get(self, table: str, key: str) -> Optional[Any]:
        """Return the cached JSON value, or None when absent.

        Args:
            table: Cache namespace (e.g. ``"http_cache"``).
            key: Entry key.

        Returns:
            The deserialised value, or None if the key has never been stored
            (or holds a value that no longer parses as JSON).
        """
        with self._lock:
            self._ensure_table(table)
            row = self._connection().execute(
                f'SELECT value FROM "{table}" WHERE key = ?', (key,)
            ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return None

    def put(self, table: str, key: str, value_json: Any) -> None:
        """Store a JSON-serialisable value, replacing any previous entry.

        Args:
            table: Cache namespace (e.g. ``"http_cache"``).
            key: Entry key.
            value_json: Any JSON-serialisable object.
        """
        payload = json.dumps(value_json, sort_keys=True)
        stamp = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._ensure_table(table)
            conn = self._connection()
            conn.execute(
                f'INSERT OR REPLACE INTO "{table}" (key, value, updated_at) '
                "VALUES (?, ?, ?)",
                (key, payload, stamp),
            )
            conn.commit()

    def close(self) -> None:
        """Close the underlying connection (safe to call repeatedly)."""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
                self._known_tables.clear()
