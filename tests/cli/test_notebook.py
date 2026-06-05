"""Tests for kbutillib.cli.notebook — list, mark-run, exec subcommands."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import nbformat
import pytest
from click.testing import CliRunner

from kbutillib.cli import main
from kbutillib.cli.manifest import (
    now_utc_iso,
    read_subproject_manifest,
    write_project_manifest,
    write_subproject_manifest,
)
from kbutillib.cli.notebook import list_notebooks, mark_run


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_project(tmp_path: Path, name: str = "myproj") -> Path:
    """Create a kbu project directory with kbu-project.toml."""
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    now = now_utc_iso()
    write_project_manifest(root, {
        "project": {"name": name, "title": name, "created_at": now},
        "kbutillib": {"source_path": "/fake", "source_commit": "abc"},
        "update": {"last_pulled_at": now, "last_pulled_commit": "abc"},
    })
    return root


def _create_subproject(root: Path, sp_name: str, created_at: str = "") -> Path:
    """Create a subproject directory and manifest."""
    now = created_at or now_utc_iso()
    sp_dir = root / "subprojects" / sp_name
    sp_dir.mkdir(parents=True, exist_ok=True)
    (sp_dir / "notebooks").mkdir(exist_ok=True)
    (sp_dir / "sessions").mkdir(exist_ok=True)
    data: dict[str, Any] = {
        "subproject": {
            "name": sp_name,
            "title": sp_name,
            "status": "plan",
            "created_at": now,
            "last_session_at": now,
        },
        "artifacts": {
            "research_plan": False,
            "report": False,
            "reviews": {"plan": [], "build": [], "synthesis": []},
        },
        "notebooks": [],
        "session_refs": [],
    }
    write_subproject_manifest(root, sp_name, data)
    return sp_dir


def _write_notebook(nb_dir: Path, name: str, cells: list[dict]) -> Path:
    """Write a minimal nbformat v4 notebook to *nb_dir/<name>*."""
    nb = nbformat.v4.new_notebook()
    nb.cells = []
    for c in cells:
        cell_type = c.get("type", "code")
        source = c.get("source", "")
        if cell_type == "code":
            nb.cells.append(nbformat.v4.new_code_cell(source))
        else:
            nb.cells.append(nbformat.v4.new_markdown_cell(source))
    path = nb_dir / name
    with open(path, "w", encoding="utf-8") as fh:
        nbformat.write(nb, fh)
    return path


def _invoke(root: Path, *args: str) -> Any:
    """Invoke the kbu CLI from *root* as cwd."""
    runner = CliRunner()
    saved = os.getcwd()
    try:
        os.chdir(root)
        return runner.invoke(main, ["notebook", *args], catch_exceptions=False)
    finally:
        os.chdir(saved)


# ── TestHelp ──────────────────────────────────────────────────────────────────


class TestHelp:
    def test_notebook_help_lists_subcommands(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["notebook", "--help"], catch_exceptions=False)
        assert result.exit_code == 0
        for cmd in ("list", "mark-run", "exec"):
            assert cmd in result.output

    def test_top_level_help_lists_notebook(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["--help"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "notebook" in result.output


# ── TestListNotebooks ─────────────────────────────────────────────────────────


class TestListNotebooks:
    def test_empty_project_just_header(self, tmp_path: Path) -> None:
        root = _make_project(tmp_path)
        result = _invoke(root, "list")
        assert result.exit_code == 0, result.output
        lines = [ln for ln in result.output.splitlines() if ln.strip()]
        assert len(lines) == 1
        assert lines[0] == "path\tsubproject\tlast_run_at\tmodified_since_run"

    def test_single_subproject_two_notebooks_three_lines(self, tmp_path: Path) -> None:
        """Header + 2 notebooks = exactly 3 output lines."""
        root = _make_project(tmp_path)
        sp_dir = _create_subproject(root, "sp1")
        nb_dir = sp_dir / "notebooks"
        _write_notebook(nb_dir, "01_intro.ipynb", [{"source": "1+1"}])
        _write_notebook(nb_dir, "02_analysis.ipynb", [{"source": "2+2"}])

        result = _invoke(root, "list")
        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        assert len(lines) == 3
        assert lines[0] == "path\tsubproject\tlast_run_at\tmodified_since_run"
        assert "01_intro.ipynb" in lines[1]
        assert "02_analysis.ipynb" in lines[2]

    def test_tsv_columns_correct(self, tmp_path: Path) -> None:
        root = _make_project(tmp_path)
        sp_dir = _create_subproject(root, "sp1")
        nb_path = _write_notebook(sp_dir / "notebooks", "test.ipynb", [{"source": "x=1"}])

        result = _invoke(root, "list")
        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        assert len(lines) == 2
        cols = lines[1].split("\t")
        assert len(cols) == 4
        assert cols[0] == str(nb_path)
        assert cols[1] == "sp1"
        assert cols[2] == ""          # never run → empty last_run_at
        assert cols[3] == "true"      # never run → modified_since_run

    def test_last_run_at_populated_after_mark_run(self, tmp_path: Path) -> None:
        root = _make_project(tmp_path)
        sp_dir = _create_subproject(root, "sp1")
        nb_path = _write_notebook(sp_dir / "notebooks", "nb.ipynb", [{"source": "1"}])

        mark_run(nb_path)

        records = list_notebooks(root)
        assert len(records) == 1
        assert records[0]["last_run_at"] != ""

    def test_modified_since_run_false_when_not_modified(self, tmp_path: Path) -> None:
        """After mark_run, modified_since_run is False (file older than run time)."""
        root = _make_project(tmp_path)
        sp_dir = _create_subproject(root, "sp1")
        nb_path = _write_notebook(sp_dir / "notebooks", "nb.ipynb", [{"source": "1"}])

        # Backdate the file so its mtime is before the mark_run timestamp
        old_mtime = time.time() - 60
        os.utime(nb_path, (old_mtime, old_mtime))

        mark_run(nb_path)

        records = list_notebooks(root)
        assert len(records) == 1
        assert records[0]["modified_since_run"] is False

    def test_modified_since_run_true_when_edited_after_run(self, tmp_path: Path) -> None:
        root = _make_project(tmp_path)
        sp_dir = _create_subproject(root, "sp1")
        nb_path = _write_notebook(sp_dir / "notebooks", "nb.ipynb", [{"source": "1"}])

        # Mark run first, then bump mtime to simulate editing
        mark_run(nb_path)
        future_mtime = time.time() + 120
        os.utime(nb_path, (future_mtime, future_mtime))

        records = list_notebooks(root)
        assert len(records) == 1
        assert records[0]["modified_since_run"] is True

    def test_multiple_subprojects_ordered_by_creation_time(self, tmp_path: Path) -> None:
        """Subprojects ordered oldest-first by created_at."""
        root = _make_project(tmp_path)
        # Create sp_b before sp_a in time so sp_b appears first in list
        sp_b_dir = _create_subproject(root, "sp_b", created_at="2026-01-01T00:00:00Z")
        sp_a_dir = _create_subproject(root, "sp_a", created_at="2026-06-01T00:00:00Z")
        _write_notebook(sp_b_dir / "notebooks", "b.ipynb", [{"source": "1"}])
        _write_notebook(sp_a_dir / "notebooks", "a.ipynb", [{"source": "1"}])

        result = _invoke(root, "list")
        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        # header + 2 notebooks
        assert len(lines) == 3
        # sp_b (older created_at) should come first
        assert "sp_b" in lines[1]
        assert "sp_a" in lines[2]

    def test_json_flag_returns_list(self, tmp_path: Path) -> None:
        root = _make_project(tmp_path)
        sp_dir = _create_subproject(root, "sp1")
        _write_notebook(sp_dir / "notebooks", "nb.ipynb", [{"source": "1"}])

        result = _invoke(root, "list", "--json")
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 1
        assert "path" in data[0]
        assert "subproject" in data[0]
        assert "last_run_at" in data[0]
        assert "modified_since_run" in data[0]

    def test_mixed_run_state_two_subprojects(self, tmp_path: Path) -> None:
        """Two subprojects: one notebook run, one not."""
        root = _make_project(tmp_path)
        sp1_dir = _create_subproject(root, "sp1", created_at="2026-01-01T00:00:00Z")
        sp2_dir = _create_subproject(root, "sp2", created_at="2026-02-01T00:00:00Z")

        nb1 = _write_notebook(sp1_dir / "notebooks", "nb1.ipynb", [{"source": "1"}])
        nb2 = _write_notebook(sp2_dir / "notebooks", "nb2.ipynb", [{"source": "2"}])

        # Backdate nb1, then mark_run it so modified_since_run is False
        old_mtime = time.time() - 60
        os.utime(nb1, (old_mtime, old_mtime))
        mark_run(nb1)

        records = list_notebooks(root)
        assert len(records) == 2
        sp1_rec = next(r for r in records if r["subproject"] == "sp1")
        sp2_rec = next(r for r in records if r["subproject"] == "sp2")
        assert sp1_rec["last_run_at"] != ""
        assert sp1_rec["modified_since_run"] is False
        assert sp2_rec["last_run_at"] == ""
        assert sp2_rec["modified_since_run"] is True


# ── TestMarkRun ───────────────────────────────────────────────────────────────


class TestMarkRun:
    def test_creates_entry_in_manifest(self, tmp_path: Path) -> None:
        root = _make_project(tmp_path)
        sp_dir = _create_subproject(root, "sp1")
        nb_path = _write_notebook(sp_dir / "notebooks", "nb.ipynb", [{"source": "1"}])

        mark_run(nb_path)

        data = read_subproject_manifest(root, "sp1")
        assert len(data["notebooks"]) == 1
        entry = data["notebooks"][0]
        assert entry["path"] == "notebooks/nb.ipynb"
        assert entry["last_run_at"] != ""
        assert entry["modified_since_run"] is False

    def test_updates_existing_entry_preserves_others(self, tmp_path: Path) -> None:
        """mark_run updates last_run_at without losing other manifest fields."""
        root = _make_project(tmp_path)
        sp_dir = _create_subproject(root, "sp1")
        nb_path = _write_notebook(sp_dir / "notebooks", "nb.ipynb", [{"source": "1"}])

        # First run
        mark_run(nb_path)
        data_after_first = read_subproject_manifest(root, "sp1")
        first_ts = data_after_first["notebooks"][0]["last_run_at"]

        # Second run (timestamp should update)
        time.sleep(0.05)  # ensure a different second if clock resolution allows
        mark_run(nb_path)
        data_after_second = read_subproject_manifest(root, "sp1")

        # Still only one entry
        assert len(data_after_second["notebooks"]) == 1
        # Other manifest fields still intact
        assert "subproject" in data_after_second
        assert data_after_second["subproject"]["name"] == "sp1"

    def test_mark_run_preserves_sibling_notebook_entries(self, tmp_path: Path) -> None:
        root = _make_project(tmp_path)
        sp_dir = _create_subproject(root, "sp1")
        nb1 = _write_notebook(sp_dir / "notebooks", "nb1.ipynb", [{"source": "1"}])
        nb2 = _write_notebook(sp_dir / "notebooks", "nb2.ipynb", [{"source": "2"}])

        mark_run(nb1)
        mark_run(nb2)

        data = read_subproject_manifest(root, "sp1")
        paths = {e["path"] for e in data["notebooks"]}
        assert "notebooks/nb1.ipynb" in paths
        assert "notebooks/nb2.ipynb" in paths

    def test_mark_run_outside_subproject_raises(self, tmp_path: Path) -> None:
        """mark_run raises ValueError for a notebook not inside a subproject."""
        root = _make_project(tmp_path)
        # Put a notebook at the project root, not inside subprojects/
        nb_path = _write_notebook(root, "toplevel.ipynb", [{"source": "1"}])

        with pytest.raises(ValueError, match="subproject"):
            mark_run(nb_path)

    def test_cli_mark_run_round_trip(self, tmp_path: Path) -> None:
        root = _make_project(tmp_path)
        sp_dir = _create_subproject(root, "sp1")
        nb_path = _write_notebook(sp_dir / "notebooks", "nb.ipynb", [{"source": "1"}])

        result = _invoke(root, "mark-run", str(nb_path))
        assert result.exit_code == 0, result.output

        data = read_subproject_manifest(root, "sp1")
        assert len(data["notebooks"]) == 1
        assert data["notebooks"][0]["last_run_at"] != ""


# ── TestExecNotebook ──────────────────────────────────────────────────────────


class TestExecNotebook:
    def test_happy_path_executes_and_marks_run(self, tmp_path: Path) -> None:
        """Passing notebook executes, backup is created, last_run_at updated."""
        root = _make_project(tmp_path)
        sp_dir = _create_subproject(root, "sp1")
        nb_path = _write_notebook(sp_dir / "notebooks", "pass.ipynb", [
            {"source": "result = 1 + 1"}
        ])

        result = _invoke(root, "exec", str(nb_path))
        assert result.exit_code == 0, result.output

        # Backup file created
        backups = list((sp_dir / "notebooks").glob("pass.bak.*.ipynb"))
        assert len(backups) == 1
        bak_name = backups[0].name
        # Timestamp format: pass.bak.<YYYYmmddTHHMMSSZ>.ipynb
        assert bak_name.startswith("pass.bak.")
        assert bak_name.endswith(".ipynb")

        # last_run_at set in manifest
        data = read_subproject_manifest(root, "sp1")
        assert len(data["notebooks"]) == 1
        assert data["notebooks"][0]["last_run_at"] != ""

    def test_backup_timestamp_format(self, tmp_path: Path) -> None:
        """Backup file name ends with .<YYYYmmddTHHMMSSZ>.ipynb."""
        import re
        root = _make_project(tmp_path)
        sp_dir = _create_subproject(root, "sp1")
        nb_path = _write_notebook(sp_dir / "notebooks", "nb.ipynb", [
            {"source": "x = 42"}
        ])

        result = _invoke(root, "exec", str(nb_path))
        assert result.exit_code == 0, result.output

        backups = list((sp_dir / "notebooks").glob("nb.bak.*.ipynb"))
        assert len(backups) == 1
        ts_part = backups[0].stem.split(".bak.")[1]
        assert re.fullmatch(r"\d{8}T\d{6}Z", ts_part), f"Unexpected timestamp: {ts_part}"

    def test_stop_on_error_by_default(self, tmp_path: Path) -> None:
        """A cell that raises stops execution when allow_errors=False."""
        from nbclient.exceptions import CellExecutionError
        from kbutillib.cli.notebook import exec_notebook

        root = _make_project(tmp_path)
        sp_dir = _create_subproject(root, "sp1")
        nb_path = _write_notebook(sp_dir / "notebooks", "err.ipynb", [
            {"source": 'raise ValueError("intentional error")'},
            {"source": "never_reached = True"},
        ])

        with pytest.raises(CellExecutionError):
            exec_notebook(nb_path, allow_errors=False)

    def test_cli_stop_on_error_exits_nonzero(self, tmp_path: Path) -> None:
        """CLI exec without --allow-errors exits non-zero on a failing cell."""
        root = _make_project(tmp_path)
        sp_dir = _create_subproject(root, "sp1")
        nb_path = _write_notebook(sp_dir / "notebooks", "err.ipynb", [
            {"source": 'raise ValueError("boom")'},
        ])

        result = _invoke(root, "exec", str(nb_path))
        assert result.exit_code != 0

    def test_allow_errors_continues(self, tmp_path: Path) -> None:
        """With --allow-errors, execution continues past cell errors."""
        root = _make_project(tmp_path)
        sp_dir = _create_subproject(root, "sp1")
        nb_path = _write_notebook(sp_dir / "notebooks", "err.ipynb", [
            {"source": 'raise ValueError("non-fatal")'},
            {"source": "x = 99"},
        ])

        result = _invoke(root, "exec", "--allow-errors", str(nb_path))
        assert result.exit_code == 0, result.output

        # Verify the notebook was written back (second cell ran)
        with open(nb_path, encoding="utf-8") as fh:
            nb = nbformat.read(fh, as_version=4)
        # Second cell should have an execution count
        assert nb.cells[1].execution_count is not None

    def test_kernel_fallback_to_python3(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When project kernel is not found, falls back to python3 with warning."""
        from kbutillib.cli import notebook as nb_mod

        root = _make_project(tmp_path, name="nonexistent_kernel_proj")
        sp_dir = _create_subproject(root, "sp1")
        nb_path = _write_notebook(sp_dir / "notebooks", "nb.ipynb", [
            {"source": "x = 1"}
        ])

        warnings_captured: list[str] = []

        def fake_select_kernel(project_root: Path) -> str:
            warnings_captured.append("fallback")
            return "python3"

        monkeypatch.setattr(nb_mod, "_select_kernel", fake_select_kernel)

        result = _invoke(root, "exec", str(nb_path))
        assert result.exit_code == 0, result.output
        assert len(warnings_captured) == 1

    def test_kernel_fallback_warning_on_missing_kernel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_select_kernel emits a warning and returns 'python3' when project kernel absent."""
        from kbutillib.cli.notebook import _select_kernel
        from jupyter_client.kernelspec import find_kernel_specs

        root = _make_project(tmp_path, name="no_such_kernel_xyz")

        # Verify python3 is available (so fallback path is exercised)
        specs = find_kernel_specs()
        if "python3" not in specs:
            pytest.skip("python3 kernel not available in this environment")

        # The project name "no_such_kernel_xyz" won't match any installed kernel
        kernel = _select_kernel(root)
        assert kernel == "python3"

    def test_cell_timeout_via_env_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """KBU_NOTEBOOK_CELL_TIMEOUT=1 causes a sleep(5) cell to time out."""
        from nbclient.exceptions import CellTimeoutError
        from kbutillib.cli.notebook import exec_notebook

        root = _make_project(tmp_path)
        sp_dir = _create_subproject(root, "sp1")
        nb_path = _write_notebook(sp_dir / "notebooks", "slow.ipynb", [
            {"source": "import time; time.sleep(5)"},
        ])

        monkeypatch.setenv("KBU_NOTEBOOK_CELL_TIMEOUT", "1")

        with pytest.raises(CellTimeoutError):
            exec_notebook(nb_path, allow_errors=False)

    def test_cell_timeout_env_var_cli(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CLI exec with KBU_NOTEBOOK_CELL_TIMEOUT=1 exits non-zero on timeout."""
        root = _make_project(tmp_path)
        sp_dir = _create_subproject(root, "sp1")
        nb_path = _write_notebook(sp_dir / "notebooks", "slow.ipynb", [
            {"source": "import time; time.sleep(5)"},
        ])

        monkeypatch.setenv("KBU_NOTEBOOK_CELL_TIMEOUT", "1")

        result = _invoke(root, "exec", str(nb_path))
        assert result.exit_code != 0
        assert "timeout" in result.output.lower() or "timed" in result.output.lower()

    def test_exec_updates_manifest_last_run_at(self, tmp_path: Path) -> None:
        """After successful exec, last_run_at is updated in the subproject manifest."""
        root = _make_project(tmp_path)
        sp_dir = _create_subproject(root, "sp1")
        nb_path = _write_notebook(sp_dir / "notebooks", "nb.ipynb", [
            {"source": "y = 2 * 3"},
        ])

        result = _invoke(root, "exec", str(nb_path))
        assert result.exit_code == 0, result.output

        data = read_subproject_manifest(root, "sp1")
        nb_entries = data.get("notebooks", [])
        assert len(nb_entries) == 1
        assert nb_entries[0]["path"] == "notebooks/nb.ipynb"
        assert nb_entries[0]["last_run_at"] != ""
