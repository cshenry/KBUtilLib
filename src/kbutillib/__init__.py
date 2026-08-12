"""KBUtilLib - Modular utility framework for scientific and development projects.

Architecture: Composition over SharedEnvUtils. Each *Impl class holds a
SharedEnvUtils instance and zero or more sibling *Impl instances.
The KBUtilLib facade provides lazy-property access to all sub-utilities.
"""

import os
import sys

# Flat modules (real infrastructure files — not shims)
from .compartments import compartment_types, normalize_compartment

# Core utilities - these should always be available
from .core.base_utils import BaseUtils
from .core.shared_env_utils import SharedEnvUtils
from .domains.modeling.model_directionality import (
    biochem_directionality,
    combine_directionality_signals,
    direction_conversion,
    directionality_from_bounds,
)
from .domains.modeling.model_helpers import _check_and_convert_model, _parse_id
from .domains.modeling.ms_remote_solve_utils import RemoteSolveResult, remote_solve

# Facade
from .toolkit import KBUtilLib

# Collected optional-import failures.  Populated by _import_error(); flushed
# to stderr by _flush_import_errors() at module-load time.
_OPTIONAL_IMPORT_FAILURES: list[tuple[str, str]] = []


def _import_error(module_name: str, error: Exception) -> None:
    """Collect an optional-import failure for deferred reporting.

    When ``KBUTILLIB_VERBOSE_IMPORTS=1`` the detail line is printed immediately
    (matching the previous behaviour).  Otherwise it is queued and a single
    summary line is emitted after all optional imports have been attempted.
    """
    detail = f"[KBUtilLib] Failed to import {module_name}: {type(error).__name__}: {error}"
    if os.environ.get("KBUTILLIB_VERBOSE_IMPORTS") == "1":
        print(detail, file=sys.stderr)
    else:
        _OPTIONAL_IMPORT_FAILURES.append((module_name, detail))


def _flush_import_errors() -> None:
    """Emit a single summary line for any queued optional-import failures.

    Called once, at the end of this module's optional-import block.  Clears
    ``_OPTIONAL_IMPORT_FAILURES`` so re-imports (e.g. in tests with
    ``importlib.reload``) see a fresh slate.
    """
    if not _OPTIONAL_IMPORT_FAILURES:
        return
    names = ", ".join(m for m, _ in _OPTIONAL_IMPORT_FAILURES)
    n = len(_OPTIONAL_IMPORT_FAILURES)
    print(
        f"[KBUtilLib] {n} optional module{'s' if n != 1 else ''} unavailable: "
        f"{names} (set KBUTILLIB_VERBOSE_IMPORTS=1 for detail)",
        file=sys.stderr,
    )
    _OPTIONAL_IMPORT_FAILURES.clear()


# ── Legacy classes (inheritance-based, kept for backward compat) ────────

try:
    from .domains.kbase.kb_ws_utils import KBWSUtils
except ImportError as e:
    _import_error("kb_ws_utils", e)
    KBWSUtils = None

try:
    from .domains.genome.kb_genome_utils import KBGenomeUtils
except ImportError as e:
    _import_error("kb_genome_utils", e)
    KBGenomeUtils = None

try:
    from .domains.biochem.ms_biochem_utils import MSBiochemUtils
except ImportError as e:
    _import_error("ms_biochem_utils", e)
    MSBiochemUtils = None

try:
    from .domains.biochem.ms_reaction_similarity_utils import (
        MSReactionSimilarityUtils,
    )
except ImportError as e:
    _import_error("ms_reaction_similarity_utils", e)
    MSReactionSimilarityUtils = None

try:
    from .domains.modeling.model_standardization_utils import ModelStandardizationUtils
except ImportError as e:
    _import_error("model_standardization_utils", e)
    ModelStandardizationUtils = None

try:
    from .domains.modeling.kb_model_utils import KBModelUtils
except ImportError as e:
    _import_error("kb_model_utils", e)
    KBModelUtils = None

try:
    from .domains.modeling.ms_reconstruction_utils import MSReconstructionUtils
except ImportError as e:
    _import_error("ms_reconstruction_utils", e)
    MSReconstructionUtils = None

try:
    from .domains.modeling.ms_fba_utils import MSFBAUtils
except ImportError as e:
    _import_error("ms_fba_utils", e)
    MSFBAUtils = None

