# KBModelUtils Module

The `KBModelUtils` class provides utilities for working with KBase metabolic models and constraint-based modeling. It combines annotation processing with biochemical modeling capabilities to support flux balance analysis and metabolic model manipulation.

## Overview

`KBModelUtils` extends both `KBAnnotationUtils` and `MSBiochemUtils` through multiple inheritance, providing comprehensive metabolic modeling capabilities. It integrates with CobraKBase, ModelSEEDPy, and other modeling frameworks to support constraint-based modeling workflows.

## Key Features

- **Model Management**: Create, modify, and save metabolic models
- **Flux Balance Analysis**: Run FBA simulations and analyze results
- **Template Integration**: Work with model templates and gap-filling
- **Phenotype Analysis**: Process growth phenotype data
- **Expression Integration**: Incorporate gene expression data into models
- **Classifier Support**: Genome classification for model template selection
- **Multi-format Support**: Handle models in various formats (KBase, COBRA, JSON)

## Class Definition

```python
class KBModelUtils(KBAnnotationUtils, MSBiochemUtils):
    """Utilities for working with KBase metabolic models and constraint-based modeling.

    Provides methods for model manipulation, flux balance analysis preparation,
    reaction and metabolite operations, and other metabolic modeling tasks.
    """
```

## Constructor

```python
def __init__(self, **kwargs: Any) -> None:
    """Initialize KBase model utilities.

    Args:
        **kwargs: Additional keyword arguments passed to parent classes
    """
```

## Core Methods

### Model Operations

```python
def get_model(self, id_or_ref: str, ws: Optional[str] = None, is_json_file: bool = False) -> Any:
    """Get a metabolic model from workspace or file.

    Args:
        id_or_ref: Model ID, reference, or file path
        ws: Workspace (if getting from KBase)
        is_json_file: Whether input is a JSON file

    Returns:
        MSModelUtil object wrapping the model
    """

def save_model(
    self,
    mdlutl: Any,
    workspace: Optional[str] = None,
    objid: Optional[str] = None,
    suffix: Optional[str] = None
) -> None:
    """Save a metabolic model to workspace or file.

    Args:
        mdlutl: MSModelUtil object to save
        workspace: Target workspace (None to save as file)
        objid: Object ID (uses model ID if None)
        suffix: Suffix to append to object ID
    """
```

### Genome and Template Operations

```python
def get_msgenome_from_ontology(
    self,
    id_or_ref: str,
    ws: Optional[str] = None,
    native_python_api: bool = False,
    output_ws: Optional[str] = None
) -> Any:
    """Get MSGenome object with ontology annotations.

    Args:
        id_or_ref: Genome reference
        ws: Workspace
        native_python_api: Use native Python API
        output_ws: Output workspace for re-annotation

    Returns:
        MSGenome object with annotations
    """

def get_msgenome(self, id_or_ref: str, ws: Optional[str] = None) -> Any:
    """Get MSGenome object from workspace.

    Args:
        id_or_ref: Genome reference
        ws: Workspace

    Returns:
        MSGenome object
    """

def get_template(self, template_id: str, ws: Optional[str] = None) -> Any:
    """Get a model template.

    Args:
        template_id: Template identifier
        ws: Workspace

    Returns:
        Model template object
    """

def get_gs_template(
    self,
    template_id: str,
    ws: str,
    core_template: Any,
    excluded_cpd: Optional[List[str]] = None
) -> Any:
    """Get gapfilling-specific template.

    Args:
        template_id: Template identifier
        ws: Workspace
        core_template: Core template to merge with
        excluded_cpd: Compounds to exclude

    Returns:
        Enhanced template for gapfilling
    """
```

### Media and Phenotype Operations

```python
def get_media(self, id_or_ref: str, ws: Optional[str] = None) -> Any:
    """Get growth media object.

    Args:
        id_or_ref: Media reference
        ws: Workspace

    Returns:
        Media object
    """

def process_media_list(
    self,
    media_list: List[str],
    default_media: str,
    workspace: str
) -> List[Any]:
    """Process and retrieve multiple media objects.

    Args:
        media_list: List of media references
        default_media: Default media to use
        workspace: Workspace context

    Returns:
        List of media objects
    """

def get_phenotypeset(
    self,
    id_or_ref: str,
    ws: Optional[str] = None,
    base_media: Optional[Any] = None,
    base_uptake: int = 0,
    base_excretion: int = 1000,
    global_atom_limits: Optional[Dict[str, Any]] = None,
) -> Any:
    """Get phenotype set for model analysis.

    Args:
        id_or_ref: Phenotype set reference
        ws: Workspace
        base_media: Base growth media
        base_uptake: Base uptake rate
        base_excretion: Base excretion rate
        global_atom_limits: Atom balance limits

    Returns:
        MSGrowthPhenotypes object
    """

def save_phenotypeset(self, data: Dict[str, Any], workspace: str, objid: str) -> None:
    """Save phenotype set to workspace.

    Args:
        data: Phenotype set data
        workspace: Target workspace
        objid: Object identifier
    """
```

### FBA Operations

```python
def save_solution_as_fba(
    self,
    fba_or_solution: Any,
    mdlutl: Any,
    media: Any,
    fbaid: str,
    workspace: Optional[str] = None,
    fbamodel_ref: Optional[str] = None,
    other_solutions: Optional[List[Any]] = None,
) -> None:
    """Save FBA solution to workspace.

    Args:
        fba_or_solution: FBA solution or MSFBA object
        mdlutl: Model utility object
        media: Growth media
        fbaid: FBA object identifier
        workspace: Target workspace
        fbamodel_ref: Reference to model used
        other_solutions: Additional solutions to include
    """
```

### Expression Data Integration

