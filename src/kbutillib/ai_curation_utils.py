"""KBase model utilities for constraint-based metabolic modeling."""

import pickle
from typing import Any, Dict, Optional
import re
import json
import subprocess
import os

# Optional imports - only needed for FBA analysis, not for AI curation
try:
    import pandas as pd
    from cobra.flux_analysis import flux_variability_analysis
    from cobra.flux_analysis import pfba
    HAS_COBRA = True
except ImportError:
    HAS_COBRA = False

from .argo_utils import ArgoUtils

class AICurationUtils(ArgoUtils):
    """Tools for running AI-powered curation using either Argo or Claude Code backends.

    Configuration (in config.yaml):
        ai_curation:
            backend: 'argo'  # or 'claude-code'
            claude_code_executable: 'claude-code'  # Full path if not in PATH
    """

    def __init__(self, backend: Optional[str] = None, **kwargs: Any) -> None:
        """Initialize AI curation utilities.

        Args:
            backend: Override backend choice ('argo' or 'claude-code').
                    If not specified, uses config value or defaults to 'argo'
            **kwargs: Additional keyword arguments passed to SharedEnvironment/ArgoUtils
        """
        super().__init__(**kwargs)

        # Determine backend from parameter, config, or default
        if backend is not None:
            self.ai_backend = backend
        else:
            self.ai_backend = self.get_config_value(
                "ai_curation.backend",
                default="argo"
            )

        # Get Claude Code executable path from config if using that backend
        if self.ai_backend == "claude-code":
            self.claude_code_executable = self.get_config_value(
                "ai_curation.claude_code_executable",
                default="claude"
            )
            self._verify_claude_code_available()

        self.log_info(f"AICurationUtils initialized with backend: {self.ai_backend}")

    def _verify_claude_code_available(self) -> None:
        """Verify that claude-code executable is available."""
        try:
            print(self.claude_code_executable)
            result = subprocess.run(
                [self.claude_code_executable, "--version"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                self.log_debug(f"Claude Code available: {result.stdout.strip()}")
            else:
                self.log_warning(
                    f"Claude Code executable found but returned non-zero: {self.claude_code_executable}"
                )
        except FileNotFoundError:
            self.log_error(
                f"Claude Code executable not found: {self.claude_code_executable}\n"
                "Please install Claude Code or set 'ai_curation.claude_code_executable' in config.yaml"
            )
            raise
        except Exception as e:
            self.log_warning(f"Could not verify Claude Code: {e}")

    def _chat_via_claude_code(self, prompt: str, system: str = "") -> str:
        """Send a chat request via Claude Code CLI.

        Args:
            prompt: The user prompt/question to send
            system: System message for context

        Returns:
            The AI response text as JSON string
        """
        # Build the command
        cmd = [
            self.claude_code_executable,
            "-p", prompt,
            "--output-format", "json",
            "--dangerously-skip-permissions"  # Skip permission prompts that could cause hangs
        ]

        # Add system prompt if provided
        if system:
            cmd.extend(["--system-prompt", system])

        try:
            # Print the full command for debugging
            self.log_info(f"Claude CLI command: {' '.join(cmd)}")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,  # 5 minute timeout
                stdin=subprocess.DEVNULL  # Prevent waiting for stdin
            )

            if result.returncode != 0:
                self.log_error(f"Claude Code failed: {result.stderr}")
                raise RuntimeError(f"Claude Code returned non-zero exit code: {result.returncode}")

            # Parse the JSON output from Claude
            # The output format is JSON with a "result" field containing the response
            try:
                output_data = json.loads(result.stdout)
                # Extract the actual response text from Claude's JSON output
                if isinstance(output_data, dict) and "result" in output_data:
                    response_text = output_data["result"]
                else:
                    response_text = result.stdout
            except json.JSONDecodeError:
                # If output isn't valid JSON, use raw stdout
                response_text = result.stdout

            # The response_text should be the JSON that the AI generated
            # Try to parse it to validate it's proper JSON, then return as string
            try:
                # Validate it's proper JSON by parsing
                parsed = json.loads(response_text)
                return json.dumps(parsed)
            except json.JSONDecodeError:
                # If not valid JSON, return as-is and let caller handle it
                return response_text

        except subprocess.TimeoutExpired:
            self.log_error("Claude Code timed out after 5 minutes")
            raise
        except Exception as e:
            self.log_error(f"Error calling Claude Code: {e}")
            raise

    def chat(self, prompt: str, *, system: str = "") -> str:
        """Send a chat request to the configured AI backend (Argo or Claude Code).

        This overrides the parent chat() method to route to different backends
        based on configuration.

        Args:
            prompt: The user prompt/question to send
            system: Optional system message for context

        Returns:
            The AI response text
        """
        if self.ai_backend == "claude-code":
            return self._chat_via_claude_code(prompt, system)
        elif self.ai_backend == "argo":
            return super().chat(prompt, system=system)
        else:
            raise ValueError(f"Unknown AI backend: {self.ai_backend}. Must be 'argo' or 'claude-code'")

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

    def find_compound_aliases(
        self,
        compounds: list,
        batch_size: int = 10,
        alias_type: str = "ChEBI"
    ) -> dict[str, Any]:
        """Use AI to find aliases for a batch of compounds.

        This function processes compounds in batches and uses AI to propose
        database identifiers (e.g., CHEBI IDs) based on compound metadata.

        Args:
            compounds: List of dicts with keys: id, name, formula, smiles, inchi,
                      inchikey, other_aliases (dict of existing aliases by source)
            batch_size: Number of compounds per AI query (for batching experiments)
            alias_type: Target alias type to find (ChEBI, KEGG, MetaCyc, etc.)

        Returns:
            Dict mapping compound IDs to:
                - proposed_aliases: List of proposed alias IDs (without prefix)
                - confidence: high/medium/low/none
                - reasoning: Brief explanation
                - alternatives: Other possible matches (if ambiguous)
        """
        system = f"""You are an expert biochemical database curator with deep knowledge of {alias_type}
(Chemical Entities of Biological Interest) database and other biochemical databases.

You will receive a batch of compounds in JSON format. For each compound,
analyze the provided information (name, formula, structure, existing aliases)
and determine the most likely {alias_type} identifier(s).

INPUT FORMAT:
[
  {{
    "id": "cpd00001",
    "name": "compound name",
    "formula": "molecular formula",
    "charge": 0,
    "smiles": "SMILES string",
    "inchi": "InChI string",
    "inchikey": "InChIKey",
    "other_aliases": {{"KEGG": [...], "MetaCyc": [...], ...}}
  }},
  ...
]

OUTPUT FORMAT (strict JSON):
{{
  "cpd00001": {{
    "proposed_aliases": ["12345"],
    "confidence": "high|medium|low|none",
    "reasoning": "Brief explanation of how you identified this {alias_type} ID",
    "alternatives": ["67890"]
  }},
  ...
}}

GUIDELINES:
1. Use structural information (InChIKey, SMILES, InChI) as primary matching criteria
   - InChIKey is the most reliable structural identifier
   - SMILES can help identify the compound structure
2. Cross-reference with KEGG, MetaCyc, and other aliases when available
   - These databases often have direct mappings to {alias_type}
3. Consider charge state and protonation forms ({alias_type} often has multiple entries)
   - The same molecule at different pH may have different {alias_type} IDs
4. Set confidence="none" and proposed_aliases=[] if no reliable match found
   - Don't guess - it's better to return no match than a wrong one
5. Use alternatives for ambiguous cases (isomers, stereoisomers, tautomers, etc.)
6. Return {alias_type} IDs WITHOUT any prefix (just the numeric ID)

CONFIDENCE LEVELS:
- "high": Structural match (InChIKey) or well-known compound with verified mapping
- "medium": Name/alias match with supporting evidence but no structural confirmation
- "low": Partial match or inference from related compounds
- "none": No reliable match found

Respond strictly in valid JSON with **no text outside the JSON**.
All keys and string values must use double quotes.
Use only plain ASCII characters."""

        user_prompt = f"""Find {alias_type} identifiers for the following compounds.

Compound batch:
"""

        # Process compounds in batches
        all_results = {}
        cache = self._load_cached_curation(f"CompoundAliases_{alias_type}")

        # Filter out compounds already in cache
        compounds_to_process = []
        for cpd in compounds:
            cpd_id = cpd.get("id", "")
            if cpd_id in cache:
                all_results[cpd_id] = cache[cpd_id]
                print(f"CompoundAliases_{alias_type}-cached: {cpd_id}")
            else:
                compounds_to_process.append(cpd)

        if not compounds_to_process:
            return all_results

        # Process remaining compounds in batches
        for i in range(0, len(compounds_to_process), batch_size):
            batch = compounds_to_process[i:i + batch_size]
            batch_ids = [cpd.get("id", f"unknown_{j}") for j, cpd in enumerate(batch)]

            self.log_info(f"Processing batch {i // batch_size + 1}: {len(batch)} compounds")

            # Prepare batch data for AI
            batch_data = []
            for cpd in batch:
                cpd_input = {
                    "id": cpd.get("id", ""),
                    "name": cpd.get("name", ""),
                    "formula": cpd.get("formula", ""),
                    "charge": cpd.get("charge", 0),
                    "smiles": cpd.get("smiles", ""),
                    "inchi": cpd.get("inchi", ""),
                    "inchikey": cpd.get("inchikey", ""),
                    "other_aliases": cpd.get("other_aliases", {})
                }
                batch_data.append(cpd_input)

            prompt = user_prompt + json.dumps(batch_data, indent=2)

            try:
                ai_output = self.chat(prompt=prompt, system=system)

                # Debug: log raw response
                self.log_debug(f"Raw AI response length: {len(ai_output) if ai_output else 0}")
                if not ai_output or not ai_output.strip():
                    self.log_warning(f"Empty AI response for batch")
                    raise json.JSONDecodeError("Empty response", "", 0)

                # Clean up the response - remove markdown code blocks
                ai_output_clean = ai_output.strip()

                # Remove markdown code block wrappers (```json ... ``` or ``` ... ```)
                if ai_output_clean.startswith('```'):
                    # Find the end of the first line (might be ```json or just ```)
                    first_newline = ai_output_clean.find('\n')
                    if first_newline != -1:
                        ai_output_clean = ai_output_clean[first_newline + 1:]
                    # Remove trailing ```
                    if ai_output_clean.endswith('```'):
                        ai_output_clean = ai_output_clean[:-3].strip()

                # If still not starting with {, try to find JSON object
                if not ai_output_clean.startswith('{'):
                    start_idx = ai_output_clean.find('{')
                    if start_idx != -1:
                        ai_output_clean = ai_output_clean[start_idx:]
                        # Find matching closing brace
                        brace_count = 0
                        end_idx = 0
                        for i, char in enumerate(ai_output_clean):
                            if char == '{':
                                brace_count += 1
                            elif char == '}':
                                brace_count -= 1
                                if brace_count == 0:
                                    end_idx = i + 1
                                    break
                        if end_idx > 0:
                            ai_output_clean = ai_output_clean[:end_idx]

                self.log_debug(f"Cleaned AI response: {ai_output_clean[:200]}...")
                batch_results = json.loads(ai_output_clean)

                # Store results in cache and all_results
                for cpd_id in batch_ids:
                    if cpd_id in batch_results:
                        cache[cpd_id] = batch_results[cpd_id]
                        all_results[cpd_id] = batch_results[cpd_id]
                    else:
                        # AI didn't return result for this compound
                        missing_result = {
                            "proposed_aliases": [],
                            "confidence": "none",
                            "reasoning": "AI did not return a result for this compound",
                            "alternatives": []
                        }
                        cache[cpd_id] = missing_result
                        all_results[cpd_id] = missing_result

                # Save cache after each batch
                self._save_cached_curation(f"CompoundAliases_{alias_type}", cache)

            except json.JSONDecodeError as e:
                self.log_error(f"Failed to parse AI response for batch: {e}")
                self.log_error(f"Raw response was: {repr(ai_output[:500] if ai_output else 'EMPTY')}")
                # Mark all compounds in batch as failed
                for cpd_id in batch_ids:
                    error_result = {
                        "proposed_aliases": [],
                        "confidence": "none",
                        "reasoning": f"AI response parsing error: {str(e)}",
                        "alternatives": []
                    }
                    cache[cpd_id] = error_result
                    all_results[cpd_id] = error_result
                self._save_cached_curation(f"CompoundAliases_{alias_type}", cache)
            except Exception as e:
                self.log_error(f"Error processing batch: {e}")
                raise

        return all_results

    def curate_biochemical_compound(self, compound) -> dict[str, Any]:
        """Use AI to validate, correct, and enrich a biochemical compound record.

        This function takes a COBRApy Metabolite object and uses AI to:
        - Validate identity and structure (name, formula, charge, mass, SMILES, InChI, InChIKey)
        - Evaluate thermodynamic data (deltag)
        - Verify formula and mass consistency
        - Validate aliases and cross-references
        - Evaluate abbreviation appropriateness

        Args:
            compound: A COBRApy Metabolite object with standard attributes

        Returns:
            Dict with the original compound data plus:
                - changes: List of changes made with field, old_value, new_value, reason
                - errors: List of error messages
                - comments: List of non-fatal observations and suggestions
                - newdata: List of proposed new data with field, value, source, confidence
        """
        system = """You are an expert biochemical database curator with deep expertise in:
small-molecule chemistry, biochemical thermodynamics, metabolite identifiers (KEGG, ChEBI, MetaCyc, BiGG), SMILES/InChI/InChIKey validation, charge and formula balancing at pH 7, and metabolic modeling databases.

You will be given ONE compound record in JSON format.

Your task is to VALIDATE, CORRECT, and ENRICH the compound record while preserving compatibility with biochemical databases.

INPUT:
A JSON object describing a biochemical compound.

VALIDATION TASKS (ALL REQUIRED)
1. Identity and Structure
   - Verify that name, formula, charge, mass, SMILES, InChI (if present), and InChIKey (if present) all describe the SAME chemical entity.
   - Confirm the charge state is appropriate for biochemical standard conditions (pH ~7).
   - IMPORTANT: InChI strings in this database represent the NEUTRAL form of the compound, while the formula represents the CHARGED (ionic) form at pH 7.
   - The formula should differ from the neutral InChI by the number of hydrogens corresponding to the charge (e.g., a -1 charge means one fewer H than the neutral form).
   - Do NOT flag formula/InChI mismatches if they are consistent with the stated charge.
   - Check that SMILES, InChI, and InChIKey are mutually consistent (all represent the neutral form).
   - If structure fields are missing but can be inferred with high confidence, propose them.

2. Thermodynamics
   - Evaluate the provided standard Gibbs free energy of formation (deltag_kcal_per_mol field).
   - IMPORTANT: The deltag values are in kcal/mol (NOT kJ/mol). This is the ModelSEED convention.
   - For reference: 1 kcal/mol = 4.184 kJ/mol. Typical values range from -200 to +50 kcal/mol.
   - Check that the magnitude and sign are reasonable for the compound class given kcal/mol units.
   - Flag values that are suspicious, inconsistent with known databases, or inappropriate for biochemical standard conditions.
   - Do NOT fabricate precise thermodynamic values; only propose replacements when well established.

3. Formula and Mass
   - The formula represents the CHARGED form at pH 7, NOT the neutral form.
   - Verify that the chemical formula matches the molecular mass within reasonable rounding.
   - When validating formula vs InChI: account for the charge. A compound with charge -1 will have one fewer H in its formula than in the neutral InChI.
   - Example: Phosphate at pH 7 might have formula "HO4P" (charge -2) while InChI shows "H3O4P" (neutral H3PO4).
   - Do NOT recommend changing the formula to match InChI without considering the charge field.

4. Aliases and Cross-References
   - IMPORTANT: This database uses UNIFIED compound records representing the predominant ionic form at pH 7.
   - DO NOT REMOVE aliases for different protonation/ionic states - they are VALID synonyms for the unified record.
   - "uric acid" and "urate" are BOTH valid aliases for the same unified compound (one is neutral name, one is ionic name).
   - "phosphoric acid", "phosphate", "HPO4", "H2PO4", "PO4" are ALL valid aliases for a unified phosphate record.
   - "H2O", "water", "hydroxide", "hydronium" are ALL valid aliases for the unified water record.
   - The ONLY reason to remove an alias is if it refers to a CHEMICALLY DISTINCT compound (different molecular skeleton/connectivity).
   - Validate all database identifiers (KEGG, ChEBI, MetaCyc, BiGG, etc.) for correctness.
   - Propose missing but well-known identifiers when appropriate.

5. Abbreviation
   - Evaluate whether the abbreviation is recognizable, standard, and unambiguous.
   - Propose a better abbreviation if appropriate.

CORRECTION RULES
- DO NOT silently overwrite any existing data.
- Any change must be explicitly recorded with a reason.
- If uncertain, propose rather than assert.
- Do not introduce speculative chemistry.
- Preserve the original JSON structure and fields.

OUTPUT REQUIREMENTS

Return the SAME JSON object, corrected as needed, and ADD the following fields:

"changes": [
  {
    "field": "<field_name>",
    "old_value": "<old_value>",
    "new_value": "<new_value>",
    "reason": "<clear, concise explanation>"
  }
]

IMPORTANT: For alias changes, list each alias modification as a SEPARATE change entry:
- Use field "alias_removed" with old_value as the removed alias and new_value as null
- Use field "alias_added" with old_value as null and new_value as the added alias
- Do NOT lump all aliases together in a single change entry
- Example:
  {"field": "alias_removed", "old_value": "bad-alias", "new_value": null, "reason": "..."}
  {"field": "alias_added", "old_value": null, "new_value": "new-alias", "reason": "..."}

"errors": [
  "<error message>"
]

"comments": [
  "<non-fatal observations, modeling implications, or suggestions>"
]

"newdata": [
  {
    "field": "<field_name>",
    "value": "<new_value>",
    "source": "<database, literature, or inference>",
    "confidence": "<high | medium | low>"
  }
]

If NO issues are found:
- Explicitly state that the record is internally consistent.
- Leave "changes" empty.
- Use "comments" to briefly explain why the record is acceptable.

OUTPUT CONSTRAINTS
- Output MUST be valid JSON.
- Do NOT include explanatory text outside the JSON.
- Do NOT reformat unrelated fields.
- Do NOT invent database identifiers or thermodynamic values.

Respond strictly in valid JSON with **no text outside the JSON**.
All keys and string values must use double quotes.
Use only plain ASCII characters.
"""

        shared_prompt = """Validate, correct, and enrich the following biochemical compound record.

Compound JSON:
"""
        cache = self._load_cached_curation("CompoundCuration")

        # Build compound data dictionary from COBRApy Metabolite object
        compound_data = {
            "id": compound.id,
            "name": compound.name,
        }

        # Add formula if available
        if hasattr(compound, 'formula') and compound.formula:
            compound_data["formula"] = compound.formula

        # Add charge if available
        if hasattr(compound, 'charge') and compound.charge is not None:
            compound_data["charge"] = compound.charge

        # Add compartment if available
        if hasattr(compound, 'compartment') and compound.compartment:
            compound_data["compartment"] = compound.compartment

        # Add abbreviation if available (ModelSEED compounds use 'abbr' attribute)
        if hasattr(compound, 'abbr') and compound.abbr:
            compound_data["abbreviation"] = compound.abbr

        # Add annotation/cross-references if available
        if hasattr(compound, 'annotation') and compound.annotation:
            compound_data["annotations"] = {}
            for anno_type, values in compound.annotation.items():
                if isinstance(values, set):
                    compound_data["annotations"][anno_type] = list(values)
                elif isinstance(values, list):
                    compound_data["annotations"][anno_type] = values
                else:
                    compound_data["annotations"][anno_type] = [values]

        # Add notes if available (may contain SMILES, InChI, deltag, etc.)
        if hasattr(compound, 'notes') and compound.notes:
            for key, value in compound.notes.items():
                if key not in compound_data:
                    compound_data[key] = value

        # Try to extract common fields from various attributes
        # SMILES
        if hasattr(compound, 'smiles') and compound.smiles:
            compound_data["smiles"] = compound.smiles
        elif 'smiles' not in compound_data and hasattr(compound, 'annotation'):
            if 'smiles' in compound.annotation:
                val = compound.annotation['smiles']
                compound_data["smiles"] = list(val)[0] if isinstance(val, set) else val

        # InChI
        if hasattr(compound, 'inchi') and compound.inchi:
            compound_data["inchi"] = compound.inchi
        elif 'inchi' not in compound_data and hasattr(compound, 'annotation'):
            if 'inchi' in compound.annotation:
                val = compound.annotation['inchi']
                compound_data["inchi"] = list(val)[0] if isinstance(val, set) else val

        # InChIKey
        if hasattr(compound, 'inchikey') and compound.inchikey:
            compound_data["inchikey"] = compound.inchikey
        elif 'inchikey' not in compound_data and hasattr(compound, 'annotation'):
            if 'inchikey' in compound.annotation:
                val = compound.annotation['inchikey']
                compound_data["inchikey"] = list(val)[0] if isinstance(val, set) else val

        # Mass
        if hasattr(compound, 'mass') and compound.mass is not None:
            compound_data["mass"] = compound.mass

        # DeltaG (standard Gibbs free energy of formation) - units are kcal/mol
        if hasattr(compound, 'deltag') and compound.deltag is not None:
            compound_data["deltag_kcal_per_mol"] = compound.deltag
        elif hasattr(compound, 'delta_g') and compound.delta_g is not None:
            compound_data["deltag_kcal_per_mol"] = compound.delta_g

        # Aliases/other names
        if hasattr(compound, 'names') and compound.names:
            compound_data["aliases"] = list(compound.names) if isinstance(compound.names, set) else compound.names

        # Use compound ID as cache key
        cache_key = compound.id

        if cache_key not in cache:
            self.log_warning(f"Querying AI to curate compound {compound.id}")
            prompt = shared_prompt + json.dumps(compound_data, indent=2)
            ai_output = self.chat(prompt=prompt, system=system)
            cache[cache_key] = json.loads(ai_output)
            self._save_cached_curation("CompoundCuration", cache)
        else:
            print("CompoundCuration-cached")

        return cache[cache_key]