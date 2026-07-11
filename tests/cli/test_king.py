"""Tests for ``kbu king`` — the KING self-install verb group.

Exercises the CLI (`kbu king install|uninstall|status`) against a temp
``$KING_APPS_DIR``, plus the vendored ``kbutillib.king_install`` module
directly for the union-recompose case (installing a second, independent
fixture bundle to prove this app's fragment survives -- the same on-disk
contract a sibling installer, e.g. ``assistant king install``, implements
independently). See `agent-io/prds/king-integration-apps/fullprompt.md`
Module C, Acceptance Criteria #13-#21.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from kbutillib import king_install
from kbutillib.cli import main

pytestmark = pytest.mark.king_install

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC_DIR = _REPO_ROOT / "src"
_BUNDLE_DIR = _SRC_DIR / "kbutillib" / "king_app"


# ── helpers ──────────────────────────────────────────────────────────────────


def _invoke(*args: str) -> Any:
    runner = CliRunner()
    return runner.invoke(main, list(args), catch_exceptions=False)


def _fake_kbu_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put a hermetic ``kbu`` shim on PATH that runs THIS worktree's kbutillib.

    Ignores whatever `kbu` may or may not be installed globally on this
    machine (which can lag behind an unmerged worktree) -- the shim embeds
    its own ``PYTHONPATH`` pointing at this checkout's ``src/``, so
    ``kbu model --help`` always reflects the code under test.
    """
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    kbu_script = bin_dir / "kbu"
    kbu_script.write_text(
        "#!/usr/bin/env bash\n"
        f'export PYTHONPATH="{_SRC_DIR}"\n'
        f'exec "{sys.executable}" -m kbutillib "$@"\n'
    )
    mode = kbu_script.stat().st_mode
    kbu_script.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv(
        "PATH", os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"])
    )
    return bin_dir


def _no_kbu_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty_bin = tmp_path / "emptybin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))


def _write_fixture_bundle(root: Path, app_id: str, title: str) -> Path:
    """A second, independent fixture bundle -- stands in for a sibling
    installer (e.g. AIAssistant's ``assistant king install``) to prove
    union-recompose without depending on any other repo's code."""
    bundle_dir = root / f"{app_id}-bundle"
    bundle_dir.mkdir()
    (bundle_dir / "bundle.json").write_text(
        json.dumps(
            {
                "id": app_id,
                "title": title,
                "description": f"Fixture app {app_id}.",
                "cli": "fixture-cli",
            }
        )
    )
    (bundle_dir / "skill.md").write_text(f"# {title}\n\nFixture skill body for {app_id}.\n")
    return bundle_dir


# ── install ──────────────────────────────────────────────────────────────────


