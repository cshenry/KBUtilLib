"""Silent-``None`` guard: every class-shaped ``kbutillib.__all__`` entry must
resolve to a real object, never ``None``.

Why this exists
----------------
``src/kbutillib/__init__.py`` re-exports most legacy/optional classes through
a ``try: from .domains.<domain>.<module> import X / except ImportError: X =
None`` pattern, so an unavailable *optional* backend (e.g. no ``rdkit``)
degrades a single class to ``None`` instead of crashing package import.

The same pattern also hides a **missed re-export** after a refactor: if a
domain module is renamed/moved and ``__init__.py`` is not updated to match,
the ``ImportError`` is swallowed exactly the same way, and the class silently
becomes ``None``. The failure then only surfaces later, at some arbitrary
call site, as a confusing ``'NoneType' object is not callable`` (or similar)
— not as a clear import-time error pointing at the actual break.

This guard closes that hole for the class-shaped names: it walks
``kbutillib.__all__`` (the reorg-integration PRD's canonical name set),
selects the entries that denote classes, and asserts each one is not
``None``. A regression here fails the test suite immediately, with the
offending name(s) in the assertion message, instead of degrading silently.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import kbutillib


def _class_entries() -> list[str]:
    """The class-shaped names in ``kbutillib.__all__``.

    kbutillib's ``__all__`` uses two disjoint naming conventions: classes /
    exceptions / dataclasses / enums are PascalCase (``MSFBAUtils``,
    ``KBWSUtils``, ``ToolUnavailableError``, ``JobState``, ...); every plain
    function or module-level constant is snake_case (``remote_solve``,
    ``base_url``, ``_parse_id``, ``compartment_types``, ...). Selecting names
    whose first character is uppercase reliably separates the two groups
    without hand-maintaining a duplicate list that would drift from
    ``__init__.py``.
    """
    return [name for name in kbutillib.__all__ if name[:1].isupper()]


def test_all_exports_a_non_trivial_class_entry_set() -> None:
    """Sanity-check the selection heuristic isn't accidentally empty/trivial."""
    names = _class_entries()
    assert len(names) >= 30, (
        f"Expected a large set of class-shaped kbutillib.__all__ entries, "
        f"got {len(names)}: {names}"
    )
    assert "KBUtilLib" in names
    assert "MSFBAUtils" in names
    assert "KBWSUtils" in names


def test_function_and_constant_entries_are_excluded() -> None:
    """Known plain-function / constant __all__ entries must NOT be treated as classes."""
    class_names = set(_class_entries())
    known_non_class = {
        "remote_solve",
        "compartment_types",
        "normalize_compartment",
        "direction_conversion",
        "directionality_from_bounds",
        "biochem_directionality",
        "combine_directionality_signals",
        "_parse_id",
        "_check_and_convert_model",
        "base_url",
        "env_from_url",
        "narrative_url",
        "service_url",
    }
    leaked = known_non_class & class_names
    assert not leaked, f"Non-class entries incorrectly classified as classes: {leaked}"


def _name_to_module_path() -> dict[str, str]:
    """Map each class-shaped ``kbutillib.__all__`` name to its backing dotted module.

    Parsed directly (via ``ast``) from ``kbutillib/__init__.py``'s ``try: from
    .domains.<x> import <Name> / except ImportError: <Name> = None`` pattern —
    the same pattern this guard is protecting — so the mapping cannot drift
    from the real re-export table.
    """
    init_path = Path(kbutillib.__file__)
    tree = ast.parse(init_path.read_text(encoding="utf-8"))
    mapping: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            for stmt in node.body:
                if isinstance(stmt, ast.ImportFrom) and stmt.module:
                    full_module = f"kbutillib.{stmt.module}"
                    for alias in stmt.names:
                        mapping[alias.asname or alias.name] = full_module
    return mapping


def test_all_class_entries_import_non_none() -> None:
    """Every class-shaped ``kbutillib.__all__`` entry must resolve to a non-None object.

    A ``None`` here means the corresponding ``try/except ImportError -> X =
    None`` block in ``kbutillib/__init__.py`` swallowed a real import error
    (e.g. a renamed or deleted domain module) instead of surfacing it — UNLESS
    that ``None`` is fully explained by a missing OPTIONAL third-party
    dependency (e.g. no ``cobra``/``aiohttp`` installed in this environment),
    in which case the name is excluded from ``failures`` and recorded in
    ``skipped_optional`` instead.
    """
    name_to_module = _name_to_module_path()
    failures: list[str] = []
    skipped_optional: list[str] = []
    for name in _class_entries():
        if getattr(kbutillib, name) is not None:
            continue
        module_path = name_to_module.get(name)
        if module_path is None:
            failures.append(name)
            continue
        try:
            importlib.import_module(module_path)
        except ModuleNotFoundError as exc:
            if exc.name and not exc.name.startswith("kbutillib"):
                skipped_optional.append(name)
                continue
        except ImportError:
            pass
        failures.append(name)
    assert not failures, (
        "The following kbutillib.__all__ class entries resolved to None "
        f"(a re-export in kbutillib/__init__.py is silently broken): {failures}\n"
        f"(entries excluded because an optional third-party dependency is "
        f"not installed: {skipped_optional})"
    )
