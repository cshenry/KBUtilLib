# SharedEnvUtils Module

The `SharedEnvUtils` class manages shared environment configuration, secrets, and runtime settings across the KBUtilLib framework. It provides centralized access to configuration files, environment variables, authentication tokens, and other shared resources.

## Overview

`SharedEnvUtils` extends `BaseUtils` to add comprehensive environment and configuration management capabilities. It handles authentication tokens, configuration files, and environment variables in a secure and standardized way.

## Key Features

- **Configuration Management**: Load and manage configuration from files and environment
- **Token Management**: Secure handling of authentication tokens for various services
- **Environment Variables**: Automatic loading and management of environment settings
- **Multi-service Authentication**: Support for different authentication namespaces
- **Secure Storage**: Safe handling of sensitive credentials and tokens

## Class Definition

```python
class SharedEnvUtils(BaseUtils):
    """Manages shared environment configuration, secrets, and runtime settings.

    Provides centralized access to configuration files, environment variables,
    authentication tokens, and other shared resources across utility modules.
    """
```

## Constructor

```python
def __init__(
    self,
    config_file: Optional[Union[str, Path]] = None,
    token_file: Optional[Union[str, Path]] = Path.home() / ".tokens",
    kbase_token_file: Optional[Union[str, Path]] = Path.home() / ".kbase" / "token",
    token: Optional[Union[str, Dict[str, str]]] = None,
    **kwargs: Any,
) -> None:
    """Initialize the shared environment manager.

    Args:
        config_file: Path to configuration file
        token_file: Path to general token file
        kbase_token_file: Path to KBase-specific token file
        token: Token(s) to set directly (string or dict)
        **kwargs: Additional arguments passed to BaseUtils
    """
```

## Core Methods

### Configuration Management

```python
def read_config(self, config_file: Optional[str] = None) -> Dict[str, Any]:
    """Read configuration from file.

    Args:
        config_file: Path to config file (uses instance default if None)

    Returns:
        Dictionary containing configuration data
    """

def get_config(self, section: str, key: str, default: Any = None) -> Any:
    """Get configuration value.

    Args:
        section: Configuration section name
        key: Configuration key name
        default: Default value if key not found

    Returns:
        Configuration value or default
    """

def set_config(self, section: str, key: str, value: Any) -> None:
    """Set configuration value.

    Args:
        section: Configuration section name
        key: Configuration key name
        value: Value to set
    """
```

### Token Management

```python
def read_token_file(self, token_file: Optional[str] = None) -> Dict[str, str]:
    """Read tokens from file.

    Args:
        token_file: Path to token file

    Returns:
        Dictionary of namespace -> token mappings
    """

def save_token_file(self, token_file: Optional[str] = None) -> None:
    """Save current tokens to file.

    Args:
        token_file: Path to save tokens to
    """

def get_token(self, namespace: str = "default") -> Optional[str]:
    """Get authentication token for namespace.

    Args:
        namespace: Token namespace (e.g., "kbase", "default")

    Returns:
        Authentication token or None if not found
    """

def set_token(self, token: str, namespace: str = "default") -> None:
    """Set authentication token for namespace.

    Args:
        token: Authentication token
        namespace: Token namespace
    """
```

### Environment Variables

```python
def load_environment_variables(self) -> None:
    """Load environment variables into internal storage."""

def get_environment_variable(self, key: str, default: Any = None) -> Any:
    """Get environment variable value.

    Args:
        key: Environment variable name
        default: Default value if not found

    Returns:
        Environment variable value or default
    """
```

## Usage Examples

### Basic Configuration

```python
from kbutillib.shared_env_utils import SharedEnvUtils

# Initialize with config file
env = SharedEnvUtils(config_file="config.yaml")

# Get configuration values
database_url = env.get_config("database", "url", "localhost")
api_key = env.get_config("api", "key")
```

### Token Management

```python
# Set tokens for different services
env.set_token("your-kbase-token", "kbase")
env.set_token("your-api-token", "external_api")

# Retrieve tokens
kbase_token = env.get_token("kbase")
api_token = env.get_token("external_api")

# Save tokens to file for persistence
env.save_token_file()
```

### Environment Variables

```python
# Environment variables are automatically loaded
debug_mode = env.get_environment_variable("DEBUG", False)
temp_dir = env.get_environment_variable("TEMP_DIR", "/tmp")
```

## Configuration File Format

Supports standard INI format configuration files:

```ini
[database]
url = postgres://localhost:5432/mydb
username = user

[api]
base_url = https://api.example.com
timeout = 30

[logging]
level = INFO
file = /var/log/app.log
```

## Token File Format

Tokens are stored in JSON format:

```json
{
  "kbase": "your-kbase-authentication-token",
  "external_api": "your-external-api-token",
  "default": "default-token"
}
```

## Security Considerations

- Token files are created with restricted permissions (600)
- Sensitive information is not logged
- Environment variables take precedence over config files
- Supports secure token storage in user home directory

## Common Use Cases

1. **Service Authentication**: Managing tokens for KBase and external APIs
2. **Configuration Management**: Centralized config for database connections, API endpoints
3. **Environment-specific Settings**: Different configs for dev/staging/production
4. **Credential Storage**: Secure handling of API keys and authentication tokens

## Dependencies

- Python standard library: `os`, `pathlib`, `configparser`
- Inherits from: `BaseUtils`

## Notes

- Configuration and token files are optional - the class works with environment variables alone
- Token namespaces allow multiple authentication contexts in the same application
- Thread-safe for read operations, write operations should be synchronized in multi-threaded environments