try:
    from .domains.modeling.ms_template_utils import MSTemplateUtils
except ImportError as e:
    _import_error("ms_template_utils", e)
    MSTemplateUtils = None

try:
    from .domains.kbase.kb_sdk_utils import KBSDKUtils
except ImportError as e:
    _import_error("kb_sdk_utils", e)
    KBSDKUtils = None

try:
    from .domains.ai.argo_utils import ArgoUtils
except ImportError as e:
    _import_error("argo_utils", e)
    ArgoUtils = None

try:
    from .domains.ai.ai_curation_utils import AICurationUtils
except ImportError as e:
    _import_error("ai_curation_utils", e)
    AICurationUtils = None

try:
    from .domains.notebook.escher_utils import EscherUtils
except ImportError as e:
    _import_error("escher_utils", e)
    EscherUtils = None

try:
    from .domains.genome.kb_annotation_utils import KBAnnotationUtils
except ImportError as e:
    _import_error("kb_annotation_utils", e)
    KBAnnotationUtils = None

try:
    from .domains.ai.kb_plm_utils import KBPLMUtils
except ImportError as e:
    _import_error("kb_plm_utils", e)
    KBPLMUtils = None

try:
    from .domains.external.kb_uniprot_utils import KBUniProtUtils
except ImportError as e:
    _import_error("kb_uniprot_utils", e)
    KBUniProtUtils = None

try:
    from .domains.genome.skani_utils import SKANIUtils
except ImportError as e:
    _import_error("skani_utils", e)
    SKANIUtils = None

try:
    from .domains.thermo.thermo_utils import ThermoUtils
except ImportError as e:
    _import_error("thermo_utils", e)
    ThermoUtils = None

try:
    from .domains.thermo.predictive_thermo_utils import PredictiveThermoUtils
except ImportError as e:
    _import_error("predictive_thermo_utils", e)
    PredictiveThermoUtils = None

try:
    from .domains.kbase.kb_reads_utils import (
        Assembly,
        AssemblySet,
        KBReadsUtils,
        Reads,
        ReadSet,
    )
except ImportError as e:
    _import_error("kb_reads_utils", e)
    KBReadsUtils = None
    Assembly = None
    AssemblySet = None
    Reads = None
    ReadSet = None

try:
    from .domains.external.bvbrc_utils import BVBRCUtils
except ImportError as e:
    _import_error("bvbrc_utils", e)
    BVBRCUtils = None

try:
    from .domains.external.patric_ws_utils import PatricWSUtils
except ImportError as e:
    _import_error("patric_ws_utils", e)
    PatricWSUtils = None

try:
    from .domains.external.rcsb_pdb_utils import RCSBPDBUtils
except ImportError as e:
    _import_error("rcsb_pdb_utils", e)
    RCSBPDBUtils = None

try:
    from .domains.genome.mmseqs_utils import MMSeqsUtils
except ImportError as e:
    _import_error("mmseqs_utils", e)
    MMSeqsUtils = None

try:
    from .domains.genome.annotation.annotator_utils import (
        AnnotationRecord,
        AnnotationResult,
        AnnotatorUtils,
        Term,
        ToolUnavailableError,
    )
except ImportError as e:
    _import_error("annotator_utils", e)
    AnnotationRecord = None
    AnnotationResult = None
    AnnotatorUtils = None
    Term = None
    ToolUnavailableError = None

try:
    from .domains.genome.annotation.prokka_utils import ProkkaUtils
except ImportError as e:
    _import_error("prokka_utils", e)
    ProkkaUtils = None

try:
    from .domains.genome.annotation.dram2_utils import DRAM2Utils
except ImportError as e:
    _import_error("dram2_utils", e)
    DRAM2Utils = None

try:
    from .domains.genome.annotation.transyt_utils import TransytUtils
except ImportError as e:
    _import_error("transyt_utils", e)
    TransytUtils = None

try:
    from .domains.genome.ontomap_utils import OntomapUtils
except ImportError as e:
    _import_error("ontomap_utils", e)
    OntomapUtils = None

try:
    from .domains.kbase.kb_berdl_utils import KBBERDLUtils
except ImportError as e:
    _import_error("kb_berdl_utils", e)
    KBBERDLUtils = None

try:
    from .domains.modeling.ms_remote_solver_utils import MSRemoteSolverUtils
