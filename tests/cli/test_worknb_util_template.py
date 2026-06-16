"""Tests for the work-notebook util.py template (Module 4 of the
work-notebooks PRD).

AC 17 (partial, covered here):
    The rendered work-notebook ``util.py`` is ``%run``-loadable and exposes
    ``PROJECT_ROOT``, ``NOTEBOOKS_DIR``, ``MODELS_DIR``, ``GENOMES_DIR``,
    ``DATA_DIR``, ``NBOUTPUT_DIR``, and a ``session`` object whose cache
    resolves to the PRJ-local ``NBCache/``.

Additional assertions:
    - The helpers marker is preserved across a re-render (smart-merge does
      not clobber hand-written helpers below the marker).
    - ``smart_merge_worknb_util`` returns ``None`` when the marker is
      missing from either argument.
    - The rendered text contains the ``for_notebook(__file__, ...)`` call
      with ``cache_dir="NBCache"`` and ``project_name=<repo_basename>``.
"""

from __future__ import annotations

import runpy
import sys
import types
from pathlib import Path

import pytest

from kbutillib.cli.worknb_util import (
    WORKNB_UTIL_MARKER,
    render_worknb_util_template,
    smart_merge_worknb_util,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render(repo_basename: str = "MyRepo", topic: str = "my_topic") -> str:
    """Render the template with sensible defaults."""
    return render_worknb_util_template(repo_basename, topic)


# ---------------------------------------------------------------------------
# render_worknb_util_template — content assertions
# ---------------------------------------------------------------------------


class TestRenderWorknbUtilTemplate:
    """Verify the rendered source text carries the required names and calls."""

    def test_contains_project_root(self) -> None:
        assert "PROJECT_ROOT" in _render()

    def test_contains_notebooks_dir(self) -> None:
        assert "NOTEBOOKS_DIR" in _render()

    def test_contains_models_dir(self) -> None:
        assert "MODELS_DIR" in _render()

    def test_contains_genomes_dir(self) -> None:
        assert "GENOMES_DIR" in _render()

    def test_contains_data_dir(self) -> None:
        assert "DATA_DIR" in _render()

    def test_contains_nboutput_dir(self) -> None:
        assert "NBOUTPUT_DIR" in _render()

    def test_contains_session_assignment(self) -> None:
        """The rendered file must assign a ``session`` variable."""
        rendered = _render()
        assert "session" in rendered
        assert "NotebookSession" in rendered

    def test_contains_for_notebook_call(self) -> None:
        """for_notebook() must be called with __file__ as the first argument."""
        rendered = _render()
        assert "for_notebook" in rendered
        assert "__file__" in rendered

    def test_cache_dir_nbcache(self) -> None:
        """cache_dir must be set to 'NBCache' so the cache lands in PRJ-local NBCache/."""
        rendered = _render()
        assert 'cache_dir="NBCache"' in rendered

    def test_project_name_is_repo_basename(self) -> None:
        """project_name must be the rendered repo_basename."""
        rendered = render_worknb_util_template("SpecialRepo", "analysis")
        assert 'project_name="SpecialRepo"' in rendered

    def test_contains_helpers_marker(self) -> None:
        assert WORKNB_UTIL_MARKER in _render()

    def test_topic_appears_in_output(self) -> None:
        """The PRJ topic should appear in the rendered doc-comment."""
        rendered = render_worknb_util_template("ARepo", "cool_topic")
        assert "cool_topic" in rendered

    def test_repo_basename_substituted(self) -> None:
        """The repo_basename placeholder is replaced everywhere."""
        rendered = render_worknb_util_template("TargetRepo", "stuff")
        assert "TargetRepo" in rendered
        # The Jinja placeholder itself must not appear in the output.
        assert "{{ repo_basename }}" not in rendered
        assert "{{ topic }}" not in rendered

    def test_newline_terminated(self) -> None:
        """Rendered output must end with a newline (keep_trailing_newline=True)."""
        rendered = _render()
        assert rendered.endswith("\n")


# ---------------------------------------------------------------------------
# Executable smoke test — importable as a module without a live kernel
# ---------------------------------------------------------------------------


class TestRenderedUtilExecutable:
    """Verify the rendered util.py is %run-loadable and exposes the six
    path constants and a session object.

    We execute the rendered source via ``exec()`` in an isolated namespace.
    ``NotebookSession.for_notebook`` is patched so no real cache directory
    is created and no Jupyter detection is attempted.
    """

    @pytest.fixture()
    def prj_dir(self, tmp_path: Path) -> Path:
        """Simulate a PRJ-my_topic/ directory inside notebooks/."""
        notebooks = tmp_path / "notebooks"
        prj = notebooks / "PRJ-my_topic"
        prj.mkdir(parents=True)
        # Create shared directories so path assertions are unambiguous.
        (notebooks / "models").mkdir()
        (notebooks / "genomes").mkdir()
        (notebooks / "data").mkdir()
        (prj / "NBOutput").mkdir()
        return prj

    def test_six_path_constants_and_session(self, prj_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """exec()-ing the rendered util.py exposes the six path constants and session."""
        rendered = render_worknb_util_template("MyRepo", "my_topic")

        # Write the rendered util.py into the fake PRJ dir.
        util_py = prj_dir / "util.py"
        util_py.write_text(rendered, encoding="utf-8")

        # Patch NotebookSession so no real cache I/O occurs.
        import kbutillib.notebook.session as _session_mod

        sentinel = object()

        def _fake_for_notebook(notebook_file=None, *, project_name=None, cache_dir=None):
            return sentinel  # type: ignore[return-value]

        monkeypatch.setattr(
            _session_mod.NotebookSession,
            "for_notebook",
            staticmethod(_fake_for_notebook),
        )

        # Execute with __file__ pointing to the rendered util.py.
        ns: dict = {"__file__": str(util_py)}
        exec(compile(rendered, str(util_py), "exec"), ns)  # noqa: S102

        # Six path constants must be present and be Path instances.
        for name in ("PROJECT_ROOT", "NOTEBOOKS_DIR", "MODELS_DIR", "GENOMES_DIR", "DATA_DIR", "NBOUTPUT_DIR"):
            assert name in ns, f"{name} not found in executed namespace"
            assert isinstance(ns[name], Path), f"{name} is not a Path"

        # ``session`` must be the sentinel returned by the patched for_notebook.
        assert "session" in ns
        assert ns["session"] is sentinel

    def test_path_constants_resolve_correctly(self, prj_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify the path constant values match the expected layout."""
        rendered = render_worknb_util_template("MyRepo", "my_topic")
        util_py = prj_dir / "util.py"
        util_py.write_text(rendered, encoding="utf-8")

        import kbutillib.notebook.session as _session_mod

        monkeypatch.setattr(
            _session_mod.NotebookSession,
            "for_notebook",
            staticmethod(lambda *a, **kw: object()),
        )

        ns: dict = {"__file__": str(util_py)}
        exec(compile(rendered, str(util_py), "exec"), ns)  # noqa: S102

        notebooks_dir = prj_dir.parent  # <repo>/notebooks/
        project_root = notebooks_dir.parent  # <repo>/

        assert ns["PROJECT_ROOT"] == project_root
        assert ns["NOTEBOOKS_DIR"] == notebooks_dir
        assert ns["MODELS_DIR"] == notebooks_dir / "models"
        assert ns["GENOMES_DIR"] == notebooks_dir / "genomes"
        assert ns["DATA_DIR"] == notebooks_dir / "data"
        assert ns["NBOUTPUT_DIR"] == prj_dir / "NBOutput"

    def test_session_receives_nbcache_cache_dir(self, prj_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """for_notebook() must be called with cache_dir='NBCache'."""
        rendered = render_worknb_util_template("MyRepo", "my_topic")
        util_py = prj_dir / "util.py"
        util_py.write_text(rendered, encoding="utf-8")

        import kbutillib.notebook.session as _session_mod

        received_kwargs: dict = {}

        def _capture_for_notebook(notebook_file=None, *, project_name=None, cache_dir=None):
            received_kwargs["cache_dir"] = cache_dir
            received_kwargs["project_name"] = project_name
            return object()

        monkeypatch.setattr(
            _session_mod.NotebookSession,
            "for_notebook",
            staticmethod(_capture_for_notebook),
        )

        ns: dict = {"__file__": str(util_py)}
        exec(compile(rendered, str(util_py), "exec"), ns)  # noqa: S102

        assert received_kwargs.get("cache_dir") == "NBCache"
        assert received_kwargs.get("project_name") == "MyRepo"


# ---------------------------------------------------------------------------
# smart_merge_worknb_util — marker preservation
# ---------------------------------------------------------------------------


class TestSmartMergeWorknbUtil:
    """Verify that re-rendering preserves hand-written helpers below the marker."""

    def test_preserves_helpers_below_marker(self) -> None:
        """Helpers below the marker are untouched after a re-render."""
        existing = (
            "# old generated header\n"
            "OLD_CONSTANT = True\n"
            f"{WORKNB_UTIL_MARKER}\n"
            "\n"
            "def my_helper():\n"
            '    return "custom"\n'
        )
        new_header = render_worknb_util_template("MyRepo", "stuff")

        result = smart_merge_worknb_util(existing, new_header)

        assert result is not None
        # Old header must be gone.
        assert "OLD_CONSTANT" not in result
        # New header content must be present.
        assert "PROJECT_ROOT" in result
        assert "NotebookSession" in result
        # Marker must appear exactly once.
        assert result.count(WORKNB_UTIL_MARKER) == 1
        # Custom helper must be preserved.
        assert "my_helper" in result
        assert "custom" in result

    def test_returns_none_when_marker_absent_in_existing(self) -> None:
        """Returns None when existing file has no marker (signals unsafe merge)."""
        existing = "# no marker here\nsome_code = 1\n"
        new_header = _render()
        assert smart_merge_worknb_util(existing, new_header) is None

    def test_returns_none_when_marker_absent_in_new_header(self) -> None:
        """Returns None when new_header has no marker."""
        existing = f"# header\n{WORKNB_UTIL_MARKER}\nfoo()\n"
        new_header = "# no marker in new\n"
        assert smart_merge_worknb_util(existing, new_header) is None

    def test_idempotent_double_merge(self) -> None:
        """Merging the same new_header twice yields the same result as merging once."""
        existing = (
            "# initial header\n"
            f"{WORKNB_UTIL_MARKER}\n"
            "CUSTOM = 42\n"
        )
        new_header = _render()

        first = smart_merge_worknb_util(existing, new_header)
        assert first is not None

        # A second merge with the same new_header should produce identical output.
        second = smart_merge_worknb_util(first, new_header)
        assert second == first

    def test_merge_preserves_multiline_helpers(self) -> None:
        """Multi-line helpers (including imports and classes) are preserved."""
        custom_code = (
            "\n"
            "import os\n"
            "\n"
            "class MyHelper:\n"
            "    def __init__(self):\n"
            "        self.x = 1\n"
            "\n"
            "def load_genome(path):\n"
            "    return path\n"
        )
        existing = (
            "# old header\n"
            f"{WORKNB_UTIL_MARKER}\n"
            + custom_code
        )
        new_header = _render()

        result = smart_merge_worknb_util(existing, new_header)
        assert result is not None
        assert "MyHelper" in result
        assert "load_genome" in result
        assert "import os" in result
