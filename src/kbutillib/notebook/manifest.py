"""Manifest API — browseable view of all notebooks, objects, and freshness state."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from .schema.manifest import AccessRecord, NotebookEntry, ObjectEntry

if TYPE_CHECKING:
    from .session import NotebookSession


class Manifest:
    """Provides a single browseable view of all notebooks, cache objects,
    vectors, their access history, and freshness state.

    Constructed via ``NotebookSession.manifest``.
    """

    def __init__(self, session: "NotebookSession") -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def notebooks(self) -> list[NotebookEntry]:
        """List all notebooks that appear in the access log."""
        conn = self._session._get_catalog().conn
        rows = conn.execute(
            "SELECT notebook, "
            "MAX(timestamp) AS last_run, "
            "SUM(CASE WHEN op='write' THEN 1 ELSE 0 END) AS write_count, "
            "SUM(CASE WHEN op='read' THEN 1 ELSE 0 END) AS read_count "
            "FROM access_log "
            "WHERE notebook IS NOT NULL "
            "GROUP BY notebook "
            "ORDER BY last_run DESC"
        ).fetchall()
        return [
            NotebookEntry(
                name=r["notebook"],
                last_run=datetime.fromisoformat(r["last_run"]) if r["last_run"] else None,
                write_count=r["write_count"],
                read_count=r["read_count"],
            )
            for r in rows
        ]

    def objects(self) -> list[ObjectEntry]:
        """List all cache and vector objects with access stats and freshness."""
        conn = self._session._get_catalog().conn

        # Pre-fetch access stats in bulk (one query)
        access_stats = self._bulk_access_stats(conn)

        entries: list[ObjectEntry] = []

        # Cache objects
        cache_rows = conn.execute(
            "SELECT id, type, created_at, metadata_json FROM cache_objects ORDER BY created_at"
        ).fetchall()
        for row in cache_rows:
            obj_id = row["id"]
            stats = access_stats.get(("cache", obj_id), {})
            meta = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
            parents = meta.get("inputs", [])
            created_at = datetime.fromisoformat(row["created_at"])
            is_stale = self._check_stale(conn, obj_id, parents, created_at)
            entries.append(
                ObjectEntry(
                    id=obj_id,
                    kind="cache",
                    type=row["type"],
                    created_at=created_at,
                    last_write=stats.get("last_write"),
                    last_read=stats.get("last_read"),
                    write_count=stats.get("write_count", 0),
                    read_count=stats.get("read_count", 0),
                    parents=parents,
                    is_stale=is_stale,
                )
            )

        # Vector objects
        vec_rows = conn.execute(
            "SELECT id, type_domain, type_scale, created_at FROM vectors ORDER BY created_at"
        ).fetchall()
        # Also fetch vector parents in bulk
        vec_parents = self._bulk_vector_parents(conn)
        for row in vec_rows:
            obj_id = row["id"]
            stats = access_stats.get(("vector", obj_id), {})
            parents = vec_parents.get(obj_id, [])
            entries.append(
                ObjectEntry(
                    id=obj_id,
                    kind="vector",
                    type=f"{row['type_scale']}-{row['type_domain']}",
                    created_at=datetime.fromisoformat(row["created_at"]),
                    last_write=stats.get("last_write"),
                    last_read=stats.get("last_read"),
                    write_count=stats.get("write_count", 0),
                    read_count=stats.get("read_count", 0),
                    parents=parents,
                    is_stale=False,  # vectors don't have input-based staleness in v1
                )
            )

        return entries

    def info(self, name: str) -> ObjectEntry:
        """Return ObjectEntry for a single object by name/id. Raises KeyError if not found."""
        for obj in self.objects():
            if obj.id == name:
                return obj
        raise KeyError(f"Object {name!r} not found in manifest")

    def what_writes(self, name: str) -> list[AccessRecord]:
        """Return all write access records for the given object."""
        return self._access_records(name, op="write")

    def what_reads(self, name: str) -> list[AccessRecord]:
        """Return all read access records for the given object."""
        return self._access_records(name, op="read")

    def stale(self) -> list[ObjectEntry]:
        """Return all objects flagged as stale."""
        return [obj for obj in self.objects() if obj.is_stale]

    def dot(self) -> str:
        """Return a Graphviz DOT string representing the producer-consumer DAG."""
        lines = ["digraph manifest {", "  rankdir=LR;"]
        all_objects = self.objects()
        for obj in all_objects:
            shape = "box" if obj.kind == "cache" else "ellipse"
            color = "red" if obj.is_stale else "black"
            lines.append(f'  "{obj.id}" [shape={shape}, color={color}];')
            for parent in obj.parents:
                lines.append(f'  "{parent}" -> "{obj.id}";')
        lines.append("}")
        return "\n".join(lines)

    def render(self, output_path: Optional[Path] = None) -> Path:
        """Generate Manifest.ipynb with browseable cells. Returns the output path.

        Uses nbformat to construct the notebook programmatically.
        Does NOT execute cells.
        """
        import nbformat

        nb = nbformat.v4.new_notebook()
        nb.metadata["kernelspec"] = {
            "display_name": "Python 3",
            "language": "python",
            "name": "python3",
        }

        # Title cell
        nb.cells.append(nbformat.v4.new_markdown_cell(
            "# Project Manifest\n\n"
            "Auto-generated overview of notebooks, cache objects, vectors, "
            "and their access history.\n\n"
            "*Re-run this notebook or call `session.manifest.render()` to refresh.*"
        ))

        # Setup cell
        nb.cells.append(nbformat.v4.new_code_cell(
            "from kbutillib.notebook import NotebookSession\n\n"
            "session = NotebookSession.for_notebook()\n"
            "manifest = session.manifest"
        ))

        # Notebooks cell
        nb.cells.append(nbformat.v4.new_markdown_cell("## Notebooks"))
        nb.cells.append(nbformat.v4.new_code_cell("manifest.notebooks()"))

        # Objects cell
        nb.cells.append(nbformat.v4.new_markdown_cell("## Objects (Cache + Vectors)"))
        nb.cells.append(nbformat.v4.new_code_cell("manifest.objects()"))

        # Stale objects cell
        nb.cells.append(nbformat.v4.new_markdown_cell("## Stale Objects"))
        nb.cells.append(nbformat.v4.new_code_cell("manifest.stale()"))

        # DAG cell
        nb.cells.append(nbformat.v4.new_markdown_cell("## Dependency DAG"))
        nb.cells.append(nbformat.v4.new_code_cell(
            "print(manifest.dot())"
        ))

        # Determine output path
        if output_path is None:
            output_path = self._session.kbcache_dir.parent / "Manifest.ipynb"
        else:
            output_path = Path(output_path)

        with open(output_path, "w", encoding="utf-8") as f:
            nbformat.write(nb, f)

        return output_path

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _access_records(self, name: str, *, op: str) -> list[AccessRecord]:
        """Fetch access log entries for a given object and operation."""
        conn = self._session._get_catalog().conn
        rows = conn.execute(
            "SELECT notebook, cell_index, cell_source_hash, op, timestamp "
            "FROM access_log WHERE object_id=? AND op=? ORDER BY timestamp",
            (name, op),
        ).fetchall()
        return [
            AccessRecord(
                notebook=r["notebook"],
                cell_index=r["cell_index"],
                cell_source_hash=r["cell_source_hash"],
                op=r["op"],
                timestamp=datetime.fromisoformat(r["timestamp"]),
            )
            for r in rows
        ]

    @staticmethod
    def _bulk_access_stats(conn) -> dict:
        """Fetch per-object access stats in a single query."""
        rows = conn.execute(
            "SELECT object_id, object_kind, op, "
            "MAX(CASE WHEN op='write' THEN timestamp END) AS last_write, "
            "MAX(CASE WHEN op='read' THEN timestamp END) AS last_read, "
            "SUM(CASE WHEN op='write' THEN 1 ELSE 0 END) AS write_count, "
            "SUM(CASE WHEN op='read' THEN 1 ELSE 0 END) AS read_count "
            "FROM access_log GROUP BY object_id, object_kind"
        ).fetchall()
        stats: dict = {}
        for r in rows:
            key = (r["object_kind"], r["object_id"])
            stats[key] = {
                "last_write": datetime.fromisoformat(r["last_write"]) if r["last_write"] else None,
                "last_read": datetime.fromisoformat(r["last_read"]) if r["last_read"] else None,
                "write_count": r["write_count"],
                "read_count": r["read_count"],
            }
        return stats

    @staticmethod
    def _bulk_vector_parents(conn) -> dict[str, list[str]]:
        """Fetch all vector parent relationships in one query."""
        rows = conn.execute(
            "SELECT child_id, parent_id FROM vector_parents"
        ).fetchall()
        parents: dict[str, list[str]] = {}
        for r in rows:
            parents.setdefault(r["child_id"], []).append(r["parent_id"])
        return parents

    @staticmethod
    def _check_stale(conn, obj_id: str, parents: list[str], created_at: datetime) -> bool:
        """Check if any parent object has a more recent created_at than this object.

        Freshness v1: stale = any declared input has a newer created_at.
        """
        if not parents:
            return False

        placeholders = ",".join("?" * len(parents))
        row = conn.execute(
            f"SELECT MAX(created_at) AS max_created FROM cache_objects WHERE id IN ({placeholders})",
            parents,
        ).fetchone()

        if row and row["max_created"]:
            parent_max = datetime.fromisoformat(row["max_created"])
            if parent_max > created_at:
                return True
        return False
