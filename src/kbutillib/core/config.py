"""Pydantic v2 config schema + loader for KBUtilLib.

Validates the well-known keys in ``config.yaml`` without rejecting unknown
keys.  Unknown / extra keys are silently ignored so the schema never rejects
a valid current config file.

Public API
----------
``load_config(path=None) -> Config``
    Read and validate ``config.yaml``.  ``path`` defaults to the
    ``config.yaml`` at the repository root (discovered relative to this
    file).  Returns a :class:`Config` instance.

``Config.get(dotted_key, default=None)``
    Convenience accessor using dot-notation (e.g.
    ``cfg.get("logging.level", "INFO")``).

Schema hierarchy
----------------
:class:`Config`
    :class:`KBaseConfig` (``kbase.*``)
    :class:`UrlsConfig` (``urls.*``)
    :class:`SdkConfig` (``sdk.*``)
    :class:`MsConfig` (``ms.*``)
    :class:`NotebookConfig` (``notebook.*``)
    :class:`LoggingConfig` (``logging.*``)
    :class:`ModelingConfig` (``modeling.*``)
    :class:`PathsConfig` (``paths.*``)
    :class:`SkaniConfig` (``skani.*``)
    :class:`AICurationConfig` (``ai_curation.*``)
    :class:`TransytConfig` (``transyt.*``)
    Plus scalar keys: ``workspace_url``, ``max_retry``, ``scratch``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, field_validator

__all__ = [
    "Config",
    "KBaseConfig",
    "UrlsConfig",
    "SdkConfig",
    "MsConfig",
    "NotebookConfig",
    "LoggingConfig",
    "ModelingConfig",
    "PathsConfig",
    "SkaniConfig",
    "AICurationConfig",
    "TransytConfig",
    "load_config",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REPO_CONFIG = Path(__file__).parent.parent.parent.parent / "config.yaml"
"""Default config path: <repo_root>/config.yaml."""


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load *path* as YAML using PyYAML (stdlib-friendly fallback to json)."""
    try:
        import yaml  # type: ignore[import-untyped]

        with path.open() as fh:
            data = yaml.safe_load(fh)
        return data if isinstance(data, dict) else {}
    except ImportError:
        # Minimal fallback: parse YAML-free JSON-style configs
        import json

        with path.open() as fh:
            return json.load(fh)


# ---------------------------------------------------------------------------
# Nested sub-models (all extra="allow" so unknown keys are silently forwarded)
# ---------------------------------------------------------------------------

_EXTRA_ALLOW = ConfigDict(extra="allow", populate_by_name=True)


class KBaseConfig(BaseModel):
    """Configuration for the ``kbase`` block."""

    model_config = _EXTRA_ALLOW

    url: str = "https://kbase.us/services"
    auth_token: str = ""


class UrlsConfig(BaseModel):
    """Configuration for the ``urls`` block."""

    model_config = _EXTRA_ALLOW

    kbase_api: str = "https://kbase.us/services"
    narrative: str = "https://narrative.kbase.us"
    search: str = "https://search.kbase.us"


class SdkConfig(BaseModel):
    """Configuration for the ``sdk`` block."""

    model_config = _EXTRA_ALLOW

    module_dir: str = "/kb/module"
    callback_url: str = ""


class MsConfig(BaseModel):
    """Configuration for the ``ms`` (mass spectrometry) block."""

    model_config = _EXTRA_ALLOW

    default_ppm_tolerance: float = 10.0
    default_da_tolerance: float = 0.01


class NotebookConfig(BaseModel):
    """Configuration for the ``notebook`` block."""

    model_config = _EXTRA_ALLOW

    auto_display: bool = True
    max_display_rows: int = 100
    max_display_cols: int = 20


class LoggingConfig(BaseModel):
    """Configuration for the ``logging`` block."""

    model_config = _EXTRA_ALLOW

    level: str = "INFO"
    format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

    @field_validator("level")
    @classmethod
    def _validate_level(cls, v: str) -> str:
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        up = v.upper()
        if up not in valid:
            logger.warning("Unknown logging level %r; keeping as-is.", v)
        return v


class ModelingConfig(BaseModel):
    """Configuration for the ``modeling`` block."""

    model_config = _EXTRA_ALLOW

    default_objective: str = "bio1"
    fba_timeout: int = 300


class PathsConfig(BaseModel):
    """Configuration for the ``paths`` block."""

    model_config = _EXTRA_ALLOW

    data_dir: str = "./data"
    output_dir: str = "./output"
    cache_dir: str = "./cache"


