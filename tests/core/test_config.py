"""Unit tests for kbutillib.core.config — Config schema + load_config().

All tests are offline — no network, no optional backends.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from kbutillib.core.config import (
    AICurationConfig,
    Config,
    KBaseConfig,
    LoggingConfig,
    ModelingConfig,
    MsConfig,
    NotebookConfig,
    PathsConfig,
    SkaniConfig,
    TransytConfig,
    load_config,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MINIMAL_YAML = """\
kbase:
  url: "https://kbase.us/services"
  auth_token: ""
workspace-url: "https://kbase.us/services/ws"
max_retry: 3
scratch: "/tmp"
"""

_FULL_YAML = """\
kbase:
  url: "https://kbase.us/services"
  auth_token: ""
urls:
  kbase_api: "https://kbase.us/services"
  narrative: "https://narrative.kbase.us"
  search: "https://search.kbase.us"
sdk:
  module_dir: "/kb/module"
  callback_url: ""
workspace-url: "https://kbase.us/services/ws"
max_retry: 3
scratch: "/tmp"
ms:
  default_ppm_tolerance: 10.0
  default_da_tolerance: 0.01
notebook:
  auto_display: true
  max_display_rows: 100
  max_display_cols: 20
logging:
  level: "INFO"
  format: "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
modeling:
  default_objective: "bio1"
  fba_timeout: 300
paths:
  data_dir: "./data"
  output_dir: "./output"
  cache_dir: "./cache"
skani:
  executable: "skani"
  cache_file: "~/.kbutillib/skani_databases.json"
ai_curation:
  backend: "argo"
  claude_code_executable: "claude-code"
transyt:
  docker_image: "merlin-sysbio/kb_transyt:latest"
  neo4j_timeout: 120
"""

_YAML_WITH_EXTRAS = """\
kbase:
  url: "https://kbase.us/services"
  auth_token: ""
  future_field: "some_value"
