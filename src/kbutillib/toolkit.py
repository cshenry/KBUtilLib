"""KBUtilLib — transport-agnostic facade for metabolic modeling utilities.

``KBUtilLib()`` is the single entry point.  Every property lazily constructs
the corresponding domain implementation:

    from kbutillib import KBUtilLib
    k = KBUtilLib()
    results = k.biochem.search_compounds("glucose")

Domain implementations live in ``kbutillib.domains.*``; transport adapters
(CLI, MCP, HTTP API) live in ``kbutillib.interfaces.*``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .core.shared_env_utils import SharedEnvUtils

if TYPE_CHECKING:
    from .domains.ai.ai_curation_utils import AICurationUtilsImpl
    from .domains.ai.argo_utils import ArgoUtilsImpl
    from .domains.ai.kb_plm_utils import KBPLMUtilsImpl
    from .domains.biochem.ms_biochem_utils import MSBiochemUtilsImpl
    from .domains.cheminformatics.network_expansion_utils import (
        NetworkExpansionUtilsImpl,
    )
    from .domains.cheminformatics.verab.facade import VerabUtilsImpl
    from .domains.external.bvbrc_utils import BVBRCUtilsImpl
    from .domains.external.kb_uniprot_utils import KBUniProtUtilsImpl
    from .domains.external.patric_ws_utils import PatricWSUtilsImpl
    from .domains.external.rcsb_pdb_utils import RCSBPDBUtilsImpl
    from .domains.genome.kb_annotation_utils import KBAnnotationUtilsImpl
    from .domains.genome.kb_genome_utils import KBGenomeUtilsImpl
    from .domains.genome.mmseqs_utils import MMSeqsUtilsImpl
    from .domains.genome.ontomap_utils import OntomapUtilsImpl
    from .domains.genome.skani_utils import SKANIUtilsImpl
    from .domains.kbase.kb_berdl_utils import KBBERDLUtilsImpl
    from .domains.kbase.kb_callback_utils import KBCallbackUtilsImpl
    from .domains.kbase.kb_reads_utils import KBReadsUtilsImpl
    from .domains.kbase.kb_sdk_utils import KBSDKUtilsImpl
    from .domains.kbase.kb_ws_utils import KBWSUtilsImpl
    from .domains.kbase.kbase_catalog_client import CatalogClient
    from .domains.modeling.kb_model_utils import KBModelUtilsImpl
    from .domains.modeling.model_standardization_utils import (
        ModelStandardizationUtilsImpl,
    )
    from .domains.modeling.ms_fba_utils import MSFBAUtilsImpl
    from .domains.modeling.ms_reconstruction_utils import MSReconstructionUtilsImpl
    from .domains.modeling.ms_remote_solve_utils import RemoteSolveResult
    from .domains.modeling.ms_remote_solver_utils import MSRemoteSolverUtilsImpl
    from .domains.modeling.ms_template_utils import MSTemplateUtilsImpl
    from .domains.notebook.escher_utils import EscherUtilsImpl
    from .domains.thermo.predictive_thermo_utils import PredictiveThermoUtilsImpl
    from .domains.thermo.thermo_utils import ThermoUtilsImpl
    from .kb_job_utils import KBJobUtils

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
        self._mmseqs = None
        self._skani = None
        self._berdl = None
        self._remote_solver = None
        self._patric = None
        self._uniprot = None
        self._pdb = None
        self._catalog = None
        self._jobs = None
        self._ontomap = None
        self._network_expansion = None
        self._verab = None

    # ── sub-utility lazy properties ──────────────────────────────────

    @property
    def ws(self) -> KBWSUtilsImpl:
        if self._ws is None:
            from .domains.kbase.kb_ws_utils import KBWSUtilsImpl
            self._ws = KBWSUtilsImpl(self.env)
        return self._ws

    @property
    def callback(self) -> KBCallbackUtilsImpl:
        if self._callback is None:
            from .domains.kbase.kb_callback_utils import KBCallbackUtilsImpl
            self._callback = KBCallbackUtilsImpl(self.env, self.ws)
        return self._callback

    @property
    def annotation(self) -> KBAnnotationUtilsImpl:
        if self._annotation is None:
            from .domains.genome.kb_annotation_utils import KBAnnotationUtilsImpl
            self._annotation = KBAnnotationUtilsImpl(self.env, self.ws, self.callback)
        return self._annotation

    @property
    def biochem(self) -> MSBiochemUtilsImpl:
        if self._biochem is None:
            from .domains.biochem.ms_biochem_utils import MSBiochemUtilsImpl
            self._biochem = MSBiochemUtilsImpl(self.env)
        return self._biochem

    @property
    def model(self) -> KBModelUtilsImpl:
        if self._model is None:
            from .domains.modeling.kb_model_utils import KBModelUtilsImpl
            self._model = KBModelUtilsImpl(self.env, self.ws, self.annotation, self.biochem)
        return self._model

    @property
    def fba(self) -> MSFBAUtilsImpl:
        if self._fba is None:
            from .domains.modeling.ms_fba_utils import MSFBAUtilsImpl
            self._fba = MSFBAUtilsImpl(self.env, self.model)
        return self._fba

    @property
    def recon(self) -> MSReconstructionUtilsImpl:
        if self._recon is None:
            from .domains.modeling.ms_reconstruction_utils import (
                MSReconstructionUtilsImpl,
            )
            self._recon = MSReconstructionUtilsImpl(self.env, self.model)
        return self._recon

    @property
    def template(self) -> MSTemplateUtilsImpl:
        if self._template is None:
            from .domains.modeling.ms_template_utils import MSTemplateUtilsImpl
            self._template = MSTemplateUtilsImpl(self.env, self.model)
        return self._template

    @property
    def escher(self) -> EscherUtilsImpl:
        if self._escher is None:
            from .domains.notebook.escher_utils import EscherUtilsImpl
            self._escher = EscherUtilsImpl(self.env, self.model, self.biochem)
        return self._escher

    @property
    def standardize(self) -> ModelStandardizationUtilsImpl:
        if self._standardize is None:
            from .domains.modeling.model_standardization_utils import (
                ModelStandardizationUtilsImpl,
            )
            self._standardize = ModelStandardizationUtilsImpl(self.env, self.biochem)
        return self._standardize

    @property
    def genome(self) -> KBGenomeUtilsImpl:
        if self._genome is None:
            from .domains.genome.kb_genome_utils import KBGenomeUtilsImpl
            self._genome = KBGenomeUtilsImpl(self.env, self.ws, self.jobs)
        return self._genome

    @property
    def plm(self) -> KBPLMUtilsImpl:
        if self._plm is None:
            from .domains.ai.kb_plm_utils import KBPLMUtilsImpl
            self._plm = KBPLMUtilsImpl(self.env, self.genome)
        return self._plm

    @property
    def bvbrc(self) -> BVBRCUtilsImpl:
        if self._bvbrc is None:
            from .domains.external.bvbrc_utils import BVBRCUtilsImpl
            self._bvbrc = BVBRCUtilsImpl(self.env, self.genome, self.annotation)
        return self._bvbrc

    @property
    def reads(self) -> KBReadsUtilsImpl:
        if self._reads is None:
            from .domains.kbase.kb_reads_utils import KBReadsUtilsImpl
            self._reads = KBReadsUtilsImpl(self.env, self.ws)
        return self._reads

    @property
    def sdk(self) -> KBSDKUtilsImpl:
        if self._sdk is None:
            from .domains.kbase.kb_sdk_utils import KBSDKUtilsImpl
            self._sdk = KBSDKUtilsImpl(self.env, self.ws)
        return self._sdk

    @property
    def argo(self) -> ArgoUtilsImpl:
        if self._argo is None:
            from .domains.ai.argo_utils import ArgoUtilsImpl
            self._argo = ArgoUtilsImpl(self.env)
        return self._argo

    @property
    def curation(self) -> AICurationUtilsImpl:
        if self._curation is None:
            from .domains.ai.ai_curation_utils import AICurationUtilsImpl
            self._curation = AICurationUtilsImpl(self.env, self.argo)
        return self._curation

    @property
    def thermo(self) -> ThermoUtilsImpl:
        if self._thermo is None:
            from .domains.thermo.thermo_utils import ThermoUtilsImpl
            self._thermo = ThermoUtilsImpl(self.env, self.biochem)
        return self._thermo

    @property
    def predictive_thermo(self) -> "PredictiveThermoUtilsImpl":
        if self._predictive_thermo is None:
            from .domains.thermo.predictive_thermo_utils import (
                PredictiveThermoUtilsImpl,
            )
            self._predictive_thermo = PredictiveThermoUtilsImpl(self.env, self.biochem)
        return self._predictive_thermo

    @property
    def mmseqs(self) -> MMSeqsUtilsImpl:
        if self._mmseqs is None:
            from .domains.genome.mmseqs_utils import MMSeqsUtilsImpl
            self._mmseqs = MMSeqsUtilsImpl(self.env)
        return self._mmseqs

    @property
    def skani(self) -> SKANIUtilsImpl:
        if self._skani is None:
            from .domains.genome.skani_utils import SKANIUtilsImpl
            self._skani = SKANIUtilsImpl(self.env)
        return self._skani

    @property
    def berdl(self) -> KBBERDLUtilsImpl:
        if self._berdl is None:
            from .domains.kbase.kb_berdl_utils import KBBERDLUtilsImpl
            self._berdl = KBBERDLUtilsImpl(self.env)
        return self._berdl

    @property
    def remote_solver(self) -> MSRemoteSolverUtilsImpl:
        if self._remote_solver is None:
            from .domains.modeling.ms_remote_solver_utils import (
                MSRemoteSolverUtilsImpl,
            )
            self._remote_solver = MSRemoteSolverUtilsImpl(self.env)
        return self._remote_solver

    def remote_solve(
        self,
        model: Any,
        solver: str | None = None,
        time_limit: float | None = None,
    ) -> "RemoteSolveResult":
        """Serialize a built cobra/optlang ``model`` and solve it remotely.

        Thin wrapper around
        :func:`kbutillib.domains.modeling.ms_remote_solve_utils.remote_solve`,
        using this facade's ``.remote_solver`` client. See that module for
        the guard/serialize/solve/wrap details (a quadratic objective
        requires a gurobi/cplex backend; a purely linear model may use any
        LP-writing backend, including GLPK). ``.primal`` on ``model``'s own
        optlang variables is NOT populated -- read fitted values via
        ``result.value(...)``.

        Args:
            model: A cobra/optlang model whose problem has already been
                built (``model.solver.problem``).
            solver: ``"gurobi"`` / ``"cplex"`` / ``None`` (service default).
            time_limit: Solver time limit in seconds.

        Returns:
            A ``RemoteSolveResult`` wrapping the remote service's response.
        """
        from .domains.modeling.ms_remote_solve_utils import (
            remote_solve as _remote_solve,
        )

        return _remote_solve(
            self.remote_solver, model, solver=solver, time_limit=time_limit
        )

    @property
    def patric(self) -> PatricWSUtilsImpl:
        if self._patric is None:
            from .domains.external.patric_ws_utils import PatricWSUtilsImpl
            self._patric = PatricWSUtilsImpl(self.env)
        return self._patric

    @property
    def uniprot(self) -> KBUniProtUtilsImpl:
        if self._uniprot is None:
            from .domains.external.kb_uniprot_utils import KBUniProtUtilsImpl
            self._uniprot = KBUniProtUtilsImpl(self.env)
        return self._uniprot

    @property
    def pdb(self) -> RCSBPDBUtilsImpl:
        if self._pdb is None:
            from .domains.external.rcsb_pdb_utils import RCSBPDBUtilsImpl
            self._pdb = RCSBPDBUtilsImpl(self.env)
        return self._pdb

    @property
    def catalog(self) -> CatalogClient:
        if self._catalog is None:
            from .domains.kbase.kbase_catalog_client import CatalogClient
            from .domains.kbase.kbase_endpoints import service_url
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
            from .domains.genome.ontomap_utils import OntomapUtilsImpl
            self._ontomap = OntomapUtilsImpl(self.env)
        return self._ontomap

    @property
    def network_expansion(self) -> "NetworkExpansionUtilsImpl":
        """Cheminformatics network-expansion facade (pickaxe / retrorules
        backends with graceful degradation)."""
        if self._network_expansion is None:
            from .domains.cheminformatics.network_expansion_utils import (
                NetworkExpansionUtilsImpl,
            )
            self._network_expansion = NetworkExpansionUtilsImpl(self.env)
        return self._network_expansion

    @property
    def chem(self) -> "NetworkExpansionUtilsImpl":
        """Alias for :attr:`network_expansion`."""
        return self.network_expansion

    @property
    def verab(self) -> "VerabUtilsImpl":
        """verAB methoxy-aromatic Pickaxe rule-discovery and genome-screening
        facade (composes network_expansion + biochem + model + genome +
        annotation with graceful RDKit/minedatabase degradation).

        Dependencies are resolved lazily — the sub-facades are only constructed
        when a method that requires them is first called, avoiding eager
        failures when optional modules (modelseedpy, etc.) are absent."""
        if self._verab is None:
            from .domains.cheminformatics.verab.facade import VerabUtilsImpl
            # Pass getter lambdas so each sub-facade is constructed only on
            # first use (avoids eager ModuleNotFoundError for optional deps).
            self._verab = VerabUtilsImpl(
                self.env,
                network_expansion=lambda: self.network_expansion,
                biochem=lambda: self.biochem,
                model=lambda: self.model,
                genome=lambda: self.genome,
                annotation=lambda: self.annotation,
            )
        return self._verab
