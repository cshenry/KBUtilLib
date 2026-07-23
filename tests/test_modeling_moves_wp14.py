"""WP14 — Modeling family move verification tests.

Verifies that:
1. All old import paths still work (backward compat shim contract).
2. All new domain import paths work.
3. Symbols from old and new paths are identical objects (shim identity).
4. The domains.modeling namespace works for aggregated access.
"""

import pytest


# ---------------------------------------------------------------------------
# model_directionality: old path
# ---------------------------------------------------------------------------

def test_old_path_model_directionality_direction_conversion():
    from kbutillib.model_directionality import direction_conversion
    assert isinstance(direction_conversion, dict)
    assert direction_conversion["forward"] == ">"


def test_old_path_model_directionality_directionality_from_bounds():
    from kbutillib.model_directionality import directionality_from_bounds
    assert callable(directionality_from_bounds)


def test_old_path_model_directionality_combine():
    from kbutillib.model_directionality import combine_directionality_signals
    assert callable(combine_directionality_signals)


# ---------------------------------------------------------------------------
# model_directionality: new domain path
# ---------------------------------------------------------------------------

def test_new_path_modeling_model_directionality_direction_conversion():
    from kbutillib.domains.modeling.model_directionality import direction_conversion
    assert isinstance(direction_conversion, dict)
    assert direction_conversion["reverse"] == "<"


def test_new_path_modeling_model_directionality_from_bounds():
    from kbutillib.domains.modeling.model_directionality import directionality_from_bounds
    assert callable(directionality_from_bounds)


# ---------------------------------------------------------------------------
# Identity: old is new (same object via shim)
# ---------------------------------------------------------------------------

def test_identity_model_directionality_direction_conversion():
    from kbutillib.model_directionality import direction_conversion as old
    from kbutillib.domains.modeling.model_directionality import direction_conversion as new
    assert old is new


def test_identity_model_directionality_from_bounds():
    from kbutillib.model_directionality import directionality_from_bounds as old
    from kbutillib.domains.modeling.model_directionality import directionality_from_bounds as new
    assert old is new


# ---------------------------------------------------------------------------
# model_helpers: old path
# ---------------------------------------------------------------------------

def test_old_path_model_helpers_parse_id():
    from kbutillib.model_helpers import _parse_id
    assert callable(_parse_id)
    base, comp, idx = _parse_id("cpd00001[c]")
    assert base == "cpd00001"
    assert comp == "c"


def test_old_path_model_helpers_check_convert():
    from kbutillib.model_helpers import _check_and_convert_model
    assert callable(_check_and_convert_model)


# ---------------------------------------------------------------------------
# model_helpers: new domain path
# ---------------------------------------------------------------------------

def test_new_path_modeling_model_helpers_parse_id():
    from kbutillib.domains.modeling.model_helpers import _parse_id
    assert callable(_parse_id)
    base, comp, idx = _parse_id("adp[c]")
    assert base == "adp"
    assert comp == "c"


def test_identity_model_helpers_parse_id():
    from kbutillib.model_helpers import _parse_id as old
    from kbutillib.domains.modeling.model_helpers import _parse_id as new
    assert old is new


# ---------------------------------------------------------------------------
# model_standardization_utils: old path
# ---------------------------------------------------------------------------

def test_old_path_model_standardization_utils_class():
    from kbutillib.model_standardization_utils import ModelStandardizationUtils
    assert ModelStandardizationUtils is not None


def test_old_path_model_standardization_utils_direction():
    from kbutillib.model_standardization_utils import direction_conversion
    assert isinstance(direction_conversion, dict)
    assert direction_conversion["reversible"] == "="


def test_old_path_model_standardization_utils_compartment_types():
    from kbutillib.model_standardization_utils import compartment_types
    assert isinstance(compartment_types, dict)
    assert "cytosol" in compartment_types


# ---------------------------------------------------------------------------
# model_standardization_utils: new domain path
# ---------------------------------------------------------------------------

def test_new_path_modeling_model_standardization_utils():
    from kbutillib.domains.modeling.model_standardization_utils import ModelStandardizationUtils
    assert ModelStandardizationUtils is not None


def test_identity_model_standardization_utils_class():
    from kbutillib.model_standardization_utils import ModelStandardizationUtils as old
    from kbutillib.domains.modeling.model_standardization_utils import ModelStandardizationUtils as new
    assert old is new


# ---------------------------------------------------------------------------
# kb_model_utils shim: old path
# ---------------------------------------------------------------------------

def test_old_path_kb_model_utils_module_importable():
    """Shim at kbutillib.kb_model_utils must be importable (no mandatory deps at import)."""
    import importlib
    # We can't *instantiate* KBModelUtils without cobra/modelseedpy,
    # but we can verify the shim module itself imports cleanly.
    try:
        mod = importlib.import_module("kbutillib.kb_model_utils")
        # If the shim loaded, KBModelUtils attr should exist
        assert hasattr(mod, "KBModelUtils")
    except ImportError as exc:
        # Optional dep (cobra/modelseedpy) missing — expected in CI, not a shim failure
        pytest.skip(f"Optional dep missing (expected in CI): {exc}")


