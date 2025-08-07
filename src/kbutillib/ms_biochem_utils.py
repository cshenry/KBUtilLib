"""ModelSEED Biochemistry Database utilities for searching compounds and reactions."""

# import os
# import subprocess
# import re
# from typing import Any, Dict, List, Optional, Set, Tuple, Union
from .shared_env_utils import *


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
                # Use dependencies directory by default
                from pathlib import Path

                dependencies_dir = Path(__file__).parent / "dependencies"
                modelseed_db_path = str(dependencies_dir / "ModelSEEDDatabase")

        self.modelseed_db_path = modelseed_db_path
        self.auto_download = auto_download
        self._biochem_db = None

        # Initialize database
        self._ensure_database_available()

    @property
    def biochem_db(self) -> Any:
        """Get the ModelSEED biochemistry database instance."""
        if self._biochem_db is None:
            self._ensure_database_available()
        return self._biochem_db

    def _ensure_database_available(self) -> None:
        """Ensure ModelSEED database is available, using dependency management."""
        # First ensure ModelSEEDpy is available
        if not self.ensure_modelseed_py():
            raise ImportError("Failed to obtain ModelSEEDpy dependency")

        # Ensure ModelSEEDDatabase is available
        if not self.ensure_modelseed_database():
            if self.auto_download:
                self.log_warning(
                    "Failed to automatically obtain ModelSEEDDatabase, falling back to manual download"
                )
                self._download_database_manual()
            else:
                raise FileNotFoundError(
                    "ModelSEED database not found and auto_download is disabled"
                )

        # Load the database
        try:
            from modelseedpy.biochem.modelseed_biochem import ModelSEEDBiochem

            self._biochem_db = ModelSEEDBiochem.get(path=self.modelseed_db_path)
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
            result = subprocess.run(
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

    def update_database(self) -> None:
        """Update the ModelSEED database to the latest version."""
        if not os.path.exists(self.modelseed_db_path):
            self._download_database()
            return

        try:
            self.log_info("Updating ModelSEED database...")
            result = subprocess.run(
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
        query: str,
        search_fields: Optional[List[str]] = None,
        exact_match: bool = False,
        case_sensitive: bool = False,
        max_results: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Search for compounds in the ModelSEED database.

        Args:
            query: Search query string
            search_fields: Fields to search in ['name', 'formula', 'aliases', 'smiles', 'inchi']
            exact_match: Whether to require exact matches
            case_sensitive: Whether search should be case sensitive
            max_results: Maximum number of results to return

        Returns:
            List of compound dictionaries with match information
        """
        if search_fields is None:
            search_fields = ["name", "aliases", "formula"]

        results = []
        query_lower = query.lower() if not case_sensitive else query

        # Compile regex for partial matching
        if exact_match:
            pattern = re.compile(
                f"^{re.escape(query)}$", re.IGNORECASE if not case_sensitive else 0
            )
        else:
            pattern = re.compile(
                re.escape(query), re.IGNORECASE if not case_sensitive else 0
            )

        for compound in self.biochem_db.compounds:
            match_score = 0
            matched_fields = []

            # Search in compound name
            if "name" in search_fields and compound.name:
                search_text = compound.name if case_sensitive else compound.name.lower()
                if exact_match:
                    if search_text == query_lower:
                        match_score += 10
                        matched_fields.append("name")
                else:
                    if query_lower in search_text:
                        match_score += 10
                        matched_fields.append("name")

            # Search in aliases
            if "aliases" in search_fields and hasattr(compound, "aliases"):
                for alias in compound.aliases:
                    search_text = alias if case_sensitive else alias.lower()
                    if exact_match:
                        if search_text == query_lower:
                            match_score += 8
                            matched_fields.append("aliases")
                    else:
                        if query_lower in search_text:
                            match_score += 8
                            matched_fields.append("aliases")

            # Search in formula
            if "formula" in search_fields and compound.formula:
                search_text = (
                    compound.formula if case_sensitive else compound.formula.lower()
                )
                if exact_match:
                    if search_text == query_lower:
                        match_score += 15
                        matched_fields.append("formula")
                else:
                    if query_lower in search_text:
                        match_score += 5
                        matched_fields.append("formula")

            # Search in SMILES
            if (
                "smiles" in search_fields
                and hasattr(compound, "smiles")
                and compound.smiles
            ):
                search_text = (
                    compound.smiles if case_sensitive else compound.smiles.lower()
                )
                if query_lower in search_text:
                    match_score += 3
                    matched_fields.append("smiles")

            # Search in InChI
            if (
                "inchi" in search_fields
                and hasattr(compound, "inchi")
                and compound.inchi
            ):
                search_text = (
                    compound.inchi if case_sensitive else compound.inchi.lower()
                )
                if query_lower in search_text:
                    match_score += 3
                    matched_fields.append("inchi")

            if match_score > 0:
                result = {
                    "compound_id": compound.id,
                    "name": compound.name,
                    "formula": compound.formula,
                    "charge": getattr(compound, "charge", None),
                    "mass": getattr(compound, "mass", None),
                    "match_score": match_score,
                    "matched_fields": list(set(matched_fields)),
                    "compound_object": compound,
                }

                # Add additional attributes if available
                if hasattr(compound, "aliases"):
                    result["aliases"] = compound.aliases
                if hasattr(compound, "smiles"):
                    result["smiles"] = compound.smiles
                if hasattr(compound, "inchi"):
                    result["inchi"] = compound.inchi

                results.append(result)

        # Sort by match score (highest first)
        results.sort(key=lambda x: x["match_score"], reverse=True)

        # Limit results if specified
        if max_results:
            results = results[:max_results]

        self.log_info(f"Found {len(results)} compounds matching '{query}'")
        return results

    def search_reactions(
        self,
        query: str,
        search_fields: Optional[List[str]] = None,
        exact_match: bool = False,
        case_sensitive: bool = False,
        max_results: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Search for reactions in the ModelSEED database.

        Args:
            query: Search query string
            search_fields: Fields to search in ['name', 'equation', 'ec_numbers', 'aliases']
            exact_match: Whether to require exact matches
            case_sensitive: Whether search should be case sensitive
            max_results: Maximum number of results to return

        Returns:
            List of reaction dictionaries with match information
        """
        if search_fields is None:
            search_fields = ["name", "equation", "aliases"]

        results = []
        query_lower = query.lower() if not case_sensitive else query

        for reaction in self.biochem_db.reactions:
            match_score = 0
            matched_fields = []

            # Search in reaction name
            if "name" in search_fields and reaction.name:
                search_text = reaction.name if case_sensitive else reaction.name.lower()
                if exact_match:
                    if search_text == query_lower:
                        match_score += 10
                        matched_fields.append("name")
                else:
                    if query_lower in search_text:
                        match_score += 10
                        matched_fields.append("name")

            # Search in equation
            if "equation" in search_fields and hasattr(reaction, "equation"):
                search_text = (
                    reaction.equation if case_sensitive else reaction.equation.lower()
                )
                if exact_match:
                    if search_text == query_lower:
                        match_score += 15
                        matched_fields.append("equation")
                else:
                    if query_lower in search_text:
                        match_score += 8
                        matched_fields.append("equation")

            # Search in aliases
            if "aliases" in search_fields and hasattr(reaction, "aliases"):
                for alias in reaction.aliases:
                    search_text = alias if case_sensitive else alias.lower()
                    if exact_match:
                        if search_text == query_lower:
                            match_score += 8
                            matched_fields.append("aliases")
                    else:
                        if query_lower in search_text:
                            match_score += 8
                            matched_fields.append("aliases")

            # Search in EC numbers
            if "ec_numbers" in search_fields and hasattr(reaction, "ec_numbers"):
                for ec in reaction.ec_numbers:
                    if query_lower in ec.lower():
                        match_score += 12
                        matched_fields.append("ec_numbers")

            if match_score > 0:
                result = {
                    "reaction_id": reaction.id,
                    "name": reaction.name,
                    "equation": getattr(reaction, "equation", ""),
                    "direction": getattr(reaction, "direction", ""),
                    "reversibility": getattr(reaction, "reversibility", ""),
                    "match_score": match_score,
                    "matched_fields": list(set(matched_fields)),
                    "reaction_object": reaction,
                }

                # Add additional attributes if available
                if hasattr(reaction, "aliases"):
                    result["aliases"] = reaction.aliases
                if hasattr(reaction, "ec_numbers"):
                    result["ec_numbers"] = reaction.ec_numbers
                if hasattr(reaction, "pathways"):
                    result["pathways"] = reaction.pathways

                results.append(result)

        # Sort by match score (highest first)
        results.sort(key=lambda x: x["match_score"], reverse=True)

        # Limit results if specified
        if max_results:
            results = results[:max_results]

        self.log_info(f"Found {len(results)} reactions matching '{query}'")
        return results

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

    def get_database_statistics(self) -> Dict[str, Any]:
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
