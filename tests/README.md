# KBUtilLib Test Suite

This directory contains the test suite for the KBUtilLib modular utility framework. The tests are organized into separate modules for better maintainability and isolation.

## Test Structure

```
tests/
├── conftest.py                  # Shared test utilities and fixtures
├── test_base_utils.py          # Tests for base_utils.py module
├── test_shared_environment.py  # Tests for shared_environment.py module
├── test_integration.py         # Cross-module integration tests
└── test_main.py                # Existing main module tests
```

## Test Modules

### `test_base_utils.py` (17 tests)

Tests the core BaseUtils functionality:

- Initialization and configuration
- Logging setup and methods
- Argument validation
- Attribute inspection utilities
- Logger level handling

### `test_shared_environment.py` (17 tests)

Tests the SharedEnvironment functionality:

- Configuration file reading and parsing
- Token management (loading, saving, retrieval)
- Environment variable handling
- KBase token integration (with safe test isolation)
- Inheritance from BaseUtils

### `test_integration.py` (8 tests)

Tests cross-module functionality:

- Full workflow integration
- Inheritance chain verification
- Multiple instance isolation
- Error handling across modules
- Configuration-driven workflows

### `conftest.py`

Provides shared test utilities:

- Temporary directory fixtures
- Sample configuration and token files
- Environment variable mocking
- Test utility classes
- Common assertion helpers

## Running Tests

### Run all tests:

```bash
python -m pytest tests/ -v
```

### Run specific module tests:

```bash
python -m pytest tests/test_base_utils.py -v
python -m pytest tests/test_shared_environment.py -v
python -m pytest tests/test_integration.py -v
```

### Run with coverage:

```bash
python -m pytest tests/ --cov=src/kbutillib --cov-report=html
```

## Test Safety

The test suite is designed with safety in mind:

- **No System File Modification**: All tests use temporary directories
- **KBase Token Protection**: Tests never modify real `~/.kbase/token` files
- **Isolated Execution**: Each test module can run independently
- **Clean Teardown**: Temporary files are automatically cleaned up

## Test Features

- **Comprehensive Coverage**: 42+ tests covering all major functionality
- **Safe Isolation**: Temporary files prevent system interference
- **Modular Design**: Tests match the modular structure of the codebase
- **Integration Testing**: Verifies cross-module interactions
- **Error Scenarios**: Tests edge cases and error conditions

## Adding New Tests

When adding new utility modules:

1. Create a corresponding `test_<module_name>.py` file
2. Use the shared fixtures from `conftest.py`
3. Add integration tests to `test_integration.py` if needed
4. Follow the existing test patterns for consistency

## Dependencies

The tests require:

- `pytest` for test execution
- `unittest.mock` for mocking (standard library)
- Standard library modules for file operations

No external testing dependencies are required beyond pytest itself.
