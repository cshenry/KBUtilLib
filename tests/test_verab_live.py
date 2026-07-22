"""Gated live integration test for verAB O-demethylation rule discovery.

This test runs the **real** 5-seed ``discover_verab_rules`` pipeline against a
real MINE-Database / Pickaxe rule data directory.  It is skipped automatically
(never errors or fails) when either required dependency is absent:

1. **RDKit** must be importable (``rdkit`` package on sys.path).
2. **MINE-Database rule data** must be available — resolved via the same
   multi-step search that :class:`kbutillib.cheminformatics.PickaxeBackend`
   uses internally:

   a. ``KBUTILLIB_PICKAXE_DATA_DIR`` environment variable pointing at the
      ``minedatabase/data/`` directory.
   b. Config key ``cheminformatics.pickaxe.data_dir`` (not checked here; the
      backend probes it).
   c. The installed ``minedatabase`` package's own bundled ``data/`` directory.
   d. DependencyManager path for the ``"MINE-Database"`` checkout
      (declared in ``dependencies.yaml``).

   Additionally, **minedatabase** (Pickaxe) itself must be importable.

Opt-in
------
Enable with the following env var gate — analogous to ``KBASE_LIVE_TESTS=1``::

    KBUTILLIB_LIVE_CHEM=1 pytest tests/test_verab_live.py -v

Without ``KBUTILLIB_LIVE_CHEM=1`` the entire module is collected but every
test is skipped with a clear reason.

Design risk validated
---------------------
This test empirically confirms **Design Risk R2** from the architecture plan:
the mechanism-informed Pickaxe operator set reproduces verAB O-demethylation
activity (≥1 operator fires on the five canonical seed compounds).
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from typing import Optional

import pytest

# ---------------------------------------------------------------------------
# Gating helpers — evaluated at collection time, zero imports of heavy deps
# ---------------------------------------------------------------------------

_LIVE_CHEM_ENABLED: bool = os.environ.get("KBUTILLIB_LIVE_CHEM") == "1"

# Fast checks that do NOT import rdkit or minedatabase (find_spec is safe).
_RDKIT_IMPORTABLE: bool = importlib.util.find_spec("rdkit") is not None
_MINEDATABASE_IMPORTABLE: bool = importlib.util.find_spec("minedatabase") is not None


def _pickaxe_data_dir_available() -> bool:
    """Return True iff a valid Pickaxe rule data directory can be found.

    Mirrors the resolution order used by :meth:`PickaxeBackend._resolve_data_dir`
    but does NOT instantiate that class (to avoid side effects at collection
    time).  The sentinel file we check is the default rule set's TSV.
    """

    def _valid(d: Path) -> bool:
        return (d / "metacyc_rules" / "metacyc_generalized_rules.tsv").is_file() or (
            d / "original_rules" / "EnzymaticReactionRules.tsv"
        ).is_file()

    candidates: list[Path] = []

    # 1. Env var (highest priority, analogous to KBUTILLIB_RETRORULES_TSV).
    env_dir = os.environ.get("KBUTILLIB_PICKAXE_DATA_DIR")
    if env_dir:
        candidates.append(Path(env_dir).expanduser())

    # 2. The installed minedatabase package's own data/ directory.
    if _MINEDATABASE_IMPORTABLE:
        try:
            import minedatabase  # noqa: F401  — only executed when find_spec passed

            mod_file = getattr(minedatabase, "__file__", None)
            if mod_file:
                candidates.append(Path(mod_file).resolve().parent / "data")
        except Exception:
            pass

    # 3. DependencyManager MINE-Database checkout.
    try:
        import sys

        # Avoid modifying sys.path at collection time; just probe the dep path.
        from kbutillib.dependency_manager import get_dependency_path

        dep = get_dependency_path("MINE-Database")
        if dep:
            candidates.append(Path(dep) / "minedatabase" / "data")
    except Exception:
        pass

    for cand in candidates:
        try:
            if cand.is_dir() and _valid(cand):
                return True
        except OSError:
            continue
    return False


# Compute once at import time for use in @pytest.mark.skipif
_PICKAXE_DATA_AVAILABLE: bool = _pickaxe_data_dir_available()

#: Combined gating reason (shown by pytest -v when skipped)
_SKIP_REASON_LIVE_GATE = (
    "Live chem tests disabled — set KBUTILLIB_LIVE_CHEM=1 to enable"
)
_SKIP_REASON_RDKIT = "rdkit not importable (conda install -c conda-forge rdkit)"
_SKIP_REASON_MINEDB = (
    "minedatabase/Pickaxe not importable or rule data absent "
    "(set KBUTILLIB_PICKAXE_DATA_DIR to .../minedatabase/data, "
    "or declare MINE-Database in dependencies.yaml)"
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pickaxe_backend():
    """Return a live :class:`PickaxeBackend` or skip the module."""
    from kbutillib.cheminformatics import PickaxeBackend

    p = PickaxeBackend()
    if not p.available:
        pytest.skip(p.unavailable_reason or "PickaxeBackend unavailable")
    return p


@pytest.fixture(scope="module")
def live_network_expansion(pickaxe_backend):
    """Return a :class:`NetworkExpansionUtils` wired to use the live Pickaxe backend."""
    from kbutillib.network_expansion_utils import NetworkExpansionUtils

    ne = NetworkExpansionUtils(config_file=False, token_file=None, kbase_token_file=None)
    ne._backends["pickaxe"] = pickaxe_backend
    return ne


@pytest.fixture(scope="module")
def live_discovery(live_network_expansion):
    """Run the real 5-seed discover_verab_rules once and cache the result."""
    from kbutillib.cheminformatics.verab.rule_discovery import discover_verab_rules

    return discover_verab_rules(
        live_network_expansion,
        generations=1,
        rule_set="metacyc_generalized",
        backend="pickaxe",
    )


# ---------------------------------------------------------------------------
# Guard decorator — skip the whole module unless BOTH gates pass
# ---------------------------------------------------------------------------

_need_live_chem = pytest.mark.skipif(
    not _LIVE_CHEM_ENABLED,
    reason=_SKIP_REASON_LIVE_GATE,
)
_need_rdkit = pytest.mark.skipif(
    not _RDKIT_IMPORTABLE,
    reason=_SKIP_REASON_RDKIT,
)
_need_minedb = pytest.mark.skipif(
    not (_MINEDATABASE_IMPORTABLE and _PICKAXE_DATA_AVAILABLE),
    reason=_SKIP_REASON_MINEDB,
)


def _full_skip(*markers):
    """Apply multiple skipif markers (all must pass)."""
    import functools

    def decorator(fn):
        return functools.reduce(lambda f, m: m(f), reversed(markers), fn)

    return decorator


# ---------------------------------------------------------------------------
# Live integration tests
# ---------------------------------------------------------------------------


@_full_skip(_need_live_chem, _need_rdkit, _need_minedb)
@pytest.mark.integration
def test_live_discovery_runs(live_discovery):
    """discover_verab_rules completes without exception on the 5 real seeds."""
    from kbutillib.cheminformatics.verab.models import VerabDiscoveryResult

    assert isinstance(live_discovery, VerabDiscoveryResult)
    assert live_discovery.rule_set == "metacyc_generalized"
    assert live_discovery.generations == 1
    assert len(live_discovery.seeds) == 5


@_full_skip(_need_live_chem, _need_rdkit, _need_minedb)
@pytest.mark.integration
def test_live_expansion_produced_reactions(live_discovery):
    """The Pickaxe expansion produces at least 1 predicted reaction."""
    summary = live_discovery.expansion_summary
    assert summary.get("n_reactions", 0) >= 1, (
        f"No reactions produced. Expansion summary: {summary}. "
        f"Warnings: {live_discovery.warnings}"
    )


@_full_skip(_need_live_chem, _need_rdkit, _need_minedb)
@pytest.mark.integration
def test_live_at_least_one_verab_operator(live_discovery):
    """≥1 operator matches verAB O-demethylation (empirical confirmation of R2).

    This is the core assertion: the mechanism-informed Pickaxe metacyc_generalized
    rule set must fire on at least one of the five canonical verAB substrate seeds
    (vanillate, isovanillate, guaiacol, 4-methoxybenzoate, veratrate) and produce
    a match classified as verAB-positive (aromatic-methoxy reactant + phenol
    product + formaldehyde product).
    """
    assert len(live_discovery.operators) >= 1, (
        "No verAB O-demethylation operator found among all reactions. "
        f"Total reactions: {live_discovery.expansion_summary.get('n_reactions', '?')}. "
        f"Matches: {live_discovery.matches}. "
        f"Warnings: {live_discovery.warnings}"
    )


@_full_skip(_need_live_chem, _need_rdkit, _need_minedb)
@pytest.mark.integration
def test_live_operator_matches_have_high_confidence(live_discovery):
    """All RDKit-confirmed matches carry confidence=1.0 and method='rdkit_transform'."""
    for match in live_discovery.matches:
        assert match.confidence == 1.0, (
            f"Operator {match.operator} has confidence {match.confidence} "
            f"(expected 1.0 for RDKit-confirmed match). method={match.method}"
        )
        assert match.method == "rdkit_transform", (
            f"Expected method='rdkit_transform', got '{match.method}' "
            f"for operator {match.operator}"
        )


@_full_skip(_need_live_chem, _need_rdkit, _need_minedb)
@pytest.mark.integration
def test_live_operator_has_ec_hint(live_discovery):
    """Every match carries the EC 1.14.13.82 hint (vanillate monooxygenase)."""
    for match in live_discovery.matches:
        assert match.ec_hint == "1.14.13.82", (
            f"Operator {match.operator} missing ec_hint '1.14.13.82', "
            f"got '{match.ec_hint}'"
        )


@_full_skip(_need_live_chem, _need_rdkit, _need_minedb)
@pytest.mark.integration
def test_live_king_artifact_emission(live_discovery, tmp_path):
    """emit_king_workflow writes all required files; manifest.json lists operators."""
    from kbutillib.cheminformatics.verab.king_artifacts import emit_king_workflow

    summary = emit_king_workflow(tmp_path / "king_run", live_discovery)

    # All expected artifact files must exist
    expected_files = [
        "seeds.tsv",
        "seeds.csv",
        "discovered_rules.tsv",
        "target_transformation.txt",
        "prompt.md",
        "manifest.json",
    ]
    for fname in expected_files:
        fpath = Path(summary["files"][fname])
        assert fpath.is_file(), f"Expected artifact not written: {fname}"

    # manifest.json must parse and include the operators
    manifest_path = Path(summary["files"]["manifest.json"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert "operators" in manifest
    assert isinstance(manifest["operators"], list)
    assert len(manifest["operators"]) >= 1, (
        f"manifest.json 'operators' is empty: {manifest['operators']}"
    )

    # Every operator found in discovery must appear in the manifest
    for op in live_discovery.operators:
        assert op in manifest["operators"], (
            f"Operator '{op}' from discovery not present in manifest.json"
        )


@_full_skip(_need_live_chem, _need_rdkit, _need_minedb)
@pytest.mark.integration
def test_live_king_seeds_tsv_content(live_discovery, tmp_path):
    """seeds.tsv has a header row and one data row per seed compound."""
    from kbutillib.cheminformatics.verab.king_artifacts import emit_king_workflow

    summary = emit_king_workflow(tmp_path / "king_seeds_check", live_discovery)
    seeds_path = Path(summary["files"]["seeds.tsv"])
    lines = seeds_path.read_text(encoding="utf-8").strip().splitlines()

    # First line is header
    assert lines[0] == "id\tsmiles", f"Unexpected seeds.tsv header: {lines[0]!r}"
    # 5 seed data rows
    data_rows = lines[1:]
    assert len(data_rows) == 5, f"Expected 5 seed rows, got {len(data_rows)}"
    for row in data_rows:
        parts = row.split("\t")
        assert len(parts) == 2, f"Expected 2 tab-separated fields, got: {row!r}"
        compound_id, smiles = parts
        assert compound_id.strip(), f"Empty compound id in seeds.tsv row: {row!r}"
        assert smiles.strip(), f"Empty SMILES in seeds.tsv row: {row!r}"


@_full_skip(_need_live_chem, _need_rdkit, _need_minedb)
@pytest.mark.integration
def test_live_verab_utils_facade(live_network_expansion):
    """VerabUtils.discover_rules delegates to the live expander and returns a result."""
    from kbutillib.verab_utils import VerabUtils

    vu = VerabUtils(network_expansion=live_network_expansion)
    result = vu.discover_rules(generations=1, rule_set="metacyc_generalized")

    from kbutillib.cheminformatics.verab.models import VerabDiscoveryResult

    assert isinstance(result, VerabDiscoveryResult)
    # At least the expansion ran (even if no verAB operator fired on this run)
    assert result.expansion_summary.get("n_compounds", 0) >= 5  # at least seeds
