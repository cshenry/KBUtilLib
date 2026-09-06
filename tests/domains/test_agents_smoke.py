"""Smoke tests for agents (king_install) + KBUtilLib facade.

Verifies import paths and structural API without network calls or side effects.
These tests are OFFLINE — no agent processes are started.
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# 1. agents package importable + lazy __getattr__
# ---------------------------------------------------------------------------


def test_agents_package_importable() -> None:
    """kbutillib.agents package is importable."""
    import kbutillib.agents as agents  # noqa: PLC0415
    assert agents is not None


def test_agents_king_install_importable() -> None:
    """kbutillib.agents.king_install is importable."""
    from kbutillib.agents import king_install  # noqa: PLC0415
    assert king_install is not None


# ---------------------------------------------------------------------------
# 2. king_install structural API
# ---------------------------------------------------------------------------


def test_king_install_load_bundle_callable() -> None:
    """king_install.load_bundle is callable."""
    from kbutillib.agents.king_install import load_bundle  # noqa: PLC0415
    assert callable(load_bundle)


def test_king_install_install_callable() -> None:
    """king_install.install is callable."""
    from kbutillib.agents.king_install import install  # noqa: PLC0415
    assert callable(install)


def test_king_install_status_callable() -> None:
    """king_install.status is callable."""
    from kbutillib.agents.king_install import status  # noqa: PLC0415
    assert callable(status)


def test_king_install_read_registry_callable() -> None:
    """king_install.read_registry is callable."""
    from kbutillib.agents.king_install import read_registry  # noqa: PLC0415
    assert callable(read_registry)


def test_king_install_bundle_error_importable() -> None:
    """king_install.BundleError is importable (exception class)."""
    from kbutillib.agents.king_install import BundleError  # noqa: PLC0415
    assert BundleError is not None
    assert issubclass(BundleError, Exception)


def test_king_install_resolve_apps_dir_callable() -> None:
    """king_install.resolve_apps_dir is callable."""
    from kbutillib.agents.king_install import resolve_apps_dir  # noqa: PLC0415
    assert callable(resolve_apps_dir)


# ---------------------------------------------------------------------------
# 4. KBUtilLib facade — domain property types (no network)
# ---------------------------------------------------------------------------


def test_kbutillib_biochem_property_type() -> None:
    """KBUtilLib().biochem returns MSBiochemUtilsImpl."""
    from kbutillib import KBUtilLib  # noqa: PLC0415
    from kbutillib.domains.biochem import MSBiochemUtilsImpl  # noqa: PLC0415
    app = KBUtilLib()
    assert isinstance(app.biochem, MSBiochemUtilsImpl)


def test_kbutillib_thermo_property_type() -> None:
    """KBUtilLib().thermo returns ThermoUtilsImpl."""
    from kbutillib import KBUtilLib  # noqa: PLC0415
    from kbutillib.domains.thermo import ThermoUtilsImpl  # noqa: PLC0415
    app = KBUtilLib()
    assert isinstance(app.thermo, ThermoUtilsImpl)


def test_kbutillib_berdl_property_type() -> None:
    """KBUtilLib().berdl returns KBBERDLUtilsImpl."""
    from kbutillib import KBUtilLib  # noqa: PLC0415
    from kbutillib.domains.kbase.kb_berdl_utils import KBBERDLUtilsImpl  # noqa: PLC0415
    app = KBUtilLib()
    assert isinstance(app.berdl, KBBERDLUtilsImpl)


def test_kbutillib_uniprot_property_type() -> None:
    """KBUtilLib().uniprot returns KBUniProtUtilsImpl."""
    from kbutillib import KBUtilLib  # noqa: PLC0415
    from kbutillib.domains.external.kb_uniprot_utils import KBUniProtUtilsImpl  # noqa: PLC0415
    app = KBUtilLib()
    assert isinstance(app.uniprot, KBUniProtUtilsImpl)


def test_kbutillib_mmseqs_property_type() -> None:
    """KBUtilLib().mmseqs returns MMSeqsUtilsImpl."""
    from kbutillib import KBUtilLib  # noqa: PLC0415
    from kbutillib.domains.genome.mmseqs_utils import MMSeqsUtilsImpl  # noqa: PLC0415
    app = KBUtilLib()
    assert isinstance(app.mmseqs, MMSeqsUtilsImpl)


def test_kbutillib_skani_property_type() -> None:
    """KBUtilLib().skani returns SKANIUtilsImpl."""
    from kbutillib import KBUtilLib  # noqa: PLC0415
    from kbutillib.domains.genome.skani_utils import SKANIUtilsImpl  # noqa: PLC0415
    app = KBUtilLib()
    assert isinstance(app.skani, SKANIUtilsImpl)
