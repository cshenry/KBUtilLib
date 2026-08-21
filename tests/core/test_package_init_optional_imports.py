"""Coverage of the ImportError fallback branch for every optional import in
``kbutillib/__init__.py``.

``src/kbutillib/__init__.py`` guards ~62 domain-module imports with ``try:
from .domains.<x> import <Name> / except ImportError [as e]: [_import_error(...)]
<Name> = None``, so that a missing *optional* third-party dependency degrades
one public name to ``None`` instead of breaking ``import kbutillib``. Under
normal test conditions almost every one of those imports succeeds (the
environment has the optional deps installed), so the ``except`` branch itself
- 172 statements - is never executed and never covered.

This module forces every one of those imports to fail (regardless of what is
actually installed) by patching ``builtins.__import__`` to raise
``ImportError`` for exactly the guarded submodules, then reloads
``kbutillib`` under that sabotage. It asserts:

* TARGET Y - ``import kbutillib`` (via ``importlib.reload``) does not raise,
  and every guarded public name is ``None``.
* TARGET Z - the failures are recorded. ``_import_error`` either appends to
  the module-level ``_OPTIONAL_IMPORT_FAILURES`` list (default) or, with
  ``KBUTILLIB_VERBOSE_IMPORTS=1``, prints one detail line per recorded
  failure referencing the underlying ``ImportError`` immediately. This test
  uses the verbose path (captured via ``capsys``) because the non-verbose
  list is cleared by ``_flush_import_errors()`` before ``importlib.reload``
  returns control to the caller - there is no way to observe it from outside
  once the reload call completes, but the verbose print statements are
  observable synchronously during the reload.
* TARGET AA - after the sabotage fixture tears down, the real module is
  restored: ``sys.modules`` is put back exactly as it was and a final clean
  ``importlib.reload`` is forced so every guarded name returns to its
  pre-sabotage (baseline) value.

Name/module derivation
-----------------------
The set of guarded names and their backing dotted module paths is derived by
parsing ``kbutillib/__init__.py``'s own AST (mirroring the technique already
used by ``tests/guard/test_silent_none_reexports.py``) rather than hand-typed,
so this test keeps working if a block is added, removed, or renamed.
"""

from __future__ import annotations

import ast
import builtins
import importlib
import re
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

import kbutillib

_INIT_PATH = Path(kbutillib.__file__)
_TREE = ast.parse(_INIT_PATH.read_text(encoding="utf-8"), filename=str(_INIT_PATH))


def _collect_guarded_blocks() -> list[dict[str, Any]]:
    """Parse every ``try: from .domains.<x> import <Name> / except ImportError``
    block at module top level.

    Returns one dict per block: ``resolved_module`` (the absolute dotted
    module path the ``from`` import resolves to - always ``kbutillib.<module>``
    since every guarded import in this file uses a single-dot relative import,
    i.e. ``level == 1``), ``names`` (every ``<Name> = None`` assignment inside
    the handler), and ``records_as`` (the module-name string argument passed
    to ``_import_error(...)`` if the handler calls it, else ``None``).
    """
    blocks: list[dict[str, Any]] = []
    for node in _TREE.body:
        if not isinstance(node, ast.Try):
            continue
        import_from = next(
            (s for s in node.body if isinstance(s, ast.ImportFrom)), None
        )
        if import_from is None or import_from.level != 1:
            continue
        handler = None
        for h in node.handlers:
            htype = h.type
            type_names: list[str] = []
            if isinstance(htype, ast.Name):
                type_names = [htype.id]
            elif isinstance(htype, ast.Tuple):
                type_names = [n.id for n in htype.elts if isinstance(n, ast.Name)]
            if "ImportError" in type_names:
                handler = h
                break
        if handler is None:
            continue
        none_names: list[str] = []
        records_as = None
        for stmt in handler.body:
            if (
                isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
                and isinstance(stmt.value, ast.Constant)
                and stmt.value.value is None
            ):
                none_names.append(stmt.targets[0].id)
            if (
                isinstance(stmt, ast.Expr)
                and isinstance(stmt.value, ast.Call)
                and isinstance(stmt.value.func, ast.Name)
                and stmt.value.func.id == "_import_error"
                and stmt.value.args
                and isinstance(stmt.value.args[0], ast.Constant)
            ):
                records_as = stmt.value.args[0].value
        if none_names:
            blocks.append(
                {
                    "resolved_module": f"kbutillib.{import_from.module}",
                    "names": tuple(none_names),
                    "records_as": records_as,
                }
            )
    return blocks


_GUARDED_BLOCKS = _collect_guarded_blocks()
assert _GUARDED_BLOCKS, (
    "AST derivation found zero guarded try/except ImportError blocks in "
    f"{_INIT_PATH} - the source shape changed; update the derivation."
)
_ALL_NAMES = sorted({name for b in _GUARDED_BLOCKS for name in b["names"]})
_RECORDING_MODULES = sorted(
    {b["records_as"] for b in _GUARDED_BLOCKS if b["records_as"]}
)
_BLOCKED_MODULES = frozenset(b["resolved_module"] for b in _GUARDED_BLOCKS)

# Baseline, captured before any sabotage, used to prove restoration (TARGET AA).
_BASELINE = {name: getattr(kbutillib, name, "<MISSING>") for name in _ALL_NAMES}


