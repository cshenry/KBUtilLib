"""SKANI utilities for fast genome distance computation and sketching.

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

from .shared_env_utils import SharedEnvUtils


class SKANIUtils(SharedEnvUtils):
    """Utilities for genome distance computation using SKANI.

    This class provides methods to:
    - Check SKANI availability
    - Create and manage sketch databases from genome directories
    - Query genomes against cached sketch databases
    - Compute pairwise genome distances
    """

    def __init__(
        self,
        cache_file: Optional[str] = None,
        **kwargs: Any
    ) -> None:
        """Initialize SKANI utilities.

        Args:
            cache_file: Path to JSON file for tracking sketch databases.
                       Defaults to config value or ~/.kbutillib/skani_databases.json
            **kwargs: Additional keyword arguments passed to SharedEnvUtils
        """
        super().__init__(**kwargs)

        # Get skani executable path from config
        self.skani_executable = self.get_config_value(
            "skani.executable",
            default="skani"
        )

        # Set up cache file path
        if cache_file is None:
            default_cache = self.get_config_value(
                "skani.cache_file",
                default="~/.kbutillib/skani_databases.json"
            )
            cache_file = os.path.expanduser(default_cache)

        self.cache_file = Path(cache_file)

        # Ensure cache file directory exists
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)

        # Initialize cache file if it doesn't exist
        if not self.cache_file.exists():
            self._save_cache({})

        self.log_info(f"SKANI database cache: {self.cache_file}")

        # Check if SKANI is available
        self._check_skani_availability()

    def _check_skani_availability(self) -> bool:
        """Check if SKANI tools are available in the system.

        Returns:
            bool: True if SKANI is available, False otherwise
        """
        try:
            result = subprocess.run(
                [self.skani_executable, "--version"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                version = result.stdout.strip()
                self.skani_available = True
                self.log_info(f"SKANI is available: {version} (executable: {self.skani_executable})")
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
            "  From source: https://github.com/bluenote-1577/skani\n"
            f"  Or set 'skani.executable' in config.yaml to the full path"
        )

    def _load_cache(self) -> Dict[str, Dict[str, Any]]:
        """Load the sketch database cache from JSON file.

        Returns:
            Dictionary mapping database names to their metadata
        """
        try:
            with open(self.cache_file, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            self.log_error(f"Failed to load cache file: {e}")
            return {}

    def _save_cache(self, cache: Dict[str, Dict[str, Any]]) -> bool:
        """Save the sketch database cache to JSON file.

        Args:
            cache: Dictionary mapping database names to their metadata

        Returns:
            bool: True if successful, False otherwise
        """
        try:
            with open(self.cache_file, 'w') as f:
                json.dump(cache, f, indent=2)
            return True
        except IOError as e:
            self.log_error(f"Failed to save cache file: {e}")
            return False

    def _get_database_info(self, database_name: str) -> Optional[Dict[str, Any]]:
        """Get information about a database from cache.

        Args:
            database_name: Name of the database

        Returns:
            Database metadata dictionary, or None if not found
        """
        cache = self._load_cache()
        return cache.get(database_name)

    def sketch_genome_directory(
        self,
        fasta_directory: str,
        database_name: str = "default",
        database_path: Optional[str] = None,
        description: Optional[str] = None,
        marker: Optional[str] = None,
        force_rebuild: bool = False,
        threads: int = 1
    ) -> Dict[str, Any]:
        """Create a SKANI sketch database from a directory of FASTA files.

        Args:
            fasta_directory: Directory containing FASTA files to sketch
            database_name: Name for this sketch database (default: "default")
            database_path: Path where sketch database will be stored.
                          If None, uses ~/.kbutillib/skani_sketches/<database_name>
            description: Optional description of this database
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

        # Load existing cache
        cache = self._load_cache()

        # Determine database path
        if database_path is None:
            sketch_dir = Path.home() / ".kbutillib" / "skani_sketches"
            sketch_dir.mkdir(parents=True, exist_ok=True)
            db_path = sketch_dir / database_name
        else:
            db_path = Path(database_path)

        db_path.mkdir(parents=True, exist_ok=True)
        sketch_db = db_path / "sketch_db"

        # Check if database exists in cache and force_rebuild is False
        if database_name in cache and not force_rebuild:
            existing_info = cache[database_name]
            self.log_info(
                f"Database '{database_name}' already exists with "
                f"{existing_info.get('genome_count', 0)} genomes. "
                f"Use force_rebuild=True to recreate."
            )
            return {
                "success": True,
                "database_name": database_name,
                "database_path": existing_info.get("path", str(db_path)),
                "genome_count": existing_info.get("genome_count", 0),
                "genomes": existing_info.get("genomes", []),
                "rebuilt": False
            }

        # Build the skani sketch command
        cmd = [self.skani_executable, "sketch"]

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

            # Create genome metadata
            genomes = []
            for fasta_file in fasta_files:
                genomes.append({
                    "id": fasta_file.stem,
                    "filename": fasta_file.name,
                    "source_path": str(fasta_file.absolute()),
                    "sketched_date": datetime.now().isoformat()
                })

            # Create database entry for cache
            db_entry = {
                "path": str(db_path),
                "description": description or f"Sketch database from {fasta_directory}",
                "created": datetime.now().isoformat(),
                "updated": datetime.now().isoformat(),
                "genome_count": len(genomes),
                "source_directory": str(fasta_dir.absolute()),
                "genomes": genomes,
                "metadata": {
                    "marker": marker,
                    "threads": threads
                }
            }

            # Update cache
            cache[database_name] = db_entry
            self._save_cache(cache)

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

    def add_skani_database(
        self,
        database_name: str,
        database_path: str,
        description: str = "",
        genome_count: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> bool:
        """Add a new SKANI sketch database to the cache.

        This allows you to register existing sketch databases that were created
        outside of this utility or manually.

        Args:
            database_name: Name to register the database under
            database_path: Path to the directory containing the sketch database
            description: Optional description of the database
            genome_count: Number of genomes in the database (optional)
            metadata: Additional metadata to store (optional)

        Returns:
            bool: True if successful, False if database_name already exists

        Example:
            >>> util = SKANIUtils()
            >>> util.add_skani_database(
            ...     "gtdb_bacteria",
            ...     "/data/gtdb/bacteria_sketches",
            ...     description="GTDB bacterial representatives r214"
            ... )
        """
        cache = self._load_cache()

        if database_name in cache:
            self.log_warning(
                f"Database '{database_name}' already exists in cache. "
                f"Use a different name or remove the existing entry first."
            )
            return False

        db_path = Path(database_path)
        if not db_path.exists():
            self.log_warning(f"Database path does not exist: {database_path}")

        # Create database entry
        db_entry = {
            "path": str(db_path),
            "description": description,
            "created": datetime.now().isoformat(),
            "updated": datetime.now().isoformat(),
            "genome_count": genome_count or 0,
            "metadata": metadata or {}
        }

        # Update cache
        cache[database_name] = db_entry
        self._save_cache(cache)

        self.log_info(f"Added database '{database_name}' to cache at {database_path}")
        return True

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

        # Check database exists in cache
        db_info = self._get_database_info(database_name)
        if not db_info:
            raise ValueError(
                f"Sketch database '{database_name}' not found in cache. "
                f"Create it first with sketch_genome_directory() or add it with add_skani_database()."
            )

        db_path = Path(db_info["path"])
        sketch_db = db_path

        if not sketch_db.exists():
            raise ValueError(
                f"Sketch database file not found: {sketch_db}. "
                f"The database may have been moved or deleted."
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
            cmd = [self.skani_executable, "search"]

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
        """List all available sketch databases from cache.

        Returns:
            List of database info dictionaries with keys:
                - name: Database name
                - path: Path to database directory
                - description: Database description
                - genome_count: Number of genomes
                - created: Creation timestamp
                - updated: Last update timestamp
        """
        cache = self._load_cache()
        databases = []

        for db_name, db_info in cache.items():
            databases.append({
                "name": db_name,
                "path": db_info.get("path", ""),
                "description": db_info.get("description", ""),
                "genome_count": db_info.get("genome_count", 0),
                "created": db_info.get("created", "unknown"),
                "updated": db_info.get("updated", "unknown")
            })

        return databases

    def get_database_info(self, database_name: str) -> Optional[Dict[str, Any]]:
        """Get detailed information about a sketch database.

        Args:
            database_name: Name of the database

        Returns:
            Database metadata dictionary, or None if not found
        """
        return self._get_database_info(database_name)

    def remove_database(self, database_name: str, delete_files: bool = False) -> bool:
        """Remove a sketch database from the cache.

        Args:
            database_name: Name of the database to remove
            delete_files: If True, also delete the database files from disk

        Returns:
            bool: True if successful, False otherwise
        """
        cache = self._load_cache()

        if database_name not in cache:
            self.log_warning(f"Database '{database_name}' not found in cache")
            return False

        db_info = cache[database_name]

        # Optionally delete files
        if delete_files:
            db_path = Path(db_info["path"])
            if db_path.exists():
                try:
                    import shutil
                    shutil.rmtree(db_path)
                    self.log_info(f"Deleted database files at {db_path}")
                except Exception as e:
                    self.log_error(f"Failed to delete database files: {e}")
                    return False

        # Remove from cache
        del cache[database_name]
        self._save_cache(cache)
        self.log_info(f"Removed database '{database_name}' from cache")
        return True

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
            cmd = [self.skani_executable, "dist"]

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
