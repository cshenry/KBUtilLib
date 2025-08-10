# Module Documentation

KBUtilLib is built on a modular architecture that allows you to compose exactly the functionality you need. Each module provides specialized capabilities while inheriting from foundational modules to ensure consistent behavior and integration.

## Module Hierarchy

The modules are organized in a hierarchical structure where specialized modules build upon foundational ones:

```
BaseUtils (foundation)
├── SharedEnvUtils (configuration & auth)
│   ├── KBWSUtils (workspace operations)
│   │   ├── KBAnnotationUtils (annotation workflows)
│   │   │   └── KBModelUtils (metabolic modeling)
│   │   └── KBSDKUtils (SDK development)
│   ├── MSBiochemUtils (biochemistry database)
│   ├── KBCallbackUtils (callback management)
│   └── ArgoUtils (language models)
├── NotebookUtils (jupyter integration)
└── KBGenomeUtils (genome analysis)
```

## Core Foundation Modules

These modules provide the foundational capabilities that other modules build upon:

### [BaseUtils](base_utils.md)

**Foundation class with core functionality**

- Logging and error handling
- Dependency management
- Provenance tracking
- Configuration support

### [SharedEnvUtils](shared_env_utils.md)

**Configuration and authentication management**

- Configuration file loading
- Authentication token management
- Environment variable handling
- Secure credential storage

## Data Access Modules

These modules provide access to KBase services and external databases:

### [KBWSUtils](kb_ws_utils.md)

**KBase workspace operations and object management**

- Workspace creation and management
- Object saving and retrieval
- Reference handling
- Multi-environment support (prod/dev/ci)

### [MSBiochemUtils](ms_biochem_utils.md)

**ModelSEED biochemistry database access**

- Compound and reaction search
- Database synchronization
- Cross-reference integration
- Biochemical data analysis

## Analysis and Processing Modules

These modules provide specialized analysis capabilities:

### [KBGenomeUtils](kb_genome_utils.md)

**Genome analysis and sequence operations**

- Genome object manipulation
- Feature extraction and analysis
- Sequence translation and analysis
- Comparative genomics tools

### [KBAnnotationUtils](kb_annotation_utils.md)

**Gene and protein annotation workflows**

- Annotation ontology integration
- Feature function assignment
- Cross-database annotation mapping
- Quality assessment tools

### [KBModelUtils](kb_model_utils.md)

**Metabolic modeling and flux balance analysis**

- Model construction and analysis
- FBA preparation and execution
- Pathway analysis
- Growth phenotype prediction

## Development and Integration Modules

These modules support KBase SDK development and service integration:

### [KBSDKUtils](kb_sdk_utils.md)

**KBase SDK development tools and workflows**

- SDK environment setup
- Report generation
- File and data management
- Service client management

### [KBCallbackUtils](kb_callback_utils.md)

**Callback service management for SDK applications**

- Callback service lifecycle
- Scratch directory management
- Service integration
- Error handling and recovery

### [ArgoUtils](argo_utils.md)

**Language model integration and inference utilities**

- LLM service integration
- Multiple model support
- Response processing
- Bioinformatics prompt formatting

## Interactive and Visualization Modules

These modules enhance the user experience in interactive environments:

### [NotebookUtils](notebook_utils.md)

**Jupyter notebook enhancements and interactive displays**

- Enhanced visualizations
- Progress tracking
- Interactive widgets
- Export utilities

## Usage Patterns

### Single Module Usage

```python
from kbutillib.kb_genome_utils import KBGenomeUtils

genome_utils = KBGenomeUtils()
# Use genome-specific functionality
```

### Multiple Inheritance Composition

```python
from kbutillib import KBWSUtils, KBGenomeUtils, SharedEnvUtils

class GenomicsWorkflow(KBWSUtils, KBGenomeUtils, SharedEnvUtils):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def analyze_genome(self, genome_ref):
        # Combine workspace and genome utilities
        genome_object = self.get_object(genome_ref)
        features = self.extract_features(genome_object, "CDS")
        return features
```

### Pre-built combinations

```python
from kbutillib.examples import KBaseWorkbench, NotebookAnalysis

# Complete analysis environment
workbench = KBaseWorkbench(config_file="config.yaml")

# Notebook-optimized tools
notebook_tools = NotebookAnalysis()
```

## Module Integration

The modular design enables powerful combinations:

- **Data Pipeline**: `SharedEnvUtils` → `KBWSUtils` → `KBAnnotationUtils` → `KBModelUtils`
- **Notebook Analysis**: `NotebookUtils` + `MSBiochemUtils` + `KBGenomeUtils`
- **SDK Development**: `KBSDKUtils` + `KBCallbackUtils` + `KBWSUtils`
- **Full Stack**: All modules combined for comprehensive functionality

Each module is designed to work independently or in combination with others, providing maximum flexibility for your specific use case.

```{toctree}
---
maxdepth: 1
---

base_utils
shared_env_utils
kb_ws_utils
ms_biochem_utils
kb_genome_utils
kb_annotation_utils
kb_model_utils
kb_sdk_utils
kb_callback_utils
argo_utils
notebook_utils
```
