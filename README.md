# KBUtilLib

[![PyPI](https://img.shields.io/pypi/v/KBUtilLib.svg)][pypi status]
[![Status](https://img.shields.io/pypi/status/KBUtilLib.svg)][pypi status]
[![Python Version](https://img.shields.io/pypi/pyversions/KBUtilLib)][pypi status]
[![License](https://img.shields.io/pypi/l/KBUtilLib)][license]

[![Read the documentation at https://KBUtilLib.readthedocs.io/](https://img.shields.io/readthedocs/KBUtilLib/latest.svg?label=Read%20the%20Docs)][read the docs]
[![Tests](https://github.com/cshenry/KBUtilLib/workflows/Tests/badge.svg)][tests]
[![Codecov](https://codecov.io/gh/cshenry/KBUtilLib/branch/main/graph/badge.svg)][codecov]

[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit&logoColor=white)][pre-commit]
[![Ruff codestyle][ruff badge]][ruff project]

> **A modular utility framework for scientific computing and bioinformatics**
>
> KBUtilLib provides a flexible, composable set of utilities for working with KBase data, genomics, metabolomics, mass spectrometry, and interactive notebook environments.

## Features

- **Modular Architecture**: Inherit from only the utility modules you need
- **Composable Design**: Create custom utility combinations via multiple inheritance
- **Shared Environment**: Centralized configuration and secret management
- **KBase Integration**: Complete API access and data manipulation tools
- **Genomics Utilities**: Sequence analysis, ORF finding, translation, and more
- **Mass Spectrometry**: Peak detection, spectrum analysis, and metabolite identification
- **Metabolic Modeling**: Model analysis, FBA preparation, and pathway utilities
- **Notebook Support**: Enhanced display and interactive features for Jupyter

## Quick Start

### Basic Usage

```python
from kbutillib import KBaseAPI, KBGenomeUtils, SharedEnvironment

# Use individual utilities
genome_utils = KBGenomeUtils()
dna_sequence = "ATGAAAGCCTAG"
protein = genome_utils.translate_sequence(dna_sequence)
print(f"Translated: {dna_sequence} -> {protein}")

# Use with shared configuration
env = SharedEnvironment(config_file="config.yaml")
kbase_api = KBaseAPI(auth_token=env.get_auth_token("kbase"))
```

### Composable Design

```python
from kbutillib import KBaseAPI, KBGenomeUtils, SharedEnvironment

# Create custom utility combinations
class MyWorkflow(KBaseAPI, KBGenomeUtils, SharedEnvironment):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def analyze_genome(self, genome_ref, workspace_id):
        # Get genome data via KBase API
        genome_data = self.get_object(workspace_id, genome_ref)

        # Analyze with genome utilities
        genome_info = self.parse_genome_object(genome_data)
        features = self.extract_features_by_type(genome_data, 'CDS')

        return genome_info, features

# Use your custom workflow
workflow = MyWorkflow(config_file="config.yaml")
```

### Pre-built Combinations

```python
from kbutillib.examples import KBaseWorkbench, NotebookAnalysis

# Complete KBase analysis environment
workbench = KBaseWorkbench(config_file="config.yaml")

# Notebook-optimized analysis tools
notebook_env = NotebookAnalysis()
notebook_env.display_dataframe(my_dataframe)
```

## Architecture

### Core Modules

- **`BaseUtils`**: Foundation class with logging and configuration
- **`SharedEnvironment`**: Configuration file loading and secret management
- **`NotebookUtils`**: Jupyter notebook integration and display utilities
- **`KBaseAPI`**: Complete KBase service API access
- **`KBGenomeUtils`**: Genomic sequence analysis and manipulation
- **`MSUtils`**: Mass spectrometry data processing
- **`KBModelUtil`**: Metabolic model analysis and FBA preparation

### Design Philosophy

The framework follows a composable design where you can inherit from any combination of utility modules to create exactly the functionality you need:

```python
# Minimal combination
class SimpleTools(KBaseAPI, SharedEnvironment):
    pass

# Complete analysis suite
class FullWorkbench(KBaseAPI, KBGenomeUtils, MSUtils, KBModelUtil, NotebookUtils, SharedEnvironment):
    pass

# Domain-specific combination
class MetabolomicsTools(MSUtils, KBModelUtil, NotebookUtils):
    pass
```

- **Modern Python Development**: Built with `uv` for fast dependency management and packaging
- **Code Quality**: Comprehensive linting with `ruff`, type checking with `mypy`, and testing with `pytest`
- **Scientific Workflows**: Ready for data analysis, computational research, and scientific computing

  - **Containerization**: Docker support for reproducible deployment and environment isolation

- **Documentation**: Automated documentation with Sphinx and Read the Docs
- **CI/CD**: GitHub Actions for automated testing, linting, and deployment

## Requirements

- Python 3.9+
- `uv` package manager (recommended) or `pip`

## Installation

### Using uv (Recommended)

```console
$ uv add KBUtilLib
```

### Using pip

You can install _KBUtilLib_ via [pip] from [PyPI]:

```console
$ pip install KBUtilLib
```

### Development Installation

For development, clone the repository and install with development dependencies:

```console
$ git clone https://github.com/cshenry/KBUtilLib.git
$ cd KBUtilLib
$ uv sync --all-groups
```

## Usage

### Command Line Interface

```console
$ KBUtilLib --help
```

### Docker

Build and run with Docker:

```console
$ docker build -t KBUtilLib .
$ docker run KBUtilLib
```

For detailed usage instructions, please see the [Command-line Reference].

## Contributing

Contributions are very welcome.
To learn more, see the [Contributor Guide].

## License

Distributed under the terms of the [MIT license][license],
_KBUtilLib_ is free and open source software.

## Issues

If you encounter any problems,
please [file an issue] along with a detailed description.

## Credits

This project was generated from Christopher Henry's [cookiecutter-henry-hypermodern-python] template,
which is based on [@cjolowicz]'s [uv hypermodern python cookiecutter] template.

**Developed at Argonne National Laboratory**

[@cjolowicz]: https://github.com/cjolowicz
[pypi]: https://pypi.org/
[pypi status]: https://pypi.org/project/KBUtilLib/
[read the docs]: https://KBUtilLib.readthedocs.io/
[tests]: https://github.com/cshenry/KBUtilLib/actions?workflow=Tests
[codecov]: https://app.codecov.io/gh/cshenry/KBUtilLib
[pre-commit]: https://github.com/pre-commit/pre-commit
[ruff badge]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json
[ruff project]: https://github.com/charliermarsh/ruff
[cookiecutter-henry-hypermodern-python]: https://github.com/chenry/cookiecutter-henry-hypermodern-python
[uv hypermodern python cookiecutter]: https://github.com/bosd/cookiecutter-uv-hypermodern-python
[file an issue]: https://github.com/cshenry/KBUtilLib/issues
[pip]: https://pip.pypa.io/

<!-- github-only -->

[license]: https://github.com/cshenry/KBUtilLib/blob/main/LICENSE
[contributor guide]: https://github.com/cshenry/KBUtilLib/blob/main/CONTRIBUTING.md
[command-line reference]: https://KBUtilLib.readthedocs.io/en/latest/usage.html
