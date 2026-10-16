"""Query types kept as YAML files in a directory, loaded on every store open.

``ai_council.query_dirs`` is a list of directories a user keeps query-type
specs in.  Every ``*.yaml`` / ``*.yml`` DIRECTLY inside each one is registered
through the normal :class:`~.registry.QueryRegistry` path on every store open
and on ``kbu council db init``, so a spec shared between machines (or kept in
a repository) is present in the store without anybody remembering to run
``kbu council query register``.

Four rules, each of which exists because of what the alternative would do:

* **Non-recursive.**  Only files directly in the directory.  A recursive walk
  would pick up a ``tests/fixtures`` tree, an editor backup directory or a
  whole second project that happened to live underneath.
* **Nothing is ever written here.**  Not a lock, not an index, not a
  ``.registered`` marker.  These directories are expected to be in Dropbox,
  and a file written into one syncs to every machine on the fleet; a sqlite
  sidecar in one would be a corruption waiting to happen.  The store is the
  only thing that records what was loaded.
* **A missing directory is a WARNING.**  One config is shared across a fleet
  whose machines mount different things (kbhub excludes several Dropbox
  paths), so a directory being absent here is normal and must not stop the
  council from opening its store.
* **An invalid file is an ERROR, naming the file.**  A spec that does not
  validate is a mistake somebody has to fix, and a directory load that
  skipped it with a log line would leave a job submitted against a query type
  the user believed they had edited.

Immutability is the store's rule and is not relaxed here: an identical
re-registration is a no-op, and the same ``name@version`` with DIFFERENT
content raises :class:`~..store.errors.QueryTypeExists` -- named with the FILE
it came from, because the user is looking at a file and not at a database row.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..store.errors import QueryTypeExists
from .errors import QuerySpecError

logger = logging.getLogger(__name__)

__all__ = [
    "SOURCE_DIRECTORY",
    "SPEC_SUFFIXES",
    "annotate_sources",
    "expand_query_dirs",
    "register_query_dirs",
    "spec_files",
]

#: The ``query_types.source`` value a directory-loaded spec is saved under.
#: The column already exists at schema v2 (``String(32)``, defaulting to
#: ``"user"``), so recording the provenance of these needs no migration; the
#: FILE a spec came from is not stored, because an absolute path on one
#: machine is meaningless on another and ``put_query_type`` compares specs
#: byte for byte -- a path inside the row would make the same spec compare
#: unequal between two machines and turn a shared directory into a
#: ``QueryTypeExists`` on arrival.
SOURCE_DIRECTORY = "directory"

#: Suffixes treated as a query-type spec.
SPEC_SUFFIXES = (".yaml", ".yml")


def expand_query_dirs(dirs: Iterable[Any]) -> List[Path]:
    """Expand ``~`` and ``${VAR}`` in each configured directory path.

    ``${VAR}`` is normally already expanded by
    :func:`~..config.expand_vars` over the whole raw config; it is done again
    here so a caller that builds a ``CouncilConfig`` directly (a test, a
    notebook) gets the same behaviour.
    """
    out: List[Path] = []
    for entry in dirs or ():
        text = str(entry or "").strip()
        if not text:
            continue
        out.append(Path(os.path.expandvars(text)).expanduser())
    return out


def spec_files(directory: Path) -> List[Path]:
    """The spec files directly in *directory*, sorted by name.

    Sorted so two machines load the same directory in the same order, which
    makes the FIRST failure in a directory with two bad files the same one
    everywhere.  A path that is missing, or is not a directory, returns ``[]``
    -- the caller warns.
    """
    try:
        if not directory.is_dir():
            return []
        entries = sorted(directory.iterdir(), key=lambda p: p.name)
    except OSError as exc:  # pragma: no cover - permissions / races
        logger.warning("council: could not read query_dir %s: %s", directory, exc)
        return []
    return [
        path
        for path in entries
        if path.suffix.lower() in SPEC_SUFFIXES and path.is_file()
    ]


def register_query_dirs(
    registry: Any,
    dirs: Iterable[Any],
    created_by: Optional[str] = None,
) -> Dict[str, str]:
    """Register every spec file in every *dirs* entry through *registry*.

    Args:
        registry: a :class:`~.registry.QueryRegistry` over the open store.
        dirs: the configured ``query_dirs`` entries.
        created_by: recorded on a row this call inserts.

    Returns:
        ``{"name@version": "/path/to/file.yaml"}`` for every spec found,
        whether this call inserted it or found it already there.  That is what
        ``kbu council query list`` shows the file from: the mapping is rebuilt
        on every store open from the directories themselves, so it reports
        where a spec IS rather than where it once was.

    Raises:
        QuerySpecError: a file does not validate (named).
        QueryTypeExists: a file holds a DIFFERENT spec for a ``name@version``
            the store already has (named).
    """
    found: Dict[str, str] = {}
    for directory in expand_query_dirs(dirs):
        files = spec_files(directory)
        if not files:
            if not directory.is_dir():
                logger.warning(
                    "council: query_dir %s does not exist (or is not a directory); "
                    "skipping it. A config is shared across machines that mount "
                    "different things, so this is a warning and not an error.",
                    directory,
                )
            continue
        for path in files:
            name, version = _register_one(registry, path, created_by)
            found[f"{name}@{version}"] = str(path)
    return found


def _register_one(
    registry: Any, path: Path, created_by: Optional[str]
) -> Tuple[str, str]:
    """Register ONE spec file, re-raising every failure named with the file."""
    try:
        spec = registry.register(path, created_by=created_by, source=SOURCE_DIRECTORY)
    except QueryTypeExists as exc:
        raise QueryTypeExists(f"{path}: {exc}") from exc
    except QuerySpecError as exc:
        raise QuerySpecError(f"{path}: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - a bad YAML file, a bad mapping
        raise QuerySpecError(
            f"{path}: not a usable query-type spec: {type(exc).__name__}: {exc}"
        ) from exc
    return str(spec.name), str(spec.version)


def annotate_sources(
    rows: Sequence[Dict[str, Any]], sources: Dict[str, str]
) -> List[Dict[str, Any]]:
    """Add a ``source_file`` to each row that came from a query directory.

    Empty for everything else, so the listing is one shape whatever a row's
    provenance is.
    """
    out: List[Dict[str, Any]] = []
    for row in rows:
        entry = dict(row)
        entry["source_file"] = sources.get(
            f"{entry.get('name')}@{entry.get('version')}", ""
        )
        out.append(entry)
    return out
