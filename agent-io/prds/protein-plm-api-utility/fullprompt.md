# Protein PLM API Utility - Complete PRD

## Executive Summary

This PRD describes the implementation of `KBPLMUtils`, a new utility module for the KBUtilLib framework that enables protein homology search using the KBase Protein Language Model (PLM) API combined with BLAST analysis. The module provides a comprehensive workflow for finding the best protein homologs from UniProt for features in KBase genome objects.

## Problem Statement

Researchers working with KBase genome data need an efficient way to:
1. Find protein homologs for features in their genome objects
2. Leverage modern protein language models for initial candidate identification
3. Validate candidates using traditional sequence alignment (BLAST)
4. Obtain UniProt identifiers for best matches to enable cross-referencing with other databases

## Solution

The `KBPLMUtils` module provides a complete pipeline that:
1. Extracts protein sequences from KBase feature container objects
2. Queries the KBase PLM API to find top candidates based on embedding similarity
3. Retrieves full sequences from UniProt for candidate proteins
4. Creates a custom BLAST database from candidates
5. Performs BLASTP searches to find best matches
6. Returns comprehensive results with UniProt IDs and scoring metrics

## Technical Architecture

### Class Hierarchy
```
BaseUtils
  └─ KBWSUtils
       └─ KBGenomeUtils
            └─ KBPLMUtils
```

### Key Methods

#### `query_plm_api(query_sequences, max_hits, similarity_threshold, return_embeddings)`
Queries the PLM API for protein homologs.

**Parameters:**
- `query_sequences`: List of dicts with 'id' and 'sequence' keys
- `max_hits`: Maximum number of hits per query (1-100)
- `similarity_threshold`: Minimum similarity score (0.0-1.0)
- `return_embeddings`: Whether to include embedding vectors

**Returns:** Dict with hits for each query sequence

#### `get_uniprot_sequences(uniprot_ids)`
Retrieves protein sequences from UniProt REST API.

**Parameters:**
- `uniprot_ids`: List of UniProt identifiers

**Returns:** Dict mapping UniProt IDs to sequences

#### `create_blast_database(sequences, db_path)`
Creates a BLAST protein database.

**Parameters:**
- `sequences`: Dict mapping sequence IDs to sequences
- `db_path`: Path for database files

**Returns:** Boolean indicating success

**Requirements:** NCBI BLAST+ tools installed

#### `run_blastp(query_sequences, database_path, evalue, max_target_seqs)`
Runs BLASTP search against custom database.

**Parameters:**
- `query_sequences`: Dict mapping query IDs to sequences
- `database_path`: Path to BLAST database
- `evalue`: E-value threshold (default: 0.001)
- `max_target_seqs`: Max hits per query (default: 1)

**Returns:** Dict mapping query IDs to BLAST hits

**Requirements:** NCBI BLAST+ tools installed

#### `find_best_hits_for_features(feature_container_name, max_plm_hits, similarity_threshold, blast_evalue, temp_dir)`
Main workflow method that orchestrates the entire pipeline.

**Parameters:**
- `feature_container_name`: Name of loaded feature container
- `max_plm_hits`: Max hits from PLM API (default: 100)
- `similarity_threshold`: PLM similarity threshold (default: 0.0)
- `blast_evalue`: BLAST E-value threshold (default: 0.001)
- `temp_dir`: Directory for temporary files (default: system temp)

**Returns:** Dict with comprehensive hit information per feature:
```python
{
    "feature_id": {
        "best_uniprot_id": str,
        "plm_score": float,
        "blast_evalue": float,
        "blast_bit_score": float,
        "blast_identity": int,
        "all_plm_hits": List[str]
    }
}
```

**Requirements:**
- Feature container must be loaded via `load_kbase_gene_container()`
- NCBI BLAST+ tools must be installed

#### `get_best_uniprot_ids(feature_container_name, **kwargs)`
Simplified method returning just the best UniProt ID for each feature.

**Parameters:**
- `feature_container_name`: Name of loaded feature container
- `**kwargs`: Additional parameters for `find_best_hits_for_features()`

**Returns:** Dict mapping feature IDs to UniProt IDs

## API Integration

### KBase PLM API

**Base URL:** `https://kbase.us/services/llm_homology_api`

**Search Endpoint:** `POST /search`

