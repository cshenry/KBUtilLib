"""Tests for KIND manifest registration (CAC I1, I4).

Covers: the manifest is a real file at ``$KING_PLUGINS_DIR`` (else
``~/kind-apps/plugins``) and NEVER at a ``$KING_STATE``-derived path; it carries
``type: app`` and ``contract_version: 1`` and a ``/health`` ready-probe; ``id``
and basename both derive from :data:`kbutillib.arc_context.APP_ID`; the symlink
farm links upstream manifests without clobbering real files and skips
``.disabled`` entries; ``king/plugins/`` is never touched; both KING_ROOT
layouts (``~/king-stack`` and flat ``$HOME``) resolve.

The KING-loader verification (loading the written manifest back through
``king_backend/plugins.py`` and asserting it appears in ``app_manifests()``) is
SKIPPED with an explicit reason when ``king_backend`` is not importable in this
environment (KIND is consume-only — it is never vendored to make the test pass).
"""

from __future__ import annotations

import importlib.util
import json

import pytest

from kbutillib.arc_context import APP_ID
from kbutillib.models_and_analyses import kind_manifest as km


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """Isolate HOME and the KING_* env vars so nothing leaks from the machine."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("KING_PLUGINS_DIR", raising=False)
    monkeypatch.delenv("KING_STATE", raising=False)
    monkeypatch.delenv("KING_ROOT", raising=False)
    return home


class TestManifestContent:
    def test_type_is_app(self):
        # plugins.py filters on type == "app"; a type-less manifest is silently
        # ignored, so this is mandatory.
        assert km.build_manifest()["type"] == "app"

    def test_contract_version_is_one(self):
        assert km.build_manifest()["contract_version"] == 1

    def test_ready_probe_points_at_health(self):
        probe = km.build_manifest()["launch"]["ready_probe"]
        assert probe["path"] == "/health"

    def test_launch_tokens_stay_literal(self):
        cmd = km.build_manifest()["launch"]["cmd"]
        assert "{port}" in cmd and "{proxy_path}" in cmd

    def test_id_and_basename_both_derive_from_app_id(self):
        # CAC I4: id, basename, console command and dist name are ONE string.
        manifest = km.build_manifest()
        assert manifest["id"] == APP_ID
        assert km.manifest_filename() == APP_ID + ".json"
        # And the launch command's program name is the same string.
        assert manifest["launch"]["cmd"][0] == APP_ID


class TestManifestPath:
    def test_uses_king_plugins_dir_when_set(self, clean_env, monkeypatch, tmp_path):
        union = tmp_path / "union"
        monkeypatch.setenv("KING_PLUGINS_DIR", str(union))
        assert km.resolve_manifest_dir() == union

    def test_defaults_to_kind_apps_plugins(self, clean_env):
        assert km.resolve_manifest_dir() == clean_env / "kind-apps" / "plugins"

    def test_never_derived_from_king_state(self, clean_env, monkeypatch):
        # KING_STATE set (and no KING_PLUGINS_DIR) must NOT influence the path —
        # the superseded formula doubled the path on the pod.
        monkeypatch.setenv("KING_STATE", str(clean_env / "somestate"))
        resolved = km.resolve_manifest_dir()
        assert "somestate" not in str(resolved)
        assert resolved == clean_env / "kind-apps" / "plugins"

    def test_manifest_written_as_real_file(self, clean_env, monkeypatch, tmp_path):
        union = tmp_path / "union"
        monkeypatch.setenv("KING_PLUGINS_DIR", str(union))
        path = km.write_manifest()
        assert path == union / (APP_ID + ".json")
        assert path.is_file()
        assert not path.is_symlink()
        data = json.loads(path.read_text())
        assert data["type"] == "app"
        assert data["id"] == APP_ID
        assert data["contract_version"] == 1

    def test_manifest_never_written_to_king_plugins(
        self, clean_env, monkeypatch, tmp_path
    ):
        # Build a laptop-layout king/plugins and assert registration never writes
        # our manifest there.
        upstream = clean_env / "king-stack" / "king" / "plugins"
        upstream.mkdir(parents=True)
        (upstream / "genepool.json").write_text("{}")
        union = tmp_path / "union"
        monkeypatch.setenv("KING_PLUGINS_DIR", str(union))

        km.register_manifest()

        assert not (upstream / (APP_ID + ".json")).exists()
        assert (union / (APP_ID + ".json")).is_file()


class TestKingRoot:
    def test_laptop_king_stack_layout(self, clean_env):
        (clean_env / "king-stack" / "king" / "plugins").mkdir(parents=True)
        assert km.resolve_king_root() == clean_env / "king-stack"

    def test_flat_home_pod_layout(self, clean_env):
        # No king-stack; king sits flat in $HOME (the pod, primary environment).
        (clean_env / "king" / "plugins").mkdir(parents=True)
        assert km.resolve_king_root() == clean_env

    def test_none_when_no_layout_resolves(self, clean_env):
        assert km.resolve_king_root() is None


class TestSymlinkFarm:
    def _upstream(self, clean_env):
        up = clean_env / "king-stack" / "king" / "plugins"
        up.mkdir(parents=True)
        return up

    def test_links_upstream_manifests(self, clean_env, monkeypatch, tmp_path):
        up = self._upstream(clean_env)
        (up / "genepool.json").write_text("{}")
        (up / "function-junction.json").write_text("{}")
        union = tmp_path / "union"
        union.mkdir()

        linked = km.refresh_plugin_union(plugins_dir=union, king_root=None)

        assert sorted(linked) == ["function-junction.json", "genepool.json"]
        assert (union / "genepool.json").is_symlink()
        assert (union / "function-junction.json").is_symlink()

    def test_does_not_clobber_real_files(self, clean_env, monkeypatch, tmp_path):
        up = self._upstream(clean_env)
        (up / "genepool.json").write_text("{}")
        union = tmp_path / "union"
        union.mkdir()
        # A real file already present in the union dir must survive.
        (union / "genepool.json").write_text("REAL")

        linked = km.refresh_plugin_union(plugins_dir=union, king_root=None)

        assert "genepool.json" not in linked
        assert not (union / "genepool.json").is_symlink()
        assert (union / "genepool.json").read_text() == "REAL"

    def test_skips_disabled_entries(self, clean_env, monkeypatch, tmp_path):
        up = self._upstream(clean_env)
        # A .disabled-suffixed manifest and a manifest with a .disabled sibling.
        (up / "off.json.disabled").write_text("{}")
        (up / "paused.json").write_text("{}")
        (up / "paused.json.disabled").write_text("")
        union = tmp_path / "union"
        union.mkdir()

        linked = km.refresh_plugin_union(plugins_dir=union, king_root=None)

        assert linked == []
        assert not (union / "off.json.disabled").exists()
        assert not (union / "paused.json").exists()

    def test_upstream_king_plugins_untouched(self, clean_env, monkeypatch, tmp_path):
        up = self._upstream(clean_env)
        (up / "genepool.json").write_text("{}")
        before = {p.name: p.read_text() for p in up.iterdir()}
        union = tmp_path / "union"
        union.mkdir()

        km.refresh_plugin_union(plugins_dir=union, king_root=None)

        after = {p.name: p.read_text() for p in up.iterdir()}
        assert after == before


class TestKingLoaderVerification:
    """Verify the written manifest through KIND's own loader when available."""

    def test_manifest_appears_in_app_manifests(
        self, clean_env, monkeypatch, tmp_path
    ):
        if importlib.util.find_spec("king_backend") is None:
            pytest.skip(
                "king_backend is not importable in this environment "
                "(ModuleNotFoundError: king_backend); KIND is consume-only and "
                "is never vendored to satisfy this check. The manifest-content, "
                "path and symlink-farm assertions above still run."
            )

        import king_backend.plugins as kb_plugins  # type: ignore[import]

        union = tmp_path / "union"
        monkeypatch.setenv("KING_PLUGINS_DIR", str(union))
        km.register_manifest()

        # Load through KIND's own loader rather than re-reading the file, so a
        # schema drift upstream is caught instead of mirrored (G25).
        manifests = kb_plugins.app_manifests()
        ids = {m.get("id") for m in manifests}
        assert APP_ID in ids
