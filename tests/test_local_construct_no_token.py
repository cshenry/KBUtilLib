"""Tests for local-only construction without a KBase token.

Covers task kbu-tmfa-p2-local-construct:
- MSFBAUtils / KBModelUtils construct cleanly with no KB_AUTH_TOKEN and no
  KBASE_AUTH_TOKEN set (no TypeError).
- The KBASE_AUTH_TOKEN env-var is recognized as a KBase token alias (bridge).
- The lazy kbase_api property raises a clear RuntimeError when accessed without
  a token — not a bare TypeError.
- Setting only KBASE_AUTH_TOKEN (not KB_AUTH_TOKEN) is sufficient for the lazy
  accessor to succeed (bridge works end-to-end into KBaseAPI construction path).
"""

import os
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean_env(extra=None):
    """Return a copy of os.environ without both KBase token vars, plus any extras."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("KB_AUTH_TOKEN", "KBASE_AUTH_TOKEN")}
    if extra:
        env.update(extra)
    return env


# ---------------------------------------------------------------------------
# 1. No-token construction — public constructor boundary
# ---------------------------------------------------------------------------

class TestNoTokenConstruction:
    """MSFBAUtils and KBModelUtils must construct without error when no token is available."""

    @pytest.fixture(autouse=True)
    def _require_deps(self):
        pytest.importorskip("cobrakbase", reason="cobrakbase required")
        pytest.importorskip("modelseedpy", reason="modelseedpy required")

    def test_kb_model_utils_no_token_no_error(self):
        """KBModelUtils(config_file=False, token_file=None, kbase_token_file=None)
        succeeds with no KB_AUTH_TOKEN and no KBASE_AUTH_TOKEN set."""
        from kbutillib.kb_model_utils import KBModelUtils

        with patch.dict(os.environ, _clean_env(), clear=True):
            obj = KBModelUtils(config_file=False, token_file=None, kbase_token_file=None)

        assert obj is not None, "KBModelUtils should construct without error"

    def test_ms_fba_utils_no_token_no_error(self):
        """MSFBAUtils(config_file=False, token_file=None, kbase_token_file=None)
        succeeds with no KB_AUTH_TOKEN and no KBASE_AUTH_TOKEN set."""
        from kbutillib.ms_fba_utils import MSFBAUtils

        with patch.dict(os.environ, _clean_env(), clear=True):
            obj = MSFBAUtils(config_file=False, token_file=None, kbase_token_file=None)

        assert obj is not None, "MSFBAUtils should construct without error"

    def test_no_token_does_not_set_kb_auth_token_env(self):
        """When no token is available, KB_AUTH_TOKEN must not be set in the environment."""
        from kbutillib.kb_model_utils import KBModelUtils

        with patch.dict(os.environ, _clean_env(), clear=True):
            KBModelUtils(config_file=False, token_file=None, kbase_token_file=None)
            assert "KB_AUTH_TOKEN" not in os.environ, (
                "KB_AUTH_TOKEN must not be written when no token is available"
            )


# ---------------------------------------------------------------------------
# 2. KBASE_AUTH_TOKEN bridge — SharedEnvUtils level
# ---------------------------------------------------------------------------

class TestKbaseAuthTokenBridge:
    """KBASE_AUTH_TOKEN env var must be recognized as a synonym for KB_AUTH_TOKEN."""

    def test_kbase_auth_token_recognized_by_shared_env(self):
        """SharedEnvUtils with only KBASE_AUTH_TOKEN set returns that value via get_token('kbase')."""
        from kbutillib.shared_env_utils import SharedEnvUtils

        token = "bridge-test-token-kbase-auth"
        with patch.dict(os.environ, _clean_env({"KBASE_AUTH_TOKEN": token}), clear=True):
            env = SharedEnvUtils(config_file=False, token_file=None, kbase_token_file=None)

        assert env.get_token("kbase") == token, (
            f"Expected KBASE_AUTH_TOKEN '{token}' to be recognized, "
            f"got '{env.get_token('kbase')}'"
        )

    def test_kb_auth_token_still_recognized(self):
        """KB_AUTH_TOKEN (the primary name) is still recognized after the bridge change."""
        from kbutillib.shared_env_utils import SharedEnvUtils

        token = "primary-kb-auth-token"
        with patch.dict(os.environ, _clean_env({"KB_AUTH_TOKEN": token}), clear=True):
            env = SharedEnvUtils(config_file=False, token_file=None, kbase_token_file=None)

        assert env.get_token("kbase") == token, (
            f"Expected KB_AUTH_TOKEN '{token}' to be recognized, "
            f"got '{env.get_token('kbase')}'"
        )

    def test_kb_auth_token_wins_over_kbase_auth_token(self):
        """When both vars are set, KB_AUTH_TOKEN takes precedence over KBASE_AUTH_TOKEN."""
        from kbutillib.shared_env_utils import SharedEnvUtils

        kb_token = "kb-auth-wins"
        kbase_token = "kbase-auth-loses"
        both = _clean_env({"KB_AUTH_TOKEN": kb_token, "KBASE_AUTH_TOKEN": kbase_token})

        with patch.dict(os.environ, both, clear=True):
            env = SharedEnvUtils(config_file=False, token_file=None, kbase_token_file=None)

        assert env.get_token("kbase") == kb_token, (
            f"KB_AUTH_TOKEN should win over KBASE_AUTH_TOKEN, "
            f"got '{env.get_token('kbase')}'"
        )

    def test_no_token_vars_returns_none(self):
        """When neither KB_AUTH_TOKEN nor KBASE_AUTH_TOKEN is set, get_token returns None."""
        from kbutillib.shared_env_utils import SharedEnvUtils

        with patch.dict(os.environ, _clean_env(), clear=True):
            env = SharedEnvUtils(config_file=False, token_file=None, kbase_token_file=None)

        assert env.get_token("kbase") is None, (
            "get_token('kbase') should be None when no token is available"
        )


# ---------------------------------------------------------------------------
# 3. Lazy kbase_api — clear error on access without token
# ---------------------------------------------------------------------------

class TestLazyKbaseApi:
    """kbase_api property must raise RuntimeError (not TypeError) when no token is set."""

    @pytest.fixture(autouse=True)
    def _require_deps(self):
        pytest.importorskip("cobrakbase", reason="cobrakbase required")
        pytest.importorskip("modelseedpy", reason="modelseedpy required")

    def test_kbase_api_raises_runtime_error_without_token(self):
        """Accessing kbase_api with no token raises RuntimeError with clear message."""
        from kbutillib.kb_model_utils import KBModelUtils

        with patch.dict(os.environ, _clean_env(), clear=True):
            obj = KBModelUtils(config_file=False, token_file=None, kbase_token_file=None)
            with pytest.raises(RuntimeError, match="No KBase token"):
                _ = obj.kbase_api

    def test_kbase_api_not_raised_on_construction(self):
        """No RuntimeError is raised during __init__ even when no token is set."""
        from kbutillib.kb_model_utils import KBModelUtils

        with patch.dict(os.environ, _clean_env(), clear=True):
            # Must not raise
            obj = KBModelUtils(config_file=False, token_file=None, kbase_token_file=None)

        assert obj is not None

    def test_kbase_api_constructs_when_token_present(self):
        """kbase_api property succeeds and returns a non-None object when a token is set."""
        pytest.importorskip("cobrakbase", reason="cobrakbase required")
        from kbutillib.kb_model_utils import KBModelUtils

        token = "fake-kbase-token-lazy-test"
        with patch.dict(os.environ, _clean_env(), clear=True):
            obj = KBModelUtils(
                config_file=False,
                token_file=None,
                kbase_token_file=None,
                token=token,
            )
            # Mock out cobrakbase.KBaseAPI so we don't need a real connection
            mock_api = MagicMock()
            obj.cobrakbase = MagicMock()
            obj.cobrakbase.KBaseAPI.return_value = mock_api
            api = obj.kbase_api

        assert api is mock_api, "kbase_api should return the cobrakbase.KBaseAPI() result"

    def test_kbase_api_lazy_via_kbase_auth_token(self):
        """kbase_api construction succeeds when token comes from KBASE_AUTH_TOKEN bridge."""
        pytest.importorskip("cobrakbase", reason="cobrakbase required")
        from kbutillib.kb_model_utils import KBModelUtils

        token = "bridge-lazy-token"
        with patch.dict(os.environ, _clean_env({"KBASE_AUTH_TOKEN": token}), clear=True):
            obj = KBModelUtils(config_file=False, token_file=None, kbase_token_file=None)
            # Confirm token was promoted correctly
            assert obj.get_token("kbase") == token, (
                f"KBASE_AUTH_TOKEN should be promoted to kbase token, "
                f"got '{obj.get_token('kbase')}'"
            )
            # Mock KBaseAPI so we don't need a live connection
            mock_api = MagicMock()
            obj.cobrakbase = MagicMock()
            obj.cobrakbase.KBaseAPI.return_value = mock_api
            # Force a fresh lazy build by resetting the private field
            obj._kbase_api = None
            api = obj.kbase_api

        assert api is mock_api
