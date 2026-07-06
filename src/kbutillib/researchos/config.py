"""researchos.config — resolve and persist Research-OS configuration.

Configuration is stored in kbu's own config (~/.kbutillib/config.yaml) under
a ``researchos`` section.

Resolution order (each path resolved independently):

researchos_root
  1. explicit argument
  2. env var ``RESEARCHOS_ROOT``
  3. config ``researchos.root``
  4. default ``~/Dropbox/Projects/ResearchOS``

tooling_venv
  1. explicit argument
  2. env var ``RESEARCHOS_TOOLING_VENV``
  3. config ``researchos.tooling_venv``
  4. default ``~/.venvs/research-os``

aiassistant_root
  1. explicit argument
  2. env var ``AIASSISTANT_ROOT``
  3. config ``researchos.aiassistant_root``
  4. default ``~/Dropbox/Projects/AIAssistant``
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml

# The standard kbu config file path.
_KBUTILLIB_DIR = Path.home() / ".kbutillib"
_DEFAULT_CONFIG_FILE = _KBUTILLIB_DIR / "config.yaml"

# Environment variable names.
_ENV_RESEARCHOS_ROOT = "RESEARCHOS_ROOT"
_ENV_TOOLING_VENV = "RESEARCHOS_TOOLING_VENV"
_ENV_AIASSISTANT_ROOT = "AIASSISTANT_ROOT"

# Defaults
_DEFAULT_ROOT = Path.home() / "Dropbox" / "Projects" / "ResearchOS"
_DEFAULT_TOOLING_VENV = Path.home() / ".venvs" / "research-os"
_DEFAULT_AIASSISTANT_ROOT = Path.home() / "Dropbox" / "Projects" / "AIAssistant"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _read_kbu_config(config_file: Optional[Path] = None) -> dict:
    """Read ~/.kbutillib/config.yaml and return its contents as a dict.

    Returns an empty dict when the file does not exist.
    """
    path = config_file or _DEFAULT_CONFIG_FILE
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data


def _write_kbu_config(data: dict, config_file: Optional[Path] = None) -> None:
    """Write *data* to ~/.kbutillib/config.yaml, creating the directory if needed."""
    path = config_file or _DEFAULT_CONFIG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        yaml.dump(data, fh, default_flow_style=False, sort_keys=False)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def resolve_researchos_root(
    explicit: Optional[str | Path] = None,
    *,
    config_file: Optional[Path] = None,
) -> Path:
    """Resolve the Research-OS root directory with documented precedence.

    Precedence: explicit arg > env RESEARCHOS_ROOT > config researchos.root
    > default ~/Dropbox/Projects/ResearchOS.

    Args:
        explicit: An explicit path string or Path object (e.g. from a CLI flag).
        config_file: Override path to the kbu config file (used in tests).

    Returns:
        Resolved absolute Path to the Research-OS root.
    """
    # 1. Explicit argument
    if explicit is not None:
        return Path(explicit).expanduser().resolve()

    # 2. Environment variable
    env_val = os.environ.get(_ENV_RESEARCHOS_ROOT)
    if env_val:
        return Path(env_val).expanduser().resolve()

    # 3. Config file
    cfg = _read_kbu_config(config_file)
    ros_section = cfg.get("researchos", {}) or {}
    cfg_val = ros_section.get("root")
    if cfg_val:
        return Path(cfg_val).expanduser().resolve()

    # 4. Default
    return _DEFAULT_ROOT.resolve()


def resolve_tooling_venv(
    explicit: Optional[str | Path] = None,
    *,
    config_file: Optional[Path] = None,
) -> Path:
    """Resolve the Research-OS tooling venv path with documented precedence.

    Precedence: explicit arg > env RESEARCHOS_TOOLING_VENV >
    config researchos.tooling_venv > default ~/.venvs/research-os.

    Args:
        explicit: An explicit path string or Path object (e.g. from a CLI flag).
        config_file: Override path to the kbu config file (used in tests).

    Returns:
        Resolved absolute Path to the tooling venv.
    """
    # 1. Explicit argument
    if explicit is not None:
        return Path(explicit).expanduser().resolve()

    # 2. Environment variable
    env_val = os.environ.get(_ENV_TOOLING_VENV)
    if env_val:
        return Path(env_val).expanduser().resolve()

    # 3. Config file
    cfg = _read_kbu_config(config_file)
    ros_section = cfg.get("researchos", {}) or {}
    cfg_val = ros_section.get("tooling_venv")
    if cfg_val:
        return Path(cfg_val).expanduser().resolve()

    # 4. Default
    return _DEFAULT_TOOLING_VENV.resolve()


def resolve_aiassistant_root(
    explicit: Optional[str | Path] = None,
    *,
    config_file: Optional[Path] = None,
) -> Path:
    """Resolve the AIAssistant root directory with documented precedence.

    Precedence: explicit arg > env AIASSISTANT_ROOT >
    config researchos.aiassistant_root > default ~/Dropbox/Projects/AIAssistant.

    Args:
        explicit: An explicit path string or Path object (e.g. from a CLI flag).
        config_file: Override path to the kbu config file (used in tests).

    Returns:
        Resolved absolute Path to the AIAssistant root.
    """
    # 1. Explicit argument
    if explicit is not None:
        return Path(explicit).expanduser().resolve()

    # 2. Environment variable
    env_val = os.environ.get(_ENV_AIASSISTANT_ROOT)
    if env_val:
        return Path(env_val).expanduser().resolve()

    # 3. Config file
    cfg = _read_kbu_config(config_file)
    ros_section = cfg.get("researchos", {}) or {}
    cfg_val = ros_section.get("aiassistant_root")
    if cfg_val:
        return Path(cfg_val).expanduser().resolve()

    # 4. Default
    return _DEFAULT_AIASSISTANT_ROOT.resolve()


def set_root(
    root: Optional[str | Path] = None,
    tooling_venv: Optional[str | Path] = None,
    aiassistant_root: Optional[str | Path] = None,
    *,
    config_file: Optional[Path] = None,
) -> None:
    """Persist researchos config to ~/.kbutillib/config.yaml.

    Expands ``~``, resolves to absolute paths, and creates the config file and
    its parent directory if missing before writing. Preserves other config
    sections.

    Args:
        root: Path to the Research-OS projects root directory.
        tooling_venv: Path to the shared Research-OS tooling venv.
        aiassistant_root: Path to the AIAssistant repository root.
        config_file: Override path to the kbu config file (used in tests).

    Raises:
        ValueError: If no argument is provided.
    """
    if root is None and tooling_venv is None and aiassistant_root is None:
        raise ValueError(
            "At least one of root, tooling_venv, or aiassistant_root must be provided."
        )

    cfg = _read_kbu_config(config_file)
    ros_section: dict = cfg.setdefault("researchos", {}) or {}
    cfg["researchos"] = ros_section

    if root is not None:
        ros_section["root"] = str(Path(root).expanduser().resolve())

    if tooling_venv is not None:
        ros_section["tooling_venv"] = str(Path(tooling_venv).expanduser().resolve())

    if aiassistant_root is not None:
        ros_section["aiassistant_root"] = str(
            Path(aiassistant_root).expanduser().resolve()
        )

    _write_kbu_config(cfg, config_file)
