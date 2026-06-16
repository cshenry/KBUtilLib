"""Unit tests for KB_AUTH_TOKEN env-var injection into SharedEnvUtils.

Acceptance Criteria 11 and 12 (kbu-skills-friction-fix PRD, Task E):
- When KB_AUTH_TOKEN is set in the environment, SharedEnvUtils().get_token('kbase')
  returns the env value even when a kbase token file also exists.
- When KB_AUTH_TOKEN is unset, get_token('kbase') falls back to the file value.
- The env-var token is NOT written to disk.
- The explicit token= constructor param still beats the env var.
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


class TestKBAuthTokenInjection:
    """KB_AUTH_TOKEN env-var injection into SharedEnvUtils."""

    @pytest.fixture
    def kbase_token_file(self, tmp_path):
        """Write a dummy kbase token file and return its path."""
        token_path = tmp_path / "kbase_token"
        token_path.write_text("file-sourced-kbase-token")
        return token_path

    def _make_env(self, kbase_token_file, tmp_path=None, **kwargs):
        """Helper: construct SharedEnvUtils with no config discovery and a real kbase token file.

        We pass a non-existent path for token_file to avoid the pre-existing
        Path(None) crash in read_token_file when token_file=None but kbase_token_file exists.
        """
        from kbutillib.shared_env_utils import SharedEnvUtils

        # Use a dummy path that doesn't exist so read_token_file skips the standard
        # token file but still reads the kbase token file correctly.
        nonexistent_token_file = Path("/nonexistent/token/file/that/does/not/exist")

        return SharedEnvUtils(
            config_file=False,
            token_file=nonexistent_token_file,
            kbase_token_file=kbase_token_file,
            **kwargs,
        )

    # ------------------------------------------------------------------
    # Direction 1: KB_AUTH_TOKEN set => env value wins over file token
    # ------------------------------------------------------------------

    def test_env_var_overrides_file_token(self, kbase_token_file):
        """get_token('kbase') returns env value when KB_AUTH_TOKEN is set."""
        env_token = "env-sourced-kbase-token"
        with patch.dict(os.environ, {"KB_AUTH_TOKEN": env_token}, clear=False):
            env_utils = self._make_env(kbase_token_file)

        assert env_utils.get_token("kbase") == env_token, (
            f"Expected env token '{env_token}', got '{env_utils.get_token('kbase')}'"
        )

    # ------------------------------------------------------------------
    # Direction 2: KB_AUTH_TOKEN unset => falls back to file token
    # ------------------------------------------------------------------

    def test_file_token_used_when_env_var_unset(self, kbase_token_file):
        """get_token('kbase') returns file value when KB_AUTH_TOKEN is not set."""
        # Make absolutely sure the env var is absent for this test.
        env_without = {k: v for k, v in os.environ.items() if k != "KB_AUTH_TOKEN"}
        with patch.dict(os.environ, env_without, clear=True):
            env_utils = self._make_env(kbase_token_file)

        assert env_utils.get_token("kbase") == "file-sourced-kbase-token", (
            f"Expected file token, got '{env_utils.get_token('kbase')}'"
        )

    # ------------------------------------------------------------------
    # Env var does NOT persist to disk
    # ------------------------------------------------------------------

    def test_env_var_token_not_written_to_disk(self, kbase_token_file):
        """Constructing with KB_AUTH_TOKEN set must not modify the token file."""
        original_contents = kbase_token_file.read_text()
        env_token = "env-sourced-kbase-token"

        with patch.dict(os.environ, {"KB_AUTH_TOKEN": env_token}, clear=False):
            self._make_env(kbase_token_file)

        # File must be byte-for-byte identical to what it was before construction.
        assert kbase_token_file.read_text() == original_contents, (
            "KB_AUTH_TOKEN env-var path must NOT write the token to disk"
        )

    # ------------------------------------------------------------------
    # Explicit token= param beats env var (existing precedence preserved)
    # ------------------------------------------------------------------

    def test_explicit_token_param_beats_env_var(self, kbase_token_file):
        """token= constructor param takes precedence over KB_AUTH_TOKEN."""
        env_token = "env-sourced-kbase-token"
        explicit_token = "explicit-param-token"

        with patch.dict(os.environ, {"KB_AUTH_TOKEN": env_token}, clear=False):
            env_utils = self._make_env(kbase_token_file, token=explicit_token)

        assert env_utils.get_token("kbase") == explicit_token, (
            f"Expected explicit token '{explicit_token}', got '{env_utils.get_token('kbase')}'"
        )
