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

compartment_types = {
    "cytosol":"c",
    "extracellar":"e",
    "extraorganism":"e",
    "periplasm":"p",
    "c":"c",
    "p":"p",
    "e":"e"
}

direction_conversion = {
    "":"-",
    "forward": ">",
    "reverse": "<",
    "reversible": "=",
    "uncertain": "?",
    "blocked":"B"
}

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

    def remove_model_periplasm_compartment(self, model_or_mdlutl):
        """Remove the periplasm compartment from a model."""
        mdlutl = self._check_and_convert_model(model_or_mdlutl)
        reactions_to_remove = []
        for cpd in mdlutl.model.metabolites:
            [base_id, compartment, index] = self._parse_id(cpd)
            if compartment == "p":
                replacement_cpd = None
                for rcpd in mdlutl.model.metabolites:
                    [rcpd_base_id, rcpd_compartment, rcpd_index] = self._parse_id(rcpd)
                    if rcpd_compartment == "e" and rcpd_base_id == base_id:
                        replacement_cpd = rcpd
                        break
                if replacement_cpd == None:
                    #Change the compound in place to extracellular
                    print("Original cpd:", cpd.id)
                    cpd.id = base_id + "_e" + index
                    cpd.compartment = "e"
                    print("Modified cpd:", cpd.id)
                else:
                    rxnlist = cpd.reactions
                    for rxn in rxnlist:
                        stoich = rxn.metabolites[cpd]
                        print("Original:", rxn.build_reaction_string())
                        rxn.add_metabolites({cpd: -stoich,replacement_cpd: stoich})
                        print("Modified:", rxn.build_reaction_string())
                        if len(rxn.metabolites) == 0:
                            print("Removed")
                            reactions_to_remove.append(rxn)
        mdlutl.model.remove_reactions(reactions_to_remove)
        return mdlutl

    def model_standardization(
        self, model_or_mdlutl, template="gp", filter_based_on_template=True, msmodel=None, model_comparison=True
    ):
        """Standardize a model or MSModelUtil object."""
        mdlutl = self._check_and_convert_model(model_or_mdlutl)
        mdlutl = self.remove_model_periplasm_compartment(mdlutl)
        match_results = self.match_model_reactions_to_db(mdlutl, template,msmodel=msmodel,filter_based_on_template=filter_based_on_template)
        count = 0
        for rxn in mdlutl.model.reactions:
            if rxn.id[0:3] not in self.const_util_rxn_prefixes():
                count += 1
        match_results["match_stats"] = {
            "num_cpd_matches": [len(match_results["cpd_matches"]),len(mdlutl.model.metabolites)],
            "num_rxn_matches": [len(match_results["rxn_matches"]),count],
            "cpd_hit_count_frequency": match_results["cpddf"]["match_count"].value_counts().to_dict(),
            "rxn_hit_count_frequency": match_results["rxndf"]["match_count"].value_counts().to_dict()
        }
        for item in  match_results["match_stats"]["cpd_hit_count_frequency"]:
            match_results["match_stats"]["cpd_hit_count_frequency"][item] = match_results["match_stats"]["cpd_hit_count_frequency"][item]/int(item)
        for item in  match_results["match_stats"]["rxn_hit_count_frequency"]:
            match_results["match_stats"]["rxn_hit_count_frequency"][item] = match_results["match_stats"]["rxn_hit_count_frequency"][item]/int(item)
        if msmodel != None and model_comparison:
            match_results["model_match_stats"] = self.compare_model_to_msmodel(mdlutl,msmodel,mapping_output=match_results)
        return match_results

    def compare_model_to_msmodel(self, model_or_mdlutl, msmodel,mapping_output=None,check_pairings_with_ai=False):
        """Compare a model or MSModelUtil object with a ModelSEED model."""
        mdlutl = self._check_and_convert_model(model_or_mdlutl)
        if mapping_output == None:
            mapping_output = self.model_standardization(mdlutl, msmodel=msmodel,model_comparison=False)
        msmodel = self._check_and_convert_model(msmodel)
        
        #Initializing output
        output = {
            "cpd_counts": [0,0,0],
            "rxn_counts": [0,0,0],
            "gene_counts": [0,0,0],
            "rxn_gene_counts": [0,0,0],
            "transport_counts": [0,0,0],
            "genes": {},
            "reactions": {}
        }
        
        #Setting compound and transport counts
        for cpd in msmodel.model.metabolites:
            if cpd.compartment.lower()[0:1] == "e":
                output["transport_counts"][1] += 1
            else:
                output["cpd_counts"][1] += 1
        for cpd in mdlutl.model.metabolites:
            if cpd.compartment.lower()[0:1] == "e":
                output["transport_counts"][0] += 1
                for hit in mapping_output["cpd_matches"][cpd.id]:
                    if hit+"_e0" in msmodel.model.metabolites:
                        output["transport_counts"][2] += 1
                        break
            else:
                output["cpd_counts"][0] += 1
                for hit in mapping_output["cpd_matches"][cpd.id]:
                    if hit+"_c0" in msmodel.model.metabolites:
                        output["cpd_counts"][2] += 1
                        break
        
        #Setting all reaction output
        matchmsrxn = {}
        ms_to_mod = {}
        for rxn in mdlutl.model.reactions:
            if rxn.id[0:3] in self.const_util_rxn_prefixes():
                continue
            #Initializing the record
            output["rxn_counts"][0] += 1
            record = {
                "ModID": rxn.id,#Done
                "MSID": None,#Done
                "Name": rxn.name,#Done
                "Equation": rxn.build_reaction_string(use_metabolite_names=True),#Done
                "Match equation": None,#Done
                "Match evidence": None,
                "Other matches":[],
                "Status": "RxnModel",#Done
                "Gene status":None,#Done
                "Direction status": direction_conversion[self.reaction_directionality_from_bounds(rxn)],#DONE
                "Reference direction": None,#Done
                "In template": [],#Done
                "Match genes": [],#Done
                "MS only genes": [],#Done
                "Model only genes": [],#Done
                "Overlapping MS matches": 0,#Done
                "Comments": []#Done
            }
            output["reactions"][rxn.id] = record
            #Identifying the best hit to associate with the model reaction
            best_gene_match = None
            best_gene_hit = None
            best_score = None
            best_score_hit = None
            best_ms_score = None
            best_ms_score_hit = None
            for hit in mapping_output["rxn_matches"][rxn.id]:
                if best_score == None or mapping_output["rxn_matches"][rxn.id][hit]["score"] > best_score:
                    best_score = mapping_output["rxn_matches"][rxn.id][hit]["score"]
                    best_score_hit = hit
                if hit+"_c0" in msmodel.model.reactions:
                    if best_ms_score == None or mapping_output["rxn_matches"][rxn.id][hit]["score"] > best_ms_score:
                        best_ms_score = mapping_output["rxn_matches"][rxn.id][hit]["score"]
                        best_ms_score_hit = hit
                    msrxn = msmodel.model.reactions.get_by_id(hit+"_c0")
                    matches = 0
                    for msgene in msrxn.genes:
                        if msgene.id[0:4] == "mRNA":
                            continue
                        for gene in rxn.genes:
                            if gene.id == msgene.id:
                                print("Match",rxn.id,msrxn.id,gene.id,msgene.id)
                                matches += 1
                                break
                    if best_gene_match == None or matches > best_gene_match:
                        best_gene_match = matches
                        best_gene_hit = hit
            #Setting the MSID based on best score or best ms score - best ms score preferred
            if best_gene_hit != None:
                record["MSID"] = best_gene_hit
            elif best_ms_score_hit != None:
                record["MSID"] = best_ms_score_hit
            elif best_score_hit != None:
                record["MSID"] = best_score_hit
            #Saving reaction associations in the matching hashes and setting MSID associated data elements
            if record["MSID"] != None:
                #Setting match evidence
                record["Match evidence"] = f"Score:{mapping_output['rxn_matches'][rxn.id][record['MSID']]['score']} EC:{mapping_output['rxn_matches'][rxn.id][record['MSID']]['ec_hits']} ID:{mapping_output['rxn_matches'][rxn.id][record['MSID']]['identifier_hits']} Transport:{mapping_output['rxn_matches'][rxn.id][record['MSID']]['transport_scores']} Equation:{mapping_output['rxn_matches'][rxn.id][record['MSID']]['equation_scores']} Protons:{mapping_output['rxn_matches'][rxn.id][record['MSID']]['proton_matches']}"
                #Adding other hits to the other match entries
                for hit in mapping_output["rxn_matches"][rxn.id]:
                    if hit != record["MSID"]:
                        msdbrxn = self.biochem_db.reactions.get_by_id(hit)
                        rxn_string = msdbrxn.build_reaction_string(use_metabolite_names=True)+":"+f"{hit}:Score:{mapping_output['rxn_matches'][rxn.id][hit]['score']} EC:{mapping_output['rxn_matches'][rxn.id][hit]['ec_hits']} ID:{mapping_output['rxn_matches'][rxn.id][hit]['identifier_hits']} Transport:{mapping_output['rxn_matches'][rxn.id][hit]['transport_scores']} Equation:{mapping_output['rxn_matches'][rxn.id][hit]['equation_scores']} Protons:{mapping_output['rxn_matches'][rxn.id][hit]['proton_matches']}"
                        record["Other matches"].append(rxn_string)
                #Setting the mod to ms and ms to mod hashes                
                ms_to_mod[record["MSID"]+"_c0"] = rxn.id
                if mapping_output["rxn_matches"][rxn.id][record["MSID"]]["template"]:
                    record["In template"] = "InTemplate"
                else:
                    record["In template"] = "NotInTemplate"
                #Setting reference direction
                biochem_direction = "?"
                ai_direction = "?"
                if record["MSID"] in self.biochem_db.reactions:
                    msdbrxn = self.biochem_db.reactions.get_by_id(record["MSID"])
                    record["Match equation"] = msdbrxn.build_reaction_string(use_metabolite_names=True)
                    biochem_direction =  direction_conversion[self.reaction_directionality_from_bounds(msdbrxn)]
                    ai_direction = direction_conversion[self.analyze_reaction_directionality(msdbrxn)["directionality"]]
                record["Reference direction"] = biochem_direction+ai_direction
                #Checking for overlapping matches
                matchmsrxn.setdefault(record["MSID"],[])
                matchmsrxn[record["MSID"]].append(rxn.id)
                for match in matchmsrxn[record["MSID"]]:
                    output["reactions"][match]["Overlapping MS matches"] = len(matchmsrxn[record["MSID"]])
            #Checking if the finalized reaction match is in the ms model and if so, fetching the ms model reaction
            msrxn = None
            if record["MSID"] != None and record["MSID"]+"_c0" in msmodel.model.reactions:
                msrxn = msmodel.model.reactions.get_by_id(record["MSID"]+"_c0")
                record["Direction status"] += direction_conversion[self.reaction_directionality_from_bounds(msrxn)]
                record["Status"] = "Both"
                output["rxn_counts"][2] += 1
            else:
                record["Status"] = "ModelOnly"
            #If the best ms score and best gene score and best score are different, adding comments
            if best_score_hit != None and best_ms_score_hit != None and best_score_hit != best_ms_score_hit:
                record["Comments"].append("Best match ("+best_score_hit+") different from MS match ("+best_ms_score_hit+")")
            if best_ms_score_hit != None and best_gene_hit != None and best_ms_score_hit != best_gene_hit:
                record["Comments"].append("Best gene match ("+best_gene_hit+") different from MS match ("+best_ms_score_hit+")")
            #Checking if the model genes are a match or in the model only
            for gene in rxn.genes:
                output["rxn_gene_counts"][0] += 1
                if msrxn != None:
                    found = False
                    for msgene in msrxn.genes:
                        if msgene.id == gene.id:
                            found = True
                            break
                    if found:
                        record["Match genes"].append(gene.id)
                        output["rxn_gene_counts"][2] += 1
                    else:
                        record["Model only genes"].append(gene.id)
            #Checking if the MS model has unique genes for this reaction and setting reaction status
            if msrxn != None:
                for gene in msmodel.model.reactions.get_by_id(record["MSID"]+"_c0").genes:
                    if gene.id[0:4] == "mRNA":
                        continue
                    if gene.id not in record["Match genes"]:
                        record["MS only genes"].append(gene.id)
            #Setting reaction gene status
            if len(record["Model only genes"]) > 0:
                if len(record["MS only genes"]) > 0:
                    record["Gene status"] = "ExtraBoth"
                elif len(record["Match genes"]) > 0:
                    record["Gene status"] = "ExtraModel"
                else:
                    record["Gene status"] = "ModelOnly"
            elif len(record["MS only genes"]) > 0:
                if len(record["Match genes"]) > 0:
                    record["Gene status"] = "ExtraMS"
                else:
                    record["Gene status"] = "MSOnly"
            elif len(record["Match genes"]) > 0:
                record["Gene status"] = "Match"
            else:
                record["Gene status"] = "NoGene"
        #Checking for unique genes and reactions in the MS model
        for rxn in msmodel.model.reactions:
            if rxn.id[0:3] in self.const_util_rxn_prefixes():
                continue
            output["rxn_counts"][1] += 1
            for gene in rxn.genes:
                if gene.id[0:4] != "mRNA":
                    output["rxn_gene_counts"][1] += 1
            if rxn.id in ms_to_mod:
                continue
            record = {
                "ModID": None,#Done
                "MSID": rxn.id,#Done
                "Name": rxn.name,#Done
                "Equation": rxn.build_reaction_string(use_metabolite_names=True),#Done
                "Match evidence": [],
                "Other match equations":[],
                "Other match evidence":[],
                "Status": "RxnMS",#Done
                "Gene status":"MSOnly",#Done
                "Direction status": "-"+direction_conversion[self.reaction_directionality_from_bounds(rxn)],#DONE
                "Reference direction": None,#Done
                "In template": "In template",#Done
                "Match genes": [],#Done
                "MS only genes": [],#Done
                "Model only genes": [],#Done
                "Overlapping MS matches": 0,#Done
                "Comments": []#Done
            }
            output["reactions"][rxn.id] = record
            if record["MSID"] in self.biochem_db.reactions:
                biochem_direction = direction_conversion[self.reaction_directionality_from_bounds(self.biochem_db.reactions.get_by_id(record["MSID"]))]
            ai_direction = direction_conversion[self.analyze_reaction_directionality(rxn)["directionality"]]
            if ai_direction == None:
                ai_direction = "?"
            record["Reference direction"] = biochem_direction+ai_direction
            for gene in rxn.genes:
                if gene.id[0:4] != "mRNA":
                    record["MS only genes"].append(gene.id)
        
       #Setting gene output
        for gene in mdlutl.model.genes:
            output["gene_counts"][0] += 1
            record = {
                "ID": gene.id,
                "Status": "GeneModel",
                "Reactions": [],
            }
            if gene.id in msmodel.model.genes:
                output["gene_counts"][2] += 1
                record["Status"] = "GeneBoth"
            for rxn in gene.reactions:
                rxnstring = rxn.id
                if output["reactions"][rxn.id]["MSID"] != None:
                    rxnstring += "("+output["reactions"][rxn.id]["MSID"]+")"
                if output["reactions"][rxn.id]["MSID"]+"_c0" in msmodel.model.reactions:
                    found = False
                    for msgene in msmodel.model.reactions.get_by_id(output["reactions"][rxn.id]["MSID"]+"_c0").genes:
                        if msgene.id == gene.id:
                            found = True
                            break
                    if found:
                        rxnstring += ":RBoth:RGBoth:"
                    else:
                        rxnstring += ":RBoth:RGMod:"
                else:
                    rxnstring += ":RMod:RGMod:"
                rxnstring += rxn.build_reaction_string(use_metabolite_names=True)
                record["Reactions"].append(rxnstring)
            output["genes"][gene.id] = record
        
        for gene in msmodel.model.genes:
            if gene.id[0:4] == "mRNA":
                continue
            output["gene_counts"][1] += 1
            #Creating a new record if it doesn't already exist
            output["genes"].setdefault(gene.id,{"ID":gene.id,"Status":"GeneMS","Reactions":[]})
            record = output["genes"][gene.id]
            for rxn in gene.reactions:
                idlength = len(rxn.id)
                rxnstring = rxn.id
                if rxn.id in ms_to_mod:
                    idlength = len(ms_to_mod[rxn.id])
                    rxnstring = ms_to_mod[rxn.id] + "("+rxn.id+"):RBoth:RGMS:"
                else: rxnstring += ":RMS:RGMS:"
                rxnstring += rxn.build_reaction_string(use_metabolite_names=True)
                if output["genes"][gene.id]["Status"] == "GeneMS":
                    record["Reactions"].append(rxnstring)
                else:
                    found = False
                    for i in range(len(record["Reactions"])):
                        if record["Reactions"][i].startswith(rxnstring[0:idlength+1]):
                            found = True
                            break
                    if not found:
                        record["Reactions"].append(rxnstring)
        
        if check_pairings_with_ai:
            for rxn in mdlutl.model.reactions:
                if rxn.id not in output["reactions"]:
                    continue
                output["reactions"][rxn.id]["ai_analysis"] = {}
                if output["reactions"][rxn.id]["MSID"] != None:
                    other_rxn = self.get_reaction_by_id(output["reactions"][rxn.id]["MSID"])
                    output["reactions"][rxn.id]["ai_analysis"][output["reactions"][rxn.id]["MSID"]] = self.evaluate_reaction_equivalence(rxn, other_rxn, output["reactions"][rxn.id]["Match evidence"])
                for index,other_hit in enumerate(output["reactions"][rxn.id]["Other match equations"]):
                    other_id = other_hit.split(":")[1]
                    other_rxn = self.reaction_id_to_msid(other_id)
                    if other_rxn != None:
                        other_rxn = self.get_reaction_by_id(other_rxn)
                        output["reactions"][rxn.id]["ai_analysis"][other_hit] = self.evaluate_reaction_equivalence(rxn, other_rxn,output["reactions"][rxn.id]["Other match evidence"][index])
        return output

    def match_model_compounds_to_db(
        self, model_or_mdlutl, template="gp", create_dataframe=True, filter_based_on_template=True, annotate_model=False
    ):  
        """Searching all compounds in a model against the ModelSEEDDatabase and a template"""
        #Getting template
        if isinstance(template, str):
            template = self.get_template(template)
        mdlutl = self._check_and_convert_model(model_or_mdlutl)
        results = {"matches": {}, "df": None}
        in_template = {}
        for cpd in mdlutl.model.metabolites:
            #First let's break this compound down into a base ID and compartment
            [base_id, compartment, index] = self._parse_id(cpd)
            in_template[cpd.id] = True
            cpdcomp = self._standardize_string(cpd.compartment)
            if cpdcomp in compartment_types:
                cpdcomp = compartment_types[cpdcomp]
            if compartment != cpdcomp:
                self.log_warning(
                    f"Compound {cpd.id} has compartment {cpdcomp} but ID indicates {compartment}"
                )
            #Now we query by ID, alias, formula, charge and score the matches
            matches = {}
            identifiers = [base_id,cpd.name]
            structures = []
            for anno_type in cpd.annotation:
                if isinstance(cpd.annotation[anno_type], set):
                    for item in cpd.annotation[anno_type]:
                        if item not in identifiers:
                            identifiers.append(item)
                elif lower(anno_type) in ["smiles","inchi","structure","inchikey"]:
                    structures.append(cpd.annotation[anno_type])
            results["matches"][cpd.id] = self.search_compounds(
                query_identifiers=identifiers,
                query_structures=structures,
                query_formula=cpd.formula
            )
            hits_to_remove = []
            for hit in results["matches"][cpd.id]:
                results["matches"][cpd.id][hit]["base_id"] = base_id
                results["matches"][cpd.id][hit]["compartment"] = compartment
                results["matches"][cpd.id][hit]["index"] = index
                results["matches"][cpd.id][hit]["match_name"] = self.biochem_db.compounds.get_by_id(hit).name
                if filter_based_on_template and hit not in template.compounds:
                    hits_to_remove.append(hit)
            if len(hits_to_remove) < len(results["matches"][cpd.id]):
                for hit in hits_to_remove:
                    del results["matches"][cpd.id][hit]
            else:
                in_template[cpd.id] = False
                self.log_warning(f"None of the hits for {cpd.id} were in the model template! Leaving all hits in.")

        #Now let's create a dataframe showing all the matches for all the compounds
        if create_dataframe:
            df_data = []
            for model_cpd_id, matches in results["matches"].items():
                count = len(matches)
                for matched_cpd_id, match_info in matches.items():
                    df_data.append({
                        "model_compound_id": model_cpd_id,
                        "base_id": match_info["base_id"],
                        "match_count": count,
                        "compartment": match_info["compartment"],
                        "index": match_info["index"],
                        "matched_compound_id": matched_cpd_id,
                        "matched_compound_name": match_info["match_name"],
                        "score": match_info["score"],
                        "id_match": str(match_info["identifier_hits"]),
                        "formula_match": str(match_info["formula_hits"]),
                        "structure_match": str(match_info["structure_hits"]),
                        "in template": in_template[model_cpd_id]
                    })
            results["df"] = pd.DataFrame(df_data)
            # Sort by model compound ID and then by score (descending)
            if not results["df"].empty:
                results["df"] = results["df"].sort_values(
                    ["model_compound_id", "score"], 
                    ascending=[True, False]
                ).reset_index(drop=True)
        if annotate_model:
            for cpd in mdlutl.model.metabolites:
                if cpd.id in results["matches"]:
                    cpd.annotation["ModelSEED"] = set()
                    for hit in results["matches"][cpd.id]:
                        cpd.annotation["ModelSEED"].add(hit)
        return results

    def match_model_reactions_to_db(
        self, model_or_mdlutl, template="gp", create_dataframe=True,msmodel=None,filter_based_on_template=True
    ):  
        """Searching all reactions in a model against the ModelSEEDDatabase and a template"""
        EC_PATTERN = re.compile(r'^(?:EC\s*)?(?:[1-7])\.(?:\d+|-)\.(?:\d+|-)\.(?:\d+|-)$', re.I)
        #Getting template
        if isinstance(template, str):
            template = self.get_template(template)
        mdlutl = self._check_and_convert_model(model_or_mdlutl)
        results = {"cpd_matches": {}, "rxn_matches": {}, "rxndf": None, "cpddf": None}
        cpd_match_hits = self.match_model_compounds_to_db(mdlutl, template, filter_based_on_template=filter_based_on_template)
        results["cpd_matches"] = cpd_match_hits["matches"]
        results["cpddf"] = cpd_match_hits["df"]
        for rxn in mdlutl.model.reactions:
            if rxn.id[0:3] not in self.const_util_rxn_prefixes():
                #First let's break this reaction down into a base ID and compartment
                [base_id, compartment, index] = self._parse_id(rxn)
                #Now we query by ID, alias, formula, charge and score the matches
                matches = {}
                identifiers = [base_id,rxn.name]
                ec_numbers = []
                for anno_type in rxn.annotation:
                    if isinstance(rxn.annotation[anno_type], set):
                        for item in rxn.annotation[anno_type]:
                            if EC_PATTERN.fullmatch(item.strip()) is not None:
                                ec_numbers.append(item.strip())
                            elif item not in identifiers:
                                identifiers.append(item)
                stoichiometry_data = self._parse_rxn_stoichiometry(rxn)
                results["rxn_matches"][rxn.id] = self.search_reactions(
                    query_identifiers=identifiers,
                    query_ec=ec_numbers,
                    query_stoichiometry=stoichiometry_data,
                    cpd_hits=cpd_match_hits
                )
                hits_to_remove = []
                for hit in results["rxn_matches"][rxn.id]:
                    results["rxn_matches"][rxn.id][hit]["base_id"] = base_id
                    results["rxn_matches"][rxn.id][hit]["equation"] = rxn.build_reaction_string(use_metabolite_names=True)
                    results["rxn_matches"][rxn.id][hit]["match_name"] = self.biochem_db.reactions.get_by_id(hit).name
                    results["rxn_matches"][rxn.id][hit]["match_equation"] = self.biochem_db.reactions.get_by_id(hit).build_reaction_string(use_metabolite_names=True)
                    if hit+"_c" not in template.reactions:
                        results["rxn_matches"][rxn.id][hit]["template"] = False
                        hits_to_remove.append(hit)
                    else:
                        results["rxn_matches"][rxn.id][hit]["template"] = True
                if len(hits_to_remove) < len(results["rxn_matches"][rxn.id]) and filter_based_on_template:
                    for hit in hits_to_remove:
                        del results["rxn_matches"][rxn.id][hit]
                else:
                    self.log_warning(f"None of the hits for {rxn.id} were in the model template! Leaving all hits in.")
                if msmodel is not None:
                    msmodel = self._check_and_convert_model(msmodel)
                    for hit in results["rxn_matches"][rxn.id]:
                        results["rxn_matches"][rxn.id][hit]["msmodel"] = "None"
                        results["rxn_matches"][rxn.id][hit]["gene_mismatches"] = []
                        results["rxn_matches"][rxn.id][hit]["gene_matches"] = []
                        results["rxn_matches"][rxn.id][hit]["ms_gene_mismatches"] = []
                        if hit+"_c0" in msmodel.model.reactions:
                            msrxn = msmodel.model.reactions.get_by_id(hit+"_c0")
                            results["rxn_matches"][rxn.id][hit]["msmodel"] = self.reaction_directionality_from_bounds(msrxn)
                            for gene in rxn.genes:
                                for ms_gene in msrxn.genes:
                                    if gene.id == ms_gene.id:
                                        results["rxn_matches"][rxn.id][hit]["score"] += 10
                                        results["rxn_matches"][rxn.id][hit]["gene_matches"].append(gene.id)
                                if gene.id not in results["rxn_matches"][rxn.id][hit]["gene_matches"]:
                                    results["rxn_matches"][rxn.id][hit]["score"] -= 5
                                    results["rxn_matches"][rxn.id][hit]["gene_mismatches"].append(gene.id)
                            for ms_gene in msrxn.genes:
                                if ms_gene.id not in results["rxn_matches"][rxn.id][hit]["gene_matches"] and ms_gene.id[0:4] != "mRNA":
                                    results["rxn_matches"][rxn.id][hit]["score"] -= 5
                                    results["rxn_matches"][rxn.id][hit]["ms_gene_mismatches"].append(ms_gene.id)
        #Now let's create a dataframe showing all the matches for all the compounds
        if create_dataframe:
            df_data = []
            for model_rxn_id, matches in results["rxn_matches"].items():
                count = len(matches)
                for matched_rxn_id, match_info in matches.items():
                    df_data.append({
                        "model_id": model_rxn_id,
                        "base_id": match_info["base_id"],
                        "equation": match_info["equation"],
                        "match_count": count,
                        "matched_reaction_id": matched_rxn_id,
                        "matched_reaction_name": match_info["match_name"],
                        "matched_reaction_equation": match_info["match_equation"],
                        "score": match_info["score"],
                        "id_match": str(match_info["identifier_hits"]),
                        "ec_match": str(match_info["ec_hits"]),
                        "transport_match": str(match_info["transport_scores"]),
                        "equation_match": str(match_info["equation_scores"]),
                        "proton_matches": str(match_info["proton_matches"]),
                        "gene_mismatches": str(match_info["gene_mismatches"]),
                        "ms_gene_mismatches": str(match_info["ms_gene_mismatches"]),
                        "gene_matches": str(match_info["gene_matches"]),
                        "msmodel": str(match_info["msmodel"]),
                        "template": match_info["template"]
                    })
            results["rxndf"] = pd.DataFrame(df_data)
            # Sort by model reaction ID and then by score (descending)
            if not results["rxndf"].empty:
                results["rxndf"] = results["rxndf"].sort_values(
                    ["model_id", "score"], 
                    ascending=[True, False]
                ).reset_index(drop=True)
        return results

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
