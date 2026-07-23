"""Construction regression tests for ArgoUtils (task: fix-argo-client-tunnel).

Defect: ``ArgoUtils.__init__`` built its ``httpx.Client`` with the
per-scheme ``proxies={...}`` mapping, which httpx 0.28 removed. Every
``ArgoUtils(...)`` construction therefore raised
``TypeError: Client.__init__() got an unexpected keyword argument 'proxies'``
whenever a ``proxy_port`` was set (the default). These tests pin the
httpx-0.28-correct ``proxy=`` construction so the regression cannot return.

``env='dev'`` is passed so ``__init__`` skips the prod->dev network ping
(the dual-env quick check only fires for ``env is None``), keeping the tests
offline. The SOCKS5 client path needs the optional ``socksio`` package
(the ``httpx[socks]`` extra); it is skipped if unavailable rather than
failing spuriously.
"""

import importlib.util
import os
from unittest.mock import patch

import pytest

_HAS_SOCKSIO = importlib.util.find_spec("socksio") is not None


def _clean_env():
    """os.environ without the KBase token vars (so construction stays local)."""
    return {
        k: v
        for k, v in os.environ.items()
        if k not in ("KB_AUTH_TOKEN", "KBASE_AUTH_TOKEN")
    }


@pytest.mark.skipif(not _HAS_SOCKSIO, reason="socksio (httpx[socks]) required for SOCKS5 proxy")
def test_argo_utils_constructs_with_socks_proxy_no_typeerror():
    """The default proxy_port path builds an httpx.Client via ``proxy=`` and
    must not raise the httpx-0.28 ``proxies`` TypeError."""
    from kbutillib.argo_utils import ArgoUtils

    with patch.dict(os.environ, _clean_env(), clear=True):
        obj = ArgoUtils(
            model="gpt4o",
            env="dev",
            config_file=False,
            token_file=None,
            kbase_token_file=None,
        )

    assert obj.cli is not None


def test_argo_utils_constructs_without_proxy():
    """With ``proxy_port=None`` the direct (no-SOCKS) client builds cleanly and
    needs no socksio."""
    from kbutillib.argo_utils import ArgoUtils

    with patch.dict(os.environ, _clean_env(), clear=True):
        obj = ArgoUtils(
            model="gpt4o",
            env="dev",
            proxy_port=None,
            config_file=False,
            token_file=None,
            kbase_token_file=None,
        )

    assert obj.cli is not None
