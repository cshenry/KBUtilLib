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


def test_all_class_entries_import_non_none() -> None:
    """Every class-shaped ``kbutillib.__all__`` entry must resolve to a non-None object.

    A ``None`` here means the corresponding ``try/except ImportError -> X =
    None`` block in ``kbutillib/__init__.py`` swallowed a real import error
    (e.g. a renamed or deleted domain module) instead of surfacing it.
    """
    failures = [name for name in _class_entries() if getattr(kbutillib, name) is None]
    assert not failures, (
        "The following kbutillib.__all__ class entries resolved to None "
        f"(a re-export in kbutillib/__init__.py is silently broken): {failures}"
    )
