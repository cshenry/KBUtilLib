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

### [BVBRCGenomeConversion.ipynb](BVBRCGenomeConversion.ipynb)

**Purpose**: Fetch and convert genome data from BV-BRC (formerly PATRIC)

**Topics Covered**:
- Fetching genome data from BV-BRC API
- Converting BV-BRC format to KBase Genome objects
- Loading genomes from local BV-BRC files
- Creating synthetic genomes from multiple sources
- Aggregating taxonomies across genome sets

**When to Use**:
- Importing genomes from BV-BRC database
- Creating composite/synthetic genomes
- Taxonomy analysis across genome collections
- Building KBase genome objects from external data

### [AssemblyUploadDownload.ipynb](AssemblyUploadDownload.ipynb)

**Purpose**: Upload and download genome assemblies in KBase

**Topics Covered**:
- Creating Assembly and AssemblySet objects
- Uploading FASTA files to KBase workspace
- Downloading assemblies from KBase
- Managing assembly collections
- JSON serialization of assembly metadata

**When to Use**:
- Uploading genome assemblies to KBase
- Downloading assemblies for local analysis
- Managing collections of genomes
- Working with assembly metadata

### [SKANIGenomeDistance.ipynb](SKANIGenomeDistance.ipynb)

**Purpose**: Fast genome sketching and ANI calculation with SKANI

**Topics Covered**:
- Creating sketch databases from genome directories
- Querying genomes for similarity
- Computing Average Nucleotide Identity (ANI)
- Managing multiple sketch databases
- Fast genome distance computation

**When to Use**:
- Taxonomic identification of genomes
- Finding similar genomes quickly
- Building reference genome databases
- Computing genome relatedness

### [ProteinLanguageModels.ipynb](ProteinLanguageModels.ipynb)

**Purpose**: Batch querying of protein language models

**Topics Covered**:
- Preparing protein sequences for analysis
- Batch querying PLM models
- Processing protein embeddings
- Integration with KBase protein data

**When to Use**:
- Protein function prediction
- Analyzing protein sequences with AI models
- Batch processing of proteomes
- Feature extraction from proteins

### [StoichiometryAnalysis.ipynb](StoichiometryAnalysis.ipynb)

**Purpose**: AI-powered metabolic reaction stoichiometry analysis

**Topics Covered**:
- Analyzing reaction stoichiometry
- Detecting mass and charge imbalances
- AI-suggested corrections
- Batch analysis of metabolic models

**When to Use**:
- Curating metabolic models
- Quality control of reaction databases
- Correcting stoichiometry errors
- Model reconstruction and refinement

### [AICuration.ipynb](AICuration.ipynb)

**Purpose**: AI-powered reaction curation with multiple backends

**Topics Covered**:
- Configuring AI backends (Argo vs Claude Code)
- Analyzing reaction directionality
- Categorizing reaction stoichiometry
- Evaluating reaction equivalence
- Assessing gene-reaction associations
- Batch processing multiple reactions
- Cache management
- Backend comparison and performance

**When to Use**:
- AI-powered metabolic model curation
- Reaction directionality prediction
- Stoichiometry validation and categorization
- Comparing reactions across databases
- Validating gene-reaction associations
- Testing different AI backends

### [KBaseWorkspaceUtilities.ipynb](KBaseWorkspaceUtilities.ipynb)

**Purpose**: KBase Workspace Service operations and datatype management

**Topics Covered**:
- Retrieving all KBase datatypes with `list_all_types()`
- Organizing datatypes by module
- Fetching type specifications with `get_type_specs()`
- Batch retrieval of all type specs
- Generating markdown documentation from type specs
- KIDL (KBase Interface Description Language) typespec format
- Module ownership request workflow
- Module version management
- Releasing modules to production

**When to Use**:
- Exploring available KBase datatypes
- Generating datatype documentation
- Creating new typespec modules
- Registering modules with KBase workspace
- Understanding KBase data structures
- Managing workspace module lifecycle

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
