# Dependency Management

KBUtilLib uses a simple path-based system to locate external dependencies. By default, dependencies are expected in sibling directories alongside the KBUtilLib repository. Custom paths can be configured via `dependencies.yaml`.

## Quick Start

### Setup

Clone KBUtilLib and its dependencies as sibling directories:

```bash
cd ~/Projects  # or wherever you keep your repos
git clone https://github.com/cshenry/KBUtilLib.git
git clone https://github.com/ModelSEED/ModelSEEDpy.git
git clone https://github.com/ModelSEED/ModelSEEDDatabase.git
git clone https://github.com/Fxe/cobrakbase.git
git clone https://github.com/kbaseapps/cb_annotation_ontology_api.git
```

This gives you a directory structure like:

```
Projects/
├── KBUtilLib/
├── ModelSEEDpy/
├── ModelSEEDDatabase/
├── cobrakbase/
└── cb_annotation_ontology_api/
```

No additional configuration is needed — KBUtilLib will find them automatically.

### Using the activate script

Source `activate.sh` to set up PYTHONPATH automatically:

```bash
source KBUtilLib/activate.sh
```

## Configuration

Dependencies are configured in `dependencies.yaml` at the repo root. Each dependency has a `path` field:

```yaml
dependencies:
  modelseedpy:
    path: "../ModelSEEDpy"

  ModelSEEDDatabase:
    path: "../ModelSEEDDatabase"

  cobrakbase:
    path: "../cobrakbase"

  cb_annotation_ontology_api:
    path: "../cb_annotation_ontology_api"
```

### Custom paths

You can point to any location by editing the path:

```yaml
# Relative path (resolved from KBUtilLib repo root)
modelseedpy:
  path: "../ModelSEEDpy"

# Absolute path
modelseedpy:
  path: "/home/user/code/ModelSEEDpy"
```

## Current Dependencies

1. **ModelSEEDpy** - Metabolic modeling tools
   - Repository: https://github.com/ModelSEED/ModelSEEDpy

2. **ModelSEEDDatabase** - Biochemistry database
   - Repository: https://github.com/ModelSEED/ModelSEEDDatabase

3. **cobrakbase** - KBase extensions for COBRA
   - Repository: https://github.com/Fxe/cobrakbase

4. **cb_annotation_ontology_api** - Annotation ontology API
   - Repository: https://github.com/kbaseapps/cb_annotation_ontology_api

## How It Works

The dependency manager (`src/kbutillib/dependency_manager.py`):
1. Reads `dependencies.yaml` for configured paths
2. Resolves relative paths from the repo root
3. Adds found dependency paths to `sys.path` for imports
4. Provides `get_data_path()` for accessing data within dependencies

### Accessing dependency data in code

```python
from kbutillib.dependency_manager import get_data_path

# Get path to a dependency
msdb_path = get_data_path("ModelSEEDDatabase")

# Get path to data within a dependency
data_path = get_data_path("cb_annotation_ontology_api", "data/FilteredReactions.csv")
```

## Adding a New Dependency

1. Clone the repo as a sibling directory (or wherever you prefer)

2. Add to `dependencies.yaml`:
```yaml
  new_dependency:
    path: "../new_dependency"
```

3. Use in code:
```python
from kbutillib.dependency_manager import get_data_path
path = get_data_path("new_dependency")
```

## Troubleshooting

### Import errors

Check that:
1. The path in `dependencies.yaml` is correct
2. The dependency repo exists at that location
3. Run `source activate.sh` to set up PYTHONPATH
