# ArgoUtils Module

The `ArgoUtils` class provides utilities for interacting with Argo LLM services and language model inference capabilities within the KBase ecosystem.

## Overview

`ArgoUtils` extends `SharedEnvUtils` to provide specialized functionality for connecting to and utilizing Argo-based language models for various computational biology and bioinformatics tasks.

## Key Features

- **LLM Integration**: Direct access to Argo language model services
- **Multiple Model Support**: Support for various model types including GPT-4o, o1-series reasoning models
- **Environment Management**: Automatic handling of production vs development environments
- **Timeout Management**: Configurable timeouts for different model types
- **Response Processing**: Built-in response validation and error handling

## Class Definition

```python
class ArgoUtils(SharedEnvUtils):
    """Utilities for Argo LLM service integration and language model operations.

    Provides methods for connecting to Argo services, managing model inference,
    and processing language model responses for bioinformatics applications.
    """
```

## Constructor

```python
def __init__(self, **kwargs: Any) -> None:
    """Initialize Argo utilities.

    Args:
        **kwargs: Additional keyword arguments passed to SharedEnvUtils
    """
```

## Key Methods

### Model Management

- `get_available_models()`: Retrieve list of available language models
- `set_model(model_name)`: Configure the active model for inference
- `get_model_info(model_name)`: Get detailed information about a specific model

### Inference Methods

- `generate_response(prompt, **kwargs)`: Generate language model response
- `batch_inference(prompts)`: Process multiple prompts in batch
- `stream_response(prompt)`: Stream real-time model responses

### Utility Functions

- `validate_model_response(response)`: Validate and parse model outputs
- `format_bioinformatics_prompt(data)`: Format data for bioinformatics queries
- `extract_structured_data(response)`: Parse structured data from model responses

## Model Support

### Production Models

- **GPT-4o**: General purpose language model
- **GPT-4o Latest**: Latest version with enhanced capabilities

### Development Models

- **O1-series**: Reasoning-focused models with extended processing time
- **Custom Models**: Support for custom Argo-deployed models

## Configuration

The module automatically detects and configures:

- Environment-specific endpoints (prod/dev)
- Model-specific timeout settings
- Authentication tokens
- Request rate limiting

## Usage Examples

```python
from kbutillib.argo_utils import ArgoUtils

# Initialize Argo utilities
argo = ArgoUtils()

# Generate a response
response = argo.generate_response(
    prompt="Analyze this protein sequence: MVLSEGEWQLVLHVWAKVEADVAGHGQDILIRLFKSHP",
    model="gpt4o"
)

# Process bioinformatics data
structured_data = argo.extract_structured_data(response)
```

## Error Handling

The module provides comprehensive error handling for:

- Network connectivity issues
- Model availability problems
- Authentication failures
- Timeout scenarios
- Invalid response formats

## Dependencies

- `requests`: For HTTP communication with Argo services
- `json`: For request/response serialization
- `re`: For response pattern matching and validation

## Integration

ArgoUtils integrates seamlessly with other KBUtilLib modules:

- Uses `SharedEnvUtils` for configuration and token management
- Compatible with `NotebookUtils` for interactive analysis
- Supports `KBSDKUtils` for SDK-based workflows
