"""``kbutillib.cli.worknb_util`` — render and smart-merge helpers for the
work-notebook ``util.py`` template (Module 4 of the work-notebooks PRD).

This module is intentionally *separate* from the BERIL ``init_notebook``
machinery.  Do not modify the BERIL ``util.py.tmpl`` or
``init_notebook.py``; this module adds parallel, work-notebook-only
functionality alongside them.

Public API
----------
WORKNB_UTIL_MARKER : str
    The sentinel line that separates the generated header from user-written
    helpers.  Identical to the BERIL marker so the user sees one consistent
    convention, but used only by the work-notebook render path.

render_worknb_util_template(repo_basename, topic)
    Render ``worknb_util.py.tmpl`` into a string ready to write as
    ``PRJ-<topic>/util.py``.

smart_merge_worknb_util(existing_content, new_header)
    Replace the generated header above the marker in *existing_content*
    with *new_header*, preserving everything below the marker.
    Returns the merged string, or ``None`` if the marker is absent from
    either input.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import jinja2


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Sentinel that separates the generated header from project-specific helpers.
#: Keeping this identical to the BERIL marker gives the user one convention.
WORKNB_UTIL_MARKER: str = "# === project-specific helpers below ==="


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _template_dir() -> Path:
    return Path(__file__).resolve().parent / "templates"


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def render_worknb_util_template(repo_basename: str, topic: str) -> str:
    """Render ``worknb_util.py.tmpl`` with the given repo basename and topic.

    Parameters
    ----------
    repo_basename:
        The repo's directory basename (e.g. ``"ModelingLOE"``).  Used as
        the ``project_name`` argument to ``NotebookSession.for_notebook``
        so the cache catalog is namespaced by repo.
    topic:
        The PRJ topic string (already normalized; used only in the
        rendered doc-comment for readability).

    Returns
    -------
    str
        Rendered Python source text, newline-terminated.
    """
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(_template_dir())),
        keep_trailing_newline=True,
    )
    tmpl = env.get_template("worknb_util.py.tmpl")
    return tmpl.render(repo_basename=repo_basename, topic=topic)


def smart_merge_worknb_util(
    existing_content: str,
    new_header: str,
) -> Optional[str]:
    """Replace the generated header above the marker, preserving helpers below.

    This is the idempotent re-render logic: on ``kbu notebook-init --update``
    a newly rendered header replaces the old one while any hand-written
    helpers below ``WORKNB_UTIL_MARKER`` are left untouched.

    Parameters
    ----------
    existing_content:
        The current on-disk content of ``util.py``.
    new_header:
        The freshly rendered template output (must contain
        ``WORKNB_UTIL_MARKER``).

    Returns
    -------
    str or None
        The merged content, or ``None`` when the marker is absent from
        either *existing_content* or *new_header* (caller should treat
        this as a signal to refuse the merge rather than clobber).
    """
    if WORKNB_UTIL_MARKER not in existing_content:
        return None
    if WORKNB_UTIL_MARKER not in new_header:
        return None

    # Everything from the marker onward in the existing file.
    idx_existing = existing_content.index(WORKNB_UTIL_MARKER)
    below_marker = existing_content[idx_existing + len(WORKNB_UTIL_MARKER):]

    # Everything up to and including the marker in the new header.
    idx_new = new_header.index(WORKNB_UTIL_MARKER)
    header_part = new_header[: idx_new + len(WORKNB_UTIL_MARKER)]

    return header_part + below_marker
