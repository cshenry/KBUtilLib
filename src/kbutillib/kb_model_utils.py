"""KBase model utilities for constraint-based metabolic modeling."""

import pickle
from typing import Any, Dict
from unittest import result
import pandas as pd
import re
import json

from .kb_annotation_utils import KBAnnotationUtils
from .ms_biochem_utils import MSBiochemUtils

# TODO: One issue exists with this module: (1) if a genome isn't RAST annotated, the call to reannotate it with RAST doesn't work unless we get callbacks to work


class KBModelUtils(KBAnnotationUtils, MSBiochemUtils):
    """Utilities for working with KBase metabolic models and constraint-based modeling.

    Provides methods for model manipulation, flux balance analysis preparation,
    reaction and metabolite operations, and other metabolic modeling tasks.
    """

    def __init__(self, **kwargs: Any) -> None:
        """Initialize KBase model utilities.

        Args:
            **kwargs: Additional keyword arguments passed to SharedEnvironment
        """
        super().__init__(**kwargs)

        # Ensure required dependencies are available
        self._ensure_dependencies()

        # Import required modules after ensuring dependencies
        self._import_modules()

        # Configuring cobrakbase API
        self.kbase_api = self.cobrakbase.KBaseAPI()
        self.kbase_api.ws_client = self.ws_client()

    def _ensure_dependencies(self) -> None:
        """Ensure all required dependencies are available."""
        dependencies_ok = True

        if not self.ensure_cobra_kbase():
            self.log_error("Failed to obtain CobraKBase dependency")
            dependencies_ok = False

        if not self.ensure_modelseed_py():
            self.log_error("Failed to obtain ModelSEEDpy dependency")
            dependencies_ok = False

        if not dependencies_ok:
            raise ImportError(
                "Required dependencies for KBModelUtils are not available"
            )

    def _import_modules(self) -> None:
        """Import required modules after dependencies are ensured."""
        try:
            import json

            import cobrakbase
            from cobrakbase.core.kbasefba import FBAModel
            from cobrakbase.core.kbasefba.fbamodel_from_cobra import CobraModelConverter
            from modelseedpy.core.annotationontology import AnnotationOntology
            from modelseedpy.core.msfba import MSFBA
            from modelseedpy.core.msgenomeclassifier import MSGenomeClassifier
            from modelseedpy.core.msgrowthphenotypes import MSGrowthPhenotypes
            from modelseedpy.core.msmodelutl import MSModelUtil

            # Store modules as instance attributes for later use
            self.cobrakbase = cobrakbase
            self.MSModelUtil = MSModelUtil
            self.MSFBA = MSFBA
            self.AnnotationOntology = AnnotationOntology
            self.MSGrowthPhenotypes = MSGrowthPhenotypes
            self.MSGenomeClassifier = MSGenomeClassifier
            self.CobraModelConverter = CobraModelConverter
            self.FBAModel = FBAModel
            self.json = json

        except ImportError as e:
            self.log_error(f"Failed to import required modules: {e}")
            raise

        # Loading default templates
        self.templates = {
            "core": "NewKBaseModelTemplates/Core-V5.2",
            "gp": "NewKBaseModelTemplates/GramPosModelTemplateV6",
            "gn": "NewKBaseModelTemplates/GramNegModelTemplateV6",
            "ar": "NewKBaseModelTemplates/ArchaeaTemplateV6",
            "grampos": "NewKBaseModelTemplates/GramPosModelTemplateV6",
            "gramneg": "NewKBaseModelTemplates/GramNegModelTemplateV6",
            "archaea": "NewKBaseModelTemplates/ArchaeaTemplateV6",
            "old_grampos": "NewKBaseModelTemplates/GramPosModelTemplateV3",
            "old_gramneg": "NewKBaseModelTemplates/GramNegModelTemplateV3",
            "custom": None,
        }

        # Setting ATP media
        if self.kb_version == "prod":
            self.ATP_media_workspace = "94026"
        elif self.kb_version == "dev":
            self.ATP_media_workspace = "68393"
        else:
            self.log_critical("KBase version not set up for modeling!")

    def _check_and_convert_model(self, model):
        """Check if the model is a MSModelUtil and convert if necessary."""
        if not isinstance(model, self.MSModelUtil):
            model = self.MSModelUtil(model)
        return model

    def _parse_id(self, object_or_id):
        #Check if input is a string or object and if it's an object, set id to object.id
        if isinstance(object_or_id, str):
            id = object_or_id
        else:
            id = object_or_id.id
        if re.search("(.+)_([a-zA-Z]+)(\d*)$", id) != None:
            m = re.search("(.+)_([a-zA-Z]+)(\d*)$", id)
            baseid = m[1]
            compartment = m[2]
            index = m[3]
            if compartment.lower() not in compartment_types:
                self.log_warning(f"Compartment type '{compartment}' not recognized. Readding compartment to base ID.")
                baseid = baseid+"_"+compartment
                compartment = "c"
            else:
                #Standardizing the compartment when it's recognizable
                compartment = compartment_types[compartment.lower()]
            return (baseid, compartment, index)
        self.log_warning(f"ID '{id}' cannot be parsed")
        return (id,None,None)

    def _parse_rxn_stoichiometry(self,rxn) -> Dict:
        """Parse reaction stoichiometry into protons, transport, and transformation"""        
        output = {"metabolite_hash":{},"transport_stoichiometry":{},"equation":rxn.build_reaction_string()}
        for metabolite in rxn.metabolites:
            (base_id, compartment, index) = self._parse_id(metabolite.id)
            if str(compartment) != "c":
                output["transport_stoichiometry"][base_id] = rxn.metabolites[metabolite]
            output["metabolite_hash"].setdefault(base_id, 0)
            output["metabolite_hash"][base_id] += rxn.metabolites[metabolite]
        output["proton_stoichiometry"] = 0
        for base_id in output["metabolite_hash"]:
            if base_id == "cpd00067":
                output["proton_stoichiometry"] += output["metabolite_hash"][base_id]
        return output

    #################Utility functions#####################
    def process_media_list(self, media_list, default_media, workspace):
        if not media_list:
            media_list = []
        # Retrieving media objects from references
        media_objects = []
        first = True
        # Cleaning out empty or invalid media references
        original_list = media_list
        media_list = []
        for media_ref in original_list:
            if not media_ref or len(media_ref) == 0:
                if first:
                    media_list.append(default_media)
                    first = False
                else:
                    print("Filtering out empty media reference")
            elif len(media_ref.split("/")) == 1:
                media_list.append(str(workspace) + "/" + media_ref)
            elif len(media_ref.split("/")) <= 3:
                media_list.append(media_ref)
            else:
                print(media_ref + " looks like an invalid workspace reference")
        # Making sure default gapfilling media is complete media
        if not media_list or len(media_list) == 0:
            media_list = [default_media]
        # Retrieving media objects
        for media_ref in media_list:
            media = self.get_media(media_ref, None)
            media_objects.append(media)
        return media_objects

    def create_minimal_medias(
        self, carbon_list, workspace, base_media="KBaseMedia/Carbon-D-Glucose"
    ):
        data = self.get_object(base_media)["data"]
        for item in carbon_list:
            self.save_json("Carbon-" + item, data)
            copy = self.load_json("Carbon-" + item)
            copy["id"] = "Carbon-" + item
            copy["name"] = "Carbon-" + item
            copy["source_id"] = "Carbon-" + item
            copy["type"] = "MinimalCarbon"
            for cpd in copy["mediacompounds"]:
                if cpd["compound_ref"].split("/")[-1] == "cpd00027":
                    cpd["compound_ref"] = cpd["compound_ref"].replace(
                        "cpd00027", carbon_list[item]
                    )
            self.save_ws_object("Carbon-" + item, workspace, copy, "KBaseBiochem.Media")

    #################Genome functions#####################
    def get_msgenome_from_ontology(
        self, id_or_ref, ws=None, native_python_api=False, output_ws=None
    ):
        annoapi = self.anno_client(native_python_api=native_python_api)
        gen_ref = self.create_ref(id_or_ref, ws)
        genome_info = self.get_object_info(gen_ref)
        annoont = self.AnnotationOntology.from_kbase_data(
            annoapi.get_annotation_ontology_events({"input_ref": gen_ref}),
            gen_ref,
            self.module_dir + "/data/",
        )
        gene_term_hash = annoont.get_gene_term_hash(ontologies=["SSO"])
        if len(gene_term_hash) == 0:
            self.log_warning(
                "Genome has not been annotated with RAST! Reannotating genome with RAST!"
            )
            gen_ref = self.annotate_genome_with_rast(
                genome_info[1], genome_info[6], output_ws
            )
            annoont = self.AnnotationOntology.from_kbase_data(
                annoapi.get_annotation_ontology_events({"input_ref": gen_ref}),
                gen_ref,
                self.module_dir + "/data/",
            )
        annoont.info = genome_info
        wsgenome = self.get_msgenome(gen_ref, ws)
        genome = annoont.get_msgenome()
        for ftr in wsgenome.features:
            for func in ftr.functions:
                if ftr.id in genome.features:
                    genome.features.get_by_id(ftr.id).add_ontology_term("RAST", func)
                else:
                    newftr = genome.create_new_feature(ftr.id, "")
                    newftr.add_ontology_term("RAST", func)
        genome.id = genome_info[1]
        genome.scientific_name = genome_info[10]["Name"]
        return genome

    def get_expression_objs(self, expression_refs, genome_objs):
        genomes_to_models_hash = {}
        for mdl in genome_objs:
            genomes_to_models_hash[genome_objs[mdl]] = mdl
        ftrhash = {}
        expression_objs = {}
        for genome_obj in genomes_to_models_hash:
            for ftr in genome_obj.features:
                ftrhash[ftr.id] = genome_obj
        for expression_ref in expression_refs:
            expression_obj = self.kbase_api.get_from_ws(expression_ref, None)
            row_ids = expression_obj.row_ids
            genome_obj_count = {}
            for ftr_id in row_ids:
                if ftr_id in ftrhash:
                    if ftrhash[ftr_id] not in genome_obj_count:
                        genome_obj_count[ftrhash[ftr_id]] = 0
                    genome_obj_count[ftrhash[ftr_id]] += 1
            best_count = None
            best_genome = None
            for genome_obj in genome_obj_count:
                if best_genome == None or genome_obj_count[genome_obj] > best_count:
                    best_genome = genome_obj
                    best_count = genome_obj_count[genome_obj]
            if best_genome:
                expression_objs[genomes_to_models_hash[best_genome]] = (
                    expression_obj.data
                )
        return expression_objs

    def get_msgenome(self, id_or_ref, ws=None):
        genome = self.kbase_api.get_from_ws(id_or_ref, ws)
        genome.id = genome.info.id
        self.input_objects.append(genome.info.reference)
        return genome

    def get_media(self, id_or_ref, ws=None):
        media = self.kbase_api.get_from_ws(id_or_ref, ws)
        media.id = media.info.id
        self.input_objects.append(media.info.reference)
        return media

    def get_phenotypeset(
        self,
        id_or_ref,
        ws=None,
        base_media=None,
        base_uptake=0,
        base_excretion=1000,
        global_atom_limits={},
    ):
        kbphenoset = self.kbase_api.get_object(id_or_ref, ws)
        phenoset = self.MSGrowthPhenotypes.from_kbase_object(
            kbphenoset,
            self.kbase_api,
            base_media,
            base_uptake,
            base_excretion,
            global_atom_limits,
        )
        return phenoset

    def get_model(self, id_or_ref, ws=None, is_json_file=False):
        if is_json_file:
            return self.MSModelUtil.build_from_kbase_json_file(id_or_ref)
        mdlutl = self.MSModelUtil(self.kbase_api.get_from_ws(id_or_ref, ws))
        mdlutl.wsid = mdlutl.model.info.id
        self.input_objects.append(mdlutl.model.info.reference)
        return mdlutl

    def extend_model_with_other_ontologies(
        self,
        mdlutl,
        anno_ont,
        builder,
        prioritized_event_list=None,
        ontologies=None,
        merge_all=True,
    ):
        gene_term_hash = anno_ont.get_gene_term_hash(
            prioritized_event_list, ontologies, merge_all, False
        )
        self.print_json_debug_file("gene_term_hash", gene_term_hash)
        residual_reaction_gene_hash = {}
        for gene in gene_term_hash:
            for term in gene_term_hash[gene]:
                if term.ontology.id != "SSO":
                    for rxn_id in term.msrxns:
                        if rxn_id not in residual_reaction_gene_hash:
                            residual_reaction_gene_hash[rxn_id] = {}
                        if gene not in residual_reaction_gene_hash[rxn_id]:
                            residual_reaction_gene_hash[rxn_id][gene] = []
                        residual_reaction_gene_hash[rxn_id][gene] = gene_term_hash[
                            gene
                        ][term]

        reactions = []
        SBO_ANNOTATION = "sbo"
        modelseeddb = self.biochem_db()
        biochemdbrxn = False
        for rxn_id in residual_reaction_gene_hash:
            if rxn_id + "_c0" not in mdlutl.model.reactions:
                reaction = None
                template_reaction = None
                if rxn_id + "_c" in mdlutl.model.template.reactions:
                    template_reaction = mdlutl.model.template.reactions.get_by_id(
                        rxn_id + "_c"
                    )
                elif rxn_id in modelseeddb.reactions:
                    rxnobj = modelseeddb.reactions.get_by_id(rxn_id)
                    if "MI" not in rxnobj.status and "CI" not in rxnobj.status:
                        # mdlutl.add_ms_reaction({rxn_id:"c0"}, compartment_trans=["c0", "e0"])
                        template_reaction = rxnobj.to_template_reaction(
                            {0: "c", 1: "e"}
                        )
                        biochemdbrxn = True
                if template_reaction:
                    for m in template_reaction.metabolites:
                        if m.compartment not in builder.compartments:
                            builder.compartments[m.compartment] = (
                                builder.template.compartments.get_by_id(m.compartment)
                            )
                        if m.id not in builder.template_species_to_model_species:
                            model_metabolite = m.to_metabolite(builder.index)
                            builder.template_species_to_model_species[m.id] = (
                                model_metabolite
                            )
                            builder.base_model.add_metabolites([model_metabolite])
                    if biochemdbrxn:
                        pass
                        # template_reaction.add_metabolites({})
                    reaction = template_reaction.to_reaction(
                        builder.base_model, builder.index
                    )
                    gpr = ""
                    probability = None
                    for gene in residual_reaction_gene_hash[rxn_id]:
                        for item in residual_reaction_gene_hash[rxn_id][gene]:
                            if "scores" in item:
                                if "probability" in item["scores"]:
                                    if (
                                        not probability
                                        or item["scores"]["probability"] > probability
                                    ):
                                        probability = item["scores"]["probability"]
                        if len(gpr) > 0:
                            gpr += " or "
                        gpr += gene.id
                    if probability != None and hasattr(reaction, "probability"):
                        reaction.probability = probability
                    reaction.gene_reaction_rule = gpr
                    reaction.annotation[SBO_ANNOTATION] = "SBO:0000176"
                    reactions.append(reaction)
                if not reaction:
                    print("Reaction ", rxn_id, " not found in template or database!")
            else:
                rxn = mdlutl.model.reactions.get_by_id(rxn_id + "_c0")
                gpr = rxn.gene_reaction_rule
                probability = None
                for gene in residual_reaction_gene_hash[rxn_id]:
                    for item in residual_reaction_gene_hash[rxn_id][gene]:
                        if "scores" in item:
                            if "probability" in item["scores"]:
                                if (
                                    not probability
                                    or item["scores"]["probability"] > probability
                                ):
                                    probability = item["scores"]["probability"]
                    if len(gpr) > 0:
                        gpr += " or "
                    gpr += gene.id
                if probability != None and hasattr(rxn, "probability"):
                    rxn.probability = probability
                rxn.gene_reaction_rule = gpr
        mdlutl.model.add_reactions(reactions)
        return mdlutl

    #################Classifier functions#####################
    def get_classifier(self):
        cls_pickle = self.config["data"] + "/knn_ACNP_RAST_full_01_17_2023.pickle"
        cls_features = (
            self.config["data"] + "/knn_ACNP_RAST_full_01_17_2023_features.json"
        )
        # cls_pickle = self.module_dir+"/data/knn_ACNP_RAST_filter.pickle"
        # cls_features = self.module_dir+"/data/knn_ACNP_RAST_filter_features.json"
        with open(cls_pickle, "rb") as fh:
            model_filter = pickle.load(fh)
        with open(cls_features) as fh:
            features = self.json.load(fh)
        return self.MSGenomeClassifier(model_filter, features)

    #################Template functions#####################
    def get_gs_template(self, template_id, ws, core_template, excluded_cpd=None):
        if excluded_cpd is None:
            excluded_cpd = []
        gs_template = self.get_template(template_id, ws)
        for cpd in core_template.compcompounds:
            if cpd.id not in gs_template.compcompounds:
                gs_template.compcompounds.append(cpd)
        for rxn in core_template.reactions:
            if rxn.id in gs_template.reactions:
                gs_template.reactions._replace_on_id(rxn)
            else:
                gs_template.reactions.append(rxn)
        for rxn in gs_template.reactions:
            for met in rxn.metabolites:
                if met.id[0:8] in excluded_cpd:
                    gs_template.reactions.remove(rxn)
        return gs_template

    def get_template(self, template_id, ws=None):
        """Retrieve a template from KBase workspace."""
        if ws is None and "/" not in template_id and template_id in self.templates:
            template_id = self.templates[template_id]
        template = self.kbase_api.get_from_ws(template_id, ws)
        # template = self.kbase_api.get_object(template_id,ws)
        # info = self.kbase_api.get_object_info(template_id,ws)
        # template = MSTemplateBuilder.from_dict(template).build()
        self.input_objects.append(template.info.reference)
        return template

    #################Save functions#####################
    def save_model(self, mdlutl, workspace=None, objid=None, suffix=None):
        # Checking for zero flux reactions
        for rxn in mdlutl.model.reactions:
            if rxn.lower_bound == 0 and rxn.upper_bound == 0:
                print("Zero flux reaction: " + rxn.id)
        # Setting the ID based on input
        if not suffix:
            suffix = ""
        if not objid:
            objid = mdlutl.wsid
        if not objid:
            self.log_critical("Must provide an ID to save a model!")
        objid = objid + suffix
        mdlutl.wsid = objid
        # Saving attributes and getting model data
        if not isinstance(mdlutl.model, self.FBAModel):
            mdlutl.model = self.CobraModelConverter(mdlutl.model).build()
        mdlutl.save_attributes()
        data = mdlutl.model.get_data()
        # If the workspace is None, then saving data to file
        if not workspace:
            self.print_json_debug_file(mdlutl.wsid + ".json", data)
        else:
            # Setting the workspace
            if workspace:
                self.set_ws(workspace)
            # Setting provenance and saving model using workspace API
            mdlutl.create_kb_gapfilling_data(data, self.ATP_media_workspace)
            params = {
                "id": self.ws_id,
                "objects": [
                    {
                        "data": data,
                        "name": objid,
                        "type": "KBaseFBA.FBAModel",
                        "meta": {},
                        "provenance": self.provenance(),
                    }
                ],
            }
            self.ws_client().save_objects(params)
            self.obj_created.append(
                {"ref": self.create_ref(objid, self.ws_name), "description": ""}
            )

    def save_phenotypeset(self, data, workspace, objid):
        self.set_ws(workspace)
        params = {
            "id": self.ws_id,
            "objects": [
                {
                    "data": data,
                    "name": objid,
                    "type": "KBasePhenotypes.PhenotypeSet",
                    "meta": {},
                    "provenance": self.provenance(),
                }
            ],
        }
        self.ws_client().save_objects(params)
        self.obj_created.append(
            {"ref": self.create_ref(objid, self.ws_name), "description": ""}
        )

    def save_solution_as_fba(
        self,
        fba_or_solution,
        mdlutl,
        media,
        fbaid,
        workspace=None,
        fbamodel_ref=None,
        other_solutions=None,
    ):
        if not isinstance(fba_or_solution, self.MSFBA):
            fba_or_solution = self.MSFBA(
                mdlutl, media, primary_solution=fba_or_solution
            )
        fba_or_solution.id = fbaid
        if other_solutions != None:
            for other_solution in other_solutions:
                fba_or_solution.add_secondary_solution(other_solution)
        data = fba_or_solution.generate_kbase_data(fbamodel_ref, media.info.reference)
        # If the workspace is None, then saving data to file
        if not workspace and self.util:
            self.util.save(fbaid, data)
        else:
            # Setting the workspace
            if workspace:
                self.set_ws(workspace)
            # Setting provenance and saving model using workspace API
            params = {
                "id": self.ws_id,
                "objects": [
                    {
                        "data": data,
                        "name": fbaid,
                        "type": "KBaseFBA.FBA",
                        "meta": {},
                        "provenance": self.provenance(),
                    }
                ],
            }
            self.ws_client().save_objects(params)
            self.obj_created.append(
                {"ref": self.create_ref(fbaid, self.ws_name), "description": ""}
            )


    ### High level model analysis functions
    def model_reaction_directionality_analysis(self, model, output_dataframe=True):
        """Analyzes model reactions for stoichiometric correctness and directionality."""
        model = self._check_and_convert_model(model)
        output = {}
        for rxn in model.model.reactions:
            if rxn.id[0:3] not in self.const_util_rxn_prefixes():
                output[rxn.id] = {
                    "reaction_id": rxn.id,
                    "name": rxn.name,
                    "equation": rxn.build_reaction_string(use_metabolite_names=True),
                    "model_direction": self.reaction_directionality_from_bounds(rxn),
                    "ai_direction": None,
                    "biochem_reversibility": self.reaction_biochem_directionality(rxn),
                    "combined": (
                            f"{direction_conversion[model_direction]}|"
                            f"{direction_conversion[ai_direction]}|"
                            f"{direction_conversion[biochem_direction]}"
                        )
                }
                output[rxn.id]["ai_output"] = self.analyze_reaction_directionality(rxn)
                output[rxn.id]["ai_direction"] = output[rxn.id]["ai_output"]["directionality"]
        if output_dataframe:
            output = pd.DataFrame.from_records(output.values())
        return output
