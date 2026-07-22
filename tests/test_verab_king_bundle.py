"""S9 tests — verAB KING app bundle validation.

Verifies that the ``king_app_verab`` bundle directory passes
``king_install.load_bundle`` schema validation without raising
``BundleError``, and that the probe/verify schema is correct.

No live CLI or network access.  Runs fully offline.
"""

from __future__ import annotations

from pathlib import Path

import kbutillib
from kbutillib.king_install import BundleError, load_bundle


# ---------------------------------------------------------------------------
# Fixture: resolve bundle dir from the installed package
# ---------------------------------------------------------------------------

BUNDLE_DIR: Path = Path(kbutillib.__file__).parent / "king_app_verab"


# ---------------------------------------------------------------------------
# Load / schema validation
# ---------------------------------------------------------------------------


def test_load_bundle_does_not_raise():
    """load_bundle(king_app_verab) must succeed without raising BundleError."""
    result = load_bundle(BUNDLE_DIR)
    assert isinstance(result, dict), "load_bundle must return a dict"
    assert "bundle" in result, "load_bundle result must contain 'bundle' key"
    assert "skill_md" in result, "load_bundle result must contain 'skill_md' key"


def test_bundle_id_matches_verab():
    """bundle.json 'id' must be 'kbutillib-verab'."""
    result = load_bundle(BUNDLE_DIR)
    assert result["bundle"]["id"] == "kbutillib-verab"


def test_bundle_required_fields_present():
    """bundle.json must contain all required fields: id, title, description, cli."""
    result = load_bundle(BUNDLE_DIR)
    bundle = result["bundle"]
    for field in ("id", "title", "description", "cli"):
        assert field in bundle, f"Required field '{field}' missing from bundle.json"
        assert bundle[field], f"Required field '{field}' must be non-empty"


def test_bundle_cli_is_kbu():
    """bundle.json 'cli' must be 'kbu'."""
    result = load_bundle(BUNDLE_DIR)
    assert result["bundle"]["cli"] == "kbu"


# ---------------------------------------------------------------------------
# Verify / probe schema
# ---------------------------------------------------------------------------


def test_bundle_verify_schema_present():
    """bundle.json must contain a 'verify' dict with 'cmd' and 'ok_text'."""
    result = load_bundle(BUNDLE_DIR)
    bundle = result["bundle"]
    assert "verify" in bundle, "bundle.json must contain 'verify' field"
    verify = bundle["verify"]
    assert isinstance(verify, dict), "'verify' must be a dict"
    assert "cmd" in verify, "'verify' must contain 'cmd'"
    assert isinstance(verify["cmd"], list), "'verify.cmd' must be a list"
    assert len(verify["cmd"]) > 0, "'verify.cmd' must be non-empty"


def test_bundle_verify_cmd_starts_with_kbu_verab():
    """verify.cmd must invoke 'kbu verab ...'."""
    result = load_bundle(BUNDLE_DIR)
    verify = result["bundle"]["verify"]
    cmd = verify["cmd"]
    assert cmd[0] == "kbu", f"verify.cmd[0] must be 'kbu', got {cmd[0]!r}"
    assert cmd[1] == "verab", f"verify.cmd[1] must be 'verab', got {cmd[1]!r}"


def test_bundle_verify_ok_text_is_verab():
    """verify.ok_text must be 'verAB' (matches the --help output)."""
    result = load_bundle(BUNDLE_DIR)
    verify = result["bundle"]["verify"]
    assert "ok_text" in verify, "'verify' must contain 'ok_text'"
    assert verify["ok_text"] == "verAB", (
        f"verify.ok_text must be 'verAB', got {verify['ok_text']!r}"
    )


# ---------------------------------------------------------------------------
# skill.md presence and content
# ---------------------------------------------------------------------------


def test_skill_md_is_non_empty():
    """skill.md must be present and non-empty."""
    result = load_bundle(BUNDLE_DIR)
    skill_md = result["skill_md"]
    assert isinstance(skill_md, str), "skill_md must be a string"
    assert len(skill_md.strip()) > 0, "skill.md must not be empty"


def test_skill_md_mentions_kbu_verab_verbs():
    """skill.md must document all four verAB verbs."""
    result = load_bundle(BUNDLE_DIR)
    skill_md = result["skill_md"]
    for verb in ("discover", "enumerate", "screen", "emit-king"):
        assert verb in skill_md, f"skill.md must mention verb '{verb}'"


def test_skill_md_mentions_scientific_context():
    """skill.md must reference the verAB scientific context (EC number + seeds)."""
    result = load_bundle(BUNDLE_DIR)
    skill_md = result["skill_md"]
    # EC number for verAB O-demethylase
    assert "1.14.13.82" in skill_md, "skill.md must mention EC 1.14.13.82"
    # At least one canonical seed compound name
    assert any(
        name in skill_md
        for name in ("vanillate", "guaiacol", "veratrate")
    ), "skill.md must mention at least one canonical seed compound name"


# ---------------------------------------------------------------------------
# Negative test: BundleError raised for a missing bundle.json
# ---------------------------------------------------------------------------


def test_load_bundle_raises_bundle_error_for_missing_dir(tmp_path: Path):
    """load_bundle must raise BundleError when bundle.json is absent."""
    import pytest

    with pytest.raises(BundleError):
        load_bundle(tmp_path)  # tmp_path has neither bundle.json nor skill.md
