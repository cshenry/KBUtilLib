"""Read-only access to the KOROS runs tree.

Provides :func:`resolve_runs_root` (the runs-root resolution chain) and
:class:`KorosArcStore` (enumeration of projects and arcs).

Nothing here is specific to any KIND app's domain, and nothing imports the
KING/KOROS backend or any of the five KING/KOROS repos — the module binds to
the documented on-disk contract, not to any upstream implementation.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List, Optional, Union

from .exceptions import RecordNotFound, RunsRootResolutionError
from .records import (
    FILE_ABSENT,
    JSON_PARSE_ERROR,
    ArcRecord,
    ProjectRecord,
    parse_provenance,
)

PROVENANCE_FILENAME = "PROVENANCE.json"


def resolve_runs_root(runs_root: Union[str, Path, None] = None) -> Path:
    """Resolve the KOROS runs root using the pinned resolution chain.

    Order (first that resolves to an existing directory wins):

      1. an explicit ``runs_root`` argument;
      2. ``$KOROS_HOME/runs`` — ``$KOROS_HOME`` is the *workspace root*, and
         runs live in its ``runs/`` subdirectory;
      3. ``$KING_KOROS_RUNS``;
      4. otherwise raise :class:`RunsRootResolutionError`.

    There is deliberately no hard-coded fallback. A machine with no KOROS
    workspace is a normal machine, and the honest response is to raise so an
    app can render "misconfigured" distinctly from "empty". Only the two
    environment variables named above are consulted.
    """
    if runs_root is not None:
        candidate = Path(runs_root)
        if candidate.is_dir():
            return candidate
        raise RunsRootResolutionError(
            f"explicit runs root does not exist or is not a directory: {candidate}"
        )

    koros_home = os.environ.get("KOROS_HOME")
    if koros_home:
        candidate = Path(koros_home) / "runs"
        if candidate.is_dir():
            return candidate

    king_koros_runs = os.environ.get("KING_KOROS_RUNS")
    if king_koros_runs:
        candidate = Path(king_koros_runs)
        if candidate.is_dir():
            return candidate

    raise RunsRootResolutionError(
        "could not resolve a KOROS runs root from --runs-root, "
        "$KOROS_HOME/runs, or $KING_KOROS_RUNS"
    )


class KorosArcStore:
    """Read-only enumeration of the KOROS runs tree.

    Projects are immediate subdirectories of the runs root. Arcs are the
    subdirectories of ``<project>/arcs/``. Enumeration is defensive: a project
    with no ``arcs/`` directory reports zero arcs, and an arc with an absent or
    unparseable ``PROVENANCE.json`` yields a record marked invalid — never a
    raise. One bad arc must not break the listing.
    """

    def __init__(self, runs_root: Union[str, Path, None] = None) -> None:
        self.runs_root: Path = resolve_runs_root(runs_root)

    # ── projects ───────────────────────────────────────────────────────────

    def list_projects(self) -> List[ProjectRecord]:
        """Return every project under the runs root, ordered by name ascending."""
        projects = [
            self._project_record(entry)
            for entry in self.runs_root.iterdir()
            if entry.is_dir()
        ]
        projects.sort(key=lambda p: p.name)
        return projects

    def get_project(self, name: str) -> ProjectRecord:
        """Return the project named *name* or raise :class:`RecordNotFound`."""
        path = self.runs_root / name
        if not path.is_dir():
            raise RecordNotFound(f"no project named {name!r} under {self.runs_root}")
        return self._project_record(path)

    def _project_record(self, path: Path) -> ProjectRecord:
        return ProjectRecord(
            name=path.name,
            path=path,
            arc_count=len(self._arc_dirs(path)),
        )

    # ── arcs ─────────────────────────────────────────────────────────────────

    def list_arcs(self, project: str) -> List[ArcRecord]:
        """Return every arc for *project*, ordered by slug ascending.

        A project with no ``arcs/`` directory (or an empty one) yields an empty
        list rather than raising.
        """
        project_path = self.runs_root / project
        if not project_path.is_dir():
            raise RecordNotFound(
                f"no project named {project!r} under {self.runs_root}"
            )
        arcs = [
            self._read_arc_dir(project, arc_dir)
            for arc_dir in self._arc_dirs(project_path)
        ]
        arcs.sort(key=lambda a: a.slug)
        return arcs

    def read_arc(self, project: str, slug: str) -> ArcRecord:
        """Return the arc ``<project>/arcs/<slug>``.

        Slug lookup is an exact, case-sensitive match. If the arc directory
        does not exist, raises :class:`RecordNotFound`. If it exists but its
        ``PROVENANCE.json`` is absent or unparseable, returns an ``ArcRecord``
        with ``valid=False`` and an ``invalid_reason`` — never ``None``, never
        a raise.
        """
        arc_dir = self.runs_root / project / "arcs" / slug
        if not arc_dir.is_dir():
            raise RecordNotFound(
                f"no arc {slug!r} in project {project!r} under {self.runs_root}"
            )
        return self._read_arc_dir(project, arc_dir)

    # ── internals ────────────────────────────────────────────────────────────

    @staticmethod
    def _arc_dirs(project_path: Path) -> List[Path]:
        """Return the arc directories under ``<project>/arcs/``.

        A project with no ``arcs/`` subdirectory returns an empty list; it must
        never raise. This is the common shape for one of the live projects.
        """
        arcs_dir = project_path / "arcs"
        if not arcs_dir.is_dir():
            return []
        return [entry for entry in arcs_dir.iterdir() if entry.is_dir()]

    def _read_arc_dir(self, project: str, arc_dir: Path) -> ArcRecord:
        """Build an ``ArcRecord`` for a known-existing arc directory."""
        slug = arc_dir.name
        prov_path = arc_dir / PROVENANCE_FILENAME

        if not prov_path.is_file():
            return ArcRecord(
                project=project,
                slug=slug,
                path=arc_dir,
                provenance=None,
                valid=False,
                invalid_reason=FILE_ABSENT,
            )

        try:
            data = json.loads(prov_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return ArcRecord(
                project=project,
                slug=slug,
                path=arc_dir,
                provenance=None,
                valid=False,
                invalid_reason=JSON_PARSE_ERROR,
            )

        provenance, reason = parse_provenance(data)
        if provenance is None:
            return ArcRecord(
                project=project,
                slug=slug,
                path=arc_dir,
                provenance=None,
                valid=False,
                invalid_reason=reason,
            )

        return ArcRecord(
            project=project,
            slug=slug,
            path=arc_dir,
            provenance=provenance,
            valid=True,
            invalid_reason=None,
        )
