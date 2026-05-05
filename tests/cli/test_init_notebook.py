"""Tests for kbutillib.cli.init_notebook — ``kbu init-notebook`` command."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
import yaml
from click.testing import CliRunner

from kbutillib.cli import main
from kbutillib.cli.init_notebook import (
    _MARKER,
    _render_util_template,
    _slugify,
    _smart_merge_util,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG = {
    "default_python": "3.12",
    "editable_installs": ["~/Dropbox/Projects/KBUtilLib"],
    "notebook_deps": ["jupyter", "ipykernel"],
}


def _mock_subprocess_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
    """Mock subprocess.run that succeeds for all expected calls."""
    result = MagicMock()
    result.returncode = 0
    result.stdout = ""
    result.stderr = ""
    return result


# ---------------------------------------------------------------------------
# slugify
# ---------------------------------------------------------------------------


class TestSlugify:
    def test_lowercase(self) -> None:
        assert _slugify("MyProject") == "myproject"

    def test_spaces_to_hyphens(self) -> None:
        assert _slugify("My Project") == "my-project"

    def test_special_chars(self) -> None:
        assert _slugify("Hello_World!") == "hello-world"

    def test_collapses_multiple_hyphens(self) -> None:
        assert _slugify("a---b") == "a-b"

    def test_strips_edge_hyphens(self) -> None:
        assert _slugify("--hello--") == "hello"

    def test_already_slugified(self) -> None:
        assert _slugify("adp1notebooks") == "adp1notebooks"


# ---------------------------------------------------------------------------
# smart_merge_util
# ---------------------------------------------------------------------------


class TestSmartMergeUtil:
    def test_preserves_below_marker(self) -> None:
        existing = (
            "# old header stuff\n"
            "old_code = True\n"
            f"{_MARKER}\n"
            "def my_custom_func():\n"
            '    return "custom"\n'
        )
        new_header = (
            "# new header\n"
            "new_code = True\n"
            f"{_MARKER}\n"
        )
        result = _smart_merge_util(existing, new_header)
        assert result is not None
        assert "new_code = True" in result
        assert "old_code" not in result
        assert "my_custom_func" in result
        assert _MARKER in result

    def test_returns_none_without_marker_in_existing(self) -> None:
        existing = "# no marker here\nold_code = True\n"
        new_header = f"# header\n{_MARKER}\n"
        assert _smart_merge_util(existing, new_header) is None

    def test_returns_none_without_marker_in_new(self) -> None:
        existing = f"# header\n{_MARKER}\nfoo()\n"
        new_header = "# no marker in new\n"
        assert _smart_merge_util(existing, new_header) is None


# ---------------------------------------------------------------------------
# render_util_template
# ---------------------------------------------------------------------------


class TestRenderUtilTemplate:
    def test_renders_project_name(self) -> None:
        rendered = _render_util_template("my-project")
        assert "my-project" in rendered
        assert "NotebookSession" in rendered
        assert _MARKER in rendered

    def test_contains_session_for(self) -> None:
        rendered = _render_util_template("test-proj")
        assert "def session_for" in rendered


# ---------------------------------------------------------------------------
# init-notebook CLI command
# ---------------------------------------------------------------------------


class TestInitNotebookCommand:
    """Test the full CLI command via Click's CliRunner."""

    @pytest.fixture()
    def project_dir(self, tmp_path: Path) -> Path:
        """Create a minimal project directory with notebooks/."""
        d = tmp_path / "my-project"
        d.mkdir()
        (d / "notebooks").mkdir()
        return d

    @pytest.fixture()
    def runner(self) -> CliRunner:
        return CliRunner()

    def _invoke(
        self,
        runner: CliRunner,
        project_dir: Path,
        extra_args: list[str] | None = None,
        alias: str = "testbox",
        no_venv: bool = True,
    ) -> object:
        """Invoke ``kbu init-notebook`` with standard mocks."""
        args = ["init-notebook"]
        if no_venv:
            args.append("--no-venv")
        args.extend(["--alias", alias])
        if extra_args:
            args.extend(extra_args)

        with (
            patch(
                "kbutillib.cli.init_notebook.load_machine_config",
                return_value=_DEFAULT_CONFIG.copy(),
            ),
            patch(
                "kbutillib.cli.init_notebook.resolve_alias",
                return_value=alias,
            ),
            patch(
                "kbutillib.cli.init_notebook.subprocess.run",
                side_effect=_mock_subprocess_run,
            ),
            patch("shutil.which", return_value="/usr/local/bin/venvman"),
        ):
            result = runner.invoke(main, args, catch_exceptions=False)
        return result

    def test_creates_util_py(self, runner: CliRunner, project_dir: Path) -> None:
        """Basic bootstrap creates notebooks/util.py."""
        os.chdir(project_dir)
        result = self._invoke(runner, project_dir)
        assert result.exit_code == 0, result.output
        util_path = project_dir / "notebooks" / "util.py"
        assert util_path.exists()
        content = util_path.read_text()
        assert "my-project" in content
        assert _MARKER in content

    def test_creates_activate_sh_no_venv(self, runner: CliRunner, project_dir: Path) -> None:
        """With --no-venv, a minimal activate.sh is written."""
        os.chdir(project_dir)
        result = self._invoke(runner, project_dir)
        assert result.exit_code == 0, result.output
        activate = project_dir / "activate.sh"
        assert activate.exists()
        assert "kbu.nb-my-project" in activate.read_text()

    def test_force_overwrites_util_header(self, runner: CliRunner, project_dir: Path) -> None:
        """--force replaces the header above the marker but keeps custom code below."""
        os.chdir(project_dir)
        # Write existing util.py with custom code
        util_path = project_dir / "notebooks" / "util.py"
        util_path.write_text(
            "# OLD HEADER\n"
            f"{_MARKER}\n"
            "def my_func():\n"
            '    return "preserved"\n'
        )
        result = self._invoke(runner, project_dir, extra_args=["--force"])
        assert result.exit_code == 0, result.output
        content = util_path.read_text()
        assert "OLD HEADER" not in content
        assert "NotebookSession" in content
        assert "my_func" in content
        assert "preserved" in content

    def test_default_preserves_existing_util(self, runner: CliRunner, project_dir: Path) -> None:
        """Without --force, existing util.py is not overwritten."""
        os.chdir(project_dir)
        util_path = project_dir / "notebooks" / "util.py"
        original = "# My custom util\npass\n"
        util_path.write_text(original)
        result = self._invoke(runner, project_dir)
        assert result.exit_code == 0, result.output
        assert util_path.read_text() == original

    def test_no_pin_kernels_skips_kernel_work(self, runner: CliRunner, project_dir: Path) -> None:
        """--no-pin-kernels skips kernel registration."""
        os.chdir(project_dir)
        result = self._invoke(runner, project_dir, extra_args=["--no-pin-kernels"])
        assert result.exit_code == 0, result.output
        assert "ipykernel" not in result.output

    def test_project_name_slugification(self, runner: CliRunner, tmp_path: Path) -> None:
        """A project name with spaces/uppercase is auto-slugified with a warning."""
        d = tmp_path / "My Project"
        d.mkdir()
        (d / "notebooks").mkdir()
        os.chdir(d)
        result = self._invoke(runner, d)
        assert result.exit_code == 0, result.output
        assert "slugified" in result.output
        assert "my-project" in result.output

    def test_idempotence(self, runner: CliRunner, project_dir: Path) -> None:
        """Running init-notebook twice completes cleanly both times."""
        os.chdir(project_dir)
        result1 = self._invoke(runner, project_dir)
        assert result1.exit_code == 0, result1.output
        result2 = self._invoke(runner, project_dir)
        assert result2.exit_code == 0, result2.output

    def test_no_venv_skips_venvman(self, runner: CliRunner, project_dir: Path) -> None:
        """--no-venv does not call venvman or pip."""
        os.chdir(project_dir)
        with (
            patch(
                "kbutillib.cli.init_notebook.load_machine_config",
                return_value=_DEFAULT_CONFIG.copy(),
            ),
            patch(
                "kbutillib.cli.init_notebook.resolve_alias",
                return_value="testbox",
            ),
            patch(
                "kbutillib.cli.init_notebook.subprocess.run",
                side_effect=_mock_subprocess_run,
            ) as mock_subproc,
        ):
            result = runner.invoke(
                main,
                ["init-notebook", "--no-venv", "--alias", "testbox"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0, result.output
        # subprocess.run should NOT have been called (no venvman, no pip, no ipykernel)
        mock_subproc.assert_not_called()

    def test_creates_notebooks_dir_if_missing(self, runner: CliRunner, tmp_path: Path) -> None:
        """If notebooks/ doesn't exist, it's created."""
        d = tmp_path / "new-project"
        d.mkdir()
        os.chdir(d)
        result = self._invoke(runner, d)
        assert result.exit_code == 0, result.output
        assert (d / "notebooks").is_dir()
        assert (d / "notebooks" / "util.py").exists()

    def test_venv_creation_calls_venvman(self, runner: CliRunner, project_dir: Path) -> None:
        """Without --no-venv, venvman create is called."""
        os.chdir(project_dir)
        with (
            patch(
                "kbutillib.cli.init_notebook.load_machine_config",
                return_value=_DEFAULT_CONFIG.copy(),
            ),
            patch(
                "kbutillib.cli.init_notebook.resolve_alias",
                return_value="testbox",
            ),
            patch(
                "kbutillib.cli.init_notebook.subprocess.run",
                side_effect=_mock_subprocess_run,
            ) as mock_subproc,
            patch("shutil.which", return_value="/usr/local/bin/venvman"),
            # Editable install path doesn't actually exist
            patch.object(Path, "exists", side_effect=lambda self=None: True),
        ):
            result = runner.invoke(
                main,
                ["init-notebook", "--alias", "testbox", "--no-pin-kernels"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0, result.output
        # venvman create should have been called
        calls = mock_subproc.call_args_list
        venvman_calls = [c for c in calls if "venvman" in str(c)]
        assert len(venvman_calls) >= 1

    def test_broken_venv_raises(self, runner: CliRunner, project_dir: Path) -> None:
        """A broken venv (dir exists but no python binary) raises an error."""
        os.chdir(project_dir)
        # Create a fake venv dir without bin/python
        venv_dir = Path("~/VirtualEnvironments/kbu.nb-my-project-py3.12").expanduser()
        venv_dir.mkdir(parents=True, exist_ok=True)
        try:
            with (
                patch(
                    "kbutillib.cli.init_notebook.load_machine_config",
                    return_value=_DEFAULT_CONFIG.copy(),
                ),
                patch(
                    "kbutillib.cli.init_notebook.resolve_alias",
                    return_value="testbox",
                ),
                patch("shutil.which", return_value="/usr/local/bin/venvman"),
            ):
                result = runner.invoke(
                    main,
                    ["init-notebook", "--alias", "testbox"],
                    catch_exceptions=False,
                )
            assert result.exit_code != 0
            assert "Broken venv" in result.output
        finally:
            # Clean up
            import shutil

            shutil.rmtree(venv_dir, ignore_errors=True)

    def test_venvman_not_on_path_raises(self, runner: CliRunner, project_dir: Path) -> None:
        """If venvman is not on PATH, abort with install instructions."""
        os.chdir(project_dir)
        with (
            patch(
                "kbutillib.cli.init_notebook.load_machine_config",
                return_value=_DEFAULT_CONFIG.copy(),
            ),
            patch(
                "kbutillib.cli.init_notebook.resolve_alias",
                return_value="testbox",
            ),
            patch("shutil.which", return_value=None),
        ):
            result = runner.invoke(
                main,
                ["init-notebook", "--alias", "testbox"],
                catch_exceptions=False,
            )
        assert result.exit_code != 0
        assert "venvman" in result.output

    def test_notebook_kernel_pin_only_kbu_kernels(
        self, runner: CliRunner, project_dir: Path
    ) -> None:
        """Without --force, only kbu.nb-* kernels get overwritten, not external ones."""
        os.chdir(project_dir)
        nb_dir = project_dir / "notebooks"

        # Create a notebook with a non-kbu kernel
        import nbformat

        nb = nbformat.v4.new_notebook()
        nb.metadata["kernelspec"] = {
            "name": "narrative_kernel",
            "display_name": "Narrative",
            "language": "python",
        }
        nbformat.write(nb, str(nb_dir / "external.ipynb"))

        # Create a notebook with a kbu kernel
        nb2 = nbformat.v4.new_notebook()
        nb2.metadata["kernelspec"] = {
            "name": "kbu.nb-old-project",
            "display_name": "KBU: old-project",
            "language": "python",
        }
        nbformat.write(nb2, str(nb_dir / "kbu_notebook.ipynb"))

        # Create a notebook with no kernel
        nb3 = nbformat.v4.new_notebook()
        nbformat.write(nb3, str(nb_dir / "no_kernel.ipynb"))

        with (
            patch(
                "kbutillib.cli.init_notebook.load_machine_config",
                return_value=_DEFAULT_CONFIG.copy(),
            ),
            patch(
                "kbutillib.cli.init_notebook.resolve_alias",
                return_value="testbox",
            ),
            patch(
                "kbutillib.cli.init_notebook.subprocess.run",
                side_effect=_mock_subprocess_run,
            ),
            patch("shutil.which", return_value="/usr/local/bin/venvman"),
            patch.object(Path, "exists", side_effect=lambda self=None: True),
        ):
            result = runner.invoke(
                main,
                ["init-notebook", "--alias", "testbox"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0, result.output

        # external.ipynb should NOT be overwritten (non-kbu kernel)
        ext = nbformat.read(str(nb_dir / "external.ipynb"), as_version=4)
        assert ext.metadata["kernelspec"]["name"] == "narrative_kernel"

        # kbu_notebook.ipynb SHOULD be updated
        kbu = nbformat.read(str(nb_dir / "kbu_notebook.ipynb"), as_version=4)
        assert kbu.metadata["kernelspec"]["name"] == "kbu.nb-my-project"

        # no_kernel.ipynb SHOULD get a kernel assigned
        nk = nbformat.read(str(nb_dir / "no_kernel.ipynb"), as_version=4)
        assert nk.metadata["kernelspec"]["name"] == "kbu.nb-my-project"

    def test_force_overrides_external_kernels(
        self, runner: CliRunner, project_dir: Path
    ) -> None:
        """With --force, even non-kbu kernels get overwritten."""
        os.chdir(project_dir)
        nb_dir = project_dir / "notebooks"

        import nbformat

        nb = nbformat.v4.new_notebook()
        nb.metadata["kernelspec"] = {
            "name": "narrative_kernel",
            "display_name": "Narrative",
            "language": "python",
        }
        nbformat.write(nb, str(nb_dir / "external.ipynb"))

        # Write a util.py with the marker so --force smart-merge works
        (nb_dir / "util.py").write_text(
            f"# old header\n{_MARKER}\n# custom code\n"
        )

        with (
            patch(
                "kbutillib.cli.init_notebook.load_machine_config",
                return_value=_DEFAULT_CONFIG.copy(),
            ),
            patch(
                "kbutillib.cli.init_notebook.resolve_alias",
                return_value="testbox",
            ),
            patch(
                "kbutillib.cli.init_notebook.subprocess.run",
                side_effect=_mock_subprocess_run,
            ),
            patch("shutil.which", return_value="/usr/local/bin/venvman"),
            patch.object(Path, "exists", side_effect=lambda self=None: True),
        ):
            result = runner.invoke(
                main,
                ["init-notebook", "--alias", "testbox", "--force"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0, result.output

        ext = nbformat.read(str(nb_dir / "external.ipynb"), as_version=4)
        assert ext.metadata["kernelspec"]["name"] == "kbu.nb-my-project"