class SkaniConfig(BaseModel):
    """Configuration for the ``skani`` block."""

    model_config = _EXTRA_ALLOW

    executable: str = "skani"
    cache_file: str = "~/.kbutillib/skani_databases.json"


class AICurationConfig(BaseModel):
    """Configuration for the ``ai_curation`` block."""

    model_config = _EXTRA_ALLOW

    backend: str = "argo"
    claude_code_executable: str = "claude-code"


class TransytConfig(BaseModel):
    """Configuration for the ``transyt`` block."""

    model_config = _EXTRA_ALLOW

    docker_image: str = "merlin-sysbio/kb_transyt:latest"
    neo4j_timeout: int = 120


# ---------------------------------------------------------------------------
# Top-level Config
# ---------------------------------------------------------------------------


class Config(BaseModel):
    """Top-level KBUtilLib configuration model.

    Maps to the sections of ``config.yaml``.  Extra keys at any level are
    silently forwarded so the schema never rejects a valid current config.

    Usage::

        cfg = load_config()
        cfg.logging.level
        cfg.get("logging.level", "INFO")
        cfg.get("skani.executable", "skani")
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    # KBase / auth
    kbase: KBaseConfig = KBaseConfig()
    urls: UrlsConfig = UrlsConfig()
    sdk: SdkConfig = SdkConfig()

    # Workspace (top-level scalar in YAML; key has a hyphen so alias needed)
    workspace_url: str = "https://kbase.us/services/ws"
    max_retry: int = 3
    scratch: str = "/tmp"

    # Domain-specific blocks
    ms: MsConfig = MsConfig()
    notebook: NotebookConfig = NotebookConfig()
    logging: LoggingConfig = LoggingConfig()
    modeling: ModelingConfig = ModelingConfig()
    paths: PathsConfig = PathsConfig()
    skani: SkaniConfig = SkaniConfig()
    ai_curation: AICurationConfig = AICurationConfig()
    transyt: TransytConfig = TransytConfig()

    # ------------------------------------------------------------------
    # Convenience accessor
    # ------------------------------------------------------------------

    def get(self, dotted_key: str, default: Any = None) -> Any:
        """Return the value at *dotted_key* or *default* if missing.

        Supports dot-separated paths like ``"logging.level"`` or simple keys
        like ``"scratch"``.  Works for any depth and tolerates unknown keys.

        Parameters
        ----------
        dotted_key:
            Dot-separated path, e.g. ``"kbase.url"`` or ``"max_retry"``.
        default:
            Value returned when any segment is missing.

        Returns
        -------
        Any
            The config value, or *default*.

        Examples
        --------
        ::

            cfg = load_config()
            cfg.get("logging.level")          # "INFO"
            cfg.get("skani.executable")       # "skani"
            cfg.get("nonexistent.key", 42)    # 42
        """
        parts = dotted_key.split(".")
        current: Any = self
        for part in parts:
            if current is None:
                return default
            if isinstance(current, BaseModel):
                # Try declared model field first, then extra fields
                try:
                    current = getattr(current, part)
                except AttributeError:
                    extra = getattr(current, "model_extra", None) or {}
                    if part in extra:
                        current = extra[part]
                    else:
                        return default
            elif isinstance(current, dict):
                if part not in current:
                    return default
                current = current[part]
            else:
                # Scalar — can't traverse further
                return default
        return current if current is not None else default


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_config(path: Optional[str | Path] = None) -> Config:
    """Read and validate the KBUtilLib config file.

    Parameters
    ----------
    path:
        Path to the YAML config file.  Defaults to ``<repo_root>/config.yaml``
        (auto-discovered relative to this file's location).

    Returns
    -------
    Config
        A validated :class:`Config` instance.

    Raises
    ------
    FileNotFoundError
        If *path* is given explicitly and does not exist.
    pydantic.ValidationError
        If the file fails structural validation (highly unlikely given
        ``extra="allow"`` on all models).
    """
    config_path = Path(path) if path is not None else _REPO_CONFIG

    if not config_path.exists():
        if path is not None:
            raise FileNotFoundError(f"Config file not found: {config_path}")
        # No config file present — return defaults silently
        logger.debug("No config.yaml found at %s; using defaults.", config_path)
        return Config()

    raw: dict[str, Any] = _load_yaml(config_path)

    # The YAML key is "workspace-url" (with a hyphen); map to "workspace_url"
    if "workspace-url" in raw and "workspace_url" not in raw:
        raw["workspace_url"] = raw.pop("workspace-url")

    return Config.model_validate(raw)
