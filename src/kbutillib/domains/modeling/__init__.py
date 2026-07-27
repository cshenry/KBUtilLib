"""Modeling domain — metabolic model utilities for KBUtilLib.

Sub-modules:
    kb_model_utils          — KBModelUtils, KBModelUtilsImpl
    ms_fba_utils            — MSFBAUtils, MSFBAUtilsImpl
    ms_template_utils       — MSTemplateUtils, MSTemplateUtilsImpl
    ms_reconstruction_utils — MSReconstructionUtils, MSReconstructionUtilsImpl
    model_helpers           — _parse_id, _check_and_convert_model (canonical helpers)
    model_directionality    — directionality_from_bounds, direction_conversion, etc.
    model_standardization_utils — ModelStandardizationUtils, ModelStandardizationUtilsImpl

Imports here are lazy to avoid pulling in optional heavy dependencies
(cobra, modelseedpy, requests_toolbelt) at package-init time.
"""


def __getattr__(name: str):  # noqa: ANN001
    """Lazy attribute access — only imports sub-module symbols when first accessed."""
    import importlib

    # Mapping: public name -> (submodule, attribute_in_submodule)
    _lazy_map = {
        # kb_model_utils
        "KBModelUtils": ("kb_model_utils", "KBModelUtils"),
        "KBModelUtilsImpl": ("kb_model_utils", "KBModelUtilsImpl"),
        # ms_fba_utils
        "MSFBAUtils": ("ms_fba_utils", "MSFBAUtils"),
        "MSFBAUtilsImpl": ("ms_fba_utils", "MSFBAUtilsImpl"),
        # ms_template_utils
        "MSTemplateUtils": ("ms_template_utils", "MSTemplateUtils"),
        "MSTemplateUtilsImpl": ("ms_template_utils", "MSTemplateUtilsImpl"),
        # ms_reconstruction_utils
        "MSReconstructionUtils": ("ms_reconstruction_utils", "MSReconstructionUtils"),
        "MSReconstructionUtilsImpl": ("ms_reconstruction_utils", "MSReconstructionUtilsImpl"),
        # model_helpers
        "_parse_id": ("model_helpers", "_parse_id"),
        "_check_and_convert_model": ("model_helpers", "_check_and_convert_model"),
        # model_directionality
        "direction_conversion": ("model_directionality", "direction_conversion"),
        "directionality_from_bounds": ("model_directionality", "directionality_from_bounds"),
        "biochem_directionality": ("model_directionality", "biochem_directionality"),
        "combine_directionality_signals": ("model_directionality", "combine_directionality_signals"),
        # model_standardization_utils
        "ModelStandardizationUtils": ("model_standardization_utils", "ModelStandardizationUtils"),
        "ModelStandardizationUtilsImpl": ("model_standardization_utils", "ModelStandardizationUtilsImpl"),
        "compartment_types": ("model_standardization_utils", "compartment_types"),
    }

    if name in _lazy_map:
        submodule_name, attr = _lazy_map[name]
        module = importlib.import_module(f"kbutillib.domains.modeling.{submodule_name}")
        return getattr(module, attr)

    raise AttributeError(f"module 'kbutillib.domains.modeling' has no attribute {name!r}")


__all__ = [
    # kb_model_utils
    "KBModelUtils",
    "KBModelUtilsImpl",
    # ms_fba_utils
    "MSFBAUtils",
    "MSFBAUtilsImpl",
    # ms_template_utils
    "MSTemplateUtils",
    "MSTemplateUtilsImpl",
    # ms_reconstruction_utils
    "MSReconstructionUtils",
    "MSReconstructionUtilsImpl",
    # model_helpers
    "_parse_id",
    "_check_and_convert_model",
    # model_directionality
    "direction_conversion",
    "directionality_from_bounds",
    "biochem_directionality",
    "combine_directionality_signals",
    # model_standardization_utils
    "ModelStandardizationUtils",
    "ModelStandardizationUtilsImpl",
    "compartment_types",
]
