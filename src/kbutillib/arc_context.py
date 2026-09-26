"""Arc resolution for KOROS analysis producers.

A producer that records an analysis needs to know which KOROS research arc it is
running in, so the record can be attributed to ``(project, arc)``. This module
answers that question and NOTHING else — it does not open the run database, does
not write into the arc directory, and never guesses.

The single public entry point is :func:`resolve_current_arc`. It consults, in
order, an explicit slug the caller passes, the ``KOROS_ARC`` environment variable
KIND sets on every session, and finally the current working directory when it
lies inside the KOROS runs tree. The first branch that resolves wins; when none
do, it returns ``None``.

Resolution reads the runs tree via the shared :mod:`kbutillib.koros_arc_store`
module. The runs-root layout it depends on is
``<runs_root>/<project>/arcs/<slug>/PROVENANCE.json``, with the documented
special case that a project directory may ITSELF be an arc (a ``PROVENANCE.json``
and no ``arcs/`` subdirectory).

Also exposed:

  * :func:`warn_not_indexed` — a one-line stderr warning for the fail-soft
    write path, so a producer can say "this result was not indexed, and why"
    without pulling in a logging framework.
  * :data:`APP_ID` and :func:`app_us` — the CAC identity of this app. ``APP_ID``
    is the ONE canonical id in hyphen form; ``app_us`` derives the underscore
    form mechanically (CAC invariant I4). The underscore spelling is never
    hand-written.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional, Tuple

from kbutillib.koros_arc_store import (
    RunsRootResolutionError,
    resolve_runs_root,
)

PROVENANCE_FILENAME = "PROVENANCE.json"

# ── CAC identity (invariant I4) ────────────────────────────────────────────────
# ONE canonical id, in hyphen form. The underscore form used for Python modules
# and SQL identifiers is DERIVED from this by app_us(), never spelled a second
# time — a hardcoded second spelling is exactly the drift I4 exists to prevent.
APP_ID = "models-and-analyses"


def app_us() -> str:
    """Return the underscore form of :data:`APP_ID`, derived mechanically.

    CAC invariant I4 (king/docs/CROSS_APP_COMMUNICATION.md section E) requires the
    underscore spelling to be COMPUTED from the canonical hyphen id, so the two
    forms can never disagree. Do not replace this with a literal.
    """
    return APP_ID.replace("-", "_")


def warn_not_indexed(reason: str) -> None:
    """Write one line to stderr saying the analysis result was not indexed.

    This is the visible half of the fail-soft write path: recording is a side
    effect the user never asked for, so when it cannot happen the honest response
    is a single warning line, not an exception and not silence. Emits exactly one
    line; configures no logging and raises nothing.
    """
    print(
        f"warning: analysis result was not indexed: {reason}",
        file=sys.stderr,
    )


def resolve_current_arc(
    explicit: Optional[str] = None,
) -> Optional[Tuple[str, str]]:
    """Resolve the ``(project, arc)`` the caller is running in, or ``None``.

    Resolution order, first match wins:

      1. *explicit* — an arc slug passed by the caller, in either bare ``slug``
         or ``project/slug`` form. Its project is found by locating the arc under
         the runs root. A bare slug matching arcs in MORE THAN ONE project is
         ambiguous: this refuses (returns ``None``) rather than guess, because
         arc slugs are unique per project, not globally.
      2. the ``KOROS_ARC`` environment variable — KIND sets this on every session
         it starts (``king_backend/session.py``). Its value is an arc SLUG (bare
         or ``project/slug``), resolved exactly as *explicit*.
      3. the current working directory, IF it lies under
         :func:`resolve_runs_root`. Walks upward from cwd to find the arc
         directory; a project directory that is itself an arc (has a
         ``PROVENANCE.json`` and no ``arcs/`` subdirectory) is handled.
      4. otherwise ``None``.

    If :func:`resolve_runs_root` raises — no KOROS runs tree on this machine —
    this returns ``None`` rather than propagating. A machine with no KOROS runs
    tree is a normal machine, not an error.
    """
    try:
        runs_root = resolve_runs_root()
    except RunsRootResolutionError:
        return None

    if explicit is not None:
        return _resolve_slug(runs_root, explicit)

    env_arc = os.environ.get("KOROS_ARC")
    if env_arc:
        resolved = _resolve_slug(runs_root, env_arc)
        if resolved is not None:
            return resolved

    return _resolve_from_cwd(runs_root)


# ── internals ─────────────────────────────────────────────────────────────────


def _resolve_slug(
    runs_root: Path, slug_spec: str
) -> Optional[Tuple[str, str]]:
    """Resolve an arc slug (bare or ``project/slug``) to ``(project, arc)``.

    A ``project/slug`` spec resolves that exact arc directly. A bare slug is
    searched across every project: a unique hit resolves; zero hits or more than
    one hit (ambiguous) returns ``None`` — never a guess.
    """
    slug_spec = slug_spec.strip()
    if not slug_spec:
        return None

    if "/" in slug_spec:
        project, _, slug = slug_spec.partition("/")
        if not project or not slug:
            return None
        arc_dir = runs_root / project / "arcs" / slug
        if _is_arc_dir(arc_dir):
            return (_arc_project(arc_dir, project), slug)
        return None

    matches = []
    for project_dir in _iter_project_dirs(runs_root):
        candidate = project_dir / "arcs" / slug_spec
        if _is_arc_dir(candidate):
            matches.append((candidate, project_dir.name, slug_spec))

    if len(matches) == 1:
        arc_dir, project_name, slug = matches[0]
        return (_arc_project(arc_dir, project_name), slug)
    # Zero matches, or ambiguous (>1 project): refuse to guess.
    return None


def _resolve_from_cwd(runs_root: Path) -> Optional[Tuple[str, str]]:
    """Resolve ``(project, arc)`` from the current working directory.

    Only fires when cwd is inside *runs_root*. Walks upward from cwd looking for
    an arc directory, handling both layouts:

      * a normal arc at ``<runs_root>/<project>/arcs/<slug>/``, identified by
        having ``<project>/arcs/`` as its parent chain; and
      * a project directory that is itself an arc — it has a ``PROVENANCE.json``
        and no ``arcs/`` subdirectory (KIND's own ``p0.py`` produces this).
    """
    try:
        cwd = Path.cwd().resolve()
    except OSError:
        return None

    runs_root = runs_root.resolve()

    # cwd must be inside the runs tree.
    try:
        cwd.relative_to(runs_root)
    except ValueError:
        return None

    # Walk upward from cwd; stop at runs_root.
    current = cwd
    while True:
        if _is_arc_dir(current):
            resolved = _classify_arc_dir(runs_root, current)
            if resolved is not None:
                return resolved
        if current == runs_root:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent

    return None


def _classify_arc_dir(
    runs_root: Path, arc_dir: Path
) -> Optional[Tuple[str, str]]:
    """Turn a confirmed arc directory into ``(project, arc)``.

    Two tree shapes yield an arc directory:

      * ``<runs_root>/<project>/arcs/<slug>`` — project is the grandparent's
        name, arc is the directory name; and
      * ``<runs_root>/<project>`` where the project directory is itself an arc —
        project and arc are both the directory name.
    """
    parent = arc_dir.parent
    if parent.name == "arcs":
        project_dir = parent.parent
        # Guard: the project must sit directly under the runs root.
        if project_dir.parent == runs_root:
            return (_arc_project(arc_dir, project_dir.name), arc_dir.name)
        return None

    # Project directory that is itself an arc: sits directly under runs_root.
    if parent == runs_root:
        return (_arc_project(arc_dir, arc_dir.name), arc_dir.name)

    return None


def _iter_project_dirs(runs_root: Path):
    """Yield the immediate project directories under *runs_root*.

    Defensive: an unreadable runs root yields nothing rather than raising.
    """
    try:
        entries = list(runs_root.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.is_dir():
            yield entry


def _is_arc_dir(path: Path) -> bool:
    """True if *path* is an arc directory — a directory holding ``PROVENANCE.json``."""
    return path.is_dir() and (path / PROVENANCE_FILENAME).is_file()


def _arc_project(arc_dir: Path, tree_project: str) -> str:
    """Return the project for an arc, preferring its ``PROVENANCE.json`` field.

    The spec resolves an arc's project by reading the ``project`` field of its
    ``PROVENANCE.json``. That field is authoritative when present; when it is
    absent, empty, or the file cannot be read, this falls back to *tree_project*
    — the project derived from where the arc sits in the runs tree, which is what
    per-project slug uniqueness is enforced against.
    """
    prov_path = arc_dir / PROVENANCE_FILENAME
    try:
        data = json.loads(prov_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return tree_project
    if isinstance(data, dict):
        project = data.get("project")
        if isinstance(project, str) and project:
            return project
    return tree_project