```python
def get_expression_objs(
    self,
    expression_refs: List[str],
    genome_objs: Dict[str, Any]
) -> Dict[str, Any]:
    """Get expression objects and map to genomes.

    Args:
        expression_refs: List of expression data references
        genome_objs: Dictionary of genome objects

    Returns:
        Dictionary mapping models to expression data
    """
```

### Model Enhancement

```python
def extend_model_with_other_ontologies(
    self,
    mdlutl: Any,
    anno_ont: Any,
    builder: Any,
    prioritized_event_list: Optional[List[str]] = None,
    ontologies: Optional[List[str]] = None,
    merge_all: bool = True,
) -> Any:
    """Extend model with reactions from other ontologies.

    Args:
        mdlutl: Model utility object
        anno_ont: Annotation ontology
        builder: Model builder
        prioritized_event_list: Priority list for events
        ontologies: Ontologies to use
        merge_all: Whether to merge all annotations

    Returns:
        Enhanced model utility object
    """
```

### Genome Classification

```python
def get_classifier(self) -> Any:
    """Get genome classifier for template selection.

    Returns:
        MSGenomeClassifier object
    """
```

### Utility Methods

```python
def create_minimal_medias(
    self,
    carbon_list: Dict[str, str],
    workspace: str,
    base_media: str = "KBaseMedia/Carbon-D-Glucose"
) -> None:
    """Create minimal media with different carbon sources.

    Args:
        carbon_list: Dictionary of carbon source mappings
        workspace: Target workspace
        base_media: Base media to modify
    """
```

## Usage Examples

### Basic Model Operations

```python
from kbutillib.kb_model_utils import KBModelUtils

# Initialize model utilities
model_utils = KBModelUtils()

# Get a model from workspace
model = model_utils.get_model("MyModel", "MyWorkspace")

# Save model with modifications
model_utils.save_model(model, "OutputWorkspace", "ModifiedModel")
```

### Genome-Based Model Building

```python
# Get genome with annotations
genome = model_utils.get_msgenome_from_ontology("GenomeRef", "GenomeWorkspace")

# Get appropriate template
template = model_utils.get_template("GramNegModelTemplateV6")

# Build model using genome and template
# (This would involve additional ModelSEEDPy operations)
```

### FBA Analysis

```python
# Get model and media
model = model_utils.get_model("model_ref")
media = model_utils.get_media("complete_media")

# Run FBA (using ModelSEEDPy)
# fba_solution = model.run_fba(media)

# Save FBA results
model_utils.save_solution_as_fba(
    fba_solution,
    model,
    media,
    "fba_analysis",
    "MyWorkspace"
)
```

### Phenotype Analysis

```python
# Get phenotype data
phenotypes = model_utils.get_phenotypeset("PhenotypeSetRef")

# Process with model
# results = model.test_phenotypes(phenotypes)

# Save results
model_utils.save_phenotypeset(results, "MyWorkspace", "PhenotypeResults")
```

### Expression Data Integration

```python
# Get expression data
expression_refs = ["expr1", "expr2", "expr3"]
genomes = {"model1": genome1, "model2": genome2}

expression_objs = model_utils.get_expression_objs(expression_refs, genomes)

# Use expression data to constrain models
for model_id, expr_data in expression_objs.items():
    # Apply expression constraints to model
    pass
```

## Model Templates

The module provides access to standard KBase model templates:

- **Core**: Universal core metabolism
- **GramPos/GramNeg**: Bacterial templates
- **Archaea**: Archaeal metabolism
- **Custom**: User-defined templates

```python
templates = {
    "core": "NewKBaseModelTemplates/Core-V5.2",
    "grampos": "NewKBaseModelTemplates/GramPosModelTemplateV6",
    "gramneg": "NewKBaseModelTemplates/GramNegModelTemplateV6",
    "archaea": "NewKBaseModelTemplates/ArchaeaTemplateV6"
}
```

## Dependencies

### Required Python Packages

- **cobrakbase**: KBase-COBRA integration
- **modelseedpy**: ModelSEED Python framework
- **pickle**: Model serialization
- Standard libraries: `json`

### KBase Dependencies

- Inherits from: `KBAnnotationUtils`, `MSBiochemUtils`
- Requires: Workspace access, authentication tokens
- Uses: Annotation Ontology API, Model templates

## Integration Points

### With ModelSEEDPy

- **MSModelUtil**: Model manipulation utilities
- **MSFBA**: Flux balance analysis
- **MSGrowthPhenotypes**: Phenotype analysis
- **MSGenomeClassifier**: Genome classification
- **AnnotationOntology**: Functional annotations

### With CobraKBase

- **FBAModel**: KBase-specific model format
- **CobraModelConverter**: Format conversion
- **KBaseAPI**: Workspace integration

## Common Workflows

### Model Reconstruction

1. Get annotated genome
2. Select appropriate template
3. Build draft model
4. Gap-fill for growth
5. Validate with phenotypes

### Comparative Analysis

1. Load multiple models
2. Run FBA on different media
3. Compare flux distributions
4. Analyze metabolic differences

### Expression Integration

1. Get expression data
2. Map to model genes
3. Apply expression constraints
4. Analyze condition-specific metabolism

## Performance Considerations

- Models are memory-intensive objects
- FBA calculations can be computationally expensive
- Template operations benefit from caching
- Large-scale analyses should use batch processing

## Error Handling

- Validates model consistency before operations
- Handles missing annotations gracefully
- Provides detailed error messages for debugging
- Supports retry logic for network operations

## Notes

- Requires installation of modeling dependencies (cobrakbase, modelseedpy)
- Some operations require CPLEX or other commercial solvers
- Templates and databases are cached locally for performance
- Full integration with KBase workspace and provenance systems
