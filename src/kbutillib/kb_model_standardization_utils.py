"""KBase model standardization utilities for matching and translating models to ModelSEED namespace.

This module provides utilities for:
- Matching model compounds and reactions to ModelSEED database
- Translating model IDs to ModelSEED namespace
- Comparing models to ModelSEED models
- Standardizing model structure and compartments
"""

import re
from typing import Any, Dict, List, Optional

import pandas as pd

from .ms_biochem_utils import MSBiochemUtils

# Module-level constants
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

        # Step 3: Match compounds and reactions
        self.log_info("Matching compounds and reactions to database")
        match_results = self.match_model_reactions_to_db(
            mdlutl,
            template=template,
            filter_based_on_template=True
        )

        cpd_matches = match_results["cpd_matches"]
        rxn_matches = match_results["rxn_matches"]

        # Initialize tracking structures
        cpd_translations = {}  # {model_cpd_id: ms_cpd_id}
        rxn_translations = {}  # {model_rxn_id: (ms_rxn_id, direction)}
        iteration_log = []

        # Step 4: Translate compounds with unique matches
        self.log_info("Step 1: Translating compounds with unique matches")
        unique_cpd_count = 0
        for model_cpd_id, matches in cpd_matches.items():
            if len(matches) == 1:
                ms_cpd_id = list(matches.keys())[0]
                cpd_translations[model_cpd_id] = ms_cpd_id
                unique_cpd_count += 1

        iteration_log.append({
            "iteration": 0,
            "type": "unique_compounds",
            "compounds_translated": unique_cpd_count,
            "reactions_translated": 0
        })
        self.log_info(f"Translated {unique_cpd_count} compounds with unique matches")

        # Step 5: Iterative reaction matching
        iteration = 1
        while iteration <= max_iterations:
            self.log_info(f"Iteration {iteration}: Matching perfect reactions")

            rxns_translated_this_iteration = 0
            cpds_translated_this_iteration = 0

            for model_rxn_id, matches in rxn_matches.items():
                # Skip if already translated
                if model_rxn_id in rxn_translations:
                    continue

                # Get the model reaction
                model_rxn = mdlutl.model.reactions.get_by_id(model_rxn_id)

                # Try to find a perfect match
                perfect_match = self._find_perfect_reaction_match(
                    model_rxn,
                    matches,
                    cpd_translations,
                    template
                )

                if perfect_match:
                    ms_rxn_id, direction, new_cpd_mappings = perfect_match
                    rxn_translations[model_rxn_id] = (ms_rxn_id, direction)
                    rxns_translated_this_iteration += 1

                    # Add any new compound translations discovered
                    for model_cpd_id, ms_cpd_id in new_cpd_mappings.items():
                        if model_cpd_id not in cpd_translations:
                            cpd_translations[model_cpd_id] = ms_cpd_id
                            cpds_translated_this_iteration += 1

            iteration_log.append({
                "iteration": iteration,
                "type": "perfect_reactions",
                "compounds_translated": cpds_translated_this_iteration,
                "reactions_translated": rxns_translated_this_iteration
            })

            self.log_info(
                f"Iteration {iteration}: Translated {rxns_translated_this_iteration} reactions, "
                f"{cpds_translated_this_iteration} new compounds"
            )

            # Stop if no progress made
            if rxns_translated_this_iteration == 0:
                break

            iteration += 1

        # Step 6: Identify proposed matches for remaining reactions
        self.log_info("Identifying proposed matches for remaining reactions")
        proposed_matches = self._identify_proposed_matches(
            mdlutl,
            rxn_matches,
            rxn_translations,
            cpd_translations,
            cpd_matches,
            template
        )

        # Step 7: Compute statistics
        match_stats = {
            "total_compounds": len(mdlutl.model.metabolites),
            "compounds_translated": len(cpd_translations),
            "compounds_with_matches": len(cpd_matches),
            "total_reactions": len([r for r in mdlutl.model.reactions if r.id[0:3] not in self.const_util_rxn_prefixes()]),
            "reactions_translated": len(rxn_translations),
            "reactions_with_matches": len(rxn_matches),
            "proposed_matches_count": len(proposed_matches),
            "iterations_used": iteration - 1
        }

        self.log_info(
            f"Translation complete: {match_stats['compounds_translated']}/{match_stats['total_compounds']} "
            f"compounds, {match_stats['reactions_translated']}/{match_stats['total_reactions']} reactions"
        )

        return {
            "cpd_translations": cpd_translations,
            "rxn_translations": rxn_translations,
            "proposed_matches": proposed_matches,
            "iteration_log": iteration_log,
            "match_stats": match_stats,
            "cpd_matches": cpd_matches,
            "rxn_matches": rxn_matches
        }

    def _find_perfect_reaction_match(self, model_rxn, matches, cpd_translations, template):
        """Find a perfect reaction match from candidates.

        A perfect match means:
        - All reactants match (either forward or reverse)
        - All stoichiometric coefficients match
        - Transport reactions match (same number of compartments)

        Args:
            model_rxn: The model reaction object
            matches: Dict of potential MS reaction matches
            cpd_translations: Current compound translations
            template: Model template

        Returns:
            Tuple of (ms_rxn_id, direction, new_cpd_mappings) or None if no perfect match
        """
        # Parse model reaction stoichiometry
        model_stoich = {}
        model_compartments = set()
        for met, coef in model_rxn.metabolites.items():
            [base_id, compartment, index] = self._parse_id(met)
            model_stoich[met.id] = coef
            model_compartments.add(compartment)

        is_transport = len(model_compartments) > 1

        # Try each potential match
        for ms_rxn_id, match_info in matches.items():
            # Get the MS reaction
            ms_rxn = self.biochem_db.reactions.get_by_id(ms_rxn_id)

            # Check both forward and reverse directions
            for direction in ["forward", "reverse"]:
                result = self._check_reaction_stoich_match(
                    model_rxn,
                    ms_rxn,
                    cpd_translations,
                    direction,
                    is_transport
                )

                if result and result["is_perfect"]:
                    return (ms_rxn_id, direction, result["new_cpd_mappings"])

        return None

    def _check_reaction_stoich_match(self, model_rxn, ms_rxn, cpd_translations, direction, is_transport):
        """Check if a model reaction matches an MS reaction stoichiometrically.

        Args:
            model_rxn: Model reaction object
            ms_rxn: ModelSEED reaction object
            cpd_translations: Current compound translations
            direction: "forward" or "reverse"
            is_transport: Whether the reaction is a transport reaction

        Returns:
            Dict with is_perfect flag and new_cpd_mappings, or None if not a match
        """
        # Get MS reaction stoichiometry
        ms_stoich = {}
        ms_compartments = set()
        for met, coef in ms_rxn.metabolites.items():
            [base_id, compartment, index] = self._parse_id(met)
            if direction == "reverse":
                coef = -coef
            ms_stoich[base_id] = (coef, compartment)
            ms_compartments.add(compartment)

        # Check transport consistency
        ms_is_transport = len(ms_compartments) > 1
        if is_transport != ms_is_transport:
            return None

        # Try to match each model metabolite
        new_cpd_mappings = {}
        for model_met, model_coef in model_rxn.metabolites.items():
            [model_base_id, model_compartment, model_index] = self._parse_id(model_met)

            # Check if we already have a translation
            if model_met.id in cpd_translations:
                ms_cpd_id = cpd_translations[model_met.id]
                ms_base_id = ms_cpd_id.rsplit("_", 1)[0] if "_" in ms_cpd_id else ms_cpd_id

                # Check if this compound is in the MS reaction with correct stoichiometry
                if ms_base_id not in ms_stoich:
                    return None

                ms_coef, ms_comp = ms_stoich[ms_base_id]
                if abs(model_coef - ms_coef) > 1e-6:  # Allow small floating point differences
                    return None

                # For transport, compartments don't need to match exactly,
                # but we track the mapping
                if not is_transport and model_compartment != ms_comp:
                    return None
            else:
                # No translation yet - need to find a match
                # Look for an MS compound in the reaction with matching stoichiometry
                found_match = False
                for ms_base_id, (ms_coef, ms_comp) in ms_stoich.items():
                    if abs(model_coef - ms_coef) < 1e-6:
                        # Potential match - construct full MS compound ID
                        ms_cpd_id = f"{ms_base_id}_{model_compartment}0"
                        new_cpd_mappings[model_met.id] = ms_cpd_id
                        found_match = True
                        break

                if not found_match:
                    return None

        # Check that all MS metabolites are accounted for
        model_base_ids = set()
        for model_met in model_rxn.metabolites:
            if model_met.id in cpd_translations:
                ms_cpd_id = cpd_translations[model_met.id]
                ms_base_id = ms_cpd_id.rsplit("_", 1)[0] if "_" in ms_cpd_id else ms_cpd_id
                model_base_ids.add(ms_base_id)

        for ms_cpd_id in new_cpd_mappings.values():
            ms_base_id = ms_cpd_id.rsplit("_", 1)[0] if "_" in ms_cpd_id else ms_cpd_id
            model_base_ids.add(ms_base_id)

        if len(model_base_ids) != len(ms_stoich):
            return None

        return {
            "is_perfect": True,
            "new_cpd_mappings": new_cpd_mappings
        }

    def _identify_proposed_matches(self, mdlutl, rxn_matches, rxn_translations, cpd_translations, cpd_matches, template):
        """Identify proposed matches for reactions that don't have perfect matches.

        This prioritizes matches that:
        1. Are in the model template
        2. Have high match scores
        3. Don't have unmatchable compounds

        Args:
            mdlutl: Model utility object
            rxn_matches: All reaction matches from match_model_reactions_to_db
            rxn_translations: Already translated reactions
            cpd_translations: Already translated compounds
            cpd_matches: All compound matches
            template: Model template

        Returns:
            List of proposed matches with analysis
        """
        proposed = []

        for model_rxn_id, matches in rxn_matches.items():
            # Skip already translated reactions
            if model_rxn_id in rxn_translations:
                continue

            model_rxn = mdlutl.model.reactions.get_by_id(model_rxn_id)

            # Check if any reactants are completely unmatchable
            has_unmatchable = False
            for met in model_rxn.metabolites:
                if met.id not in cpd_matches or len(cpd_matches[met.id]) == 0:
                    has_unmatchable = True
                    break

            if has_unmatchable:
                continue

            # Find best match, prioritizing template matches
            best_match = None
            best_score = -float('inf')

            for ms_rxn_id, match_info in matches.items():
                # Bonus for being in template
                score = match_info["score"]
                if match_info.get("template", False):
                    score += 50

                if score > best_score:
                    best_score = score
                    best_match = (ms_rxn_id, match_info)

            if best_match:
                ms_rxn_id, match_info = best_match
                proposed.append({
                    "model_rxn_id": model_rxn_id,
                    "model_equation": model_rxn.build_reaction_string(use_metabolite_names=True),
                    "proposed_ms_rxn_id": ms_rxn_id,
                    "ms_equation": match_info["match_equation"],
                    "score": best_score,
                    "in_template": match_info.get("template", False),
                    "match_info": match_info
                })

        # Sort by score descending
        proposed.sort(key=lambda x: x["score"], reverse=True)

        return proposed

    def apply_translation_to_model(
        self,
        model_or_mdlutl,
        translation_results,
        user_approved_matches=None
    ):
        """Apply ModelSEED namespace translations to a model.

        This function renames compounds and reactions in the model based on
        the translation results from translate_model_to_ms_namespace.

        Args:
            model_or_mdlutl: Model or MSModelUtil object to modify
            translation_results: Results dict from translate_model_to_ms_namespace
            user_approved_matches: Optional dict of user-approved proposed matches
                                   {model_rxn_id: (ms_rxn_id, direction)}

        Returns:
            Dict with statistics about what was changed
        """
        mdlutl = self._check_and_convert_model(model_or_mdlutl)

        cpd_translations = translation_results["cpd_translations"]
        rxn_translations = translation_results["rxn_translations"]

        # Add user-approved matches if provided
        if user_approved_matches:
            for model_rxn_id, (ms_rxn_id, direction) in user_approved_matches.items():
                rxn_translations[model_rxn_id] = (ms_rxn_id, direction)

        stats = {
            "compounds_renamed": 0,
            "reactions_renamed": 0,
            "reactions_reversed": 0
        }

        # Apply compound translations
        self.log_info("Applying compound translations")
        for model_cpd_id, ms_cpd_id in cpd_translations.items():
            if model_cpd_id in mdlutl.model.metabolites:
                cpd = mdlutl.model.metabolites.get_by_id(model_cpd_id)

                # Parse the original compound to get compartment and index
                [base_id, compartment, index] = self._parse_id(cpd)

                # Construct new compound ID with compartment
                # ms_cpd_id is just the base ID (e.g., "cpd01024")
                # We need to add compartment suffix (e.g., "cpd01024_c0")
                new_cpd_id = f"{ms_cpd_id}_{compartment}{index if index else '0'}"

                cpd.id = new_cpd_id
                stats["compounds_renamed"] += 1

        # Apply reaction translations
        self.log_info("Applying reaction translations")
        for model_rxn_id, (ms_rxn_id, direction) in rxn_translations.items():
            if model_rxn_id not in mdlutl.model.reactions:
                continue

            rxn = mdlutl.model.reactions.get_by_id(model_rxn_id)

            # Parse model reaction compartment
            [base_id, compartment, index] = self._parse_id(rxn)

            # Construct new reaction ID with compartment
            new_rxn_id = f"{ms_rxn_id}_{compartment}{index if index else '0'}"

            # Reverse reaction if needed
            if direction == "reverse":
                # Reverse the reaction stoichiometry
                new_metabolites = {}
                for met, coef in rxn.metabolites.items():
                    new_metabolites[met] = -coef
                rxn.subtract_metabolites(rxn.metabolites)
                rxn.add_metabolites(new_metabolites)

                # Swap bounds
                rxn.lower_bound, rxn.upper_bound = -rxn.upper_bound, -rxn.lower_bound

                stats["reactions_reversed"] += 1

            # Rename the reaction
            rxn.id = new_rxn_id
            stats["reactions_renamed"] += 1

        self.log_info(
            f"Translation applied: {stats['compounds_renamed']} compounds, "
            f"{stats['reactions_renamed']} reactions "
            f"({stats['reactions_reversed']} reversed)"
        )

        return stats
