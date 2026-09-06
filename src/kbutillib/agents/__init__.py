"""kbutillib.agents — single source of truth for agent/skill/bundle/KING code.

Public API is re-exported lazily from sub-modules:
- ``kbutillib.agents.king_install`` — KING bundle installer (load_bundle, install, etc.)
- ``kbutillib.agents.king_app``     — package-data directory (bundle.json, skill.md)

The old import path ``kbutillib.king_install`` is a shim that re-exports from
here, so existing code is unaffected.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kbutillib.agents.king_install import (
        BundleError,
        compose_context,
        detect_llm_route,
        detect_versions,
        generate_serve_script,
        install,
        load_bundle,
        read_registry,
        resolve_apps_dir,
        resolve_king_stack_dir,
        run_verify_probe,
        status,
        uninstall,
        write_registry,
    )

# ---------------------------------------------------------------------------
# Lazy __getattr__ — defer import until attribute is accessed
# ---------------------------------------------------------------------------

_KING_INSTALL_SYMBOLS = frozenset(
    [
        "BundleError",
        "compose_context",
        "detect_llm_route",
        "detect_versions",
        "generate_serve_script",
        "install",
        "load_bundle",
        "read_registry",
        "resolve_apps_dir",
        "resolve_king_stack_dir",
        "run_verify_probe",
        "status",
        "uninstall",
        "write_registry",
    ]
)

def __getattr__(name: str):  # type: ignore[return]
    if name in _KING_INSTALL_SYMBOLS:
        import kbutillib.agents.king_install as _ki

        return getattr(_ki, name)
    raise AttributeError(f"module 'kbutillib.agents' has no attribute {name!r}")


__all__ = sorted(_KING_INSTALL_SYMBOLS)