class TestInstall:
    def test_install_writes_context_registry_and_serve_script(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_kbu_on_path(tmp_path, monkeypatch)
        apps_dir = tmp_path / "king-apps"
        king_stack_dir = tmp_path / "king-stack"  # deliberately never created

        r = _invoke(
            "king",
            "install",
            "--apps-dir",
            str(apps_dir),
            "--json",
        )
        assert r.exit_code == 0, r.output
        data = json.loads(r.output)
        assert data["id"] == "kbutillib-modeling"
        assert data["cli_on_path"] is True
        assert data["verify_probe_ok"] is True
        assert data["changed"] is True

        # registry.json has the id
        registry = json.loads((apps_dir / "registry.json").read_text())
        assert "kbutillib-modeling" in registry
        assert registry["kbutillib-modeling"]["cli"] == "kbu"

        # CONTEXT.md contains the exact header + a skill.md fragment
        context = (apps_dir / "CONTEXT.md").read_text()
        assert (
            "# [KING App] KBUtilLib Metabolic Modeling (id: kbutillib-modeling)"
            in context
        )
        assert "kbu model reconstruct" in context

        # serve-king.sh exports KING_CONTEXT and never writes under
        # ~/king-stack/king/ (the dir is referenced, but must not exist).
        serve_script = (apps_dir / "serve-king.sh").read_text()
        assert 'export KING_CONTEXT="' in serve_script
        assert str(apps_dir / "CONTEXT.md") in serve_script
        assert not king_stack_dir.exists()
        assert not (king_stack_dir / "king").exists()

    def test_install_idempotent_second_run_is_noop_diff(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_kbu_on_path(tmp_path, monkeypatch)
        apps_dir = tmp_path / "king-apps"

        r1 = _invoke("king", "install", "--apps-dir", str(apps_dir), "--json")
        assert r1.exit_code == 0, r1.output

        registry_before = (apps_dir / "registry.json").read_text()
        context_before = (apps_dir / "CONTEXT.md").read_text()
        serve_before = (apps_dir / "serve-king.sh").read_text()

        r2 = _invoke("king", "install", "--apps-dir", str(apps_dir), "--json")
        assert r2.exit_code == 0, r2.output
        data2 = json.loads(r2.output)
        assert data2["changed"] is False

        assert (apps_dir / "registry.json").read_text() == registry_before
        assert (apps_dir / "CONTEXT.md").read_text() == context_before
        assert (apps_dir / "serve-king.sh").read_text() == serve_before

    def test_install_union_recompose_keeps_other_apps_fragment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A second, independently-installed app must never clobber this
        app's CONTEXT.md fragment (or vice versa) -- proves the
        registry.json-union recompose contract that lets kbu's and
        assistant's installers coexist."""
        _fake_kbu_on_path(tmp_path, monkeypatch)
        apps_dir = tmp_path / "king-apps"

        r = _invoke("king", "install", "--apps-dir", str(apps_dir), "--json")
        assert r.exit_code == 0, r.output

        other_bundle_dir = _write_fixture_bundle(tmp_path, "aiassistant", "AIAssistant")
        king_install.install(other_bundle_dir, apps_dir=apps_dir)

        context = (apps_dir / "CONTEXT.md").read_text()
        assert (
            "# [KING App] KBUtilLib Metabolic Modeling (id: kbutillib-modeling)"
            in context
        )
        assert "# [KING App] AIAssistant (id: aiassistant)" in context
        assert "Fixture skill body for aiassistant" in context

        registry = json.loads((apps_dir / "registry.json").read_text())
        assert set(registry) == {"kbutillib-modeling", "aiassistant"}

        # ids appear exactly once each, in lexicographic order.
        idx_aia = context.index("(id: aiassistant)")
        idx_kbu = context.index("(id: kbutillib-modeling)")
        assert idx_aia < idx_kbu  # "aiassistant" < "kbutillib-modeling"
        assert context.count("(id: aiassistant)") == 1
        assert context.count("(id: kbutillib-modeling)") == 1

    def test_install_reports_state_when_cli_missing_never_crashes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _no_kbu_on_path(tmp_path, monkeypatch)
        apps_dir = tmp_path / "king-apps"

        r = _invoke("king", "install", "--apps-dir", str(apps_dir), "--json")
        assert r.exit_code == 0, r.output
        data = json.loads(r.output)
        assert data["cli_on_path"] is False
        assert data["verify_probe_ok"] is False
        # Composition still happens even though the hand is missing.
        assert (apps_dir / "registry.json").is_file()
        assert (apps_dir / "CONTEXT.md").is_file()


# ── status ───────────────────────────────────────────────────────────────────


class TestStatus:
    def test_status_green_when_cli_present_probe_passes_and_wired(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_kbu_on_path(tmp_path, monkeypatch)
        apps_dir = tmp_path / "king-apps"
        _invoke("king", "install", "--apps-dir", str(apps_dir), "--json")

        # No KING_CONTEXT set in the calling shell -- `king status` is
        # normally run right after `king install`, from an ordinary shell,
        # not from inside a KING launch. "Wired" must be judged from the
        # generated serve-king.sh wrapper, not the caller's live env.
        monkeypatch.delenv("KING_CONTEXT", raising=False)
        r = _invoke("king", "status", "--apps-dir", str(apps_dir), "--json")
        assert r.exit_code == 0, r.output
        data = json.loads(r.output)
        assert data["color"] == "green"
        assert data["cli_on_path"] is True
        assert data["verify_probe_ok"] is True
        assert data["context_has_header"] is True
        assert data["king_context_wired"] is True
        assert set(data["versions"]) == {"kbutillib", "cobra", "modelseedpy"}

    def test_status_green_via_live_king_context_env_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A KING_CONTEXT already correctly set in the current process env
        also counts (e.g. when status is checked from inside a KING-launched
        shell), even if the serve-king.sh wrapper were somehow unreadable."""
        _fake_kbu_on_path(tmp_path, monkeypatch)
        apps_dir = tmp_path / "king-apps"
        _invoke("king", "install", "--apps-dir", str(apps_dir), "--json")

        monkeypatch.setenv("KING_CONTEXT", str(apps_dir / "CONTEXT.md"))
        r = _invoke("king", "status", "--apps-dir", str(apps_dir), "--json")
        assert r.exit_code == 0, r.output
        data = json.loads(r.output)
        assert data["color"] == "green"
        assert data["king_context_wired"] is True

    def test_status_amber_when_cli_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_kbu_on_path(tmp_path, monkeypatch)
        apps_dir = tmp_path / "king-apps"
        _invoke("king", "install", "--apps-dir", str(apps_dir), "--json")

        _no_kbu_on_path(tmp_path, monkeypatch)
        r = _invoke("king", "status", "--apps-dir", str(apps_dir), "--json")
        assert r.exit_code == 1
        data = json.loads(r.output)
        assert data["color"] == "amber"
        assert data["cli_on_path"] is False
        assert data["remediation"] is not None

    def test_status_red_when_king_context_not_wired(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_kbu_on_path(tmp_path, monkeypatch)
        apps_dir = tmp_path / "king-apps"
        _invoke("king", "install", "--apps-dir", str(apps_dir), "--json")

        # Actually break the wiring: corrupt the generated serve-king.sh so
        # it no longer exports KING_CONTEXT, and make sure the calling
        # shell doesn't have it set either. (Merely unsetting the calling
        # shell's env is NOT broken wiring -- serve-king.sh is what wires
        # KING_CONTEXT for the real launch; status must judge "wired" from
        # that generated script, not the caller's own env.)
        (apps_dir / "serve-king.sh").write_text("#!/usr/bin/env bash\necho stub\n")
        monkeypatch.delenv("KING_CONTEXT", raising=False)
        r = _invoke("king", "status", "--apps-dir", str(apps_dir), "--json")
        assert r.exit_code == 2
        data = json.loads(r.output)
        assert data["color"] == "red"
        assert data["cli_on_path"] is True
        assert data["king_context_wired"] is False


# ── uninstall ────────────────────────────────────────────────────────────────


class TestUninstall:
    def test_uninstall_removes_id_and_recomposes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_kbu_on_path(tmp_path, monkeypatch)
        apps_dir = tmp_path / "king-apps"
        _invoke("king", "install", "--apps-dir", str(apps_dir), "--json")
        assert (apps_dir / "kbutillib-modeling").is_dir()

        r = _invoke("king", "uninstall", "--apps-dir", str(apps_dir), "--json")
        assert r.exit_code == 0, r.output
        data = json.loads(r.output)
        assert data["removed"] is True

        assert not (apps_dir / "kbutillib-modeling").is_dir()
        registry = json.loads((apps_dir / "registry.json").read_text())
        assert "kbutillib-modeling" not in registry
        context = (apps_dir / "CONTEXT.md").read_text()
        assert "kbutillib-modeling" not in context

    def test_uninstall_keeps_other_apps_fragment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_kbu_on_path(tmp_path, monkeypatch)
        apps_dir = tmp_path / "king-apps"
        _invoke("king", "install", "--apps-dir", str(apps_dir), "--json")

        other_bundle_dir = _write_fixture_bundle(tmp_path, "aiassistant", "AIAssistant")
        king_install.install(other_bundle_dir, apps_dir=apps_dir)

        r = _invoke("king", "uninstall", "--apps-dir", str(apps_dir), "--json")
        assert r.exit_code == 0, r.output

        context = (apps_dir / "CONTEXT.md").read_text()
        assert "kbutillib-modeling" not in context
        assert "# [KING App] AIAssistant (id: aiassistant)" in context

    def test_uninstall_already_absent_is_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_kbu_on_path(tmp_path, monkeypatch)
        apps_dir = tmp_path / "king-apps"

        r = _invoke("king", "uninstall", "--apps-dir", str(apps_dir), "--json")
        assert r.exit_code == 0, r.output
        data = json.loads(r.output)
        assert data["removed"] is False


# ── bundle schema (AC #14) ───────────────────────────────────────────────────


class TestBundleSchema:
    def test_packaged_bundle_conforms_to_schema(self) -> None:
        loaded = king_install.load_bundle(_BUNDLE_DIR)
        bundle = loaded["bundle"]
        assert set(bundle) >= {"id", "title", "description", "cli"}
        assert bundle["cli"] == "kbu"
        assert bundle["verify"]["cmd"] == ["kbu", "model", "--help"]
        assert loaded["skill_md"].strip().startswith("#")

    def test_load_bundle_missing_field_raises(self, tmp_path: Path) -> None:
        bad_dir = tmp_path / "bad-bundle"
        bad_dir.mkdir()
        (bad_dir / "bundle.json").write_text(json.dumps({"id": "x"}))
        (bad_dir / "skill.md").write_text("# X\n")
        with pytest.raises(king_install.BundleError):
            king_install.load_bundle(bad_dir)
