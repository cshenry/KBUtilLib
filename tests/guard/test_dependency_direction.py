"""Dependency-direction guard: kbutillib must not import genome_annotation_aggregator.

This test scans every Python source file under src/kbutillib/ and fails if
any import of ``genome_annotation_aggregator`` is found.  The dependency
between the two packages is strictly one-directional: GAA depends on
KBUtilLib, never the reverse.
"""

from __future__ import annotations

import ast
from pathlib import Path


_SRC_ROOT = Path(__file__).parent.parent.parent / "src" / "kbutillib"
_FORBIDDEN = "genome_annotation_aggregator"


def _imports_forbidden(path: Path) -> list[str]:
    """Return a list of offending import lines in *path*.

    Returns an empty list when the file is clean.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return []

    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _FORBIDDEN in alias.name:
                    hits.append(
                        f"{path}:{node.lineno}: import {alias.name}"
                    )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if _FORBIDDEN in module:
                hits.append(
                    f"{path}:{node.lineno}: from {module} import ..."
                )
    return hits


def test_no_gaa_imports_in_kbutillib() -> None:
    """No file under src/kbutillib/ may import genome_annotation_aggregator."""
    py_files = sorted(_SRC_ROOT.rglob("*.py"))
    assert py_files, f"No .py files found under {_SRC_ROOT}"

    violations: list[str] = []
    for py_file in py_files:
        violations.extend(_imports_forbidden(py_file))

    assert not violations, (
        "One-directional dependency violation: kbutillib must not import "
        f"genome_annotation_aggregator.\n"
        + "\n".join(violations)
    )
