"""Tests for kbutillib.arc_context (arc resolution for KOROS producers).

Covers the full precedence chain of :func:`resolve_current_arc` against a
synthetic runs tree built in a ``tmp_path`` fixture — explicit over env, env over
cwd, cwd inside vs outside the tree, a project directory that is itself an arc, an
unknown explicit slug, an ambiguous bare slug, and an unresolvable runs root. It
also checks :func:`warn_not_indexed` and the CAC identity helpers (I4).

Everything is driven through ``KOROS_HOME``/``KOROS_ARC`` via ``monkeypatch``;
no test depends on a real path on any machine.
"""

from __future__ import annotations

import json
import os

import pytest

from kbutillib.arc_context import (
    APP_ID,
    app_us,
    resolve_current_arc,
    warn_not_indexed,
)

# The three environment variables that steer runs-root resolution and the env
# branch. Every test clears them, then sets only what it needs.
_ENV_VARS = ("KOROS_HOME", "KOROS_ARC", "KING_KOROS_RUNS")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Clear the KOROS environment so ambient config never leaks into a test."""
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    yield


def _write_provenance(arc_dir, project=None):
    """Write a minimal valid PROVENANCE.json into *arc_dir*.

    ``run_id`` and ``created_at`` are the only required fields; ``project`` is
    written when supplied so the resolver's project-field read has something to
    return.
    """
    arc_dir.mkdir(parents=True, exist_ok=True)
    payload = {"run_id": f"run-{arc_dir.name}", "created_at": "2026-09-26T00:00:00Z"}
    if project is not None:
        payload["project"] = project
    (arc_dir / "PROVENANCE.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def runs_tree(tmp_path, monkeypatch):
    """Build a synthetic runs tree and point KOROS_HOME at its workspace root.

    Layout (runs root is ``<workspace>/runs``):

        <runs>/alpha/arcs/shared/        (project alpha)
        <runs>/alpha/arcs/only-alpha/    (project alpha)
        <runs>/beta/arcs/shared/         (project beta — 'shared' is ambiguous)
        <runs>/beta/arcs/only-beta/      (project beta)
        <runs>/gamma/                     (project gamma is ITSELF an arc)

    Returns the resolved runs-root Path. ``KOROS_HOME`` is set to the workspace
    root (not the runs dir) to exercise the ``$KOROS_HOME/runs`` chain.
    """
    workspace = tmp_path / "workspace"
    runs = workspace / "runs"

    _write_provenance(runs / "alpha" / "arcs" / "shared", project="alpha")
    _write_provenance(runs / "alpha" / "arcs" / "only-alpha", project="alpha")
    _write_provenance(runs / "beta" / "arcs" / "shared", project="beta")
    _write_provenance(runs / "beta" / "arcs" / "only-beta", project="beta")
    # gamma is a project directory that is itself an arc: PROVENANCE.json and no
    # arcs/ subdirectory.
    _write_provenance(runs / "gamma", project="gamma")

    monkeypatch.setenv("KOROS_HOME", str(workspace))
    return runs


# ── precedence: explicit wins over env ─────────────────────────────────────────


def test_explicit_project_slug_resolves(runs_tree):
    assert resolve_current_arc("alpha/only-alpha") == ("alpha", "only-alpha")


def test_explicit_bare_unique_slug_resolves(runs_tree):
    assert resolve_current_arc("only-beta") == ("beta", "only-beta")


def test_explicit_wins_over_env(runs_tree, monkeypatch):
    monkeypatch.setenv("KOROS_ARC", "only-beta")
    # explicit points at a different, unambiguous arc and must win.
    assert resolve_current_arc("only-alpha") == ("alpha", "only-alpha")


# ── precedence: env wins over cwd ──────────────────────────────────────────────


def test_env_resolves(runs_tree, monkeypatch):
    monkeypatch.setenv("KOROS_ARC", "only-alpha")
    assert resolve_current_arc() == ("alpha", "only-alpha")


def test_env_project_slug_resolves(runs_tree, monkeypatch):
    monkeypatch.setenv("KOROS_ARC", "beta/shared")
    assert resolve_current_arc() == ("beta", "shared")


def test_env_wins_over_cwd(runs_tree, monkeypatch):
    # cwd is inside beta/only-beta, but KOROS_ARC names a different arc; env wins.
    monkeypatch.chdir(runs_tree / "beta" / "arcs" / "only-beta")
    monkeypatch.setenv("KOROS_ARC", "only-alpha")
    assert resolve_current_arc() == ("alpha", "only-alpha")


def test_unresolvable_env_falls_through_to_cwd(runs_tree, monkeypatch):
    # An ambiguous/unknown KOROS_ARC does not resolve, so resolution falls
    # through to the cwd branch rather than returning None outright.
    monkeypatch.chdir(runs_tree / "beta" / "arcs" / "only-beta")
    monkeypatch.setenv("KOROS_ARC", "no-such-arc")
    assert resolve_current_arc() == ("beta", "only-beta")


# ── precedence: cwd branch ─────────────────────────────────────────────────────


def test_cwd_inside_arc_resolves(runs_tree, monkeypatch):
    monkeypatch.chdir(runs_tree / "alpha" / "arcs" / "only-alpha")
    assert resolve_current_arc() == ("alpha", "only-alpha")


def test_cwd_below_arc_walks_upward(runs_tree, monkeypatch):
    # A working directory nested BELOW the arc dir still resolves by walking up.
    nested = runs_tree / "alpha" / "arcs" / "only-alpha" / "outputs" / "figures"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    assert resolve_current_arc() == ("alpha", "only-alpha")


def test_cwd_outside_tree_returns_none(runs_tree, monkeypatch, tmp_path):
    # A directory outside the runs tree resolves to nothing.
    outside = tmp_path / "somewhere-else"
    outside.mkdir()
    monkeypatch.chdir(outside)
    assert resolve_current_arc() is None


def test_cwd_at_project_that_is_itself_an_arc(runs_tree, monkeypatch):
    # gamma is a project dir that is itself an arc: project == arc == 'gamma'.
    monkeypatch.chdir(runs_tree / "gamma")
    assert resolve_current_arc() == ("gamma", "gamma")


def test_cwd_at_runs_root_returns_none(runs_tree, monkeypatch):
    # The runs root itself is not an arc.
    monkeypatch.chdir(runs_tree)
    assert resolve_current_arc() is None


def test_cwd_at_project_with_arcs_returns_none(runs_tree, monkeypatch):
    # A project directory that has an arcs/ subdir is NOT itself an arc.
    monkeypatch.chdir(runs_tree / "alpha")
    assert resolve_current_arc() is None


# ── refusals: never guess ──────────────────────────────────────────────────────


def test_unknown_explicit_slug_returns_none(runs_tree):
    assert resolve_current_arc("does-not-exist") is None


def test_unknown_explicit_project_slug_returns_none(runs_tree):
    assert resolve_current_arc("alpha/nope") is None
    assert resolve_current_arc("nope/only-alpha") is None


def test_ambiguous_bare_slug_refuses(runs_tree):
    # 'shared' exists under both alpha and beta; a bare slug must refuse rather
    # than pick one (arc slugs are unique per project, not globally).
    assert resolve_current_arc("shared") is None


def test_ambiguous_bare_slug_from_env_refuses(runs_tree, monkeypatch):
    monkeypatch.setenv("KOROS_ARC", "shared")
    # cwd is outside the tree, so env is the only signal; ambiguous env refuses.
    monkeypatch.chdir(runs_tree.parent)  # workspace root, outside runs tree
    assert resolve_current_arc() is None


def test_ambiguous_disambiguated_by_project(runs_tree):
    # The same ambiguous slug resolves once the project qualifies it.
    assert resolve_current_arc("alpha/shared") == ("alpha", "shared")
    assert resolve_current_arc("beta/shared") == ("beta", "shared")


# ── unresolvable runs root ─────────────────────────────────────────────────────


def test_unresolvable_runs_root_returns_none(monkeypatch, tmp_path):
    # No KOROS_HOME and no KING_KOROS_RUNS: resolve_runs_root raises, and
    # resolve_current_arc must swallow that and return None, not propagate.
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    assert resolve_current_arc() is None
    # Even with an explicit slug, an unresolvable root yields None.
    assert resolve_current_arc("anything") is None


def test_king_koros_runs_override(tmp_path, monkeypatch):
    # The KING override (KING_KOROS_RUNS) points directly at the runs dir.
    runs = tmp_path / "king-runs"
    _write_provenance(runs / "proj" / "arcs" / "arc1", project="proj")
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("KING_KOROS_RUNS", str(runs))
    assert resolve_current_arc("arc1") == ("proj", "arc1")


# ── project field from PROVENANCE.json ─────────────────────────────────────────


def test_project_field_is_read_from_provenance(tmp_path, monkeypatch):
    # When PROVENANCE.json carries a 'project' field, its value is returned as
    # the project (the spec resolves project by reading that field).
    runs = tmp_path / "workspace" / "runs"
    arc_dir = runs / "dirname" / "arcs" / "myarc"
    _write_provenance(arc_dir, project="canonical-project-name")
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("KOROS_HOME", str(tmp_path / "workspace"))
    assert resolve_current_arc("dirname/myarc") == (
        "canonical-project-name",
        "myarc",
    )


def test_project_falls_back_to_tree_when_field_absent(tmp_path, monkeypatch):
    # No 'project' field: fall back to the directory-name project.
    runs = tmp_path / "workspace" / "runs"
    arc_dir = runs / "dirname" / "arcs" / "myarc"
    _write_provenance(arc_dir, project=None)
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("KOROS_HOME", str(tmp_path / "workspace"))
    assert resolve_current_arc("dirname/myarc") == ("dirname", "myarc")


# ── warn_not_indexed ───────────────────────────────────────────────────────────


def test_warn_not_indexed_writes_one_line_to_stderr(capsys):
    warn_not_indexed("database is locked")
    captured = capsys.readouterr()
    assert captured.out == ""
    lines = captured.err.splitlines()
    assert len(lines) == 1
    assert "not indexed" in lines[0]
    assert "database is locked" in lines[0]


def test_warn_not_indexed_returns_none(capsys):
    assert warn_not_indexed("some reason") is None


# ── CAC identity (invariant I4) ────────────────────────────────────────────────


def test_app_id_is_hyphen_form():
    assert APP_ID == "models-and-analyses"


def test_app_us_is_derived_not_hardcoded():
    # I4: the underscore form is COMPUTED from the canonical hyphen id, never a
    # hand-chosen second spelling. Asserting the derivation (not a literal) is
    # exactly what I4 requires.
    assert app_us() == APP_ID.replace("-", "_")
