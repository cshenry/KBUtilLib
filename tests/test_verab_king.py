"""S6 unit tests — KING coscientist artifact emission (king_artifacts.py).

Tests run without any optional dependency (RDKit, minedatabase):
  * emit_king_workflow writes all expected files into a tmp_path.
  * seeds.tsv contains the id/smiles rows for all 5 canonical seed compounds.
  * manifest.json parses correctly and lists the expected operator(s).
  * prompt.md mentions ``kbu verab discover`` and all 5 compound names.
  * No top-level RDKit or minedatabase import in king_artifacts.py.

No test here requires RDKit or a live database.  The synthetic
VerabDiscoveryResult is built directly from models.py (stdlib only).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kbutillib.cheminformatics.verab.models import VerabDiscoveryResult, VerabRuleMatch
from kbutillib.cheminformatics.verab.smarts import SEED_COMPOUNDS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_synthetic_discovery() -> VerabDiscoveryResult:
    """Return a minimal VerabDiscoveryResult with one operator match."""
    match = VerabRuleMatch(
        operator="ruleXXXX",
        reaction_id="rxn_verab_001",
        backend="pickaxe",
        reactant_ids=["cpd_vanillate"],
        product_ids=["cpd_protocatechuate", "cpd_formaldehyde"],
        method="rdkit_transform",
        confidence=1.0,
        ec_hint="1.14.13.82",
    )
    return VerabDiscoveryResult(
        rule_set="metacyc_generalized",
        generations=1,
        seeds=list(SEED_COMPOUNDS),
        matches=[match],
        operators=["ruleXXXX"],
        expansion_summary={"n_compounds": 42, "n_reactions": 17, "warnings": []},
        warnings=[],
    )


# ---------------------------------------------------------------------------
# Import safety: king_artifacts.py must be importable without any optional dep
# ---------------------------------------------------------------------------


def test_import_king_artifacts_module():
    """king_artifacts.py must be importable without RDKit or minedatabase."""
    from kbutillib.cheminformatics.verab import king_artifacts  # noqa: F401


def test_king_artifacts_no_toplevel_rdkit_import():
    """king_artifacts.py must NOT import rdkit or minedatabase at module level."""
    import kbutillib.cheminformatics.verab.king_artifacts as ka_mod

    module_dict = vars(ka_mod)
    assert "rdkit" not in module_dict, "rdkit imported at module level in king_artifacts.py"
    assert "Chem" not in module_dict, "rdkit.Chem imported at module level in king_artifacts.py"
    assert "minedatabase" not in module_dict, "minedatabase imported at module level in king_artifacts.py"


def test_emit_king_workflow_importable_from_package():
    """emit_king_workflow must be accessible from the top-level verab package."""
    from kbutillib.cheminformatics.verab import emit_king_workflow  # noqa: F401


# ---------------------------------------------------------------------------
# Core emission tests
# ---------------------------------------------------------------------------


def test_emit_king_workflow_all_files_exist(tmp_path: Path):
    """emit_king_workflow writes all 6 expected artifact files."""
    from kbutillib.cheminformatics.verab.king_artifacts import emit_king_workflow

    discovery = _build_synthetic_discovery()
    result = emit_king_workflow(tmp_path, discovery, SEED_COMPOUNDS)

    expected_files = {
        "seeds.tsv",
        "seeds.csv",
        "discovered_rules.tsv",
        "target_transformation.txt",
        "prompt.md",
        "manifest.json",
    }
    for fname in expected_files:
        fpath = tmp_path / fname
        assert fpath.exists(), f"Expected artifact file not found: {fname}"
        assert fpath.stat().st_size > 0, f"Artifact file is empty: {fname}"

    # The returned dict's "files" mapping must also contain all keys
    assert expected_files == set(result["files"].keys())


def test_emit_king_workflow_creates_outdir_if_absent(tmp_path: Path):
    """emit_king_workflow creates the output directory when it does not exist."""
    from kbutillib.cheminformatics.verab.king_artifacts import emit_king_workflow

    target = tmp_path / "new_subdir" / "king_run"
    assert not target.exists()
    discovery = _build_synthetic_discovery()
    emit_king_workflow(target, discovery, SEED_COMPOUNDS)
    assert target.is_dir()


def test_emit_king_workflow_returns_summary(tmp_path: Path):
    """emit_king_workflow returns a dict with outdir, files, n_operators, n_seeds."""
    from kbutillib.cheminformatics.verab.king_artifacts import emit_king_workflow

    discovery = _build_synthetic_discovery()
    result = emit_king_workflow(tmp_path, discovery, SEED_COMPOUNDS)

    assert "outdir" in result
    assert "files" in result
    assert "n_operators" in result
    assert "n_seeds" in result
    assert result["n_operators"] == 1
    assert result["n_seeds"] == 5


# ---------------------------------------------------------------------------
# seeds.tsv content tests
# ---------------------------------------------------------------------------


def test_seeds_tsv_has_header_and_all_seeds(tmp_path: Path):
    """seeds.tsv must have a header row and one data row per seed compound."""
    from kbutillib.cheminformatics.verab.king_artifacts import emit_king_workflow

    discovery = _build_synthetic_discovery()
    emit_king_workflow(tmp_path, discovery, SEED_COMPOUNDS)

    lines = (tmp_path / "seeds.tsv").read_text(encoding="utf-8").strip().splitlines()
    assert lines[0] == "id\tsmiles", f"Unexpected header: {lines[0]!r}"
    # One data row per seed (5 seeds)
    data_rows = lines[1:]
    assert len(data_rows) == len(SEED_COMPOUNDS), (
        f"Expected {len(SEED_COMPOUNDS)} seed rows, got {len(data_rows)}"
    )
    # Every data row must have exactly 2 tab-separated fields
    for row in data_rows:
        parts = row.split("\t")
        assert len(parts) == 2, f"Malformed row in seeds.tsv: {row!r}"


def test_seeds_tsv_contains_all_seed_ids(tmp_path: Path):
    """seeds.tsv must contain the id of every seed compound."""
    from kbutillib.cheminformatics.verab.king_artifacts import emit_king_workflow

    discovery = _build_synthetic_discovery()
    emit_king_workflow(tmp_path, discovery, SEED_COMPOUNDS)

    content = (tmp_path / "seeds.tsv").read_text(encoding="utf-8")
    for seed in SEED_COMPOUNDS:
        assert seed["id"] in content, f"Seed id {seed['id']!r} not found in seeds.tsv"


def test_seeds_tsv_contains_all_seed_smiles(tmp_path: Path):
    """seeds.tsv must contain the SMILES of every seed compound."""
    from kbutillib.cheminformatics.verab.king_artifacts import emit_king_workflow

    discovery = _build_synthetic_discovery()
    emit_king_workflow(tmp_path, discovery, SEED_COMPOUNDS)

    content = (tmp_path / "seeds.tsv").read_text(encoding="utf-8")
    for seed in SEED_COMPOUNDS:
        assert seed["smiles"] in content, (
            f"Seed SMILES for {seed['id']!r} not found in seeds.tsv"
        )


# ---------------------------------------------------------------------------
# seeds.csv content tests
# ---------------------------------------------------------------------------


def test_seeds_csv_has_header_and_all_seeds(tmp_path: Path):
    """seeds.csv must have a header row and one data row per seed compound."""
    from kbutillib.cheminformatics.verab.king_artifacts import emit_king_workflow

    discovery = _build_synthetic_discovery()
    emit_king_workflow(tmp_path, discovery, SEED_COMPOUNDS)

    lines = (tmp_path / "seeds.csv").read_text(encoding="utf-8").strip().splitlines()
    assert lines[0] == "id,smiles", f"Unexpected header: {lines[0]!r}"
    data_rows = lines[1:]
    assert len(data_rows) == len(SEED_COMPOUNDS)


# ---------------------------------------------------------------------------
# discovered_rules.tsv content tests
# ---------------------------------------------------------------------------


def test_discovered_rules_tsv_has_header(tmp_path: Path):
    """discovered_rules.tsv must have the expected header."""
    from kbutillib.cheminformatics.verab.king_artifacts import emit_king_workflow

    discovery = _build_synthetic_discovery()
    emit_king_workflow(tmp_path, discovery, SEED_COMPOUNDS)

    lines = (tmp_path / "discovered_rules.tsv").read_text(encoding="utf-8").strip().splitlines()
    header = lines[0]
    for col in ("operator", "ec_hint", "confidence", "method", "reaction_id"):
        assert col in header, f"Column {col!r} missing from discovered_rules.tsv header"


def test_discovered_rules_tsv_contains_operator(tmp_path: Path):
    """discovered_rules.tsv must list the firing operator."""
    from kbutillib.cheminformatics.verab.king_artifacts import emit_king_workflow

    discovery = _build_synthetic_discovery()
    emit_king_workflow(tmp_path, discovery, SEED_COMPOUNDS)

    content = (tmp_path / "discovered_rules.tsv").read_text(encoding="utf-8")
    assert "ruleXXXX" in content


# ---------------------------------------------------------------------------
# manifest.json content tests
# ---------------------------------------------------------------------------


def test_manifest_json_is_valid_json(tmp_path: Path):
    """manifest.json must be parseable JSON."""
    from kbutillib.cheminformatics.verab.king_artifacts import emit_king_workflow

    discovery = _build_synthetic_discovery()
    emit_king_workflow(tmp_path, discovery, SEED_COMPOUNDS)

    raw = (tmp_path / "manifest.json").read_text(encoding="utf-8")
    manifest = json.loads(raw)
    assert isinstance(manifest, dict)


def test_manifest_json_required_keys(tmp_path: Path):
    """manifest.json must contain all required provenance keys."""
    from kbutillib.cheminformatics.verab.king_artifacts import emit_king_workflow

    discovery = _build_synthetic_discovery()
    emit_king_workflow(tmp_path, discovery, SEED_COMPOUNDS)

    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    required_keys = {
        "tool", "version", "rule_set", "generations",
        "seeds", "operators", "created", "git_sha", "inputs",
    }
    for k in required_keys:
        assert k in manifest, f"Required key {k!r} missing from manifest.json"


def test_manifest_json_lists_operators(tmp_path: Path):
    """manifest.json operators field must list the firing operator(s)."""
    from kbutillib.cheminformatics.verab.king_artifacts import emit_king_workflow

    discovery = _build_synthetic_discovery()
    emit_king_workflow(tmp_path, discovery, SEED_COMPOUNDS)

    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert isinstance(manifest["operators"], list)
    assert "ruleXXXX" in manifest["operators"]


def test_manifest_json_seeds_count(tmp_path: Path):
    """manifest.json seeds field must have one entry per seed compound."""
    from kbutillib.cheminformatics.verab.king_artifacts import emit_king_workflow

    discovery = _build_synthetic_discovery()
    emit_king_workflow(tmp_path, discovery, SEED_COMPOUNDS)

    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["seeds"]) == len(SEED_COMPOUNDS)


def test_manifest_json_tool_name(tmp_path: Path):
    """manifest.json tool field must identify the kbu verab tool."""
    from kbutillib.cheminformatics.verab.king_artifacts import emit_king_workflow

    discovery = _build_synthetic_discovery()
    emit_king_workflow(tmp_path, discovery, SEED_COMPOUNDS)

    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert "kbu" in manifest["tool"], f"Expected 'kbu' in tool name, got {manifest['tool']!r}"
    assert "verab" in manifest["tool"], f"Expected 'verab' in tool name, got {manifest['tool']!r}"


# ---------------------------------------------------------------------------
# prompt.md content tests
# ---------------------------------------------------------------------------


def test_prompt_md_mentions_kbu_verab_discover(tmp_path: Path):
    """prompt.md must mention the ``kbu verab discover`` command."""
    from kbutillib.cheminformatics.verab.king_artifacts import emit_king_workflow

    discovery = _build_synthetic_discovery()
    emit_king_workflow(tmp_path, discovery, SEED_COMPOUNDS)

    content = (tmp_path / "prompt.md").read_text(encoding="utf-8")
    assert "kbu verab discover" in content, (
        "prompt.md does not mention 'kbu verab discover'"
    )


def test_prompt_md_mentions_all_5_compound_names(tmp_path: Path):
    """prompt.md must mention all 5 canonical seed compound names."""
    from kbutillib.cheminformatics.verab.king_artifacts import emit_king_workflow

    discovery = _build_synthetic_discovery()
    emit_king_workflow(tmp_path, discovery, SEED_COMPOUNDS)

    content = (tmp_path / "prompt.md").read_text(encoding="utf-8")
    expected_names = [
        "vanillate",
        "isovanillate",
        "guaiacol",
        "4-methoxybenzoate",
        "veratrate",
    ]
    for name in expected_names:
        assert name in content, f"Compound name {name!r} not found in prompt.md"


def test_prompt_md_is_markdown(tmp_path: Path):
    """prompt.md must start with a Markdown heading."""
    from kbutillib.cheminformatics.verab.king_artifacts import emit_king_workflow

    discovery = _build_synthetic_discovery()
    emit_king_workflow(tmp_path, discovery, SEED_COMPOUNDS)

    content = (tmp_path / "prompt.md").read_text(encoding="utf-8")
    assert content.startswith("#"), "prompt.md must start with a Markdown heading"


# ---------------------------------------------------------------------------
# target_transformation.txt content tests
# ---------------------------------------------------------------------------


def test_target_transformation_contains_smarts(tmp_path: Path):
    """target_transformation.txt must contain the VERAB_ODEMETHYLATION_SMARTS."""
    from kbutillib.cheminformatics.verab.king_artifacts import emit_king_workflow
    from kbutillib.cheminformatics.verab.smarts import VERAB_ODEMETHYLATION_SMARTS

    discovery = _build_synthetic_discovery()
    emit_king_workflow(tmp_path, discovery, SEED_COMPOUNDS)

    content = (tmp_path / "target_transformation.txt").read_text(encoding="utf-8")
    assert VERAB_ODEMETHYLATION_SMARTS in content


def test_target_transformation_contains_ec(tmp_path: Path):
    """target_transformation.txt must reference the EC number 1.14.13.82."""
    from kbutillib.cheminformatics.verab.king_artifacts import emit_king_workflow

    discovery = _build_synthetic_discovery()
    emit_king_workflow(tmp_path, discovery, SEED_COMPOUNDS)

    content = (tmp_path / "target_transformation.txt").read_text(encoding="utf-8")
    assert "1.14.13.82" in content


# ---------------------------------------------------------------------------
# Idempotency test (second call overwrites, no crash)
# ---------------------------------------------------------------------------


def test_emit_king_workflow_idempotent(tmp_path: Path):
    """Calling emit_king_workflow twice on the same outdir must not raise."""
    from kbutillib.cheminformatics.verab.king_artifacts import emit_king_workflow

    discovery = _build_synthetic_discovery()
    emit_king_workflow(tmp_path, discovery, SEED_COMPOUNDS)
    # Second call should overwrite silently
    emit_king_workflow(tmp_path, discovery, SEED_COMPOUNDS)

    # Files should still exist and be valid
    assert (tmp_path / "manifest.json").exists()
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert "operators" in manifest


# ---------------------------------------------------------------------------
# Empty operators case (discovery with no matches)
# ---------------------------------------------------------------------------


def test_emit_king_workflow_no_operators(tmp_path: Path):
    """emit_king_workflow handles a discovery result with no matching operators."""
    from kbutillib.cheminformatics.verab.king_artifacts import emit_king_workflow

    empty_discovery = VerabDiscoveryResult(
        rule_set="metacyc_generalized",
        generations=1,
        seeds=list(SEED_COMPOUNDS),
        matches=[],
        operators=[],
        expansion_summary={"n_compounds": 5, "n_reactions": 0, "warnings": []},
        warnings=["no matches found"],
    )
    emit_king_workflow(tmp_path, empty_discovery, SEED_COMPOUNDS)

    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["operators"] == []

    # seeds.tsv should still have all 5 seeds
    lines = (tmp_path / "seeds.tsv").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1 + len(SEED_COMPOUNDS)  # header + 5 data rows


# ---------------------------------------------------------------------------
# Multiple operators case
# ---------------------------------------------------------------------------


def test_emit_king_workflow_multiple_operators(tmp_path: Path):
    """emit_king_workflow lists all operators when there are multiple matches."""
    from kbutillib.cheminformatics.verab.king_artifacts import emit_king_workflow

    match_a = VerabRuleMatch(
        operator="ruleAAAA",
        reaction_id="rxn_001",
        backend="pickaxe",
        method="rdkit_transform",
        confidence=1.0,
        ec_hint="1.14.13.82",
    )
    match_b = VerabRuleMatch(
        operator="ruleBBBB",
        reaction_id="rxn_002",
        backend="pickaxe",
        method="smarts_text",
        confidence=0.5,
        ec_hint="1.14.13.82",
    )
    multi_discovery = VerabDiscoveryResult(
        rule_set="metacyc_generalized",
        generations=1,
        seeds=list(SEED_COMPOUNDS),
        matches=[match_a, match_b],
        operators=["ruleAAAA", "ruleBBBB"],
        expansion_summary={"n_compounds": 10, "n_reactions": 5, "warnings": []},
        warnings=[],
    )
    emit_king_workflow(tmp_path, multi_discovery, SEED_COMPOUNDS)

    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert "ruleAAAA" in manifest["operators"]
    assert "ruleBBBB" in manifest["operators"]

    rules_content = (tmp_path / "discovered_rules.tsv").read_text(encoding="utf-8")
    assert "ruleAAAA" in rules_content
    assert "ruleBBBB" in rules_content


# ---------------------------------------------------------------------------
# Default seeds (seeds=None uses SEED_COMPOUNDS)
# ---------------------------------------------------------------------------


def test_emit_king_workflow_default_seeds(tmp_path: Path):
    """emit_king_workflow defaults to SEED_COMPOUNDS when seeds=None."""
    from kbutillib.cheminformatics.verab.king_artifacts import emit_king_workflow

    discovery = _build_synthetic_discovery()
    result = emit_king_workflow(tmp_path, discovery, seeds=None)

    assert result["n_seeds"] == len(SEED_COMPOUNDS)
    # seeds.tsv should contain all 5
    content = (tmp_path / "seeds.tsv").read_text(encoding="utf-8")
    for seed in SEED_COMPOUNDS:
        assert seed["id"] in content
