# KBUtilLib Development Guide

Step-by-step guide for developing and contributing to KBUtilLib.

## Development Setup

### Prerequisites
- Python 3.9+
- `uv` package manager (recommended)
- Git with submodule support

### Initial Setup
```bash
# Clone repository
git clone https://github.com/your-org/KBUtilLib.git
cd KBUtilLib

# Initialize submodules
git submodule update --init --recursive

# Install with development dependencies
uv sync --all-extras

# Verify installation
uv run python -c "import kbutillib; print(kbutillib.__all__)"
```

### Alternative: pip Installation
```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install in editable mode
pip install -e ".[dev,notebooks]"
```

## Adding a New Utility Module (composition pattern)

### Step 1: Plan Your Module

Before coding, determine:
1. **Purpose** — What does this module do?
2. **Composed dependencies** — Which sibling `*Impl` classes does it need? (e.g., `env`, `ws`, `biochem`). Match the pattern in PRD §6.3.
3. **External dependencies** — What pip libraries / vendored clients?
4. **API surface** — What methods will be public?
5. **Facade attribute name** — Pick a short attribute (e.g., `kbu.<name>`); avoid overlap with existing 25.

Reference shape: read `src/kbutillib/kb_job_utils/utils.py` first.

### Step 2: Create the Module File

```python
# src/kbutillib/my_new_utils.py
"""My New Utilities module.

This module provides utilities for [purpose].

Example:
    >>> from kbutillib import KBUtilLib
    >>> kbu = KBUtilLib()
    >>> result = kbu.my_new.my_method("param")
"""

from typing import Any, Dict, Optional

from .shared_env_utils import SharedEnvUtils
# Composed sibling Impls (if any):
# from .kb_ws_utils import KBWSUtilsImpl


class MyNewUtilsImpl:
    """Utility class for [purpose].

    Composes SharedEnvUtils (held, NOT inherited). External clients
    constructed lazily on first access.

    Attributes:
        env: SharedEnvUtils instance held for config + tokens + logger.
        logger: Delegated to env.logger.

    Example:
        >>> from kbutillib import KBUtilLib
        >>> kbu = KBUtilLib(env=SharedEnvUtils(config_file="my_config.yaml"))
        >>> kbu.my_new.my_method("test")
    """

    def __init__(
        self,
        env: SharedEnvUtils,
        # ws: KBWSUtilsImpl | None = None,  # if composed
        **kwargs: Any,
    ) -> None:
        """Initialize MyNewUtilsImpl.

        Args:
            env: Shared environment (config, tokens, logger).
            ws: Composed workspace utility (if needed).
        """
        self.env = env
        self.logger = env.logger
        # self.ws = ws

        # External-client placeholders (lazy)
        self._client = None

        # Module-specific state
        self._cache: Dict[str, Any] = {}

    @property
    def client(self):
        """Lazy external client; constructed on first access."""
        if self._client is None:
            self._client = SomeRemoteClient(token=self.env.get_token("my_service"))
        return self._client

    def my_method(
        self,
        required_param: str,
        optional_param: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Brief description of method.

        Args:
            required_param: Description.
            optional_param: Description. Defaults to None.

        Returns:
            Dictionary containing key1, key2.

        Raises:
            ValueError: When required_param is empty.

        Example:
            >>> kbu.my_new.my_method("test", optional_param=5)
        """
        self.env.initialize_call("my_method", {
            "required_param": required_param,
            "optional_param": optional_param,
        })

        if not required_param:
            raise ValueError("required_param cannot be empty")

        cache_key = f"my_method:{required_param}"
        if cache_key in self._cache:
            self.logger.debug(f"Cache hit: {cache_key}")
            return self._cache[cache_key]

        self.logger.info(f"Processing: {required_param}")
        result = self._do_actual_work(required_param, optional_param)
        self._cache[cache_key] = result
        return result

    def _do_actual_work(self, param1: str, param2: Optional[int]) -> Dict[str, Any]:
        return {"key1": param1, "key2": param2 or 0}


# Legacy alias for import compatibility (constructor signature changed)
MyNewUtils = MyNewUtilsImpl
```

### Step 3: Wire into the KBUtilLib facade

```python
# src/kbutillib/toolkit.py

class KBUtilLib:
    def __init__(self, env: SharedEnvUtils | None = None, **env_kwargs):
        # ... existing __init__ ...
        self._my_new = None  # add this line

    # Add a lazy property:
    @property
    def my_new(self) -> "MyNewUtilsImpl":
        if self._my_new is None:
            from .my_new_utils import MyNewUtilsImpl
            self._my_new = MyNewUtilsImpl(self.env)
            # If your *Impl composes other siblings, pass them here:
            # self._my_new = MyNewUtilsImpl(self.env, self.ws)
        return self._my_new
```

### Step 4: Add to Package Exports

