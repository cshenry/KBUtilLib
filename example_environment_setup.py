#!/usr/bin/env python
"""Example script demonstrating environment initialization and configuration.

This script shows how to use the new SharedEnvUtils features for managing
the ~/.kbutillib directory and configuration files.
"""

import sys
import os

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from kbutillib import SharedEnvUtils
from pathlib import Path


def main():
    """Demonstrate environment initialization and configuration."""

    print("=" * 80)
    print("KBUtilLib Environment Initialization & Configuration")
    print("=" * 80)
    print()

    print("CONFIGURATION PRIORITY ORDER:")
    print("-" * 80)
    print("1. Explicitly provided config_file parameter")
    print("2. ~/.kbutillib/config.yaml (user config)")
    print("3. Project root config.yaml (repository default)")
    print()

    print("=" * 80)
    print()
    print("1. INITIALIZE ENVIRONMENT:")
    print("-" * 80)
    print()
    print("Initialize ~/.kbutillib directory and copy default config:")
    print("""
    from kbutillib import SharedEnvUtils

    # Initialize environment (creates ~/.kbutillib/ and copies config)
    result = SharedEnvUtils.initialize_environment()

    print(f"Success: {result['success']}")
    print(f"Directory created: {result['directory_created']}")
    print(f"Config copied: {result['config_copied']}")
    print(f"Config path: {result['config_path']}")
    print(f"Message: {result['message']}")
    """)

    print("Example output:")
    print("-" * 40)
    # Demonstrate initialization
    result = SharedEnvUtils.initialize_environment()
    print(f"Success: {result['success']}")
    print(f"Directory created: {result['directory_created']}")
    print(f"Config copied: {result['config_copied']}")
    print(f"Config path: {result['config_path']}")
    print(f"Message: {result['message']}")
    print()

    print("=" * 80)
    print()
    print("2. LOAD CONFIGURATION:")
    print("-" * 80)
    print()
    print("Configuration is automatically loaded with priority:")
    print("""
    from kbutillib import SharedEnvUtils

    # Loads config from priority order (see above)
    util = SharedEnvUtils()

    # Check which config was loaded
    env = util.export_environment()
    print(f"Config file: {env['config_file']}")
    """)

    print("Example:")
    print("-" * 40)
    util = SharedEnvUtils()
    env = util.export_environment()
    print(f"Config file: {env['config_file']}")
    print()

    print("=" * 80)
    print()
    print("3. ACCESS CONFIG VALUES (DOT NOTATION):")
    print("-" * 80)
    print()
    print("Use get_config_value() with dot notation:")
    print("""
    from kbutillib import SharedEnvUtils

    util = SharedEnvUtils()

    # Access nested config values with dot notation
    skani_exec = util.get_config_value("skani.executable", default="skani")
    cache_file = util.get_config_value("skani.cache_file")
    data_dir = util.get_config_value("paths.data_dir", default="./data")

    print(f"SKANI executable: {skani_exec}")
    print(f"Cache file: {cache_file}")
    print(f"Data directory: {data_dir}")
    """)

    print("Example:")
    print("-" * 40)
    if env['config_file']:
        skani_exec = util.get_config_value("skani.executable", default="skani")
        cache_file = util.get_config_value("skani.cache_file")
        data_dir = util.get_config_value("paths.data_dir", default="./data")

        print(f"SKANI executable: {skani_exec}")
        print(f"Cache file: {cache_file}")
        print(f"Data directory: {data_dir}")
    else:
        print("No config file loaded - would use defaults")
    print()

    print("=" * 80)
    print()
    print("4. FORCE REINITIALIZE:")
    print("-" * 80)
    print()
    print("Overwrite existing config with project default:")
    print("""
    # Force copy of config even if it already exists
    result = SharedEnvUtils.initialize_environment(force=True)
    print(result['message'])
    """)
    print()

    print("=" * 80)
    print()
    print("5. INITIALIZE WITH CUSTOM CONFIG:")
    print("-" * 80)
    print()
    print("Copy a specific config file to ~/.kbutillib/:")
    print("""
    # Initialize with custom config file
    result = SharedEnvUtils.initialize_environment(
        source_config="/path/to/custom/config.yaml"
    )
    print(result['message'])
    """)
    print()

    print("=" * 80)
    print()
    print("DIRECTORY STRUCTURE:")
    print("-" * 80)
    print("""
~/.kbutillib/
├── config.yaml              # User configuration (highest priority)
├── skani_databases.json     # SKANI sketch database registry
└── skani_sketches/          # Default location for sketch databases
    ├── database1/
    │   └── sketch_db
    └── database2/
        └── sketch_db
    """)

    print("=" * 80)
    print()
    print("KEY BENEFITS:")
    print("-" * 80)
    print("- Centralized user configuration in ~/.kbutillib/")
    print("- Config priority: user > project > defaults")
    print("- Easy environment initialization")
    print("- YAML config support with dot notation access")
    print("- Backwards compatible with INI format")
    print("- Automatic config discovery in project hierarchy")
    print()

    print("=" * 80)
    print()
    print("MIGRATION GUIDE:")
    print("-" * 80)
    print("""
# One-time setup for existing users:
1. Run initialize_environment() to create ~/.kbutillib/
2. Customize ~/.kbutillib/config.yaml with your settings
3. All utilities will automatically use this config

# For new users:
- First run automatically creates ~/.kbutillib/ with default config
- Customize as needed
    """)

    print("=" * 80)


if __name__ == '__main__':
    main()