except ImportError as e:
    _import_error("ms_remote_solver_utils", e)
    MSRemoteSolverUtils = None

try:
    from .domains.kbase.kb_callback_utils import KBCallbackUtils
except ImportError as e:
    _import_error("kb_callback_utils", e)
    KBCallbackUtils = None

try:
    from .kb_job_utils import (
        ChainStep,
        JobRecord,
        JobState,
        JobStore,
        KBJobUtils,
        PipelineState,
        PipelineStatus,
    )
except ImportError as e:
    _import_error("kb_job_utils", e)
    KBJobUtils = None
    JobRecord = None
    JobState = None
    JobStore = None
    PipelineState = None
    PipelineStatus = None
    ChainStep = None

try:
    from .domains.kbase.kbase_endpoints import (
        base_url,
        env_from_url,
        narrative_url,
        service_url,
    )
except ImportError as e:
    _import_error("kbase_endpoints", e)
    base_url = None
    service_url = None
    narrative_url = None
    env_from_url = None


# ── Composition-based *Impl classes ────────────────────────────────────

try:
    from .domains.kbase.kb_ws_utils import KBWSUtilsImpl
except ImportError:
    KBWSUtilsImpl = None

try:
    from .domains.kbase.kb_callback_utils import KBCallbackUtilsImpl
except ImportError:
    KBCallbackUtilsImpl = None

try:
    from .domains.genome.kb_annotation_utils import KBAnnotationUtilsImpl
except ImportError:
    KBAnnotationUtilsImpl = None

try:
    from .domains.biochem.ms_biochem_utils import MSBiochemUtilsImpl
except ImportError:
    MSBiochemUtilsImpl = None

try:
    from .domains.biochem.ms_reaction_similarity_utils import (
        MSReactionSimilarityUtilsImpl,
    )
except ImportError:
    MSReactionSimilarityUtilsImpl = None

try:
    from .domains.modeling.kb_model_utils import KBModelUtilsImpl
except ImportError:
    KBModelUtilsImpl = None

try:
    from .domains.modeling.ms_fba_utils import MSFBAUtilsImpl
except ImportError:
    MSFBAUtilsImpl = None

try:
    from .domains.modeling.ms_template_utils import MSTemplateUtilsImpl
except ImportError:
    MSTemplateUtilsImpl = None

try:
    from .domains.modeling.ms_reconstruction_utils import MSReconstructionUtilsImpl
except ImportError:
    MSReconstructionUtilsImpl = None

try:
    from .domains.notebook.escher_utils import EscherUtilsImpl
except ImportError:
    EscherUtilsImpl = None

try:
    from .domains.modeling.model_standardization_utils import (
        ModelStandardizationUtilsImpl,
    )
except ImportError:
    ModelStandardizationUtilsImpl = None

try:
    from .domains.genome.kb_genome_utils import KBGenomeUtilsImpl
except ImportError:
    KBGenomeUtilsImpl = None

try:
    from .domains.ai.kb_plm_utils import KBPLMUtilsImpl
except ImportError:
    KBPLMUtilsImpl = None

try:
    from .domains.external.bvbrc_utils import BVBRCUtilsImpl
except ImportError:
    BVBRCUtilsImpl = None

try:
    from .domains.kbase.kb_reads_utils import KBReadsUtilsImpl
except ImportError:
    KBReadsUtilsImpl = None

try:
    from .domains.kbase.kb_sdk_utils import KBSDKUtilsImpl
except ImportError:
    KBSDKUtilsImpl = None

try:
    from .domains.ai.argo_utils import ArgoUtilsImpl
except ImportError:
    ArgoUtilsImpl = None

try:
    from .domains.ai.ai_curation_utils import AICurationUtilsImpl
except ImportError:
    AICurationUtilsImpl = None

try:
    from .domains.thermo.thermo_utils import ThermoUtilsImpl
except ImportError:
    ThermoUtilsImpl = None

try:
    from .domains.thermo.predictive_thermo_utils import PredictiveThermoUtilsImpl
except ImportError:
    PredictiveThermoUtilsImpl = None

try:
    from .domains.genome.mmseqs_utils import MMSeqsUtilsImpl
except ImportError:
    MMSeqsUtilsImpl = None

try:
    from .domains.genome.skani_utils import SKANIUtilsImpl
