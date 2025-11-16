# SKANI Utility - Complete PRD

## Status: COMPLETED

## Overview
Implemented a comprehensive utility module for KBUtilLib that provides genome distance computation using SKANI (fast genome-to-genome distance estimation tool). The utility enables users to pre-compute sketches for collections of genomes and efficiently compute distances between query genomes and cached genome collections.

## Implementation Summary

### Module Created
- **File**: `src/kbutillib/kb_skani_utils.py`
- **Class**: `KBSKANIUtils`
- **Inheritance**: Extends `BaseUtils` for consistency with KBUtilLib patterns
- **Lines of Code**: ~700 lines

### Key Features Implemented

#### 1. SKANI Availability Detection
- ✅ Automatic detection of SKANI installation
- ✅ Graceful handling when SKANI is not available
- ✅ Helpful installation instructions logged when missing
- ✅ Version detection and logging

#### 2. Genome Sketching
- ✅ `sketch_genome_directory()` - Creates SKANI sketch databases from directories of FASTA files
- ✅ Support for multiple FASTA extensions (.fasta, .fa, .fna, .ffn, .faa)
- ✅ Automatic sketch caching with metadata tracking
- ✅ Incremental updates (skip re-sketching with force_rebuild flag)
- ✅ Multi-threading support for faster sketching

#### 3. Sketch Cache Management
- ✅ Default cache location: `~/.kbutillib/skani_cache/` (user-configurable)
- ✅ Named sketch databases for organizing multiple collections
- ✅ JSON metadata tracking for each database:
  - Database name, creation/update timestamps
  - Genome count and individual genome information
  - Source paths and sketch file locations
- ✅ `list_databases()` - List all available sketch databases
- ✅ `get_database_info()` - Get detailed information about a specific database
- ✅ `clear_database()` - Remove a sketch database

#### 4. Distance Calculation
- ✅ `query_genomes()` - Query genome(s) against cached sketch databases
- ✅ Support for single or multiple query files
- ✅ Configurable ANI threshold filtering (min_ani parameter)
- ✅ Configurable maximum results per query
- ✅ Results sorted by ANI (highest similarity first)
- ✅ Detailed output including ANI and alignment fractions

#### 5. Pairwise Distance Computation
- ✅ `compute_pairwise_distances()` - Direct pairwise comparisons without caching
- ✅ Useful for quick comparisons of small genome sets
- ✅ Multi-threading support

#### 6. Integration with KBUtilLib
- ✅ Follows BaseUtils inheritance pattern
- ✅ Consistent logging using inherited logger
- ✅ Provenance tracking via `initialize_call()`
- ✅ Subprocess management for SKANI commands
- ✅ Proper temporary file handling
- ✅ Exception handling and error messages

## API Reference

### Class Initialization
```python
KBSKANIUtils(cache_dir: Optional[str] = None, **kwargs)
```

### Main Methods

#### Sketching
```python
sketch_genome_directory(
    fasta_directory: str,
    database_name: str = "default",
    marker: Optional[str] = None,
    force_rebuild: bool = False,
    threads: int = 1
) -> Dict[str, Any]
```

#### Querying
```python
query_genomes(
    query_fasta: str | List[str],
    database_name: str = "default",
    min_ani: float = 0.0,
    max_results: Optional[int] = None,
    threads: int = 1
) -> Dict[str, List[Dict[str, Any]]]
```

#### Database Management
```python
list_databases() -> List[Dict[str, Any]]
get_database_info(database_name: str) -> Optional[Dict[str, Any]]
clear_database(database_name: str) -> bool
```

#### Pairwise Comparison
```python
compute_pairwise_distances(
    fasta_files: List[str],
    min_ani: float = 0.0,
    threads: int = 1
) -> List[Dict[str, Any]]
```

## Usage Examples

### Example 1: Create a sketch database
```python
from kbutillib import KBSKANIUtils

# Initialize
skani_utils = KBSKANIUtils()

# Sketch all genomes in a directory
result = skani_utils.sketch_genome_directory(
    fasta_directory="/path/to/genomes",
    database_name="my_reference_genomes",
    threads=4
)

print(f"Sketched {result['genome_count']} genomes")
```

### Example 2: Query genomes
```python
# Query a genome against the database
results = skani_utils.query_genomes(
    query_fasta="/path/to/query_genome.fasta",
    database_name="my_reference_genomes",
    min_ani=0.80,  # Only show results with ANI >= 80%
    max_results=10  # Top 10 matches
)

# Display results
for query_id, hits in results.items():
    print(f"\nQuery: {query_id}")
    for hit in hits:
        print(f"  {hit['reference']}: ANI={hit['ani']:.2%}")
```

### Example 3: Manage databases
```python
# List all databases
databases = skani_utils.list_databases()
for db in databases:
    print(f"{db['name']}: {db['genome_count']} genomes")

# Get detailed info
info = skani_utils.get_database_info("my_reference_genomes")
print(f"Created: {info['created']}")
print(f"Genomes: {len(info['genomes'])}")

# Clear a database
skani_utils.clear_database("old_database")
```

