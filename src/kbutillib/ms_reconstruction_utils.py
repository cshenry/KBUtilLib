"""ModelSEED reconstruction utilities for building and gapfilling metabolic models.

This module provides utilities for metabolic model reconstruction and gapfilling,
translating the functionality from KB-ModelSEEDReconstruction/modelseedrecon.py
into a KBUtilLib-compatible format with standard named arguments.
"""

import logging
import os
import pandas as pd
from typing import Any, Dict, List, Optional, Union

from .kb_model_utils import KBModelUtils

logger = logging.getLogger(__name__)

class MSReconstructionUtils(KBModelUtils):
    """Utilities for metabolic model reconstruction and gapfilling.

    Provides methods for building models from genomes, running gapfilling,
    and related metabolic modeling operations. This class translates the
    functionality from ModelSEEDRecon into a KBUtilLib-compatible interface
    with standard named arguments instead of params dictionaries.
    """

    def __init__(self, **kwargs: Any) -> None:
        """Initialize ModelSEED reconstruction utilities.

        Args:
            **kwargs: Additional keyword arguments passed to KBModelUtils
        """
        super().__init__(**kwargs)
        self._reconstruction_imports()
        self.FBAModel = None
        self.version = "0.1.0.msrecon"
        self.native_ontology = False

    @property
    def module_dir(self) -> str:
        """Get the KBUtilLib root directory path.

        Returns:
            Path to KBUtilLib root directory (parent of src/kbutillib)
        """
        src_dir = os.path.dirname(os.path.abspath(__file__))
        return os.path.dirname(os.path.dirname(src_dir))

    @property
    def modelseedpy_data_dir(self) -> str:
        """Get the ModelSEEDpy data directory path.

        Returns:
            Path to ModelSEEDpy data directory
        """
        import modelseedpy
        return os.path.join(os.path.dirname(modelseedpy.__file__), "data")

    def _reconstruction_imports(self) -> None:
        """Import additional modules needed for reconstruction."""
        try:
            from modelseedpy import (
                MSBuilder,
                MSGapfill,
                MSATPCorrection,
                MSModelReport,
            )
            from modelseedpy.helpers import get_template

            self.MSBuilder = MSBuilder
            self.MSGapfill = MSGapfill
            self.MSATPCorrection = MSATPCorrection
            self.MSModelReport = MSModelReport
            self.get_template_helper = get_template

        except ImportError as e:
            self.log_error(f"Failed to import reconstruction modules: {e}")
            raise
    
    def _kbase_imports(self) -> None:
        """Import additional modules needed for kbase-related operations."""
        try:
            from cobrakbase.core.kbasefba import FBAModel
            self.FBAModel = FBAModel

        except ImportError as e:
            self.log_error(f"Failed to import kbase-related modules: {e}")
            raise

    def _add_reactions_from_gene_mapping(
        self,
        mdlutl: Any,
        builder: Any,
        reaction_gene_hash: Dict[str, List[str]],
        template: Any,
    ) -> List[Any]:
        """Add reactions to model based on reaction-to-gene mapping.

        Pulls reaction definitions from the template or ModelSEED database
        and adds them to the model with the specified gene associations.

        Args:
            mdlutl: MSModelUtil object for the model
            builder: MSBuilder object for creating reactions
            reaction_gene_hash: Dictionary mapping reaction IDs to lists of gene IDs
            template: Template object for the model

        Returns:
            List of reactions that were added to the model
        """
        SBO_ANNOTATION = "sbo"
        modelseeddb = self.biochem_db()
        reactions_added = []

        for rxn_id, gene_ids in reaction_gene_hash.items():
            if rxn_id + "_c0" not in mdlutl.model.reactions:
                reaction = None
                template_reaction = None

                # Try to get reaction from template first
                if rxn_id + "_c" in template.reactions:
                    template_reaction = template.reactions.get_by_id(rxn_id + "_c")
                # Fall back to ModelSEED database
                elif rxn_id in modelseeddb.reactions:
                    rxnobj = modelseeddb.reactions.get_by_id(rxn_id)
                    if "MI" not in rxnobj.status and "CI" not in rxnobj.status:
                        template_reaction = rxnobj.to_template_reaction({0: "c", 1: "e"})

                if template_reaction:
                    # Add metabolites if needed
                    for m in template_reaction.metabolites:
                        if m.compartment not in builder.compartments:
                            builder.compartments[m.compartment] = (
                                builder.template.compartments.get_by_id(m.compartment)
                            )
                        if m.id not in builder.template_species_to_model_species:
                            model_metabolite = m.to_metabolite(builder.index)
                            builder.template_species_to_model_species[m.id] = model_metabolite
                            builder.base_model.add_metabolites([model_metabolite])

                    # Create the reaction
                    reaction = template_reaction.to_reaction(builder.base_model, builder.index)

                    # Build GPR from gene IDs
                    gpr = " or ".join(gene_ids)
                    reaction.gene_reaction_rule = gpr
                    reaction.annotation[SBO_ANNOTATION] = "SBO:0000176"

                    mdlutl.model.add_reactions([reaction])
                    reactions_added.append(reaction)
                else:
                    print(f"Reaction {rxn_id} not found in template or database!")
            else:
                # Update existing reaction with additional genes
                rxn = mdlutl.model.reactions.get_by_id(rxn_id + "_c0")
                existing_gpr = rxn.gene_reaction_rule
                for gene_id in gene_ids:
                    if gene_id not in existing_gpr:
                        if existing_gpr:
                            existing_gpr += " or " + gene_id
                        else:
                            existing_gpr = gene_id
                rxn.gene_reaction_rule = existing_gpr

        return reactions_added

    def build_metabolic_model(
        self,
        genome: Any,
        genome_classifier: Any,
        base_model: Optional[Any] = None,
        model_id: Optional[str] = None,
        model_name: Optional[str] = None,
        gs_template: str = "auto",
        core_template: Optional[Any] = None,
        gs_template_obj: Optional[Any] = None,
        atp_safe: bool = True,
        forced_atp_list: List[str] = None,
        atp_medias: List[str] = None,
        load_default_medias: bool = True,
        max_gapfilling: int = 10,
        gapfilling_delta: float = 0,
        remove_noncore_genome_reactions: bool = False,
        reactions_to_add: Optional[Dict[str, List[str]]] = None,
    ) -> tuple:
        """Build a single metabolic model from an MSGenome object.

        This is a generalized function that builds a metabolic model without
        KBase-specific dependencies. It accepts an MSGenome directly rather
        than workspace references.

        Args:
            genome: MSGenome object to build model from
            genome_classifier: Classifier object for determining template type
            base_model: Optional base FBAModel to build upon
            model_id: ID for the model (defaults to genome.id)
            model_name: Name for the model (defaults to genome.scientific_name)
            gs_template: Gram-specific template type ("auto", "gp", "gn", "ar", etc.)
            core_template: Core template object (loaded template, not reference)
            gs_template_obj: GS template object (loaded template, not reference)
            atp_safe: Whether to apply ATP-safe constraints
            forced_atp_list: List of media IDs where ATP production should be forced
            atp_medias: List of ATP media references
            load_default_medias: Whether to load default ATP medias
            max_gapfilling: Maximum number of gapfilling iterations
            gapfilling_delta: Delta threshold for gapfilling
            remove_noncore_genome_reactions: If True, remove non-core genome reactions
                (excludes bio, DM_, EX_, SK_ reactions)
            reactions_to_add: Dictionary mapping reaction IDs to lists of gene IDs
                to add to the model. Reactions are pulled from template or ModelSEED DB.

        Returns:
            Tuple of (current_output dict, MSModelUtil object) or (current_output dict, None) if skipped
        """
        # Set defaults for mutable arguments
        if forced_atp_list is None:
            forced_atp_list = []
        if atp_medias is None:
            atp_medias = []
        if reactions_to_add is None:
            reactions_to_add = {}

        # Load core template if not provided
        if core_template is None:
            core_template = self.get_template(self.templates["core"], None)

        # Use local variable for GS template
        active_gs_template = gs_template_obj

        # Initialize output row
        current_output = {
            "Model": None,
            "Genome": None,
            "Genes": None,
            "Class": None,
            "Model genes": None,
            "Reactions": None,
            "Core GF": None,
            "GS GF": None,
            "Growth": None,
            "Comments": [],
        }

        # Set genome info
        gid = model_id if model_id else genome.id
        current_output["Genome"] = genome.id
        current_output["Genes"] = len(genome.features) if hasattr(genome, 'features') else 0

        template_type = gs_template

        # Classify genome if template is auto
        if template_type == "auto":
            current_output["Class"] = genome_classifier.classify(genome)
            print(current_output["Class"])
            if current_output["Class"] == "P":
                current_output["Class"] = "Gram Positive"
                template_type = "gp"
            elif current_output["Class"] == "N" or current_output["Class"] == "--":
                current_output["Class"] = "Gram Negative"
                template_type = "gn"
            elif current_output["Class"] == "A":
                current_output["Class"] = "Archaea"
                template_type = "ar"
            elif current_output["Class"] == "C":
                current_output["Class"] = "Cyanobacteria"
                template_type = "cyano"
                current_output["Comments"].append(
                    "Cyanobacteria not yet supported. Skipping genome."
                )
                return current_output, None
            else:
                current_output["Comments"].append(
                    f"Unrecognized genome class {current_output['Class']}. Skipping genome."
                )
                return current_output, None

        # Load GS template if not provided
        if active_gs_template is None:
            active_gs_template = self.get_template(self.templates[template_type], None)

        # Create base model if not provided
        if base_model is None:
            import cobra
            base_model = cobra.Model(
                id_or_model=gid,
                name=model_name if model_name else genome.scientific_name
            )

        # Build model
        builder = self.MSBuilder(genome, active_gs_template)
        mdl = builder.build(base_model, "0", False, False)
        mdl.genome = genome
        mdl.template = active_gs_template
        mdl.core_template_ref = str(core_template.info)
        mdl.template_ref = str(active_gs_template.info)
        current_output["Core GF"] = "NA"

        mdlutl = self.MSModelUtil.get(mdl)

        # Remove non-core genome reactions if requested
        if remove_noncore_genome_reactions:
            rxns_to_remove = []
            for rxn in mdlutl.model.reactions:
                if (
                    not mdlutl.is_core(rxn)
                    and rxn.id[0:3] != "bio"
                    and rxn.id[0:3] != "DM_"
                    and rxn.id[0:3] != "EX_"
                    and rxn.id[0:3] != "SK_"
                ):
                    rxns_to_remove.append(rxn.id)
            if rxns_to_remove:
                mdl.remove_reactions(rxns_to_remove)

        # Add reactions from reaction-to-gene mapping
        if reactions_to_add:
            self._add_reactions_from_gene_mapping(
                mdlutl, builder, reactions_to_add, active_gs_template
            )

        # Run ATP correction
        if atp_safe:
            atpcorrection = self.MSATPCorrection(
                mdlutl,
                core_template,
                atp_medias,
                load_default_medias=load_default_medias,
                max_gapfilling=max_gapfilling,
                gapfilling_delta=gapfilling_delta,
                forced_media=forced_atp_list,
                default_media_path=self.modelseedpy_data_dir + "/atp_medias.tsv",
            )
            tests = atpcorrection.run_atp_correction()
            current_output["Core GF"] = len(atpcorrection.cumulative_core_gapfilling)

        # Set model attributes
        mdlutl.get_attributes()["class"] = current_output["Class"]
        mdlutl.wsid = gid

        current_output["Reactions"] = mdlutl.nonexchange_reaction_count()
        current_output["Model genes"] = len(mdlutl.model.genes)

        return current_output, mdlutl

    def compute_ontology_model_changes(
        self,
        anno_ont: Any,
        annotation_priority: List[str] = None,
        ontology_events: Optional[List] = None,
        merge_annotations: bool = False,
    ) -> Dict[str, List[str]]:
        """Compute reaction-to-gene mapping from ontology annotations.

        This function analyzes annotation ontology to determine which reactions
        should be added to a model based on gene annotations.

        Args:
            anno_ont: Annotation ontology object (genome.annoont)
            annotation_priority: List of annotation priorities (e.g., ["RAST", "KEGG"])
            ontology_events: Specific ontology events to use for extension
            merge_annotations: Whether to merge annotations from multiple sources

        Returns:
            Dictionary mapping reaction IDs to lists of gene IDs
        """
        if annotation_priority is None:
            annotation_priority = []

        reaction_gene_hash = {}

        # Get ontology events if not provided
        if ontology_events is None and len(annotation_priority) > 0:
            ontology_events = anno_ont.get_events_from_priority_list(
                annotation_priority
            )

        # Compute reaction-to-gene mapping from ontology
        if ontology_events is not None or len(annotation_priority) > 0:
            gene_term_hash = anno_ont.get_gene_term_hash(
                ontology_events, None, merge_annotations, False
            )
            self.print_json_debug_file("gene_term_hash", gene_term_hash)

            for gene in gene_term_hash:
                for term in gene_term_hash[gene]:
                    if term.ontology.id != "SSO":
                        for rxn_id in term.msrxns:
                            if rxn_id not in reaction_gene_hash:
                                reaction_gene_hash[rxn_id] = []
                            if gene.id not in reaction_gene_hash[rxn_id]:
                                reaction_gene_hash[rxn_id].append(gene.id)

        return reaction_gene_hash

    def kb_build_metabolic_models(
        self,
        workspace: str,
        genome_refs: List[str] = None,
        run_gapfilling: bool = False,
        atp_safe: bool = True,
        forced_atp_list: List[str] = None,
        gapfilling_media_list: List[str] = None,
        suffix: str = ".mdl",
        core_template: str = "auto",
        gs_template: str = "auto",
        gs_template_ref: Optional[Union[str, Any]] = None,
        core_template_ref: Optional[Union[str, Any]] = None,
        template_reactions_only: bool = True,
        output_core_models: bool = False,
        automated_atp_evaluation: bool = True,
        atp_medias: List[str] = None,
        load_default_medias: bool = True,
        max_gapfilling: int = 10,
        gapfilling_delta: float = 0,
        return_model_objects: bool = False,
        return_data: bool = False,
        save_report_to_kbase: bool = True,
        change_to_complete: bool = False,
        gapfilling_mode: str = "Sequential",
        base_media: Optional[Any] = None,
        compound_list: Optional[List[str]] = None,
        base_media_target_element: str = "C",
        expression_refs: Optional[List[str]] = None,
        extend_model_with_ontology: bool = False,
        ontology_events: Optional[List] = None,
        save_models_to_kbase: bool = True,
        save_gapfilling_fba_to_kbase: bool = True,
        annotation_priority: List[str] = None,
        merge_annotations: bool = False,
    ) -> Dict:
        """Build metabolic models from genome references.

        Args:
            workspace: KBase workspace ID or name for output
            genome_refs: List of genome references to build models from
            run_gapfilling: Whether to run gapfilling after model construction
            atp_safe: Whether to apply ATP-safe constraints
            forced_atp_list: List of media IDs where ATP production should be forced
            gapfilling_media_list: List of media references for gapfilling
            suffix: Suffix to add to model IDs
            core_template: Core template type ("auto", "gp", "gn", "ar", etc.)
            gs_template: Gram-specific template type ("auto", "gp", "gn", "ar", etc.)
            gs_template_ref: Direct reference to GS template (workspace ref or object)
            core_template_ref: Direct reference to core template (workspace ref or object)
            template_reactions_only: Only include reactions from template
            output_core_models: Whether to output core models
            automated_atp_evaluation: Whether to run automated ATP evaluation
            atp_medias: List of ATP media references
            load_default_medias: Whether to load default ATP medias
            max_gapfilling: Maximum number of gapfilling iterations
            gapfilling_delta: Delta threshold for gapfilling
            return_model_objects: Whether to return model objects in output
            return_data: Whether to return data table in output
            save_report_to_kbase: Whether to save HTML report to KBase
            change_to_complete: Whether to change default media to complete
            gapfilling_mode: Gapfilling mode ("Sequential" or "Cumulative")
            base_media: Base media object for gapfilling
            compound_list: List of compounds for media supplementation
            base_media_target_element: Target element for base media
            expression_refs: Expression data references
            extend_model_with_ontology: Whether to extend model with ontology
            ontology_events: Ontology events for model extension
            save_models_to_kbase: Whether to save models to KBase workspace
            save_gapfilling_fba_to_kbase: Whether to save gapfilling FBA to KBase
            annotation_priority: List of annotation priorities
            merge_annotations: Whether to merge annotations

        Returns:
            Dictionary with report information and optionally model objects/data
        """
        if self.FBAModel is None:
            self._kbase_imports()
        # Set defaults for mutable arguments
        if genome_refs is None:
            genome_refs = []
        if forced_atp_list is None:
            forced_atp_list = []
        if atp_medias is None:
            atp_medias = []
        if annotation_priority is None:
            annotation_priority = []

        default_media = "KBaseMedia/AuxoMedia"
        if change_to_complete:
            default_media = "KBaseMedia/Complete"

        # Process genome list
        genome_refs = self.process_genome_list(genome_refs, workspace)

        # Process media list
        gapfilling_media_objs = self.process_media_list(
            gapfilling_media_list, default_media, workspace
        )

        # Load templates (using local variables, not class state)
        loaded_gs_template = None
        if gs_template_ref is not None and not isinstance(gs_template_ref, str):
            # User passed the GS template object directly
            loaded_gs_template = gs_template_ref
        elif gs_template_ref:
            # User passed a workspace reference
            loaded_gs_template = self.get_template(gs_template_ref, None)

        loaded_core_template = None
        if core_template_ref is not None and not isinstance(core_template_ref, str):
            # User passed the core template object directly
            loaded_core_template = core_template_ref
        elif core_template_ref:
            # User passed a workspace reference
            loaded_core_template = self.get_template(core_template_ref, None)
        else:
            # Load default core template
            loaded_core_template = self.get_template(self.templates["core"], None)

        # Initialize classifier
        genome_classifier = self.get_classifier()

        # Initialize output data table
        result_table = pd.DataFrame({})

        # Process each genome
        mdllist = []
        for i, gen_ref in enumerate(genome_refs):
            # Get genome with annotation ontology
            genome = self.get_msgenome_from_ontology(
                gen_ref, native_python_api=self.native_ontology, output_ws=workspace
            )

            gid = genome.id
            base_model = self.FBAModel(
                {"id": gid, "name": genome.scientific_name}
            )

            # Compute ontology-based reaction additions if requested (KBase-specific)
            reactions_to_add = {}
            remove_noncore = False
            if extend_model_with_ontology or len(annotation_priority) > 0:
                reactions_to_add = self.compute_ontology_model_changes(
                    anno_ont=genome.annoont,
                    annotation_priority=annotation_priority,
                    ontology_events=ontology_events,
                    merge_annotations=merge_annotations,
                )
                # Remove non-core reactions if RAST not in priority
                if "RAST" not in annotation_priority and "all" not in annotation_priority:
                    remove_noncore = True

            # Call the generalized build_metabolic_model function
            current_output, mdlutl = self.build_metabolic_model(
                genome=genome,
                genome_classifier=genome_classifier,
                base_model=base_model,
                model_id=gid + suffix,
                model_name=genome.scientific_name,
                gs_template=gs_template,
                core_template=loaded_core_template,
                gs_template_obj=loaded_gs_template,
                atp_safe=atp_safe,
                forced_atp_list=forced_atp_list,
                atp_medias=atp_medias,
                load_default_medias=load_default_medias,
                max_gapfilling=max_gapfilling,
                gapfilling_delta=gapfilling_delta,
                remove_noncore_genome_reactions=remove_noncore,
                reactions_to_add=reactions_to_add,
            )

            # Add comment about ontology changes if applicable
            if mdlutl is not None and (extend_model_with_ontology or len(annotation_priority) > 0):
                current_output["Comments"].append(
                    f"Extended model with ontology: added {len(reactions_to_add)} reaction mappings."
                )

            # Add KBase-specific output formatting
            current_output["Model"] = (
                f'{gid}{suffix}<br><a href="{gid}{suffix}-recon.html" target="_blank">'
                f'(see reconstruction report)</a><br><a href="{gid}{suffix}-full.html" '
                f'target="_blank">(see full view)</a>'
            )
            current_output["Genome"] = genome.annoont.info[10]["Name"]
            current_output["Genes"] = genome.annoont.info[10][
                "Number of Protein Encoding Genes"
            ]

            # Skip if model was not built (e.g., unsupported genome class)
            if mdlutl is None:
                result_table = pd.concat(
                    [result_table, pd.DataFrame([current_output])], ignore_index=True
                )
                continue

            # Add KBase-specific references
            mdlutl.model.genome_ref = self.wsinfo_to_ref(genome.annoont.info)
            mdlutl.wsid = gid + suffix

            # Get the GS template from the model (build_metabolic_model may have loaded it)
            model_gs_template = mdlutl.model.template

            mdlutl.save_model("base_model.json")
            genome_objs = {mdlutl: genome}

            # Handle expression data
            expression_objs = None
            if expression_refs:
                expression_objs = self.get_expression_objs(expression_refs, genome_objs)

            # Run gapfilling if requested
            current_output["GS GF"] = "NA"
            if run_gapfilling:
                self.kb_gapfill_metabolic_models(
                    workspace=workspace,
                    media_objs=gapfilling_media_objs,
                    model_objs=[mdlutl],
                    genome_objs=genome_objs,
                    expression_objs=expression_objs,
                    atp_safe=atp_safe,
                    suffix="",
                    default_objective="bio1",
                    output_data={mdlutl: current_output},
                    forced_atp_list=forced_atp_list,
                    templates=[model_gs_template],
                    internal_call=True,
                    gapfilling_mode=gapfilling_mode,
                    base_media=base_media,
                    compound_list=compound_list,
                    base_media_target_element=base_media_target_element,
                    save_models_to_kbase=save_models_to_kbase,
                    save_gapfilling_fba_to_kbase=save_gapfilling_fba_to_kbase,
                )
            else:
                if save_models_to_kbase:
                    self.save_model(mdlutl, workspace, None)
                mdlutl.model.objective = "bio1"
                mdlutl.pkgmgr.getpkg("KBaseMediaPkg").build_package(None)
                current_output["Growth"] = "Complete:" + str(mdlutl.model.slim_optimize())

            current_output["Reactions"] = mdlutl.nonexchange_reaction_count()
            current_output["Model genes"] = len(mdlutl.model.genes)

            result_table = pd.concat(
                [result_table, pd.DataFrame([current_output])], ignore_index=True
            )
            mdllist.append(mdlutl)

        # Build report
        output = {}
        self._build_dataframe_report(result_table, mdllist)

        if save_report_to_kbase:
            output = self.save_report_to_kbase()

        if return_data:
            output["data"] = result_table.to_json()

        if return_model_objects:
            output["model_objs"] = mdllist

        return output

    def gapfill_metabolic_model(
        self,
        mdlutl: Any,
        genome: Any,
        media_objs: List[Any],
        templates: List[Any],
        core_template: Optional[Any] = None,
        source_models: Optional[List[Any]] = None,
        additional_tests: Optional[List[Dict]] = None,
        expression_obj: Optional[Any] = None,
        atp_safe: bool = True,
        reaction_exclusion_list: Optional[List[str]] = None,
        objective: str = "bio1",
        minimum_objective: float = 0.01,
        gapfilling_mode: str = "Sequential",
        base_media: Optional[Any] = None,
        base_media_target_element: str = "C",
        reaction_scores: Optional[Dict[str, float]] = {},
    ) -> tuple:
        """Gapfill a single metabolic model to enable growth on specified media.

        This is a generalized function that gapfills a metabolic model without
        KBase-specific dependencies. It accepts pre-loaded objects rather than
        workspace references.

        Args:
            mdlutl: MSModelUtil object to gapfill
            genome: MSGenome object associated with the model
            media_objs: List of media objects for gapfilling
            templates: List of template objects for gapfilling
            core_template: Core template object (for ATP tests)
            source_models: Source models for reaction candidates
            additional_tests: Additional constraint tests for gapfilling
            expression_obj: Expression data object for this model
            atp_safe: Whether to apply ATP-safe constraints
            reaction_exclusion_list: Reactions to exclude from gapfilling
            objective: Objective function to optimize
            minimum_objective: Minimum objective value for gapfilling
            gapfilling_mode: Gapfilling mode ("Sequential" or "Cumulative")
            base_media: Base media object for gapfilling
            base_media_target_element: Target element for base media

        Returns:
            Tuple of (current_output dict, solutions dict, output_solution, output_solution_media)
        """
        import cobra

        # Set defaults for mutable arguments
        if source_models is None:
            source_models = []
        if additional_tests is None:
            additional_tests = []
        if reaction_exclusion_list is None:
            reaction_exclusion_list = []

        # Load core template if not provided
        if core_template is None:
            core_template = self.get_template(self.templates["core"], None)

        # Initialize output
        current_output = {
            "Model": None,
            "Genome": None,
            "Genes": None,
            "Class": None,
            "Model genes": None,
            "Reactions": None,
            "Core GF": None,
            "GS GF": None,
            "Growth": None,
            "Comments": [],
        }

        # Compute tests for ATP safe gapfilling
        atp_tests = []
        if atp_safe:
            atp_tests = mdlutl.get_atp_tests(
                core_template=core_template,
                atp_media_filename=self.modelseedpy_data_dir + "/atp_medias.tsv",
                recompute=False,
            )
            print("Tests:", atp_tests)

        all_tests = additional_tests + atp_tests

        # Create gapfilling object
        msgapfill = self.MSGapfill(
            mdlutl,
            templates,
            source_models,
            all_tests,
            blacklist=reaction_exclusion_list,
            default_target=objective,
            minimum_obj=minimum_objective,
            base_media=base_media,
            base_media_target_element=base_media_target_element,
        )

        # Set reaction scores from genome
        msgapfill.reaction_scores = reaction_scores

        # Handle expression data
        if expression_obj:
            expression_scores = msgapfill.compute_reaction_weights_from_expression_data(
                expression_obj, genome.annoont
            )
            for rxn_id in msgapfill.reaction_scores:
                for gene in msgapfill.reaction_scores[rxn_id]:
                    if gene in expression_scores:
                        msgapfill.reaction_scores[rxn_id][gene]["probability"] = (
                            expression_scores[gene] + 0.5
                        )

        # Run gapfilling in all conditions
        mdlutl.gfutl.cumulative_gapfilling = []
        growth_array = []
        solutions = msgapfill.run_multi_gapfill(
            media_objs,
            target=objective,
            default_minimum_objective=minimum_objective,
            binary_check=False,
            prefilter=True,
            check_for_growth=True,
            gapfilling_mode=gapfilling_mode,
            run_sensitivity_analysis=True,
            integrate_solutions=True,
        )

        output_solution = None
        output_solution_media = None
        for media in media_objs:
            if media in solutions and "growth" in solutions[media]:
                growth_array.append(media.id + ":" + str(solutions[media]["growth"]))
                if solutions[media]["growth"] > 0 and output_solution is None:
                    mdlutl.pkgmgr.getpkg("KBaseMediaPkg").build_package(media)
                    mdlutl.pkgmgr.getpkg("ElementUptakePkg").build_package({"C": 60})
                    output_solution = cobra.flux_analysis.pfba(mdlutl.model)
                    output_solution_media = media

        solution_rxn_types = ["new", "reversed"]
        if output_solution and output_solution_media in solutions:
            gfsolution = solutions[output_solution_media]
            for rxn_type in solution_rxn_types:
                for rxn_id in gfsolution[rxn_type]:
                    if gfsolution[rxn_type][rxn_id] == ">":
                        output_solution.fluxes[rxn_id] = 1000
                    else:
                        output_solution.fluxes[rxn_id] = -1000

        current_output["Growth"] = "<br>".join(growth_array)
        current_output["GS GF"] = len(mdlutl.gfutl.cumulative_gapfilling)
        current_output["Reactions"] = mdlutl.nonexchange_reaction_count()
        current_output["Model genes"] = len(mdlutl.model.genes)

        return current_output, solutions, output_solution, output_solution_media

    def kb_gapfill_metabolic_models(
        self,
        workspace: str,
        media_list: Optional[List[str]] = None,
        media_objs: Optional[List[Any]] = None,
        genome_objs: Optional[Dict] = None,
        expression_refs: Optional[List[str]] = None,
        expression_objs: Optional[Dict] = None,
        model_list: Optional[List[str]] = None,
        model_objectives: Optional[List[str]] = None,
        model_objs: Optional[List[Any]] = None,
        atp_safe: bool = True,
        suffix: str = ".gf",
        forced_atp_list: Optional[List[str]] = None,
        templates: Optional[List[Any]] = None,
        core_template_ref: Optional[str] = None,
        source_models: Optional[List[Any]] = None,
        limit_medias: Optional[List[str]] = None,
        limit_objectives: Optional[List[str]] = None,
        limit_thresholds: Optional[List[float]] = None,
        is_max_limits: Optional[List[bool]] = None,
        minimum_objective: float = 0.01,
        reaction_exclusion_list: Optional[List[str]] = None,
        default_objective: str = "bio1",
        kbmodel_hash: Optional[Dict] = None,
        output_data: Optional[Dict] = None,
        internal_call: bool = False,
        atp_medias: Optional[List[str]] = None,
        load_default_atp_medias: bool = True,
        max_atp_gapfilling: int = 0,
        gapfilling_delta: float = 0,
        return_model_objects: bool = False,
        return_data: bool = False,
        save_report_to_kbase: bool = True,
        change_to_complete: bool = False,
        gapfilling_mode: str = "Sequential",
        base_media: Optional[Any] = None,
        compound_list: Optional[List[str]] = None,
        base_media_target_element: str = "C",
        save_models_to_kbase: bool = True,
        save_gapfilling_fba_to_kbase: bool = True,
    ) -> Dict:
        """Gapfill metabolic models to enable growth on specified media.

        This is a KBase-specific wrapper that handles workspace references
        and KBase-specific saving operations, delegating core gapfilling
        to gapfill_metabolic_model.

        Args:
            workspace: KBase workspace ID or name for output
            media_list: List of media references for gapfilling
            media_objs: Pre-loaded media objects (alternative to media_list)
            genome_objs: Dictionary mapping model utilities to genome objects
            expression_refs: Expression data references
            expression_objs: Pre-loaded expression objects
            model_list: List of model references to gapfill
            model_objectives: Objectives for each model
            model_objs: Pre-loaded model utility objects
            atp_safe: Whether to apply ATP-safe constraints
            suffix: Suffix to add to gapfilled model IDs
            forced_atp_list: List of media IDs where ATP production should be forced
            templates: List of templates for gapfilling
            core_template_ref: Reference to core template
            source_models: Source models for reaction candidates
            limit_medias: Media for additional growth constraints
            limit_objectives: Objectives for limit constraints
            limit_thresholds: Threshold values for limits
            is_max_limits: Whether limits are maximum constraints
            minimum_objective: Minimum objective value for gapfilling
            reaction_exclusion_list: Reactions to exclude from gapfilling
            default_objective: Default objective function
            kbmodel_hash: Hash of KBase model data
            output_data: Pre-initialized output data (for internal calls)
            internal_call: Whether this is an internal call from build_metabolic_models
            atp_medias: List of ATP media references
            load_default_atp_medias: Whether to load default ATP medias
            max_atp_gapfilling: Maximum ATP gapfilling iterations
            gapfilling_delta: Delta threshold for gapfilling
            return_model_objects: Whether to return model objects in output
            return_data: Whether to return data table in output
            save_report_to_kbase: Whether to save HTML report to KBase
            change_to_complete: Whether to change default media to complete
            gapfilling_mode: Gapfilling mode ("Sequential" or "Cumulative")
            base_media: Base media object for gapfilling
            compound_list: List of compounds for media supplementation
            base_media_target_element: Target element for base media
            save_models_to_kbase: Whether to save models to KBase workspace
            save_gapfilling_fba_to_kbase: Whether to save gapfilling FBA to KBase

        Returns:
            Dictionary with report information and optionally model objects/data
        """
        if self.FBAModel is None:
            self._kbase_imports()
        # Set defaults for mutable arguments
        if model_objectives is None:
            model_objectives = []
        if model_objs is None:
            model_objs = []
        if forced_atp_list is None:
            forced_atp_list = []
        if source_models is None:
            source_models = []
        if limit_medias is None:
            limit_medias = []
        if limit_objectives is None:
            limit_objectives = []
        if limit_thresholds is None:
            limit_thresholds = []
        if is_max_limits is None:
            is_max_limits = []
        if reaction_exclusion_list is None:
            reaction_exclusion_list = []
        if kbmodel_hash is None:
            kbmodel_hash = {}
        if atp_medias is None:
            atp_medias = []

        default_media = "KBaseMedia/AuxoMedia"
        base_comments = []
        if change_to_complete:
            base_comments.append("Changing default to complete.")
            default_media = "KBaseMedia/Complete"

        result_table = pd.DataFrame({})

        # Retrieve models if not provided
        if not model_objs or len(model_objs) == 0:
            model_objs = []
            if model_list:
                for mdl_ref in model_list:
                    model_objs.append(self.get_model(mdl_ref))

        # Retrieve genomes if not provided
        if not genome_objs:
            genome_objs = {}
            for mdl in model_objs:
                genome_objs[mdl] = self.get_msgenome_from_ontology(
                    mdl.model.genome_ref,
                    native_python_api=self.native_ontology,
                    output_ws=workspace,
                )

        # Retrieve expression data if not provided
        if not expression_objs and expression_refs:
            expression_objs = self.get_expression_objs(expression_refs, genome_objs)

        # Process media
        if not media_objs:
            media_objs = self.process_media_list(media_list, default_media, workspace)

        # Process compound list
        if compound_list:
            if not base_media:
                base_comments.append("No base media provided. Ignoring compound list.")
            else:
                for cpd in compound_list:
                    newmedia = self.MSMedia.from_dict({cpd: 100})
                    newmedia.merge(base_media)
                    media_objs.append(newmedia)

        # Compile additional tests from limit constraints
        additional_tests = []
        for i, limit_media in enumerate(limit_medias):
            additional_tests.append(
                {
                    "objective": limit_objectives[i],
                    "media": self.get_media(limit_media, None),
                    "is_max_threshold": is_max_limits[i],
                    "threshold": limit_thresholds[i],
                }
            )

        # Get core template (using local variable, not class state)
        loaded_core_template = None
        if core_template_ref:
            loaded_core_template = self.get_template(core_template_ref, None)
        else:
            loaded_core_template = self.get_template(self.templates["core"], None)

        # Iterate over each model and run gapfilling
        for i, mdlutl in enumerate(model_objs):
            # Use provided output data if available (for internal calls)
            if output_data and mdlutl in output_data:
                current_output = output_data[mdlutl]
            else:
                current_output = {
                    "Model": None,
                    "Genome": None,
                    "Genes": None,
                    "Class": None,
                    "Model genes": None,
                    "Reactions": None,
                    "Core GF": None,
                    "GS GF": None,
                    "Growth": None,
                    "Comments": base_comments.copy(),
                }

            # Add KBase-specific output formatting
            current_output["Model"] = (
                f'{mdlutl.wsid}{suffix}<br><a href="{mdlutl.wsid}{suffix}-recon.html" '
                f'target="_blank">(see reconstruction report)</a><br>'
                f'<a href="{mdlutl.wsid}{suffix}-full.html" target="_blank">(see full view)</a>'
            )

            # Set the objective
            if i < len(model_objectives):
                if not model_objectives[i]:
                    model_objectives[i] = default_objective
            else:
                model_objectives.append(default_objective)

            # Get templates for this model
            model_templates = templates
            if not model_templates:
                model_templates = [self.get_template(mdlutl.model.template_ref)]

            # Get expression object for this model
            expression_obj = None
            if expression_objs and mdlutl in expression_objs:
                expression_obj = expression_objs[mdlutl]

            reaction_scores = genome.annoont.get_reaction_gene_hash(
                feature_type="gene"
            )

            # Call the generalized gapfill_metabolic_model function
            gf_output, solutions, output_solution, output_solution_media = self.gapfill_metabolic_model(
                mdlutl=mdlutl,
                genome=genome_objs[mdlutl],
                media_objs=media_objs,
                templates=model_templates,
                core_template=loaded_core_template,
                source_models=source_models,
                additional_tests=additional_tests,
                expression_obj=expression_obj,
                atp_safe=atp_safe,
                reaction_exclusion_list=reaction_exclusion_list,
                objective=model_objectives[i],
                minimum_objective=minimum_objective,
                gapfilling_mode=gapfilling_mode,
                base_media=base_media,
                base_media_target_element=base_media_target_element,
                reaction_scores=reaction_scores
            )

            # Merge gapfill output into current_output
            current_output["Growth"] = gf_output["Growth"]
            current_output["GS GF"] = gf_output["GS GF"]
            current_output["Reactions"] = gf_output["Reactions"]
            current_output["Model genes"] = gf_output["Model genes"]

            # Save model to KBase
            if save_models_to_kbase:
                self.save_model(mdlutl, workspace, None, suffix)

            # Save FBA solution to KBase
            if save_gapfilling_fba_to_kbase and output_solution:
                self.save_solution_as_fba(
                    output_solution,
                    mdlutl,
                    output_solution_media,
                    mdlutl.wsid + ".fba",
                    workspace=workspace,
                    fbamodel_ref=str(workspace) + "/" + mdlutl.wsid,
                )

            if not internal_call:
                result_table = pd.concat(
                    [result_table, pd.DataFrame([current_output])], ignore_index=True
                )

        output = {}
        if not internal_call:
            self._build_dataframe_report(result_table, model_objs)
            if save_report_to_kbase:
                output = self.save_report_to_kbase()
            if return_data:
                output["data"] = result_table.to_json()
            if return_model_objects:
                output["model_objs"] = model_objs

        return output

    def _build_dataframe_report(
        self, table: pd.DataFrame, model_objs: Optional[List[Any]] = None
    ) -> None:
        """Build HTML report from results dataframe.

        Args:
            table: Pandas DataFrame with reconstruction/gapfilling results
            model_objs: List of model utility objects for generating model reports
        """
        import jinja2

        context = {"initial_model": table.iloc[0]["Model"] if len(table) > 0 else ""}

        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(self.module_dir + "/data/"),
            autoescape=jinja2.select_autoescape(["html", "xml"]),
        )

        try:
            html = env.get_template("ReportTemplate.html").render(context)
        except jinja2.TemplateNotFound:
            # Fallback if template not found
            html = f"<html><body><h1>Model Reconstruction Report</h1></body></html>"

        os.makedirs(self.working_dir + "/html", exist_ok=True)

        if model_objs:
            for model in model_objs:
                try:
                    msmodrep = self.MSModelReport(model)
                    msmodrep.build_report(
                        self.working_dir + "/html/" + model.wsid + "-recon.html"
                    )
                    msmodrep.build_multitab_report(
                        self.working_dir + "/html/" + model.wsid + "-full.html"
                    )
                except Exception as e:
                    self.log_warning(f"Could not generate report for {model.wsid}: {e}")

        print("Output dir:", self.working_dir + "/html/index.html")
        with open(self.working_dir + "/html/index.html", "w") as f:
            f.write(html)

        # Create data table file
        json_str = '{"data":' + table.to_json(orient="records") + "}"
        with open(self.working_dir + "/html/data.json", "w") as f:
            f.write(json_str)

    # Backward compatibility aliases
    build_metabolic_models = kb_build_metabolic_models
    gapfill_metabolic_models = kb_gapfill_metabolic_models