**Request Schema:**
```json
{
  "query_sequences": [
    {
      "id": "feature_id",
      "sequence": "MKLAVLGAAV..."
    }
  ],
  "max_hits": 100,
  "similarity_threshold": 0.0,
  "best_hit_only": false,
  "return_query_embeddings": false,
  "return_hit_embeddings": false
}
```

**Response Schema:**
```json
{
  "hits": [
    {
      "query_id": "feature_id",
      "best_hit": {
        "id": "P12345",
        "score": 0.95,
        "embedding": [...]
      },
      "hits": [
        {
          "id": "P12345",
          "score": 0.95,
          "embedding": [...]
        }
      ],
      "query_embedding": [...],
      "total_hits": 100
    }
  ]
}
```

**Constraints:**
- Max 500 protein sequences per request
- Max 5000 residues per sequence
- Max 100 hits returned per query

### UniProt REST API

**Base URL:** `https://rest.uniprot.org/uniprotkb`

**Sequence Retrieval:** `GET /{uniprot_id}.fasta`

**Response:** FASTA format protein sequence

## Dependencies

### Python Libraries
- **Standard Library:**
  - `json`: JSON parsing
  - `os`: File operations
  - `subprocess`: BLAST command execution
  - `tempfile`: Temporary file management
  - `pathlib`: Path handling
  - `typing`: Type hints

- **External (already available):**
  - `requests`: HTTP API calls

### System Requirements
- **NCBI BLAST+ (optional):**
  - `blastp`: Protein-protein BLAST search
  - `makeblastdb`: BLAST database creation
  - Install on Ubuntu/Debian: `sudo apt-get install ncbi-blast+`
  - Install on MacOS: `brew install blast`
  - **Note:** Module works without BLAST but with limited functionality

## Error Handling

### Validation Errors
- Empty query sequences
- Invalid max_hits range (must be 1-100)
- Feature container not loaded
- No features found in container
- No protein sequences in features

### API Errors
- PLM API timeouts (300s timeout)
- PLM API HTTP errors
- UniProt retrieval failures (per-sequence, non-fatal)

### BLAST Errors
- BLAST tools not available (warning on initialization)
- Database creation failures
- BLAST search failures

### File System Errors
- Temporary file creation failures
- Database cleanup failures (logged as warnings)

## Logging

The module uses the inherited BaseUtils logging infrastructure:

- **INFO:** Normal operation messages
  - Initialization
  - API queries
  - Database creation
  - BLAST execution
  - Results compilation

- **WARNING:** Non-fatal issues
  - BLAST not available
  - Individual sequence retrieval failures
  - Temporary file cleanup failures
  - No BLAST hits for a feature

- **ERROR:** Fatal errors
  - API request failures
  - Database creation failures
  - BLAST execution failures

## Usage Examples

### Example 1: Complete Workflow

```python
from kbutillib import KBPLMUtils

# Initialize
plm_utils = KBPLMUtils(log_level="INFO")

# Load genome from file
plm_utils.load_kbase_gene_container(
    "genome.json",
    localname="my_genome"
)

# Find best hits with full details
results = plm_utils.find_best_hits_for_features(
    feature_container_name="my_genome",
    max_plm_hits=100,
    similarity_threshold=0.0,
    blast_evalue=0.001
)

# Process results
for feature_id, hit_data in results.items():
    print(f"Feature: {feature_id}")
    print(f"  Best Hit: {hit_data['best_uniprot_id']}")
    print(f"  PLM Score: {hit_data['plm_score']:.3f}")
    print(f"  BLAST E-value: {hit_data['blast_evalue']:.2e}")
    print(f"  Total Candidates: {len(hit_data['all_plm_hits'])}")
```

### Example 2: Simplified Interface

```python
from kbutillib import KBPLMUtils

plm_utils = KBPLMUtils()
plm_utils.load_kbase_gene_container("genome.json", localname="genome")

# Get just the UniProt IDs
mapping = plm_utils.get_best_uniprot_ids("genome", max_plm_hits=50)

for feature_id, uniprot_id in mapping.items():
    print(f"{feature_id} -> {uniprot_id}")
```

### Example 3: Direct API Query

