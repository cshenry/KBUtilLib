"""Smoke tests for the KBUtilLib BERIL skill bundle.

Validates:
- Each SKILL.md has valid YAML frontmatter with required fields
  (name, description containing 'Use when', allowed-tools;
   kbu also requires user-invocable: true)
- The util.py.tmpl compiles without syntax errors
- preferences.md contains all required threshold / sampling keys (AC #11)

References: PRD kbu-beril-augmentation, Module 2.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
import yaml

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SKILLS_ROOT = _REPO_ROOT / "src" / "kbutillib" / "beril" / "skills"

_KBU_DIR = _SKILLS_ROOT / "kbu"
_KBU_NOTEBOOK_DIR = _SKILLS_ROOT / "kbu-notebook"
_KBU_FBA_DIR = _SKILLS_ROOT / "kbu-fba"

_KBU_SKILL_MD = _KBU_DIR / "SKILL.md"
_NOTEBOOK_SKILL_MD = _KBU_NOTEBOOK_DIR / "SKILL.md"
_FBA_SKILL_MD = _KBU_FBA_DIR / "SKILL.md"
_PREFERENCES_MD = _KBU_DIR / "preferences.md"
# Unified template lives in the CLI templates directory (Task B: single source of truth).
_UTIL_TMPL = _REPO_ROOT / "src" / "kbutillib" / "cli" / "templates" / "util.py.tmpl"
# The beril/skills/kbu-notebook copy was deleted; this path must NOT exist.
_OLD_UTIL_TMPL = _KBU_NOTEBOOK_DIR / "util.py.tmpl"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_frontmatter(path: Path) -> Dict[str, Any]:
    """Extract and parse YAML frontmatter delimited by '---' lines.

    Returns the parsed dict.  Raises ValueError if no frontmatter is found.
    """
    text = path.read_text(encoding="utf-8")
    # Match content between the first pair of '---' lines.
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not match:
        raise ValueError(f"No YAML frontmatter found in {path}")
    return yaml.safe_load(match.group(1)) or {}


def _extract_yaml_block(text: str) -> Optional[str]:
    """Extract the first fenced ```yaml ... ``` block from a markdown string."""
    match = re.search(r"```yaml\s*\n(.*?)```", text, re.DOTALL)
    if not match:
        return None
    return match.group(1)


# ---------------------------------------------------------------------------
# SKILL.md frontmatter tests
# ---------------------------------------------------------------------------


class TestSkillFrontmatter:
    """Each SKILL.md must have valid YAML frontmatter with required fields."""

    @pytest.mark.parametrize(
        "skill_path,expect_user_invocable",
        [
            pytest.param(_KBU_SKILL_MD, True, id="kbu"),
            pytest.param(_NOTEBOOK_SKILL_MD, False, id="kbu-notebook"),
            pytest.param(_FBA_SKILL_MD, False, id="kbu-fba"),
        ],
    )
    def test_skill_md_exists(self, skill_path, expect_user_invocable):
        """SKILL.md file exists at the expected path."""
        assert skill_path.exists(), f"Missing SKILL.md at {skill_path}"

    @pytest.mark.parametrize(
        "skill_path,expected_name",
        [
            pytest.param(_KBU_SKILL_MD, "kbu", id="kbu"),
            pytest.param(_NOTEBOOK_SKILL_MD, "kbu-notebook", id="kbu-notebook"),
            pytest.param(_FBA_SKILL_MD, "kbu-fba", id="kbu-fba"),
        ],
    )
    def test_frontmatter_parses(self, skill_path, expected_name):
        """SKILL.md frontmatter parses without error and has expected name."""
        fm = _parse_frontmatter(skill_path)
        assert fm.get("name") == expected_name, (
            f"{skill_path.name}: expected name={expected_name!r}, got {fm.get('name')!r}"
        )

    @pytest.mark.parametrize(
        "skill_path",
        [
            pytest.param(_KBU_SKILL_MD, id="kbu"),
            pytest.param(_NOTEBOOK_SKILL_MD, id="kbu-notebook"),
            pytest.param(_FBA_SKILL_MD, id="kbu-fba"),
        ],
    )
    def test_description_has_use_when(self, skill_path):
        """description field exists and contains 'Use when'."""
        fm = _parse_frontmatter(skill_path)
        desc = fm.get("description", "") or ""
        assert "Use when" in desc, (
            f"{skill_path.parent.name}/SKILL.md: description must contain 'Use when', "
            f"got: {desc[:120]!r}"
        )

    @pytest.mark.parametrize(
        "skill_path",
        [
            pytest.param(_KBU_SKILL_MD, id="kbu"),
            pytest.param(_NOTEBOOK_SKILL_MD, id="kbu-notebook"),
            pytest.param(_FBA_SKILL_MD, id="kbu-fba"),
        ],
    )
    def test_allowed_tools_present(self, skill_path):
        """allowed-tools field is present and is a non-empty list."""
        fm = _parse_frontmatter(skill_path)
        tools = fm.get("allowed-tools")
        assert isinstance(tools, list) and len(tools) > 0, (
            f"{skill_path.parent.name}/SKILL.md: allowed-tools must be a non-empty list, "
            f"got {tools!r}"
        )

    def test_kbu_user_invocable_true(self):
        """kbu/SKILL.md must have user-invocable: true."""
        fm = _parse_frontmatter(_KBU_SKILL_MD)
        assert fm.get("user-invocable") is True, (
            f"kbu/SKILL.md: expected user-invocable: true, got {fm.get('user-invocable')!r}"
        )

    def test_kbu_notebook_no_user_invocable(self):
        """kbu-notebook/SKILL.md should not set user-invocable (auto-discoverable)."""
        fm = _parse_frontmatter(_NOTEBOOK_SKILL_MD)
        # user-invocable may be absent or false for auto-discoverable skills
        assert fm.get("user-invocable") is not True, (
            "kbu-notebook/SKILL.md should not have user-invocable: true"
        )

    def test_kbu_fba_no_user_invocable(self):
        """kbu-fba/SKILL.md should not set user-invocable (auto-discoverable)."""
        fm = _parse_frontmatter(_FBA_SKILL_MD)
        assert fm.get("user-invocable") is not True, (
            "kbu-fba/SKILL.md should not have user-invocable: true"
        )


# ---------------------------------------------------------------------------
# util.py template syntax test
# ---------------------------------------------------------------------------


class TestUtilTemplate:
    """The unified util.py.tmpl must pass all Task B acceptance criteria (AC 4, 5, 6)."""

    def test_util_tmpl_exists(self):
        """Unified util.py.tmpl exists at cli/templates/ (AC 4)."""
        assert _UTIL_TMPL.exists(), f"Missing unified util.py.tmpl at {_UTIL_TMPL}"

    def test_old_util_tmpl_deleted(self):
        """beril/skills/kbu-notebook/util.py.tmpl has been deleted (AC 4)."""
        assert not _OLD_UTIL_TMPL.exists(), (
            f"Duplicate util.py.tmpl still exists at {_OLD_UTIL_TMPL}; "
            "it must be deleted — cli/templates/util.py.tmpl is the single source of truth."
        )

    def test_util_tmpl_is_valid_python(self):
        """util.py.tmpl parses without syntax errors via ast.parse."""
        source = _UTIL_TMPL.read_text(encoding="utf-8")
        try:
            ast.parse(source)
        except SyntaxError as exc:
            pytest.fail(f"util.py.tmpl has a syntax error: {exc}")

    def test_util_tmpl_has_bootstrap(self):
        """util.py.tmpl contains the sys-path bootstrap (_bootstrap_sys_paths) (AC 4)."""
        source = _UTIL_TMPL.read_text(encoding="utf-8")
        assert "_bootstrap_sys_paths" in source, (
            "util.py.tmpl must include the _bootstrap_sys_paths function "
            "(portability via ~/.kbu-sys-paths)"
        )

    def test_util_tmpl_has_notebook_session(self):
        """util.py.tmpl imports and instantiates NotebookSession with __file__ (AC 4)."""
        source = _UTIL_TMPL.read_text(encoding="utf-8")
        assert "from kbutillib.notebook import NotebookSession" in source, (
            "util.py.tmpl must use 'from kbutillib.notebook import NotebookSession'"
        )
        assert "for_notebook" in source, (
            "util.py.tmpl must call NotebookSession.for_notebook()"
        )
        assert "for_notebook(\n    __file__" in source or "for_notebook(__file__" in source, (
            "util.py.tmpl must pass __file__ to for_notebook() for cwd-independent anchoring"
        )

    def _assert_import_is_guarded(self, source: str, import_token: str) -> None:
        """Assert that any line containing import_token is preceded by a 'try:' line."""
        lines = source.splitlines()
        for i, line in enumerate(lines):
            stripped = line.strip()
            if import_token in stripped and stripped.startswith("import "):
                # Look back for a 'try:' line at the top level
                for j in range(i - 1, max(i - 5, -1), -1):
                    prev = lines[j].strip()
                    if prev == "try:":
                        break  # found guard
                    if prev and not prev.startswith("#"):
                        pytest.fail(
                            f"'{stripped}' is not inside a try/except ImportError block. "
                            f"It must be guarded."
                        )

    def test_util_tmpl_guarded_numpy(self):
        """numpy import is guarded in try/except ImportError (AC 4)."""
        source = _UTIL_TMPL.read_text(encoding="utf-8")
        assert "import numpy" in source, "util.py.tmpl must import numpy"
        self._assert_import_is_guarded(source, "numpy")
        # The except clause must set np = None
        assert "np = None" in source, "util.py.tmpl numpy guard must set np = None"

    def test_util_tmpl_guarded_pandas(self):
        """pandas import is guarded in try/except ImportError (AC 4)."""
        source = _UTIL_TMPL.read_text(encoding="utf-8")
        assert "import pandas" in source, "util.py.tmpl must import pandas"
        self._assert_import_is_guarded(source, "pandas")
        # The except clause must set pd = None
        assert "pd = None" in source, "util.py.tmpl pandas guard must set pd = None"

    def test_util_tmpl_guarded_cobra(self):
        """cobra import is guarded in try/except ImportError (AC 4)."""
        source = _UTIL_TMPL.read_text(encoding="utf-8")
        assert "import cobra" in source, "util.py.tmpl must import cobra"
        self._assert_import_is_guarded(source, "cobra")
        # The except clause must set cobra = None
        assert "cobra = None" in source, "util.py.tmpl cobra guard must set cobra = None"

    def test_util_tmpl_no_helpers_import(self):
        """util.py.tmpl must NOT import kbutillib.notebook.helpers (AC 4)."""
        source = _UTIL_TMPL.read_text(encoding="utf-8")
        assert "kbutillib.notebook.helpers" not in source, (
            "util.py.tmpl must NOT import kbutillib.notebook.helpers "
            "(unguarded helpers import removed from unified template)"
        )

    def test_util_tmpl_no_schema_import(self):
        """util.py.tmpl must NOT import kbutillib.notebook.schema (AC 4)."""
        source = _UTIL_TMPL.read_text(encoding="utf-8")
        assert "kbutillib.notebook.schema" not in source, (
            "util.py.tmpl must NOT import kbutillib.notebook.schema "
            "(removed from unified template)"
        )

    def test_util_tmpl_no_session_for_shim(self):
        """util.py.tmpl must NOT contain the session_for() back-compat shim (AC 4)."""
        source = _UTIL_TMPL.read_text(encoding="utf-8")
        assert "def session_for(" not in source, (
            "util.py.tmpl must NOT contain the session_for() shim "
            "(dropped in unified template)"
        )

    def test_util_tmpl_flat_project_root(self):
        """util.py.tmpl defines PROJECT_ROOT = NOTEBOOK_DIR.parent (one level) (AC 5)."""
        source = _UTIL_TMPL.read_text(encoding="utf-8")
        assert "PROJECT_ROOT" in source, "util.py.tmpl must define PROJECT_ROOT"
        assert "NOTEBOOK_DIR.parent" in source, (
            "util.py.tmpl must set PROJECT_ROOT = NOTEBOOK_DIR.parent (FLAT, one level)"
        )
        assert "parent.parent" not in source, (
            "util.py.tmpl must NOT use parent.parent — FLAT layout is one level up"
        )

    def test_util_tmpl_has_smart_merge_marker(self):
        """util.py.tmpl contains the smart-merge marker for --force round-trips (AC 6)."""
        source = _UTIL_TMPL.read_text(encoding="utf-8")
        assert "# === project-specific helpers below ===" in source, (
            "util.py.tmpl must contain the smart-merge marker "
            "'# === project-specific helpers below ==='"
        )

    def test_smart_merge_round_trip(self, tmp_path):
        """kbu init-notebook --force smart-merge finds the marker and preserves helpers (AC 6)."""
        import sys
        import importlib

        # Render the template with a fake project name (replicate CLI render logic)
        import jinja2
        tmpl_dir = str(_UTIL_TMPL.parent)
        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(tmpl_dir),
            keep_trailing_newline=True,
        )
        tmpl = env.get_template("util.py.tmpl")
        rendered = tmpl.render(project_name="test-project")

        # Write initial util.py with user-added helper below the marker
        util_py = tmp_path / "util.py"
        user_helper = "\n\ndef my_custom_helper():\n    return 42\n"
        initial_content = rendered + user_helper
        util_py.write_text(initial_content)

        # Now simulate --force: smart-merge with a re-render
        re_rendered = tmpl.render(project_name="test-project-renamed")

        # Import the smart-merge logic
        repo_src = str(_REPO_ROOT / "src")
        if repo_src not in sys.path:
            sys.path.insert(0, repo_src)
        from kbutillib.cli.init_notebook import _smart_merge_util

        merged = _smart_merge_util(util_py.read_text(), re_rendered)
        assert merged is not None, (
            "_smart_merge_util returned None — marker not found in the rendered template"
        )
        # Header should reflect re-render (new project name)
        assert "test-project-renamed" in merged, (
            "Merged util.py should contain the updated project name from re-render"
        )
        # User helper must be preserved
        assert "my_custom_helper" in merged, (
            "Smart-merge must preserve user helpers below the marker"
        )


# ---------------------------------------------------------------------------
# preferences.md threshold-key tests (AC #11)
# ---------------------------------------------------------------------------


class TestPreferencesTemplate:
    """preferences.md must exist and contain all required YAML keys."""

    _REQUIRED_KEYS = [
        "execution.runtime_threshold_seconds",
        "execution.fanout_threshold",
        "sampling.reconstruction_n",
        "sampling.gapfill_media_n",
        "sampling.gapfill_max_solutions",
        "sampling.fva_reaction_n",
        "solver.name",
        "gapfill.comprehensive",
        "organism.focus",
        "media.default",
        "version",
    ]

    def test_preferences_md_exists(self):
        """preferences.md exists at the expected path."""
        assert _PREFERENCES_MD.exists(), f"Missing preferences.md at {_PREFERENCES_MD}"

    def test_preferences_yaml_block_present(self):
        """preferences.md contains a fenced ```yaml``` block."""
        text = _PREFERENCES_MD.read_text(encoding="utf-8")
        block = _extract_yaml_block(text)
        assert block is not None, "preferences.md must contain a ```yaml ... ``` block"

    def test_preferences_yaml_parses(self):
        """The YAML block in preferences.md parses without error."""
        text = _PREFERENCES_MD.read_text(encoding="utf-8")
        block = _extract_yaml_block(text)
        assert block is not None
        try:
            data = yaml.safe_load(block)
        except yaml.YAMLError as exc:
            pytest.fail(f"preferences.md YAML block failed to parse: {exc}")
        assert isinstance(data, dict), "preferences.md YAML block must be a mapping"

    @pytest.mark.parametrize(
        "dotted_key",
        _REQUIRED_KEYS,
    )
    def test_preferences_has_required_key(self, dotted_key):
        """preferences.md YAML block contains every required key (AC #11)."""
        text = _PREFERENCES_MD.read_text(encoding="utf-8")
        block = _extract_yaml_block(text)
        assert block is not None
        data = yaml.safe_load(block)
        assert isinstance(data, dict)

        # Traverse dotted path.
        parts = dotted_key.split(".")
        node: Any = data
        for part in parts:
            if not isinstance(node, dict) or part not in node:
                pytest.fail(
                    f"preferences.md YAML is missing required key '{dotted_key}' "
                    f"(missing segment '{part}')"
                )
            node = node[part]
        # Key exists (value may be None / empty — that's allowed for a template).

    def test_preferences_runtime_threshold_default_60(self):
        """execution.runtime_threshold_seconds defaults to 60 in the template."""
        text = _PREFERENCES_MD.read_text(encoding="utf-8")
        block = _extract_yaml_block(text)
        data = yaml.safe_load(block)
        assert data["execution"]["runtime_threshold_seconds"] == 60, (
            "Default runtime_threshold_seconds must be 60 seconds"
        )

    def test_preferences_sampling_fva_reaction_n_default_10(self):
        """sampling.fva_reaction_n defaults to 10 in the template."""
        text = _PREFERENCES_MD.read_text(encoding="utf-8")
        block = _extract_yaml_block(text)
        data = yaml.safe_load(block)
        assert data["sampling"]["fva_reaction_n"] == 10, (
            "Default sampling.fva_reaction_n must be 10"
        )


# ---------------------------------------------------------------------------
# kbu-fba content tests
# ---------------------------------------------------------------------------


class TestFbaSkillContent:
    """kbu-fba SKILL.md must mandate run_fva and forbid cobra FVA."""

    def test_fba_skill_mandates_run_fva(self):
        """kbu-fba SKILL.md mentions MSFBAUtils.run_fva as the required function."""
        text = _FBA_SKILL_MD.read_text(encoding="utf-8")
        assert "run_fva" in text, (
            "kbu-fba/SKILL.md must mandate MSFBAUtils.run_fva"
        )

    def test_fba_skill_forbids_cobra_fva(self):
        """kbu-fba SKILL.md explicitly forbids cobra.flux_variability_analysis."""
        text = _FBA_SKILL_MD.read_text(encoding="utf-8")
        assert "cobra.flux_variability_analysis" in text, (
            "kbu-fba/SKILL.md must mention cobra.flux_variability_analysis "
            "to explicitly forbid it"
        )
        # The word 'broken' or 'Never' or 'never' must appear near the prohibition.
        assert any(
            word in text for word in ("broken", "Never", "never", "FORBID", "forbid")
        ), (
            "kbu-fba/SKILL.md must explicitly forbid cobra.flux_variability_analysis "
            "(use 'never', 'broken', or 'FORBID')"
        )

    def test_fba_skill_has_graduated_policy(self):
        """kbu-fba SKILL.md contains the graduated execution policy tiers."""
        text = _FBA_SKILL_MD.read_text(encoding="utf-8")
        for marker in ("🟢", "🟡", "🔴"):
            assert marker in text, (
                f"kbu-fba/SKILL.md is missing graduated execution tier marker '{marker}'"
            )

    def test_fba_skill_has_runtime_rubric(self):
        """kbu-fba SKILL.md contains the <5s / 5-60s / >60s runtime rubric."""
        text = _FBA_SKILL_MD.read_text(encoding="utf-8")
        # Check that all three boundary values are mentioned.
        assert "5 s" in text or "< 5" in text or "<5" in text, (
            "kbu-fba/SKILL.md must include the <5s boundary of the runtime rubric"
        )
        assert "60 s" in text or "60s" in text, (
            "kbu-fba/SKILL.md must include the 60s boundary of the runtime rubric"
        )


# ---------------------------------------------------------------------------
# kbu-notebook content tests
# ---------------------------------------------------------------------------


class TestNotebookSkillContent:
    """kbu-notebook SKILL.md must encode the graduated execution policy."""

    def test_notebook_skill_has_graduated_policy(self):
        """kbu-notebook SKILL.md contains the graduated execution policy tiers."""
        text = _NOTEBOOK_SKILL_MD.read_text(encoding="utf-8")
        for marker in ("🟢", "🟡", "🔴"):
            assert marker in text, (
                f"kbu-notebook/SKILL.md is missing graduated execution tier marker '{marker}'"
            )

    def test_notebook_skill_has_runtime_rubric(self):
        """kbu-notebook SKILL.md contains the <5s / 5-60s / >60s runtime rubric."""
        text = _NOTEBOOK_SKILL_MD.read_text(encoding="utf-8")
        assert "5 s" in text or "< 5" in text or "<5" in text, (
            "kbu-notebook/SKILL.md must include the <5s boundary of the runtime rubric"
        )
        assert "60 s" in text or "60s" in text, (
            "kbu-notebook/SKILL.md must include the 60s boundary of the runtime rubric"
        )

    def test_notebook_skill_has_kbcache(self):
        """kbu-notebook SKILL.md documents the .kbcache/ directory."""
        text = _NOTEBOOK_SKILL_MD.read_text(encoding="utf-8")
        assert ".kbcache" in text, (
            "kbu-notebook/SKILL.md must document the .kbcache/ directory"
        )

    def test_notebook_skill_supersedes_jupyter_dev(self):
        """kbu-notebook SKILL.md states it supersedes jupyter-dev."""
        text = _NOTEBOOK_SKILL_MD.read_text(encoding="utf-8")
        assert "jupyter-dev" in text or "supersedes" in text.lower(), (
            "kbu-notebook/SKILL.md must state that it supersedes jupyter-dev"
        )

    def test_notebook_skill_flat_project_root(self):
        """kbu-notebook SKILL.md defines PROJECT_ROOT as NOTEBOOK_DIR.parent (AC 5)."""
        text = _NOTEBOOK_SKILL_MD.read_text(encoding="utf-8")
        assert "PROJECT_ROOT" in text, (
            "kbu-notebook/SKILL.md must document PROJECT_ROOT"
        )
        assert "NOTEBOOK_DIR.parent" in text, (
            "kbu-notebook/SKILL.md must show PROJECT_ROOT = NOTEBOOK_DIR.parent (FLAT, one level)"
        )
        assert "parent.parent" not in text, (
            "kbu-notebook/SKILL.md must NOT use parent.parent — FLAT layout is one level up"
        )

    def test_notebook_skill_template_ref_points_to_cli(self):
        """kbu-notebook SKILL.md references cli/templates/util.py.tmpl (AC 4)."""
        text = _NOTEBOOK_SKILL_MD.read_text(encoding="utf-8")
        assert "cli/templates/util.py.tmpl" in text, (
            "kbu-notebook/SKILL.md must reference cli/templates/util.py.tmpl "
            "(the unified single-source template)"
        )

    def test_notebook_skill_no_old_tmpl_ref(self):
        """kbu-notebook SKILL.md must not reference the deleted beril/skills tmpl path (AC 4)."""
        text = _NOTEBOOK_SKILL_MD.read_text(encoding="utf-8")
        assert "beril/skills/kbu-notebook/util.py.tmpl" not in text, (
            "kbu-notebook/SKILL.md must not reference the deleted "
            "beril/skills/kbu-notebook/util.py.tmpl"
        )
