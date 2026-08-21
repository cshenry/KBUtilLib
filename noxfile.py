"""Nox sessions."""

import os
import shlex
import sys
from pathlib import Path
from textwrap import dedent

import nox

nox.options.default_venv_backend = "uv"

package = "kbutillib"
python_versions = ["3.12", "3.13", "3.11", "3.10", "3.9"]
nox.needs_version = ">= 2021.6.6"
nox.options.sessions = (
    "pre-commit",
    "mypy",
    "tests",
    "typeguard",
    "xdoctest",
)


def activate_virtualenv_in_precommit_hooks(session: nox.Session) -> None:
    """Activate virtualenv in hooks installed by pre-commit.

    This function patches git hooks installed by pre-commit to activate the
    session's virtual environment. This allows pre-commit to locate hooks in
    that environment when invoked from git.

    Args:
        session: The Session object.
    """
    assert session.bin is not None  # nosec

    # Only patch hooks containing a reference to this session's bindir. Support
    # quoting rules for Python and bash, but strip the outermost quotes so we
    # can detect paths within the bindir, like <bindir>/python.
    bindirs = [
        bindir[1:-1] if bindir[0] in "'\"" else bindir
        for bindir in (repr(session.bin), shlex.quote(session.bin))
    ]

    virtualenv = session.env.get("VIRTUAL_ENV")
    if virtualenv is None:
        return

    headers = {
        # pre-commit < 2.16.0
        "python": f"""\
            import os
            os.environ["VIRTUAL_ENV"] = {virtualenv!r}
            os.environ["PATH"] = os.pathsep.join((
                {session.bin!r},
                os.environ.get("PATH", ""),
            ))
            """,
        # pre-commit >= 2.16.0
        "bash": f"""\
            VIRTUAL_ENV={shlex.quote(virtualenv)}
            PATH={shlex.quote(session.bin)}"{os.pathsep}$PATH"
            """,
        # pre-commit >= 2.17.0 on Windows forces sh shebang
        "/bin/sh": f"""\
            VIRTUAL_ENV={shlex.quote(virtualenv)}
            PATH={shlex.quote(session.bin)}"{os.pathsep}$PATH"
            """,
    }

    hookdir = Path(".git") / "hooks"
    if not hookdir.is_dir():
        return

    for hook in hookdir.iterdir():
        if hook.name.endswith(".sample") or not hook.is_file():
            continue

        if not hook.read_bytes().startswith(b"#!"):
            continue

        text = hook.read_text()

        if not any(
            (Path("A") == Path("a") and bindir.lower() in text.lower())
            or bindir in text
            for bindir in bindirs
        ):
            continue

        lines = text.splitlines()

        for executable, header in headers.items():
            if executable in lines[0].lower():
                lines.insert(1, dedent(header))
                hook.write_text("\n".join(lines))
                break


@nox.session(name="pre-commit", python=python_versions[0])
def precommit(session: nox.Session) -> None:
    """Lint using pre-commit."""
    args = session.posargs or [
        "run",
        "--all-files",
        "--hook-stage=manual",
        "--show-diff-on-failure",
    ]
    session.run("uv", "sync", "--group", "dev", "--group", "lint", external=True)
    session.run("pre-commit", *args, external=True)
    if args and args[0] == "install":
        activate_virtualenv_in_precommit_hooks(session)


@nox.session(python=python_versions)
def mypy(session: nox.Session) -> None:
    """Type-check using mypy."""
    args = session.posargs or ["src", "tests"]

    session.run(
        "uv",
        "sync",
        "--group",
        "dev",
        "--group",
        "mypy",
        external=True,
    )

    session.install("mypy")

    session.install("pytest")

    session.install("-e", ".")
    session.run("mypy", *args)
    if not session.posargs:
        session.run("mypy", f"--python-executable={sys.executable}", "noxfile.py")


@nox.session(python=python_versions)
def tests(session: nox.Session) -> None:
    """Run the test suite."""
    session.run(
        "uv",
        "sync",
        "--group",
        "dev",
        "--group",
        "lint",
        external=True,
    )

    session.install("pytest", "coverage")
    session.install("-e", ".")
    session.run("pytest", *session.posargs)


@nox.session(python=python_versions[0])
def coverage(session: nox.Session) -> None:
    """Produce the coverage report."""
    # `args` previously defaulted to ["report"], a leftover from the
    # cookiecutter template where the session ran `coverage combine` and
    # `coverage report` as separate coverage(1) commands. Here it was spliced
    # into the pytest invocation instead, so pytest received "report" as a test
    # path and died with `file or directory not found: report` before running
    # anything. Nobody saw it for a year because this job is `needs: tests` and
    # the tests job never got far enough to trigger it.
    args = session.posargs

    # Install the dev extra, not a bare `-e .` -- the suite needs pytest-cov and
    # pandas, and a bare install leaves them out.
    session.install("-e", ".[dev]")

    session.log("Running pytest with coverage...")

    # Ignore list mirrors .github/workflows/ci.yml. If you change one, change
    # both -- a divergence here means the coverage number describes a different
    # suite than the one the pytest jobs run.
    session.run(
        "pytest",
        "--ignore=tests/notebook/helpers",
        "--ignore=tests/modeling/test_comprehensive_gapfill_wrapper.py",
        "--ignore=tests/biochem/test_escher_utils.py",
        "--ignore=tests/kbase/test_kb_narrative_provenance.py",
        "--ignore=tests/kbase/test_kb_plm_utils.py",
        "--ignore=tests/kbase/test_kb_ws_utils.py",
        "--ignore=tests/modeling/test_ms_reconstruction_utils.py",
        "--ignore=tests/kbase/test_upload_blob_file_streaming.py",
        "--cov=src",
        "--cov-report=xml",
        "--cov-report=term-missing",
        # Coverage is reported here, gated elsewhere -- see [tool.coverage.report].
        "--cov-fail-under=0",
        *args,
    )


@nox.session(name="typeguard", python=python_versions[0])
def typeguard_tests(session: nox.Session) -> None:
    """Run tests with typeguard."""
    session.run(
        "uv",
        "sync",
        "--group",
        "dev",
        "--group",
        "typeguard",
        external=True,
    )

    session.install("typeguard", "pytest")
    session.install("-e", ".")
    session.run("pytest", "--typeguard-packages", package, *session.posargs)


@nox.session(python=python_versions)
def xdoctest(session: nox.Session) -> None:
    """Run examples with xdoctest."""
    if session.posargs:
        args = [package, *session.posargs]
    else:
        args = [f"--modname={package}", "--command=all"]
        if "FORCE_COLOR" in os.environ:
            args.append("--colored=1")
    session.run(
        "uv",
        "sync",
        "--group",
        "dev",
        "--group",
        "xdoctest",
        external=True,
    )
    session.install("xdoctest")
    session.install("-e", ".")
    session.run("python", "-m", "xdoctest", package, *args)

