"""Suite-wide safety guard: the REAL ``maestro`` must be unreachable here.

This machine has a real, working ``maestro`` on PATH
(``/home/chenry/.local/bin/maestro`` on h100) which submits real, paid tasks
to the real fleet.  :mod:`kbutillib.domains.literature.maestro_batch` runs
whatever ``maestro_exe`` it is given, and its default is the bare name
``"maestro"`` -- resolved by ``subprocess`` on PATH, exactly as a shell would.

Every test in this package passes ``maestro_exe`` explicitly, pointing at a
fake script under ``tmp_path``.  That is the brace.  This is the belt: a
session-scoped fake named ``maestro`` is put FIRST on PATH for every test in
``tests/literature``, so a call that forgot the argument -- now, or in an edit
years from now -- resolves to a script that records the attempt and exits 97
rather than queueing work on the fleet.

``tests/literature/test_maestro_batch.py::TestPathGuard`` asserts both halves
of that: that ``shutil.which("maestro")`` is the fake, and that a submit with
no ``maestro_exe`` reaches the fake and fails.
"""

from __future__ import annotations

import os
import stat
import sys
from types import SimpleNamespace

import pytest

#: Exit code the guard fake uses.  Distinctive on purpose: if it ever shows
#: up in a failure message, the message is "something called maestro without
#: saying which maestro", which is the only thing it can mean.
GUARD_EXIT_CODE = 97

_GUARD_SCRIPT = '''#!@INTERPRETER@
"""Not maestro.  The literature suite's PATH guard -- see conftest.py."""
import sys

with open(@MARKER@, "a") as handle:
    handle.write(repr(sys.argv[1:]) + "\\n")
sys.stderr.write(
    "REFUSED: this is the tests/literature PATH guard, not the real "
    "maestro. A test called a maestro submitter without naming one "
    "explicitly, and the real binary on this machine submits real paid "
    "tasks. Pass maestro_exe=<a fake script> .\\n"
)
sys.exit(@EXIT_CODE@)
'''


@pytest.fixture(scope="session")
def maestro_path_guard(tmp_path_factory):
    """A directory holding a refusing fake ``maestro``, plus its marker file.

    Session-scoped so that the recorded invocations accumulate across the
    whole run: an empty marker file is itself the assertion that no test
    fell through to a PATH-resolved submitter.

    Returns:
        ``SimpleNamespace(directory, script, marker, exit_code)``.
    """
    directory = tmp_path_factory.mktemp("maestro-path-guard")
    marker = directory / "guard-invocations.log"
    marker.write_text("")
    script = directory / "maestro"
    script.write_text(
        _GUARD_SCRIPT.replace("@INTERPRETER@", sys.executable)
        .replace("@MARKER@", repr(str(marker)))
        .replace("@EXIT_CODE@", str(GUARD_EXIT_CODE))
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return SimpleNamespace(
        directory=directory,
        script=script,
        marker=marker,
        exit_code=GUARD_EXIT_CODE,
    )


@pytest.fixture(autouse=True)
def guard_maestro_on_path(maestro_path_guard, monkeypatch):
    """Put the refusing fake first on PATH for every test in this package."""
    monkeypatch.setenv(
        "PATH",
        str(maestro_path_guard.directory) + os.pathsep + os.environ.get("PATH", ""),
    )
    return maestro_path_guard
