"""WP12 — cheminformatics + verAB unification: shim + domain import regression tests.

Tests
-----
1.  Old path: ``from kbutillib.cheminformatics import BackendUnavailableError`` works.
2.  New path: ``from kbutillib.domains.cheminformatics import BackendUnavailableError`` works.
3.  Same-object check: old and new paths resolve to identical class.
4.  Old path: ``PickaxeBackend`` importable.
5.  New path: ``PickaxeBackend`` importable — same class.
6.  Old path: ``RetroRulesBackend`` importable.
7.  New path: ``RetroRulesBackend`` importable — same class.
8.  Old path: ``cheminformatics.base`` submodule importable.
9.  New path: ``domains.cheminformatics.base`` submodule importable.
10. Old path: ``kbutillib.cheminformatics.verab`` importable.
11. New path: ``kbutillib.domains.cheminformatics.verab`` importable.
12. verAB smarts constants importable via old path.
13. verAB smarts constants same object via new path.
14. ``from kbutillib.domains.cheminformatics.verab.facade import VerabUtilsImpl`` works (old top-level path).
15. ``from kbutillib.domains.cheminformatics.verab_utils import VerabUtilsImpl`` works.
16. VerabUtilsImpl same class via old and new paths.
17. ``import kbutillib`` clean (no ImportError).
18. ``KBUtilLib().verab`` accessible.
19. ``KBUtilLib().network_expansion`` accessible.
20. Shim ``__all__`` contains expected public names.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 1. Old path — BackendUnavailableError
# ---------------------------------------------------------------------------


def test_old_path_backend_unavailable_error():
    """Old cheminformatics shim path must be importable."""
    from kbutillib.cheminformatics import BackendUnavailableError  # noqa: F401
    assert BackendUnavailableError is not None


# ---------------------------------------------------------------------------
# 2. New path — BackendUnavailableError
# ---------------------------------------------------------------------------


def test_new_path_backend_unavailable_error():
    """New domain path must be importable."""
    from kbutillib.domains.cheminformatics import BackendUnavailableError  # noqa: F401
    assert BackendUnavailableError is not None


# ---------------------------------------------------------------------------
# 3. Same-object check — BackendUnavailableError
# ---------------------------------------------------------------------------


def test_backend_unavailable_error_same_object():
    """Both paths must resolve to the identical class."""
    from kbutillib.cheminformatics import BackendUnavailableError as OLD
    from kbutillib.domains.cheminformatics import BackendUnavailableError as NEW
    assert OLD is NEW, "BackendUnavailableError must be identical via both import paths"


# ---------------------------------------------------------------------------
# 4. Old path — PickaxeBackend
# ---------------------------------------------------------------------------


def test_old_path_pickaxe_backend():
    from kbutillib.cheminformatics import PickaxeBackend  # noqa: F401
    assert PickaxeBackend is not None


# ---------------------------------------------------------------------------
# 5. New path — PickaxeBackend same object
# ---------------------------------------------------------------------------


def test_pickaxe_backend_same_object():
    from kbutillib.cheminformatics import PickaxeBackend as OLD
    from kbutillib.domains.cheminformatics import PickaxeBackend as NEW
    assert OLD is NEW


# ---------------------------------------------------------------------------
# 6. Old path — RetroRulesBackend
# ---------------------------------------------------------------------------


def test_old_path_retrorules_backend():
    from kbutillib.cheminformatics import RetroRulesBackend  # noqa: F401
    assert RetroRulesBackend is not None


# ---------------------------------------------------------------------------
# 7. New path — RetroRulesBackend same object
# ---------------------------------------------------------------------------


def test_retrorules_backend_same_object():
    from kbutillib.cheminformatics import RetroRulesBackend as OLD
    from kbutillib.domains.cheminformatics import RetroRulesBackend as NEW
    assert OLD is NEW


# ---------------------------------------------------------------------------
# 8. Old path — cheminformatics.base submodule
# ---------------------------------------------------------------------------


def test_old_cheminformatics_base_importable():
    from kbutillib.cheminformatics.base import (
        BackendUnavailableError,
        ExpansionBackend,
        ExpansionResult,
        PredictedCompound,
        PredictedReaction,
    )
    assert BackendUnavailableError is not None
    assert ExpansionBackend is not None
    assert ExpansionResult is not None
    assert PredictedCompound is not None
    assert PredictedReaction is not None


# ---------------------------------------------------------------------------
# 9. New path — domains.cheminformatics.base submodule
# ---------------------------------------------------------------------------


def test_new_cheminformatics_base_importable():
    from kbutillib.domains.cheminformatics.base import (
        BackendUnavailableError,
        PredictedReaction,
    )
    assert BackendUnavailableError is not None
    assert PredictedReaction is not None


# ---------------------------------------------------------------------------
# 10. Old path — cheminformatics.verab
# ---------------------------------------------------------------------------


def test_old_cheminformatics_verab_importable():
    """Old cheminformatics/verab package shim must be importable."""
    import kbutillib.cheminformatics.verab  # noqa: F401


# ---------------------------------------------------------------------------
# 11. New path — domains.cheminformatics.verab
# ---------------------------------------------------------------------------


def test_new_cheminformatics_verab_importable():
    """New domains/cheminformatics/verab package must be importable."""
    import kbutillib.domains.cheminformatics.verab  # noqa: F401


# ---------------------------------------------------------------------------
# 12. verAB smarts constants via old path
# ---------------------------------------------------------------------------


def test_old_verab_smarts_importable():
    from kbutillib.cheminformatics.verab.smarts import (
        METHOXY_AROMATIC_SMARTS,
        SEED_COMPOUNDS,
        VERAB_ODEMETHYLATION_SMARTS,
    )
    assert isinstance(VERAB_ODEMETHYLATION_SMARTS, str)
    assert isinstance(METHOXY_AROMATIC_SMARTS, str)
    assert isinstance(SEED_COMPOUNDS, list)
    assert len(SEED_COMPOUNDS) == 5


# ---------------------------------------------------------------------------
# 13. verAB smarts constants same object via new path
# ---------------------------------------------------------------------------


def test_verab_smarts_same_object():
    from kbutillib.cheminformatics.verab.smarts import (
        VERAB_ODEMETHYLATION_SMARTS as OLD,
    )
    from kbutillib.domains.cheminformatics.verab.smarts import (
        VERAB_ODEMETHYLATION_SMARTS as NEW,
    )
    assert OLD is NEW


# ---------------------------------------------------------------------------
# 14. Old top-level path — verab_utils.VerabUtilsImpl
# ---------------------------------------------------------------------------


def test_old_top_level_verab_utils_impl():
    """Old top-level kbutillib.verab_utils.VerabUtilsImpl must work."""
    from kbutillib.domains.cheminformatics.verab.facade import VerabUtilsImpl  # noqa: F401
    assert VerabUtilsImpl is not None


# ---------------------------------------------------------------------------
# 15. New path — domains.cheminformatics.verab_utils.VerabUtilsImpl
# ---------------------------------------------------------------------------


def test_new_verab_utils_impl():
    from kbutillib.domains.cheminformatics.verab_utils import (
        VerabUtilsImpl,  # noqa: F401
    )
    assert VerabUtilsImpl is not None


# ---------------------------------------------------------------------------
# 16. VerabUtilsImpl same class via old and new paths
# ---------------------------------------------------------------------------


def test_verab_utils_impl_same_object():
    from kbutillib.domains.cheminformatics.verab_utils import VerabUtilsImpl as NEW
    from kbutillib.domains.cheminformatics.verab.facade import VerabUtilsImpl as OLD
    assert OLD is NEW, "VerabUtilsImpl must be identical via both import paths"


# ---------------------------------------------------------------------------
# 17. kbutillib import clean
# ---------------------------------------------------------------------------


def test_kbutillib_import_clean():
    import kbutillib  # noqa: F401


# ---------------------------------------------------------------------------
# 18. KBUtilLib().verab accessible
# ---------------------------------------------------------------------------


def test_kbutillib_verab_attr():
    from kbutillib import KBUtilLib
    kbu = KBUtilLib()
    assert hasattr(kbu, "verab"), "KBUtilLib instance must have a .verab attribute"
    verab = kbu.verab
    assert verab is not None, "kbu.verab must not be None"


# ---------------------------------------------------------------------------
# 19. KBUtilLib().network_expansion accessible
# ---------------------------------------------------------------------------


def test_kbutillib_network_expansion_attr():
    from kbutillib import KBUtilLib
    kbu = KBUtilLib()
    assert hasattr(kbu, "network_expansion"), "KBUtilLib must have .network_expansion attribute"
    ne = kbu.network_expansion
    assert ne is not None


# ---------------------------------------------------------------------------
# 20. Shim __all__ contains expected public names
# ---------------------------------------------------------------------------


def test_shim_all_contents():
    import kbutillib.cheminformatics as shim
    assert hasattr(shim, "__all__"), "cheminformatics shim must define __all__"
    assert "BackendUnavailableError" in shim.__all__
    assert "ExpansionBackend" in shim.__all__
    assert "ExpansionResult" in shim.__all__
    assert "PickaxeBackend" in shim.__all__
    assert "RetroRulesBackend" in shim.__all__


def test_verab_utils_shim_all():
    import kbutillib.domains.cheminformatics.verab.facade as shim
    assert hasattr(shim, "__all__"), "verab_utils shim must define __all__"
    assert "VerabUtils" in shim.__all__
    assert "VerabUtilsImpl" in shim.__all__
