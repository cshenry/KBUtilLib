"""KBase model standardization utilities for matching and translating models to ModelSEED namespace.

This module provides utilities for:
- Matching model compounds and reactions to ModelSEED database
- Translating model IDs to ModelSEED namespace
- Comparing models to ModelSEED models
- Standardizing model structure and compartments
"""

from email.policy import default
import re
from typing import Any, Dict, List, Optional

import pandas as pd

from .ms_biochem_utils import MSBiochemUtils

# Module-level constants
compartment_types = {
    "cytosol":"c",
    "extracellar":"e",
    "extracellular":"e",
    "extraorganism":"e",
    "periplasm":"p",
    "membrane":"m",
    "mitochondria":"m",
    "environment":"e",
    "env":"e",
    "c":"c",
    "p":"p",
    "e":"e",
    "m":"m"
}

direction_conversion = {
    "":"-",
    "forward": ">",
    "reverse": "<",
    "reversible": "=",
    "uncertain": "?",
    "blocked":"B"
}


class ModelStandardizationUtils(MSBiochemUtils):
    """Utilities for standardizing models and matching to ModelSEED namespace.

    This class provides methods for:
    - Matching model compounds to ModelSEED compounds
    - Matching model reactions to ModelSEED reactions
    - Translating model IDs iteratively to ModelSEED IDs
    - Comparing models to reference ModelSEED models
    - Removing periplasm compartments
    """

    def __init__(self, **kwargs: Any) -> None:
        """Initialize model standardization utilities.

        Args:
            **kwargs: Additional keyword arguments passed to MSBiochemUtils
        """
        super().__init__(**kwargs)

    def _parse_id(self, object_or_id):
        """Parse a compound or reaction ID to extract base ID, compartment, and index.

        Supports two notation styles:
        - Bracket notation: "adp[c]", "h[e]", "cpd00001[c]"
        - Underscore notation: "cpd01024_c0", "rxn00001_c"

        Args:
            object_or_id: Either a string ID or an object with an .id attribute

        Returns:
            Tuple of (base_id, compartment, index) where:
            - base_id: The compound/reaction ID without compartment
            - compartment: Single letter compartment code (c, e, p, m)
            - index: Compartment index (usually "" or "0")
        """
        # Check if input is a string or object and if it's an object, set id to object.id
        if isinstance(object_or_id, str):
            id = object_or_id
        else:
            id = object_or_id.id

        # Try bracket notation first (e.g., "adp[c]" or "h[e]")
        bracket_match = re.search(r"(.+)\[([a-zA-Z]+)\]$", id)
        if bracket_match:
            baseid = bracket_match[1]
            compartment = bracket_match[2]
            index = ""  # Bracket notation doesn't have index
            if compartment.lower() not in compartment_types:
                self.log_warning(f"Compartment type '{compartment}' not recognized in bracket notation. Using default 'c'.")
                compartment = "c"
            else:
                compartment = compartment_types[compartment.lower()]
            return (baseid, compartment, index)

        # Try underscore notation (e.g., "cpd01024_c0")
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
                # Standardizing the compartment when it's recognizable
                compartment = compartment_types[compartment.lower()]
            return (baseid, compartment, index)

        # If no compartment notation found, default to cytosol "c"
        self.log_warning(f"ID '{id}' cannot be parsed - using ID as base and defaulting to compartment 'c'")
        return (id, "c", "")

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
                elif anno_type.lower() in ["smiles","inchi","structure","inchikey"]:
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
        self, model_or_mdlutl, template="gp", create_dataframe=True,msmodel=None,filter_based_on_template=True,cpd_match_hits=None
    ):  
        """Searching all reactions in a model against the ModelSEEDDatabase and a template"""
        EC_PATTERN = re.compile(r'^(?:EC\s*)?(?:[1-7])\.(?:\d+|-)\.(?:\d+|-)\.(?:\d+|-)$', re.I)
        #Getting template
        if isinstance(template, str):
            template = self.get_template(template)
        mdlutl = self._check_and_convert_model(model_or_mdlutl)
        results = {"cpd_matches": {}, "rxn_matches": {}, "rxndf": None, "cpddf": None}
        if cpd_match_hits is None:
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
                    # Initialize gene matching fields (will be populated if msmodel provided)
                    results["rxn_matches"][rxn.id][hit]["msmodel"] = "None"
                    results["rxn_matches"][rxn.id][hit]["gene_mismatches"] = []
                    results["rxn_matches"][rxn.id][hit]["gene_matches"] = []
                    results["rxn_matches"][rxn.id][hit]["ms_gene_mismatches"] = []
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

    def check_for_perfect_matches(self, matches, cpd_translations):
        """Check for perfect matches in reaction matching results.

        Evaluates both equation-based matches and transport reaction matches.
        A perfect match is one where all compounds match (or only H+ differs).

        Args:
            matches: Dict of {ms_rxn_id: match_info} from reaction search
            cpd_translations: Dict of {model_cpd_id: [ms_cpd_id, info]} translations

        Returns:
            [ms_rxn_id, match_type] if a single best match found, None otherwise
        """
        output = None
        perfect_matches = {}

        for ms_rxn_id, match_info in matches.items():
            is_perfect = False
            match_type = "equation"

            # Check equation-based matches
            if "equation_scores" in match_info and len(match_info["equation_scores"]) > 3:
                unmatched = match_info["equation_scores"][3]
                if len(unmatched) == 0:
                    is_perfect = True
                elif len(unmatched) == 1:
                    # Allow if only H+ is unmatched
                    unmatched_id = list(unmatched.keys())[0]
                    if unmatched_id in cpd_translations and cpd_translations[unmatched_id][0] == "cpd00067":
                        is_perfect = True
                    elif unmatched_id == "h" or unmatched_id == "cpd00067":
                        is_perfect = True

            # Check transport reaction matches
            if "transport_scores" in match_info and len(match_info["transport_scores"]) > 0:
                transport_scores = match_info["transport_scores"]
                # transport_scores format: [match_fraction, num_compounds, direction_match, unmatched_substrates, unmatched_products]
                if len(transport_scores) >= 3:
                    match_fraction = transport_scores[0]
                    # Perfect transport match: fraction is 1.0 (all compounds matched)
                    if match_fraction == 1.0:
                        # Check for unmatched compounds in positions 3 and 4 if they exist
                        has_unmatched = False
                        if len(transport_scores) > 3 and transport_scores[3] and len(transport_scores[3]) > 0:
                            has_unmatched = True
                        if len(transport_scores) > 4 and transport_scores[4] and len(transport_scores[4]) > 0:
                            has_unmatched = True
                        if not has_unmatched:
                            is_perfect = True
                            match_type = "transport"

            if is_perfect:
                perfect_matches[ms_rxn_id] = match_info

        if len(perfect_matches) == 1:
            ms_rxn_id = list(perfect_matches.keys())[0]
            output = [ms_rxn_id, "only_match"]
        elif len(perfect_matches) > 1:
            # Multiple perfect matches - pick the best one based on cpd translations, score, and template
            best_hit = None
            least_mismatches = None
            most_matches = None
            best_score = None
            best_template = None

            for ms_rxn_id, match_info in perfect_matches.items():
                cpd_translation_match = 0
                cpd_mismatch = 0

                # Check equation_scores for compound matches
                if "equation_scores" in match_info and len(match_info["equation_scores"]) > 4:
                    for model_cpd_id in match_info["equation_scores"][4]:
                        ms_cpd_id = match_info["equation_scores"][4][model_cpd_id]
                        if model_cpd_id in cpd_translations:
                            if isinstance(cpd_translations[model_cpd_id], list):
                                if cpd_translations[model_cpd_id][0] == ms_cpd_id:
                                    cpd_translation_match += 1
                                else:
                                    cpd_mismatch += 1
                            elif cpd_translations[model_cpd_id] == ms_cpd_id:
                                cpd_translation_match += 1
                            else:
                                cpd_mismatch += 1
                        else:
                            cpd_mismatch += 1

                # Get the original match score for tiebreaking
                current_score = matches[ms_rxn_id].get("score", 0)

                # Scoring logic: prefer fewer mismatches, then more matches, then higher score, then template
                if least_mismatches is None or cpd_mismatch < least_mismatches:
                    least_mismatches = cpd_mismatch
                    most_matches = cpd_translation_match
                    best_score = current_score
                    best_hit = ms_rxn_id
                    best_template = matches[ms_rxn_id].get("template", False)
                elif cpd_mismatch == least_mismatches:
                    if most_matches is None or cpd_translation_match > most_matches:
                        most_matches = cpd_translation_match
                        best_score = current_score
                        best_hit = ms_rxn_id
                        best_template = matches[ms_rxn_id].get("template", False)
                    elif cpd_translation_match == most_matches:
                        # Use match score as tiebreaker
                        if best_score is None or current_score > best_score:
                            best_score = current_score
                            best_hit = ms_rxn_id
                            best_template = matches[ms_rxn_id].get("template", False)
                        elif current_score == best_score:
                            # Prefer template reactions as final tiebreaker
                            if matches[ms_rxn_id].get("template", False) and not best_template:
                                best_hit = ms_rxn_id
                                best_template = True

            if best_hit:
                output = [best_hit, str(most_matches or 0) + "/" + str(least_mismatches or 0)]

        return output

    def analyze_compound_matches(self, matches):
        strong_matches = []
        formula_only_matches = []
        for ms_cpd_id, match_info in matches.items():
            has_id_match = match_info.get("identifier_hits", []) and len(match_info["identifier_hits"]) > 0
            has_structure_match = match_info.get("structure_hits", []) and len(match_info["structure_hits"]) > 0
            has_formula_match = match_info.get("formula_hits", []) and len(match_info["formula_hits"]) > 0
            if has_id_match or has_structure_match:
                strong_matches.append(ms_cpd_id)
            elif has_formula_match:
                formula_only_matches.append(ms_cpd_id)
        return strong_matches, formula_only_matches
    
    
    def translate_model_to_ms_namespace(
        self,
        model_or_mdlutl,
        template="gn",
        remove_periplasm=True,
        max_iterations=10
    ):
        """Translate model compound and reaction IDs to ModelSEED namespace iteratively.

        This function performs an iterative translation of model compounds and reactions
        to ModelSEED IDs, starting with unique matches and progressively handling
        ambiguous cases.

        Args:
            model_or_mdlutl: Model or MSModelUtil object to translate
            template: Template to use for matching (default: "gn")
            remove_periplasm: Whether to remove periplasm compartment first (default: True)
            max_iterations: Maximum number of matching iterations (default: 10)

        Returns:
            Dict containing:
                - cpd_translations: Final compound ID mappings {model_id: ms_id}
                - rxn_translations: Final reaction ID mappings {model_id: (ms_id, direction)}
                - proposed_matches: Ambiguous matches for user review
                - iteration_log: Log of what happened in each iteration
                - match_stats: Statistics about the translation process
        """
        mdlutl = self._check_and_convert_model(model_or_mdlutl)

        # Step 1: Optionally remove periplasm compartment
        if remove_periplasm:
            self.log_info("Removing periplasm compartment")
            mdlutl = self.remove_model_periplasm_compartment(mdlutl)

        # Step 2: Get template
        if isinstance(template, str):
            template = self.get_template(template)

        # Step 3: Match compounds
        cpd_matches = self.match_model_compounds_to_db(
            mdlutl,
            template=template,
            filter_based_on_template=True,
            create_dataframe=False
        )
        cpd_matches = cpd_matches["matches"]
        #Removing formula-only matches when strong matches exist
        for model_cpd_id, matches in cpd_matches.items():
            if matches is not None and len(matches) > 1:
                strong_matches, formula_only_matches = self.analyze_compound_matches(matches)

                # If we have strong matches, remove formula-only matches from cpd_matches
                if len(strong_matches) > 0 and len(formula_only_matches) > 0:
                    for formula_cpd_id in formula_only_matches:
                        del matches[formula_cpd_id]
                    self.log_info(
                        f"Compound {model_cpd_id}: Removed {len(formula_only_matches)} formula-only matches, "
                        f"keeping {len(strong_matches)} strong matches"
                    )

        # Step 4: Match reactions
        # Initialize tracking structures  # {model_cpd_id: ms_cpd_id}
        rxn_translations = {}  # {model_rxn_id: (ms_rxn_id, direction)}
        cpd_translations = {}
        conflicts = {}
        iteration_log = []
        iterate = True
        while (iterate == True):
            new_cpd_matchs = 0
            new_rxn_matchs = 0
            for model_cpd_id, matches in cpd_matches.items():
                if matches is not None and len(matches) >= 1:
                    strong_matches, formula_only_matches = self.analyze_compound_matches(matches)
                    if len(strong_matches) == 1:
                       if model_cpd_id not in cpd_translations:
                           cpd_translations[model_cpd_id] = [strong_matches[0],{"only strong match": True,"equation_match": []}]
                           cpd_translations[matches[strong_matches[0]]["base_id"]] = [strong_matches[0],{"only strong match": True,"equation_match": []}]
                           new_cpd_matchs += 1
            rxn_translation_count = 0
            rxn_matches = self.match_model_reactions_to_db(
                mdlutl,
                template=template,
                filter_based_on_template=True,
                cpd_match_hits={"matches":cpd_matches,"df":None},
                create_dataframe=False
            )
            rxn_matches = rxn_matches["rxn_matches"]
            #Removing formula-only matches when strong matches exist
            for model_rxn_id, matches in rxn_matches.items():
                if model_rxn_id not in rxn_translations and matches is not None:
                    output = self.check_for_perfect_matches(matches,cpd_translations)
                    if output is not None:
                        new_rxn_matchs += 1
                        rxn_translations[model_rxn_id] = output
                        match_info = matches[output[0]]
                        if "equation_scores" in match_info and len(match_info["equation_scores"]) > 4:
                            for model_cpd_id in match_info["equation_scores"][4]:
                                if model_cpd_id not in cpd_translations:
                                    cpd_translations[model_cpd_id] = match_info["equation_scores"][4][model_cpd_id]
                                    new_cpd_matchs += 1
                                    #Removing all other matching compounds to reduce ambiguity
                                    if model_cpd_id in cpd_matches:
                                        mdlcpdmatches = cpd_matches[model_cpd_id]
                                        for mdlcpd_id, mdlcpd_matches in mdlcpdmatches.items():
                                            if mdlcpd_id != match_info["equation_scores"][4][model_cpd_id]:
                                                del mdlcpdmatches[mdlcpd_id]
            if (new_cpd_matchs == 0 and new_rxn_matchs == 0):
                iterate = False
        return (cpd_translations,rxn_translations,cpd_matches,rxn_matches)

    def apply_translation_to_model(
        self,
        model_or_mdlutl,
        translation_results,
        user_approved_matches=None,
        organism_indices=None
    ):
        """Apply ModelSEED namespace translations to a model.

        This function renames compounds and reactions in the model based on
        the translation results from translate_model_to_ms_namespace.

        Naming conventions:
        - Metabolites: h[c] -> cpd00067_c0, h[env] -> cpd00067_e0
        - Reactions: ANME_1 -> rxn05467_c1, SRB_3 -> rxn08173_c2
        - Compartment index is based on organism (1 for ANME, 2 for SRB by default)
        - Stoichiometry is NOT changed - only IDs are updated

        Args:
            model_or_mdlutl: Model or MSModelUtil object to modify
            translation_results: Tuple from translate_model_to_ms_namespace:
                (cpd_translations, rxn_translations, cpd_matches, rxn_matches)
            user_approved_matches: Optional dict of user-approved proposed matches
                {model_rxn_id: [ms_rxn_id, match_type]}
            organism_indices: Dict mapping organism prefix to compartment index
                Default: {"ANME": "1", "SRB": "2"}

        Returns:
            Dict with statistics about what was changed
        """
        mdlutl = self._check_and_convert_model(model_or_mdlutl)

        # Handle both tuple format (from translate_model_to_ms_namespace) and dict format
        if isinstance(translation_results, tuple):
            cpd_translations = translation_results[0]
            rxn_translations = translation_results[1]
        else:
            cpd_translations = translation_results.get("cpd_translations", translation_results.get(0, {}))
            rxn_translations = translation_results.get("rxn_translations", translation_results.get(1, {}))

        # Default organism indices
        if organism_indices is None:
            organism_indices = {"ANME": "1", "SRB": "2"}

        # Add user-approved matches if provided
        if user_approved_matches:
            for model_rxn_id, match_info in user_approved_matches.items():
                if isinstance(match_info, (list, tuple)):
                    rxn_translations[model_rxn_id] = match_info
                else:
                    rxn_translations[model_rxn_id] = [match_info, "user_approved"]

        # Count total compounds and reactions in model for fraction calculation
        total_compounds = len(mdlutl.model.metabolites)
        total_reactions = len(mdlutl.model.reactions)

        stats = {
            "compounds_renamed": 0,
            "reactions_renamed": 0,
            "reactions_reversed": 0,
            "compound_mapping": {},
            "reaction_mapping": {},
            "total_compounds": total_compounds,
            "total_reactions": total_reactions,
            "compound_fraction": 0.0,
            "reaction_fraction": 0.0,
            "untranslated_compounds": [],
            "untranslated_reactions": []
        }

        # Track which metabolites and reactions have translations
        translated_cpd_ids = set()
        translated_rxn_ids = set()

        # Map compartments to ModelSEED style
        compartment_map = {
            "c": "c",
            "e": "e",
            "p": "p",
            "m": "m",
            "env": "e",  # environment -> extracellular
        }

        # Apply compound translations
        self.log_info("Applying compound translations")

        # First pass: build mapping of new IDs to handle duplicates
        new_id_to_originals = {}
        for model_cpd_id in cpd_translations:
            if model_cpd_id not in mdlutl.model.metabolites:
                continue

            cpd = mdlutl.model.metabolites.get_by_id(model_cpd_id)
            [base_id, compartment, index] = self._parse_id(cpd)

            # Get the ModelSEED compound ID
            ms_cpd_info = cpd_translations[model_cpd_id]
            if isinstance(ms_cpd_info, list):
                ms_cpd_id = ms_cpd_info[0]
            else:
                ms_cpd_id = ms_cpd_info

            # Map compartment to ModelSEED style
            ms_compartment = compartment_map.get(compartment.lower(), compartment)

            # Build new ID: cpd00067_c0 (compartment index is 0 for metabolites)
            new_cpd_id = f"{ms_cpd_id}_{ms_compartment}0"

            if new_cpd_id not in new_id_to_originals:
                new_id_to_originals[new_cpd_id] = []
            new_id_to_originals[new_cpd_id].append(model_cpd_id)

        # Second pass: rename compounds
        for model_cpd_id in cpd_translations:
            if model_cpd_id not in mdlutl.model.metabolites:
                continue

            cpd = mdlutl.model.metabolites.get_by_id(model_cpd_id)
            [base_id, compartment, index] = self._parse_id(cpd)

            ms_cpd_info = cpd_translations[model_cpd_id]
            if isinstance(ms_cpd_info, list):
                ms_cpd_id = ms_cpd_info[0]
            else:
                ms_cpd_id = ms_cpd_info

            ms_compartment = compartment_map.get(compartment.lower(), compartment)
            new_cpd_id = f"{ms_cpd_id}_{ms_compartment}0"

            # Handle duplicates
            originals_for_new_id = new_id_to_originals.get(new_cpd_id, [])
            if len(originals_for_new_id) > 1 and model_cpd_id != originals_for_new_id[0]:
                self.log_warning(
                    f"Skipping rename of '{model_cpd_id}' to '{new_cpd_id}' - "
                    f"already renamed '{originals_for_new_id[0]}' to this ID"
                )
                continue

            if new_cpd_id in mdlutl.model.metabolites and new_cpd_id != model_cpd_id:
                self.log_warning(
                    f"Skipping rename of '{model_cpd_id}' to '{new_cpd_id}' - "
                    f"ID already exists in model"
                )
                continue

            # Rename the compound
            old_id = cpd.id
            cpd.id = new_cpd_id
            cpd.compartment = ms_compartment
            stats["compounds_renamed"] += 1
            stats["compound_mapping"][old_id] = new_cpd_id
            translated_cpd_ids.add(old_id)

        # Track untranslated compounds
        # After renaming, any metabolite whose ID is NOT in compound_mapping.values()
        # (the new ModelSEED IDs) is untranslated
        translated_new_ids = set(stats["compound_mapping"].values())
        for met in mdlutl.model.metabolites:
            if met.id not in translated_new_ids:
                stats["untranslated_compounds"].append(met.id)

        # Apply reaction translations
        self.log_info("Applying reaction translations")
        for model_rxn_id, rxn_info in rxn_translations.items():
            if model_rxn_id not in mdlutl.model.reactions:
                continue

            rxn = mdlutl.model.reactions.get_by_id(model_rxn_id)

            # Get the ModelSEED reaction ID
            if isinstance(rxn_info, list):
                ms_rxn_id = rxn_info[0]
            else:
                ms_rxn_id = rxn_info

            # Determine organism index from reaction ID prefix
            org_index = "0"
            for prefix, idx in organism_indices.items():
                if model_rxn_id.startswith(prefix):
                    org_index = idx
                    break

            # Determine the primary compartment for the reaction
            # Use 'c' (cytosol) as default, but check reaction metabolites
            primary_compartment = "c"
            compartments_in_rxn = set()
            for met in rxn.metabolites:
                [_, met_comp, _] = self._parse_id(met)
                compartments_in_rxn.add(met_comp)

            # If reaction only has cytosolic metabolites, use 'c'
            # If it's a transport reaction (multiple compartments), still use 'c' as base
            if "c" in compartments_in_rxn:
                primary_compartment = "c"
            elif len(compartments_in_rxn) == 1:
                primary_compartment = list(compartments_in_rxn)[0]

            # Build new reaction ID: rxn05467_c1 (compartment + organism index)
            new_rxn_id = f"{ms_rxn_id}_{primary_compartment}{org_index}"

            # Check for duplicate reaction IDs
            if new_rxn_id in mdlutl.model.reactions and new_rxn_id != model_rxn_id:
                self.log_warning(
                    f"Skipping rename of '{model_rxn_id}' to '{new_rxn_id}' - "
                    f"ID already exists in model"
                )
                continue

            # Rename the reaction (DO NOT change stoichiometry)
            old_id = rxn.id
            rxn.id = new_rxn_id
            stats["reactions_renamed"] += 1
            stats["reaction_mapping"][old_id] = new_rxn_id
            translated_rxn_ids.add(old_id)

        # Track untranslated reactions
        # After renaming, any reaction whose ID is NOT in reaction_mapping.values()
        # (the new ModelSEED IDs) is untranslated
        translated_rxn_new_ids = set(stats["reaction_mapping"].values())
        for rxn in mdlutl.model.reactions:
            if rxn.id not in translated_rxn_new_ids:
                stats["untranslated_reactions"].append(rxn.id)

        # Calculate fractions
        if total_compounds > 0:
            stats["compound_fraction"] = stats["compounds_renamed"] / total_compounds
        if total_reactions > 0:
            stats["reaction_fraction"] = stats["reactions_renamed"] / total_reactions

        self.log_info(
            f"Translation applied: {stats['compounds_renamed']}/{total_compounds} compounds "
            f"({stats['compound_fraction']*100:.1f}%), "
            f"{stats['reactions_renamed']}/{total_reactions} reactions "
            f"({stats['reaction_fraction']*100:.1f}%)"
        )

        if stats["untranslated_reactions"]:
            self.log_info(f"Untranslated reactions: {stats['untranslated_reactions']}")

        return stats