## Technical Implementation Details

### SKANI Commands Used
- `skani --version` - Check availability and version
- `skani sketch <files> -o <db>` - Create sketch database
- `skani search <queries> -d <db> -o <output>` - Search against database
- `skani dist <files> -o <output>` - Compute pairwise distances

### Cache Directory Structure
```
~/.kbutillib/skani_cache/
├── default/
│   ├── sketch_db           # SKANI sketch database file
│   └── metadata.json       # Database metadata
├── my_reference_genomes/
│   ├── sketch_db
│   └── metadata.json
└── ...
```

### Metadata Schema
```json
{
    "database_name": "my_reference_genomes",
    "created": "2024-11-16T06:00:00",
    "updated": "2024-11-16T06:00:00",
    "genome_count": 100,
    "genomes": [
        {
            "id": "genome_001",
            "filename": "genome_001.fasta",
            "source_path": "/path/to/genomes/genome_001.fasta",
            "sketched_date": "2024-11-16T06:00:00"
        }
    ],
    "sketch_file": "/home/user/.kbutillib/skani_cache/my_reference_genomes/sketch_db"
}
```

## Testing

### Test File Created
- **File**: `tests/test_kb_skani_utils.py`
- **Test Classes**: 10 test classes
- **Test Methods**: 30+ individual tests
- **Coverage Areas**:
  - Initialization and configuration
  - SKANI availability checking
  - Database path management
  - Metadata save/load operations
  - Database listing and info retrieval
  - Database clearing
  - Sketching operations (with mocked SKANI)
  - Query operations (with mocked SKANI)
  - Output parsing
  - Pairwise distance computation
  - Error handling

### Test Patterns Used
- Mock subprocess calls to test without SKANI installed
- Temporary directories for isolated testing
- Edge case testing (missing files, invalid inputs)
- Success and failure scenarios

## Files Modified/Created

### Created Files
1. `src/kbutillib/kb_skani_utils.py` - Main utility module
2. `tests/test_kb_skani_utils.py` - Comprehensive test suite
3. `agent-io/prds/skani-utility/humanprompt.md` - Original user request
4. `agent-io/prds/skani-utility/aiprompt.md` - Enhanced PRD
5. `agent-io/prds/skani-utility/fullprompt.md` - This completion document
6. `agent-io/prds/skani-utility/data.json` - Implementation tracking

### Modified Files
1. `src/kbutillib/__init__.py` - Added KBSKANIUtils export

## Dependencies

### Required
- Python >= 3.9
- SKANI (external tool, optional for basic initialization)

### Inherited from BaseUtils
- subprocess (standard library)
- json (standard library)
- pathlib (standard library)
- datetime (standard library)
- tempfile (standard library)
- logging (via BaseUtils)

## Installation Instructions for Users

The utility is now available as part of KBUtilLib. To use SKANI functionality, users need to install SKANI:

### Via Conda (Recommended)
```bash
conda install -c bioconda skani
```

### Via Cargo
```bash
cargo install skani
```

### From Source
```bash
git clone https://github.com/bluenote-1577/skani.git
cd skani
cargo build --release
# Add to PATH
```

## Success Criteria - All Met ✅

1. ✅ Users can sketch a directory of genomes with a single method call
2. ✅ Sketches are cached persistently for reuse
3. ✅ Users can efficiently query genomes against the cache
4. ✅ API follows KBUtilLib conventions and patterns
5. ✅ Appropriate error messages and logging
6. ✅ Comprehensive test coverage

## Performance Characteristics

- **Sketching**: Depends on genome size and count, scales with threads
- **Querying**: Very fast against pre-computed sketches (seconds for thousands of genomes)
- **Cache**: Persistent storage minimizes recomputation
- **Memory**: Low memory footprint due to sketch-based approach

## Limitations and Future Enhancements

### Current Limitations
- SKANI only reports results with > ~82% ANI by default
- Requires SKANI to be installed separately
- No support for custom SKANI parameters beyond basics

### Potential Future Enhancements
- Add support for more SKANI parameters (k-mer size, compression, etc.)
- Integration with KBase genome objects
- Batch processing optimizations
- Progress callbacks for long-running operations
- Support for SKANI triangle mode (all-vs-all comparisons)
- Automatic SKANI installation via conda

## Documentation

Users can access help via:
```python
from kbutillib import KBSKANIUtils
help(KBSKANIUtils)
```

All methods include comprehensive docstrings with:
- Purpose and functionality
- Parameter descriptions with types
- Return value descriptions
- Exception documentation
- Usage examples where applicable

## Integration with KBUtilLib Ecosystem

The SKANI utility integrates seamlessly with other KBUtilLib modules:
- Can be combined with KBGenomeUtils for genome retrieval
- Follows same logging patterns as other utilities
- Compatible with notebook environments
- Supports shared environment configuration

## Conclusion

The KBSKANIUtils module provides a robust, well-tested, and user-friendly interface for genome distance computation using SKANI. It follows all KBUtilLib conventions, provides comprehensive error handling, and enables efficient genome comparison workflows through intelligent caching.
