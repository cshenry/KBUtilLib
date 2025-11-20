# Dependency Management

KBUtilLib uses Git submodules to manage external dependencies. This provides a standardized, version-controlled way to include required libraries and data repositories.

## Quick Start

### First-time setup

When cloning the repository, initialize submodules:

```bash
git clone https://github.com/cshenry/KBUtilLib.git
cd KBUtilLib
git submodule update --init --recursive
```

Or clone with submodules in one step:

```bash
git clone --recursive https://github.com/cshenry/KBUtilLib.git
```

### Updating dependencies

To update all dependencies to their latest commits:

```bash
git submodule update --remote
```

To update a specific dependency:

```bash
git submodule update --remote dependencies/ModelSEEDpy
```

## Configuration

Dependencies are configured in `dependencies.yaml`. This file specifies:

- **git_url**: The repository URL
- **branch**: The branch to track
- **commit**: Optional specific commit to pin (set to `null` to use branch head)
- **path**: Local path where the dependency is located

### Example configuration

```yaml
dependencies:
  modelseedpy:
    git_url: "https://github.com/ModelSEED/ModelSEEDpy.git"
    branch: "main"
    commit: null
    path: "dependencies/ModelSEEDpy"
```

## Custom Dependency Locations

You can use custom paths for dependencies by modifying `dependencies.yaml`:

### Using a relative path

```yaml
modelseedpy:
  git_url: "https://github.com/ModelSEED/ModelSEEDpy.git"
  branch: "main"
  commit: null
  path: "../ModelSEEDpy"  # Sibling directory
```

### Using an absolute path

```yaml
modelseedpy:
  git_url: "https://github.com/ModelSEED/ModelSEEDpy.git"
  branch: "main"
  commit: null
  path: "/home/user/code/ModelSEEDpy"  # Absolute path
```

**Note**: When using custom paths outside the `dependencies/` directory, you need to ensure the repository is cloned and available at that location. The dependency manager will not automatically clone repositories outside the default `dependencies/` directory.

## Current Dependencies

KBUtilLib includes the following dependencies:

1. **ModelSEEDpy** - Metabolic modeling tools
   - Repository: https://github.com/ModelSEED/ModelSEEDpy
   - Branch: main

2. **ModelSEEDDatabase** - Biochemistry database
   - Repository: https://github.com/ModelSEED/ModelSEEDDatabase
   - Branch: master

3. **cobrakbase** - KBase extensions for COBRA
   - Repository: https://github.com/Fxe/cobrakbase
   - Branch: master

4. **cb_annotation_ontology_api** - Annotation ontology API
   - Repository: https://github.com/kbaseapps/cb_annotation_ontology_api
   - Branch: main

## How It Works

The dependency management system consists of two main components:

### 1. Git Submodules

Git submodules track specific commits of external repositories. Configuration is stored in:
- `.gitmodules` - Submodule configuration
- `.git/modules/` - Submodule data

### 2. Dependency Manager

The Python dependency manager (`src/kbutillib/dependency_manager.py`):
- Reads `dependencies.yaml` for configuration
- Resolves dependency paths (absolute and relative)
- Adds dependencies to `sys.path` for imports
- Provides utility functions for accessing dependency data

### Automatic Initialization

When you import KBUtilLib modules, the dependency manager automatically:
1. Loads the configuration from `dependencies.yaml`
2. Resolves all dependency paths
3. Checks if dependencies exist at configured paths
4. For paths in the `dependencies/` directory, initializes submodules if needed
5. Adds all dependency paths to Python's `sys.path`

## Developer Guide

### Adding a new dependency

1. Add the dependency configuration to `dependencies.yaml`:

```yaml
dependencies:
  new_dependency:
    git_url: "https://github.com/org/repo.git"
    branch: "main"
    commit: null
    path: "dependencies/new_dependency"
```

2. Add the submodule:

```bash
git submodule add -b main https://github.com/org/repo.git dependencies/new_dependency
git submodule update --init --recursive dependencies/new_dependency
```

3. Commit the changes:

```bash
git add .gitmodules dependencies.yaml dependencies/new_dependency
git commit -m "Add new_dependency submodule"
```

### Accessing dependency data

Use the dependency manager's helper functions:

```python
from kbutillib.dependency_manager import get_data_path

# Get path to a dependency
msdb_path = get_data_path("ModelSEEDDatabase")

# Get path to data within a dependency
data_path = get_data_path("cb_annotation_ontology_api", "data/FilteredReactions.csv")
```

### Pinning to a specific commit

To pin a dependency to a specific commit:

1. Navigate to the dependency directory:
```bash
cd dependencies/ModelSEEDpy
```

2. Checkout the desired commit:
```bash
git checkout abc123def
```

3. Update `dependencies.yaml`:
```yaml
modelseedpy:
  commit: "abc123def"
  # ... rest of config
```

4. Commit the change from the repository root:
```bash
cd ../..
git add dependencies/ModelSEEDpy dependencies.yaml
git commit -m "Pin ModelSEEDpy to commit abc123def"
```

## Troubleshooting

### Submodule directory is empty

Run:
```bash
git submodule update --init --recursive
```

### Import errors

Ensure the dependency manager is initialized:
```python
from kbutillib.dependency_manager import get_dependency_manager
dep_mgr = get_dependency_manager()
```

### Dependency not found

Check that:
1. The path in `dependencies.yaml` is correct
2. The dependency exists at that location
3. For submodules, run `git submodule update --init --recursive`

## Migration from Old System

The previous dependency management system used symlinks and direct cloning. Key changes:

- **Old location**: `src/kbutillib/dependencies/`
- **New location**: `dependencies/` (repository root)
- **Old method**: Runtime cloning with `_ensure_git_dependency()`
- **New method**: Git submodules with configuration file

The new system is automatically initialized when importing KBUtilLib modules.
