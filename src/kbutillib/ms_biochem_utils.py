"""ModelSEED Biochemistry Database utilities for compound and reaction searches."""

import os
import re
import subprocess
import string
import json
import pandas as pd
from typing import Any, Optional, Dict
from collections import defaultdict

from .shared_env_utils import SharedEnvUtils
from .dependency_manager import get_data_path

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

class MSBiochemUtils(SharedEnvUtils):
    """Utilities for searching the ModelSEED Biochemistry Database.

    Provides methods for searching compounds and reactions by name, equation,
    formula, and other properties. Automatically manages the ModelSEED Database
    git repository and provides convenient search interfaces.
    """

    def __init__(
        self,
        modelseed_db_path: Optional[str] = None,
        auto_download: bool = True,
        **kwargs: Any,
    ) -> None:
        """Initialize ModelSEED Biochemistry utilities.

        Args:
            modelseed_db_path: Path to ModelSEED database directory
            auto_download: Whether to automatically download database if not found
            **kwargs: Additional keyword arguments passed to SharedEnvironment
        """
        super().__init__(**kwargs)

        # Set default database path to dependencies directory
        if modelseed_db_path is None:
            # First check if user has configured a path
            config_path = self.get_config(
                "ModelSEEDBiochem", "modelseedbiochem_directory"
            )
            if config_path:
                modelseed_db_path = config_path
            else:
                # Use dependency manager to get the ModelSEEDDatabase path
                db_path = get_data_path("ModelSEEDDatabase")
                if db_path:
                    modelseed_db_path = str(db_path)
                else:
                    # Fallback: look in sibling directory
                    from pathlib import Path
                    repo_root = Path(__file__).parent.parent.parent
                    modelseed_db_path = str((repo_root / ".." / "ModelSEEDDatabase").resolve())

        self.modelseed_db_path = modelseed_db_path
        self.auto_download = auto_download
        self._biochem_db = None
        self._identifier_hash = None
        self._structure_hash = None
        self._element_hashes = None
        self._rxn_identifier_hash = None
        self._rxn_stoichiometry_hash = None

        # Initialize database
        self._ensure_database_available()

    @property
    def biochem_db(self) -> Any:
        """Get the ModelSEED biochemistry database instance."""
        if self._biochem_db is None:
            self._ensure_database_available()
        return self._biochem_db
    
    @property
    def identifier_hash(self) -> Dict:
        """Index the biochemistry compounds by their identifiers."""
        if self._identifier_hash is None:
            self._identifier_hash = {}
            for cpd in self.biochem_db.compounds:
                if cpd.is_obsolete == False:
                    item = self._standardize_string(cpd.id)
                    self._identifier_hash.setdefault(item, {"type": "msid", "ids": []})
                    self._identifier_hash[item]["ids"].append(cpd.id)
                    item = self._standardize_string(cpd.name)
                    self._identifier_hash.setdefault(item, {"type": "name", "ids": []})
                    self._identifier_hash[item]["ids"].append(cpd.id)   
                    for name in cpd.names:
                        name = self._standardize_string(str(name))
                        self._identifier_hash.setdefault(name, {"type": "synonym", "ids": []})
                        self._identifier_hash[name]["ids"].append(cpd.id)
                    for anno_type in cpd.annotation:
                        if isinstance(cpd.annotation[anno_type], set):
                            for item in cpd.annotation[anno_type]:
                                item = self._standardize_string(item)
                                self._identifier_hash.setdefault(item, {"type": anno_type, "ids": []})
                                self._identifier_hash[item]["ids"].append(cpd.id)
        return self._identifier_hash
    
    @property
    def rxn_identifier_hash(self) -> Dict:
        """Index the biochemistry reactions by their identifiers."""
        if self._rxn_identifier_hash is None:
            self._rxn_identifier_hash = {}
            for rxn in self.biochem_db.reactions:
                if rxn.is_obsolete == False:
                    item = self._standardize_string(rxn.id)
                    self._rxn_identifier_hash.setdefault(item, {"type": "msid", "ids": []})
                    self._rxn_identifier_hash[item]["ids"].append(rxn.id)
                    item = self._standardize_string(rxn.name)
                    self._rxn_identifier_hash.setdefault(item, {"type": "name", "ids": []})
                    self._rxn_identifier_hash[item]["ids"].append(rxn.id)
                    for name in rxn.names:
                        name = self._standardize_string(str(name))
                        self._rxn_identifier_hash.setdefault(name, {"type": "synonym", "ids": []})
                        self._rxn_identifier_hash[name]["ids"].append(rxn.id)
                    for anno_type in rxn.annotation:
                        if isinstance(rxn.annotation[anno_type], set):
                            for item in rxn.annotation[anno_type]:
                                if anno_type != "ec-code":
                                    item = self._standardize_string(item)
                                else:
                                    array = item.split(".")
                                    self._rxn_identifier_hash.setdefault(array[0]+"."+array[1]+"."+array[2], {"type": "3rd-lvl-"+anno_type, "ids": []})
                                    self._rxn_identifier_hash[array[0]+"."+array[1]+"."+array[2]]["ids"].append(rxn.id)
                                self._rxn_identifier_hash.setdefault(item, {"type": anno_type, "ids": []})
                                self._rxn_identifier_hash[item]["ids"].append(rxn.id)
        return self._rxn_identifier_hash
    
    @property
    def rxn_stoichiometry_hash(self) -> Dict:#TODO: This is AI code currently - need to revise this
        """Index the biochemistry reactions by their stoichiometry."""
        if self._rxn_stoichiometry_hash is None:
            self._rxn_stoichiometry_hash = {"proton_stoichiometry": {},"transport_stoichiometry":{},"metabolite_hash":{},"rxn_hash":{}}
            for rxn in self.biochem_db.reactions:
                if rxn.is_obsolete == False:
                    base_cpd_hash = {}
                    transport_hash = {}
                    for metabolite in rxn.metabolites:
                        base_id = metabolite.id[0:-2]
                        compartment = metabolite.id[-1:]
                        if str(compartment) == "1":
                            self._rxn_stoichiometry_hash["transport_stoichiometry"].setdefault(base_id, {})
                            self._rxn_stoichiometry_hash["transport_stoichiometry"][base_id].setdefault(rxn.metabolites[metabolite], [])
                            self._rxn_stoichiometry_hash["transport_stoichiometry"][base_id][rxn.metabolites[metabolite]].append(rxn.id)
                            transport_hash[base_id] = rxn.metabolites[metabolite]
                        base_cpd_hash.setdefault(base_id, 0)
                        base_cpd_hash[base_id] += rxn.metabolites[metabolite]
                    self._rxn_stoichiometry_hash["rxn_hash"][rxn.id] = {"metabolite_hash": base_cpd_hash,"transport_hash":transport_hash,"proton_stoichiometry":0,"equation":rxn.build_reaction_string(),"id":rxn.id,"name":rxn.name}
                    self._rxn_stoichiometry_hash["proton_stoichiometry"][rxn.id] = 0
                    for base_id in base_cpd_hash:    
                        if base_id == "cpd00067":
                            self._rxn_stoichiometry_hash["proton_stoichiometry"][rxn.id] = base_cpd_hash[base_id]
                            self._rxn_stoichiometry_hash["rxn_hash"][rxn.id]["proton_stoichiometry"] = base_cpd_hash[base_id]
                        elif base_cpd_hash[base_id] != 0:
                            self._rxn_stoichiometry_hash["metabolite_hash"].setdefault(base_id, {})
                            self._rxn_stoichiometry_hash["metabolite_hash"][base_id].setdefault(base_cpd_hash[base_id], [])
                            self._rxn_stoichiometry_hash["metabolite_hash"][base_id][base_cpd_hash[base_id]].append(rxn.id)
        return self._rxn_stoichiometry_hash

    @property
    def structure_hash(self) -> Dict:
        """Index the biochemistry compounds by their structures."""
        if self._structure_hash is None:
            self._structure_hash = {}
            for cpd in self.biochem_db.compounds:
                if cpd.is_obsolete == False:
                    #All structures are unified in a single hash, and we store the type and ids for each struct in the hash values
                    if "InChI" in cpd.annotation:
                        self._structure_hash.setdefault(cpd.annotation["InChI"], {"type": "InChI", "ids": []})
                        self._structure_hash[cpd.annotation["InChI"]]["ids"].append(cpd.id)
                    if "SMILE" in cpd.annotation:
                        self._structure_hash.setdefault(cpd.annotation["SMILE"], {"type": "SMILE", "ids": []})
                        self._structure_hash[cpd.annotation["SMILE"]]["ids"].append(cpd.id)
                    if "InChIKey" in cpd.annotation:
                        key = cpd.annotation["InChIKey"]
                        self._structure_hash.setdefault(key, {"type": "InChIKey", "ids": []})
                        self._structure_hash[key]["ids"].append(cpd.id)
                        key_components = key.split("-")
                        self._structure_hash.setdefault(key_components[0], {"type": "InChIKeyBaseOne", "ids": []})
                        self._structure_hash[key_components[0]]["ids"].append(cpd.id)
                        self._structure_hash.setdefault(key_components[0]+"-"+key_components[1], {"type": "InChIKeyBaseTwo", "ids": []})
                        self._structure_hash[key_components[0]+"-"+key_components[1]]["ids"].append(cpd.id)
        return self._structure_hash

    @property
    def element_hashes(self) -> Dict:
        """Index the biochemistry compounds by the elements they contain."""
        if self._element_hashes is None:
            self._element_hashes = {"element_hash":{},"element_count":{},"cpd_elements":{}}
            for cpd in self.biochem_db.compounds:
                if cpd.is_obsolete == False and cpd.formula is not None and len(cpd.formula) > 0:
                    elements = self._parse_formula(cpd.formula)
                    self._element_hashes["cpd_elements"][cpd.id] = elements
                    self._element_hashes["element_count"][cpd.id] = len(elements)
                    if "H" in elements:
                        self._element_hashes["element_count"][cpd.id] += -1
                    for element, count in elements.items():
                        self._element_hashes["element_hash"].setdefault(element, {})
                        self._element_hashes["element_hash"][element].setdefault(count, [])
                        self._element_hashes["element_hash"][element][count].append(cpd.id)
        return self._element_hashes

    def _standardize_string(self, input_string: str) -> str:
        """Standardize a string by lowercasing and stripping whitespace."""
        input_string = input_string.replace("_DASH_", "-")
        input_string = input_string.replace("_COLON_", ":")
        input_string = input_string.translate(str.maketrans('', '', string.punctuation + string.whitespace))
        return input_string.lower().strip()

    def normalize_compound_name(self, name: str) -> list[str]:
        """Normalize a compound name for better matching in the biochemistry database.

        Applies a series of transformations to handle common naming variations
        in chemical compound names (salts, hydrates, stereochemistry, etc.).

        Args:
            name: The compound name to normalize

        Returns:
            List of normalized name variants to try for matching.
            The first element is the most normalized form, followed by
            intermediate variants that may also match.
        """
        if not name:
            return [name]

        variants = set()
        current = name.strip()

        # Apply transformations iteratively to build up normalized forms
        def apply_transforms(s: str) -> str:
            result = s

            # 1. Remove metal ion prefixes (Sodium, Potassium, etc.)
            # Handle multi-sodium prefixes first (Trisodium, Disodium)
            result = re.sub(r'^Trisodium\s+', '', result, flags=re.IGNORECASE)
            result = re.sub(r'^Disodium\s+', '', result, flags=re.IGNORECASE)
            result = re.sub(r'^Sodium\s+', '', result, flags=re.IGNORECASE)
            result = re.sub(r'^Tripotassium\s+', '', result, flags=re.IGNORECASE)
            result = re.sub(r'^Dipotassium\s+', '', result, flags=re.IGNORECASE)
            result = re.sub(r'^Potassium\s+', '', result, flags=re.IGNORECASE)
            result = re.sub(r'^Calcium\s+', '', result, flags=re.IGNORECASE)
            result = re.sub(r'^Magnesium\s+', '', result, flags=re.IGNORECASE)
            result = re.sub(r'^Ammonium\s+', '', result, flags=re.IGNORECASE)

            # 2. Remove hydrate suffixes (dihydrate, pentahydrate, hemihydrate, etc.)
            result = re.sub(r'\s+\w*hydrate\b', '', result, flags=re.IGNORECASE)

            # 3. Remove salt suffixes (including combined salt+hydrate patterns)
            result = re.sub(r'\s+monopotassium\s+salt\s+\w*hydrate\b', '', result, flags=re.IGNORECASE)
            result = re.sub(r'\s+monosodium\s+salt\s+\w*hydrate\b', '', result, flags=re.IGNORECASE)
            result = re.sub(r'\s+trisodium\s+salt\b', '', result, flags=re.IGNORECASE)
            result = re.sub(r'\s+disodium\s+salt\b', '', result, flags=re.IGNORECASE)
            result = re.sub(r'\s+monosodium\s+salt\b', '', result, flags=re.IGNORECASE)
            result = re.sub(r'\s+sodium\s+salt\b', '', result, flags=re.IGNORECASE)
            result = re.sub(r'\s+tripotassium\s+salt\b', '', result, flags=re.IGNORECASE)
            result = re.sub(r'\s+dipotassium\s+salt\b', '', result, flags=re.IGNORECASE)
            result = re.sub(r'\s+monopotassium\s+salt\b', '', result, flags=re.IGNORECASE)
            result = re.sub(r'\s+potassium\s+salt\b', '', result, flags=re.IGNORECASE)
            result = re.sub(r'\s+salt\b', '', result, flags=re.IGNORECASE)

            # 4. Remove hydrochloride suffixes
            result = re.sub(r'\s+dihydrochloride\b', '', result, flags=re.IGNORECASE)
            result = re.sub(r'\s+hydrochloride\b', '', result, flags=re.IGNORECASE)
            result = re.sub(r'\s+HCl\b', '', result, flags=re.IGNORECASE)

            # 5. Remove dibasic/monobasic qualifiers
            result = re.sub(r'\s+dibasic\b', '', result, flags=re.IGNORECASE)
            result = re.sub(r'\s+monobasic\b', '', result, flags=re.IGNORECASE)
            result = re.sub(r'\s+tribasic\b', '', result, flags=re.IGNORECASE)

            # Clean up any double spaces
            result = re.sub(r'\s+', ' ', result).strip()

            return result

        # Apply basic transformations
        normalized = apply_transforms(current)
        variants.add(normalized)

        # 6. Handle D,L stereochemistry - try both D,L -> L and D,L -> D
        if 'D,L' in normalized or 'd,l' in normalized.lower():
            variants.add(re.sub(r'D,L[-\s]?', 'L-', normalized, flags=re.IGNORECASE))
            variants.add(re.sub(r'D,L[-\s]?', 'D-', normalized, flags=re.IGNORECASE))
            variants.add(re.sub(r'D,L[-\s]?', '', normalized, flags=re.IGNORECASE))
        if 'DL-' in normalized or 'dl-' in normalized.lower():
            variants.add(re.sub(r'DL[-\s]?', 'L-', normalized, flags=re.IGNORECASE))
            variants.add(re.sub(r'DL[-\s]?', 'D-', normalized, flags=re.IGNORECASE))
            variants.add(re.sub(r'DL[-\s]?', '', normalized, flags=re.IGNORECASE))

        # 7. Convert "ic acid" to "ate" (e.g., "Pyruvic acid" -> "Pyruvate")
        acid_variants = set()
        for v in list(variants):
            if re.search(r'ic\s+acid\b', v, flags=re.IGNORECASE):
                ate_form = re.sub(r'ic\s+acid\b', 'ate', v, flags=re.IGNORECASE)
                acid_variants.add(ate_form)
            # Also try the reverse: "ate" -> "ic acid"
            if re.search(r'ate\b', v, flags=re.IGNORECASE) and not re.search(r'ic\s+acid\b', v, flags=re.IGNORECASE):
                acid_form = re.sub(r'ate\b', 'ic acid', v, flags=re.IGNORECASE)
                acid_variants.add(acid_form)
        variants.update(acid_variants)

        # 8. Handle common acid name variations
        for v in list(variants):
            # "ous acid" -> "ite"
            if re.search(r'ous\s+acid\b', v, flags=re.IGNORECASE):
                variants.add(re.sub(r'ous\s+acid\b', 'ite', v, flags=re.IGNORECASE))

        # Add original name as fallback
        variants.add(name.strip())

        # Sort by length (shortest/most normalized first) and return as list
        return sorted(list(variants), key=len)

    def normalize_and_search_compound(self, name: str) -> dict[str, Any]:
        """Search for a compound using normalized name variants.

        Tries multiple normalized forms of the compound name to find the best match.

        Args:
            name: The compound name to search for

        Returns:
            Dict with search results, including which variant matched
        """
        variants = self.normalize_compound_name(name)
        all_matches = {}
        matched_variant = None

        for variant in variants:
            matches = self.search_compounds(query_identifiers=[variant])
            if matches:
                for cpd_id, match_info in matches.items():
                    if cpd_id not in all_matches:
                        all_matches[cpd_id] = match_info
                        all_matches[cpd_id]['matched_variant'] = variant
                    elif match_info['score'] > all_matches[cpd_id]['score']:
                        all_matches[cpd_id] = match_info
                        all_matches[cpd_id]['matched_variant'] = variant

                if matched_variant is None:
                    matched_variant = variant

        return {
            'matches': all_matches,
            'original_name': name,
            'variants_tried': variants,
            'first_matched_variant': matched_variant
        }

    def _parse_formula(self,formula: str) -> dict:
        # Match elements (capital letter + optional lowercase letters) followed by optional digits
        tokens = re.findall(r'([A-Z][a-z]*)(\d*)', formula)
        
        counts = defaultdict(int)
        for element, num in tokens:
            counts[element] += int(num) if num else 1
        return dict(counts)

    def _ensure_database_available(self) -> None:
        # Load the database
        try:
            from modelseedpy.biochem.modelseed_biochem import ModelSEEDBiochem
            self._biochem_db =  ModelSEEDBiochem.get(path=self.modelseed_db_path)
            self.log_info(f"ModelSEED database loaded from {self.modelseed_db_path}")
        except Exception as e:
            self.log_error(f"Failed to load ModelSEED database: {e}")
            raise

    def _download_database_manual(self) -> None:
        """Download the ModelSEED database from GitHub as fallback."""
        git_url = "https://github.com/ModelSEED/ModelSEEDDatabase.git"

        # Remove existing directory if it exists and is problematic
        if os.path.exists(self.modelseed_db_path):
            import shutil

            self.log_info(f"Removing existing directory {self.modelseed_db_path}")
            shutil.rmtree(self.modelseed_db_path)

        try:
            # Create parent directory if it doesn't exist
            parent_dir = os.path.dirname(self.modelseed_db_path)
            os.makedirs(parent_dir, exist_ok=True)

            # Clone the repository
            self.log_info(f"Cloning ModelSEED database from {git_url}")
            subprocess.run(
                ["git", "clone", git_url, self.modelseed_db_path],
                capture_output=True,
                text=True,
                check=True,
                timeout=180,  # Longer timeout for this large repository
            )

            self.log_info("ModelSEED database downloaded successfully")

        except subprocess.TimeoutExpired:
            error_msg = f"Timeout while cloning ModelSEED database from {git_url}"
            self.log_error(error_msg)
            raise RuntimeError(error_msg)
        except subprocess.CalledProcessError as e:
            error_msg = f"Failed to clone ModelSEED database: {e.stderr}"
            self.log_error(error_msg)
            raise RuntimeError(error_msg)
        except Exception as e:
            error_msg = f"Error downloading ModelSEED database: {e}"
            self.log_error(error_msg)
            raise

    
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
        baseid = id
        compartment = None
        index = None
        bracket_match = re.search(r"(.+)\[([a-zA-Z]+)(\d*)\]$", id)
        if bracket_match:
            baseid = bracket_match[1]
            compartment = bracket_match[2]
            index = bracket_match[3]
            if compartment.lower() not in compartment_types:
                self.log_warning(f"Compartment type '{compartment}' not recognized in bracket notation.") # Try underscore notation (e.g., "cpd01024_c0")
        elif re.search("(.+)_([a-zA-Z]+)(\d*)$", id) != None:
            m = re.search("(.+)_([a-zA-Z]+)(\d*)$", id)
            baseid = m[1]
            compartment = m[2]
            index = m[3]
            if compartment.lower() not in compartment_types:
                self.log_warning(f"Compartment type '{compartment}' not recognized in underscore notation.")
        if hasattr(object_or_id, 'compartment') and object_or_id.compartment:
            objcomp = object_or_id.compartment
            if compartment is None:
                compartment = objcomp
                self.log_warning("Comparment is not encoded in the ID but is encoded in the object:",object_or_id.id,objcomp)
            elif objcomp != compartment:
                self.log_warning("Compartment mismatch between ID and object:",met_id,objcomp,compartment)
        return (baseid, compartment, index)

    def update_database(self) -> None:
        """Update the ModelSEED database to the latest version."""
        if not os.path.exists(self.modelseed_db_path):
            self._download_database()
            return

        try:
            self.log_info("Updating ModelSEED database...")
            subprocess.run(
                ["git", "pull"],
                cwd=self.modelseed_db_path,
                capture_output=True,
                text=True,
                check=True,
            )

            self.log_info("ModelSEED database updated successfully")

            # Reload the database
            self._biochem_db = None
            self._ensure_database_available()

        except subprocess.CalledProcessError as e:
            error_msg = f"Failed to update ModelSEED database: {e.stderr}"
            self.log_error(error_msg)
            raise RuntimeError(error_msg)          

    def search_compounds(
        self,
        query_identifiers: list[str] = [],
        query_structures: list[str] = [],
        query_formula: str = None
    ) -> list[dict[str, Any]]:
        """Search for compounds in the ModelSEED database."""
        identifier_hash = self.identifier_hash
        element_hashes = self.element_hashes
        structure_hash = self.structure_hash
        matches = {}
        for item in query_identifiers:
            item = self._standardize_string(item)
            if item in identifier_hash:
                for hit in identifier_hash[item]["ids"]:
                    matches.setdefault(hit, {"score": 0, "identifier_hits": {}, "formula_hits": {},"structure_hits": {}})
                    matches[hit]["identifier_hits"][item] = 1 * identifier_hash[item]["type"]
                    matches[hit]["score"] += 10
            elif ":" in item:
                array = item.split(":")[1]
                itemid = array[1]
                itemtype = array[0]
                if itemid in identifier_hash:
                    for hit in identifier_hash[item]["ids"]:
                        if itemtype in identifier_hash[itemid]["type"]:
                            matches.setdefault(hit, {"score": 0, "identifier_hits": {}, "formula_hits": {},"structure_hits": {}})
                            matches[hit]["identifier_hits"][item] = 1 * identifier_hash[item]["type"]
                            matches[hit]["score"] += 10
        for item in query_structures:
            if item in structure_hash:
                for hit in structure_hash[item]["ids"]:
                    matches.setdefault(hit, {"score": 0, "identifier_hits": {}, "formula_hits": {},"structure_hits": {}})
                    matches[hit]["structure_hits"][item] = 1 * structure_hash[item]["type"]
                    matches[hit]["score"] += 8
        if query_formula is not None and len(query_formula) > 0:
            elements = self._parse_formula(query_formula)
            accumulated_hits = {}
            element_count = 0
            for element in elements:
                if element != "H":
                    element_count += 1
                    if element in element_hashes["element_hash"] and elements[element] in element_hashes["element_hash"][element]:
                        for hit in element_hashes["element_hash"][element][elements[element]]:
                            accumulated_hits.setdefault(hit, 0)
                            accumulated_hits[hit] += 1
            #Checking if the hits have the same number of elements and the same number of instances of each element
            for hit in accumulated_hits:
                if accumulated_hits[hit] == element_count and element_hashes["element_count"][hit] == element_count:
                    matches.setdefault(hit, {"score": 0, "identifier_hits": {}, "formula_hits": {}, "structure_hits": {}})
                    hittype = "No H"
                    matches[hit]["score"] += 2
                    if "H" in elements and "H" in element_hashes["cpd_elements"][hit] and elements["H"] == element_hashes["cpd_elements"][hit]["H"]:
                        hittype = "H match"
                        matches[hit]["score"] += 2
                    matches[hit]["formula_hits"][query_formula+":"+self.biochem_db.compounds.get_by_id(hit).formula] = hittype
        return matches

    def search_reactions(
        self,
        query_identifiers: Optional[list[str]] = [],
        query_ec: Optional[list[str]] = [],
        query_stoichiometry: Optional[dict[str, Any]] = None,
        cpd_hits: Optional[dict[str, Any]] = None,
        default_missing_count: Optional[float] = 1
    ) -> list[dict[str, Any]]:
        """Search for reactions in the ModelSEED database."""
        identifier_hash = self.rxn_identifier_hash
        stoichiometry_hash = self.rxn_stoichiometry_hash
        matches = {}
        for item in query_identifiers:
            item = self._standardize_string(item)
            if item in identifier_hash:
                for hit in identifier_hash[item]["ids"]:
                    matches.setdefault(hit, {"score": 10, "identifier_hits": {}, "ec_hits": {},"transport_scores":{},"equation_scores":{},"proton_matches":[]})
                    matches[hit]["identifier_hits"][item] = 1 * identifier_hash[item]["type"]
        full_hits = {}
        third_lvl_hits = {}
        for item in query_ec:
            if item in identifier_hash:
                for hit in identifier_hash[item]["ids"]:
                    matches.setdefault(hit, {"score": 0, "identifier_hits": {}, "ec_hits": {},"transport_scores":{},"equation_scores":{},"proton_matches":[]})
                    matches[hit]["ec_hits"][item] = 1 * identifier_hash[item]["type"]
                    full_hits[hit] = 1
            else:
                array = item.split(".")
                item = array[0]+"."+array[1]+"."+array[2]
                if item in identifier_hash:
                    for hit in identifier_hash[item]["ids"]:
                        matches.setdefault(hit, {"score": 0, "identifier_hits": {}, "ec_hits": {}, "transport_scores": {}, "equation_scores": {}, "proton_matches":[]})
                        matches[hit]["ec_hits"][item] = 1 * identifier_hash[item]["type"]
                        third_lvl_hits[hit] = 1
        for hit in full_hits:
            matches[hit]["score"] += 8
        for hit in third_lvl_hits:
            if hit not in full_hits:
                matches[hit]["score"] += 6
        if query_stoichiometry is not None and len(query_stoichiometry) > 0:
            #Checking for best base equation match
            signs = [-1,1]
            missing_count = default_missing_count
            if len(query_stoichiometry["metabolite_hash"]) <= 2:
                missing_count = 0
            for sign in signs:
                highest_hit_scores = {}
                highest_hit_score = 0
                highest_hit = None
                hit_counts = {}
                unmatch_cpd = {}
                unmatch_db_cpd = {}
                for base_id in query_stoichiometry["metabolite_hash"]:
                    querystoich = sign*query_stoichiometry["metabolite_hash"][base_id]
                    if base_id not in cpd_hits:
                        cpd_hits[base_id] = self.search_compounds(query_identifiers=[base_id])
                        if len(cpd_hits[base_id]) == 0:
                            cpd_hits[base_id] = {base_id: {}}
                    for cpdmatch in cpd_hits[base_id]:
                        if cpdmatch in stoichiometry_hash["metabolite_hash"]:
                            if querystoich in stoichiometry_hash["metabolite_hash"][cpdmatch]:
                                for hit in stoichiometry_hash["metabolite_hash"][cpdmatch][querystoich]:
                                    hit_counts.setdefault(hit, {})
                                    hit_counts[hit].setdefault(base_id, {})
                                    hit_counts[hit][base_id].setdefault(cpdmatch, 0)
                                    hit_counts[hit][base_id][cpdmatch] += 1
                                    highest_hit_scores[hit] = len(hit_counts[hit])/len(query_stoichiometry["metabolite_hash"])
                                    if highest_hit_scores[hit] > highest_hit_score:
                                        highest_hit = hit
                                        highest_hit_score = highest_hit_scores[hit]
                    for hit in hit_counts:
                        if base_id not in hit_counts[hit]:
                            unmatch_cpd.setdefault(hit, {})
                            unmatch_cpd[hit][base_id] = querystoich
                for hit in hit_counts:
                    unmatch_cpd.setdefault(hit, {})
                    unmatch_db_cpd.setdefault(hit, {})
                    for cpdmatch in stoichiometry_hash["rxn_hash"][hit]["metabolite_hash"]:
                        found = False
                        for base_id in hit_counts[hit]:
                            if cpdmatch in hit_counts[hit][base_id]:
                                found = True
                        if not found and cpdmatch != "cpd00067":
                            unmatch_db_cpd[hit][cpdmatch] = stoichiometry_hash["rxn_hash"][hit]["metabolite_hash"][cpdmatch]
                if highest_hit != None:
                    for hit in highest_hit_scores:
                        if hit in matches or (highest_hit_scores[hit] >= (len(query_stoichiometry["metabolite_hash"])-missing_count)/len(query_stoichiometry["metabolite_hash"]) and len(query_stoichiometry["metabolite_hash"]) == len(stoichiometry_hash["rxn_hash"][hit]["metabolite_hash"])):
                            matches.setdefault(hit, {"score": 0, "identifier_hits": {}, "ec_hits": {},"transport_scores":{},"equation_scores":{},"proton_matches":[]})
                            matches[hit]["score"] += 20*highest_hit_scores[hit]
                            matches[hit]["equation_scores"] = (highest_hit_scores[hit], len(hit_counts[hit]), sign,unmatch_cpd[hit],unmatch_db_cpd[hit],{})
                            for base_id in hit_counts[hit]:
                                for cpdmatch in hit_counts[hit][base_id]:
                                    cpd_hits[base_id][cpdmatch].setdefault("equation_hits", {})#HERE
                                    cpd_hits[base_id][cpdmatch]["equation_hits"][hit] = highest_hit_scores[hit]
                                    matches[hit]["equation_scores"][4][base_id] = cpdmatch
                #Checking for best transport match from the previous hits
                highest_hit_scores = {}
                highest_hit_score = 0
                highest_hit = None
                hit_counts = {}
                unmatch_cpd = {}
                unmatch_db_cpd = {}
                for base_id in query_stoichiometry["transport_stoichiometry"]:
                    querystoich = sign*query_stoichiometry["transport_stoichiometry"][base_id]
                    if base_id not in cpd_hits:
                        cpd_hits[base_id] = self.search_compounds(query_identifiers=[base_id])
                        if len(cpd_hits[base_id]) == 0:
                            cpd_hits[base_id] = {base_id: {}}
                    for cpdmatch in cpd_hits[base_id]:
                        if cpdmatch in stoichiometry_hash["transport_stoichiometry"]:
                            if querystoich in stoichiometry_hash["transport_stoichiometry"][cpdmatch]:
                                for hit in stoichiometry_hash["transport_stoichiometry"][cpdmatch][querystoich]:
                                    hit_counts.setdefault(hit, {})
                                    hit_counts[hit].setdefault(base_id, {})
                                    hit_counts[hit][base_id].setdefault(cpdmatch, 0)
                                    hit_counts[hit][base_id][cpdmatch] += 1
                                    highest_hit_scores[hit] = len(hit_counts[hit])/len(query_stoichiometry["transport_stoichiometry"])
                                    if highest_hit_scores[hit] > highest_hit_score:
                                        highest_hit = hit
                                        highest_hit_score = highest_hit_scores[hit]
                    for hit in hit_counts:
                        unmatch_cpd[hit] = {}
                        if base_id not in hit_counts[hit]:
                            unmatch_cpd[hit][base_id] = querystoich
                for hit in hit_counts:
                    unmatch_db_cpd[hit] = {}
                    for cpdmatch in stoichiometry_hash["rxn_hash"][hit]["transport_hash"]:
                        found = False
                        for base_id in hit_counts[hit]:
                            if cpdmatch in hit_counts[hit][base_id]:
                                found = True
                        if not found:
                            unmatch_db_cpd[hit][cpdmatch] = stoichiometry_hash["rxn_hash"][hit]["transport_hash"][cpdmatch]
                if highest_hit != None:
                    for hit in highest_hit_scores:
                        if highest_hit_scores[hit] >= 1:
                            # For pure transport reactions (no transformation), add even without identifier match
                            is_pure_transport = query_stoichiometry.get("is_pure_transport", False)
                            if hit in matches or is_pure_transport:
                                matches.setdefault(hit, {"score": 0, "identifier_hits": {}, "ec_hits": {},"transport_scores":{},"equation_scores":{},"proton_matches":[]})
                                matches[hit]["score"] += highest_hit_scores[hit]*10*len(hit_counts[hit])
                                matches[hit]["transport_scores"] = (highest_hit_scores[hit], len(hit_counts[hit]), sign,unmatch_cpd[hit],unmatch_db_cpd[hit])
                                for base_id in hit_counts[hit]:
                                    for cpdmatch in hit_counts[hit][base_id]:
                                        cpd_hits[base_id][cpdmatch].setdefault("transport_hits", {})
                                        cpd_hits[base_id][cpdmatch]["transport_hits"][hit] = highest_hit_scores[hit]
                #Increasing hit scores if the proton stoichiometry matches
                for hit in matches:
                    if hit in stoichiometry_hash["proton_stoichiometry"] and sign*query_stoichiometry["proton_stoichiometry"] == stoichiometry_hash["proton_stoichiometry"][hit]:
                        matches[hit]["score"] += 2
                        matches[hit]["proton_matches"].append(hit)
        return matches

    def get_compound_by_id(self, compound_id: str) -> Optional[Any]:
        """Get a compound by its ModelSEED ID.

        Args:
            compound_id: ModelSEED compound ID

        Returns:
            Compound object or None if not found
        """
        try:
            return self.biochem_db.compounds.get_by_id(compound_id)
        except:
            return None

    def get_reaction_by_id(self, reaction_id: str) -> Optional[Any]:
        """Get a reaction by its ModelSEED ID.

        Args:
            reaction_id: ModelSEED reaction ID

        Returns:
            Reaction object or None if not found
        """
        try:
            return self.biochem_db.reactions.get_by_id(reaction_id)
        except:
            return None

    def get_database_statistics(self) -> dict[str, Any]:
        """Get statistics about the ModelSEED database.

        Returns:
            Dictionary with database statistics
        """
        stats = {
            "database_path": self.modelseed_db_path,
            "total_compounds": len(self.biochem_db.compounds)
            if self.biochem_db.compounds
            else 0,
            "total_reactions": len(self.biochem_db.reactions)
            if self.biochem_db.reactions
            else 0,
        }

        # Count compounds by charge
        charge_distribution = {}
        formula_patterns = {}

        for compound in self.biochem_db.compounds:
            charge = getattr(compound, "charge", 0)
            charge_distribution[charge] = charge_distribution.get(charge, 0) + 1

            # Count common formula patterns
            if compound.formula:
                # Extract major elements
                elements = re.findall(r"[A-Z][a-z]?", compound.formula)
                for element in elements:
                    formula_patterns[element] = formula_patterns.get(element, 0) + 1

        stats["charge_distribution"] = charge_distribution
        stats["common_elements"] = dict(
            sorted(formula_patterns.items(), key=lambda x: x[1], reverse=True)[:10]
        )

        # Count reactions by reversibility
        reversibility_stats = {}
        for reaction in self.biochem_db.reactions:
            reversibility = getattr(reaction, "reversibility", "unknown")
            reversibility_stats[reversibility] = (
                reversibility_stats.get(reversibility, 0) + 1
            )

        stats["reversibility_distribution"] = reversibility_stats

        return stats
    
    def reaction_to_string(self,reaction):
        """Converts reaction into string representation."""
        [base_id, compartment, index] = self._parse_id(reaction)
        name = re.sub(r'\s*\[[a-zA-Z0-9]+\]$', '', reaction.name)
        output = {"rxnstring": base_id + "(" + name + ")","base_id": base_id,"compartment": compartment,"index": index}
        if compartment != None and compartment != "c":
            output["rxnstring"] += "[" + str(compartment) + "]"
        equation = reaction.build_reaction_string(use_metabolite_names=True)
        if "<--" in equation:
            array = equation.split("<--")
            equation = array[1] + " --> " + array[0]
            output["reversed"] = True
        output["rxnstring"] += ": " + equation
        return output

    def reaction_id_to_msid(self,reaction_id):
        """Converts reaction ID into ModelSEED ID if possible."""
        pattern = re.compile(r"rxn\d+")
        match = pattern.search(reaction_id)
        if match:
            return match.group()
        return None

    def reaction_to_msid(self,reaction):
        """Converts reaction into ModelSEED ID if possible."""
        pattern = re.compile(r"rxn\d+")
        idstring = None
        if isinstance(reaction, str):
            idstring = reaction
        else:
            idstring = reaction.id
        match = pattern.search(idstring)
        if match:
            return match.group()
        if not isinstance(reaction, str):
            for anno_type in reaction.annotation:
                if isinstance(reaction.annotation[anno_type], set):
                    for alias in reaction.annotation[anno_type]:
                        match = pattern.search(alias)
                        if match:
                            return match.group()
        return None

    def reaction_directionality_from_bounds(self, reaction, tol=1e-9):
        """
        Classify directionality from a Reaction's bounds only.

        Returns one of: 'forward', 'reverse', 'reversible', 'blocked'.
        """
        lb, ub = reaction.lower_bound, reaction.upper_bound

        # Treat tiny bounds as zero
        if abs(lb) < tol:
            lb = 0.0
        if abs(ub) < tol:
            ub = 0.0

        if lb < 0 and ub > 0:
            return "reversible"
        if lb >= 0 and ub > 0:
            return "forward"
        if lb < 0 and ub <= 0:
            return "reverse"
        return "blocked"

    def reaction_biochem_directionality(self,reaction):
        """Determines the directionalities of a reaction in the ModelSEED biochemistry database."""
        rxnobj = self.get_reaction_by_id(reaction)
        if rxnobj is None:
            return None
        return self.reaction_directionality_from_bounds(rxnobj)

    def build_model_metabolite_index(self,model):
        """Build a hash of metabolite base IDs linked to compartments"""
        metabolite_index = {}
        base_id_index = {}
        for met in model.metabolites:
            (base_id,compartment,index) = self._parse_id(met)
            metabolite_index[met.id] = {
                "base_id": base_id,
                "compartment": compartment,
                "index": index
            }
            compartment_index = base_id_index.get(base_id,{})
            compartment_index[compartment] = met
        return metabolite_index,base_id_index

    def is_water(self,met):
        (base_id,compartment,index) = self._parse_id(met)
        if base_id in ["h2o","cpd00001"]:
            return True
        if met.formula == "H2O" and met.charge == 0:
            return True
        return False
    
    def is_proton(self,met):
        (base_id,compartment,index) = self._parse_id(met)
        if base_id in ["h","cpd00067"]:
            return True
        if met.formula == "H" and met.charge == 1:
            return True
        return False
    
    def find_proton_in_compartment(self,model, compartment):
        """Find or create H+ metabolite in the given compartment."""
        metabolite_index,base_id_index = self.build_model_metabolite_index(model)
        h_base_ids = ["h","cpd00067"]
        for item in h_base_ids:
            if item in base_id_index:
                if compartment in base_id_index[item]:
                    return base_id_index[item][compartment]
        for met in model.metabolites:
            print(met.id,met.formula,met.charge,metabolite_index[met.id]["compartment"])
            if met.formula == "H" and met.charge == 1 and metabolite_index[met.id]["compartment"] == compartment:
                return met
        return None

    def parse_formula(self,formula_str):
        """Parse a chemical formula string into element counts."""
        if not formula_str or pd.isna(formula_str):
            return {}
        elements = {}
        # Match element symbols followed by optional counts
        import re
        matches = re.findall(r'([A-Z][a-z]?)(\d*)', formula_str)
        for element, count in matches:
            if element:
                elements[element] = elements.get(element, 0) + (int(count) if count else 1)
        return elements

    def check_reaction_balance(self,rxn):
        """Check mass and charge balance for a reaction.
        
        Returns:
            dict with keys:
            - 'mass_balanced': bool
            - 'charge_balanced': bool
            - 'element_imbalance': dict of element -> imbalance (positive = excess on product side)
            - 'charge_imbalance': int (positive = excess positive charge on product side)
            - 'missing_formulas': list of metabolite IDs missing formulas
        """
        element_balance = defaultdict(float)
        charge_balance = 0
        missing_formulas = []
        stoichiometry = {}

        for met, coeff in rxn.metabolites.items():
            # Check formula using COBRApy's built-in elements property
            if met.formula and not pd.isna(met.formula) and met.formula != '':
                elements = met.elements  # COBRApy parses formula automatically
                for element, count in elements.items():
                    element_balance[element] += coeff * count
            else:
                missing_formulas.append(met.id)
            
            # Check charge
            if met.charge is not None:
                charge_balance += coeff * met.charge
        
            stoichiometry[met.id] = {
                'formula': met.formula,
                'charge': met.charge,
                'coefficient': coeff,
                'deltag': met.notes.get('deltag', None)
            }
            if "seed.compound" in met.annotation:
                stoichiometry[met.id]['seed_compound'] = met.annotation['seed.compound']

        # Round to handle floating point errors
        element_imbalance = {k: round(v, 6) for k, v in element_balance.items() if round(v, 6) != 0}
        charge_imbalance = round(charge_balance, 6)
        
        return {
            'mass_balanced': len(element_imbalance) == 0,
            'charge_balanced': charge_imbalance == 0,
            'element_imbalance': element_imbalance,
            'charge_imbalance': charge_imbalance,
            'missing_formulas': missing_formulas,
            'stoichiometry': stoichiometry
        }

    def can_fix_with_protons(self,balance_result):
        """Check if imbalance can be fixed by adjusting H+ only.
        
        Returns:
            tuple (can_fix: bool, proton_adjustment: float)
            proton_adjustment is the number of H+ to ADD to the reaction
            (negative means remove H+ from products / add to substrates)
        """
        elem_imbalance = balance_result['element_imbalance']
        charge_imbalance = balance_result['charge_imbalance']
        
        # If only H is imbalanced (or nothing is imbalanced in mass)
        non_h_imbalance = {k: v for k, v in elem_imbalance.items() if k != 'H'}
        
        if non_h_imbalance:
            # There's an imbalance in elements other than H
            return False, 0
        
        # Get H imbalance (positive = excess H on product side)
        h_imbalance = elem_imbalance.get('H', 0)
        
        # Check if H imbalance matches charge imbalance
        # H+ has +1 charge, so adding 1 H+ adds +1 charge and +1 H
        # To fix: we need to add protons to the substrate side (negative coeff)
        # proton_adjustment = -h_imbalance (to cancel out the H excess)
        
        # Verify that fixing H also fixes charge
        # If we add -h_imbalance protons (to substrate side), 
        # charge change = -h_imbalance * 1 = -h_imbalance
        # This should equal -charge_imbalance to balance
        
        if h_imbalance == charge_imbalance:
            # Both can be fixed by adjusting H+ by -h_imbalance
            return True, -h_imbalance
        elif h_imbalance == 0 and charge_imbalance != 0:
            # No H imbalance but charge is off - can fix with H+ 
            return True, -charge_imbalance
        else:
            # H and charge imbalances don't match - can't fix with just H+
            return False, 0