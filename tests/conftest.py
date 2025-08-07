"""Shared test utilities and fixtures for the KBUtilLib test suite.

This module provides common fixtures, utilities, and configuration
that can be used across all test modules.
"""

import logging
import os
import shutil

# Import the modules for testing
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@pytest.fixture
def temp_dir():
    """Create a temporary directory for test files that gets cleaned up automatically."""
    temp_dir = tempfile.mkdtemp()
    yield temp_dir
    shutil.rmtree(temp_dir)


@pytest.fixture
def sample_config_file(temp_dir):
    """Create a sample configuration file for testing."""
    config_file = Path(temp_dir) / "test_config.ini"
    config_file.write_text("""[section1]
key1=value1
key2=value2

[section2]
key3=value3
key4=value4

[kbase]
url=https://kbase.us/services
token_namespace=kbase
""")
    return config_file


@pytest.fixture
def sample_token_file(temp_dir):
    """Create a sample token file for testing."""
    token_file = Path(temp_dir) / "test_tokens"
    token_file.write_text("""token1=abc123
token2=def456
service1=token_value1
service2=token_value2
""")
    return token_file


@pytest.fixture
def sample_kbase_token_file(temp_dir):
    """Create a sample KBase token file for testing (safe location)."""
    kbase_token_file = Path(temp_dir) / "test_kbase_token"
    kbase_token_file.write_text("test_kbase_token_value_safe")
    return kbase_token_file


@pytest.fixture
def mock_env_vars():
    """Mock environment variables for testing."""
    env_vars = {
        "KB_TEST_VAR": "kb_value",
        "KBASE_TEST_VAR": "kbase_value",
        "MS_TEST_VAR": "ms_value",
        "NOTEBOOK_TEST_VAR": "notebook_value",
        "OTHER_VAR": "other_value",  # This should not be loaded
    }
    with patch.dict(os.environ, env_vars):
        yield env_vars


@pytest.fixture
def safe_kbase_dir(temp_dir):
    """Create a safe .kbase directory structure for testing."""
    kbase_dir = Path(temp_dir) / ".kbase"
    kbase_dir.mkdir()
    kbase_token_file = kbase_dir / "token"
    kbase_token_file.write_text("safe_test_kbase_token")
    return kbase_dir


class TestUtilities:
    """Utility class with helper methods for testing."""

    @staticmethod
    def create_test_config(temp_dir: str, sections: dict) -> Path:
        """Create a test configuration file with custom sections.

        Args:
            temp_dir: Temporary directory path
            sections: Dictionary of sections and their key-value pairs

        Returns:
            Path to the created config file
        """
        config_file = Path(temp_dir) / "custom_config.ini"
        content = ""
        for section_name, section_data in sections.items():
            content += f"[{section_name}]\n"
            for key, value in section_data.items():
                content += f"{key}={value}\n"
            content += "\n"
        config_file.write_text(content)
        return config_file

    @staticmethod
    def create_test_token_file(temp_dir: str, tokens: dict) -> Path:
        """Create a test token file with custom tokens.

        Args:
            temp_dir: Temporary directory path
            tokens: Dictionary of token names and values

        Returns:
            Path to the created token file
        """
        token_file = Path(temp_dir) / "custom_tokens"
        content = ""
        for name, value in tokens.items():
            content += f"{name}={value}\n"
        token_file.write_text(content)
        return token_file

    @staticmethod
    def assert_log_contains(caplog, level: str, message: str):
        """Assert that a log message with specific level and content was recorded.

        Args:
            caplog: pytest caplog fixture
            level: Log level (INFO, WARNING, ERROR, DEBUG)
            message: Expected message content
        """
        level_num = getattr(logging, level.upper())
        for record in caplog.records:
            if record.levelno == level_num and message in record.message:
                return True
        pytest.fail(f"Log message '{message}' with level {level} not found in logs")

    @staticmethod
    def get_safe_file_paths(temp_dir: str) -> dict:
        """Get a dictionary of safe file paths for testing that won't interfere with system files.

        Args:
            temp_dir: Temporary directory path

        Returns:
            Dictionary with common file path keys
        """
        return {
            "token_file": Path(temp_dir) / "safe_tokens",
            "kbase_token_file": Path(temp_dir) / "safe_kbase_token",
            "config_file": Path(temp_dir) / "safe_config.ini",
            "kbase_dir": Path(temp_dir) / ".kbase",
        }


@pytest.fixture
def test_utils():
    """Provide the TestUtilities class as a fixture."""
    return TestUtilities


# Configure logging for tests
@pytest.fixture(autouse=True)
def configure_test_logging():
    """Configure logging for all tests."""
    logging.basicConfig(level=logging.DEBUG, force=True)
    yield
    # Reset logging after tests
    logging.getLogger().handlers.clear()


# Marker for slow tests
def pytest_configure(config):
    """Configure custom pytest markers."""
    config.addinivalue_line("markers", "slow: mark test as slow running")
    config.addinivalue_line("markers", "integration: mark test as integration test")
