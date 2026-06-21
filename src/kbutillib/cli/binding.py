"""kbu binding helpers — project-to-AIAssistant project_id binding.

Stores and retrieves the ``[aiassistant]`` table in ``kbu-project.toml``.
Uses ``read_project_manifest`` / ``write_project_manifest`` from manifest.py.
Project-grain only (one binding per kbu project root).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .manifest import read_project_manifest, write_project_manifest


# ── read ───────────────────────────────────────────────────────────────────────


def resolve_binding(project_root: Path) -> Optional[str]:
    """Return the bound AIAssistant ``project_id`` for *project_root*, or ``None``.

    Reads the ``[aiassistant]`` table in ``kbu-project.toml``.  Returns ``None``
    if the manifest does not exist, if the table is absent, or if ``project_id``
    is missing or empty.
    """
    try:
        data = read_project_manifest(project_root)
    except FileNotFoundError:
        return None
    aia = data.get("aiassistant", {})
    pid = aia.get("project_id", "")
    return pid if pid else None


# ── write ──────────────────────────────────────────────────────────────────────


def set_binding(project_root: Path, project_id: str, project_name: str) -> None:
    """Persist ``{project_id, project_name}`` in the ``[aiassistant]`` table.

    Reads the existing manifest (creating a minimal one if absent) and writes back
    with the ``[aiassistant]`` table updated.  All other manifest fields are
    preserved.

    Args:
        project_root: Path to the kbu project root containing ``kbu-project.toml``.
        project_id: The AIAssistant project id slug to bind (e.g. ``"modelingloe"``).
        project_name: Human-readable display name for the project.

    Raises:
        ImportError: if ``tomli-w`` is not installed (propagated from
            ``write_project_manifest``).
    """
    try:
        data = read_project_manifest(project_root)
    except FileNotFoundError:
        # No manifest yet — create a minimal skeleton so write_project_manifest
        # has something to merge into.  Callers should not normally hit this
        # because kbu init creates the manifest, but we handle it defensively.
        data = {}
    data["aiassistant"] = {
        "project_id": project_id,
        "project_name": project_name,
    }
    write_project_manifest(project_root, data)