```python
from kbutillib import KBPLMUtils

plm_utils = KBPLMUtils()

# Query with custom sequences
results = plm_utils.query_plm_api(
    query_sequences=[
        {
            "id": "my_protein",
            "sequence": "MKLAVLGAAVLGAAVIGPGQFHQ..."
        }
    ],
    max_hits=10,
    similarity_threshold=0.5
)

# Process hits
for hit_data in results["hits"]:
    print(f"Query: {hit_data['query_id']}")
    print(f"Best hit: {hit_data['best_hit']['id']} "
          f"(score: {hit_data['best_hit']['score']:.3f})")
```

### Example 4: UniProt Sequence Retrieval

```python
from kbutillib import KBPLMUtils

plm_utils = KBPLMUtils()

# Get sequences for specific UniProt IDs
sequences = plm_utils.get_uniprot_sequences(
    ["P12345", "Q9Y6K9", "O15552"]
)

for uniprot_id, sequence in sequences.items():
    print(f"{uniprot_id}: {len(sequence)} residues")
```

## Testing Strategy

### Unit Tests
- Module import
- Class instantiation
- API URL configuration
- BLAST availability check

### Integration Tests (require external services)
- PLM API query
- UniProt sequence retrieval
- BLAST database creation (requires BLAST)
- BLAST search (requires BLAST)

### End-to-End Tests (require full setup)
- Complete workflow with test genome
- Error handling scenarios
- Cleanup verification

## Success Metrics

### Functional
- ✅ Module imports successfully
- ✅ Class instantiates without errors
- ✅ BLAST availability checked on init
- ✅ All public methods have docstrings
- ✅ Follows existing code patterns
- ✅ Proper error handling implemented
- ✅ Logging at appropriate levels

### Performance (not measured in this implementation)
- PLM API queries complete within 5 minutes for 100 sequences
- BLAST searches complete within 5 minutes for 100 queries
- Temporary files cleaned up properly

## Future Enhancements

### Potential Improvements
1. **Batch Processing:** Handle large genomes with automatic batching
2. **Caching:** Cache PLM results and UniProt sequences
3. **Alternative BLAST:** Support other alignment tools (Diamond, MMseqs2)
4. **Parallel Processing:** Parallelize UniProt retrieval and BLAST
5. **Result Persistence:** Save results to workspace objects
6. **Visualization:** Generate alignment visualizations
7. **Alternative APIs:** Support other protein embedding APIs

### Not Implemented (Out of Scope)
- Direct protein structure prediction
- Multiple sequence alignment
- Phylogenetic analysis
- Functional annotation prediction beyond homology

## Implementation Status

### Completed
- ✅ Core module implementation (`kb_plm_utils.py`)
- ✅ Package exports updated (`__init__.py`)
- ✅ Usage examples created (`examples/plm_utils_example.py`)
- ✅ Documentation (this PRD)
- ✅ Basic testing (import and instantiation)

### Not Implemented
- ❌ Comprehensive unit tests
- ❌ Integration tests
- ❌ End-to-end tests
- ❌ Performance benchmarks

## References

### API Documentation
- KBase PLM API: https://kbase.us/services/llm_homology_api/docs
- Protein Happi GitHub: https://github.com/bio-boris/protein_happi
- UniProt REST API: https://www.uniprot.org/help/api

### Tools
- NCBI BLAST+: https://blast.ncbi.nlm.nih.gov/Blast.cgi?PAGE_TYPE=BlastDocs&DOC_TYPE=Download
- BLAST Command Line Documentation: https://www.ncbi.nlm.nih.gov/books/NBK279690/

### Related KBase Documentation
- KBase Workspace API: https://kbase.us/services/ws/docs/
- KBase Data Types: https://kbase.us/data-types/

## File Locations

### Implementation
- Module: `src/kbutillib/kb_plm_utils.py`
- Package: `src/kbutillib/__init__.py`

### Documentation
- PRD: `agent-io/prds/protein-plm-api-utility/`
- Example: `examples/plm_utils_example.py`

### Data
- Tracking: `agent-io/prds/protein-plm-api-utility/data.json`

## Conclusion

The `KBPLMUtils` module successfully implements a comprehensive protein homology search pipeline that combines modern protein language models with traditional sequence alignment. The implementation follows existing code patterns, provides clear error handling and logging, and works gracefully with or without BLAST installed. The module is ready for use in KBase workflows requiring protein annotation and homology identification.
