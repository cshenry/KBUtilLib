"""KBUtilLib facade — lazy-loading access to all sub-utilities.

Usage::

    kbu = KBUtilLib()
    kbu.fba.run_fba(model)
    kbu.biochem.search_compounds("glucose")
    kbu.ws.get_object("12345/6/7")
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from .shared_env_utils import SharedEnvUtils

if TYPE_CHECKING:
    from .kb_ws_utils import KBWSUtilsImpl
    from .kb_callback_utils import KBCallbackUtilsImpl
    from .kb_annotation_utils import KBAnnotationUtilsImpl
    from .ms_biochem_utils import MSBiochemUtilsImpl
    from .kb_model_utils import KBModelUtilsImpl
    from .ms_fba_utils import MSFBAUtilsImpl
    from .ms_reconstruction_utils import MSReconstructionUtilsImpl
    from .ms_template_utils import MSTemplateUtilsImpl
    from .escher_utils import EscherUtilsImpl
    from .model_standardization_utils import ModelStandardizationUtilsImpl
    from .kb_genome_utils import KBGenomeUtilsImpl
    from .kb_plm_utils import KBPLMUtilsImpl
    from .bvbrc_utils import BVBRCUtilsImpl
    from .kb_reads_utils import KBReadsUtilsImpl
    from .kb_sdk_utils import KBSDKUtilsImpl
    from .argo_utils import ArgoUtilsImpl
    from .ai_curation_utils import AICurationUtilsImpl
    from .thermo_utils import ThermoUtilsImpl
    from .predictive_thermo_utils import PredictiveThermoUtilsImpl
    from .network_expansion_utils import NetworkExpansionUtilsImpl
    from .mmseqs_utils import MMSeqsUtilsImpl
    from .skani_utils import SKANIUtilsImpl
    from .kb_berdl_utils import KBBERDLUtilsImpl
    from .patric_ws_utils import PatricWSUtilsImpl
    from .kb_uniprot_utils import KBUniProtUtilsImpl
    from .rcsb_pdb_utils import RCSBPDBUtilsImpl
    from .kbase_catalog_client import CatalogClient
    from .kb_job_utils import KBJobUtils
    from .ontomap_utils import OntomapUtilsImpl

logger = logging.getLogger(__name__)


class KBUtilLib:
    """Lazy-loading facade for all KBUtilLib sub-utilities.

    Usage::

        kbu = KBUtilLib()
        kbu.fba.run_fba(model)
        kbu.biochem.search_compounds("glucose")
        kbu.ws.get_object("12345/6/7")

    All sub-utilities are instantiated lazily on first access.
    """

    def __init__(
        self,
        env: SharedEnvUtils | None = None,
        **env_kwargs: Any,
    ) -> None:
        if env is not None:
            self.env = env
        else:
            # Default to no-file-discovery mode if no kwargs given
            if not env_kwargs:
                env_kwargs = {"config_file": False, "token_file": None, "kbase_token_file": None}
            self.env = SharedEnvUtils(**env_kwargs)

        # Private backing fields for lazy properties
        self._ws = None
        self._callback = None
        self._annotation = None
        self._biochem = None
        self._model = None
        self._fba = None
        self._recon = None
        self._template = None
        self._escher = None
        self._standardize = None
        self._genome = None
        self._plm = None
        self._bvbrc = None
        self._reads = None
        self._sdk = None
        self._argo = None
        self._curation = None
        self._thermo = None
        self._predictive_thermo = None
        self._network_expansion = None
        self._mmseqs = None
        self._skani = None
        self._berdl = None
        self._patric = None
        self._uniprot = None
        self._pdb = None
        self._catalog = None
        self._jobs = None
        self._ontomap = None

    # ── sub-utility lazy properties ──────────────────────────────────

    @property
    def ws(self) -> KBWSUtilsImpl:
        if self._ws is None:
            from .kb_ws_utils import KBWSUtilsImpl
            self._ws = KBWSUtilsImpl(self.env)
        return self._ws

    @property
    def callback(self) -> KBCallbackUtilsImpl:
        if self._callback is None:
            from .kb_callback_utils import KBCallbackUtilsImpl
            self._callback = KBCallbackUtilsImpl(self.env, self.ws)
        return self._callback

    @property
    def annotation(self) -> KBAnnotationUtilsImpl:
        if self._annotation is None:
            from .kb_annotation_utils import KBAnnotationUtilsImpl
            self._annotation = KBAnnotationUtilsImpl(self.env, self.ws, self.callback)
        return self._annotation

    @property
    def biochem(self) -> MSBiochemUtilsImpl:
        if self._biochem is None:
            from .ms_biochem_utils import MSBiochemUtilsImpl
            self._biochem = MSBiochemUtilsImpl(self.env)
        return self._biochem

    @property
    def model(self) -> KBModelUtilsImpl:
        if self._model is None:
            from .kb_model_utils import KBModelUtilsImpl
            self._model = KBModelUtilsImpl(self.env, self.ws, self.annotation, self.biochem)
        return self._model

    @property
    def fba(self) -> MSFBAUtilsImpl:
        if self._fba is None:
            from .ms_fba_utils import MSFBAUtilsImpl
            self._fba = MSFBAUtilsImpl(self.env, self.model)
        return self._fba

    @property
    def recon(self) -> MSReconstructionUtilsImpl:
        if self._recon is None:
            from .ms_reconstruction_utils import MSReconstructionUtilsImpl
            self._recon = MSReconstructionUtilsImpl(self.env, self.model)
        return self._recon

    @property
    def template(self) -> MSTemplateUtilsImpl:
        if self._template is None:
            from .ms_template_utils import MSTemplateUtilsImpl
            self._template = MSTemplateUtilsImpl(self.env, self.model)
        return self._template

    @property
    def escher(self) -> EscherUtilsImpl:
        if self._escher is None:
            from .escher_utils import EscherUtilsImpl
            self._escher = EscherUtilsImpl(self.env, self.model, self.biochem)
        return self._escher

    @property
    def standardize(self) -> ModelStandardizationUtilsImpl:
        if self._standardize is None:
            from .model_standardization_utils import ModelStandardizationUtilsImpl
            self._standardize = ModelStandardizationUtilsImpl(self.env, self.biochem)
        return self._standardize

    @property
    def genome(self) -> KBGenomeUtilsImpl:
        if self._genome is None:
            from .kb_genome_utils import KBGenomeUtilsImpl
            self._genome = KBGenomeUtilsImpl(self.env, self.ws, self.jobs)
        return self._genome

    @property
    def plm(self) -> KBPLMUtilsImpl:
        if self._plm is None:
            from .kb_plm_utils import KBPLMUtilsImpl
            self._plm = KBPLMUtilsImpl(self.env, self.genome)
        return self._plm

    @property
    def bvbrc(self) -> BVBRCUtilsImpl:
        if self._bvbrc is None:
            from .bvbrc_utils import BVBRCUtilsImpl
            self._bvbrc = BVBRCUtilsImpl(self.env, self.genome, self.annotation)
        return self._bvbrc

    @property
    def reads(self) -> KBReadsUtilsImpl:
        if self._reads is None:
            from .kb_reads_utils import KBReadsUtilsImpl
            self._reads = KBReadsUtilsImpl(self.env, self.ws)
        return self._reads

    @property
    def sdk(self) -> KBSDKUtilsImpl:
        if self._sdk is None:
            from .kb_sdk_utils import KBSDKUtilsImpl
            self._sdk = KBSDKUtilsImpl(self.env, self.ws)
        return self._sdk

    @property
    def argo(self) -> ArgoUtilsImpl:
        if self._argo is None:
            from .argo_utils import ArgoUtilsImpl
            self._argo = ArgoUtilsImpl(self.env)
        return self._argo

    @property
    def curation(self) -> AICurationUtilsImpl:
        if self._curation is None:
            from .ai_curation_utils import AICurationUtilsImpl
            self._curation = AICurationUtilsImpl(self.env, self.argo)
        return self._curation

    @property
    def thermo(self) -> ThermoUtilsImpl:
        if self._thermo is None:
            from .thermo_utils import ThermoUtilsImpl
            self._thermo = ThermoUtilsImpl(self.env, self.biochem)
        return self._thermo

    @property
    def predictive_thermo(self) -> "PredictiveThermoUtilsImpl":
        """Predictive thermodynamics facade (equilibrator / dGPredictor /
        molGPK / ModelSEED backends with graceful degradation)."""
        if self._predictive_thermo is None:
            from .predictive_thermo_utils import PredictiveThermoUtilsImpl
            self._predictive_thermo = PredictiveThermoUtilsImpl(self.env, self.thermo)
        return self._predictive_thermo

    @property
    def network_expansion(self) -> "NetworkExpansionUtilsImpl":
        """Cheminformatics network-expansion facade (pickaxe / retrorules
        backends with graceful degradation)."""
        if self._network_expansion is None:
            from .network_expansion_utils import NetworkExpansionUtilsImpl
            self._network_expansion = NetworkExpansionUtilsImpl(self.env)
        return self._network_expansion

    @property
    def mmseqs(self) -> MMSeqsUtilsImpl:
        if self._mmseqs is None:
            from .mmseqs_utils import MMSeqsUtilsImpl
            self._mmseqs = MMSeqsUtilsImpl(self.env)
        return self._mmseqs

    @property
    def skani(self) -> SKANIUtilsImpl:
        if self._skani is None:
            from .skani_utils import SKANIUtilsImpl
            self._skani = SKANIUtilsImpl(self.env)
        return self._skani

    @property
    def berdl(self) -> KBBERDLUtilsImpl:
        if self._berdl is None:
            from .kb_berdl_utils import KBBERDLUtilsImpl
            self._berdl = KBBERDLUtilsImpl(self.env)
        return self._berdl

    @property
    def patric(self) -> PatricWSUtilsImpl:
        if self._patric is None:
            from .patric_ws_utils import PatricWSUtilsImpl
            self._patric = PatricWSUtilsImpl(self.env)
        return self._patric

    @property
    def uniprot(self) -> KBUniProtUtilsImpl:
        if self._uniprot is None:
            from .kb_uniprot_utils import KBUniProtUtilsImpl
            self._uniprot = KBUniProtUtilsImpl(self.env)
        return self._uniprot

    @property
    def pdb(self) -> RCSBPDBUtilsImpl:
        if self._pdb is None:
            from .rcsb_pdb_utils import RCSBPDBUtilsImpl
            self._pdb = RCSBPDBUtilsImpl(self.env)
        return self._pdb

    @property
    def catalog(self) -> CatalogClient:
        if self._catalog is None:
            from .kbase_catalog_client import CatalogClient
            from .kbase_endpoints import service_url
            self._catalog = CatalogClient(url=service_url("catalog"))
        return self._catalog

    @property
    def jobs(self) -> KBJobUtils:
        if self._jobs is None:
            from .kb_job_utils import KBJobUtils
            self._jobs = KBJobUtils(self.env)
        return self._jobs

    @property
    def ontomap(self) -> "OntomapUtilsImpl":
        if self._ontomap is None:
            from .ontomap_utils import OntomapUtilsImpl
            self._ontomap = OntomapUtilsImpl(self.env)
        return self._ontomap
