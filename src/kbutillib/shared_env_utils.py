"""Shared environment management for configuration and secrets."""

import os
import shutil
from configparser import ConfigParser
from pathlib import Path
from typing import Any, Dict, Optional, Union

from .base_utils import BaseUtils

# Standard kbutillib directory
KBUTILLIB_DIR = Path.home() / ".kbutillib"
DEFAULT_CONFIG_FILE = KBUTILLIB_DIR / "config.yaml"


class SharedEnvUtils(BaseUtils):
    """Manages shared environment configuration, secrets, and runtime settings.

    Provides centralized access to configuration files, environment variables,
    authentication tokens, and other shared resources across utility modules.

    Configuration priority order:
    1. Explicitly provided config_file parameter
    2. ~/.kbutillib/config.yaml (user config)
    3. Project root config.yaml (repository default)
    """

    def __init__(
        self,
        config_file: Optional[Union[str, Path]] = None,
        token_file: Optional[Union[str, Path]] = Path.home() / ".tokens",
        kbase_token_file: Optional[Union[str, Path]] = Path.home() / ".kbase" / "token",
        token: Optional[Union[str, Dict[str, str]]] = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the shared environment which manages configurations and authentication tokens.

        Args:
            config_file: Optional explicit config file path. If None, uses priority order.
            token_file: Path to token file (default: ~/.tokens)
            kbase_token_file: Path to KBase token file (default: ~/.kbase/token)
            token: Optional token(s) to set (string or dict)
            **kwargs: Additional arguments passed to BaseUtils
        """
        super().__init__(**kwargs)

        # Determine config file using priority order
        self._config_hash = {}
        self._config_file = self._find_config_file(config_file)

        if self._config_file:
            self._config_hash = self.read_config()
            self.log_info(f"Loaded configuration from: {self._config_file}")

        # Reading token file
        self._token_hash = {}
        self._env_vars = {}
        self._token_file = token_file
        self._kbase_token_file = kbase_token_file
        if (token_file and Path(token_file).exists()) or (
            kbase_token_file and Path(kbase_token_file).exists()
        ):
            self._token_hash = self.read_token_file()
        # Loading environment variables
        self.load_environment_variables()

        # Setting KBase token from input argument if provided
        if token is not None:
            if isinstance(token, str):
                # If token is a string, set it directly
                self.set_token(token, namespace="kbase")
            elif isinstance(token, dict):
                # If token is a dictionary, set each key-value pair
                for key, value in token.items():
                    self.set_token(value, key)

    def _find_config_file(self, explicit_path: Optional[Union[str, Path]] = None) -> Optional[Path]:
        """Find the configuration file using priority order.

        Priority:
        1. Explicitly provided path
        2. ~/.kbutillib/config.yaml (user config)
        3. Project root config.yaml (repository default)

        Args:
            explicit_path: Optional explicit config file path

        Returns:
            Path to config file, or None if not found
        """
        # Priority 1: Explicit path
        if explicit_path:
            explicit = Path(explicit_path)
            if explicit.exists():
                return explicit
            else:
                self.log_warning(f"Explicit config file not found: {explicit_path}")
                return None

        # Priority 2: User config in ~/.kbutillib/
        if DEFAULT_CONFIG_FILE.exists():
            return DEFAULT_CONFIG_FILE

        # Priority 3: Project root config.yaml
        # Try to find project root by looking for config.yaml in parent directories
        current = Path.cwd()
        for _ in range(5):  # Search up to 5 levels
            project_config = current / "config.yaml"
            if project_config.exists():
                return project_config
            current = current.parent

        self.log_debug("No configuration file found")
        return None

    # Reading config file - supports both YAML and INI formats
    def read_config(
        self, config_file: Optional[Union[str, Path]] = None
    ) -> Dict[str, Any]:
        """Read configuration from a file.

        Supports both YAML (.yaml, .yml) and INI formats.

        Args:
            config_file: Optional path to config file. Uses self._config_file if None.

        Returns:
            Configuration dictionary

        Raises:
            Exception: If config file cannot be read or parsed
        """
        if config_file is None:
            config_file = self._config_file

        if config_file is None:
            return {}

        config_path = Path(config_file)
        confighash = {}

        try:
            if not config_path.exists():
                self.log_warning(f"Config file not found: {config_path}")
                return confighash

            # Determine format from file extension
            if config_path.suffix in ['.yaml', '.yml']:
                # YAML format
                try:
                    import yaml
                    with open(config_path, 'r') as f:
                        confighash = yaml.safe_load(f) or {}
                    self.log_debug(f"Loaded YAML config from {config_path}")
                except ImportError:
                    self.log_error(
                        "PyYAML not installed. Install with: pip install pyyaml"
                    )
                    raise
            else:
                # INI format (ConfigParser)
                config = ConfigParser()
                config.read(config_path)
                for section in config.sections():
                    confighash[section] = {}
                    for nameval in config.items(section):
                        confighash[section][nameval[0]] = nameval[1]
                self.log_debug(f"Loaded INI config from {config_path}")

            return confighash

        except Exception as e:
            self.log_error(f"Error parsing config file {config_path}: {e}")
            raise

    def get_config(self, section, key, default=None):
        """Get a configuration value from section.key format.

        Deprecated: Use get_config_value() with dot notation instead.

        Args:
            section: Configuration section
            key: Configuration key
            default: Default value if not found

        Returns:
            Configuration value or default
        """
        if section not in self._config_hash:
            self.log_debug(f"Section '{section}' not found in config")
            return default
        if key not in self._config_hash[section]:
            self.log_debug(f"Key '{key}' not found in section '{section}'")
            return default
        return self._config_hash.get(section).get(key)

    def get_config_value(self, key_path: str, default: Any = None) -> Any:
        """Get a configuration value using dot notation.

        Supports nested dictionary access with dot-separated keys.

        Args:
            key_path: Dot-separated path to config value (e.g., "skani.executable")
            default: Default value if key not found

        Returns:
            Configuration value or default

        Examples:
            >>> util.get_config_value("skani.executable")
            'skani'
            >>> util.get_config_value("skani.cache_file")
            '~/.kbutillib/skani_databases.json'
            >>> util.get_config_value("paths.data_dir", default="./data")
            './data'
        """
        keys = key_path.split('.')
        value = self._config_hash

        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                self.log_debug(f"Config key '{key_path}' not found, using default: {default}")
                return default

        return value

    def read_token_file(
        self, token_file: Optional[Union[str, Path]] = None
    ) -> Dict[str, Any]:
        """Load authentication tokens from a file."""
        # Load a standard text file with token name on one line and token value on the next
        token_hash = {}
        if token_file is None:
            if self._token_file:
                token_file = self._token_file

        token_path = Path(token_file)
        try:
            if token_path.exists():
                self.log_info(f"Loaded {len(token_hash)} tokens from {token_file}")
                with open(token_path) as fh:
                    for line in fh:
                        if line.strip():  # Ignore empty lines
                            key, value = line.strip().split("=", 1)
                            token_hash[key] = value
            if self._kbase_token_file is not None and self._kbase_token_file.exists():
                self.log_info(f"Loaded kbase tokens from {self._kbase_token_file}")
                with open(self._kbase_token_file) as fh:
                    token_hash["kbase"] = fh.read().strip()
            return token_hash
        except Exception as e:
            self.log_error(f"Error loading tokens from {token_file}: {e}")
            raise

    def save_token_file(self, token_file: Optional[Union[str, Path]] = None):
        """Save authentication tokens to a file."""
        if token_file is None:
            token_file = self._token_file
        if token_file is None:
            self.log_warning("No token file specified for saving")
            return

        token_path = Path(token_file)
        try:
            with open(token_path, "w") as fh:
                for key, value in self._token_hash.items():
                    fh.write(f"{key}={value}\n")
            # Also saving kbase tokens in the .kbase directory to ensure compatibility with CobraKBase
            if "kbase" in self._token_hash:
                if self._kbase_token_file is not None:
                    directory = Path(self._kbase_token_file).parent
                    if not directory.exists():
                        directory.mkdir(parents=True, exist_ok=True)
                with open(self._kbase_token_file, "w") as fh:
                    fh.write(self._token_hash["kbase"])
            self.log_info(f"Saved {len(self._token_hash)} tokens to {token_file}")
        except Exception as e:
            self.log_error(f"Error saving tokens to {token_file}: {e}")
            raise

    def set_token(self, token, namespace="kbase", save_file=True):
        """Set a token and save it to the token file."""
        self._token_hash[namespace] = token
        if save_file:
            self.save_token_file()

    # Retrieves specified token from cached data
    def get_token(self, namespace="kbase") -> Any:
        """Retrieve a stored secret."""
        return self._token_hash.get(namespace, None)

    def load_environment_variables(self) -> None:
        """Load relevant environment variables into the shared environment."""
        # Load common environment variables that might be used across utilities
        env_prefixes = ["KB_", "KBASE_", "MS_", "NOTEBOOK_"]

        for key, value in os.environ.items():
            if any(key.startswith(prefix) for prefix in env_prefixes):
                self._env_vars[key] = value

        self.log_debug(f"Loaded {len(self._env_vars)} environment variables")

    def get_env_var(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Get an environment variable value.

        Args:
            key: Environment variable name
            default: Default value if not found

        Returns:
            Environment variable value or default
        """
        return self._env_vars.get(key, default)

    # Printing environment state for debugging or inspection
    def export_environment(self) -> Dict[str, Any]:
        """Export the current environment state for debugging or inspection."""
        return {
            "config": self._config_hash if self._config_hash else {},
            "config_file": str(self._config_file) if self._config_file else None,
            "kbase_token_file": str(self._kbase_token_file)
            if self._kbase_token_file
            else None,
            "token_file": str(self._token_file) if self._token_file else None,
            "env_vars": self._env_vars if self._env_vars else {},
            "token_keys": list(self._token_hash.keys())
            if self._token_hash
            else [],  # Don't expose secret values
        }

    @staticmethod
    def initialize_environment(
        source_config: Optional[Union[str, Path]] = None,
        force: bool = False
    ) -> Dict[str, Any]:
        """Initialize the ~/.kbutillib environment directory and configuration.

        Creates the ~/.kbutillib directory and copies the default config.yaml
        from the project root if it doesn't exist.

        Args:
            source_config: Optional path to config file to copy. If None, searches
                          for config.yaml in the current working directory and parent dirs.
            force: If True, overwrites existing config file in ~/.kbutillib/

        Returns:
            Dict with status information:
            {
                "success": bool,
                "directory_created": bool,
                "config_copied": bool,
                "config_path": str,
                "message": str
            }

        Examples:
            >>> # Initialize with default config from project
            >>> SharedEnvUtils.initialize_environment()

            >>> # Initialize with specific config file
            >>> SharedEnvUtils.initialize_environment(source_config="/path/to/config.yaml")

            >>> # Force overwrite existing config
            >>> SharedEnvUtils.initialize_environment(force=True)
        """
        result = {
            "success": False,
            "directory_created": False,
            "config_copied": False,
            "config_path": str(DEFAULT_CONFIG_FILE),
            "message": ""
        }

        # Create ~/.kbutillib directory if it doesn't exist
        if not KBUTILLIB_DIR.exists():
            try:
                KBUTILLIB_DIR.mkdir(parents=True, exist_ok=True)
                result["directory_created"] = True
                result["message"] = f"Created directory: {KBUTILLIB_DIR}"
            except Exception as e:
                result["message"] = f"Failed to create directory {KBUTILLIB_DIR}: {e}"
                return result
        else:
            result["message"] = f"Directory already exists: {KBUTILLIB_DIR}"

        # Handle config file
        if DEFAULT_CONFIG_FILE.exists() and not force:
            result["success"] = True
            result["message"] += f"\nConfig file already exists: {DEFAULT_CONFIG_FILE}"
            return result

        # Find source config file
        if source_config:
            source_path = Path(source_config)
            if not source_path.exists():
                result["message"] += f"\nSource config not found: {source_config}"
                return result
        else:
            # Search for config.yaml in current directory and parent directories
            source_path = None
            current = Path.cwd()
            for _ in range(5):  # Search up to 5 levels
                candidate = current / "config.yaml"
                if candidate.exists():
                    source_path = candidate
                    break
                current = current.parent

            if source_path is None:
                result["message"] += "\nNo source config.yaml found in project. Skipping config copy."
                result["success"] = True
                return result

        # Copy config file
        try:
            shutil.copy2(source_path, DEFAULT_CONFIG_FILE)
            result["config_copied"] = True
            result["success"] = True
            result["message"] += f"\nCopied config from {source_path} to {DEFAULT_CONFIG_FILE}"
        except Exception as e:
            result["message"] += f"\nFailed to copy config file: {e}"
            return result

        return result
