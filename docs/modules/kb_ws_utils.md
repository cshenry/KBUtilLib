# KBWSUtils Module

The `KBWSUtils` class provides utilities for interacting with KBase (Department of Energy Systems Biology Knowledgebase) APIs and services, with a focus on workspace operations and data management.

## Overview

`KBWSUtils` extends `SharedEnvUtils` to provide comprehensive KBase workspace functionality. It handles authentication, workspace operations, object management, and data retrieval from the KBase platform.

## Key Features

- **Workspace Operations**: Complete workspace management (create, list, delete)
- **Object Management**: Save, retrieve, and manipulate KBase objects
- **Type Discovery**: List and inspect available KBase datatypes
- **Type Specifications**: Retrieve detailed type schemas and metadata
- **Authentication**: Integrated KBase authentication and token management
- **Multi-environment Support**: Support for prod, dev, and CI environments
- **Retry Logic**: Built-in retry mechanisms for network operations
- **Reference Handling**: Flexible object reference and ID processing

## Class Definition

```python
class KBWSUtils(SharedEnvUtils):
    """Utilities for interacting with KBase (Department of Energy Systems Biology
    Knowledgebase) APIs and services.

    Provides methods for authentication, data retrieval, workspace operations,
    and other KBase-specific functionality.
    """
```

## Constructor

```python
def __init__(
    self,
    kb_version: Optional[str] = "prod",
    max_retry: int = 3,
    **kwargs: Any
) -> None:
    """Initialize KBase Workspace utilities.

    Args:
        kb_version: KBase environment ("prod", "appdev", "ci")
        max_retry: Maximum retry attempts for failed operations
        **kwargs: Additional arguments passed to SharedEnvUtils
    """
```

## Core Methods

### Workspace Management

```python
def set_ws(self, workspace: Union[str, int]) -> None:
    """Set the current workspace context.

    Args:
        workspace: Workspace name or ID
    """

def ws_client(self) -> Workspace:
    """Get the Workspace client instance.

    Returns:
        Configured Workspace client
    """

def list_ws_objects(
    self,
    wsid_or_ref: Union[str, int],
    type: Optional[str] = None,
    include_metadata: bool = True
) -> Dict[str, Any]:
    """List objects in a workspace.

    Args:
        wsid_or_ref: Workspace ID or reference
        type: Filter by object type (optional)
        include_metadata: Include object metadata

    Returns:
        Dictionary of object name -> object info mappings
    """
```

### Object Operations

```python
def get_object(self, id_or_ref: str, ws: Optional[Union[str, int]] = None) -> Dict[str, Any]:
    """Get an object from workspace.

    Args:
        id_or_ref: Object ID or reference
        ws: Workspace (uses current if None)

    Returns:
        Object data and metadata
    """

def get_object_info(
    self,
    id_or_ref: str,
    ws: Optional[Union[str, int]] = None
) -> List[Any]:
    """Get object information without data.

    Args:
        id_or_ref: Object ID or reference
        ws: Workspace (uses current if None)

    Returns:
        Object info list [id, name, type, save_date, version, ...]
    """

def save_ws_object(
    self,
    objid: str,
    workspace: Union[str, int],
    obj_json: Dict[str, Any],
    obj_type: str
) -> Dict[str, Any]:
    """Save an object to workspace.

    Args:
        objid: Object identifier
        workspace: Target workspace
        obj_json: Object data
        obj_type: KBase object type

    Returns:
        Save operation result
    """
```

### Reference Processing

```python
def process_ws_ids(
    self,
    id_or_ref: str,
    workspace: Optional[Union[str, int]] = None,
    no_ref: bool = False
) -> Dict[str, Any]:
    """Process workspace IDs/references into standard format.

    Args:
        id_or_ref: Object ID or reference string
        workspace: Workspace context
        no_ref: Return components instead of reference

    Returns:
        Standardized object specification
    """

def create_ref(self, id_or_ref: str, ws: Optional[Union[str, int]] = None) -> str:
    """Create a workspace reference string.

    Args:
        id_or_ref: Object ID or existing reference
        ws: Workspace (uses current if None)

    Returns:
        Workspace reference in format "ws/obj/ver"
    """
```

### File Operations

```python
def download_blob_file(self, handle_id: str, file_path: str) -> Optional[str]:
    """Download a file from Shock storage.

    Args:
        handle_id: Handle ID for the file
        file_path: Local path to save file

    Returns:
        Path to downloaded file or None if failed
    """
```

### Type Discovery and Management

```python
def list_all_types(
    self,
    include_empty_modules: bool = False,
    track_provenance: bool = False
) -> List[str]:
    """List all released types from all modules in the KBase Workspace.

    Args:
        include_empty_modules: If True, include modules with no released types
        track_provenance: If True, track this operation in provenance

    Returns:
        List of type strings (e.g., ['KBaseGenomes.Genome', 'KBaseFBA.FBAModel'])
    """

def get_type_specs(
    self,
    type_list: List[str],
    track_provenance: bool = False
) -> Dict[str, Any]:
    """Retrieve detailed specifications for specific datatypes.

    Args:
        type_list: List of type strings to retrieve specs for
        track_provenance: If True, track this operation in provenance

    Returns:
        Dictionary mapping type strings to their full specifications
        (includes type_def, description, json_schema, spec_def, etc.)

    Raises:
        ValueError: If type_list is empty or not a list
        Exception: If any type doesn't exist or API call fails
    """
```

