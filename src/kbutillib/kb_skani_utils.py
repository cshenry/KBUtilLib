"""KBase SKANI utilities for fast genome distance computation and sketching.

This module provides utilities for using SKANI to compute genome distances
and manage sketch databases for efficient genome comparison.
"""

import json
import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from .base_utils import BaseUtils


class KBSKANIUtils(BaseUtils):
    """Utilities for genome distance computation using SKANI.

    This class provides methods to:
    - Check SKANI availability
    - Create and manage sketch databases from genome directories
    - Query genomes against cached sketch databases
    - Compute pairwise genome distances
    """

    def __init__(
        self,
        cache_dir: Optional[str] = None,
        **kwargs: Any
    ) -> None:
        """Initialize KBase SKANI utilities.

        Args:
            cache_dir: Directory for storing sketch databases.
                      Defaults to ~/.kbutillib/skani_cache/
            **kwargs: Additional keyword arguments passed to BaseUtils
        """
        super().__init__(name="KBSKANIUtils", **kwargs)

        # Set up cache directory
        if cache_dir is None:
            home = Path.home()
            self.cache_dir = home / ".kbutillib" / "skani_cache"
        else:
            self.cache_dir = Path(cache_dir)

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.log_info(f"SKANI cache directory: {self.cache_dir}")

        # Check if SKANI is available
        self._check_skani_availability()

    def _check_skani_availability(self) -> bool:
        """Check if SKANI tools are available in the system.

        Returns:
            bool: True if SKANI is available, False otherwise
        """
        try:
            result = subprocess.run(
                ["skani", "--version"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                version = result.stdout.strip()
                self.skani_available = True
                self.log_info(f"SKANI is available: {version}")
                return True
            else:
                self.skani_available = False
                self._log_skani_installation_instructions()
                return False
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            self.skani_available = False
            self._log_skani_installation_instructions()
            return False

    def _log_skani_installation_instructions(self) -> None:
        """Log instructions for installing SKANI."""
        self.log_warning(
            "SKANI not found. Install SKANI to use this functionality:\n"
            "  Via conda: conda install -c bioconda skani\n"
            "  Via cargo: cargo install skani\n"
            "  From source: https://github.com/bluenote-1577/skani"
        )

    def _get_database_path(self, database_name: str) -> Path:
        """Get the path to a sketch database.

        Args:
            database_name: Name of the database

        Returns:
            Path to the database directory
        """
        return self.cache_dir / database_name

    def _get_metadata_file(self, database_name: str) -> Path:
        """Get the path to a database's metadata file.

        Args:
            database_name: Name of the database

        Returns:
            Path to the metadata JSON file
        """
        return self._get_database_path(database_name) / "metadata.json"

    def _load_metadata(self, database_name: str) -> Optional[Dict[str, Any]]:
        """Load metadata for a sketch database.

        Args:
            database_name: Name of the database

        Returns:
            Metadata dictionary, or None if not found
        """
        metadata_file = self._get_metadata_file(database_name)
        if not metadata_file.exists():
            return None

        try:
            with open(metadata_file, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            self.log_error(f"Failed to load metadata for {database_name}: {e}")
            return None

    def _save_metadata(self, database_name: str, metadata: Dict[str, Any]) -> bool:
        """Save metadata for a sketch database.

        Args:
            database_name: Name of the database
            metadata: Metadata dictionary to save

        Returns:
            bool: True if successful, False otherwise
        """
        metadata_file = self._get_metadata_file(database_name)
        try:
            with open(metadata_file, 'w') as f:
                json.dump(metadata, f, indent=2)
            return True
        except IOError as e:
            self.log_error(f"Failed to save metadata for {database_name}: {e}")
            return False

    def sketch_genome_directory(
        self,
        fasta_directory: str,
        database_name: str = "default",
        marker: Optional[str] = None,
        force_rebuild: bool = False,
        threads: int = 1
    ) -> Dict[str, Any]:
        """Create a SKANI sketch database from a directory of FASTA files.

        Args:
            fasta_directory: Directory containing FASTA files to sketch
            database_name: Name for this sketch database (default: "default")
            marker: Marker mode for skani (e.g., --marker-compression)
            force_rebuild: If True, rebuild even if database exists
            threads: Number of threads to use for sketching

        Returns:
            Dict containing:
                - success: bool
                - database_name: str
                - database_path: str
                - genome_count: int
                - genomes: List of genome info dicts

        Raises:
            RuntimeError: If SKANI is not available
            ValueError: If fasta_directory doesn't exist or has no FASTA files
        """
        self.initialize_call(
            "sketch_genome_directory",
            {
                "fasta_directory": fasta_directory,
                "database_name": database_name,
                "marker": marker,
                "force_rebuild": force_rebuild,
                "threads": threads
            },
            print_params=True
        )

        if not self.skani_available:
            raise RuntimeError(
                "SKANI is not available. Please install SKANI first."
            )

        fasta_dir = Path(fasta_directory)
        if not fasta_dir.exists() or not fasta_dir.is_dir():
            raise ValueError(f"Directory not found: {fasta_directory}")

        # Find all FASTA files
        fasta_extensions = ["*.fasta", "*.fa", "*.fna", "*.ffn", "*.faa"]
        fasta_files = []
        for ext in fasta_extensions:
            fasta_files.extend(fasta_dir.glob(ext))

        if not fasta_files:
            raise ValueError(
                f"No FASTA files found in {fasta_directory}. "
                f"Looking for extensions: {', '.join(fasta_extensions)}"
            )

        self.log_info(f"Found {len(fasta_files)} FASTA files to sketch")

        # Create database directory
        db_path = self._get_database_path(database_name)
        db_path.mkdir(parents=True, exist_ok=True)

        sketch_db = db_path / "sketch_db"

        # Check if database exists and force_rebuild is False
        if sketch_db.exists() and not force_rebuild:
            metadata = self._load_metadata(database_name)
            if metadata:
                self.log_info(
                    f"Database '{database_name}' already exists with "
                    f"{metadata.get('genome_count', 0)} genomes. "
                    f"Use force_rebuild=True to recreate."
                )
                return {
                    "success": True,
                    "database_name": database_name,
                    "database_path": str(db_path),
                    "genome_count": metadata.get("genome_count", 0),
                    "genomes": metadata.get("genomes", []),
                    "rebuilt": False
                }

        # Build the skani sketch command
        cmd = ["skani", "sketch"]

        # Add input files
        for fasta_file in fasta_files:
            cmd.append(str(fasta_file))

        # Add output database
        cmd.extend(["-o", str(sketch_db)])

        # Add threads if specified
        if threads > 1:
            cmd.extend(["-t", str(threads)])

        # Add marker compression if specified
        if marker:
            cmd.append(marker)

        self.log_info(f"Creating sketch database: {' '.join(cmd[:5])}...")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600  # 10 minute timeout
            )

            if result.returncode != 0:
                self.log_error(f"skani sketch failed: {result.stderr}")
                return {
                    "success": False,
                    "error": result.stderr,
                    "database_name": database_name
                }

            self.log_info("Sketch database created successfully")

            # Create metadata
            genomes = []
            for fasta_file in fasta_files:
                genomes.append({
                    "id": fasta_file.stem,
                    "filename": fasta_file.name,
                    "source_path": str(fasta_file.absolute()),
                    "sketched_date": datetime.now().isoformat()
                })

            metadata = {
                "database_name": database_name,
                "created": datetime.now().isoformat(),
                "updated": datetime.now().isoformat(),
                "genome_count": len(genomes),
                "genomes": genomes,
                "sketch_file": str(sketch_db)
            }

            self._save_metadata(database_name, metadata)

            return {
                "success": True,
                "database_name": database_name,
                "database_path": str(db_path),
                "genome_count": len(genomes),
                "genomes": genomes,
                "rebuilt": True
            }

        except subprocess.TimeoutExpired:
            self.log_error("skani sketch timed out after 10 minutes")
            return {
                "success": False,
                "error": "Timeout after 10 minutes",
                "database_name": database_name
            }
        except Exception as e:
            self.log_error(f"Failed to create sketch database: {e}")
            return {
                "success": False,
                "error": str(e),
                "database_name": database_name
            }

    def query_genomes(
        self,
        query_fasta: Union[str, List[str]],
        database_name: str = "default",
        min_ani: float = 0.0,
        max_results: Optional[int] = None,
        threads: int = 1
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Query genome(s) against a sketch database.

        Args:
            query_fasta: Path to query FASTA file(s)
            database_name: Name of the sketch database to query against
            min_ani: Minimum ANI threshold (0.0-1.0)
            max_results: Maximum number of results per query (None = all)
            threads: Number of threads to use

        Returns:
            Dict mapping query IDs to lists of hit dictionaries:
            {
                "query_id": [
                    {
                        "reference": str,
                        "ani": float,
                        "align_fraction_query": float,
                        "align_fraction_ref": float
                    }
                ]
            }

        Raises:
            RuntimeError: If SKANI is not available
            ValueError: If database or query files don't exist
        """
        self.initialize_call(
            "query_genomes",
            {
                "query_fasta": query_fasta,
                "database_name": database_name,
                "min_ani": min_ani,
                "max_results": max_results,
                "threads": threads
            },
            print_params=True
        )

        if not self.skani_available:
            raise RuntimeError(
                "SKANI is not available. Please install SKANI first."
            )

        # Check database exists
        db_path = self._get_database_path(database_name)
        sketch_db = db_path / "sketch_db"

        if not sketch_db.exists():
            raise ValueError(
                f"Sketch database '{database_name}' not found. "
                f"Create it first with sketch_genome_directory()."
            )

        # Handle single file or list of files
        if isinstance(query_fasta, str):
            query_files = [Path(query_fasta)]
        else:
            query_files = [Path(f) for f in query_fasta]

        # Validate query files exist
        for qfile in query_files:
            if not qfile.exists():
                raise ValueError(f"Query file not found: {qfile}")

        # Create temporary output file
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.txt', delete=False
        ) as f:
            output_file = f.name

        try:
            # Build skani search command
            cmd = ["skani", "search"]

            # Add query files
            for qfile in query_files:
                cmd.append(str(qfile))

            # Add database
            cmd.extend(["-d", str(sketch_db)])

            # Add output file
            cmd.extend(["-o", output_file])

            # Add threads
            if threads > 1:
                cmd.extend(["-t", str(threads)])

            self.log_info(
                f"Searching {len(query_files)} query genome(s) "
                f"against database '{database_name}'"
            )

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300  # 5 minute timeout
            )

            if result.returncode != 0:
                self.log_error(f"skani search failed: {result.stderr}")
                raise RuntimeError(f"skani search failed: {result.stderr}")

            # Parse results
            results_by_query = self._parse_skani_output(
                output_file,
                min_ani=min_ani,
                max_results=max_results
            )

            self.log_info(
                f"Search completed: found results for "
                f"{len(results_by_query)} query genome(s)"
            )

            return results_by_query

        except subprocess.TimeoutExpired:
            self.log_error("skani search timed out after 5 minutes")
            raise RuntimeError("skani search timed out")
        finally:
            # Clean up temporary file
            try:
                os.unlink(output_file)
            except:
                pass

    def _parse_skani_output(
        self,
        output_file: str,
        min_ani: float = 0.0,
        max_results: Optional[int] = None
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Parse SKANI output file.

        Args:
            output_file: Path to SKANI output file
            min_ani: Minimum ANI threshold
            max_results: Maximum results per query

        Returns:
            Dict mapping query IDs to hit lists
        """
        results_by_query = {}

        try:
            with open(output_file, 'r') as f:
                # Skip header line
                header = f.readline()

                for line in f:
                    if not line.strip():
                        continue

                    parts = line.strip().split('\t')
                    if len(parts) < 5:
                        continue

                    # SKANI output format:
                    # Ref_file Query_file ANI Align_fraction_ref Align_fraction_query ...
                    ref_file = parts[0]
                    query_file = parts[1]
                    ani = float(parts[2]) / 100.0  # Convert percentage to fraction
                    align_frac_ref = float(parts[3])
                    align_frac_query = float(parts[4])

                    # Filter by ANI threshold
                    if ani < min_ani:
                        continue

                    # Extract query ID from filename
                    query_id = Path(query_file).stem

                    # Create hit entry
                    hit = {
                        "reference": Path(ref_file).stem,
                        "reference_file": ref_file,
                        "ani": ani,
                        "align_fraction_query": align_frac_query,
                        "align_fraction_ref": align_frac_ref
                    }

                    if query_id not in results_by_query:
                        results_by_query[query_id] = []

                    results_by_query[query_id].append(hit)

            # Sort results by ANI (highest first) and apply max_results
            for query_id in results_by_query:
                results_by_query[query_id].sort(
                    key=lambda x: x["ani"],
                    reverse=True
                )
                if max_results:
                    results_by_query[query_id] = (
                        results_by_query[query_id][:max_results]
                    )

        except Exception as e:
            self.log_error(f"Failed to parse SKANI output: {e}")
            raise

        return results_by_query

    def list_databases(self) -> List[Dict[str, Any]]:
        """List all available sketch databases.

        Returns:
            List of database info dictionaries
        """
        databases = []

        if not self.cache_dir.exists():
            return databases

        for db_dir in self.cache_dir.iterdir():
            if db_dir.is_dir():
                metadata = self._load_metadata(db_dir.name)
                if metadata:
                    databases.append({
                        "name": db_dir.name,
                        "path": str(db_dir),
                        "genome_count": metadata.get("genome_count", 0),
                        "created": metadata.get("created", "unknown"),
                        "updated": metadata.get("updated", "unknown")
                    })
                else:
                    # Directory exists but no metadata
                    databases.append({
                        "name": db_dir.name,
                        "path": str(db_dir),
                        "genome_count": 0,
                        "created": "unknown",
                        "updated": "unknown",
                        "note": "No metadata found"
                    })

        return databases

    def get_database_info(self, database_name: str) -> Optional[Dict[str, Any]]:
        """Get detailed information about a sketch database.

        Args:
            database_name: Name of the database

        Returns:
            Database metadata dictionary, or None if not found
        """
        return self._load_metadata(database_name)

    def clear_database(self, database_name: str) -> bool:
        """Delete a sketch database and its metadata.

        Args:
            database_name: Name of the database to delete

        Returns:
            bool: True if successful, False otherwise
        """
        db_path = self._get_database_path(database_name)

        if not db_path.exists():
            self.log_warning(f"Database '{database_name}' does not exist")
            return False

        try:
            import shutil
            shutil.rmtree(db_path)
            self.log_info(f"Deleted database '{database_name}'")
            return True
        except Exception as e:
            self.log_error(f"Failed to delete database '{database_name}': {e}")
            return False

    def compute_pairwise_distances(
        self,
        fasta_files: List[str],
        min_ani: float = 0.0,
        threads: int = 1
    ) -> List[Dict[str, Any]]:
        """Compute pairwise distances between genomes without caching.

        Args:
            fasta_files: List of paths to FASTA files
            min_ani: Minimum ANI threshold
            threads: Number of threads to use

        Returns:
            List of pairwise comparison dictionaries

        Raises:
            RuntimeError: If SKANI is not available
        """
        self.initialize_call(
            "compute_pairwise_distances",
            {
                "fasta_files": fasta_files,
                "min_ani": min_ani,
                "threads": threads
            },
            print_params=True
        )

        if not self.skani_available:
            raise RuntimeError(
                "SKANI is not available. Please install SKANI first."
            )

        # Validate files
        for fasta_file in fasta_files:
            if not Path(fasta_file).exists():
                raise ValueError(f"File not found: {fasta_file}")

        # Create temporary output file
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.txt', delete=False
        ) as f:
            output_file = f.name

        try:
            # Build skani dist command
            cmd = ["skani", "dist"]

            # Add files
            for fasta_file in fasta_files:
                cmd.append(str(fasta_file))

            # Add output
            cmd.extend(["-o", output_file])

            # Add threads
            if threads > 1:
                cmd.extend(["-t", str(threads)])

            self.log_info(
                f"Computing pairwise distances for {len(fasta_files)} genomes"
            )

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300
            )

            if result.returncode != 0:
                self.log_error(f"skani dist failed: {result.stderr}")
                raise RuntimeError(f"skani dist failed: {result.stderr}")

            # Parse results - for dist, format is different
            comparisons = []

            with open(output_file, 'r') as f:
                # Skip header
                header = f.readline()

                for line in f:
                    if not line.strip():
                        continue

                    parts = line.strip().split('\t')
                    if len(parts) < 5:
                        continue

                    ref_file = parts[0]
                    query_file = parts[1]
                    ani = float(parts[2]) / 100.0
                    align_frac_ref = float(parts[3])
                    align_frac_query = float(parts[4])

                    if ani >= min_ani:
                        comparisons.append({
                            "genome1": Path(ref_file).stem,
                            "genome2": Path(query_file).stem,
                            "genome1_file": ref_file,
                            "genome2_file": query_file,
                            "ani": ani,
                            "align_fraction_1": align_frac_ref,
                            "align_fraction_2": align_frac_query
                        })

            self.log_info(f"Computed {len(comparisons)} pairwise comparisons")
            return comparisons

        except subprocess.TimeoutExpired:
            self.log_error("skani dist timed out")
            raise RuntimeError("skani dist timed out")
        finally:
            try:
                os.unlink(output_file)
            except:
                pass
