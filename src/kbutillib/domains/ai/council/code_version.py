"""Which build of this package a council job was SUBMITTED by.

``jobs.code_version`` is three facts about the submitting process --
``package_version``, ``git_commit``, ``git_dirty`` -- and any of them may be
``None``.  It exists because the parts of the council that turn a job into a
verdict are CODE in this repo: the renderer, the normaliser's two repairs and
the decision rule.  A store row records what was decided; nothing in it
recorded which build decided it (KBUtilLib#92 item 2).

WHAT IT IS NOT.  It is not a way to reproduce or verify a prompt.  Prompt text
is never stored, and knowing the commit a job was submitted from does not
recover one -- the job may have been WORKED by a different process, from a
different checkout, on a different day, and nothing anywhere records that
second build.  This is submission provenance and only that.

THE GIT FIELDS ARE RECORDED ONLY IF THE ENCLOSING REPOSITORY OWNS THIS
PACKAGE.  A wheel installed into a venv that happens to sit inside somebody
else's checkout is the case that forces this: ``git -C <package dir>
rev-parse HEAD`` would cheerfully answer with THAT project's commit, and a
provenance field which is confidently wrong is worse than one that is null.
So :func:`code_version` first asks git whether it tracks the package's own
``__init__.py`` (``ls-files --error-unmatch``) and leaves both git fields
``None`` when it does not.

Everything here is best-effort by construction: every git call is bounded by
:data:`GIT_TIMEOUT_S` and every failure of any kind becomes ``None`` for that
field.  Submitting a job must never fail, or be delayed, because a git
subprocess was unhappy.
"""

from __future__ import annotations

import logging
import subprocess  # noqa: S404 - argv is built here, never from user text
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

__all__ = ["GIT_TIMEOUT_S", "PACKAGE_NAME", "code_version"]

logger = logging.getLogger(__name__)

#: The distribution name :mod:`importlib.metadata` is asked for.
PACKAGE_NAME = "kbutillib"

#: Seconds any one git call may take.  Short on purpose: this runs on the
#: submit path, and a hung git (a dead NFS mount, an index lock held by an
#: editor) must cost a null field rather than a stalled submission.
GIT_TIMEOUT_S = 2.0

#: The installed package directory -- ``.../kbutillib`` -- resolved from this
#: module's own location rather than by importing the top-level package,
#: which would make this module's import order matter.
_PACKAGE_DIR = Path(__file__).resolve().parents[3]


def code_version() -> Dict[str, Any]:
    """``{"package_version", "git_commit", "git_dirty"}`` for THIS process.

    Resolved once per process (see :func:`_resolved`) and returned as a fresh
    dict each time, so a caller that stores it -- or mutates what it stored
    -- cannot disturb the next caller's answer.  The build does not change
    while the process runs, and three subprocesses per submitted job would be
    paid for nothing.
    """
    return dict(_resolved())


@lru_cache(maxsize=1)
def _resolved() -> Dict[str, Any]:
    """The cached answer.  Separate from :func:`code_version` so the public
    function can hand out a copy of it."""
    commit, dirty = _git_fields(_PACKAGE_DIR)
    return {
        "package_version": _package_version(),
        "git_commit": commit,
        "git_dirty": dirty,
    }


def _package_version() -> Optional[str]:
    """The installed distribution version, or ``None``.

    ``None`` is the honest answer for a source tree that was never installed
    (``PYTHONPATH=src``), which is how much of this fleet runs it.
    """
    try:
        from importlib.metadata import version

        return str(version(PACKAGE_NAME))
    except Exception:  # noqa: BLE001 - any failure is "nothing to say"
        return None


def _git_fields(package_dir: Path) -> Tuple[Optional[str], Optional[bool]]:
    """``(git_commit, git_dirty)`` for the repository that OWNS *package_dir*.

    ``(None, None)`` unless ``git ls-files --error-unmatch`` confirms that
    the enclosing repository tracks ``package_dir/__init__.py``.  Both fields
    go together: a commit without the dirty flag would read as a clean tree,
    and a dirty flag without a commit names nothing.
    """
    if not _repository_tracks_package(package_dir):
        return None, None
    commit = _git(package_dir, "rev-parse", "HEAD")
    if commit is None:
        return None, None
    status = _git(package_dir, "status", "--porcelain")
    return commit, None if status is None else bool(status)


def _repository_tracks_package(package_dir: Path) -> bool:
    """Does the repository enclosing *package_dir* track this package?

    The question is asked of ``__init__.py`` because that one file exists in
    every shape this package is installed in, and a tracked ``__init__.py``
    is the narrowest evidence that the enclosing checkout is this project's
    source and not an unrelated repository the package was merely dropped
    inside.
    """
    return (
        _git(
            package_dir,
            "ls-files",
            "--error-unmatch",
            str(package_dir / "__init__.py"),
        )
        is not None
    )


def _git(cwd: Path, *args: str) -> Optional[str]:
    """Stripped stdout of ``git -C cwd <args>``, or ``None`` on any failure.

    "Any failure" is meant literally: a non-zero exit, a timeout, no git on
    PATH, a directory that does not exist, a repository git refuses to read
    as its owner.  Each is the same answer to the caller -- this field cannot
    be filled in -- and none of them may raise into a submit.
    """
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 - see the docstring
        logger.debug("code_version: git %s failed: %s", " ".join(args), exc)
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()
