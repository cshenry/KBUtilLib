# Migration Guide: Moving Existing Utilities to KBUtilLib

This guide helps you migrate existing utility code into the KBUtilLib modular framework.

## Step 1: Analyze Your Existing Code

Before migrating, categorize your existing utilities:

- **KBase API operations** → `KBaseAPI` module
- **Genome/sequence analysis** → `KBGenomeUtils` module
- **Mass spectrometry/metabolomics** → `MSUtils` module
- **Metabolic modeling** → `KBModelUtil` module
- **Notebook/display utilities** → `NotebookUtils` module
- **Configuration/environment** → `SharedEnvironment` module
- **General utilities** → `BaseUtils` module or new custom modules

## Step 2: Migration Patterns

### Pattern 1: Standalone Functions

```python
# BEFORE: standalone function
def my_genome_function(sequence):
    # existing code
    return result

# AFTER: add to KBGenomeUtils
# In src/kbutillib/kb_genome_utils.py
class KBGenomeUtils(BaseUtils):
    # ... existing methods ...

    def my_genome_function(self, sequence):
        """Migrated function with documentation."""
        self.log_info(f"Processing sequence of length {len(sequence)}")
        # existing code (adapted as needed)
        return result
```

### Pattern 2: Class Methods

```python
# BEFORE: custom utility class
class MyGenomeTools:
    def __init__(self):
        self.setup_logging()

    def analyze_sequence(self, seq):
        # existing code
        pass

# AFTER: inherit from KBGenomeUtils and add methods
class MyGenomeTools(KBGenomeUtils):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # any additional initialization

    def analyze_sequence(self, seq):
        """Migrated method."""
        self.log_info("Analyzing sequence")
        # existing code (using self.log_info instead of custom logging)
        pass
```

### Pattern 3: Configuration/Secrets

```python
# BEFORE: hardcoded config
def get_kbase_data():
    token = "hardcoded_token"
    url = "https://kbase.us/services"
    # api calls...

# AFTER: use SharedEnvironment
class MyKBaseTools(KBaseAPI, SharedEnvironment):
    def get_kbase_data(self):
        token = self.get_auth_token("kbase")
        url = self.get_service_url("kbase") or self.kbase_url
        # api calls using inherited methods...
```

## Step 3: Adapting Existing Code

### Common Adaptations Needed:

1. **Logging**: Replace custom logging with `self.log_info()`, `self.log_error()`, etc.
2. **Configuration**: Use `self.get_config()` and `SharedEnvironment` methods
3. **Error Handling**: Leverage base class error handling patterns
4. **Documentation**: Add proper docstrings following the existing style

### Example Migration:

```python
# BEFORE: existing utility
import logging
import requests

class OldKBaseUtils:
    def __init__(self, token, url="https://kbase.us/services"):
        self.token = token
        self.url = url
        self.logger = logging.getLogger(__name__)

    def get_workspace_info(self, ws_id):
        headers = {"Authorization": f"Bearer {self.token}"}
        response = requests.post(f"{self.url}/ws", headers=headers, json={
            "method": "Workspace.get_workspace_info",
            "params": [{"id": ws_id}]
        })
        if response.status_code == 200:
            return response.json()
        else:
            self.logger.error(f"API call failed: {response.status_code}")
            return None

# AFTER: migrated to framework
from kbutillib import KBaseAPI

class MyKBaseUtils(KBaseAPI):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def get_workspace_info(self, ws_id):
        """
        Get workspace information (enhanced version of existing method).

        Args:
            ws_id: Workspace ID or name

        Returns:
            Workspace information dictionary
        """
        try:
            # Use inherited method which handles auth, error checking, etc.
            return super().get_workspace_info(ws_id)
        except Exception as e:
            self.log_error(f"Failed to get workspace info: {e}")
            return None
```

## Step 4: Testing Your Migration

1. **Create test instances**:

```python
# Test individual modules
from kbutillib import KBGenomeUtils
genome_utils = KBGenomeUtils()

# Test your migrated composite class
class MyTools(KBaseAPI, KBGenomeUtils, SharedEnvironment):
    pass

tools = MyTools(config_file="config.yaml")
```

2. **Verify functionality**:

```python
# Test that old functionality works
result = tools.my_migrated_function(test_data)
assert result == expected_result
```

3. **Check logging and configuration**:

```python
# Verify logging works
tools.log_info("Test message")

# Verify config loading
config_value = tools.get_config("some_setting")
```

## Step 5: Creating Domain-Specific Modules

If your utilities don't fit the existing modules, create new ones:

```python
# src/kbutillib/my_custom_utils.py
from .base_utils import BaseUtils

class MyCustomUtils(BaseUtils):
    """Custom utilities for my specific domain."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.log_info("MyCustomUtils initialized")

    def my_specialized_function(self, data):
        """Custom function for specialized workflow."""
        self.log_info("Running specialized function")
        # your custom logic here
        return result

# Update src/kbutillib/__init__.py
from .my_custom_utils import MyCustomUtils

__all__ = [
    # ... existing exports ...
    "MyCustomUtils",
]
```

## Step 6: Best Practices for Migration

1. **Preserve existing APIs**: Keep function signatures the same where possible
2. **Add comprehensive docstrings**: Follow the established pattern
3. **Use inherited logging**: Replace custom logging with base class methods
4. **Leverage shared configuration**: Move hardcoded values to config files
5. **Add error handling**: Use try/catch blocks and log errors appropriately
6. **Test thoroughly**: Ensure migrated code works with existing workflows

## Step 7: Advanced Integration

### Combining Multiple Utility Types:

```python
class AdvancedAnalysis(KBaseAPI, KBGenomeUtils, MSUtils, NotebookUtils):
    """Advanced analysis combining multiple domains."""

    def integrated_workflow(self, genome_ref, ms_data):
        """Example of workflow using multiple utility types."""
        # Get genome data (KBaseAPI)
        genome = self.get_object("workspace", genome_ref)

        # Analyze genome (KBGenomeUtils)
        features = self.extract_features_by_type(genome, "CDS")

        # Process MS data (MSUtils)
        peaks = self.find_peaks(ms_data["mz"], ms_data["intensity"])

        # Display results (NotebookUtils)
        if self.is_notebook_environment():
            self.display_json({"features": len(features), "peaks": len(peaks)})

        return {"genome_features": features, "ms_peaks": peaks}
```

### Custom Configuration Integration:

```python
# config.yaml
my_custom_settings:
  analysis_threshold: 0.05
  output_format: "json"

# In your migrated class
class MyAnalysis(BaseUtils, SharedEnvironment):
    def __init__(self, **kwargs):
        super().__init__(config_file="config.yaml", **kwargs)

        # Get custom settings
        self.threshold = self.get_config("my_custom_settings.analysis_threshold", 0.01)
        self.output_format = self.get_config("my_custom_settings.output_format", "csv")
```

## Summary

The migration process involves:

1. Categorizing existing code by domain
2. Adapting code to inherit from appropriate base classes
3. Replacing custom logging/config with framework methods
4. Testing the migrated functionality
5. Creating custom modules for specialized needs

This framework provides a solid foundation while preserving your existing functionality and making it more modular and reusable.