### Environment Configuration

```python
def get_base_url_from_version(self, version: str) -> str:
    """Get base URL for KBase environment.

    Args:
        version: Environment version ("prod", "appdev", "ci")

    Returns:
        Base URL for the environment
    """
```

## Usage Examples

### Basic Workspace Operations

```python
from kbutillib.kb_ws_utils import KBWSUtils

# Initialize for production environment
kb = KBWSUtils(kb_version="prod")

# Set workspace context
kb.set_ws("MyWorkspace")

# List objects in workspace
objects = kb.list_ws_objects("MyWorkspace", type="KBaseGenomes.Genome")

# Get specific object
genome = kb.get_object("genome_id", "MyWorkspace")
```

### Object Management

```python
# Get object information
info = kb.get_object_info("genome_id")
print(f"Object type: {info[2]}")
print(f"Save date: {info[3]}")

# Save new object
data = {"id": "new_genome", "features": [...]}
result = kb.save_ws_object(
    "new_genome",
    "MyWorkspace",
    data,
    "KBaseGenomes.Genome"
)
```

### Working with References

```python
# Create references
ref = kb.create_ref("genome_id", "MyWorkspace")  # Returns "workspace_id/object_id/version"

# Process complex references
obj_spec = kb.process_ws_ids("workspace/object/version")
obj_spec = kb.process_ws_ids("object_name", workspace="MyWorkspace")
```

### File Downloads

```python
# Download files from Shock storage
local_path = kb.download_blob_file("handle_12345", "/tmp/downloaded_file")
if local_path:
    print(f"File downloaded to: {local_path}")
```

### Multi-environment Usage

```python
# Production environment
prod_kb = KBWSUtils(kb_version="prod")

# Development environment
dev_kb = KBWSUtils(kb_version="appdev")

# CI environment
ci_kb = KBWSUtils(kb_version="ci")
```

### Type Discovery and Specifications

```python
# List all available types
all_types = kb.list_all_types()
print(f"Total types: {len(all_types)}")
print(f"Sample types: {all_types[:5]}")

# List types including empty modules
all_types_with_empty = kb.list_all_types(include_empty_modules=True)

# Filter types by pattern
genome_types = [t for t in all_types if 'Genome' in t]
print(f"Genome-related types: {genome_types}")

# Get detailed specifications for specific types
type_specs = kb.get_type_specs(['KBaseGenomes.Genome', 'KBaseFBA.FBAModel'])

for type_name, spec in type_specs.items():
    print(f"\nType: {type_name}")
    print(f"  Definition: {spec['type_def']}")
    print(f"  Description: {spec['description']}")
    print(f"  Has JSON schema: {'json_schema' in spec}")

# Practical workflow: Find and inspect types
all_types = kb.list_all_types()
assembly_types = [t for t in all_types if 'Assembly' in t]
assembly_specs = kb.get_type_specs(assembly_types[:3])

# With provenance tracking
types = kb.list_all_types(track_provenance=True)
specs = kb.get_type_specs(['KBaseGenomes.Genome'], track_provenance=True)
```

## Environment URLs

The module automatically configures URLs based on the environment:

- **Production**: `https://kbase.us/services`
- **AppDev**: `https://appdev.kbase.us/services`
- **CI**: `https://ci.kbase.us/services`

## Error Handling

- Built-in retry logic for network failures
- Comprehensive error logging with context
- Graceful handling of authentication failures
- Detailed error messages for debugging

## KBase Object Types

Common object types you can work with:

- `KBaseGenomes.Genome` - Genome objects
- `KBaseFBA.FBAModel` - Metabolic models
- `KBaseBiochem.Media` - Growth media
- `KBaseExpression.ExpressionMatrix` - Gene expression data
- `KBasePhenotypes.PhenotypeSet` - Phenotype data

## Authentication

Requires a valid KBase authentication token:

1. Set via constructor: `KBWSUtils(token="your_token")`
2. Set via token file: `~/.kbase/token`
3. Set via generic token file with namespace

## Dependencies

- KBase Workspace Client (`WorkspaceClient`)
- Handle Service Client (`AbstractHandleClient`)
- Standard libraries: `json`, `os`, `re`, `sys`, `time`, `requests`
- Inherits from: `SharedEnvUtils`

## Common Patterns

### Batch Operations

```python
# Process multiple objects
object_ids = ["obj1", "obj2", "obj3"]
objects = []
for obj_id in object_ids:
    obj = kb.get_object(obj_id)
    objects.append(obj)
```

### Workspace Context Management

```python
# Work with multiple workspaces
workspaces = ["ws1", "ws2", "ws3"]
results = {}

for ws in workspaces:
    kb.set_ws(ws)
    objects = kb.list_ws_objects(ws)
    results[ws] = objects
```

## Notes

- All operations require valid KBase authentication
- Workspace operations maintain provenance information
- Object references are versioned and immutable once saved
- The module handles both object names and numeric IDs transparently
