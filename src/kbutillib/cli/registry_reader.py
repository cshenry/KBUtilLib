"""kbu registry_reader — read-only ranked candidate lookup for AIAssistant projects.

Reads ``state/project_registry.yaml`` (top-level ``projects`` dict) from the
AIAssistant repo and ranks entries by string similarity (stdlib ``difflib``)
against a local project title.

Takes NO ``assistant.*`` import — pure stdlib + PyYAML.
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any, Optional

import yaml


def rank_candidates(
    local_title: str,
    aia_root: Optional[Path] = None,
    query: Optional[str] = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Return ranked AIAssistant project candidates matching *local_title*.

    Args:
        local_title: The local kbu project title / name used as the primary
            similarity key.
        aia_root: Path to the AIAssistant repo root (parent of ``state/``).
            When ``None`` the function returns ``[]`` silently.
        query: Optional free-text search string.  When provided, similarity is
            computed against ``query`` instead of *local_title*.
        limit: Maximum number of candidates to return.

    Returns:
        A list of dicts with keys ``project_id``, ``name``, ``score`` (float
        0.0–1.0), sorted descending by score.  Returns an empty list if the
        registry is missing, unreadable, or ``aia_root`` is ``None``.
    """
    if aia_root is None:
        return []

    registry_path = aia_root / "state" / "project_registry.yaml"
    try:
        with open(registry_path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    except Exception:
        # Missing or unreadable registry — return empty without raising.
        return []

    projects: dict[str, Any] = raw.get("projects", {})
    if not isinstance(projects, dict):
        return []

    needle = (query or local_title).lower()

    results: list[dict[str, Any]] = []
    for pid, meta in projects.items():
        if not isinstance(meta, dict):
            continue
        name = meta.get("name", "") or pid
        # Compare against both the id slug and the display name.
        id_score = difflib.SequenceMatcher(None, needle, pid.lower()).ratio()
        name_score = difflib.SequenceMatcher(None, needle, name.lower()).ratio()
        score = max(id_score, name_score)
        results.append({
            "project_id": pid,
            "name": name,
            "score": score,
        })

    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:limit]