except ImportError:
    SKANIUtilsImpl = None

try:
    from .domains.kbase.kb_berdl_utils import KBBERDLUtilsImpl
except ImportError:
    KBBERDLUtilsImpl = None

try:
    from .domains.modeling.ms_remote_solver_utils import MSRemoteSolverUtilsImpl
except ImportError:
    MSRemoteSolverUtilsImpl = None

try:
    from .domains.external.patric_ws_utils import PatricWSUtilsImpl
except ImportError:
    PatricWSUtilsImpl = None

try:
    from .domains.external.kb_uniprot_utils import KBUniProtUtilsImpl
except ImportError:
    KBUniProtUtilsImpl = None

try:
    from .domains.external.rcsb_pdb_utils import RCSBPDBUtilsImpl
except ImportError:
    RCSBPDBUtilsImpl = None

try:
    from .domains.genome.ontomap_utils import OntomapUtilsImpl
except ImportError:
    OntomapUtilsImpl = None


# Retired
examples = None

# Emit a single summary line for any optional-import failures collected above.
# Must be called after all optional-import try/except blocks.
_flush_import_errors()


__all__ = [
    # Facade
    "KBUtilLib",
    # Core
    "BaseUtils",
    "SharedEnvUtils",
    # Flat modules
    "compartment_types",
    "normalize_compartment",
    "direction_conversion",
    "directionality_from_bounds",
    "biochem_directionality",
    "combine_directionality_signals",
    "_parse_id",
    "_check_and_convert_model",
    "RemoteSolveResult",
    "remote_solve",
    # Legacy class names (inheritance-based)
    "AICurationUtils",
    "ArgoUtils",
    "Assembly",
    "AssemblySet",
    "BVBRCUtils",
    "ChainStep",
    "EscherUtils",
    "JobRecord",
    "JobState",
    "JobStore",
    "KBAnnotationUtils",
    "KBBERDLUtils",
    "KBCallbackUtils",
    "KBGenomeUtils",
    "KBJobUtils",
    "KBModelUtils",
    "KBPLMUtils",
    "KBReadsUtils",
    "KBSDKUtils",
    "KBUniProtUtils",
    "KBWSUtils",
    "AnnotationRecord",
    "AnnotationResult",
    "AnnotatorUtils",
    "DRAM2Utils",
    "MMSeqsUtils",
    "ProkkaUtils",
    "ModelStandardizationUtils",
    "MSBiochemUtils",
    "MSReactionSimilarityUtils",
    "MSFBAUtils",
    "MSTemplateUtils",
    "MSReconstructionUtils",
    "MSRemoteSolverUtils",
    "PatricWSUtils",
    "PipelineState",
    "PipelineStatus",
    "RCSBPDBUtils",
    "Reads",
    "ReadSet",
    "SKANIUtils",
    "Term",
    "ThermoUtils",
    "PredictiveThermoUtils",
    "ToolUnavailableError",
    "TransytUtils",
    "OntomapUtils",
    # Composition-based Impl classes
    "AICurationUtilsImpl",
    "ArgoUtilsImpl",
    "BVBRCUtilsImpl",
    "EscherUtilsImpl",
    "KBAnnotationUtilsImpl",
    "KBBERDLUtilsImpl",
    "KBCallbackUtilsImpl",
    "KBGenomeUtilsImpl",
    "KBModelUtilsImpl",
    "KBPLMUtilsImpl",
    "KBReadsUtilsImpl",
    "KBSDKUtilsImpl",
    "KBUniProtUtilsImpl",
    "KBWSUtilsImpl",
    "MMSeqsUtilsImpl",
    "ModelStandardizationUtilsImpl",
    "MSBiochemUtilsImpl",
    "MSReactionSimilarityUtilsImpl",
    "MSFBAUtilsImpl",
    "MSTemplateUtilsImpl",
    "MSReconstructionUtilsImpl",
    "MSRemoteSolverUtilsImpl",
    "PatricWSUtilsImpl",
    "RCSBPDBUtilsImpl",
    "SKANIUtilsImpl",
    "ThermoUtilsImpl",
    "PredictiveThermoUtilsImpl",
    "OntomapUtilsImpl",
    # Endpoints
    "base_url",
    "env_from_url",
    "narrative_url",
    "service_url",
]

__version__ = "0.1.0"
