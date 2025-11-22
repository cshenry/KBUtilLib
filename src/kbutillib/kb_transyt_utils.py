"""TranSyT transporter annotation utilities for KBase.

This module provides utilities for annotating membrane transport proteins
using TranSyT (Transport Systems Tracker). It integrates with KBase to
annotate proteins from genomes and integrate predicted transporters into
metabolic models.

TranSyT predicts and annotates membrane transport proteins using machine learning
by analyzing protein sequences for transporter features, predicting metabolite
substrates and transport directionality, and associating predicted transporters
with genes (GPRs).

Note: TranSyT requires external dependencies:
- Java 11+
- Neo4j database (4.0.2+)
- TranSyT JAR file
"""

import math
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from .kb_model_utils import KBModelUtils


class KBTransyTUtils(KBModelUtils):
    """Utilities for TranSyT transporter annotation in KBase.

    This class provides methods for:
    - Extracting protein sequences from KBase genomes
    - Preparing input files for TranSyT analysis
    - Running TranSyT transporter prediction
    - Parsing TranSyT SBML output
    - Integrating predicted transporters into metabolic models
    - Managing GPR (Gene-Protein-Reaction) rules

    TranSyT is a tool for predicting and annotating membrane transport proteins
    in metabolic models using machine learning.

    Attributes:
        MERGE_RULES: Available model merge rules for transporter integration
        DEFAULT_PARAMS: Default parameters for TranSyT analysis
    """

    # Available merge rules for integrating TranSyT results
    MERGE_RULES = {
        "replace_all": "Remove all old transporters, use only TranSyT predictions",
        "merge_reactions_only": "Add new reactions, keep original GPRs",
        "merge_reactions_and_gpr": "Merge both reactions and GPRs",
        "merge_reactions_replace_gpr": "Add new reactions, replace GPRs (default)",
    }

    # Default TranSyT parameters
    DEFAULT_PARAMS = {
        "score_threshold": 0.5,
        "accept_transyt_ids": 1,
        "cpmds_filter": 0,
        "ignore_m2": 0,
        "rule": "merge_reactions_replace_gpr",
        "tax_id": "",
    }

    def __init__(
        self,
        java_path: str = "/opt/jdk/jdk-11.0.1/bin/java",
        transyt_jar: str = "/opt/transyt/transyt.jar",
        neo4j_path: str = "/opt/neo4j/neo4j-community-4.0.2/bin/neo4j",
        working_dir: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        """Initialize TranSyT utilities.

        Args:
            java_path: Path to Java executable (Java 11+)
            transyt_jar: Path to TranSyT JAR file
            neo4j_path: Path to Neo4j start script
            working_dir: Working directory for TranSyT processing
            **kwargs: Additional arguments passed to parent class
        """
        super().__init__(**kwargs)

        # TranSyT configuration
        self.java_path = java_path
        self.transyt_jar = transyt_jar
        self.neo4j_path = neo4j_path
        self.ref_database = "ModelSEED"

        # Set up working directory
        if working_dir:
            self.working_dir = Path(working_dir)
        else:
            self.working_dir = Path(tempfile.mkdtemp(prefix="transyt_"))

        # Ensure working directory exists
        self.working_dir.mkdir(parents=True, exist_ok=True)

        # Paths for input/output
        self.inputs_path = self.working_dir / "processingDir"
        self.results_path: Optional[Path] = None

    def check_dependencies(self) -> Dict[str, bool]:
        """Check if TranSyT dependencies are available.

        Returns:
            Dictionary with dependency names and their availability status
        """
        deps = {
            "java": os.path.exists(self.java_path),
            "transyt_jar": os.path.exists(self.transyt_jar),
            "neo4j": os.path.exists(self.neo4j_path),
            "cobra": False,
        }

        try:
            import cobra

            deps["cobra"] = True
        except ImportError:
            pass

        return deps

    def require_dependencies(self) -> None:
        """Verify all required dependencies are available.

        Raises:
            RuntimeError: If any required dependency is missing
        """
        deps = self.check_dependencies()
        missing = [name for name, available in deps.items() if not available]
        if missing:
            raise RuntimeError(
                f"Missing TranSyT dependencies: {', '.join(missing)}. "
                f"TranSyT requires Java 11+, Neo4j database, and the TranSyT JAR file."
            )

    # ==================== Genome/Protein Extraction ====================

    def extract_proteins_to_fasta(
        self,
        genome: Any,
        output_path: Optional[str] = None,
    ) -> str:
        """Extract protein sequences from a genome to FASTA format.

        Args:
            genome: KBase genome object or genome data dictionary
            output_path: Optional output file path. If not provided,
                        creates a file in the working directory.

        Returns:
            Path to the generated FASTA file
        """
        if output_path is None:
            self.inputs_path.mkdir(parents=True, exist_ok=True)
            output_path = str(self.inputs_path / "protein.faa")

        # Handle genome as object or dictionary
        if hasattr(genome, "features"):
            features = genome.features
        elif isinstance(genome, dict) and "features" in genome:
            features = genome["features"]
        else:
            raise ValueError("Invalid genome format: must have 'features' attribute or key")

        faa_entries = []
        for feature in features:
            # Handle feature as object or dictionary
            if hasattr(feature, "id"):
                ftr_id = feature.id
                protein = getattr(feature, "protein_translation", "")
            else:
                ftr_id = feature.get("id", "")
                protein = feature.get("protein_translation", "")

            if protein:
                faa_entries.append(f">{ftr_id}\n{protein}")

        with open(output_path, "w") as f:
            f.write("\n".join(faa_entries))

        self.log_info(f"Extracted {len(faa_entries)} protein sequences to {output_path}")
        return output_path

    def extract_model_compounds(
        self,
        model: Any,
        output_path: Optional[str] = None,
    ) -> str:
        """Extract compound IDs from a metabolic model for TranSyT filtering.

        Args:
            model: KBase FBA model or model data dictionary
            output_path: Optional output file path

        Returns:
            Path to the generated metabolites file
        """
        if output_path is None:
            self.inputs_path.mkdir(parents=True, exist_ok=True)
            output_path = str(self.inputs_path / "metabolites.txt")

        # Handle model as MSModelUtil, cobra model, or dictionary
        if hasattr(model, "model"):  # MSModelUtil wrapper
            model_data = model.model
        else:
            model_data = model

        compounds_list = []

        # Try to get compounds from different formats
        if hasattr(model_data, "metabolites"):
            # Cobra model format
            for met in model_data.metabolites:
                mseed_id = met.id.split("_")[0]
                if mseed_id not in compounds_list:
                    compounds_list.append(mseed_id)
        elif isinstance(model_data, dict) and "modelcompounds" in model_data:
            # KBase model dictionary format
            for compound in model_data["modelcompounds"]:
                mseed_id = compound["id"].split("_")[0]
                if mseed_id not in compounds_list:
                    compounds_list.append(mseed_id)

        with open(output_path, "w") as f:
            f.write("\n".join(compounds_list))

        self.log_info(f"Extracted {len(compounds_list)} compounds to {output_path}")
        return output_path

    def get_taxonomy_id(self, genome: Any) -> Optional[int]:
        """Extract taxonomy ID from a genome.

        Args:
            genome: KBase genome object or dictionary

        Returns:
            NCBI taxonomy ID or None if not found
        """
        # Handle genome as object or dictionary
        if hasattr(genome, "info") and hasattr(genome.info, "metadata"):
            metadata = genome.info.metadata
            if "taxonomy_id" in metadata:
                return int(metadata["taxonomy_id"])

        if isinstance(genome, dict):
            # Try direct taxonomy_id field
            if "taxonomy_id" in genome:
                return int(genome["taxonomy_id"])
            # Try taxon_ref and resolve
            if "taxon_ref" in genome:
                try:
                    ref_data = self.kbase_api.get_object_info_from_ref(genome["taxon_ref"])
                    ktaxon = self.kbase_api.get_object(ref_data.id, ref_data.workspace_id)
                    return int(ktaxon.get("taxonomy_id", 0)) or None
                except Exception as e:
                    self.log_warning(f"Could not resolve taxon_ref: {e}")

        return None

    # ==================== TranSyT Parameter Handling ====================

    def prepare_params_file(
        self,
        params: Dict[str, Any],
        taxonomy_id: Optional[int] = None,
        output_path: Optional[str] = None,
    ) -> str:
        """Create TranSyT parameters file.

        Args:
            params: Dictionary of TranSyT parameters
            taxonomy_id: NCBI taxonomy ID for the organism
            output_path: Optional output file path

        Returns:
            Path to the generated parameters file
        """
        if output_path is None:
            self.inputs_path.mkdir(parents=True, exist_ok=True)
            output_path = str(self.inputs_path / "params.txt")

        # Merge with defaults
        full_params = {**self.DEFAULT_PARAMS, **params}

        # Handle special parameter transformations
        if full_params.get("ignore_m2") == 1:
            full_params["score_threshold"] = 1

        with open(output_path, "w") as f:
            for key, value in full_params.items():
                f.write(f"{key}\t{value}\n")

            if taxonomy_id:
                f.write(f"taxID\t{taxonomy_id}\n")
            f.write(f"reference_database\t{self.ref_database}")

        self.log_info(f"Created parameters file at {output_path}")
        return output_path

    def validate_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Validate and normalize TranSyT parameters.

        Args:
            params: User-provided parameters

        Returns:
            Validated and normalized parameters

        Raises:
            ValueError: If invalid parameters are provided
        """
        validated = {**self.DEFAULT_PARAMS, **params}

        # Validate merge rule
        if validated.get("rule") not in self.MERGE_RULES:
            raise ValueError(
                f"Invalid merge rule '{validated.get('rule')}'. "
                f"Must be one of: {list(self.MERGE_RULES.keys())}"
            )

        # Validate score threshold
        score = validated.get("score_threshold", 0.5)
        if not (0 <= score <= 1):
            raise ValueError(f"score_threshold must be between 0 and 1, got {score}")

        return validated

    # ==================== TranSyT Execution ====================

    def deploy_neo4j_database(self) -> subprocess.Popen:
        """Start the Neo4j database for TranSyT.

        Returns:
            Popen object for the Neo4j process
        """
        self.log_info("Starting Neo4j database...")
        return subprocess.Popen([self.neo4j_path, "start"])

    def run_transyt_jar(
        self,
        inputs_path: Optional[str] = None,
        memory_mb: int = 4096,
        timeout_seconds: Optional[int] = None,
    ) -> int:
        """Execute the TranSyT JAR file.

        Args:
            inputs_path: Path to input directory containing protein.faa, params.txt
            memory_mb: Maximum Java heap memory in MB
            timeout_seconds: Optional timeout for the subprocess

        Returns:
            Exit code from TranSyT (0 = success)
        """
        if inputs_path is None:
            inputs_path = str(self.inputs_path)

        self.require_dependencies()

        cmd = [
            self.java_path,
            "-jar",
            "--add-exports",
            "java.base/jdk.internal.misc=ALL-UNNAMED",
            "-Dio.netty.tryReflectionSetAccessible=true",
            "-Dworkdir=/workdir",
            "-Dlogback.configurationFile=/kb/module/conf/logback.xml",
            f"-Xmx{memory_mb}m",
            self.transyt_jar,
            "3",  # Mode for KBase
            inputs_path,
        ]

        self.log_info(f"Running TranSyT: {' '.join(cmd)}")

        try:
            process = subprocess.Popen(cmd)
            exit_code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            self.log_error("TranSyT process timed out")
            return -1

        self.results_path = Path(inputs_path) / "results"
        self.log_info(f"TranSyT finished with exit code: {exit_code}")
        return exit_code

    def run_annotation(
        self,
        genome: Any,
        model: Optional[Any] = None,
        params: Optional[Dict[str, Any]] = None,
        taxonomy_id: Optional[int] = None,
        deploy_database: bool = True,
    ) -> Dict[str, Any]:
        """Run complete TranSyT annotation workflow.

        This is the main entry point for TranSyT annotation. It:
        1. Extracts proteins from the genome
        2. Optionally extracts compounds from the model for filtering
        3. Prepares TranSyT parameters
        4. Runs TranSyT
        5. Returns results for further processing

        Args:
            genome: KBase genome object or dictionary
            model: Optional KBase model for compound filtering
            params: TranSyT parameters (uses defaults if not provided)
            taxonomy_id: NCBI taxonomy ID (extracted from genome if not provided)
            deploy_database: Whether to start Neo4j database

        Returns:
            Dictionary with:
                - exit_code: TranSyT exit code
                - results_path: Path to results directory
                - sbml_path: Path to output SBML file (if successful)
                - references_path: Path to reaction references file
        """
        params = params or {}
        validated_params = self.validate_params(params)

        # Create fresh input directory
        if self.inputs_path.exists():
            shutil.rmtree(self.inputs_path)
        self.inputs_path.mkdir(parents=True)

        # Extract taxonomy ID if not provided
        if taxonomy_id is None:
            taxonomy_id = self.get_taxonomy_id(genome)
            if validated_params.get("tax_id"):
                taxonomy_id = int(validated_params["tax_id"])

        if taxonomy_id is None:
            return {
                "exit_code": 8,
                "error": "Taxonomy ID not found. Please provide a valid NCBI taxonomy ID.",
            }

        # Prepare input files
        self.extract_proteins_to_fasta(genome)

        if model is not None and validated_params.get("cpmds_filter", 0) == 1:
            self.extract_model_compounds(model)

        self.prepare_params_file(validated_params, taxonomy_id)

        # Start database if needed
        if deploy_database:
            self.deploy_neo4j_database()

        # Run TranSyT
        exit_code = self.run_transyt_jar()

        results = {
            "exit_code": exit_code,
            "results_path": str(self.results_path) if self.results_path else None,
            "taxonomy_id": taxonomy_id,
        }

        if exit_code == 0 and self.results_path:
            sbml_path = self.results_path / "transyt.xml"
            refs_path = self.results_path / "reactions_references.txt"

            if sbml_path.exists():
                results["sbml_path"] = str(sbml_path)
            if refs_path.exists():
                results["references_path"] = str(refs_path)

        return results

    # ==================== Output Parsing ====================

    def read_reaction_references(
        self, references_path: Optional[str] = None
    ) -> Dict[str, str]:
        """Read TranSyT reaction ID to ModelSEED ID mappings.

        Args:
            references_path: Path to reactions_references.txt file

        Returns:
            Dictionary mapping TranSyT reaction IDs to ModelSEED IDs
        """
        if references_path is None:
            if self.results_path is None:
                raise ValueError("No results path set. Run annotation first.")
            references_path = str(self.results_path / "reactions_references.txt")

        references = {}
        with open(references_path, "r") as f:
            for line in f:
                parts = line.strip().split("\t")
                if len(parts) >= 2:
                    transyt_id = parts[0].strip()
                    mseed_id = (
                        parts[1].strip().replace("[", "").replace("]", "").split("; ")[0]
                    )
                    references[transyt_id] = mseed_id

        return references

    def fix_transyt_sbml(
        self,
        input_path: str,
        output_path: Optional[str] = None,
    ) -> str:
        """Fix TranSyT SBML output formatting issues.

        TranSyT generates SBML with some formatting issues that need
        to be corrected before parsing with COBRApy.

        Args:
            input_path: Path to TranSyT output SBML
            output_path: Optional path for fixed SBML

        Returns:
            Path to the fixed SBML file
        """
        if output_path is None:
            output_path = input_path.replace(".xml", "_fixed.xml")

        sbml_tag = (
            '<sbml xmlns="http://www.sbml.org/sbml/level3/version1/core" '
            'fbc:required="false" groups:required="false" level="3" '
            'sboTerm="SBO:0000624" version="1" '
            'xmlns:fbc="http://www.sbml.org/sbml/level3/version1/fbc/version2" '
            'xmlns:groups="http://www.sbml.org/sbml/level3/version1/groups/version1">'
        )
        model_tag = (
            '<model extentUnits="substance" fbc:strict="true" id="transyt" '
            'metaid="transyt" name="transyt" substanceUnits="substance" timeUnits="time">'
        )

        with open(input_path, "r") as f:
            xml_data = f.readlines()

        xml_fix = ""
        for line in xml_data:
            if line.strip().startswith("<sbml"):
                xml_fix += sbml_tag + "\n"
            elif line.strip().startswith("<model"):
                xml_fix += model_tag + "\n"
            else:
                xml_fix += line

        with open(output_path, "w") as f:
            f.write(xml_fix)

        return output_path

    def parse_transyt_sbml(
        self, sbml_path: str, fix_sbml: bool = True
    ) -> Any:
        """Parse TranSyT SBML output into a COBRA model.

        Args:
            sbml_path: Path to TranSyT SBML file
            fix_sbml: Whether to fix SBML formatting issues first

        Returns:
            COBRA model object
        """
        import cobra

        if fix_sbml:
            sbml_path = self.fix_transyt_sbml(sbml_path)

        return cobra.io.read_sbml_model(sbml_path)

    # ==================== Model Integration ====================

    def get_model_transporters(self, kbase_model: Dict[str, Any]) -> Dict[str, Any]:
        """Identify transport reactions in a KBase model.

        Transport reactions are identified as reactions that involve
        metabolites in multiple compartments.

        Args:
            kbase_model: KBase model dictionary

        Returns:
            Dictionary mapping reaction base IDs to reaction data
        """
        transporters = {}

        for reaction in kbase_model.get("modelreactions", []):
            compartments = set()
            for reagent in reaction.get("modelReactionReagents", []):
                cpd_ref = reagent.get("modelcompound_ref", "")
                if "_" in cpd_ref:
                    compartment = cpd_ref.split("/")[-1].split("_")[1]
                    compartments.add(compartment)

            if len(compartments) > 1:
                base_id = reaction["id"].split("_")[0]
                transporters[base_id] = reaction

        return transporters

    def build_gpr_string(self, kbase_gpr: List[Dict]) -> str:
        """Build GPR string from KBase GPR structure.

        Args:
            kbase_gpr: KBase modelReactionProteins structure

        Returns:
            GPR string in format "(gene1 and gene2) or gene3"
        """
        subunits = self._build_gpr_subunits(kbase_gpr)
        return " or ".join(subunits)

    def _build_gpr_subunits(self, kbase_gpr: List[Dict]) -> List[str]:
        """Build list of GPR subunit strings.

        Args:
            kbase_gpr: KBase modelReactionProteins structure

        Returns:
            List of GPR subunit strings
        """
        gpr_parts = []

        for model_protein in kbase_gpr:
            protein_genes = []
            for model_subunit in model_protein.get("modelReactionProteinSubunits", []):
                for gene_ref in model_subunit.get("feature_refs", []):
                    gene_id = gene_ref.split("/")[-1].strip()
                    if gene_id:
                        protein_genes.append(gene_id)

            if protein_genes:
                protein_genes.sort()
                gpr_parts.append(" and ".join(protein_genes))

        return gpr_parts

    def merge_gpr(
        self,
        existing_gpr: List[Dict],
        new_gpr: List[Dict],
    ) -> List[Dict]:
        """Merge two GPR structures.

        Args:
            existing_gpr: Existing KBase GPR structure
            new_gpr: New KBase GPR structure to merge

        Returns:
            Merged GPR structure
        """
        existing_subunits = set(self._build_gpr_subunits(existing_gpr))

        for model_protein in new_gpr:
            protein_genes = []
            for model_subunit in model_protein.get("modelReactionProteinSubunits", []):
                for gene_ref in model_subunit.get("feature_refs", []):
                    gene_id = gene_ref.split("/")[-1].strip()
                    protein_genes.append(gene_id)
            protein_genes.sort()
            subunit_str = " and ".join(protein_genes)

            if subunit_str not in existing_subunits:
                existing_gpr.append(model_protein)

        return existing_gpr

    def integrate_transyt_results(
        self,
        kbase_model: Dict[str, Any],
        transyt_model: Any,
        references: Dict[str, str],
        rule: str = "merge_reactions_replace_gpr",
        accept_transyt_ids: bool = True,
    ) -> Dict[str, Any]:
        """Integrate TranSyT results into a KBase model.

        Args:
            kbase_model: KBase model dictionary
            transyt_model: COBRA model from TranSyT output
            references: TranSyT ID to ModelSEED ID mapping
            rule: Merge rule (see MERGE_RULES)
            accept_transyt_ids: Whether to accept reactions without ModelSEED IDs

        Returns:
            Dictionary with integration statistics and modified model
        """
        from .cobra_to_kbase_utils import (
            convert_to_kbase_reaction,
            get_compartmets_references,
            get_compounds_references,
            build_model_compound,
            build_model_compartment,
        )

        report = {
            "new_reactions": {},
            "removed_reactions": {},
            "modified_gprs": {},
            "rejected_reactions": {},
            "new_compartments": {},
        }

        # Get existing transporters in model
        existing_transporters = self.get_model_transporters(kbase_model)

        # Get existing compounds and compartments
        existing_compounds = set()
        existing_compartments = set()
        for cpd in kbase_model.get("modelcompounds", []):
            existing_compounds.add(cpd["id"])
        for comp in kbase_model.get("modelcompartments", []):
            existing_compartments.add(comp["id"])

        # Handle replace_all rule - remove existing transporters
        if rule == "replace_all":
            reactions_to_remove = []
            for rxn in kbase_model["modelreactions"]:
                base_id = rxn["id"].split("_")[0]
                if base_id in existing_transporters:
                    reactions_to_remove.append(rxn)
                    report["removed_reactions"][rxn["id"]] = rxn

            for rxn in reactions_to_remove:
                kbase_model["modelreactions"].remove(rxn)

        # Get compound/compartment references from TranSyT model
        compartment_refs = get_compartmets_references(transyt_model)
        compound_refs = get_compounds_references(transyt_model)

        # Process TranSyT reactions
        for reaction in transyt_model.reactions:
            original_id = reaction.id
            reaction_id = reaction.id

            # Map to ModelSEED ID if available
            if reaction_id in references:
                reaction_id = references[reaction_id]
                reaction.id = reaction_id

            # Check if reaction should be saved
            save_reaction = False
            model_reaction = convert_to_kbase_reaction(reaction, compound_refs)

            if rule == "replace_all":
                save_reaction = True
            elif reaction_id in existing_transporters:
                if rule == "merge_reactions_only":
                    continue
                elif rule == "merge_reactions_and_gpr":
                    original_gpr = self.build_gpr_string(
                        existing_transporters[reaction_id]["modelReactionProteins"]
                    )
                    existing_transporters[reaction_id]["modelReactionProteins"] = (
                        self.merge_gpr(
                            existing_transporters[reaction_id]["modelReactionProteins"],
                            model_reaction["modelReactionProteins"],
                        )
                    )
                    new_gpr = self.build_gpr_string(
                        existing_transporters[reaction_id]["modelReactionProteins"]
                    )
                    report["modified_gprs"][original_id] = (original_gpr, new_gpr)
                elif rule == "merge_reactions_replace_gpr":
                    original_gpr = self.build_gpr_string(
                        existing_transporters[reaction_id]["modelReactionProteins"]
                    )
                    new_gpr = self.build_gpr_string(
                        model_reaction["modelReactionProteins"]
                    )
                    existing_transporters[reaction_id]["modelReactionProteins"] = (
                        model_reaction["modelReactionProteins"]
                    )
                    report["modified_gprs"][original_id] = (original_gpr, new_gpr)
            else:
                save_reaction = True

            # Add new reaction and associated compounds/compartments
            if save_reaction and accept_transyt_ids:
                for metabolite in reaction.metabolites:
                    comp_id = metabolite.compartment + "0"

                    # Add new compartment if needed
                    if comp_id not in existing_compartments:
                        model_compartment = build_model_compartment(
                            comp_id,
                            compartment_refs[metabolite.compartment],
                            transyt_model.compartments[metabolite.compartment] + "_0",
                        )
                        kbase_model["modelcompartments"].append(model_compartment)
                        existing_compartments.add(comp_id)
                        report["new_compartments"][comp_id] = transyt_model.compartments[
                            metabolite.compartment
                        ]

                    # Add new compound if needed
                    if metabolite.id not in existing_compounds:
                        model_compound = build_model_compound(metabolite, compartment_refs)
                        kbase_model["modelcompounds"].append(model_compound)
                        existing_compounds.add(metabolite.id)

                kbase_model["modelreactions"].append(model_reaction)
                report["new_reactions"][original_id] = reaction
            elif not accept_transyt_ids:
                report["rejected_reactions"][original_id] = reaction

        return {
            "model": kbase_model,
            "report": report,
        }

    # ==================== High-level Annotation Functions ====================

    def annotate_genome_transporters(
        self,
        genome_ref: str,
        model_ref: Optional[str] = None,
        workspace: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
        output_model_name: Optional[str] = None,
        save_model: bool = True,
    ) -> Dict[str, Any]:
        """Annotate genome with transporter predictions and integrate into model.

        This is the highest-level function for TranSyT annotation workflow.

        Args:
            genome_ref: KBase genome reference
            model_ref: Optional KBase model reference
            workspace: Workspace for saving results
            params: TranSyT parameters
            output_model_name: Name for output model (uses model_ref name if not provided)
            save_model: Whether to save the annotated model to KBase

        Returns:
            Dictionary with annotation results including:
                - model: Annotated model
                - report: Integration statistics
                - transyt_results: Raw TranSyT results
        """
        params = params or {}

        # Get genome
        genome = self.get_msgenome(genome_ref, workspace)

        # Get model if provided
        model = None
        kbase_model = None
        if model_ref:
            model = self.get_model(model_ref, workspace)
            kbase_model = self.kbase_api.get_object(model_ref, workspace)

        # Run TranSyT annotation
        transyt_results = self.run_annotation(
            genome=genome,
            model=model,
            params=params,
        )

        if transyt_results["exit_code"] != 0:
            return {
                "success": False,
                "error": transyt_results.get("error", f"TranSyT failed with exit code {transyt_results['exit_code']}"),
                "transyt_results": transyt_results,
            }

        # Parse results
        if "sbml_path" not in transyt_results:
            return {
                "success": False,
                "error": "TranSyT did not produce output SBML",
                "transyt_results": transyt_results,
            }

        transyt_model = self.parse_transyt_sbml(transyt_results["sbml_path"])
        references = self.read_reaction_references(
            transyt_results.get("references_path")
        )

        # Integrate results into model if provided
        if kbase_model:
            integration_results = self.integrate_transyt_results(
                kbase_model=kbase_model,
                transyt_model=transyt_model,
                references=references,
                rule=params.get("rule", "merge_reactions_replace_gpr"),
                accept_transyt_ids=params.get("accept_transyt_ids", 1) == 1,
            )

            if save_model and workspace:
                model_name = output_model_name or model_ref.split("/")[-1]
                self.kbase_api.save_object(
                    model_name,
                    workspace,
                    "KBaseFBA.FBAModel",
                    integration_results["model"],
                )

            return {
                "success": True,
                "model": integration_results["model"],
                "report": integration_results["report"],
                "transyt_results": transyt_results,
                "transyt_model": transyt_model,
                "references": references,
            }

        # Return just the transyt results if no model integration
        return {
            "success": True,
            "transyt_model": transyt_model,
            "references": references,
            "transyt_results": transyt_results,
        }

    def get_transporter_annotation_summary(
        self, report: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Generate a summary of transporter annotation results.

        Args:
            report: Integration report from integrate_transyt_results

        Returns:
            Summary statistics dictionary
        """
        return {
            "new_reactions_count": len(report.get("new_reactions", {})),
            "removed_reactions_count": len(report.get("removed_reactions", {})),
            "modified_gprs_count": len(report.get("modified_gprs", {})),
            "rejected_reactions_count": len(report.get("rejected_reactions", {})),
            "new_compartments_count": len(report.get("new_compartments", {})),
            "new_reaction_ids": list(report.get("new_reactions", {}).keys()),
            "removed_reaction_ids": list(report.get("removed_reactions", {}).keys()),
            "new_compartment_ids": list(report.get("new_compartments", {}).keys()),
        }

    def cleanup(self) -> None:
        """Clean up temporary files and directories."""
        if self.working_dir.exists():
            shutil.rmtree(self.working_dir)
            self.log_info(f"Cleaned up working directory: {self.working_dir}")
