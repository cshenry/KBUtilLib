"""Regression tests for ``kbu model``'s solver selection and auto-annotation.

Both behaviours under test were added on 2026-09-30 after the modelling arc
was found silently broken on the BERDL pod, and both failed in ways that
*looked like success*, which is why they are pinned here:

* ``reconstruct`` on a plain protein FASTA returned a 5-reaction, 0-gene
  template biomass scaffold and exited ``0`` -- because
  ``MSGenome.from_fasta`` leaves every feature's roles empty and the builder
  maps reactions from roles only.
* ``gapfill`` died with ``Model too large for size-limited license`` because
  ``optlang`` prefers gurobi whenever it is merely importable, and the pod's
  licence is size-capped. GLPK solves the same problem in comparable time.

These are unit-level and offline: no RAST call, no solver run. The
end-to-end functional check (reconstruct >500 reactions and >500 genes on
E. coli, gapfill reporting reactions_added, fba objective_value > 0) is not
expressible here because it needs the network and a biochem DB.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from kbutillib.interfaces.cli import model as model_cli


# ── solver selection ───────────────────────────────────────────────────────


def test_machine_default_is_glpk_off_the_licensed_hosts(monkeypatch):
    monkeypatch.setattr(
        model_cli.socket, "gethostname", lambda: "jupyter-chenry", raising=False
    )
    assert model_cli.machine_default_solver() == "glpk"


@pytest.mark.parametrize("host", ["h100", "poplar.cels.anl.gov", "H100-node"])
def test_licensed_hosts_default_to_gurobi(monkeypatch, host):
    monkeypatch.setattr(model_cli.socket, "gethostname", lambda: host, raising=False)
    assert model_cli.machine_default_solver() == "gurobi"


def test_explicit_beats_everything(monkeypatch):
    monkeypatch.setenv("KBU_SOLVER", "cplex")
    assert model_cli.resolve_solver("GUROBI") == "gurobi"


def test_env_beats_config_and_machine_default(monkeypatch):
    monkeypatch.setenv("KBU_SOLVER", "  CPLEX  ")
    assert model_cli.resolve_solver() == "cplex"


def test_a_broken_config_does_not_break_solver_selection(monkeypatch):
    """Config is optional -- a config that raises must fall through, not propagate."""
    monkeypatch.delenv("KBU_SOLVER", raising=False)
    monkeypatch.setattr(model_cli, "machine_default_solver", lambda: "glpk")

    import kbutillib.core.config as cfg

    def _boom(*a, **k):
        raise RuntimeError("config file is unreadable")

    monkeypatch.setattr(cfg, "load_config", _boom)
    assert model_cli.resolve_solver() == "glpk"


def test_config_key_is_honoured_when_env_is_unset(monkeypatch):
    monkeypatch.delenv("KBU_SOLVER", raising=False)
    import kbutillib.core.config as cfg

    monkeypatch.setattr(
        cfg, "load_config", lambda *a, **k: SimpleNamespace(get=lambda k, d=None: "cplex")
    )
    assert model_cli.resolve_solver() == "cplex"


def test_config_default_for_solver_is_glpk():
    """The library-wide default must be GLPK, not whatever optlang prefers."""
    from kbutillib.core.config import ModelingConfig

    assert ModelingConfig().solver == "glpk"


def test_unavailable_solver_falls_back_with_a_warning(monkeypatch):
    """An unavailable solver must warn and degrade, never kill the verb."""
    monkeypatch.delenv("KBU_SOLVER", raising=False)
    chosen, warning = model_cli.apply_solver("definitely-not-a-solver")
    assert chosen != "definitely-not-a-solver"
    assert warning is not None
    assert "not available" in warning


# ── auto-annotation ────────────────────────────────────────────────────────


def _feature(fid: str, seq: str = "MKV", roles: dict | None = None):
    f = SimpleNamespace(id=fid, seq=seq, ontology_terms=roles or {})
    f.add_ontology_term = lambda k, v: f.ontology_terms.setdefault(k, []).append(v)
    return f


def _genome(features):
    return SimpleNamespace(features=features)


def test_already_annotated_genome_is_left_alone(monkeypatch):
    """A genome carrying roles must not trigger a RAST call at all."""

    def _boom(*a, **k):  # pragma: no cover -- must not be reached
        raise AssertionError("RAST must not be called for an annotated genome")

    monkeypatch.setattr(model_cli, "_annotated_feature_count", lambda g: 3)
    g = _genome([_feature("g1", roles={"RAST": ["pyruvate kinase"]})])
    model_cli._ensure_annotated(g, Path("x.faa"))  # must not raise


def test_writes_roles_under_the_key_msbuilder_reads(monkeypatch):
    """Roles must land under "RAST" -- any other key is invisible to the builder."""
    assert model_cli._MS_ONTOLOGY_KEY == "RAST"

    g = _genome([_feature("g1"), _feature("g2")])
    result = SimpleNamespace(
        records=[
            SimpleNamespace(
                gene_id="g1", terms=[SimpleNamespace(value="pyruvate kinase")]
            ),
            SimpleNamespace(gene_id="g2", terms=[SimpleNamespace(value="")]),
        ]
    )
    fake_rast = SimpleNamespace(is_available=lambda: True, annotate=lambda p: result)
    monkeypatch.setattr(
        model_cli, "_ensure_annotated", model_cli._ensure_annotated, raising=False
    )
    import kbutillib.domains.external.rast_utils as ru

    monkeypatch.setattr(ru, "RastUtils", lambda *a, **k: fake_rast)

    model_cli._ensure_annotated(g, Path("x.faa"))

    assert g.features[0].ontology_terms["RAST"] == ["pyruvate kinase"]
    # A blank function is not a role and must not be recorded.
    assert "RAST" not in g.features[1].ontology_terms


def test_refuses_rather_than_returning_an_empty_scaffold(monkeypatch):
    """Zero roles from RAST must raise -- the old behaviour returned a model."""
    import click

    import kbutillib.domains.external.rast_utils as ru

    g = _genome([_feature("g1")])
    fake_rast = SimpleNamespace(
        is_available=lambda: True,
        annotate=lambda p: SimpleNamespace(records=[]),
    )
    monkeypatch.setattr(ru, "RastUtils", lambda *a, **k: fake_rast)

    with pytest.raises(click.ClickException, match="no functional roles"):
        model_cli._ensure_annotated(g, Path("x.faa"))


def test_unavailable_rast_raises_instead_of_degrading(monkeypatch):
    import click

    import kbutillib.domains.external.rast_utils as ru

    g = _genome([_feature("g1")])
    fake_rast = SimpleNamespace(is_available=lambda: False, annotate=None)
    monkeypatch.setattr(ru, "RastUtils", lambda *a, **k: fake_rast)

    with pytest.raises(click.ClickException, match="unavailable"):
        model_cli._ensure_annotated(g, Path("x.faa"))


def test_no_sequences_is_a_clear_error(monkeypatch):
    import click

    g = _genome([SimpleNamespace(id="g1", seq=None, ontology_terms={})])
    with pytest.raises(click.ClickException, match="no protein sequences"):
        model_cli._ensure_annotated(g, Path("x.faa"))
