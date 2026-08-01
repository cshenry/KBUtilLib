"""Unit tests for kbutillib.domains.kbase.berdl.tokens.

Pure logic, no network: fixed env -> file -> config token precedence,
fallback behavior when earlier sources are absent, and an explicit
assertion that no token value is ever emitted (printed/logged) by the
resolution path.
"""

import logging

import pytest

from kbutillib.domains.kbase.berdl.tokens import (
    KBASE_AUTH_TOKEN_ENV_VAR,
    NoTokenAvailableError,
    require_token,
    resolve_token,
)


class TestPrecedence:
    def test_env_var_wins_over_file_and_config(self, tmp_path):
        token_file = tmp_path / "token"
        token_file.write_text("file-token")

        result = resolve_token(
            env={KBASE_AUTH_TOKEN_ENV_VAR: "env-token"},
            token_file=token_file,
            config_token="config-token",
        )

        assert result == "env-token"

    def test_file_wins_over_config_when_env_absent(self, tmp_path):
        token_file = tmp_path / "token"
        token_file.write_text("file-token")

        result = resolve_token(
            env={},
            token_file=token_file,
            config_token="config-token",
        )

        assert result == "file-token"

    def test_config_used_when_env_and_file_absent(self, tmp_path):
        # token_file points somewhere that doesn't exist -> file source
        # yields nothing, so config is the last resort.
        missing_file = tmp_path / "does-not-exist" / "token"

        result = resolve_token(
            env={},
            token_file=missing_file,
            config_token="config-token",
        )

        assert result == "config-token"

    def test_none_when_no_source_has_a_value(self, tmp_path):
        missing_file = tmp_path / "does-not-exist" / "token"

        result = resolve_token(env={}, token_file=missing_file, config_token=None)

        assert result is None

    def test_this_fixes_the_verified_in_pod_bug(self, tmp_path):
        """In-pod, KBASE_AUTH_TOKEN is set but there's no ~/.kbase/token file.

        The legacy KBBERDLUtils path (file-only) raises 'No KBase token
        available' in exactly this situation. The fixed precedence must
        resolve the env var instead.
        """
        missing_file = tmp_path / "does-not-exist" / "token"

        result = resolve_token(
            env={KBASE_AUTH_TOKEN_ENV_VAR: "in-pod-token"},
            token_file=missing_file,
            config_token=None,
        )

        assert result == "in-pod-token"

    def test_blank_env_value_falls_through_to_file(self, tmp_path):
        """An empty-string env var is treated as absent, not as a real token."""
        token_file = tmp_path / "token"
        token_file.write_text("file-token")

        result = resolve_token(
            env={KBASE_AUTH_TOKEN_ENV_VAR: ""},
            token_file=token_file,
            config_token=None,
        )

        assert result == "file-token"

    def test_file_contents_are_stripped(self, tmp_path):
        token_file = tmp_path / "token"
        token_file.write_text("  file-token-with-whitespace  \n")

        result = resolve_token(env={}, token_file=token_file, config_token=None)

        assert result == "file-token-with-whitespace"

    def test_file_source_can_be_disabled(self):
        """token_file=None skips the file source entirely (no filesystem access)."""
        result = resolve_token(env={}, token_file=None, config_token="config-token")

        assert result == "config-token"


class TestRequireToken:
    def test_returns_resolved_token(self, tmp_path):
        missing_file = tmp_path / "does-not-exist" / "token"

        result = require_token(
            env={KBASE_AUTH_TOKEN_ENV_VAR: "env-token"},
            token_file=missing_file,
        )

        assert result == "env-token"

    def test_raises_when_nothing_resolves(self, tmp_path):
        missing_file = tmp_path / "does-not-exist" / "token"

        with pytest.raises(NoTokenAvailableError):
            require_token(env={}, token_file=missing_file, config_token=None)


class TestTokenNeverEmitted:
    """No token value ever appears in printed, logged, or exception output."""

    SECRET_TOKEN = "sekrit-value-should-never-leak-9f3c2a"  # noqa: S105 (test fixture)

    def test_no_token_value_in_captured_stdout_or_stderr(self, capsys, tmp_path):
        missing_file = tmp_path / "does-not-exist" / "token"

        resolve_token(
            env={KBASE_AUTH_TOKEN_ENV_VAR: self.SECRET_TOKEN},
            token_file=missing_file,
            config_token=None,
        )
        require_token(
            env={KBASE_AUTH_TOKEN_ENV_VAR: self.SECRET_TOKEN},
            token_file=missing_file,
            config_token=None,
        )

        captured = capsys.readouterr()
        assert self.SECRET_TOKEN not in captured.out
        assert self.SECRET_TOKEN not in captured.err

    def test_no_token_value_in_log_records(self, caplog, tmp_path):
        missing_file = tmp_path / "does-not-exist" / "token"

        with caplog.at_level(logging.DEBUG):
            resolve_token(
                env={KBASE_AUTH_TOKEN_ENV_VAR: self.SECRET_TOKEN},
                token_file=missing_file,
                config_token=None,
            )

        for record in caplog.records:
            assert self.SECRET_TOKEN not in record.getMessage()

    def test_no_token_available_error_message_omits_any_value(self, tmp_path):
        """The failure path never had a value, but assert its message stays value-free too."""
        missing_file = tmp_path / "does-not-exist" / "token"

        with pytest.raises(NoTokenAvailableError) as exc_info:
            require_token(env={}, token_file=missing_file, config_token=None)

        message = str(exc_info.value)
        assert self.SECRET_TOKEN not in message
        # The message should describe *sources*, not carry a token value.
        assert KBASE_AUTH_TOKEN_ENV_VAR in message

    def test_default_token_file_is_user_kbase_token(self):
        """Sanity check on the documented default location."""
        from pathlib import Path

        from kbutillib.domains.kbase.berdl.tokens import DEFAULT_TOKEN_FILE

        assert DEFAULT_TOKEN_FILE == Path.home() / ".kbase" / "token"
