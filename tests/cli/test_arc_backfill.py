"""CLI tests for ``kbu arc backfill`` and the ``kbu kind install`` manifest step.

Exercises the command wiring end to end through Click against a temp runs tree
and a temp ``$KING_PLUGINS_DIR``: the ``arc`` group is registered, ``backfill``
honours ``--dry-run``, and ``kbu kind install`` writes the app manifest into the
plugin-union directory (never ``king/plugins/``).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from kbutillib.arc_context import APP_ID
from kbutillib.interfaces.cli import main


@pytest.fixture
def runs_root(tmp_path, monkeypatch):
    arc = tmp_path / "runs" / "projA" / "arcs" / "arc1"
    arc.mkdir(parents=True)
    (arc / "ecoli.model.json").write_text("{}")
    # KOROS_HOME/runs is the store's first resolution branch.
    monkeypatch.setenv("KOROS_HOME", str(tmp_path))
    return tmp_path / "runs"


class TestArcGroupRegistered:
    def test_arc_backfill_is_a_command(self):
        result = CliRunner().invoke(main, ["arc", "--help"])
        assert result.exit_code == 0
        assert "backfill" in result.output


class TestBackfillCli:
    def test_dry_run_writes_nothing_and_reports(self, runs_root, monkeypatch):
        # Disable the run DB so a real store write path is inert; dry-run must
        # not write regardless.
        monkeypatch.setenv("KBDL_RUN_DB_ENABLED", "0")
        result = CliRunner().invoke(
            main, ["arc", "backfill", "--dry-run", "--no-store"]
        )
        assert result.exit_code == 0, result.output
        assert "arcs scanned" in result.output
        assert "dry-run" in result.output

    def test_backfill_runs_and_reports_coverage(self, runs_root, monkeypatch):
        monkeypatch.setenv("KBDL_RUN_DB_ENABLED", "1")
        result = CliRunner().invoke(main, ["arc", "backfill", "--no-store"])
        assert result.exit_code == 0, result.output
        assert "arcs scanned:    1" in result.output
        assert "records written:" in result.output


class TestKindInstallWritesManifest:
    def test_install_writes_manifest_to_union_not_king_plugins(
        self, tmp_path, monkeypatch
    ):
        # Point the union dir at a temp path; install must place the app manifest
        # there and never in king/plugins.
        home = tmp_path / "home"
        home.mkdir()
        upstream = home / "king-stack" / "king" / "plugins"
        upstream.mkdir(parents=True)
        (upstream / "genepool.json").write_text("{}")
        union = tmp_path / "union"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("KING_PLUGINS_DIR", str(union))
        # Keep the bundle install side inert-ish by pointing KIND_APPS_DIR at a
        # temp dir; the bundle install may still warn, but the manifest step is
        # what we assert.
        monkeypatch.setenv("KIND_APPS_DIR", str(tmp_path / "apps"))
        monkeypatch.setenv("KING_STACK_DIR", str(home / "king-stack"))

        # The bundle-install step may hard-fail on an environment with no KING
        # launcher, but the manifest registration runs FIRST and independently,
        # so the manifest must land in the union dir regardless of the overall
        # exit code — and never in king/plugins.
        CliRunner().invoke(main, ["kind", "install", "--json"])
        manifest_path = union / (APP_ID + ".json")
        assert manifest_path.is_file()
        data = manifest_path.read_text()
        assert '"type": "app"' in data
        assert not (upstream / (APP_ID + ".json")).exists()
