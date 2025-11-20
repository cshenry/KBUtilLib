"""Dependency manager for KBUtilLib.

Manages external dependencies using git submodules and a configuration file.
Automatically initializes submodules and configures Python paths.
"""

import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, Any, Optional
import yaml


class DependencyManager:
    """Manages external dependencies for KBUtilLib.

    Reads configuration from dependencies.yaml and ensures all dependencies
    are available, either via git submodules or custom paths.
    """

    def __init__(self, config_path: Optional[Path] = None, auto_init: bool = True):
        """Initialize the dependency manager.

        Args:
            config_path: Path to dependencies.yaml (defaults to repo root)
            auto_init: Automatically initialize dependencies on creation
        """
        if config_path is None:
            # Default to dependencies.yaml in the repository root
            config_path = Path(__file__).parent.parent.parent / "dependencies.yaml"

        self.config_path = Path(config_path)
        self.repo_root = self._find_repo_root()
        self.config = self._load_config()
        self.dependency_paths: Dict[str, Path] = {}

        if auto_init:
            self.initialize_dependencies()

    def _find_repo_root(self) -> Path:
        """Find the git repository root directory."""
        current = Path(__file__).parent
        while current != current.parent:
            if (current / ".git").exists():
                return current
            current = current.parent
        # If no .git found, assume the project root is 3 levels up from this file
        return Path(__file__).parent.parent.parent

    def _load_config(self) -> Dict[str, Any]:
        """Load the dependencies configuration file."""
        if not self.config_path.exists():
            raise FileNotFoundError(
                f"Dependencies configuration file not found: {self.config_path}"
            )

        with open(self.config_path, 'r') as f:
            config = yaml.safe_load(f)

        if not config or 'dependencies' not in config:
            raise ValueError(
                f"Invalid configuration file: {self.config_path}. "
                "Must contain a 'dependencies' section."
            )

        return config['dependencies']

    def _resolve_path(self, path_str: str) -> Path:
        """Resolve a path from the config (handles relative and absolute paths).

        Args:
            path_str: Path string from config (can be relative or absolute)

        Returns:
            Absolute Path object
        """
        path = Path(path_str)
        if path.is_absolute():
            return path
        else:
            # Relative paths are relative to repo root
            return (self.repo_root / path).resolve()

    def _is_submodule_path(self, dep_name: str, dep_config: Dict[str, Any]) -> bool:
        """Check if a dependency path is in the default submodules directory.

        Args:
            dep_name: Name of the dependency
            dep_config: Configuration dictionary for the dependency

        Returns:
            True if the path is in the dependencies/ directory
        """
        dep_path = self._resolve_path(dep_config['path'])
        submodules_dir = self.repo_root / "dependencies"
        try:
            dep_path.relative_to(submodules_dir)
            return True
        except ValueError:
            return False

    def _init_submodule(self, dep_name: str, dep_config: Dict[str, Any]) -> bool:
        """Initialize a git submodule for a dependency.

        Args:
            dep_name: Name of the dependency
            dep_config: Configuration dictionary for the dependency

        Returns:
            True if successful, False otherwise
        """
        dep_path = self._resolve_path(dep_config['path'])
        git_url = dep_config['git_url']
        branch = dep_config.get('branch', 'main')
        commit = dep_config.get('commit')

        # Check if .gitmodules exists and already has this submodule
        gitmodules_path = self.repo_root / ".gitmodules"
        submodule_exists_in_config = False

        if gitmodules_path.exists():
            with open(gitmodules_path, 'r') as f:
                content = f.read()
                if f'path = {dep_config["path"]}' in content:
                    submodule_exists_in_config = True

        try:
            if not submodule_exists_in_config:
                # Add the submodule
                result = subprocess.run(
                    ["git", "submodule", "add", "-b", branch, git_url, dep_config['path']],
                    cwd=self.repo_root,
                    capture_output=True,
                    text=True,
                    timeout=120
                )

                if result.returncode != 0:
                    # Check if it's already added
                    if "already exists in the index" not in result.stderr:
                        print(f"Warning: Failed to add submodule {dep_name}: {result.stderr}")
                        return False

            # Initialize and update the submodule
            subprocess.run(
                ["git", "submodule", "update", "--init", "--recursive", dep_config['path']],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=120
            )

            # If a specific commit is requested, checkout that commit
            if commit:
                subprocess.run(
                    ["git", "checkout", commit],
                    cwd=dep_path,
                    capture_output=True,
                    text=True,
                    timeout=30
                )

            return True

        except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as e:
            print(f"Warning: Error initializing submodule {dep_name}: {e}")
            return False

    def initialize_dependencies(self) -> None:
        """Initialize all dependencies from the configuration.

        For each dependency:
        1. Resolve the path (absolute or relative)
        2. If path doesn't exist and is in submodules dir, init submodule
        3. Add the path to sys.path for imports
        4. Store the resolved path for data access
        """
        for dep_name, dep_config in self.config.items():
            dep_path = self._resolve_path(dep_config['path'])

            # Check if dependency exists
            if not dep_path.exists():
                # If it's in the submodules directory, try to initialize it
                if self._is_submodule_path(dep_name, dep_config):
                    print(f"Initializing submodule for {dep_name}...")
                    if not self._init_submodule(dep_name, dep_config):
                        print(f"Warning: Failed to initialize {dep_name}, skipping")
                        continue
                else:
                    print(f"Warning: Dependency {dep_name} not found at {dep_path}")
                    continue

            # Store the resolved path
            self.dependency_paths[dep_name] = dep_path

            # Add to Python path if not already present
            dep_path_str = str(dep_path)
            if dep_path_str not in sys.path:
                sys.path.insert(0, dep_path_str)

            # For some dependencies, we need to add the parent directory
            # (e.g., cobrakbase needs its parent in the path)
            if dep_name == 'cobrakbase':
                parent_path = str(dep_path.parent)
                if parent_path not in sys.path:
                    sys.path.insert(0, parent_path)

    def get_dependency_path(self, dep_name: str) -> Optional[Path]:
        """Get the resolved path for a dependency.

        Args:
            dep_name: Name of the dependency

        Returns:
            Path object or None if dependency not found
        """
        return self.dependency_paths.get(dep_name)

    def get_data_path(self, dep_name: str, relative_path: str = "") -> Optional[Path]:
        """Get a path to data within a dependency.

        Args:
            dep_name: Name of the dependency
            relative_path: Relative path within the dependency

        Returns:
            Path object or None if dependency not found
        """
        dep_path = self.get_dependency_path(dep_name)
        if dep_path is None:
            return None

        if relative_path:
            return dep_path / relative_path
        return dep_path


# Global dependency manager instance
_dependency_manager: Optional[DependencyManager] = None


def get_dependency_manager() -> DependencyManager:
    """Get the global dependency manager instance.

    Returns:
        DependencyManager instance
    """
    global _dependency_manager
    if _dependency_manager is None:
        _dependency_manager = DependencyManager()
    return _dependency_manager


def get_dependency_path(dep_name: str) -> Optional[Path]:
    """Get the resolved path for a dependency.

    Args:
        dep_name: Name of the dependency

    Returns:
        Path object or None if dependency not found
    """
    return get_dependency_manager().get_dependency_path(dep_name)


def get_data_path(dep_name: str, relative_path: str = "") -> Optional[Path]:
    """Get a path to data within a dependency.

    Args:
        dep_name: Name of the dependency
        relative_path: Relative path within the dependency (optional)

    Returns:
        Path object or None if dependency not found
    """
    return get_dependency_manager().get_data_path(dep_name, relative_path)
