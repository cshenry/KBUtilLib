# KBAnnotationUtils Module

The `KBAnnotationUtils` class provides utilities for managing gene annotation information within KBase genome objects, implementing the KBase Annotation Ontology API functionality for use outside the SDK environment.

## Overview

`KBAnnotationUtils` extends `KBWSUtils` to provide comprehensive gene and genome annotation capabilities. It handles functional annotations, ontology term processing, and integration with various biological databases and ontologies.

## Key Features

- **Annotation Processing**: Process and standardize functional annotations
- **Ontology Integration**: Support for multiple ontologies (GO, KEGG, SSO, etc.)
- **Term Translation**: Convert between different annotation vocabularies
- **Feature Enhancement**: Upgrade genome features with additional annotations
- **Database Integration**: Interface with ModelSEED and other biological databases
- **Event Management**: Track and manage annotation events and provenance

## Class Definition

```python
class KBAnnotationUtils(KBWSUtils):
    """Utilities for managing gene annotation information within KBase genome objects in the workspace"""
```

## Constructor

```python
def __init__(self, **kwargs: Any) -> None:
    """Initialize KBase annotation utilities.

    Args:
        **kwargs: Additional arguments passed to KBWSUtils
    """
```

## Core Methods

### Annotation Event Management

```python
def get_annotation_ontology_events(self, params: Dict[str, Any]) -> Dict[str, Any]:
    """Get annotation ontology events for a genome.

    Args:
        params: Parameters including input_ref for the genome

    Returns:
        Dictionary containing annotation events data
    """

def add_annotation_ontology_events(self, params: Dict[str, Any]) -> Dict[str, Any]:
    """Add annotation ontology events to a genome.

    Args:
        params: Parameters including input_ref, events, and output settings

    Returns:
        Result of the annotation addition operation
    """
```

### Object Processing

```python
def process_object(self, params: Dict[str, Any]) -> Dict[str, Any]:
    """Process a genome object to add annotations.

    Args:
        params: Processing parameters including input/output references

    Returns:
        Processing results and updated object information
    """

def save_object(self, params: Dict[str, Any]) -> Dict[str, Any]:
    """Save an annotated object back to the workspace.

    Args:
        params: Save parameters including object data and workspace info

    Returns:
        Save operation results
    """
```

### Feature Processing

```python
def upgrade_feature(self, ftr: Dict[str, Any]) -> Dict[str, Any]:
    """Upgrade a feature with additional annotation information.

    Args:
        ftr: Feature dictionary to upgrade

    Returns:
        Enhanced feature with additional annotations
    """

def process_feature_aliases(self, ftr: Dict[str, Any]) -> None:
    """Process and standardize feature aliases.

    Args:
        ftr: Feature dictionary to process aliases for
    """

def add_feature_ontology_terms(
    self,
    feature: Dict[str, Any],
    event: Dict[str, Any],
    ftrid: str
) -> None:
    """Add ontology terms to a feature.

    Args:
        feature: Feature to add terms to
        event: Annotation event containing terms
        ftrid: Feature ID
    """
```

### Term Processing and Translation

```python
def translate_term_to_modelseed(self, term: str) -> Optional[str]:
    """Translate a term to ModelSEED format.

    Args:
        term: Original term to translate

    Returns:
        Translated ModelSEED term or None if no translation
    """

def convert_role_to_searchrole(self, term: str) -> str:
    """Convert a functional role to searchable format.

    Args:
        term: Original role term

    Returns:
        Processed searchable role term
    """

def translate_rast_function_to_sso(self, input_term: str) -> Optional[str]:
    """Translate RAST function to SSO term.

    Args:
        input_term: RAST functional annotation

    Returns:
        Corresponding SSO term or None
    """
```

### Ontology and Database Access

```python
def get_alias_hash(self, namespace: str) -> Dict[str, Any]:
    """Get alias hash for a namespace.

    Args:
        namespace: Namespace identifier (e.g., "MSRXN")

    Returns:
        Dictionary mapping aliases to canonical terms
    """

def get_term_name(self, type: str, term: str) -> Optional[str]:
    """Get the name for an ontology term.

    Args:
        type: Ontology type (e.g., "GO", "SSO")
        term: Term identifier

    Returns:
        Human-readable term name or None
    """
```

### Genome Validation

```python
def check_genome(self, genome: Dict[str, Any], ref: Optional[str] = None) -> Dict[str, Any]:
    """Check and validate a genome object.

    Args:
        genome: Genome object to check
        ref: Optional genome reference

    Returns:
        Validation results and any issues found
    """
```

## Usage Examples

### Basic Annotation Processing

