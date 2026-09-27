"""CAC conformance tests (plane 1 invariants) for the data/service layer.

Covers I4 (APP_ID / app_us from arc_context, never hardcoded), I5 (contract
version startup gate — any difference refuses startup, no minor/warn branch), and
the S16 state-directory helper (one literal, no doubled kind-apps under
$KING_STATE). App-level I1/I6 live in test_app.py where the FastAPI app is built.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kbutillib.arc_context import APP_ID, app_us
from kbutillib.koros_arc_store import CONTRACT_VERSION
from kbutillib.koros_arc_store.exceptions import ContractVersionMismatch
from kbutillib.models_and_analyses import (
    build_arc_models,
    build_portfolio,
    check_startup_contract_version,
    resolve_app_state_dir,
)

# ── I4: identity comes from arc_context, never hardcoded ─────────────────────────


def test_app_id_and_module_from_arc_context():
    assert APP_ID == "models-and-analyses"
    assert app_us() == "models_and_analyses"
    # Derived mechanically, not a second spelling.
    assert app_us() == APP_ID.replace("-", "_")


def test_responses_carry_arc_context_identity(seeded_store):
    portfolio = build_portfolio(seeded_store)
    assert portfolio["app"]["id"] == APP_ID
    assert portfolio["app"]["module"] == app_us()
    models = build_arc_models(seeded_store, "projA", "main")
    assert models["app"]["id"] == APP_ID
    assert models["app"]["module"] == app_us()


# ── I5: contract-version startup gate ────────────────────────────────────────────


def test_matching_contract_version_proceeds():
    # Equal proceeds (returns None, no raise).
    assert check_startup_contract_version(CONTRACT_VERSION) is None


def test_any_contract_version_difference_refuses_startup():
    with pytest.raises(ContractVersionMismatch) as exc:
        check_startup_contract_version(CONTRACT_VERSION + 1)
    # The error names BOTH versions.
    msg = str(exc.value)
    assert str(CONTRACT_VERSION) in msg
    assert str(CONTRACT_VERSION + 1) in msg


def test_no_minor_warn_branch_even_for_adjacent_version():
    """The criteria win: ANY difference refuses — there is no warn-and-proceed."""
    with pytest.raises(ContractVersionMismatch):
        check_startup_contract_version(0)
    with pytest.raises(ContractVersionMismatch):
        check_startup_contract_version(2)


def test_absent_king_version_is_a_noop_for_standalone():
    """Standalone (KING not visible) does not gate on a version it cannot see (I1)."""
    assert check_startup_contract_version(None) is None


# ── S16: one state-directory helper ──────────────────────────────────────────────


def test_state_dir_uses_king_state_without_doubling(tmp_path, monkeypatch):
    king_state = tmp_path / "kind-apps"
    monkeypatch.setenv("KING_STATE", str(king_state))
    state_dir = resolve_app_state_dir()
    # Exactly $KING_STATE/state/models-and-analyses — kind-apps is NOT doubled.
    assert state_dir == king_state / "state" / APP_ID
    # The literal kind-apps appears once (it came from KING_STATE), not twice.
    assert str(state_dir).count("kind-apps") == 1
    assert state_dir.is_dir()  # parents created


def test_state_dir_falls_back_without_king_state(tmp_path, monkeypatch):
    monkeypatch.delenv("KING_STATE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    # Re-resolve HOME by reimporting Path.home semantics: resolve_app_state_dir
    # reads Path.home() at call time.
    state_dir = resolve_app_state_dir()
    assert state_dir == Path.home() / "kind-apps" / "state" / APP_ID
    assert state_dir.is_dir()
