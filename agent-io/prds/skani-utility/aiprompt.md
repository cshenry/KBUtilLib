# SKANI Utility - Enhanced PRD

## Overview
Implement a utility module for KBUtilLib that provides genome distance computation using SKANI (fast genome-to-genome distance estimation tool). The utility will enable users to pre-compute sketches for a collection of genomes and efficiently compute distances between query genomes and the cached genome collection.

## Background
SKANI is a modern tool for Average Nucleotide Identity (ANI) calculation that uses k-mer sketching for fast, accurate genome distance estimation. It's particularly useful for:
- Genome classification and taxonomic assignment
- Finding the closest reference genome for a query
- Large-scale genome comparisons

## Features

### 1. SKANI Availability Check
- Detect if SKANI is installed on the system
- Provide helpful installation instructions if not available
- Handle graceful degradation

### 2. Genome Sketching
- Accept a directory containing FASTA files
- Create SKANI sketches for all genomes in the directory
- Store sketches in a local cache directory
- Maintain metadata about sketched genomes (filename, path, timestamp)
- Support incremental updates (skip already-sketched genomes)

### 3. Sketch Cache Management
- Store sketches in `~/.kbutillib/skani_cache/` by default (user-configurable)
- Save/load metadata about cached sketches
- Support multiple named sketch databases
- Provide methods to list, clear, and manage cache

### 4. Distance Calculation
- Accept query genome(s) as FASTA file(s) or sequences
- Compute ANI/distance against all cached genome sketches
- Return results sorted by similarity
- Support batch queries for multiple genomes
- Allow filtering results by distance threshold

### 5. Integration with KBUtilLib Patterns
- Inherit from BaseUtils for consistency
- Follow logging and error handling patterns
- Support provenance tracking via initialize_call()
- Use subprocess for SKANI command execution
- Handle temporary files appropriately

## API Design

```python
class KBSKANIUtils(BaseUtils):
    def __init__(self, cache_dir: str = None, **kwargs):
        """Initialize with optional cache directory."""

    def sketch_genome_directory(
        self,
        fasta_directory: str,
        database_name: str = "default",
        marker: str = None,
        force_rebuild: bool = False
    ) -> Dict[str, Any]:
        """Sketch all FASTA files in a directory and cache them."""

    def query_genomes(
        self,
        query_fasta: str | List[str],
        database_name: str = "default",
        min_ani: float = 0.0,
        max_results: int = None
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Compute distances between query genome(s) and cached sketches."""

    def list_databases(self) -> List[str]:
        """List available sketch databases."""

    def clear_database(self, database_name: str) -> bool:
        """Clear a specific sketch database."""

    def get_database_info(self, database_name: str) -> Dict[str, Any]:
        """Get information about a sketch database."""
```

## Technical Details

### SKANI Commands Used
- `skani sketch` - Create sketches from FASTA files
- `skani search` or `skani dist` - Compute distances

### Cache Structure
```
~/.kbutillib/skani_cache/
├── default/
│   ├── sketches/           # Sketch files
│   ├── metadata.json       # Database metadata
│   └── genomes.txt         # List of genome files
└── other_db/
    └── ...
```

### Metadata Format
```json
{
    "database_name": "default",
    "created": "2024-01-01T00:00:00",
    "updated": "2024-01-02T00:00:00",
    "genome_count": 100,
    "genomes": [
        {
            "id": "genome_001",
            "filename": "genome_001.fasta",
            "source_path": "/path/to/original/genome_001.fasta",
            "sketch_file": "sketches/genome_001.sketch",
            "sketched_date": "2024-01-01T00:00:00"
        }
    ]
}
```

## Testing Requirements
1. Test SKANI availability checking
2. Test sketch creation (mock SKANI if not installed)
3. Test cache management (create, list, clear)
4. Test distance calculations
5. Test error handling (missing files, invalid inputs)

## Dependencies
- SKANI (external tool, not a Python package)
- Standard library: subprocess, json, pathlib, tempfile
- Existing: BaseUtils from KBUtilLib

## Installation Notes for Users
```bash
# Ubuntu/Debian
conda install -c bioconda skani

# or from source
cargo install skani
```

## Success Criteria
1. Users can sketch a directory of genomes with a single method call
2. Sketches are cached persistently for reuse
3. Users can efficiently query genomes against the cache
4. API follows KBUtilLib conventions and patterns
5. Appropriate error messages and logging
6. Tests provide good coverage
