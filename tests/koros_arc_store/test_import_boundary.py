"""Import-boundary guard for the koros_arc_store module family.

``kbutillib.koros_arc_store`` (the store, its records, identity, exceptions and
run database), ``kbutillib.koros_arc_store_testing`` (the reference fake) and the
CAC conformance helpers are CONSUME-ONLY with respect to the five KING/KOROS
repos: they bind to the documented contract, never to any upstream
implementation. This test fails if any of those source files imports
``king_backend`` or any of the five KING/KOROS repos.

Modelled on KBDLJobRunningPrototype's tests/test_layering.py and the sibling
``tests/guard/test_dependency_direction.py`` in this repo: an AST scan, not a
substring grep, so a comment or a string literal mentioning a forbidden name is
not a false positive.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parent.parent.parent / "src" / "kbutillib"

# The module family under guard: the store package, the shipped test double, and
# the CAC helpers (which live inside the store package as conformance.py).
_GUARDED_PATHS = [
    _SRC_ROOT / "koros_arc_store",
    _SRC_ROOT / "koros_arc_store_testing.py",
]

# The KING/KOROS surface this module must never import. king_backend is the
# backend package; the other five are the CONSUME-ONLY repos named in the spec.
_FORBIDDEN_ROOTS = frozenset(
    {
        "king_backend",
        "king",
        "koros",
        "semcat",
        "lakehouse_explorer",
        "narrative_connector",
    }
)


def _top_level(module: str) -> str:
    """Return the first dotted segment of an imported module name."""
    return module.split(".", 1)[0]


def _forbidden_imports(path: Path) -> list[str]:
    """Return offending import lines in *path*; empty when clean."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:  # pragma: no cover - guard should never hit this
        return []

    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _top_level(alias.name) in _FORBIDDEN_ROOTS:
                    hits.append(f"{path}:{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            # Skip relative imports (node.level > 0): they never reach a
            # forbidden top-level package.
            if node.level == 0 and node.module:
                if _top_level(node.module) in _FORBIDDEN_ROOTS:
                    hits.append(f"{path}:{node.lineno}: from {node.module} import ...")
    return hits


def _guarded_files() -> list[Path]:
    files: list[Path] = []
    for target in _GUARDED_PATHS:
        if target.is_dir():
            files.extend(sorted(target.rglob("*.py")))
        elif target.is_file():
            files.append(target)
    return files


def test_guarded_files_exist() -> None:
    """The module family under guard must actually be present."""
    files = _guarded_files()
    names = {f.name for f in files}
    assert "store.py" in names
    assert "run_db.py" in names
    assert "conformance.py" in names
    assert "koros_arc_store_testing.py" in names


def test_no_king_koros_imports() -> None:
    """No guarded source file may import king_backend or the five KING/KOROS repos."""
    files = _guarded_files()
    assert files, f"No guarded .py files found under {_SRC_ROOT}"

    violations: list[str] = []
    for py_file in files:
        violations.extend(_forbidden_imports(py_file))

    assert not violations, (
        "koros_arc_store / koros_arc_store_testing / the CAC helpers are "
        "consume-only and must not import king_backend or the five KING/KOROS "
        "repos.\n" + "\n".join(violations)
    )
