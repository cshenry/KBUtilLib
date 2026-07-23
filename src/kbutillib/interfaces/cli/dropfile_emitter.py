"""kbu dropfile_emitter — atomic JSON drop-file writer for session mirroring.

Writes one JSON object per session into ``<aia_root>/state/session-inbox/``
using a temp-file + rename pattern so the ingester never sees a partial file.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


# Fields required in every drop-file (ingester rejects if any are absent).
_REQUIRED_FIELDS = {"session_id", "project_id", "command", "summary", "started_at"}

# List-typed optional fields that default to [] when absent.
_LIST_FIELDS = ("topics_discussed", "decisions_made", "work_submitted", "next_steps")


def emit_session_dropfile(aia_root: Path, payload: dict[str, Any]) -> Path:
    """Write *payload* as an atomic JSON drop-file in the AIAssistant session-inbox.

    The inbox directory is created on demand.  The write is atomic: data is
    written to a temporary file in the same directory and then renamed over the
    target path, so the ingester never sees a partial write.

    Args:
        aia_root: Path to the AIAssistant repo root (parent of ``state/``).
        payload: Session payload dict.  Must contain at minimum the fields
            ``session_id``, ``project_id``, ``command``, ``summary``,
            ``started_at``.  Optional list fields (``topics_discussed``,
            ``decisions_made``, ``work_submitted``, ``next_steps``) are
            normalised to ``[]`` when absent.  ``project_name`` defaults to
            ``project_id`` when absent.  ``ended_at`` is included as-is.

    Returns:
        The ``Path`` of the written drop-file.

    Raises:
        ValueError: if any required field is missing from *payload*.
        OSError: if the atomic rename fails for an OS-level reason.
    """
    missing = _REQUIRED_FIELDS - set(payload.keys())
    if missing:
        raise ValueError(
            f"drop-file payload is missing required fields: {sorted(missing)}"
        )

    session_id: str = payload["session_id"]

    # Build the canonical drop-file object.
    drop: dict[str, Any] = {
        "session_id": session_id,
        "project_id": payload["project_id"],
        "project_name": payload.get("project_name") or payload["project_id"],
        "command": payload["command"],
        "summary": payload["summary"],
        "started_at": payload["started_at"],
    }
    # Optional ended_at
    if "ended_at" in payload:
        drop["ended_at"] = payload["ended_at"]

    # Normalise list fields
    for field in _LIST_FIELDS:
        raw = payload.get(field, [])
        if isinstance(raw, list):
            drop[field] = [str(item) for item in raw if str(item).strip()]
        elif isinstance(raw, str):
            drop[field] = [line.strip() for line in raw.splitlines() if line.strip()]
        else:
            drop[field] = []

    inbox_dir = aia_root / "state" / "session-inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)

    target = inbox_dir / f"{session_id}.json"

    # Atomic write: temp file in same directory, then os.replace (rename).
    fd, tmp_path = tempfile.mkstemp(dir=inbox_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(drop, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_path, target)
    except Exception:
        # Clean up the temp file on failure.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return target
