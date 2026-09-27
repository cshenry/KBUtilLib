"""Import-boundary guard for the Models and Analyses app.

The app must hold NO filesystem knowledge of its own — every read of the runs
tree goes through :class:`KorosArcStore`. A reviewer must be able to confirm the
DATA-LAYER modules never import ``pathlib`` to walk the runs tree.

Modelled on ``tests/koros_arc_store/test_import_boundary.py``: an AST scan, not a
substring grep, so a comment or a string literal mentioning ``pathlib`` is not a
false positive.

Nuance: ``pathlib`` itself is not forbidden everywhere — ``service.py`` uses
``Path`` for :func:`resolve_app_state_dir` (the app's OWN state directory, not the
runs tree), and ``app.py`` uses it for the state dir too. The boundary being
guarded is that the runs-tree readers (``build_portfolio`` / ``build_arc_models``)
never construct a runs-tree path; they reach the runs tree ONLY through the store.
This test asserts the store-only discipline two ways:

  1. No data-layer module imports ``os`` or ``pathlib`` and then calls
     ``iterdir`` / ``glob`` / ``walk`` / ``listdir`` — the runs-tree traversal
     verbs. Finding one would mean the app is walking the tree itself.
  2. The data-layer modules import the store surface (the sanctioned door).
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parent.parent.parent / "src" / "kbutillib"
_APP_PKG = _SRC_ROOT / "models_and_analyses"

# The traversal verbs that would indicate the app is walking a directory tree
# itself instead of going through the store.
_TRAVERSAL_CALLS = frozenset({"iterdir", "glob", "rglob", "walk", "listdir", "scandir"})


def _module_files() -> list[Path]:
    return sorted(_APP_PKG.rglob("*.py"))


def _traversal_calls(path: Path) -> list[str]:
    """Return offending directory-traversal call sites in *path*; empty when clean."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in _TRAVERSAL_CALLS:
                hits.append(f"{path.name}:{node.lineno}: .{node.func.attr}(...)")
    return hits


def test_app_package_exists():
    files = {f.name for f in _module_files()}
    assert "service.py" in files
    assert "app.py" in files
    assert "prefixes.py" in files
    assert "metrics.py" in files


def test_data_layer_never_walks_the_runs_tree():
    """No app module traverses a directory tree — the store is the only door."""
    violations: list[str] = []
    for py_file in _module_files():
        violations.extend(_traversal_calls(py_file))
    assert not violations, (
        "The app must reach the runs tree ONLY through KorosArcStore; it must "
        "not walk directories itself.\n" + "\n".join(violations)
    )


def test_service_imports_the_store_surface():
    """The data layer reaches its data through the store module (the sanctioned door)."""
    source = (_APP_PKG / "service.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports_store = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if "koros_arc_store" in node.module:
                imports_store = True
    assert imports_store, "service.py must import the koros_arc_store surface"
