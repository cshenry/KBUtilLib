"""Token source tracking and fail-over in SharedEnvUtils (KBUtilLib#94).

A stale pod env token used to outrank a valid ``~/.kbase/token`` with nothing
to say which one was sent. Every token here is a fake string; the env vars are
monkeypatched and the token file lives in ``tmp_path``.
"""
from __future__ import annotations

import pytest

from kbutillib.core.shared_env_utils import SharedEnvUtils

ENV_TOKEN = "fake-env-token"
FILE_TOKEN = "fake-file-token"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("KB_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("KBASE_AUTH_TOKEN", raising=False)


def make_env(tmp_path, file_token=FILE_TOKEN, **kwargs):
    token_file = tmp_path / "token"
    if file_token is not None:
        token_file.write_text(file_token + "\n")
    return SharedEnvUtils(
        config_file=False,
        token_file=tmp_path / "absent-tokens",
        kbase_token_file=token_file,
        **kwargs,
    )


def test_env_token_still_takes_precedence_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv("KBASE_AUTH_TOKEN", ENV_TOKEN)
    env = make_env(tmp_path)
    assert env.get_token("kbase") == ENV_TOKEN
    assert env.get_token_source("kbase") == "env:KBASE_AUTH_TOKEN"


def test_file_token_source_names_the_file(tmp_path):
    env = make_env(tmp_path)
    assert env.get_token("kbase") == FILE_TOKEN
    assert env.get_token_source("kbase") == f"file:{tmp_path / 'token'}"


def test_explicit_token_source(tmp_path, monkeypatch):
    monkeypatch.setenv("KBASE_AUTH_TOKEN", ENV_TOKEN)
    env = make_env(tmp_path, token="fake-explicit")
    assert env.get_token_source("kbase") == "explicit"


def test_no_token_has_no_source(tmp_path):
    env = make_env(tmp_path, file_token=None)
    assert env.get_token_source("kbase") is None


def test_disagreeing_sources_are_logged_by_source_never_by_value(
    tmp_path, monkeypatch
):
    # BaseUtils loggers do not propagate and bind their handler to whichever
    # stderr existed first, so neither caplog nor capfd is reliable here.
    messages = []
    monkeypatch.setattr(SharedEnvUtils, "log_info", lambda self, m: messages.append(m))
    monkeypatch.setenv("KBASE_AUTH_TOKEN", ENV_TOKEN)
    make_env(tmp_path)
    logged = "\n".join(messages)
    assert "using env:KBASE_AUTH_TOKEN" in logged
    assert "DIFFERENT token is also present" in logged
    assert ENV_TOKEN not in logged
    assert FILE_TOKEN not in logged


def test_fail_over_from_rejected_env_token_to_file(tmp_path, monkeypatch):
    monkeypatch.setenv("KBASE_AUTH_TOKEN", ENV_TOKEN)
    env = make_env(tmp_path)
    assert env.fail_over_token("kbase") is True
    assert env.get_token("kbase") == FILE_TOKEN
    assert env.get_token_source("kbase") == f"file:{tmp_path / 'token'}"
    # Both now rejected: nothing left.
    assert env.fail_over_token("kbase") is False
    assert env.get_token("kbase") == FILE_TOKEN


def test_fail_over_rereads_a_file_refreshed_after_construction(tmp_path, monkeypatch):
    """The point for a long-lived process: the file may change after start."""
    monkeypatch.setenv("KBASE_AUTH_TOKEN", ENV_TOKEN)
    env = make_env(tmp_path, file_token=ENV_TOKEN)  # file held the same token
    (tmp_path / "token").write_text("fake-refreshed-token\n")
    assert env.fail_over_token("kbase") is True
    assert env.get_token("kbase") == "fake-refreshed-token"


def test_identical_tokens_do_not_fail_over(tmp_path, monkeypatch):
    monkeypatch.setenv("KBASE_AUTH_TOKEN", ENV_TOKEN)
    env = make_env(tmp_path, file_token=ENV_TOKEN)
    assert env.fail_over_token("kbase") is False


def test_explicit_token_is_never_failed_over(tmp_path, monkeypatch):
    monkeypatch.setenv("KBASE_AUTH_TOKEN", ENV_TOKEN)
    env = make_env(tmp_path, token="fake-explicit")
    assert env.fail_over_token("kbase") is False
    assert env.get_token("kbase") == "fake-explicit"


def test_other_namespaces_do_not_fail_over(tmp_path):
    env = make_env(tmp_path)
    env.set_token("fake-berdl", namespace="berdl", save_file=False)
    assert env.fail_over_token("berdl") is False
