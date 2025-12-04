#!/usr/bin/env python
"""Example script demonstrating the refactored SKANI utilities.

This script shows how to use the new config-based SKANI utilities with
JSON cache for managing sketch databases.
"""

import sys
import os

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from kbutillib import SKANIUtils


def main():
    """Demonstrate SKANI utilities functionality."""

    print("=" * 80)
    print("SKANI Utilities - Configuration & JSON Cache Example")
    print("=" * 80)
    print()

    print("CONFIGURATION:")
    print("-" * 80)
    print("The SKANI utilities now use config.yaml for configuration:")
    print()
    print("skani:")
    print("  executable: 'skani'  # Or full path like '/usr/local/bin/skani'")
    print("  cache_file: '~/.kbutillib/skani_databases.json'")
    print()

    print("=" * 80)
    print()
    print("USAGE EXAMPLES:")
    print("-" * 80)
    print()

    print("1. Initialize SKANIUtils:")
    print("-" * 40)
    print("""
    from kbutillib import SKANIUtils

    # Uses config values by default
    util = SKANIUtils()

    # Or specify custom cache file
    util = SKANIUtils(cache_file="/path/to/my_databases.json")
    """)

    print("2. Create a sketch database:")
    print("-" * 40)
    print("""
    # Sketch a directory of FASTA files
    result = util.sketch_genome_directory(
        fasta_directory="/path/to/genomes",
        database_name="my_genomes",
        description="My genome collection for analysis",
        threads=4
    )

    # Result stored in JSON cache with metadata:
    # {
    #   "my_genomes": {
    #     "path": "/home/user/.kbutillib/skani_sketches/my_genomes",
    #     "description": "My genome collection for analysis",
    #     "created": "2025-12-02T10:30:00",
    #     "genome_count": 50,
    #     "genomes": [...],
    #     "metadata": {"threads": 4}
    #   }
    # }
    """)

    print("3. Add an existing sketch database to cache:")
    print("-" * 40)
    print("""
    # Register a database that was created externally
    util.add_skani_database(
        database_name="gtdb_bacteria",
        database_path="/data/gtdb/bacteria_r214_sketches",
        description="GTDB bacterial representatives r214",
        genome_count=85205
    )

    # Now it's available in the cache for queries
    """)

    print("4. List all available databases:")
    print("-" * 40)
    print("""
    databases = util.list_databases()

    for db in databases:
        print(f"Name: {db['name']}")
        print(f"  Path: {db['path']}")
        print(f"  Description: {db['description']}")
        print(f"  Genomes: {db['genome_count']}")
        print(f"  Created: {db['created']}")
        print()
    """)

    print("5. Query genomes against a database:")
    print("-" * 40)
    print("""
    # Query uses the database from cache
    results = util.query_genomes(
        query_fasta="/path/to/query_genome.fasta",
        database_name="my_genomes",
        min_ani=0.95,
        threads=4
    )

    for query_id, hits in results.items():
        print(f"Query: {query_id}")
        for hit in hits:
            print(f"  {hit['reference']}: ANI={hit['ani']:.4f}")
    """)

    print("6. Get detailed database information:")
    print("-" * 40)
    print("""
    db_info = util.get_database_info("my_genomes")

    if db_info:
        print(f"Path: {db_info['path']}")
        print(f"Genome count: {db_info['genome_count']}")
        print(f"Description: {db_info['description']}")
        print(f"Created: {db_info['created']}")
    """)

    print("7. Remove a database from cache:")
    print("-" * 40)
    print("""
    # Remove from cache only (keep files)
    util.remove_database("my_genomes", delete_files=False)

    # Remove from cache AND delete files
    util.remove_database("my_genomes", delete_files=True)
    """)

    print()
    print("=" * 80)
    print()
    print("KEY BENEFITS:")
    print("-" * 80)
    print("- Configuration via config.yaml (skani executable path)")
    print("- Single JSON file tracks all sketch databases")
    print("- Database registry with names, paths, descriptions, metadata")
    print("- Easy to add externally-created databases")
    print("- Simple database discovery and management")
    print("- Portable cache file can be shared across systems")
    print()

    print("=" * 80)
    print()
    print("JSON CACHE STRUCTURE:")
    print("-" * 80)
    print("""
{
  "database_name": {
    "path": "/path/to/sketch_database",
    "description": "Database description",
    "created": "2025-12-02T10:30:00",
    "updated": "2025-12-02T10:30:00",
    "genome_count": 100,
    "source_directory": "/original/fasta/directory",
    "genomes": [
      {
        "id": "genome_001",
        "filename": "genome_001.fasta",
        "source_path": "/original/fasta/directory/genome_001.fasta",
        "sketched_date": "2025-12-02T10:30:00"
      }
    ],
    "metadata": {
      "marker": null,
      "threads": 4
    }
  }
}
    """)

    print("=" * 80)


if __name__ == '__main__':
    main()
