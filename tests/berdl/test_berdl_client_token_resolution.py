"""Token resolution order for the BERDL REST client (KBBERDLUtils._get_headers).

Order: get_token("berdl") -> get_token("kbase") -> $KB_AUTH_TOKEN.

The "berdl" namespace must win because berdl/transports.py seeds the client
with set_token(..., namespace="berdl"). These live under tests/berdl rather
than tests/kbase because everything under tests/kbase is skipped unless
KBASE_LIVE_TESTS=1, and these tests are pure logic with no network.
"""

import pytest

from kbutillib.domains.kbase.kb_berdl_utils import KBBERDLUtils


@pytest.fixture
def client(monkeypatch):
    """A KBBERDLUtils with no file discovery and no ambient token."""
    monkeypatch.delenv("KB_AUTH_TOKEN", raising=False)
    return KBBERDLUtils(config_file=False, token_file=None, kbase_token_file=None)


def _bearer(client):
    return client._get_headers()["Authorization"]


def test_berdl_namespace_token_is_used(client, monkeypatch):
    """(a) A token stored with namespace="berdl" is found, and beats the rest."""
    monkeypatch.setenv("KB_AUTH_TOKEN", "env-token")
    client.set_token("kbase-token", namespace="kbase", save_file=False)
    client.set_token("berdl-token", namespace="berdl", save_file=False)

    assert _bearer(client) == "Bearer berdl-token"


def test_kbase_token_used_when_no_berdl_token(client, monkeypatch):
    """(b) With no "berdl" token, the "kbase" token is used (over the env var)."""
    monkeypatch.setenv("KB_AUTH_TOKEN", "env-token")
    client.set_token("kbase-token", namespace="kbase", save_file=False)
    assert client.get_token(namespace="berdl") is None

    assert _bearer(client) == "Bearer kbase-token"


def test_env_var_used_when_no_berdl_or_kbase_token(client, monkeypatch):
    """(c) With neither namespace set, KB_AUTH_TOKEN is used."""
    monkeypatch.setenv("KB_AUTH_TOKEN", "env-token")
    assert client.get_token(namespace="berdl") is None
    assert client.get_token(namespace="kbase") is None

    assert _bearer(client) == "Bearer env-token"


def test_no_token_error_lists_all_three_sources(client):
    with pytest.raises(ValueError, match="No KBase token available") as excinfo:
        client._get_headers()

    message = str(excinfo.value)
    assert 'namespace="berdl"' in message
    assert '"kbase" token' in message
    assert "KB_AUTH_TOKEN" in message


def test_transport_seeded_token_reaches_client_headers(client):
    """The real seeding path in berdl/transports.py must authenticate requests."""
    from kbutillib.domains.kbase.berdl.transports import OffPodTransport

    client.set_token("other-kbase-token", namespace="kbase", save_file=False)
    transport = OffPodTransport(token="seeded-token", client=client)
    assert _bearer(transport._client) == "Bearer seeded-token"
