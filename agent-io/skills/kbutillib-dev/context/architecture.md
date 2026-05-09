# KBUtilLib Architecture

Comprehensive architecture documentation for KBUtilLib developers.

## Core Design Philosophy

KBUtilLib (post-2026-05) is built on three core principles:

1. **Composition over inheritance** — Utility classes (`*Impl`) **hold** a `SharedEnvUtils` and any composed sibling Impls; they do NOT inherit from a common ancestor chain. The `KBUtilLib` facade in `toolkit.py` lazy-instantiates and wires them.
2. **Modularity** — Each utility is independent and self-contained. Sub-utilities depend on each other only via explicit constructor parameters.
3. **Simplicity** — Focused classes with clear responsibilities, lazy external clients, single-source-of-truth env.

> **Architectural history:** Pre-2026-05, KBUtilLib used multi-inheritance. The 2026-05 composition refactor (PRD: `agent-io/prds/kbutillib-composition-refactor/fullprompt.md`) inverted the design. KBJobUtils (`src/kbutillib/kb_job_utils/`) was the pilot module; the rest of the codebase followed.

## Repository Structure

```
KBUtilLib/
├── src/kbutillib/                    # Main package (~30 modules)
│   ├── __init__.py                   # Public exports + legacy class-name aliases
│   ├── __main__.py                   # CLI entry point (Click-based) — `kbu`
│   ├── toolkit.py                    # KBUtilLib facade with lazy sub-utility properties
│   │
│   ├── # Foundation Layer
│   ├── base_utils.py                 # BaseUtils - still inherited by SharedEnvUtils
│   ├── shared_env_utils.py           # SharedEnvUtils - HELD by every *Impl, not inherited
│   │
│   ├── # Flat-module helpers (no class)
│   ├── kbase_endpoints.py            # base_url, service_url, narrative_url
│   ├── compartments.py               # compartment_types, normalize_compartment
│   ├── model_directionality.py       # direction_conversion + helpers
│   ├── model_helpers.py              # _check_and_convert_model, _parse_id (canonical)
│   │
│   ├── # KBase Integration Layer (*Impl classes)
│   ├── kb_ws_utils.py                # KBWSUtilsImpl - Workspace Service API
│   ├── kb_genome_utils.py            # KBGenomeUtilsImpl
│   ├── kb_annotation_utils.py        # KBAnnotationUtilsImpl
│   ├── kb_model_utils.py             # KBModelUtilsImpl
│   ├── kb_reads_utils.py             # KBReadsUtilsImpl
│   ├── kb_callback_utils.py          # KBCallbackUtilsImpl
│   ├── kb_sdk_utils.py               # KBSDKUtilsImpl
│   ├── kb_uniprot_utils.py           # KBUniProtUtilsImpl
│   ├── kb_plm_utils.py               # KBPLMUtilsImpl
│   ├── kb_berdl_utils.py             # KBBERDLUtilsImpl
│   │
│   ├── # ModelSEED Integration Layer (*Impl)
│   ├── ms_biochem_utils.py           # MSBiochemUtilsImpl
│   ├── ms_fba_utils.py               # MSFBAUtilsImpl  (preserves AP3 carve-outs)
│   ├── ms_reconstruction_utils.py    # MSReconstructionUtilsImpl
│   │
│   ├── # AI/ML Layer (*Impl)
│   ├── argo_utils.py                 # ArgoUtilsImpl - LLM gateway (lazy client init)
│   ├── ai_curation_utils.py          # AICurationUtilsImpl
│   │
│   ├── # External APIs Layer (*Impl)
│   ├── bvbrc_utils.py                # BVBRCUtilsImpl
│   ├── patric_ws_utils.py            # PatricWSUtilsImpl
│   ├── rcsb_pdb_utils.py             # RCSBPDBUtilsImpl
│   │
│   ├── # Utility Layer
│   ├── escher_utils.py               # EscherUtilsImpl
│   ├── skani_utils.py                # SKANIUtilsImpl
│   ├── mmseqs_utils.py               # MMSeqsUtilsImpl
│   ├── model_standardization_utils.py # ModelStandardizationUtilsImpl
│   ├── thermo_utils.py               # ThermoUtilsImpl
│   │
│   ├── # KBJobUtils package (composition reference)
│   ├── kb_job_utils/
│   │   ├── state.py                  # JobState, JobStatus, ChainStep, PipelineState
│   │   ├── store.py                  # SQLite JobStore at ~/.kbjobs/kbjobs.db
│   │   ├── utils.py                  # KBJobUtils + Watcher
│   │   └── pipeline.py               # Linear chain support
│   │
│   ├── # Vendored clients
│   ├── installed_clients/
│   │   ├── WorkspaceClient.py
│   │   ├── AbstractHandleClient.py
│   │   ├── execution_engine2Client.py
│   │   └── baseclient.py, authclient.py
│   │
│   ├── # Notebook engine
│   ├── notebook/
│   │   ├── session.py                # NotebookSession (with .kbu, .cache, .vectors)
│   │   └── helpers/                  # Promoted helpers (compartment, reaction, fva)
│   │
│   └── cli/                          # `kbu init-notebook`, `kbu jobs`, `kbu jobdaemon`
│
├── notebooks/                   # Example Jupyter notebooks
│   ├── ConfigureEnvironment.ipynb
│   ├── BVBRCGenomeConversion.ipynb
│   ├── AssemblyUploadDownload.ipynb
│   ├── SKANIGenomeDistance.ipynb
│   ├── ProteinLanguageModels.ipynb
│   ├── StoichiometryAnalysis.ipynb
│   ├── AICuration.ipynb
│   └── KBaseWorkspaceUtilities.ipynb
│
├── examples/                    # Standalone example scripts
│   ├── example_ai_curation_usage.py
│   ├── example_bvbrc_usage.py
│   └── example_skani_usage.py
│
├── tests/                       # pytest test suite
│   ├── conftest.py             # Fixtures and configuration
│   ├── test_base_utils.py
│   └── test_*.py               # Module-specific tests
│
├── docs/                        # Sphinx documentation
│   ├── conf.py                 # Sphinx configuration
│   ├── index.md                # Documentation home
│   └── modules/                # Module documentation
│
├── dependencies/                # Git submodules
│   ├── ModelSEEDpy/
│   ├── ModelSEEDDatabase/
│   ├── cobrakbase/
│   └── cb_annotation_ontology_api/
│
├── config/                      # Configuration templates
│   └── default_config.yaml
│
├── pyproject.toml              # UV/pip packaging
├── DEPENDENCIES.md             # Dependency management docs
└── README.md                   # Project overview
```