def test_old_path_kb_model_utils_impl_importable():
    import importlib
    try:
        mod = importlib.import_module("kbutillib.kb_model_utils")
        assert hasattr(mod, "KBModelUtilsImpl")
    except ImportError as exc:
        pytest.skip(f"Optional dep missing: {exc}")


# ---------------------------------------------------------------------------
# ms_fba_utils shim: old path
# ---------------------------------------------------------------------------

def test_old_path_ms_fba_utils_importable():
    import importlib
    try:
        mod = importlib.import_module("kbutillib.ms_fba_utils")
        assert hasattr(mod, "MSFBAUtils")
        assert hasattr(mod, "MSFBAUtilsImpl")
    except ImportError as exc:
        pytest.skip(f"Optional dep missing: {exc}")


# ---------------------------------------------------------------------------
# ms_template_utils shim: old path
# ---------------------------------------------------------------------------

def test_old_path_ms_template_utils_importable():
    import importlib
    try:
        mod = importlib.import_module("kbutillib.ms_template_utils")
        assert hasattr(mod, "MSTemplateUtils")
        assert hasattr(mod, "MSTemplateUtilsImpl")
    except ImportError as exc:
        pytest.skip(f"Optional dep missing: {exc}")


# ---------------------------------------------------------------------------
# ms_reconstruction_utils shim: old path
# ---------------------------------------------------------------------------

def test_old_path_ms_reconstruction_utils_importable():
    import importlib
    try:
        mod = importlib.import_module("kbutillib.ms_reconstruction_utils")
        assert hasattr(mod, "MSReconstructionUtils")
        assert hasattr(mod, "MSReconstructionUtilsImpl")
    except ImportError as exc:
        pytest.skip(f"Optional dep missing: {exc}")


# ---------------------------------------------------------------------------
# domains.modeling aggregate namespace
# ---------------------------------------------------------------------------

def test_domains_modeling_direction_conversion():
    from kbutillib.domains.modeling import direction_conversion
    assert isinstance(direction_conversion, dict)
    assert direction_conversion["blocked"] == "B"


def test_domains_modeling_compartment_types():
    from kbutillib.domains.modeling import compartment_types
    assert isinstance(compartment_types, dict)
    assert compartment_types["c"] == "c"


def test_domains_modeling_model_directionality_submodule():
    """Direct submodule import from new domain location works."""
    from kbutillib.domains.modeling import model_directionality
    assert hasattr(model_directionality, "direction_conversion")


def test_domains_modeling_model_helpers_submodule():
    """Direct submodule import from new domain location works."""
    from kbutillib.domains.modeling import model_helpers
    assert hasattr(model_helpers, "_parse_id")


# ---------------------------------------------------------------------------
# Functional smoke: directionality helpers work correctly
# ---------------------------------------------------------------------------

class _FakeReaction:
    """Minimal fake COBRApy Reaction for testing directionality_from_bounds."""
    def __init__(self, lb, ub):
        self.lower_bound = lb
        self.upper_bound = ub


def test_directionality_from_bounds_forward():
    from kbutillib.domains.modeling.model_directionality import directionality_from_bounds
    rxn = _FakeReaction(0.0, 1000.0)
    assert directionality_from_bounds(rxn) == "forward"


def test_directionality_from_bounds_reversible():
    from kbutillib.domains.modeling.model_directionality import directionality_from_bounds
    rxn = _FakeReaction(-1000.0, 1000.0)
    assert directionality_from_bounds(rxn) == "reversible"


def test_directionality_from_bounds_blocked():
    from kbutillib.domains.modeling.model_directionality import directionality_from_bounds
    rxn = _FakeReaction(0.0, 0.0)
    assert directionality_from_bounds(rxn) == "blocked"


def test_parse_id_plain():
    from kbutillib.domains.modeling.model_helpers import _parse_id
    base, comp, idx = _parse_id("cpd00001")
    assert base == "cpd00001"
    assert comp is None


def test_parse_id_bracket():
    from kbutillib.domains.modeling.model_helpers import _parse_id
    base, comp, idx = _parse_id("cpd00001[c0]")
    assert base == "cpd00001"
    assert comp == "c"


def test_parse_id_underscore():
    from kbutillib.domains.modeling.model_helpers import _parse_id
    base, comp, idx = _parse_id("cpd01024_c0")
    assert base == "cpd01024"
    assert comp == "c"
    assert idx == "0"


def test_combine_directionality_signals():
    from kbutillib.domains.modeling.model_directionality import combine_directionality_signals
    result = combine_directionality_signals("forward", "reversible")
    assert "model" in result
    assert result["model"] == "forward"
    assert result["biochem"] == "reversible"
    assert "combined" in result


def test_direction_conversion_keys():
    """All expected direction keys are present."""
    from kbutillib.domains.modeling.model_directionality import direction_conversion
    for key in ("", "forward", "reverse", "reversible", "uncertain", "blocked"):
        assert key in direction_conversion