def _fake_import(real_import: Callable[..., Any]) -> Callable[..., Any]:
    """Build an ``__import__`` replacement that raises ``ImportError`` for
    exactly the guarded submodules and delegates everything else untouched."""

    def fake(  # noqa: A002
        name: str,
        globals: dict[str, Any] | None = None,
        locals: dict[str, Any] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
        /,
        **kw: Any,
    ) -> Any:
        resolved = name
        if level == 1 and globals is not None:
            package = globals.get("__package__")
            if package:
                resolved = f"{package}.{name}" if name else package
        if resolved in _BLOCKED_MODULES:
            raise ImportError(f"sabotaged for test: {resolved}")
        return real_import(name, globals, locals, fromlist, level, **kw)

    return fake


@pytest.fixture(scope="module")
def sabotaged() -> Iterator[tuple[ModuleType, str]]:
    """Reload ``kbutillib`` with every guarded import forced to fail.

    Snapshots ``sys.modules`` entries for the package before mutating
    anything, patches ``builtins.__import__`` and ``KBUTILLIB_VERBOSE_IMPORTS``
    for the duration of a single ``importlib.reload``, captures the verbose
    detail lines emitted during that reload via ``contextlib.redirect_stderr``
    (module-scoped, so pytest's function-scoped ``capsys`` cannot be used
    here), then - unconditionally, via ``finally`` - restores
    ``builtins.__import__``, the environment variable, ``sys.modules``, and
    performs one final clean reload so later tests never see the sabotaged
    module state (TARGET AA).
    """
    import contextlib
    import io
    import os

    module_snapshot = {
        name: mod for name, mod in sys.modules.items() if name.startswith("kbutillib")
    }
    real_import = builtins.__import__
    had_verbose = "KBUTILLIB_VERBOSE_IMPORTS" in os.environ
    old_verbose = os.environ.get("KBUTILLIB_VERBOSE_IMPORTS")

    builtins.__import__ = _fake_import(real_import)
    os.environ["KBUTILLIB_VERBOSE_IMPORTS"] = "1"
    try:
        stderr_buffer = io.StringIO()
        with contextlib.redirect_stderr(stderr_buffer):
            reloaded = importlib.reload(kbutillib)
        captured_err = stderr_buffer.getvalue()
        yield reloaded, captured_err
    finally:
        builtins.__import__ = real_import
        if had_verbose:
            os.environ["KBUTILLIB_VERBOSE_IMPORTS"] = cast(str, old_verbose)
        else:
            os.environ.pop("KBUTILLIB_VERBOSE_IMPORTS", None)
        # Drop anything imported/partially-initialized under sabotage, restore
        # the exact pre-sabotage sys.modules entries, then force one clean
        # re-exec of kbutillib/__init__.py so the live module object's
        # attributes go back to their real (non-sabotaged) values.
        for name in list(sys.modules):
            if name.startswith("kbutillib") and name not in module_snapshot:
                del sys.modules[name]
        sys.modules.update(module_snapshot)
        importlib.reload(kbutillib)
        for name, expected in _BASELINE.items():
            assert getattr(kbutillib, name, "<MISSING>") == expected, (
                f"TARGET AA violated: kbutillib.{name} was not restored to its "
                f"pre-sabotage value after the sabotage fixture tore down "
                f"(expected {expected!r})"
            )


def test_ast_derivation_is_non_trivial() -> None:
    """Sanity-check the derivation isn't silently matching nothing."""
    assert len(_GUARDED_BLOCKS) > 30
    assert len(_ALL_NAMES) > 30
    assert len(_RECORDING_MODULES) > 10


def test_reload_under_forced_failure_does_not_raise(
    sabotaged: tuple[ModuleType, str],
) -> None:
    """TARGET Y (part 1): the fixture itself proves this - if reload had
    raised, fixture setup would have failed and every test would error."""
    reloaded, _ = sabotaged
    assert reloaded is kbutillib


@pytest.mark.parametrize("name", _ALL_NAMES)
def test_guarded_name_is_none_under_forced_failure(
    sabotaged: tuple[ModuleType, str], name: str
) -> None:
    """TARGET Y (part 2): every guarded public name degrades to None."""
    reloaded, _ = sabotaged
    assert getattr(reloaded, name) is None


def test_forced_failures_are_recorded_for_every_recording_module(
    sabotaged: tuple[ModuleType, str],
) -> None:
    """TARGET Z: one detail line per module that calls ``_import_error``,
    each referencing the underlying ImportError."""
    _, captured_err = sabotaged
    detail_lines = re.findall(
        r"^\[KBUtilLib\] Failed to import (\S+): (.+)$", captured_err, re.MULTILINE
    )
    recorded = {module: message for module, message in detail_lines}

    assert set(recorded) == set(_RECORDING_MODULES), (
        "Recorded failure set does not match the AST-derived set of modules "
        "whose except-handler calls _import_error()"
    )
    assert len(detail_lines) == len(_RECORDING_MODULES)

    # Representative named entry (src/kbutillib/__init__.py:69-73).
    assert "kb_ws_utils" in recorded
    assert "ImportError" in recorded["kb_ws_utils"]
    assert "sabotaged for test" in recorded["kb_ws_utils"]

    for module, message in recorded.items():
        assert "ImportError" in message, (
            f"detail line for {module!r} does not reference ImportError: {message!r}"
        )
