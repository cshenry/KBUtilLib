"""``resolve_credentials`` for an ``anthropic`` endpoint: resolve, or REFUSE.

An injected environment mapping is used throughout; the real process
environment is never touched.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Mapping

import pytest

from kbutillib.domains.genome.ontology_mapping.council_run import (
    CREDENTIAL_CHECKS,
    RunRefused,
    resolve_credentials,
)

SECRET = "sk-ant-api03-DO-NOT-LEAK-0123456789abcdef"


class _Config:
    def __init__(self, endpoints: Mapping[str, Mapping[str, Any]]) -> None:
        self._endpoints = {k: dict(v) for k, v in endpoints.items()}

    def endpoint(self, name: str) -> Dict[str, Any]:
        return dict(self._endpoints[name])


def _plan(*endpoints: str) -> Dict[str, Any]:
    return {
        "transport_allocation": [
            {"seat": f"proposer_{i}", "endpoint": name, "endpoint_kind": "anthropic"}
            for i, name in enumerate(endpoints, 1)
        ]
    }


def test_environment_variable_resolves_by_name_never_value() -> None:
    config = _Config({"a-haiku": {"kind": "anthropic", "api_key_env": "MY_ANTHROPIC_KEY"}})
    env = {"MY_ANTHROPIC_KEY": SECRET}
    rows = resolve_credentials(_plan("a-haiku"), config, environ=env)
    assert rows == [
        {
            "endpoint": "a-haiku",
            "kind": "anthropic",
            "requires": "api_key",
            "name": "MY_ANTHROPIC_KEY",
            "source": "environment",
        }
    ]
    assert SECRET not in json.dumps(rows)


def test_default_variable_is_anthropic_api_key() -> None:
    config = _Config({"a": {"kind": "anthropic"}})
    rows = resolve_credentials(_plan("a"), config, environ={"ANTHROPIC_API_KEY": SECRET})
    assert rows[0]["name"] == "ANTHROPIC_API_KEY"
    assert rows[0]["requires"] == "api_key"
    assert SECRET not in json.dumps(rows)


def test_explicit_api_key_reports_config_source() -> None:
    config = _Config({"a": {"kind": "anthropic", "api_key": SECRET}})
    rows = resolve_credentials(_plan("a"), config, environ={})
    assert rows[0]["requires"] == "api_key"
    assert rows[0]["name"] == "api_key"
    assert rows[0]["source"] == "config"
    assert SECRET not in json.dumps(rows)


def test_neither_refuses_naming_endpoint_and_variable() -> None:
    config = _Config({"a-opus5": {"kind": "anthropic", "api_key_env": "MY_ANTHROPIC_KEY"}})
    with pytest.raises(RunRefused) as excinfo:
        resolve_credentials(_plan("a-opus5"), config, environ={"ANTHROPIC_API_KEY": "other"})
    assert excinfo.value.check == "anthropic_api_key"
    assert "anthropic_api_key" in CREDENTIAL_CHECKS
    message = str(excinfo.value)
    assert "a-opus5" in message
    assert "MY_ANTHROPIC_KEY" in message
    assert "export MY_ANTHROPIC_KEY=" in message
    assert "park" in message


def test_empty_variable_refuses() -> None:
    config = _Config({"a": {"kind": "anthropic"}})
    with pytest.raises(RunRefused):
        resolve_credentials(_plan("a"), config, environ={"ANTHROPIC_API_KEY": "   "})


def test_plan_kind_alone_never_reaches_requires_none() -> None:
    """With no config, the plan's recorded kind still routes to the anthropic
    branch: it refuses rather than reporting ``requires: none``."""
    with pytest.raises(RunRefused):
        resolve_credentials(_plan("a"), None, environ={})
    rows = resolve_credentials(_plan("a"), None, environ={"ANTHROPIC_API_KEY": SECRET})
    assert rows[0]["requires"] == "api_key"
