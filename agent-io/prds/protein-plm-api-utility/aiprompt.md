# Protein PLM API Utility - Enhanced Requirements

## Overview
Implement a new utility module (`kb_plm_utils.py`) that integrates the KBase Protein Language Model (PLM) API to find protein homologs for features in KBase genome objects.

## Functional Requirements

### 1. API Integration
- Interface with the KBase PLM API at `https://kbase.us/services/llm_homology_api`
- Support querying multiple protein sequences in a single request
- Retrieve top N hits (configurable, default 100) per query sequence
- Parse API responses to extract UniProt IDs and similarity scores

### 2. Feature Container Support
- Accept KBase feature container objects (Genomes, ProtSeqSets, FeatureSets, etc.)
- Extract protein sequences from features using the existing `KBGenomeUtils` base class
- Handle features with `protein_translation` field

### 3. Sequence Retrieval
- Retrieve full protein sequences from UniProt for hit proteins
- Use UniProt REST API to fetch sequences in FASTA format
- Handle API failures gracefully with logging

### 4. BLAST Database Creation
- Create custom BLAST databases from retrieved UniProt sequences
- Use `makeblastdb` command-line tool
- Support temporary database creation with cleanup

### 5. BLAST Search
- Run BLASTP searches against the custom database
- Support configurable E-value thresholds
- Return best hit for each query feature
- Parse BLAST JSON output format

### 6. Result Compilation
- Match each feature to its best BLAST hit
- Return comprehensive results including:
  - Best UniProt ID
  - PLM similarity score
  - BLAST E-value
  - BLAST bit score
  - BLAST identity
  - List of all PLM hits
- Provide simplified interface to get just UniProt IDs

## Technical Requirements

### Architecture
- Inherit from `KBGenomeUtils` to access feature extraction methods
- Follow existing utility module patterns in the codebase
- Use BaseUtils logging infrastructure

### Dependencies
- Standard library: `json`, `os`, `subprocess`, `tempfile`, `pathlib`, `typing`
- External: `requests` (already available)
- System: NCBI BLAST+ tools (optional, with graceful degradation)

### Error Handling
- Check BLAST availability on initialization
- Validate input parameters
- Handle API timeouts and failures
- Provide clear error messages
- Clean up temporary files

### Code Style
- Follow existing code patterns
- Keep implementation simple and straightforward
- Use type hints
- Document all public methods with docstrings
- Implement proper logging at INFO, WARNING, and ERROR levels

## API Endpoints

### PLM Search Endpoint
- **URL**: `POST /search`
- **Request**:
  ```json
  {
    "query_sequences": [{"id": "...", "sequence": "..."}],
    "max_hits": 100,
    "similarity_threshold": 0.0,
    "best_hit_only": false,
    "return_query_embeddings": false,
    "return_hit_embeddings": false
  }
  ```
- **Response**:
  ```json
  {
    "hits": [
      {
        "query_id": "...",
        "best_hit": {"id": "...", "score": 0.95},
        "hits": [{"id": "...", "score": 0.95}, ...],
        "total_hits": 100
      }
    ]
  }
  ```

## Usage Pattern

```python
from kbutillib import KBPLMUtils

# Initialize
plm_utils = KBPLMUtils()

# Load a feature container
plm_utils.load_kbase_gene_container("genome.json", localname="my_genome")

# Find best hits
results = plm_utils.find_best_hits_for_features(
    feature_container_name="my_genome",
    max_plm_hits=100,
    blast_evalue=0.001
)

# Or get just UniProt IDs
uniprot_ids = plm_utils.get_best_uniprot_ids("my_genome")
```

## Deliverables

1. **Module File**: `src/kbutillib/kb_plm_utils.py`
2. **Package Export**: Update `src/kbutillib/__init__.py`
3. **Example**: `examples/plm_utils_example.py`
4. **Documentation**: This PRD and usage examples
5. **Testing**: Basic import and instantiation tests

## Success Criteria

- Module imports successfully
- Can instantiate KBPLMUtils class
- All public methods have proper docstrings
- Follows existing code patterns
- Handles errors gracefully
- Provides clear logging output
- Works with or without BLAST installed (with appropriate warnings)
