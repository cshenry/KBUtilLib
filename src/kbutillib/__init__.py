"""KBUtilLib - Modular utility framework for scientific and development projects."""

# Core utilities - these should always be available
from .base_utils import BaseUtils
from .shared_env_utils import SharedEnvUtils

# Optional modules - wrapped in try-except to handle missing dependencies
try:
    from .notebook_utils import NotebookUtils, DataObject, NumberType, DataType
except ImportError:
    NotebookUtils = None
    DataObject = None
    NumberType = None
    DataType = None

try:
    from .kb_ws_utils import KBWSUtils
except ImportError:
    KBWSUtils = None

try:
    from .kb_genome_utils import KBGenomeUtils
except ImportError:
    KBGenomeUtils = None

try:
    from .ms_biochem_utils import MSBiochemUtils
except ImportError:
    MSBiochemUtils = None

try:
    from .kb_model_standardization_utils import ModelStandardizationUtils
except ImportError:
    ModelStandardizationUtils = None

try:
    from .kb_model_utils import KBModelUtils
except ImportError:
    KBModelUtils = None

try:
    from .ms_fba_utils import MSFBAUtils
except ImportError:
    MSFBAUtils = None

try:
    from .kb_sdk_utils import KBSDKUtils
except ImportError:
    KBSDKUtils = None

try:
    from .argo_utils import ArgoUtils
except ImportError:
    ArgoUtils = None

try:
    from .ai_curation_utils import AICurationUtils
except ImportError:
    AICurationUtils = None

try:
    from .escher_utils import EscherUtils
except ImportError:
    EscherUtils = None

try:
    from .kb_annotation_utils import KBAnnotationUtils
except ImportError:
    KBAnnotationUtils = None

try:
    from .kb_plm_utils import KBPLMUtils
except ImportError:
    KBPLMUtils = None

try:
    from .kb_uniprot_utils import KBUniProtUtils
except ImportError:
    KBUniProtUtils = None

try:
    from .kb_skani_utils import KBSKANIUtils
except ImportError:
    KBSKANIUtils = None

try:
    from .kb_reads_utils import KBReadsUtils
except ImportError:
    KBReadsUtils = None

try:
    from .kb_transyt_utils import KBTransyTUtils
except ImportError:
    KBTransyTUtils = None

try:
    from .cobra_to_kbase_utils import (
        convert_to_kbase_reaction,
        convert_cobra_model_to_kbase,
        build_model_compound,
        build_model_compartment,
        get_compartmets_references,
        get_compounds_references,
        parse_gpr_string,
        build_model_reaction_proteins,
    )
except ImportError:
    convert_to_kbase_reaction = None
    convert_cobra_model_to_kbase = None
    build_model_compound = None
    build_model_compartment = None
    get_compartmets_references = None
    get_compounds_references = None
    parse_gpr_string = None
    build_model_reaction_proteins = None

# Import example composite classes
# Temporarily disabled for testing core functionality
# try:
#     from . import examples
# except ImportError:
#     examples = None
examples = None

__all__ = [
    "ArgoUtilsMSBiochemUtils",
    "BaseUtils",
    "DataObject",
    "DataType",
    "KBAnnotationUtils",
    "KBGenomeUtils",
    "KBTransyTUtils",
    "ModelStandardizationUtils",
    "KBModelUtils",
    "KBPLMUtils",
    "KBReadsUtils",
    "KBSDKUtils",
    "KBSKANIUtils",
    "KBUniProtUtils",
    "KBWSUtils",
    "MSFBAUtils",
    "MSBiochemUtils",
    "NotebookUtils",
    "NumberType",
    "SharedEnvUtils",
    "EscherUtils",
    "examples",
    # COBRA to KBase conversion utilities
    "convert_to_kbase_reaction",
    "convert_cobra_model_to_kbase",
    "build_model_compound",
    "build_model_compartment",
    "get_compartmets_references",
    "get_compounds_references",
    "parse_gpr_string",
    "build_model_reaction_proteins",
]

__version__ = "0.1.0"