## Architecture: composition graph

### KBUtilLib facade (toolkit.py)

```
KBUtilLib (single class; held by callers)
├── env: SharedEnvUtils (constructed once, shared across all sub-utilities)
└── lazy properties (constructed on first access; held thereafter)
    ├── ws → KBWSUtilsImpl(env)
    ├── callback → KBCallbackUtilsImpl(env, ws)
    ├── annotation → KBAnnotationUtilsImpl(env, ws, callback)
    ├── biochem → MSBiochemUtilsImpl(env)
    ├── model → KBModelUtilsImpl(env, ws, annotation, biochem)
    ├── fba → MSFBAUtilsImpl(env, model)         # AP3 carve-outs preserved
    ├── recon → MSReconstructionUtilsImpl(env, model)
    ├── escher → EscherUtilsImpl(env, model, biochem)
    ├── standardize → ModelStandardizationUtilsImpl(env, biochem)
    ├── genome → KBGenomeUtilsImpl(env, ws)
    ├── plm → KBPLMUtilsImpl(env, genome)
    ├── bvbrc → BVBRCUtilsImpl(env, genome, annotation)
    ├── reads → KBReadsUtilsImpl(env, ws)
    ├── sdk → KBSDKUtilsImpl(env, ws)
    ├── argo → ArgoUtilsImpl(env)
    ├── curation → AICurationUtilsImpl(env, argo)
    ├── thermo → ThermoUtilsImpl(env, biochem)
    ├── mmseqs → MMSeqsUtilsImpl(env)
    ├── skani → SKANIUtilsImpl(env)
    ├── berdl → KBBERDLUtilsImpl(env)
    ├── patric → PatricWSUtilsImpl(env)
    ├── uniprot → KBUniProtUtilsImpl(env)
    ├── pdb → RCSBPDBUtilsImpl(env)
    ├── catalog → CatalogClient (standalone)
    └── jobs → KBJobUtils(env)
```