```python
from kbutillib.kb_annotation_utils import KBAnnotationUtils

# Initialize annotation utilities
anno = KBAnnotationUtils()

# Get annotation events for a genome
events = anno.get_annotation_ontology_events({
    "input_ref": "genome_workspace/genome_object"
})

# Process a genome to add annotations
result = anno.process_object({
    "input_ref": "genome_workspace/genome_object",
    "output_name": "annotated_genome",
    "output_workspace": "output_workspace"
})
```

### Feature Enhancement

```python
# Upgrade a feature with additional annotations
feature = {
    "id": "gene_001",
    "function": "hypothetical protein",
    "dna_sequence": "ATGCGATCG...",
    "protein_translation": "MRSDT..."
}

enhanced_feature = anno.upgrade_feature(feature)
print(f"Enhanced feature: {enhanced_feature}")
```

### Term Translation

```python
# Translate terms between vocabularies
modelseed_term = anno.translate_term_to_modelseed("GO:0008152")
sso_term = anno.translate_rast_function_to_sso("ABC transporter")

# Get term names
go_name = anno.get_term_name("GO", "GO:0008152")
print(f"GO term name: {go_name}")
```

### Working with Aliases

```python
# Get reaction aliases
rxn_aliases = anno.get_alias_hash("MSRXN")
print(f"Available reaction aliases: {len(rxn_aliases)}")
```

## Supported Ontologies

The module supports various biological ontologies and databases:

- **GO (Gene Ontology)**: Molecular function, biological process, cellular component
- **KEGG**: Kyoto Encyclopedia of Genes and Genomes
- **SSO (Seed Subsystem Ontology)**: SEED functional categories
- **ModelSEED**: Metabolic reaction and compound identifiers
- **BiGG**: Biochemically, Genetically and Genomically structured database
- **MetaCyc**: Metabolic pathway database
- **Rhea**: Biochemical reactions database

## Annotation Event Structure

Annotation events follow a standardized structure:

```python
event = {
    "method": "annotation_method",
    "event_id": "unique_identifier",
    "timestamp": "2024-01-01T00:00:00Z",
    "ontology_terms": [
        {
            "ontology": "GO",
            "term": "GO:0008152",
            "evidence": "IEA",
            "score": 0.95
        }
    ]
}
```

## Data Processing Pipeline

1. **Input Processing**: Parse genome objects and extract features
2. **Annotation Enhancement**: Add functional annotations and ontology terms
3. **Term Standardization**: Normalize terms across different vocabularies
4. **Quality Control**: Validate annotations and check consistency
5. **Output Generation**: Save enhanced genome back to workspace

## Dependencies

- **pandas**: Data processing and analysis
- **ModelSEED Database**: Biochemical reaction and compound data
- **Annotation Ontology Data**: Local ontology files and mappings
- Standard libraries: `hashlib`, `json`, `os`, `re`
- Inherits from: `KBWSUtils`

## Configuration Files

The module uses several data files for annotation processing:

- `FilteredReactions.csv`: Curated reaction filters
- `*_dictionary.json`: Ontology term mappings
- `*_translation.json`: Cross-ontology translations
- Various alias and mapping files

## Common Use Cases

### Genome Annotation Pipeline

```python
# Complete genome annotation workflow
anno = KBAnnotationUtils()

# Process a newly uploaded genome
result = anno.process_object({
    "input_ref": "workspace/raw_genome",
    "output_name": "annotated_genome",
    "output_workspace": "workspace",
    "add_ontology_terms": True
})

# Add additional annotation events
anno.add_annotation_ontology_events({
    "input_ref": "workspace/annotated_genome",
    "events": custom_events,
    "output_name": "fully_annotated_genome"
})
```

### Feature Analysis

```python
# Analyze features in a genome
genome = anno.get_object("workspace/genome")
for feature in genome["data"]["features"]:
    # Upgrade each feature
    enhanced_feature = anno.upgrade_feature(feature)

    # Check for specific annotations
    if "ontology_terms" in enhanced_feature:
        print(f"Feature {feature['id']} has {len(enhanced_feature['ontology_terms'])} terms")
```

## Error Handling

- Comprehensive validation of input genome objects
- Graceful handling of missing ontology terms
- Detailed error messages for debugging annotation issues
- Automatic fallback to alternative annotation sources

## Performance Considerations

- Efficient bulk processing of genome features
- Caching of ontology data and translations
- Optimized database queries for large genomes
- Memory-efficient processing of annotation events

## Notes

- Requires access to ModelSEED database and ontology files
- Works with both RAST-annotated and custom-annotated genomes
- Maintains full provenance of annotation sources and methods
- Compatible with KBase genome object standards