```python
# src/kbutillib/__init__.py

# Add import with try/except for optional deps:
try:
    from .my_new_utils import MyNewUtilsImpl
    MyNewUtils = MyNewUtilsImpl  # legacy alias
except ImportError as e:
    import logging
    logging.getLogger(__name__).debug(f"MyNewUtilsImpl not available: {e}")
    MyNewUtilsImpl = MyNewUtils = None

__all__ = [
    # ... existing exports ...
    "MyNewUtilsImpl", "MyNewUtils",
]
```

### Step 5: Write Tests

```python
# tests/test_my_new_utils.py
"""Tests for MyNewUtilsImpl."""

import pytest
from kbutillib import KBUtilLib, MyNewUtilsImpl


class TestMyNewUtilsImpl:
    @pytest.fixture
    def kbu(self):
        return KBUtilLib()

    def test_facade_construction(self, kbu):
        assert kbu.my_new is not None
        assert isinstance(kbu.my_new, MyNewUtilsImpl)

    def test_facade_lazy_singleton(self, kbu):
        assert kbu.my_new is kbu.my_new  # cached

    def test_my_method_basic(self, kbu):
        result = kbu.my_new.my_method("test_param")
        assert result["key1"] == "test_param"

    def test_my_method_with_optional(self, kbu):
        result = kbu.my_new.my_method("test", optional_param=42)
        assert result["key2"] == 42

    def test_my_method_empty_param_raises(self, kbu):
        with pytest.raises(ValueError, match="cannot be empty"):
            kbu.my_new.my_method("")

    def test_my_method_caching(self, kbu):
        result1 = kbu.my_new.my_method("cached_param")
        result2 = kbu.my_new.my_method("cached_param")
        assert result1 is result2

    @pytest.mark.parametrize("param,expected", [
        ("a", "a"), ("test", "test"), ("longer_param", "longer_param"),
    ])
    def test_my_method_various_inputs(self, kbu, param, expected):
        result = kbu.my_new.my_method(param)
        assert result["key1"] == expected


class TestMyNewUtilsImplDirect:
    """Tests bypassing the facade (for unit-testing in isolation)."""

    def test_direct_construction(self):
        from kbutillib import SharedEnvUtils
        env = SharedEnvUtils()
        impl = MyNewUtilsImpl(env)
        assert impl.env is env
        assert impl.logger is env.logger
```

### Step 6: Add Documentation

```markdown
# docs/modules/my_new_utils.md

# MyNewUtilsImpl

Utility class for [purpose] (composition pattern).

## Overview

`MyNewUtilsImpl` holds a `SharedEnvUtils` and is exposed as `kbu.my_new`
on the `KBUtilLib` facade. Legacy alias `MyNewUtils` remains for
import compatibility.

## Quick Start

```python
from kbutillib import KBUtilLib
kbu = KBUtilLib()
result = kbu.my_new.my_method("parameter")
```

## API Reference

### my_method(required_param, optional_param=None) → dict
Brief description.

## See Also
- [Architecture](../architecture.md) — composition pattern
- [`KBJobUtils`](kb_job_utils.md) — composition reference shape
```

### Step 7: Update README

Add a brief mention to the main README.md if the module is significant.

## Running Tests

### Full Test Suite
```bash
# Run all tests
uv run pytest

# With coverage report
uv run pytest --cov=kbutillib --cov-report=html

# Verbose output
uv run pytest -v

# Stop on first failure
uv run pytest -x
```

### Specific Tests
```bash
# Single file
uv run pytest tests/test_my_new_utils.py

# Single test class
uv run pytest tests/test_my_new_utils.py::TestMyNewUtils

# Single test
uv run pytest tests/test_my_new_utils.py::TestMyNewUtils::test_my_method_basic

# Pattern matching
uv run pytest -k "my_method"
```

### Test Markers
```bash
# Skip slow tests
uv run pytest -m "not slow"

# Only integration tests
uv run pytest -m integration
```

## Code Quality

### Linting with Ruff
```bash
# Check for issues
uv run ruff check src/

# Auto-fix issues
uv run ruff check --fix src/

# Format code
uv run ruff format src/
```

### Type Checking with MyPy
```bash
# Check types
uv run mypy src/kbutillib/

# Specific file
uv run mypy src/kbutillib/my_new_utils.py
```

### Pre-commit Hooks
```bash
# Install hooks
uv run pre-commit install

# Run manually
uv run pre-commit run --all-files
```

## Working with Dependencies

### Adding Runtime Dependencies
```bash
# Add to project
uv add requests

# With version constraint
uv add "requests>=2.28"
```

### Adding Development Dependencies
```bash
uv add --dev pytest-cov
```

### Adding Optional Dependencies
Edit pyproject.toml:
```toml
[project.optional-dependencies]
ml = ["torch>=2.0", "transformers>=4.0"]
```

### Managing Git Submodules
```bash
# Initialize
git submodule update --init --recursive

# Update to latest
git submodule update --remote

# Check status
git submodule status
```

## Common Development Patterns (composition)

