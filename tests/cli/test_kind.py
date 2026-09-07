"""Tests for ``kbu kind`` — the KING self-install verb group.

Exercises the CLI (`kbu kind install|uninstall|status`) against a temp
``$KIND_APPS_DIR``, plus the vendored ``kbutillib.kind_install`` module
directly for the union-recompose case (installing a second, independent
fixture bundle to prove this app's fragment survives -- the same on-disk
contract a sibling installer, e.g. ``assistant kind install``, implements
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

from kbutillib.agents import kind_install
from kbutillib.cli import main

pytestmark = pytest.mark.kind_install

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC_DIR = _REPO_ROOT / "src"
_BUNDLE_DIR = _SRC_DIR / "kbutillib" / "kind_app"


# ── helpers ──────────────────────────────────────────────────────────────────


def _json_one(result: Any) -> Any:
    """Sole element of a ``--json`` payload, which is always a LIST.

    ``kbu kind`` grew a second bundle (``persistentai-wake``), so ``--json``
    emits one object per app acted on -- a list even for a single ``--app``.
    Asserting the length here also proves the ``--app`` selector really
    narrowed the run rather than silently acting on everything.
    """
    data = json.loads(result.output)
    assert isinstance(data, list), data
    assert len(data) == 1, data
    return data[0]


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
    installer (e.g. AIAssistant's ``assistant kind install``) to prove
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
        apps_dir = tmp_path / "kind-apps"
        king_stack_dir = tmp_path / "king-stack"  # deliberately never created

        r = _invoke(
            "king",
            "install",
            "--app",
            "modeling",
            "--apps-dir",
            str(apps_dir),
            "--json",
        )
        assert r.exit_code == 0, r.output
        data = _json_one(r)
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
            "# [KIND App] KBUtilLib Metabolic Modeling (id: kbutillib-modeling)"
            in context
        )
        assert "kbu model reconstruct" in context

        # serve-kind.sh exports KING_CONTEXT and never writes under
        # ~/king-stack/king/ (the dir is referenced, but must not exist).
        serve_script = (apps_dir / "serve-kind.sh").read_text()
        assert 'export KING_CONTEXT="' in serve_script
        assert str(apps_dir / "CONTEXT.md") in serve_script
        assert not king_stack_dir.exists()
        assert not (king_stack_dir / "king").exists()

    def test_install_idempotent_second_run_is_noop_diff(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_kbu_on_path(tmp_path, monkeypatch)
        apps_dir = tmp_path / "kind-apps"

        r1 = _invoke("king", "install", "--app", "modeling", "--apps-dir", str(apps_dir), "--json")
        assert r1.exit_code == 0, r1.output

        registry_before = (apps_dir / "registry.json").read_text()
        context_before = (apps_dir / "CONTEXT.md").read_text()
        serve_before = (apps_dir / "serve-kind.sh").read_text()

        r2 = _invoke("king", "install", "--app", "modeling", "--apps-dir", str(apps_dir), "--json")
        assert r2.exit_code == 0, r2.output
        data2 = _json_one(r2)
        assert data2["changed"] is False

        assert (apps_dir / "registry.json").read_text() == registry_before
        assert (apps_dir / "CONTEXT.md").read_text() == context_before
        assert (apps_dir / "serve-kind.sh").read_text() == serve_before

    def test_install_union_recompose_keeps_other_apps_fragment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A second, independently-installed app must never clobber this
        app's CONTEXT.md fragment (or vice versa) -- proves the
        registry.json-union recompose contract that lets kbu's and
        assistant's installers coexist."""
        _fake_kbu_on_path(tmp_path, monkeypatch)
        apps_dir = tmp_path / "kind-apps"

        r = _invoke("king", "install", "--app", "modeling", "--apps-dir", str(apps_dir), "--json")
        assert r.exit_code == 0, r.output

        other_bundle_dir = _write_fixture_bundle(tmp_path, "aiassistant", "AIAssistant")
        kind_install.install(other_bundle_dir, apps_dir=apps_dir)

        context = (apps_dir / "CONTEXT.md").read_text()
        assert (
            "# [KIND App] KBUtilLib Metabolic Modeling (id: kbutillib-modeling)"
            in context
        )
        assert "# [KIND App] AIAssistant (id: aiassistant)" in context
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
        apps_dir = tmp_path / "kind-apps"

        r = _invoke("king", "install", "--app", "modeling", "--apps-dir", str(apps_dir), "--json")
        assert r.exit_code == 0, r.output
        data = _json_one(r)
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
        apps_dir = tmp_path / "kind-apps"
        _invoke("king", "install", "--app", "modeling", "--apps-dir", str(apps_dir), "--json")

        # No KING_CONTEXT set in the calling shell -- `king status` is
        # normally run right after `king install`, from an ordinary shell,
        # not from inside a KING launch. "Wired" must be judged from the
        # generated serve-kind.sh wrapper, not the caller's live env.
        monkeypatch.delenv("KING_CONTEXT", raising=False)
        r = _invoke("king", "status", "--app", "modeling", "--apps-dir", str(apps_dir), "--json")
        assert r.exit_code == 0, r.output
        data = _json_one(r)
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
        shell), even if the serve-kind.sh wrapper were somehow unreadable."""
        _fake_kbu_on_path(tmp_path, monkeypatch)
        apps_dir = tmp_path / "kind-apps"
        _invoke("king", "install", "--app", "modeling", "--apps-dir", str(apps_dir), "--json")

        monkeypatch.setenv("KING_CONTEXT", str(apps_dir / "CONTEXT.md"))
        r = _invoke("king", "status", "--app", "modeling", "--apps-dir", str(apps_dir), "--json")
        assert r.exit_code == 0, r.output
        data = _json_one(r)
        assert data["color"] == "green"
        assert data["king_context_wired"] is True

    def test_status_amber_when_cli_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_kbu_on_path(tmp_path, monkeypatch)
        apps_dir = tmp_path / "kind-apps"
        _invoke("king", "install", "--app", "modeling", "--apps-dir", str(apps_dir), "--json")

        _no_kbu_on_path(tmp_path, monkeypatch)
        r = _invoke("king", "status", "--app", "modeling", "--apps-dir", str(apps_dir), "--json")
        assert r.exit_code == 1
        data = _json_one(r)
        assert data["color"] == "amber"
        assert data["cli_on_path"] is False
        assert data["remediation"] is not None

    def test_status_red_when_king_context_not_wired(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_kbu_on_path(tmp_path, monkeypatch)
        apps_dir = tmp_path / "kind-apps"
        _invoke("king", "install", "--app", "modeling", "--apps-dir", str(apps_dir), "--json")

        # Actually break the wiring: corrupt the generated serve-kind.sh so
        # it no longer exports KING_CONTEXT, and make sure the calling
        # shell doesn't have it set either. (Merely unsetting the calling
        # shell's env is NOT broken wiring -- serve-kind.sh is what wires
        # KING_CONTEXT for the real launch; status must judge "wired" from
        # that generated script, not the caller's own env.)
        (apps_dir / "serve-kind.sh").write_text("#!/usr/bin/env bash\necho stub\n")
        monkeypatch.delenv("KING_CONTEXT", raising=False)
        r = _invoke("king", "status", "--app", "modeling", "--apps-dir", str(apps_dir), "--json")
        assert r.exit_code == 2
        data = _json_one(r)
        assert data["color"] == "red"
        assert data["cli_on_path"] is True
        assert data["king_context_wired"] is False


# ── uninstall ────────────────────────────────────────────────────────────────


class TestUninstall:
    def test_uninstall_removes_id_and_recomposes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_kbu_on_path(tmp_path, monkeypatch)
        apps_dir = tmp_path / "kind-apps"
        _invoke("king", "install", "--app", "modeling", "--apps-dir", str(apps_dir), "--json")
        assert (apps_dir / "kbutillib-modeling").is_dir()

        r = _invoke("king", "uninstall", "--app", "modeling", "--apps-dir", str(apps_dir), "--json")
        assert r.exit_code == 0, r.output
        data = _json_one(r)
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
        apps_dir = tmp_path / "kind-apps"
        _invoke("king", "install", "--app", "modeling", "--apps-dir", str(apps_dir), "--json")

        other_bundle_dir = _write_fixture_bundle(tmp_path, "aiassistant", "AIAssistant")
        kind_install.install(other_bundle_dir, apps_dir=apps_dir)

        r = _invoke("king", "uninstall", "--app", "modeling", "--apps-dir", str(apps_dir), "--json")
        assert r.exit_code == 0, r.output

        context = (apps_dir / "CONTEXT.md").read_text()
        assert "kbutillib-modeling" not in context
        assert "# [KIND App] AIAssistant (id: aiassistant)" in context

    def test_uninstall_already_absent_is_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_kbu_on_path(tmp_path, monkeypatch)
        apps_dir = tmp_path / "kind-apps"

        r = _invoke("king", "uninstall", "--app", "modeling", "--apps-dir", str(apps_dir), "--json")
        assert r.exit_code == 0, r.output
        data = _json_one(r)
        assert data["removed"] is False


# ── bundle schema (AC #14) ───────────────────────────────────────────────────


class TestBundleSchema:
    def test_packaged_bundle_conforms_to_schema(self) -> None:
        loaded = kind_install.load_bundle(_BUNDLE_DIR)
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
        with pytest.raises(kind_install.BundleError):
            kind_install.load_bundle(bad_dir)


# ── the wake bundle (persistentai-wake) ──────────────────────────────────────


_WAKE_BUNDLE_DIR = _SRC_DIR / "kbutillib" / "kind_app_wake"


class TestWakeBundle:
    """This repo's SECOND KING app: firing a triggered wake at luna/miles.

    Its `cli` is `persistentai`, which lives in another repo and is
    deliberately absent from the hermetic PATH these tests build. That is
    the point of the "report state, don't crash" contract -- composition
    must still happen so the app is installable before its CLI is.
    """

    def test_bundle_conforms_to_schema(self) -> None:
        loaded = kind_install.load_bundle(_WAKE_BUNDLE_DIR)
        bundle = loaded["bundle"]
        assert set(bundle) >= {"id", "title", "description", "cli"}
        assert bundle["id"] == "persistentai-wake"
        assert bundle["cli"] == "persistentai"
        assert bundle["verify"]["cmd"] == ["persistentai", "trigger", "--help"]

    def test_skill_md_states_the_load_bearing_constraints(self) -> None:
        """The prose IS the deliverable -- it is injected verbatim and is the
        only thing a KOROS session will ever know about this rail. Each
        assertion below is a wrong assumption an agent would otherwise make.
        """
        skill_md = kind_install.load_bundle(_WAKE_BUNDLE_DIR)["skill_md"]

        # Both addressing axes, and the only valid pairings.
        assert "--to-machine primary-laptop" in skill_md
        assert "--to miles --to-machine h100" in skill_md
        # The rail is one-way: no reply, no return value, minutes of latency.
        assert "one-way" in skill_md
        assert "3–15 minutes" in skill_md
        # An envelope is a request, not a grant of scope.
        assert "Not an authorisation." in skill_md
        # The payload is a file, never argv.
        assert "--payload-file" in skill_md
        # Not available off primary-laptop.
        assert "only on primary-laptop" in skill_md
        # PERSISTENTAI_MACHINE is required and unset in a KOROS session --
        # omitting it makes the very first emit fail with an error whose
        # cause is not obvious from the message.
        assert "PERSISTENTAI_MACHINE=primary-laptop persistentai trigger emit" in skill_md
        assert "required and you must set it yourself" in skill_md

    def test_install_composes_wake_app_even_though_its_cli_is_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _no_kbu_on_path(tmp_path, monkeypatch)  # neither kbu NOR persistentai
        apps_dir = tmp_path / "kind-apps"

        r = _invoke("king", "install", "--app", "wake", "--apps-dir", str(apps_dir), "--json")
        assert r.exit_code == 0, r.output
        data = _json_one(r)
        assert data["id"] == "persistentai-wake"
        assert data["cli"] == "persistentai"
        assert data["cli_on_path"] is False
        assert data["changed"] is True

        registry = json.loads((apps_dir / "registry.json").read_text())
        assert registry["persistentai-wake"]["cli"] == "persistentai"

        context = (apps_dir / "CONTEXT.md").read_text()
        assert (
            "# [KIND App] Wake a Persistent Agent (Luna / Miles) "
            "(id: persistentai-wake)" in context
        )
        assert "persistentai trigger emit" in context

    def test_bare_install_ships_every_app_this_repo_offers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No --app means all of them. A per-app default would silently ship
        a subset, which is exactly how the second app would go unnoticed."""
        _fake_kbu_on_path(tmp_path, monkeypatch)
        apps_dir = tmp_path / "kind-apps"

        r = _invoke("king", "install", "--apps-dir", str(apps_dir), "--json")
        assert r.exit_code == 0, r.output
        data = json.loads(r.output)
        assert {entry["id"] for entry in data} == {
            "kbutillib-modeling",
            "persistentai-wake",
        }

        registry = json.loads((apps_dir / "registry.json").read_text())
        assert set(registry) == {"kbutillib-modeling", "persistentai-wake"}

        context = (apps_dir / "CONTEXT.md").read_text()
        assert "(id: kbutillib-modeling)" in context
        assert "(id: persistentai-wake)" in context

    def test_uninstalling_one_app_keeps_the_others_fragment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_kbu_on_path(tmp_path, monkeypatch)
        apps_dir = tmp_path / "kind-apps"
        _invoke("king", "install", "--apps-dir", str(apps_dir), "--json")

        r = _invoke("king", "uninstall", "--app", "wake", "--apps-dir", str(apps_dir), "--json")
        assert r.exit_code == 0, r.output
        assert _json_one(r)["removed"] is True

        registry = json.loads((apps_dir / "registry.json").read_text())
        assert set(registry) == {"kbutillib-modeling"}
        context = (apps_dir / "CONTEXT.md").read_text()
        assert "(id: persistentai-wake)" not in context
        assert "(id: kbutillib-modeling)" in context

    def test_status_worst_color_wins_across_apps(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A green modeling app must not mask the wake app's missing CLI."""
        _fake_kbu_on_path(tmp_path, monkeypatch)  # kbu present, persistentai not
        apps_dir = tmp_path / "kind-apps"
        _invoke("king", "install", "--apps-dir", str(apps_dir), "--json")
        monkeypatch.delenv("KING_CONTEXT", raising=False)

        r = _invoke("king", "status", "--app", "modeling", "--apps-dir", str(apps_dir), "--json")
        assert r.exit_code == 0  # green on its own

        r = _invoke("king", "status", "--apps-dir", str(apps_dir), "--json")
        assert r.exit_code == 1, r.output  # amber, because persistentai is absent
        colors = {e["id"]: e["color"] for e in json.loads(r.output)}
        assert colors == {"kbutillib-modeling": "green", "persistentai-wake": "amber"}


class TestRetiredKingAlias:
    """`kbu kind` was renamed to `kbu kind`; the old spelling must still work."""

    def test_king_alias_still_resolves(self):
        """`kbu kind --help` resolves to the kind group (cw-kind still calls it)."""
        from kbutillib.interfaces.cli import main

        result = CliRunner().invoke(main, ["king", "--help"])
        assert result.exit_code == 0, result.output
        assert "install" in result.output

    def test_king_alias_not_advertised(self):
        """The retired name is resolvable but never listed in --help."""
        from kbutillib.interfaces.cli import main

        result = CliRunner().invoke(main, ["--help"])
        assert result.exit_code == 0, result.output
        assert "\n  kind" in result.output
        assert "\n  king" not in result.output


# ── bundle `files` ───────────────────────────────────────────────────────────


class TestBundleFiles:
    """A bundle may declare ``files``, copied beside skill.md in its app dir.

    ``skill.md`` is composed into CONTEXT.md for EVERY session, so a bundle
    with a long reference splits it out and tells the session to read it on
    demand. The extra file has to land or that instruction dangles. Kept in
    step with AIAssistant's vendored copy of this contract, which is where
    the need showed up (its about-kind bundle).
    """

    def _bundle_with_files(self, root: Path) -> Path:
        bundle_dir = _write_fixture_bundle(root, "with-files", "With Files")
        bundle = json.loads((bundle_dir / "bundle.json").read_text())
        bundle["files"] = ["reference.md"]
        (bundle_dir / "bundle.json").write_text(json.dumps(bundle))
        (bundle_dir / "reference.md").write_text("THE REFERENCE BODY\n")
        return bundle_dir

    def test_declared_files_land_beside_skill_md(self, tmp_path):
        apps_dir = tmp_path / "apps"
        kind_install.install(self._bundle_with_files(tmp_path), apps_dir=apps_dir)
        assert (apps_dir / "with-files" / "reference.md").read_text() == "THE REFERENCE BODY\n"

    def test_declared_files_stay_out_of_context_md(self, tmp_path):
        apps_dir = tmp_path / "apps"
        kind_install.install(self._bundle_with_files(tmp_path), apps_dir=apps_dir)
        assert "THE REFERENCE BODY" not in (apps_dir / "CONTEXT.md").read_text()

    def test_reinstall_is_idempotent_on_unchanged_files(self, tmp_path):
        apps_dir = tmp_path / "apps"
        bundle_dir = self._bundle_with_files(tmp_path)
        kind_install.install(bundle_dir, apps_dir=apps_dir)
        second = kind_install.install(bundle_dir, apps_dir=apps_dir)
        assert second["changed"] is False

    def test_changed_reference_is_reported_as_changed(self, tmp_path):
        apps_dir = tmp_path / "apps"
        bundle_dir = self._bundle_with_files(tmp_path)
        kind_install.install(bundle_dir, apps_dir=apps_dir)
        (bundle_dir / "reference.md").write_text("EDITED BODY\n")
        second = kind_install.install(bundle_dir, apps_dir=apps_dir)
        assert second["changed"] is True
        assert (apps_dir / "with-files" / "reference.md").read_text() == "EDITED BODY\n"

    def test_files_names_cannot_escape_the_app_dir(self, tmp_path):
        apps_dir = tmp_path / "apps"
        bundle_dir = self._bundle_with_files(tmp_path)
        bundle = json.loads((bundle_dir / "bundle.json").read_text())
        bundle["files"] = ["../../escaped.md"]
        (bundle_dir / "bundle.json").write_text(json.dumps(bundle))
        (bundle_dir / "escaped.md").write_text("nope\n")

        kind_install.install(bundle_dir, apps_dir=apps_dir)

        assert not (apps_dir.parent / "escaped.md").exists()
        assert (apps_dir / "with-files" / "escaped.md").exists()
