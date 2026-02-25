"""Dependency manager for KBUtilLib.

Manages external dependency paths using a configuration file (dependencies.yaml).
By default, dependencies are expected to be in sibling directories alongside
the KBUtilLib repository. Custom paths can be configured via dependencies.yaml.
"""

import subprocess
import sys
from pathlib import Path
from typing import Dict, Any, Optional
import yaml

KBUTILLIB_DIR = Path.home() / ".kbutillib"
DEFAULT_DEPENDENCIES_FILE = KBUTILLIB_DIR / "dependencies.yaml"

class DependencyManager:
    """Manages external dependency paths for KBUtilLib.

    Reads configuration from dependencies.yaml and resolves paths to
    external libraries. Libraries are expected to be installed in sibling
    directories by default, with config file overrides for custom locations.
    """

    def __init__(self, config_path: Optional[Path] = None, auto_init: bool = True):
        """Initialize the dependency manager.

        Config file priority (when config_path is None):
        1. ~/.kbutillib/dependencies.yaml (local user config)
        2. Repo root dependencies.yaml (repository default)

        Args:
            config_path: Explicit path to dependencies.yaml. If None, uses priority order.
            auto_init: Automatically resolve dependency paths on creation
        """
        self.repo_root = self._find_repo_root()
        self.repo_dependencies_file = self.repo_root / "dependencies.yaml"

        if config_path is None:
            if DEFAULT_DEPENDENCIES_FILE.exists():
                config_path = DEFAULT_DEPENDENCIES_FILE
            else:
                config_path = self.repo_dependencies_file

        self.config_path = Path(config_path)
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
        return Path(__file__).parent.parent.parent

    def _load_config(self) -> Dict[str, Any]:
        """Load the dependencies configuration file."""
        if not self.config_path.exists():
            return {}

        with open(self.config_path, 'r') as f:
            config = yaml.safe_load(f)

        if not config or 'dependencies' not in config:
            return {}

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
        return (self.repo_root / path).resolve()

    def _clone_dependency(self, dep_name: str, git_url: str, target_path: Path) -> None:
        """Clone a dependency from its git URL.

        Args:
            dep_name: Name of the dependency (for logging)
            git_url: Git repository URL
            target_path: Local path to clone into
        """
        print(f"Cloning {dep_name} from {git_url} to {target_path}...")
        try:
            subprocess.run(
                ["git", "clone", git_url, str(target_path)],
                check=True,
                capture_output=True,
                text=True,
            )
            print(f"Successfully cloned {dep_name}")
        except subprocess.CalledProcessError as e:
            print(f"Error cloning {dep_name}: {e.stderr.strip()}")
        except FileNotFoundError:
            print("Error: git is not installed or not in PATH")

    def initialize_dependencies(self, checkout_if_missing: bool = False) -> None:
        """Resolve all dependency paths from the configuration.

        For each dependency:
        1. Resolve the configured path (absolute or relative to repo root)
        2. If the path doesn't exist and checkout_if_missing is True, clone from git URL
        3. If the path exists, add it to sys.path for imports
        4. Store the resolved path for data access

        Args:
            checkout_if_missing: If True, clone missing dependencies from their git URL
        """
        for dep_name, dep_config in self.config.items():
            dep_path = self._resolve_path(dep_config['path'])

            if not dep_path.exists():
                if checkout_if_missing and 'git' in dep_config:
                    self._clone_dependency(dep_name, dep_config['git'], dep_path)
                else:
                    print(f"Warning: Dependency {dep_name} not found at {dep_path}")
                    continue

            if not dep_path.exists():
                continue

            self.dependency_paths[dep_name] = dep_path

            # Add to Python path if not already present
            dep_path_str = str(dep_path)
            if dep_path_str not in sys.path:
                sys.path.insert(0, dep_path_str)

            # cobrakbase needs its parent directory in the path
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
    """Get the global dependency manager instance."""
    global _dependency_manager
    if _dependency_manager is None:
        _dependency_manager = DependencyManager()
    return _dependency_manager


def get_dependency_path(dep_name: str) -> Optional[Path]:
    """Get the resolved path for a dependency."""
    return get_dependency_manager().get_dependency_path(dep_name)


def get_data_path(dep_name: str, relative_path: str = "") -> Optional[Path]:
    """Get a path to data within a dependency."""
    return get_dependency_manager().get_data_path(dep_name, relative_path)