### Basic *Impl shape
```python
from .shared_env_utils import SharedEnvUtils

class MyUtilsImpl:
    def __init__(self, env: SharedEnvUtils) -> None:
        self.env = env
        self.logger = env.logger

    def my_method(self):
        self.env.initialize_call("my_method", {})
        self.logger.info("Starting...")
        # work
        self.logger.debug("Details...")
        return result
```

### Composing sibling Impls
```python
class MyAnnotationUtilsImpl:
    def __init__(
        self,
        env: SharedEnvUtils,
        ws: "KBWSUtilsImpl",
        biochem: "MSBiochemUtilsImpl",
    ) -> None:
        self.env = env
        self.logger = env.logger
        self.ws = ws
        self.biochem = biochem

    def annotate_genome(self, genome_ref):
        genome = self.ws.get_object(genome_ref)
        # Use composed sibling: self.biochem.search_compounds(...)
        ...
```

The facade wires deps automatically (see `toolkit.py` lazy properties).

### HTTP client pattern (with lazy construction)
```python
import requests
from .shared_env_utils import SharedEnvUtils

class MyAPIUtilsImpl:
    def __init__(self, env: SharedEnvUtils) -> None:
        self.env = env
        self.logger = env.logger
        self._session: requests.Session | None = None

    @property
    def session(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
        return self._session

    @property
    def base_url(self) -> str:
        return self.env.get_config_value("my_api.endpoint")

    def _request(self, method, endpoint, **kwargs):
        url = f"{self.base_url}/{endpoint}"
        headers = kwargs.pop("headers", {})
        token = self.env.get_token("my_api")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        response = self.session.request(method, url, headers=headers, **kwargs)
        response.raise_for_status()
        return response.json()

    def get_resource(self, resource_id):
        return self._request("GET", f"resources/{resource_id}")
```

### Caching Pattern
```python
from functools import lru_cache

class CachedUtilsImpl:
    def __init__(self, env: SharedEnvUtils) -> None:
        self.env = env
        self.logger = env.logger
        self._cache = {}

    def get_with_cache(self, key):
        if key not in self._cache:
            self._cache[key] = self._fetch(key)
        return self._cache[key]

    def clear_cache(self):
        self._cache.clear()

    @lru_cache(maxsize=100)
    def get_with_lru(self, key):
        """Uses built-in LRU cache."""
        return self._fetch(key)
```

### KBJobUtils package layout (multi-file Impl)

When an Impl is too big for one file, follow the KBJobUtils pattern:

```
src/kbutillib/my_complex_utils/
├── __init__.py                     # exports the *Impl class + state types
├── state.py                        # @dataclass for state objects + enums
├── store.py                        # SQLite or other persistence
├── utils.py                        # the *Impl class (main API)
└── (other.py)                      # optional: workers, helpers
```

Reference: `src/kbutillib/kb_job_utils/`. The package's `__init__.py` re-exports the public surface; the facade only sees the package-level imports.

## Debugging Tips

### Enable Debug Logging
```python
import logging
logging.getLogger("kbutillib").setLevel(logging.DEBUG)

utils = MyUtils()
utils.my_method("test")  # Will show debug output
```

### Interactive Debugging
```python
# Add breakpoint
import pdb; pdb.set_trace()

# Or use pytest debugging
# pytest --pdb  # Drop into debugger on failure
# pytest --pdb-first  # Drop on first failure
```

### Inspect Provenance
```python
from kbutillib import KBUtilLib
kbu = KBUtilLib()
kbu.my_new.my_method("test")
kbu.my_new.another_method("param")

# Provenance lives on the held SharedEnvUtils
for call in kbu.env.provenance:
    print(f"{call['method']}: {call['params']}")
```

## Pull Request Checklist

Before submitting a PR:

- [ ] All tests pass: `uv run pytest` (incl. `tests/test_composition_smoke.py`)
- [ ] New `*Impl` follows composition pattern — holds `env: SharedEnvUtils`, NOT inheriting
- [ ] New `*Impl` wired into `KBUtilLib` facade in `toolkit.py` as a lazy property
- [ ] Legacy alias added in `__init__.py` (`MyUtils = MyUtilsImpl`)
- [ ] Linting passes: `uv run ruff check src/`
- [ ] Types check: `uv run mypy src/kbutillib/`
- [ ] New code has tests (both via `KBUtilLib()` facade and direct `Impl` construction)
- [ ] Docstrings follow Google style
- [ ] README updated if adding major feature
- [ ] No secrets in code

## Troubleshooting

### Import Errors
```python
# Check if module is available
from kbutillib import MyUtilsImpl
if MyUtilsImpl is None:
    print("Module not available - check dependencies")

# Or via facade — accessing the property triggers import
from kbutillib import KBUtilLib
kbu = KBUtilLib()
try:
    kbu.my_new
except (ImportError, AttributeError) as e:
    print(f"Module not loaded: {e}")
```

### Submodule Issues
```bash
# Reset submodules
git submodule deinit -f --all
git submodule update --init --recursive
```

### Test Discovery Issues
```bash
# Check pytest can find tests
uv run pytest --collect-only

# Verbose collection
uv run pytest --collect-only -v
```
