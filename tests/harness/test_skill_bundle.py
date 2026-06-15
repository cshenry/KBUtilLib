"""Smoke tests for the kbu-run harness skill bundle.

Validates that SKILL.md has valid YAML frontmatter with the required fields:
- name: kbu-run
- description containing 'Use when'
- allowed-tools (non-empty list)
- user-invocable: true

References: PRD kbu-harness, Module 2, Acceptance Criteria #25, #26, #37.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SKILL_DIR = _REPO_ROOT / "src" / "kbutillib" / "harness" / "skills" / "kbu-run"
_SKILL_MD = _SKILL_DIR / "SKILL.md"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_frontmatter(path: Path) -> Dict[str, Any]:
    """Extract and parse YAML frontmatter delimited by '---' lines.

    Returns the parsed dict.  Raises ValueError if no frontmatter is found.
    """
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not match:
        raise ValueError(f"No YAML frontmatter found in {path}")
    return yaml.safe_load(match.group(1)) or {}


# ---------------------------------------------------------------------------
# SKILL.md existence
# ---------------------------------------------------------------------------


class TestSkillBundleExists:
    """The kbu-run skill bundle must be present at the expected path."""

    def test_skill_dir_exists(self):
        """The kbu-run skill directory exists."""
        assert _SKILL_DIR.exists(), (
            f"Missing kbu-run skill directory at {_SKILL_DIR}"
        )

    def test_skill_md_exists(self):
        """SKILL.md exists inside the kbu-run skill directory."""
        assert _SKILL_MD.exists(), (
            f"Missing SKILL.md at {_SKILL_MD}"
        )


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


class TestSkillFrontmatter:
    """SKILL.md must have valid YAML frontmatter with required fields."""

    def test_frontmatter_parses(self):
        """SKILL.md frontmatter parses without error."""
        fm = _parse_frontmatter(_SKILL_MD)
        assert isinstance(fm, dict), "Frontmatter must be a YAML mapping"

    def test_name_is_kbu_run(self):
        """name field must be 'kbu-run' (AC #25)."""
        fm = _parse_frontmatter(_SKILL_MD)
        assert fm.get("name") == "kbu-run", (
            f"kbu-run/SKILL.md: expected name='kbu-run', got {fm.get('name')!r}"
        )

    def test_description_starts_with_use_when(self):
        """description must start with 'Use when' (AC #25)."""
        fm = _parse_frontmatter(_SKILL_MD)
        desc = fm.get("description", "") or ""
        # YAML folded/literal scalars may have leading whitespace — strip it.
        desc_stripped = desc.strip()
        assert desc_stripped.startswith("Use when"), (
            f"kbu-run/SKILL.md: description must start with 'Use when', "
            f"got: {desc_stripped[:120]!r}"
        )

    def test_description_mentions_cobra_or_msmodelutil(self):
        """description must be scoped to COBRA/MSModelUtil modeling projects (AC #25)."""
        fm = _parse_frontmatter(_SKILL_MD)
        desc = (fm.get("description", "") or "").strip()
        assert any(
            keyword in desc
            for keyword in ("COBRA", "MSModelUtil", "BERIL", "harness")
        ), (
            f"kbu-run/SKILL.md: description must mention COBRA, MSModelUtil, "
            f"BERIL, or harness to scope the skill; got: {desc[:200]!r}"
        )

    def test_allowed_tools_present_and_non_empty(self):
        """allowed-tools field must be a non-empty list (AC #25)."""
        fm = _parse_frontmatter(_SKILL_MD)
        tools = fm.get("allowed-tools")
        assert isinstance(tools, list) and len(tools) > 0, (
            f"kbu-run/SKILL.md: allowed-tools must be a non-empty list, "
            f"got {tools!r}"
        )

    def test_allowed_tools_contains_read_and_bash(self):
        """allowed-tools must include Read and Bash (AC #25)."""
        fm = _parse_frontmatter(_SKILL_MD)
        tools = fm.get("allowed-tools", [])
        assert "Read" in tools, (
            f"kbu-run/SKILL.md: allowed-tools must include 'Read', got {tools!r}"
        )
        assert "Bash" in tools, (
            f"kbu-run/SKILL.md: allowed-tools must include 'Bash', got {tools!r}"
        )

    def test_user_invocable_true(self):
        """user-invocable must be true (AC #25)."""
        fm = _parse_frontmatter(_SKILL_MD)
        assert fm.get("user-invocable") is True, (
            f"kbu-run/SKILL.md: expected user-invocable: true, "
            f"got {fm.get('user-invocable')!r}"
        )


# ---------------------------------------------------------------------------
# Body content smoke tests (AC #26, #37)
# ---------------------------------------------------------------------------


class TestSkillBodyContent:
    """SKILL.md body must document the required workflow steps (AC #26, #37)."""

    def _body(self) -> str:
        text = _SKILL_MD.read_text(encoding="utf-8")
        # Strip frontmatter.
        match = re.match(r"^---\s*\n.*?\n---\s*\n", text, re.DOTALL)
        return text[match.end():] if match else text

    def test_body_documents_pull_step(self):
        """Body must reference 'kbu harness pull'."""
        assert "kbu harness pull" in self._body(), (
            "kbu-run/SKILL.md body must document the pull step"
        )

    def test_body_documents_preferences_classification(self):
        """Body must reference preferences.md threshold classification."""
        body = self._body()
        assert "preferences.md" in body or "preferences" in body.lower(), (
            "kbu-run/SKILL.md body must reference preferences.md for classification"
        )

    def test_body_documents_graduated_policy(self):
        """Body must document the graduated execution policy tiers."""
        body = self._body()
        for marker in ("🟢", "🟡", "🔴"):
            assert marker in body, (
                f"kbu-run/SKILL.md body is missing graduated policy tier marker '{marker}'"
            )

    def test_body_documents_kbu_harness_run(self):
        """Body must reference 'kbu harness run'."""
        assert "kbu harness run" in self._body(), (
            "kbu-run/SKILL.md body must document the run step"
        )

    def test_body_documents_verify_outputs(self):
        """Body must mention output verification."""
        body = self._body()
        assert "outputs_present" in body or "verify" in body.lower(), (
            "kbu-run/SKILL.md body must document output verification"
        )

    def test_body_documents_devlog(self):
        """Body must reference DEVLOG.md."""
        assert "DEVLOG.md" in self._body(), (
            "kbu-run/SKILL.md body must reference DEVLOG.md"
        )

    def test_body_documents_push_confirmation_prompt(self):
        """Body must contain the exact push confirmation prompt (AC #37)."""
        body = self._body()
        assert "Push results back to BERIL now? (y/N)" in body, (
            "kbu-run/SKILL.md body must contain the exact prompt "
            "'Push results back to BERIL now? (y/N)' (AC #37)"
        )

    def test_body_documents_kbu_harness_push(self):
        """Body must reference 'kbu harness push' for the success path."""
        assert "kbu harness push" in self._body(), (
            "kbu-run/SKILL.md body must document 'kbu harness push'"
        )

    def test_body_documents_beril_commit_reminder(self):
        """Body must remind the user to commit in BERIL."""
        body = self._body()
        assert "commit" in body.lower() and "BERIL" in body, (
            "kbu-run/SKILL.md body must remind the user to commit in BERIL"
        )

    def test_body_documents_failure_no_code_edit(self):
        """Body must state that no code is edited on failure (AC #26)."""
        body = self._body()
        assert "Edit no code" in body or "edit no code" in body.lower() or "no code" in body.lower(), (
            "kbu-run/SKILL.md body must state that no code is edited on failure"
        )

    def test_body_documents_blocked_report(self):
        """Body must reference a BLOCKED escalation on failure (AC #26)."""
        assert "BLOCKED" in self._body(), (
            "kbu-run/SKILL.md body must reference a BLOCKED report on failure"
        )

    def test_body_no_anthropic_api_call(self):
        """Body must not invoke the Anthropic API or subprocess claude."""
        body = self._body()
        # Simple text checks — a skill body referencing these in prohibition
        # context is fine, but direct invocation patterns must not appear.
        assert "anthropic.Anthropic" not in body, (
            "kbu-run/SKILL.md must not invoke the Anthropic API"
        )
        assert "subprocess.run([\"claude\"" not in body, (
            "kbu-run/SKILL.md must not invoke claude as a subprocess"
        )
        assert "subprocess.call([\"claude\"" not in body, (
            "kbu-run/SKILL.md must not invoke claude as a subprocess"
        )
