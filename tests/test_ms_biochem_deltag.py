"""Tests for KBUtilLib package imports."""

from kbutillib import BaseUtils, SharedEnvUtils


def test_core_imports():
    """Verify core modules are importable."""
    assert BaseUtils is not None
    assert SharedEnvUtils is not None
