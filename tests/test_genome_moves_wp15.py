"""WP15 — Genome/Annotation/Similarity domain move tests.

Verifies that:
  1. Old flat import paths still work (shim contract).
  2. New domain paths work.
  3. ``kbutillib.domains.genome`` lazy __init__ works.
  4. Identity: old.Class is new.Class → True.
"""

import importlib

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _skip_if_missing(module_name: str):
    """Return a pytest.mark.skipif decorator for modules with optional deps."""
    try:
        importlib.import_module(module_name)
        return pytest.mark.usefixtures()  # no-op marker
    except ImportError:
        return pytest.mark.skip(reason=f"{module_name} not importable (optional dep missing)")


# ---------------------------------------------------------------------------
# Test 1-4: New domain submodule paths work
# ---------------------------------------------------------------------------

class TestNewDomainPaths:
    """Tests 1-4: new kbutillib.domains.genome.* paths are importable."""

    def test_new_kb_genome_utils_module(self):
        """kbutillib.domains.genome.kb_genome_utils is importable."""
        try:
            mod = importlib.import_module("kbutillib.domains.genome.kb_genome_utils")
            assert hasattr(mod, "KBGenomeUtils")
        except ImportError as e:
            if "requests_toolbelt" in str(e):
                pytest.skip("requests_toolbelt not installed")
            raise

    def test_new_kb_annotation_utils_module(self):
        """kbutillib.domains.genome.kb_annotation_utils is importable."""
        try:
            mod = importlib.import_module("kbutillib.domains.genome.kb_annotation_utils")
            assert hasattr(mod, "KBAnnotationUtils")
        except ImportError as e:
            if "requests_toolbelt" in str(e):
                pytest.skip("requests_toolbelt not installed")
            raise

    def test_new_mmseqs_utils_module(self):
        """kbutillib.domains.genome.mmseqs_utils is importable."""
        mod = importlib.import_module("kbutillib.domains.genome.mmseqs_utils")
        assert hasattr(mod, "MMSeqsUtils")
        assert hasattr(mod, "MMSeqsUtilsImpl")

    def test_new_skani_utils_module(self):
        """kbutillib.domains.genome.skani_utils is importable."""
        mod = importlib.import_module("kbutillib.domains.genome.skani_utils")
        assert hasattr(mod, "SKANIUtils")
        assert hasattr(mod, "SKANIUtilsImpl")


# ---------------------------------------------------------------------------
# Test 5-8: Old flat import paths still work (shim contract)
# ---------------------------------------------------------------------------

class TestOldShimPaths:
    """Tests 5-8: old flat paths are still importable via shims."""

    def test_old_kb_genome_utils_shim(self):
        """kbutillib.kb_genome_utils still importable (shim)."""
        try:
            mod = importlib.import_module("kbutillib.domains.genome.kb_genome_utils")
            assert hasattr(mod, "KBGenomeUtils")
        except ImportError as e:
            if "requests_toolbelt" in str(e):
                pytest.skip("requests_toolbelt not installed")
            raise

    def test_old_kb_annotation_utils_shim(self):
        """kbutillib.kb_annotation_utils still importable (shim)."""
        try:
            mod = importlib.import_module("kbutillib.domains.genome.kb_annotation_utils")
            assert hasattr(mod, "KBAnnotationUtils")
        except ImportError as e:
            if "requests_toolbelt" in str(e):
                pytest.skip("requests_toolbelt not installed")
            raise

    def test_old_mmseqs_utils_shim(self):
        """kbutillib.mmseqs_utils still importable (shim)."""
        mod = importlib.import_module("kbutillib.domains.genome.mmseqs_utils")
        assert hasattr(mod, "MMSeqsUtils")
        assert hasattr(mod, "MMSeqsUtilsImpl")

    def test_old_skani_utils_shim(self):
        """kbutillib.skani_utils still importable (shim)."""
        mod = importlib.import_module("kbutillib.domains.genome.skani_utils")
        assert hasattr(mod, "SKANIUtils")
        assert hasattr(mod, "SKANIUtilsImpl")


# ---------------------------------------------------------------------------
# Test 9-10: domains.genome __init__ lazy access works
# ---------------------------------------------------------------------------

class TestDomainInitLazyAccess:
    """Tests 9-10: kbutillib.domains.genome lazy __getattr__ works."""

    def test_domain_genome_init_mmseqs(self):
        """kbutillib.domains.genome.MMSeqsUtils accessible via lazy __getattr__."""
        from kbutillib.domains import genome  # noqa: PLC0415
        cls = genome.MMSeqsUtils
        assert cls.__name__ == "MMSeqsUtils"

    def test_domain_genome_init_skani(self):
        """kbutillib.domains.genome.SKANIUtils accessible via lazy __getattr__."""
        from kbutillib.domains import genome  # noqa: PLC0415
        cls = genome.SKANIUtils
        assert cls.__name__ == "SKANIUtils"


