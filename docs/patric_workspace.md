# PATRIC/BV-BRC Workspace Utilities

This module provides Python utilities for interacting with the PATRIC/BV-BRC workspace service, specifically for saving and loading model-related objects. It is a Python port of the workspace functionality from ProbModelSEED (Perl).

## Overview

The `PatricWSUtils` class provides high-level utilities for working with metabolic models and related objects in the PATRIC/BV-BRC workspace. It follows the same design patterns as other KBUtilLib modules and is compatible with the existing framework.

## Installation

The module is part of KBUtilLib and will be automatically available when you install the package:

```bash
pip install kbutillib
```

## Authentication

Before using the PATRIC workspace utilities, you need to set up authentication. The module looks for a PATRIC authentication token in your environment. You can obtain a token from the PATRIC/BV-BRC website.

Set the token in your environment:

```bash
export PATRIC_TOKEN="your_token_here"
```

Or configure it in your `.patric_config` file in your home directory.

## Basic Usage

### Initializing the Utilities

```python
from kbutillib import PatricWSUtils

# Initialize with production workspace
utils = PatricWSUtils(version='prod')

# Or use development workspace
utils_dev = PatricWSUtils(version='dev')
```

### Saving Objects

#### Save a Metabolic Model

```python
# Prepare model data (typically a dictionary with model information)
model_data = {
    'id': 'my_model',
    'name': 'My Metabolic Model',
    'reactions': [...],
    'metabolites': [...],
    'genes': [...]
}

# Save to workspace
result = utils.save_model_object(
    model_data,
    '/username/models/my_model',
    metadata={'description': 'E. coli metabolic model'}
)

print(f"Model saved: {result}")
```

#### Save an FBA Result

```python
fba_data = {
    'id': 'my_fba',
    'objective_value': 0.85,
    'fluxes': {...}
}

result = utils.save_fba_object(
    fba_data,
    '/username/fba_results/my_fba'
)
```

#### Save a Media Formulation

```python
media_data = {
    'id': 'minimal_media',
    'compounds': [
        {'compound': 'cpd00027', 'concentration': 10},  # Glucose
        {'compound': 'cpd00001', 'concentration': 1000}  # H2O
    ]
}

result = utils.save_media_object(
    media_data,
    '/username/media/minimal_media'
)
```

### Loading Objects

#### Load a Metabolic Model

```python
# Get a model from workspace
model_obj = utils.get_model_object('/username/models/my_model')

# Access model data
model_data = model_obj['data']
print(f"Model ID: {model_data['id']}")
print(f"Number of reactions: {len(model_data['reactions'])}")
```

#### Load an FBA Result

```python
fba_obj = utils.get_fba_object('/username/fba_results/my_fba')
fba_data = fba_obj['data']
print(f"Objective value: {fba_data['objective_value']}")
```

### Listing Objects

#### List All Models in a Directory

```python
# List models in a specific directory
models = utils.list_models('/username/models')

for model in models:
    print(f"Model: {model['name']}, Type: {model['type']}")

# List recursively in subdirectories
all_models = utils.list_models('/username', recursive=True)
```

#### List FBA Results

```python
fba_results = utils.list_fbas('/username/fba_results')
for fba in fba_results:
    print(f"FBA: {fba['name']}")
```

#### List Media Formulations

```python
media_list = utils.list_media('/username/media')
for media in media_list:
    print(f"Media: {media['name']}")
```

### Managing Objects

#### Copy an Object

```python
# Copy a model to a new location
result = utils.copy_object(
    '/username/models/my_model',
    '/username/models/my_model_backup'
)
```

#### Move an Object

```python
# Move a model to a different location
result = utils.copy_object(
    '/username/models/old_model',
    '/username/archive/old_model',
    move=True
)
```

#### Delete an Object

```python
# Delete a model from workspace
success = utils.delete_object('/username/models/old_model')
if success:
    print("Model deleted successfully")
```

## Advanced Usage

### Using the Low-Level Workspace Client

For more advanced operations, you can access the low-level workspace client:

```python
# Get the workspace client
ws_client = utils.ws_client()

# Create objects with custom parameters
objects = [{
    'path': '/username/custom/my_object',
    'type': 'CustomType',
    'data': {'key': 'value'},
    'metadata': {'tag': 'test'}
}]

result = ws_client.create(objects, overwrite=True)
```

### Building Workspace References

```python
# Build a full workspace reference path
ref = utils.build_ref('my_model', '/username/models')
print(f"Full reference: {ref}")  # Output: /username/models/my_model
```

### Saving Generic Objects

For object types not covered by convenience methods:

```python
# Save a custom object type
custom_data = {'custom_field': 'value'}
result = utils.save_object(
    custom_data,
    '/username/custom/my_object',
    'CustomObjectType',
    metadata={'description': 'Custom object'}
)
```

## Object Types

The module supports the following standard ModelSEED object types:

- `FBAModel` - Metabolic models
- `FBA` - Flux balance analysis results
- `Media` - Media formulations
- `Genome` - Genome annotations
- `ModelTemplate` - Model templates
- `PhenotypeSet` - Phenotype data sets
- `PhenotypeSimulationSet` - Phenotype simulation results
- `Biochemistry` - Biochemistry databases

## Workspace URLs

The module uses the following default URLs:

**Production:**
- Workspace: `https://p3.theseed.org/services/Workspace`
- ModelSEED: `https://p3.theseed.org/services/ProbModelSEED`

**Development:**
- Workspace: `http://p3c.theseed.org/dev1/services/Workspace`
- ModelSEED: `http://p3c.theseed.org/dev1/services/ProbModelSEED`

## Comparison with ProbModelSEED (Perl)

This Python module ports the following functions from ProbModelSEED:

| Perl Function | Python Method |
|---------------|---------------|
| `util_save_object()` | `save_object()` |
| `util_get_object()` | `get_object()` |
| `buildref()` | `build_ref()` |
| `util_get_ref()` | `get_ref()` |

The Python implementation follows the same logic and API structure as the Perl version while adopting Pythonic conventions and type hints.

## Integration with ModelSEED/COBRApy

This module is designed to work alongside other modeling tools:

```python
from kbutillib import PatricWSUtils
import cobra

# Initialize workspace utils
ws_utils = PatricWSUtils()

# Load a model from workspace
model_obj = ws_utils.get_model_object('/username/models/ecoli')
model_data = model_obj['data']

# Convert to COBRApy model (requires additional conversion logic)
# cobra_model = convert_to_cobra(model_data)

# Perform analysis with COBRApy
# solution = cobra_model.optimize()

# Save results back to workspace
# fba_data = convert_cobra_solution(solution)
# ws_utils.save_fba_object(fba_data, '/username/fba_results/ecoli_fba')
```

## Error Handling

The module provides logging for operations:

```python
from kbutillib import PatricWSUtils

utils = PatricWSUtils()

# The module will log errors and warnings
try:
    model = utils.get_model_object('/nonexistent/path')
except Exception as e:
    print(f"Error: {e}")
```

## Examples

See the `examples/` directory for complete examples:

- `save_load_model.py` - Basic model save/load operations
- `workspace_management.py` - Managing workspace objects
- `batch_operations.py` - Batch processing of models

## API Reference

For detailed API documentation, see the docstrings in `patric_ws_utils.py` or generate documentation with:

```bash
pdoc --html kbutillib.patric_ws_utils
```

## Support

For issues or questions:
- GitHub Issues: https://github.com/ModelSEED/KBUtilLib/issues
- PATRIC/BV-BRC Documentation: https://www.bv-brc.org/docs/

## License

This module is part of KBUtilLib and follows the same license terms.
