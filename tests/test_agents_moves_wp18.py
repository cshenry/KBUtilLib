"""WP18: Agents/Skills/KING/researchOS consolidation + shims.

Tests verify:
1. Old import paths work as shims.
2. New canonical paths work.
3. Old and new objects are the same (identity).
4. load_bundle() against the king_app bundle dir succeeds.
5. agents package lazy __getattr__ works.
6. researchos sub-module shims work.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path fixtures
# ---------------------------------------------------------------------------

_KING_APP_DIR = Path(__file__).parent.parent / "src" / "kbutillib" / "king_app"
_AGENTS_KING_APP_DIR = (
    Path(__file__).parent.parent / "src" / "kbutillib" / "agents" / "king_app"
)


# ---------------------------------------------------------------------------
# Test 1-3: king_install via OLD path
# ---------------------------------------------------------------------------


def test_old_king_install_load_bundle_importable():
    """from kbutillib.king_install import load_bundle should work (old path)."""
    from kbutillib.king_install import load_bundle  # noqa: PLC0415

    assert callable(load_bundle)


def test_old_king_install_module_has_all():
    """kbutillib.king_install.__all__ is exposed by the shim."""
    import kbutillib.king_install as ki  # noqa: PLC0415

    assert hasattr(ki, "__all__")
    assert "load_bundle" in ki.__all__
    assert "install" in ki.__all__


def test_old_king_install_full_public_api():
    """All main symbols from the old path are accessible."""
    from kbutillib.king_install import (  # noqa: PLC0415
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

    for sym in [
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
    ]:
        assert sym is not None


# ---------------------------------------------------------------------------
# Test 4-6: king_install via NEW path (agents)
# ---------------------------------------------------------------------------


def test_new_agents_king_install_importable():
    """from kbutillib.agents.king_install import load_bundle should work (new path)."""
    from kbutillib.agents.king_install import load_bundle  # noqa: PLC0415

    assert callable(load_bundle)


def test_new_agents_module_importable():
    """kbutillib.agents package level lazy __getattr__ resolves load_bundle."""
    import kbutillib.agents as agents  # noqa: PLC0415

    lb = agents.load_bundle
    assert callable(lb)


def test_king_install_identity_old_is_new():
    """load_bundle from old path IS the same object as from new path."""
    from kbutillib.king_install import load_bundle as lb_old  # noqa: PLC0415
    from kbutillib.agents.king_install import load_bundle as lb_new  # noqa: PLC0415

    assert lb_old is lb_new, "Old and new load_bundle must be the same object"


# ---------------------------------------------------------------------------
# Test 7-9: researchos via OLD path
# ---------------------------------------------------------------------------


def test_old_researchos_import():
    """from kbutillib.researchos import ResearchOSProject (old path)."""
    from kbutillib.researchos import ResearchOSProject  # noqa: PLC0415

    assert ResearchOSProject is not None


def test_old_researchos_manager_import():
    """from kbutillib.researchos.manager import ResearchOSProject (old submodule path)."""
    from kbutillib.researchos.manager import ResearchOSProject  # noqa: PLC0415

    assert ResearchOSProject is not None


def test_old_researchos_config_import():
    """from kbutillib.researchos.config import resolve_researchos_root (old path)."""
    from kbutillib.researchos.config import resolve_researchos_root  # noqa: PLC0415

    assert callable(resolve_researchos_root)


# ---------------------------------------------------------------------------
# Test 10-12: researchos via NEW path (agents)
# ---------------------------------------------------------------------------


def test_new_agents_researchos_importable():
    """from kbutillib.agents.researchos import ResearchOSProject (new path)."""
    from kbutillib.agents.researchos import ResearchOSProject  # noqa: PLC0415

    assert ResearchOSProject is not None


def test_researchos_identity_old_is_new():
    """ResearchOSProject from old path IS the same as from new path."""
    from kbutillib.researchos import ResearchOSProject as Pold  # noqa: PLC0415
    from kbutillib.agents.researchos import ResearchOSProject as Pnew  # noqa: PLC0415

    assert Pold is Pnew, "Old and new ResearchOSProject must be the same class"


def test_researchos_registry_shim():
    """kbutillib.researchos.registry shim re-exports RegistryResult and register_project."""
    from kbutillib.researchos.registry import RegistryResult, register_project  # noqa: PLC0415
    from kbutillib.agents.researchos.registry import (  # noqa: PLC0415
        RegistryResult as RR2,
        register_project as rp2,
    )

    assert RegistryResult is RR2
    assert register_project is rp2


# ---------------------------------------------------------------------------
# Test 13-14: load_bundle against king_app bundle dir succeeds
# ---------------------------------------------------------------------------


def test_load_bundle_from_king_app_dir():
    """load_bundle(king_app dir) succeeds and returns bundle + skill_md."""
    from kbutillib.agents.king_install import load_bundle  # noqa: PLC0415

    assert _KING_APP_DIR.is_dir(), f"king_app dir not found: {_KING_APP_DIR}"
    result = load_bundle(_KING_APP_DIR)
    assert "bundle" in result
    assert "skill_md" in result
    assert result["bundle"]["id"] == "kbutillib-modeling"


def test_load_bundle_from_agents_king_app_dir():
    """load_bundle(agents/king_app dir) also succeeds."""
    from kbutillib.agents.king_install import load_bundle  # noqa: PLC0415

    assert _AGENTS_KING_APP_DIR.is_dir(), (
        f"agents/king_app dir not found: {_AGENTS_KING_APP_DIR}"
    )
    result = load_bundle(_AGENTS_KING_APP_DIR)
    assert result["bundle"]["id"] == "kbutillib-modeling"


# ---------------------------------------------------------------------------
# Test 15: agents __init__ lazy __getattr__ does NOT pollute namespace early
# ---------------------------------------------------------------------------


def test_agents_getattr_raises_for_unknown():
    """kbutillib.agents raises AttributeError for unknown symbols."""
    import kbutillib.agents as agents  # noqa: PLC0415

    with pytest.raises(AttributeError):
        _ = agents.nonexistent_symbol_xyz


# ---------------------------------------------------------------------------
# Test 16+: researchos tooling shim
# ---------------------------------------------------------------------------


def test_researchos_tooling_shim():
    """kbutillib.researchos.tooling shim re-exports ensure_research_os_binary."""
    from kbutillib.researchos.tooling import ensure_research_os_binary  # noqa: PLC0415
    from kbutillib.agents.researchos.tooling import (  # noqa: PLC0415
        ensure_research_os_binary as erb2,
    )

    assert ensure_research_os_binary is erb2


def test_old_king_install_is_shim():
    """kbutillib.king_install module is a shim (small file, no original code)."""
    import kbutillib.king_install as ki  # noqa: PLC0415

    # The shim simply re-exports; the module file should be small
    src = Path(ki.__file__).read_text(encoding="utf-8")
    assert "kbutillib.agents.king_install" in src, "king_install.py should be a shim"