### Class structure (every `*Impl`)

```python
class XxxImpl:
    def __init__(self, env: SharedEnvUtils, *deps) -> None:
        self.env = env                      # held, not inherited
        self.logger = env.logger            # delegated
        self.<dep_name> = <dep>             # composed siblings
        self._client = None                 # lazy external client

    @property
    def client(self):
        if self._client is None:
            self._client = SomeRemoteClient(token=self.env.get_token("..."))
        return self._client

    def public_method(self, ...):
        # Use self.env.* for config/tokens
        # Use self.<dep_name>.* for cross-utility calls
        # Use self.logger for logging
        ...
```

### Legacy alias layer (for import compatibility)

`__init__.py` exports both names:

```python
from .ms_fba_utils import MSFBAUtilsImpl
MSFBAUtils = MSFBAUtilsImpl  # legacy alias
```

Existing `from kbutillib import MSFBAUtils` still resolves. But constructor signatures changed (composition takes deps explicitly), so `MSFBAUtils()` from old code may need updating. Always prefer the facade.

## Configuration System

### Config File Priority
1. Explicit `config_file` parameter
2. `~/kbutillib_config.yaml` (user config)
3. `config/default_config.yaml` (repository defaults)

### Config File Structure
```yaml
# Example configuration
kbase:
  endpoint: https://kbase.us/services
  workspace_url: https://kbase.us/services/ws
  auth_service_url: https://kbase.us/services/auth

argo:
  endpoint: https://api.cels.anl.gov/argo/api/v1
  default_model: gpt4o

modelseed:
  database_path: ~/ModelSEEDDatabase

logging:
  level: INFO
```

### Token Management
```python
# Tokens stored per namespace
tokens = {
    "kbase": "...",
    "argo": "...",
    "custom": "..."
}

# Environment variables also checked:
# KBASE_AUTH_TOKEN, ARGO_API_TOKEN
```

## Provenance System

`SharedEnvUtils` (held by every `*Impl`) tracks calls. Inside an Impl method:

```python
class MyUtilsImpl:
    def __init__(self, env: SharedEnvUtils) -> None:
        self.env = env
        self.logger = env.logger

    def my_method(self, param1):
        self.env.initialize_call("my_method", {"param1": param1})
        result = self._do_work(param1)
        return result

# Access provenance from the facade
from kbutillib import KBUtilLib
kbu = KBUtilLib()
kbu.my_new.my_method("test")
print(kbu.env.provenance)
# [{"method": "my_method", "params": {"param1": "test"}, "timestamp": "..."}]
```

## Export System

The `__init__.py` exports the facade, all `*Impl` classes, legacy aliases, and key flat-module helpers:

```python
# src/kbutillib/__init__.py

# Foundation
from .base_utils import BaseUtils
from .shared_env_utils import SharedEnvUtils

# Facade (top-level entry point)
from .toolkit import KBUtilLib

# *Impl classes + legacy aliases
from .kb_ws_utils import KBWSUtilsImpl
from .ms_fba_utils import MSFBAUtilsImpl
# ... etc for every module
KBWSUtils = KBWSUtilsImpl     # legacy alias
MSFBAUtils = MSFBAUtilsImpl   # legacy alias

# KBJobUtils — composition pilot
from .kb_job_utils import (
    KBJobUtils, JobState, JobStatus,
    PipelineState, PipelineStatus, ChainStep,
)

# Optional - may have missing dependencies
try:
    from .kb_plm_utils import KBPLMUtilsImpl
    KBPLMUtils = KBPLMUtilsImpl
except ImportError:
    KBPLMUtils = KBPLMUtilsImpl = None

__all__ = [
    "BaseUtils", "SharedEnvUtils", "KBUtilLib",
    "KBWSUtilsImpl", "KBWSUtils",  # both names exported
    "MSFBAUtilsImpl", "MSFBAUtils",
    "KBJobUtils", "JobState", "JobStatus",
    "PipelineState", "PipelineStatus", "ChainStep",
    # ... etc
]
```

