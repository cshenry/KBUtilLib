"""KBUtilLib - Modular utility framework for scientific and development projects."""

import sys

# Core utilities - these should always be available
from .base_utils import BaseUtils
from .shared_env_utils import SharedEnvUtils


def _import_error(module_name: str, error: Exception) -> None:
    """Print import error details to stderr for debugging."""
    print(f"[KBUtilLib] Failed to import {module_name}: {type(error).__name__}: {error}", file=sys.stderr)


# Optional modules - wrapped in try-except to handle missing dependencies
try:
    from .notebook_utils import NotebookUtils, DataObject, NumberType, DataType
except ImportError as e:
    _import_error("notebook_utils", e)
    NotebookUtils = None
    DataObject = None
    NumberType = None
    DataType = None

try:
    from .kb_ws_utils import KBWSUtils
except ImportError as e:
    _import_error("kb_ws_utils", e)
    KBWSUtils = None

try:
    from .kb_genome_utils import KBGenomeUtils
except ImportError as e:
    _import_error("kb_genome_utils", e)
    KBGenomeUtils = None

try:
    from .ms_biochem_utils import MSBiochemUtils
except ImportError as e:
    _import_error("ms_biochem_utils", e)
    MSBiochemUtils = None

try:
    from .model_standardization_utils import ModelStandardizationUtils
except ImportError as e:
    _import_error("model_standardization_utils", e)
    ModelStandardizationUtils = None

try:
    from .kb_model_utils import KBModelUtils
except ImportError as e:
    _import_error("kb_model_utils", e)
    KBModelUtils = None

try:
    from .ms_reconstruction_utils import MSReconstructionUtils
except ImportError as e:
    _import_error("ms_reconstruction_utils", e)
    MSReconstructionUtils = None

try:
    from .ms_fba_utils import MSFBAUtils
except ImportError as e:
    _import_error("ms_fba_utils", e)
    MSFBAUtils = None

try:
    from .kb_sdk_utils import KBSDKUtils
except ImportError as e:
    _import_error("kb_sdk_utils", e)
    KBSDKUtils = None

try:
    from .argo_utils import ArgoUtils
except ImportError as e:
    _import_error("argo_utils", e)
    ArgoUtils = None

try:
    from .ai_curation_utils import AICurationUtils
except ImportError as e:
    _import_error("ai_curation_utils", e)
    AICurationUtils = None

try:
    from .escher_utils import EscherUtils
except ImportError as e:
    _import_error("escher_utils", e)
    EscherUtils = None

try:
    from .kb_annotation_utils import KBAnnotationUtils
except ImportError as e:
    _import_error("kb_annotation_utils", e)
    KBAnnotationUtils = None

try:
    from .kb_plm_utils import KBPLMUtils
except ImportError as e:
    _import_error("kb_plm_utils", e)
    KBPLMUtils = None

try:
    from .kb_uniprot_utils import KBUniProtUtils
except ImportError as e:
    _import_error("kb_uniprot_utils", e)
    KBUniProtUtils = None

try:
    from .skani_utils import SKANIUtils
except ImportError as e:
    _import_error("skani_utils", e)
    SKANIUtils = None

try:
    from .thermo_utils import ThermoUtils
except ImportError as e:
    _import_error("thermo_utils", e)
    ThermoUtils = None

try:
    from .kb_reads_utils import KBReadsUtils, Assembly, AssemblySet, Reads, ReadSet
except ImportError as e:
    _import_error("kb_reads_utils", e)
    KBReadsUtils = None
    Assembly = None
    AssemblySet = None
    Reads = None
    ReadSet = None

try:
    from .bvbrc_utils import BVBRCUtils
except ImportError as e:
    _import_error("bvbrc_utils", e)
    BVBRCUtils = None

try:
    from .patric_ws_utils import PatricWSUtils
except ImportError as e:
    _import_error("patric_ws_utils", e)
    PatricWSUtils = None

try:
    from .rcsb_pdb_utils import RCSBPDBUtils
except ImportError as e:
    _import_error("rcsb_pdb_utils", e)
    RCSBPDBUtils = None

try:
    from .mmseqs_utils import MMSeqsUtils
except ImportError as e:
    _import_error("mmseqs_utils", e)
    MMSeqsUtils = None

try:
    from .kb_berdl_utils import KBBERDLUtils
except ImportError as e:
    _import_error("kb_berdl_utils", e)
    KBBERDLUtils = None

# Import example composite classes
# Temporarily disabled for testing core functionality
# try:
#     from . import examples
# except ImportError:
#     examples = None
examples = None

__all__ = [
    "AICurationUtils",
    "ArgoUtils",
    "Assembly",
    "AssemblySet",
    "BaseUtils",
    "BVBRCUtils",
    "DataObject",
    "DataType",
    "EscherUtils",
    "KBAnnotationUtils",
    "KBBERDLUtils",
    "KBGenomeUtils",
    "KBModelUtils",
    "KBPLMUtils",
    "KBReadsUtils",
    "KBSDKUtils",
    "KBUniProtUtils",
    "KBWSUtils",
    "MMSeqsUtils",
    "ModelStandardizationUtils",
    "MSBiochemUtils",
    "MSFBAUtils",
    "MSReconstructionUtils",
    "NotebookUtils",
    "NumberType",
    "PatricWSUtils",
    "RCSBPDBUtils",
    "Reads",
    "ReadSet",
    "SharedEnvUtils",
    "SKANIUtils",
    "ThermoUtils",
    "examples",
]

__version__ = "0.1.0"
