# KBUtilLib Notebooks

This directory contains Jupyter notebooks demonstrating KBUtilLib functionality and workflows.

## Available Notebooks

### [ConfigureEnvironment.ipynb](ConfigureEnvironment.ipynb)

**Purpose**: Set up and configure the KBUtilLib environment

**Topics Covered**:
- Initializing the `~/.kbutillib/` directory
- Loading and viewing configuration
- Accessing config values with dot notation
- Customizing user configuration
- Testing SKANI integration
- Environment state inspection

**When to Use**:
- First-time setup of KBUtilLib
- Understanding the configuration system
- Troubleshooting configuration issues
- Migrating to user-specific config

## Getting Started

1. **Launch Jupyter**:
   ```bash
   cd /path/to/KBUtilLib/notebooks
   jupyter notebook
   ```

2. **Run ConfigureEnvironment.ipynb** first to set up your environment

3. **Explore other notebooks** as needed for specific functionality

## Notebook Organization

Notebooks in this directory follow these principles:

- **Self-contained**: Each notebook includes necessary imports and setup
- **Educational**: Clear explanations with code examples
- **Reproducible**: Can be run multiple times safely
- **Practical**: Focus on real-world usage patterns

## Configuration

All notebooks assume:
- KBUtilLib source is in `../src/`
- Configuration is in `~/.kbutillib/config.yaml` (after running ConfigureEnvironment)
- Python 3.7+

## Adding New Notebooks

When creating new notebooks:

1. Add project root to path:
   ```python
   import sys
   from pathlib import Path
   sys.path.insert(0, str(Path.cwd().parent / "src"))
   ```

2. Include clear section headers and explanations
3. Test that notebook runs from clean state
4. Update this README with notebook description

## Support

For issues or questions:
- Check the main [README.md](../README.md)
- Review [example scripts](../) in project root
- Open an issue on GitHub