unknown_top_level_key: 42
workspace-url: "https://kbase.us/services/ws"
max_retry: 3
scratch: "/tmp"
"""


def _write_yaml(content: str) -> Path:
    """Write *content* to a temp file and return the path."""
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, encoding="utf-8"
    )
    tmp.write(content)
    tmp.flush()
    tmp.close()
    return Path(tmp.name)


# ---------------------------------------------------------------------------
# Tests: load_config() with the REAL repo config.yaml
# ---------------------------------------------------------------------------


class TestLoadRealConfig:
    """load_config() must validate the real repo config.yaml without error."""

    def test_load_real_config_no_error(self) -> None:
        cfg = load_config()
        assert isinstance(cfg, Config)

    def test_real_config_kbase_url(self) -> None:
        cfg = load_config()
        assert "kbase.us" in cfg.kbase.url

    def test_real_config_logging_level(self) -> None:
        cfg = load_config()
        assert cfg.logging.level in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

    def test_real_config_max_retry_is_int(self) -> None:
        cfg = load_config()
        assert isinstance(cfg.max_retry, int)
        assert cfg.max_retry >= 0

    def test_real_config_skani_executable(self) -> None:
        cfg = load_config()
        assert isinstance(cfg.skani.executable, str)
        assert len(cfg.skani.executable) > 0

    def test_real_config_transyt_timeout_is_int(self) -> None:
        cfg = load_config()
        assert isinstance(cfg.transyt.neo4j_timeout, int)

    def test_real_config_ms_tolerances(self) -> None:
        cfg = load_config()
        assert isinstance(cfg.ms.default_ppm_tolerance, float)
        assert isinstance(cfg.ms.default_da_tolerance, float)


# ---------------------------------------------------------------------------
# Tests: load_config() with written YAML files
# ---------------------------------------------------------------------------


class TestLoadWrittenConfig:
    def test_minimal_yaml_loads(self) -> None:
        p = _write_yaml(_MINIMAL_YAML)
        cfg = load_config(p)
        assert isinstance(cfg, Config)

    def test_full_yaml_loads(self) -> None:
        p = _write_yaml(_FULL_YAML)
        cfg = load_config(p)
        assert isinstance(cfg, Config)

    def test_full_yaml_values(self) -> None:
        p = _write_yaml(_FULL_YAML)
        cfg = load_config(p)
        assert cfg.kbase.url == "https://kbase.us/services"
        assert cfg.max_retry == 3
        assert cfg.scratch == "/tmp"
        assert cfg.logging.level == "INFO"
        assert cfg.modeling.default_objective == "bio1"
        assert cfg.modeling.fba_timeout == 300
        assert cfg.ms.default_ppm_tolerance == 10.0
        assert cfg.notebook.auto_display is True
        assert cfg.paths.data_dir == "./data"
        assert cfg.skani.executable == "skani"
        assert cfg.ai_curation.backend == "argo"
        assert cfg.transyt.neo4j_timeout == 120

    def test_workspace_url_hyphen_key(self) -> None:
        """The YAML key 'workspace-url' must map to Config.workspace_url."""
        p = _write_yaml(_FULL_YAML)
        cfg = load_config(p)
        assert cfg.workspace_url == "https://kbase.us/services/ws"

    def test_extra_keys_ignored(self) -> None:
        """Unknown top-level and nested keys must not raise."""
        p = _write_yaml(_YAML_WITH_EXTRAS)
        cfg = load_config(p)
        assert isinstance(cfg, Config)

    def test_missing_file_given_path_raises(self) -> None:
        with pytest.raises(FileNotFoundError):
            load_config("/definitely/does/not/exist/config.yaml")

    def test_missing_default_path_returns_defaults(self) -> None:
        """When no file at default path, load_config() returns default Config."""
        # Patch the default path by passing a non-existent path explicitly would
        # raise, so instead test that the defaults are sane on a minimal file.
        p = _write_yaml("")  # empty YAML → empty dict → all defaults
        cfg = load_config(p)
        assert isinstance(cfg, Config)
        assert cfg.logging.level == "INFO"  # default

    def test_empty_yaml_uses_defaults(self) -> None:
        p = _write_yaml("")
        cfg = load_config(p)
        assert cfg.kbase.url == "https://kbase.us/services"
        assert cfg.max_retry == 3


# ---------------------------------------------------------------------------
# Tests: Config.get() dotted accessor
# ---------------------------------------------------------------------------


class TestConfigGet:
    def _cfg(self) -> Config:
        p = _write_yaml(_FULL_YAML)
        return load_config(p)

    def test_simple_top_level_key(self) -> None:
        cfg = self._cfg()
        assert cfg.get("scratch") == "/tmp"

    def test_dotted_key_two_levels(self) -> None:
        cfg = self._cfg()
        assert cfg.get("logging.level") == "INFO"

    def test_dotted_key_skani_executable(self) -> None:
        cfg = self._cfg()
        assert cfg.get("skani.executable") == "skani"

    def test_dotted_key_kbase_url(self) -> None:
        cfg = self._cfg()
        assert cfg.get("kbase.url") == "https://kbase.us/services"

    def test_dotted_key_modeling_timeout(self) -> None:
        cfg = self._cfg()
        assert cfg.get("modeling.fba_timeout") == 300

    def test_dotted_key_ms_ppm(self) -> None:
        cfg = self._cfg()
        assert cfg.get("ms.default_ppm_tolerance") == 10.0

    def test_missing_key_returns_default(self) -> None:
        cfg = self._cfg()
        assert cfg.get("nonexistent.key", "fallback") == "fallback"

    def test_missing_key_returns_none_by_default(self) -> None:
        cfg = self._cfg()
        assert cfg.get("totally.missing") is None

    def test_missing_nested_key_returns_default(self) -> None:
        cfg = self._cfg()
        assert cfg.get("kbase.nonexistent_field", 99) == 99

    def test_integer_default(self) -> None:
        cfg = self._cfg()
        result = cfg.get("missing", 42)
        assert result == 42

    def test_max_retry_direct(self) -> None:
        cfg = self._cfg()
        assert cfg.get("max_retry") == 3

    def test_transyt_docker_image(self) -> None:
        cfg = self._cfg()
        assert "transyt" in cfg.get("transyt.docker_image", "")


# ---------------------------------------------------------------------------
# Tests: sub-model defaults
# ---------------------------------------------------------------------------


class TestSubModelDefaults:
    """Verify defaults are correct when no YAML is provided."""

    def test_kbase_defaults(self) -> None:
        c = KBaseConfig()
        assert c.url == "https://kbase.us/services"
        assert c.auth_token == ""

    def test_logging_defaults(self) -> None:
        c = LoggingConfig()
        assert c.level == "INFO"

    def test_modeling_defaults(self) -> None:
        c = ModelingConfig()
        assert c.default_objective == "bio1"
        assert c.fba_timeout == 300

    def test_ms_defaults(self) -> None:
        c = MsConfig()
        assert c.default_ppm_tolerance == 10.0
        assert c.default_da_tolerance == 0.01

    def test_notebook_defaults(self) -> None:
        c = NotebookConfig()
        assert c.auto_display is True
        assert c.max_display_rows == 100

    def test_paths_defaults(self) -> None:
        c = PathsConfig()
        assert c.data_dir == "./data"

    def test_skani_defaults(self) -> None:
        c = SkaniConfig()
        assert c.executable == "skani"

    def test_ai_curation_defaults(self) -> None:
        c = AICurationConfig()
        assert c.backend == "argo"

    def test_transyt_defaults(self) -> None:
        c = TransytConfig()
        assert c.neo4j_timeout == 120

    def test_config_top_level_defaults(self) -> None:
        cfg = Config()
        assert cfg.max_retry == 3
        assert cfg.scratch == "/tmp"
