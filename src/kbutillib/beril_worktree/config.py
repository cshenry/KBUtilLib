"""beril_worktree.config — resolve and persist BERIL worktree configuration.

Configuration is stored in kbu's own config (~/.kbutillib/config.yaml) under
a ``beril`` section.  It is never written to BERIL's own config.toml.

Resolution order (each path resolved independently):

beril_root
  1. explicit ``beril_root`` argument
  2. env var ``BERIL_ROOT``
  3. config ``beril.root``
  4. (none) — raises ValueError with guidance

worktree_root
  1. explicit ``worktree_root`` argument
  2. env var ``WORKING_BERIL_DIRECTORY``
  3. config ``beril.worktree_root``
  4. default ``<beril_root>/../WorkingBERIL``
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
_ENV_BERIL_ROOT = "BERIL_ROOT"
_ENV_WORKTREE_ROOT = "WORKING_BERIL_DIRECTORY"


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


def resolve_beril_root(
    explicit: Optional[str | Path] = None,
    *,
    config_file: Optional[Path] = None,
) -> Path:
    """Resolve the BERIL repository root with documented precedence.

    Precedence: explicit arg > env BERIL_ROOT > config beril.root.
    Raises ValueError if unresolved (no silent default).

    Args:
        explicit: An explicit path string or Path object (e.g. from a CLI flag).
        config_file: Override path to the kbu config file (used in tests).

    Returns:
        Resolved absolute Path to the BERIL root.

    Raises:
        ValueError: When no source provides a value.
    """
    # 1. Explicit argument
    if explicit is not None:
        return Path(explicit).expanduser().resolve()

    # 2. Environment variable
    env_val = os.environ.get(_ENV_BERIL_ROOT)
    if env_val:
        return Path(env_val).expanduser().resolve()

    # 3. Config file
    cfg = _read_kbu_config(config_file)
    beril_section = cfg.get("beril", {}) or {}
    cfg_val = beril_section.get("root")
    if cfg_val:
        return Path(cfg_val).expanduser().resolve()

    raise ValueError(
        "BERIL root is not configured.\n"
        "Set it with one of:\n"
        "  kbu beril worktree set-root --beril-root <PATH>\n"
        f"  export {_ENV_BERIL_ROOT}=<PATH>\n"
        "  Add 'beril.root' to ~/.kbutillib/config.yaml"
    )


def resolve_worktree_root(
    explicit: Optional[str | Path] = None,
    *,
    beril_root: Optional[Path] = None,
    config_file: Optional[Path] = None,
) -> Path:
    """Resolve the worktree root with documented precedence.

    Precedence: explicit arg > env WORKING_BERIL_DIRECTORY > config
    beril.worktree_root > default <beril_root>/../WorkingBERIL.

    The default requires *beril_root* to be supplied (or already resolved via
    :func:`resolve_beril_root`).

    Args:
        explicit: An explicit path string or Path object (e.g. from a CLI flag).
        beril_root: The resolved BERIL root (used only when falling back to
            the default ``<beril_root>/../WorkingBERIL``).
        config_file: Override path to the kbu config file (used in tests).

    Returns:
        Resolved absolute Path to the worktree root.
    """
    # 1. Explicit argument
    if explicit is not None:
        return Path(explicit).expanduser().resolve()

    # 2. Environment variable
    env_val = os.environ.get(_ENV_WORKTREE_ROOT)
    if env_val:
        return Path(env_val).expanduser().resolve()

    # 3. Config file
    cfg = _read_kbu_config(config_file)
    beril_section = cfg.get("beril", {}) or {}
    cfg_val = beril_section.get("worktree_root")
    if cfg_val:
        return Path(cfg_val).expanduser().resolve()

    # 4. Default: sibling of beril_root
    if beril_root is not None:
        return (beril_root / ".." / "WorkingBERIL").resolve()

    # Compute beril_root from the config/env so we can use the default.
    # We do not error here — resolve_beril_root() will error if needed.
    try:
        computed_root = resolve_beril_root(config_file=config_file)
    except ValueError:
        # If beril_root is also unknown, we can't compute the default.
        # The caller will hit the error when it resolves beril_root separately.
        return Path.home() / "WorkingBERIL"
    return (computed_root / ".." / "WorkingBERIL").resolve()


def set_root(
    beril_root: Optional[str | Path] = None,
    worktree_root: Optional[str | Path] = None,
    *,
    config_file: Optional[Path] = None,
) -> None:
    """Persist beril.root and/or beril.worktree_root to ~/.kbutillib/config.yaml.

    Expands ``~``, resolves to absolute paths, and creates the config file and
    its parent directory if missing before writing.  Never writes to BERIL's
    own ``~/.config/beril/config.toml``.

    Args:
        beril_root: Path to the primary BERIL repository checkout.
        worktree_root: Path under which per-project worktrees will be created.
        config_file: Override path to the kbu config file (used in tests).

    Raises:
        ValueError: If neither argument is provided.
    """
    if beril_root is None and worktree_root is None:
        raise ValueError("At least one of beril_root or worktree_root must be provided.")

    cfg = _read_kbu_config(config_file)
    beril_section: dict = cfg.setdefault("beril", {}) or {}
    cfg["beril"] = beril_section

    if beril_root is not None:
        beril_section["root"] = str(Path(beril_root).expanduser().resolve())

    if worktree_root is not None:
        beril_section["worktree_root"] = str(
            Path(worktree_root).expanduser().resolve()
        )

    _write_kbu_config(cfg, config_file)
