"""KBase model utilities for constraint-based metabolic modeling."""

import pickle
from typing import Any, Dict
import pandas as pd
import re
import json

from cobra.flux_analysis import flux_variability_analysis
from cobra.flux_analysis import pfba 

from .argo_utils import ArgoUtils

class AICurationUtils(ArgoUtils):
    """Tools for running a wide range of FBA analysis on metabolic models in KBase
    """

    def __init__(self, **kwargs: Any) -> None:
        """Initialize MS model utilities

        Args:
            **kwargs: Additional keyword arguments passed to SharedEnvironment
        """
        super().__init__(**kwargs)

    def _load_cached_curation(self,cache_name) -> dict[str, Any]:
        """Load cached curation data"""
        cache = self.load_util_data("AICurationCache"+cache_name,default={})
        return cache
    
    def _save_cached_curation(self,cache_name,cache) -> None:
        """Save cached curation data"""
        self.save_util_data("AICurationCache"+cache_name,cache)

    def analyze_reaction_directionality(self, rxn) -> dict[str, Any]:
        """Use AI to analyze reaction directionality for an input reaction"""
        system = """
        You are an expert in biochemistry and molecular biology. 
        You will receive a biochemical reaction and must evaluate it for stoichiometric 
        correctness and biological directionality.

        Respond strictly in valid JSON with **no text outside the JSON**. 
        All keys and string values must use double quotes. 
        Use only plain ASCII characters.
        """

        shared_prompt = """Analyze the following reaction for stoichiometric correctness and 
        directionality in vivo. 

        Return a JSON object in this exact format:

        {
        "errors": ["error 1", "error 2"],
        "directionality": "forward|reverse|reversible|uncertain",
        "other_comments": "Brief general comments about the reaction so I know you understood the input.",
        "confidence": "high|medium|low|none"
        }

        Reaction:
        """
        cache = self._load_cached_curation("ReactionDirectionality")
        if rxn.id[0:3] in self.const_util_rxn_prefixes():
            return None
        rxn_output = self.reaction_to_string(rxn)
        if rxn_output["base_id"] not in cache:
            self.log_warning(f"Querying AI with {rxn_output['base_id']}")
            prompt = shared_prompt + rxn_output["rxnstring"]
            ai_output = self.chat(prompt=prompt, system=system)
            cache[rxn_output["base_id"]] = json.loads(ai_output)
            if "reversed" in rxn_output:
                cache[rxn_output["base_id"]]["other_comments"] += " Reaction was inverted to avoid AI confusion, but AI directionality was corrected after the AI analysis was concluded."
                if cache[rxn_output["base_id"]]["directionality"] == "forward":
                    cache[rxn_output["base_id"]]["directionality"] = "reverse"
                elif cache[rxn_output["base_id"]]["directionality"] == "reverse":
                    cache[rxn_output["base_id"]]["directionality"] = "forward"
            self._save_cached_curation("ReactionDirectionality",cache)
        else:
            print("ReactionDirectionality-cached")
        return cache[rxn_output["base_id"]]
        
    def evaluate_reaction_equivalence(self, rxn1,rxn2,comparison_evidence) -> dict[str, Any]:
        """Use AI to analyze reaction directionality for an input reaction"""
        system = """
        You are an expert in biochemistry and molecular biology. 
        You will receive data about two reactions in JSON format, labeled reaction1 and reaction2.
        These reactions will be in two different name spaces, but you can assume that the compounds in each reaction are equivalent based on the evidence provided. 
        Where possible, mappings between the two name spaces will be provided, but the mappings will not be complete.
        Your task is to determine if the two reactions are equivalent based on the compounds and stoichiometry provided.
        You will label the reaction pair from among the following categories:
        "equivalent" - the reactions are equivalent
        "generalization" - rection 1 is a more general version of reaction 2 (e.g. "an alcohol" vs "ethanol")
        "specialization" - reaction 1 is a more specific version of reaction 2 (e.g. "ethanol" vs "an alcohol")
        "related" - the reactions are similar but not equivalent because they operate on slightly different compounds (e.g. "ethanol" vs "methanol")
        "different" - the reactions are not equivalent and not similar
        You will also provide a brief explanation of your reasoning.

        Respond strictly in valid JSON with **no text outside the JSON**. 
        All keys and string values must use double quotes. 
        Use only plain ASCII characters.
        """

        shared_prompt = """Analyze if reaction1 and reaction2 in the following JSON object are equivalent based on all provided data.

        Return a JSON object in this exact format:

        {
        "equivalence": "equivalent|generalization|specialization|related|different",
        "explanation": "Brief explanation of the reasoning behind the equivalence determination."
        }

        JSON data:
        """
        cache = self._load_cached_curation("ReactionEquivalence")
        if rxn1.id[0:3] in self.const_util_rxn_prefixes():
            return None
        if rxn2.id[0:3] in self.const_util_rxn_prefixes():
            return None
        if rxn1.id in cache:
            if rxn2.id in cache[rxn1.id]:
                print("ReactionEquivalence-cached")
                return cache[rxn1.id][rxn2.id]
        cache.setdefault(rxn1.id,{})
        cache[rxn1.id].setdefault(rxn2.id,{})
        rxnhash = {"reaction1":rxn1,"reaction2":rxn2}
        input_data = {"comparison_evidence": comparison_evidence}
        for item in rxnhash:
            input_data[item] = {
                "id": rxnhash[item].id,
                "name": rxnhash[item].name,
                "equation": rxnhash[item].build_reaction_string(use_metabolite_names=True)
            }
            if hasattr(rxnhash[item], "names"):
                for name in rxnhash[item].names:
                    input_data[item].setdefault("other_names",[])
                    input_data[item]["other_names"].append(name)
            for anno_type in rxnhash[item].annotation:
                if isinstance(rxnhash[item].annotation[anno_type], set):
                    for alias in rxnhash[item].annotation[anno_type]:
                        input_data[item].setdefault(anno_type, [])
                        input_data[item][anno_type].append(alias)
        prompt = shared_prompt + json.dumps(input_data,indent=2)
        ai_output = self.chat(prompt=prompt, system=system)
        cache[rxn1.id][rxn2.id] = json.loads(ai_output)
        self._save_cached_curation("ReactionEquivalence",cache)
        return cache[rxn1.id][rxn2.id]
    
    def evaluate_reaction_gene_association(self, rxn,genedata) -> dict[str, Any]:
        """Use AI to analyze reaction directionality for an input reaction"""
        system = """
        You are an expert in biochemistry and molecular biology. 
        You will receive data about one reaction and one gene in JSON format, labeled reaction and gene.
        Your task is to determine if the reaction should be associated with the gene based on the provided data.
        You will label the association from among the following categories:
        "exact" - the reaction is an exact match for the gene's known function
        "related" - the reaction is related with the gene's known function, but not an exact match
        "similar" - the reaction performs a similar reaction but on a different substrate
        "different" - the reaction is not associated with the gene
        "uncertain" - it is uncertain if the reaction is associated with the gene
        You will also provide a brief explanation of your reasoning.

        Respond strictly in valid JSON with **no text outside the JSON**. 
        All keys and string values must use double quotes. 
        Use only plain ASCII characters.
        """

        shared_prompt = """Analyze if reaction should be associated with gene in the following JSON object.

        Return a JSON object in this exact format:

        {
        "association": "exact|related|similar|different|uncertain",
        "explanation": "Brief explanation of the reasoning behind the association determination."
        }

        JSON data:
        """
        cache = self._load_cached_curation("GeneAssociation")
        if rxn.id[0:3] in self.const_util_rxn_prefixes():
            return None
        if rxn.id in cache:
            if genedata["ID"] in cache[rxn.id]:
                print("ReactionGeneAssociation-cached")
                return cache[rxn.id][genedata["ID"]]
        cache.setdefault(rxn.id,{})
        cache[rxn.id].setdefault(genedata["ID"],{})
        input_data = {
            "reaction": {
                "id": rxn.id,
                "name": rxn.name,
                "equation": rxn.build_reaction_string(use_metabolite_names=True)
            },
            "gene": genedata
        }
        if hasattr(rxn, "names"):
            for name in rxn.names:
                input_data["reaction"].setdefault("other_names", [])
                input_data["reaction"]["other_names"].append(name)
        for anno_type in rxn.annotation:
            if isinstance(rxn.annotation[anno_type], set):
                for alias in rxn.annotation[anno_type]:
                    input_data["reaction"].setdefault(anno_type, [])
                    input_data["reaction"][anno_type].append(alias)
        prompt = shared_prompt + json.dumps(input_data,indent=2)
        ai_output = self.chat(prompt=prompt, system=system)
        cache[rxn.id][genedata["ID"]] = json.loads(ai_output)
        self._save_cached_curation("GeneAssociation",cache)
        return cache[rxn.id][genedata["ID"]]

    def analyze_reaction_stoichiometry(self, rxn) -> dict[str, Any]:
        """Use AI to analyze and categorize reaction stoichiometry into primary, cofactor, and minor components.

        This function breaks down a reaction's stoichiometry into three categories:
        - Primary stoichiometry: Main compounds and carbon backbone chemistry
        - Cofactor stoichiometry: Cofactors like NAD, NADH, ATP, ADP, etc.
        - Minor stoichiometry: Minor substrates like protons, water, CO2, etc.

        Args:
            rxn: A reaction object with standard attributes (id, name, build_reaction_string, etc.)

        Returns:
            Dict with keys:
                - primary_stoichiometry: Dict mapping compound names to their stoichiometric coefficients
                - cofactor_stoichiometry: Dict mapping cofactor names to their stoichiometric coefficients
                - minor_stoichiometry: Dict mapping minor compound names to their stoichiometric coefficients
                - primary_chemistry: Brief description of the main chemistry occurring
                - confidence: "high|medium|low|none"
                - other_comments: General comments about the categorization
        """
        system = """
        You are an expert in biochemistry and molecular biology.
        You will receive a biochemical reaction and must analyze its stoichiometry,
        categorizing the compounds into three groups:

        1. PRIMARY STOICHIOMETRY - The main compounds involved in the core chemistry
           (e.g., the carbon backbone transformations, main substrates and products)
        2. COFACTOR STOICHIOMETRY - Cofactors and coenzymes involved
           (e.g., NAD, NADH, ATP, ADP, FAD, FADH2, CoA derivatives)
        3. MINOR STOICHIOMETRY - Minor compounds and prosthetic groups
           (e.g., H+, H2O, CO2, NH3, phosphate, small inorganic ions)

        Respond strictly in valid JSON with **no text outside the JSON**.
        All keys and string values must use double quotes.
        Use only plain ASCII characters.
        """

        shared_prompt = """Analyze the following reaction and categorize its stoichiometry
        into primary, cofactor, and minor components.

        Return a JSON object in this exact format:

        {
        "primary_stoichiometry": {"compound_name": coefficient, ...},
        "cofactor_stoichiometry": {"cofactor_name": coefficient, ...},
        "minor_stoichiometry": {"minor_compound_name": coefficient, ...},
        "primary_chemistry": "Brief description of the main chemical transformation",
        "other_comments": "Brief comments about the categorization decisions.",
        "confidence": "high|medium|low|none"
        }

        Notes:
        - Use positive coefficients for products and negative for reactants
        - If a compound's role is ambiguous, use your best judgment and note it in other_comments
        - The primary_chemistry should describe the core transformation (e.g., "oxidation of alcohol to aldehyde",
          "phosphorylation of glucose", "decarboxylation of amino acid")

        Reaction:
        """
        cache = self._load_cached_curation("ReactionStoichiometry")
        if rxn.id[0:3] in self.const_util_rxn_prefixes():
            return None
        rxn_output = self.reaction_to_string(rxn)
        if rxn_output["base_id"] not in cache:
            self.log_warning(f"Querying AI with {rxn_output['base_id']}")
            prompt = shared_prompt + rxn_output["rxnstring"]
            ai_output = self.chat(prompt=prompt, system=system)
            cache[rxn_output["base_id"]] = json.loads(ai_output)
            if "reversed" in rxn_output:
                # If the reaction was reversed for AI analysis, flip all stoichiometric coefficients back
                cache[rxn_output["base_id"]]["other_comments"] += " Reaction was inverted to avoid AI confusion, and stoichiometric coefficients were inverted after analysis."
                for category in ["primary_stoichiometry", "cofactor_stoichiometry", "minor_stoichiometry"]:
                    if category in cache[rxn_output["base_id"]]:
                        for compound in cache[rxn_output["base_id"]][category]:
                            cache[rxn_output["base_id"]][category][compound] *= -1
            self._save_cached_curation("ReactionStoichiometry",cache)
        else:
            print("ReactionStoichiometry-cached")
        return cache[rxn_output["base_id"]]

    def build_reaction_from_functional_roles(self, functional_roles: set[str]) -> dict[str, Any]:
        """Use AI to construct biochemical reactions from protein functional role strings.

        This function takes a set of protein function strings and uses AI to propose
        biochemical reactions for each function, returning detailed reaction information
        including stoichiometry, compounds, and database references.

        Args:
            functional_roles: A set of strings, where each string describes a protein function
                            (e.g., "FMNH2-dependent alkanesulfonate monooxygenase (EC 1.14.14.5)")

        Returns:
            Dict mapping each function string to a reaction dictionary with keys:
                - reaction_name: Short descriptive name
                - ec_number: EC number as string or null
                - dbxrefs: List of reaction database IDs
                - reactants: List of compound dicts with stoichiometry, name, formula, smiles/inchi, dbxrefs
                - comments: Free-text comments about assumptions and references
                - confidence: "high" | "medium" | "low"
        """
        system = """
        You are an expert biochemical curator and metabolic modeler. Your task is to take a JSON-formatted list of protein function strings and, for each function, propose a single biochemical reaction and return the results in a strict JSON format.

        ## Input

        You will be given **only** a JSON list of function strings, for example:

        ```json
        [
          "FMNH2-dependent alkanesulfonate monooxygenase (EC 1.14.14.5)",
          "DNA-directed RNA polymerase subunit beta'",
          "Serine protease (EC 3.4.21.-)"
        ]
        ```

        Each element is a single function string, possibly including an EC number in parentheses.

        ## Your Task

        For **each** function string, you must create **one** reaction entry and return a **single JSON object** (dictionary) where:

        - **Keys** are the original function strings (exactly as provided).
        - **Values** are dictionaries describing the proposed reaction.

        Do **not** skip any input function. Every function must appear as a key in the output JSON.

        ## Output Format (Schema)

        Your entire response must be valid JSON, with no extra commentary or text, and must follow this structure:

        ```json
        {
          "<function_string_1>": {
            "reaction_name": "<short descriptive reaction name>",
            "ec_number": "<EC number as string or null>",
            "dbxrefs": [
              "<reaction_db_id_1>",
              "<reaction_db_id_2>"
            ],
            "reactants": [
              {
                "stoichiometry": -1,
                "name": "<compound_name>",
                "formula": "<chemical_formula_or_null>",
                "smiles/inchi": "<SMILES_or_InChI_or_null>",
                "dbxrefs": [
                  "<compound_db_id_1>",
                  "<compound_db_id_2>"
                ]
              },
              {
                "stoichiometry": 1,
                "name": "<compound_name>",
                "formula": "<chemical_formula_or_null>",
                "smiles/inchi": "<SMILES_or_InChI_or_null>",
                "dbxrefs": [
                  "<compound_db_id_1>",
                  "<compound_db_id_2>"
                ]
              }
              // more reactants/products as needed
            ],
            "comments": "<free-text comments about assumptions, ambiguities, or references>",
            "confidence": "high" | "medium" | "low"
          },

          "<function_string_2>": {
            ...
          }
        }
        ```

        ### Important Field Conventions

        - **`reaction_name`**
          - A concise human-readable name (e.g., `"FMNH2-dependent alkanesulfonate monooxygenase"`).

        - **`ec_number`**
          - The EC number as a **string** if known (e.g., `"1.14.14.5"`).
          - If no EC number is given or confidently inferable, use `null`.

        - **`dbxrefs` (reaction-level)**
          - A list of database identifiers for the reaction if you know them (e.g. KEGG, MetaCyc, ModelSEED, Rhea).
          - Example: `["RHEA:12345", "RXN-1234", "rxn08469"]`.
          - If none are known, use an empty list: `[]`.

        - **`reactants`**
          - A list of compounds and their stoichiometries.
          - **Reactants** (substrates) must have **negative** stoichiometry (e.g., `-1`, `-2`).
          - **Products** must have **positive** stoichiometry (e.g., `1`, `2`).
          - Use integers where possible; use decimals if needed (e.g., `-0.5` for half-reactions).

        - **`name` (for each reactant)**
          - Use a clear biochemical name, preferably the most standard/common name (e.g., `"oxygen"`, `"FMNH2"`, `"ethanesulfonate"`).

        - **`formula`**
          - Molecular formula if you know it (e.g., `"O2"`, `"C2H6O3S"`).
          - If unknown, use `null`.

        - **`smiles/inchi`**
          - A SMILES or InChI string if you know one.
          - If unknown, use `null`.

        - **`dbxrefs` (compound-level)**
          - Known identifiers, e.g. from KEGG (e.g., `"C00007"`), ChEBI, MetaCyc, ModelSEED (e.g., `"cpd00007"`), etc.
          - If none are known, use an empty list: `[]`.

        - **`comments`**
          - A short free-text note about any assumptions, uncertainties, alternative stoichiometries, or special conditions (e.g., cofactors, electron acceptors).

        - **`confidence`**
          - `"high"`: Reaction is well-defined, well-known, and you are confident in stoichiometry and participants.
          - `"medium"`: General reaction is clear but stoichiometry, cofactors, or some details are uncertain.
          - `"low"`: Only a rough guess; substrate or product identities are uncertain.

        ## Special Cases

        ### 1. Non-metabolic Functions

        If a function clearly describes a **non-metabolic** biological activity (e.g., DNA binding proteins, transcription factors, structural proteins, secretion systems, chaperones without a clear chemical transformation):

        Set:

        ```json
        "reaction_name": null,
        "ec_number": null,
        "dbxrefs": [],
        "reactants": [],
        "comments": "nonmetabolic",
        "confidence": "low"
        ```

        ### 2. Unclear Reactions

        If the function is too vague to determine a chemical reaction, or you genuinely cannot infer a plausible reaction:

        Set:

        ```json
        "reaction_name": null,
        "ec_number": null,
        "dbxrefs": [],
        "reactants": [],
        "comments": "reaction is unclear from specified function",
        "confidence": "low"
        ```

        ### 3. Metabolic but Ambiguous

        If the function is metabolic but ambiguous (e.g., incomplete EC number, multiple possible substrates):

        - Propose one **most plausible** reaction.
        - Be explicit in `comments` about any assumptions (e.g., assumed electron acceptor, assumed specific substrate).
        - Set `confidence` to `"medium"` or `"low"` depending on how speculative it is.

        ## Reaction Construction Guidelines

        For functions that describe metabolic enzymes or transporters:

        1. **Interpret the function and EC number**
           - Use the EC number, substrate names, and enzyme class to determine the chemical transformation.
           - Include typical cofactors and co-substrates (e.g., NAD⁺/NADH, NADP⁺/NADPH, ATP/ADP/Pi, FMN/FMNH2, FAD/FADH2, O₂, H₂O, protons) if they are normally part of that reaction class.

        2. **Balance the reaction as well as possible**
           - Aim for approximate mass and charge balance.
           - If balancing is difficult or uncertain, provide the best plausible stoichiometry, and explain the uncertainty in `comments`.

        3. **Compound details**
           - For each metabolite:
             - Provide `name`, and when possible `formula`, `smiles/inchi`, and `dbxrefs`.
             - Prefer well-known identifiers (e.g., KEGG, ChEBI, MetaCyc, ModelSEED).
           - If you are not reasonably confident about an identifier or structure, leave that field as `null` or an empty list rather than guessing wildly.

        4. **Transport reactions**
           - For pure transporters with no chemical transformation (just movement across a membrane), you may still represent them as:
             - The same compound on both sides with different "compartment" annotations in the `comments`, or
             - You may consider these as nonmetabolic if there is truly no chemical transformation and the requested use-case focuses only on metabolic conversions.
           - Explain your choice in `comments` and set an appropriate `confidence`.

        ## General Output Rules

        - **Return only JSON**. Do not include explanation, Markdown, or prose outside the JSON object.
        - The top-level value must be a single JSON object whose keys are exactly the input function strings.
        - Every function string in the input list must appear as a key in the output.
        - Fields that are unknown should be set to `null` (for single values) or `[]` (for lists), not omitted.
        - Double-check that the JSON is syntactically valid (no trailing commas, properly quoted strings, etc.).

        Respond strictly in valid JSON with **no text outside the JSON**.
        All keys and string values must use double quotes.
        Use only plain ASCII characters.
        """

        user_prompt = """When you are ready, I will provide the JSON list of function strings; you will then respond with only the JSON object described above.

        Here is the JSON list of function strings:

        """

        cache = self._load_cached_curation("ReactionFromFunctionalRoles")

        # Convert set to sorted list for consistent ordering and JSON serialization
        role_list = sorted(list(functional_roles))

        # Create a cache key from the sorted list
        cache_key = json.dumps(role_list)

        if cache_key not in cache:
            self.log_warning(f"Querying AI to build reactions from {len(role_list)} functional roles")
            prompt = user_prompt + json.dumps(role_list, indent=2)
            ai_output = self.chat(prompt=prompt, system=system)
            cache[cache_key] = json.loads(ai_output)
            self._save_cached_curation("ReactionFromFunctionalRoles", cache)
        else:
            print("ReactionFromFunctionalRoles-cached")

        return cache[cache_key]