## Dependency Architecture

### Core Dependencies (always required)
- `requests` - HTTP client
- `pyyaml` - Configuration files
- `python-dotenv` - Environment variables

### Optional Dependencies (graceful degradation)
- `pandas` - DataFrame operations
- `cobra` - Constraint-based modeling
- `ipywidgets` - Notebook widgets
- `escher` - Pathway visualization

### Git Submodule Dependencies
Located in `dependencies/`:
- `ModelSEEDpy` - Metabolic modeling
- `ModelSEEDDatabase` - Biochemistry data
- `cobrakbase` - KBase COBRA extensions
- `cb_annotation_ontology_api` - Annotation ontology

## Testing Architecture

### Test Organization
```
tests/
├── conftest.py          # Shared fixtures
├── test_base_utils.py   # BaseUtils tests
├── test_kb_ws_utils.py  # KBWSUtils tests
└── ...
```

### Fixtures (conftest.py)
```python
import pytest

@pytest.fixture
def mock_config():
    return {
        "kbase": {"endpoint": "https://test.kbase.us"}
    }

@pytest.fixture
def base_utils():
    return BaseUtils()
```

### Test Patterns
```python
class TestBaseUtils:
    def test_initialization(self, base_utils):
        assert base_utils.logger is not None

    def test_logging(self, base_utils):
        base_utils.log_info("Test message")
        # Assert logging occurred

    @pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING"])
    def test_log_levels(self, base_utils, level):
        base_utils.logger.setLevel(level)
        assert base_utils.logger.level == getattr(logging, level)
```

## CI/CD Pipeline

### GitHub Actions Workflow
```yaml
# .github/workflows/ci.yml
name: CI
on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          submodules: recursive

      - name: Install uv
        uses: astral-sh/setup-uv@v1

      - name: Run tests
        run: uv run pytest

      - name: Lint
        run: uv run ruff check src/

      - name: Type check
        run: uv run mypy src/kbutillib/
```

## Build System

### pyproject.toml Structure
```toml
[project]
name = "kbutillib"
version = "0.1.0"
description = "Modular utility framework for bioinformatics"
requires-python = ">=3.9"
dependencies = [
    "requests>=2.28",
    "pyyaml>=6.0",
    "python-dotenv>=1.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=7.0",
    "pytest-cov>=4.0",
    "ruff>=0.1",
    "mypy>=1.0",
]
notebooks = [
    "jupyter>=1.0",
    "ipywidgets>=8.0",
    "itables>=1.0",
]

[project.scripts]
kbutillib = "kbutillib.__main__:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.ruff]
line-length = 100
target-version = "py39"

[tool.mypy]
python_version = "3.9"
ignore_missing_imports = true
```

## Documentation System

### Sphinx + MyST Configuration
```python
# docs/conf.py
extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
]

myst_enable_extensions = [
    "colon_fence",
    "deflist",
]
```

### Documentation Structure
```
docs/
├── conf.py              # Sphinx config
├── index.md             # Home page
├── getting-started.md   # Quick start
├── modules/             # Module docs
│   ├── base_utils.md
│   ├── kb_ws_utils.md
│   └── ...
└── api/                 # Auto-generated API docs
```

## Error Handling Patterns

### Standard Error Pattern
```python
def my_method(self, required_param, optional_param=None):
    if not required_param:
        raise ValueError("required_param is required")

    try:
        result = self._external_call(required_param)
    except ConnectionError as e:
        self.logger.error(f"Connection failed: {e}")
        raise
    except Exception as e:
        self.logger.error(f"Unexpected error: {e}")
        raise

    return result
```

### Graceful Degradation
```python
try:
    from .optional_module import OptionalFeature
    HAS_OPTIONAL = True
except ImportError:
    OptionalFeature = None
    HAS_OPTIONAL = False

class MyUtilsImpl:
    def __init__(self, env: SharedEnvUtils) -> None:
        self.env = env
        self.logger = env.logger

    def optional_method(self):
        if not HAS_OPTIONAL:
            self.logger.warning("Optional feature not available")
            return None
        return OptionalFeature.do_something()
```
