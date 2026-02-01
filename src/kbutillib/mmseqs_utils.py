"""MMseqs2 utilities for protein sequence clustering and searching.

This module provides utilities for using MMseqs2 to cluster protein sequences
and search sequence databases efficiently.
"""

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .shared_env_utils import SharedEnvUtils


class MMSeqsUtils(SharedEnvUtils):
    """Utilities for protein sequence clustering using MMseqs2.

    This class provides methods to:
    - Check MMseqs2 availability
    - Cluster protein sequences with configurable parameters
    - Parse and return clustering results

    Example:
        >>> from kbutillib import MMSeqsUtils
        >>> utils = MMSeqsUtils()
        >>> proteins = [
        ...     {"id": "prot1", "protein_translation": "MKTAYIAKQRQISFVK..."},
        ...     {"id": "prot2", "protein_translation": "MKTAYIAKQRQISFVK..."},
        ... ]
        >>> clusters = utils.cluster_proteins(proteins)
    """

    def __init__(
        self,
        **kwargs: Any
    ) -> None:
        """Initialize MMseqs2 utilities.

        Args:
            **kwargs: Additional keyword arguments passed to SharedEnvUtils
        """
        super().__init__(**kwargs)

        # Get mmseqs executable path from config
        self.mmseqs_executable = self.get_config_value(
            "mmseqs.executable",
            default="mmseqs"
        )

        self.log_info(f"MMSeqsUtils initialized (executable: {self.mmseqs_executable})")

        # Check if MMseqs2 is available
        self._check_mmseqs_availability()

    def _check_mmseqs_availability(self) -> bool:
        """Check if MMseqs2 is available in the system.

        Returns:
            bool: True if MMseqs2 is available, False otherwise
        """
        try:
            result = subprocess.run(
                [self.mmseqs_executable, "version"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                version = result.stdout.strip()
                self.mmseqs_available = True
                self.log_info(f"MMseqs2 is available: {version}")
                return True
            else:
                self.mmseqs_available = False
                self._log_mmseqs_installation_instructions()
                return False
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            self.mmseqs_available = False
            self._log_mmseqs_installation_instructions()
            return False

    def _log_mmseqs_installation_instructions(self) -> None:
        """Log instructions for installing MMseqs2."""
        self.log_warning(
            "MMseqs2 not found. Install MMseqs2 to use this functionality:\n"
            "  Via conda: conda install -c conda-forge -c bioconda mmseqs2\n"
            "  Via brew: brew install mmseqs2\n"
            "  From source: https://github.com/soedinglab/MMseqs2\n"
            f"  Or set 'mmseqs.executable' in config.yaml to the full path"
        )

    def cluster_proteins(
        self,
        proteins: List[Dict[str, Any]],
        min_seq_id: float = 0.5,
        coverage: float = 0.8,
        coverage_mode: int = 0,
        cluster_mode: int = 0,
        sensitivity: Optional[float] = None,
        threads: int = 1,
        extra_args: Optional[List[str]] = None,
        keep_temp_files: bool = False,
        temp_dir: Optional[str] = None
    ) -> Dict[str, Any]:
        """Cluster protein sequences using MMseqs2.

        Args:
            proteins: List of protein dictionaries, each containing at minimum:
                - id: Unique identifier for the protein
                - protein_translation: Amino acid sequence
            min_seq_id: Minimum sequence identity threshold (0.0-1.0). Default: 0.5
            coverage: Minimum coverage threshold (0.0-1.0). Default: 0.8
            coverage_mode: Coverage mode for MMseqs2:
                - 0: Coverage of query and target
                - 1: Coverage of target
                - 2: Coverage of query
                - 3: Target seqlen needs to be X% of query seqlen
                Default: 0
            cluster_mode: Clustering algorithm:
                - 0: Set cover (greedy)
                - 1: Connected component
                - 2: Greedy incremental
                Default: 0
            sensitivity: Sensitivity parameter (-s). Higher is more sensitive but slower.
                If None, uses MMseqs2 default (4.0).
            threads: Number of threads to use. Default: 1
            extra_args: Additional command-line arguments to pass to MMseqs2.
            keep_temp_files: If True, don't delete temporary files. Default: False
            temp_dir: Directory for temporary files. If None, uses system temp.

        Returns:
            Dict containing:
                - success: bool indicating if clustering succeeded
                - clusters: List of cluster dictionaries, each containing:
                    - representative: ID of the cluster representative
                    - members: List of member protein IDs (including representative)
                    - size: Number of proteins in cluster
                - num_clusters: Total number of clusters
                - num_proteins: Total number of input proteins
                - singletons: Number of single-protein clusters
                - parameters: Dict of parameters used
                - error: Error message if success is False

        Raises:
            RuntimeError: If MMseqs2 is not available
            ValueError: If proteins list is empty or malformed

        Example:
            >>> utils = MMSeqsUtils()
            >>> proteins = [
            ...     {"id": "prot1", "protein_translation": "MKTAYIAKQRQISFVK"},
            ...     {"id": "prot2", "protein_translation": "MKTAYIAKQRQISFVK"},
            ...     {"id": "prot3", "protein_translation": "DIFFERENT_SEQUENCE"},
            ... ]
            >>> result = utils.cluster_proteins(proteins, min_seq_id=0.9)
            >>> print(f"Found {result['num_clusters']} clusters")
        """
        self.initialize_call(
            "cluster_proteins",
            {
                "num_proteins": len(proteins),
                "min_seq_id": min_seq_id,
                "coverage": coverage,
                "coverage_mode": coverage_mode,
                "cluster_mode": cluster_mode,
                "sensitivity": sensitivity,
                "threads": threads
            },
            print_params=True
        )

        if not self.mmseqs_available:
            raise RuntimeError(
                "MMseqs2 is not available. Please install MMseqs2 first."
            )

        if not proteins:
            raise ValueError("proteins list cannot be empty")

        # Validate protein data
        for i, protein in enumerate(proteins):
            if "id" not in protein:
                raise ValueError(f"Protein at index {i} missing 'id' field")
            if "protein_translation" not in protein:
                raise ValueError(
                    f"Protein '{protein.get('id', i)}' missing 'protein_translation' field"
                )

        # Create temporary directory for MMseqs2 files
        if temp_dir:
            temp_path = Path(temp_dir)
            temp_path.mkdir(parents=True, exist_ok=True)
            work_dir = tempfile.mkdtemp(dir=temp_path)
        else:
            work_dir = tempfile.mkdtemp(prefix="mmseqs_")

        try:
            # Create ID mapping (MMseqs2 uses numeric IDs internally)
            id_to_name = {}
            name_to_id = {}
            for idx, protein in enumerate(proteins):
                prot_id = protein["id"]
                id_to_name[idx] = prot_id
                name_to_id[prot_id] = idx

            # Write proteins to FASTA file
            fasta_file = Path(work_dir) / "sequences.fasta"
            self._write_fasta(proteins, fasta_file)

            # Define MMseqs2 database and output paths
            db_path = Path(work_dir) / "seqDB"
            cluster_db_path = Path(work_dir) / "clusterDB"
            tmp_path = Path(work_dir) / "tmp"
            tsv_output = Path(work_dir) / "clusters.tsv"

            # Step 1: Create sequence database
            self.log_info("Creating MMseqs2 sequence database...")
            create_db_cmd = [
                self.mmseqs_executable, "createdb",
                str(fasta_file),
                str(db_path)
            ]
            result = subprocess.run(
                create_db_cmd,
                capture_output=True,
                text=True,
                timeout=300
            )
            if result.returncode != 0:
                self.log_error(f"createdb failed: {result.stderr}")
                return {
                    "success": False,
                    "error": f"createdb failed: {result.stderr}",
                    "clusters": [],
                    "num_clusters": 0,
                    "num_proteins": len(proteins)
                }

            # Step 2: Run clustering
            self.log_info(
                f"Clustering {len(proteins)} proteins "
                f"(min_seq_id={min_seq_id}, coverage={coverage})..."
            )
            cluster_cmd = [
                self.mmseqs_executable, "cluster",
                str(db_path),
                str(cluster_db_path),
                str(tmp_path),
                "--min-seq-id", str(min_seq_id),
                "-c", str(coverage),
                "--cov-mode", str(coverage_mode),
                "--cluster-mode", str(cluster_mode),
                "--threads", str(threads)
            ]

            if sensitivity is not None:
                cluster_cmd.extend(["-s", str(sensitivity)])

            if extra_args:
                cluster_cmd.extend(extra_args)

            result = subprocess.run(
                cluster_cmd,
                capture_output=True,
                text=True,
                timeout=600  # 10 minute timeout for clustering
            )
            if result.returncode != 0:
                self.log_error(f"cluster failed: {result.stderr}")
                return {
                    "success": False,
                    "error": f"cluster failed: {result.stderr}",
                    "clusters": [],
                    "num_clusters": 0,
                    "num_proteins": len(proteins)
                }

            # Step 3: Convert results to TSV
            self.log_info("Converting cluster results to TSV...")
            tsv_cmd = [
                self.mmseqs_executable, "createtsv",
                str(db_path),
                str(db_path),
                str(cluster_db_path),
                str(tsv_output)
            ]
            result = subprocess.run(
                tsv_cmd,
                capture_output=True,
                text=True,
                timeout=120
            )
            if result.returncode != 0:
                self.log_error(f"createtsv failed: {result.stderr}")
                return {
                    "success": False,
                    "error": f"createtsv failed: {result.stderr}",
                    "clusters": [],
                    "num_clusters": 0,
                    "num_proteins": len(proteins)
                }

            # Step 4: Parse results
            clusters = self._parse_cluster_tsv(tsv_output)

            # Count singletons
            singletons = sum(1 for c in clusters if c["size"] == 1)

            self.log_info(
                f"Clustering complete: {len(clusters)} clusters "
                f"({singletons} singletons)"
            )

            return {
                "success": True,
                "clusters": clusters,
                "num_clusters": len(clusters),
                "num_proteins": len(proteins),
                "singletons": singletons,
                "parameters": {
                    "min_seq_id": min_seq_id,
                    "coverage": coverage,
                    "coverage_mode": coverage_mode,
                    "cluster_mode": cluster_mode,
                    "sensitivity": sensitivity,
                    "threads": threads,
                    "extra_args": extra_args
                }
            }

        except subprocess.TimeoutExpired as e:
            self.log_error(f"MMseqs2 command timed out: {e}")
            return {
                "success": False,
                "error": f"Timeout: {e}",
                "clusters": [],
                "num_clusters": 0,
                "num_proteins": len(proteins)
            }
        except Exception as e:
            self.log_error(f"Clustering failed: {e}")
            return {
                "success": False,
                "error": str(e),
                "clusters": [],
                "num_clusters": 0,
                "num_proteins": len(proteins)
            }
        finally:
            # Clean up temporary files unless requested to keep them
            if not keep_temp_files:
                try:
                    import shutil
                    shutil.rmtree(work_dir)
                except Exception as e:
                    self.log_warning(f"Failed to clean up temp directory: {e}")
            else:
                self.log_info(f"Temporary files kept at: {work_dir}")

    def _write_fasta(
        self,
        proteins: List[Dict[str, Any]],
        output_path: Path
    ) -> None:
        """Write proteins to a FASTA file.

        Args:
            proteins: List of protein dictionaries with 'id' and 'protein_translation'
            output_path: Path to write the FASTA file
        """
        with open(output_path, 'w') as f:
            for protein in proteins:
                prot_id = protein["id"]
                sequence = protein["protein_translation"]
                # Write FASTA entry
                f.write(f">{prot_id}\n")
                # Write sequence in lines of 80 characters
                for i in range(0, len(sequence), 80):
                    f.write(sequence[i:i+80] + "\n")

    def _parse_cluster_tsv(self, tsv_path: Path) -> List[Dict[str, Any]]:
        """Parse MMseqs2 cluster TSV output.

        The TSV format is: representative_id<tab>member_id

        Args:
            tsv_path: Path to the cluster TSV file

        Returns:
            List of cluster dictionaries
        """
        clusters_dict: Dict[str, List[str]] = {}

        with open(tsv_path, 'r') as f:
            for line in f:
                if not line.strip():
                    continue
                parts = line.strip().split('\t')
                if len(parts) >= 2:
                    representative = parts[0]
                    member = parts[1]

                    if representative not in clusters_dict:
                        clusters_dict[representative] = []
                    clusters_dict[representative].append(member)

        # Convert to list format
        clusters = []
        for representative, members in clusters_dict.items():
            clusters.append({
                "representative": representative,
                "members": members,
                "size": len(members)
            })

        # Sort by cluster size (largest first)
        clusters.sort(key=lambda x: x["size"], reverse=True)

        return clusters

    def easy_cluster(
        self,
        proteins: List[Dict[str, Any]],
        min_seq_id: float = 0.5,
        coverage: float = 0.8,
        threads: int = 1
    ) -> Dict[str, Any]:
        """Simplified clustering interface with sensible defaults.

        This is a convenience wrapper around cluster_proteins() with
        commonly-used default parameters.

        Args:
            proteins: List of protein dictionaries with 'id' and 'protein_translation'
            min_seq_id: Minimum sequence identity (0.0-1.0). Default: 0.5 (50%)
            coverage: Minimum coverage (0.0-1.0). Default: 0.8 (80%)
            threads: Number of threads. Default: 1

        Returns:
            Same as cluster_proteins()

        Example:
            >>> utils = MMSeqsUtils()
            >>> result = utils.easy_cluster(proteins, min_seq_id=0.9)
        """
        return self.cluster_proteins(
            proteins=proteins,
            min_seq_id=min_seq_id,
            coverage=coverage,
            coverage_mode=0,
            cluster_mode=0,
            threads=threads
        )

    def get_cluster_representatives(
        self,
        cluster_result: Dict[str, Any],
        proteins: Optional[List[Dict[str, Any]]] = None
    ) -> List[Dict[str, Any]]:
        """Extract representative proteins from clustering results.

        Args:
            cluster_result: Result dictionary from cluster_proteins()
            proteins: Optional original protein list to return full protein data.
                     If provided, returns full protein dictionaries.
                     If None, returns just IDs.

        Returns:
            List of representative proteins (either full dicts or just IDs)
        """
        if not cluster_result.get("success"):
            return []

        representative_ids = [
            cluster["representative"]
            for cluster in cluster_result.get("clusters", [])
        ]

        if proteins is None:
            return [{"id": rep_id} for rep_id in representative_ids]

        # Build lookup map
        protein_map = {p["id"]: p for p in proteins}

        return [
            protein_map[rep_id]
            for rep_id in representative_ids
            if rep_id in protein_map
        ]

    def get_cluster_membership(
        self,
        cluster_result: Dict[str, Any]
    ) -> Dict[str, str]:
        """Create a mapping of protein IDs to their cluster representative.

        Args:
            cluster_result: Result dictionary from cluster_proteins()

        Returns:
            Dict mapping each protein ID to its cluster representative ID
        """
        membership = {}

        if not cluster_result.get("success"):
            return membership

        for cluster in cluster_result.get("clusters", []):
            representative = cluster["representative"]
            for member in cluster["members"]:
                membership[member] = representative

        return membership
