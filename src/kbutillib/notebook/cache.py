"""Cache — general-purpose blob cache backed by .kbcache/blobs/ + catalog.cache_objects."""

from __future__ import annotations

import functools
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .detect import get_cell_index, get_cell_source_hash
from .serialization import auto_dispatch, get_serializer
from .storage.blobs import BlobStore
from .storage.catalog import Catalog

_MISSING = object()


@dataclass
class CacheEntry:
    """Metadata about a cached object."""

    id: str
    type: str
    blob_path: str
    content_hash: str
    n_bytes: int
    metadata: Optional[dict] = None
    created_at: Optional[datetime] = None


class Cache:
    """General-purpose blob cache.

    Backed by ``.kbcache/blobs/`` + ``catalog.cache_objects``.
    """

    def __init__(
        self,
        catalog: Catalog,
        blob_store: BlobStore,
        notebook_name: Optional[str] = None,
    ) -> None:
        self._catalog = catalog
        self._blobs = blob_store
        self._notebook = notebook_name

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def save(
        self,
        name: str,
        obj: Any,
        *,
        type_hint: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> CacheEntry:
        """Save *obj* under *name*. Silent overwrite if name exists.

        Picks serializer via *type_hint* or auto-dispatch.
        Computes content_hash, writes blob if hash is new, updates catalog row.
        Logs write event in access_log.
        """
        ser = get_serializer(type_hint) if type_hint else auto_dispatch(obj)

        # Serialize to a temp path, compute hash
        tmp_path = self._blobs.blobs_dir / f"_tmp_{name}{ser.file_extension}"
        extra_meta = ser.serialize(obj, tmp_path)
        raw = tmp_path.read_bytes()
        content_hash = hashlib.sha256(raw).hexdigest()
        n_bytes = len(raw)

        # Check skip-write optimisation
        existing = self._get_row(name)
        if existing and existing["content_hash"] == content_hash:
            # Content unchanged — remove temp, just log
            tmp_path.unlink(missing_ok=True)
        else:
            # Write (or overwrite) the content-addressed blob
            blob_path = self._blobs.blob_path(content_hash, ser.file_extension)
            tmp_path.rename(blob_path)

        blob_rel = f"blobs/{content_hash}{ser.file_extension}"
        now = datetime.now(timezone.utc).isoformat()

        combined_meta = {**(metadata or {}), **(extra_meta or {})}
        meta_json = json.dumps(combined_meta) if combined_meta else None

        if existing:
            self._catalog.conn.execute(
                "UPDATE cache_objects SET type=?, blob_path=?, content_hash=?, "
                "n_bytes=?, metadata_json=?, created_at=? WHERE id=?",
                (ser.type_name, blob_rel, content_hash, n_bytes, meta_json, now, name),
            )
        else:
            self._catalog.conn.execute(
                "INSERT INTO cache_objects (id, type, blob_path, content_hash, n_bytes, metadata_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, ser.type_name, blob_rel, content_hash, n_bytes, meta_json, now),
            )
        self._catalog.conn.commit()

        self._catalog.log_access(
            object_id=name,
            object_kind="cache",
            op="write",
            notebook=self._notebook,
            cell_index=get_cell_index(),
            cell_source_hash=get_cell_source_hash(),
            content_hash=content_hash,
        )

        return CacheEntry(
            id=name,
            type=ser.type_name,
            blob_path=blob_rel,
            content_hash=content_hash,
            n_bytes=n_bytes,
            metadata=combined_meta or None,
            created_at=datetime.fromisoformat(now),
        )

    def load(
        self,
        name: str,
        *,
        default: Any = _MISSING,
        expected_type: Optional[str] = None,
    ) -> Any:
        """Load an object by name. Raises KeyError if missing and no default."""
        row = self._get_row(name)
        if row is None:
            if default is not _MISSING:
                return default
            raise KeyError(f"Cache object {name!r} not found")

        type_name = expected_type or row["type"]
        ser = get_serializer(type_name)
        meta = json.loads(row["metadata_json"]) if row["metadata_json"] else {}

        blob_path = self._blobs.blob_path(row["content_hash"], ser.file_extension)
        obj = ser.deserialize(blob_path, meta)

        self._catalog.log_access(
            object_id=name,
            object_kind="cache",
            op="read",
            notebook=self._notebook,
            cell_index=get_cell_index(),
            cell_source_hash=get_cell_source_hash(),
            content_hash=row["content_hash"],
        )
        return obj

    def exists(self, name: str) -> bool:
        return self._get_row(name) is not None

    def info(self, name: str) -> CacheEntry:
        row = self._get_row(name)
        if row is None:
            raise KeyError(f"Cache object {name!r} not found")
        return self._row_to_entry(row)

    def list(self, *, type_filter: Optional[str] = None) -> list[CacheEntry]:
        if type_filter:
            rows = self._catalog.conn.execute(
                "SELECT * FROM cache_objects WHERE type=? ORDER BY created_at",
                (type_filter,),
            ).fetchall()
        else:
            rows = self._catalog.conn.execute(
                "SELECT * FROM cache_objects ORDER BY created_at"
            ).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def delete(self, name: str) -> None:
        row = self._get_row(name)
        if row is None:
            raise KeyError(f"Cache object {name!r} not found")
        self._catalog.conn.execute("DELETE FROM cache_objects WHERE id=?", (name,))
        self._catalog.conn.commit()

        self._catalog.log_access(
            object_id=name,
            object_kind="cache",
            op="delete",
            notebook=self._notebook,
            cell_index=get_cell_index(),
            cell_source_hash=get_cell_source_hash(),
            content_hash=row["content_hash"],
        )

    def cached(
        self,
        name: str,
        *,
        inputs: Optional[list[str]] = None,
        type_hint: Optional[str] = None,
    ):
        """Decorator: returns cached value if present, otherwise computes + saves.

        ``inputs`` is purely declarative for now (Phase 3 manifest will use it).
        """

        def decorator(fn):
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                if self.exists(name):
                    return self.load(name)
                result = fn(*args, **kwargs)
                self.save(name, result, type_hint=type_hint)
                return result

            return wrapper

        return decorator

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_row(self, name: str):
        return self._catalog.conn.execute(
            "SELECT * FROM cache_objects WHERE id=?", (name,)
        ).fetchone()

    @staticmethod
    def _row_to_entry(row) -> CacheEntry:
        meta = json.loads(row["metadata_json"]) if row["metadata_json"] else None
        return CacheEntry(
            id=row["id"],
            type=row["type"],
            blob_path=row["blob_path"],
            content_hash=row["content_hash"],
            n_bytes=row["n_bytes"],
            metadata=meta,
            created_at=datetime.fromisoformat(row["created_at"]),
        )