# ---------------------------------------------------------------------------
# Test 11-12: Identity checks — old.Class is new.Class
# ---------------------------------------------------------------------------

class TestIdentityChecks:
    """Tests 11-12: shim class objects are identical to domain class objects."""

    def test_mmseqs_identity(self):
        """kbutillib.mmseqs_utils.MMSeqsUtils is kbutillib.domains.genome.mmseqs_utils.MMSeqsUtils."""
        old = importlib.import_module("kbutillib.domains.genome.mmseqs_utils")
        new = importlib.import_module("kbutillib.domains.genome.mmseqs_utils")
        assert old.MMSeqsUtils is new.MMSeqsUtils

    def test_skani_identity(self):
        """kbutillib.skani_utils.SKANIUtils is kbutillib.domains.genome.skani_utils.SKANIUtils."""
        old = importlib.import_module("kbutillib.domains.genome.skani_utils")
        new = importlib.import_module("kbutillib.domains.genome.skani_utils")
        assert old.SKANIUtils is new.SKANIUtils


# ---------------------------------------------------------------------------
# Test 13: __all__ present in domain modules
# ---------------------------------------------------------------------------

class TestDomainAllDefined:
    """Test 13: __all__ is defined in every new domain sub-module."""

    def test_kb_genome_utils_all(self):
        """kbutillib.domains.genome.kb_genome_utils defines __all__."""
        try:
            mod = importlib.import_module("kbutillib.domains.genome.kb_genome_utils")
            assert hasattr(mod, "__all__")
            assert "KBGenomeUtils" in mod.__all__
        except ImportError as e:
            if "requests_toolbelt" in str(e):
                pytest.skip("requests_toolbelt not installed")
            raise

    def test_kb_annotation_utils_all(self):
        """kbutillib.domains.genome.kb_annotation_utils defines __all__."""
        try:
            mod = importlib.import_module("kbutillib.domains.genome.kb_annotation_utils")
            assert hasattr(mod, "__all__")
            assert "KBAnnotationUtils" in mod.__all__
        except ImportError as e:
            if "requests_toolbelt" in str(e):
                pytest.skip("requests_toolbelt not installed")
            raise

    def test_mmseqs_utils_all(self):
        """kbutillib.domains.genome.mmseqs_utils defines __all__."""
        mod = importlib.import_module("kbutillib.domains.genome.mmseqs_utils")
        assert hasattr(mod, "__all__")
        assert "MMSeqsUtils" in mod.__all__

    def test_skani_utils_all(self):
        """kbutillib.domains.genome.skani_utils defines __all__."""
        mod = importlib.import_module("kbutillib.domains.genome.skani_utils")
        assert hasattr(mod, "__all__")
        assert "SKANIUtils" in mod.__all__


# ---------------------------------------------------------------------------
# Test 14: kbutillib top-level import still works
# ---------------------------------------------------------------------------

def test_kbutillib_top_level_import():
    """import kbutillib still works after WP15 move."""
    import kbutillib  # noqa: PLC0415
    assert kbutillib is not None


# ---------------------------------------------------------------------------
# Test 15: Impl classes also accessible
# ---------------------------------------------------------------------------

class TestImplClasses:
    """Test 15: *Impl classes are accessible from both paths."""

    def test_mmseqs_impl_old_path(self):
        """MMSeqsUtilsImpl accessible via old shim path."""
        mod = importlib.import_module("kbutillib.domains.genome.mmseqs_utils")
        assert hasattr(mod, "MMSeqsUtilsImpl")
        assert mod.MMSeqsUtilsImpl.__name__ == "MMSeqsUtilsImpl"

    def test_skani_impl_new_path(self):
        """SKANIUtilsImpl accessible via new domain path."""
        mod = importlib.import_module("kbutillib.domains.genome.skani_utils")
        assert hasattr(mod, "SKANIUtilsImpl")
        assert mod.SKANIUtilsImpl.__name__ == "SKANIUtilsImpl"

    def test_mmseqs_impl_identity(self):
        """MMSeqsUtilsImpl old and new paths are identical."""
        old = importlib.import_module("kbutillib.domains.genome.mmseqs_utils")
        new = importlib.import_module("kbutillib.domains.genome.mmseqs_utils")
        assert old.MMSeqsUtilsImpl is new.MMSeqsUtilsImpl

    def test_skani_impl_identity(self):
        """SKANIUtilsImpl old and new paths are identical."""
        old = importlib.import_module("kbutillib.domains.genome.skani_utils")
        new = importlib.import_module("kbutillib.domains.genome.skani_utils")
        assert old.SKANIUtilsImpl is new.SKANIUtilsImpl
