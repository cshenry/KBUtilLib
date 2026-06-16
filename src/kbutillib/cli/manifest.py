"""kbu manifest helpers — shared TOML I/O for kbu-project.toml and kbu-subproject.toml.

All downstream CLI phases (subproject, session, notebook, new-project, update)
import from this module.  Never duplicate TOML I/O logic elsewhere.
"""

from __future__ import annotations

import hashlib
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ── timestamp ──────────────────────────────────────────────────────────────


def now_utc_iso() -> str:
    """Return the current UTC time as ISO-8601 with a ``Z`` suffix.

    Example: ``"2026-06-04T15:30:00Z"``
    """
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── file hashing ───────────────────────────────────────────────────────────


def sha256_file(path: Path) -> str:
    """Return the hex-encoded SHA-256 digest of *path* contents.

    Returns a plain hex string (64 chars), not prefixed.
    """
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ── root project manifest ──────────────────────────────────────────────────

_PROJECT_MANIFEST_NAME = "kbu-project.toml"


def _project_manifest_path(project_root: Path) -> Path:
    return project_root / _PROJECT_MANIFEST_NAME


def read_project_manifest(project_root: Path) -> dict[str, Any]:
    """Read ``kbu-project.toml`` from *project_root* and return it as a dict.

    Raises ``FileNotFoundError`` if the manifest does not exist.
    """
    p = _project_manifest_path(project_root)
    with open(p, "rb") as fh:
        return tomllib.load(fh)


def write_project_manifest(project_root: Path, data: dict[str, Any]) -> None:
    """Write *data* to ``kbu-project.toml`` in *project_root*.

    Creates the directory if it does not exist.

    Requires ``tomli-w`` (``pip install 'tomli-w>=1.0'``).  The dependency is
    imported lazily so that ``kbu doctor`` and other read-only CLI paths remain
    available even in a venv that lacks tomli-w.
    """
    try:
        import tomli_w  # noqa: PLC0415
    except ImportError as exc:
        raise ImportError(
            "tomli-w is required to write TOML manifests. "
            "Install it with: pip install 'tomli-w>=1.0'"
        ) from exc
    project_root.mkdir(parents=True, exist_ok=True)
    p = _project_manifest_path(project_root)
    with open(p, "wb") as fh:
        tomli_w.dump(data, fh)


# ── subproject manifest ────────────────────────────────────────────────────

_SUBPROJECT_MANIFEST_NAME = "kbu-subproject.toml"


def _subproject_dir(project_root: Path, subproject_name: str) -> Path:
    return project_root / "subprojects" / subproject_name


def _subproject_manifest_path(project_root: Path, subproject_name: str) -> Path:
    return _subproject_dir(project_root, subproject_name) / _SUBPROJECT_MANIFEST_NAME


def read_subproject_manifest(
    project_root: Path, subproject_name: str
) -> dict[str, Any]:
    """Read ``kbu-subproject.toml`` for *subproject_name* under *project_root*.

    Raises ``FileNotFoundError`` if the manifest does not exist.
    """
    p = _subproject_manifest_path(project_root, subproject_name)
    with open(p, "rb") as fh:
        return tomllib.load(fh)


def write_subproject_manifest(
    project_root: Path, subproject_name: str, data: dict[str, Any]
) -> None:
    """Write *data* to ``kbu-subproject.toml`` for *subproject_name*.

    Creates the subproject directory if it does not exist.

    Requires ``tomli-w`` (``pip install 'tomli-w>=1.0'``).  The dependency is
    imported lazily so that ``kbu doctor`` and other read-only CLI paths remain
    available even in a venv that lacks tomli-w.
    """
    try:
        import tomli_w  # noqa: PLC0415
    except ImportError as exc:
        raise ImportError(
            "tomli-w is required to write TOML manifests. "
            "Install it with: pip install 'tomli-w>=1.0'"
        ) from exc
    d = _subproject_dir(project_root, subproject_name)
    d.mkdir(parents=True, exist_ok=True)
    p = _subproject_manifest_path(project_root, subproject_name)
    with open(p, "wb") as fh:
        tomli_w.dump(data, fh)


# ── partial-write helpers ──────────────────────────────────────────────────


def append_session_ref(
    project_root: Path,
    subproject_name: str,
    ref: dict[str, Any],
) -> None:
    """Append a session reference entry to ``[[session_refs]]`` in the subproject manifest.

    *ref* should contain at minimum: ``id``, ``skill``, ``at``, ``summary``.
    All other fields in the manifest are preserved exactly.
    """
    data = read_subproject_manifest(project_root, subproject_name)
    if "session_refs" not in data:
        data["session_refs"] = []
    data["session_refs"].append(ref)
    # Update last_session_at on the top-level subproject section
    if "subproject" in data:
        data["subproject"]["last_session_at"] = ref.get("at", now_utc_iso())
    write_subproject_manifest(project_root, subproject_name, data)


def append_notebook_entry_or_update(
    project_root: Path,
    subproject_name: str,
    slug: str,
    last_run_at: str,
) -> None:
    """Update ``last_run_at`` for an existing notebook entry or append a new one.

    *slug* is the notebook's stable key — its filename without the ``.ipynb``
    extension (e.g. ``"01_data_exploration"``).  This is the same ``slug`` key
    that ``kbu-plan`` writes into ``[[notebooks]]`` and that ``buildplan.json``
    uses, so run metadata merges into the canonical entry instead of creating a
    duplicate keyed by a path string.  If an entry with this slug already
    exists, its ``last_run_at`` is updated and ``modified_since_run`` is set to
    ``false``.  Otherwise a new entry is appended.

    All other manifest fields are preserved.
    """
    data = read_subproject_manifest(project_root, subproject_name)
    notebooks: list[dict[str, Any]] = data.get("notebooks", [])
    found = False
    for entry in notebooks:
        if entry.get("slug") == slug:
            entry["last_run_at"] = last_run_at
            entry["modified_since_run"] = False
            found = True
            break
    if not found:
        notebooks.append({
            "slug": slug,
            "last_run_at": last_run_at,
            "modified_since_run": False,
        })
    data["notebooks"] = notebooks
    write_subproject_manifest(project_root, subproject_name, data)
