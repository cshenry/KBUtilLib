# Quick Reference Guide

This guide helps you quickly identify which modules to use for common tasks.

## Common Use Cases

### I want to...

#### **Access KBase Data**

- **Load objects from KBase workspace** → [`KBWSUtils`](modules/kb_ws_utils.md)
- **Search biochemistry databases** → [`MSBiochemUtils`](modules/ms_biochem_utils.md)
- **Analyze genome objects** → [`KBGenomeUtils`](modules/kb_genome_utils.md)

#### **Develop KBase Applications**

- **Build KBase SDK services** → [`KBSDKUtils`](modules/kb_sdk_utils.md) + [`KBCallbackUtils`](modules/kb_callback_utils.md)
- **Create reports and outputs** → [`KBSDKUtils`](modules/kb_sdk_utils.md)
- **Handle authentication** → [`SharedEnvUtils`](modules/shared_env_utils.md)

#### **Perform Scientific Analysis**

- **Analyze genomic sequences** → [`KBGenomeUtils`](modules/kb_genome_utils.md)
- **Annotate genes and proteins** → [`KBAnnotationUtils`](modules/kb_annotation_utils.md)
- **Build metabolic models** → [`KBModelUtils`](modules/kb_model_utils.md)
- **Search metabolic compounds** → [`MSBiochemUtils`](modules/ms_biochem_utils.md)

#### **Work in Jupyter Notebooks**

- **Enhanced plotting and display** → [`NotebookUtils`](modules/notebook_utils.md)
- **Interactive data exploration** → [`NotebookUtils`](modules/notebook_utils.md)
- **Progress tracking** → [`NotebookUtils`](modules/notebook_utils.md)

#### **Use Language Models**

- **Generate bioinformatics insights** → [`ArgoUtils`](modules/argo_utils.md)
- **Process biological text** → [`ArgoUtils`](modules/argo_utils.md)
- **Automated analysis** → [`ArgoUtils`](modules/argo_utils.md)

## Module Combinations

### For Different Workflows

#### **Basic KBase Access**

```python
from kbutillib import KBWSUtils, SharedEnvUtils

class BasicKBase(KBWSUtils, SharedEnvUtils):
    pass
```

#### **Genome Analysis Workflow**

```python
from kbutillib import KBGenomeUtils, KBWSUtils, SharedEnvUtils

class GenomeAnalysis(KBGenomeUtils, KBWSUtils, SharedEnvUtils):
    pass
```

#### **Metabolic Modeling Pipeline**

```python
from kbutillib import KBModelUtils, KBAnnotationUtils, MSBiochemUtils, KBWSUtils, SharedEnvUtils

class MetabolicModeling(KBModelUtils, KBAnnotationUtils, MSBiochemUtils, KBWSUtils, SharedEnvUtils):
    pass
```

#### **Notebook-based Analysis**

```python
from kbutillib import NotebookUtils, KBGenomeUtils, MSBiochemUtils, SharedEnvUtils

class NotebookAnalysis(NotebookUtils, KBGenomeUtils, MSBiochemUtils, SharedEnvUtils):
    pass
```

#### **SDK Service Development**

```python
from kbutillib import KBSDKUtils, KBCallbackUtils, KBAnnotationUtils, KBWSUtils, SharedEnvUtils

class KBaseService(KBSDKUtils, KBCallbackUtils, KBAnnotationUtils, KBWSUtils, SharedEnvUtils):
    pass
```

#### **AI-Enhanced Analysis**

```python
from kbutillib import ArgoUtils, KBGenomeUtils, KBAnnotationUtils, NotebookUtils, SharedEnvUtils

class AIBioinformatics(ArgoUtils, KBGenomeUtils, KBAnnotationUtils, NotebookUtils, SharedEnvUtils):
    pass
```

## Quick Start Examples

### Load and Analyze a Genome

```python
from kbutillib import KBGenomeUtils, KBWSUtils, SharedEnvUtils

class QuickGenome(KBGenomeUtils, KBWSUtils, SharedEnvUtils):
    pass

analyzer = QuickGenome(config_file="config.yaml")
genome = analyzer.get_object("my_workspace", "my_genome")
features = analyzer.extract_features(genome, "CDS")
```

### Search Biochemistry Database

```python
from kbutillib import MSBiochemUtils, SharedEnvUtils

class QuickSearch(MSBiochemUtils, SharedEnvUtils):
    pass

searcher = QuickSearch()
compounds = searcher.search_compounds(query="glucose")
```

### Build a Metabolic Model

```python
from kbutillib import KBModelUtils, KBAnnotationUtils, MSBiochemUtils, KBWSUtils, SharedEnvUtils

class QuickModel(KBModelUtils, KBAnnotationUtils, MSBiochemUtils, KBWSUtils, SharedEnvUtils):
    pass

modeler = QuickModel(config_file="config.yaml")
model = modeler.build_metabolic_model(genome_ref="123/456/7")
```

### Create Interactive Notebook

```python
from kbutillib import NotebookUtils, KBGenomeUtils, SharedEnvUtils

class QuickNotebook(NotebookUtils, KBGenomeUtils, SharedEnvUtils):
    pass

nb = QuickNotebook(notebook_folder="./analysis")
nb.plot_genome_features(genome_data)
```

## Module Dependencies

Understanding module relationships helps you choose the right combination:

```
BaseUtils (required by all)
├── SharedEnvUtils (configuration)
│   ├── KBWSUtils (workspace access)
│   │   ├── KBAnnotationUtils (annotation)
│   │   │   └── KBModelUtils (modeling)
│   │   └── KBSDKUtils (SDK development)
│   ├── MSBiochemUtils (biochemistry)
│   ├── ArgoUtils (language models)
│   └── KBCallbackUtils (callbacks)
├── NotebookUtils (notebooks)
└── KBGenomeUtils (genome analysis)
```

## Configuration Requirements

### Minimal Configuration

- **SharedEnvUtils**: Optional config file, optional tokens
- **BaseUtils descendants**: No special configuration needed

### KBase Integration

- **KBWSUtils**: Requires KBase authentication token
- **KBSDKUtils**: Requires SDK environment variables
- **KBCallbackUtils**: Requires callback service configuration

### External Services

- **ArgoUtils**: Requires Argo service access tokens
- **MSBiochemUtils**: Automatically downloads ModelSEED database

## Performance Considerations

### For Large-scale Analysis

- Use `MSBiochemUtils` for batch compound searches
- Use `KBGenomeUtils` for efficient feature extraction
- Use `KBModelUtils` for automated model building

### For Interactive Work

- Combine with `NotebookUtils` for enhanced display
- Use progress tracking for long-running operations
- Cache results using `SharedEnvUtils` configuration

### For Production Services

- Use `KBSDKUtils` + `KBCallbackUtils` for robust services
- Implement proper error handling with `BaseUtils` logging
- Use `SharedEnvUtils` for secure credential management
