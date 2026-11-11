"""``scripts/council/anthropic_acceptance_rehearsal.py`` REFUSES before spending.

Only the refusal paths are tested: the script's whole purpose is a LIVE run,
and it has no fixture mode to test -- by design, so it can never report
success for a check that did not happen.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SCRIPT_PATH = _REPO_ROOT / "scripts/council/anthropic_acceptance_rehearsal.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("anthropic_acceptance_rehearsal", _SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_refuses_without_a_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                               capsys: Any) -> None:
    monkeypatch.delenv("KBU_REHEARSAL_TEST_KEY", raising=False)
    work = tmp_path / "work"
    code = _load().main(["--work-root", str(work), "--api-key-env", "KBU_REHEARSAL_TEST_KEY",
                         "--confirm-spend", "--report", str(tmp_path / "r.txt"),
                         "--json", str(tmp_path / "r.json")])
    assert code == 2
    err = capsys.readouterr().err
    assert "REFUSED" in err and "KBU_REHEARSAL_TEST_KEY" in err
    assert "fixture" in err
    assert not work.exists()
    assert not (tmp_path / "r.json").exists()


@pytest.mark.skipif(importlib.util.find_spec("anthropic") is None,
                    reason="anthropic SDK not installed")
def test_prints_estimate_and_refuses_without_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.setenv("KBU_REHEARSAL_TEST_KEY", "sk-ant-not-real")
    monkeypatch.delenv("KBU_ANTHROPIC_REHEARSAL_CONFIRM", raising=False)
    work = tmp_path / "work"
    code = _load().main(["--work-root", str(work), "--api-key-env", "KBU_REHEARSAL_TEST_KEY",
                         "--report", str(tmp_path / "r.txt"), "--json", str(tmp_path / "r.json")])
    assert code == 2
    err = capsys.readouterr().err
    assert "ESTIMATED COST CEILING" in err
    assert "nothing was sent" in err
    assert not work.exists()


@pytest.mark.skipif(importlib.util.find_spec("anthropic") is None,
                    reason="anthropic SDK not installed")
def test_refuses_an_invented_model_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                      capsys: Any) -> None:
    monkeypatch.setenv("KBU_REHEARSAL_TEST_KEY", "sk-ant-not-real")
    code = _load().main(["--work-root", str(tmp_path / "w"), "--api-key-env",
                         "KBU_REHEARSAL_TEST_KEY", "--meta", "claude-opus-5-5", "--confirm-spend"])
    assert code == 2
    assert "claude-opus-5-5" in capsys.readouterr().err
