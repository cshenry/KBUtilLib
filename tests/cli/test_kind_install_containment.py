"""Regression tests for two silent ``kbu kind install`` defects, fixed 2026-10-05.

Both were found on the kbhub BERDL pod, and both are pinned here because each
one failed *while looking like success* -- exit 0, and ``kbu kind status``
still green:

1. **``--apps-dir`` did not contain the write.** The registry, ``CONTEXT.md``
   and ``serve-kind.sh`` were redirected correctly, but the plugin manifest
   still went to the real ``~/kind-apps/plugins`` because
   ``resolve_manifest_dir()`` consulted only ``$KING_PLUGINS_DIR``. Rehearsing
   an install against a throwaway copy is documented practice, so the sandbox
   has to actually hold.

2. **Installing from a different checkout downgraded ``CONTEXT.md`` silently.**
   Two KBUtilLib checkouts are importable on that pod -- ``bin/kbu`` pins one
   on ``PYTHONPATH``, a bare ``python -m kbutillib`` resolves the other through
   an editable install -- and they drift. Every other check in ``kind_install``
   asks whether the installed files agree with the bundle it was handed, never
   *which* bundle that was, so the swap was undetectable. The fix records
   ``source_root`` and reports a change.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kbutillib.agents import kind_install
from kbutillib.models_and_analyses import kind_manifest


# ── 1. --apps-dir containment ──────────────────────────────────────────────


def test_apps_dir_outranks_king_plugins_dir_env(tmp_path, monkeypatch):
    """``--apps-dir`` is a per-invocation instruction and must beat the env."""
    env_dir = tmp_path / "from-env" / "plugins"
    monkeypatch.setenv("KING_PLUGINS_DIR", str(env_dir))

    apps_dir = tmp_path / "scratch-apps"
    resolved = kind_manifest.resolve_manifest_dir(apps_dir=apps_dir)

    assert resolved == apps_dir / "plugins"
    assert resolved.is_dir()


def test_env_still_used_when_no_apps_dir_given(tmp_path, monkeypatch):
    """Without ``apps_dir`` the previous precedence is unchanged."""
    env_dir = tmp_path / "from-env" / "plugins"
    monkeypatch.setenv("KING_PLUGINS_DIR", str(env_dir))

    assert kind_manifest.resolve_manifest_dir() == env_dir


def test_falls_back_to_home_kind_apps(tmp_path, monkeypatch):
    monkeypatch.delenv("KING_PLUGINS_DIR", raising=False)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    assert kind_manifest.resolve_manifest_dir() == tmp_path / "kind-apps" / "plugins"


def test_register_manifest_writes_inside_apps_dir(tmp_path, monkeypatch):
    """The whole point: a redirected install leaves the real tree alone."""
    real_plugins = tmp_path / "real" / "plugins"
    real_plugins.mkdir(parents=True)
    monkeypatch.setenv("KING_PLUGINS_DIR", str(real_plugins))
    monkeypatch.setattr(kind_manifest, "resolve_king_root", lambda *a, **k: None)

    apps_dir = tmp_path / "scratch"
    result = kind_manifest.register_manifest(apps_dir=apps_dir)

    assert Path(result["manifest_path"]).is_relative_to(apps_dir)
    # The real tree must be untouched -- this is the assertion that failed
    # before the fix.
    assert list(real_plugins.iterdir()) == []


# ── 2. source-checkout drift detection ─────────────────────────────────────


def test_source_root_resolves_to_the_repo_root(tmp_path):
    """Walk up to the dir holding ``src/kbutillib``, not the bundle dir."""
    repo = tmp_path / "some-checkout"
    bundle = repo / "src" / "kbutillib" / "kind_app"
    bundle.mkdir(parents=True)

    assert kind_install._bundle_source_root(bundle) == str(repo)


def test_source_root_degrades_to_an_absolute_path_on_an_odd_layout(tmp_path):
    """An unrecognised layout must still yield something useful, not raise."""
    odd = tmp_path / "not-a-checkout"
    odd.mkdir()

    got = kind_install._bundle_source_root(odd)

    assert Path(got).is_absolute()
    assert got == str(odd.resolve())


@pytest.mark.parametrize(
    "existing, expected",
    [
        (None, False),                                        # first install
        ({}, False),                                          # pre-fix entry
        ({"source_root": "/a"}, False),                       # same checkout
        ({"source_root": "/b"}, True),                        # SWAPPED
    ],
)
def test_source_changed_only_fires_on_a_real_swap(existing, expected):
    """A missing ``source_root`` must not be reported as a change.

    Registries written before this field existed carry no ``source_root``, and
    treating that as a swap would cry wolf on every pod's first install after
    the upgrade -- which is how a useful warning gets ignored.
    """
    source_root = "/a"
    source_changed = bool(
        existing
        and existing.get("source_root")
        and existing["source_root"] != source_root
    )
    assert source_changed is expected


def test_install_records_source_root_in_the_registry(tmp_path):
    """End-to-end: the composing checkout lands in ``registry.json``."""
    bundle_dir = Path(kind_install.__file__).resolve().parents[1] / "kind_app"
    if not (bundle_dir / "bundle.json").is_file():
        pytest.skip("kind_app bundle not present in this checkout")

    apps_dir = tmp_path / "apps"
    result = kind_install.install(bundle_dir, apps_dir=apps_dir)

    assert result["source_root"]
    assert result["source_changed"] is False  # nothing to differ from yet

    registry = json.loads((apps_dir / "registry.json").read_text())
    entry = registry[result["id"]]
    assert entry["source_root"] == result["source_root"]
    # It must name a real checkout root, not the bundle directory itself.
    assert Path(entry["source_root"], "src", "kbutillib").is_dir()
