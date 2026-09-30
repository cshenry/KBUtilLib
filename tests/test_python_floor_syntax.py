"""Guard: every source file must be syntactically valid on the DECLARED
minimum Python, not merely on the interpreter that happens to run the tests.

Why this file exists
--------------------
On 2026-09-27 commit 5fc10ba shipped ``segment.strip('`\\"')`` inside an
f-string *expression* in two clearinghouse modules. A backslash inside an
f-string expression is a hard ``SyntaxError`` before Python 3.12 (PEP 701
relaxed it), while ``pyproject.toml`` declares ``requires-python >= 3.9``.
The author's interpreter was 3.12, so the code looked fine; on every
supported interpreter below that, ``kbutillib.domains.kbase.berdl`` became
unimportable outright.

Two things made that expensive rather than obvious:

* ``domains/kbase/berdl/__init__.py`` imports the offending modules at
  package scope, so 12 test modules failed COLLECTION -- which aborts the
  whole pytest run. The commit's own 71 new tests never executed on 3.11.
* ``SyntaxError`` is not an ``ImportError`` but IS an ``Exception``. Any
  consumer that treats "cannot import the capability" as "the capability is
  unavailable" therefore reads broken code as *absent environment*. On a
  3.11 BERDL pod that silently reads as off-pod, and in-pod-only work stops
  running with no error anyone sees.

A plain ``compile()`` sweep cannot catch this on a modern interpreter -- the
file compiles fine on 3.12, which is exactly the blind spot. So this walks
the AST instead and inspects the source text of each f-string expression,
which is version-independent: it flags code that WOULD break below 3.12 even
while running on 3.12.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"

#: Below this version, a backslash anywhere in an f-string expression is a
#: SyntaxError. PEP 701 (Python 3.12) lifted the restriction.
FSTRING_BACKSLASH_ALLOWED_FROM = (3, 12)


def _source_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def test_every_source_file_parses() -> None:
    """A file that will not parse on THIS interpreter is always a defect.

    On an interpreter below 3.12 this is what catches the f-string-backslash
    case directly; on 3.12+ it is a cheap sanity check and
    :func:`test_no_backslash_in_fstring_expression` does the real work.
    """
    broken: list[str] = []
    for path in _source_files():
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            broken.append(f"{path.relative_to(SRC)}:{exc.lineno}: {exc.msg}")
    assert not broken, "source files do not parse on {}: {}".format(
        ".".join(str(n) for n in sys.version_info[:3]), broken
    )


def test_no_backslash_in_fstring_expression() -> None:
    """No f-string EXPRESSION may contain a backslash.

    Legal from Python 3.12, a SyntaxError on the 3.9-3.11 this package still
    declares support for. Hoist the offending value into a local and
    interpolate the local instead -- see the comment at the fix site in
    ``clearinghouse_capability._quote_fqn``.
    """
    offenders: list[str] = []
    for path in _source_files():
        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError:
            continue  # already reported by test_every_source_file_parses
        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr):
                continue
            for part in node.values:
                if not isinstance(part, ast.FormattedValue):
                    continue
                segment = ast.get_source_segment(text, part.value)
                if segment and "\\" in segment:
                    offenders.append(
                        f"{path.relative_to(SRC)}:{part.value.lineno}: "
                        f"backslash in f-string expression {segment!r}"
                    )
    assert not offenders, (
        "backslash inside an f-string expression is a SyntaxError below "
        f"Python {'.'.join(str(n) for n in FSTRING_BACKSLASH_ALLOWED_FROM)}, "
        "which this package still supports: " + str(offenders)
    )


def test_declared_floor_is_below_the_fstring_relaxation() -> None:
    """Pin the premise: if the floor ever rises to 3.12+, this guard is moot.

    Written as a test rather than a comment so raising ``requires-python``
    fails HERE, pointing at the guard to delete, instead of leaving a check
    nobody can explain.
    """
    pyproject = (SRC.parent / "pyproject.toml").read_text(encoding="utf-8")
    line = next(
        (l for l in pyproject.splitlines() if l.strip().startswith("requires-python")),
        None,
    )
    assert line is not None, "pyproject.toml declares no requires-python"
    assert "3.12" not in line and "3.13" not in line, (
        f"requires-python is now {line.strip()!r}; if the floor is 3.12+ then "
        "backslashes in f-string expressions are legal and this module should "
        "be deleted"
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
