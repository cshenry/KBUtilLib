"""``resolve_credentials`` for an ``alcf`` endpoint: resolve the token source, or REFUSE.

Token sources are fake local commands; the live service is never contacted.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, Mapping

import pytest

from kbutillib.domains.ai.llm.apis import alcf as alcf_module
from kbutillib.domains.genome.ontology_mapping.council_run import (
    CREDENTIAL_CHECKS,
    RunRefused,
    resolve_credentials,
)

SECRET = "globus-DO-NOT-LEAK-0123456789abcdef"


class _Config:
    def __init__(self, endpoints: Mapping[str, Mapping[str, Any]]) -> None:
        self._endpoints = {k: dict(v) for k, v in endpoints.items()}

    def endpoint(self, name: str) -> Dict[str, Any]:
        return dict(self._endpoints[name])


def _plan(*endpoints: str) -> Dict[str, Any]:
    return {
        "transport_allocation": [
            {"seat": f"proposer_{i}", "endpoint": name, "endpoint_kind": "alcf"}
            for i, name in enumerate(endpoints, 1)
        ]
    }


@pytest.fixture(autouse=True)
def _fresh_cache():
    alcf_module.clear_token_cache()
    yield
    alcf_module.clear_token_cache()


def _echo(token: str = SECRET) -> list:
    return [sys.executable, "-c", f"print({token!r})"]


def _fail() -> list:
    return [sys.executable, "-c", "import sys; sys.stderr.write('x'); sys.exit(3)"]


def test_alcf_token_is_a_credential_check() -> None:
    assert "alcf_token" in CREDENTIAL_CHECKS
    assert len(CREDENTIAL_CHECKS) == 4
    assert CREDENTIAL_CHECKS[:3] == ("argo_user", "amsc_token", "anthropic_api_key")


def test_working_token_command_appends_one_row_without_the_token() -> None:
    command = _echo()
    config = _Config({"alcf-tara": {"kind": "alcf", "options": {"token_command": command}}})
    env: Dict[str, str] = {}
    rows = resolve_credentials(_plan("alcf-tara"), config, environ=env)
    assert rows == [
        {
            "endpoint": "alcf-tara",
            "kind": "alcf",
            "requires": "globus_token",
            "name": " ".join(command),
            "source": "token_command",
        }
    ]
    assert SECRET not in json.dumps(rows).replace(" ".join(command), "")
    assert env == {}


def test_working_token_import_reports_the_import_path(tmp_path, monkeypatch) -> None:
    (tmp_path / "fake_alcf_tok.py").write_text(f"def get(scope):\n    return {SECRET!r}\n")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    config = _Config(
        {"a": {"kind": "alcf", "options": {"token_import": "fake_alcf_tok:get"}}}
    )
    rows = resolve_credentials(_plan("a"), config, environ={})
    assert rows[0]["name"] == "fake_alcf_tok:get"
    assert rows[0]["source"] == "token_import"
    assert rows[0]["requires"] == "globus_token"
    assert SECRET not in json.dumps(rows)


def test_failing_source_refuses_naming_endpoint_and_fix() -> None:
    config = _Config({"alcf-sophia": {"kind": "alcf", "options": {"token_command": _fail()}}})
    with pytest.raises(RunRefused) as excinfo:
        resolve_credentials(_plan("alcf-sophia"), config, environ={})
    assert excinfo.value.check == "alcf_token"
    message = str(excinfo.value)
    assert "alcf-sophia" in message
    assert "alcf-tokens login" in message
    assert "30-day" in message
    assert "park" in message
    assert "token_command" in message


def test_refusal_never_carries_the_token(tmp_path) -> None:
    # The token comes from a script's output, not its argv (argv is persisted
    # and quoted in the refusal by design, so it must carry no credential).
    script = tmp_path / "tok.py"
    script.write_text(f"print({SECRET!r})\nprint('second line')\n")
    config = _Config(
        {"a": {"kind": "alcf", "options": {"token_command": [sys.executable, str(script)]}}}
    )
    with pytest.raises(RunRefused) as excinfo:
        resolve_credentials(_plan("a"), config, environ={})
    assert SECRET not in str(excinfo.value)


def test_plan_kind_alone_routes_to_alcf_branch(tmp_path, monkeypatch) -> None:
    """With no config the default ``alcf-tokens`` command is run; with it off
    PATH the run refuses rather than reporting ``requires: none``."""
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(RunRefused) as excinfo:
        resolve_credentials(_plan("a"), None, environ={})
    assert excinfo.value.check == "alcf_token"
    assert "alcf-tokens" in str(excinfo.value)
