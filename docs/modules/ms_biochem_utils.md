# MSBiochemUtils Module

The `MSBiochemUtils` class provides utilities for working with ModelSEED biochemistry databases and metabolic compound/reaction data.

## Overview

`MSBiochemUtils` extends `SharedEnvUtils` to provide specialized functionality for accessing and searching ModelSEED biochemistry databases, managing metabolic compounds and reactions, and performing biochemical data analysis.

## Key Features

- **ModelSEED Database Access**: Direct interface to ModelSEED biochemistry databases
- **Compound Search**: Advanced searching and filtering of metabolic compounds
- **Reaction Analysis**: Tools for analyzing metabolic reactions and pathways
- **Database Synchronization**: Automatic updates and version management
- **Cross-Reference Support**: Integration with multiple biochemical databases

## Class Definition

```python
class MSBiochemUtils(SharedEnvUtils):
    """Utilities for ModelSEED biochemistry database operations.

    Provides methods for accessing biochemical databases, searching compounds
    and reactions, and performing metabolic data analysis tasks.
    """
```

## Constructor

```python
def __init__(self, **kwargs: Any) -> None:
    """Initialize ModelSEED biochemistry utilities.

    Args:
        **kwargs: Additional keyword arguments passed to SharedEnvUtils
    """
```

## Core Methods

### Database Management

- `biochem_db()`: Get access to the main biochemistry database
- `update_database()`: Update local database to latest version
- `get_database_info()`: Retrieve database version and metadata
- `validate_database()`: Check database integrity and completeness

### Compound Operations

- `search_compounds(query, **filters)`: Search for metabolic compounds
- `get_compound_by_id(compound_id)`: Retrieve specific compound data
- `get_compound_structure(compound_id)`: Get chemical structure information
- `find_similar_compounds(compound_id, threshold)`: Find structurally similar compounds

### Reaction Operations

- `search_reactions(query, **filters)`: Search for biochemical reactions
- `get_reaction_by_id(reaction_id)`: Retrieve specific reaction data
- `get_reaction_participants(reaction_id)`: Get reactants and products
- `balance_reaction(reaction_id)`: Check reaction stoichiometry

### Analysis Methods

- `analyze_pathway(pathway_compounds)`: Analyze metabolic pathway completeness
- `find_missing_reactions(compound_list)`: Identify gaps in reaction networks
- `compute_mass_balance(reaction_set)`: Verify mass balance across reactions
- `generate_reaction_network(seed_compounds)`: Build reaction networks

## Database Schema

### Compounds Table

- **ID**: Unique compound identifier
- **Name**: Common compound name
- **Formula**: Chemical formula
- **Charge**: Net charge at physiological pH
- **Structure**: Chemical structure representation
- **Aliases**: Alternative identifiers and names

### Reactions Table

- **ID**: Unique reaction identifier
- **Equation**: Balanced chemical equation
- **Direction**: Reaction directionality (reversible/irreversible)
- **Compartment**: Cellular compartment information
- **Enzymes**: Associated enzyme information

## Search Capabilities

### Compound Search Filters

- **Formula**: Exact or partial chemical formula matching
- **Mass Range**: Molecular weight filtering
- **Charge State**: Ionic charge filtering
- **Database Source**: Filter by originating database
- **Structural Features**: Functional group presence

### Reaction Search Filters

- **Participants**: Search by reactants/products
- **Pathway**: Filter by metabolic pathway
- **Enzyme Class**: Filter by EC number classification
- **Thermodynamics**: Energy change filtering
- **Compartment**: Cellular location filtering

## Integration Features

### External Database Cross-References

- **KEGG**: Kyoto Encyclopedia integration
- **BiGG**: Biochemical database linking
- **MetaCyc**: Metabolic pathway database
- **ChEBI**: Chemical entities database
- **PubChem**: Chemical information database

### Format Support

- **SBML**: Systems Biology Markup Language
- **JSON**: JavaScript Object Notation
- **TSV**: Tab-separated values
- **SDF**: Structure Data Format

## Usage Examples

```python
from kbutillib.ms_biochem_utils import MSBiochemUtils

# Initialize biochemistry utilities
biochem = MSBiochemUtils()

# Access the database
db = biochem.biochem_db()

# Search for glucose compounds
glucose_compounds = biochem.search_compounds(
    query="glucose",
    formula_pattern="C6H12O6"
)

# Find reactions involving ATP
atp_reactions = biochem.search_reactions(
    participants=["cpd00002"]  # ATP compound ID
)

# Analyze a metabolic pathway
pathway_analysis = biochem.analyze_pathway([
    "cpd00027",  # D-Glucose
    "cpd00002",  # ATP
    "cpd00008"   # ADP
])
```

## Performance Considerations

- **Caching**: Frequently accessed data is cached for performance
- **Indexing**: Database queries are optimized with appropriate indexing
- **Memory Management**: Large datasets are handled with streaming
- **Parallel Processing**: Batch operations support parallel execution

## Data Sources

The module integrates data from multiple authoritative sources:

- **ModelSEED**: Primary biochemistry database
- **KEGG Database**: Metabolic pathway information
- **MetaCyc**: Comprehensive metabolic data
- **BiGG Models**: Genome-scale metabolic models
- **SEED Database**: Subsystems and functional roles

## Error Handling

Comprehensive error handling includes:

- Database connectivity issues
- Invalid compound/reaction identifiers
- Data format inconsistencies
- Network timeout scenarios
- Memory limitations for large queries

## Dependencies

- **pandas**: For data manipulation and analysis
- **requests**: For database API communication
- **json**: For data serialization
- **sqlite3**: For local database operations
- **rdkit**: For chemical structure operations (optional)
